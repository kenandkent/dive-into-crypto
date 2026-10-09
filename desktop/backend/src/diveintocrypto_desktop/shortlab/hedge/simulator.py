"""H04 simulator validation: readiness gate over a finished simulation.

Design: B15 (simulation output), B16.8 (rounding drift), B14 (break-even),
B17 (liquidation path), B18/B48.2 (safety + READY), B20.2/B22.1 (no false
verified stops, CROSSED stays LIMITED).

Pure function: inspects a frozen :class:`HedgeSimulationResult` plus the
validation clock. No network, no plan writes.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.hedge.models import HedgeSimulationResult, Readiness

__all__ = ["SimulationValidationError", "validate_simulation", "validate_goal_mode"]


class SimulationValidationError(ValueError):
    """Raised when the result shape itself is unreadable (not a gate verdict)."""


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError):
            raise SimulationValidationError(f"not a decimal string: {value!r}")
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = Decimal(value)
    else:
        raise SimulationValidationError(f"float/unknown decimal: {value!r}")
    if not parsed.is_finite():
        raise SimulationValidationError(f"non-finite decimal: {value!r}")
    return parsed


def _actual_ratio(result: HedgeSimulationResult) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Return (target, actual, drift) or Nones when quantities are missing."""
    try:
        target = _dec_or_none(result.target_hedge_ratio)
        spot = _dec_or_none(result.target_spot_qty)
        fut = _dec_or_none(result.canonical_futures_qty)
    except SimulationValidationError:
        return None, None, None
    if target is None or spot is None or fut is None or fut == 0:
        return target, None, None
    actual = spot / fut
    return target, actual, abs(actual - target)


def validate_goal_mode(request: Any | None) -> tuple[str | None, list[str]]:
    """Validate repair goal/mode pair (D03.3, pure).

    Returns ``(effective_goal, reasons)``. ``None`` request carries no
    goal verdict (legacy history, read-only). ``ABSOLUTE`` only allows
    ``CARRY_CAPTURE`` (explicit ``BALANCED``/``DIRECTIONAL_SHORT`` is
    ``GOAL_MODE_MISMATCH``); ``RELATIVE`` without goal derives
    ``BALANCED``; explicit ``DIRECTIONAL_SHORT`` is allowed.
    """
    if request is None:
        return None, []
    mode = request.get("mode", getattr(request, "mode", None)) if isinstance(request, Mapping) else getattr(request, "mode", None)
    goal = request.get("goal", getattr(request, "goal", None)) if isinstance(request, Mapping) else getattr(request, "goal", None)
    if mode not in ("ABSOLUTE", "RELATIVE"):
        return None, ["HEDGE_MODE_UNKNOWN"]
    if goal is None:
        effective = "CARRY_CAPTURE" if mode == "ABSOLUTE" else "BALANCED"
        return effective, []
    if goal not in ("CARRY_CAPTURE", "DIRECTIONAL_SHORT", "BALANCED"):
        return None, ["HEDGE_GOAL_INVALID"]
    if mode == "ABSOLUTE" and goal != "CARRY_CAPTURE":
        return goal, ["GOAL_MODE_MISMATCH"]
    return goal, []


def _validate_breakdown(result: HedgeSimulationResult) -> list[str]:
    """Validate repair ``readiness_breakdown``/``economics`` internals (D03.3/D18).

    Checks internal consistency only (gates -> readiness, enum validity);
    a repair ``NOT_READY`` alongside a legacy ``READY`` is allowed: they are
    separate tracks (legacy research vs bound-ports repair). Missing
    breakdown/economics on legacy results is also allowed (LEGACY).
    """
    extra: list[str] = []
    bd = getattr(result, "readiness_breakdown", None)
    if bd is None:
        return extra
    if not isinstance(bd, Mapping):
        return ["BREAKDOWN_INVALID"]
    for key in ("data_complete", "funding_gate", "execution_gate", "economic_gate", "protection_status", "readiness"):
        if key not in bd:
            extra.append("BREAKDOWN_INVALID")
            return extra
    if bd.get("protection_status") not in (
        "CONFIRMED",
        "PENDING",
        "UNSUPPORTED",
        "UNKNOWN",
        "EXPIRED",
        "MANUAL_EXIT_ONLY",
    ):
        extra.append("BREAKDOWN_PROTECTION_INVALID")
    if bd.get("readiness") not in ("READY", "NOT_READY", "BLOCKED"):
        extra.append("BREAKDOWN_READINESS_INVALID")
    # Gate internal consistency: READY requires all three PASS.
    try:
        gates = [bd.get("funding_gate"), bd.get("execution_gate"), bd.get("economic_gate")]
        statuses = []
        for gate in gates:
            if isinstance(gate, Mapping):
                statuses.append(str(gate.get("status", "UNKNOWN")))
            else:
                statuses.append(str(getattr(gate, "status", "UNKNOWN")))
        if bd.get("readiness") == "READY" and not all(s == "PASS" for s in statuses):
            extra.append("BREAKDOWN_READINESS_MISMATCH")
        if bd.get("readiness") not in ("READY", "NOT_READY", "BLOCKED"):
            pass
    except Exception:
        extra.append("BREAKDOWN_INVALID")
    # Economics shape: hold 1..365 or None; unknown stays UNKNOWN, never 0.
    econ = getattr(result, "economics", None)
    if econ is not None:
        if not isinstance(econ, Mapping):
            extra.append("ECONOMICS_INVALID")
        else:
            hold = econ.get("hold_days")
            if hold is not None and (not isinstance(hold, int) or isinstance(hold, bool) or not 1 <= hold <= 365):
                extra.append("HOLD_DAYS_UNKNOWN")
    return extra


def validate_simulation(
    result: HedgeSimulationResult,
    now_ms: int,
    *,
    policy: Any = None,
    request: Any | None = None,
) -> Readiness:
    """Gate a simulation result to a :class:`Readiness` verdict (H04.2).

    Enforces, in order:

    - expiry (``SIMULATION_EXPIRED``);
    - rounding drift ``> 5%`` never READY (``ROUNDING_DRIFT_CRITICAL``);
    - conservative funding ``<= 0`` implies break-even null and NOT_READY
      (``NOT_READY_NO_POSITIVE_CONSERVATIVE_CARRY``);
    - ``+100%`` / ``+200%`` legs crossing liquidation must be
      ``INVALID_AFTER_LIQUIDATION`` with null terminal PnL (no false
      ``OK`` terminal return);
    - no verified liquidation price caps safety at 75 and forces
      ``DRAFT``/``LIMITED`` semantics (NOT_READY + ``LIMITED``/``UNKNOWN``);
    - ``CROSSED`` / missing conditional-order support must never read as
      ``VERIFIED`` (``RISK_VALIDATION_FALSE_VERIFIED``).
    """
    if not isinstance(now_ms, int) or isinstance(now_ms, bool):
        raise SimulationValidationError("now_ms must be an int")
    reasons: list[str] = []

    # 1. Expiry (B32.3: earliest quote expiry / 60s TTL).
    if now_ms >= result.expires_at_ms:
        reasons.append("SIMULATION_EXPIRED")

    # 2. Rounding drift (B16.8: > 5pts never READY).
    _, _, drift = _actual_ratio(result)
    if drift is None:
        reasons.append("RATIO_UNAVAILABLE")
    elif drift > Decimal("0.05"):
        reasons.append("ROUNDING_DRIFT_CRITICAL")

    # 3. Break-even / conservative carry (B14).
    break_even = dict(result.break_even) if isinstance(result.break_even, Mapping) else {}
    be_days_raw = break_even.get("break_even_days", break_even.get("estimated_break_even_days"))
    be_days = _dec_or_none(be_days_raw) if be_days_raw is not None else None
    funding_metrics = dict(result.funding_metrics) if isinstance(result.funding_metrics, Mapping) else {}
    conservative = _dec_or_none(funding_metrics.get("conservative_apr", break_even.get("conservative_apr")))
    if conservative is None or conservative <= 0:
        reasons.append("NOT_READY_NO_POSITIVE_CONSERVATIVE_CARRY")
        # Contract: conservative <= 0 forces break-even null.
        if be_days is not None:
            reasons.append("BREAK_EVEN_MUST_BE_NULL_WITHOUT_CARRY")
    if be_days is None and "NOT_READY_NO_POSITIVE_CONSERVATIVE_CARRY" not in reasons:
        # Break-even unavailable for any other reason also blocks READY.
        reasons.append("BREAK_EVEN_UNAVAILABLE")

    # 4. Stress liquidation path (B17).
    scenarios = tuple(result.stress_scenarios) if result.stress_scenarios else ()
    by_move: dict[str, Mapping[str, Any]] = {}
    for scen in scenarios:
        if not isinstance(scen, Mapping):
            continue
        move = str(scen.get("move_pct", ""))
        by_move[move] = scen
    # Required moves: ABSOLUTE needs +100%; RELATIVE additionally +200%.
    required = {"-0.5", "-0.25", "0.25", "0.5", "1"}
    if result.mode == "RELATIVE":
        required.add("2")
    # Accept both "1.0"/"1" and "2.0"/"2" spellings from the planner.
    normalised = {k.rstrip("0").rstrip(".") if "." in k else k for k in by_move}
    for want in required:
        if want not in normalised and want not in by_move:
            # try decimal-equivalent match
            found = False
            for have in by_move:
                try:
                    if Decimal(have) == Decimal(want):
                        found = True
                        break
                except Exception:
                    continue
            if not found:
                reasons.append(f"MISSING_STRESS_SCENARIO_{want}")
    for move_key in ("1", "2"):
        match: Mapping[str, Any] | None = None
        for have, scen in by_move.items():
            try:
                if Decimal(have) == Decimal(move_key):
                    match = scen
                    break
            except Exception:
                continue
        if match is None:
            continue
        status = str(match.get("status", ""))
        terminal = match.get("directional_pnl_usd", match.get("directionalPnl"))
        if status == "INVALID_AFTER_LIQUIDATION":
            if terminal is not None:
                reasons.append(f"FALSE_TERMINAL_RETURN_AFTER_LIQUIDATION_{move_key}")
        elif status == "OK":
            # OK is allowed only when the path genuinely avoids liquidation;
            # a scenario that claims OK while carrying a liq flag is rejected.
            if match.get("liq_crossed") is True:
                reasons.append(f"STRESS_{move_key}_CROSSES_LIQUIDATION_BUT_OK")

    # 5. Safety / liquidation verification (B18.2/B48.2: no liq => <= 75, DRAFT/LIMITED).
    score = result.plan_safety_score
    if score is None:
        reasons.append("PLAN_SAFETY_UNKNOWN")
    elif float(score) < 80:
        reasons.append("PLAN_SAFETY_BELOW_THRESHOLD")
    liq_status = str(result.liquidation_check_status or "UNKNOWN")
    if liq_status != "VERIFIED" and score is not None and float(score) > 75:
        reasons.append("SAFETY_OVERSTATED_WITHOUT_VERIFIED_LIQ")
    if liq_status in ("UNKNOWN", "LIQ_PRICE_NOT_VERIFIED", "STALE", "ESTIMATED_UNVERIFIED", "CROSSED_LIMITED", "LIQUIDATION_PRICE_STALE"):
        if result.risk_validation == "VERIFIED":
            reasons.append("RISK_VALIDATION_FALSE_VERIFIED")

    # 6. CROSSED / conditional-order support never pseudo-verified (B20.2/B22.1).
    # B48.2: without verified liq / on CROSSED / without platform-stop support
    # the simulation stays NOT_READY (DRAFT/LIMITED semantics) even when the
    # risk label is already correctly LIMITED.
    warn_set = set(tuple(result.warnings or ()))
    for flag in ("CROSSED_MARGIN_LIMITED", "PLATFORM_STOP_UNSUPPORTED", "LIQUIDATION_NOT_VERIFIED"):
        if flag in warn_set and flag not in reasons:
            reasons.append(flag)
    if result.monitoring_capability == "LIMITED" and result.risk_validation != "VERIFIED":
        # LIMITED monitoring alone does not add a new code beyond the flags
        # above, but it must never validate as READY without VERIFIED risk.
        if "RISK_NOT_VERIFIED" not in reasons:
            reasons.append("RISK_NOT_VERIFIED")
    if result.risk_validation == "VERIFIED":
        if liq_status != "VERIFIED":
            if "RISK_VALIDATION_FALSE_VERIFIED" not in reasons:
                reasons.append("RISK_VALIDATION_FALSE_VERIFIED")
        if result.monitoring_capability not in ("FULL",):
            # VERIFIED risk requires full monitoring; LIMITED monitoring with
            # VERIFIED risk is a contradiction (CROSSED / PLATFORM_STOP_UNSUPPORTED).
            if "RISK_VALIDATION_FALSE_VERIFIED" not in reasons:
                reasons.append("RISK_VALIDATION_FALSE_VERIFIED")
    # Explicit LIMITED signals in warnings that contradict VERIFIED risk.
    for flag in ("CROSSED_MARGIN_LIMITED", "PLATFORM_STOP_UNSUPPORTED", "LIQUIDATION_NOT_VERIFIED"):
        if flag in tuple(result.warnings) and result.risk_validation == "VERIFIED":
            if "RISK_VALIDATION_FALSE_VERIFIED" not in reasons:
                reasons.append("RISK_VALIDATION_FALSE_VERIFIED")
            break

    # 7. R06b repair: goal/mode + breakdown/economics internals (D03.3/D18).
    # Legacy results without a request stay read-only (no new verdict).
    _, _goal_reasons = validate_goal_mode(request)
    for reason in _goal_reasons:
        if reason not in reasons:
            reasons.append(reason)
    for reason in _validate_breakdown(result):
        if reason not in reasons:
            reasons.append(reason)

    value = "NOT_READY" if reasons else "READY"
    risk = str(result.risk_validation or "UNKNOWN")
    if value == "NOT_READY" and risk == "VERIFIED":
        risk = "LIMITED"
    if risk not in ("VERIFIED", "LIMITED", "UNKNOWN"):
        risk = "UNKNOWN"
    return Readiness(value=value, reasons=tuple(reasons), risk_validation=risk)  # type: ignore[arg-type]
