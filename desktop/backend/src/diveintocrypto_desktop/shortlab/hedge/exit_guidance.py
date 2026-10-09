"""R08b pair exit guidance (D10/D18.1, V06).

Pure module: no network, no DB, no clock reads. All amounts are Decimal
strings (D03.1); floats are rejected at the boundary. Maths runs under an
80-digit localcontext; formatting is canonical fixed-point (``-0`` -> ``0``).

``build_pair_exit_guidance(plan, positions, market_context, rules, now_ms)``
is read-only and never produces trades/close events:

- Futures leg is ``BUY`` with ``reduce_only=True``; spot leg is ``SELL``.
- Native quantities never exceed the true ``remaining`` from effective event
  aggregation (``positions`` must come from :func:`ledger.aggregate_events`,
  never UI self-reports) and are floored to the native ``step_size``.
- Sub-step remainders are listed separately in ``unexecutable_dust`` and the
  position is never claimed as closed/zeroed.
- A single remaining leg after both were opened is an orphan
  (``CRITICAL_ORPHAN_*`` + ``URGENT_PAIR_EXIT``); a leg that was never opened
  is ``ORPHAN_LEG_WARNING``. Manual ``LIQUIDATION`` only closes the true
  futures quantity -- the surviving spot leg keeps its single-leg SELL
  guidance.
- A Mark touching the user liquidation price only yields
  ``POSSIBLE_LIQUIDATION_UNCONFIRMED`` -- never an auto liquidation and never
  a quantity rewrite.
- Missing quotes never hide the known remaining: capability ``UNKNOWN`` and
  no executable claim.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext, ROUND_DOWN
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.repair_contracts import PairExitGuidance

__all__ = ["build_pair_exit_guidance"]


# ---------------------------------------------------------------------------
# Decimal helpers (exit boundary: never float for qty/price).
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
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal string, got {value!r}")
    if isinstance(value, float):
        raise ValueError(f"{name} must be a decimal string (float rejected)")
    if isinstance(value, (int, Decimal)):
        parsed = Decimal(value) if isinstance(value, int) else value
        if not parsed.is_finite():
            raise ValueError(f"{name} must be finite")
        return parsed
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError):
            return None
        if not parsed.is_finite():
            return None
        return parsed
    return None


def _fmt(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(value)
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


def _plan_field(plan: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(plan, Mapping):
            if name in plan and plan[name] is not None:
                return plan[name]
        elif plan is not None and hasattr(plan, name):
            value = getattr(plan, name)
            if value is not None:
                return value
    return default


def _floor_to_step(qty: Decimal, step: Decimal | None) -> Decimal:
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


def _rules_for_leg(rules: Any, leg_type: str) -> Mapping[str, Any] | None:
    if rules is None:
        return None
    if isinstance(rules, Mapping):
        if leg_type in rules and isinstance(rules[leg_type], Mapping):
            return rules[leg_type]  # type: ignore[return-value]
        if any(k in rules for k in ("lot_rules", "step_size", "min_qty", "venue")):
            return rules  # type: ignore[return-value]
        return None
    leg = getattr(rules, "leg_type", None)
    if leg is not None and leg != leg_type:
        return None
    return rules  # type: ignore[return-value]


def _step_for_leg(leg_rules: Any) -> Decimal | None:
    if leg_rules is None:
        return None
    lot: Any = None
    if isinstance(leg_rules, Mapping):
        lot = leg_rules.get("lot_rules")
        if lot is None:
            # Flat form: step_size at top level.
            for key in ("step_size", "stepSize"):
                if leg_rules.get(key) is not None:
                    lot = leg_rules
                    break
    else:
        lot = getattr(leg_rules, "lot_rules", None)
        if lot is None:
            return None
    raw: Any = None
    if isinstance(lot, Mapping):
        raw = lot.get("step_size", lot.get("stepSize"))
    else:
        raw = getattr(lot, "step_size", None)
    if raw is None:
        return None
    if isinstance(raw, bool) or isinstance(raw, float):
        return None
    try:
        text = str(raw).strip() if isinstance(raw, str) else str(raw)
        parsed = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    if parsed == 0:
        return None
    return parsed


def _qty_currency_for_plan(plan: Any) -> str:
    symbol = _plan_field(plan, "symbol", "display_symbol", default=None)
    if not isinstance(symbol, str) or not symbol.strip():
        return "NATIVE"
    text = symbol.strip().upper()
    for suffix in ("USDT", "USDC", "USD"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    # Strip leading contract multipliers like 1000PEPE -> PEPE.
    stripped = text.lstrip("0123456789")
    return stripped or text


def _positions_by_leg(positions: Any) -> dict[str, dict[str, Decimal]]:
    out: dict[str, dict[str, Decimal]] = {
        "FUTURES_SHORT": {"remaining": Decimal(0), "open": Decimal(0),
                          "closed": Decimal(0)},
        "SPOT_LONG": {"remaining": Decimal(0), "open": Decimal(0),
                      "closed": Decimal(0)},
    }
    seq = list(positions) if positions is not None else []
    for item in seq:
        if isinstance(item, Mapping):
            leg = str(item.get("leg_type", item.get("legType", "")))
            rem = _parse_optional_decimal(
                item.get("remaining_qty", item.get("remainingQty")), "remaining")
            opn = _parse_optional_decimal(
                item.get("open_qty", item.get("openQty")), "open")
            cls = _parse_optional_decimal(
                item.get("closed_qty", item.get("closedQty")), "closed")
        else:
            leg = str(getattr(item, "leg_type", ""))
            rem = _parse_optional_decimal(
                getattr(item, "remaining_qty", None), "remaining")
            opn = _parse_optional_decimal(
                getattr(item, "open_qty", None), "open")
            cls = _parse_optional_decimal(
                getattr(item, "closed_qty", None), "closed")
        if leg not in out:
            continue
        out[leg]["remaining"] = rem if rem is not None else Decimal(0)
        out[leg]["open"] = opn if opn is not None else Decimal(0)
        out[leg]["closed"] = cls if cls is not None else Decimal(0)
        if out[leg]["remaining"] < 0 or out[leg]["open"] < 0:
            raise ValueError("position quantities must be non-negative")
    return out


def build_pair_exit_guidance(
    plan: Mapping[str, Any],
    positions: Any,
    market_context: Mapping[str, Any],
    rules: Mapping[str, Any],
    now_ms: int,
) -> PairExitGuidance:
    """Build read-only dual-leg exit guidance (D10, pure).

    ``positions`` must be the effective-event aggregation (e.g.
    :func:`ledger.aggregate_events`); UI self-reported quantities are never
    accepted as a separate input -- the function only reads ``positions``.
    """
    if isinstance(now_ms, bool) or not isinstance(now_ms, int):
        raise ValueError("now_ms must be an int")
    if now_ms < 0:
        raise ValueError("now_ms must be >= 0")
    if not isinstance(market_context, Mapping):
        raise ValueError("market_context must be a mapping")
    if rules is not None and not isinstance(rules, Mapping) and not hasattr(
        rules, "leg_type"
    ):
        # Plain rules mapping expected; attribute doubles accepted.
        if not isinstance(rules, Mapping):
            raise ValueError("rules must be a mapping")
    mctx: dict[str, Any] = dict(market_context) if isinstance(
        market_context, Mapping) else {}

    plan_id = str(_plan_field(plan, "plan_id", "planId", default="plan-unknown"))
    if not plan_id:
        raise ValueError("plan_id must be non-empty")
    raw_version = _plan_field(plan, "plan_version", "planVersion", default=1)
    try:
        plan_version = int(raw_version)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("plan_version must be an int >= 1")
    if isinstance(plan_version, bool) or plan_version < 1:
        raise ValueError("plan_version must be an int >= 1")

    by_leg = _positions_by_leg(positions)
    fut_rem = by_leg["FUTURES_SHORT"]["remaining"]
    spot_rem = by_leg["SPOT_LONG"]["remaining"]
    fut_open = by_leg["FUTURES_SHORT"]["open"]
    spot_open = by_leg["SPOT_LONG"]["open"]

    # Market exit quotes (D18.1 market_context reuse).
    fut_vwap = _parse_optional_decimal(
        mctx.get("futures_buy_vwap_native", mctx.get("futuresBuyVwapNative")),
        "futures_buy_vwap")
    spot_vwap = _parse_optional_decimal(
        mctx.get("spot_sell_vwap_native", mctx.get("spotSellVwapNative")),
        "spot_sell_vwap")
    fut_fx = _parse_optional_decimal(
        mctx.get("futures_quote_fx", mctx.get("futuresQuoteFx")), "futures_fx")
    spot_fx = _parse_optional_decimal(
        mctx.get("spot_quote_fx", mctx.get("spotQuoteFx")), "spot_fx")
    fut_cov_raw = mctx.get("futures_exit_coverage",
                           mctx.get("futuresExitCoverage"))
    spot_cov_raw = mctx.get("spot_exit_coverage", mctx.get("spotExitCoverage"))
    fut_cov = _parse_optional_decimal(fut_cov_raw, "futures_coverage")
    spot_cov = _parse_optional_decimal(spot_cov_raw, "spot_coverage")
    exit_refs = mctx.get("exit_quote_refs", mctx.get("exitQuoteRefs", {}))
    if not isinstance(exit_refs, Mapping):
        exit_refs = {}
    raw_expires = mctx.get("expires_at_ms", mctx.get("expiresAtMs"))
    try:
        expires_at = int(raw_expires) if raw_expires is not None else int(now_ms) + 20000
    except (TypeError, ValueError):
        expires_at = int(now_ms) + 20000
    if expires_at <= int(now_ms):
        expires_at = int(now_ms) + 20000

    price_currency = str(mctx.get("price_currency",
                                 mctx.get("priceCurrency", "USDT")))
    if not price_currency.strip():
        price_currency = "USDT"
    qty_currency = _qty_currency_for_plan(plan)

    # Orphan classification (mirrors ledger.recommend_exit_action, read-only).
    top_reasons: list[str] = []
    orphan_kind: str | None = None
    if fut_rem == 0 and spot_rem > 0:
        if fut_open == 0:
            orphan_kind = "ORPHAN_LEG_WARNING"
            top_reasons.append("ORPHAN_LEG_WARNING")
        else:
            orphan_kind = "CRITICAL_ORPHAN_SPOT_LEG"
            top_reasons += ["CRITICAL_ORPHAN_SPOT_LEG", "URGENT_PAIR_EXIT"]
    elif spot_rem == 0 and fut_rem > 0:
        if spot_open == 0:
            orphan_kind = "ORPHAN_LEG_WARNING"
            top_reasons.append("ORPHAN_LEG_WARNING")
        else:
            orphan_kind = "CRITICAL_ORPHAN_FUTURES_LEG"
            top_reasons += ["CRITICAL_ORPHAN_FUTURES_LEG", "URGENT_PAIR_EXIT"]

    # Mark vs user liquidation: POSSIBLE only, never auto-liquidated.
    liq_raw = _plan_field(plan, "liquidation_price", "liquidationPrice",
                          default=None)
    mark_raw = mctx.get("futures_mark_native", mctx.get("futuresMarkNative"))
    liq_dec = _parse_optional_decimal(liq_raw, "liquidation_price")
    mark_dec = _parse_optional_decimal(mark_raw, "futures_mark")
    mark_breach = False
    if liq_dec is not None and mark_dec is not None and mark_dec != 0:
        try:
            with localcontext() as ctx:
                ctx.prec = 80
                dist = (liq_dec - mark_dec) / mark_dec
                if dist <= 0:
                    mark_breach = True
        except (InvalidOperation, ValueError, ArithmeticError,
                ZeroDivisionError):
            mark_breach = False
    if mark_breach:
        top_reasons.append("POSSIBLE_LIQUIDATION_UNCONFIRMED")

    legs: list[dict[str, Any]] = []
    dust: dict[str, str] = {}

    for leg_type in ("FUTURES_SHORT", "SPOT_LONG"):
        remaining = by_leg[leg_type]["remaining"]
        if remaining == 0:
            continue
        leg_rules = _rules_for_leg(rules, leg_type)
        step = _step_for_leg(leg_rules)
        try:
            floored = _floor_to_step(remaining, step)
        except ValueError:
            floored = remaining
        if floored == 0 and remaining > 0:
            # Sub-step dust: separate column, never zeroed.
            dust[leg_type] = _fmt(remaining)
            top_reasons.append("DUST_BELOW_STEP")
            continue
        if floored < 0 or floored > remaining:
            raise ValueError(f"floored qty out of range for {leg_type}")
        is_fut = leg_type == "FUTURES_SHORT"
        side = "BUY" if is_fut else "SELL"
        vwap = fut_vwap if is_fut else spot_vwap
        fx = fut_fx if is_fut else spot_fx
        cov = fut_cov if is_fut else spot_cov
        # Venue: per-leg rules win, else plan, else BINANCE_SPOT.
        venue: Any = None
        if isinstance(leg_rules, Mapping):
            venue = leg_rules.get("venue", leg_rules.get("market"))
        if venue is None:
            venue = _plan_field(plan, "spot_venue", "spotVenue",
                                "venue", default=None)
        if not isinstance(venue, str) or not venue.strip():
            venue = "BINANCE_SPOT"
        # Capability: missing quote/coverage never claims executable.
        if vwap is None or cov is None:
            capability = "UNKNOWN"
        elif cov >= 1:
            capability = "CONFIRMED"
        elif cov > 0:
            capability = "PARTIAL"
        else:
            capability = "NO"
        # Notional (display only); unknown FX/price stays null.
        notional: str | None = None
        if vwap is not None and fx is not None:
            try:
                with localcontext() as ctx:
                    ctx.prec = 80
                    notional = _fmt(floored * vwap * fx)
            except (InvalidOperation, ValueError, ArithmeticError):
                notional = None
        # Coverage string for transparency.
        coverage_str: str | None = None
        if cov is not None:
            try:
                coverage_str = _fmt(cov)
            except (InvalidOperation, ValueError, ArithmeticError):
                coverage_str = None
        leg_reasons: list[str] = []
        if floored != remaining:
            leg_reasons.append("STEP_FLOORED")
        if orphan_kind is not None:
            # Only the surviving leg carries the orphan tag.
            surviving = ("SPOT_LONG" if fut_rem == 0 and spot_rem > 0
                         else "FUTURES_SHORT" if spot_rem == 0 and fut_rem > 0
                         else None)
            if leg_type == surviving:
                leg_reasons.append(orphan_kind)
                if orphan_kind.startswith("CRITICAL"):
                    leg_reasons.append("URGENT_PAIR_EXIT")
        if mark_breach:
            leg_reasons.append("POSSIBLE_LIQUIDATION_UNCONFIRMED")
        if vwap is None:
            leg_reasons.append("QUOTE_UNKNOWN")
        if not leg_reasons:
            leg_reasons.append("PAIR_EXIT_READY")
        quote_ref: Any = None
        if isinstance(exit_refs, Mapping):
            quote_ref = (exit_refs.get("futures") if is_fut
                         else exit_refs.get("spot"))
            if quote_ref is None:
                quote_ref = (exit_refs.get("futures_quote") if is_fut
                             else exit_refs.get("spot_quote"))
        source_refs: dict[str, Any] = {}
        if quote_ref is not None:
            source_refs["exit_quote"] = str(quote_ref)
        legs.append({
            "leg_type": leg_type,
            "venue": venue,
            "side": side,
            "native_qty": _fmt(floored),
            "qty_currency": qty_currency,
            "price_currency": price_currency,
            "vwap_native": _fmt(vwap) if vwap is not None else None,
            "notional_usd": notional,
            "reduce_only": bool(is_fut),
            "coverage": coverage_str,
            "capability": capability,
            "source_refs": source_refs,
            "reasons": tuple(sorted(set(leg_reasons))),
        })
        # Top-level mirrors leg-level urgency for monitor/alert parity.
        for r in leg_reasons:
            if r in ("URGENT_PAIR_EXIT", "CRITICAL_ORPHAN_SPOT_LEG",
                     "CRITICAL_ORPHAN_FUTURES_LEG", "ORPHAN_LEG_WARNING",
                     "POSSIBLE_LIQUIDATION_UNCONFIRMED"):
                if r not in top_reasons:
                    top_reasons.append(r)

    if not legs and not dust:
        top_reasons.append("NO_REMAINING_POSITION")
    if dust and not legs:
        # All remaining is dust: still requires confirmation, never auto-close.
        pass

    return PairExitGuidance(
        plan_id=plan_id,
        plan_version=plan_version,
        generated_at_ms=int(now_ms),
        expires_at_ms=int(expires_at),
        legs=tuple(legs),
        unexecutable_dust=dict(dust),
        reasons=tuple(sorted(set(top_reasons))),
        confirmation_required=True,
    )
