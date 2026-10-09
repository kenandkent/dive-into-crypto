"""R06b protection: platform STOP capability + manual confirmation (D06.3/D18.1, pure).

Pure module: no network, no DB, no clock reads. All amounts are Decimal
strings (D03.1); floats are rejected at the boundary. Maths runs under an
80-digit localcontext where needed; hashes use canonical JSON
(sorted keys, compact, UTF-8, no trailing newline) -> SHA256.

- :func:`resolve_stop_capability`: ``SUPPORTED``/``UNSUPPORTED``/``UNKNOWN``
  from explicit rule evidence only. ``stop_policy`` (user choice) never
  changes the platform capability. ``orderTypes`` containing an explicit
  ``STOP``/``STOP_MARKET``/``STOP_LOSS``/``CONDITIONAL`` ``True`` -- or
  ``stop_orders_supported=True`` with a non-empty
  ``conditional_orders_source_ref`` -- is ``SUPPORTED``. An explicit
  ``stop_orders_supported=False`` with a source ref -- or explicit
  ``STOP`` keys all ``False`` with no ``True`` -- is ``UNSUPPORTED``.
  Mere absence of ``STOP`` keys is ``UNKNOWN`` (never defaulted to
  ``UNSUPPORTED`` per D06.3/D15). ``None``/empty/malformed rules are
  ``UNKNOWN``, never an exception for missing evidence.
- :func:`compute_protected_position_hash`: canonical hash binding the two
  remaining quantities, liquidation/STOP params and rule IDs. Version bumps
  alone never invalidate: verification compares the hash, not the version.
- :func:`validate_protection_confirmation`: two-leg ``USER_CONFIRMED``
  validation with 24h default expiry. Futures leg carries
  STOP/qty/trigger-basis/platform receipt ref; spot leg carries the paired
  exit plan/remaining qty/platform capability. Future-dated confirmations
  are rejected; ``now >= expires`` is ``EXPIRED`` (``FAIL`` with
  ``PROTECTION_EXPIRED``); remaining-qty/liquidation/trigger/rule drift
  fails the hash binding; ordinary ``plan_version`` increments with an
  identical hash stay ``PASS``. ``PRIVATE_API_VERIFIED`` is never accepted:
  the source must be ``USER_CONFIRMED``. ``MANUAL_EXIT_ONLY`` spot plans
  validate ``PASS`` (ACTIVE allowed) but callers must keep monitoring
  ``LIMITED`` with continuous alerting.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from diveintocrypto_desktop.shortlab.repair_contracts import GateResult

__all__ = [
    "SUPPORTED",
    "UNSUPPORTED",
    "UNKNOWN",
    "CONFIRMATION_TTL_SEC",
    "CONFIRMATION_TTL_MS",
    "resolve_stop_capability",
    "compute_protected_position_hash",
    "validate_protection_confirmation",
]

SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
UNKNOWN = "UNKNOWN"

#: Default confirmation TTL: 24h (D06.3/D15).
CONFIRMATION_TTL_SEC = 86400
CONFIRMATION_TTL_MS = 86400 * 1000

_STOP_KEYS = ("STOP", "STOP_MARKET", "STOP_LOSS", "CONDITIONAL")


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_order_types(rules: Any) -> dict[str, Any] | None:
    raw = _field(rules, "order_types", None)
    if raw is None:
        # Flat mapping doubles may carry STOP keys at top level.
        if isinstance(rules, Mapping):
            found = {k: rules[k] for k in _STOP_KEYS if k in rules}
            found_limit = {k: rules[k] for k in ("LIMIT", "MARKET") if k in rules}
            if found or found_limit:
                merged: dict[str, Any] = {}
                merged.update(found_limit)
                merged.update(found)
                return merged
        return None
    if isinstance(raw, Mapping):
        return dict(raw)
    return None


def _normalise_decimal_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        text = str(value).strip() if isinstance(value, str) else str(value)
        parsed = Decimal(text)
    except (InvalidOperation, ValueError, AttributeError, ArithmeticError):
        return None
    if not parsed.is_finite():
        return None
    if parsed == 0:
        return "0"
    out = format(parsed, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
        if out in ("", "-"):
            return "0"
        if out.startswith("."):
            out = "0" + out
        elif out.startswith("-."):
            out = "-0." + out[2:]
    if out == "-0":
        return "0"
    return out


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


# ---------------------------------------------------------------------------
# resolve_stop_capability (D06.3/D18.1).
# ---------------------------------------------------------------------------


def resolve_stop_capability(rules: Mapping[str, Any] | Any | None) -> str:
    """Resolve futures STOP capability from explicit rule evidence.

    ``rules`` is a :class:`TradingRulesSnapshot` or a plain mapping with
    ``order_types`` / ``stop_orders_supported`` /
    ``conditional_orders_source_ref``. Returns ``SUPPORTED``/``UNSUPPORTED``
    /``UNKNOWN`` (never raises for missing evidence; ``None``/empty is
    ``UNKNOWN``). The caller's ``stop_policy`` is deliberately not an
    argument: user choice never changes platform capability.
    """
    if rules is None:
        return UNKNOWN
    # Explicit typed flag wins when accompanied by a source ref.
    flag = _field(rules, "stop_orders_supported", None)
    source_ref = _field(rules, "conditional_orders_source_ref", None)
    has_source = isinstance(source_ref, str) and bool(source_ref.strip())
    if flag is True and has_source:
        return SUPPORTED
    if flag is False and has_source:
        return UNSUPPORTED
    # Order-type evidence: any explicit STOP-family True is SUPPORTED.
    order_types = _as_order_types(rules)
    if order_types is not None:
        for key in _STOP_KEYS:
            if key in order_types and order_types[key] is True:
                return SUPPORTED
        # Explicit all-False STOP evidence (keys present, none True) is
        # UNSUPPORTED. Mere absence of STOP keys stays UNKNOWN per D15:
        # "仅不含STOP不能直接认定UNSUPPORTED".
        stop_keys_present = [k for k in _STOP_KEYS if k in order_types]
        if stop_keys_present:
            # All present STOP keys are False (bool False; truthy values
            # already returned SUPPORTED above).
            if all(order_types[k] is False for k in stop_keys_present):
                # Require at least LIMIT/MARKET context so a degenerate
                # {"STOP": False} alone is not over-interpreted? No: explicit
                # False is still evidence. Return UNSUPPORTED.
                return UNSUPPORTED
            # Mixed non-bool values (None, strings) are not explicit
            # evidence: UNKNOWN.
            return UNKNOWN
        # No STOP keys at all: UNKNOWN (even when flag is set without a
        # source ref, which lacks rule evidence).
        if flag is True or flag is False:
            # Flag without source ref has no rule evidence.
            return UNKNOWN
        return UNKNOWN
    # No order_types and no usable flag: UNKNOWN.
    if isinstance(rules, Mapping) and not rules:
        return UNKNOWN
    return UNKNOWN


# ---------------------------------------------------------------------------
# protected_position_hash (D06.3).
# ---------------------------------------------------------------------------


def compute_protected_position_hash(
    *,
    futures_remaining: Any,
    spot_remaining: Any,
    liquidation_price: Any,
    stop_trigger_price: Any | None = None,
    stop_trigger_basis: Any | None = None,
    rule_ids: Sequence[Any] | Any | None = None,
) -> str:
    """Canonical hash binding remaining qtys, liq/STOP params and rule IDs.

    All quantity/price inputs are normalised through Decimal fixed-point
    (``-0`` -> ``0``); rule IDs are stringified verbatim and sorted.
    """
    fut = _normalise_decimal_str(futures_remaining)
    spot = _normalise_decimal_str(spot_remaining)
    liq = _normalise_decimal_str(liquidation_price)
    if fut is None or spot is None or liq is None:
        raise ValueError(
            "futures_remaining/spot_remaining/liquidation_price must be decimal strings"
        )
    stop_px: str | None = None
    if stop_trigger_price is not None:
        stop_px = _normalise_decimal_str(stop_trigger_price)
        if stop_px is None:
            raise ValueError("stop_trigger_price must be a decimal string or None")
    basis = str(stop_trigger_basis).strip() if stop_trigger_basis is not None else None
    if isinstance(rule_ids, (str, bytes)):
        ids = [str(rule_ids)]
    elif rule_ids is None:
        ids = []
    else:
        try:
            ids = [str(v) for v in tuple(rule_ids)]
        except TypeError:
            ids = [str(rule_ids)]
    ids = sorted(ids)
    payload = {
        "futures_remaining": fut,
        "liquidation_price": liq,
        "rule_ids": ids,
        "spot_remaining": spot,
        "stop_trigger_basis": basis,
        "stop_trigger_price": stop_px,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _expected_hash_for_plan(
    plan: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]],
) -> str | None:
    """Recompute the expected hash from current plan + positions."""
    fut_rem: Any = None
    spot_rem: Any = None
    for pos in positions or ():
        if not isinstance(pos, Mapping):
            leg = getattr(pos, "leg_type", None)
            rem = getattr(pos, "remaining_qty", None)
        else:
            leg = pos.get("leg_type", pos.get("legType"))
            rem = pos.get("remaining_qty", pos.get("remainingQty", pos.get("remaining")))
        if not isinstance(leg, str):
            continue
        upper = leg.strip().upper()
        if upper == "FUTURES_SHORT" and fut_rem is None:
            fut_rem = rem
        elif upper == "SPOT_LONG" and spot_rem is None:
            spot_rem = rem
    if fut_rem is None or spot_rem is None:
        return None
    liq = plan.get("liquidation_price", plan.get("liquidationPrice"))
    stop_px = plan.get("stop_trigger_price", plan.get("stopTriggerPrice"))
    basis = plan.get("stop_trigger_basis", plan.get("stopTriggerBasis", "MARK_PRICE"))
    rule_ids: Any = plan.get("rule_ids", plan.get("ruleIds", plan.get("rule_id")))
    if rule_ids is None:
        # Single rule_version / rule_id fallbacks.
        for key in ("rule_version", "ruleVersion", "conditional_orders_source_ref"):
            if plan.get(key) is not None:
                rule_ids = (plan.get(key),)
                break
        else:
            rule_ids = ()
    try:
        return compute_protected_position_hash(
            futures_remaining=fut_rem,
            spot_remaining=spot_rem,
            liquidation_price=liq,
            stop_trigger_price=stop_px,
            stop_trigger_basis=basis,
            rule_ids=rule_ids,
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# validate_protection_confirmation (D06.3/D12/D18.1).
# ---------------------------------------------------------------------------


def _conf_value(conf: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in conf and conf[name] is not None:
            return conf[name]
    return default


def validate_protection_confirmation(
    record: Mapping[str, Any],
    plan: Mapping[str, Any],
    positions: Sequence[Mapping[str, Any]],
    now_ms: int,
) -> GateResult:
    """Validate a two-leg manual protection confirmation (pure).

    ``record`` is the stored protection row (with ``confirmation_json``) or
    the confirmation JSON itself; ``plan`` carries the current
    liquidation/STOP/rule IDs (``plan_version`` is ignored: only the hash
    binds validity); ``positions`` carries the current per-leg
    ``remaining_qty``. Returns :class:`GateResult` with
    ``checked_at_ms=now_ms``: ``PASS`` for a live binding confirmation,
    ``FAIL`` for explicit mismatch/expiry/cancellation, ``UNKNOWN`` when
    required evidence is missing (never defaulted to ``PASS``).
    """
    if isinstance(now_ms, bool) or not isinstance(now_ms, int):
        raise ValueError("now_ms must be an int")
    if not isinstance(record, Mapping):
        return GateResult("UNKNOWN", ("PROTECTION_RECORD_UNKNOWN",), int(now_ms), {})
    if not isinstance(plan, Mapping):
        return GateResult("UNKNOWN", ("PROTECTION_PLAN_UNKNOWN",), int(now_ms), {})
    plan_id = str(plan.get("plan_id", plan.get("planId", "unknown")))

    # Unwrap repository envelope vs raw confirmation JSON.
    conf: Mapping[str, Any]
    raw_conf = record.get("confirmation_json", record.get("confirmationJson"))
    if isinstance(raw_conf, str):
        try:
            parsed = json.loads(raw_conf)
            conf = parsed if isinstance(parsed, Mapping) else dict(record)
        except ValueError:
            conf = dict(record)
    elif isinstance(raw_conf, Mapping):
        conf = dict(raw_conf)
        # Top-level envelope fields (confirmed/expires/source/hash) may live
        # outside confirmation_json; prefer the envelope when present.
        for key in (
            "confirmed_at_ms",
            "confirmedAtMs",
            "expires_at_ms",
            "expiresAtMs",
            "protected_position_hash",
            "protectedPositionHash",
            "source",
            "confirmation_id",
            "confirmationId",
            "client_request_id",
            "clientRequestId",
        ):
            if record.get(key) is not None and conf.get(key) is None:
                try:
                    conf[key] = record[key]  # type: ignore[index]
                except TypeError:
                    pass
    else:
        conf = dict(record)

    confirmed = _conf_value(conf, "confirmed_at_ms", "confirmedAtMs")
    expires = _conf_value(conf, "expires_at_ms", "expiresAtMs")
    try:
        confirmed_ms = int(confirmed) if confirmed is not None else None
    except (TypeError, ValueError):
        confirmed_ms = None
    try:
        expires_ms = int(expires) if expires is not None else None
    except (TypeError, ValueError):
        expires_ms = None
    if confirmed_ms is None:
        return GateResult("UNKNOWN", ("PROTECTION_TIME_UNKNOWN",), int(now_ms), {"plan": plan_id})
    if isinstance(confirmed_ms, bool) or confirmed_ms < 0:
        return GateResult("UNKNOWN", ("PROTECTION_TIME_UNKNOWN",), int(now_ms), {"plan": plan_id})
    if confirmed_ms > int(now_ms):
        return GateResult(
            "FAIL", ("PROTECTION_FUTURE_CONFIRMATION",), int(now_ms), {"plan": plan_id}
        )
    if expires_ms is None:
        expires_ms = int(confirmed_ms) + int(CONFIRMATION_TTL_MS)
    if int(now_ms) >= int(expires_ms):
        return GateResult("FAIL", ("PROTECTION_EXPIRED",), int(now_ms), {"plan": plan_id})

    source = _conf_value(conf, "source", default="USER_CONFIRMED")
    if isinstance(source, str) and source.strip() == "PRIVATE_API_VERIFIED":
        return GateResult(
            "FAIL", ("PROTECTION_SOURCE_UNVERIFIED",), int(now_ms), {"plan": plan_id}
        )

    futures = _conf_value(conf, "futures", default=None)
    spot = _conf_value(conf, "spot", default=None)
    if not isinstance(futures, Mapping) or not isinstance(spot, Mapping):
        return GateResult(
            "FAIL", ("PROTECTION_LEGS_INCOMPLETE",), int(now_ms), {"plan": plan_id}
        )
    fut_status = str(
        _conf_value(futures, "status", default="") or ""
    ).strip().upper()
    spot_status = str(_conf_value(spot, "status", default="") or "").strip().upper()
    if fut_status == "" or spot_status == "":
        return GateResult(
            "FAIL", ("PROTECTION_LEGS_INCOMPLETE",), int(now_ms), {"plan": plan_id}
        )
    if fut_status == "CANCELLED" or spot_status == "CANCELLED":
        return GateResult(
            "FAIL", ("PROTECTION_CANCELLED",), int(now_ms), {"plan": plan_id}
        )
    if fut_status != "CONFIRMED" or spot_status != "CONFIRMED":
        return GateResult(
            "UNKNOWN", ("PROTECTION_STATUS_UNKNOWN",), int(now_ms), {"plan": plan_id}
        )

    # Spot exit mode: PLATFORM_ORDER or MANUAL_EXIT_ONLY (both validate PASS;
    # callers keep MANUAL_EXIT_ONLY at LIMITED monitoring).
    exit_mode = str(
        _conf_value(spot, "exitMode", "exit_mode", default="") or ""
    ).strip().upper()
    if exit_mode not in ("", "PLATFORM_ORDER", "MANUAL_EXIT_ONLY"):
        return GateResult(
            "FAIL", ("PROTECTION_EXIT_MODE_UNKNOWN",), int(now_ms), {"plan": plan_id}
        )

    # Quantity binding: confirmation legs must match current remainings.
    fut_conf_qty = _normalise_decimal_str(
        _conf_value(futures, "nativeQty", "native_qty", "qty", default=None)
    )
    spot_conf_qty = _normalise_decimal_str(
        _conf_value(spot, "nativeQty", "native_qty", "qty", default=None)
    )
    if fut_conf_qty is None or spot_conf_qty is None:
        return GateResult(
            "UNKNOWN", ("PROTECTION_QTY_UNKNOWN",), int(now_ms), {"plan": plan_id}
        )
    # Current remainings from positions.
    cur_fut: Any = None
    cur_spot: Any = None
    for pos in positions or ():
        if isinstance(pos, Mapping):
            leg = pos.get("leg_type", pos.get("legType"))
            rem = pos.get("remaining_qty", pos.get("remainingQty", pos.get("remaining")))
        else:
            leg = getattr(pos, "leg_type", None)
            rem = getattr(pos, "remaining_qty", None)
        if not isinstance(leg, str):
            continue
        upper = leg.strip().upper()
        if upper == "FUTURES_SHORT" and cur_fut is None:
            cur_fut = _normalise_decimal_str(rem)
        elif upper == "SPOT_LONG" and cur_spot is None:
            cur_spot = _normalise_decimal_str(rem)
    if cur_fut is None or cur_spot is None:
        return GateResult(
            "UNKNOWN", ("PROTECTION_POSITIONS_UNKNOWN",), int(now_ms), {"plan": plan_id}
        )
    try:
        if Decimal(fut_conf_qty) != Decimal(cur_fut) or Decimal(spot_conf_qty) != Decimal(cur_spot):
            return GateResult(
                "FAIL", ("PROTECTION_QTY_MISMATCH",), int(now_ms), {"plan": plan_id}
            )
    except (InvalidOperation, ValueError):
        return GateResult(
            "UNKNOWN", ("PROTECTION_QTY_UNKNOWN",), int(now_ms), {"plan": plan_id}
        )

    # Hash binding: stored hash must match recomputed current hash.
    stored_hash = _conf_value(
        conf, "protected_position_hash", "protectedPositionHash", default=None
    )
    if not isinstance(stored_hash, str) or not stored_hash.strip():
        return GateResult(
            "UNKNOWN", ("PROTECTION_HASH_UNKNOWN",), int(now_ms), {"plan": plan_id}
        )
    expected = _expected_hash_for_plan(plan, positions)
    if expected is None:
        return GateResult(
            "UNKNOWN", ("PROTECTION_HASH_UNKNOWN",), int(now_ms), {"plan": plan_id}
        )
    if stored_hash.strip().lower() != expected.strip().lower():
        return GateResult(
            "FAIL", ("PROTECTION_HASH_MISMATCH",), int(now_ms), {"plan": plan_id}
        )
    # Trigger/liq binding is covered by the hash; an additional direct
    # trigger-price check guards callers that rotate hashes without updating
    # the confirmation legs.
    conf_trigger = _normalise_decimal_str(
        _conf_value(futures, "triggerPrice", "trigger_price", "trigger", default=None)
    )
    plan_trigger = _normalise_decimal_str(
        plan.get("stop_trigger_price", plan.get("stopTriggerPrice"))
    )
    if conf_trigger is not None and plan_trigger is not None:
        try:
            if Decimal(conf_trigger) != Decimal(plan_trigger):
                return GateResult(
                    "FAIL", ("PROTECTION_TRIGGER_MISMATCH",), int(now_ms), {"plan": plan_id}
                )
        except (InvalidOperation, ValueError):
            pass

    conf_id = str(
        _conf_value(conf, "confirmation_id", "confirmationId", default="unknown")
    )
    return GateResult("PASS", (), int(now_ms), {"plan": plan_id, "confirmation": conf_id})
