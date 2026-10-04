"""H01 models: frozen Hedge DTOs, snake_case/camelCase, Decimal strings.

Covers plan H01.1/H01.2 minimal scene (DTO freeze only):
- every H02-H10 output type lives in hedge/models.py (no consumer copy);
- internal snake_case, API aliases camelCase;
- qty/price native decimal strings (float rejected);
- frozen records reject mutation;
- HistoricalMarketProvider protocol + three frozen history DTOs per B39.1.1.
"""

from __future__ import annotations

import dataclasses

import pytest


def _all_dto_names():
    from diveintocrypto_desktop.shortlab.hedge import models as m
    return [
        "SpotVenueQuote", "TradingRulesSnapshot", "OnchainQuote",
        "HedgeSimulationRequest", "HedgeSimulationResult",
        "HedgeEvent", "HedgePosition", "HedgeAlert", "HedgeMonitor",
        "FundingMetrics", "FCSResult", "DQResult", "OrderGuidance",
        "Readiness", "LedgerResult", "PlanState", "HedgeOutcome",
        "HedgeEvidenceSummary", "HistoricalPriceBar", "HistoricalFundingEvent",
        "HistoricalLifecycle",
    ]


def test_all_hedge_dtos_importable_from_models():
    from diveintocrypto_desktop.shortlab.hedge import models as m
    for name in _all_dto_names():
        assert hasattr(m, name), name
    # Protocol itself is frozen here (H10 must not copy SpotVenueQuote).
    assert hasattr(m, "HistoricalMarketProvider")
    # Version stamps (B41).
    assert m.FCS_VERSION == "fcs_v1"
    assert m.HEDGE_FORMULA_VERSION == "hedge_v1"
    assert m.HEDGE_EVIDENCE_VERSION == "hedge_evidence_v2"


def _spot_kwargs(**over):
    base = dict(
        venue="BINANCE_SPOT", canonical_id="bitcoin", symbol="BTCUSDT",
        chain=None, contract_address=None, as_of_ms=1_760_000_000_000,
        expires_at_ms=1_760_000_060_000, reference_notional_usd="10000",
        mid_price="67000.5", buy_vwap="67010.25", sell_vwap="66990.75",
        buy_executable_qty="1.500", sell_executable_qty="1.500",
        buy_slippage_bps=30.0, sell_slippage_bps=31.0,
        estimated_fee_usd="10.5", estimated_gas_usd=None,
        direction_costs={}, entry_feasible=True, exit_feasible=True,
        exit_feasibility="CONFIRMED", quote_currency="USDT",
        quote_to_usd="1", source_timestamp_ms=1_759_999_999_000,
        fetched_at_ms=1_760_000_000_000, requested_canonical_qty="1.500",
        trading_rules={}, capabilities={}, identity_confidence="VERIFIED",
        status="OK", reason_code=None,
    )
    base.update(over)
    return base


def test_spot_venue_quote_freezes_b9_fields_with_decimal_qty_price():
    from diveintocrypto_desktop.shortlab.hedge.models import SpotVenueQuote
    quote = SpotVenueQuote(**_spot_kwargs())
    assert quote.buy_executable_qty == "1.500"
    assert quote.mid_price == "67000.5"
    # Frozen: mutation raises.
    with pytest.raises(dataclasses.FrozenInstanceError):
        quote.canonical_id = "other"  # type: ignore[misc]
    # qty/price as float is rejected (native Decimal strings only).
    with pytest.raises(ValueError):
        SpotVenueQuote(**_spot_kwargs(requested_canonical_qty=1.5))
    with pytest.raises(ValueError):
        SpotVenueQuote(**_spot_kwargs(mid_price=67000.5))


def test_internal_snake_case_api_aliases_camel_case():
    from diveintocrypto_desktop.shortlab.hedge.models import (
        SpotVenueQuote, from_api_dict, to_api_dict,
    )
    quote = SpotVenueQuote(**_spot_kwargs())
    api = to_api_dict(quote)
    assert api["requestedCanonicalQty"] == "1.500"
    assert api["buyExecutableQty"] == "1.500"
    assert "requested_canonical_qty" not in api
    # from_api_dict accepts either spelling.
    rebuilt = from_api_dict(SpotVenueQuote, api)
    assert rebuilt == quote
    rebuilt2 = from_api_dict(SpotVenueQuote, dataclasses.asdict(quote))
    assert rebuilt2 == quote


def test_trading_rules_snapshot_freezes_decimal_rule_strings():
    from diveintocrypto_desktop.shortlab.hedge.models import TradingRulesSnapshot
    snap = TradingRulesSnapshot(
        venue="BINANCE_SPOT", instrument_id="BTCUSDT",
        source_as_of_ms=1_760_000_000_000, known_at_ms=1_760_000_001_000,
        rule_version="rules-v1", raw_filters={},
        order_types={"LIMIT": True, "MARKET": True},
        price_rules={"tick_size": "0.10"},
        lot_rules={"step_size": "0.001", "min_qty": "0.001", "max_qty": "1000"},
        notional_rules={"min_notional": "10", "max_notional": "1000000"},
    )
    assert snap.price_rules["tick_size"] == "0.10"
    with pytest.raises(ValueError):
        TradingRulesSnapshot(
            venue="BINANCE_SPOT", instrument_id="BTCUSDT",
            source_as_of_ms=None, known_at_ms=1, rule_version="v",
            raw_filters={}, order_types={},
            price_rules={"tick_size": 0.1}, lot_rules={}, notional_rules={},
        )


def test_onchain_quote_uses_atomic_integers_and_indicative_only():
    from diveintocrypto_desktop.shortlab.hedge.models import OnchainQuote
    quote = OnchainQuote(
        venue="ONCHAIN_DEX", canonical_id="ethereum", chain="ethereum",
        contract_address="0x" + "ab" * 20, as_of_ms=1_760_000_000_000,
        expires_at_ms=1_760_000_030_000, requested_canonical_qty="10.5",
        provider_id="ETHEREUM_0X_PRICE_V2", api_version="v2", chain_id=1,
        block_number=12345678,
        atomic_amounts={"buy_amount": "10500000", "sell_amount": "10500000"},
        decimals=6, quote_kind="INDICATIVE",
        buy_vwap="3200.5", sell_vwap="3199.5",
        buy_executable_qty="10.5", sell_executable_qty="10.5",
        gas_units="21000", gas_price="15.5", native_gas_fx="3200",
        estimated_gas_usd="1.05", estimated_fee_usd="2.5",
        token_tax_status="NO_TAX", route_complete=True,
        simulation_verified=False, quote_currency="USDC",
        quote_to_usd="1", fetched_at_ms=1_760_000_000_000,
        status="OK", reason_code=None,
    )
    assert quote.atomic_amounts["buy_amount"] == "10500000"
    # Float 10**decimals math is rejected (must be integer strings).
    with pytest.raises(ValueError):
        OnchainQuote(
            venue="ONCHAIN_DEX", canonical_id="ethereum", chain="ethereum",
            contract_address="0x" + "ab" * 20, as_of_ms=1, expires_at_ms=2,
            requested_canonical_qty="10.5", provider_id="ETHEREUM_0X_PRICE_V2",
            api_version="v2", chain_id=1, block_number=None,
            atomic_amounts={"buy_amount": 10500000.0}, decimals=6,
            fetched_at_ms=1,
        )
    # simulation_verified can never be True in V1.
    with pytest.raises(ValueError):
        dataclasses.replace(quote, simulation_verified=True)


def test_hedge_event_freezes_v1_schema_and_funding_receipt_shape():
    from diveintocrypto_desktop.shortlab.hedge.models import HedgeEvent
    fill = HedgeEvent(
        schema_version="hedge-event-v1", leg_type="SPOT_LONG",
        event_type="OPEN_SPOT_LONG", native_qty="1.5", canonical_qty="1.5",
        native_price="67000", price_currency="USDT", fee_currency="BNB",
        fee_amount="0.001", fee_usd="0.5", gas_usd=None, source="USER_ENTERED",
        executed_at_ms=1_760_000_000_000, gross_qty="1.5", net_qty="1.499",
    )
    assert fill.net_qty == "1.499"
    funding = HedgeEvent(
        schema_version="hedge-event-v1", leg_type="FUNDING",
        event_type="FUNDING_RECEIPT", native_qty=None, canonical_qty=None,
        native_price=None, price_currency=None, fee_currency=None,
        fee_amount=None, fee_usd=None, gas_usd=None, source="USER_ENTERED",
        executed_at_ms=1_760_000_000_000, amount="12.5", currency="USDT",
        public_funding_event_id="BTCUSDT:123",
    )
    assert funding.amount == "12.5"
    with pytest.raises(ValueError):
        HedgeEvent(
            schema_version="hedge-event-v1", leg_type="SPOT_LONG",
            event_type="FUNDING_RECEIPT", native_qty="1.0", canonical_qty="1.0",
            native_price=None, price_currency=None, fee_currency=None,
            fee_amount=None, fee_usd=None, gas_usd=None, source="USER_ENTERED",
            executed_at_ms=1, amount="1", currency="USDT",
            public_funding_event_id="x",
        )


def test_position_ledger_result_plan_state_alert_monitor_shapes():
    from diveintocrypto_desktop.shortlab.hedge.models import (
        DQResult, FCSResult, FundingMetrics, HedgeAlert, HedgeEvidenceSummary,
        HedgeMonitor, HedgeOutcome, HedgePosition, LedgerResult, OrderGuidance,
        PlanState, Readiness,
    )
    pos = HedgePosition(
        plan_id="plan-1", leg_type="SPOT_LONG", open_qty="0.1",
        closed_qty="0", remaining_qty="0.1", gross_qty="0.1", net_qty="0.1",
        weighted_avg_price="67000", event_ids=("e1",),
    )
    assert pos.remaining_qty == "0.1"
    ledger = LedgerResult(
        event_id="e1", plan_id="plan-1", plan_version=2, positions=(pos,),
        balance_source="CONFIRMED", estimated=False,
    )
    assert ledger.plan_version == 2
    state = PlanState(plan_id="plan-1", status="ACTIVE", plan_version=2,
                      mode="ABSOLUTE", updated_at_ms=1)
    assert state.status == "ACTIVE"
    alert = HedgeAlert(
        alert_id="a1", plan_id="plan-1", code="SPOT_EXIT_CAPACITY",
        severity="CRITICAL", state="OPEN", opened_at_ms=1, last_seen_at_ms=1,
        dedup_key="plan-1:SPOT_EXIT_CAPACITY:", episode=1,
        recommended_action="PAIR_EXIT", context_json={},
    )
    assert alert.episode == 1
    monitor = HedgeMonitor(
        snapshot_id="m1", plan_id="plan-1", as_of_ms=1,
        source_meta_json={}, quality_json={}, metrics_json={},
        status="OK", created_at_ms=1,
    )
    assert monitor.status == "OK"
    metrics = FundingMetrics(symbol="BTCUSDT", funding_30d="0.012")
    assert metrics.funding_30d == "0.012"
    fcs = FCSResult(
        snapshot_id="fcs-1", symbol="BTCUSDT", canonical_id="bitcoin",
        as_of_ms=1, fcs_version="fcs_v1", fcs_config_hash="x" * 64,
        reference_notional_usd="10000", fcs=82.5, module_scores={},
        funding_metrics={}, venue_summary={}, risk={}, readiness="READY",
        reasons=(), created_at_ms=1,
    )
    assert fcs.fcs == 82.5
    dq = DQResult(score=85.0, group_scores={}, field_credits={},
                  reasons=(), readiness="READY")
    assert dq.score == 85.0
    guide = OrderGuidance(
        leg="SPOT_LONG", venue="BINANCE_SPOT", instrument="BTCUSDT",
        side="BUY", order_type="LIMIT", qty="1.5", limit_price="67000",
    )
    assert guide.qty == "1.5"
    readiness = Readiness(value="READY", reasons=(), risk_validation="VERIFIED")
    assert readiness.value == "READY"
    outcome = HedgeOutcome(
        outcome_id="o1", fcs_snapshot_id="fcs-1", strategy="ABSOLUTE_100",
        horizon_days=30, outcome_status="PENDING", reason_code=None,
        evidence_version="hedge_evidence_v2", cost_config_hash="y" * 64,
        outcome_json={}, updated_at_ms=1,
    )
    assert outcome.horizon_days == 30
    summary = HedgeEvidenceSummary(
        strategy="ABSOLUTE_100", horizon_days=30, sample_count=10,
        complete_count=8, censored_count=1, avg_net_return="0.012",
    )
    assert summary.sample_count == 10


def test_historical_protocol_has_five_methods_and_frozen_types():
    from diveintocrypto_desktop.shortlab.hedge.models import (
        HistoricalFundingEvent, HistoricalLifecycle, HistoricalMarketProvider,
        HistoricalPriceBar,
    )
    for method in ("read_frozen_quote", "find_frozen_quote", "read_price_bars",
                   "read_settled_funding", "read_lifecycle"):
        assert hasattr(HistoricalMarketProvider, method), method
    bar = HistoricalPriceBar(
        symbol="BTCUSDT", open_ms=1, close_ms=2, native_open="67000",
        native_high="67100", native_low="66900", native_close="67050",
        quote_asset="USDT", fx_to_usd="1", source="binance", known_at_ms=3,
    )
    assert bar.native_close == "67050"
    funding = HistoricalFundingEvent(
        symbol="BTCUSDT", funding_time_ms=1, rate="0.0001",
        mark_price="67000", quote_asset="USDT", fx_to_usd="1",
        source="binance", known_at_ms=2,
    )
    assert funding.rate == "0.0001"
    life = HistoricalLifecycle(
        futures_symbol="BTCUSDT", cutoff_ms=10, onboard_at_ms=1,
        contract_type="PERPETUAL", exchange_status="TRADING", source_id="s1",
    )
    assert life.cutoff_ms == 10
