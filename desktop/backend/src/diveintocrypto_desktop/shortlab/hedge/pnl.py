"""R08a ledger PnL pure function (D09/D18.1, V11).

Pure module: no network, no DB, no clock reads. All amounts are Decimal
strings (D03.1); floats are rejected at the boundary. Maths runs under an
80-digit localcontext; formatting is canonical fixed-point (``-0`` -> ``0``).

``compute_ledger_pnl(events, identity, event_fx, market_context) -> LedgerPnl``
implements D09 moving-weighted cost:

- Corrections are resolved first (single-reverse guard, time-ordered), then
  moving-weighted cost. Quantity maths never reads DOUBLE balances.
- Futures PnL is in settlement quote; USD uses the close execution-time
  quote FX; unrealised uses current mark/FX. Margin FX is never merged.
- Spot USD entry cost is frozen at each buy with execution-time FX; exit or
  current FX never rewrites history. Base fees (net < gross) are already in
  ``net_qty`` and never deducted twice; quote/third-currency fees are
  independent costs in USD at execution-time FX.
- ``FundingReceipt`` actuals stay independent from unconfirmed estimates; a
  confirmed receipt with the same ``public_funding_event_id`` covers (and
  excludes) the estimate without double-counting.
- Unknown price/FX/fee/gas is ``None`` + ``unknown_components`` (never
  zero-filled). ``0`` must be explicit. ``funding_basis`` follows the D09
  truth table (any unconfirmed estimate -- even ``0`` -- is ``ESTIMATED``).
- ``known_net_subtotal`` is the partial sum of known legs; ``net_before`` /
  ``net_after`` are ``None`` whenever a necessary input is unknown.

Monitor/Exit guidance belongs to R08b and is not imported here.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping, Sequence

from diveintocrypto_desktop.shortlab.repair_contracts import LedgerPnl

__all__ = ["compute_ledger_pnl"]


# ---------------------------------------------------------------------------
# Decimal helpers (ledger boundary: never float).
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


def _fmt(value: Decimal) -> str:
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


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


_CAMEL_TO_SNAKE = {
    "eventId": "event_id",
    "legType": "leg_type",
    "eventType": "event_type",
    "nativeQty": "native_qty",
    "canonicalQty": "canonical_qty",
    "nativePrice": "native_price",
    "priceCurrency": "price_currency",
    "feeCurrency": "fee_currency",
    "feeAmount": "fee_amount",
    "feeUsd": "fee_usd",
    "gasUsd": "gas_usd",
    "executedAtMs": "executed_at_ms",
    "supersedesEventId": "supersedes_event_id",
    "reversesEventId": "reverses_event_id",
    "grossQty": "gross_qty",
    "netQty": "net_qty",
    "publicFundingEventId": "public_funding_event_id",
    "clientEventId": "client_event_id",
    "planId": "plan_id",
}


def _to_event_dict(raw: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(raw) and not isinstance(raw, type):
        return dict(dataclasses.asdict(raw))
    if isinstance(raw, Mapping):
        data: dict[str, Any] = {}
        for k, v in dict(raw).items():
            snake = _CAMEL_TO_SNAKE.get(k, k)
            if snake in data and k != snake:
                continue
            data[snake] = v
        return data
    raise ValueError("hedge event must be a mapping or frozen DTO")


def _canonicalise_event_type(event_type: Any, leg_type: Any, data: Mapping[str, Any]) -> Any:
    # Accept B19 shorthand OPEN/CLOSE/ADJUSTMENT like ledger.py; frozen names
    # pass through. Unknown stays as-is for unknown-component handling.
    if not isinstance(event_type, str):
        return event_type
    frozen = {
        "OPEN_FUTURES_SHORT",
        "OPEN_SPOT_LONG",
        "CLOSE_FUTURES_SHORT",
        "CLOSE_SPOT_LONG",
        "CORRECT_REVERSAL",
        "CORRECT_SUPERSEDE",
        "FUNDING_RECEIPT",
        "LIQUIDATION",
    }
    if event_type in frozen:
        return event_type
    if event_type == "OPEN" and leg_type == "FUTURES_SHORT":
        return "OPEN_FUTURES_SHORT"
    if event_type == "OPEN" and leg_type == "SPOT_LONG":
        return "OPEN_SPOT_LONG"
    if event_type == "CLOSE" and leg_type == "FUTURES_SHORT":
        return "CLOSE_FUTURES_SHORT"
    if event_type == "CLOSE" and leg_type == "SPOT_LONG":
        return "CLOSE_SPOT_LONG"
    if event_type == "ADJUSTMENT":
        has_rev = data.get("reverses_event_id") is not None
        has_sup = data.get("supersedes_event_id") is not None
        if has_rev and not has_sup:
            return "CORRECT_REVERSAL"
        return "CORRECT_SUPERSEDE"
    return event_type


def _multiplier_from_identity(identity: Any) -> Decimal | None:
    raw = _field(identity, "contract_multiplier", None)
    if raw is None:
        return None
    # Unverified multiplier (no source) must not carry ledger maths.
    src = _field(identity, "multiplier_source", None)
    if isinstance(identity, Mapping):
        # Mapping identity without explicit source is unverified.
        if src is None and raw is not None:
            return None
    else:
        if src is None and raw is not None:
            return None
    if isinstance(raw, bool):
        raise ValueError("contract_multiplier must be a positive number")
    if isinstance(raw, float):
        raise ValueError("contract_multiplier must be a decimal (float rejected)")
    try:
        mult = _parse_decimal(raw, "contract_multiplier")
    except ValueError:
        return None
    if mult <= 0 or not mult.is_finite():
        return None
    return mult


def _resolve_fx(currency: Any, fx_raw: Any) -> Decimal | None:
    # USD needs no external quote; USDT/USDC must not assume 1 (D18.1).
    if currency == "USD":
        return Decimal(1)
    if fx_raw is None:
        return None
    try:
        fx = _parse_decimal(fx_raw, "fx")
    except ValueError:
        return None
    if not fx.is_finite() or fx <= 0:
        return None
    return fx


def _is_estimated_funding_event(data: Mapping[str, Any]) -> bool:
    # Explicit estimate markers. Frozen FUNDING_RECEIPT has no estimated flag,
    # so estimates arrive either as a custom FUNDING_ESTIMATE type or with an
    # explicit estimated marker. Any presence (even amount 0) forces ESTIMATED.
    etype = data.get("event_type")
    if etype in ("FUNDING_ESTIMATE", "FUNDING_PROJECTION", "ESTIMATED_FUNDING"):
        return True
    for key in ("estimated", "is_estimate", "is_estimated", "unconfirmed"):
        val = data.get(key)
        if val is True:
            return True
        if isinstance(val, str) and val.strip().upper() in ("TRUE", "ESTIMATED", "UNCONFIRMED"):
            return True
    status = data.get("confirmation") or data.get("status")
    if isinstance(status, str) and status.strip().upper() in ("ESTIMATED", "UNCONFIRMED", "PROJECTED"):
        return True
    return False


def compute_ledger_pnl(
    events: Sequence[Mapping[str, Any]],
    identity: Any,
    event_fx: Mapping[str, Mapping[str, Any]],
    market_context: Mapping[str, Any],
) -> LedgerPnl:
    """Compute realised/unrealised/cost/funding PnL from effective events.

    Pure: no network, no DB, no clock. See module docstring for D09 rules.
    """
    if events is None:
        raise ValueError("events must be a sequence")
    seq = list(events)
    if event_fx is None or not isinstance(event_fx, Mapping):
        raise ValueError("event_fx must be a mapping")
    if market_context is None or not isinstance(market_context, Mapping):
        raise ValueError("market_context must be a mapping")
    fx_map: dict[str, Any] = dict(event_fx)
    mctx: dict[str, Any] = dict(market_context)

    try:
        as_of_ms = int(mctx.get("now_ms", 0))
    except (TypeError, ValueError):
        raise ValueError("market_context.now_ms must be an int")
    if isinstance(as_of_ms, bool) or as_of_ms < 0:
        raise ValueError("market_context.now_ms must be >= 0")

    multiplier = _multiplier_from_identity(identity)

    # -- normalise + time order (unknown timing never blocks) ----------------
    norm: list[dict[str, Any]] = []
    for idx, raw in enumerate(seq):
        data = _to_event_dict(raw)
        eid = data.get("event_id")
        if not isinstance(eid, str) or not eid:
            eid = f"pure-{idx}"
            data["event_id"] = eid
        leg = data.get("leg_type")
        etype = _canonicalise_event_type(data.get("event_type"), leg, data)
        data["event_type"] = etype
        data["leg_type"] = leg
        exec_ms = data.get("executed_at_ms")
        if not isinstance(exec_ms, int) or isinstance(exec_ms, bool):
            exec_ms = 0
        data["_sort_ms"] = exec_ms
        data["_sort_id"] = eid
        norm.append(data)
    norm.sort(key=lambda d: (d["_sort_ms"], d["_sort_id"]))
    by_id: dict[str, dict[str, Any]] = {d["event_id"]: d for d in norm}

    # -- correction guards (same single-reverse rule as the ledger) -----------
    reversed_targets: set[str] = set()
    for entry in norm:
        target = entry.get("reverses_event_id")
        if target is not None:
            if not isinstance(target, str) or not target:
                raise ValueError("reverses_event_id must be a non-empty str")
            if target in reversed_targets:
                raise ValueError("an event may only be reversed once")
            reversed_targets.add(target)

    unknown: set[str] = set()
    priced = 0
    total = 0

    # -- futures moving-weighted state (canonical units) ----------------------
    fut_open = Decimal(0)
    fut_cost_native = Decimal(0)  # quote units (settlement), canonical price base
    fut_realised_quote = Decimal(0)
    fut_realised_usd = Decimal(0)
    fut_realised_ok = True
    fut_avg_known = True

    # -- spot moving-weighted state (quote + frozen USD) ----------------------
    spot_inv = Decimal(0)
    spot_cost_quote = Decimal(0)
    spot_cost_usd = Decimal(0)
    spot_realised_quote = Decimal(0)
    spot_realised_usd = Decimal(0)
    spot_realised_ok = True
    spot_avg_known = True

    # -- fees ----------------------------------------------------------------
    known_cost_usd = Decimal(0)
    known_cost_has_known = False
    fee_unknown = False

    # -- funding ---------------------------------------------------------------
    actual_funding_usd = Decimal(0)
    actual_has = False
    actual_unknown = False
    estimated_usd = Decimal(0)
    estimated_has = False  # any unconfirmed estimate participates (even 0)
    estimated_unknown = False
    receipt_public_ids: set[str] = set()
    estimate_entries: list[dict[str, Any]] = []

    # First pass: collect receipt public ids for cover-dedup.
    for entry in norm:
        if entry.get("event_type") == "FUNDING_RECEIPT" and not _is_estimated_funding_event(entry):
            pub = entry.get("public_funding_event_id")
            if isinstance(pub, str) and pub:
                receipt_public_ids.add(pub)

    def _fx_for(eid: str) -> Mapping[str, Any]:
        val = fx_map.get(eid)
        if isinstance(val, Mapping):
            return val
        return {}

    def _note_unknown(code: str) -> None:
        unknown.add(code)

    # -- main walk --------------------------------------------------------------
    with localcontext() as ctx:
        ctx.prec = 80
        for entry in norm:
            eid = entry["event_id"]
            leg = entry.get("leg_type")
            etype = entry.get("event_type")
            fx_entry = _fx_for(eid)

            if etype == "FUNDING_RECEIPT" or _is_estimated_funding_event(entry):
                total += 1
                is_estimate = _is_estimated_funding_event(entry)
                # Covered estimates never double-count.
                if is_estimate:
                    pub = entry.get("public_funding_event_id")
                    if isinstance(pub, str) and pub and pub in receipt_public_ids:
                        priced += 1
                        continue
                    estimated_has = True
                amount_raw = entry.get("amount")
                currency = entry.get("currency")
                if amount_raw is None or currency is None:
                    if is_estimate:
                        estimated_unknown = True
                    else:
                        actual_unknown = True
                    _note_unknown("UNKNOWN_FUNDING_FX" if currency is None else "UNKNOWN_FX")
                    continue
                try:
                    amount = _parse_decimal(amount_raw, "amount")
                except ValueError:
                    if is_estimate:
                        estimated_unknown = True
                    else:
                        actual_unknown = True
                    _note_unknown("UNKNOWN_FX")
                    continue
                if not isinstance(currency, str) or not currency:
                    if is_estimate:
                        estimated_unknown = True
                    else:
                        actual_unknown = True
                    _note_unknown("UNKNOWN_FUNDING_FX")
                    continue
                ffx_raw = fx_entry.get("funding_fx", fx_entry.get("fundingFx"))
                if ffx_raw is None:
                    # Fall back to price_fx for fixtures that only carry price_fx.
                    ffx_raw = fx_entry.get("price_fx", fx_entry.get("priceFx"))
                ffx = _resolve_fx(currency, ffx_raw)
                if ffx is None:
                    if is_estimate:
                        estimated_unknown = True
                    else:
                        actual_unknown = True
                    _note_unknown("UNKNOWN_FX")
                    continue
                usd = amount * ffx
                priced += 1
                if is_estimate:
                    estimated_usd += usd
                else:
                    actual_funding_usd += usd
                    actual_has = True
                continue

            if leg not in ("FUTURES_SHORT", "SPOT_LONG"):
                total += 1
                _note_unknown("UNKNOWN_EVENT")
                continue
            if etype not in (
                "OPEN_FUTURES_SHORT",
                "OPEN_SPOT_LONG",
                "CLOSE_FUTURES_SHORT",
                "CLOSE_SPOT_LONG",
                "CORRECT_REVERSAL",
                "CORRECT_SUPERSEDE",
                "LIQUIDATION",
            ):
                total += 1
                _note_unknown("UNKNOWN_EVENT")
                continue
            total += 1

            # ---- quantity + price extraction --------------------------------
            is_correction = etype in ("CORRECT_REVERSAL", "CORRECT_SUPERSEDE")
            # Effective inventory qty + cost qty.
            qty_text: str | None = None
            cost_qty_text: str | None = None
            inv_qty = Decimal(0)
            cost_qty = Decimal(0)
            qty_ok = True
            try:
                if leg == "FUTURES_SHORT":
                    # Ledger preference: net -> canonical -> native*mult -> gross.
                    cand: str | None = None
                    for key in ("net_qty", "canonical_qty", "native_qty", "gross_qty"):
                        val = entry.get(key)
                        if val is not None and str(val).strip():
                            cand = str(val).strip()
                            if key == "native_qty" and entry.get("net_qty") is None and entry.get("canonical_qty") is None:
                                if multiplier is None:
                                    raise ValueError("native futures quantity requires a verified contract multiplier")
                                cand = _fmt(_parse_decimal(val, "native_qty") * multiplier)
                            break
                    if cand is None:
                        raise ValueError("non-funding events require a quantity")
                    qty_text = cand
                    cost_qty_text = cand
                    inv_qty = _parse_decimal(qty_text, "qty")
                    cost_qty = inv_qty
                    if inv_qty < 0:
                        raise ValueError("event quantity must be non-negative")
                else:
                    # Spot: inventory is net (base fee already deducted);
                    # spend uses gross when present.
                    inv_cand: str | None = None
                    for key in ("net_qty", "canonical_qty", "native_qty", "gross_qty"):
                        val = entry.get(key)
                        if val is not None and str(val).strip():
                            inv_cand = str(val).strip()
                            break
                    if inv_cand is None:
                        raise ValueError("non-funding events require a quantity")
                    gross_raw = entry.get("gross_qty")
                    if gross_raw is not None and str(gross_raw).strip():
                        cost_qty_text = str(gross_raw).strip()
                    else:
                        cost_qty_text = inv_cand
                    qty_text = inv_cand
                    inv_qty = _parse_decimal(qty_text, "qty")
                    cost_qty = _parse_decimal(cost_qty_text, "qty")
                    if inv_qty < 0 or cost_qty < 0:
                        raise ValueError("event quantity must be non-negative")
            except ValueError as exc:
                # Structural qty errors fail loudly (exact, no epsilon).
                msg = str(exc)
                if "verified contract multiplier" in msg:
                    qty_ok = False
                    _note_unknown("UNKNOWN_FX")
                    if leg == "FUTURES_SHORT":
                        fut_realised_ok = False
                        fut_avg_known = False
                    # Fees still evaluated below; skip qty maths.
                elif "require a quantity" in msg or "non-negative" in msg:
                    raise
                else:
                    raise
                qty_ok = False

            price_raw = entry.get("native_price")
            price_ccy = entry.get("price_currency")
            price_known = True
            price_dec = Decimal(0)
            price_fx_dec: Decimal | None = None
            if qty_ok:
                if price_raw is None or (isinstance(price_raw, str) and not price_raw.strip()):
                    price_known = False
                    _note_unknown("UNKNOWN_PRICE")
                else:
                    try:
                        price_dec = _parse_decimal(price_raw, "native_price")
                    except ValueError:
                        price_known = False
                        _note_unknown("UNKNOWN_PRICE")
                if price_ccy is None or (isinstance(price_ccy, str) and not str(price_ccy).strip()):
                    # Missing currency cannot prove FX.
                    if price_known:
                        price_known = False
                        _note_unknown("UNKNOWN_FX")
                else:
                    pfx_raw = fx_entry.get("price_fx", fx_entry.get("priceFx"))
                    price_fx_dec = _resolve_fx(str(price_ccy), pfx_raw)
                    if price_fx_dec is None and price_known:
                        price_known = False
                        _note_unknown("UNKNOWN_FX")
                # Futures canonical price alignment (qty*m, price/m).
                if leg == "FUTURES_SHORT" and price_known and multiplier is not None:
                    # Only align when qty came from native contracts; when qty
                    # is already canonical tokens the stored native_price is
                    # per-contract and must still be aligned. Ledger always
                    # aligns futures prices, so mirror it.
                    price_dec = price_dec / multiplier

            event_priced = bool(qty_ok and price_known)
            if event_priced:
                priced += 1
            else:
                _note_unknown("PARTIAL_COVERAGE")

            # ---- fees (counted once; base fee already in net_qty) ------------
            fee_ccy = entry.get("fee_currency")
            fee_amt_raw = entry.get("fee_amount")
            fee_usd_raw = entry.get("fee_usd")
            gas_raw = entry.get("gas_usd")
            # Base-fee detection: net < gross means the base fee/transfer tax
            # already reduced inventory; do not add the fee again.
            base_fee_skip = False
            try:
                gross_r = entry.get("gross_qty")
                net_r = entry.get("net_qty")
                if gross_r is not None and net_r is not None:
                    g = _parse_decimal(gross_r, "gross_qty")
                    n = _parse_decimal(net_r, "net_qty")
                    if n < g:
                        base_fee_skip = True
            except ValueError:
                pass
            if not base_fee_skip:
                if fee_amt_raw is None and fee_usd_raw is None:
                    fee_unknown = True
                    _note_unknown("UNKNOWN_FEE")
                elif fee_amt_raw is not None and fee_usd_raw is not None:
                    try:
                        amt = _parse_decimal(fee_amt_raw, "fee_amount")
                        stated = _parse_decimal(fee_usd_raw, "fee_usd")
                    except ValueError:
                        fee_unknown = True
                        _note_unknown("UNKNOWN_FEE")
                    else:
                        if fee_ccy == "USD":
                            expected = amt
                        else:
                            ffx_raw = fx_entry.get("fee_fx", fx_entry.get("feeFx"))
                            if ffx_raw is None:
                                ffx_raw = fx_entry.get("price_fx", fx_entry.get("priceFx"))
                            ffx = _resolve_fx(fee_ccy, ffx_raw) if isinstance(fee_ccy, str) and fee_ccy else None
                            if ffx is None:
                                fee_unknown = True
                                _note_unknown("UNKNOWN_FX")
                                expected = None
                            else:
                                expected = amt * ffx
                        if expected is not None:
                            tol = Decimal("0.00000001") * max(Decimal(1), abs(expected), abs(stated))
                            if abs(expected - stated) > tol:
                                fee_unknown = True
                                _note_unknown("UNKNOWN_FEE")
                            else:
                                known_cost_usd += stated
                                known_cost_has_known = True
                elif fee_amt_raw is not None:
                    try:
                        amt = _parse_decimal(fee_amt_raw, "fee_amount")
                    except ValueError:
                        fee_unknown = True
                        _note_unknown("UNKNOWN_FEE")
                    else:
                        if fee_ccy == "USD":
                            known_cost_usd += amt
                            known_cost_has_known = True
                        else:
                            ffx_raw = fx_entry.get("fee_fx", fx_entry.get("feeFx"))
                            if ffx_raw is None:
                                ffx_raw = fx_entry.get("price_fx", fx_entry.get("priceFx"))
                            ffx = _resolve_fx(fee_ccy, ffx_raw) if isinstance(fee_ccy, str) and fee_ccy else None
                            if ffx is None:
                                fee_unknown = True
                                _note_unknown("UNKNOWN_FX")
                            else:
                                known_cost_usd += amt * ffx
                                known_cost_has_known = True
                else:  # only fee_usd
                    try:
                        stated = _parse_decimal(fee_usd_raw, "fee_usd")
                    except ValueError:
                        fee_unknown = True
                        _note_unknown("UNKNOWN_FEE")
                    else:
                        known_cost_usd += stated
                        known_cost_has_known = True
            # Gas: explicit 0 is known; missing is unknown (never default-free).
            if gas_raw is None:
                fee_unknown = True
                _note_unknown("UNKNOWN_FEE")
            else:
                try:
                    gas_dec = _parse_decimal(gas_raw, "gas_usd")
                except ValueError:
                    fee_unknown = True
                    _note_unknown("UNKNOWN_FEE")
                else:
                    known_cost_usd += gas_dec
                    known_cost_has_known = True

            if not qty_ok:
                # Multiplier unknown: qty maths skipped, fees already handled.
                continue

            # ---- moving-weighted maths ---------------------------------------
            if leg == "FUTURES_SHORT":
                if etype in ("OPEN_FUTURES_SHORT",):
                    if not price_known:
                        fut_avg_known = False
                        fut_realised_ok = False
                        # Inventory still accrues for coverage/remaining, but
                        # cost basis is unknown so later realised/unrealised
                        # must be null.
                        fut_open += inv_qty
                    else:
                        fut_open += inv_qty
                        fut_cost_native += cost_qty * price_dec
                elif etype in ("CLOSE_FUTURES_SHORT", "LIQUIDATION"):
                    if fut_open - inv_qty < 0 and (fut_open - inv_qty) != 0:
                        # Exact over-close guard (no epsilon).
                        if inv_qty > fut_open:
                            raise ValueError("cannot close more than the remaining quantity")
                    if not price_known or not fut_avg_known or fut_open <= 0:
                        fut_realised_ok = False
                        if fut_open > 0 and price_known and fut_avg_known:
                            avg = fut_cost_native / fut_open
                            fut_cost_native -= inv_qty * avg
                            fut_open -= inv_qty
                        elif fut_open > 0:
                            # Unknown cost: consume inventory without basis.
                            fut_open -= inv_qty
                        else:
                            raise ValueError("cannot close more than the remaining quantity")
                    else:
                        avg = fut_cost_native / fut_open if fut_open != 0 else Decimal(0)
                        realised_q = inv_qty * (avg - price_dec)
                        fut_realised_quote += realised_q
                        # USD at execution-time quote FX.
                        if price_fx_dec is None:
                            fut_realised_ok = False
                            _note_unknown("UNKNOWN_FX")
                        else:
                            fut_realised_usd += realised_q * price_fx_dec
                        fut_cost_native -= inv_qty * avg
                        fut_open -= inv_qty
                else:  # corrections: consume cost without proceeds
                    target = entry.get("reverses_event_id") or entry.get("supersedes_event_id")
                    target_entry = by_id.get(target) if isinstance(target, str) else None
                    target_type = (target_entry or {}).get("event_type") if target_entry else None
                    if target_type in ("CLOSE_FUTURES_SHORT", "CLOSE_SPOT_LONG", "LIQUIDATION"):
                        # Reversal of a close restores inventory at avg cost.
                        if fut_avg_known and fut_open > 0:
                            avg = fut_cost_native / fut_open
                            fut_open += inv_qty
                            fut_cost_native += inv_qty * avg
                        elif fut_open == 0 and fut_avg_known:
                            # No basis to restore; inventory returns without cost.
                            fut_open += inv_qty
                        else:
                            fut_open += inv_qty
                            fut_avg_known = False
                            fut_realised_ok = False
                    else:
                        if inv_qty > fut_open:
                            raise ValueError("cannot close more than the remaining quantity")
                        if fut_avg_known and fut_open > 0:
                            avg = fut_cost_native / fut_open
                            fut_cost_native -= inv_qty * avg
                            fut_open -= inv_qty
                        else:
                            fut_open -= inv_qty
                            fut_avg_known = False
            else:  # SPOT_LONG
                if etype in ("OPEN_SPOT_LONG",):
                    if not price_known:
                        spot_avg_known = False
                        spot_realised_ok = False
                        spot_inv += inv_qty
                    else:
                        cost_q = cost_qty * price_dec
                        cost_u = cost_q * price_fx_dec if price_fx_dec is not None else None
                        if cost_u is None:
                            spot_avg_known = False
                            spot_realised_ok = False
                            spot_inv += inv_qty
                            # Quote cost still accrues for display; USD unknown.
                            spot_cost_quote += cost_q
                        else:
                            spot_inv += inv_qty
                            spot_cost_quote += cost_q
                            spot_cost_usd += cost_u
                elif etype in ("CLOSE_SPOT_LONG", "LIQUIDATION"):
                    if inv_qty > spot_inv:
                        raise ValueError("cannot close more than the remaining quantity")
                    if not price_known or not spot_avg_known or spot_inv <= 0:
                        spot_realised_ok = False
                        if spot_inv > 0 and spot_avg_known:
                            avg_q = spot_cost_quote / spot_inv
                            avg_u = spot_cost_usd / spot_inv
                            spot_cost_quote -= inv_qty * avg_q
                            spot_cost_usd -= inv_qty * avg_u
                            spot_inv -= inv_qty
                        elif spot_inv > 0:
                            spot_inv -= inv_qty
                        else:
                            raise ValueError("cannot close more than the remaining quantity")
                    else:
                        avg_q = spot_cost_quote / spot_inv if spot_inv != 0 else Decimal(0)
                        avg_u = spot_cost_usd / spot_inv if spot_inv != 0 else Decimal(0)
                        proceeds_q = inv_qty * price_dec
                        allocated_q = inv_qty * avg_q
                        spot_realised_quote += proceeds_q - allocated_q
                        if price_fx_dec is None:
                            spot_realised_ok = False
                            _note_unknown("UNKNOWN_FX")
                        else:
                            proceeds_u = proceeds_q * price_fx_dec
                            allocated_u = inv_qty * avg_u
                            spot_realised_usd += proceeds_u - allocated_u
                        spot_cost_quote -= inv_qty * avg_q
                        spot_cost_usd -= inv_qty * avg_u
                        spot_inv -= inv_qty
                else:  # corrections without proceeds
                    target = entry.get("reverses_event_id") or entry.get("supersedes_event_id")
                    target_entry = by_id.get(target) if isinstance(target, str) else None
                    target_type = (target_entry or {}).get("event_type") if target_entry else None
                    if target_type in ("CLOSE_FUTURES_SHORT", "CLOSE_SPOT_LONG", "LIQUIDATION"):
                        if spot_avg_known and spot_inv > 0:
                            avg_q = spot_cost_quote / spot_inv
                            avg_u = spot_cost_usd / spot_inv
                            spot_inv += inv_qty
                            spot_cost_quote += inv_qty * avg_q
                            spot_cost_usd += inv_qty * avg_u
                        else:
                            spot_inv += inv_qty
                            spot_avg_known = False
                            spot_realised_ok = False
                    else:
                        if inv_qty > spot_inv:
                            raise ValueError("cannot close more than the remaining quantity")
                        if spot_avg_known and spot_inv > 0:
                            avg_q = spot_cost_quote / spot_inv
                            avg_u = spot_cost_usd / spot_inv
                            spot_cost_quote -= inv_qty * avg_q
                            spot_cost_usd -= inv_qty * avg_u
                            spot_inv -= inv_qty
                        else:
                            spot_inv -= inv_qty
                            spot_avg_known = False

    # -- market context (current marks + exit cost) -----------------------------
    fut_mark_raw = mctx.get("futures_mark_native")
    fut_cur_fx_raw = mctx.get("futures_quote_fx")
    spot_vwap_raw = mctx.get("spot_sell_vwap_native")
    spot_cur_fx_raw = mctx.get("spot_quote_fx")
    exit_fee_raw = mctx.get("estimated_exit_fee_usd")

    # Infer quote currencies from events when possible (first known price ccy).
    fut_quote_ccy: Any = None
    spot_quote_ccy: Any = None
    for entry in norm:
        if entry.get("event_type") == "FUNDING_RECEIPT":
            continue
        if entry.get("leg_type") == "FUTURES_SHORT" and fut_quote_ccy is None:
            ccy = entry.get("price_currency")
            if isinstance(ccy, str) and ccy:
                fut_quote_ccy = ccy
        if entry.get("leg_type") == "SPOT_LONG" and spot_quote_ccy is None:
            ccy = entry.get("price_currency")
            if isinstance(ccy, str) and ccy:
                spot_quote_ccy = ccy

    # -- realised components ----------------------------------------------------
    realised_fut: str | None = None
    realised_spot: str | None = None
    if fut_realised_ok and fut_avg_known:
        realised_fut = _fmt(fut_realised_usd)
    else:
        if total > 0:
            # No futures closes is a known zero, not unknown.
            has_fut_close = any(
                d.get("leg_type") == "FUTURES_SHORT"
                and d.get("event_type") in ("CLOSE_FUTURES_SHORT", "LIQUIDATION")
                for d in norm
            )
            has_fut_any = any(d.get("leg_type") == "FUTURES_SHORT" and d.get("event_type") != "FUNDING_RECEIPT" for d in norm)
            if not has_fut_any or (not has_fut_close and fut_avg_known):
                realised_fut = "0"
            else:
                realised_fut = None
                _note_unknown("PARTIAL_COVERAGE")
        else:
            realised_fut = "0"
    if spot_realised_ok and spot_avg_known:
        realised_spot = _fmt(spot_realised_usd)
    else:
        if total > 0:
            has_spot_close = any(
                d.get("leg_type") == "SPOT_LONG"
                and d.get("event_type") in ("CLOSE_SPOT_LONG", "LIQUIDATION")
                for d in norm
            )
            has_spot_any = any(d.get("leg_type") == "SPOT_LONG" and d.get("event_type") != "FUNDING_RECEIPT" for d in norm)
            if not has_spot_any or (not has_spot_close and spot_avg_known):
                realised_spot = "0"
            else:
                realised_spot = None
                _note_unknown("PARTIAL_COVERAGE")
        else:
            realised_spot = "0"

    # -- unrealised ---------------------------------------------------------------
    unreal_fut: str | None = None
    unreal_spot: str | None = None
    if fut_open == 0:
        unreal_fut = "0"
    elif not fut_avg_known:
        unreal_fut = None
        _note_unknown("PARTIAL_COVERAGE")
    else:
        if fut_mark_raw is None:
            unreal_fut = None
            _note_unknown("UNKNOWN_PRICE")
        else:
            try:
                mark = _parse_decimal(fut_mark_raw, "futures_mark_native")
            except ValueError:
                unreal_fut = None
                _note_unknown("UNKNOWN_PRICE")
            else:
                if multiplier is not None:
                    mark = mark / multiplier
                avg = fut_cost_native / fut_open if fut_open != 0 else Decimal(0)
                unreal_q = fut_open * (avg - mark)
                cur_fx = _resolve_fx(fut_quote_ccy or "USDT", fut_cur_fx_raw)
                # USD quote without explicit FX still needs proof unless USD.
                if fut_quote_ccy == "USD":
                    cur_fx = Decimal(1)
                if cur_fx is None:
                    unreal_fut = None
                    _note_unknown("UNKNOWN_FX")
                else:
                    unreal_fut = _fmt(unreal_q * cur_fx)
    if spot_inv == 0:
        unreal_spot = "0"
    elif not spot_avg_known:
        unreal_spot = None
        _note_unknown("PARTIAL_COVERAGE")
    else:
        if spot_vwap_raw is None:
            unreal_spot = None
            _note_unknown("UNKNOWN_PRICE")
        else:
            try:
                vwap = _parse_decimal(spot_vwap_raw, "spot_sell_vwap_native")
            except ValueError:
                unreal_spot = None
                _note_unknown("UNKNOWN_PRICE")
            else:
                cur_fx = _resolve_fx(spot_quote_ccy or "USDT", spot_cur_fx_raw)
                if spot_quote_ccy == "USD":
                    cur_fx = Decimal(1)
                if cur_fx is None:
                    unreal_spot = None
                    _note_unknown("UNKNOWN_FX")
                else:
                    unreal_spot = _fmt(spot_inv * vwap * cur_fx - spot_cost_usd)

    # -- funding outputs ------------------------------------------------------------
    # Market-context unconfirmed estimates (even "0") force ESTIMATED.
    mctx_estimate_present = False
    mctx_estimate_usd: Decimal | None = None
    for key in ("estimated_unconfirmed_funding_usd", "estimated_funding_usd", "unconfirmed_funding_usd"):
        val = mctx.get(key)
        if val is not None:
            mctx_estimate_present = True
            try:
                mctx_estimate_usd = _parse_decimal(val, key)
            except ValueError:
                estimated_unknown = True
                _note_unknown("UNKNOWN_FX")
                mctx_estimate_usd = None
            break
    if isinstance(mctx.get("funding_estimates"), (list, tuple)) and mctx.get("funding_estimates"):
        mctx_estimate_present = True
        # Sum if decimal strings; unknown entries mark unknown.
        tot = Decimal(0)
        ok_any = False
        for item in mctx["funding_estimates"]:
            try:
                tot += _parse_decimal(item, "funding_estimate")
                ok_any = True
            except ValueError:
                estimated_unknown = True
                _note_unknown("UNKNOWN_FX")
        if ok_any:
            estimated_usd += tot
            estimated_has = True
    if mctx_estimate_present and mctx_estimate_usd is not None:
        estimated_usd += mctx_estimate_usd
        estimated_has = True

    if estimated_has and not estimated_unknown:
        # estimated_usd already summed (including zero).
        pass

    if actual_unknown:
        actual_funding: str | None = None
    elif not actual_has and not estimated_has:
        actual_funding = "0"
    elif not actual_has and estimated_has:
        # No receipts but estimates exist: actual leg is known zero.
        actual_funding = "0"
    else:
        actual_funding = _fmt(actual_funding_usd)

    if estimated_has:
        if estimated_unknown:
            estimated_out: str | None = None
        else:
            estimated_out = _fmt(estimated_usd)
    else:
        # No unconfirmed estimates: explicit null (not zero-filled as funding).
        estimated_out = None
    if estimated_unknown:
        _note_unknown("UNKNOWN_FX")

    if estimated_has:
        funding_basis = "ESTIMATED"
    else:
        funding_basis = "ACTUAL_RECEIPTS_ONLY"

    # -- costs -----------------------------------------------------------------------
    if fee_unknown:
        if known_cost_has_known:
            known_cost: str | None = _fmt(known_cost_usd)
        else:
            known_cost = None
        # UNKNOWN_FEE already noted; ensure coverage partial.
        _note_unknown("PARTIAL_COVERAGE")
    else:
        known_cost = _fmt(known_cost_usd)

    if exit_fee_raw is None:
        estimated_exit: str | None = None
        # Missing exit quote is unknown only when there is remaining inventory.
        if fut_open > 0 or spot_inv > 0:
            _note_unknown("UNKNOWN_EXIT_COST")
            _note_unknown("PARTIAL_COVERAGE")
        else:
            estimated_exit = "0"
            # No remaining: exit cost known zero without quote.
            if exit_fee_raw is None and fut_open == 0 and spot_inv == 0:
                pass
        if fut_open > 0 or spot_inv > 0:
            estimated_exit = None
    else:
        try:
            estimated_exit = _fmt(_parse_decimal(exit_fee_raw, "estimated_exit_fee_usd"))
        except ValueError:
            estimated_exit = None
            _note_unknown("UNKNOWN_EXIT_COST")

    # -- nets --------------------------------------------------------------------------
    necessary_unknown = (
        realised_fut is None
        or realised_spot is None
        or actual_funding is None
        or known_cost is None
        or fee_unknown
        or actual_unknown
        or (not fut_avg_known)
        or (not spot_avg_known)
    )
    # Partial sums for the visible subtotal (unknowns as 0, but flagged).
    def _opt(s: str | None) -> Decimal:
        return _parse_decimal(s, "part") if s is not None else Decimal(0)

    if realised_fut is None and realised_spot is None and actual_funding is None and known_cost is None and total > 0:
        known_subtotal: str | None = None
    else:
        sub = _opt(realised_fut) + _opt(realised_spot) + _opt(actual_funding) - _opt(known_cost)
        known_subtotal = _fmt(sub)
        # If every input is unknown and sub is 0 from emptiness, keep "0" only
        # when there are no unknowns; otherwise the subtotal itself is null.
        if not known_cost_has_known and realised_fut is None and realised_spot is None and actual_funding is None:
            known_subtotal = None

    if necessary_unknown:
        net_before: str | None = None
        _note_unknown("PARTIAL_COVERAGE")
    else:
        assert realised_fut is not None and realised_spot is not None
        assert actual_funding is not None and known_cost is not None
        net_before = _fmt(
            _parse_decimal(realised_fut, "r")
            + _parse_decimal(realised_spot, "r")
            + _parse_decimal(actual_funding, "r")
            - _parse_decimal(known_cost, "r")
        )
    if net_before is None or estimated_exit is None:
        net_after: str | None = None
        if (fut_open > 0 or spot_inv > 0) and estimated_exit is None:
            _note_unknown("PARTIAL_COVERAGE")
    else:
        net_after = _fmt(_parse_decimal(net_before, "n") - _parse_decimal(estimated_exit, "e"))

    # -- coverage -----------------------------------------------------------------------
    if total == 0:
        coverage = {"priced_events": 0, "total_events": 0, "status": "COMPLETE"}
    else:
        status = "COMPLETE" if (priced == total and not unknown) else "PARTIAL"
        if priced != total:
            _note_unknown("PARTIAL_COVERAGE")
        coverage = {"priced_events": priced, "total_events": total, "status": status}

    return LedgerPnl(
        realized_futures_usd=realised_fut,
        realized_spot_usd=realised_spot,
        unrealized_futures_usd=unreal_fut,
        unrealized_spot_usd=unreal_spot,
        actual_funding_usd=actual_funding,
        estimated_unconfirmed_funding_usd=estimated_out,
        known_cost_usd=known_cost,
        estimated_exit_cost_usd=estimated_exit,
        known_net_subtotal_usd=known_subtotal,
        net_before_exit_usd=net_before,
        net_after_exit_usd=net_after,
        unknown_components=tuple(sorted(unknown)),
        coverage=dict(coverage),
        funding_basis=funding_basis,
        as_of_ms=as_of_ms,
    )
