"""H03 Funding statistics + FCS scoring (pure, no network).

Design: B6.1 / B8 / B14 / B40. Plan H03.1-H03.5. R05 (D05/D03.3): unified
``FundingContext`` + ``ReadinessBreakdown`` builders live here; Service/jobs
wiring is untouched (pure helpers only).

Pure functions only. No HTTP, no DB, no clock reads. Callers pass
``cutoff_ms`` (as_of) explicitly and frozen policy mappings.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from diveintocrypto_desktop.shortlab.hedge import (
    FCS_VERSION,
    FCS_VERSION_V2,
    FCS_VERSIONS_LEGACY,
)
from diveintocrypto_desktop.shortlab.hedge.models import FCSResult, FundingMetrics

DAY_MS = 86_400_000
MAX_GAP_MS = 24 * 3_600_000
MIN_EVENTS = 3

__all__ = [
    "DAY_MS",
    "compute_funding_metrics",
    "compute_funding_std_30d",
    "compute_longest_negative_streak",
    "compute_p25",
    "compute_rolling_7d_aprs",
    "compute_conservative_apr",
    "resolve_history_class",
    "score_fcs",
    "build_fcs_distribution_report",
    "build_funding_context",
    "build_funding_readiness_breakdown",
]


# ---------------------------------------------------------------------------
# Event normalisation (H03.1: left-open right-closed, dedup, sort)
# ---------------------------------------------------------------------------

def _event_time_ms(ev: Any) -> int | None:
    if isinstance(ev, Mapping):
        for key in ("funding_time_ms", "fundingTime", "t", "funding_time"):
            if key in ev and ev[key] is not None:
                try:
                    return int(ev[key])
                except (TypeError, ValueError):
                    return None
        return None
    for attr in ("funding_time_ms", "fundingTime", "t"):
        if hasattr(ev, attr):
            try:
                return int(getattr(ev, attr))
            except (TypeError, ValueError):
                return None
    return None


def _event_rate(ev: Any) -> Decimal | None:
    if isinstance(ev, Mapping):
        for key in ("rate", "fundingRate", "funding_rate"):
            if key in ev and ev[key] is not None:
                try:
                    return Decimal(str(ev[key]))
                except (InvalidOperation, ValueError):
                    return None
        return None
    for attr in ("rate", "fundingRate", "funding_rate"):
        if hasattr(ev, attr):
            val = getattr(ev, attr)
            if val is None:
                return None
            try:
                return Decimal(str(val))
            except (InvalidOperation, ValueError):
                return None
    return None


def _event_symbol(ev: Any) -> str | None:
    if isinstance(ev, Mapping):
        for key in ("symbol", "futures_symbol"):
            if key in ev and ev[key]:
                return str(ev[key])
        return None
    for attr in ("symbol", "futures_symbol"):
        if hasattr(ev, attr):
            val = getattr(ev, attr)
            if val:
                return str(val)
    return None


def _dedup_sort(events: Sequence[Any]) -> list[tuple[int, Decimal, str]]:
    """Dedup by (symbol, fundingTime), sort ascending (B6.1)."""
    seen: dict[tuple[str, int], Decimal] = {}
    order: dict[tuple[str, int], int] = {}
    for idx, ev in enumerate(events or []):
        t = _event_time_ms(ev)
        r = _event_rate(ev)
        if t is None or r is None:
            continue
        sym = _event_symbol(ev) or ""
        key = (sym, int(t))
        if key not in seen:
            seen[key] = r
            order[key] = idx
        # duplicate: keep first, ignore later (same key, any rate)
    items = [(t, seen[(s, t)], s) for (s, t) in seen]
    items.sort(key=lambda x: (x[0], x[2]))
    return items


def _window_items(
    sorted_items: Sequence[tuple[int, Decimal, str]],
    cutoff_ms: int,
    days: int,
) -> list[tuple[int, Decimal, str]]:
    start = int(cutoff_ms) - int(days) * DAY_MS
    out = [it for it in sorted_items if it[0] > start and it[0] <= int(cutoff_ms)]
    return out


def _coverage(
    times: Sequence[int], window_start_ms: int, window_end_ms: int
) -> tuple[bool, float]:
    """B6 coverage rule (mirrors data/funding.py §10.1 verbatim).

    Complete iff >=3 events, head/tail edges <=24h and every adjacent gap
    <=24h. Fraction is 1.0 when complete, else covered-span formula.
    """
    window_len = int(window_end_ms) - int(window_start_ms)
    uniq = sorted(set(int(t) for t in times))
    if window_len <= 0 or not uniq:
        return False, 0.0
    adjacent_excess = sum(
        max(0, b - a - MAX_GAP_MS) for a, b in zip(uniq, uniq[1:])
    )
    head_gap = uniq[0] - int(window_start_ms)
    tail_gap = int(window_end_ms) - uniq[-1]
    complete = (
        len(uniq) >= MIN_EVENTS
        and head_gap <= MAX_GAP_MS
        and tail_gap <= MAX_GAP_MS
        and all(b - a <= MAX_GAP_MS for a, b in zip(uniq, uniq[1:]))
    )
    if complete:
        return True, 1.0
    span = min(uniq[-1], int(window_end_ms)) - max(uniq[0], int(window_start_ms))
    covered = max(0, span - adjacent_excess)
    frac = max(0.0, min(1.0, covered / window_len)) if window_len > 0 else 0.0
    return False, float(frac)


def _parse_listing_age(
    listing_age: Any, cutoff_ms: int
) -> tuple[float | None, bool, int | None]:
    """Return (age_days|None, reliable, onboard_ms|None).

    Accepts None (unknown), int/float days (reliable), ms timestamps
    (>1e12 treated as onboard_ms), or mappings with age/reliable keys.
    Unreliable ages never grant PARTIAL_90D N/A.
    """
    if listing_age is None:
        return None, False, None
    if isinstance(listing_age, Mapping):
        reliable = listing_age.get("reliable", listing_age.get("is_reliable", True))
        reliable = bool(reliable) if reliable is not None else True
        for key in ("age_days", "listing_age_days", "ageDays", "age_days_float"):
            if listing_age.get(key) is not None:
                try:
                    return float(listing_age[key]), bool(reliable), None
                except (TypeError, ValueError):
                    pass
        for key in ("onboard_ms", "onboardMs", "listing_ms", "onboard_at_ms"):
            if listing_age.get(key) is not None:
                try:
                    ob = int(listing_age[key])
                    age = (int(cutoff_ms) - ob) / DAY_MS
                    return float(age), bool(reliable), ob
                except (TypeError, ValueError):
                    pass
        # bare {"days": n}
        if listing_age.get("days") is not None:
            try:
                return float(listing_age["days"]), bool(reliable), None
            except (TypeError, ValueError):
                pass
        return None, False, None
    try:
        val = float(listing_age)
    except (TypeError, ValueError):
        return None, False, None
    # Heuristic: values > 1e12 are ms timestamps (onboard_ms).
    if val > 1e12:
        ob = int(val)
        return (int(cutoff_ms) - ob) / DAY_MS, True, ob
    return float(val), True, None


def resolve_history_class(
    listing_age: Any, cutoff_ms: int
) -> tuple[str, float]:
    """Map listing age to (history_class, available_max_score).

    PARTIAL_90D only for reliable ages in [0, 90). Unknown/unreliable
    ages never yield N/A (they are MISSING when 90D is absent).
    """
    age_days, reliable, _ = _parse_listing_age(listing_age, cutoff_ms)
    if reliable and age_days is not None and 0 <= age_days < 90:
        return "PARTIAL_90D", 90.0
    if reliable and age_days is not None and age_days >= 90:
        return "FULL_90D", 100.0
    # Unknown age: class is decided by data completeness downstream.
    # Default to FULL_90D shape (max 100) so missing 90D => null, not N/A.
    return "FULL_90D", 100.0


def _onboard_ms(listing_age: Any, cutoff_ms: int) -> int | None:
    _, reliable, ob = _parse_listing_age(listing_age, cutoff_ms)
    if reliable and ob is not None:
        return ob
    age, reliable2, _ = _parse_listing_age(listing_age, cutoff_ms)
    if reliable2 and age is not None and age >= 0:
        return int(int(cutoff_ms) - float(age) * DAY_MS)
    return None


def _decimal_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return format(d, "f")


# ---------------------------------------------------------------------------
# Interval resolution + Std8h (H03.2)
# ---------------------------------------------------------------------------

def _normalise_interval_map(interval_observations: Any) -> dict[int, float | None]:
    """Accept Mapping[time->hours|{interval_hours}] or list of mappings."""
    if interval_observations is None:
        return {}
    if isinstance(interval_observations, Mapping):
        out: dict[int, float | None] = {}
        for k, v in interval_observations.items():
            try:
                t = int(k)
            except (TypeError, ValueError):
                continue
            if v is None:
                out[t] = None
                continue
            if isinstance(v, Mapping):
                for ik in ("interval_hours", "intervalHours", "hours", "interval"):
                    if ik in v and v[ik] is not None:
                        try:
                            out[t] = float(v[ik])
                        except (TypeError, ValueError):
                            out[t] = None
                        break
                else:
                    out[t] = None
            else:
                try:
                    out[t] = float(v)
                except (TypeError, ValueError):
                    out[t] = None
        return out
    if isinstance(interval_observations, (list, tuple)):
        out2: dict[int, float | None] = {}
        for row in interval_observations:
            if isinstance(row, Mapping):
                t = None
                for tk in ("funding_time_ms", "fundingTime", "t"):
                    if row.get(tk) is not None:
                        try:
                            t = int(row[tk])
                        except (TypeError, ValueError):
                            t = None
                        break
                if t is None:
                    continue
                v = None
                for ik in ("interval_hours", "intervalHours", "hours", "interval"):
                    if row.get(ik) is not None:
                        v = row[ik]
                        break
                if v is None:
                    out2[t] = None
                else:
                    try:
                        out2[t] = float(v)
                    except (TypeError, ValueError):
                        out2[t] = None
        return out2
    return {}


def _resolve_interval_hours(
    window: Sequence[tuple[int, Decimal, str]],
    interval_map: Mapping[int, float | None],
) -> list[float] | None:
    """Per-event interval_hours. Explicit metadata wins; else verifiable gap.

    A single current interval is never broadcast across history: each event
    needs its own entry. Gap inference only succeeds when the window gaps
    are uniform (regular cadence); irregular/unknown gaps stay missing.
    """
    if not window:
        return None
    times = [t for t, _, _ in window]
    # Check uniformity for inference fallback.
    gaps = [b - a for a, b in zip(times, times[1:])] if len(times) > 1 else []
    uniform_gap: int | None = None
    if gaps and max(gaps) == min(gaps) and 0 < gaps[0] <= MAX_GAP_MS:
        uniform_gap = gaps[0]
    resolved: list[float] = []
    for idx, (t, _, _) in enumerate(window):
        if t in interval_map:
            v = interval_map[t]
            if v is None or not math.isfinite(float(v)) or float(v) <= 0:
                return None
            resolved.append(float(v))
            continue
        # Fallback: verifiable adjacent gap (uniform cadence only).
        if uniform_gap is not None:
            resolved.append(uniform_gap / 3_600_000.0)
            continue
        # Single-event window: no gap to verify.
        return None
    if any(h <= 0 or not math.isfinite(h) for h in resolved):
        return None
    return resolved


def compute_funding_std_30d(
    events: Sequence[Any],
    interval_observations: Any,
    cutoff_ms: int,
    *,
    coverage_overrides: Mapping[str, Any] | None = None,
) -> float | None:
    """Population std (ddof=0) of 8h-equivalent rates in the 30D window.

    Returns None when the 30D window is incomplete or any interval is
    unknown/non-positive. Never uses a single current interval.
    """
    items = _dedup_sort(events)
    window = _window_items(items, int(cutoff_ms), 30)
    start = int(cutoff_ms) - 30 * DAY_MS
    if coverage_overrides and "30d" in coverage_overrides:
        cov = coverage_overrides["30d"]
        complete = bool(cov.get("complete", False)) if isinstance(cov, Mapping) else bool(cov)
    else:
        complete, _ = _coverage([t for t, _, _ in window], start, int(cutoff_ms))
    if not complete or not window:
        return None
    interval_map = _normalise_interval_map(interval_observations)
    hours = _resolve_interval_hours(window, interval_map)
    if hours is None:
        return None
    equiv = [float(r) * 8.0 / h for (_, r, _), h in zip(window, hours)]
    n = len(equiv)
    if n == 0:
        return None
    mean = sum(equiv) / n
    var = sum((x - mean) ** 2 for x in equiv) / n  # ddof=0
    return math.sqrt(max(0.0, var))


def compute_longest_negative_streak(
    events: Sequence[Any],
    cutoff_ms: int,
    *,
    days: int = 30,
    coverage_overrides: Mapping[str, Any] | None = None,
) -> int | None:
    """Longest consecutive rate<0 run in window. 0 breaks. None if incomplete."""
    items = _dedup_sort(events)
    window = _window_items(items, int(cutoff_ms), int(days))
    start = int(cutoff_ms) - int(days) * DAY_MS
    key = f"{int(days)}d"
    if coverage_overrides and key in coverage_overrides:
        cov = coverage_overrides[key]
        complete = bool(cov.get("complete", False)) if isinstance(cov, Mapping) else bool(cov)
    else:
        complete, _ = _coverage([t for t, _, _ in window], start, int(cutoff_ms))
    if not complete:
        return None
    best = 0
    cur = 0
    for _, r, _ in window:
        if r < 0:
            cur += 1
            best = max(best, cur)
        else:  # 0 breaks the streak
            cur = 0
    return best


# ---------------------------------------------------------------------------
# Rolling 7D APR P25 + Conservative APR (H03.3, B14)
# ---------------------------------------------------------------------------

def compute_p25(values: Sequence[float]) -> float | None:
    """Linear P25 with index=(n-1)*0.25. None when empty."""
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    if n == 1:
        return vals[0]
    pos = (n - 1) * 0.25
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def compute_rolling_7d_aprs(
    events: Sequence[Any],
    cutoff_ms: int,
    *,
    lookback_days: int = 30,
) -> list[float]:
    """Rolling 7D simple APRs for each complete UTC day-end in lookback.

    Each window is (day_end-7D, day_end] with day_end a UTC midnight
    <= cutoff. Only complete (24h-gap rule) windows are kept.
    """
    items = _dedup_sort(events)
    cutoff = int(cutoff_ms)
    # Latest UTC midnight <= cutoff.
    latest_day_end = (cutoff // DAY_MS) * DAY_MS
    start_bound = cutoff - int(lookback_days) * DAY_MS
    aprs: list[float] = []
    day_end = latest_day_end
    while day_end > start_bound and day_end <= cutoff:
        wstart = day_end - 7 * DAY_MS
        win = [it for it in items if it[0] > wstart and it[0] <= day_end]
        complete, _ = _coverage([t for t, _, _ in win], wstart, day_end)
        if complete and win:
            total = sum((r for _, r, _ in win), Decimal(0))
            aprs.append(float(total) * 365.0 / 7.0)
        # Step one UTC day back.
        day_end -= DAY_MS
    return aprs


def compute_conservative_apr(
    funding_30d: Any,
    funding_90d: Any,
    p25_30d: float | None,
    p25_90d: float | None,
    history_class: str,
) -> tuple[str | None, str]:
    """B14 ConservativeAPR. Returns (decimal_str|None, method)."""
    try:
        apr30 = float(Decimal(str(funding_30d))) * 365.0 / 30.0 if funding_30d is not None else None
    except (InvalidOperation, ValueError, TypeError):
        apr30 = None
    try:
        apr90 = float(Decimal(str(funding_90d))) * 365.0 / 90.0 if funding_90d is not None else None
    except (InvalidOperation, ValueError, TypeError):
        apr90 = None
    if history_class == "PARTIAL_90D":
        if apr30 is None or p25_30d is None or not math.isfinite(p25_30d):
            return None, "MIN_APR30_P25_30D_INSUFFICIENT"
        val = max(0.0, min(apr30, float(p25_30d)))
        return _decimal_str(val), "MIN_APR30_P25_30D"
    # FULL path requires all three.
    if apr30 is None or apr90 is None or p25_90d is None or not math.isfinite(p25_90d):
        return None, "MIN_APR30_APR90_P25_90D_INSUFFICIENT"
    val2 = max(0.0, min(apr30, apr90, float(p25_90d)))
    return _decimal_str(val2), "MIN_APR30_APR90_P25_90D"


# ---------------------------------------------------------------------------
# compute_funding_metrics (H03.1-H03.3)
# ---------------------------------------------------------------------------

def compute_funding_metrics(
    events: Sequence[Any],
    interval_observations: Any,
    cutoff_ms: int,
    listing_age: Any,
    *,
    symbol: str | None = None,
    current_rate: Any = None,
    last_settled_rate: Any = None,
    coverage_overrides: Mapping[str, Any] | None = None,
) -> FundingMetrics:
    """Per-symbol funding carry inputs (B6.1). Pure.

    Windows are left-open right-closed ``(cutoff-D*DAY, cutoff]`` over
    settled events only, deduped by (symbol, fundingTime) and ascending.
    Any window with ``complete=false`` yields null sums/ratios even when
    its ``coverage_fraction`` is high. 90D is N/A only for reliable young
    ages (PARTIAL_90D); unknown ages never mask missing history.
    """
    cutoff = int(cutoff_ms)
    items = _dedup_sort(events)
    sym = symbol or (_event_symbol(events[0]) if events else None) or "UNKNOWN"

    onboard = _onboard_ms(listing_age, cutoff)
    history_class, _ = resolve_history_class(listing_age, cutoff)

    def _window(days: int) -> tuple[list[tuple[int, Decimal, str]], bool, float]:
        win = _window_items(items, cutoff, days)
        raw_start = cutoff - days * DAY_MS
        start = max(raw_start, onboard) if onboard is not None else raw_start
        key = f"{days}d"
        if coverage_overrides and key in coverage_overrides:
            cov = coverage_overrides[key]
            if isinstance(cov, Mapping):
                complete = bool(cov.get("complete", False))
                frac = float(cov.get("coverage_fraction", 0.0))
            else:
                complete = bool(cov)
                _, frac = _coverage([t for t, _, _ in win], start, cutoff)
        else:
            complete, frac = _coverage([t for t, _, _ in win], start, cutoff)
        return win, complete, float(frac)

    win7, complete7, frac7 = _window(7)
    win30, complete30, frac30 = _window(30)
    win90, complete90, frac90 = _window(90)

    def _sums(win: list[tuple[int, Decimal, str]], complete: bool):
        if not complete or not win:
            return None, None
        total = sum((r for _, r, _ in win), Decimal(0))
        pos = sum(1 for _, r, _ in win if r > 0)
        return total, Decimal(pos) / Decimal(len(win))

    sum7, _ = _sums(win7, complete7)
    sum30, ratio30 = _sums(win30, complete30)

    # 90D: PARTIAL ages keep N/A (None) without failing; otherwise missing => None (caller maps to FCS null).
    if history_class == "PARTIAL_90D":
        sum90: Decimal | None = None
        ratio90: Decimal | None = None
        if complete90 and win90:
            sum90 = sum((r for _, r, _ in win90), Decimal(0))
            ratio90 = Decimal(sum(1 for _, r, _ in win90 if r > 0)) / Decimal(len(win90))
        else:
            sum90, ratio90 = None, None
    else:
        sum90, ratio90 = _sums(win90, complete90)
        if not complete90:
            sum90, ratio90 = None, None

    # Last settled = latest settled event <= cutoff.
    if last_settled_rate is None:
        past = [it for it in items if it[0] <= cutoff]
        last_settled_rate = str(past[-1][1]) if past else None

    # Conservative APR needs rolling P25s (20/60 window rule).
    aprs30 = compute_rolling_7d_aprs(events, cutoff, lookback_days=30)
    aprs90 = compute_rolling_7d_aprs(events, cutoff, lookback_days=90)
    p25_30 = compute_p25(aprs30) if len(aprs30) >= 20 else None
    p25_90 = compute_p25(aprs90) if len(aprs90) >= 60 else None
    # PARTIAL uses 30D P25; FULL uses 90D P25. Missing windows => conservative null.
    cons_val, _ = compute_conservative_apr(
        str(sum30) if sum30 is not None else None,
        str(sum90) if sum90 is not None else None,
        p25_30,
        p25_90,
        history_class,
    )

    # history_coverage: 90D fraction when FULL else 30D fraction (diagnostic).
    hist_cov = frac90 if history_class == "FULL_90D" else frac30

    return FundingMetrics(
        symbol=str(sym),
        current_rate=_decimal_str(current_rate),
        last_settled_rate=_decimal_str(last_settled_rate),
        funding_7d=_decimal_str(sum7),
        funding_30d=_decimal_str(sum30),
        funding_90d=_decimal_str(sum90),
        positive_ratio_30d=_decimal_str(ratio30),
        positive_ratio_90d=_decimal_str(ratio90),
        coverage_30d=_decimal_str(frac30),
        coverage_90d=_decimal_str(frac90),
        conservative_apr=_decimal_str(cons_val),
        history_coverage=_decimal_str(hist_cov),
    )


# ---------------------------------------------------------------------------
# score_fcs (H03.3-H03.4, B8/B40 bins read from policy, no reweight)
# ---------------------------------------------------------------------------

def _extract_funding_capture(policy: Any) -> tuple[Mapping[str, Any], Mapping[str, Any], Any, str]:
    """Return (score_rules, statistics, reference_notional, fcs_version).

    R00/D15: accepts ``fcs_v1``/``fcs_v2``; old ``fcs_v1`` input still
    decodes (legacy bucket) while new calculations use ``fcs_v2``. Bins are
    identical across versions (weights never reallocated); only the emitted
    ``fcs_version`` stamp differs.
    """
    if hasattr(policy, "funding_capture"):
        fc = policy.funding_capture  # ShortLabConfig
        return (
            dict(fc.score_rules),
            dict(fc.statistics),
            fc.reference_notional_usd,
            fc.fcs_version,
        )
    if hasattr(policy, "score_rules"):
        fc2 = policy  # FundingCaptureConfig
        stats = dict(getattr(fc2, "statistics", {}))
        return (
            dict(fc2.score_rules),
            stats,
            getattr(fc2, "reference_notional_usd", 10000),
            getattr(fc2, "fcs_version", FCS_VERSION_V2),
        )
    if isinstance(policy, Mapping):
        node: Any = policy
        if "funding_capture" in node and isinstance(node["funding_capture"], Mapping):
            node = node["funding_capture"]
        rules = node.get("score_rules", {})
        stats = node.get("statistics", {})
        ref = node.get("reference_notional_usd", 10000)
        ver = node.get("fcs_version", FCS_VERSION_V2)
        return dict(rules), dict(stats), ref, str(ver)
    raise ValueError("policy must carry funding_capture score_rules")


def _pick_min_inclusive(value: float, bins: Sequence[Sequence[Any]], fallback: Any) -> int:
    ordered = sorted(((float(t), int(s)) for t, s in bins), key=lambda x: -x[0])
    for threshold, score in ordered:
        if value >= threshold:
            return score
    try:
        return int(fallback)
    except (TypeError, ValueError):
        return 0


def _pick_max_inclusive(value: float, bins: Sequence[Sequence[Any]], fallback: Any) -> int:
    ordered = sorted(((float(t), int(s)) for t, s in bins), key=lambda x: x[0])
    for threshold, score in ordered:
        if value <= threshold:
            return score
    try:
        return int(fallback)
    except (TypeError, ValueError):
        return 0


def _pick_exact(value: Any, bins: Sequence[Sequence[Any]], fallback: Any) -> int:
    for threshold, score in bins:
        try:
            if int(value) == int(threshold):
                return int(score)
        except (TypeError, ValueError):
            continue
    try:
        return int(fallback)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return f if math.isfinite(f) else None


def score_fcs(
    metrics: FundingMetrics,
    venue: Mapping[str, Any] | None,
    basis: Mapping[str, Any] | float | int | None,
    identity: Mapping[str, Any] | None,
    policy: Any,
    *,
    symbol: str | None = None,
    canonical_id: str | None = None,
    as_of_ms: int | None = None,
    snapshot_id: str | None = None,
    fcs_config_hash: str | None = None,
    reference_notional_usd: Any = None,
    funding_stats: Mapping[str, Any] | None = None,
    conservative: Mapping[str, Any] | None = None,
    history_class: str | None = None,
    history_context: Mapping[str, Any] | None = None,
    created_at_ms: int | None = None,
) -> FCSResult:
    """Deterministic FCS 0..100 (B8.3). Missing applicable input => fcs null.

    Bins are read逐项from the default policy (B40); weights are never
    reallocated. Reliable young ages keep 90D as N/A (0, cap 90, fixed
    0..100 axis); ordinary missing history yields null. R00/D15: both
    ``fcs_v1`` and ``fcs_v2`` policies are accepted with identical bins;
    the emitted ``fcs_version`` follows the policy version.
    """
    rules, _stats, policy_ref, policy_ver = _extract_funding_capture(policy)
    if policy_ver not in FCS_VERSIONS_LEGACY:
        raise ValueError(
            f"policy fcs_version must be one of {sorted(FCS_VERSIONS_LEGACY)}, "
            f"got {policy_ver!r}"
        )

    # History context: explicit history_class wins; else conservative/history_context.
    hist = history_class
    avail_max = 100.0
    cons_method: str | None = None
    cons_value: str | None = getattr(metrics, "conservative_apr", None)
    if conservative and isinstance(conservative, Mapping):
        hist = hist or conservative.get("history_class") or conservative.get("historyClass")
        cons_method = conservative.get("method") or conservative.get("conservative_apr_method")
        if conservative.get("conservative_apr") is not None:
            cons_value = _decimal_str(conservative.get("conservative_apr"))
        if conservative.get("available_max_score") is not None:
            try:
                avail_max = float(conservative.get("available_max_score"))
            except (TypeError, ValueError):
                pass
    if history_context and isinstance(history_context, Mapping):
        hist = hist or history_context.get("history_class") or history_context.get("historyClass")
        if history_context.get("available_max_score") is not None:
            try:
                avail_max = float(history_context.get("available_max_score"))
            except (TypeError, ValueError):
                pass
        if history_context.get("conservative_apr_method") is not None:
            cons_method = str(history_context.get("conservative_apr_method"))
    if hist is None:
        hist = "PARTIAL_90D" if avail_max == 90.0 else "FULL_90D"
    if hist == "PARTIAL_90D":
        avail_max = 90.0

    reasons: list[str] = []
    null_reason: str | None = None

    f30 = _to_float(getattr(metrics, "funding_30d", None))
    pr30 = _to_float(getattr(metrics, "positive_ratio_30d", None))
    pr90 = _to_float(getattr(metrics, "positive_ratio_90d", None))

    # Funding stats (std/streak) come from the caller-computed bundle.
    std_val: float | None = None
    streak_val: int | None = None
    p25_30: float | None = None
    p25_90: float | None = None
    if funding_stats and isinstance(funding_stats, Mapping):
        for k in ("funding_std_30d", "std_30d", "fundingStd30d"):
            if funding_stats.get(k) is not None:
                std_val = _to_float(funding_stats.get(k))
                break
        for k in ("longest_negative_streak_30d", "negative_streak", "longestNegativeStreak30d"):
            if funding_stats.get(k) is not None:
                try:
                    streak_val = int(funding_stats.get(k))
                except (TypeError, ValueError):
                    streak_val = None
                break
        for k in ("p25_rolling_7d_apr_30d", "p25_30d"):
            if funding_stats.get(k) is not None:
                std_tmp = _to_float(funding_stats.get(k))
                p25_30 = std_tmp
                break
        for k in ("p25_rolling_7d_apr_90d", "p25_90d"):
            if funding_stats.get(k) is not None:
                p25_90 = _to_float(funding_stats.get(k))
                break
        if funding_stats.get("history_class"):
            hist = str(funding_stats.get("history_class"))
            avail_max = 90.0 if hist == "PARTIAL_90D" else 100.0

    # --- Gate: any applicable missing => null (no zero substitution). ---
    if f30 is None:
        null_reason = "NOT_READY_FUNDING_COVERAGE"
    elif pr30 is None:
        null_reason = "NOT_READY_FUNDING_PERSISTENCE"
    elif pr90 is None and hist != "PARTIAL_90D":
        null_reason = "NOT_READY_FUNDING_PERSISTENCE_90D"
    elif std_val is None or streak_val is None:
        null_reason = "NOT_READY_FUNDING_STABILITY"

    venue_scores: dict[str, Any] = {}
    basis_score: int | None = None
    basis_value: float | None = None
    op_score: int | None = None

    if null_reason is None:
        # Venue (all three sub-items required verbatim from policy).
        if not isinstance(venue, Mapping):
            null_reason = "NOT_READY_VENUE_MISSING"
        else:
            buy = _to_float(venue.get("buy_executable_qty", venue.get("buyExecutableQty")))
            sell = _to_float(venue.get("sell_executable_qty", venue.get("sellExecutableQty")))
            cost = _to_float(venue.get("roundtrip_cost_pct", venue.get("roundtripCostPct",
                           venue.get("roundtrip_cost", venue.get("cost_pct")))))
            exit_f = venue.get("exit_feasibility", venue.get("exitFeasibility"))
            ref = reference_notional_usd if reference_notional_usd is not None else policy_ref
            try:
                ref_f = float(Decimal(str(ref)))
            except (InvalidOperation, ValueError, TypeError):
                ref_f = None
            requested = _to_float(venue.get('requested_canonical_qty'))
            if buy is None or sell is None or cost is None or exit_f is None or ref_f is None or ref_f <= 0 or requested is None or requested <= 0:
                null_reason = "NOT_READY_VENUE_MISSING"
            else:
                vrule = rules.get("venue", {})
                min_cov_bins = vrule.get("min_coverage_bins", [])
                cov_fallback = vrule.get("coverage_fallback", 0)
                # Executable quantities and the requested quantity share the
                # canonical token unit. USD notionals are never a denominator.
                min_cov = min(buy / requested, sell / requested)
                s_cov = _pick_min_inclusive(min_cov, min_cov_bins, cov_fallback)
                cost_bins = vrule.get("roundtrip_cost_max_bins", [])
                cost_fallback = vrule.get("cost_fallback", 0)
                s_cost = _pick_max_inclusive(cost, cost_bins, cost_fallback)
                exit_scores = dict(vrule.get("exit_scores", {}))
                s_exit = exit_scores.get(str(exit_f))
                if s_exit is None:
                    null_reason = "NOT_READY_VENUE_MISSING"
                else:
                    venue_scores = {
                        "coverage": int(s_cov),
                        "roundtrip_cost": int(s_cost),
                        "exit": int(s_exit),
                        "min_coverage": min_cov,
                    }
    if null_reason is None:
        # Basis.
        if isinstance(basis, Mapping):
            braw = basis.get("basis", basis.get("basis_pct", basis.get("value")))
        else:
            braw = basis
        basis_value = _to_float(braw)
        if basis_value is None:
            null_reason = "NOT_READY_BASIS_MISSING"
        else:
            brule = rules.get("basis", {})
            nonneg = brule.get("nonnegative_max_bins", [])
            pos_above = brule.get("positive_above", 2)
            neg_floor = _to_float(brule.get("negative_floor", -0.005))
            neg_above = brule.get("negative_at_or_above", 3)
            neg_below = brule.get("negative_below", 0)
            b = float(basis_value)
            if b < 0:
                basis_score = int(neg_above) if (neg_floor is not None and b >= neg_floor) else int(neg_below)
            else:
                picked = None
                for threshold, score in sorted(
                    ((float(t), int(s)) for t, s in nonneg), key=lambda x: x[0]
                ):
                    if b <= threshold:
                        picked = int(score)
                        break
                basis_score = picked if picked is not None else int(pos_above)
    if null_reason is None:
        # Operational (each item binary; unknown => null, not 0).
        if not isinstance(identity, Mapping):
            null_reason = "NOT_READY_OPERATIONAL_MISSING"
        else:
            orule = rules.get("operational", {})
            fut = identity.get("futures_status", identity.get("futuresStatus"))
            delist = identity.get("no_delisting", identity.get("delisting_confirmed",
                       identity.get("has_delisting", identity.get("delisting"))))
            # Normalise delisting: explicit True(confirmed delisting)=fail.
            if "no_delisting" in identity:
                no_del_ok = identity.get("no_delisting")
            elif "delisting_confirmed" in identity:
                dc = identity.get("delisting_confirmed")
                no_del_ok = (not bool(dc)) if dc is not None else None
            elif "has_delisting" in identity or "delisting" in identity:
                dc2 = identity.get("has_delisting", identity.get("delisting"))
                no_del_ok = (not bool(dc2)) if dc2 is not None else None
            else:
                no_del_ok = None
            idconf = identity.get("identity_confidence", identity.get("identityConfidence"))
            mult = identity.get("multiplier_verified", identity.get("multiplierVerified",
                      identity.get("multiplier")))
            qfresh = identity.get("spot_quote_fresh", identity.get("spotQuoteFresh",
                       identity.get("quote_fresh")))
            ftime = identity.get("next_funding_time_known", identity.get("nextFundingTimeKnown",
                      identity.get("funding_time_known", identity.get("funding_time"))))
            if fut is None or no_del_ok is None or idconf is None or mult is None or qfresh is None or ftime is None:
                null_reason = "NOT_READY_OPERATIONAL_MISSING"
            else:
                s = 0
                s += int(orule.get("trading", 2)) if str(fut) == "TRADING" else 0
                s += int(orule.get("no_delisting", 2)) if bool(no_del_ok) else 0
                s += int(orule.get("identity", 2)) if str(idconf) in ("VERIFIED", "HIGH") else 0
                # multiplier may be bool or "VERIFIED".
                mult_ok = bool(mult) if not isinstance(mult, str) else (str(mult) in ("VERIFIED", "TRUE", "True", "true", "1"))
                # Explicit False string handling.
                if isinstance(mult, bool):
                    mult_ok = mult
                elif isinstance(mult, str) and mult.upper() in ("FALSE", "UNVERIFIED", "NO"):
                    mult_ok = False
                s += int(orule.get("multiplier", 2)) if mult_ok else 0
                s += int(orule.get("quote_fresh", 1)) if bool(qfresh) else 0
                s += int(orule.get("funding_time", 1)) if bool(ftime) else 0
                op_score = int(s)

    module_scores: dict[str, Any] = {}
    fcs_value: float | None = None
    readiness = "NOT_READY"

    if null_reason is not None:
        reasons.append(null_reason)
        if hist == "PARTIAL_90D":
            reasons.append("FCS_PARTIAL_HISTORY")
    else:
        assert f30 is not None and pr30 is not None
        yrule = rules.get("yield_30d", {})
        s_yield = _pick_min_inclusive(
            float(f30), yrule.get("min_inclusive_bins", []),
            yrule.get("positive_fallback", 0) if float(f30) > 0 else yrule.get("non_positive", 0),
        )
        # Positive fallback applies only when >0 but below lowest bin.
        if float(f30) > 0:
            lowest = min((float(t) for t, _ in yrule.get("min_inclusive_bins", [[10**9, 0]])), default=None)
            if lowest is not None and float(f30) < lowest:
                s_yield = int(yrule.get("positive_fallback", 3))
        else:
            s_yield = int(yrule.get("non_positive", 0))

        pr30_rule = rules.get("positive_ratio_30d", {})
        s_pr30 = _pick_min_inclusive(float(pr30), pr30_rule.get("min_inclusive_bins", []), pr30_rule.get("fallback", 0))

        pr90_rule = rules.get("positive_ratio_90d", {})
        if hist == "PARTIAL_90D":
            s_pr90: Any = int(pr90_rule.get("not_applicable_score", 0))
            pr90_status = "N/A"
            reasons.append("FCS_PARTIAL_HISTORY")
        else:
            assert pr90 is not None
            s_pr90 = _pick_min_inclusive(float(pr90), pr90_rule.get("min_inclusive_bins", []), pr90_rule.get("fallback", 0))
            pr90_status = "APPLICABLE"

        std_rule = rules.get("std_30d", {})
        assert std_val is not None
        s_std = _pick_max_inclusive(float(std_val), std_rule.get("max_inclusive_bins", []), std_rule.get("fallback", 0))
        neg_rule = rules.get("negative_streak", {})
        assert streak_val is not None
        s_streak = _pick_exact(int(streak_val), neg_rule.get("exact_bins", []), neg_rule.get("fallback", 0))

        assert venue_scores and basis_score is not None and op_score is not None
        total = (
            int(s_yield) + int(s_pr30) + int(s_pr90) + int(s_std) + int(s_streak)
            + int(venue_scores["coverage"]) + int(venue_scores["roundtrip_cost"]) + int(venue_scores["exit"])
            + int(basis_score) + int(op_score)
        )
        fcs_value = float(total)
        readiness = "READY"
        module_scores = {
            "funding_yield": int(s_yield),
            "funding_persistence_30d": int(s_pr30),
            "funding_persistence_90d": int(s_pr90),
            "funding_persistence_90d_status": pr90_status if hist != "PARTIAL_90D" else "N/A",
            "funding_stability_std": int(s_std),
            "funding_stability_streak": int(s_streak),
            "venue_coverage": int(venue_scores["coverage"]),
            "venue_roundtrip_cost": int(venue_scores["roundtrip_cost"]),
            "venue_exit": int(venue_scores["exit"]),
            "basis_quality": int(basis_score),
            "operational_safety": int(op_score),
            "total": int(total),
        }

    sym_out = symbol or getattr(metrics, "symbol", "UNKNOWN")
    canon = canonical_id or sym_out
    cutoff_out = int(as_of_ms) if as_of_ms is not None else 0
    snap = snapshot_id or f"{sym_out}:{cutoff_out}"
    ref_out = reference_notional_usd if reference_notional_usd is not None else policy_ref
    try:
        ref_str = _decimal_str(ref_out) or "10000"
    except Exception:
        ref_str = "10000"

    funding_metrics_dict: dict[str, Any] = {
        "funding_7d": getattr(metrics, "funding_7d", None),
        "funding_30d": getattr(metrics, "funding_30d", None),
        "funding_90d": getattr(metrics, "funding_90d", None),
        "positive_ratio_30d": getattr(metrics, "positive_ratio_30d", None),
        "positive_ratio_90d": getattr(metrics, "positive_ratio_90d", None),
        "coverage_30d": getattr(metrics, "coverage_30d", None),
        "coverage_90d": getattr(metrics, "coverage_90d", None),
        "conservative_apr": cons_value,
        "conservative_apr_method": cons_method,
        "history_coverage": getattr(metrics, "history_coverage", None),
        "history_class": hist,
        "available_max_score": avail_max,
        "funding_std_30d": std_val,
        "longest_negative_streak_30d": streak_val,
        "p25_rolling_7d_apr_30d": p25_30,
        "p25_rolling_7d_apr_90d": p25_90,
        "current_rate": getattr(metrics, "current_rate", None),
        "last_settled_rate": getattr(metrics, "last_settled_rate", None),
    }
    venue_summary_dict: dict[str, Any] = dict(venue) if isinstance(venue, Mapping) else {}
    venue_summary_dict.setdefault("reference_notional_usd", ref_str)
    if venue_scores:
        venue_summary_dict["venue_scores"] = dict(venue_scores)
    basis_dict: dict[str, Any] | None = None
    if isinstance(basis, Mapping):
        basis_dict = dict(basis)
        basis_dict["basis"] = basis_value
        basis_dict["basis_score"] = basis_score
    elif basis is not None:
        basis_dict = {"basis": basis_value, "basis_score": basis_score}
    risk_dict: dict[str, Any] = {
        "history_class": hist,
        "available_max_score": avail_max,
        "missing_reason": null_reason,
        "conservative_apr_method": cons_method,
    }
    if hist == "PARTIAL_90D" and null_reason == "NOT_READY_FUNDING_PERSISTENCE_90D":
        # PARTIAL must not null on 90D; defensive (already handled).
        pass

    return FCSResult(
        snapshot_id=str(snap),
        symbol=str(sym_out),
        canonical_id=str(canon),
        as_of_ms=cutoff_out,
        fcs_version=str(policy_ver),
        fcs_config_hash=str(fcs_config_hash or ""),
        reference_notional_usd=str(ref_str),
        fcs=fcs_value,
        module_scores=dict(module_scores),
        funding_metrics=dict(funding_metrics_dict),
        venue_summary=dict(venue_summary_dict),
        basis=basis_dict,
        risk=dict(risk_dict),
        readiness=readiness,
        reasons=tuple(reasons),
        created_at_ms=int(created_at_ms) if created_at_ms is not None else cutoff_out,
    )


# ---------------------------------------------------------------------------
# R05 unified FundingContext + ReadinessBreakdown builders (D05/D03.3)
# ---------------------------------------------------------------------------

def build_funding_context(
    metrics: FundingMetrics,
    coverage_7d: Any,
    coverage_30d: Any,
    coverage_90d: Any,
    *,
    history_class: str,
    listing_age_days: int | None,
    conservative_apr: str | None,
    conservative_method: str,
    current_observation: Any,
    last_settled_observation: Any | None,
    schedule_refs: Sequence[str] = (),
    input_refs: Mapping[str, str] | None = None,
    schedules: Sequence[Any] | None = None,
    as_of_ms: int | None = None,
) -> Any:
    """Assemble a frozen :class:`FundingContext` from already-computed parts.

    Pure assembler (no recomputation, no network): ``metrics`` comes from
    :func:`compute_funding_metrics`, coverages from
    ``funding_schedule.compute_schedule_coverage``. History class follows
    D05.2 (``FULL_90D``/``PARTIAL_90D``/``INSUFFICIENT``/
    ``HISTORY_CLASS_UNKNOWN``); unknown listing never borrows ``first_seen``.
    Missing ``mark`` never blocks this assembler -- it only lowers the
    separate USD-carry ``priced_event_coverage`` (see
    ``funding_schedule.compute_priced_event_coverage``).
    """
    from diveintocrypto_desktop.shortlab.repair_contracts import FundingContext

    if history_class not in ("FULL_90D", "PARTIAL_90D", "INSUFFICIENT", "HISTORY_CLASS_UNKNOWN"):
        raise ValueError(f"history_class={history_class!r} unknown")
    frozen_refs = dict(input_refs) if input_refs is not None else {}
    if as_of_ms is not None:
        cutoff = int(as_of_ms)
        from diveintocrypto_desktop.shortlab.funding_schedule import (
            latest_expected_settled_slot,
        )

        symbol = str(getattr(metrics, "symbol", "") or "") or None
        expected = latest_expected_settled_slot(
            schedules or (), as_of_ms=cutoff, known_by_ms=cutoff, symbol=symbol
        )
        frozen_refs["schedule_checked_at_ms"] = str(cutoff)
        if expected is not None:
            frozen_refs["last_expected_slot_ms"] = str(expected)
        else:
            frozen_refs.pop("last_expected_slot_ms", None)
    return FundingContext(
        metrics=metrics,
        coverage_7d=coverage_7d,
        coverage_30d=coverage_30d,
        coverage_90d=coverage_90d,
        history_class=history_class,
        listing_age_days=listing_age_days,
        conservative_apr=conservative_apr,
        conservative_method=conservative_method,
        current_observation=current_observation,
        last_settled_observation=last_settled_observation,
        schedule_refs=tuple(schedule_refs),
        input_refs=frozen_refs,
    )


def build_funding_readiness_breakdown(
    funding_gate: Any,
    *,
    execution_gate: Any | None = None,
    economic_gate: Any | None = None,
    protection_status: str = "UNKNOWN",
    data_complete: bool | None = None,
) -> Any:
    """Assemble a frozen :class:`ReadinessBreakdown` (D03.3) around a funding gate.

    Pure: ``funding_gate`` comes from
    ``hedge.entry_gate.evaluate_funding_entry_gate``. Missing execution /
    economic gates default to ``UNKNOWN`` (never PASS by default, never
    0-fill). ``readiness`` is ``READY`` only when all three gates PASS;
    otherwise ``NOT_READY`` (funding alone never grants ``BLOCKED``; BLOCKED
    is reserved for delisted/identity states owned elsewhere). FCS scores
    never override this gate (thresholds stay independent).
    """
    from diveintocrypto_desktop.shortlab.repair_contracts import (
        GateResult,
        ReadinessBreakdown,
    )

    if not isinstance(funding_gate, GateResult):
        raise ValueError("funding_gate must be GateResult")
    checked = int(funding_gate.checked_at_ms)
    if execution_gate is None:
        execution_gate = GateResult("UNKNOWN", ("EXECUTION_GATE_NOT_EVALUATED",), checked, {})
    if economic_gate is None:
        economic_gate = GateResult("UNKNOWN", ("ECONOMIC_GATE_NOT_EVALUATED",), checked, {})
    if not isinstance(execution_gate, GateResult) or not isinstance(economic_gate, GateResult):
        raise ValueError("execution_gate/economic_gate must be GateResult or None")
    if data_complete is None:
        data_complete = (
            funding_gate.status != "UNKNOWN"
            and execution_gate.status != "UNKNOWN"
            and economic_gate.status != "UNKNOWN"
        )
    readiness = (
        "READY"
        if (
            funding_gate.status == "PASS"
            and execution_gate.status == "PASS"
            and economic_gate.status == "PASS"
        )
        else "NOT_READY"
    )
    return ReadinessBreakdown(
        data_complete=bool(data_complete),
        funding_gate=funding_gate,
        execution_gate=execution_gate,
        economic_gate=economic_gate,
        protection_status=protection_status,
        readiness=readiness,
    )


# ---------------------------------------------------------------------------
# Distribution report (B8.4 evidence, offline fixture only)
# ---------------------------------------------------------------------------

def build_fcs_distribution_report(
    results: Sequence[FCSResult],
    *,
    source: str,
    dump_sha256: str,
    fcs_version: str = FCS_VERSION_V2,
) -> dict[str, Any]:
    """Offline B8.4 distribution evidence (no network, no yield optimisation).

    Records ``source`` + ``dump_sha256`` of the frozen input dump, per-bin
    counts, quantiles, histogram and NOT_READY reason distribution. Never
    tunes thresholds from observed returns.
    """
    scored = [float(r.fcs) for r in results if r.fcs is not None]
    scored.sort()
    n = len(results)
    n_scored = len(scored)

    def _quantile(q: float) -> float | None:
        if not scored:
            return None
        pos = (len(scored) - 1) * q
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return scored[lo]
        frac = pos - lo
        return scored[lo] * (1 - frac) + scored[hi] * frac

    edges = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    histogram: list[dict[str, Any]] = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        if hi == 100:
            cnt = sum(1 for v in scored if lo <= v <= hi)
        else:
            cnt = sum(1 for v in scored if lo <= v < hi)
        histogram.append({"lo": lo, "hi": hi, "n": cnt})
    reason_dist: dict[str, int] = {}
    class_dist: dict[str, int] = {}
    for r in results:
        for reason in (r.reasons or ()):
            reason_dist[str(reason)] = reason_dist.get(str(reason), 0) + 1
        hist_class = None
        try:
            hist_class = (r.risk or {}).get("history_class") or (r.funding_metrics or {}).get("history_class")
        except Exception:
            hist_class = None
        hist_class = str(hist_class or "UNKNOWN")
        class_dist[hist_class] = class_dist.get(hist_class, 0) + 1
    return {
        "source": str(source),
        "dump_sha256": str(dump_sha256),
        "fcs_version": str(fcs_version),
        "n": int(n),
        "n_scored": int(n_scored),
        "n_null": int(n - n_scored),
        "min": scored[0] if scored else None,
        "max": scored[-1] if scored else None,
        "p25": _quantile(0.25),
        "p50": _quantile(0.50),
        "p75": _quantile(0.75),
        "histogram": histogram,
        "reason_distribution": dict(reason_dist),
        "history_class_distribution": dict(class_dist),
        "note": "distribution evidence only; thresholds are fixed defaults, not yield-optimised",
    }
