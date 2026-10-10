import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from diveintocrypto_desktop.shortlab.hedge.jobs import HedgeJobs

@pytest.mark.asyncio
async def test_ticks_reuse_mirror_and_never_fabricate_prices():
    repo = SimpleNamespace(list_hedge_plans=AsyncMock(return_value=[{'plan_id':'p','symbol':'BTCUSDT','status':'ACTIVE'}]), aggregate_hedge_position=AsyncMock(return_value=[]), latest_hedge_monitor=AsyncMock(return_value=None), list_hedge_alerts=AsyncMock(return_value=[]), list_funding_events=AsyncMock(return_value=[]), save_monitor_snapshot=AsyncMock(), upsert_hedge_alert=AsyncMock(return_value={"alert_id":"mock-a","last_seen_at_ms":100000}), resolve_hedge_alert=AsyncMock())
    service = SimpleNamespace(_repository=repo, _config=None, _now=lambda:100000, _ensure_hedge_available=AsyncMock(), _hedge_lock_for=lambda _:asyncio.Lock(), _hedge_market=SimpleNamespace(collect=AsyncMock(return_value={})))
    jobs = HedgeJobs(service)
    ctx = SimpleNamespace(trace_id='t',clock_ms=lambda:100000)
    await jobs.monitor(ctx)
    await jobs.monitor(ctx)
    assert repo.list_hedge_plans.await_count == 3
    assert repo.aggregate_hedge_position.await_count == 1
    snap=repo.save_monitor_snapshot.call_args.args[0]
    assert snap['mark_price'] is None and snap['spot_price'] is None
    assert snap['status'] == 'MONITOR_DEGRADED'

@pytest.mark.asyncio
async def test_alert_recovery_requires_two_fresh_ticks_after_restart():
    from diveintocrypto_desktop.shortlab.hedge.models import HedgeMonitor
    repo=SimpleNamespace(resolve_hedge_alert=AsyncMock(),upsert_hedge_alert=AsyncMock())
    service=SimpleNamespace(_repository=repo,_config=None)
    jobs=HedgeJobs(service)
    key='p:FUNDING_TURNED_NON_POSITIVE:'
    jobs.mirror['p']={'alerts':{key:{'alert_id':'a','state':'ACKNOWLEDGED','last_seen_at_ms':0}}}
    snap=HedgeMonitor('m','p',100000,metrics_json={'funding_current_rate':'0.01'})
    await jobs._alerts('p',None,snap,100000)
    repo.resolve_hedge_alert.assert_not_awaited()
    stale=HedgeMonitor('s','p',110000,metrics_json={'funding_current_rate':'0.01','futures_mark_expired':True})
    await jobs._alerts('p',snap,stale,110000)
    await jobs._alerts('p',stale,snap,120000)
    repo.resolve_hedge_alert.assert_not_awaited()
    await jobs._alerts('p',snap,snap,130000)
    repo.resolve_hedge_alert.assert_awaited_once_with('a',130000)


def test_settlement_no_historical_fx_does_not_assume_one():
    jobs=HedgeJobs(SimpleNamespace())
    state={'plan':{'contract_multiplier':'1000'},'fills':[{'event_id':'a','event_type':'OPEN','leg_type':'FUTURES_SHORT','native_qty':'2','native_price':'0.1','executed_at_ms':1}], 'quotes':[]}
    event=jobs._settled_inputs(state,[{'funding_time_ms':100,'funding_rate':0.01,'mark_price':0.1}])[0]
    assert event['short_qty']=='2'
    assert 'fx_to_usd' not in event

@pytest.mark.asyncio
async def test_opportunity_persists_real_fcs_and_quote_for_shortlist():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics
    cfg=load_shortlab_config()
    repo=SimpleNamespace(list_funding_events=AsyncMock(return_value=[]),save_funding_capture_snapshot=AsyncMock(),save_spot_venue_snapshot=AsyncMock())
    svc=SimpleNamespace(_repository=repo,_config=cfg,_ensure_hedge_available=AsyncMock(),_universe_fn=lambda n:[{'symbol':'1000PEPEUSDT','quote_volume':100}],_hedge_identity_for=AsyncMock(return_value={'canonical_id':'pepe','contract_multiplier':1000,'identity_confidence':'VERIFIED'}),_hedge_mark_for=AsyncMock(return_value={'price':'0.00001','canonical_price_usd':'0.00001'}),_hedge_funding_for=AsyncMock(return_value=FundingMetrics('1000PEPEUSDT')),_hedge_quote_for=AsyncMock(return_value={'snapshot_id':'q','venue':'BINANCE_SPOT','mid_price':'0.00001','as_of_ms':100000,'fetched_at_ms':100000,'status':'OK'}))
    jobs=HedgeJobs(svc)
    result=await jobs.opportunity(SimpleNamespace(trace_id='t',clock_ms=lambda:100000))
    assert result.stats=={'computed':1,'failed':0}
    assert svc._hedge_quote_for.call_args.args[1]=='1.0000E+9'
    record=repo.save_funding_capture_snapshot.call_args.args[0]
    assert record['fcs'] is None
    assert record['readiness']=='NOT_READY'
    repo.save_spot_venue_snapshot.assert_awaited_once()

@pytest.mark.asyncio
async def test_database_failure_marks_monitor_degraded_without_closing_plan():
    repo = SimpleNamespace(list_hedge_plans=AsyncMock(side_effect=lambda **kw: [{'plan_id':'p','symbol':'BTCUSDT','status':'ACTIVE'}] if kw['status']=='ACTIVE' else []), aggregate_hedge_position=AsyncMock(return_value=[]), latest_hedge_monitor=AsyncMock(return_value=None), list_hedge_alerts=AsyncMock(return_value=[]), save_monitor_snapshot=AsyncMock(side_effect=TimeoutError), upsert_hedge_alert=AsyncMock(return_value={"alert_id":"mock-a","last_seen_at_ms":100000}), resolve_hedge_alert=AsyncMock())
    service=SimpleNamespace(_repository=repo,_config=None,_ensure_hedge_available=AsyncMock(),_hedge_lock_for=lambda _:asyncio.Lock(),_hedge_market=SimpleNamespace(collect=AsyncMock(return_value={})))
    jobs=HedgeJobs(service)
    result=await jobs.monitor(SimpleNamespace(trace_id='t',clock_ms=lambda:100000))
    assert result.stats['degraded']==1
    assert jobs.mirror['p']['plan']['status']=='ACTIVE'
    assert jobs.mirror['p']['previous'].metrics_json['persist_lag_ms']==5001

@pytest.mark.asyncio
async def test_real_repository_jobs_persist_prices_fcs_and_negative_funding_alert(tmp_path):
    from dataclasses import replace
    from test_shortlab_hedge_repository import _sim, _plan, _open_spot, NOW
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.hedge.funding_score import compute_funding_metrics
    repo=await ShortLabRepository.open(tmp_path/'jobs.duckdb')
    await repo.migrate(target_version=5)
    clock=[NOW]
    events=[{'funding_time_ms':NOW-i*8*3600000,'funding_rate':0.0001,'mark_price':100} for i in range(271)]
    for e in events:
        await repo.upsert_funding_events([dict(e,symbol='BTCUSDT')])
    quote={'snapshot_id':'q','venue':'BINANCE_SPOT','venue_symbol':'BTCUSDT','mid_price':'100','buy_vwap':'100.01','sell_vwap':'99.99','buy_executable_qty':'100','sell_executable_qty':'100','requested_canonical_qty':'100','exit_feasibility':'CONFIRMED','quote_to_usd':'0.99','as_of_ms':NOW,'fetched_at_ms':NOW,'expires_at_ms':NOW+60000,'status':'OK'}
    mark={'price':'100','canonical_price_usd':'99','next_funding_time_ms':NOW+1000}
    identity={'canonical_id':'bitcoin','contract_multiplier':'1','identity_confidence':'VERIFIED','onboard_at_ms':NOW-400*86400000,'reliable':True}
    funding=compute_funding_metrics(events,[],NOW,identity,symbol='BTCUSDT',current_rate='0.0001')
    async def info(): return {'symbols':[{'symbol':'BTCUSDT','status':'TRADING','deliveryDate':NOW+86400000}]}
    async def collect(*args):
        now=clock[0]
        return {'futures_mark':{'price':'102','quote_to_usd':'0.99','as_of_ms':now,'fetched_at_ms':now,'expires_at_ms':now+60000},'spot_quote':{**quote,'as_of_ms':now,'fetched_at_ms':now,'expires_at_ms':now+60000},'funding_metrics':replace(funding,current_rate='-0.0001')}
    cfg=load_shortlab_config()
    from diveintocrypto_desktop.shortlab.service import ShortLabService
    from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
    svc=ShortLabService(config=cfg,repository=repo,registry=ProviderRegistry(),clock=lambda:clock[0],universe_fn=lambda n:[{'symbol':'BTCUSDT','quote_volume':1}],hedge_identity_fn=AsyncMock(return_value=identity),hedge_mark_fn=AsyncMock(return_value=mark),hedge_quote_fn=AsyncMock(return_value=quote),hedge_funding_fn=AsyncMock(return_value=funding),hedge_available=True)
    svc._hedge_market=SimpleNamespace(_exchange_info=info,collect=collect)
    jobs=HedgeJobs(svc); ctx=SimpleNamespace(trace_id='real',clock_ms=lambda:clock[0])
    try:
        result=await svc.run_hedge_opportunity(ctx)
        assert result.stats['computed']==1
        saved=await repo.list_fcs('BTCUSDT')
        assert saved[0]['fcs'] is not None
        await repo.save_hedge_simulation(_sim())
        plan=_plan()
        plan['plan_config_json'].update(contract_multiplier='1000',simulation_input={'liquidation_price':'110000','liquidation_price_source':'USER_ENTERED','liquidation_price_updated_at_ms':NOW})
        await repo.create_hedge_plan(plan)
        await repo.append_hedge_fill_event('plan-1','spot',1,_open_spot('1','100'))
        await repo.append_hedge_fill_event('plan-1','future',2,{**_open_spot('1','100'),'leg_type':'FUTURES_SHORT','event_type':'OPEN_FUTURES_SHORT'})
        await repo.update_hedge_plan('plan-1','ACTIVE',3,NOW)
        result=await svc.run_hedge_monitor(ctx)
        assert result.stats['persisted']==1
        snapshot=await repo.latest_hedge_monitor('plan-1')
        assert snapshot['mark_price']==102
        assert snapshot['created_at_ms']==NOW
        assert float(snapshot['liquidation_distance'])==pytest.approx((110-102)/102)
        assert jobs.mirror['plan-1']['plan']['contract_multiplier']=='1000'
        alerts=await repo.list_hedge_alerts('plan-1','OPEN')
        assert any(a['code']=='FUNDING_TURNED_NON_POSITIVE' for a in alerts)
        clock[0]+=10000
        result=await svc.run_hedge_monitor(ctx)
        assert result.stats['persisted']==0
    finally: await repo.close()

def test_settlement_requires_fx_source_and_known_time_not_quote_timestamp():
    jobs=HedgeJobs(SimpleNamespace())
    state={'plan':{'contract_multiplier':'1000'},'fills':[], 'quotes':[{'quote_to_usd':'0.98','as_of_ms':1000,'fetched_at_ms':1000,'capabilities':{'fx':{'source_as_of_ms':1000,'known_at_ms':1001,'currency':'USDT'}}}]}
    assert 'fx_to_usd' not in jobs._settled_inputs(state,[{'funding_time_ms':1000,'funding_rate':'.001','mark_price':'.01'}])[0]
    state['quotes'][0]['capabilities']['fx']['known_at_ms']=999
    assert jobs._settled_inputs(state,[{'funding_time_ms':1000,'funding_rate':'.001','mark_price':'.01'}])[0]['fx_to_usd']=='0.98'

@pytest.mark.asyncio
async def test_failed_mirror_rehydrate_exposes_degraded_memory_not_old_safe_snapshot():
    from diveintocrypto_desktop.shortlab.hedge.models import HedgeMonitor
    repo = SimpleNamespace(list_hedge_plans=AsyncMock(side_effect=TimeoutError('queue unavailable')))
    svc = SimpleNamespace(_repository=repo, _config=None, _ensure_hedge_available=AsyncMock())
    jobs=HedgeJobs(svc)
    jobs.mirror['p'] = {'plan': {'plan_id':'p','status':'ACTIVE'}, 'positions': [],
                       'previous': HedgeMonitor('old','p',100000,mark_price='100',status='OK')}
    jobs.invalidate('p')
    result = await jobs.monitor(SimpleNamespace(trace_id='t',clock_ms=lambda:110000))
    assert result.stats['degraded'] == 1
    assert jobs.mirror['p']['previous'].status == 'MONITOR_DEGRADED'
    assert jobs.mirror['p']['previous'].mark_price is None
    assert jobs.mirror['p']['plan']['status'] == 'ACTIVE'


@pytest.mark.asyncio
async def test_opportunity_writes_projection_v2_queryable_without_manual_seed(tmp_path):
    """CR11 (D08/R09): real opportunity job persists frozen projection_v2.

    No manual FCS/projection seed: the job calls the single frozen
    projection implementation and the new row is queryable via the current
    API with Gate/hash/term/expiry (never LEGACY/stale by omission).
    """
    import json
    from diveintocrypto_desktop.shortlab.config import fcs_config_hash, load_shortlab_config
    from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics
    from diveintocrypto_desktop.shortlab.repair_contracts import OpportunityQuery
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    NOW = 1791417600000
    repo = await ShortLabRepository.open(tmp_path / "cr11.duckdb")
    await repo.migrate(target_version=6)
    try:
        cfg = load_shortlab_config()
        expected_hash = fcs_config_hash(cfg)
        identity = {'canonical_id': 'bitcoin', 'contract_multiplier': '1',
                    'identity_confidence': 'VERIFIED',
                    'onboard_at_ms': NOW - 400 * 86400000, 'reliable': True}
        mark = {'price': '100', 'canonical_price_usd': '100',
                'next_funding_time_ms': NOW + 1000, 'expires_at_ms': NOW + 30000}
        quote = {'venue': 'BINANCE_SPOT', 'status': 'OK', 'mid_price': '100',
                 'buy_vwap': '100.01', 'sell_vwap': '99.99',
                 'buy_executable_qty': '1000', 'sell_executable_qty': '1000',
                 'exit_feasibility': 'CONFIRMED',
                 'as_of_ms': NOW, 'fetched_at_ms': NOW, 'expires_at_ms': NOW + 60000}
        funding = FundingMetrics(symbol='BTCUSDT', funding_7d='0.01', funding_30d='0.05',
                                 funding_90d='0.10', positive_ratio_30d='0.9',
                                 positive_ratio_90d='0.9', coverage_30d='1.0',
                                 coverage_90d='1.0', conservative_apr='0.5',
                                 history_coverage='1.0')
        svc = SimpleNamespace(_repository=repo, _config=cfg,
                              _ensure_hedge_available=AsyncMock(),
                              _universe_fn=lambda n: [{'symbol': 'BTCUSDT', 'quote_volume': 1}],
                              _hedge_identity_for=AsyncMock(return_value=dict(identity)),
                              _hedge_mark_for=AsyncMock(return_value=dict(mark)),
                              _hedge_funding_for=AsyncMock(return_value=funding),
                              _hedge_quote_for=AsyncMock(return_value=dict(quote)))
        jobs = HedgeJobs(svc)
        result = await jobs.opportunity(SimpleNamespace(trace_id='t', clock_ms=lambda: NOW))
        assert result.stats == {'computed': 1, 'failed': 0}
        # Raw row carries the frozen envelope (identity + projection_v2).
        rows = await repo.list_fcs('BTCUSDT')
        assert len(rows) == 1
        risk = json.loads(rows[0]['risk_json']) if isinstance(rows[0]['risk_json'], str) else dict(rows[0]['risk_json'])
        assert set(risk) == {'identity', 'projection_v2'}
        assert risk['identity']['canonical_id'] == 'bitcoin'
        proj = dict(risk['projection_v2'])
        for key in ('snapshot_id', 'symbol', 'canonical_id', 'as_of_ms', 'expires_at_ms',
                    'stale', 'fcs', 'fcs_config_hash', 'funding_7d', 'funding_30d',
                    'positive_ratio_30d', 'history_class', 'best_venue', 'break_even_days',
                    'conservative_apr', 'readiness_breakdown', 'readiness', 'reasons'):
            assert key in proj, key
        # Gate/hash/term/expiry are real frozen values (never invented).
        assert proj['fcs_config_hash'] == expected_hash
        assert rows[0]['fcs_config_hash'] == expected_hash
        assert proj['history_class'] == 'FULL_90D'
        assert proj['funding_7d'] == '0.01' and proj['funding_30d'] == '0.05'
        assert proj['best_venue'] == 'BINANCE_SPOT'
        assert proj['expires_at_ms'] == NOW + 30000
        bd = proj['readiness_breakdown']
        assert set(bd) >= {'funding_gate', 'execution_gate', 'economic_gate', 'readiness'}
        # Current API without manual seed: new row is visible and legal.
        page = await repo.list_current_funding_opportunities(
            OpportunityQuery(symbol='BTCUSDT'), as_of_ms=NOW + 1000)
        assert page.total == 1 and len(page.items) == 1
        item = dict(page.items[0])
        assert item['snapshot_id'] == 'BTCUSDT:fcs:%d' % NOW
        assert item['stale'] is False
        assert 'LEGACY' not in list(item.get('reasons') or [])
        assert item['fcs_config_hash'] == expected_hash
        assert item['best_venue'] == 'BINANCE_SPOT'
        assert item['expires_at_ms'] == NOW + 30000
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# CR14 (D11/D19.3): Jobs caller context — _request_context_for preserves
# job_id/deadline (never dropped after verification); monitor uses monitor,
# venue uses scanner, opportunity uses opportunity (BACKGROUND) and forwards
# the immutable context to market so mixed loads bill real family/job_type
# and background never eats the monitor reserve.
# ---------------------------------------------------------------------------

def test_cr14_request_context_preserves_job_id_and_deadline():
    from diveintocrypto_desktop.shortlab.request_budget import RequestBudget
    svc = SimpleNamespace(_repository=SimpleNamespace(), _config=None)
    jobs = HedgeJobs(svc)
    mon_ctx = SimpleNamespace(trace_id='m', clock_ms=lambda: 1_791_417_600_000,
                              request_budget=RequestBudget(max_sends=240))
    rc = jobs._request_context_for(mon_ctx, job_type='monitor', job_id='hedge_monitor')
    assert rc.job_type == 'monitor'
    assert rc.job_id == 'hedge_monitor'
    assert rc.deadline_ms == 1_791_417_600_000 + 8000
    assert rc.trace_id == 'm'
    ev = jobs._request_context_for(mon_ctx, job_type='evidence', job_id='strategy_capture')
    assert ev.job_type == 'evidence' and ev.job_id == 'strategy_capture'
    assert ev.deadline_ms == 1_791_417_600_000 + 8000
    opp = jobs._request_context_for(mon_ctx, job_type='opportunity',
                                    job_id='funding_capture_refresh')
    assert opp.job_type == 'opportunity' and opp.job_id == 'funding_capture_refresh'
    from diveintocrypto_desktop.shortlab.request_budget import budget_class
    assert budget_class('monitor') == 'MONITOR'
    assert budget_class('opportunity') == 'BACKGROUND'
    assert budget_class('scanner') == 'SCANNER'
    assert budget_class('evidence') == 'BACKGROUND'


@pytest.mark.asyncio
async def test_cr14_opportunity_forwards_caller_context_to_market():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics
    cfg = load_shortlab_config()
    seen: dict = {}

    async def _fake_exchange_info(request_context=None):
        seen['jt'] = getattr(request_context, 'job_type', None)
        seen['jid'] = getattr(request_context, 'job_id', None)
        seen['dl'] = getattr(request_context, 'deadline_ms', None)
        return {'symbols': []}

    repo = SimpleNamespace(list_funding_events=AsyncMock(return_value=[]),
                           save_funding_capture_snapshot=AsyncMock(),
                           save_spot_venue_snapshot=AsyncMock())
    svc = SimpleNamespace(_repository=repo, _config=cfg,
                          _ensure_hedge_available=AsyncMock(),
                          _universe_fn=lambda n: [{'symbol': '1000PEPEUSDT', 'quote_volume': 1}],
                          _hedge_identity_for=AsyncMock(return_value={
                              'canonical_id': 'pepe', 'contract_multiplier': 1000,
                              'identity_confidence': 'VERIFIED'}),
                          _hedge_mark_for=AsyncMock(return_value={
                              'price': '0.00001', 'canonical_price_usd': '0.00001'}),
                          _hedge_funding_for=AsyncMock(return_value=FundingMetrics('1000PEPEUSDT')),
                          _hedge_quote_for=AsyncMock(return_value={
                              'snapshot_id': 'q', 'venue': 'BINANCE_SPOT',
                              'mid_price': '0.00001', 'as_of_ms': 100000,
                              'fetched_at_ms': 100000, 'status': 'OK'}),
                          _hedge_market=SimpleNamespace(_exchange_info=_fake_exchange_info))
    jobs = HedgeJobs(svc)
    ctx = SimpleNamespace(trace_id='t', clock_ms=lambda: 100000, request_budget=None)
    result = await jobs.opportunity(ctx, job_id='funding_capture_refresh')
    assert result.stats == {'computed': 1, 'failed': 0}
    assert seen.get('jt') == 'opportunity', seen
    assert seen.get('jid') == 'funding_capture_refresh', seen
    assert isinstance(seen.get('dl'), int), seen


@pytest.mark.asyncio
async def test_cr14_venue_refresh_uses_scanner_not_monitor():
    seen: dict = {}

    async def _fake_collect(symbol, qty, venue=None, request_context=None):
        seen['jt'] = getattr(request_context, 'job_type', None)
        seen['jid'] = getattr(request_context, 'job_id', None)
        return {'spot_quote': {'snapshot_id': 'q', 'venue': 'BINANCE_SPOT',
                               'venue_symbol': 'BTCUSDT', 'as_of_ms': 100000,
                               'fetched_at_ms': 100000, 'expires_at_ms': 200000,
                               'reference_notional_usd': '10000', 'quote_json': {},
                               'status': 'OK'}}

    repo = SimpleNamespace(
        list_hedge_plans=AsyncMock(side_effect=lambda **kw: [
            {'plan_id': 'p', 'symbol': 'BTCUSDT', 'status': 'ACTIVE',
             'canonical_id': 'c', 'plan_config_json': {}}] if kw.get('status') == 'ACTIVE' else []),
        aggregate_hedge_position=AsyncMock(return_value=[]),
        latest_hedge_monitor=AsyncMock(return_value=None),
        list_hedge_alerts=AsyncMock(return_value=[]),
        save_spot_venue_snapshot=AsyncMock())
    svc = SimpleNamespace(_repository=repo, _config=None,
                          _ensure_hedge_available=AsyncMock(),
                          _hedge_lock_for=lambda pid: asyncio.Lock(),
                          _hedge_market=SimpleNamespace(collect=_fake_collect))
    jobs = HedgeJobs(svc)
    ctx = SimpleNamespace(trace_id='v', clock_ms=lambda: 100000, request_budget=None)
    result = await jobs.venue_refresh(ctx, job_id='hedge_venue_refresh')
    assert result.stats == {'refreshed': 1, 'failed': 0}
    assert seen.get('jt') == 'scanner', seen
    assert seen.get('jid') == 'hedge_venue_refresh', seen


@pytest.mark.asyncio
async def test_cr14_collect_verifies_signature_then_forwards_not_drops():
    # _collect inspects market.collect for request_context support; with the
    # CR14 market signature it must forward (never silently drop).
    seen: dict = {}

    async def _fake_collect(symbol, qty, venue=None, request_context=None):
        seen['jt'] = getattr(request_context, 'job_type', None)
        return {'ok': True}

    svc = SimpleNamespace(_repository=SimpleNamespace(), _config=None,
                          _ensure_hedge_available=AsyncMock(),
                          _hedge_lock_for=lambda pid: asyncio.Lock(),
                          _hedge_market=SimpleNamespace(collect=_fake_collect))
    jobs = HedgeJobs(svc)
    plan = {'plan_id': 'p', 'symbol': 'BTCUSDT', 'canonical_id': 'c',
            'status': 'ACTIVE', 'plan_config_json': {}}
    from diveintocrypto_desktop.shortlab.request_budget import make_request_context
    ctx = make_request_context(None, job_type='monitor', host='fapi',
                               trace_id='t-m', job_id='hedge_monitor')
    out = await jobs._collect(plan, [], 100000, ctx)
    assert out == {'ok': True}
    assert seen.get('jt') == 'monitor', seen
