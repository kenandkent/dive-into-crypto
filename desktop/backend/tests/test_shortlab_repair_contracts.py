"""R00 contracts: Gate/Decimal/strict-JSON/ports freeze (D03/D18/D19)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest


def test_unknown_gate_is_not_pass():
    from diveintocrypto_desktop.shortlab.repair_contracts import GateResult

    gate = GateResult('UNKNOWN', ('FUNDING_SCHEDULE_UNKNOWN',), 1000, {})
    assert gate.status != 'PASS'
    assert Decimal('0.000001') * Decimal('1000') == Decimal('0.001')


def test_decimal_precision_and_canonical():
    from diveintocrypto_desktop.shortlab.repair_contracts import (
        decimal_to_str,
        normalize_decimal_str,
    )

    assert Decimal('0.000001') * Decimal('1000') == Decimal('0.001')
    assert normalize_decimal_str('-0.000') == '0'
    assert normalize_decimal_str('1.5000') == '1.5'
    assert normalize_decimal_str('100.00') == '100'
    # No exponent in canonical form.
    assert 'e' not in normalize_decimal_str('0.000001').lower()
    assert decimal_to_str(Decimal('10.2500')) == '10.25'
    with pytest.raises(ValueError):
        normalize_decimal_str('NaN')
    with pytest.raises(ValueError):
        normalize_decimal_str('Infinity')


def test_load_json_strict_rejects_duplicates_and_nan():
    from diveintocrypto_desktop.shortlab.repair_contracts import load_json_strict

    assert load_json_strict('{"a":1}') == {"a": 1}
    with pytest.raises(ValueError):
        load_json_strict('{"a":1,"a":2}')
    with pytest.raises(ValueError):
        load_json_strict('{"a":NaN}')
    with pytest.raises(ValueError):
        load_json_strict('{"a":Infinity}')
    with pytest.raises(ValueError):
        load_json_strict('"scalar"')


def test_gate_reasons_sorted_deduped_and_frozen():
    from diveintocrypto_desktop.shortlab.repair_contracts import GateResult

    gate = GateResult('FAIL', ('B', 'A', 'A'), 5, {'funding': 'f1'})
    assert gate.reasons == ('A', 'B')
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.status = 'PASS'  # type: ignore[misc]
    # Nested mapping cannot be mutated through the DTO.
    refs = gate.input_refs
    assert isinstance(refs, dict)
    refs['evil'] = 'x'
    assert 'evil' not in GateResult('FAIL', ('A',), 5, {'funding': 'f1'}).input_refs


def test_repair_contract_version_and_record_roundtrip():
    from diveintocrypto_desktop.shortlab.repair_contracts import (
        REPAIR_CONTRACT_VERSION,
        GateResult,
        from_record_dict,
        to_record_dict,
    )

    assert REPAIR_CONTRACT_VERSION == 'repair-contract-v1'
    gate = GateResult('PASS', (), 1000, {})
    record = to_record_dict(gate)
    assert record['schema_version'] == 'repair-contract-v1'
    rebuilt = from_record_dict(GateResult, record)
    assert rebuilt == gate
    with pytest.raises(ValueError):
        from_record_dict(GateResult, {'status': 'PASS'})


def test_reexports_are_not_synonyms():
    # R00 must re-export existing types, never define same-named duplicates.
    from diveintocrypto_desktop.shortlab import models as shortlab_models
    from diveintocrypto_desktop.shortlab.hedge import models as hedge_models
    from diveintocrypto_desktop.shortlab import observations as obs
    from diveintocrypto_desktop.shortlab import request_budget as rb
    from diveintocrypto_desktop.shortlab import repair_contracts as rc

    assert rc.AssetIdentity is shortlab_models.AssetIdentity
    assert rc.FundingMetrics is hedge_models.FundingMetrics
    assert rc.TradingRulesSnapshot is hedge_models.TradingRulesSnapshot
    assert rc.SpotVenueQuote is hedge_models.SpotVenueQuote
    assert rc.Observed is obs.Observed
    assert rc.RequestContext is rb.RequestContext


def test_repair_ports_nine_keys_and_require():
    from diveintocrypto_desktop.shortlab.repair_ports import (
        REPAIR_PORT_KEYS,
        RepairDependencyUnavailable,
        RepairPorts,
    )

    assert len(REPAIR_PORT_KEYS) == 9
    assert set(REPAIR_PORT_KEYS) == {
        'compute_schedule_coverage', 'evaluate_funding_entry_gate',
        'build_ratio_proposal', 'compute_ledger_pnl',
        'build_pair_exit_guidance', 'project_opportunity',
        'capture_strategy_entries', 'collect_due_quotes', 'simulate_hedge',
    }
    ports = RepairPorts()
    for key in REPAIR_PORT_KEYS:
        assert getattr(ports, key) is None
        with pytest.raises(RepairDependencyUnavailable):
            ports.require(key)
    with pytest.raises(ValueError):
        ports.require('nope')


def test_repository_port_has_20_methods_and_market_3():
    from diveintocrypto_desktop.shortlab.repair_ports import MarketPort, RepositoryPort

    repo_methods = [m for m in dir(RepositoryPort) if not m.startswith('_')]
    # Protocol exposes exactly the 20 D13.1 async methods (incl. 2 budget).
    expected = {
        'save_market_observation', 'get_market_observation',
        'list_market_observations', 'list_funding_observations',
        'save_funding_schedule', 'list_funding_schedules',
        'save_fx_observation', 'get_fx_at',
        'save_hedge_decision', 'get_hedge_decision',
        'save_protection_confirmation', 'get_protection_confirmation',
        'reserve_provider_request', 'finish_provider_request',
        'list_current_funding_opportunities',
        'save_strategy_entry', 'list_strategy_entries',
        'save_strategy_quote_task', 'claim_due_quote_tasks', 'finish_quote_task',
    }
    assert expected.issubset(set(repo_methods)), set(repo_methods)
    assert len(expected) == 20
    market_methods = {m for m in dir(MarketPort) if not m.startswith('_')}
    assert {'collect_futures', 'collect_spot', 'collect_funding'}.issubset(market_methods)


def test_make_ports_defaults_and_overrides():
    from tests.repair_fixtures import make_ports
    from diveintocrypto_desktop.shortlab.repair_ports import REPAIR_PORT_KEYS

    ports = make_ports()
    for key in REPAIR_PORT_KEYS:
        assert callable(getattr(ports, key))
    # Override one key keeps the other eight.
    sentinel = lambda *a, **k: None
    ports2 = make_ports(build_ratio_proposal=sentinel)
    assert ports2.build_ratio_proposal is sentinel
    assert callable(ports2.compute_schedule_coverage)
    # None is an explicit unbound test.
    ports3 = make_ports(compute_schedule_coverage=None)
    assert ports3.compute_schedule_coverage is None
    with pytest.raises(ValueError):
        make_ports(unknown_key=sentinel)
    with pytest.raises(ValueError):
        make_ports(compute_schedule_coverage='not-callable')
    # Fresh instances each call (no shared mutable state).
    assert make_ports() is not make_ports()


def test_default_fixtures_meme_full_valid_conformance():
    from tests.repair_fixtures import (
        make_decision_context,
        make_decision_request,
        make_event_fx,
        make_events,
        make_market_context,
        make_ports,
    )

    ctx = make_decision_context()
    req = make_decision_request()
    assert req.symbol == '1000PEPEUSDT'
    assert ctx.funding_context.history_class == 'FULL_90D'
    assert ctx.identity.binance_futures_symbol == '1000PEPEUSDT'
    # Liquidation well above 2x Mark (Mark 0.01, UP_100 -> 0.02 < 0.025).
    mark = float(ctx.futures_mark.value['price'])
    liq = float(req.liquidation_price)
    assert liq > 2 * mark
    # Five ratios, h1 legal, positive net Carry, UP_100 below liquidation.
    ports = make_ports()
    for target in ('0', '0.25', '0.5', '0.75', '1'):
        proposal = ports.require('build_ratio_proposal')(req, ctx, target, {})
        assert proposal.target_ratio == target
    full = ports.require('build_ratio_proposal')(req, ctx, '1', {})
    assert full.spot_venue == 'BINANCE_SPOT'
    assert full.economics is not None and full.economics.net_carry_usd is not None
    assert float(full.economics.net_carry_usd) > 0
    up100 = next(s for s in full.scenarios if s.scenario_id == 'UP_100')
    assert up100.status == 'VALID'
    # Events/FX/market share the same partial_close set, FX1, explicit zero fees.
    events = make_events()
    fx = make_event_fx()
    market = make_market_context()
    assert len(events) == 4
    assert set(fx) == {e['event_id'] for e in events}
    assert market['futures_quote_fx'] == '1'
    assert market['estimated_exit_fee_usd'] == '0'


def test_fake_gate_matrix_and_unknown_case():
    from tests.repair_fixtures import make_funding_context, make_ports

    ports = make_ports()
    gate_fn = ports.require('evaluate_funding_entry_gate')
    assert gate_fn(make_funding_context('valid'), {}, 1).status == 'PASS'
    assert gate_fn(make_funding_context('negative'), {}, 1).status == 'FAIL'
    unknown = gate_fn(make_funding_context('unknown'), {}, 1)
    assert unknown.status == 'UNKNOWN'
    assert unknown.status != 'PASS'
