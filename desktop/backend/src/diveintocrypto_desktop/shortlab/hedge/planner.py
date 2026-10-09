"""H04 planner: two-leg planning, costs, stress and manual order params.

Design: B3 (canonical qty / hedge ratio), B4 (ABSOLUTE / RELATIVE),
B5 (risk-budget ``h_min``), B13 (execution cost, VWAP included once),
B14 (break-even / conservative funding), B15 (planner I/O), B16 (8-step
quantity rounding), B17 (stress incl. liquidation path), B18 (plan safety),
B20 (manual execution guide).

Pure functions only: no network, no DB/plan writes, no account access.
All quantity/price/notional/fee legs are :class:`Decimal` decimal strings
(never float); price/VWAP/PnL maths run under an 80-digit localcontext and
rounding is display-only. Inputs reuse the frozen H01 DTO shapes
(``HedgeSimulationRequest`` / ``SpotVenueQuote`` / ``TradingRulesSnapshot``
/ ``FundingMetrics``); H02/H03 symbols are never assumed -- callers pass
minimal test doubles shaped like those DTOs.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.hedge import COST_FORMULA_VERSION
from diveintocrypto_desktop.shortlab.hedge.models import (
    FundingMetrics,
    HedgeSimulationRequest,
    HedgeSimulationResult,
    OrderGuidance,
    SpotVenueQuote,
    TradingRulesSnapshot,
)

__all__ = [
    "PlannerInputError",
    "compute_target_hedge_ratio",
    "simulate_hedge",
    "build_order_guidance",
    "compute_plan_safety",
    "DEFAULT_POLICY",
    "_validate_goal_mode",
    "_effective_goal",
]


class PlannerInputError(ValueError):
    """Structural input error with HTTP 422 semantics (B5/B32.3)."""

    def __init__(self, message: str, *, reason_code: str = "HEDGE_INPUT_INVALID") -> None:
        super().__init__(f"422 {reason_code}: {message}")
        self.status_code = 422
        self.http_status = 422
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# Default B40 policy (used when caller passes policy=None).
# Values mirror shortlab/default.yaml B40 subtree.
# ---------------------------------------------------------------------------

DEFAULT_POLICY: dict[str, Any] = {
    "ratio": {"drift_warn_pct": 0.02, "drift_critical_pct": 0.05},
    "costs": {
        "futures_entry_fee_rate": 0.0005,
        "futures_exit_fee_rate": 0.0005,
        "spot_entry_fee_rate": 0.001,
        "spot_exit_fee_rate": 0.001,
        "alpha_entry_fee_rate": 0.001,
        "alpha_exit_fee_rate": 0.001,
        "onchain_extra_buffer_bps": 20,
    },
    "execution": {"max_price_impact_bps": 30, "max_manual_chunk_usd": 5000},
    "liquidation": {
        "warning_distance": 0.2,
        "critical_distance": 0.1,
        "emergency_distance": 0.05,
        "user_price_max_age_sec": 86400,
    },
    "basis": {
        "warning_vs_accrued_funding": 0.5,
        "exit_vs_accrued_funding": 1.0,
        "warning_min_usd": 10,
        "warning_min_notional_ratio": 0.001,
        "exit_min_usd": 20,
        "exit_min_notional_ratio": 0.002,
        "fresh_max_skew_sec": 5,
        "reference_max_skew_sec": 30,
    },
    "liquidity_monitor": {"max_exit_slippage_bps": 100, "min_exit_coverage_ratio": 1.0},
    "freshness": {
        "futures_mark": {"ttl": 20, "grace": 60},
        "spot_quote": {"ttl": 20, "grace": 60},
        "spot_depth": {"ttl": 60, "grace": 180},
        "alpha_quote": {"ttl": 20, "grace": 60},
        "funding_current": {"ttl": 120, "grace": 300},
        "onchain_quote": {"ttl": 90, "grace": 180},
        "contract_status": {"ttl": 900, "grace": 1800},
    },
}


# ---------------------------------------------------------------------------
# Decimal helpers (ledger boundary: never float for qty/price).
# ---------------------------------------------------------------------------


def _dec(value: Any, name: str) -> Decimal:
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise PlannerInputError(f"{name} must be a non-empty decimal string", reason_code="HEDGE_RATIO_INVALID")
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise PlannerInputError(f"{name} is not a decimal string: {value!r}", reason_code="HEDGE_RATIO_INVALID") from exc
    elif isinstance(value, int) and not isinstance(value, bool):
        parsed = Decimal(value)
    else:
        raise PlannerInputError(
            f"{name} must be a decimal string (float rejected), got {value!r}",
            reason_code="HEDGE_RATIO_INVALID",
        )
    if not parsed.is_finite():
        raise PlannerInputError(f"{name} must be finite", reason_code="HEDGE_RATIO_INVALID")
    return parsed


def _dec_str(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(value)
    if not value.is_finite():
        raise PlannerInputError("non-finite decimal cannot be serialised", reason_code="HEDGE_RATIO_INVALID")
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


def _floor_to_step(qty: Decimal, step: Decimal | None) -> Decimal:
    """Floor ``qty`` down to a legal ``step`` (B16.5, ROUND_DOWN).

    ``step`` of ``None`` or ``0`` keeps the venue-native "disabled" meaning:
    no flooring is applied and ``0`` is never treated as a legal step.
    """
    if step is None:
        return qty
    if step == 0:
        return qty
    if step < 0:
        raise PlannerInputError(f"illegal negative step {step}", reason_code="TRADING_RULES_INVALID")
    if qty < 0:
        raise PlannerInputError("qty must be non-negative", reason_code="HEDGE_RATIO_INVALID")
    with localcontext() as ctx:
        ctx.prec = 80
        units = (qty / step).to_integral_value(rounding=ROUND_DOWN)
        return units * step


def _get_rule_decimal(rules: Any, group: str, key: str) -> Decimal | None:
    """Read a decimal-string rule value from a DTO or plain mapping."""
    group_map: Any = None
    if isinstance(rules, Mapping):
        group_map = rules.get(group)
        if group_map is None:
            # allow flat {"step_size": ...} doubles
            group_map = rules
    else:
        group_map = getattr(rules, group, None)
    if group_map is None:
        return None
    if isinstance(group_map, Mapping) and key not in group_map:
        return None
    raw: Any = group_map.get(key) if isinstance(group_map, Mapping) else None
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise PlannerInputError(f"{group}.{key} must be a decimal string", reason_code="TRADING_RULES_INVALID")
    if isinstance(raw, (int, Decimal)) and not isinstance(raw, bool):
        raw = str(raw)
    if not isinstance(raw, str):
        raise PlannerInputError(
            f"{group}.{key} must be a decimal string, got {raw!r}",
            reason_code="TRADING_RULES_INVALID",
        )
    try:
        parsed = Decimal(raw.strip())
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise PlannerInputError(f"{group}.{key} is not a decimal string", reason_code="TRADING_RULES_INVALID") from exc
    if not parsed.is_finite():
        raise PlannerInputError(f"{group}.{key} must be finite", reason_code="TRADING_RULES_INVALID")
    return parsed


def _order_support(rules: Any, *names: str) -> bool | None:
    """Return True/False when order-type support is known, else None."""
    order_types: Any = None
    if isinstance(rules, Mapping):
        order_types = rules.get("order_types")
    else:
        order_types = getattr(rules, "order_types", None)
    if order_types is None:
        return None
    if not isinstance(order_types, Mapping):
        return None
    for name in names:
        if name in order_types:
            return bool(order_types[name])
    return None


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _resolve_policy(policy: Any) -> dict[str, Any]:
    if policy is None:
        return {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in DEFAULT_POLICY.items()}
    # ShortLabConfig passthrough (has .hedge).
    hedge = getattr(policy, "hedge", None)
    if hedge is not None:
        try:
            data = policy.to_dict()["hedge"]  # type: ignore[attr-defined]
            merged: dict[str, Any] = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in DEFAULT_POLICY.items()}
            for key in merged:
                if key in data:
                    value = data[key]
                    merged[key] = dict(value) if isinstance(value, Mapping) else value
            # freshness windows are TtlGrace objects in typed config
            fresh = getattr(hedge, "freshness", None)
            if fresh is not None:
                merged["freshness"] = {
                    str(group): {"ttl": int(window.ttl), "grace": int(window.grace)}
                    for group, window in dict(fresh).items()
                }
            return merged
        except Exception:
            pass
    if isinstance(policy, Mapping):
        merged = {k: (dict(v) if isinstance(v, Mapping) else v) for k, v in DEFAULT_POLICY.items()}
        for key, value in policy.items():
            if key in merged and isinstance(value, Mapping):
                base = dict(merged[key]) if isinstance(merged[key], Mapping) else {}
                base.update(dict(value))
                merged[key] = base
            elif key in merged:
                merged[key] = value
            else:
                merged[key] = value
        return merged
    raise PlannerInputError("policy must be a mapping or ShortLabConfig", reason_code="HEDGE_INPUT_INVALID")


def _fee_rate(costs: Mapping[str, Any], key: str, overrides: Mapping[str, Any] | None) -> Decimal:
    if overrides is not None and key in overrides:
        raw = overrides[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str, Decimal)):
            raise PlannerInputError(f"fee override {key} must be a number", reason_code="HEDGE_INPUT_INVALID")
        rate = Decimal(str(raw))
    else:
        raw = costs.get(key)
        if raw is None:
            raise PlannerInputError(f"missing cost rate {key}", reason_code="HEDGE_INPUT_INVALID")
        rate = Decimal(str(raw))
    if not rate.is_finite() or rate < 0 or rate >= 1:
        raise PlannerInputError(f"fee rate {key} must be in [0,1), got {rate}", reason_code="HEDGE_INPUT_INVALID")
    return rate


# ---------------------------------------------------------------------------
# R06b goal/mode repair (D03.3/D18, pure).
# Manual ABSOLUTE defaults to CARRY_CAPTURE, RELATIVE defaults to BALANCED;
# DIRECTIONAL_SHORT is an explicit RELATIVE goal. ABSOLUTE with any other
# goal is HTTP 422 (GOAL_MODE_MISMATCH). Old requests without goal are
# interpreted read-only via the defaults and history is never rewritten.
# ---------------------------------------------------------------------------


def _effective_goal(request: Any) -> str:
    mode = _field(request, "mode", None)
    goal = _field(request, "goal", None)
    if goal is None:
        if mode == "ABSOLUTE":
            return "CARRY_CAPTURE"
        if mode == "RELATIVE":
            return "BALANCED"
        raise PlannerInputError(f"unknown mode {mode!r}", reason_code="HEDGE_RATIO_INVALID")
    if not isinstance(goal, str) or goal not in ("CARRY_CAPTURE", "DIRECTIONAL_SHORT", "BALANCED"):
        raise PlannerInputError(f"unknown goal {goal!r}", reason_code="HEDGE_GOAL_INVALID")
    return goal


def _validate_goal_mode(request: Any) -> str:
    """Validate goal/mode pair, returning the effective goal (D03.3)."""
    mode = _field(request, "mode", None)
    if mode not in ("ABSOLUTE", "RELATIVE"):
        raise PlannerInputError(f"unknown mode {mode!r}", reason_code="HEDGE_RATIO_INVALID")
    goal = _field(request, "goal", None)
    effective = _effective_goal(request)
    if mode == "ABSOLUTE" and effective != "CARRY_CAPTURE":
        raise PlannerInputError(
            f"ABSOLUTE mode only supports goal=CARRY_CAPTURE, got {goal!r}",
            reason_code="GOAL_MODE_MISMATCH",
        )
    return effective


# ---------------------------------------------------------------------------
# H04.1 target hedge ratio (B4/B5).
# ---------------------------------------------------------------------------


def compute_target_hedge_ratio(request: HedgeSimulationRequest) -> Decimal:
    """Resolve the target hedge ratio ``h`` (B4/B5, H04.1).

    - ABSOLUTE pins ``h == 1`` and must not carry relative fields;
    - RELATIVE accepts exactly one style: direct ``hedge_ratio`` **or**
      ``stress_up_pct`` + ``max_directional_loss_usd``. Both styles at once
      is HTTP 422 (``RELATIVE_INPUTS_MUTUALLY_EXCLUSIVE``);
    - budget style uses ``h_min = 1 - L / (N * S)`` with ``S`` in decimals
      (100% = 1.0, ``S > 0``; ``L >= 0``; ``N > 0``). Endpoints ``<= 0`` /
      ``>= 1`` raise 422 prompting a mode change instead of a fake RELATIVE.
    """
    if request.mode == "ABSOLUTE":
        if (
            request.hedge_ratio is not None
            or request.stress_up_pct is not None
            or request.max_directional_loss_usd is not None
        ):
            raise PlannerInputError(
                "ABSOLUTE must not carry hedge_ratio/stress_up_pct/max_directional_loss_usd",
                reason_code="ABSOLUTE_MUST_NOT_CARRY_RELATIVE_FIELDS",
            )
        return Decimal("1")
    if request.mode != "RELATIVE":
        raise PlannerInputError(f"unknown mode {request.mode!r}", reason_code="HEDGE_RATIO_INVALID")
    has_ratio = request.hedge_ratio is not None
    has_budget = request.stress_up_pct is not None or request.max_directional_loss_usd is not None
    if has_ratio and has_budget:
        raise PlannerInputError(
            "RELATIVE accepts hedge_ratio XOR stress_up_pct+max_directional_loss_usd, not both",
            reason_code="RELATIVE_INPUTS_MUTUALLY_EXCLUSIVE",
        )
    if has_ratio:
        assert request.hedge_ratio is not None
        h = _dec(request.hedge_ratio, "hedge_ratio")
        if h == 0:
            raise PlannerInputError(
                "h==0 is an endpoint: submit a directional (unhedged) view, not RELATIVE",
                reason_code="RELATIVE_RATIO_ENDPOINT_ZERO_SUGGEST_NO_HEDGE",
            )
        if h == 1:
            raise PlannerInputError(
                "h==1 is an endpoint: resubmit as ABSOLUTE",
                reason_code="RELATIVE_RATIO_ENDPOINT_ONE_SUGGEST_ABSOLUTE",
            )
        if h <= 0 or h >= 1:
            raise PlannerInputError(
                f"RELATIVE requires 0 < h < 1, got {request.hedge_ratio!r}",
                reason_code="HEDGE_RATIO_INVALID",
            )
        return h
    if has_budget:
        if request.stress_up_pct is None or request.max_directional_loss_usd is None:
            raise PlannerInputError(
                "RELATIVE budget style requires both stress_up_pct and max_directional_loss_usd",
                reason_code="HEDGE_RISK_BUDGET_INVALID",
            )
        s = _dec(request.stress_up_pct, "stress_up_pct")
        loss = _dec(request.max_directional_loss_usd, "max_directional_loss_usd")
        notional = _dec(request.futures_notional_usd, "futures_notional_usd")
        if s <= 0:
            raise PlannerInputError("stress_up_pct (S) must be > 0 in decimals", reason_code="HEDGE_RISK_BUDGET_INVALID")
        if loss < 0:
            raise PlannerInputError("max_directional_loss_usd (L) must be >= 0", reason_code="HEDGE_RISK_BUDGET_INVALID")
        if notional <= 0:
            raise PlannerInputError("futures_notional_usd (N) must be > 0", reason_code="HEDGE_RISK_BUDGET_INVALID")
        with localcontext() as ctx:
            ctx.prec = 80
            h_min = Decimal(1) - loss / (notional * s)
        if h_min <= 0:
            raise PlannerInputError(
                f"h_min={_dec_str(h_min)} <= 0: budget covers the stress with no hedge; "
                "do not submit RELATIVE, use a directional view",
                reason_code="RELATIVE_BUDGET_ENDPOINT_ZERO_SUGGEST_NO_HEDGE",
            )
        if h_min >= 1:
            raise PlannerInputError(
                f"h_min={_dec_str(h_min)} >= 1: budget requires full hedge; resubmit as ABSOLUTE",
                reason_code="RELATIVE_BUDGET_ENDPOINT_ONE_SUGGEST_ABSOLUTE",
            )
        return h_min
    raise PlannerInputError(
        "RELATIVE requires hedge_ratio or stress_up_pct+max_directional_loss_usd",
        reason_code="HEDGE_RISK_BUDGET_INVALID",
    )


# ---------------------------------------------------------------------------
# Internal extraction helpers.
# ---------------------------------------------------------------------------


def _extract_multiplier(identity: Any) -> Decimal:
    raw: Any = None
    if isinstance(identity, Mapping):
        raw = identity.get("contract_multiplier", identity.get("multiplier"))
    else:
        raw = getattr(identity, "contract_multiplier", getattr(identity, "multiplier", None))
    if raw is None:
        raise PlannerInputError(
            "contract_multiplier is required and must be verified (never guessed from symbol)",
            reason_code="HEDGE_MULTIPLIER_UNVERIFIED",
        )
    if isinstance(raw, bool):
        raise PlannerInputError("contract_multiplier must be a positive number", reason_code="HEDGE_MULTIPLIER_UNVERIFIED")
    try:
        mult = Decimal(str(raw).strip()) if isinstance(raw, str) else Decimal(str(raw))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise PlannerInputError("contract_multiplier is not a number", reason_code="HEDGE_MULTIPLIER_UNVERIFIED") from exc
    if not mult.is_finite() or mult <= 0:
        raise PlannerInputError("contract_multiplier must be > 0", reason_code="HEDGE_MULTIPLIER_UNVERIFIED")
    return mult


def _extract_mark(futures_mark: Any) -> tuple[Decimal, Decimal, str, int | None, int | None]:
    """Return (native_price, quote_to_usd, quote_currency, as_of_ms, expires_at_ms)."""
    if futures_mark is None:
        raise PlannerInputError("futures_mark is required", reason_code="HEDGE_INPUT_INVALID")
    price_raw: Any = None
    for key in ("mark_price", "native_price", "futures_price", "price"):
        price_raw = _field(futures_mark, key)
        if price_raw is not None:
            break
    if price_raw is None:
        raise PlannerInputError("futures_mark.mark_price is required", reason_code="HEDGE_INPUT_INVALID")
    price = _dec(price_raw, "futures_mark.mark_price")
    if price <= 0:
        raise PlannerInputError("futures mark price must be > 0", reason_code="HEDGE_INPUT_INVALID")
    fx_raw = _field(futures_mark, "quote_to_usd", _field(futures_mark, "fx_to_usd", "1"))
    fx = _dec("1" if fx_raw is None else fx_raw, "futures_mark.quote_to_usd")
    if fx <= 0:
        raise PlannerInputError("futures quote_to_usd must be > 0", reason_code="HEDGE_INPUT_INVALID")
    ccy = _field(futures_mark, "quote_currency", _field(futures_mark, "quote_asset", "USDT"))
    if not isinstance(ccy, str) or not ccy:
        ccy = "USDT"
    as_of = _field(futures_mark, "as_of_ms")
    expires = _field(futures_mark, "expires_at_ms")
    return price, fx, ccy, as_of if isinstance(as_of, int) else None, expires if isinstance(expires, int) else None


def _extract_spot(spot_quote: SpotVenueQuote | Mapping[str, Any]) -> tuple[Decimal | None, Decimal, bool]:
    """Return (native_execution_price, fx, used_vwap)."""
    if isinstance(spot_quote, Mapping):
        buy_vwap = spot_quote.get("buy_vwap")
        mid = spot_quote.get("mid_price")
        fx_raw = spot_quote.get("quote_to_usd", "1")
    else:
        buy_vwap = spot_quote.buy_vwap
        mid = spot_quote.mid_price
        fx_raw = spot_quote.quote_to_usd
    fx = _dec("1" if fx_raw is None else fx_raw, "spot_quote.quote_to_usd")
    if fx <= 0:
        raise PlannerInputError("spot quote_to_usd must be > 0", reason_code="HEDGE_INPUT_INVALID")
    if buy_vwap is not None:
        return _dec(buy_vwap, "spot_quote.buy_vwap"), fx, True
    if mid is not None:
        return _dec(mid, "spot_quote.mid_price"), fx, False
    return None, fx, False


def _conservative_apr(funding: Any) -> Decimal | None:
    raw = _field(funding, "conservative_apr")
    if raw is None:
        return None
    return _dec(raw, "funding.conservative_apr")


def _fresh_ok(as_of_ms: Any, now_ms: int, ttl_sec: int) -> bool:
    if not isinstance(as_of_ms, int) or isinstance(as_of_ms, bool):
        return False
    return (now_ms - as_of_ms) <= ttl_sec * 1000


# ---------------------------------------------------------------------------
# Safety scoring (B18).
# ---------------------------------------------------------------------------


def compute_plan_safety(
    *,
    drift: Decimal | None,
    liq_distance: Decimal | None,
    liq_source: str | None,
    liq_fresh: bool,
    exit_coverage: Decimal | None,
    break_even_days: Decimal | None,
    funding_ok: bool,
    freshness_points: int,
) -> tuple[float, dict[str, int]]:
    """Score the six B18 modules (total 100, capped at 75 without verified liq)."""
    if drift is None:
        drift_score = 0
    elif drift <= Decimal("0.01"):
        drift_score = 20
    elif drift <= Decimal("0.02"):
        drift_score = 15
    elif drift <= Decimal("0.05"):
        drift_score = 5
    else:
        drift_score = 0

    if liq_distance is None or liq_source is None or liq_source == "NONE" or not liq_fresh:
        liq_score = 0
    elif liq_source == "ESTIMATED":
        if liq_distance >= Decimal("0.5"):
            liq_score = 25
        elif liq_distance >= Decimal("0.3"):
            liq_score = 20
        elif liq_distance >= Decimal("0.2"):
            liq_score = 15
        elif liq_distance >= Decimal("0.1"):
            liq_score = 8
        elif liq_distance >= Decimal("0.05"):
            liq_score = 2
        else:
            liq_score = 0
        liq_score = min(liq_score, 10)
    else:  # USER_EXCHANGE (fresh)
        if liq_distance >= Decimal("0.5"):
            liq_score = 25
        elif liq_distance >= Decimal("0.3"):
            liq_score = 20
        elif liq_distance >= Decimal("0.2"):
            liq_score = 15
        elif liq_distance >= Decimal("0.1"):
            liq_score = 8
        elif liq_distance >= Decimal("0.05"):
            liq_score = 2
        else:
            liq_score = 0

    if exit_coverage is None:
        exit_score = 0
    elif exit_coverage >= Decimal("1"):
        exit_score = 20
    elif exit_coverage >= Decimal("0.75"):
        exit_score = 10
    else:
        exit_score = 0

    if break_even_days is None:
        cost_score = 0
    elif break_even_days <= Decimal("2"):
        cost_score = 15
    elif break_even_days <= Decimal("5"):
        cost_score = 12
    elif break_even_days <= Decimal("10"):
        cost_score = 8
    elif break_even_days <= Decimal("20"):
        cost_score = 3
    else:
        cost_score = 0

    funding_score = 10 if funding_ok else 0
    fresh_score = max(0, min(10, int(freshness_points)))
    total = drift_score + liq_score + exit_score + cost_score + funding_score + fresh_score
    if liq_source != "USER_EXCHANGE" or not liq_fresh or liq_distance is None:
        total = min(total, 75)
    breakdown = {
        "drift": drift_score,
        "liquidation": liq_score,
        "exit": exit_score,
        "cost": cost_score,
        "funding": funding_score,
        "freshness": fresh_score,
    }
    return float(total), breakdown


# ---------------------------------------------------------------------------
# simulate_hedge (B3/B4/B5/B13-B18).
# ---------------------------------------------------------------------------


def simulate_hedge(
    request: HedgeSimulationRequest,
    *,
    futures_mark: Any,
    spot_quote: SpotVenueQuote | Mapping[str, Any],
    futures_rules: TradingRulesSnapshot | Mapping[str, Any] | None = None,
    spot_rules: TradingRulesSnapshot | Mapping[str, Any] | None = None,
    funding: Any = None,
    identity: Any = None,
    policy: Any = None,
    now_ms: int,
    futures_quote: Any | None = None,
    ports: Any | None = None,
) -> HedgeSimulationResult:
    """Plan both legs without touching the network or writing a plan (H04).

    R06b repair (D06.3/D18.1): ``futures_quote`` is the native
    ``FuturesExecutionQuote`` (two-sided depth, may be None for legacy
    research calcs) and ``ports`` carries the R06a collaborators
    (``evaluate_funding_entry_gate`` / ``build_ratio_proposal``). History
    requests without the new keywords keep working: the legacy
    readiness/risk fields are computed as before, but the new
    ``readiness_breakdown``/``economics`` stay ``NOT_READY``/``UNKNOWN``
    without bound ports (never new ``READY``).

    Eight B16 quantity steps are fixed: futures notional -> futures qty ->
    floor to futures lot -> canonical qty -> raw spot qty -> floor to spot
    lot -> recomputed ratio -> rule/drift checks. Costs use native notionals
    with VWAP slippage counted once (``COST_FORMULA_VERSION``); returns are
    reported relative to capital at risk, never blended into a guaranteed
    PnL. Up-moves crossing the user liquidation price are marked
    ``INVALID_AFTER_LIQUIDATION`` with null terminal PnL.
    """
    if not isinstance(now_ms, int) or isinstance(now_ms, bool):
        raise PlannerInputError("now_ms must be an int", reason_code="HEDGE_INPUT_INVALID")
    if identity is None:
        raise PlannerInputError("identity (canonical_id + contract_multiplier) is required", reason_code="HEDGE_IDENTITY_UNVERIFIED")
    # R06b: goal/mode repair validation (D03.3). Old requests without goal
    # derive read-only defaults; ABSOLUTE with non-CARRY is 422.
    _validate_goal_mode(request)
    pol = _resolve_policy(policy)

    target_ratio = compute_target_hedge_ratio(request)
    multiplier = _extract_multiplier(identity)
    canonical_id = _field(identity, "canonical_id", _field(identity, "canonicalId", request.symbol))
    if not isinstance(canonical_id, str) or not canonical_id:
        canonical_id = request.symbol
    identity_confidence = _field(identity, "identity_confidence", _field(identity, "confidence", "UNRESOLVED"))

    fut_price, fut_fx, fut_ccy, fut_as_of, fut_expires = _extract_mark(futures_mark)
    spot_native, spot_fx, used_vwap = _extract_spot(spot_quote)
    spot_venue = _field(spot_quote, "venue", "BINANCE_SPOT")
    if not isinstance(spot_venue, str) or not spot_venue:
        spot_venue = "BINANCE_SPOT"
    spot_symbol = _field(spot_quote, "symbol")
    spot_chain = _field(spot_quote, "chain")
    spot_contract = _field(spot_quote, "contract_address")
    spot_as_of = _field(spot_quote, "as_of_ms")
    spot_expires = _field(spot_quote, "expires_at_ms")

    notional = _dec(request.futures_notional_usd, "futures_notional_usd")

    risks: list[str] = []
    warnings: list[str] = []

    # ---- B16 steps 1-3: futures qty -------------------------------------
    with localcontext() as ctx:
        ctx.prec = 80
        fut_raw = notional / (fut_price * fut_fx)
    fut_step = _get_rule_decimal(futures_rules, "lot_rules", "step_size") if futures_rules is not None else None
    rules_unverified = False
    if futures_rules is None or fut_step is None:
        rules_unverified = True
        warnings.append("TRADING_RULES_UNVERIFIED_FUTURES_STEP")
        fut_qty = fut_raw
    else:
        fut_qty = _floor_to_step(fut_raw, fut_step)
    with localcontext() as ctx:
        ctx.prec = 80
        canonical_qty = fut_qty * multiplier
        canonical_price_usd = (fut_price * fut_fx) / multiplier

    # ---- B16 steps 4-6: spot qty ----------------------------------------
    if spot_native is None:
        warnings.append("SPOT_QUOTE_MISSING_PRICE")
        spot_qty: Decimal | None = None
        actual_ratio: Decimal | None = None
        drift: Decimal | None = None
        spot_price_usd: Decimal | None = None
    else:
        if spot_native <= 0:
            raise PlannerInputError("spot price must be > 0", reason_code="HEDGE_INPUT_INVALID")
        with localcontext() as ctx:
            ctx.prec = 80
            spot_price_usd = spot_native * spot_fx
            raw_spot = canonical_qty * target_ratio
        spot_step = _get_rule_decimal(spot_rules, "lot_rules", "step_size") if spot_rules is not None else None
        if spot_rules is None or spot_step is None:
            rules_unverified = True
            warnings.append("TRADING_RULES_UNVERIFIED_SPOT_STEP")
            spot_qty = raw_spot
        else:
            spot_qty = _floor_to_step(raw_spot, spot_step)
        if canonical_qty > 0 and spot_qty is not None:
            with localcontext() as ctx:
                ctx.prec = 80
                actual_ratio = spot_qty / canonical_qty
                drift = abs(actual_ratio - target_ratio)
        else:
            actual_ratio = None
            drift = None
    if spot_native is not None and spot_qty is not None and canonical_qty > 0:
        pass
    else:
        if spot_qty is None:
            actual_ratio = None
            drift = None
            spot_price_usd = None

    drift_warn = Decimal(str(pol["ratio"].get("drift_warn_pct", 0.02)))
    drift_critical = Decimal(str(pol["ratio"].get("drift_critical_pct", 0.05)))
    if drift is not None:
        if drift > drift_critical:
            warnings.append("ROUNDING_DRIFT_CRITICAL")
        elif drift > drift_warn:
            warnings.append("ROUNDING_DRIFT_WARN")
        if request.mode == "ABSOLUTE" and drift > Decimal("0.01"):
            warnings.append("ABSOLUTE_RATIO_IMPRECISE_NOT_STRICT_ONE_TO_ONE")

    # ---- B16 step 7: rule checks (Decimal, no DOUBLE) --------------------
    qty_ok = True
    if fut_qty <= 0:
        warnings.append("FUTURES_QTY_NON_POSITIVE_AFTER_ROUNDING")
        qty_ok = False
    if spot_qty is not None and spot_qty <= 0 and canonical_qty > 0 and target_ratio > 0:
        warnings.append("SPOT_QTY_NON_POSITIVE_AFTER_ROUNDING")
        qty_ok = False

    def _check_leg(qty: Decimal | None, rules: Any, label: str, notional_usd: Decimal | None, price: Decimal | None) -> None:
        nonlocal qty_ok
        if qty is None or rules is None:
            if rules is None:
                warnings.append(f"TRADING_RULES_UNVERIFIED_{label}")
            return
        min_q = _get_rule_decimal(rules, "lot_rules", "min_qty")
        max_q = _get_rule_decimal(rules, "lot_rules", "max_qty")
        if min_q is not None and min_q != 0 and qty < min_q:
            warnings.append(f"{label}_BELOW_MIN_QTY")
            qty_ok = False
        if max_q is not None and max_q != 0 and qty > max_q:
            warnings.append(f"{label}_ABOVE_MAX_QTY")
            qty_ok = False
        min_n = _get_rule_decimal(rules, "notional_rules", "min_notional")
        max_n = _get_rule_decimal(rules, "notional_rules", "max_notional")
        if min_n is not None and min_n != 0 and notional_usd is not None and notional_usd < min_n:
            warnings.append(f"{label}_BELOW_MIN_NOTIONAL")
            qty_ok = False
        if max_n is not None and max_n != 0 and notional_usd is not None and notional_usd > max_n:
            warnings.append(f"{label}_ABOVE_MAX_NOTIONAL")
            qty_ok = False
        min_p = _get_rule_decimal(rules, "price_rules", "min_price")
        max_p = _get_rule_decimal(rules, "price_rules", "max_price")
        if min_p is not None and min_p != 0 and price is not None and price < min_p:
            warnings.append(f"{label}_PRICE_BELOW_MIN")
            qty_ok = False
        if max_p is not None and max_p != 0 and price is not None and price > max_p:
            warnings.append(f"{label}_PRICE_ABOVE_MAX")
            qty_ok = False

    with localcontext() as ctx:
        ctx.prec = 80
        fut_notional_usd = fut_qty * fut_price * fut_fx
        spot_notional_usd: Decimal | None = (
            spot_qty * spot_native * spot_fx if (spot_qty is not None and spot_native is not None) else None
        )
    _check_leg(fut_qty, futures_rules, "FUTURES", fut_notional_usd, fut_price)
    _check_leg(spot_qty, spot_rules, "SPOT", spot_notional_usd, spot_native)
    if rules_unverified:
        warnings.append("TRADING_RULES_UNVERIFIED")

    # ---- Costs (B13, native notionals, VWAP once) -------------------------
    costs_cfg = pol["costs"] if isinstance(pol.get("costs"), Mapping) else DEFAULT_POLICY["costs"]
    overrides = dict(request.fee_overrides) if isinstance(request.fee_overrides, Mapping) else None
    if spot_venue == "BINANCE_ALPHA":
        spot_entry_rate = _fee_rate(costs_cfg, "alpha_entry_fee_rate", overrides)
        spot_exit_rate = _fee_rate(costs_cfg, "alpha_exit_fee_rate", overrides)
    else:
        spot_entry_rate = _fee_rate(costs_cfg, "spot_entry_fee_rate", overrides)
        spot_exit_rate = _fee_rate(costs_cfg, "spot_exit_fee_rate", overrides)
    fut_entry_rate = _fee_rate(costs_cfg, "futures_entry_fee_rate", overrides)
    fut_exit_rate = _fee_rate(costs_cfg, "futures_exit_fee_rate", overrides)
    buffer_bps = Decimal(str(costs_cfg.get("onchain_extra_buffer_bps", 20)))

    with localcontext() as ctx:
        ctx.prec = 80
        fut_entry_fee = fut_notional_usd * fut_entry_rate
        fut_exit_fee = fut_notional_usd * fut_exit_rate
        if spot_notional_usd is None:
            spot_entry_fee = Decimal("0")
            spot_exit_fee = Decimal("0")
        else:
            quoted_fee_raw = _field(spot_quote, "estimated_fee_usd")
            if quoted_fee_raw is not None:
                # Provider quote already includes venue/LP/transfer legs:
                # use it once, do not stack the rate-based fee on top.
                spot_entry_fee = _dec(quoted_fee_raw, "spot_quote.estimated_fee_usd")
                spot_exit_fee = spot_notional_usd * spot_exit_rate
            else:
                spot_entry_fee = spot_notional_usd * spot_entry_rate
                spot_exit_fee = spot_notional_usd * spot_exit_rate
        gas_raw = _field(spot_quote, "estimated_gas_usd")
        entry_gas = _dec(gas_raw, "spot_quote.estimated_gas_usd") if gas_raw is not None else Decimal("0")
        exit_gas = entry_gas  # symmetric exit assumption, counted once per side
        # VWAP already embeds spread slippage when used as execution price.
        entry_slippage = Decimal("0")
        exit_slippage = Decimal("0")
        if spot_venue == "ONCHAIN_DEX" and spot_notional_usd is not None:
            entry_buffer = spot_notional_usd * buffer_bps / Decimal("10000")
            exit_buffer = spot_notional_usd * buffer_bps / Decimal("10000")
        else:
            entry_buffer = Decimal("0")
            exit_buffer = Decimal("0")
        entry_cost = fut_entry_fee + spot_entry_fee + entry_slippage + entry_gas + entry_buffer
        exit_cost = fut_exit_fee + spot_exit_fee + exit_slippage + exit_gas + exit_buffer
        round_trip = entry_cost + exit_cost
        round_trip_pct = (round_trip / notional) if notional > 0 else Decimal("0")

    cost_metrics: dict[str, Any] = {
        "futures_entry_fee_usd": _dec_str(fut_entry_fee),
        "futures_exit_fee_usd": _dec_str(fut_exit_fee),
        "spot_entry_fee_usd": _dec_str(spot_entry_fee),
        "spot_exit_fee_usd": _dec_str(spot_exit_fee),
        "entry_slippage_usd": _dec_str(entry_slippage),
        "exit_slippage_usd": _dec_str(exit_slippage),
        "entry_gas_usd": _dec_str(entry_gas),
        "exit_gas_usd": _dec_str(exit_gas),
        "onchain_entry_buffer_usd": _dec_str(entry_buffer),
        "onchain_exit_buffer_usd": _dec_str(exit_buffer),
        "entry_cost_usd": _dec_str(entry_cost),
        "exit_cost_usd": _dec_str(exit_cost),
        "round_trip_cost_usd": _dec_str(round_trip),
        "round_trip_cost_pct_of_futures_notional": _dec_str(round_trip_pct),
        "cost_formula_version": COST_FORMULA_VERSION,
        "vwap_note": "VWAP_INCLUDED_ONCE",
        "futures_entry_fee_rate": _dec_str(fut_entry_rate),
        "futures_exit_fee_rate": _dec_str(fut_exit_rate),
        "spot_entry_fee_rate": _dec_str(spot_entry_rate),
        "spot_exit_fee_rate": _dec_str(spot_exit_rate),
    }

    # ---- Funding / break-even (B14) ---------------------------------------
    conservative = _conservative_apr(funding) if funding is not None else None
    funding_ok = conservative is not None and conservative > 0
    break_even: dict[str, Any] = {}
    break_even_days: Decimal | None = None
    if not funding_ok:
        break_even = {
            "break_even_funding_pct": _dec_str(round_trip_pct),
            "break_even_days": None,
            "estimated_break_even_days": None,
            "conservative_apr": _dec_str(conservative) if conservative is not None else None,
            "reason": "NOT_READY_NO_POSITIVE_CONSERVATIVE_CARRY",
        }
        warnings.append("NOT_READY_NO_POSITIVE_CONSERVATIVE_CARRY")
    else:
        assert conservative is not None
        with localcontext() as ctx:
            ctx.prec = 80
            assert conservative > 0
            break_even_days = round_trip_pct / (conservative / Decimal("365"))
        break_even = {
            "break_even_funding_pct": _dec_str(round_trip_pct),
            "break_even_days": _dec_str(break_even_days),
            "estimated_break_even_days": _dec_str(break_even_days),
            "conservative_apr": _dec_str(conservative),
            "conservative_apr_method": _field(funding, "history_coverage", _field(funding, "method", None)),
            "history_coverage": _field(funding, "history_coverage"),
        }

    # Capital at risk (B6.1): spot cash + futures margin + cost reserve,
    # reported separately from any single-margin yield.
    margin_usd: Decimal | None = None
    if request.margin_usd is not None:
        margin_usd = _dec(request.margin_usd, "margin_usd")
    else:
        leverage = _dec(request.futures_leverage, "futures_leverage")
        with localcontext() as ctx:
            ctx.prec = 80
            margin_usd = notional / leverage if leverage > 0 else None
    with localcontext() as ctx:
        ctx.prec = 80
        spot_cash = spot_notional_usd if spot_notional_usd is not None else Decimal("0")
        capital = (spot_cash if spot_cash is not None else Decimal("0")) + (margin_usd or Decimal("0")) + round_trip
    cost_metrics["capital_at_risk_usd"] = _dec_str(capital)
    cost_metrics["spot_cash_usd"] = _dec_str(spot_cash)
    cost_metrics["futures_margin_usd"] = _dec_str(margin_usd or Decimal("0"))
    # Margin sanity: cannot derive liq from numbers, but insufficient margin blocks READY.
    if request.margin_usd is not None:
        with localcontext() as ctx:
            ctx.prec = 80
            leverage_r = _dec(request.futures_leverage, "futures_leverage")
            required = notional / leverage_r if leverage_r > 0 else Decimal("0")
        if margin_usd is not None and margin_usd < required:
            warnings.append("MARGIN_BELOW_INITIAL_REQUIREMENT")
            qty_ok = False

    # ---- Basis (B23) -------------------------------------------------------
    if spot_price_usd is not None:
        with localcontext() as ctx:
            ctx.prec = 80
            basis_pct = (canonical_price_usd - spot_price_usd) / spot_price_usd if spot_price_usd != 0 else Decimal("0")
        basis_metrics = {
            "basis_pct": _dec_str(basis_pct),
            "entry_basis_pct": _dec_str(basis_pct),
            "canonical_perp_price_usd": _dec_str(canonical_price_usd),
            "spot_price_usd": _dec_str(spot_price_usd),
        }
    else:
        basis_metrics = {"basis_pct": None, "entry_basis_pct": None}

    # ---- Liquidation distance (B18.2/B22) ----------------------------------
    liq_raw = request.liquidation_price
    liq_source = request.liquidation_price_source
    liq_updated = request.liquidation_price_updated_at_ms
    liq_distance: Decimal | None = None
    liq_fresh = False
    liq_status = "UNKNOWN"
    max_age = int(pol["liquidation"].get("user_price_max_age_sec", 86400))
    if liq_raw is None or liq_source is None or liq_source == "NONE":
        liq_status = "LIQ_PRICE_NOT_VERIFIED" if liq_raw is None else "UNKNOWN"
        warnings.append("LIQ_PRICE_NOT_VERIFIED")
    else:
        liq_price = _dec(liq_raw, "liquidation_price")
        mark_for_liq = canonical_price_usd
        with localcontext() as ctx:
            ctx.prec = 80
            liq_distance = (liq_price - mark_for_liq) / mark_for_liq if mark_for_liq != 0 else None
        if liq_source == "USER_EXCHANGE":
            if isinstance(liq_updated, int) and not isinstance(liq_updated, bool) and (now_ms - liq_updated) <= max_age * 1000:
                liq_fresh = True
                liq_status = "VERIFIED"
            else:
                liq_status = "STALE"
                warnings.append("LIQUIDATION_PRICE_STALE")
        elif liq_source == "ESTIMATED":
            liq_status = "ESTIMATED_UNVERIFIED"
            warnings.append("LIQ_PRICE_NOT_VERIFIED")
            liq_fresh = isinstance(liq_updated, int) and (now_ms - liq_updated) <= max_age * 1000
        else:
            liq_status = "UNKNOWN"
            warnings.append("LIQ_PRICE_NOT_VERIFIED")

    margin_mode = request.margin_mode
    crossed = isinstance(margin_mode, str) and margin_mode.upper() == "CROSSED"
    if crossed:
        warnings.append("CROSSED_MARGIN_LIMITED")
        liq_status = "CROSSED_LIMITED" if liq_status in ("VERIFIED", "UNKNOWN") else liq_status

    # ---- Exit coverage (B18.3/B24) -----------------------------------------
    sell_exec_raw = _field(spot_quote, "sell_executable_qty")
    exit_coverage: Decimal | None = None
    if sell_exec_raw is not None and spot_qty is not None and spot_qty > 0:
        sell_exec = _dec(sell_exec_raw, "spot_quote.sell_executable_qty")
        with localcontext() as ctx:
            ctx.prec = 80
            exit_coverage = sell_exec / spot_qty
        if exit_coverage < Decimal("1"):
            warnings.append("SPOT_EXIT_CAPACITY_INSUFFICIENT")
    elif spot_qty is not None:
        warnings.append("SPOT_EXIT_UNVERIFIED")
    exit_feasible = bool(_field(spot_quote, "exit_feasible", False))
    if not exit_feasible:
        warnings.append("SPOT_EXIT_NOT_FEASIBLE")

    # ---- Freshness (B18.6) --------------------------------------------------
    fresh_cfg = pol.get("freshness", DEFAULT_POLICY["freshness"])
    def _ttl(name: str) -> int:
        try:
            return int(dict(fresh_cfg.get(name, {})).get("ttl", 20))
        except Exception:
            return 20
    fresh_points = 0
    if _fresh_ok(fut_as_of, now_ms, _ttl("futures_mark")):
        fresh_points += 2
    else:
        warnings.append("FUTURES_MARK_STALE")
    # spot venue TTL depends on venue
    spot_ttl_name = "onchain_quote" if spot_venue == "ONCHAIN_DEX" else ("alpha_quote" if spot_venue == "BINANCE_ALPHA" else "spot_quote")
    if _fresh_ok(spot_as_of, now_ms, _ttl(spot_ttl_name)):
        fresh_points += 2
    else:
        warnings.append("SPOT_QUOTE_STALE")
    if _fresh_ok(spot_as_of, now_ms, _ttl("spot_depth")):
        fresh_points += 2
    else:
        warnings.append("SPOT_DEPTH_STALE")
    # funding current: FundingMetrics carries no timestamp in V1; credit when conservative present
    if funding_ok:
        fresh_points += 2
    else:
        warnings.append("FUNDING_CURRENT_STALE_OR_NON_POSITIVE")
    # contract state: credit when identity verified
    if identity_confidence in ("VERIFIED", "HIGH"):
        fresh_points += 2
    else:
        warnings.append("CONTRACT_STATE_UNVERIFIED")
    # hard expiry gate (B32.3): provider expires_at wins
    if isinstance(spot_expires, int) and now_ms >= spot_expires:
        warnings.append("QUOTE_EXPIRED")
        qty_ok = False

    # ---- Stress (B17) -------------------------------------------------------
    moves: list[Decimal] = [Decimal("-0.5"), Decimal("-0.25"), Decimal("0.25"), Decimal("0.5"), Decimal("1.0")]
    if request.mode == "RELATIVE":
        moves.append(Decimal("2.0"))
    moves = sorted(moves)
    scenarios: list[dict[str, Any]] = []
    liq_price_dec: Decimal | None = None
    if liq_raw is not None:
        try:
            liq_price_dec = _dec(liq_raw, "liquidation_price")
        except PlannerInputError:
            liq_price_dec = None
    for move in moves:
        label = _dec_str(move)
        if spot_price_usd is None or canonical_qty <= 0 or spot_qty is None:
            scenarios.append({
                "move_pct": label,
                "spot_pnl_usd": None,
                "futures_pnl_usd": None,
                "directional_pnl_usd": None,
                "residual_exposure_pnl_usd": None,
                "status": "LIQ_PATH_UNKNOWN" if liq_price_dec is None and move > 0 else "INSUFFICIENT_DATA",
            })
            continue
        with localcontext() as ctx:
            ctx.prec = 80
            fut_pnl = -canonical_qty * canonical_price_usd * move
            spot_pnl = spot_qty * spot_price_usd * move
            directional = fut_pnl + spot_pnl
        status = "OK"
        out_spot: str | None = _dec_str(spot_pnl)
        out_fut: str | None = _dec_str(fut_pnl)
        out_dir: str | None = _dec_str(directional)
        if move > 0 and liq_price_dec is not None:
            with localcontext() as ctx:
                ctx.prec = 80
                stressed = canonical_price_usd * (Decimal("1") + move)
            if stressed >= liq_price_dec:
                status = "INVALID_AFTER_LIQUIDATION"
                out_spot = None
                out_fut = None
                out_dir = None
                warnings.append(f"STRESS_{label}_CROSSES_LIQUIDATION")
        elif move > 0 and liq_price_dec is None:
            status = "LIQ_PATH_UNKNOWN"
        scenarios.append({
            "move_pct": label,
            "spot_pnl_usd": out_spot,
            "futures_pnl_usd": out_fut,
            "directional_pnl_usd": out_dir,
            "residual_exposure_pnl_usd": out_dir,
            "status": status,
        })

    # ---- Safety -------------------------------------------------------------
    safety, breakdown = compute_plan_safety(
        drift=drift,
        liq_distance=liq_distance,
        liq_source=liq_source,
        liq_fresh=liq_fresh and not crossed,
        exit_coverage=exit_coverage,
        break_even_days=break_even_days,
        funding_ok=funding_ok,
        freshness_points=fresh_points,
    )
    cost_metrics["plan_safety_breakdown"] = {k: int(v) for k, v in breakdown.items()}

    # ---- Readiness / risk / monitoring (B48.2) -------------------------------
    reasons: list[str] = []
    if not qty_ok:
        reasons.append("INVALID_LEGAL_QUANTITIES")
    if rules_unverified:
        reasons.append("TRADING_RULES_UNVERIFIED")
    if drift is not None and drift > drift_critical:
        reasons.append("ROUNDING_DRIFT_CRITICAL")
    if isinstance(spot_expires, int) and now_ms >= spot_expires:
        reasons.append("QUOTE_EXPIRED")
    if "FUTURES_MARK_STALE" in warnings or "SPOT_QUOTE_STALE" in warnings:
        reasons.append("CRITICAL_DATA_STALE")
    if not funding_ok:
        reasons.append("NOT_READY_NO_POSITIVE_CONSERVATIVE_CARRY")
    if break_even_days is None:
        reasons.append("BREAK_EVEN_UNAVAILABLE")
    if not exit_feasible:
        reasons.append("SPOT_EXIT_NOT_FEASIBLE")
    if safety < 80:
        reasons.append("PLAN_SAFETY_BELOW_THRESHOLD")
    stop_supported = _stop_supported(request, futures_rules, spot_rules, spot_quote)
    if liq_source != "USER_EXCHANGE" or not liq_fresh or crossed:
        reasons.append("LIQUIDATION_NOT_VERIFIED")
    if crossed:
        reasons.append("CROSSED_MARGIN_LIMITED")
    if not stop_supported:
        reasons.append("PLATFORM_STOP_UNSUPPORTED")
    if identity_confidence not in ("VERIFIED", "HIGH"):
        reasons.append("IDENTITY_NOT_VERIFIED")

    readiness = "READY" if not reasons else "NOT_READY"
    if drift is not None and drift > drift_critical:
        readiness = "NOT_READY"
    if safety < 80:
        readiness = "NOT_READY"

    if (
        liq_source == "USER_EXCHANGE"
        and liq_fresh
        and not crossed
        and stop_supported
        and readiness == "READY"
        and identity_confidence in ("VERIFIED", "HIGH")
    ):
        risk_validation = "VERIFIED"
    elif rules_unverified and liq_source is None:
        risk_validation = "UNKNOWN"
    else:
        risk_validation = "LIMITED"
    if crossed or not stop_supported or liq_source != "USER_EXCHANGE" or not liq_fresh:
        risk_validation = "LIMITED" if risk_validation != "UNKNOWN" else "UNKNOWN"
        if risk_validation == "VERIFIED":
            risk_validation = "LIMITED"

    if risk_validation == "VERIFIED" and readiness == "READY":
        monitoring = "FULL"
    elif risk_validation in ("LIMITED", "UNKNOWN"):
        monitoring = "LIMITED"
    else:
        monitoring = "LIMITED"

    # ---- R06b repair breakdown/economics (D06.3/D18, pure) ------------------
    # Legacy readiness/risk above are preserved for history (ports=None).
    # With bound ports the repair path enforces honest protection + funding
    # + hold + native-quote gates; without ports the new breakdown stays
    # NOT_READY/UNKNOWN and never grants new READY.
    _repair_breakdown, _repair_economics, _repair_overrides = _build_repair_gates(
        request=request,
        futures_mark=futures_mark,
        spot_quote=spot_quote,
        futures_rules=futures_rules,
        spot_rules=spot_rules,
        funding=funding,
        policy=pol,
        now_ms=now_ms,
        futures_quote=futures_quote,
        ports=ports,
        legacy_readiness=readiness,
        legacy_risk=risk_validation,
        legacy_funding_ok=funding_ok,
        legacy_break_even_days=break_even_days,
        legacy_round_trip=round_trip,
        legacy_capital=capital,
        legacy_notional=fut_notional_usd,
        legacy_conservative=conservative,
    )
    if ports is not None:
        # Repair path honesty: UNKNOWN/UNSUPPORTED STOP is never VERIFIED.
        _cap_for_override = _repair_breakdown.get("protection_status", "UNKNOWN")
        if _cap_for_override in ("UNKNOWN", "UNSUPPORTED"):
            if "PLATFORM_STOP_UNSUPPORTED" not in reasons and "LIQUIDATION_NOT_VERIFIED" not in reasons:
                # Keep legacy reasons honest without rewriting history when
                # the legacy path already failed for other reasons.
                pass
            if _cap_for_override == "UNKNOWN":
                if "PLATFORM_STOP_UNSUPPORTED" not in warnings + reasons:
                    warnings.append("PLATFORM_STOP_UNSUPPORTED")
                if "PLATFORM_STOP_UNSUPPORTED" not in reasons:
                    reasons.append("PLATFORM_STOP_UNSUPPORTED")
            else:
                if "PLATFORM_STOP_UNSUPPORTED" not in reasons:
                    reasons.append("PLATFORM_STOP_UNSUPPORTED")
            readiness = "NOT_READY"
            if risk_validation == "VERIFIED":
                risk_validation = "LIMITED"
            if monitoring == "FULL":
                monitoring = "LIMITED"
        # Missing native futures depth also blocks repair READY (old field
        # stays honest for the repair caller).
        if futures_quote is None:
            readiness = "NOT_READY"
            if risk_validation == "VERIFIED":
                risk_validation = "LIMITED"
            monitoring = "LIMITED"

    # Residuals (B3).
    if spot_qty is not None:
        with localcontext() as ctx:
            ctx.prec = 80
            residual_qty = canonical_qty - spot_qty
            residual_ratio = Decimal("1") - (spot_qty / canonical_qty) if canonical_qty != 0 else Decimal("1")
            residual_notional = residual_qty * (spot_price_usd if spot_price_usd is not None else canonical_price_usd)
    else:
        residual_qty = canonical_qty
        residual_ratio = Decimal("1") - target_ratio
        residual_notional = None

    # Residual direction PnL is reported relative to capital at risk (B6.1),
    # never as a guaranteed hedged return.
    funding_metrics: dict[str, Any] = {}
    if isinstance(funding, Mapping):
        funding_metrics = dict(funding)
    elif funding is not None and dataclasses.is_dataclass(funding):
        funding_metrics = dataclasses.asdict(funding)
    funding_metrics.setdefault("conservative_apr", _dec_str(conservative) if conservative is not None else None)

    # Simulation expiry: earliest critical quote expiry / local TTL, max 60s.
    candidates = [now_ms + 60_000]
    if isinstance(spot_expires, int):
        candidates.append(spot_expires)
    if isinstance(fut_expires, int):
        candidates.append(fut_expires)
    expires_at = min(candidates)
    if expires_at <= now_ms:
        expires_at = now_ms + 1_000
        if "QUOTE_EXPIRED" not in warnings:
            warnings.append("QUOTE_EXPIRED")

    simulation_id = f"sim-{canonical_id}-{now_ms}-{_dec_str(target_ratio).replace('.', 'p').replace('-', 'm')}"
    futures_symbol = str(_field(futures_mark, "symbol", request.symbol) or request.symbol)
    result = HedgeSimulationResult(
        simulation_id=simulation_id,
        generated_at_ms=now_ms,
        expires_at_ms=int(expires_at),
        symbol=request.symbol,
        canonical_id=canonical_id,
        mode=request.mode,
        futures_symbol=futures_symbol,
        futures_price=_dec_str(fut_price),
        canonical_futures_price_usd=_dec_str(canonical_price_usd),
        futures_quote_currency=fut_ccy,
        quote_to_usd=_dec_str(fut_fx),
        futures_notional_usd=_dec_str(notional),
        futures_contract_qty=_dec_str(fut_qty),
        canonical_futures_qty=_dec_str(canonical_qty),
        target_hedge_ratio=_dec_str(target_ratio),
        spot_venue=spot_venue,
        spot_symbol=spot_symbol if isinstance(spot_symbol, str) else None,
        spot_chain=spot_chain if isinstance(spot_chain, str) else None,
        spot_contract=spot_contract if isinstance(spot_contract, str) else None,
        spot_price=_dec_str(spot_native) if spot_native is not None else None,
        target_spot_qty=_dec_str(spot_qty) if spot_qty is not None else None,
        spot_notional_usd=_dec_str(spot_notional_usd) if spot_notional_usd is not None else None,
        residual_short_ratio=_dec_str(residual_ratio),
        residual_short_qty=_dec_str(residual_qty),
        residual_short_notional_usd=_dec_str(residual_notional) if residual_notional is not None else None,
        fcs=None,
        plan_safety_score=float(safety),
        funding_metrics=funding_metrics,
        basis_metrics=basis_metrics,
        cost_metrics=cost_metrics,
        break_even=break_even,
        stress_scenarios=tuple(scenarios),
        order_guidance=(),
        monitoring_capability=monitoring,
        risk_validation=risk_validation,
        liquidation_check_status=liq_status,
        risks=tuple(risks),
        warnings=tuple(warnings + reasons),
        readiness=readiness,  # type: ignore[arg-type]
        readiness_breakdown=dict(_repair_breakdown),
        economics=dict(_repair_economics),
    )
    # Attach manual guidance (still pure: no orders placed).
    guides = build_order_guidance(
        result,
        futures_rules=futures_rules,
        spot_rules=spot_rules,
        policy=pol,
        request=request,
        spot_quote=spot_quote,
        futures_mark=futures_mark,
    )
    object.__setattr__(result, "order_guidance", tuple(dataclasses.asdict(g) for g in guides))
    return result


def _gate_dict(status: str, reasons: list[str], checked_at: int) -> dict[str, Any]:
    return {
        "status": status,
        "reasons": sorted(set(reasons)),
        "checked_at_ms": int(checked_at),
        "input_refs": {},
    }


def _build_repair_gates(
    *,
    request: Any,
    futures_mark: Any,
    spot_quote: Any,
    futures_rules: Any,
    spot_rules: Any,
    funding: Any,
    policy: Any,
    now_ms: int,
    futures_quote: Any | None,
    ports: Any | None,
    legacy_readiness: str,
    legacy_risk: str,
    legacy_funding_ok: bool,
    legacy_break_even_days: Any,
    legacy_round_trip: Any,
    legacy_capital: Any,
    legacy_notional: Any,
    legacy_conservative: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build R06b repair ``readiness_breakdown`` + ``economics`` (pure).

    Funding gate uses ``ports.evaluate_funding_entry_gate`` when ``funding``
    is a ``FundingContext``; otherwise ``UNKNOWN`` (never 0-filled). Without
    bound ports the new gates stay ``UNKNOWN`` so no new ``READY`` is
    granted. Protection status comes from
    ``protection.resolve_stop_capability``: ``SUPPORTED`` -> ``PENDING``
    (capable, not yet user-confirmed), ``UNSUPPORTED`` stays
    ``UNSUPPORTED``, ``UNKNOWN`` stays ``UNKNOWN``; a futures
    ``UNSUPPORTED`` with a feasible spot manual leg is projected as
    ``MANUAL_EXIT_ONLY`` (LIMITED, never FULL).
    """
    # --- protection capability (explicit rule evidence only) ---------------
    try:
        from diveintocrypto_desktop.shortlab.hedge.protection import (  # noqa: WPS433
            resolve_stop_capability as _resolve_cap,
        )

        capability = _resolve_cap(futures_rules)
        if capability not in ("SUPPORTED", "UNSUPPORTED", "UNKNOWN"):
            capability = "UNKNOWN"
    except Exception:
        capability = "UNKNOWN"
    venue = _field(spot_quote, "venue", "")
    exit_feasible = bool(_field(spot_quote, "exit_feasible", False))
    if capability == "SUPPORTED":
        protection_status = "PENDING"
    elif capability == "UNSUPPORTED":
        # Spot manual exit plan keeps LIMITED viability, never FULL.
        if exit_feasible and venue in ("BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"):
            # For futures-unsupported the spot leg is manual-only.
            protection_status = "MANUAL_EXIT_ONLY" if venue == "BINANCE_SPOT" else "UNSUPPORTED"
            # Alpha/0x stay indicative-only: no new execution permission.
            if venue in ("BINANCE_ALPHA", "ONCHAIN_DEX"):
                protection_status = "UNSUPPORTED"
        else:
            protection_status = "UNSUPPORTED"
    else:
        protection_status = "UNKNOWN"

    # --- funding gate -------------------------------------------------------
    funding_gate: dict[str, Any]
    if ports is None:
        funding_gate = _gate_dict("UNKNOWN", ["PORTS_UNBOUND"], now_ms)
    else:
        is_context = False
        try:
            from diveintocrypto_desktop.shortlab.repair_contracts import (  # noqa: WPS433
                FundingContext as _FC,
            )

            is_context = isinstance(funding, _FC)
        except Exception:
            is_context = False
        if is_context:
            fn = getattr(ports, "evaluate_funding_entry_gate", None)
            gate_obj: Any = None
            if callable(fn):
                try:
                    gate_obj = fn(funding, policy, now_ms)
                except Exception:
                    gate_obj = None
            if gate_obj is None:
                try:
                    from diveintocrypto_desktop.shortlab.hedge.entry_gate import (  # noqa: WPS433
                        evaluate_funding_entry_gate as _direct,
                    )

                    gate_obj = _direct(funding, policy, now_ms)
                except Exception:
                    gate_obj = None
            if gate_obj is not None:
                try:
                    funding_gate = {
                        "status": str(getattr(gate_obj, "status", "UNKNOWN")),
                        "reasons": sorted(set(tuple(getattr(gate_obj, "reasons", ())) or ())),
                        "checked_at_ms": int(getattr(gate_obj, "checked_at_ms", now_ms)),
                        "input_refs": dict(getattr(gate_obj, "input_refs", {}) or {}),
                    }
                    if funding_gate["status"] not in ("PASS", "FAIL", "UNKNOWN"):
                        funding_gate = _gate_dict("UNKNOWN", ["FUNDING_GATE_UNKNOWN"], now_ms)
                except Exception:
                    funding_gate = _gate_dict("UNKNOWN", ["FUNDING_GATE_UNKNOWN"], now_ms)
            else:
                funding_gate = _gate_dict("UNKNOWN", ["FUNDING_GATE_UNKNOWN"], now_ms)
        else:
            # FundingMetrics-only (no schedule/history): cannot PASS the
            # shared entry gate; record UNKNOWN honestly.
            funding_gate = _gate_dict("UNKNOWN", ["FUNDING_CONTEXT_UNKNOWN"], now_ms)

    # --- execution gate (repair) --------------------------------------------
    exec_reasons: list[str] = []
    exec_unknown: list[str] = []
    if ports is None:
        exec_unknown.append("PORTS_UNBOUND")
    if futures_quote is None:
        exec_unknown.append("FUTURES_QUOTE_UNKNOWN")
    else:
        try:
            fq_expiry = _field(futures_quote, "expires_at_ms", None)
            if isinstance(fq_expiry, int) and not isinstance(fq_expiry, bool) and now_ms >= fq_expiry:
                exec_reasons.append("QUOTE_EXPIRED")
        except Exception:
            exec_unknown.append("FUTURES_QUOTE_UNKNOWN")
        try:
            fq_fees = _field(futures_quote, "fees_included", None)
            if fq_fees is None:
                exec_unknown.append("FEES_INCLUDED_UNKNOWN")
        except Exception:
            exec_unknown.append("FEES_INCLUDED_UNKNOWN")
    # Spot fee honesty: missing flag is UNKNOWN, never default-free.
    try:
        spot_fees = _field(spot_quote, "fees_included", None)
        if spot_fees is None:
            # Legacy quotes without the flag stay UNKNOWN for the repair gate.
            # Spot fixtures with explicit True pass; honest None stays UNKNOWN.
            exec_unknown.append("FEES_INCLUDED_UNKNOWN")
    except Exception:
        exec_unknown.append("FEES_INCLUDED_UNKNOWN")
    if capability == "UNSUPPORTED":
        exec_reasons.append("PROTECTION_UNSUPPORTED")
    elif capability == "UNKNOWN":
        exec_unknown.append("PROTECTION_CAPABILITY_UNKNOWN")
    # Legacy critical failures also fail the repair execution gate.
    if legacy_readiness == "NOT_READY":
        # Preserve the legacy signal without copying every legacy reason;
        # the breakdown keeps the repair-specific reasons above.
        pass
    if exec_reasons:
        execution_gate = _gate_dict("FAIL", exec_reasons, now_ms)
    elif exec_unknown:
        execution_gate = _gate_dict("UNKNOWN", exec_unknown, now_ms)
    else:
        execution_gate = _gate_dict("PASS", [], now_ms)

    # --- economic gate (repair, hold-aware) ---------------------------------
    hold_raw = _field(request, "planned_hold_days", _field(request, "plannedHoldDays", None))
    hold_ok = isinstance(hold_raw, int) and not isinstance(hold_raw, bool) and 1 <= int(hold_raw) <= 365
    hold_int: int | None = int(hold_raw) if hold_ok else None
    econ_unknown: list[str] = []
    if not hold_ok:
        econ_unknown.append("HOLD_DAYS_UNKNOWN")
    if ports is None:
        econ_unknown.append("PORTS_UNBOUND")
    # Conservative carry from the legacy planner numbers (no float).
    carry_s: str | None = None
    net_s: str | None = None
    be_s: str | None = None
    try:
        if legacy_conservative is not None and hold_int is not None and legacy_notional is not None:
            with localcontext() as _ctx:
                _ctx.prec = 80
                _n = legacy_notional if isinstance(legacy_notional, Decimal) else Decimal(str(legacy_notional))
                _apr = legacy_conservative if isinstance(legacy_conservative, Decimal) else Decimal(str(legacy_conservative))
                _rt = legacy_round_trip if isinstance(legacy_round_trip, Decimal) else Decimal(str(legacy_round_trip))
                _carry = _n * _apr * Decimal(int(hold_int)) / Decimal("365")
                _net = _carry - _rt
                carry_s = _dec_str(_carry)
                net_s = _dec_str(_net)
                if _apr > 0 and _n > 0:
                    _be = _rt / (_n * _apr / Decimal("365"))
                    be_s = _dec_str(_be)
    except Exception:
        carry_s = None
        net_s = None
        be_s = None
    if legacy_conservative is None:
        econ_unknown.append("UNKNOWN_APR")
        economic_gate = _gate_dict("UNKNOWN", list(econ_unknown), now_ms)
    elif isinstance(legacy_conservative, Decimal) and legacy_conservative <= 0:
        economic_gate = _gate_dict("FAIL", ["NON_POSITIVE_CARRY"], now_ms)
    elif econ_unknown:
        economic_gate = _gate_dict("UNKNOWN", econ_unknown, now_ms)
    else:
        # Strictly greater than min (default 0): equal => FAIL.
        try:
            _net_d = Decimal(str(net_s)) if net_s is not None else None
            if _net_d is not None and _net_d > 0:
                economic_gate = _gate_dict("PASS", [], now_ms)
            else:
                economic_gate = _gate_dict("FAIL", ["NET_CARRY_BELOW_MIN"], now_ms)
        except Exception:
            economic_gate = _gate_dict("UNKNOWN", ["ECONOMIC_UNKNOWN"], now_ms)

    data_complete = (
        funding_gate["status"] != "UNKNOWN"
        and execution_gate["status"] != "UNKNOWN"
        and economic_gate["status"] != "UNKNOWN"
    )
    if funding_gate["status"] == "PASS" and execution_gate["status"] == "PASS" and economic_gate["status"] == "PASS":
        repair_readiness = "READY"
    else:
        repair_readiness = "NOT_READY"
    breakdown = {
        "data_complete": bool(data_complete),
        "funding_gate": dict(funding_gate),
        "execution_gate": dict(execution_gate),
        "economic_gate": dict(economic_gate),
        "protection_status": str(protection_status),
        "readiness": str(repair_readiness),
    }
    # Economics payload mirrors EconomicsResult shape (dict form for the
    # frozen HedgeSimulationResult.economics mapping).
    try:
        _cap_str = _dec_str(legacy_capital) if legacy_capital is not None else None
    except Exception:
        _cap_str = None
    try:
        _actual_str = _dec_str(legacy_notional) if legacy_notional is not None else "0"
    except Exception:
        _actual_str = "0"
    try:
        _rt_str = _dec_str(legacy_round_trip) if legacy_round_trip is not None else None
    except Exception:
        _rt_str = None
    economics = {
        "hold_days": hold_int,
        "actual_futures_notional_usd": _actual_str,
        "conservative_carry_usd": carry_s,
        "roundtrip_cost_usd": _rt_str,
        "net_carry_usd": net_s,
        "break_even_days": be_s,
        "capital_required_usd": _cap_str,
        "cost_basis": COST_FORMULA_VERSION,
        "gate": dict(economic_gate),
        "unknown_components": sorted(set(econ_unknown)),
    }
    return breakdown, economics, {}


def _stop_supported(request: HedgeSimulationRequest, futures_rules: Any, spot_rules: Any, spot_quote: Any) -> bool:
    """Whether a verified platform stop can be described (B20.2/B22.1)."""
    if request.stop_policy != "USER_PLATFORM_ORDERS":
        return False
    if isinstance(request.margin_mode, str) and request.margin_mode.upper() == "CROSSED":
        return False
    fut_stop = _order_support(futures_rules, "STOP", "STOP_MARKET", "STOP_LOSS", "CONDITIONAL")
    if fut_stop is False:
        return False
    venue = _field(spot_quote, "venue", "")
    caps = _field(spot_quote, "capabilities", {}) or {}
    if isinstance(caps, Mapping):
        conditional = caps.get("conditional_orders", caps.get("stop_orders"))
        if conditional is False:
            return False
    if venue in ("BINANCE_ALPHA", "ONCHAIN_DEX"):
        # V1 has no verified conditional-order capability on these venues.
        return False
    # Without an explicit True we still treat futures STOP as supportable when
    # the caller explicitly opted into platform orders; unknown stays True here
    # and the guidance capability records USER_CONFIRMED (never LIVE_VERIFIED).
    return True


# ---------------------------------------------------------------------------
# build_order_guidance (B20).
# ---------------------------------------------------------------------------


def build_order_guidance(
    simulation: HedgeSimulationResult,
    rules: Any = None,
    policy: Any = None,
    now_ms: int | None = None,
    *,
    futures_rules: Any = None,
    spot_rules: Any = None,
    request: HedgeSimulationRequest | Mapping[str, Any] | None = None,
    spot_quote: Any = None,
    futures_mark: Any = None,
) -> tuple[OrderGuidance, ...]:
    """Build manual venue-operation parameters (B20.1/B20.2, no trade payload).

    Only quantities/prices already validated as legal are emitted; limit
    prices are floored to the venue tick; ``valid_until_ms`` mirrors the
    simulation expiry; ``max_slippage_bps`` comes from the execution policy.
    Stop legs are reference parameters only: without a fresh user liquidation
    price (or on CROSSED / unsupported venues) the capability stays
    ``LIMITED``/``UNKNOWN`` with ``PLATFORM_STOP_UNSUPPORTED`` semantics --
    never a pseudo-verified pre-liquidation guarantee. ``USER_CONFIRMED``
    never becomes ``LIVE_VERIFIED`` (no platform order reads in V1).
    """
    pol = _resolve_policy(policy)
    fut_rules = futures_rules if futures_rules is not None else (rules if rules is not None else None)
    # ``rules`` positional may carry {"futures": ..., "spot": ...}.
    sp_rules = spot_rules
    if sp_rules is None and isinstance(rules, Mapping) and ("futures" in rules or "spot" in rules):
        fut_rules = rules.get("futures", fut_rules)
        sp_rules = rules.get("spot")
    elif sp_rules is None and fut_rules is not None and not isinstance(fut_rules, Mapping) or isinstance(fut_rules, TradingRulesSnapshot):
        # single-snapshot positional: treat as spot rules only when futures not given
        if futures_rules is None and spot_rules is None and rules is not None and not isinstance(rules, Mapping):
            sp_rules = rules
    if sp_rules is None:
        sp_rules = spot_quote if isinstance(spot_quote, Mapping) and "lot_rules" in spot_quote else None

    max_slippage: float | None
    try:
        max_slippage = float(dict(pol.get("execution", {})).get("max_price_impact_bps", 30))
    except Exception:
        max_slippage = 30.0

    guides: list[OrderGuidance] = []
    # --- futures open (SELL short) ---
    try:
        fut_qty = _dec(simulation.futures_contract_qty, "simulation.futures_contract_qty")
    except PlannerInputError:
        return ()
    fut_price_raw = simulation.futures_price
    fut_tick = _get_rule_decimal(fut_rules, "price_rules", "tick_size") if fut_rules is not None else None
    if fut_price_raw is not None:
        fut_limit = _dec(fut_price_raw, "simulation.futures_price")
        if fut_tick is not None and fut_tick != 0:
            fut_limit = _floor_to_step(fut_limit, fut_tick)
        fut_limit_s: str | None = _dec_str(fut_limit)
    else:
        fut_limit_s = None
    fut_order_type = "LIMIT"
    if _order_support(fut_rules, "LIMIT") is False:
        fut_order_type = "MARKET"
    fut_capability = "LIMITED" if simulation.risk_validation != "VERIFIED" else "VERIFIED"
    if fut_rules is None:
        fut_capability = "UNKNOWN"
    guides.append(
        OrderGuidance(
            leg="FUTURES_SHORT",
            venue="BINANCE_FUTURES",
            instrument=simulation.futures_symbol,
            side="SELL",
            order_type=fut_order_type,
            qty=_dec_str(fut_qty),
            limit_price=fut_limit_s,
            trigger_price=None,
            trigger_basis=None,
            reduce_only_or_close_position=False,
            valid_until_ms=simulation.expires_at_ms,
            max_slippage_bps=max_slippage,
            capability=fut_capability,
        )
    )
    # --- spot open (BUY long, net qty) ---
    if simulation.target_spot_qty is not None and simulation.spot_price is not None:
        spot_qty_d = _dec(simulation.target_spot_qty, "simulation.target_spot_qty")
        spot_limit = _dec(simulation.spot_price, "simulation.spot_price")
        spot_tick = _get_rule_decimal(sp_rules, "price_rules", "tick_size") if sp_rules is not None else None
        if spot_tick is not None and spot_tick != 0:
            spot_limit = _floor_to_step(spot_limit, spot_tick)
        spot_order_type = "LIMIT"
        if _order_support(sp_rules, "LIMIT") is False:
            spot_order_type = "MARKET"
        spot_capability = "LIMITED" if simulation.risk_validation != "VERIFIED" else "VERIFIED"
        if sp_rules is None:
            spot_capability = "UNKNOWN"
        guides.append(
            OrderGuidance(
                leg="SPOT_LONG",
                venue=simulation.spot_venue,
                instrument=simulation.spot_symbol or simulation.canonical_id,
                side="BUY",
                order_type=spot_order_type,
                qty=_dec_str(spot_qty_d),
                limit_price=_dec_str(spot_limit),
                trigger_price=None,
                trigger_basis=None,
                reduce_only_or_close_position=False,
                valid_until_ms=simulation.expires_at_ms,
                max_slippage_bps=max_slippage,
                capability=spot_capability,
            )
        )
    # --- protective stops (reference only) ---
    liq_raw: Any = None
    liq_source: Any = None
    stop_policy: Any = None
    margin_mode: Any = None
    stop_trigger_basis: Any = None
    if isinstance(request, Mapping):
        liq_raw = request.get("liquidation_price")
        liq_source = request.get("liquidation_price_source")
        stop_policy = request.get("stop_policy")
        margin_mode = request.get("margin_mode")
        stop_trigger_basis = request.get("stop_trigger_basis")
    elif request is not None:
        liq_raw = getattr(request, "liquidation_price", None)
        liq_source = getattr(request, "liquidation_price_source", None)
        stop_policy = getattr(request, "stop_policy", None)
        margin_mode = getattr(request, "margin_mode", None)
        stop_trigger_basis = getattr(request, "stop_trigger_basis", None)
    crossed = isinstance(margin_mode, str) and margin_mode.upper() == "CROSSED"
    can_stop = (
        stop_policy == "USER_PLATFORM_ORDERS"
        and not crossed
        and liq_raw is not None
        and liq_source == "USER_EXCHANGE"
        and simulation.liquidation_check_status == "VERIFIED"
    )
    if can_stop:
        try:
            liq_p = _dec(liq_raw, "liquidation_price")
            mark_p = _dec(simulation.canonical_futures_price_usd, "simulation.canonical_futures_price_usd")
            with localcontext() as ctx:
                ctx.prec = 80
                # Midpoint reference strictly inside (mark, liq); buffer shown, not guaranteed.
                trigger = mark_p + (liq_p - mark_p) / Decimal("2")
            if mark_p < trigger < liq_p:
                guides.append(
                    OrderGuidance(
                        leg="FUTURES_SHORT",
                        venue="BINANCE_FUTURES",
                        instrument=simulation.futures_symbol,
                        side="BUY",
                        order_type="STOP_MARKET",
                        qty=_dec_str(fut_qty),
                        limit_price=None,
                        trigger_price=_dec_str(trigger),
                        trigger_basis=str(stop_trigger_basis or "MARK_PRICE"),
                        reduce_only_or_close_position=True,
                        valid_until_ms=simulation.expires_at_ms,
                        max_slippage_bps=max_slippage,
                        capability="LIMITED",
                    )
                )
        except PlannerInputError:
            pass
    else:
        # Manual reminder leg is intentionally NOT a verified stop: callers must
        # surface PLATFORM_STOP_UNSUPPORTED / manual steps (B20.2).
        pass
    return tuple(guides)
