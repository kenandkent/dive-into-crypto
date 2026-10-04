"""H04 planner: two-leg planning, costs, stress and manual guidance.

Uses only frozen H01 DTOs (+ B40 config for policy); H02/H03 are parallel
so quote/rule/funding inputs are minimal local doubles shaped like H01.
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


def _rules(venue="BINANCE_SPOT", instrument="BTCUSDT", **over) -> TradingRulesSnapshot:
    base = dict(
        venue=venue,
        instrument_id=instrument,
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
        funding_7d="0.007",
        funding_30d="0.03",
        funding_90d="0.09",
        positive_ratio_30d="0.9",
        positive_ratio_90d="0.8",
        coverage_30d="1.0",
        coverage_90d="1.0",
        conservative_apr=apr,
        history_coverage="0.95",
    )


def _identity(multiplier="1", canonical="bitcoin", confidence="VERIFIED"):
    return {"canonical_id": canonical, "contract_multiplier": multiplier, "identity_confidence": confidence}


def _mark(price="67000", **over):
    base = dict(
        symbol="BTCUSDT",
        mark_price=price,
        quote_currency="USDT",
        quote_to_usd="1",
        as_of_ms=NOW,
        expires_at_ms=NOW + 60_000,
    )
    base.update(over)
    return base


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
        futures_mark=_mark(),
        spot_quote=_spot_quote(),
        futures_rules=_rules(),
        spot_rules=_rules(),
        funding=_funding(),
        identity=_identity(),
        policy=_policy(),
        now_ms=NOW,
    )
    kw.update(over)
    return simulate_hedge(request, **kw)


# ---------------------------------------------------------------- ABSOLUTE ---


def test_absolute_h_is_one():
    from diveintocrypto_desktop.shortlab.hedge.planner import compute_target_hedge_ratio

    assert compute_target_hedge_ratio(_req(mode="ABSOLUTE")) == Decimal("1")


def test_absolute_1000_coin_multiplier_canonical():
    # B44.1 shape: futures qty 100 x multiplier 1000 -> 100000 canonical.
    req = HedgeSimulationRequest(
        symbol="1000PEPEUSDT",
        mode="ABSOLUTE",
        futures_notional_usd="1000",
        preferred_spot_venue="AUTO",
        futures_leverage="1",
    )
    quote = _spot_quote(canonical_id="pepe", symbol="PEPEUSDT", mid_price="0.01", buy_vwap="0.01001",
                        sell_vwap="0.00999", buy_executable_qty="200000", sell_executable_qty="200000",
                        requested_canonical_qty="100000")
    frules = _rules(instrument="1000PEPEUSDT", lot_rules={"step_size": "1", "min_qty": "1", "max_qty": "1000000"},
                    price_rules={"tick_size": "0.00001", "min_price": "0.00001", "max_price": "100"},
                    notional_rules={"min_notional": "5", "max_notional": "1000000"})
    srules = _rules(instrument="PEPEUSDT", lot_rules={"step_size": "1", "min_qty": "1", "max_qty": "10000000"},
                    price_rules={"tick_size": "0.0000001", "min_price": "0.0000001", "max_price": "1"},
                    notional_rules={"min_notional": "5", "max_notional": "1000000"})
    funding = FundingMetrics(symbol="1000PEPEUSDT", conservative_apr="0.365")
    from diveintocrypto_desktop.shortlab.hedge.planner import simulate_hedge

    result = simulate_hedge(
        req,
        futures_mark=_mark(price="10", symbol="1000PEPEUSDT"),
        spot_quote=quote,
        futures_rules=frules,
        spot_rules=srules,
        funding=funding,
        identity=_identity(multiplier="1000", canonical="pepe"),
        policy=_policy(),
        now_ms=NOW,
    )
    assert result.canonical_futures_qty == "100000"
    assert result.target_spot_qty == "100000"
    assert result.target_hedge_ratio == "1"
    # Quote-volume style USD notionals never multiply the multiplier twice:
    # futures notional stays ~1000 (100 x 10), not 1000x larger.
    assert Decimal(result.spot_notional_usd) < Decimal("5000")


# --------------------------------------------------------------- RELATIVE ---


@pytest.mark.parametrize("ratio", ["0.25", "0.5", "0.75"])
def test_relative_25_50_75(ratio):
    req = _req(mode="RELATIVE", hedge_ratio=ratio, stress_up_pct=None, max_directional_loss_usd=None,
               liquidation_price="100000")
    result = _simulate(req)
    assert result.target_hedge_ratio == ratio
    actual = Decimal(result.target_spot_qty) / Decimal(result.canonical_futures_qty)
    assert abs(actual - Decimal(ratio)) < Decimal("0.01")


def test_relative_budget_h_min():
    # N=10000, S=1.0, L=2000 -> h_min=0.8
    req = _req(mode="RELATIVE", hedge_ratio=None, stress_up_pct="1.0", max_directional_loss_usd="2000")
    from diveintocrypto_desktop.shortlab.hedge.planner import compute_target_hedge_ratio

    assert compute_target_hedge_ratio(req) == Decimal("0.8")
    result = _simulate(req)
    assert result.target_hedge_ratio == "0.8"


def test_simultaneous_inputs_422():
    from diveintocrypto_desktop.shortlab.hedge.planner import PlannerInputError, compute_target_hedge_ratio

    req = _req(mode="RELATIVE", hedge_ratio="0.5", stress_up_pct="1.0", max_directional_loss_usd="1000")
    with pytest.raises(PlannerInputError) as exc:
        compute_target_hedge_ratio(req)
    assert "422" in str(exc.value)
    assert exc.value.status_code == 422
    assert exc.value.reason_code == "RELATIVE_INPUTS_MUTUALLY_EXCLUSIVE"


@pytest.mark.parametrize("bad", ["0", "1", "1.5", "-0.2"])
def test_direct_ratio_endpoints_422(bad):
    from diveintocrypto_desktop.shortlab.hedge.planner import PlannerInputError, compute_target_hedge_ratio

    req = _req(mode="RELATIVE", hedge_ratio=bad, stress_up_pct=None, max_directional_loss_usd=None)
    with pytest.raises(PlannerInputError) as exc:
        compute_target_hedge_ratio(req)
    assert "422" in str(exc.value)


def test_budget_endpoints_prompt_mode_change_422():
    from diveintocrypto_desktop.shortlab.hedge.planner import PlannerInputError, compute_target_hedge_ratio

    # h_min >= 1 -> suggest ABSOLUTE (L=0 covers nothing... actually L large? L=0 -> h=1)
    req_abs = _req(mode="RELATIVE", hedge_ratio=None, stress_up_pct="1.0", max_directional_loss_usd="0")
    with pytest.raises(PlannerInputError) as exc:
        compute_target_hedge_ratio(req_abs)
    assert exc.value.reason_code == "RELATIVE_BUDGET_ENDPOINT_ONE_SUGGEST_ABSOLUTE"
    # h_min <= 0 -> suggest no hedge (L huge)
    req_zero = _req(mode="RELATIVE", hedge_ratio=None, stress_up_pct="1.0", max_directional_loss_usd="20000")
    with pytest.raises(PlannerInputError) as exc2:
        compute_target_hedge_ratio(req_zero)
    assert exc2.value.reason_code == "RELATIVE_BUDGET_ENDPOINT_ZERO_SUGGEST_NO_HEDGE"


# ------------------------------------------------------- rules / expiry ---


def test_min_qty_violation_not_ready():
    srules = _rules(lot_rules={"step_size": "0.001", "min_qty": "1000000", "max_qty": "2000000"})
    result = _simulate(_req(), spot_rules=srules)
    assert result.readiness == "NOT_READY"
    assert any("MIN_QTY" in w or "INVALID_LEGAL" in w for w in result.warnings)


def test_price_filter_violation_not_ready():
    srules = _rules(price_rules={"tick_size": "0.10", "min_price": "1000000", "max_price": "2000000"})
    result = _simulate(_req(), spot_rules=srules)
    assert result.readiness == "NOT_READY"


def test_quote_expired_not_ready():
    quote = _spot_quote(expires_at_ms=NOW - 1)
    result = _simulate(_req(), spot_quote=quote)
    assert result.readiness == "NOT_READY"
    assert any("QUOTE_EXPIRED" in w for w in result.warnings)


def test_rounding_drift_over_5_not_ready():
    # Force large flooring drift with a coarse spot step:
    # N=10000 @ price 100 -> futures 100, canonical 100, raw spot 75,
    # step 20 floors to 60 -> actual 0.6 vs target 0.75 (drift 0.15).
    req = HedgeSimulationRequest(symbol="BTCUSDT", mode="RELATIVE", futures_notional_usd="10000",
                                 hedge_ratio="0.75", preferred_spot_venue="AUTO", futures_leverage="1")
    frules = _rules(lot_rules={"step_size": "1", "min_qty": "1", "max_qty": "1000000"},
                    price_rules={"tick_size": "0.10", "min_price": "1", "max_price": "1000000"},
                    notional_rules={"min_notional": "1", "max_notional": "100000000"})
    srules = _rules(lot_rules={"step_size": "20", "min_qty": "1", "max_qty": "1000000"},
                    price_rules={"tick_size": "0.10", "min_price": "1", "max_price": "1000000"},
                    notional_rules={"min_notional": "1", "max_notional": "100000000"})
    quote = _spot_quote(mid_price="100", buy_vwap="100.1", sell_vwap="99.9",
                        buy_executable_qty="1000", sell_executable_qty="1000")
    result = _simulate(req, futures_rules=frules, spot_rules=srules,
                       futures_mark=_mark(price="100"), spot_quote=quote)
    target = Decimal(result.target_hedge_ratio)
    actual = Decimal(result.target_spot_qty) / Decimal(result.canonical_futures_qty)
    drift = abs(actual - target)
    assert drift > Decimal("0.05")
    assert result.readiness == "NOT_READY"
    assert any("ROUNDING_DRIFT_CRITICAL" in w for w in result.warnings)
    # safety drift module must be 0
    assert result.cost_metrics["plan_safety_breakdown"]["drift"] == 0


# ------------------------------------------------------------- costs ---


def test_cost_no_double_count_vwap_once():
    quote = _spot_quote(buy_vwap="67010", mid_price="67000", estimated_fee_usd="5", estimated_gas_usd="2")
    result = _simulate(_req(), spot_quote=quote)
    costs = result.cost_metrics
    # VWAP execution embeds slippage: separate slippage legs stay 0.
    assert costs["entry_slippage_usd"] == "0"
    assert costs["exit_slippage_usd"] == "0"
    assert costs["vwap_note"] == "VWAP_INCLUDED_ONCE"
    assert costs["cost_formula_version"] == "NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V1"
    total = Decimal(costs["round_trip_cost_usd"])
    parts = (Decimal(costs["futures_entry_fee_usd"]) + Decimal(costs["futures_exit_fee_usd"])
             + Decimal(costs["spot_entry_fee_usd"]) + Decimal(costs["spot_exit_fee_usd"])
             + Decimal(costs["entry_gas_usd"]) + Decimal(costs["exit_gas_usd"])
             + Decimal(costs["onchain_entry_buffer_usd"]) + Decimal(costs["onchain_exit_buffer_usd"]))
    assert total == parts
    # quoted fee used once for entry (5), exit uses rate-based fee (not 5 again)
    assert costs["spot_entry_fee_usd"] == "5"
    assert Decimal(costs["spot_exit_fee_usd"]) != Decimal("5") or Decimal(result.spot_notional_usd) * Decimal("0.001") == Decimal("5")
    # gas counted exactly entry+exit (2+2)
    assert Decimal(costs["entry_gas_usd"]) == Decimal("2")
    assert Decimal(costs["exit_gas_usd"]) == Decimal("2")


def test_conservative_funding_nonpositive_break_even_null():
    for apr in ("0", "-0.01"):
        result = _simulate(_req(), funding=_funding(apr=apr))
        assert result.break_even["break_even_days"] is None
        assert result.break_even["estimated_break_even_days"] is None
        assert result.readiness == "NOT_READY"
        assert any("NOT_READY_NO_POSITIVE_CONSERVATIVE_CARRY" in w for w in result.warnings)
    # None conservative also null
    result_none = _simulate(_req(), funding=FundingMetrics(symbol="BTCUSDT", conservative_apr=None))
    assert result_none.break_even["break_even_days"] is None


def test_ready_when_all_verified():
    result = _simulate(_req())
    assert result.readiness == "READY"
    assert result.risk_validation == "VERIFIED"
    assert result.monitoring_capability == "FULL"
    assert result.liquidation_check_status == "VERIFIED"
    assert result.plan_safety_score is not None and result.plan_safety_score >= 80


# ------------------------------------------------- safety / verified ---


def test_no_liq_safety_capped_75_limited():
    req = HedgeSimulationRequest(symbol="BTCUSDT", mode="ABSOLUTE", futures_notional_usd="10000",
                                 preferred_spot_venue="AUTO", futures_leverage="1")
    result = _simulate(req)
    assert result.plan_safety_score is not None and result.plan_safety_score <= 75
    assert result.readiness == "NOT_READY"
    assert result.risk_validation in ("LIMITED", "UNKNOWN")
    assert result.monitoring_capability == "LIMITED"
    assert result.liquidation_check_status in ("LIQ_PRICE_NOT_VERIFIED", "UNKNOWN")


def test_crossed_never_verified():
    req = _req(margin_mode="CROSSED")
    result = _simulate(req)
    assert result.risk_validation != "VERIFIED"
    assert result.monitoring_capability == "LIMITED"
    assert result.readiness == "NOT_READY"


def test_no_platform_stop_never_verified_guidance():
    from diveintocrypto_desktop.shortlab.hedge.planner import build_order_guidance

    req = _req(stop_policy="ALERT_ONLY")
    result = _simulate(req)
    assert result.risk_validation != "VERIFIED"
    guides = build_order_guidance(result, futures_rules=_rules(), spot_rules=_rules(), request=req)
    for guide in guides:
        assert guide.capability != "VERIFIED"
    # open legs still emitted as manual params
    sides = {(g.leg, g.side) for g in guides}
    assert ("FUTURES_SHORT", "SELL") in sides
    assert ("SPOT_LONG", "BUY") in sides


# ---------------------------------------------------------------- stress ---


def test_stress_liquidation_invalid_no_false_return():
    req = _req(mode="ABSOLUTE", liquidation_price="70000",
               liquidation_price_source="USER_EXCHANGE", liquidation_price_updated_at_ms=NOW)
    result = _simulate(req, futures_mark=_mark(price="67000"))
    plus100 = next(s for s in result.stress_scenarios if Decimal(s["move_pct"]) == Decimal("1.0"))
    assert plus100["status"] == "INVALID_AFTER_LIQUIDATION"
    assert plus100["directional_pnl_usd"] is None
    # ABSOLUTE has exactly -50/-25/+25/+50/+100
    moves = sorted(Decimal(s["move_pct"]) for s in result.stress_scenarios)
    assert moves == [Decimal("-0.5"), Decimal("-0.25"), Decimal("0.25"), Decimal("0.5"), Decimal("1")]
    req_rel = _req(mode="RELATIVE", hedge_ratio="0.5", stress_up_pct=None, max_directional_loss_usd=None,
                   liquidation_price="70000")
    result_rel = _simulate(req_rel, futures_mark=_mark(price="67000"))
    moves_rel = sorted(Decimal(s["move_pct"]) for s in result_rel.stress_scenarios)
    assert Decimal("2") in moves_rel
    plus200 = next(s for s in result_rel.stress_scenarios if Decimal(s["move_pct"]) == Decimal("2"))
    assert plus200["status"] == "INVALID_AFTER_LIQUIDATION"
    assert plus200["directional_pnl_usd"] is None


def test_stress_unknown_liq_path_without_price():
    req = HedgeSimulationRequest(symbol="BTCUSDT", mode="ABSOLUTE", futures_notional_usd="10000",
                                 preferred_spot_venue="AUTO", futures_leverage="1")
    result = _simulate(req)
    plus100 = next(s for s in result.stress_scenarios if Decimal(s["move_pct"]) == Decimal("1.0"))
    assert plus100["status"] == "LIQ_PATH_UNKNOWN"


# -------------------------------------------------------------- guidance ---


def test_build_order_guidance_params_tick_aligned():
    from diveintocrypto_desktop.shortlab.hedge.planner import build_order_guidance

    result = _simulate(_req())
    guides = build_order_guidance(result, futures_rules=_rules(), spot_rules=_rules(),
                                  policy=_policy(), request=_req())
    assert len(guides) >= 2
    fut = next(g for g in guides if g.leg == "FUTURES_SHORT" and g.side == "SELL")
    spot = next(g for g in guides if g.leg == "SPOT_LONG")
    assert fut.qty == result.futures_contract_qty
    assert spot.qty == result.target_spot_qty
    assert Decimal(fut.qty) > 0 and Decimal(spot.qty) > 0
    # tick alignment: (price / tick) is integral
    assert (Decimal(fut.limit_price) / Decimal("0.10")) == (Decimal(fut.limit_price) // Decimal("0.10"))
    assert fut.valid_until_ms == result.expires_at_ms
    assert fut.max_slippage_bps == 30.0
    # returns are relative to capital at risk, carried separately
    assert Decimal(result.cost_metrics["capital_at_risk_usd"]) > Decimal(result.futures_notional_usd)
