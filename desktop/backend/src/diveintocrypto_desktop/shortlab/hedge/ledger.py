"""H06 immutable manual-fill ledger + state machine (B19/B21/B28.7.1).

Design: B19 (plan vs actual fill, ACTIVE gate, PARTIALLY_FILLED),
B21 (paired exit, dust, orphan legs), B28.0/B28.7 (Decimal boundary,
integer accumulation, immutable events, single-worker transaction).

This module is the only H06 owner. It never touches the network, never
reads ``sl_hedge_plan``/``sl_hedge_leg`` DOUBLE projections for balances,
and never invents a second DTO: all outputs are the frozen H01 types in
:mod:`hedge.models` (``HedgeEvent`` / ``HedgePosition`` / ``LedgerResult``
/ ``PlanState``).

Concurrency contract (B28.7.1):

- :meth:`Ledger.apply_event` delegates to the H01 single-worker
  transaction ``repository.apply_hedge_event`` (BEGIN -> idempotency ->
  version/balance checks -> insert -> leg rebuild -> CAS -> COMMIT).
- A loop-bound per-plan :class:`asyncio.Lock` only covers the submit
  (the single ``await repository.apply_hedge_event`` call). It never
  covers provider requests, pure aggregation, or state evaluation, and it
  never replaces the database CAS check. Different plans use different
  locks and still serialize on the single worker.

Quantity contract (B28.0):

- Every balance accumulates from immutable ``event_json`` decimal strings
  with integer coefficient/exponent alignment (:func:`sum_qty_strs`);
  never ``float``, never ``SQL SUM(qty)``, never a DOUBLE read-back, and
  never the default 28-digit context.
- The effective quantity of a non-funding event is ``net_qty`` when
  present (base-token fee / transfer tax already deducted), otherwise
  ``canonical_qty``, otherwise ``native_qty`` (converted via
  ``identity.contract_multiplier`` when only native is present),
  otherwise ``gross_qty`` as a last fallback. Quote-currency fees stay a
  cost line and are never subtracted a second time.
- ``FUNDING_RECEIPT`` carries ``amount``/``currency`` only and never
  enters quantity maths; :func:`sum_funding_receipts` keeps actual
  receipts in a separate calibre from any estimate (never added).
- Zero / over-close checks use exact :class:`Decimal` equality; dust is
  never swallowed by an epsilon. ``CLOSED`` allows each leg to hold
  exactly ``0`` or rule dust (see :func:`is_dust`); RELATIVE legs are
  never required to be equal.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.hedge.models import (
    HEDGE_EVENT_SCHEMA_VERSION,
    HedgePosition,
    LedgerResult,
    PlanState,
)

__all__ = [
    "DRIFT_TOLERANCE",
    "Ledger",
    "LedgerError",
    "aggregate_events",
    "build_event_fx_records",
    "canonical_event_type",
    "describe_position",
    "effective_qty_str",
    "evaluate_position_state",
    "is_dust",
    "normalize_event_for_repo",
    "recommend_exit_action",
    "sum_funding_receipts",
    "sum_qty_strs",
]

#: ACTIVE / simulation drift gate: absolute ratio points (B19.3, H04.2).
DRIFT_TOLERANCE = Decimal("0.05")

_GENERIC_OPEN = "OPEN"
_GENERIC_CLOSE = "CLOSE"
_GENERIC_ADJUSTMENT = "ADJUSTMENT"

_CANONICAL_OPEN = {
    "FUTURES_SHORT": "OPEN_FUTURES_SHORT",
    "SPOT_LONG": "OPEN_SPOT_LONG",
}
_CANONICAL_CLOSE = {
    "FUTURES_SHORT": "CLOSE_FUTURES_SHORT",
    "SPOT_LONG": "CLOSE_SPOT_LONG",
}
_CANONICAL_TYPES = frozenset(
    {
        "OPEN_FUTURES_SHORT",
        "OPEN_SPOT_LONG",
        "CLOSE_FUTURES_SHORT",
        "CLOSE_SPOT_LONG",
        "CORRECT_REVERSAL",
        "CORRECT_SUPERSEDE",
        "FUNDING_RECEIPT",
        "LIQUIDATION",
    }
)


class LedgerError(ValueError):
    """Ledger-side validation failure (caller-side, HTTP 422 semantics)."""


# ---------------------------------------------------------------------------
# Decimal helpers (ledger boundary: never float for qty/price).
# ---------------------------------------------------------------------------


def _parse_decimal(name: str, value: Any) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool):
        raise LedgerError(f"{name} must be a decimal string, got {value!r}")
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise LedgerError(f"{name} must be a non-empty decimal string")
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise LedgerError(f"{name} is not a decimal string: {value!r}") from exc
    else:
        raise LedgerError(
            f"{name} must be a decimal string (float rejected), got {value!r}"
        )
    if not parsed.is_finite():
        raise LedgerError(f"{name} must be finite, got {value!r}")
    return parsed


def _format_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(value)
    if not value.is_finite():
        raise LedgerError("non-finite decimal cannot be serialised")
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


def sum_qty_strs(values: object) -> str:
    """Exact sum via integer coefficient/exponent alignment (B28.0).

    Immune to the default 28-digit context: every input is scaled to the
    minimum exponent as an integer, summed as Python ints, then
    re-encoded. ``0.1 + 0.2`` is exactly ``0.3``; dust is never rounded
    to zero by an epsilon. Accepts any iterable of decimal strings
    (negated strings such as ``"-0.5"`` are allowed for ``open-closed``
    maths). Empty input is ``"0"``.
    """
    items = list(values) if values is not None else []
    if not items:
        return "0"
    decoded = [_parse_decimal("qty", v) for v in items]
    min_exp = min(d.as_tuple().exponent for d in decoded)
    total = 0
    for d in decoded:
        sign, digits, exp = d.as_tuple()
        coeff = 0
        for digit in digits:
            coeff = coeff * 10 + digit
        if sign:
            coeff = -coeff
        total += coeff * (10 ** (exp - min_exp))
    if total == 0:
        return "0"
    negative = total < 0
    total = abs(total)
    digits_str = str(total)
    if min_exp >= 0:
        out = digits_str + ("0" * min_exp)
    else:
        point = len(digits_str) + min_exp
        if point > 0:
            out = digits_str[:point] + "." + digits_str[point:]
        else:
            out = "0." + ("0" * (-point)) + digits_str
        out = out.rstrip("0").rstrip(".")
        if out in ("", "-"):
            out = "0"
    return ("-" if negative else "") + out


def _negate_str(value: str) -> str:
    text = str(value).strip()
    if text.startswith("-"):
        return text[1:]
    return "-" + text


# ---------------------------------------------------------------------------
# Event normalisation (generic B19 names -> frozen H01 names).
# ---------------------------------------------------------------------------


def canonical_event_type(
    event_type: str,
    leg_type: str,
    *,
    has_reverses: bool = False,
    has_supersedes: bool = False,
) -> str:
    """Translate a B19 generic type to the frozen H01 ``event_type``.

    Accepted generic spellings (task shorthand):

    - ``OPEN`` + leg -> ``OPEN_FUTURES_SHORT`` / ``OPEN_SPOT_LONG``;
    - ``CLOSE`` + leg -> ``CLOSE_FUTURES_SHORT`` / ``CLOSE_SPOT_LONG``;
    - ``ADJUSTMENT`` -> ``CORRECT_REVERSAL`` when the payload carries
      ``reverses_event_id`` (without ``supersedes_event_id``),
      otherwise ``CORRECT_SUPERSEDE``;
    - ``LIQUIDATION`` / ``FUNDING_RECEIPT`` pass through.

    Frozen H01 spellings pass through unchanged. Anything else raises
    :class:`LedgerError`.
    """
    if event_type in _CANONICAL_TYPES:
        return event_type
    if event_type == _GENERIC_OPEN:
        try:
            return _CANONICAL_OPEN[leg_type]
        except KeyError as exc:
            raise LedgerError(
                f"OPEN requires leg FUTURES_SHORT/SPOT_LONG, got {leg_type!r}"
            ) from exc
    if event_type == _GENERIC_CLOSE:
        try:
            return _CANONICAL_CLOSE[leg_type]
        except KeyError as exc:
            raise LedgerError(
                f"CLOSE requires leg FUTURES_SHORT/SPOT_LONG, got {leg_type!r}"
            ) from exc
    if event_type == _GENERIC_ADJUSTMENT:
        if has_reverses and not has_supersedes:
            return "CORRECT_REVERSAL"
        return "CORRECT_SUPERSEDE"
    raise LedgerError(f"unknown event_type {event_type!r} for leg {leg_type!r}")


_CAMEL_TO_SNAKE = {
    "schemaVersion": "schema_version",
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
    "eventId": "event_id",
}


def _event_to_dict(event: Any) -> dict[str, Any]:
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        data = dataclasses.asdict(event)
        # Drop unknown dataclass extras (none today) but keep everything.
        return dict(data)
    if isinstance(event, Mapping):
        data: dict[str, Any] = {}
        for key, value in dict(event).items():
            snake = _CAMEL_TO_SNAKE.get(key, key)
            # Avoid clobbering an explicit snake_case entry with a camel
            # duplicate; snake wins when both are present.
            if snake in data and key != snake:
                continue
            data[snake] = value
        return data
    raise LedgerError("hedge event must be a mapping or frozen DTO")


def normalize_event_for_repo(event: Any) -> dict[str, Any]:
    """Return a repo-ready event dict (generic -> canonical, lenient).

    - Generic ``OPEN`` / ``CLOSE`` / ``ADJUSTMENT`` are translated via
      :func:`canonical_event_type` so the frozen H01 validator accepts
      them; canonical spellings pass through.
    - ``camelCase`` aliases are normalised to ``snake_case``; **unknown
      extra keys are preserved untouched** -- unknown market/provider
      data never blocks recording a real fill (H06.4).
    - Missing ``schema_version`` defaults to ``hedge-event-v1`` and
      missing ``source`` defaults to ``USER_ENTERED`` so hand-written
      fixtures stay focused on ledger fields; explicit wrong values still
      fail at the repository validator.
    """
    data = _event_to_dict(event)
    leg = data.get("leg_type", data.get("legType"))
    etype = data.get("event_type", data.get("eventType"))
    if not isinstance(leg, str) or not leg:
        # Leave the error to the repository validator (keeps one owner
        # for enum messages) -- but generic translation needs both.
        return data
    if not isinstance(etype, str) or not etype:
        return data
    has_reverses = data.get("reverses_event_id") is not None
    has_supersedes = data.get("supersedes_event_id") is not None
    try:
        canonical = canonical_event_type(
            etype, leg, has_reverses=has_reverses, has_supersedes=has_supersedes
        )
    except LedgerError:
        # Unknown type: leave untouched for the repository to reject with
        # its own message (single validator owner).
        return data
    data["leg_type"] = leg
    data["event_type"] = canonical
    if data.get("schema_version") is None:
        data["schema_version"] = HEDGE_EVENT_SCHEMA_VERSION
    if data.get("source") is None:
        data["source"] = "USER_ENTERED"
    return data


def effective_qty_str(event: Mapping[str, Any]) -> str | None:
    """Return the balance-relevant quantity string for one event.

    Preference (B28.7): ``net_qty`` (base fee / transfer tax already
    deducted) -> ``canonical_qty`` -> ``native_qty`` -> ``gross_qty``.
    ``FUNDING_RECEIPT`` has no quantity and returns ``None``. The same
    fee is never subtracted twice: when ``net_qty`` is present it wins
    and ``fee_amount`` is treated as a cost line only.
    """
    if event.get("event_type") == "FUNDING_RECEIPT":
        return None
    for key in ("net_qty", "canonical_qty", "native_qty", "gross_qty"):
        value = event.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return None


def _extract_multiplier(identity: Any) -> Decimal | None:
    if identity is None:
        return None
    if isinstance(identity, Mapping):
        raw = identity.get("contract_multiplier", identity.get("multiplier"))
    else:
        raw = getattr(
            identity, "contract_multiplier", getattr(identity, "multiplier", None)
        )
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise LedgerError("contract_multiplier must be a positive number")
    try:
        mult = (
            Decimal(str(raw).strip()) if isinstance(raw, str) else Decimal(str(raw))
        )
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise LedgerError("contract_multiplier is not a number") from exc
    if not mult.is_finite() or mult <= 0:
        raise LedgerError("contract_multiplier must be > 0")
    return mult


def _rules_for_leg(rules: Any, leg_type: str) -> Mapping[str, Any] | None:
    """Extract the per-leg trading-rules mapping for dust checks."""
    if rules is None:
        return None
    if isinstance(rules, Mapping):
        # Leg-keyed form: {FUTURES_SHORT: {...}, SPOT_LONG: {...}}.
        if leg_type in rules and isinstance(rules[leg_type], Mapping):
            return rules[leg_type]  # type: ignore[return-value]
        # Flat snapshot form: {lot_rules: {...}, ...} or a single group.
        if any(k in rules for k in ("lot_rules", "step_size", "min_qty")):
            return rules  # type: ignore[return-value]
        return None
    # Frozen TradingRulesSnapshot DTO (or test double with attributes).
    group_leg = getattr(rules, "leg_type", None)
    if group_leg is not None and group_leg != leg_type:
        return None
    return rules  # type: ignore[return-value]


def _rule_decimal(rules: Any, group: str, key: str) -> Decimal | None:
    group_map: Any = None
    if isinstance(rules, Mapping):
        group_map = rules.get(group)
        if group_map is None and key in rules:
            group_map = rules
    else:
        group_map = getattr(rules, group, None)
    if group_map is None:
        return None
    raw: Any = group_map.get(key) if isinstance(group_map, Mapping) else None
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise LedgerError(f"{group}.{key} must be a decimal string")
    if isinstance(raw, (int, Decimal)) and not isinstance(raw, bool):
        raw = str(raw)
    if not isinstance(raw, str):
        return None  # unknown rule payload never blocks the ledger
    try:
        parsed = Decimal(raw.strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def is_dust(remaining_qty: str | Decimal, rules_for_leg: Any) -> bool:
    """Whether a positive remainder is rule dust (B21, explicit only).

    ``True`` only when ``0 < remaining < min_qty`` or
    ``0 < remaining < step_size`` (a ``0`` step keeps its venue-native
    "disabled" meaning and never marks dust). ``rules_for_leg`` of
    ``None`` (or without usable thresholds) means only an exact ``0`` is
    closed -- tiny dust is never swallowed by an epsilon.
    """
    remaining = (
        remaining_qty
        if isinstance(remaining_qty, Decimal)
        else _parse_decimal("remaining_qty", remaining_qty)
    )
    if remaining <= 0:
        return False
    if rules_for_leg is None:
        return False
    for key in ("min_qty", "step_size"):
        threshold = _rule_decimal(rules_for_leg, "lot_rules", key)
        if threshold is None or threshold == 0:
            continue
        if threshold < 0:
            continue
        if remaining < threshold:
            return True
    return False


def _is_zero_or_dust(remaining: Decimal, rules_for_leg: Any) -> bool:
    if remaining == 0:
        return True
    if rules_for_leg is None:
        return False
    return is_dust(remaining, rules_for_leg)


# ---------------------------------------------------------------------------
# Pure aggregation (mirrors the H01 worker projection, no DOUBLE reads).
# ---------------------------------------------------------------------------


def _normalise_pure_event(
    raw: Any, index: int, *, multiplier: Decimal | None
) -> dict[str, Any]:
    data = normalize_event_for_repo(raw)
    event_id = data.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        event_id = f"pure-{index}"
    executed = data.get("executed_at_ms")
    if not isinstance(executed, int) or isinstance(executed, bool):
        # Unknown timing never blocks the fill: order by index instead.
        executed = 0
    leg = data.get("leg_type")
    etype = data.get("event_type")
    if etype == "FUNDING_RECEIPT":
        return {
            "event_id": event_id,
            "plan_id": data.get("plan_id"),
            "leg_type": leg,
            "event_type": etype,
            "executed_at_ms": executed,
            "qty": None,
            "price": data.get("native_price"),
            "reverses": data.get("reverses_event_id"),
            "supersedes": data.get("supersedes_event_id"),
            "raw": data,
        }
    qty_text = effective_qty_str(data)
    if (data.get("net_qty") is None and data.get("canonical_qty") is None
            and data.get("native_qty") is not None and leg == "FUTURES_SHORT"):
        if multiplier is None:
            raise LedgerError("native futures quantity requires a verified contract multiplier")
        with localcontext() as ctx:
            ctx.prec = 80
            qty_text = _format_decimal(_parse_decimal("native_qty", data["native_qty"]) * multiplier)
    if qty_text is None:
        # Fall back to native * multiplier when only native is present
        # and the multiplier is verified (1000-token contracts).
        native = data.get("native_qty")
        if native is not None and multiplier is not None:
            with localcontext() as ctx:
                ctx.prec = 80
                qty_text = _format_decimal(
                    _parse_decimal("native_qty", native) * multiplier
                )
        else:
            raise LedgerError("non-funding events require a quantity")
    qty = _parse_decimal("qty", qty_text)
    if qty < 0:
        raise LedgerError("event quantity must be non-negative")
    price = data.get("native_price")
    if price is not None and leg == "FUTURES_SHORT" and multiplier is not None:
        with localcontext() as ctx:
            ctx.prec = 80
            price = _format_decimal(_parse_decimal("native_price", price) / multiplier)
    return {
        "event_id": event_id,
        "plan_id": data.get("plan_id"),
        "leg_type": leg,
        "event_type": etype,
        "executed_at_ms": executed,
        "qty": _format_decimal(qty),
        "price": price,
        "reverses": data.get("reverses_event_id"),
        "supersedes": data.get("supersedes_event_id"),
        "raw": data,
    }


def aggregate_events(
    events: Any,
    identity: Any = None,
    rules: Any = None,
    *,
    plan_id: str = "",
) -> tuple[HedgePosition, ...]:
    """Aggregate immutable events to per-leg :class:`HedgePosition` (H06.1).

    Pure function: no network, no DB, no DOUBLE reads. Quantity maths use
    :func:`sum_qty_strs` (integer alignment); price/VWAP maths run under
    an 80-digit localcontext and are display-only. ``FUNDING_RECEIPT``
    never enters quantity maths (see :func:`sum_funding_receipts`).

    Correction guards (same-plan, single reverse) mirror the H01 worker:

    - ``reverses_event_id`` / ``supersedes_event_id`` targets must exist
      in the supplied batch when ``plan_id`` is given and must belong to
      the same plan; a target may only be reversed once -- the second
      reversal raises :class:`LedgerError`.
    - Over-close beyond the exact Decimal remainder raises
      :class:`LedgerError` (no epsilon).
    - Unknown extra keys / missing optional prices never block the fill.
    """
    seq = list(events) if events is not None else []
    multiplier = _extract_multiplier(identity)
    normalised = [
        _normalise_pure_event(raw, idx, multiplier=multiplier)
        for idx, raw in enumerate(seq)
    ]
    normalised.sort(key=lambda e: (e["executed_at_ms"], e["event_id"]))

    by_id = {e["event_id"]: e for e in normalised}
    reversed_targets: set[str] = set()

    # Validate correction references before maths (same-plan + single).
    for entry in normalised:
        for key, target in (
            ("supersedes", entry["supersedes"]),
            ("reverses", entry["reverses"]),
        ):
            if target is None:
                continue
            if not isinstance(target, str) or not target:
                raise LedgerError(f"{key}_event_id must be a non-empty str")
            target_entry = by_id.get(target)
            if target_entry is not None and plan_id:
                target_plan = target_entry.get("plan_id") or entry.get("plan_id")
                entry_plan = entry.get("plan_id") or plan_id
                if target_plan and entry_plan and target_plan != entry_plan:
                    raise LedgerError(
                        f"{key}_event_id must reference the same plan"
                    )
            if key == "reverses":
                if target in reversed_targets:
                    raise LedgerError("an event may only be reversed once")
                reversed_targets.add(target)

    per_leg: dict[str, dict[str, Any]] = {}
    running_open: dict[str, Decimal] = {}
    running_closed: dict[str, Decimal] = {}

    for entry in normalised:
        if entry["event_type"] == "FUNDING_RECEIPT":
            continue
        leg = entry["leg_type"]
        if leg not in ("FUTURES_SHORT", "SPOT_LONG"):
            continue  # unknown leg never blocks known fills
        bucket = per_leg.setdefault(
            leg, {"open": [], "closed": [], "ids": [], "prices": []}
        )
        bucket["ids"].append(entry["event_id"])
        etype = entry["event_type"]
        qty = entry["qty"]
        assert qty is not None
        if etype in ("OPEN_FUTURES_SHORT", "OPEN_SPOT_LONG"):
            # Sequential over-close guard is for closes only; opens
            # always accumulate.
            bucket["open"].append(qty)
            running_open[leg] = running_open.get(leg, Decimal(0)) + Decimal(qty)
            if entry["price"] is not None:
                try:
                    _parse_decimal("native_price", entry["price"])
                except LedgerError:
                    pass  # unknown price never blocks the fill
                else:
                    bucket["prices"].append((qty, str(entry["price"])))
        elif etype in (
            "CLOSE_FUTURES_SHORT",
            "CLOSE_SPOT_LONG",
            "CORRECT_REVERSAL",
            "CORRECT_SUPERSEDE",
            "LIQUIDATION",
        ):
            opened = running_open.get(leg, Decimal(0))
            closed = running_closed.get(leg, Decimal(0))
            remaining = opened - closed
            if Decimal(qty) > remaining:
                raise LedgerError(
                    "cannot close more than the remaining quantity"
                )
            bucket["closed"].append(qty)
            running_closed[leg] = closed + Decimal(qty)
        else:
            raise LedgerError(f"unknown event_type {etype!r} for leg {leg!r}")

    positions: list[HedgePosition] = []
    for leg in ("FUTURES_SHORT", "SPOT_LONG"):
        bucket = per_leg.get(leg, {"open": [], "closed": [], "ids": [], "prices": []})
        open_qty = sum_qty_strs(bucket["open"])
        closed_qty = sum_qty_strs(bucket["closed"])
        remaining_qty = sum_qty_strs(
            bucket["open"] + [_negate_str(q) for q in bucket["closed"]]
        )
        wavg: str | None = None
        if bucket["prices"]:
            try:
                with localcontext() as ctx:
                    ctx.prec = 80
                    num = sum(
                        (Decimal(q) * Decimal(p) for q, p in bucket["prices"]),
                        Decimal(0),
                    )
                    den = sum((Decimal(q) for q, _ in bucket["prices"]), Decimal(0))
                    wavg = _format_decimal(num / den) if den != 0 else None
            except (InvalidOperation, ValueError, ArithmeticError):
                wavg = None  # unknown price never blocks the fill
        positions.append(
            HedgePosition(
                plan_id=plan_id,
                leg_type=leg,  # type: ignore[arg-type]
                open_qty=open_qty,
                closed_qty=closed_qty,
                remaining_qty=remaining_qty,
                gross_qty=open_qty,
                net_qty=remaining_qty,
                weighted_avg_price=wavg,
                event_ids=tuple(bucket["ids"]),
            )
        )
    return tuple(positions)


def sum_funding_receipts(events: Any) -> dict[str, str]:
    """Sum actual ``FUNDING_RECEIPT`` amounts per currency (H06.4).

    Actual receipts stay in their own calibre and are never added to any
    estimated funding number. Returns ``{currency: decimal_str}``.
    """
    totals: dict[str, Decimal] = {}
    seq = list(events) if events is not None else []
    for raw in seq:
        data = _event_to_dict(raw)
        etype = data.get("event_type", data.get("eventType"))
        # Accept both the frozen name and the B19 shorthand.
        if etype not in ("FUNDING_RECEIPT",):
            continue
        amount = data.get("amount")
        currency = data.get("currency")
        if amount is None or currency is None:
            continue
        totals[str(currency)] = totals.get(str(currency), Decimal(0)) + _parse_decimal(
            "amount", amount
        )
    return {ccy: _format_decimal(total) for ccy, total in totals.items()}


def build_event_fx_records(
    events: Any,
    event_fx: Mapping[str, Mapping[str, Any]],
    symbol: str,
    known_at_ms: int,
    *,
    source_as_of_ms: int | None = None,
) -> tuple[dict[str, Any], ...]:
    """Build ``EVENT_FX`` persistence records (R08b/D18.1, pure).

    Each input event yields one ``sl_market_observation``-ready record with
    ``kind="EVENT_FX"`` whose ``value_json`` holds the ``event_id`` plus the
    D18.1 FX mapping (``price_fx``/``fee_fx``/``funding_fx`` + ids) and the
    event currencies (``price_currency``/``fee_currency``/``currency``).
    Records are saveable via
    ``repository.save_market_observation`` (``EVENT_FX`` requires
    ``event_id`` in ``value_json``) and listable via
    ``list_market_observations(symbol, "EVENT_FX", ...)``. Pure: no DB, no
    network, no clock reads.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise LedgerError("symbol must be a non-empty str")
    if isinstance(known_at_ms, bool) or not isinstance(known_at_ms, int):
        raise LedgerError("known_at_ms must be an int")
    if known_at_ms < 0:
        raise LedgerError("known_at_ms must be >= 0")
    if event_fx is None or not isinstance(event_fx, Mapping):
        raise LedgerError("event_fx must be a mapping")
    seq = list(events) if events is not None else []
    out: list[dict[str, Any]] = []
    for idx, raw in enumerate(seq):
        data = _event_to_dict(raw)
        eid = data.get("event_id")
        if not isinstance(eid, str) or not eid:
            eid = f"pure-{idx}"
        fx_entry = event_fx.get(eid)
        fx_dict: dict[str, Any] = dict(fx_entry) if isinstance(
            fx_entry, Mapping) else {}
        exec_ms = data.get("executed_at_ms")
        src: int | None = None
        if isinstance(exec_ms, int) and not isinstance(exec_ms, bool):
            src = exec_ms
        elif source_as_of_ms is not None:
            try:
                src = int(source_as_of_ms)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                src = None
        if src is None and source_as_of_ms is not None:
            try:
                src = int(source_as_of_ms)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                src = None
        value: dict[str, Any] = {"event_id": eid}
        for key in ("price_currency", "fee_currency", "currency", "amount"):
            if data.get(key) is not None:
                value[key] = data.get(key)
        # D18.1 FX mapping verbatim (ids + rates).
        for key in ("price_fx_id", "price_fx", "fee_fx_id", "fee_fx",
                    "funding_fx_id", "funding_fx"):
            if fx_dict.get(key) is not None:
                value[key] = fx_dict.get(key)
        # Compat aliases (camelCase fixtures) mirrored to snake.
        for camel, snake in (("priceFx", "price_fx"), ("feeFx", "fee_fx"),
                             ("fundingFx", "funding_fx")):
            if fx_dict.get(camel) is not None and value.get(snake) is None:
                value[snake] = fx_dict.get(camel)
        meta: dict[str, Any] = {
            "status": "OK",
            "source": "LEDGER_EVENT_FX",
            "source_schema_version": "observations-v1",
            "source_as_of_ms": src,
            "known_at_ms": int(known_at_ms),
            "repair_schema_version": "repair-contract-v1",
            "schema_version": "hedge-source-v1",
        }
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False, default=str)
        raw_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        out.append({
            "observation_id": f"{symbol.strip()}:EVENT_FX:{eid}:{int(known_at_ms)}",
            "symbol": symbol.strip(),
            "kind": "EVENT_FX",
            "source_as_of_ms": src,
            "known_at_ms": int(known_at_ms),
            "value_json": value,
            "meta_json": meta,
            "raw_sha256": raw_sha,
        })
    return tuple(out)


# ---------------------------------------------------------------------------
# State machine (B19.3/B21) + independent exit suggestion.
# ---------------------------------------------------------------------------


def _positions_by_leg(positions: Any) -> dict[str, dict[str, Decimal | str]]:
    if isinstance(positions, Mapping):
        # Single position mapping.
        items = [positions]
    elif isinstance(positions, (HedgePosition,)):
        items = [positions]
    else:
        items = list(positions or [])
    out: dict[str, dict[str, Decimal | str]] = {}
    for item in items:
        if isinstance(item, Mapping):
            leg = str(item.get("leg_type", item.get("legType", "")))
            open_q = _parse_decimal("open_qty", str(item.get("open_qty", "0")))
            closed_q = _parse_decimal("closed_qty", str(item.get("closed_qty", "0")))
            remaining_q = _parse_decimal(
                "remaining_qty", str(item.get("remaining_qty", "0"))
            )
        elif isinstance(item, HedgePosition):
            leg = item.leg_type
            open_q = _parse_decimal("open_qty", item.open_qty)
            closed_q = _parse_decimal("closed_qty", item.closed_qty)
            remaining_q = _parse_decimal("remaining_qty", item.remaining_qty)
        else:
            # Test doubles shaped like the frozen DTO.
            leg = str(getattr(item, "leg_type", ""))
            open_q = _parse_decimal("open_qty", str(getattr(item, "open_qty", "0")))
            closed_q = _parse_decimal(
                "closed_qty", str(getattr(item, "closed_qty", "0"))
            )
            remaining_q = _parse_decimal(
                "remaining_qty", str(getattr(item, "remaining_qty", "0"))
            )
        if leg:
            out[leg] = {
                "open": open_q,
                "closed": closed_q,
                "remaining": remaining_q,
            }
    for leg in ("FUTURES_SHORT", "SPOT_LONG"):
        out.setdefault(
            leg, {"open": Decimal(0), "closed": Decimal(0), "remaining": Decimal(0)}
        )
    return out


def _parse_target_ratio(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, float):
        return None  # float targets never carry ledger state
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


def evaluate_position_state(
    positions: Any,
    target_ratio: Any = None,
    *,
    mode: str = "ABSOLUTE",
    plan_id: str = "",
    plan_version: int = 1,
    updated_at_ms: int = 0,
    prior_status: str | None = None,
    rules: Any = None,
    activated: bool | None = None,
) -> PlanState:
    """Evaluate the持仓 state machine (B19.3/B21, H06.2-H06.4).

    - No fills (both ``open == 0``): keep ``prior_status`` when it is a
      non-fill status (``DRAFT`` / ``READY`` / ``INVALID``), else
      ``DRAFT``. ``INVALID`` is only for never-filled plans; a filled
      plan never returns to ``INVALID``.
    - Both legs ``0``-or-dust with both ``open > 0``: ``CLOSED``.
      RELATIVE legs are never required to be equal.
    - Both legs positive: ``ACTIVE`` only when the actual ratio
      ``spot_remaining / futures_remaining`` drifts no more than 5
      points from ``target_ratio``; otherwise ``PARTIALLY_FILLED``.
      A missing/unknown target never promotes to ``ACTIVE``.
    - Both legs positive with closes already recorded: ``CLOSING``
      (paired exit in progress).
    - Exactly one leg positive: ``PARTIALLY_FILLED`` when the empty leg
      was never opened, else ``CLOSING`` (paired exit with an orphan
      leg; see :func:`recommend_exit_action` for the independent
      ``CRITICAL_ORPHAN_*`` code -- the code never rewrites this
      status).

    R08b (D12/D18.1): ``activated`` selects the funded-but-not-activated
    state. ``None`` (default) preserves the pre-repair ``ACTIVE`` for
    legacy callers; ``False`` returns ``FUNDED_PENDING_ACTIVATION`` for a
    healthy both-legs-filled position that has not passed the explicit
    activation re-check (six items + ``ACTIVATION_CHECK`` + protection
    hash); ``True`` returns ``ACTIVE`` after explicit activation. Only the
    healthy no-close branch is affected -- ``CLOSING``/``CLOSED``/
    ``PARTIALLY_FILLED`` are unchanged.
    """
    if mode not in ("ABSOLUTE", "RELATIVE"):
        raise LedgerError(f"mode={mode!r} must be ABSOLUTE/RELATIVE")
    by_leg = _positions_by_leg(positions)
    fut = by_leg["FUTURES_SHORT"]
    spot = by_leg["SPOT_LONG"]
    assert isinstance(fut["open"], Decimal)
    fut_open = fut["open"]
    spot_open = spot["open"]
    fut_rem_raw = fut["remaining"]
    spot_rem_raw = spot["remaining"]
    assert isinstance(fut_rem_raw, Decimal) and isinstance(spot_rem_raw, Decimal)

    if fut_open == 0 and spot_open == 0:
        if prior_status in ("DRAFT", "READY", "INVALID",
                            "FUNDED_PENDING_ACTIVATION"):
            status = prior_status
        else:
            status = "DRAFT"
        return PlanState(
            plan_id=plan_id,
            status=status,  # type: ignore[arg-type]
            plan_version=int(plan_version),
            mode=mode,  # type: ignore[arg-type]
            updated_at_ms=int(updated_at_ms),
        )

    fut_rules = _rules_for_leg(rules, "FUTURES_SHORT")
    spot_rules = _rules_for_leg(rules, "SPOT_LONG")
    fut_done = _is_zero_or_dust(fut_rem_raw, fut_rules)
    spot_done = _is_zero_or_dust(spot_rem_raw, spot_rules)

    if fut_done and spot_done:
        # Both legs drained (each 0 or explicit rule dust) after fills.
        status = "CLOSED"
    elif not fut_done and not spot_done:
        closed_total = fut["closed"] + spot["closed"]  # type: ignore[operator]
        assert isinstance(closed_total, Decimal)
        target = _parse_target_ratio(target_ratio)
        drift: Decimal | None = None
        if target is not None and fut_rem_raw != 0:
            with localcontext() as ctx:
                ctx.prec = 80
                drift = abs(spot_rem_raw / fut_rem_raw - target)
        if closed_total > 0:
            # Paired exit already started but both legs still hold.
            status = "CLOSING"
        elif drift is not None and drift <= DRIFT_TOLERANCE:
            if activated is False:
                # R08b: both legs filled but not explicitly activated.
                status = "FUNDED_PENDING_ACTIVATION"
            else:
                status = "ACTIVE"
        else:
            status = "PARTIALLY_FILLED"
    else:
        # Exactly one leg still holds. The empty (done) leg was never
        # opened -> PARTIALLY_FILLED (single-leg open); otherwise both
        # were opened and one drained -> CLOSING with an orphan code.
        if (fut_done and fut_open == 0) or (spot_done and spot_open == 0):
            status = "PARTIALLY_FILLED"
        else:
            status = "CLOSING"

    return PlanState(
        plan_id=plan_id,
        status=status,  # type: ignore[arg-type]
        plan_version=int(plan_version),
        mode=mode,  # type: ignore[arg-type]
        updated_at_ms=int(updated_at_ms),
    )


def recommend_exit_action(
    positions: Any,
    target_ratio: Any = None,
    *,
    mode: str = "ABSOLUTE",
    rules: Any = None,
    has_liquidation: bool = False,
) -> tuple[str, str]:
    """Return the independent exit suggestion ``(alert_code, action)``.

    The suggestion never rewrites :class:`PlanState.status` (B19.3):

    - ``CLOSED`` -> ``("NONE", "NONE")``;
    - single leg never opened -> ``("ORPHAN_LEG_WARNING", "REVIEW")``;
    - one leg drained after both opened ->
      ``("CRITICAL_ORPHAN_SPOT_LEG" | "CRITICAL_ORPHAN_FUTURES_LEG",
      "URGENT_PAIR_EXIT")``;
    - both legs positive but drift ``> 5pts`` or target unknown ->
      ``("RATIO_DRIFT_WARN", "REVIEW")``;
    - paired exit in progress (closes recorded, both still holding) ->
      ``("PAIR_EXIT_RECOMMENDED", "PAIR_EXIT")``;
    - healthy ``ACTIVE`` with no closes -> ``("NONE", "NONE")``.

    ``has_liquidation`` marks the orphan as a liquidation fallback
    (``CRITICAL_ORPHAN_*``); a normal early-zero leg uses the same
    urgent action with its own orphan code.
    """
    by_leg = _positions_by_leg(positions)
    fut = by_leg["FUTURES_SHORT"]
    spot = by_leg["SPOT_LONG"]
    fut_open = fut["open"]
    spot_open = spot["open"]
    fut_rem = fut["remaining"]
    spot_rem = spot["remaining"]
    assert isinstance(fut_open, Decimal)
    assert isinstance(spot_open, Decimal)
    assert isinstance(fut_rem, Decimal) and isinstance(spot_rem, Decimal)

    fut_rules = _rules_for_leg(rules, "FUTURES_SHORT")
    spot_rules = _rules_for_leg(rules, "SPOT_LONG")
    fut_done = _is_zero_or_dust(fut_rem, fut_rules)
    spot_done = _is_zero_or_dust(spot_rem, spot_rules)

    if fut_open == 0 and spot_open == 0:
        return ("NONE", "NONE")
    if fut_done and spot_done:
        return ("NONE", "NONE")
    if not fut_done and not spot_done:
        closed_total = fut["closed"] + spot["closed"]  # type: ignore[operator]
        assert isinstance(closed_total, Decimal)
        if closed_total > 0:
            return ("PAIR_EXIT_RECOMMENDED", "PAIR_EXIT")
        target = _parse_target_ratio(target_ratio)
        if target is None or fut_rem == 0:
            return ("RATIO_DRIFT_WARN", "REVIEW")
        with localcontext() as ctx:
            ctx.prec = 80
            drift = abs(spot_rem / fut_rem - target)
        if drift > DRIFT_TOLERANCE:
            return ("RATIO_DRIFT_WARN", "REVIEW")
        return ("NONE", "NONE")
    # Exactly one leg holds.
    if not fut_done and spot_done:
        # Spot drained, futures remain.
        if spot_open == 0:
            return ("ORPHAN_LEG_WARNING", "REVIEW")
        return ("CRITICAL_ORPHAN_FUTURES_LEG", "URGENT_PAIR_EXIT")
    # Spot holds, futures drained.
    if fut_open == 0:
        return ("ORPHAN_LEG_WARNING", "REVIEW")
    _ = has_liquidation  # liquidation and normal early-zero share the
    # urgent orphan path; the code already names the stranded leg.
    return ("CRITICAL_ORPHAN_SPOT_LEG", "URGENT_PAIR_EXIT")


def describe_position(
    positions: Any,
    target_ratio: Any = None,
    *,
    mode: str = "ABSOLUTE",
    plan_id: str = "",
    plan_version: int = 1,
    updated_at_ms: int = 0,
    prior_status: str | None = None,
    rules: Any = None,
    has_liquidation: bool = False,
) -> tuple[PlanState, str, str]:
    """Evaluate state plus the independent ``(alert_code, action)``."""
    state = evaluate_position_state(
        positions,
        target_ratio,
        mode=mode,
        plan_id=plan_id,
        plan_version=plan_version,
        updated_at_ms=updated_at_ms,
        prior_status=prior_status,
        rules=rules,
    )
    code, action = recommend_exit_action(
        positions,
        target_ratio,
        mode=mode,
        rules=rules,
        has_liquidation=has_liquidation,
    )
    return (state, code, action)


# ---------------------------------------------------------------------------
# Ledger: thin async wrapper over the H01 single-worker transaction.
# ---------------------------------------------------------------------------


class Ledger:
    """Immutable fill ledger over a :class:`ShortLabRepository` (H06).

    :meth:`apply_event` normalises generic B19 names (``OPEN`` / ``CLOSE``
    / ``ADJUSTMENT``) to the frozen H01 types, then delegates to the
    single indivisible worker op
    ``repository.apply_hedge_event``. The loop-bound per-plan
    :class:`asyncio.Lock` only guards that single submit await; pure
    normalisation before it and DTO conversion / state evaluation after
    it run outside the lock so provider requests never hold it.
    """

    def __init__(self, repository: Any) -> None:
        if repository is None:
            raise LedgerError("Ledger requires a ShortLabRepository")
        self._repo = repository
        # (running-loop id, plan_id) -> asyncio.Lock. Locks are created
        # lazily without awaits, so creation is atomic on the loop.
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    def _lock_for(self, plan_id: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        key = (id(loop), str(plan_id))
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def apply_event(
        self,
        plan_id: str,
        client_event_id: str,
        expected_version: int,
        event: Any,
        *,
        recorded_at_ms: int | None = None,
    ) -> LedgerResult:
        """Append one immutable event and return the new :class:`LedgerResult`.

        Thin wrapper: pure normalisation happens before the per-plan
        lock, exactly one ``await repository.apply_hedge_event`` happens
        under it, and frozen DTO conversion happens after it.
        Idempotency (same payload -> same result) and version conflicts
        (``HedgeIdempotencyError`` / ``HedgeVersionConflictError`` -> HTTP
        409) propagate unchanged from the repository.
        """
        if not plan_id or not client_event_id:
            raise LedgerError("plan_id/client_event_id must be non-empty")
        normalised = normalize_event_for_repo(event)
        lock = self._lock_for(plan_id)
        async with lock:
            if recorded_at_ms is None:
                raw = await self._repo.apply_hedge_event(
                    plan_id, client_event_id, expected_version, normalised
                )
            else:
                raw = await self._repo.apply_hedge_event(
                    plan_id,
                    client_event_id,
                    expected_version,
                    normalised,
                    recorded_at_ms=int(recorded_at_ms),
                )
        positions = tuple(
            HedgePosition(
                plan_id=str(item.get("plan_id", plan_id)),
                leg_type=str(item.get("leg_type")),
                open_qty=str(item.get("open_qty")),
                closed_qty=str(item.get("closed_qty")),
                remaining_qty=str(item.get("remaining_qty")),
                gross_qty=str(item.get("gross_qty", item.get("open_qty"))),
                net_qty=str(item.get("net_qty", item.get("remaining_qty"))),
                weighted_avg_price=item.get("weighted_avg_price"),
                event_ids=tuple(item.get("event_ids", ())),
            )
            for item in (raw.get("positions", ()) or ())
        )
        return LedgerResult(
            event_id=str(raw.get("event_id")),
            plan_id=str(raw.get("plan_id", plan_id)),
            plan_version=int(raw.get("plan_version")),
            positions=positions,
            balance_source=str(raw.get("balance_source", "CONFIRMED")),
            estimated=bool(raw.get("estimated", False)),
        )

    async def read_positions(self, plan_id: str) -> tuple[HedgePosition, ...]:
        """Re-aggregate live positions from immutable events (no DOUBLE)."""
        rows = await self._repo.aggregate_hedge_position(plan_id)
        return tuple(
            HedgePosition(
                plan_id=str(item.get("plan_id", plan_id)),
                leg_type=str(item.get("leg_type")),
                open_qty=str(item.get("open_qty")),
                closed_qty=str(item.get("closed_qty")),
                remaining_qty=str(item.get("remaining_qty")),
                gross_qty=str(item.get("gross_qty", item.get("open_qty"))),
                net_qty=str(item.get("net_qty", item.get("remaining_qty"))),
                weighted_avg_price=item.get("weighted_avg_price"),
                event_ids=tuple(item.get("event_ids", ())),
            )
            for item in (rows or ())
        )
