"""H04 simulator validation: stress path, drift, break-even, safety, verified.

Builds on the planner's pure output; only H01 DTOs are constructed locally.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from diveintocrypto_desktop.shortlab.hedge.models import (
    FundingMetrics,
    HedgeSimulationRequest,
    SpotVenueQuote,
    TradingRulesSnapshot,
)

NOW = 1_760_000_000_000


def _policy():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    return load_shortlab_config()


def _spot_quote(**over) -> SpotVenueQuote:
    base = dict(
        venue="BINANCE_SPOT",
        canonical_id="bitcoin",
        symbol="BTCUSDT",
        chain=None,
        contract_address=None,
        as_of_ms=NOW,
        expires_at_ms=NOW + 60_000,
        reference_notional_usd="10000",
        mid_price="67000",
        buy_vwap="67010",
        sell_vwap="66990",
        buy_executable_qty="5",
        sell_executable_qty="5",
        buy_slippage_bps=5.0,
        sell_slippage_bps=5.0,
        estimated_fee_usd=None,
        estimated_gas_usd=None,
        direction_costs={},
        entry_feasible=True,
        exit_feasible=True,
        exit_feasibility="CONFIRMED",
        quote_currency="USDT",
        quote_to_usd="1",
        source_timestamp_ms=NOW - 1_000,
        fetched_at_ms=NOW,
        requested_canonical_qty="0.15",
        trading_rules={},
        capabilities={},
        identity_confidence="VERIFIED",
        status="OK",
        reason_code=None,
    )
    base.update(over)
    return SpotVenueQuote(**base)


def _rules(**over) -> TradingRulesSnapshot:
    base = dict(
        venue="BINANCE_SPOT",
        instrument_id="BTCUSDT",
        source_as_of_ms=NOW,
        known_at_ms=NOW,
        rule_version="rules-v1",
        raw_filters={},
        order_types={"LIMIT": True, "MARKET": True},
        price_rules={"tick_size": "0.10", "min_price": "1", "max_price": "1000000"},
        lot_rules={"step_size": "0.001", "min_qty": "0.001", "max_qty": "1000"},
        notional_rules={"min_notional": "10", "max_notional": "1000000"},
    )
    for key in ("price_rules", "lot_rules", "notional_rules", "order_types"):
        if key in over:
            merged = dict(base[key])
            merged.update(over.pop(key))
            base[key] = merged
    base.update(over)
    return TradingRulesSnapshot(**base)


def _funding(apr="0.365") -> FundingMetrics:
    return FundingMetrics(
        symbol="BTCUSDT",
        current_rate="0.0001",
        last_settled_rate="0.0001",
        funding_30d="0.03",
        conservative_apr=apr,
        history_coverage="0.95",
    )


def _req(**over) -> HedgeSimulationRequest:
    base = dict(
        symbol="BTCUSDT",
        mode="ABSOLUTE",
        futures_notional_usd="10000",
        preferred_spot_venue="AUTO",
        futures_leverage="1",
        margin_mode="ISOLATED",
        margin_usd="10000",
        liquidation_price="100000",
        liquidation_price_source="USER_EXCHANGE",
        liquidation_price_updated_at_ms=NOW,
        stop_policy="USER_PLATFORM_ORDERS",
        stop_trigger_basis="MARK_PRICE",
        planned_hold_days=30,
    )
    base.update(over)
    return HedgeSimulationRequest(**base)


def _simulate(request, **over):
    from diveintocrypto_desktop.shortlab.hedge.planner import simulate_hedge

    kw = dict(
        futures_mark=dict(symbol="BTCUSDT", mark_price="67000", quote_currency="USDT",
                          quote_to_usd="1", as_of_ms=NOW, expires_at_ms=NOW + 60_000),
        spot_quote=_spot_quote(),
        futures_rules=_rules(),
        spot_rules=_rules(),
        funding=_funding(),
        identity={"canonical_id": "bitcoin", "contract_multiplier": "1", "identity_confidence": "VERIFIED"},
        policy=_policy(),
        now_ms=NOW,
    )
    kw.update(over)
    return simulate_hedge(request, **kw)


def _validate(result, at_ms=NOW):
    from diveintocrypto_desktop.shortlab.hedge.simulator import validate_simulation

    return validate_simulation(result, at_ms)


def test_validate_ready_when_all_verified():
    result = _simulate(_req())
    assert result.readiness == "READY"
    verdict = _validate(result)
    assert verdict.value == "READY"
    assert verdict.reasons == ()
    assert verdict.risk_validation == "VERIFIED"


def test_expired_simulation_not_ready():
    result = _simulate(_req())
    verdict = _validate(result, at_ms=result.expires_at_ms)
    assert verdict.value == "NOT_READY"
    assert "SIMULATION_EXPIRED" in verdict.reasons


def test_rounding_drift_over_5_not_ready():
    import dataclasses

    result = _simulate(_req())
    # Force drift > 5% by shrinking the planned spot qty.
    drifted_qty = str(Decimal(result.target_spot_qty) * Decimal("0.5"))
    drifted = dataclasses.replace(result, target_spot_qty=drifted_qty)
    verdict = _validate(drifted)
    assert verdict.value == "NOT_READY"
    assert "ROUNDING_DRIFT_CRITICAL" in verdict.reasons


def test_conservative_nonpositive_break_even_null_gate():
    result = _simulate(_req(), funding=_funding(apr="0"))
    assert result.break_even["break_even_days"] is None
    verdict = _validate(result)
    assert verdict.value == "NOT_READY"
    assert "NOT_READY_NO_POSITIVE_CONSERVATIVE_CARRY" in verdict.reasons
    # A forged non-null break-even with no carry must also fail.
    import dataclasses

    forged_be = dict(result.break_even)
    forged_be["break_even_days"] = "5"
    forged = dataclasses.replace(result, break_even=forged_be)
    verdict2 = _validate(forged)
    assert "BREAK_EVEN_MUST_BE_NULL_WITHOUT_CARRY" in verdict2.reasons


def test_no_liq_safety_limited_draft_semantics():
    req = HedgeSimulationRequest(symbol="BTCUSDT", mode="ABSOLUTE", futures_notional_usd="10000",
                                 preferred_spot_venue="AUTO", futures_leverage="1")
    result = _simulate(req)
    assert result.plan_safety_score is not None and result.plan_safety_score <= 75
    verdict = _validate(result)
    assert verdict.value == "NOT_READY"
    assert verdict.risk_validation in ("LIMITED", "UNKNOWN")
    assert result.monitoring_capability == "LIMITED"


def test_crossed_or_unsupported_never_verified():
    for extra in (dict(margin_mode="CROSSED"), dict(stop_policy="ALERT_ONLY")):
        result = _simulate(_req(**extra))
        assert result.risk_validation != "VERIFIED"
        verdict = _validate(result)
        assert verdict.value == "NOT_READY"
        assert verdict.risk_validation != "VERIFIED"
    # Forged VERIFIED on a CROSSED-style result must be rejected.
    import dataclasses

    result = _simulate(_req(margin_mode="CROSSED"))
    forged = dataclasses.replace(result, risk_validation="VERIFIED")
    verdict = _validate(forged)
    assert verdict.value == "NOT_READY"
    assert "RISK_VALIDATION_FALSE_VERIFIED" in verdict.reasons


def test_invalid_after_liquidation_has_no_terminal_return():
    result = _simulate(_req(liquidation_price="70000"))
    plus100 = next(s for s in result.stress_scenarios if Decimal(s["move_pct"]) == Decimal("1.0"))
    assert plus100["status"] == "INVALID_AFTER_LIQUIDATION"
    assert plus100["directional_pnl_usd"] is None
    verdict = _validate(result)
    # The gate itself does not invent a terminal return; scenario stays invalid.
    assert plus100["directional_pnl_usd"] is None
    # Forging an OK terminal return over liquidation must fail validation.
    import dataclasses

    forged_scenarios = []
    for scen in result.stress_scenarios:
        if Decimal(scen["move_pct"]) == Decimal("1.0"):
            forged_scenarios.append({**scen, "status": "INVALID_AFTER_LIQUIDATION",
                                    "directional_pnl_usd": "123.45"})
        else:
            forged_scenarios.append(dict(scen))
    forged = dataclasses.replace(result, stress_scenarios=tuple(forged_scenarios))
    verdict2 = _validate(forged)
    assert any("FALSE_TERMINAL_RETURN" in r for r in verdict2.reasons)


def test_safety_overstated_without_liq_rejected():
    import dataclasses

    req = HedgeSimulationRequest(symbol="BTCUSDT", mode="ABSOLUTE", futures_notional_usd="10000",
                                 preferred_spot_venue="AUTO", futures_leverage="1")
    result = _simulate(req)
    forged = dataclasses.replace(result, plan_safety_score=95.0)
    verdict = _validate(forged)
    assert verdict.value == "NOT_READY"
    assert "SAFETY_OVERSTATED_WITHOUT_VERIFIED_LIQ" in verdict.reasons


def test_missing_stress_scenario_rejected():
    import dataclasses

    result = _simulate(_req())
    trimmed = tuple(s for s in result.stress_scenarios if Decimal(s["move_pct"]) != Decimal("1.0"))
    forged = dataclasses.replace(result, stress_scenarios=trimmed)
    verdict = _validate(forged)
    assert any("MISSING_STRESS_SCENARIO" in r for r in verdict.reasons)
