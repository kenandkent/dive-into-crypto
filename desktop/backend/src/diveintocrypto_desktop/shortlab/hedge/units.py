"""R06a units: native/canonical/FX conversions + contract VWAP + stop reference (D06.1, pure).

Pure module: no network, no DB, no clock reads. All amounts are Decimal
strings (D03.1); floats are rejected at the boundary. Quantity/price maths
runs under an 80-digit localcontext; rounding is display-only via
canonical fixed-point normalisation (``-0`` -> ``0``, no exponent).

Formulas (D06.1):

- ``P_canonical_usd = P_native * FX / m``
- ``Q_canonical = Q_contract * m``
- contract ``Notional = P_native * Q_contract * FX``
- Quote Volume / OI notionals are already quote-notional and are NEVER
  scaled by ``m`` (they must use :func:`unscaled_notional_usd`, which takes
  no multiplier, so a 1000x contract cannot leak into them).
- liquidation distance ``(L_native - Mark_native) / Mark_native``;
  ``L <= Mark`` is already-crossed/input-error but still returns the
  computed (``<= 0``) distance so callers can mark
  ``INVALID_AFTER_LIQUIDATION`` instead of guessing.
- default STOP reference midpoint ``(Mark + L) / 2`` with STOP_MARKET
  native tick, BUY stop rounded UP (``ROUND_CEILING``), requiring
  ``Mark < Stop < L``. A too-coarse tick with no legal value returns
  ``None`` (caller records ``STOP_PRICE_UNREPRESENTABLE``). This is
  reference guidance, never a fill guarantee.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, localcontext
from typing import Any, Mapping

__all__ = [
    "STOP_PRICE_UNREPRESENTABLE",
    "canonical_price_usd",
    "canonical_qty",
    "canonical_to_contract_qty",
    "contract_to_canonical_qty",
    "contract_notional_usd",
    "unscaled_notional_usd",
    "quote_volume_usd",
    "oi_notional_usd",
    "native_liquidation_distance",
    "native_stop_reference",
    "compute_contract_vwap",
]

#: Reason code when no tick-aligned BUY stop satisfies ``Mark < Stop < L``.
STOP_PRICE_UNREPRESENTABLE = "STOP_PRICE_UNREPRESENTABLE"


# ---------------------------------------------------------------------------
# Decimal helpers (ledger boundary: never float for qty/price).
# ---------------------------------------------------------------------------


def _parse_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal string, got {value!r}")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{name} must be a non-empty decimal string")
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise ValueError(f"{name} is not a decimal string: {value!r}") from exc
    else:
        raise ValueError(
            f"{name} must be a decimal string (float rejected), got {value!r}"
        )
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")
    return parsed


def _parse_optional_decimal(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    return _parse_decimal(value, name)


def _dec_str(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(value)
    if not value.is_finite():
        raise ValueError("non-finite decimal cannot be serialised")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if text in ("", "-"):
            return "0"
        if text.startswith("."):
            text = "0" + text
        elif text.startswith("-."):
            text = "-0." + text[2:]
    if text == "-0":
        return "0"
    return text


def _parse_multiplier(value: Any) -> Decimal | None:
    if value is None:
        return None
    parsed = _parse_decimal(value, "multiplier")
    if parsed <= 0:
        raise ValueError(f"multiplier must be > 0, got {value!r}")
    return parsed


def _parse_fx(value: Any) -> Decimal | None:
    if value is None:
        return None
    parsed = _parse_decimal(value, "fx_to_usd")
    if parsed <= 0:
        raise ValueError(f"fx_to_usd must be > 0, got {value!r}")
    return parsed


# ---------------------------------------------------------------------------
# Canonical / notional conversions (D06.1).
# ---------------------------------------------------------------------------


def canonical_price_usd(
    native_price: str | Decimal | int,
    fx_to_usd: str | Decimal | int | None,
    multiplier: str | Decimal | int | None,
) -> str | None:
    """Canonical per-coin USD price ``P_native * FX / m``.

    Returns ``None`` (with no guessing) when ``FX`` or ``multiplier`` is
    unknown (``None``). Raises ``ValueError`` for non-decimal or
    non-positive ``FX``/``multiplier``.
    """
    if fx_to_usd is None or multiplier is None:
        return None
    native = _parse_decimal(native_price, "native_price")
    fx = _parse_fx(fx_to_usd)
    mult = _parse_multiplier(multiplier)
    assert fx is not None and mult is not None
    with localcontext() as ctx:
        ctx.prec = 80
        out = native * fx / mult
    return _dec_str(out)


def canonical_qty(
    contract_qty: str | Decimal | int,
    multiplier: str | Decimal | int | None,
) -> str | None:
    """Canonical coin quantity ``Q_contract * m``.

    Returns ``None`` when ``multiplier`` is unknown. Raises for invalid
    multiplier.
    """
    if multiplier is None:
        return None
    qty = _parse_decimal(contract_qty, "contract_qty")
    mult = _parse_multiplier(multiplier)
    assert mult is not None
    with localcontext() as ctx:
        ctx.prec = 80
        out = qty * mult
    return _dec_str(out)


def canonical_to_contract_qty(
    canonical_quantity: str | Decimal | int,
    multiplier: str | Decimal | int | None,
) -> str | None:
    """Convert a canonical coin quantity to native futures contracts."""
    if multiplier is None:
        return None
    canonical = _parse_decimal(canonical_quantity, "canonical_quantity")
    mult = _parse_multiplier(multiplier)
    assert mult is not None
    with localcontext() as ctx:
        ctx.prec = 80
        return _dec_str(canonical / mult)


def contract_to_canonical_qty(
    contract_quantity: str | Decimal | int,
    multiplier: str | Decimal | int | None,
) -> str | None:
    """Convert native futures contracts to canonical coin quantity."""
    return canonical_qty(contract_quantity, multiplier)


def contract_notional_usd(
    native_price: str | Decimal | int,
    contract_qty: str | Decimal | int,
    fx_to_usd: str | Decimal | int | None,
) -> str | None:
    """Contract notional ``P_native * Q_contract * FX``.

    Returns ``None`` when ``FX`` is unknown (never default ``1``).
    """
    if fx_to_usd is None:
        return None
    price = _parse_decimal(native_price, "native_price")
    qty = _parse_decimal(contract_qty, "contract_qty")
    fx = _parse_fx(fx_to_usd)
    assert fx is not None
    with localcontext() as ctx:
        ctx.prec = 80
        out = price * qty * fx
    return _dec_str(out)


def unscaled_notional_usd(
    price: str | Decimal | int,
    qty: str | Decimal | int,
    fx_to_usd: str | Decimal | int | None,
) -> str | None:
    """Quote-notional ``price * qty * FX`` with NO multiplier scaling.

    Quote Volume and OI notionals are already quote-notional and must use
    this helper (which takes no multiplier argument, so a 1000x contract
    cannot leak into them). Returns ``None`` when ``FX`` is unknown.
    """
    if fx_to_usd is None:
        return None
    p = _parse_decimal(price, "price")
    q = _parse_decimal(qty, "qty")
    fx = _parse_fx(fx_to_usd)
    assert fx is not None
    with localcontext() as ctx:
        ctx.prec = 80
        out = p * q * fx
    return _dec_str(out)


def quote_volume_usd(
    price: str | Decimal | int,
    qty: str | Decimal | int,
    fx_to_usd: str | Decimal | int | None,
) -> str | None:
    """Quote Volume notional (never scaled by ``m``). Alias of :func:`unscaled_notional_usd`."""
    return unscaled_notional_usd(price, qty, fx_to_usd)


def oi_notional_usd(
    price: str | Decimal | int,
    qty: str | Decimal | int,
    fx_to_usd: str | Decimal | int | None,
) -> str | None:
    """OI notional (never scaled by ``m``). Alias of :func:`unscaled_notional_usd`."""
    return unscaled_notional_usd(price, qty, fx_to_usd)


# ---------------------------------------------------------------------------
# Liquidation distance + STOP reference (D06.1).
# ---------------------------------------------------------------------------


def native_liquidation_distance(mark: str | Decimal | int, liquidation: str | Decimal | int) -> str:
    """Native liquidation distance ``(L - Mark) / Mark``.

    Both legs are exchange-contract native quote prices. ``Mark <= 0``
    raises; ``L <= Mark`` returns the computed ``<= 0`` distance (already
    crossed) so scenario checks can mark ``INVALID_AFTER_LIQUIDATION``
    instead of inventing a positive distance.
    """
    m = _parse_decimal(mark, "mark")
    liq = _parse_decimal(liquidation, "liquidation")
    if m <= 0:
        raise ValueError(f"mark must be > 0, got {mark!r}")
    if liq <= 0:
        raise ValueError(f"liquidation must be > 0, got {liquidation!r}")
    with localcontext() as ctx:
        ctx.prec = 80
        out = (liq - m) / m
    return _dec_str(out)


def native_stop_reference(
    mark: str | Decimal | int,
    liquidation: str | Decimal | int,
    tick: str | Decimal | int,
) -> str | None:
    """Default STOP reference ``(Mark + L) / 2`` tick-aligned UP for BUY.

    Uses STOP_MARKET native tick with ``ROUND_CEILING``. Requires
    ``Mark < Stop < L`` strictly; a too-coarse tick with no legal value
    returns ``None`` (caller records ``STOP_PRICE_UNREPRESENTABLE``).
    ``Mark >= L`` also returns ``None``. Raises for non-positive tick.
    """
    m = _parse_decimal(mark, "mark")
    liq = _parse_decimal(liquidation, "liquidation")
    t = _parse_decimal(tick, "tick_size")
    if m <= 0:
        raise ValueError(f"mark must be > 0, got {mark!r}")
    if liq <= 0:
        raise ValueError(f"liquidation must be > 0, got {liquidation!r}")
    if t <= 0:
        raise ValueError(f"tick_size must be > 0, got {tick!r}")
    if m >= liq:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        mid = (m + liq) / Decimal("2")
        units = (mid / t).to_integral_value(rounding=ROUND_CEILING)
        stop = units * t
    # Strictly inside (Mark, L); equality on either side is unrepresentable.
    if stop <= m or stop >= liq:
        return None
    return _dec_str(stop)


# ---------------------------------------------------------------------------
# Contract VWAP (D18.1: units.py, per-side native contracts).
# ---------------------------------------------------------------------------


def _extract_book_sides(book: Any) -> tuple[Any, Any]:
    """Return ``(bids, asks)`` verbatim level lists from an Observed/mapping."""
    value: Any = None
    if hasattr(book, "value"):
        value = getattr(book, "value")
    elif isinstance(book, Mapping):
        inner = book.get("value")
        if isinstance(inner, Mapping) and ("bids" in inner or "asks" in inner):
            value = inner
        elif "bids" in book or "asks" in book:
            value = book
        else:
            raise ValueError("book mapping must carry bids/asks (or a value envelope)")
    else:
        raise ValueError(f"book must be Observed or mapping, got {type(book).__name__}")
    if not isinstance(value, Mapping):
        raise ValueError("book value must be a mapping with bids/asks")
    bids = value.get("bids")
    asks = value.get("asks")
    if bids is None or asks is None:
        raise ValueError("book value must carry both bids and asks separately")
    return bids, asks


def _parse_level(level: Any) -> tuple[Decimal, Decimal] | None:
    """Parse one ``[price, qty]`` level; ``None`` when invalid/non-positive."""
    try:
        if isinstance(level, Mapping):
            raw_p = level.get("price", level.get("p"))
            raw_q = level.get("qty", level.get("q", level.get("quantity")))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            raw_p, raw_q = level[0], level[1]
        else:
            return None
        if isinstance(raw_p, bool) or isinstance(raw_q, bool):
            return None
        if isinstance(raw_p, float) or isinstance(raw_q, float):
            # Production uses decimal strings; accept floats via str() for
            # robustness but never via binary arithmetic.
            p = Decimal(str(raw_p))
            q = Decimal(str(raw_q))
        elif isinstance(raw_p, Decimal) and isinstance(raw_q, Decimal):
            p, q = raw_p, raw_q
        elif isinstance(raw_p, int) and isinstance(raw_q, int):
            p, q = Decimal(raw_p), Decimal(raw_q)
        else:
            p = Decimal(str(raw_p).strip()) if isinstance(raw_p, str) else Decimal(str(raw_p))
            q = Decimal(str(raw_q).strip()) if isinstance(raw_q, str) else Decimal(str(raw_q))
    except (InvalidOperation, ValueError, ArithmeticError, TypeError, AttributeError):
        return None
    if not p.is_finite() or not q.is_finite():
        return None
    if p <= 0 or q <= 0:
        return None
    return p, q


def compute_contract_vwap(
    book: Any,
    contract_qty: str | Decimal | int,
    side: str,
) -> Mapping[str, Any]:
    """Per-side contract VWAP over native contracts (D18.1).

    ``side`` is ``BUY`` (consume ``asks``) or ``SELL`` (consume ``bids``);
    the opposite side is never merged into the total. Quantities are
    native contracts (never scaled by ``m``). Returns a mapping with
    ``vwap_native`` (``None`` when nothing executable),
    ``executable_contract_qty``, ``coverage`` (``executable/requested``),
    ``side`` and ``requested_contract_qty``.
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
    requested = _parse_decimal(contract_qty, "contract_qty")
    if requested <= 0:
        raise ValueError(f"contract_qty must be > 0, got {contract_qty!r}")
    bids, asks = _extract_book_sides(book)
    levels_raw = asks if side == "BUY" else bids
    if not isinstance(levels_raw, (list, tuple)):
        raise ValueError("book side levels must be a list")
    levels: list[tuple[Decimal, Decimal]] = []
    for row in levels_raw:
        parsed = _parse_level(row)
        if parsed is not None:
            levels.append(parsed)
    # Preserve venue order (bids descending, asks ascending as published);
    # never merge the opposite side.
    taken = Decimal("0")
    cost = Decimal("0")
    remaining = requested
    with localcontext() as ctx:
        ctx.prec = 80
        for price, qty in levels:
            if remaining <= 0:
                break
            take = qty if qty < remaining else remaining
            cost += take * price
            taken += take
            remaining -= take
        if taken > 0:
            vwap: Decimal | None = cost / taken
            coverage = taken / requested
        else:
            vwap = None
            coverage = Decimal("0")
    return {
        "vwap_native": _dec_str(vwap) if vwap is not None else None,
        "executable_contract_qty": _dec_str(taken),
        "coverage": _dec_str(coverage),
        "side": side,
        "requested_contract_qty": _dec_str(requested),
    }


def floor_to_step(qty: Decimal, step: Decimal | None) -> Decimal:
    """Floor ``qty`` down to a legal ``step`` (ROUND_DOWN).

    ``None``/``0`` keeps the venue-native "disabled" meaning (no flooring,
    never treat ``0`` as a legal step).
    """
    if step is None or step == 0:
        return qty
    if step < 0:
        raise ValueError(f"illegal negative step {step}")
    if qty < 0:
        raise ValueError("qty must be non-negative")
    with localcontext() as ctx:
        ctx.prec = 80
        units = (qty / step).to_integral_value(rounding=ROUND_DOWN)
        return units * step
