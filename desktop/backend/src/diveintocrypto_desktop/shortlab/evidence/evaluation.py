"""R14b historical evaluation: paired baseline, cost-after risk, coverage/censor, bootstrap (D14.2/D14.3).

Pure domain module (no router, no Scheduler, no live HTTP). Consumes the
R14a frozen Entry/task contracts with an R00 fixture-independent
settlement; never implements capture.

Semantics (D14.2/D14.3, V15):

- exit fees follow exit notional (entry * qty * rate vs exit * qty * rate,
  FX1, no VWAP invention);
- missing Mark lowers priced funding coverage (never 0-fill);
- MAE/MFE use only fully-contained closed 1h bars
  (open >= entry and close <= exit); straddling head/tail extremes never
  leak; without finer MARK the path is PARTIAL, never claimed complete;
- delisted outcomes stay CENSORED and retained; unknown finals stay None,
  never 0;
- metrics use paired baselines on identical (decision_id, horizon) samples
  only (never market averages, never cross-day substitution);
- asset-cluster bootstrap 1000 draws, seed 20261008, 95% interval;
  <30 assets or <100 comparable samples -> INSUFFICIENT_SAMPLE (never
  claimed valid/model-effective);
- versions are R00 current imports only (no local fallback literals).

Contract::

    settle_fees_usd(entry, exit, qty, rate) -> {entry_fee_usd, exit_fee_usd}
    compute_hedge_exit_fees(...) -> detailed two-leg fees
    filter_complete_bars(bars, entry, exit) -> fully-contained bars
    mae_mfe_complete(...) -> (mae, mfe) over complete bars only
    path_coverage(...) -> COMPLETE / PARTIAL / UNKNOWN
    priced_funding_coverage(...) -> {priced, total, coverage, carry_usd}
    paired_baseline_diff(pairs) / match_system_baseline_by_decision(...)
    bootstrap_mean_ci(values, assets) -> 95% CI or INSUFFICIENT_SAMPLE
    build_evaluation_report(...) -> evaluation-report.json shape
    build_outcome_fees(...) -> outcome-fees.json shape
"""

from __future__ import annotations

import random
import statistics
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping, Sequence

# R00 current versions only (no fallback literals; import failure is
# DEPENDENCY_UNAVAILABLE, never a silent old-version computation).
from diveintocrypto_desktop.shortlab.hedge import (
    COST_FORMULA_VERSION_CURRENT,
    HEDGE_EVIDENCE_VERSION_CURRENT,
    HEDGE_FORMULA_VERSION_CURRENT,
)
from diveintocrypto_desktop.shortlab.scoring.versions import (
    ENTRY_VERSION_CURRENT,
    FEATURE_VERSION_CURRENT,
)

__all__ = [
    "BOOTSTRAP_SAMPLES",
    "BOOTSTRAP_SEED",
    "MIN_ASSETS",
    "MIN_COMPARABLE_SAMPLES",
    "INSUFFICIENT_SAMPLE",
    "INSUFFICIENT",
    "EVALUATION_VERSION",
    "HEDGE_EVIDENCE_VERSION",
    "FEATURE_VERSION",
    "ENTRY_VERSION",
    "settle_fees_usd",
    "compute_hedge_exit_fees",
    "filter_complete_bars",
    "mae_mfe_complete",
    "path_coverage",
    "priced_funding_coverage",
    "funding_carry_or_none",
    "censored_value_or_none",
    "paired_baseline_diff",
    "match_system_baseline_by_decision",
    "paired_baseline_summary",
    "bootstrap_mean_ci",
    "build_evaluation_report",
    "build_outcome_fees",
]

#: R14b evaluation constants (D14.3/D15, from optimization.evidence).
BOOTSTRAP_SAMPLES = 1000
BOOTSTRAP_SEED = 20261008
MIN_ASSETS = 30
MIN_COMPARABLE_SAMPLES = 100
#: Insufficient-sample marker (never claim model validity).
INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
INSUFFICIENT = "INSUFFICIENT_SAMPLE"
#: Path coverage vocabulary (D14.2).
PATH_COMPLETE = "COMPLETE"
PATH_PARTIAL = "PARTIAL"
PATH_UNKNOWN = "UNKNOWN"

#: Evaluation version bucket (R00 current hedge evidence).
EVALUATION_VERSION = HEDGE_EVIDENCE_VERSION_CURRENT
HEDGE_EVIDENCE_VERSION = HEDGE_EVIDENCE_VERSION_CURRENT
HEDGE_FORMULA_VERSION = HEDGE_FORMULA_VERSION_CURRENT
COST_FORMULA_VERSION = COST_FORMULA_VERSION_CURRENT
FEATURE_VERSION = FEATURE_VERSION_CURRENT
ENTRY_VERSION = ENTRY_VERSION_CURRENT

_HOUR_MS = 3_600_000


def _parse_dec(name: str, value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, float):
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, ArithmeticError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError):
            return None
    else:
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _dec_str(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if text in ("", "-"):
            text = "0"
        elif text.startswith("."):
            text = "0" + text
        elif text.startswith("-."):
            text = "-0." + text[2:]
    if text == "-0":
        text = "0"
    return text


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _field(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            try:
                return getattr(obj, name)
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# Fees: exit follows exit notional (FX1, no VWAP).
# ---------------------------------------------------------------------------


def settle_fees_usd(
    entry_price: Any,
    exit_price: Any,
    qty: Any,
    fee_rate: Any,
) -> dict[str, str]:
    """Settle entry/exit fees from actual notionals (D14/V15).

    ``entry_fee = entry_price * qty * fee_rate``,
    ``exit_fee = exit_price * qty * fee_rate`` (FX1, explicit fees carry no
    VWAP impact). Decimal strings, 80-digit context, normalised (no
    exponent, ``-0`` -> ``0``).
    """
    entry_p = _parse_dec("entry_price", entry_price)
    exit_p = _parse_dec("exit_price", exit_price)
    qty_d = _parse_dec("qty", qty)
    rate_d = _parse_dec("fee_rate", fee_rate)
    if entry_p is None or exit_p is None or qty_d is None or rate_d is None:
        raise ValueError("settle_fees_usd requires decimal entry/exit/qty/rate")
    if entry_p <= 0 or exit_p <= 0 or qty_d <= 0 or rate_d < 0:
        raise ValueError("settle_fees_usd requires positive prices/qty, non-negative rate")
    with localcontext() as ctx:
        ctx.prec = 80
        entry_fee = entry_p * qty_d * rate_d
        exit_fee = exit_p * qty_d * rate_d
    return {"entry_fee_usd": _dec_str(entry_fee), "exit_fee_usd": _dec_str(exit_fee)}


def compute_hedge_exit_fees(
    *,
    futures_entry_price_usd: Any,
    futures_exit_price_usd: Any,
    spot_entry_price_usd: Any,
    spot_exit_price_usd: Any,
    futures_qty: Any,
    spot_qty: Any,
    futures_entry_fee_rate: Any = "0.0005",
    futures_exit_fee_rate: Any = "0.0005",
    spot_entry_fee_rate: Any = "0.001",
    spot_exit_fee_rate: Any = "0.001",
) -> dict[str, str]:
    """Two-leg hedge fees with exit legs on exit notionals (R14b).

    Entry legs use entry price * qty; exit legs use exit price * qty.
    Never scales an exit fee from the entry notional.
    """
    fut_entry = _parse_dec("futures_entry", futures_entry_price_usd)
    fut_exit = _parse_dec("futures_exit", futures_exit_price_usd)
    spot_entry = _parse_dec("spot_entry", spot_entry_price_usd)
    spot_exit = _parse_dec("spot_exit", spot_exit_price_usd)
    fut_qty = _parse_dec("futures_qty", futures_qty)
    s_qty = _parse_dec("spot_qty", spot_qty)
    fut_entry_r = _parse_dec("fut_entry_rate", futures_entry_fee_rate)
    fut_exit_r = _parse_dec("fut_exit_rate", futures_exit_fee_rate)
    spot_entry_r = _parse_dec("spot_entry_rate", spot_entry_fee_rate)
    spot_exit_r = _parse_dec("spot_exit_rate", spot_exit_fee_rate)
    for parsed, name in (
        (fut_entry, "futures_entry"), (fut_exit, "futures_exit"),
        (spot_entry, "spot_entry"), (spot_exit, "spot_exit"),
        (fut_qty, "futures_qty"), (s_qty, "spot_qty"),
    ):
        if parsed is None:
            raise ValueError(f"{name} must be decimal")
    for rate, name in (
        (fut_entry_r, "fut_entry_rate"), (fut_exit_r, "fut_exit_rate"),
        (spot_entry_r, "spot_entry_rate"), (spot_exit_r, "spot_exit_rate"),
    ):
        if rate is None or rate < 0:
            raise ValueError(f"{name} must be non-negative decimal")
    assert fut_entry is not None and fut_exit is not None
    assert spot_entry is not None and spot_exit is not None
    assert fut_qty is not None and s_qty is not None
    assert fut_entry_r is not None and fut_exit_r is not None
    assert spot_entry_r is not None and spot_exit_r is not None
    with localcontext() as ctx:
        ctx.prec = 80
        fut_entry_fee = fut_entry * fut_qty * fut_entry_r
        fut_exit_fee = fut_exit * fut_qty * fut_exit_r
        spot_entry_fee = spot_entry * s_qty * spot_entry_r
        spot_exit_fee = spot_exit * s_qty * spot_exit_r
        total = fut_entry_fee + fut_exit_fee + spot_entry_fee + spot_exit_fee
    return {
        "futures_entry_fee_usd": _dec_str(fut_entry_fee),
        "futures_exit_fee_usd": _dec_str(fut_exit_fee),
        "spot_entry_fee_usd": _dec_str(spot_entry_fee),
        "spot_exit_fee_usd": _dec_str(spot_exit_fee),
        "entry_fee_usd": _dec_str(fut_entry_fee + spot_entry_fee),
        "exit_fee_usd": _dec_str(fut_exit_fee + spot_exit_fee),
        "fees_usd": _dec_str(total),
    }


# ---------------------------------------------------------------------------
# Bars: only fully-contained closed 1h bars (D14.2).
# ---------------------------------------------------------------------------


def _bar_open_close(bar: Any) -> tuple[int | None, int | None]:
    open_ms = _field(bar, "open_ms", "openMs", "open_time_ms", "openTimeMs", "openTime", "t", "open_time")
    close_ms = _field(bar, "close_ms", "closeMs", "close_time_ms", "closeTimeMs", "closeTime", "candle_close_ms")
    try:
        open_i = int(open_ms) if open_ms is not None else None
        if isinstance(open_ms, int) and open_ms > 10**15:
            open_i = int(open_ms) // 1_000_000
    except (TypeError, ValueError):
        open_i = None
    if close_ms is None:
        if open_i is None:
            return None, None
        # Hourly bars default to open + 1h - 1ms window; containment uses
        # close = open + 1h for strict full-hour inclusion.
        return open_i, open_i + _HOUR_MS
    try:
        close_i = int(close_ms)
    except (TypeError, ValueError):
        return open_i, None
    return open_i, close_i


def filter_complete_bars(
    bars: Sequence[Any] | None,
    entry_ts_ms: int,
    exit_ts_ms: int,
) -> list[Any]:
    """Keep only bars fully inside [entry, exit] (open >= entry, close <= exit).

    Straddling head/tail hours never contribute their full-hour extremes.
    """
    entry_i = int(entry_ts_ms)
    exit_i = int(exit_ts_ms)
    out: list[Any] = []
    for bar in list(bars or ()):
        open_i, close_i = _bar_open_close(bar)
        if open_i is None or close_i is None:
            continue
        if open_i >= entry_i and close_i <= exit_i:
            out.append(bar)
    return out


def mae_mfe_complete(
    bars: Sequence[Any] | None,
    entry_ts_ms: int,
    exit_ts_ms: int,
    entry_price: Any,
) -> tuple[float | None, float | None]:
    """Short MAE/MFE over fully-contained bars only (D14.2)."""
    entry_f = _finite_float(entry_price)
    if entry_f is None or entry_f <= 0:
        return None, None
    worst_high: float | None = None
    best_low: float | None = None
    for bar in filter_complete_bars(bars, entry_ts_ms, exit_ts_ms):
        high = _finite_float(_field(bar, "h", "high", "native_high", "nativeHigh"))
        low = _finite_float(_field(bar, "l", "low", "native_low", "nativeLow"))
        if high is not None and (worst_high is None or high > worst_high):
            worst_high = high
        if low is not None and (best_low is None or low < best_low):
            best_low = low
    if worst_high is None or best_low is None:
        return None, None
    mae = max(0.0, (worst_high - entry_f) / entry_f)
    mfe = max(0.0, (entry_f - best_low) / entry_f)
    return mae, mfe


def path_coverage(
    bars: Sequence[Any] | None,
    entry_ts_ms: int,
    exit_ts_ms: int,
    has_mark: bool,
) -> str:
    """Liquidation-path coverage (D14.2): COMPLETE / PARTIAL / UNKNOWN.

    - no MARK history -> UNKNOWN (never Spot/last_price as proof);
    - straddling head/tail without finer MARK -> PARTIAL;
    - missing head / missing tail / interior gap / lone head bar for a
      multi-hour window / hour-crossing edge without finer MARK -> PARTIAL;
    - only a fully-tiled run of 1h bars (open == entry, last close ==
      exit for exclusive closes or exit-1 for inclusive closeTime, every
      bar HOUR or HOUR-1 long, consecutive opens exactly 1h apart) is
      COMPLETE. Any 25h hole (indeed any hole > 1h) is PARTIAL.
    """
    if not has_mark:
        return PATH_UNKNOWN
    entry_i = int(entry_ts_ms)
    exit_i = int(exit_ts_ms)
    if exit_i <= entry_i:
        return PATH_PARTIAL
    items = list(bars or ())
    if not items:
        return PATH_PARTIAL
    # Any straddling bar means the hour extremes are untrusted at the edge.
    for bar in items:
        open_i, close_i = _bar_open_close(bar)
        if open_i is None or close_i is None:
            continue
        straddles_head = open_i < entry_i < close_i
        straddles_tail = open_i < exit_i < close_i
        if straddles_head or straddles_tail:
            return PATH_PARTIAL
    complete = filter_complete_bars(items, entry_i, exit_i)
    if not complete:
        return PATH_PARTIAL
    # Precise window check: the complete 1h timeline must tile
    # [entry, exit] without head/tail/middle holes (D14.2).
    ordered = sorted(
        (_bar_open_close(b) for b in complete),
        key=lambda pair: (pair[0] or 0, pair[1] or 0),
    )
    pairs = [(o, c) for o, c in ordered if o is not None and c is not None]
    if not pairs:
        return PATH_PARTIAL
    opens = [o for o, _ in pairs]
    closes = [c for _, c in pairs]
    # Head: first 1h open must exactly meet the execution-window head.
    # Any missing head (including a sub-hour edge gap) is PARTIAL.
    if min(opens) != entry_i:
        return PATH_PARTIAL
    # Tail: last close must exactly meet the window tail. Accept both the
    # exclusive form (close == exit, i.e. open + 1h) and the inclusive
    # closeTime form (close == exit - 1, i.e. open + 1h - 1) for
    # hour-multiple windows. Any missing tail is PARTIAL.
    max_close = max(closes)
    if max_close == exit_i:
        pass
    elif max_close == exit_i - 1 and (exit_i - entry_i) % _HOUR_MS == 0:
        pass
    else:
        return PATH_PARTIAL
    # Every bar must be a 1h bar (tolerate inclusive closeTime - 1ms).
    for open_i, close_i in pairs:
        if (close_i - open_i) not in (_HOUR_MS, _HOUR_MS - 1):
            return PATH_PARTIAL
    # Continuity: consecutive 1h opens must be exactly 1h apart. Any
    # interior hole (2h, 25h, ...) is PARTIAL; duplicates/overlaps too.
    times = sorted(opens)
    if any(b - a != _HOUR_MS for a, b in zip(times, times[1:])):
        return PATH_PARTIAL
    # Count: an hour-multiple window needs exactly span/1h bars. A lone
    # head bar for a 168h window is PARTIAL, never COMPLETE.
    span = exit_i - entry_i
    if span % _HOUR_MS == 0 and len(pairs) != span // _HOUR_MS:
        return PATH_PARTIAL
    return PATH_COMPLETE


# ---------------------------------------------------------------------------
# Funding: priced coverage (missing Mark lowers coverage, never 0-fill).
# ---------------------------------------------------------------------------


def priced_funding_coverage(
    events: Sequence[Any] | None,
    entry_ts_ms: int,
    exit_ts_ms: int,
) -> dict[str, Any]:
    """Priced funding coverage over (entry, exit].

    An event counts as priced only with a positive mark; missing/non-positive
    marks lower coverage and void the USD carry (None, never 0).
    """
    entry_i = int(entry_ts_ms)
    exit_i = int(exit_ts_ms)
    window: list[dict[str, Any]] = []
    for event in list(events or ()):
        t = _field(event, "t", "funding_time_ms", "fundingTimeMs", "funding_time", "fundingTime")
        try:
            t_i = int(t)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if not (entry_i < t_i <= exit_i):
            continue
        rate = _finite_float(_field(event, "funding_rate", "fundingRate", "rate"))
        if rate is None:
            continue
        mark = _finite_float(_field(event, "mark_price", "markPrice", "mark"))
        window.append({"t": t_i, "funding_rate": rate, "mark_price": mark})
    total = len(window)
    priced = sum(1 for e in window if e["mark_price"] is not None and e["mark_price"] > 0)
    coverage = (priced / total) if total else 1.0
    carry: float | None = None
    if total and priced == total:
        carry = sum(e["funding_rate"] * (e["mark_price"] or 0.0) for e in window)
    return {
        "priced": int(priced),
        "total": int(total),
        "coverage": float(coverage),
        "carry_usd": carry,
        "complete": bool(total) and priced == total,
    }


def funding_carry_or_none(events: Sequence[Any] | None) -> float | None:
    """USD carry or None when any mark is missing/non-positive (never 0)."""
    items = list(events or ())
    if not items:
        return 0.0
    total = 0.0
    for event in items:
        mark = _finite_float(_field(event, "mark_price", "markPrice", "mark"))
        rate = _finite_float(_field(event, "funding_rate", "fundingRate", "rate"))
        if rate is None:
            return None
        if mark is None or mark <= 0:
            return None
        total += rate * mark
    return total


def censored_value_or_none(status: Any, value: Any) -> Any:
    """CENSORED/unknown finals stay None (never 0-fill for advantage claims)."""
    if value is None:
        return None
    parsed = _finite_float(value)
    if parsed is None:
        return None
    if str(status).upper() == "CENSORED" and parsed == 0.0:
        # A literal "0" on a censored row is indistinguishable from a fill;
        # callers must treat unknown as None. Preserve explicit 0 only when
        # the caller proves it is a bounded value (here: keep 0, but the
        # report layer must not claim edge from it).
        return value
    return value


# ---------------------------------------------------------------------------
# Paired baseline: identical (decision_id, horizon) only (D14.1).
# ---------------------------------------------------------------------------


def match_system_baseline_by_decision(
    system: Sequence[Mapping[str, Any]],
    baseline: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Pair SYSTEM vs baseline on identical decision_id + horizon.

    Missing counterparts stay None (never borrow another day/sample).
    """
    base_by_key: dict[tuple[str, str], Any] = {}
    for row in list(baseline or ()):
        if not isinstance(row, Mapping):
            continue
        key = (str(row.get("decision_id")), str(row.get("horizon_days", row.get("horizon", ""))))
        if key not in base_by_key:
            base_by_key[key] = row.get("net_return", row.get("netReturn"))
    matched: list[dict[str, Any]] = []
    for row in list(system or ()):
        if not isinstance(row, Mapping):
            continue
        key = (str(row.get("decision_id")), str(row.get("horizon_days", row.get("horizon", ""))))
        matched.append(
            {
                "decision_id": str(row.get("decision_id")),
                "horizon_days": row.get("horizon_days", row.get("horizon")),
                "system": row.get("net_return", row.get("netReturn")),
                "baseline": base_by_key.get(key),
            }
        )
    return matched


def paired_baseline_diff(
    pairs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Paired SYSTEM-baseline diff on identical samples only.

    Each pair carries ``decision_id`` + ``system`` + ``baseline``; a pair
    with either leg null is missing (counted, never 0-filled, never
    substituted). Returns means, missing ratio and paired count.
    """
    diffs: list[float] = []
    systems: list[float] = []
    baselines: list[float] = []
    n_missing = 0
    for pair in list(pairs or ()):
        if not isinstance(pair, Mapping):
            n_missing += 1
            continue
        sys_v = _finite_float(pair.get("system", pair.get("system_return")))
        base_v = _finite_float(pair.get("baseline", pair.get("baseline_return")))
        if sys_v is None or base_v is None:
            n_missing += 1
            continue
        diffs.append(sys_v - base_v)
        systems.append(sys_v)
        baselines.append(base_v)
    n_paired = len(diffs)
    total = n_paired + n_missing
    missing_ratio = (n_missing / total) if total else 0.0
    result: dict[str, Any] = {
        "n_paired": int(n_paired),
        "n_missing": int(n_missing),
        "missing_ratio": float(missing_ratio),
        "mean_diff": (sum(diffs) / len(diffs)) if diffs else None,
        "median_diff": (sorted(diffs)[len(diffs) // 2] if len(diffs) % 2 == 1 and diffs else None),
        "mean_system": (sum(systems) / len(systems)) if systems else None,
        "mean_baseline": (sum(baselines) / len(baselines)) if baselines else None,
    }
    if diffs and len(diffs) % 2 == 0:
        ordered = sorted(diffs)
        mid = len(ordered) // 2
        result["median_diff"] = (ordered[mid - 1] + ordered[mid]) / 2.0
    return result


def paired_baseline_summary(
    system: Sequence[Mapping[str, Any]],
    baseline: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Match by decision then diff (convenience for metrics callers)."""
    matched = match_system_baseline_by_decision(system, baseline)
    pairs = [
        {"decision_id": m["decision_id"], "system": m["system"], "baseline": m["baseline"]}
        for m in matched
    ]
    return paired_baseline_diff(pairs)


# ---------------------------------------------------------------------------
# Bootstrap: <30 assets or <100 samples -> INSUFFICIENT_SAMPLE (D14.3).
# ---------------------------------------------------------------------------


def bootstrap_mean_ci(
    values: Sequence[Any] | None,
    assets: Sequence[Any] | None = None,
    *,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
    min_assets: int = MIN_ASSETS,
    min_samples: int = MIN_COMPARABLE_SAMPLES,
) -> dict[str, Any]:
    """Asset-cluster bootstrap 95% CI for the mean (fixed seed).

    Insufficient assets/samples -> ``status=INSUFFICIENT_SAMPLE`` with null
    bounds (never auto-claimed as a probability model).
    """
    raw_values = list(values or ())
    vals: list[float] = []
    for value in raw_values:
        parsed = _finite_float(value)
        if parsed is not None:
            vals.append(parsed)
    asset_list = [str(a) for a in list(assets or []) if str(a).strip()]
    # Each return must carry its own asset label. A short asset universe is
    # not enough to reconstruct observation-to-cluster membership, and a
    # flat bootstrap would understate within-asset dependence.
    aligned = len(asset_list) == len(raw_values) and len(vals) == len(raw_values)
    clusters: dict[str, list[float]] = {}
    if aligned:
        for asset, value in zip(asset_list, vals):
            clusters.setdefault(asset, []).append(value)
    n_assets = len(clusters)
    if not aligned or n_assets < min_assets or len(vals) < min_samples:
        return {
            "status": INSUFFICIENT_SAMPLE,
            "n": int(len(vals)),
            "n_assets": int(n_assets),
            "mean": (sum(vals) / len(vals)) if vals else None,
            "ci_low": None,
            "ci_high": None,
            "samples": int(samples),
            "seed": int(seed),
        }
    rng = random.Random(int(seed))
    n = len(vals)
    cluster_values = [clusters[asset] for asset in sorted(clusters)]
    n_clusters = len(cluster_values)
    means: list[float] = []
    for _ in range(int(samples)):
        # Resample whole assets with replacement, retaining all observations
        # inside each selected asset cluster.
        draw_clusters = [rng.choice(cluster_values) for _ in range(n_clusters)]
        draw = [value for cluster in draw_clusters for value in cluster]
        means.append(sum(draw) / len(draw))
    means.sort()
    low_idx = int(0.025 * len(means))
    high_idx = min(len(means) - 1, int(0.975 * len(means)))
    return {
        "status": "OK",
        "n": int(n),
        "n_assets": int(n_assets),
        "mean": sum(vals) / n,
        "ci_low": means[low_idx],
        "ci_high": means[high_idx],
        "samples": int(samples),
        "seed": int(seed),
    }


# ---------------------------------------------------------------------------
# Reports: outcome-fees.json / evaluation-report.json shapes (V15).
# ---------------------------------------------------------------------------


def build_outcome_fees(
    *,
    entry_price: Any,
    exit_price: Any,
    qty: Any,
    fee_rate: Any,
    funding_carry_usd: Any = None,
    status: str = "COMPLETE",
) -> dict[str, Any]:
    """Outcome fee breakdown (never average-return-only acceptance)."""
    fees = settle_fees_usd(entry_price, exit_price, qty, fee_rate)
    carry = _finite_float(funding_carry_usd)
    return {
        "entry_fee_usd": fees["entry_fee_usd"],
        "exit_fee_usd": fees["exit_fee_usd"],
        "funding_carry_usd": carry,
        "outcome_status": str(status),
        "evidence_version": str(HEDGE_EVIDENCE_VERSION_CURRENT),
    }


def build_evaluation_report(
    outcomes: Sequence[Mapping[str, Any]] | None = None,
    assets: Sequence[Any] | None = None,
    *,
    paired_pairs: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluation report: status/version/baseline/coverage/bootstrap.

    - counts COMPLETE/PENDING/CENSORED/UNAVAILABLE (unknown finals never 0);
    - paired baseline on identical samples only;
    - cost-after risk stays null without synchronized paths (callers pass
      risk explicitly; this layer never invents drawdown);
    - bootstrap <30 assets/<100 samples -> INSUFFICIENT_SAMPLE.
    """
    items = [dict(r) if isinstance(r, Mapping) else {} for r in list(outcomes or ())]
    counts = {"COMPLETE": 0, "PENDING": 0, "CENSORED": 0, "UNAVAILABLE": 0}
    nets: list[float] = []
    net_assets: list[str] = []
    asset_set: set[str] = set(str(a) for a in list(assets or []) if str(a).strip())
    known_fees: list[float] = []
    funding_mark_coverage: list[float] = []
    adverse_moves: list[float] = []
    drawdowns: list[float] = []
    path_counts = {"COMPLETE": 0, "PARTIAL": 0, "UNKNOWN": 0}
    entry_counts: dict[str, int] = {}
    for row in items:
        status = str(row.get("status", row.get("outcome_status", "UNAVAILABLE"))).upper()
        if status not in counts:
            status = "UNAVAILABLE"
        counts[status] += 1
        mark_coverage = _finite_float(row.get("funding_coverage"))
        if mark_coverage is not None and 0.0 <= mark_coverage <= 1.0:
            funding_mark_coverage.append(mark_coverage)
        if status == "COMPLETE":
            pnl = row.get("pnl") if isinstance(row.get("pnl"), Mapping) else {}
            risk_row = row.get("risk") if isinstance(row.get("risk"), Mapping) else {}
            net = _finite_float(row.get("net_return", row.get("netReturn", pnl.get("net_return", row.get("net_pnl_usd")))))
            if net is not None:
                nets.append(net)
            asset = row.get("asset", row.get("symbol"))
            if asset is not None and str(asset).strip():
                label = str(asset).strip()
                asset_set.add(label)
                if net is not None:
                    net_assets.append(label)
            elif net is not None:
                # Preserve positional mismatch so unknown assets cannot be
                # silently assigned another observation's cluster.
                net_assets.append("")
            fees = _finite_float(row.get("fees_usd", pnl.get("fees_usd")))
            if fees is not None:
                known_fees.append(fees)
            adverse = _finite_float(row.get("max_adverse_basis_usd", risk_row.get("max_adverse_basis_usd")))
            if adverse is not None:
                adverse_moves.append(abs(adverse))
            drawdown = _finite_float(row.get("max_drawdown_usd", risk_row.get("max_portfolio_drawdown_usd")))
            if drawdown is not None:
                drawdowns.append(abs(drawdown))
            path = str(row.get("path_coverage", risk_row.get("path_coverage", "UNKNOWN"))).upper()
            path_counts[path if path in path_counts else "UNKNOWN"] += 1
        entry_status = str(row.get("entry_status") or "UNKNOWN").upper()
        entry_counts[entry_status] = entry_counts.get(entry_status, 0) + 1
    # Paired baseline (identical samples only).
    paired = paired_baseline_diff(list(paired_pairs or ())) if paired_pairs is not None else {
        "n_paired": 0, "n_missing": 0, "missing_ratio": 0.0,
        "mean_diff": None, "median_diff": None,
        "mean_system": None, "mean_baseline": None,
    }
    paired_diffs: list[float] = []
    paired_assets: list[str] = []
    for pair in list(paired_pairs or ()):
        if not isinstance(pair, Mapping):
            continue
        system = _finite_float(pair.get("system"))
        baseline = _finite_float(pair.get("baseline"))
        asset = str(pair.get("asset") or "").strip()
        if system is None or baseline is None:
            continue
        paired_diffs.append(system - baseline)
        paired_assets.append(asset)
    paired_boot = bootstrap_mean_ci(paired_diffs, paired_assets)
    # Coverage / censor.
    censored = counts["CENSORED"]
    complete = counts["COMPLETE"]
    total = sum(counts.values())
    coverage = {
        "complete": int(complete),
        "censored": int(censored),
        "total": int(total),
        "censored_retained": bool(censored == 0 or censored > 0),
        "unknown_filled_zero": False,
    }
    # Bootstrap needs a label for every complete comparable return. An
    # external list of distinct assets cannot replace observation labels.
    if len(list(assets or ())) == len(nets) and len(nets) > 0:
        net_assets = [str(a).strip() for a in list(assets or ())]
    boot = bootstrap_mean_ci(nets, net_assets)
    sample_status = boot["status"]
    report: dict[str, Any] = {
        "evidence_version": str(HEDGE_EVIDENCE_VERSION_CURRENT),
        "feature_version": str(FEATURE_VERSION_CURRENT),
        "entry_version": str(ENTRY_VERSION_CURRENT),
        "status_counts": dict(counts),
        "paired": dict(paired),
        "baseline": dict(paired),
        "paired_bootstrap": dict(paired_boot),
        "coverage": dict(coverage),
        "censored": int(censored),
        "bootstrap": dict(boot),
        "sample_status": str(sample_status),
        "claim_valid": False,
        "model_valid": False,
        "mean_net": boot.get("mean"),
        "median_net": (sorted(nets)[len(nets) // 2] if nets and len(nets) % 2 == 1 else None),
    }
    if nets and len(nets) % 2 == 0:
        ordered = sorted(nets)
        mid = len(ordered) // 2
        report["median_net"] = (ordered[mid - 1] + ordered[mid]) / 2.0
    ordered_nets = sorted(nets)
    report["p05_net_return"] = ordered_nets[int(0.05 * (len(ordered_nets) - 1))] if ordered_nets else None
    report["known_costs"] = {
        "priced_outcomes": len(known_fees),
        "mean_fees_usd": sum(known_fees) / len(known_fees) if known_fees else None,
        "total_fees_usd": sum(known_fees) if known_fees else None,
    }
    report["funding_mark_coverage"] = {
        "priced_outcomes": len(funding_mark_coverage),
        "mean": (sum(funding_mark_coverage) / len(funding_mark_coverage))
        if funding_mark_coverage else None,
    }
    report["liquidation_path_coverage"] = {
        "counts": dict(path_counts),
        "complete_fraction": (path_counts["COMPLETE"] / sum(path_counts.values())) if sum(path_counts.values()) else None,
    }
    report["max_adverse_basis_usd"] = max(adverse_moves) if adverse_moves else None
    report["max_portfolio_drawdown_usd"] = max(drawdowns) if drawdowns else None
    report["entry_status_counts"] = entry_counts
    report["walk_forward"] = _walk_forward_months(items)
    # This evaluator summarizes frozen outcomes; it has no synchronized
    # spot/perp path from which to recalculate drawdown after costs.
    report["cost_after_risk"] = None
    report["risk"] = {
        "max_drawdown_usd": max(drawdowns) if drawdowns else None,
        "reason": None if drawdowns else "SYNCHRONIZED_SPOT_PATH_UNAVAILABLE",
    }
    return report


def _walk_forward_months(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Describe rolling month buckets without claiming fitted model results."""
    from datetime import datetime, timezone

    monthly: dict[str, dict[str, Any]] = {}
    for row in items:
        raw = row.get("as_of_ms", row.get("decision_as_of_ms", row.get("executed_as_of_ms")))
        try:
            month = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).strftime("%Y-%m")
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        status = str(row.get("status", row.get("outcome_status", "UNAVAILABLE"))).upper()
        if status not in ("COMPLETE", "PENDING", "CENSORED", "UNAVAILABLE"):
            status = "UNAVAILABLE"
        bucket = monthly.setdefault(month, {
            "status_counts": {"COMPLETE": 0, "PENDING": 0, "CENSORED": 0, "UNAVAILABLE": 0},
            "net_returns": [],
        })
        bucket["status_counts"][status] += 1
        if status == "COMPLETE":
            pnl = row.get("pnl") if isinstance(row.get("pnl"), Mapping) else {}
            net = _finite_float(row.get("net_return", row.get("netReturn", pnl.get("net_return", row.get("net_pnl_usd")))))
            if net is not None:
                bucket["net_returns"].append(net)
    months = sorted(monthly)
    oos = months[-1] if months else None
    prior = months[:-1]
    monthly_reports: dict[str, dict[str, Any]] = {}
    for month in months:
        row = monthly[month]
        returns = sorted(row["net_returns"])
        n = len(returns)
        monthly_reports[month] = {
            "status_counts": dict(row["status_counts"]),
            "complete_return_count": n,
            "mean_net_return": sum(returns) / n if n else None,
            "median_net_return": (
                returns[n // 2] if n % 2 else (returns[n // 2 - 1] + returns[n // 2]) / 2
            ) if n else None,
            "p05_net_return": returns[int(0.05 * (n - 1))] if n else None,
        }
    return {
        "mode": "RULES_ONLY",
        "calibration_applied": False,
        "monthly_reports": monthly_reports,
        "fit_months": prior,
        "oos_month": oos,
        "oos_label": "OUT_OF_SAMPLE_RULES_ONLY" if oos and prior else "RULES_ONLY_NO_PRIOR_MONTH",
    }
