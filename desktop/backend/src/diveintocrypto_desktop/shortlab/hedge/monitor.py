"""H07 active-plan monitor: cached PnL, basis, funding carry and liq distance.

Design: B22 (liquidation), B23 (basis), B24 (exit liquidity), B25 (PnL),
B28.5/B28.8 (snapshot shape + persist cadence), B30 (10s memory tick, 60s or
risk-change persist, queue coalescing), B31/B38.1 (freshness + async skew).

Pure in-memory maths only: no network, no DB reads, no sleeps. Callers pass
the H06 :class:`HedgePosition` mirror, an in-memory ``market_cache`` and the
settled public funding events. The 10s tick calls :func:`compute_monitor`;
persistence (60s or risk change, latest-only queue) is decided by
:func:`should_persist` and executed by the H08 worker -- this module never
touches DuckDB.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping, Sequence

from diveintocrypto_desktop.shortlab.hedge.models import HedgeMonitor

__all__ = [
    "MONITOR_TICK_SEC",
    "MONITOR_PERSIST_SEC",
    "PERSIST_LAG_SEC",
    "FRESH_SKEW_SEC",
    "REFERENCE_SKEW_SEC",
    "HIGH_FREQUENCY_STATUSES",
    "MonitorError",
    "compute_monitor",
    "should_persist",
    "risk_key",
    "is_high_frequency_status",
    "basis_readiness_for_new_plan",
]

#: In-memory compute cadence (B30): every 10s from the memory mirror.
MONITOR_TICK_SEC = 10
#: Persist cadence (B28.8/B30): every 60s or on risk-state change.
MONITOR_PERSIST_SEC = 60
#: Persist-queue lag that marks MONITOR_DEGRADED without touching positions.
PERSIST_LAG_SEC = 5
#: Fresh basis skew for a new READY simulation (B38.1): <= 5s.
FRESH_SKEW_SEC = 5
#: 5..30s is reference-only (BASIS_ASYNC_STALE); > 30s basis is null.
REFERENCE_SKEW_SEC = 30

#: Only these持仓 states get the high-frequency 10s monitor (B30).
HIGH_FREQUENCY_STATUSES = frozenset({"PARTIALLY_FILLED", "ACTIVE", "CLOSING"})


class MonitorError(ValueError):
    """Structural monitor input error (caller-side, HTTP 422 semantics)."""


# ---------------------------------------------------------------------------
# Decimal + policy helpers (ledger boundary: never float for money).
# ---------------------------------------------------------------------------


def _parse_opt_decimal(name: str, value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool):
        raise MonitorError(f"{name} must be a decimal string, got {value!r}")
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise MonitorError(f"{name} is not a decimal string: {value!r}") from exc
    else:
        raise MonitorError(
            f"{name} must be a decimal string (float rejected), got {value!r}"
        )
    if not parsed.is_finite():
        raise MonitorError(f"{name} must be finite, got {value!r}")
    return parsed


def _fmt(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(value)
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


def _hedge_subtree(policy: Any) -> dict[str, Any]:
    """Extract the ``hedge`` subtree from ShortLabConfig / dict / None."""
    if policy is None:
        return {}
    if isinstance(policy, Mapping):
        if "hedge" in policy and isinstance(policy["hedge"], Mapping):
            return dict(policy["hedge"])
        return dict(policy)
    hedge = getattr(policy, "hedge", None)
    if hedge is None:
        return {}
    if isinstance(hedge, Mapping):
        return dict(hedge)
    out: dict[str, Any] = {}
    for key in (
        "liquidation",
        "basis",
        "liquidity_monitor",
        "freshness",
        "refresh",
        "runtime",
    ):
        node = getattr(hedge, key, None)
        if node is None:
            continue
        out[key] = dict(node) if isinstance(node, Mapping) else node
    return out


def _num(node: Any, key: str, default: float) -> float:
    if isinstance(node, Mapping) and key in node:
        try:
            return float(node[key])
        except (TypeError, ValueError):
            return default
    return default


def _int(node: Any, key: str, default: int) -> int:
    if isinstance(node, Mapping) and key in node:
        try:
            return int(node[key])
        except (TypeError, ValueError):
            return default
    return default


def _freshness_window(hedge: Mapping[str, Any], group: str) -> tuple[int, int]:
    fresh = hedge.get("freshness") if isinstance(hedge.get("freshness"), Mapping) else {}
    assert isinstance(fresh, Mapping)
    window = fresh.get(group)
    defaults = {
        "futures_mark": (20, 60),
        "spot_quote": (20, 60),
        "spot_depth": (60, 180),
        "alpha_quote": (20, 60),
        "funding_current": (120, 300),
        "onchain_quote": (90, 180),
        "contract_status": (900, 1800),
    }
    ttl_d, grace_d = defaults.get(group, (20, 60))
    if isinstance(window, Mapping):
        try:
            ttl = int(window.get("ttl", ttl_d))
        except (TypeError, ValueError):
            ttl = ttl_d
        try:
            grace = int(window.get("grace", grace_d))
        except (TypeError, ValueError):
            grace = grace_d
        return (ttl, grace)
    ttl_obj = getattr(window, "ttl", None) if window is not None else None
    grace_obj = getattr(window, "grace", None) if window is not None else None
    try:
        ttl = int(ttl_obj) if ttl_obj is not None else ttl_d
    except (TypeError, ValueError):
        ttl = ttl_d
    try:
        grace = int(grace_obj) if grace_obj is not None else grace_d
    except (TypeError, ValueError):
        grace = grace_d
    return (ttl, grace)


# ---------------------------------------------------------------------------
# Input normalisation (frozen DTOs or plain test mappings).
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    out: dict[str, Any] = {}
    for key in (
        "price",
        "mark_price",
        "mid_price",
        "buy_vwap",
        "sell_vwap",
        "quote_currency",
        "quote_to_usd",
        "as_of_ms",
        "source_timestamp_ms",
        "fetched_at_ms",
        "expires_at_ms",
        "status",
        "sell_executable_qty",
        "buy_executable_qty",
        "sell_slippage_bps",
        "buy_slippage_bps",
        "known_cost_usd",
        "estimated_exit_cost_usd",
        "projected_next_funding_usd",
        "contract_status",
        "realized_spot_pnl_usd",
        "realized_futures_pnl_usd",
        "persist_lag_ms",
        "actual_funding_receipts_usd",
        "entry",
    ):
        if hasattr(value, key):
            out[key] = getattr(value, key)
    # SpotVenueQuote-shaped doubles.
    for alias, target in (
        ("mid_price", "mid_price"),
        ("buy_vwap", "buy_vwap"),
        ("sell_vwap", "sell_vwap"),
        ("quote_currency", "quote_currency"),
        ("quote_to_usd", "quote_to_usd"),
        ("as_of_ms", "as_of_ms"),
        ("source_timestamp_ms", "source_timestamp_ms"),
        ("fetched_at_ms", "fetched_at_ms"),
        ("expires_at_ms", "expires_at_ms"),
        ("sell_executable_qty", "sell_executable_qty"),
        ("sell_slippage_bps", "sell_slippage_bps"),
    ):
        if alias not in out and hasattr(value, alias):
            out[alias] = getattr(value, alias)
    return out


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _leg_positions(
    positions: Any,
) -> dict[str, dict[str, Any]]:
    """Return per-leg ``{remaining, open, closed, entry}`` (Decimal or None)."""
    out: dict[str, dict[str, Any]] = {
        "FUTURES_SHORT": {"remaining": Decimal(0), "open": Decimal(0),
                          "closed": Decimal(0), "entry": None},
        "SPOT_LONG": {"remaining": Decimal(0), "open": Decimal(0),
                      "closed": Decimal(0), "entry": None},
    }
    seq = list(positions) if positions is not None else []
    for item in seq:
        if isinstance(item, Mapping):
            leg = str(item.get("leg_type", item.get("legType", "")))
            rem = _parse_opt_decimal("remaining_qty", item.get("remaining_qty"))
            opn = _parse_opt_decimal("open_qty", item.get("open_qty"))
            cls = _parse_opt_decimal("closed_qty", item.get("closed_qty"))
            entry = _parse_opt_decimal(
                "weighted_avg_price",
                item.get("weighted_avg_price", item.get("weightedAvgPrice")),
            )
        else:
            leg = str(getattr(item, "leg_type", ""))
            rem = _parse_opt_decimal("remaining_qty",
                                     getattr(item, "remaining_qty", "0"))
            opn = _parse_opt_decimal("open_qty", getattr(item, "open_qty", "0"))
            cls = _parse_opt_decimal("closed_qty",
                                     getattr(item, "closed_qty", "0"))
            entry = _parse_opt_decimal(
                "weighted_avg_price",
                getattr(item, "weighted_avg_price", None),
            )
        if leg not in out:
            continue
        out[leg]["remaining"] = rem if rem is not None else Decimal(0)
        out[leg]["open"] = opn if opn is not None else Decimal(0)
        out[leg]["closed"] = cls if cls is not None else Decimal(0)
        out[leg]["entry"] = entry
    return out


def _settled_field(event: Any, *names: str) -> Any:
    if isinstance(event, Mapping):
        for name in names:
            if name in event and event[name] is not None:
                return event[name]
        return None
    for name in names:
        if hasattr(event, name):
            value = getattr(event, name)
            if value is not None:
                return value
    return None


def is_high_frequency_status(status: str | None) -> bool:
    """Whether ``status`` gets the 10s high-frequency monitor (B30)."""
    return str(status or "") in HIGH_FREQUENCY_STATUSES


def basis_readiness_for_new_plan(
    futures_as_of_ms: int | None,
    spot_as_of_ms: int | None,
    *,
    policy: Any = None,
) -> tuple[bool, str, int | None]:
    """Gate a *new* READY simulation on basis source skew (B38.1).

    Returns ``(ready, reason, skew_sec)``. ``skew <= 5s`` may be READY;
    ``5..30s`` is reference-only (``BASIS_ASYNC_STALE``) and must not
    produce a new READY plan; ``> 30s`` or missing times are stale-null.
    """
    hedge = _hedge_subtree(policy)
    basis_cfg = hedge.get("basis") if isinstance(hedge.get("basis"), Mapping) else {}
    assert isinstance(basis_cfg, Mapping)
    fresh_max = _int(basis_cfg, "fresh_max_skew_sec", FRESH_SKEW_SEC)
    ref_max = _int(basis_cfg, "reference_max_skew_sec", REFERENCE_SKEW_SEC)
    if futures_as_of_ms is None or spot_as_of_ms is None:
        return (False, "BASIS_SKEW_UNKNOWN", None)
    skew = abs(int(futures_as_of_ms) - int(spot_as_of_ms)) // 1000
    if skew <= fresh_max:
        return (True, "BASIS_FRESH", skew)
    if skew <= ref_max:
        return (False, "BASIS_ASYNC_STALE", skew)
    return (False, "BASIS_STALE_NULL", skew)


# ---------------------------------------------------------------------------
# Funding carry (B25): per-event short_qty * native mark * rate,当时FX.
# ---------------------------------------------------------------------------


def _compute_funding_carry(
    settled_events: Any,
) -> tuple[str | None, str, int, int, int, list[str]]:
    """Return ``(total_or_None, known_subtotal, known, unknown, total, flags)``.

    Each event needs ``rate`` + ``mark_price`` + ``short_qty`` + ``fx_to_usd``;
    boundary-uncertain events and any missing key move the event to the
    unknown bucket. The complete total is ``None`` whenever the unknown
    bucket is non-empty; the known subtotal plus coverage stay available and
    no ``0`` is ever filled in.
    """
    seq = list(settled_events) if settled_events is not None else []
    # Stable time order; missing times sort first without blocking.
    def _t(ev: Any) -> int:
        raw = _settled_field(ev, "funding_time_ms", "fundingTime", "t")
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0
    seq = sorted(seq, key=_t)
    known_total = Decimal(0)
    known = 0
    unknown = 0
    for event in seq:
        rate = _parse_opt_decimal("rate", _settled_field(
            event, "rate", "fundingRate", "funding_rate"))
        mark = _parse_opt_decimal("mark_price", _settled_field(
            event, "mark_price", "markPrice"))
        qty = _parse_opt_decimal("short_qty", _settled_field(
            event, "short_qty", "shortQty", "short_native_qty",
            "shortNativeQty"))
        fx = _parse_opt_decimal("fx_to_usd", _settled_field(
            event, "fx_to_usd", "fxToUsd", "fx"))
        boundary = False
        if isinstance(event, Mapping):
            boundary = bool(event.get("boundary_uncertain",
                                      event.get("boundaryUncertain", False)))
        else:
            boundary = bool(getattr(event, "boundary_uncertain", False))
        if boundary or rate is None or mark is None or qty is None or fx is None:
            unknown += 1
            continue
        with localcontext() as ctx:
            ctx.prec = 80
            known_total += qty * mark * rate * fx
        known += 1
    total = known + unknown
    flags: list[str] = []
    if unknown:
        flags.append("FUNDING_COVERAGE_PARTIAL")
    if total and known == total:
        flags.append("FUNDING_COVERAGE_COMPLETE")
    if not total:
        flags.append("FUNDING_NO_EVENTS")
    total_str = None if unknown else _fmt(known_total)
    return (total_str, _fmt(known_total), known, unknown, total, flags)


# ---------------------------------------------------------------------------
# Main compute (10s in-memory, read-only).
# ---------------------------------------------------------------------------


def compute_monitor(
    plan: Any,
    positions: Any,
    market_cache: Any,
    settled_events: Any,
    policy: Any = None,
    now_ms: int = 0,
) -> HedgeMonitor:
    """Compute one in-memory monitor snapshot (H07.1, B25).

    Read-only over the H06 position mirror, ``market_cache`` and settled
    public funding events. Funding uses each event's *then* short quantity
    times the native mark times the rate converted with the *then* FX;
    any event missing rate/mark/qty/FX (or flagged boundary-uncertain)
    forces the complete ``estimated_settled_funding_usd`` to ``None``
    while ``metrics_json`` keeps the known subtotal plus coverage.
    ``basis_pnl_usd`` is already inside the two leg PnLs and is never
    added a second time; cross-currency legs record an independent
    ``fx_pnl_adjustment_usd`` and null the complete USD net when FX is
    missing. USDT/USDC are never assumed to equal 1 USD.
    """
    hedge = _hedge_subtree(policy)
    basis_cfg = hedge.get("basis") if isinstance(hedge.get("basis"), Mapping) else {}
    liq_cfg = hedge.get("liquidation") if isinstance(hedge.get("liquidation"), Mapping) else {}
    exit_cfg = hedge.get("liquidity_monitor") if isinstance(hedge.get("liquidity_monitor"), Mapping) else {}
    refresh_cfg = hedge.get("refresh") if isinstance(hedge.get("refresh"), Mapping) else {}
    runtime_cfg = hedge.get("runtime") if isinstance(hedge.get("runtime"), Mapping) else {}
    assert isinstance(basis_cfg, Mapping)
    assert isinstance(liq_cfg, Mapping)
    now = int(now_ms)

    plan_d = _as_dict(plan) if not isinstance(plan, Mapping) else dict(plan)
    # Plan fields accept both snake and camel spellings.
    def _plan(*names: str, default: Any = None) -> Any:
        for name in names:
            if name in plan_d and plan_d[name] is not None:
                return plan_d[name]
        if not isinstance(plan, Mapping) and plan is not None:
            for name in names:
                if hasattr(plan, name):
                    value = getattr(plan, name)
                    if value is not None:
                        return value
        return default

    plan_id = str(_plan("plan_id", "planId", default="plan-unknown"))
    target_ratio = _parse_opt_decimal(
        "target_hedge_ratio",
        _plan("target_hedge_ratio", "targetHedgeRatio", "target_ratio"),
    )
    liq_price = _parse_opt_decimal(
        "liquidation_price",
        _plan("liquidation_price", "liquidationPrice"),
    )
    liq_source = _plan("liquidation_price_source", "liquidationPriceSource",
                       default=None)
    liq_updated = _plan("liquidation_price_updated_at_ms",
                        "liquidationPriceUpdatedAtMs", default=None)
    try:
        liq_updated_ms = int(liq_updated) if liq_updated is not None else None
    except (TypeError, ValueError):
        liq_updated_ms = None
    plan_status = _plan("status", default=None)

    legs = _leg_positions(positions)
    fut_rem: Decimal = legs["FUTURES_SHORT"]["remaining"]
    spot_rem: Decimal = legs["SPOT_LONG"]["remaining"]
    fut_open: Decimal = legs["FUTURES_SHORT"]["open"]
    spot_open: Decimal = legs["SPOT_LONG"]["open"]
    fut_closed: Decimal = legs["FUTURES_SHORT"]["closed"]
    spot_closed: Decimal = legs["SPOT_LONG"]["closed"]

    cache = dict(market_cache) if isinstance(market_cache, Mapping) else {}
    fut_m = cache.get("futures_mark", cache.get("futuresMark", {}))
    spot_m = cache.get("spot_quote", cache.get("spotQuote", {}))
    fut_d = _as_dict(fut_m)
    spot_d = _as_dict(spot_m)
    entry_d = cache.get("entry", cache.get("entry_snapshot", {}))
    entry_d = dict(entry_d) if isinstance(entry_d, Mapping) else _as_dict(entry_d)

    def _m(d: dict[str, Any], *names: str) -> Any:
        for name in names:
            if name in d and d[name] is not None:
                return d[name]
        return None

    fut_mark = _parse_opt_decimal("futures_mark", _m(fut_d, "price", "mark_price",
                                                    "markPrice"))
    fut_ccy = _m(fut_d, "quote_currency", "quoteCurrency")
    fut_fx = _parse_opt_decimal("futures_fx", _m(fut_d, "quote_to_usd", "quoteToUsd"))
    fut_asof = _m(fut_d, "as_of_ms", "asOfMs", "source_timestamp_ms",
                   "sourceTimestampMs")
    try:
        fut_asof_ms = int(fut_asof) if fut_asof is not None else None
    except (TypeError, ValueError):
        fut_asof_ms = None
    fut_expires = _m(fut_d, "expires_at_ms", "expiresAtMs")
    try:
        fut_expires_ms = int(fut_expires) if fut_expires is not None else None
    except (TypeError, ValueError):
        fut_expires_ms = None

    spot_mark = _parse_opt_decimal("spot_price", _m(
        spot_d, "sell_vwap", "sellVwap", "mid_price", "midPrice", "price"))
    spot_ccy = _m(spot_d, "quote_currency", "quoteCurrency")
    spot_fx = _parse_opt_decimal("spot_fx", _m(spot_d, "quote_to_usd", "quoteToUsd"))
    spot_asof = _m(spot_d, "as_of_ms", "asOfMs", "source_timestamp_ms",
                    "sourceTimestampMs")
    try:
        spot_asof_ms = int(spot_asof) if spot_asof is not None else None
    except (TypeError, ValueError):
        spot_asof_ms = None
    spot_expires = _m(spot_d, "expires_at_ms", "expiresAtMs")
    try:
        spot_expires_ms = int(spot_expires) if spot_expires is not None else None
    except (TypeError, ValueError):
        spot_expires_ms = None

    # Entry prices: explicit entry snapshot wins, else position VWAP.
    fut_entry = _parse_opt_decimal("futures_entry", _m(
        entry_d, "futures_entry_price", "futuresEntryPrice"))
    if fut_entry is None:
        fut_entry = legs["FUTURES_SHORT"]["entry"]
    spot_entry = _parse_opt_decimal("spot_entry", _m(
        entry_d, "spot_entry_price", "spotEntryPrice"))
    if spot_entry is None:
        spot_entry = legs["SPOT_LONG"]["entry"]
    fut_entry_fx = _parse_opt_decimal("futures_entry_fx", _m(
        entry_d, "futures_entry_fx", "futuresEntryFx"))
    if fut_entry_fx is None:
        fut_entry_fx = fut_fx
    spot_entry_fx = _parse_opt_decimal("spot_entry_fx", _m(
        entry_d, "spot_entry_fx", "spotEntryFx"))
    if spot_entry_fx is None:
        spot_entry_fx = spot_fx
    fut_entry_ccy = _m(entry_d, "futures_entry_currency", "futuresEntryCurrency") or fut_ccy
    spot_entry_ccy = _m(entry_d, "spot_entry_currency", "spotEntryCurrency") or spot_ccy

    # -- freshness ---------------------------------------------------------
    fut_ttl, fut_grace = _freshness_window(hedge, "futures_mark")
    spot_ttl, spot_grace = _freshness_window(hedge, "spot_quote")
    metrics: dict[str, Any] = {}
    quality: dict[str, Any] = {}
    source_meta: dict[str, Any] = {"schema_version": "hedge-source-v1"}

    def _age(asof: int | None) -> int | None:
        return (now - asof) if asof is not None else None

    fut_age = _age(fut_asof_ms)
    spot_age = _age(spot_asof_ms)
    fut_stale = fut_age is None or fut_age > fut_ttl * 1000
    spot_stale = spot_age is None or spot_age > spot_ttl * 1000
    fut_expired = (
        fut_age is None
        or fut_age > fut_grace * 1000
        or (fut_expires_ms is not None and now > fut_expires_ms)
        or fut_mark is None
    )
    spot_expired = (
        spot_age is None
        or spot_age > spot_grace * 1000
        or (spot_expires_ms is not None and now > spot_expires_ms)
        or spot_mark is None
    )
    metrics["futures_mark_age_ms"] = fut_age
    metrics["spot_quote_age_ms"] = spot_age
    metrics["futures_mark_stale"] = bool(fut_stale)
    metrics["spot_quote_stale"] = bool(spot_stale)
    metrics["futures_mark_expired"] = bool(fut_expired)
    metrics["spot_quote_expired"] = bool(spot_expired)

    # Basis skew gate (B38.1): <=5s fresh, 5..30s reference, >30s null.
    ready, skew_reason, skew_sec = basis_readiness_for_new_plan(
        fut_asof_ms, spot_asof_ms, policy=policy)
    metrics["basis_skew_sec"] = skew_sec
    metrics["basis_status"] = skew_reason
    metrics["new_plan_ready_on_basis"] = bool(ready)
    if skew_reason == "BASIS_ASYNC_STALE":
        metrics["time_alignment_credit"] = 0
    source_meta["basis_skew_sec"] = skew_sec
    source_meta["basis_status"] = skew_reason

    # -- basis + leg PnL (canonical, same-currency strict) ------------------
    current_basis_pct: str | None = None
    basis_pnl_usd: str | None = None
    spot_pnl_usd: str | None = None
    futures_pnl_usd: str | None = None
    fx_adjustment_usd: str | None = None
    basis_flags: list[str] = []
    same_ccy = (
        fut_ccy is not None and spot_ccy is not None and fut_ccy == spot_ccy
        and fut_entry_ccy == spot_entry_ccy == fut_ccy
    )
    metrics["quote_currency_futures"] = fut_ccy
    metrics["quote_currency_spot"] = spot_ccy
    metrics["same_quote_currency"] = bool(same_ccy)

    if skew_reason == "BASIS_STALE_NULL" or fut_expired or spot_expired:
        basis_flags.append("BASIS_NULL_STALE")
    elif skew_reason == "BASIS_ASYNC_STALE":
        basis_flags.append("BASIS_ASYNC_STALE")
    else:
        basis_flags.append("BASIS_FRESH")

    with localcontext() as ctx:
        ctx.prec = 80
        # Current basis % (needs both marks + spot base).
        if (fut_mark is not None and spot_mark is not None and spot_mark != 0
                and skew_reason != "BASIS_STALE_NULL"
                and not fut_expired and not spot_expired):
            try:
                current_basis_pct = _fmt((fut_mark - spot_mark) / spot_mark)
            except (InvalidOperation, ValueError, ArithmeticError, ZeroDivisionError):
                current_basis_pct = None
        # Entry basis + matched basis PnL (settle first, then USD).
        basis_settle: Decimal | None = None
        matched = min(fut_rem, spot_rem)
        if (fut_entry is not None and spot_entry is not None
                and fut_mark is not None and spot_mark is not None
                and spot_entry != 0 and spot_mark is not None
                and matched > 0 and same_ccy
                and skew_reason != "BASIS_STALE_NULL"
                and not fut_expired and not spot_expired):
            try:
                entry_spread = fut_entry - spot_entry
                now_spread = fut_mark - spot_mark
                basis_settle = matched * (entry_spread - now_spread)
            except (InvalidOperation, ValueError, ArithmeticError):
                basis_settle = None
        elif matched > 0 and not same_ccy:
            basis_flags.append("BASIS_CROSS_CURRENCY_NO_SINGLE_SPREAD")

        # Per-leg unrealized (remaining only) in settle, then USD.
        fut_unreal_settle: Decimal | None = None
        spot_unreal_settle: Decimal | None = None
        if fut_entry is not None and fut_mark is not None and fut_rem > 0:
            try:
                fut_unreal_settle = fut_rem * (fut_entry - fut_mark)
            except (InvalidOperation, ValueError, ArithmeticError):
                fut_unreal_settle = None
        elif fut_rem == 0:
            fut_unreal_settle = Decimal(0)
        if spot_entry is not None and spot_mark is not None and spot_rem > 0:
            try:
                spot_unreal_settle = spot_rem * (spot_mark - spot_entry)
            except (InvalidOperation, ValueError, ArithmeticError):
                spot_unreal_settle = None
        elif spot_rem == 0:
            spot_unreal_settle = Decimal(0)

        # Realized: closed qty needs an explicit exit snapshot; without it
        # the complete leg PnL stays null (never invent 0).
        realized_spot = _parse_opt_decimal(
            "realized_spot", cache.get("realized_spot_pnl_usd"))
        realized_fut = _parse_opt_decimal(
            "realized_futures", cache.get("realized_futures_pnl_usd"))
        if spot_closed > 0 and realized_spot is None:
            metrics["realized_spot_unknown"] = True
        if fut_closed > 0 and realized_fut is None:
            metrics["realized_futures_unknown"] = True

        # Convert to USD with current FX (unrealized) -- any missing FX
        # nulls the complete leg, never 0.
        fut_unreal_usd: Decimal | None = None
        spot_unreal_usd: Decimal | None = None
        if fut_unreal_settle is not None:
            if fut_fx is None:
                metrics["futures_fx_missing"] = True
            else:
                fut_unreal_usd = fut_unreal_settle * fut_fx
        if spot_unreal_settle is not None:
            if spot_fx is None:
                metrics["spot_fx_missing"] = True
            else:
                spot_unreal_usd = spot_unreal_settle * spot_fx

        if (fut_unreal_usd is not None and (fut_closed == 0 or realized_fut is not None)):
            futures_pnl_usd = _fmt(fut_unreal_usd + (realized_fut or Decimal(0)))
        if (spot_unreal_usd is not None and (spot_closed == 0 or realized_spot is not None)):
            spot_pnl_usd = _fmt(spot_unreal_usd + (realized_spot or Decimal(0)))

        # Basis USD: convert settle spread with the (common) current FX.
        if basis_settle is not None:
            basis_fx = fut_fx if fut_fx is not None else spot_fx
            if basis_fx is None:
                metrics["basis_fx_missing"] = True
            else:
                basis_pnl_usd = _fmt(basis_settle * basis_fx)

        # FX residual (independent, never folded into basis).
        if fut_unreal_settle is not None and spot_unreal_settle is not None:
            if fut_fx is None or spot_fx is None:
                fx_adjustment_usd = None
                metrics["fx_residual"] = "UNKNOWN_FX_MISSING"
            elif not same_ccy or fut_fx != spot_fx:
                try:
                    # Residual of converting legs separately vs at fut FX.
                    separate = fut_unreal_settle * fut_fx + spot_unreal_settle * spot_fx
                    at_fut = (fut_unreal_settle + spot_unreal_settle) * fut_fx
                    fx_adjustment_usd = _fmt(separate - at_fut)
                    metrics["fx_residual"] = "CROSS_CURRENCY_SEPARATE"
                except (InvalidOperation, ValueError, ArithmeticError):
                    fx_adjustment_usd = None
            else:
                fx_adjustment_usd = _fmt(Decimal(0))
                metrics["fx_residual"] = "NONE_SAME_FX"
        metrics["fx_pnl_adjustment_usd"] = fx_adjustment_usd

    # Basis-vs-carry gates are evaluated in alerts.py; here we expose the
    # adverse loss and the fixed fallback thresholds for transparency.
    adverse_basis_loss: Decimal | None = None
    if basis_pnl_usd is not None:
        try:
            parsed = Decimal(basis_pnl_usd)
            adverse_basis_loss = -parsed if parsed < 0 else Decimal(0)
        except (InvalidOperation, ValueError, ArithmeticError):
            adverse_basis_loss = None
    metrics["adverse_basis_loss_usd"] = (
        _fmt(adverse_basis_loss) if adverse_basis_loss is not None else None)

    # -- funding carry ------------------------------------------------------
    funding_total, funding_known, f_known, f_unknown, f_total, f_flags = (
        _compute_funding_carry(settled_events))
    metrics["funding_known_subtotal_usd"] = funding_known
    metrics["funding_known_count"] = f_known
    metrics["funding_unknown_count"] = f_unknown
    metrics["funding_event_count"] = f_total
    metrics["funding_coverage"] = (
        f"{f_known}/{f_total}" if f_total else "0/0")
    metrics["funding_flags"] = f_flags
    metrics["funding_complete"] = (f_unknown == 0 and f_total > 0)
    if f_unknown:
        metrics["carry_comparison"] = "CARRY_COMPARISON_UNAVAILABLE"
    # Actual user receipts stay independent (never added to the estimate).
    actual_receipts = cache.get("actual_funding_receipts_usd",
                                cache.get("actualFundingReceiptsUsd"))
    if isinstance(actual_receipts, Mapping):
        metrics["actual_funding_receipts_usd"] = dict(actual_receipts)
    projected = _parse_opt_decimal(
        "projected_next", cache.get("projected_next_funding_usd",
                                    cache.get("projectedNextFundingUsd")))
    projected_str = _fmt(projected) if projected is not None else None

    # -- costs + net ---------------------------------------------------------
    known_cost = _parse_opt_decimal("known_cost", cache.get("known_cost_usd",
                                                            cache.get("knownCostUsd")))
    exit_cost = _parse_opt_decimal("exit_cost", cache.get("estimated_exit_cost_usd",
                                                          cache.get("estimatedExitCostUsd")))
    if known_cost is None:
        metrics["known_cost_unknown"] = True
    if exit_cost is None:
        metrics["exit_cost_unknown"] = True
    known_cost_str = _fmt(known_cost) if known_cost is not None else None
    exit_cost_str = _fmt(exit_cost) if exit_cost is not None else None

    net_before: str | None = None
    net_after: str | None = None
    with localcontext() as ctx:
        ctx.prec = 80
        if (spot_pnl_usd is not None and futures_pnl_usd is not None
                and funding_total is not None and known_cost is not None):
            try:
                net_before = _fmt(Decimal(spot_pnl_usd) + Decimal(futures_pnl_usd)
                                  + Decimal(funding_total) - known_cost)
            except (InvalidOperation, ValueError, ArithmeticError):
                net_before = None
        else:
            missing = []
            if spot_pnl_usd is None:
                missing.append("spot_pnl")
            if futures_pnl_usd is None:
                missing.append("futures_pnl")
            if funding_total is None:
                missing.append("funding_carry")
            if known_cost is None:
                missing.append("known_cost")
            metrics["net_before_missing"] = missing
        if net_before is not None and exit_cost is not None:
            try:
                net_after = _fmt(Decimal(net_before) - exit_cost)
            except (InvalidOperation, ValueError, ArithmeticError):
                net_after = None

    # -- ratio / residual -----------------------------------------------------
    actual_ratio: str | None = None
    residual_short_usd: str | None = None
    with localcontext() as ctx:
        ctx.prec = 80
        if fut_rem != 0:
            try:
                actual_ratio = _fmt(spot_rem / fut_rem)
            except (InvalidOperation, ValueError, ArithmeticError, ZeroDivisionError):
                actual_ratio = None
        if fut_mark is not None and fut_fx is not None:
            try:
                residual = (fut_rem - spot_rem)
                if target_ratio is not None and fut_rem != 0:
                    # Residual vs target: (fut - spot/target?) keep simple
                    # short notional net of spot at futures mark.
                    residual = fut_rem - spot_rem
                residual_short_usd = _fmt(residual * fut_mark * fut_fx)
            except (InvalidOperation, ValueError, ArithmeticError):
                residual_short_usd = None

    # -- liquidation distance (B22; mark over user price is unconfirmed) -----
    liq_distance: str | None = None
    liq_state = "LIQ_UNKNOWN"
    liq_stale = False
    if liq_updated_ms is not None:
        max_age = _int(liq_cfg, "user_price_max_age_sec", 86400) * 1000
        liq_stale = (now - liq_updated_ms) > max_age
    metrics["liq_price_source"] = liq_source
    metrics["liq_price_stale"] = bool(liq_stale)
    if liq_source == "ESTIMATED":
        metrics["liq_estimate_note"] = "Estimate only"
    if liq_price is not None and fut_mark is not None and fut_mark != 0:
        with localcontext() as ctx:
            ctx.prec = 80
            try:
                liq_distance = _fmt((liq_price - fut_mark) / fut_mark)
                dist = (liq_price - fut_mark) / fut_mark
                warn_d = Decimal(str(_num(liq_cfg, "warning_distance", 0.20)))
                crit_d = Decimal(str(_num(liq_cfg, "critical_distance", 0.10)))
                emerg_d = Decimal(str(_num(liq_cfg, "emergency_distance", 0.05)))
                if dist <= 0:
                    # Mark reached the user price: possible, never confirmed,
                    # never auto-liquidated, never zeroed.
                    liq_state = "LIQUIDATION_POSSIBLE_UNCONFIRMED"
                elif dist < emerg_d:
                    liq_state = "EMERGENCY"
                elif dist < crit_d:
                    liq_state = "CRITICAL"
                elif dist < warn_d:
                    liq_state = "WARNING"
                else:
                    liq_state = "NORMAL"
            except (InvalidOperation, ValueError, ArithmeticError):
                liq_distance = None
                liq_state = "LIQ_UNKNOWN"
    elif liq_price is None:
        liq_state = "LIQ_UNKNOWN_NO_PRICE"
    else:
        liq_state = "LIQ_UNKNOWN_NO_MARK"
    metrics["liq_state"] = liq_state

    # -- exit liquidity (B24) --------------------------------------------------
    sell_exec = _parse_opt_decimal(
        "sell_exec", _m(spot_d, "sell_executable_qty", "sellExecutableQty"))
    sell_slip: Any = _m(spot_d, "sell_slippage_bps", "sellSlippageBps")
    try:
        sell_slip_f = float(sell_slip) if sell_slip is not None else None
    except (TypeError, ValueError):
        sell_slip_f = None
    max_slip = _int(exit_cfg, "max_exit_slippage_bps", 100)
    min_cover = _num(exit_cfg, "min_exit_coverage_ratio", 1.0)
    exit_liq: dict[str, Any] = {}
    if sell_exec is not None:
        exit_liq["sell_executable_qty"] = _fmt(sell_exec)
        with localcontext() as ctx:
            ctx.prec = 80
            try:
                exit_liq["covers_remaining"] = bool(
                    spot_rem == 0 or sell_exec >= spot_rem * Decimal(str(min_cover)))
            except (InvalidOperation, ValueError, ArithmeticError):
                exit_liq["covers_remaining"] = False
    else:
        exit_liq["sell_executable_qty"] = None
        exit_liq["covers_remaining"] = None
    exit_liq["sell_slippage_bps"] = sell_slip_f
    exit_liq["max_exit_slippage_bps"] = max_slip
    metrics["exit_liquidity"] = exit_liq

    # -- status ---------------------------------------------------------------
    persist_lag = cache.get("persist_lag_ms", cache.get("persistLagMs", 0))
    try:
        persist_lag_ms = int(persist_lag) if persist_lag is not None else 0
    except (TypeError, ValueError):
        persist_lag_ms = 0
    lag_limit_ms = _int(runtime_cfg, "db_persist_timeout_sec", PERSIST_LAG_SEC) * 1000
    degraded_reasons: list[str] = []
    if fut_stale or spot_stale:
        degraded_reasons.append("QUOTE_STALE")
    if skew_reason == "BASIS_ASYNC_STALE":
        degraded_reasons.append("BASIS_ASYNC_STALE")
    if persist_lag_ms > lag_limit_ms:
        degraded_reasons.append("PERSISTENCE_LAG")
    status = "OK"
    if fut_expired or spot_expired:
        status = "NOT_READY"
    if degraded_reasons:
        status = "MONITOR_DEGRADED"
    # Real positions are never rewritten by degraded flags.
    metrics["degraded_reasons"] = degraded_reasons
    metrics["persist_lag_ms"] = persist_lag_ms
    metrics["basis_flags"] = basis_flags
    metrics["eligible_high_frequency"] = is_high_frequency_status(plan_status)
    metrics["plan_status"] = plan_status
    metrics["actual_funding_receipts_separate"] = True
    metrics["projected_excluded_from_accrued"] = True
    metrics["basis_not_double_added"] = True
    source_meta["futures_mark_as_of_ms"] = fut_asof_ms
    source_meta["spot_quote_as_of_ms"] = spot_asof_ms
    source_meta["known_at_ms"] = now
    quality["basis_status"] = skew_reason
    quality["funding_complete"] = metrics["funding_complete"]

    snapshot_id = f"{plan_id}:monitor:{now}"
    return HedgeMonitor(
        snapshot_id=snapshot_id,
        plan_id=plan_id,
        as_of_ms=now,
        actual_hedge_ratio=actual_ratio,
        residual_short_notional_usd=residual_short_usd,
        mark_price=_fmt(fut_mark) if fut_mark is not None else None,
        spot_price=_fmt(spot_mark) if spot_mark is not None else None,
        current_basis_pct=current_basis_pct,
        basis_pnl_usd=basis_pnl_usd,
        estimated_settled_funding_usd=funding_total,
        projected_next_funding_usd=projected_str,
        spot_pnl_usd=spot_pnl_usd,
        futures_pnl_usd=futures_pnl_usd,
        known_cost_usd=known_cost_str,
        estimated_exit_cost_usd=exit_cost_str,
        net_pnl_before_exit_usd=net_before,
        estimated_net_pnl_after_exit_usd=net_after,
        liquidation_distance=liq_distance,
        exit_liquidity_json=exit_liq,
        source_meta_json=source_meta,
        quality_json=quality,
        metrics_json=metrics,
        safety_score=None,
        status=status,
        created_at_ms=now,
    )


def risk_key(snapshot: Any) -> tuple[str, ...]:
    """Return the persist-relevant risk key (B28.8/B30).

    Changes in liquidation band, basis alert state, ratio alert, orphan
    hint, degraded status or quote-expiry force an out-of-cycle persist
    even before the 60s sampler fires.
    """
    if isinstance(snapshot, Mapping):
        metrics = snapshot.get("metrics_json", snapshot.get("metrics", {}))
        status = snapshot.get("status", "OK")
        liq = ""
        if isinstance(metrics, Mapping):
            liq = str(metrics.get("liq_state", ""))
            degraded = tuple(sorted(metrics.get("degraded_reasons", []) or []))
            basis = str(metrics.get("basis_status", ""))
            ratio = str(metrics.get("ratio_alert", metrics.get("ratio_state", "")))
            orphan = str(metrics.get("orphan_hint", ""))
            expired = (bool(metrics.get("futures_mark_expired", False)),
                       bool(metrics.get("spot_quote_expired", False)))
        else:
            degraded, basis, ratio, orphan, expired = (), "", "", "", (False, False)
        return (str(status), liq, basis, ratio, orphan,
                ",".join(degraded), str(expired))
    metrics = getattr(snapshot, "metrics_json", {}) or {}
    status = getattr(snapshot, "status", "OK")
    liq = str(metrics.get("liq_state", ""))
    degraded = tuple(sorted(metrics.get("degraded_reasons", []) or []))
    basis = str(metrics.get("basis_status", ""))
    ratio = str(metrics.get("ratio_alert", metrics.get("ratio_state", "")))
    orphan = str(metrics.get("orphan_hint", ""))
    expired = (bool(metrics.get("futures_mark_expired", False)),
               bool(metrics.get("spot_quote_expired", False)))
    return (str(status), liq, basis, ratio, orphan,
            ",".join(degraded), str(expired))


def should_persist(
    previous: Any | None,
    current: Any,
    last_persist_ms: int | None,
    policy: Any = None,
    now_ms: int | None = None,
) -> bool:
    """Whether the current snapshot must be persisted (B28.8/B30).

    Persist when there is no previous snapshot, when ``now -
    last_persist`` reaches ``monitor_persist_sec`` (default 60s), or when
    the risk key changes (liquidation band, basis/ratio/orphan hint,
    degraded set, expiry). The persist queue keeps only the latest entry
    per plan and never reads the full DB -- see the H08 worker.
    """
    hedge = _hedge_subtree(policy)
    refresh_cfg = hedge.get("refresh") if isinstance(hedge.get("refresh"), Mapping) else {}
    assert isinstance(refresh_cfg, Mapping)
    persist_sec = _int(refresh_cfg, "monitor_persist_sec", MONITOR_PERSIST_SEC)
    if previous is None:
        return True
    now = int(now_ms) if now_ms is not None else int(
        getattr(current, "as_of_ms", 0)
        if not isinstance(current, Mapping) else current.get("as_of_ms", 0))
    last = int(last_persist_ms) if last_persist_ms is not None else None
    if last is None:
        return True
    if now - last >= persist_sec * 1000:
        return True
    try:
        return risk_key(previous) != risk_key(current)
    except Exception:
        return True
