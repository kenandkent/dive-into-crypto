"""H02 venue base: DepthSnapshot, level VWAP, quote cache (design B9/B16/B31).

- ``DepthSnapshot`` keeps original price/qty **strings** plus ``source`` and
  the receive interval (``source_timestamp_ms``/``fetched_at_ms`` /
  ``received_at_ms``). No float conversion, no 1%-band inference.
- Level VWAP walks price levels in order for the **same** canonical target
  on both sides (buy from asks, sell to bids). It never reverse-engineers a
  VWAP from ``notional_1pct``/total depth.
- ``QuoteCache`` keys on ``(venue, canonical_id, instrument_id,
  rule_version, requested_qty)``: a small-quantity quote is never reused
  for a different quantity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping, Protocol, runtime_checkable

__all__ = [
    "DepthSnapshot",
    "parse_depth_snapshot",
    "vwap_for_qty",
    "mid_from_book",
    "slippage_bps",
    "build_quote_cache_key",
    "QuoteCache",
    "reset_quote_cache",
    "Venue",
    "SPOT_FEES_INCLUDED",
    "ONCHAIN_FEES_INCLUDED",
    "SPOT_CONDITIONAL_CAPABILITY",
    "ONCHAIN_EXECUTION_KIND",
    "project_spot_capabilities",
    "project_onchain_capabilities",
]


#: R06b (D06.2/D06.3): honest fee-inclusion flags (no default-free).
#: Spot/Alpha quotes carry an explicit policy-estimate fee
#: (``estimated_fee_usd`` with VWAP impact single-counted), so
#: ``fees_included=True``. On-chain 0x price carries no fee leg
#: (``estimated_fee_usd=None``, gas may be null), so ``False``.
SPOT_FEES_INCLUDED = True
ONCHAIN_FEES_INCLUDED = False

#: R06b: real capability projection (never an execution permission).
#: Spot venues expose no verified conditional/stop execution in V1; the
#: manual exit plan stays ``MANUAL_EXIT_ONLY``. On-chain quotes stay
#: ``INDICATIVE`` only.
SPOT_CONDITIONAL_CAPABILITY = "MANUAL_EXIT_ONLY"
ONCHAIN_EXECUTION_KIND = "INDICATIVE"


def project_spot_capabilities(
    rules: Any | None,
    *,
    venue: str,
    depth_limit: int | None = None,
    max_price_impact_bps: float | None = None,
) -> dict[str, Any]:
    """Project honest spot capability extras (R06b, read-only).

    Returns capability keys only (no order placement, no execution
    permission): ``fees_included`` (always ``True`` for spot), explicit
    ``stop_orders_supported`` evidence (``True``/``False`` only with a
    source ref, else ``None`` for ``UNKNOWN``) and the manual-exit marker.
    Callers merge the result into ``SpotVenueQuote.capabilities``.
    """
    stop_flag: bool | None = None
    source_ref: str | None = None
    try:
        raw_flag = getattr(rules, "stop_orders_supported", None)
        if isinstance(raw_flag, bool):
            stop_flag = raw_flag
        raw_ref = getattr(rules, "conditional_orders_source_ref", None)
        if isinstance(raw_ref, str) and raw_ref.strip():
            source_ref = raw_ref.strip()
        order_types = getattr(rules, "order_types", None)
        if isinstance(order_types, Mapping):
            has_stop_true = any(
                order_types.get(k) is True for k in ("STOP", "STOP_MARKET", "STOP_LOSS", "CONDITIONAL")
            )
            has_stop_keys = any(k in order_types for k in ("STOP", "STOP_MARKET", "STOP_LOSS", "CONDITIONAL"))
            if has_stop_true and stop_flag is None:
                stop_flag = True
                if source_ref is None:
                    try:
                        source_ref = str(getattr(rules, "rule_version", "") or "")
                    except Exception:
                        source_ref = None
                    if not source_ref:
                        source_ref = None
            elif has_stop_keys and stop_flag is None:
                # Explicit all-False STOP keys are UNSUPPORTED evidence;
                # absence alone stays None (UNKNOWN).
                if all(order_types.get(k) is False for k in ("STOP", "STOP_MARKET", "STOP_LOSS", "CONDITIONAL") if k in order_types):
                    stop_flag = False
                    if source_ref is None:
                        try:
                            source_ref = str(getattr(rules, "rule_version", "") or "")
                        except Exception:
                            source_ref = None
                        if not source_ref:
                            source_ref = None
    except Exception:
        pass
    # Without a source ref the flag has no rule evidence: project UNKNOWN.
    if stop_flag is not None and not source_ref:
        stop_flag = None
    out: dict[str, Any] = {
        "fees_included": bool(SPOT_FEES_INCLUDED),
        "stop_orders_supported": stop_flag,
        "spot_conditional_capability": str(SPOT_CONDITIONAL_CAPABILITY),
        "execution_kind": "QUOTE_ONLY",
    }
    if source_ref is not None:
        out["conditional_orders_source_ref"] = str(source_ref)
    if depth_limit is not None:
        out["depth_limit_projection"] = int(depth_limit)
    if max_price_impact_bps is not None:
        try:
            out["max_price_impact_bps_projection"] = float(max_price_impact_bps)
        except (TypeError, ValueError):
            pass
    _ = venue
    return out


def project_onchain_capabilities() -> dict[str, Any]:
    """Project honest on-chain capability extras (R06b, read-only).

    ``INDICATIVE`` only: no trade payload, no execution permission.
    """
    return {
        "fees_included": bool(ONCHAIN_FEES_INCLUDED),
        "quote_kind": str(ONCHAIN_EXECUTION_KIND),
        "execution_kind": "INDICATIVE_ONLY",
        "simulation_verified": False,
    }


def _require_decimal_str(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty decimal string, got {value!r}")
    text = value.strip()
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{name} is not a decimal string: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")
    return text


@dataclass(frozen=True)
class DepthSnapshot:
    """Raw order-book mirror for one quote (B9/B11, strings preserved).

    ``bids``/``asks`` are ``((price_str, qty_str), ...)`` in exchange order
    (bids descending, asks ascending). ``source`` names the upstream book
    (e.g. ``"binance-spot-depth"``); ``source_timestamp_ms`` is the exchange
    ``E``/``T`` (None when the venue gives no source clock and downstream
    records ``VERIFIED_INGEST``); ``fetched_at_ms``/``received_at_ms`` bound
    the local receive interval.
    """

    venue: str
    instrument_id: str
    bids: tuple[tuple[str, str], ...] = ()
    asks: tuple[tuple[str, str], ...] = ()
    source: str = ""
    source_timestamp_ms: int | None = None
    fetched_at_ms: int = 0
    received_at_ms: int = 0

    def __post_init__(self) -> None:
        if not self.venue or not isinstance(self.venue, str):
            raise ValueError("venue must be a non-empty str")
        if not self.instrument_id or not isinstance(self.instrument_id, str):
            raise ValueError("instrument_id must be a non-empty str")
        for side_name in ("bids", "asks"):
            levels = getattr(self, side_name)
            checked: list[tuple[str, str]] = []
            for level in levels:
                if not isinstance(level, (list, tuple)) or len(level) != 2:
                    raise ValueError(f"{side_name} level must be (price, qty), got {level!r}")
                price = _require_decimal_str(f"{side_name}.price", level[0])
                qty = _require_decimal_str(f"{side_name}.qty", level[1])
                if Decimal(price) <= 0 or Decimal(qty) <= 0:
                    raise ValueError(f"{side_name} price/qty must be > 0, got {level!r}")
                checked.append((price, qty))
            object.__setattr__(self, side_name, tuple(checked))


def parse_depth_snapshot(
    *,
    venue: str,
    instrument_id: str,
    raw_bids: Any,
    raw_asks: Any,
    source: str,
    source_timestamp_ms: int | None,
    fetched_at_ms: int,
    received_at_ms: int,
) -> DepthSnapshot:
    """Build a :class:`DepthSnapshot` from raw ``[[price, qty], ...]`` sides.

    Raw entries may be strings or numbers; stored values are always the
    original decimal **strings** (numbers are stringified verbatim, never
    float-rounded). Malformed levels are skipped; empty sides stay empty so
    callers report thin books honestly instead of fabricating depth.
    """
    def _clean(rows: Any) -> tuple[tuple[str, str], ...]:
        out: list[tuple[str, str]] = []
        for row in rows or []:
            try:
                raw_price, raw_qty = row[0], row[1]
            except (IndexError, TypeError):
                continue
            try:
                price_s = str(raw_price).strip()
                qty_s = str(raw_qty).strip()
                _require_decimal_str("price", price_s)
                _require_decimal_str("qty", qty_s)
            except ValueError:
                continue
            try:
                if Decimal(price_s) <= 0 or Decimal(qty_s) <= 0:
                    continue
            except (InvalidOperation, ValueError):
                continue
            out.append((price_s, qty_s))
        return tuple(out)

    return DepthSnapshot(
        venue=venue,
        instrument_id=instrument_id,
        bids=_clean(raw_bids),
        asks=_clean(raw_asks),
        source=source,
        source_timestamp_ms=source_timestamp_ms,
        fetched_at_ms=int(fetched_at_ms),
        received_at_ms=int(received_at_ms),
    )


def vwap_for_qty(
    levels: tuple[tuple[str, str], ...] | list[tuple[str, str]],
    target_qty: Decimal,
) -> tuple[Decimal | None, Decimal]:
    """Level VWAP for ``target_qty`` (Decimal, 80-digit localcontext).

    Walks ``levels`` in order, taking ``min(level_qty, remaining)`` at each
    price. Returns ``(vwap_or_None, executable_qty)`` where ``executable_qty``
    is the depth actually available (``<= target``) and ``vwap`` is the
    quantity-weighted average over the executed portion (None when no depth).
    Never derives a price from aggregated 1% notional.
    """
    target = Decimal(target_qty)
    if target <= 0:
        raise ValueError("target_qty must be > 0")
    with localcontext() as ctx:
        ctx.prec = 80
        filled = Decimal(0)
        notional = Decimal(0)
        for price_s, qty_s in levels or []:
            if filled >= target:
                break
            try:
                price, qty = Decimal(price_s), Decimal(qty_s)
            except (InvalidOperation, ValueError):
                continue
            if price <= 0 or qty <= 0:
                continue
            take = qty if filled + qty <= target else target - filled
            notional += price * take
            filled += take
        if filled <= 0:
            return None, Decimal(0)
        return notional / filled, filled


def mid_from_book(bid_str: str | None, ask_str: str | None) -> Decimal | None:
    """``(best_bid + best_ask) / 2`` (Decimal) or None when either is missing."""
    if bid_str is None or ask_str is None:
        return None
    try:
        bid, ask = Decimal(str(bid_str)), Decimal(str(ask_str))
    except (InvalidOperation, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        return (bid + ask) / Decimal(2)


def slippage_bps(vwap: Decimal | None, mid: Decimal | None) -> float | None:
    """``(vwap - mid) / mid * 10000`` as float bps (display only)."""
    if vwap is None or mid is None:
        return None
    try:
        with localcontext() as ctx:
            ctx.prec = 80
            value = (Decimal(vwap) - Decimal(mid)) / Decimal(mid) * Decimal(10000)
        result = float(value)
    except (InvalidOperation, ValueError, ZeroDivisionError, ArithmeticError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN/inf guard
        return None
    return result


def build_quote_cache_key(
    venue: str,
    canonical_id: str,
    instrument_id: str,
    rule_version: str,
    requested_qty: str,
) -> tuple[str, str, str, str, str]:
    """Quote cache key: venue/identity/rules/quantity (exact qty string).

    ``requested_qty`` is normalised through ``Decimal`` string form so
    ``"1.50"`` and ``"1.5"`` share a key, but different quantities never do.
    """
    qty_norm = str(Decimal(str(requested_qty).strip()).normalize())
    return (str(venue), str(canonical_id), str(instrument_id),
            str(rule_version), qty_norm)


class QuoteCache:
    """Per-process venue quote cache (B31 TTL, quantity-keyed).

    :meth:`get` returns the identical ``SpotVenueQuote`` on hit (original
    ``as_of_ms``/``fetched_at_ms``); any quantity change is a different key
    so a small-amount quote never stands in for a larger exit.
    """

    def __init__(self, *, ttl_sec: float = 20.0) -> None:
        self._ttl = float(ttl_sec)
        self._store: dict[tuple[str, str, str, str, str], tuple[Any, float]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple[str, str, str, str, str]) -> Any | None:
        row = self._store.get(key)
        if row is None:
            self.misses += 1
            return None
        quote, expires_mono = row
        if time.monotonic() >= expires_mono:
            self._store.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return quote

    def put(self, key: tuple[str, str, str, str, str], quote: Any) -> None:
        self._store[key] = (quote, time.monotonic() + self._ttl)

    def invalidate(self, key: tuple[str, str, str, str, str]) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._store)


#: Shared quote cache (venues use per-instance caches in tests).
_default_quote_cache = QuoteCache()


def reset_quote_cache() -> None:
    """Drop the shared quote cache (test hook)."""
    _default_quote_cache.clear()


@runtime_checkable
class Venue(Protocol):
    """Unified spot-venue business interface (B9.1).

    ``quote`` buys the target canonical quantity first, then quotes the
    sale of the *same* net quantity (no two independent notionals). Both
    legs share ``canonical_qty`` verbatim.
    """

    async def quote(
        self,
        identity: Any,
        canonical_qty: str,
        request_context: Any | None = None,
    ) -> Any: ...
