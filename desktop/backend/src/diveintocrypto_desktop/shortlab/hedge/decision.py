"""R07 joint hedge decision (D07/D18, pure).

Pure module: no network, no DB, no clock reads. Consumes R05 funding gate
and R06a ratio proposals exclusively via ``RepairPorts`` callbacks; direct
imports of unmerged producers are forbidden. ``ports=None`` grants no
execution credit (DATA_INSUFFICIENT, never PASS).

Selection (D07.2):

- candidates ``0,0.25,0.5,0.75,1`` with legal quantities; actual vs target
  absolute deviation ``> 0.02`` is eliminated;
- CARRY only compares ``h=1``; DIRECTIONAL compares ``0..1`` and picks the
  minimal feasible target (most retained short exposure); BALANCED compares
  ``0.25..1`` requiring net Carry ``> 0`` and picks the minimal feasible;
- all six stress scenarios are required (pressure carry credit 0, 100bps
  exit stress lives inside R06a proposals); any liquidation crossing fails
  the candidate risk gate and never auto-assumes a stop fill;
- same-target multi-venue tie-break (cost asc, earliest expiry desc, venue
  asc) lives in R06a; R07 keeps alternatives deterministically ordered and
  never promotes a theory ``h`` over its legalised actual ``h``;
- amounts are Decimal-compared; unknown cost never enters executable
  selection; ``h0`` is contract-only read-only (no Spot leg).

Output (D07.3): ``DATA_INSUFFICIENT`` for unknown public inputs first,
``AVOID`` for explicit prohibition or no capital/cost feasible candidate,
``MANUAL_REVIEW`` only when failures are exclusively liquidation/protection
pending, otherwise ``NO_HEDGE``/``PARTIAL_HEDGE``/``FULL_HEDGE`` by selected
actual ratio (``h=1`` within 2%, ``h=0`` no Spot, else partial).
``NO_HEDGE`` is read-only contract guidance (no two-leg plan).
Decision expiry is ``min(generated + quote_valid_sec, used quote expiry)``.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping, Sequence

from diveintocrypto_desktop.shortlab.repair_contracts import (
    DecisionContext,
    DecisionRequest,
    DecisionResult,
    GateResult,
    RatioProposal,
)

__all__ = ["recommend_hedge"]

_FORMULA_VERSION = "hedge-decision-v1"
_VALIDATION_LEVEL = "RULE_BASED_UNVALIDATED"

_CARRY_TARGETS = ("1",)
_DIRECTIONAL_TARGETS = ("0", "0.25", "0.5", "0.75", "1")
_BALANCED_TARGETS = ("0.25", "0.5", "0.75", "1")

_EXPECTED_SCENARIOS = ("UP_50", "UP_100", "DOWN_50", "BASIS_UP", "BASIS_DOWN", "FX_DOWN")

_DEFAULT_TOLERANCE = Decimal("0.02")
_DEFAULT_QUOTE_VALID_SEC = 20
_DEFAULT_NEW_TOKEN_DAYS = 45
_DEFAULT_LIQ_MAX_AGE_MS = 86400 * 1000
_FUTURE_SKEW_MS = 2000


# ---------------------------------------------------------------------------
# Decimal helpers (no float ledger).
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
            raise ValueError(f"{name} must be non-empty")
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise ValueError(f"{name} is not a decimal string: {value!r}") from exc
    else:
        raise ValueError(f"{name} must be a decimal string (float rejected), got {value!r}")
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")
    return parsed


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


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# Policy extraction (ShortLabConfig or Mapping, read-only, with defaults).
# ---------------------------------------------------------------------------


def _extract_decision_node(policy: Any) -> dict[str, Any]:
    if policy is None:
        return {}
    opt = getattr(policy, "optimization", None)
    if opt is not None:
        dec = getattr(opt, "decision", None)
        if isinstance(dec, Mapping):
            return dict(dec)
        if dec is not None:
            try:
                return dict(dec)  # type: ignore[arg-type]
            except Exception:
                pass
    if isinstance(policy, Mapping):
        node: Any = policy
        if "optimization" in node and isinstance(node["optimization"], Mapping):
            node = node["optimization"]
            if "decision" in node and isinstance(node["decision"], Mapping):
                return dict(node["decision"])
            if "min_net_carry_usd" in node:
                return dict(node)
        if "decision" in node and isinstance(node["decision"], Mapping):
            return dict(node["decision"])
        if "min_net_carry_usd" in node or "capital_reserve_fraction" in node:
            return dict(node)
    return {}


def _extract_tolerance(policy: Any) -> Decimal:
    node = _extract_decision_node(policy)
    raw = node.get("ratio_tolerance", "0.02")
    try:
        parsed = _parse_decimal(raw, "ratio_tolerance")
    except ValueError:
        return _DEFAULT_TOLERANCE
    if parsed < 0 or parsed > 1:
        return _DEFAULT_TOLERANCE
    return parsed


def _extract_quote_valid_sec(policy: Any) -> int:
    node = _extract_decision_node(policy)
    raw = node.get("quote_valid_sec", _DEFAULT_QUOTE_VALID_SEC)
    try:
        if isinstance(raw, bool):
            raise ValueError("bool")
        out = int(Decimal(str(raw)))
    except Exception:
        return _DEFAULT_QUOTE_VALID_SEC
    return out if out >= 1 else _DEFAULT_QUOTE_VALID_SEC


def _extract_candidate_thresholds(policy: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ready_ltss": 80,
        "ready_entry": 70,
        "ready_data_quality": 80,
        "ready_tradeability_score": 7,
    }
    cand: Any = None
    if hasattr(policy, "candidate"):
        cand = getattr(policy, "candidate")
    elif isinstance(policy, Mapping):
        cand = policy.get("candidate")
        if cand is None and "shortlab" in policy and isinstance(policy["shortlab"], Mapping):
            cand = policy["shortlab"].get("candidate")
    if cand is None:
        return out
    for key in ("ready_ltss", "ready_entry", "ready_data_quality", "ready_tradeability_score"):
        raw = _field(cand, key, out[key])
        try:
            if isinstance(raw, bool):
                raise ValueError("bool")
            # tradeability is 0..10 int-ish, others 0..100.
            if key == "ready_tradeability_score":
                out[key] = int(Decimal(str(raw)))
            else:
                out[key] = int(Decimal(str(raw)))
        except Exception:
            pass
    return out


def _extract_new_token_days(policy: Any) -> int:
    default = _DEFAULT_NEW_TOKEN_DAYS
    veto: Any = None
    if hasattr(policy, "veto"):
        veto = getattr(policy, "veto")
    elif isinstance(policy, Mapping):
        veto = policy.get("veto")
        if veto is None and "shortlab" in policy and isinstance(policy["shortlab"], Mapping):
            veto = policy["shortlab"].get("veto")
    if veto is None:
        return default
    raw = _field(veto, "new_token_days", default)
    try:
        if isinstance(raw, bool):
            raise ValueError("bool")
        out = int(Decimal(str(raw)))
        return out if out >= 0 else default
    except Exception:
        return default


def _extract_liq_max_age_ms(policy: Any) -> int:
    default = _DEFAULT_LIQ_MAX_AGE_MS
    liq: Any = None
    if hasattr(policy, "hedge"):
        hedge = getattr(policy, "hedge")
        liq = getattr(hedge, "liquidation", None)
        if isinstance(liq, Mapping):
            raw = liq.get("user_price_max_age_sec", None)
            if raw is not None:
                try:
                    if not isinstance(raw, bool):
                        return int(Decimal(str(raw)) * Decimal("1000"))
                except Exception:
                    pass
            return default
        if liq is not None:
            raw2 = getattr(liq, "user_price_max_age_sec", None) if not isinstance(liq, Mapping) else None
            # Mapping-like hedge.liquidation may expose via _field.
            if raw2 is None:
                raw2 = _field(liq, "user_price_max_age_sec", None)
            if raw2 is not None:
                try:
                    if not isinstance(raw2, bool):
                        return int(Decimal(str(raw2)) * Decimal("1000"))
                except Exception:
                    pass
            return default
    if isinstance(policy, Mapping):
        node: Any = policy
        if "hedge" in node and isinstance(node["hedge"], Mapping):
            inner = node["hedge"].get("liquidation", None)
            if isinstance(inner, Mapping) and "user_price_max_age_sec" in inner:
                try:
                    raw3 = inner["user_price_max_age_sec"]
                    if not isinstance(raw3, bool):
                        return int(Decimal(str(raw3)) * Decimal("1000"))
                except Exception:
                    pass
        if "shortlab" in node and isinstance(node["shortlab"], Mapping):
            sub = node["shortlab"].get("hedge", None)
            if isinstance(sub, Mapping):
                inner2 = sub.get("liquidation", None)
                if isinstance(inner2, Mapping) and "user_price_max_age_sec" in inner2:
                    try:
                        raw4 = inner2["user_price_max_age_sec"]
                        if not isinstance(raw4, bool):
                            return int(Decimal(str(raw4)) * Decimal("1000"))
                    except Exception:
                        pass
    return default


def _compute_policy_hash(policy: Any) -> str:
    try:
        from diveintocrypto_desktop.shortlab.config import decision_policy_hash as _hash

        return str(_hash(policy))
    except Exception:
        return ""


def _formula_version(policy: Any) -> str:
    node = _extract_decision_node(policy)
    raw = node.get("version", _FORMULA_VERSION)
    if isinstance(raw, str) and raw:
        return raw
    return _FORMULA_VERSION


def _validation_level(policy: Any) -> str:
    node = _extract_decision_node(policy)
    raw = node.get("validation_level", _VALIDATION_LEVEL)
    if isinstance(raw, str) and raw:
        return raw
    return _VALIDATION_LEVEL


# ---------------------------------------------------------------------------
# Observation helpers (no clock reads; only compare to as_of).
# ---------------------------------------------------------------------------


def _obs_meta(obs: Any) -> Any:
    if obs is None:
        return None
    meta = getattr(obs, "meta", None)
    if meta is None and isinstance(obs, Mapping):
        meta = obs.get("meta", obs)
    return meta


def _obs_int(meta: Any, *keys: str) -> int | None:
    if meta is None:
        return None
    if isinstance(meta, Mapping):
        for key in keys:
            if key in meta and meta[key] is not None:
                try:
                    return int(meta[key])
                except (TypeError, ValueError):
                    return None
        return None
    for key in keys:
        if hasattr(meta, key):
            try:
                value = getattr(meta, key)
                return None if value is None else int(value)
            except (TypeError, ValueError):
                return None
    return None


def _extract_mark_native(futures_mark: Any) -> Decimal | None:
    if futures_mark is None:
        return None
    value = getattr(futures_mark, "value", None)
    if value is None and isinstance(futures_mark, Mapping):
        value = futures_mark.get("value", futures_mark)
    if isinstance(value, Mapping):
        for key in ("price", "mark_price", "markPrice", "native_price", "futures_price", "mark"):
            if value.get(key) is not None:
                try:
                    parsed = _parse_decimal(value[key], "futures_mark.price")
                    return parsed if parsed > 0 else None
                except ValueError:
                    continue
        return None
    try:
        parsed2 = _parse_decimal(value, "futures_mark.price")
        return parsed2 if parsed2 > 0 else None
    except ValueError:
        return None


def _extract_multiplier(identity: Any) -> Decimal | None:
    raw: Any = None
    if isinstance(identity, Mapping):
        raw = identity.get("contract_multiplier", identity.get("multiplier"))
    else:
        raw = getattr(identity, "contract_multiplier", getattr(identity, "multiplier", None))
    if raw is None:
        return None
    try:
        if isinstance(raw, bool):
            return None
        parsed = Decimal(str(raw).strip()) if isinstance(raw, str) else Decimal(str(raw))
    except Exception:
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _quote_expiry(quote: Any) -> int | None:
    if quote is None:
        return None
    raw = _field(quote, "expires_at_ms", None)
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Public input checks (D07.1). Unknown first, prohibition second.
# ---------------------------------------------------------------------------


def _check_identity(identity: Any) -> tuple[list[str], list[str]]:
    unknown: list[str] = []
    fail: list[str] = []
    conf = _field(identity, "mapping_confidence", None)
    if conf != "VERIFIED":
        unknown.append("IDENTITY_UNRESOLVED")
        return unknown, fail
    if _extract_multiplier(identity) is None:
        unknown.append("UNKNOWN_MULTIPLIER")
    fut_sym = _field(identity, "binance_futures_symbol", None)
    if not isinstance(fut_sym, str) or not fut_sym:
        unknown.append("IDENTITY_FUTURES_SYMBOL_UNKNOWN")
    return unknown, fail


def _check_mark(mark: Any, as_of: int, ttl_ms: int) -> tuple[list[str], list[str]]:
    unknown: list[str] = []
    fail: list[str] = []
    price = _extract_mark_native(mark)
    if price is None:
        unknown.append("UNKNOWN_PRICE")
        return unknown, fail
    meta = _obs_meta(mark)
    source = _obs_int(meta, "source_as_of_ms", "sourceAsOf")
    known = _obs_int(meta, "known_at_ms", "known_at", "fetched_at_ms")
    if source is None or known is None:
        unknown.append("MARK_STALE")
        return unknown, fail
    if known > as_of + _FUTURE_SKEW_MS:
        unknown.append("MARK_STALE")
        return unknown, fail
    if source > as_of + _FUTURE_SKEW_MS:
        unknown.append("MARK_STALE")
        return unknown, fail
    if as_of - source > ttl_ms:
        unknown.append("MARK_STALE")
        return unknown, fail
    return unknown, fail


def _check_liquidation(request: DecisionRequest, as_of: int, max_age_ms: int) -> tuple[list[str], list[str]]:
    unknown: list[str] = []
    fail: list[str] = []
    updated = int(request.liquidation_price_updated_at_ms)
    if updated > as_of + _FUTURE_SKEW_MS:
        unknown.append("LIQUIDATION_PRICE_STALE")
        return unknown, fail
    if as_of - updated > max_age_ms:
        unknown.append("LIQUIDATION_PRICE_STALE")
        return unknown, fail
    return unknown, fail


def _check_listing(funding_context: Any, new_token_days: int) -> tuple[list[str], list[str]]:
    unknown: list[str] = []
    fail: list[str] = []
    age = _field(funding_context, "listing_age_days", None)
    if age is None:
        unknown.append("LISTING_AGE_UNKNOWN")
        return unknown, fail
    try:
        if isinstance(age, bool):
            raise ValueError("bool")
        days = int(age)
    except Exception:
        unknown.append("LISTING_AGE_UNKNOWN")
        return unknown, fail
    if days < 0:
        unknown.append("LISTING_AGE_UNKNOWN")
        return unknown, fail
    if days < new_token_days:
        fail.append("NEW_TOKEN_PAUSE")
    return unknown, fail


def _as_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # NaN/Inf are unknown, never thresholds.
    try:
        import math as _math

        if not _math.isfinite(out):
            return None
    except Exception:
        return None
    return out


def _check_directional(
    directional: Any | None,
    goal: str,
    thresholds: dict[str, Any],
) -> tuple[list[str], list[str]]:
    unknown: list[str] = []
    fail: list[str] = []
    if goal == "CARRY_CAPTURE":
        # Carry does not require directional LTSS; if a directional snapshot is
        # present we still enforce no BLOCK/PAUSE + READY execution (public
        # trading-status gate), but never LTSS thresholds.
        if directional is None:
            return unknown, fail
        vetoes = _field(directional, "vetoes", [])
        pauses = _field(directional, "pauses", [])
        try:
            veto_list = list(vetoes) if isinstance(vetoes, (list, tuple)) else []
        except Exception:
            veto_list = []
        try:
            pause_list = list(pauses) if isinstance(pauses, (list, tuple)) else []
        except Exception:
            pause_list = []
        if veto_list:
            fail.append("DIRECTIONAL_VETOED")
        if pause_list:
            fail.append("DIRECTIONAL_PAUSED")
        exec_status = _field(directional, "execution_status", None)
        cand_status = _field(directional, "candidate_status", None)
        if isinstance(exec_status, str) and exec_status in ("BLOCKED", "BLOCK", "PAUSE", "PAUSED"):
            fail.append("DIRECTIONAL_BLOCKED")
        if isinstance(cand_status, str) and cand_status in ("BLOCKED", "BLOCK"):
            fail.append("DIRECTIONAL_BLOCKED")
        # Explicit READY requirement for carry as well when directional present?
        # Only enforce BLOCK/PAUSE, not full READY, to keep CARRY null-compatible.
        return unknown, fail
    # DIRECTIONAL_SHORT / BALANCED require full directional readiness.
    if directional is None:
        unknown.append("DIRECTIONAL_SCORE_UNKNOWN")
        return unknown, fail
    if not isinstance(directional, Mapping):
        unknown.append("DIRECTIONAL_SCORE_UNKNOWN")
        return unknown, fail
    profile = directional.get("profile", None)
    if profile != "MEME":
        fail.append("DIRECTIONAL_PROFILE_MISMATCH")
    # Scores may be null only as unknown (never default).
    ltss = _as_float_or_none(directional.get("ltss", None))
    entry = _as_float_or_none(directional.get("entry", None))
    dq = _as_float_or_none(directional.get("data_quality", None))
    trade = _as_float_or_none(directional.get("tradeability", None))
    if ltss is None or entry is None or dq is None or trade is None:
        unknown.append("DIRECTIONAL_SCORE_UNKNOWN")
        return unknown, fail
    try:
        need_ltss = int(thresholds.get("ready_ltss", 80))
        need_entry = int(thresholds.get("ready_entry", 70))
        need_dq = int(thresholds.get("ready_data_quality", 80))
        need_trade = int(thresholds.get("ready_tradeability_score", 7))
    except Exception:
        need_ltss, need_entry, need_dq, need_trade = 80, 70, 80, 7
    if not (ltss >= need_ltss and entry >= need_entry and dq >= need_dq and trade >= need_trade):
        fail.append("DIRECTIONAL_NOT_READY")
    cand_status2 = directional.get("candidate_status", None)
    exec_status2 = directional.get("execution_status", None)
    if exec_status2 != "READY":
        fail.append("DIRECTIONAL_NOT_READY")
    if cand_status2 not in ("CANDIDATE", "READY"):
        # CANDIDATE is the frozen READY-bucket marker; anything else blocks.
        if cand_status2 in ("BLOCKED", "BLOCK", "WATCH", "PAUSED", "PAUSE"):
            fail.append("DIRECTIONAL_BLOCKED")
        else:
            fail.append("DIRECTIONAL_NOT_READY")
    vetoes2 = directional.get("vetoes", [])
    pauses2 = directional.get("pauses", [])
    try:
        v2 = list(vetoes2) if isinstance(vetoes2, (list, tuple)) else []
    except Exception:
        v2 = []
    try:
        p2 = list(pauses2) if isinstance(pauses2, (list, tuple)) else []
    except Exception:
        p2 = []
    if v2:
        fail.append("DIRECTIONAL_VETOED")
    if p2:
        fail.append("DIRECTIONAL_PAUSED")
    return unknown, fail


# ---------------------------------------------------------------------------
# Per-proposal feasibility (actual proposal only, Decimal compare).
# ---------------------------------------------------------------------------


def _proposal_feasibility(
    proposal: RatioProposal,
    request: DecisionRequest,
    goal: str,
    tolerance: Decimal,
) -> tuple[bool, list[str], list[str]]:
    """Return (feasible, fail_reasons, unknown_reasons) for one proposal."""
    fail: list[str] = []
    unknown: list[str] = []

    # -- scenarios: exactly six frozen ids ------------------------------------
    scenarios: Sequence[Any] = tuple(getattr(proposal, "scenarios", ()) or ())
    if len(scenarios) != 6:
        unknown.append("SCENARIO_UNKNOWN")
        return False, fail, unknown
    ids = {getattr(s, "scenario_id", None) for s in scenarios}
    if set(_EXPECTED_SCENARIOS) != ids:
        unknown.append("SCENARIO_UNKNOWN")
        return False, fail, unknown

    # -- target / actual deviation (absolute, not relative) --------------------
    try:
        target = _parse_decimal(proposal.target_ratio, "target_ratio")
        actual = _parse_decimal(proposal.actual_ratio, "actual_ratio")
    except ValueError:
        unknown.append("RATIO_UNKNOWN")
        return False, fail, unknown
    with localcontext() as ctx:
        ctx.prec = 80
        deviation = abs(actual - target)
    if deviation > tolerance:
        fail.append("RATIO_DEVIATION_EXCEEDS_TOLERANCE")

    # -- h0 read-only honesty --------------------------------------------------
    try:
        h0_target = _parse_decimal(proposal.target_ratio, "t") == 0
    except ValueError:
        h0_target = False
    if h0_target:
        try:
            spot_qty = _parse_decimal(proposal.spot_net_qty, "spot_net_qty")
        except ValueError:
            unknown.append("SPOT_QTY_UNKNOWN")
            spot_qty = None
        if spot_qty is not None and spot_qty != 0:
            fail.append("H0_SPOT_MUST_BE_ZERO")
        if getattr(proposal, "spot_venue", None) is not None:
            fail.append("H0_SPOT_MUST_BE_ZERO")
        # NOTE: R00 TEST_FAKE ratio fixtures keep a stale "spot" quote_ref for
        # h0; real R06a h0 proposals carry no Spot refs. Only the quantities
        # and venue are enforced here so the fake stays consumable.

    # -- execution / risk gates -------------------------------------------------
    exec_gate = getattr(proposal, "execution_gate", None)
    risk_gate = getattr(proposal, "risk_gate", None)
    econ = getattr(proposal, "economics", None)

    # Economics gate handling per goal (R07 refines R06a safety mark).
    econ_fail: list[str] = []
    econ_unknown: list[str] = []
    net: Decimal | None = None
    net_known = False
    if econ is None:
        econ_unknown.append("ECONOMIC_GATE_UNKNOWN")
    else:
        gate = getattr(econ, "gate", None)
        if gate is None:
            econ_unknown.append("ECONOMIC_GATE_UNKNOWN")
        elif getattr(gate, "status", None) == "FAIL":
            reasons = tuple(getattr(gate, "reasons", ()) or ())
            # Directional ignores pure net-carry shortfalls (price PnL, not
            # carry, is the directional edge); unknown costs still block.
            if goal == "DIRECTIONAL_SHORT" and reasons and all(
                r in ("NET_CARRY_BELOW_MIN", "NON_POSITIVE_CARRY") for r in reasons
            ):
                pass
            else:
                econ_fail.append("ECONOMIC_GATE_FAIL")
                for r in reasons:
                    if r not in econ_fail:
                        econ_fail.append(str(r))
        elif getattr(gate, "status", None) == "UNKNOWN":
            econ_unknown.append("ECONOMIC_GATE_UNKNOWN")
            for r in tuple(getattr(gate, "reasons", ()) or ()):
                if r not in econ_unknown:
                    econ_unknown.append(str(r))
        elif getattr(gate, "status", None) != "PASS":
            econ_unknown.append("ECONOMIC_GATE_UNKNOWN")
        # Net carry value for BALANCED explicit check.
        try:
            raw_net = getattr(econ, "net_carry_usd", None)
            if raw_net is not None:
                net = _parse_decimal(raw_net, "net_carry_usd")
                net_known = True
        except ValueError:
            net = None
            net_known = False
        # Roundtrip cost must be known for any executable choice.
        try:
            raw_cost = getattr(econ, "roundtrip_cost_usd", None)
            if raw_cost is None:
                econ_unknown.append("UNKNOWN_COST")
            else:
                _parse_decimal(raw_cost, "roundtrip_cost_usd")
        except ValueError:
            if "UNKNOWN_COST" not in econ_unknown:
                econ_unknown.append("UNKNOWN_COST")
        # Capital must be known; comparison below decides FAIL vs UNKNOWN.
        try:
            raw_cap = getattr(econ, "capital_required_usd", None)
            if raw_cap is None:
                econ_unknown.append("UNKNOWN_CAPITAL")
            else:
                cap_req = _parse_decimal(raw_cap, "capital_required_usd")
                try:
                    avail = _parse_decimal(request.available_capital_usd, "available_capital_usd")
                except ValueError:
                    econ_unknown.append("UNKNOWN_CAPITAL")
                    cap_req = None  # type: ignore[assignment]
                if cap_req is not None:
                    with localcontext() as ctx:
                        ctx.prec = 80
                        if cap_req > avail:
                            fail.append("INSUFFICIENT_CAPITAL")
        except ValueError:
            if "UNKNOWN_CAPITAL" not in econ_unknown:
                econ_unknown.append("UNKNOWN_CAPITAL")

    # BALANCED explicitly requires net Carry > 0 (strictly).
    if goal == "BALANCED":
        if not net_known or net is None:
            if "UNKNOWN_COST" not in econ_unknown and "ECONOMIC_GATE_UNKNOWN" not in econ_unknown:
                econ_unknown.append("UNKNOWN_COST")
        elif net <= 0:
            fail.append("NET_CARRY_BELOW_MIN")

    if econ_fail:
        fail.extend([r for r in econ_fail if r not in fail])
    if econ_unknown:
        unknown.extend([r for r in econ_unknown if r not in unknown])

    # Execution gate (filter ignorable economic fail for directional).
    if exec_gate is None:
        unknown.append("EXECUTION_GATE_UNKNOWN")
    elif getattr(exec_gate, "status", None) == "FAIL":
        for r in tuple(getattr(exec_gate, "reasons", ()) or ()):
            code = str(r)
            if goal == "DIRECTIONAL_SHORT" and code == "ECONOMIC_GATE_FAIL":
                # Ignorable only when economics failure itself was ignorable
                # (pure net shortfall, no unknown/capital).
                if not econ_fail and not econ_unknown:
                    continue
                # If econ_fail is empty due to directional ignore, skip.
                if not econ_fail:
                    continue
            if code not in fail:
                fail.append(code)
        if not tuple(getattr(exec_gate, "reasons", ()) or ()):
            if "EXECUTION_GATE_FAIL" not in fail:
                fail.append("EXECUTION_GATE_FAIL")
    elif getattr(exec_gate, "status", None) == "UNKNOWN":
        for r in tuple(getattr(exec_gate, "reasons", ()) or ()):
            code = str(r)
            if code not in unknown:
                unknown.append(code)
        if not tuple(getattr(exec_gate, "reasons", ()) or ()):
            if "EXECUTION_GATE_UNKNOWN" not in unknown:
                unknown.append("EXECUTION_GATE_UNKNOWN")
    elif getattr(exec_gate, "status", None) != "PASS":
        unknown.append("EXECUTION_GATE_UNKNOWN")

    # Risk gate + scenario liquidation / loss budget (actual stressed losses).
    scen_liquidation = False
    scen_unknown = False
    worst_loss: Decimal | None = None
    for s in scenarios:
        status = getattr(s, "status", None)
        if status == "INVALID_AFTER_LIQUIDATION":
            scen_liquidation = True
        elif status == "UNKNOWN":
            scen_unknown = True
        elif status == "VALID":
            raw_loss = getattr(s, "loss_usd", None)
            if raw_loss is not None:
                try:
                    lv = _parse_decimal(raw_loss, "loss_usd")
                    worst_loss = lv if worst_loss is None or lv > worst_loss else worst_loss
                except ValueError:
                    scen_unknown = True
            else:
                # VALID without loss is treated as zero loss only when net is
                # present; missing loss with present net is still comparable as
                # zero? To avoid zero-filling, mark unknown when net present
                # but loss missing.
                raw_net_s = getattr(s, "net_pnl_usd", None)
                if raw_net_s is not None:
                    scen_unknown = True
        else:
            scen_unknown = True
    if scen_liquidation:
        if "SCENARIO_LIQUIDATION" not in fail:
            fail.append("SCENARIO_LIQUIDATION")
    if scen_unknown:
        if "SCENARIO_UNKNOWN" not in unknown:
            unknown.append("SCENARIO_UNKNOWN")

    if risk_gate is None:
        unknown.append("RISK_GATE_UNKNOWN")
    elif getattr(risk_gate, "status", None) == "FAIL":
        for r in tuple(getattr(risk_gate, "reasons", ()) or ()):
            code = str(r)
            if code not in fail:
                fail.append(code)
        if not tuple(getattr(risk_gate, "reasons", ()) or ()):
            if "RISK_GATE_FAIL" not in fail:
                fail.append("RISK_GATE_FAIL")
    elif getattr(risk_gate, "status", None) == "UNKNOWN":
        for r in tuple(getattr(risk_gate, "reasons", ()) or ()):
            code = str(r)
            if code not in unknown:
                unknown.append(code)
        if not tuple(getattr(risk_gate, "reasons", ()) or ()):
            if "RISK_GATE_UNKNOWN" not in unknown:
                unknown.append("RISK_GATE_UNKNOWN")
    elif getattr(risk_gate, "status", None) != "PASS":
        unknown.append("RISK_GATE_UNKNOWN")

    # Loss budget from actual stressed losses (carry credit 0 in proposals).
    if worst_loss is not None and not scen_unknown:
        try:
            budget = _parse_decimal(request.max_scenario_loss_usd, "max_scenario_loss_usd")
            with localcontext() as ctx:
                ctx.prec = 80
                if worst_loss > budget:
                    if "SCENARIO_LOSS_EXCEEDS_BUDGET" not in fail:
                        fail.append("SCENARIO_LOSS_EXCEEDS_BUDGET")
        except ValueError:
            if "SCENARIO_UNKNOWN" not in unknown:
                unknown.append("SCENARIO_UNKNOWN")

    feasible = (not fail) and (not unknown)
    return feasible, sorted(set(fail)), sorted(set(unknown))


# ---------------------------------------------------------------------------
# Decision assembly helpers.
# ---------------------------------------------------------------------------


def _compute_decision_id(
    request: DecisionRequest,
    context: DecisionContext,
    policy_hash: str,
    generated_at: int,
) -> str:
    from diveintocrypto_desktop.shortlab.repair_contracts import canonical_json

    payload = {
        "available_capital_usd": str(request.available_capital_usd),
        "fcs_snapshot_id": str(context.fcs_snapshot_id),
        "futures_notional_usd": str(request.futures_notional_usd),
        "generated_at_ms": int(generated_at),
        "goal": str(request.goal),
        "identity_snapshot_id": str(context.identity_snapshot_id),
        "liquidation_price": str(request.liquidation_price),
        "liquidation_price_updated_at_ms": int(request.liquidation_price_updated_at_ms),
        "margin_usd": str(request.margin_usd),
        "max_scenario_loss_usd": str(request.max_scenario_loss_usd),
        "planned_hold_days": int(request.planned_hold_days),
        "policy_hash": str(policy_hash),
        "preferred_spot_venue": str(request.preferred_spot_venue),
        "symbol": str(request.symbol),
    }
    raw = canonical_json(payload)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"dec-{digest}"


def _compute_expires(
    generated_at: int,
    quote_valid_sec: int,
    context: DecisionContext,
    selected: RatioProposal | None,
) -> int:
    gen_expiry = int(generated_at) + int(quote_valid_sec) * 1000
    expiries: list[int] = []
    fq = getattr(context, "futures_quote", None)
    fq_exp = _quote_expiry(fq)
    if fq_exp is not None:
        expiries.append(int(fq_exp))
    venues: Sequence[Any] = tuple(getattr(context, "venue_quotes", ()) or ())
    if selected is not None:
        venue_name = getattr(selected, "spot_venue", None)
        if venue_name is not None:
            for q in venues:
                if str(_field(q, "venue", "")) == str(venue_name):
                    exp = _quote_expiry(q)
                    if exp is not None:
                        expiries.append(int(exp))
                    break
        # h0 has no spot expiry beyond futures.
    else:
        for q in venues:
            exp = _quote_expiry(q)
            if exp is not None:
                expiries.append(int(exp))
    if not expiries:
        return int(gen_expiry)
    # min(generated + 20s, used current Mark/Quote expiry).
    out = min([int(gen_expiry)] + [int(e) for e in expiries])
    # DecisionResult requires expires > generated; an already-expired quote
    # cannot yield an executable selection, but the envelope must stay valid.
    if out <= int(generated_at):
        return int(gen_expiry)
    return int(out)


def _compute_context_refs(context: DecisionContext) -> dict[str, str]:
    refs: dict[str, str] = {}
    refs["identity"] = str(context.identity_snapshot_id)
    refs["fcs"] = str(context.fcs_snapshot_id)
    # Carry through explicit source refs (frozen, no invention).
    try:
        src = dict(getattr(context, "source_refs", {}) or {})
        for k, v in src.items():
            if isinstance(k, str) and isinstance(v, str) and k not in refs:
                refs[k] = v
    except Exception:
        pass
    # Funding input refs (e.g. {"funding": "fcs-..."}).
    try:
        funding = getattr(context, "funding_context", None)
        inp = getattr(funding, "input_refs", None)
        if isinstance(inp, Mapping):
            for k, v in inp.items():
                if isinstance(k, str) and isinstance(v, str) and k not in refs:
                    refs[f"funding_{k}"] = v
    except Exception:
        pass
    if getattr(context, "directional_score_id", None):
        try:
            refs["directional_score"] = str(context.directional_score_id)
        except Exception:
            pass
    return refs


# ---------------------------------------------------------------------------
# Main entry.
# ---------------------------------------------------------------------------


def recommend_hedge(
    request: DecisionRequest,
    context: DecisionContext,
    policy: Mapping[str, Any] | Any,
    *,
    ports: Any | None = None,
) -> DecisionResult:
    """Joint rule suggestion (pure, no network/DB/clock reads)."""
    if not isinstance(request, DecisionRequest):
        raise TypeError(f"request must be DecisionRequest, got {type(request).__name__}")
    if not isinstance(context, DecisionContext):
        raise TypeError(f"context must be DecisionContext, got {type(context).__name__}")

    as_of = int(getattr(context, "as_of_ms", 0) or 0)
    generated_at = int(as_of)
    tolerance = _extract_tolerance(policy)
    quote_valid_sec = _extract_quote_valid_sec(policy)
    policy_hash = _compute_policy_hash(policy)
    formula_version = _formula_version(policy)
    validation_level = _validation_level(policy)
    decision_id = _compute_decision_id(request, context, policy_hash, generated_at)
    context_refs = _compute_context_refs(context)

    if request.goal == "CARRY_CAPTURE":
        targets: Sequence[str] = _CARRY_TARGETS
    elif request.goal == "DIRECTIONAL_SHORT":
        targets = _DIRECTIONAL_TARGETS
    elif request.goal == "BALANCED":
        targets = _BALANCED_TARGETS
    else:  # pragma: no cover - DTO already validates goal.
        raise ValueError(f"unknown goal {request.goal!r}")

    # -- ports=None grants no execution credit ---------------------------------
    build_fn = getattr(ports, "build_ratio_proposal", None) if ports is not None else None
    gate_fn = getattr(ports, "evaluate_funding_entry_gate", None) if ports is not None else None
    if ports is None or not callable(build_fn) or not callable(gate_fn):
        expires = _compute_expires(generated_at, quote_valid_sec, context, None)
        return DecisionResult(
            decision_id=decision_id,
            generated_at_ms=generated_at,
            expires_at_ms=expires,
            request=request,
            context_refs=dict(context_refs),
            recommendation="DATA_INSUFFICIENT",
            selected_proposal=None,
            alternatives=(),
            reasons=("IMPLEMENTATION_UNAVAILABLE", "PORTS_UNBOUND"),
            assumptions=(),
            validation_level=validation_level,
            decision_policy_hash=policy_hash,
            formula_version=formula_version,
        )

    # -- funding gate via ports (read-only consumption) -------------------------
    try:
        funding_gate = gate_fn(context.funding_context, policy, as_of)
        if not isinstance(funding_gate, GateResult):
            funding_gate = GateResult("UNKNOWN", ("FUNDING_GATE_UNKNOWN",), as_of, {})
    except Exception as exc:
        # Unbound collaborator surfaces as IMPLEMENTATION_UNAVAILABLE.
        name = getattr(exc, "port_name", None)
        if name is not None or "not bound" in str(exc).lower() or "unavailable" in str(exc).lower():
            funding_gate = GateResult("UNKNOWN", ("IMPLEMENTATION_UNAVAILABLE",), as_of, {})
        else:
            funding_gate = GateResult("UNKNOWN", ("FUNDING_GATE_UNAVAILABLE",), as_of, {})

    # -- public input checks ------------------------------------------------------
    public_unknown: list[str] = []
    public_fail: list[str] = []

    u_id, f_id = _check_identity(getattr(context, "identity", None))
    public_unknown.extend(u_id)
    public_fail.extend(f_id)

    mark_ttl_ms = int(quote_valid_sec) * 1000
    u_mark, f_mark = _check_mark(getattr(context, "futures_mark", None), as_of, mark_ttl_ms)
    public_unknown.extend(u_mark)
    public_fail.extend(f_mark)

    u_liq, f_liq = _check_liquidation(request, as_of, _extract_liq_max_age_ms(policy))
    public_unknown.extend(u_liq)
    public_fail.extend(f_liq)

    u_list, f_list = _check_listing(
        getattr(context, "funding_context", None), _extract_new_token_days(policy)
    )
    public_unknown.extend(u_list)
    public_fail.extend(f_list)

    u_dir, f_dir = _check_directional(
        getattr(context, "directional", None), str(request.goal), _extract_candidate_thresholds(policy)
    )
    public_unknown.extend(u_dir)
    public_fail.extend(f_dir)

    if getattr(funding_gate, "status", None) == "UNKNOWN":
        for r in tuple(getattr(funding_gate, "reasons", ()) or ()):
            if str(r) not in public_unknown:
                public_unknown.append(str(r))
        if not tuple(getattr(funding_gate, "reasons", ()) or ()):
            public_unknown.append("FUNDING_GATE_UNKNOWN")
    elif getattr(funding_gate, "status", None) == "FAIL":
        for r in tuple(getattr(funding_gate, "reasons", ()) or ()):
            if str(r) not in public_fail:
                public_fail.append(str(r))
        if not tuple(getattr(funding_gate, "reasons", ()) or ()):
            public_fail.append("FUNDING_GATE_FAIL")

    # -- per-candidate proposals via ports ----------------------------------------
    proposals: dict[str, RatioProposal | None] = {}
    candidate_fail: dict[str, list[str]] = {}
    candidate_unknown: dict[str, list[str]] = {}
    feasible: list[RatioProposal] = []

    for target in targets:
        try:
            prop = build_fn(request, context, str(target), policy, ports=ports)
            if not isinstance(prop, RatioProposal):
                proposals[str(target)] = None
                candidate_unknown[str(target)] = ["PROPOSAL_UNAVAILABLE"]
                continue
        except Exception as exc:
            proposals[str(target)] = None
            msg = str(exc)
            if "not bound" in msg.lower() or "unavailable" in msg.lower():
                candidate_unknown[str(target)] = ["IMPLEMENTATION_UNAVAILABLE"]
            else:
                candidate_unknown[str(target)] = ["PROPOSAL_UNAVAILABLE"]
            continue
        proposals[str(target)] = prop
        ok, fail_reasons, unknown_reasons = _proposal_feasibility(prop, request, str(request.goal), tolerance)
        candidate_fail[str(target)] = list(fail_reasons)
        candidate_unknown[str(target)] = list(unknown_reasons)
        if ok:
            feasible.append(prop)

    # Deterministic order: minimal target first (most retained short exposure).
    def _target_key(p: RatioProposal) -> Any:
        try:
            return _parse_decimal(p.target_ratio, "t")
        except ValueError:
            return Decimal("999")

    feasible.sort(key=_target_key)

    # -- public gates outrank any fake-feasible proposal ---------------------------
    # D07.3 priority: unknown public inputs first (even when a TEST_FAKE
    # proposal claims PASS), then explicit prohibition. This keeps funding
    # FAIL / listing pause / directional BLOCK honest when fakes ignore them.
    if public_unknown:
        expires_pu = _compute_expires(generated_at, quote_valid_sec, context, None)
        return DecisionResult(
            decision_id=decision_id,
            generated_at_ms=generated_at,
            expires_at_ms=expires_pu,
            request=request,
            context_refs=dict(context_refs),
            recommendation="DATA_INSUFFICIENT",
            selected_proposal=None,
            alternatives=(),
            reasons=tuple(sorted(set(public_unknown))),
            assumptions=(),
            validation_level=validation_level,
            decision_policy_hash=policy_hash,
            formula_version=formula_version,
        )
    if public_fail:
        expires_pf = _compute_expires(generated_at, quote_valid_sec, context, None)
        return DecisionResult(
            decision_id=decision_id,
            generated_at_ms=generated_at,
            expires_at_ms=expires_pf,
            request=request,
            context_refs=dict(context_refs),
            recommendation="AVOID",
            selected_proposal=None,
            alternatives=(),
            reasons=tuple(sorted(set(public_fail))),
            assumptions=(),
            validation_level=validation_level,
            decision_policy_hash=policy_hash,
            formula_version=formula_version,
        )

    # -- selection ------------------------------------------------------------------
    if feasible:
        selected = feasible[0]
        # h0 is contract-only read-only: strip any stale Spot refs the
        # TEST_FAKE keeps so the selected proposal never fabricates a Spot leg.
        try:
            _sel_target = _parse_decimal(selected.target_ratio, "t")
        except ValueError:
            _sel_target = None
        if _sel_target is not None and _sel_target == 0:
            try:
                _refs = dict(getattr(selected, "quote_refs", {}) or {})
                if "spot" in _refs:
                    _clean_refs = {k: v for k, v in _refs.items() if k != "spot"}
                    selected = RatioProposal(
                        target_ratio=selected.target_ratio,
                        actual_ratio=selected.actual_ratio,
                        futures_contract_qty=selected.futures_contract_qty,
                        canonical_futures_qty=selected.canonical_futures_qty,
                        spot_net_qty=selected.spot_net_qty,
                        spot_venue=None,
                        quote_refs=_clean_refs,
                        economics=selected.economics,
                        scenarios=selected.scenarios,
                        execution_gate=selected.execution_gate,
                        risk_gate=selected.risk_gate,
                        order_guidance=tuple(
                            g for g in (selected.order_guidance or ()) if not (isinstance(g, Mapping) and g.get("leg") == "SPOT_LONG")
                        ),
                    )
                    # Keep feasible/alternatives consistent (selected cleaned).
                    feasible = [selected] + [p for p in feasible[1:] if p is not feasible[0]]
            except Exception:
                pass
        # Map actual ratio to recommendation (2% absolute tolerance).
        try:
            actual_dec = _parse_decimal(selected.actual_ratio, "actual_ratio")
        except ValueError:
            actual_dec = Decimal("-1")
        is_h0 = actual_dec == 0 and getattr(selected, "spot_venue", None) is None
        is_full = abs(actual_dec - Decimal("1")) <= tolerance and str(selected.target_ratio) in ("1", "1.0", "1.00")
        if is_h0:
            recommendation = "NO_HEDGE"
        elif is_full:
            recommendation = "FULL_HEDGE"
        else:
            recommendation = "PARTIAL_HEDGE"
        alternatives = tuple(p for p in feasible[1:])
        # Deterministic alternatives: cost asc, expiry desc, venue asc (R06a
        # already tie-breaks venues; keep stable order by target then cost).
        reasons: tuple[str, ...] = ()
        assumptions: tuple[str, ...] = ()
        expires = _compute_expires(generated_at, quote_valid_sec, context, selected)
        return DecisionResult(
            decision_id=decision_id,
            generated_at_ms=generated_at,
            expires_at_ms=expires,
            request=request,
            context_refs=dict(context_refs),
            recommendation=recommendation,
            selected_proposal=selected,
            alternatives=alternatives,
            reasons=reasons,
            assumptions=assumptions,
            validation_level=validation_level,
            decision_policy_hash=policy_hash,
            formula_version=formula_version,
        )

    # -- no feasible: classify -------------------------------------------------------
    # Gather all per-candidate reasons for decision-cases.json completeness.
    all_fail: list[str] = []
    all_unknown: list[str] = []
    for t in targets:
        all_fail.extend(candidate_fail.get(str(t), []))
        all_unknown.extend(candidate_unknown.get(str(t), []))
    all_fail.extend([r for r in public_fail if r not in all_fail])
    # Public unknowns already tracked separately; per-candidate unknowns join.
    combined_unknown = list(public_unknown) + [r for r in all_unknown if r not in public_unknown]

    # Priority 1: any unknown public input or candidate unknown.
    if combined_unknown:
        reasons_u = sorted(set(combined_unknown))
        # Spot-only unknowns for h>0 must not mask an otherwise AVOID/MANUAL
        # when h0 was never a candidate (Carry/Balanced): they remain unknown.
        expires_u = _compute_expires(generated_at, quote_valid_sec, context, None)
        return DecisionResult(
            decision_id=decision_id,
            generated_at_ms=generated_at,
            expires_at_ms=expires_u,
            request=request,
            context_refs=dict(context_refs),
            recommendation="DATA_INSUFFICIENT",
            selected_proposal=None,
            alternatives=(),
            reasons=tuple(reasons_u),
            assumptions=(),
            validation_level=validation_level,
            decision_policy_hash=policy_hash,
            formula_version=formula_version,
        )

    # Priority 2: exclusively liquidation/protection pending -> MANUAL_REVIEW.
    liquidation_codes = {"SCENARIO_LIQUIDATION", "LIQUIDATION_CROSSED", "INVALID_AFTER_LIQUIDATION"}
    # Every evaluated candidate must carry a liquidation fail and no other
    # non-liquidation fail; public fails must also be empty.
    has_liquidation = any(r in liquidation_codes or r == "SCENARIO_LIQUIDATION" for r in all_fail)
    non_liq_fails = [r for r in all_fail if r not in liquidation_codes and r != "SCENARIO_LIQUIDATION"]
    # Risk-gate SCENARIO_LIQUIDATION is the only liquidation marker we emit;
    # treat any additional fail as disqualifying MANUAL_REVIEW.
    only_liquidation = has_liquidation and not non_liq_fails and not public_fail
    # Also require at least one candidate actually evaluated (not missing).
    evaluated = [t for t in targets if proposals.get(str(t)) is not None]
    if only_liquidation and evaluated:
        reasons_m = sorted(set([r for r in all_fail if r in liquidation_codes or r == "SCENARIO_LIQUIDATION"] or ["SCENARIO_LIQUIDATION"]))
        expires_m = _compute_expires(generated_at, quote_valid_sec, context, None)
        return DecisionResult(
            decision_id=decision_id,
            generated_at_ms=generated_at,
            expires_at_ms=expires_m,
            request=request,
            context_refs=dict(context_refs),
            recommendation="MANUAL_REVIEW",
            selected_proposal=None,
            alternatives=(),
            reasons=tuple(reasons_m),
            assumptions=("REDUCE_NOTIONAL_OR_INCREASE_MARGIN_OR_MANUAL_RECHECK",),
            validation_level=validation_level,
            decision_policy_hash=policy_hash,
            formula_version=formula_version,
        )
    # If liquidation present but mixed with other fails, AVOID outranks per
    # D07.3 ordering (explicit prohibition / no feasible capital-cost first).
    # Fall through to AVOID.

    # Priority 3: AVOID (explicit prohibition or no capital/cost feasible).
    reasons_a = sorted(set(all_fail or public_fail or ["NO_FEASIBLE_CANDIDATE"]))
    expires_a = _compute_expires(generated_at, quote_valid_sec, context, None)
    return DecisionResult(
        decision_id=decision_id,
        generated_at_ms=generated_at,
        expires_at_ms=expires_a,
        request=request,
        context_refs=dict(context_refs),
        recommendation="AVOID",
        selected_proposal=None,
        alternatives=(),
        reasons=tuple(reasons_a),
        assumptions=(),
        validation_level=validation_level,
        decision_policy_hash=policy_hash,
        formula_version=formula_version,
    )
