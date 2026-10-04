"""H02 unified trading-rules parser/cache (design B16.1; plan H02.1).

Futures/Spot/Alpha ``exchangeInfo`` symbol entries share one parser:

- ``LOT_SIZE`` gates ``LIMIT`` quantity, ``MARKET_LOT_SIZE`` (when present)
  gates ``MARKET`` quantity with fallback to ``LOT_SIZE`` ("MARKET prefers
  MARKET_LOT_SIZE");
- ``MIN_NOTIONAL``/``NOTIONAL`` gate notional for the applicable order type;
- ``PRICE_FILTER`` (+ optional ``PERCENT_PRICE`` guard) gates price;
- ``pricePrecision``/``quantityPrecision``/``baseAssetPrecision`` /
  ``quotePrecision`` are NEVER substituted for tick/step (precision ban);
- a ``0`` keeps its venue-native "disabled" meaning and is never treated as
  a legal step (no division by zero, no ``qty % 0``);
- unknown ``orderType`` or unknown/missing rule for that order type marks
  the order type unsupported; callers surface
  ``TRADING_RULES_UNVERIFIED`` instead of guessing.

Raw shape (Binance Spot/Futures + Alpha ``get-exchange-info`` symbol entry,
field names verbatim)::

    {
      "symbol": "BTCUSDT",
      "status": "TRADING",
      "orderTypes": ["LIMIT", "MARKET"],
      "filters": [
        {"filterType": "PRICE_FILTER", "minPrice": "0.01",
         "maxPrice": "1000000", "tickSize": "0.01"},
        {"filterType": "LOT_SIZE", "minQty": "0.001",
         "maxQty": "1000", "stepSize": "0.001"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001",
         "maxQty": "1000", "stepSize": "0.001"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "10",
         "applyToMarket": true, "avgPriceMins": 5}
      ]
    }

Alpha envelopes wrap the same symbol entries; the Alpha data adapter unwraps
``code == "000000"`` before calling this parser, so this module never sees
the envelope.
"""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.hedge.models import TradingRulesSnapshot

__all__ = [
    "TRADING_RULES_UNVERIFIED",
    "TradingRulesError",
    "RULES_CACHE_TTL_SEC",
    "parse_trading_rules",
    "is_order_type_supported",
    "validate_qty",
    "validate_price",
    "validate_notional",
    "validate_order",
    "TradingRulesCache",
    "reset_trading_rules_cache",
]

TRADING_RULES_UNVERIFIED = "TRADING_RULES_UNVERIFIED"

#: exchangeInfo cache TTL (B16.1: 30 minutes).
RULES_CACHE_TTL_SEC = 1800.0

#: Filter types understood by the unified parser. Unknown types are kept in
#: ``raw_filters`` for audit but never gate an order type.
_KNOWN_FILTERS = frozenset({
    "PRICE_FILTER",
    "LOT_SIZE",
    "MARKET_LOT_SIZE",
    "MIN_NOTIONAL",
    "NOTIONAL",
    "PERCENT_PRICE",
    "PERCENT_PRICE_BY_SIDE",
})

#: Precision keys that must NEVER substitute for tick/step (ban).
_PRECISION_KEYS = frozenset({
    "pricePrecision",
    "quantityPrecision",
    "baseAssetPrecision",
    "quotePrecision",
    "tickSizePrecision",
})


class TradingRulesError(ValueError):
    """Structurally illegal filters (non-decimal, min>max, negative step)."""

    def __init__(self, message: str, *, reason_code: str = TRADING_RULES_UNVERIFIED) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _require_decimal_str(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TradingRulesError(f"{name} must be a decimal string, got {value!r}")
    text = value.strip()
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise TradingRulesError(f"{name} is not a decimal string: {value!r}") from exc
    if not parsed.is_finite():
        raise TradingRulesError(f"{name} must be finite, got {value!r}")
    return text


def _decimal(value: str) -> Decimal:
    return Decimal(value)


def _is_zero(value: str) -> bool:
    return _decimal(value) == 0


def _check_range(name_min: str, vmin: str, name_max: str, vmax: str) -> None:
    """Both non-zero bounds must satisfy min <= max; zero means disabled."""
    dmin, dmax = _decimal(vmin), _decimal(vmax)
    if dmin < 0 or dmax < 0:
        raise TradingRulesError(f"{name_min}/{name_max} must be non-negative")
    if dmin != 0 and dmax != 0 and dmin > dmax:
        raise TradingRulesError(f"{name_min} {vmin!r} > {name_max} {vmax!r}")


def _filter_by_type(filters: list[dict[str, Any]], ftype: str) -> dict[str, Any] | None:
    for entry in filters:
        if isinstance(entry, dict) and entry.get("filterType") == ftype:
            return entry
    return None


def _canonical_rule_version(venue: str, instrument: str, filters: Any) -> str:
    try:
        canon = json.dumps(filters, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False, default=str)
    except (TypeError, ValueError):
        canon = repr(filters)
    digest = hashlib.sha256(f"{venue}|{instrument}|{canon}".encode("utf-8")).hexdigest()[:16]
    return f"rules-v1-{digest}"


def parse_trading_rules(
    raw: Mapping[str, Any],
    venue: str,
    instrument: str,
    meta: Mapping[str, Any] | None = None,
) -> TradingRulesSnapshot:
    """Parse one ``exchangeInfo`` symbol entry into a frozen snapshot.

    ``raw`` is the symbol entry (with ``filters`` list); ``venue`` is
    ``BINANCE_SPOT``/``BINANCE_ALPHA`` (Futures-shaped filters are accepted
    through the same code path with a Spot/Alpha venue label); ``instrument``
    is the venue instrument ID; ``meta`` may carry ``source_as_of_ms``,
    ``known_at_ms`` and an explicit ``rule_version`` override.

    Precision keys in ``raw`` are ignored. Missing required filters do not
    raise -- they mark that order type unsupported. Structurally illegal
    filter payloads raise :class:`TradingRulesError`.
    """
    if not isinstance(raw, Mapping):
        raise TradingRulesError(f"raw symbol entry must be a mapping, got {type(raw).__name__}")
    if not instrument or not isinstance(instrument, str):
        raise TradingRulesError("instrument must be a non-empty str")
    meta = dict(meta) if isinstance(meta, Mapping) else {}

    filters_raw = raw.get("filters")
    if not isinstance(filters_raw, list):
        raise TradingRulesError("symbol entry missing filters list (precision ban: "
                                "pricePrecision/quantityPrecision never substitute)")
    filters: list[dict[str, Any]] = [dict(f) for f in filters_raw if isinstance(f, dict)]

    raw_filters: dict[str, Any] = {}
    for entry in filters:
        ftype = entry.get("filterType")
        if isinstance(ftype, str) and ftype:
            # Keep first occurrence; duplicates are ambiguous but not fatal.
            raw_filters.setdefault(ftype, dict(entry))

    # ---- PRICE_FILTER ---------------------------------------------------
    price_rules: dict[str, Any] = {}
    price_filter = _filter_by_type(filters, "PRICE_FILTER")
    if price_filter is not None:
        for key in ("minPrice", "maxPrice", "tickSize"):
            if key not in price_filter:
                raise TradingRulesError(f"PRICE_FILTER missing {key}")
        min_price = _require_decimal_str("PRICE_FILTER.minPrice", price_filter["minPrice"])
        max_price = _require_decimal_str("PRICE_FILTER.maxPrice", price_filter["maxPrice"])
        tick_size = _require_decimal_str("PRICE_FILTER.tickSize", price_filter["tickSize"])
        if _decimal(tick_size) < 0:
            raise TradingRulesError("PRICE_FILTER.tickSize must be non-negative")
        _check_range("minPrice", min_price, "maxPrice", max_price)
        price_rules = {"tick_size": tick_size, "min_price": min_price, "max_price": max_price}
        # Optional PERCENT_PRICE guard (kept verbatim when present).
        percent = _filter_by_type(filters, "PERCENT_PRICE")
        if percent is None:
            percent = _filter_by_type(filters, "PERCENT_PRICE_BY_SIDE")
        if percent is not None:
            for key, out_key in (("multiplierUp", "multiplier_up"),
                                 ("multiplierDown", "multiplier_down"),
                                 ("multiplierDecimal", "multiplier_decimal")):
                if key in percent and percent[key] is not None:
                    price_rules[out_key] = _require_decimal_str(
                        f"PERCENT_PRICE.{key}", percent[key])
            if "avgPriceMins" in percent and percent["avgPriceMins"] is not None:
                try:
                    price_rules["avg_price_mins"] = str(int(percent["avgPriceMins"]))
                except (TypeError, ValueError) as exc:
                    raise TradingRulesError("PERCENT_PRICE.avgPriceMins must be an int") from exc

    # ---- LOT_SIZE / MARKET_LOT_SIZE -------------------------------------
    lot_rules: dict[str, Any] = {}
    lot = _filter_by_type(filters, "LOT_SIZE")
    lot_parsed: dict[str, str] | None = None
    if lot is not None:
        for key in ("minQty", "maxQty", "stepSize"):
            if key not in lot:
                raise TradingRulesError(f"LOT_SIZE missing {key}")
        min_qty = _require_decimal_str("LOT_SIZE.minQty", lot["minQty"])
        max_qty = _require_decimal_str("LOT_SIZE.maxQty", lot["maxQty"])
        step_size = _require_decimal_str("LOT_SIZE.stepSize", lot["stepSize"])
        if _decimal(step_size) < 0:
            raise TradingRulesError("LOT_SIZE.stepSize must be non-negative")
        _check_range("minQty", min_qty, "maxQty", max_qty)
        lot_parsed = {"step_size": step_size, "min_qty": min_qty, "max_qty": max_qty}
        lot_rules.update(lot_parsed)

    market_lot = _filter_by_type(filters, "MARKET_LOT_SIZE")
    market_parsed: dict[str, str] | None = None
    if market_lot is not None:
        for key in ("minQty", "maxQty", "stepSize"):
            if key not in market_lot:
                raise TradingRulesError(f"MARKET_LOT_SIZE missing {key}")
        m_min = _require_decimal_str("MARKET_LOT_SIZE.minQty", market_lot["minQty"])
        m_max = _require_decimal_str("MARKET_LOT_SIZE.maxQty", market_lot["maxQty"])
        m_step = _require_decimal_str("MARKET_LOT_SIZE.stepSize", market_lot["stepSize"])
        if _decimal(m_step) < 0:
            raise TradingRulesError("MARKET_LOT_SIZE.stepSize must be non-negative")
        _check_range("marketMinQty", m_min, "marketMaxQty", m_max)
        market_parsed = {"market_step_size": m_step, "market_min_qty": m_min,
                         "market_max_qty": m_max}
        lot_rules.update(market_parsed)
    elif lot_parsed is not None:
        # MARKET falls back to LOT_SIZE ("MARKET prefers MARKET_LOT_SIZE").
        lot_rules["market_step_size"] = lot_parsed["step_size"]
        lot_rules["market_min_qty"] = lot_parsed["min_qty"]
        lot_rules["market_max_qty"] = lot_parsed["max_qty"]

    # ---- MIN_NOTIONAL / NOTIONAL ----------------------------------------
    notional_rules: dict[str, Any] = {}
    notional_filter = _filter_by_type(filters, "MIN_NOTIONAL")
    if notional_filter is None:
        notional_filter = _filter_by_type(filters, "NOTIONAL")
    if notional_filter is not None:
        ftype = notional_filter.get("filterType")
        min_key = "minNotional" if "minNotional" in notional_filter else (
            "notional" if "notional" in notional_filter else (
                "min_notional" if "min_notional" in notional_filter else None))
        if min_key is None:
            raise TradingRulesError(f"{ftype} missing minNotional/notional")
        min_notional = _require_decimal_str(f"{ftype}.{min_key}", notional_filter[min_key])
        if _decimal(min_notional) < 0:
            raise TradingRulesError(f"{ftype}.{min_key} must be non-negative")
        notional_rules["min_notional"] = min_notional
        for max_key in ("maxNotional", "max_notional"):
            if max_key in notional_filter and notional_filter[max_key] is not None:
                max_notional = _require_decimal_str(
                    f"{ftype}.{max_key}", notional_filter[max_key])
                if _decimal(max_notional) < 0:
                    raise TradingRulesError(f"{ftype}.{max_key} must be non-negative")
                notional_rules["max_notional"] = max_notional
                break
        else:
            notional_rules["max_notional"] = "0"
        # applyToMarket / avgPriceMins are venue-native flags (kept verbatim).
        if "applyToMarket" in notional_filter:
            notional_rules["apply_to_market"] = bool(notional_filter["applyToMarket"])
        if "avgPriceMins" in notional_filter and notional_filter["avgPriceMins"] is not None:
            try:
                notional_rules["avg_price_mins"] = str(int(notional_filter["avgPriceMins"]))
            except (TypeError, ValueError) as exc:
                raise TradingRulesError(f"{ftype}.avgPriceMins must be an int") from exc

    # ---- order types ------------------------------------------------------
    # Explicit venue list wins when present; otherwise infer from filters.
    declared: set[str] | None = None
    for key in ("orderTypes", "order_types"):
        if isinstance(raw.get(key), (list, tuple)):
            declared = {str(v).upper() for v in raw[key] if isinstance(v, str)}
            break
    has_limit_filters = lot_parsed is not None and bool(price_rules)
    has_market_filters = (market_parsed is not None or lot_parsed is not None)
    if declared is not None:
        limit_ok = "LIMIT" in declared and has_limit_filters
        market_ok = "MARKET" in declared and has_market_filters
        # Unknown orderType strings never enable a type; missing rule for a
        # declared type disables that type (TRADING_RULES_UNVERIFIED downstream).
    else:
        limit_ok = has_limit_filters
        market_ok = has_market_filters
    order_types: dict[str, Any] = {"LIMIT": bool(limit_ok), "MARKET": bool(market_ok)}

    source_as_of_ms = meta.get("source_as_of_ms")
    if source_as_of_ms is not None:
        try:
            source_as_of_ms = int(source_as_of_ms)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise TradingRulesError("meta.source_as_of_ms must be an int") from exc
    known_at_ms = meta.get("known_at_ms")
    if known_at_ms is None:
        import time as _time
        known_at_ms = int(_time.time() * 1000)
    else:
        try:
            known_at_ms = int(known_at_ms)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise TradingRulesError("meta.known_at_ms must be an int") from exc
    rule_version = meta.get("rule_version")
    if not isinstance(rule_version, str) or not rule_version:
        rule_version = _canonical_rule_version(venue, instrument, filters)

    return TradingRulesSnapshot(
        venue=venue,
        instrument_id=instrument,
        source_as_of_ms=source_as_of_ms,
        known_at_ms=known_at_ms,
        rule_version=rule_version,
        raw_filters=raw_filters,
        order_types=order_types,
        price_rules=price_rules,
        lot_rules=lot_rules,
        notional_rules=notional_rules,
    )


def is_order_type_supported(snapshot: TradingRulesSnapshot, order_type: str) -> bool:
    """True when ``snapshot`` supports ``order_type`` (LIMIT/MARKET)."""
    return bool(snapshot.order_types.get(str(order_type).upper(), False))


def _lot_bounds(snapshot: TradingRulesSnapshot, order_type: str) -> tuple[str, str, str] | None:
    lot = snapshot.lot_rules
    otype = str(order_type).upper()
    if otype == "MARKET" and "market_step_size" in lot:
        return (lot.get("market_step_size", "0"), lot.get("market_min_qty", "0"),
                lot.get("market_max_qty", "0"))
    if "step_size" in lot:
        return (lot.get("step_size", "0"), lot.get("min_qty", "0"), lot.get("max_qty", "0"))
    return None


def validate_qty(
    snapshot: TradingRulesSnapshot, qty_str: str, order_type: str = "LIMIT"
) -> tuple[bool, str | None]:
    """Check ``qty`` against LOT_SIZE/MARKET_LOT_SIZE (Decimal, step-aware).

    ``0`` step/min/max means disabled (no constraint), never a legal step.
    Returns ``(True, None)`` on pass, ``(False, reason)`` on reject.
    """
    otype = str(order_type).upper()
    if not is_order_type_supported(snapshot, otype):
        return False, f"{otype} order type unsupported ({TRADING_RULES_UNVERIFIED})"
    try:
        qty = Decimal(str(qty_str).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return False, f"qty is not a decimal string: {qty_str!r}"
    if not qty.is_finite() or qty <= 0:
        return False, f"qty must be > 0, got {qty_str!r}"
    bounds = _lot_bounds(snapshot, otype)
    if bounds is None:
        return False, f"{otype} lot rules unknown ({TRADING_RULES_UNVERIFIED})"
    step_s, min_s, max_s = bounds
    try:
        step, vmin, vmax = Decimal(step_s), Decimal(min_s), Decimal(max_s)
    except (InvalidOperation, ValueError):
        return False, f"{otype} lot rules malformed ({TRADING_RULES_UNVERIFIED})"
    if vmin != 0 and qty < vmin:
        return False, f"qty {qty_str!r} below minQty {min_s!r}"
    if vmax != 0 and qty > vmax:
        return False, f"qty {qty_str!r} above maxQty {max_s!r}"
    if step != 0:
        base = vmin if vmin != 0 else Decimal(0)
        remainder = (qty - base) % step
        # Decimal modulo keeps exactness; allow both 0 and step (edge).
        if remainder != 0:
            return False, f"qty {qty_str!r} not aligned to stepSize {step_s!r}"
    return True, None


def validate_price(
    snapshot: TradingRulesSnapshot, price_str: str, order_type: str = "LIMIT"
) -> tuple[bool, str | None]:
    """Check ``price`` against PRICE_FILTER (Decimal, tick-aware)."""
    otype = str(order_type).upper()
    if not is_order_type_supported(snapshot, otype):
        return False, f"{otype} order type unsupported ({TRADING_RULES_UNVERIFIED})"
    rules = snapshot.price_rules
    if "tick_size" not in rules:
        return False, f"{otype} price rules unknown ({TRADING_RULES_UNVERIFIED})"
    try:
        price = Decimal(str(price_str).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return False, f"price is not a decimal string: {price_str!r}"
    if not price.is_finite() or price <= 0:
        return False, f"price must be > 0, got {price_str!r}"
    try:
        tick = Decimal(rules["tick_size"])
        vmin = Decimal(rules.get("min_price", "0"))
        vmax = Decimal(rules.get("max_price", "0"))
    except (InvalidOperation, ValueError):
        return False, f"{otype} price rules malformed ({TRADING_RULES_UNVERIFIED})"
    if vmin != 0 and price < vmin:
        return False, f"price {price_str!r} below minPrice {rules.get('min_price')!r}"
    if vmax != 0 and price > vmax:
        return False, f"price {price_str!r} above maxPrice {rules.get('max_price')!r}"
    if tick != 0:
        base = vmin if vmin != 0 else Decimal(0)
        if (price - base) % tick != 0:
            return False, f"price {price_str!r} not aligned to tickSize {rules['tick_size']!r}"
    return True, None


def validate_notional(
    snapshot: TradingRulesSnapshot,
    notional_str: str,
    order_type: str = "LIMIT",
) -> tuple[bool, str | None]:
    """Check ``notional`` against MIN_NOTIONAL/NOTIONAL (Decimal)."""
    otype = str(order_type).upper()
    if not is_order_type_supported(snapshot, otype):
        return False, f"{otype} order type unsupported ({TRADING_RULES_UNVERIFIED})"
    rules = snapshot.notional_rules
    if "min_notional" not in rules:
        # No notional rule for this venue: vacuous pass.
        return True, None
    try:
        notional = Decimal(str(notional_str).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return False, f"notional is not a decimal string: {notional_str!r}"
    if not notional.is_finite() or notional < 0:
        return False, f"notional must be >= 0, got {notional_str!r}"
    try:
        vmin = Decimal(rules["min_notional"])
        vmax = Decimal(rules.get("max_notional", "0"))
    except (InvalidOperation, ValueError):
        return False, f"{otype} notional rules malformed ({TRADING_RULES_UNVERIFIED})"
    if vmin != 0 and notional < vmin:
        return False, f"notional {notional_str!r} below minNotional {rules['min_notional']!r}"
    if vmax != 0 and notional > vmax:
        return False, f"notional {notional_str!r} above maxNotional {rules.get('max_notional')!r}"
    return True, None


def validate_order(
    snapshot: TradingRulesSnapshot,
    qty_str: str,
    price_str: str | None,
    notional_str: str | None,
    order_type: str = "LIMIT",
) -> tuple[bool, str | None]:
    """Validate one order against all applicable rules (qty, price, notional)."""
    ok, reason = validate_qty(snapshot, qty_str, order_type)
    if not ok:
        return ok, reason
    if price_str is not None:
        ok, reason = validate_price(snapshot, price_str, order_type)
        if not ok:
            return ok, reason
    if notional_str is not None:
        ok, reason = validate_notional(snapshot, notional_str, order_type)
        if not ok:
            return ok, reason
    return True, None


class TradingRulesCache:
    """In-memory rules snapshot cache (30-minute TTL, version-preserving).

    Keyed by ``(venue, instrument_id)``; :meth:`get` returns the identical
    snapshot (original ``rule_version``/``known_at_ms``), never a restamp.
    """

    def __init__(self, *, ttl_sec: float = RULES_CACHE_TTL_SEC,
                 clock_monotonic=None, clock_ms=None) -> None:
        self._ttl = float(ttl_sec)
        self._store: dict[tuple[str, str], tuple[TradingRulesSnapshot, float]] = {}
        self._mono = clock_monotonic or time.monotonic
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def _key(self, venue: str, instrument_id: str) -> tuple[str, str]:
        return (str(venue), str(instrument_id))

    def get(self, venue: str, instrument_id: str) -> TradingRulesSnapshot | None:
        row = self._store.get(self._key(venue, instrument_id))
        if row is None:
            return None
        snapshot, expires_mono = row
        if self._mono() >= expires_mono:
            self._store.pop(self._key(venue, instrument_id), None)
            return None
        return snapshot

    def put(self, snapshot: TradingRulesSnapshot) -> None:
        self._store[self._key(snapshot.venue, snapshot.instrument_id)] = (
            snapshot, self._mono() + self._ttl)

    def invalidate(self, venue: str, instrument_id: str) -> None:
        self._store.pop(self._key(venue, instrument_id), None)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


#: Shared process cache (venues hold their own instance in tests).
_default_cache = TradingRulesCache()


def reset_trading_rules_cache() -> None:
    """Drop the shared rules cache (test hook)."""
    _default_cache.clear()
