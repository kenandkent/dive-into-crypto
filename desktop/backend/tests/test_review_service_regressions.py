"""Production-path regressions from the implementation review."""
import pytest
from types import SimpleNamespace
from diveintocrypto_desktop.shortlab.service import ShortLabService
from diveintocrypto_desktop.shortlab.models import ProviderResult
from diveintocrypto_desktop.shortlab.config import load_shortlab_config

NOW = 1760000000000

@pytest.mark.asyncio
async def test_close_read_failure_never_closes_plan():
    class Repo:
        changed = False
        async def get_hedge_plan(self, pid):
            return {'plan_id': pid, 'plan_version': 1, 'status': 'ACTIVE'}
        async def aggregate_hedge_position(self, pid):
            raise RuntimeError('database read failed')
        async def update_hedge_plan(self, *args):
            self.changed = True
            return 2
        async def resolve_plan_alerts(self, *args):
            return 0
    repo = Repo()
    svc = ShortLabService(repository=repo, hedge_available=True)
    with pytest.raises(Exception, match='database read failed'):
        await svc.close('p', {'expectedVersion': 1})
    assert not repo.changed

@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['activate', 'close'])
@pytest.mark.parametrize('body', ['{broken', 'null', '[]'])
async def test_state_routes_reject_invalid_json(action, body):
    from test_shortlab_hedge_api import make_app
    from fastapi.testclient import TestClient
    class Service:
        called = False
        async def activate(self, *args):
            self.called = True
            return {'status': 'ACTIVE'}
        async def close(self, *args):
            self.called = True
            return {'status': 'CLOSED'}
    svc = Service()
    app = make_app(svc)
    with TestClient(app) as client:
        response = client.post(f'/api/short/hedge/plans/p/{action}', content=body,
                               headers={'content-type': 'application/json'})
    assert response.status_code == 422
    assert not svc.called

def test_current_price_and_ath_are_same_base_asset_units():
    svc = ShortLabService()
    identity = SimpleNamespace(contract_multiplier=1000, multiplier_source='MANUAL', mapping_confidence='VERIFIED')
    result = svc._build_inputs('1000PEPEUSDT', {'price': .01}, identity, {'quote_to_usd': '1'},
        {'ath_usd': .00002}, ProviderResult('UNAVAILABLE', 'spot', NOW, None, None, False, None, None),
        {}, {'windows': {30: {'complete': True}, 90: {}, 7: {}},
             'events': [{'t': NOW - 1, 'funding_rate': .0001}]}, NOW)
    assert result['current_price'] == pytest.approx(.00001)
    assert result['ath_price'] == pytest.approx(.00002)
    assert result['funding_rates_30d'] == [.0001]

def test_service_uses_frozen_observed_input_factory():
    from test_shortlab_production_inputs import _full_observations, _identity, ASOF_MS
    from diveintocrypto_desktop.shortlab.inputs import build_feature_inputs
    cfg = load_shortlab_config()
    svc = ShortLabService(config=cfg)
    observed = _full_observations()
    expected = build_feature_inputs('TESTUSDT', observed, _identity(), ASOF_MS, cfg)
    result = svc._build_inputs('TESTUSDT', {'price': 999999}, _identity(), {}, None,
        ProviderResult('UNAVAILABLE', 'spot', ASOF_MS, None, None, False, None, None),
        {'observations': observed}, {'windows': {}}, ASOF_MS)
    assert result['current_price'] == expected.current_price
    assert result['daily_closes'] == list(expected.daily_closes)
    assert result['funding_rates_30d'] == list(expected.funding_rates_30d)

@pytest.mark.asyncio
async def test_scoring_reuses_complete_funding_archive_without_send():
    day = 86400000
    events = [SimpleNamespace(funding_time_ms=t, funding_rate=.0001, mark_price=50)
              for t in range(NOW - 90 * day, NOW + 1, 8 * 3600000)]
    class Repo:
        async def list_funding_events(self, *args):
            return events
    calls = []
    async def fetch(*args):
        calls.append(args)
        return []
    svc = ShortLabService(repository=Repo(), funding_history_fn=fetch)
    svc._funding_window_start_ms = NOW
    svc._funding_window_used = 80
    result = await svc._fetch_funding('BTCUSDT', {}, NOW, NOW)
    assert result['windows'][30]['complete']
    assert result['requested'] == 0
    assert not calls

@pytest.mark.asyncio
async def test_entry_batch_uses_per_symbol_cutoffs(monkeypatch):
    from diveintocrypto_desktop.shortlab import entry
    seen = {}
    async def build(symbol, tf, **kwargs):
        seen[symbol] = kwargs['as_of_ms']
        return SimpleNamespace(symbol=symbol, entry_score=None, reason_code=None)
    monkeypatch.setattr(entry, 'build_entry_snapshot', build)
    await entry.run_entry_batch(['AUSDT', 'BUSDT'], as_of_ms=NOW,
                               as_of_by_symbol={'AUSDT': NOW-1000, 'BUSDT': NOW+1000})
    assert seen == {'AUSDT': NOW-1000, 'BUSDT': NOW+1000}

def test_usd_ath_is_not_divided_by_contract_multiplier():
    from test_shortlab_production_inputs import _full_observations, _identity, ASOF_MS, _obs
    from diveintocrypto_desktop.shortlab.inputs import build_feature_inputs
    observations = _full_observations()
    observations['ath'] = _obs({'ath_usd': .00002}, 'coingecko')
    observations['ticker_24h'] = _obs({'price': .01}, 'binance-ticker')
    observations['fx_rate'] = _obs('1', 'archived-fx')
    inputs = build_feature_inputs('1000PEPEUSDT', observations, _identity(multiplier=1000), ASOF_MS, load_shortlab_config())
    assert inputs.ath_price == .00002
    assert inputs.current_price == .00001

def test_missing_stablecoin_fx_does_not_assume_dollar_peg():
    from test_shortlab_production_inputs import _full_observations, _identity, ASOF_MS
    from diveintocrypto_desktop.shortlab.inputs import build_feature_inputs
    observations = _full_observations()
    observations.pop('fx_rate', None)
    inputs = build_feature_inputs('TESTUSDT', observations, _identity(), ASOF_MS, load_shortlab_config())
    assert inputs.current_price is None

@pytest.mark.asyncio
async def test_new_decision_recomputes_frozen_score_and_entry_at_same_final_cutoff(tmp_path):
    from test_shortlab_runtime import FakeClock, open_repo, make_service, FakeCoinGecko, make_overrides
    from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
    clock = FakeClock(NOW)
    repo = await open_repo(tmp_path, clock)
    registry = ProviderRegistry()
    registry.register('coingecko', FakeCoinGecko(clock))
    svc, _ = make_service(repo, clock, n=1, registry=registry)
    svc._identity_overrides = make_overrides(['T000USDT'], coingecko_id='bitcoin')
    rows = await svc._universe_fn()
    outcome = await svc._score_symbol('T000USDT', rows[0], {'T000USDT'}, await svc._metadata_fn(), NOW, 'LITE')
    clock.advance(5000)
    final = svc._refreeze_scored_outcome(outcome, clock())
    assert final['decision_cutoff_ms'] == clock()
    assert final['feature_record'].as_of_ms == clock()
    assert final['feature_record'].snapshot_id != outcome['feature_record'].snapshot_id
    assert final['breakdown'].ltss is not None
    await repo.close()

@pytest.mark.asyncio
async def test_simulation_archives_identity_quote_and_exact_lot_quantity(tmp_path):
    from test_shortlab_hedge_api import make_service, FakeClock, open_repo, _simulate_body
    clock = FakeClock(NOW)
    repo = await open_repo(tmp_path)
    svc = make_service(repo, clock)
    seen = []
    original_quote = svc._hedge_quote_fn
    async def quote(symbol, qty, venue):
        seen.append(qty)
        result = await original_quote(symbol, qty, venue)
        result.update(requested_canonical_qty=qty, snapshot_id='actual-quote')
        return result
    svc._hedge_quote_fn = quote
    svc._hedge_futures_rules_fn = lambda: {'lot_rules': {'step_size':'0.001'}}
    svc._hedge_spot_rules_fn = lambda: {'lot_rules': {'step_size':'0.001'}}
    result = await svc.simulate(_simulate_body())
    assert seen == ['0.149']
    saved = await repo.get_hedge_simulation(result['simulationId'])
    assert saved['source_meta_json']['identity']['contract_multiplier'] == '1'
    assert await repo.read_frozen_quote('actual-quote') is not None
    # Later identity changes must not change the plan's frozen contract unit.
    svc._hedge_identity_fn = lambda s: {'canonical_id':'bitcoin','contract_multiplier':'1000','multiplier_source':'MANUAL'}
    plan = await svc.save_plan({'simulationId':result['simulationId'],'clientRequestId':'frozen-units'})
    row = await repo.get_hedge_plan(plan['planId'])
    assert row['plan_config_json']['contract_multiplier'] == '1'
    await repo.close()

@pytest.mark.asyncio
async def test_relative_simulation_uses_frozen_ratio_contract(tmp_path):
    from test_shortlab_hedge_api import make_service, FakeClock, open_repo, _simulate_body
    repo = await open_repo(tmp_path)
    svc = make_service(repo, FakeClock(NOW))
    result = await svc.simulate(_simulate_body(mode='RELATIVE', hedgeRatio='0.5'))
    assert result['result']['targetHedgeRatio'] == '0.5'
    await repo.close()

@pytest.mark.asyncio
async def test_monitor_reads_latest_memory_without_waiting_for_database():
    from diveintocrypto_desktop.shortlab.hedge.models import HedgeMonitor
    class Repo:
        async def get_hedge_plan(self, pid):
            raise AssertionError('must not query busy DB on memory monitor path')
    svc = ShortLabService(repository=Repo(), hedge_available=True)
    snap = HedgeMonitor('m', 'p', NOW, mark_price='102', status='MONITOR_DEGRADED')
    svc._hedge_jobs = SimpleNamespace(mirror={'p': {'plan': {'plan_id':'p'}, 'positions': [],
                                      'alerts': {}, 'previous': snap}})
    response = await svc.monitor('p')
    assert response['markPrice'] == '102'
    assert response['status'] == 'MONITOR_DEGRADED'

@pytest.mark.asyncio
async def test_scheduled_monitor_status_does_not_require_per_tick_job_row(tmp_path):
    from test_shortlab_hedge_api import make_service, FakeClock, open_repo
    from diveintocrypto_desktop.shortlab.service import JOB_TYPE_HEDGE_MONITOR
    repo = await open_repo(tmp_path)
    svc = make_service(repo, FakeClock(NOW))
    status = await svc.run_refresh(JOB_TYPE_HEDGE_MONITOR)
    assert status.status == 'SUCCEEDED'
    assert (await svc.job_status(status.job_id)).status == 'SUCCEEDED'
    assert await repo.get_job_run(status.job_id) is None
    await repo.close()

@pytest.mark.asyncio
async def test_scanner_collection_uses_runtime_shared_send_budget(tmp_path):
    from test_shortlab_hedge_api import open_repo
    from diveintocrypto_desktop.shortlab.request_budget import RequestBudget, get_current_request_context
    repo = await open_repo(tmp_path)
    budget = RequestBudget()
    seen = []
    async def universe(limit):
        seen.append(get_current_request_context())
        return []
    svc = ShortLabService(repository=repo, request_budget=budget, universe_fn=universe,
                         metadata_fn=lambda: {})
    await svc.run_refresh()
    assert seen[0] is not None
    assert seen[0].budget is budget
    assert seen[0].job_type == 'score_refresh'
    await repo.close()
