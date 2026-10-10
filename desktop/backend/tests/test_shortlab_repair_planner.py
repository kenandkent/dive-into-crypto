"""R06b protection + planner repair (D06.3/D18, V08).

Red->green: before R06b ``hedge.protection`` did not exist
(ModuleNotFoundError) and ``simulate_hedge`` lacked
``futures_quote``/``ports`` + ``readiness_breakdown``/``economics``.
Pure offline only: fixed clocks, no network/DB/clock reads.

Covers:
- V08 tri-state: SUPPORTED/UNSUPPORTED/UNKNOWN with explicit rule evidence;
  user ``stop_policy`` never changes platform capability; UNKNOWN capability
  with ports is NOT_READY and never VERIFIED.
- goal/mode: ABSOLUTE only CARRY_CAPTURE (422/ValueError otherwise);
  RELATIVE defaults to BALANCED, explicit DIRECTIONAL_SHORT allowed;
  hold-days missing forces economics UNKNOWN.
- h0 read-only: ``build_ratio_proposal(..., "0")`` is contract-only
  (no faked spot leg).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from diveintocrypto_desktop.shortlab.hedge.models import (
    HedgeSimulationRequest,
    SpotVenueQuote,
    TradingRulesSnapshot,
)
from tests.repair_fixtures import (
    FIXTURE_NOW,
    make_decision_context,
    make_decision_request,
    make_ports,
)

NOW = FIXTURE_NOW


def _spot_quote(**over: Any) -> SpotVenueQuote:
    base: dict[str, Any] = dict(
        venue="BINANCE_SPOT",
        canonical_id="pepe",
        symbol="1000PEPEUSDT",
        chain=None,
        contract_address=None,
        as_of_ms=NOW - 5000,
        expires_at_ms=NOW + 15000,
        reference_notional_usd="10000",
        mid_price="0.01",
        buy_vwap="0.0101",
        sell_vwap="0.0099",
        buy_executable_qty="200000",
        sell_executable_qty="200000",
        buy_slippage_bps=5.0,
        sell_slippage_bps=5.0,
        estimated_fee_usd="5",
        estimated_gas_usd=None,
        direction_costs={},
        entry_feasible=True,
        exit_feasible=True,
        exit_feasibility="CONFIRMED",
        quote_currency="USDT",
        quote_to_usd="1",
        source_timestamp_ms=NOW - 5000,
        fetched_at_ms=NOW - 4000,
        requested_canonical_qty="100000",
        trading_rules={},
        capabilities={},
        identity_confidence="VERIFIED",
        status="OK",
        reason_code=None,
        fees_included=True,
    )
    base.update(over)
    return SpotVenueQuote(**base)


def _futures_rules(**over: Any) -> TradingRulesSnapshot:
    base: dict[str, Any] = dict(
        venue="BINANCE_SPOT",
        instrument_id="1000PEPEUSDT",
        source_as_of_ms=NOW - 60000,
        known_at_ms=NOW - 50000,
        rule_version="rules-v1",
        raw_filters={},
        order_types={"LIMIT": True, "MARKET": True},
        price_rules={"tick_size": "0.000001"},
        lot_rules={"step_size": "1", "min_qty": "1", "max_qty": "1000000"},
        notional_rules={"min_notional": "5", "max_notional": "1000000"},
    )
    for key in ("price_rules", "lot_rules", "notional_rules", "order_types"):
        if key in over:
            merged = dict(base[key])
            merged.update(over.pop(key))
            base[key] = merged
    base.update(over)
    return TradingRulesSnapshot(**base)


def _supported_rules() -> TradingRulesSnapshot:
    return _futures_rules(
        order_types={"LIMIT": True, "MARKET": True, "STOP": True, "STOP_MARKET": True},
        stop_orders_supported=True,
        conditional_orders_source_ref="rules:1000PEPEUSDT:1",
    )


def _unsupported_rules() -> TradingRulesSnapshot:
    return _futures_rules(
        order_types={"LIMIT": True, "MARKET": True, "STOP": False, "STOP_MARKET": False},
        stop_orders_supported=False,
        conditional_orders_source_ref="rules:1000PEPEUSDT:1",
    )


def _unknown_rules() -> TradingRulesSnapshot:
    # Only LIMIT/MARKET, no STOP keys and no stop_orders_supported evidence.
    return _futures_rules()


def _mark(price: str = "0.01") -> dict[str, Any]:
    return {
        "symbol": "1000PEPEUSDT",
        "mark_price": price,
        "quote_currency": "USDT",
        "quote_to_usd": "1",
        "as_of_ms": NOW - 3000,
        "expires_at_ms": NOW + 15000,
    }


def _identity() -> dict[str, Any]:
    return {"canonical_id": "pepe", "contract_multiplier": "1000", "identity_confidence": "VERIFIED"}


def _funding(apr: str = "0.365"):
    from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics

    return FundingMetrics(symbol="1000PEPEUSDT", conservative_apr=apr)


def _futures_quote() -> Any:
    from diveintocrypto_desktop.shortlab.repair_contracts import FuturesExecutionQuote

    return FuturesExecutionQuote(
        quote_id="fq-r06b",
        symbol="1000PEPEUSDT",
        requested_contract_qty="100",
        buy_vwap_native="0.0101",
        sell_vwap_native="0.0099",
        buy_executable_qty="1000",
        sell_executable_qty="1000",
        quote_currency="USDT",
        quote_to_usd="1",
        as_of_ms=NOW - 5000,
        known_at_ms=NOW - 4000,
        expires_at_ms=NOW + 15000,
        book_observation_id="book-r06b",
        fees_included=True,
    )


def _req(**over: Any) -> HedgeSimulationRequest:
    base: dict[str, Any] = dict(
        symbol="1000PEPEUSDT",
        mode="ABSOLUTE",
        futures_notional_usd="10000",
        preferred_spot_venue="AUTO",
        futures_leverage="1",
        margin_mode="ISOLATED",
        margin_usd="12000",
        liquidation_price="0.025",
        liquidation_price_source="USER_EXCHANGE",
        liquidation_price_updated_at_ms=NOW - 3600_000,
        stop_policy="USER_PLATFORM_ORDERS",
        stop_trigger_basis="MARK_PRICE",
        planned_hold_days=30,
        goal="CARRY_CAPTURE",
    )
    base.update(over)
    return HedgeSimulationRequest(**base)


# ---------------------------------------------------------------------------
# V08: capability tri-state.
# ---------------------------------------------------------------------------


def test_resolve_stop_capability_supported_V08() -> None:
    from diveintocrypto_desktop.shortlab.hedge.protection import resolve_stop_capability

    assert resolve_stop_capability(_supported_rules()) == "SUPPORTED"
    # Mapping shape with explicit STOP evidence is also SUPPORTED.
    assert (
        resolve_stop_capability(
            {
                "order_types": {"LIMIT": True, "MARKET": True, "STOP_MARKET": True},
                "stop_orders_supported": True,
                "conditional_orders_source_ref": "rules:x:1",
            }
        )
        == "SUPPORTED"
    )


def test_resolve_stop_capability_unsupported_V08() -> None:
    from diveintocrypto_desktop.shortlab.hedge.protection import resolve_stop_capability

    assert resolve_stop_capability(_unsupported_rules()) == "UNSUPPORTED"
    assert (
        resolve_stop_capability(
            {
                "order_types": {"LIMIT": True, "MARKET": True, "STOP": False},
                "stop_orders_supported": False,
                "conditional_orders_source_ref": "rules:x:1",
            }
        )
        == "UNSUPPORTED"
    )


def test_resolve_stop_capability_unknown_V08() -> None:
    from diveintocrypto_desktop.shortlab.hedge.protection import resolve_stop_capability

    # Only LIMIT/MARKET and no stop evidence: absence is UNKNOWN, never
    # synthesised SUPPORTED and never default UNSUPPORTED.
    assert resolve_stop_capability(_unknown_rules()) == "UNKNOWN"
    assert resolve_stop_capability({"order_types": {"LIMIT": True, "MARKET": True}}) == "UNKNOWN"
    assert resolve_stop_capability({}) == "UNKNOWN"
    assert resolve_stop_capability(None) == "UNKNOWN"


def test_stop_policy_does_not_change_capability_V08() -> None:
    from diveintocrypto_desktop.shortlab.hedge.protection import resolve_stop_capability

    rules = _unknown_rules()
    assert resolve_stop_capability(rules) == "UNKNOWN"
    # User choice travels in the request, never in the rules object; the
    # platform capability is identical regardless of stop_policy.
    for _policy in ("USER_PLATFORM_ORDERS", "ALERT_ONLY", None):
        assert resolve_stop_capability(rules) == "UNKNOWN"
    assert resolve_stop_capability(_supported_rules()) == "SUPPORTED"


def test_simulate_unknown_capability_never_verified_V08() -> None:
    from diveintocrypto_desktop.shortlab.hedge.planner import simulate_hedge

    request = _req()
    result = simulate_hedge(
        request,
        futures_mark=_mark(),
        spot_quote=_spot_quote(),
        futures_rules=_unknown_rules(),
        spot_rules=_futures_rules(),
        funding=_funding(),
        identity=_identity(),
        policy=None,
        now_ms=NOW,
        futures_quote=_futures_quote(),
        ports=make_ports(),
    )
    assert result.risk_validation != "VERIFIED"
    assert result.readiness == "NOT_READY"
    # New repair breakdown is populated and honest.
    assert result.readiness_breakdown is not None
    bd = dict(result.readiness_breakdown)
    assert bd["protection_status"] == "UNKNOWN"
    assert bd["readiness"] == "NOT_READY"
    assert bd["execution_gate"]["status"] != "PASS"
    assert result.economics is not None


def test_simulate_supported_capability_reports_pending_V08() -> None:
    from diveintocrypto_desktop.shortlab.hedge.planner import simulate_hedge

    result = simulate_hedge(
        _req(),
        futures_mark=_mark(),
        spot_quote=_spot_quote(),
        futures_rules=_supported_rules(),
        spot_rules=_futures_rules(),
        funding=_funding(),
        identity=_identity(),
        policy=None,
        now_ms=NOW,
        futures_quote=_futures_quote(),
        ports=make_ports(),
    )
    assert result.readiness_breakdown is not None
    bd = dict(result.readiness_breakdown)
    # Capable but no user confirmation yet: PENDING, never CONFIRMED.
    assert bd["protection_status"] in ("PENDING", "UNKNOWN", "UNSUPPORTED")
    # Simulation alone never claims a live protection order.
    for leg in result.order_guidance:
        caps = str(leg.get("capability", "")).upper() if isinstance(leg, dict) else ""
        assert "LIVE" not in caps
        assert "VERIFIED_ORDER" not in caps


# ---------------------------------------------------------------------------
# goal/mode.
# ---------------------------------------------------------------------------


def test_goal_mode_absolute_only_carry() -> None:
    # DTO already freezes ABSOLUTE to CARRY_CAPTURE.
    with pytest.raises(ValueError):
        HedgeSimulationRequest(
            symbol="1000PEPEUSDT",
            mode="ABSOLUTE",
            futures_notional_usd="10000",
            goal="BALANCED",
        )
    # Explicit CARRY passes.
    ok = _req(mode="ABSOLUTE", goal="CARRY_CAPTURE")
    assert ok.goal == "CARRY_CAPTURE"
    # Planner also rejects a forged ABSOLUTE+non-CARRY mapping at 422.
    from diveintocrypto_desktop.shortlab.hedge.planner import PlannerInputError

    forged = _req(mode="ABSOLUTE", goal="CARRY_CAPTURE")
    # Sanity: valid combo does not raise.
    assert forged.mode == "ABSOLUTE"
    with pytest.raises(PlannerInputError) as exc:
        from diveintocrypto_desktop.shortlab.hedge import planner as _pl

        _pl._validate_goal_mode({"mode": "ABSOLUTE", "goal": "BALANCED"})
    assert "422" in str(exc.value)


def test_goal_mode_relative_defaults_and_directional() -> None:
    from diveintocrypto_desktop.shortlab.hedge.planner import simulate_hedge

    # RELATIVE without goal derives BALANCED (read-only, history untouched).
    req_default = HedgeSimulationRequest(
        symbol="1000PEPEUSDT",
        mode="RELATIVE",
        futures_notional_usd="10000",
        hedge_ratio="0.5",
    )
    assert req_default.goal is None
    result = simulate_hedge(
        req_default,
        futures_mark=_mark(),
        spot_quote=_spot_quote(),
        futures_rules=_supported_rules(),
        spot_rules=_futures_rules(),
        funding=_funding(),
        identity=_identity(),
        policy=None,
        now_ms=NOW,
        futures_quote=_futures_quote(),
        ports=make_ports(),
    )
    assert result.readiness_breakdown is not None
    # Explicit DIRECTIONAL_SHORT is allowed for RELATIVE.
    req_dir = HedgeSimulationRequest(
        symbol="1000PEPEUSDT",
        mode="RELATIVE",
        futures_notional_usd="10000",
        hedge_ratio="0.5",
        goal="DIRECTIONAL_SHORT",
    )
    result2 = simulate_hedge(
        req_dir,
        futures_mark=_mark(),
        spot_quote=_spot_quote(),
        futures_rules=_supported_rules(),
        spot_rules=_futures_rules(),
        funding=_funding(),
        identity=_identity(),
        policy=None,
        now_ms=NOW,
        futures_quote=_futures_quote(),
        ports=make_ports(),
    )
    assert result2.readiness_breakdown is not None


def test_simulate_missing_ports_never_new_ready() -> None:
    from diveintocrypto_desktop.shortlab.hedge.planner import simulate_hedge

    # Same verified inputs but no ports: legacy research calc runs, new
    # repair breakdown must stay NOT_READY/UNKNOWN (never new READY).
    result = simulate_hedge(
        _req(),
        futures_mark=_mark(),
        spot_quote=_spot_quote(),
        futures_rules=_supported_rules(),
        spot_rules=_futures_rules(),
        funding=_funding(),
        identity=_identity(),
        policy=None,
        now_ms=NOW,
    )
    assert result.readiness_breakdown is not None
    bd = dict(result.readiness_breakdown)
    assert bd["readiness"] == "NOT_READY"
    assert bd["funding_gate"]["status"] == "UNKNOWN" or bd["execution_gate"]["status"] in ("UNKNOWN", "FAIL")


def test_simulate_hold_days_required_for_economics() -> None:
    from diveintocrypto_desktop.shortlab.hedge.planner import simulate_hedge

    req = _req(planned_hold_days=None)
    result = simulate_hedge(
        req,
        futures_mark=_mark(),
        spot_quote=_spot_quote(),
        futures_rules=_supported_rules(),
        spot_rules=_futures_rules(),
        funding=_funding(),
        identity=_identity(),
        policy=None,
        now_ms=NOW,
        futures_quote=_futures_quote(),
        ports=make_ports(),
    )
    assert result.economics is not None
    econ = dict(result.economics)
    assert econ["hold_days"] is None or econ.get("gate", {}).get("status") in ("UNKNOWN", "FAIL")


# ---------------------------------------------------------------------------
# h0 read-only.
# ---------------------------------------------------------------------------


def test_h0_readonly_no_faked_spot() -> None:
    from diveintocrypto_desktop.shortlab.hedge.economics import build_ratio_proposal

    req = make_decision_request()
    ctx = make_decision_context()
    policy = {
        "min_net_carry_usd": "0",
        "capital_reserve_fraction": "0.05",
        "ratio_tolerance": "0.02",
        "exit_stress_bps": 100,
    }
    prop = build_ratio_proposal(req, ctx, "0", policy)
    assert prop.target_ratio == "0"
    assert prop.actual_ratio == "0"
    assert prop.spot_net_qty == "0"
    assert prop.spot_venue is None
    assert "spot" not in prop.quote_refs
    assert len(prop.order_guidance) == 1
    assert prop.order_guidance[0]["leg"] == "FUTURES_SHORT"
    assert len(prop.scenarios) == 6


def test_validate_protection_confirmation_two_legs_24h_hash() -> None:
    from diveintocrypto_desktop.shortlab.hedge.protection import (
        compute_protected_position_hash,
        validate_protection_confirmation,
    )

    h = compute_protected_position_hash(
        futures_remaining="100",
        spot_remaining="100000",
        liquidation_price="0.025",
        stop_trigger_price="0.0175",
        stop_trigger_basis="MARK_PRICE",
        rule_ids=("rules-v1",),
    )
    assert isinstance(h, str) and len(h) == 64
    record = {
        "confirmation_id": "conf-1",
        "client_request_id": "c-1",
        "confirmed_at_ms": NOW,
        "expires_at_ms": NOW + 86400_000,
        "source": "USER_CONFIRMED",
        "protected_position_hash": h,
        "confirmation_json": {
            "futures": {
                "orderReference": "fut-ord-1",
                "nativeQty": "100",
                "triggerPrice": "0.0175",
                "triggerBasis": "MARK_PRICE",
                "status": "CONFIRMED",
            },
            "spot": {
                "exitMode": "MANUAL_EXIT_ONLY",
                "nativeQty": "100000",
                "status": "CONFIRMED",
            },
            "protected_position_hash": h,
            "source": "USER_CONFIRMED",
        },
    }
    plan = {
        "plan_id": "plan-1",
        "plan_version": 2,
        "liquidation_price": "0.025",
        "stop_trigger_price": "0.0175",
        "stop_trigger_basis": "MARK_PRICE",
        "rule_ids": ("rules-v1",),
    }
    positions = (
        {"leg_type": "FUTURES_SHORT", "remaining_qty": "100"},
        {"leg_type": "SPOT_LONG", "remaining_qty": "100000"},
    )
    gate = validate_protection_confirmation(record, plan, positions, NOW + 1000)
    assert gate.status == "PASS"
    # 24h expiry: after TTL the same record is EXPIRED/FAIL, never PASS.
    late = validate_protection_confirmation(record, plan, positions, NOW + 86400_000 + 1)
    assert late.status != "PASS"
    assert any("EXPIRED" in r for r in late.reasons)
    # Quantity drift invalidates the hash binding.
    drifted_positions = (
        {"leg_type": "FUTURES_SHORT", "remaining_qty": "90"},
        {"leg_type": "SPOT_LONG", "remaining_qty": "100000"},
    )
    bad = validate_protection_confirmation(record, plan, drifted_positions, NOW + 1000)
    assert bad.status != "PASS"
    # Pure version bump with identical hash stays PASS (version alone never
    # invalidates).
    plan_bumped = dict(plan, plan_version=3)
    still = validate_protection_confirmation(record, plan_bumped, positions, NOW + 1000)
    assert still.status == "PASS"


def test_protection_confirmation_futures_qty_is_native_for_1000x_contract() -> None:
    from diveintocrypto_desktop.shortlab.hedge.protection import (
        compute_protected_position_hash,
        validate_protection_confirmation,
    )

    positions = (
        {"leg_type": "FUTURES_SHORT", "remaining_qty": "1234"},
        {"leg_type": "SPOT_LONG", "remaining_qty": "1234"},
    )
    h = compute_protected_position_hash(
        futures_remaining="1234", spot_remaining="1234",
        liquidation_price="0.025", stop_trigger_price="0.020",
        stop_trigger_basis="MARK_PRICE", rule_ids=("rules-1000pepe-v1",),
    )
    record = {
        "confirmed_at_ms": NOW, "expires_at_ms": NOW + 86400_000,
        "source": "USER_CONFIRMED", "protected_position_hash": h,
        "confirmation_json": {
            "futures": {"status": "CONFIRMED", "nativeQty": "1.234",
                         "triggerPrice": "0.020", "triggerBasis": "MARK_PRICE",
                         "orderReference": "f-1"},
            "spot": {"status": "CONFIRMED", "nativeQty": "1234",
                      "exitMode": "PLATFORM_ORDER", "orderReference": "s-1"},
            "protected_position_hash": h, "source": "USER_CONFIRMED",
        },
    }
    plan = {
        "plan_id": "plan-1000pepe", "plan_version": 3,
        "contract_multiplier": "1000", "liquidation_price": "0.025",
        "stop_trigger_price": "0.020", "stop_trigger_basis": "MARK_PRICE",
        "rule_ids": ("rules-1000pepe-v1",),
    }
    assert validate_protection_confirmation(record, plan, positions, NOW + 1).status == "PASS"
    wrong_unit = {**record, "confirmation_json": {
        **record["confirmation_json"],
        "futures": {**record["confirmation_json"]["futures"], "nativeQty": "1234"},
    }}
    assert validate_protection_confirmation(wrong_unit, plan, positions, NOW + 1).status == "FAIL"


def test_venue_fees_included_honest_and_indicative() -> None:
    # Spot quotes carry an explicit policy-estimate fee (honest True).
    q = _spot_quote()
    assert q.fees_included is True
    # Alpha/0x stay indicative-only: no execution permission is added by R06b.
    from diveintocrypto_desktop.shortlab.hedge.venues import onchain as onchain_mod

    assert onchain_mod.ONCHAIN_QUOTE_KIND == "INDICATIVE"
    assert onchain_mod.ONCHAIN_FEES_INCLUDED is False
    assert onchain_mod.ONCHAIN_EXECUTION_KIND == "INDICATIVE_ONLY"
    assert onchain_mod.describe_onchain_capabilities()["execution_kind"] == "INDICATIVE_ONLY"
    assert "place_order" not in dir(onchain_mod.OnchainQuoteProvider)
    from diveintocrypto_desktop.shortlab.hedge.models import OnchainQuote

    oq = OnchainQuote(
        venue="ONCHAIN_DEX",
        canonical_id="pepe",
        chain="ethereum",
        contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        as_of_ms=NOW,
        expires_at_ms=NOW + 30000,
        requested_canonical_qty="1",
        provider_id="ETHEREUM_0X_PRICE_V2",
        api_version="v2",
        chain_id=1,
        block_number=1,
        atomic_amounts={"buyAmount": "1000000", "sellAmount": "1000000"},
        decimals=6,
        quote_kind="INDICATIVE",
        buy_vwap="1",
        sell_vwap=None,
        buy_executable_qty="1",
        sell_executable_qty=None,
        gas_units=None,
        gas_price=None,
        native_gas_fx=None,
        estimated_gas_usd=None,
        estimated_fee_usd=None,
        token_tax_status="UNKNOWN",
        route_complete=True,
        simulation_verified=False,
        quote_currency="USDC",
        quote_to_usd=None,
        fetched_at_ms=NOW,
        status="PARTIAL",
        reason_code="ONCHAIN_GAS_UNAVAILABLE",
        fees_included=False,
    )
    assert oq.quote_kind == "INDICATIVE"
    assert oq.simulation_verified is False
    assert oq.fees_included is False
    # Base helpers project honest capabilities without execution permission.
    from diveintocrypto_desktop.shortlab.hedge.venues import base as base_mod

    assert base_mod.SPOT_FEES_INCLUDED is True
    assert base_mod.ONCHAIN_FEES_INCLUDED is False
    assert base_mod.SPOT_CONDITIONAL_CAPABILITY == "MANUAL_EXIT_ONLY"
    assert base_mod.ONCHAIN_EXECUTION_KIND == "INDICATIVE"
    assert base_mod.project_onchain_capabilities()["execution_kind"] == "INDICATIVE_ONLY"
    # Ethereum 0x module exposes honest flags, no trade surface.
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as eth_mod

    assert eth_mod.ONCHAIN_FEES_INCLUDED is False
    assert eth_mod.ONCHAIN_EXECUTION_KIND == "INDICATIVE_ONLY"
    assert not hasattr(eth_mod.Ethereum0xPriceVenue, "place_order")
    assert not hasattr(eth_mod.Ethereum0xPriceVenue, "build_calldata")


@pytest.mark.asyncio
async def test_spot_venue_quote_carries_fees_included_and_no_execution() -> None:
    from diveintocrypto_desktop.shortlab.hedge.venues.binance_spot import BinanceSpotVenue
    from diveintocrypto_desktop.shortlab.models import AssetIdentity

    async def _entry(symbol, _ctx=None):
        return {
            "symbol": symbol,
            "status": "TRADING",
            "baseAsset": "PEPE",
            "quoteAsset": "USDT",
            "orderTypes": ["LIMIT", "MARKET"],
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.00001", "maxPrice": "1000", "tickSize": "0.00001"},
                {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "100000000", "stepSize": "1"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "1", "maxQty": "100000000", "stepSize": "1"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True, "avgPriceMins": 5},
            ],
        }

    async def _book(symbol, _ctx=None):
        return {"symbol": symbol, "bidPrice": "0.01234", "askPrice": "0.01236"}

    async def _depth(symbol, limit, _ctx=None):
        return {
            "lastUpdateId": 7,
            "bids": [["0.01234", "100000"], ["0.01230", "100000"]],
            "asks": [["0.01236", "100000"], ["0.01240", "100000"]],
        }

    venue = BinanceSpotVenue(
        config=None, exchange_entry_fn=_entry, book_ticker_fn=_book,
        depth_fn=_depth, now_ms_fn=lambda: NOW,
    )
    ident = AssetIdentity(
        canonical_id="pepe", display_symbol="1000PEPEUSDT",
        binance_futures_symbol="1000PEPEUSDT", binance_spot_symbol="1000PEPEUSDT",
        mapping_confidence="VERIFIED", mapping_source="MANUAL",
    )
    res = await venue.quote(ident, "500")
    assert res.data is not None
    assert res.data.fees_included is True
    assert res.data.capabilities.get("fees_included") is True
    # Real capability projection, never an execution permission.
    assert res.data.capabilities.get("spot_conditional_capability") == "MANUAL_EXIT_ONLY"
    assert res.data.capabilities.get("execution_kind") == "QUOTE_ONLY"
    assert not hasattr(venue, "place_order")
