"""Forward grader for 7D/30D/90D short outcomes (Task 16, design section 21).

Contract (design sections 21.1-21.3, plan Task 16):

- ``grade(score_snapshot_id, horizon, as_of_ms) -> OutcomeRecord`` with
  ``outcome_status`` in ``PENDING / COMPLETE / CENSORED / UNAVAILABLE``.
- Entry is the open of the first *complete* 1h bar strictly after the score's
  ``as_of_ms``; exit is the open of the first *complete* 1h bar strictly after
  ``as_of_ms + horizon``. "Complete" means the bar's close is at or before the
  grading ``as_of_ms`` -- a bar that is still repainting can never be an
  entry/exit price, and missing bars are never guessed forward.
- ``price_short_return = 1 - exit_price / entry_price``.
- ``funding_carry = SUM(rate * mark / entry_price)`` over settlement events
  with ``entry_ts < funding_time <= exit_ts``. A settlement event whose
  ``mark_price`` is missing/non-positive is never patched with a ticker,
  a neighbouring K-line or the rate itself: that event voids the carry,
  ``funding_carry``/``net_short_return`` stay null (price return is kept
  independently), the reason is ``FUNDING_MARK_MISSING``, a live contract
  reports ``UNAVAILABLE`` and a delisted contract stays ``CENSORED``.
- ``net_short_return = price + carry - fee - slippage`` with the default
  research assumptions ``fee = 0.0005 + 0.0005 = 0.001`` (both sides) and
  ``slippage = 0.001 + 0.001 = 0.002`` (both sides), total ``0.003``. The fee,
  slippage and ``cost_config_hash`` are stored on every outcome; changing any
  cost only writes another outcome row (the table key includes the cost hash),
  never overwrites history.
- ``MAE / MFE`` use only fully-contained closed 1h bars with
  ``open_ms >= entry_ts`` and ``close <= exit_ts`` (R14b/D14.2: straddling
  head/tail hours never contribute their full-hour extremes; without finer
  MARK the path is PARTIAL).
- Before the horizon is due the outcome is ``PENDING``. After it is due,
  missing bars give ``UNAVAILABLE`` and a delisted/expired contract gives
  ``CENSORED`` with the last evidenced tradable time/price. A missing or
  unreliable exit price is never ``COMPLETE``.
- Point-in-time: the grader reads only the archived score snapshot (plus its
  referenced feature snapshot for audit) and market data *after* the score
  time. It never imports or calls ``scan/symbol_builder.build_symbol`` --
  that helper still reads present-day OI/ratio/funding and must not backfill
  historical entries (plan review focus 2).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Awaitable, Callable, Mapping

HOUR_MS = 3_600_000
DAY_MS = 86_400_000

HORIZON_MS: dict[str, int] = {
    "7D": 7 * DAY_MS,
    "30D": 30 * DAY_MS,
    "90D": 90 * DAY_MS,
}

FORMULA_VERSION = "forward-v1"

DEFAULT_FEE_ASSUMPTION = 0.001
DEFAULT_SLIPPAGE_ASSUMPTION = 0.002
DEFAULT_TOTAL_COST = DEFAULT_FEE_ASSUMPTION + DEFAULT_SLIPPAGE_ASSUMPTION

OUTCOME_STATUSES = ("PENDING", "COMPLETE", "CENSORED", "UNAVAILABLE")

PENDING_NOT_DUE = "PENDING_NOT_DUE"
NOT_GRADED = "NOT_GRADED"
#: Due but never graded (F07/A8): metrics reports it as UNAVAILABLE with this
#: reason and a queue depth; run_due grades the missing row on its next pass.
NOT_GRADED_DUE = "NOT_GRADED_DUE"
NO_ENTRY_BAR = "NO_ENTRY_BAR"
NO_EXIT_BAR = "NO_EXIT_BAR"
EXIT_BAR_INCOMPLETE = "EXIT_BAR_INCOMPLETE"
FUNDING_MARK_MISSING = "FUNDING_MARK_MISSING"
FUNDING_HISTORY_INCOMPLETE = "FUNDING_HISTORY_INCOMPLETE"
CONTRACT_DELISTED = "CONTRACT_DELISTED"
BAD_ENTRY_PRICE = "BAD_ENTRY_PRICE"
NO_EXIT_PRICE = "NO_EXIT_PRICE"

_DELISTED_STATUSES = frozenset(
    {
        "DELISTED",
        "DELISTING",
        "SETTLED",
        "SETTLING",
        "EXPIRED",
        "DELIVERED",
        "CLOSED",
        "TERMINATED",
    }
)

_DEFAULT_EVIDENCE_COST = {
    "entry_fee": 0.0005,
    "exit_fee": 0.0005,
    "entry_slippage": 0.001,
    "exit_slippage": 0.001,
}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def default_cost_hash() -> str:
    """Cost hash for the default research assumptions (no config loaded)."""
    payload = _canonical_json({"evidence_cost": dict(_DEFAULT_EVIDENCE_COST)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _override_cost_hash(fee: float, slippage: float) -> str:
    payload = _canonical_json(
        {"fee_assumption": float(fee), "slippage_assumption": float(slippage)}
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_cost(
    config: Any | None,
    fee_assumption: float | None,
    slippage_assumption: float | None,
    cost_config_hash: str | None,
) -> tuple[float, float, str]:
    """Resolve (fee, slippage, cost_hash) for one grading call.

    Default path derives both legs from ``config.evidence_cost`` and hashes
    with ``config.cost_config_hash`` (design section 24). Any explicit
    override (fee, slippage or hash) leaves the default path and mints a
    distinct hash so the new outcome coexists with -- never overwrites --
    the default-cost row.
    """
    if (
        fee_assumption is not None
        or slippage_assumption is not None
        or cost_config_hash is not None
    ):
        fee = DEFAULT_FEE_ASSUMPTION if fee_assumption is None else float(fee_assumption)
        slip = (
            DEFAULT_SLIPPAGE_ASSUMPTION
            if slippage_assumption is None
            else float(slippage_assumption)
        )
        digest = cost_config_hash or _override_cost_hash(fee, slip)
        return fee, slip, str(digest)
    if config is not None:
        try:
            from diveintocrypto_desktop.shortlab.config import (
                cost_config_hash as _cost_hash,
            )

            evidence_cost = config.evidence_cost
            fee = float(evidence_cost.entry_fee) + float(evidence_cost.exit_fee)
            slip = float(evidence_cost.entry_slippage) + float(evidence_cost.exit_slippage)
            return fee, slip, str(_cost_hash(config))
        except Exception:
            pass
    return DEFAULT_FEE_ASSUMPTION, DEFAULT_SLIPPAGE_ASSUMPTION, default_cost_hash()


def settle_fees_usd(entry_price: Any, exit_price: Any, qty: Any, fee_rate: Any) -> dict[str, str]:
    """R14b exit-follows-exit fee helper (FX1, no VWAP).

    Delegates to the frozen ``evaluation.settle_fees_usd`` so the directional
    and hedge paths share one notional-based definition: entry uses entry
    notional, exit uses exit notional.
    """
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        settle_fees_usd as _settle,
    )

    return _settle(entry_price, exit_price, qty, fee_rate)


def horizon_due_ms(score_as_of_ms: int, horizon: str) -> int:
    """Maturity instant for one score snapshot and horizon."""
    if horizon not in HORIZON_MS:
        raise ValueError(f"horizon={horizon!r} must be one of {sorted(HORIZON_MS)}")
    return int(score_as_of_ms) + HORIZON_MS[horizon]


# ---------------------------------------------------------------------------
# Market-data normalisation (production DTOs use mixed units)
# ---------------------------------------------------------------------------


def _bar_open_ms(raw_t: Any) -> int | None:
    try:
        moment = int(raw_t)
    except (TypeError, ValueError):
        return None
    if moment > 10**17:  # nanoseconds (production klines carry ns ``t``)
        return moment // 1_000_000
    if moment > 10**14:  # microseconds (defensive; never observed)
        return moment // 1_000
    return moment


def _normalize_bars(bars: Any) -> list[dict[str, Any]]:
    """Keep ``{open_ms, o, h, l, c}`` with finite prices; drop the rest."""
    out: list[dict[str, Any]] = []
    for bar in bars or []:
        if not isinstance(bar, Mapping):
            continue
        open_ms = _bar_open_ms(bar.get("t", bar.get("open_time_ms", bar.get("open_ms"))))
        if open_ms is None:
            continue
        row = {
            "open_ms": open_ms,
            "o": _finite(bar.get("o", bar.get("open"))),
            "h": _finite(bar.get("h", bar.get("high"))),
            "l": _finite(bar.get("l", bar.get("low"))),
            "c": _finite(bar.get("c", bar.get("close"))),
        }
        out.append(row)
    out.sort(key=lambda row: row["open_ms"])
    return out


def _first_bar_after(
    bars: list[dict[str, Any]], anchor_ms: int, as_of_ms: int
) -> dict[str, Any] | None:
    """First bar with ``open_ms > anchor_ms`` that is complete at ``as_of_ms``."""
    for bar in bars:
        if bar["open_ms"] > anchor_ms and bar["open_ms"] + HOUR_MS <= as_of_ms:
            return bar
    return None


def _normalize_funding(events: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, Mapping):
            continue
        raw_t = event.get("t", event.get("funding_time_ms", event.get("fundingTime")))
        try:
            moment = int(raw_t)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if moment > 10**17:  # defensive: accept ns as well
            moment //= 1_000_000
        rate = _finite(event.get("funding_rate", event.get("fundingRate")))
        if rate is None:
            continue
        raw_mark = event.get("mark_price", event.get("markPrice"))
        mark = _finite(raw_mark)
        out.append({"t": moment, "funding_rate": rate, "mark_price": mark})
    out.sort(key=lambda row: row["t"])
    return out


# ---------------------------------------------------------------------------
# Lifecycle (delisting) view -- explicit evidence only, never a guess
# ---------------------------------------------------------------------------


def _field(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            try:
                return getattr(obj, name)
            except Exception:  # noqa: BLE001 - defensive accessor
                continue
    return None


def interpret_lifecycle(info: Any) -> dict[str, Any]:
    """Normalise a lifecycle probe to ``{delisted, last_tradable_ms, ...}``.

    Only explicit evidence counts as delisted: an ``is_delisted``/``delisted``
    flag, or a terminal ``status`` (``DELISTED``/``SETTLING``/``EXPIRED`` ...
    -- never ``HALT``/``BREAK``/``None``, which stay tradable-or-unknown).
    A symbol that merely vanished from the live universe without a verified
    terminal state is *not* delisted here (design section 10.2).
    """
    if info is None:
        return {"delisted": False}
    flag = _field(info, "is_delisted", "delisted")
    status = _field(info, "status", "exchange_status")
    delisted = bool(flag) or (
        isinstance(status, str) and status.upper() in _DELISTED_STATUSES
    )
    if not delisted:
        return {"delisted": False, "status": status}
    last_tradable = _field(
        info, "last_tradable_ms", "last_tradable", "delist_ms", "delivery_at_ms"
    )
    try:
        last_tradable_ms = int(last_tradable) if last_tradable is not None else None
    except (TypeError, ValueError):
        last_tradable_ms = None
    return {
        "delisted": True,
        "status": status,
        "last_tradable_ms": last_tradable_ms,
        "last_price": _finite(
            _field(info, "last_price", "last_tradable_price", "last_tradable_open")
        ),
        "delivery_at_ms": _finite(_field(info, "delivery_at_ms")) is not None
        and _field(info, "delivery_at_ms")
        or _field(info, "delivery_at_ms"),
    }


# ---------------------------------------------------------------------------
# Default production fetchers (tests inject fakes; never called offline)
# ---------------------------------------------------------------------------


async def _default_klines_fn(
    symbol: str, interval: str, start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    from diveintocrypto_desktop.data import binance_klines as _klines

    return await _klines.fetch_klines_range(
        symbol, interval, int(start_ms), int(end_ms)
    )


async def _default_funding_fn(
    symbol: str, start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    from diveintocrypto_desktop.data import funding as _funding

    return await _funding.funding_history_range(symbol, int(start_ms), int(end_ms))


def _funding_coverage(
    window: list[dict[str, Any]], start_ms: int, end_ms: int
) -> tuple[bool, float, str | None]:
    """Completeness of the holding-window funding leg (design section 10.1)."""
    try:
        from diveintocrypto_desktop.data.funding import (
            FUNDING_HISTORY_INCOMPLETE,
            funding_coverage as _coverage,
        )

        coverage = _coverage(list(window), int(start_ms), int(end_ms), None)
        return (
            bool(coverage.complete),
            float(coverage.coverage_fraction),
            coverage.reason_code,
        )
    except Exception:  # noqa: BLE001 - offline fallback mirrors the rule
        times = sorted(event["t"] for event in window)
        if len(times) < 3:
            return False, 0.0, "FUNDING_HISTORY_INCOMPLETE"
        gap = 24 * 3_600_000
        if times[0] - start_ms > gap or end_ms - times[-1] > gap:
            return False, 0.0, "FUNDING_HISTORY_INCOMPLETE"
        if any(b - a > gap for a, b in zip(times, times[1:])):
            return False, 0.0, "FUNDING_HISTORY_INCOMPLETE"
        return True, 1.0, None


def _mae_mfe(
    bars: list[dict[str, Any]],
    entry_ts_ms: int,
    exit_ts_ms: int,
    entry_price: float,
) -> tuple[float | None, float | None]:
    """Short MAE/MFE over fully-contained bars (R14b/D14.2).

    Only bars with ``open_ms >= entry_ts`` and ``close <= exit_ts`` count.
    A straddling head/tail hour never contributes its full-hour extremes;
    without finer MARK the path is PARTIAL (see ``evaluation.path_coverage``).
    """
    worst_high: float | None = None
    best_low: float | None = None
    for bar in bars:
        try:
            open_ms = int(bar["open_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        close_ms = open_ms + HOUR_MS
        if not (open_ms >= int(entry_ts_ms) and close_ms <= int(exit_ts_ms)):
            continue
        high, low = bar["h"], bar["l"]
        if high is not None and (worst_high is None or high > worst_high):
            worst_high = high
        if low is not None and (best_low is None or low < best_low):
            best_low = low
    if worst_high is None or best_low is None:
        return None, None
    mae = max(0.0, (worst_high - entry_price) / entry_price)
    mfe = max(0.0, (entry_price - best_low) / entry_price)
    return mae, mfe


def complete_bars_in_window(
    bars: list[dict[str, Any]],
    entry_ts_ms: int,
    exit_ts_ms: int,
) -> list[dict[str, Any]]:
    """R14b helper: fully-contained bars (open >= entry, close <= exit)."""
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        filter_complete_bars as _filter,
    )

    return list(_filter(bars, entry_ts_ms, exit_ts_ms))


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


async def grade(
    score_snapshot_id: str,
    horizon: str,
    as_of_ms: int,
    *,
    repository: Any,
    klines_fn: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    funding_fn: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    lifecycle_fn: Callable[[str], Awaitable[Any] | Any] | None = None,
    config: Any | None = None,
    fee_assumption: float | None = None,
    slippage_assumption: float | None = None,
    cost_config_hash: str | None = None,
    clock: Callable[[], int] | None = None,
) -> Any:
    """Grade one archived score snapshot for one horizon.

    Reads only the stored score (and its feature snapshot for audit) -- never
    live provider state and never the legacy per-symbol builder with a
    historical end_ms. Persists the
    :class:`OutcomeRecord` through Task 2's ``save_outcome`` (same content is
    idempotent; a changed cost mints a new ``cost_config_hash`` row) and
    returns it.
    """
    from diveintocrypto_desktop.shortlab.repository import (
        OutcomeRecord,
        ReferenceNotFoundError,
    )

    if horizon not in HORIZON_MS:
        raise ValueError(f"horizon={horizon!r} must be one of {sorted(HORIZON_MS)}")
    as_of_ms = int(as_of_ms)
    score = await repository.get_score(score_snapshot_id)
    if score is None:
        raise ReferenceNotFoundError(
            f"score snapshot {score_snapshot_id!r} not found"
        )
    symbol = str(score.symbol)
    score_as_of = int(score.as_of_ms)
    due_ms = horizon_due_ms(score_as_of, horizon)
    fee, slip, cost_hash = resolve_cost(
        config, fee_assumption, slippage_assumption, cost_config_hash
    )
    graded_at = int(clock()) if clock is not None else as_of_ms

    def _store(**overrides: Any) -> Any:
        base: dict[str, Any] = {
            "score_snapshot_id": score_snapshot_id,
            "horizon": horizon,
            "outcome_status": "PENDING",
            "reason_code": PENDING_NOT_DUE,
            "entry_ts_ms": None,
            "exit_ts_ms": None,
            "horizon_due_ms": due_ms,
            "formula_version": FORMULA_VERSION,
            "cost_config_hash": cost_hash,
            "funding_event_count": None,
            "funding_coverage": None,
            "graded_at_ms": graded_at,
            "entry_price": None,
            "exit_price": None,
            "price_short_return": None,
            "funding_carry": None,
            "fee_assumption": fee,
            "slippage_assumption": slip,
            "net_short_return": None,
            "mae": None,
            "mfe": None,
        }
        base.update(overrides)
        return OutcomeRecord(**base)

    async def _persist(record: Any) -> Any:
        await repository.save_outcome(record)
        return record

    # Idempotent re-grade: the outcome key (score, horizon, formula, cost)
    # is write-once (F01 immutability). A stored row is returned as-is so a
    # retried run_due batch never raises SnapshotImmutableError. In
    # particular run_due never mints PENDING rows (see below), so a stored
    # PENDING row can only come from an explicit pre-maturity grade and is
    # left for the metrics queue count instead of being overwritten here.
    try:
        existing = await repository.get_outcome(
            score_snapshot_id, horizon, FORMULA_VERSION, cost_hash
        )
    except Exception:  # noqa: BLE001 - a failed read never blocks grading
        existing = None
    if existing is not None:
        return existing

    # Best-effort audit read of the point-in-time feature snapshot. It proves
    # the score existed with archived inputs; grading never falls back to
    # live data when it is absent.
    try:
        await repository.get_feature(score.feature_snapshot_id)
    except Exception:  # noqa: BLE001 - audit only, never blocks grading
        pass

    lifecycle = {"delisted": False}
    if lifecycle_fn is not None:
        try:
            probed = lifecycle_fn(symbol)
            if hasattr(probed, "__await__"):
                probed = await probed
            lifecycle = interpret_lifecycle(probed)
        except Exception:  # noqa: BLE001 - unknown lifecycle degrades to live
            lifecycle = {"delisted": False}
    last_tradable = lifecycle.get("last_tradable_ms")
    delisted_before_due = bool(
        lifecycle.get("delisted")
        and isinstance(last_tradable, int)
        and last_tradable <= as_of_ms
        and last_tradable < due_ms
    )

    if as_of_ms < due_ms and not delisted_before_due:
        # F07/A8: PENDING is virtual until maturity. It is returned without a
        # DB write so a later terminal grade for the same key never hits the
        # F01 write-once guard; metrics counts missing rows as PENDING and
        # run_due only grades once due.
        return _store()

    klines = klines_fn or _default_klines_fn
    funding = funding_fn or _default_funding_fn
    try:
        raw_bars = await klines(
            symbol, "1h", score_as_of, max(as_of_ms, due_ms) + HOUR_MS
        )
    except Exception:  # noqa: BLE001 - fetcher failure is UNAVAILABLE, not fatal
        raw_bars = []
    bars = _normalize_bars(raw_bars)

    entry_bar = _first_bar_after(bars, score_as_of, as_of_ms)
    if entry_bar is None or entry_bar["o"] is None or entry_bar["o"] <= 0:
        if lifecycle.get("delisted"):
            return await _persist(
                _store(outcome_status="CENSORED", reason_code=CONTRACT_DELISTED)
            )
        return await _persist(
            _store(outcome_status="UNAVAILABLE", reason_code=NO_ENTRY_BAR)
        )
    entry_ts = int(entry_bar["open_ms"])
    entry_price = float(entry_bar["o"])

    exit_bar = _first_bar_after(bars, due_ms, as_of_ms)
    censored_exit: dict[str, Any] | None = None
    if delisted_before_due or (
        lifecycle.get("delisted")
        and isinstance(last_tradable, int)
        and last_tradable <= as_of_ms
        and (exit_bar is None or last_tradable < int(exit_bar["open_ms"]))
    ):
        censored_exit = _resolve_censored_exit(bars, lifecycle, as_of_ms)
        if censored_exit is None:
            return await _persist(
                _store(
                    outcome_status="CENSORED",
                    reason_code=CONTRACT_DELISTED,
                    entry_ts_ms=entry_ts,
                    entry_price=entry_price,
                )
            )
        return await _finish_with_exit(
            _store,
            _persist,
            bars,
            funding,
            symbol,
            entry_ts,
            entry_price,
            int(censored_exit["exit_ts_ms"]),
            censored_exit["exit_price"],
            fee,
            slip,
            censored=True,
        )

    if exit_bar is None:
        # Due but the first post-due bar is still incomplete: it exists in
        # wall-clock time yet has no trustworthy open. It is a missing exit,
        # not a pending horizon.
        incomplete_exists = any(bar["open_ms"] > due_ms for bar in bars)
        return await _persist(
            _store(
                outcome_status="UNAVAILABLE",
                reason_code=EXIT_BAR_INCOMPLETE if incomplete_exists else NO_EXIT_BAR,
                entry_ts_ms=entry_ts,
                entry_price=entry_price,
            )
        )
    exit_ts = int(exit_bar["open_ms"])
    exit_price = exit_bar["o"]
    if exit_price is None or exit_price <= 0:
        return await _persist(
            _store(
                outcome_status="UNAVAILABLE",
                reason_code=NO_EXIT_PRICE,
                entry_ts_ms=entry_ts,
                entry_price=entry_price,
                exit_ts_ms=exit_ts,
            )
        )
    return await _finish_with_exit(
        _store,
        _persist,
        bars,
        funding,
        symbol,
        entry_ts,
        entry_price,
        exit_ts,
        float(exit_price),
        fee,
        slip,
        censored=False,
    )


def _resolve_censored_exit(
    bars: list[dict[str, Any]], lifecycle: Mapping[str, Any], as_of_ms: int
) -> dict[str, Any] | None:
    """Last evidenced tradable time/price for a delisted contract."""
    last_tradable = lifecycle.get("last_tradable_ms")
    if not isinstance(last_tradable, int):
        return None
    last_price = _finite(lifecycle.get("last_price"))
    if last_price is not None and last_price > 0:
        return {"exit_ts_ms": last_tradable, "exit_price": float(last_price)}
    # Fall back to the last complete bar at or before the delisting instant.
    candidate: dict[str, Any] | None = None
    for bar in bars:
        if bar["open_ms"] <= last_tradable and bar["open_ms"] + HOUR_MS <= as_of_ms:
            candidate = bar
    if candidate is None or candidate["o"] is None or candidate["o"] <= 0:
        return None
    return {"exit_ts_ms": int(candidate["open_ms"]), "exit_price": float(candidate["o"])}


async def _finish_with_exit(
    store: Callable[..., Any],
    persist: Callable[[Any], Awaitable[Any]],
    bars: list[dict[str, Any]],
    funding_fn: Callable[..., Awaitable[list[dict[str, Any]]]],
    symbol: str,
    entry_ts: int,
    entry_price: float,
    exit_ts: int,
    exit_price: float,
    fee: float,
    slip: float,
    *,
    censored: bool,
) -> Any:
    """Price + funding + MAE/MFE settlement for a known (entry, exit] pair."""
    price_return = 1.0 - exit_price / entry_price
    try:
        raw_events = await funding_fn(symbol, entry_ts, exit_ts)
    except Exception:  # noqa: BLE001 - fetcher failure voids funding, keeps price
        raw_events = []
    window = [
        event
        for event in _normalize_funding(raw_events)
        if entry_ts < event["t"] <= exit_ts
    ]
    mae, mfe = _mae_mfe(bars, entry_ts, exit_ts, entry_price)
    common: dict[str, Any] = {
        "entry_ts_ms": entry_ts,
        "exit_ts_ms": exit_ts,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "price_short_return": price_return,
        "funding_event_count": len(window),
        "mae": mae,
        "mfe": mfe,
    }
    bad_marks = [
        event
        for event in window
        if event["mark_price"] is None or event["mark_price"] <= 0
    ]
    if bad_marks:
        complete, fraction, _ = _funding_coverage(window, entry_ts, exit_ts)
        _ = complete
        # R14b/D14.2: missing Mark lowers priced funding coverage (never 0-fill).
        priced = ((len(window) - len(bad_marks)) / len(window)) if window else 0.0
        priced_coverage = min(float(fraction), float(priced))
        return await persist(
            store(
                **{
                    **common,
                    "outcome_status": "CENSORED" if censored else "UNAVAILABLE",
                    "reason_code": FUNDING_MARK_MISSING,
                    "funding_coverage": float(priced_coverage),
                    "funding_carry": None,
                    "net_short_return": None,
                }
            )
        )
    complete, fraction, coverage_reason = _funding_coverage(window, entry_ts, exit_ts)
    if not complete:
        if censored:
            return await persist(
                store(
                    **{
                        **common,
                        "outcome_status": "CENSORED",
                        "reason_code": CONTRACT_DELISTED,
                        "funding_coverage": float(fraction),
                        "funding_carry": None,
                        "net_short_return": None,
                    }
                )
            )
        return await persist(
            store(
                **{
                    **common,
                    "outcome_status": "UNAVAILABLE",
                    "reason_code": coverage_reason or FUNDING_HISTORY_INCOMPLETE,
                    "funding_coverage": float(fraction),
                    "funding_carry": None,
                    "net_short_return": None,
                }
            )
        )
    carry = sum(
        event["funding_rate"] * event["mark_price"] / entry_price for event in window
    )
    net = price_return + carry - fee - slip
    return await persist(
        store(
            **{
                **common,
                "outcome_status": "CENSORED" if censored else "COMPLETE",
                "reason_code": CONTRACT_DELISTED if censored else None,
                "funding_coverage": float(fraction),
                "funding_carry": float(carry),
                "net_short_return": float(net),
            }
        )
    )
