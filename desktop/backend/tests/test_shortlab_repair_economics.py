"""R06a units + economics (D06.1/D06.2/D07.2/D18, V06/V07).

Red->green: before R06a ``hedge.units`` / ``hedge.economics`` did not exist
(ModuleNotFoundError). Pure offline only: fixed clocks, D15 merged config,
no network/DB/clock reads. Amount assertions use Decimal tolerance 1e-8;
Gate/status/quantity checks are exact (no tolerance masking unit errors).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from diveintocrypto_desktop.shortlab.hedge import units
from diveintocrypto_desktop.shortlab.hedge.units import (
    STOP_PRICE_UNREPRESENTABLE,
    canonical_price_usd,
    canonical_qty,
    compute_contract_vwap,
    contract_notional_usd,
    native_liquidation_distance,
    native_stop_reference,
    oi_notional_usd,
    quote_volume_usd,
    unscaled_notional_usd,
)
from diveintocrypto_desktop.shortlab.hedge.economics import (
    build_ratio_proposal,
    evaluate_economics,
)
from diveintocrypto_desktop.shortlab.observations import ObservationMeta, Observed
from diveintocrypto_desktop.shortlab.repair_contracts import FundingContext
from tests.repair_fixtures import (
    FIXTURE_NOW,
    make_decision_context,
    make_decision_request,
    make_funding_context,
)

TOL = Decimal("0.00000001")


def _assert_close(actual: str | None, expected: str, label: str = "") -> None:
    assert actual is not None, label
    diff = abs(Decimal(str(actual)) - Decimal(str(expected)))
    assert diff <= TOL, f"{label}: |{actual}-{expected}|={diff} > {TOL}"


def _observed_price(price: str) -> Observed:
    return Observed(
        value={"price": price},
        meta=ObservationMeta(
            status="OK",
            source="binance:fapi/mark",
            source_as_of_ms=FIXTURE_NOW - 3000,
            fetched_at_ms=FIXTURE_NOW - 2000,
            known_at_ms=FIXTURE_NOW - 2000,
        ),
    )


def _book(bids: list, asks: list) -> Observed:
    return Observed(
        value={"symbol": "T", "bids": bids, "asks": asks, "panel": {}},
        meta=ObservationMeta(
            status="OK",
            source="binance-futures-book",
            source_as_of_ms=FIXTURE_NOW - 1000,
            fetched_at_ms=FIXTURE_NOW - 500,
            known_at_ms=FIXTURE_NOW - 500,
        ),
    )


def _proposal(
    *,
    actual: str = "10000",
    spot: str = "0",
    margin: str = "12000",
    entry: str = "15",
    exit: str = "15",
    slippage: str = "0",
    gas: str = "0",
    fees_included: Any = True,
    reserve: str = "0.05",
) -> dict[str, Any]:
    return {
        "actual_futures_notional_usd": actual,
        "spot_cash_usd": spot,
        "margin_usd": margin,
        "entry_fee_usd": entry,
        "exit_fee_usd": exit,
        "slippage_usd": slippage,
        "gas_usd": gas,
        "fees_included": fees_included,
        "reserve_fraction": reserve,
    }


def _funding(apr: str | None) -> FundingContext:
    return make_funding_context(conservative_apr=apr)


# ---------------------------------------------------------------------------
# V06: units.
# ---------------------------------------------------------------------------


def test_native_liquidation_distance_V06() -> None:
    assert native_liquidation_distance("10", "15") == "0.5"
    # Exact: (15-10)/10.
    _assert_close(native_liquidation_distance("10", "15"), "0.5", "distance")
    # Equal => 0 (already at liquidation, caller marks INVALID).
    assert native_liquidation_distance("15", "15") == "0"
    # Below => negative (already crossed, never faked positive).
    assert Decimal(native_liquidation_distance("15", "10")) < 0


def test_native_stop_reference_V06_tick_upwards() -> None:
    assert native_stop_reference("10", "15", "0.005") == "12.5"
    stop = Decimal(native_stop_reference("10", "15", "0.005") or "")
    assert Decimal("10") < stop < Decimal("15")
    # Upwards: mid 12.502 with tick 0.005 must ceil to 12.505 (never floor).
    got = native_stop_reference("10", "15.004", "0.005")
    # mid = 12.502 -> ceil(2500.4) = 2501 * 0.005 = 12.505
    assert got == "12.505"
    assert Decimal("10") < Decimal(str(got)) < Decimal("15.004")


def test_stop_unrepresentable_coarse_tick() -> None:
    # Mark 10, L 10.01, tick 0.1: mid 10.005 -> ceil 10.1 >= L => None.
    assert native_stop_reference("10", "10.01", "0.1") is None
    assert STOP_PRICE_UNREPRESENTABLE == "STOP_PRICE_UNREPRESENTABLE"
    # Mark >= L has no Mark<Stop<L interval.
    assert native_stop_reference("15", "10", "0.005") is None
    assert native_stop_reference("10", "10", "0.005") is None
    with pytest.raises(ValueError):
        native_stop_reference("10", "15", "0")


def test_canonical_conversions_m1_m1000_FX097() -> None:
    # P_canonical = P_native * FX / m
    assert canonical_price_usd("0.01", "1", "1000") == "0.00001"
    _assert_close(canonical_price_usd("0.01", "0.97", "1000") or "", "0.0000097", "fx0.97")
    assert canonical_price_usd("100", "1", "1") == "100"
    # Q_canonical = Q_contract * m
    assert canonical_qty("100", "1000") == "100000"
    assert canonical_qty("100", "1") == "100"
    # Notional = P_native * Q_contract * FX
    assert contract_notional_usd("0.01", "100", "1") == "1"
    _assert_close(contract_notional_usd("0.01", "100", "0.97") or "", "0.97", "notional fx")
    # Unknown FX/multiplier => None (never default 1).
    assert canonical_price_usd("0.01", None, "1000") is None
    assert canonical_qty("100", None) is None
    assert contract_notional_usd("0.01", "100", None) is None


def test_qv_oi_never_scaled() -> None:
    # QV/OI helpers take no multiplier: m=1000 cannot leak into them.
    a = unscaled_notional_usd("100", "10", "1")
    b = quote_volume_usd("100", "10", "1")
    c = oi_notional_usd("100", "10", "1")
    assert a == b == c == "1000"
    # Same inputs always give the same output regardless of any contract m
    # (the functions do not even accept m, so scaling is impossible).
    assert unscaled_notional_usd("0.01", "100", "0.97") == "0.97"
    assert quote_volume_usd("0.01", "100", "0.97") == "0.97"
    assert oi_notional_usd("0.01", "100", "0.97") == "0.97"
    # Unknown FX => None, never 0.
    assert unscaled_notional_usd("100", "10", None) is None


def test_compute_contract_vwap_sides_not_merged() -> None:
    # Bids 100@10 (SELL side), asks 10@10 (BUY side). BUY 50 must only see asks.
    book = _book(
        bids=[["10", "100"]],
        asks=[["10", "10"]],
    )
    buy = compute_contract_vwap(book, "50", "BUY")
    assert buy["executable_contract_qty"] == "10"
    assert buy["coverage"] == "0.2"
    assert buy["vwap_native"] == "10"
    sell = compute_contract_vwap(book, "50", "SELL")
    assert sell["executable_contract_qty"] == "50"
    assert sell["coverage"] == "1"
    assert sell["vwap_native"] == "10"
    # Full coverage.
    full = compute_contract_vwap(_book([["10", "60"]], [["11", "60"]]), "50", "BUY")
    assert full["executable_contract_qty"] == "50"
    assert full["coverage"] == "1"
    assert full["vwap_native"] == "11"
    with pytest.raises(ValueError):
        compute_contract_vwap(book, "10", "HOLD")


def test_compute_contract_vwap_weighted_and_partial() -> None:
    # Asks: 10@10, 12@10. BUY 15 => (10*10+12*5)/15 = 160/15 = 10.666...
    book = _book(bids=[["9", "100"]], asks=[["10", "10"], ["12", "10"]])
    out = compute_contract_vwap(book, "15", "BUY")
    assert out["executable_contract_qty"] == "15"
    _assert_close(out["vwap_native"], str(Decimal("160") / Decimal("15")), "vwap")
    # Partial: request 100, only 20 available.
    thin = _book(bids=[["9", "20"]], asks=[["10", "20"]])
    part = compute_contract_vwap(thin, "100", "BUY")
    assert part["executable_contract_qty"] == "20"
    assert part["coverage"] == "0.2"


# ---------------------------------------------------------------------------
# V07: economics.
# ---------------------------------------------------------------------------


def test_evaluate_economics_V07_hold1_hold60_BE() -> None:
    # N=10000, APR=0.02, roundtrip=30.
    base = _proposal(actual="10000", entry="15", exit="15", slippage="0", gas="0")
    ctx = _funding("0.02")
    policy = {"min_net_carry_usd": "0"}
    e1 = evaluate_economics(base, ctx, 1, policy)
    # carry = 10000*0.02*1/365 = 0.54794520...
    _assert_close(e1.conservative_carry_usd, "0.54794520547945205479", "carry1")
    _assert_close(e1.roundtrip_cost_usd, "30", "roundtrip")
    assert Decimal(str(e1.net_carry_usd)) < 0
    assert e1.gate.status == "FAIL"
    _assert_close(e1.break_even_days, "54.75", "be")
    e60 = evaluate_economics(base, ctx, 60, policy)
    _assert_close(e60.conservative_carry_usd, "32.87671232876712328767", "carry60")
    assert Decimal(str(e60.net_carry_usd)) > 0
    assert e60.gate.status == "PASS"
    assert e60.cost_basis == "NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V2"


def test_evaluate_economics_fee_base_single_count() -> None:
    # Base-fee honesty: entry+exit+slippage+gas counted once each, no double.
    p = _proposal(actual="10000", entry="5", exit="7", slippage="2", gas="3")
    ctx = _funding("0.5")
    e = evaluate_economics(p, ctx, 30, {"min_net_carry_usd": "0"})
    assert e.roundtrip_cost_usd is not None
    _assert_close(e.roundtrip_cost_usd, "17", "roundtrip once")
    # carry = 10000*0.5*30/365 = 410.9589..., net = carry-17
    _assert_close(e.conservative_carry_usd, str(Decimal("10000") * Decimal("0.5") * Decimal("30") / Decimal("365")), "carry")
    _assert_close(e.net_carry_usd, str(Decimal(str(e.conservative_carry_usd)) - Decimal("17")), "net")


def test_evaluate_economics_vwap_impact_not_double_deducted() -> None:
    # VWAP spread is already in the execution price: explicit slippage 0 must
    # not add a second impact charge.
    p = _proposal(actual="10000", entry="10", exit="10", slippage="0", gas="0")
    ctx = _funding("0.25")
    e = evaluate_economics(p, ctx, 30, {"min_net_carry_usd": "0"})
    _assert_close(e.roundtrip_cost_usd, "20", "no double impact")


def test_evaluate_economics_APR_non_positive() -> None:
    p = _proposal()
    for apr in ("0", "-0.01"):
        ctx = _funding(apr)
        e = evaluate_economics(p, ctx, 30, {"min_net_carry_usd": "0"})
        assert e.break_even_days is None
        assert e.gate.status == "FAIL"
        assert "NON_POSITIVE_CARRY" in e.gate.reasons


def test_evaluate_economics_unknown_exit_fee() -> None:
    p = _proposal()
    p["exit_fee_usd"] = None
    e = evaluate_economics(p, _funding("0.02"), 30, {"min_net_carry_usd": "0"})
    assert e.gate.status == "UNKNOWN"
    assert e.net_carry_usd is None
    assert "UNKNOWN_COST" in e.gate.reasons
    assert "UNKNOWN_COST" in e.unknown_components


def test_evaluate_economics_hold_required() -> None:
    p = _proposal()
    ctx = _funding("0.02")
    for bad in (None, 0, 366):
        e = evaluate_economics(p, ctx, bad, {"min_net_carry_usd": "0"})  # type: ignore[arg-type]
        assert e.gate.status == "UNKNOWN"
        assert e.hold_days is None
        assert "HOLD_DAYS_UNKNOWN" in e.gate.reasons


def test_evaluate_economics_fees_included_explicit() -> None:
    p = _proposal()
    p["fees_included"] = None
    e = evaluate_economics(p, _funding("0.02"), 30, {"min_net_carry_usd": "0"})
    assert e.gate.status == "UNKNOWN"
    assert "UNKNOWN_COST" in e.gate.reasons or "FEES_INCLUDED_UNKNOWN" in e.gate.reasons


def test_evaluate_economics_strict_greater() -> None:
    # net == min => FAIL (strictly greater required).
    # carry = 100*0.365*10/365 = 1.0 ; roundtrip 1.0 => net 0.0
    p = _proposal(actual="100", entry="0.5", exit="0.5", slippage="0", gas="0")
    ctx = _funding("0.365")
    e_eq = evaluate_economics(p, ctx, 10, {"min_net_carry_usd": "0"})
    _assert_close(e_eq.net_carry_usd, "0", "net eq")
    assert e_eq.gate.status == "FAIL"
    e_gt = evaluate_economics(p, ctx, 11, {"min_net_carry_usd": "0"})
    assert Decimal(str(e_gt.net_carry_usd)) > 0
    assert e_gt.gate.status == "PASS"
    # Positive min: net 0.1 vs min 0.1 => FAIL; vs min 0.09 => PASS.
    e2 = evaluate_economics(p, ctx, 11, {"min_net_carry_usd": str(e_gt.net_carry_usd)})
    assert e2.gate.status == "FAIL"


# ---------------------------------------------------------------------------
# build_ratio_proposal (h0 read-only, rounding, six scenarios).
# ---------------------------------------------------------------------------


def test_build_ratio_proposal_h0_readonly() -> None:
    req = make_decision_request()
    ctx = make_decision_context()
    policy = {"min_net_carry_usd": "0", "capital_reserve_fraction": "0.05", "ratio_tolerance": "0.02", "exit_stress_bps": 100}
    prop = build_ratio_proposal(req, ctx, "0", policy)
    assert prop.target_ratio == "0"
    assert prop.actual_ratio == "0"
    assert prop.spot_net_qty == "0"
    assert prop.spot_venue is None
    assert "spot" not in prop.quote_refs
    assert len(prop.order_guidance) == 1
    assert prop.order_guidance[0]["leg"] == "FUTURES_SHORT"
    assert prop.economics is not None
    assert len(prop.scenarios) == 6


def test_build_ratio_proposal_rounding_recalc() -> None:
    # Futures step 10 floors raw qty; N/h/capital recomputed from legal qty.
    from diveintocrypto_desktop.shortlab.hedge.models import TradingRulesSnapshot

    req = make_decision_request(futures_notional_usd="1000", planned_hold_days=30)
    ctx = make_decision_context()
    # Coarse futures step forces flooring.
    rules = TradingRulesSnapshot(
        venue="BINANCE_SPOT",
        instrument_id="1000PEPEUSDT",
        source_as_of_ms=FIXTURE_NOW - 60000,
        known_at_ms=FIXTURE_NOW - 50000,
        rule_version="rules-v1",
        raw_filters={},
        order_types={"LIMIT": True, "MARKET": True, "STOP": True, "STOP_MARKET": True},
        price_rules={"tick_size": "0.000001"},
        lot_rules={"step_size": "500", "min_qty": "1", "max_qty": "1000000"},
        notional_rules={"min_notional": "5", "max_notional": "1000000"},
        stop_orders_supported=True,
        conditional_orders_source_ref="rules:1",
    )
    ctx2 = make_decision_context(futures_rules=rules)
    policy = {"min_net_carry_usd": "0", "capital_reserve_fraction": "0.05", "ratio_tolerance": "0.02", "exit_stress_bps": 100}
    prop = build_ratio_proposal(req, ctx2, "0", policy)
    qty = Decimal(str(prop.futures_contract_qty))
    # Raw would be 1000/0.0099 ~= 101010.1; floored to 500-step => %500==0.
    assert qty % Decimal("500") == 0
    # N recomputed from floored qty, not raw.
    sell = Decimal("0.0099")
    _assert_close(prop.economics.actual_futures_notional_usd, str(qty * sell * Decimal("1")), "recalc N")


def test_build_ratio_proposal_six_scenarios_liquidation_null() -> None:
    # V06 pressure: Mark 10, L 15 => +50% (15) and +100% (20) cross.
    req = make_decision_request(liquidation_price="15", futures_notional_usd="100")
    ctx = make_decision_context(futures_mark=_observed_price("10"))
    policy = {"min_net_carry_usd": "0", "capital_reserve_fraction": "0.05", "ratio_tolerance": "0.02", "exit_stress_bps": 100}
    prop = build_ratio_proposal(req, ctx, "0", policy)
    by_id = {s.scenario_id: s for s in prop.scenarios}
    assert set(by_id) == {"UP_50", "UP_100", "DOWN_50", "BASIS_UP", "BASIS_DOWN", "FX_DOWN"}
    for sid in ("UP_50", "UP_100"):
        assert by_id[sid].status == "INVALID_AFTER_LIQUIDATION"
        assert by_id[sid].net_pnl_usd is None
        assert by_id[sid].loss_usd is None
    for sid in ("DOWN_50", "BASIS_UP", "BASIS_DOWN", "FX_DOWN"):
        assert by_id[sid].status == "VALID"
        assert by_id[sid].net_pnl_usd is not None
    # Any crossing scenario fails the risk gate (never auto-assumed stopped).
    assert prop.risk_gate.status == "FAIL"
    assert "SCENARIO_LIQUIDATION" in prop.risk_gate.reasons


def test_build_ratio_proposal_uses_real_exit_amounts() -> None:
    # Scenario exit fees use stressed notionals: UP_50 exit > DOWN_50 exit,
    # so their nets differ (no flat-fee shortcut).
    req = make_decision_request(liquidation_price="100", futures_notional_usd="1000")
    ctx = make_decision_context(futures_mark=_observed_price("10"))
    policy = {"min_net_carry_usd": "0", "capital_reserve_fraction": "0.05", "ratio_tolerance": "0.02", "exit_stress_bps": 100}
    prop = build_ratio_proposal(req, ctx, "0", policy)
    by_id = {s.scenario_id: s for s in prop.scenarios}
    up = Decimal(str(by_id["UP_50"].net_pnl_usd))
    down = Decimal(str(by_id["DOWN_50"].net_pnl_usd))
    assert up != down


def test_build_ratio_proposal_unknown_spot_for_h1() -> None:
    # h>0 without any venue quote cannot fake a spot leg.
    req = make_decision_request()
    ctx = make_decision_context(venue_quotes=())
    policy = {"min_net_carry_usd": "0", "capital_reserve_fraction": "0.05", "ratio_tolerance": "0.02", "exit_stress_bps": 100}
    prop = build_ratio_proposal(req, ctx, "1", policy)
    assert prop.spot_net_qty == "0"
    assert prop.spot_venue is None
    assert prop.execution_gate.status != "PASS"
    assert "spot" not in prop.quote_refs
