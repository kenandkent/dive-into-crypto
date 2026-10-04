from decimal import Decimal
from types import SimpleNamespace
import pytest
from diveintocrypto_desktop.shortlab.evidence import hedge_grader as grader


def test_single_futures_timeline_cannot_measure_hedged_drawdown():
    bars = [{"open_ms": i*3600000, "close_ms": (i+1)*3600000,
             "native_close": Decimal(px), "fx_to_usd": Decimal('1')}
            for i, px in enumerate(['100','150','50'])]
    assert grader._drawdown_if_complete(bars, 0, 7200000, Decimal('10'),
                                        Decimal('10'), Decimal('100'), Decimal('100')) == (None, None)


@pytest.mark.asyncio
async def test_outcome_pin_failure_is_not_retried_without_refs():
    calls = []
    class Repo:
        async def save_hedge_outcome(self, record, refs):
            calls.append(refs)
            raise RuntimeError('pin unavailable')
    with pytest.raises(RuntimeError, match='pin unavailable'):
        await grader._save_outcome_best_effort(Repo(), SimpleNamespace(), [('VENUE_QUOTE','q','entry')])
    assert len(calls) == 1

@pytest.mark.asyncio
async def test_historical_provider_bounds_bars_and_preserves_missing_fx():
    from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider
    calls=[]
    async def fetch(*args):
        calls.append(args)
        return [{'t': (1760000000000)*1000000, 'o':'100','h':'150','l':'50','c':'100'},
                {'t':(1760003600000)*1000000, 'o':'100','h':'100','l':'100','c':'100'}]
    provider=RepositoryHistoricalMarketProvider(None, price_bars_fn=fetch)
    bars=await provider.read_price_bars('XUSDT',1760000000000,1760003600000)
    assert calls == [('XUSDT','1h',1760000000000,1760003600000)]
    assert len(bars)==1 and bars[0].fx_to_usd is None

@pytest.mark.asyncio
async def test_historical_funding_missing_mark_and_fx_remain_unknown():
    from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider
    class Repo:
        async def list_funding_events(self, symbol, start, end):
            return [SimpleNamespace(funding_time_ms=10,funding_rate=.0001,mark_price=None)]
    events=await RepositoryHistoricalMarketProvider(Repo()).read_settled_funding('XUSDT',0,20)
    assert events[0].mark_price is None and events[0].fx_to_usd is None

@pytest.mark.asyncio
async def test_default_archive_fx_respects_currency_and_both_timestamps(tmp_path):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider
    repo=await ShortLabRepository.open(tmp_path/'archive-fx.duckdb')
    await repo.migrate(target_version=5)
    try:
        async def save(id, at, known, currency, fx):
            await repo.save_spot_venue_snapshot(dict(snapshot_id=id,canonical_id='bitcoin',venue='BINANCE_SPOT',as_of_ms=at,fetched_at_ms=known,reference_notional_usd=1000,status='OK',quote_json=dict(quote_currency=currency,quote_to_usd=fx,fx_source_as_of_ms=at,fx_known_at_ms=known,source_timestamp_ms=at,fetched_at_ms=known)))
        await save('old',1000,1000,'USDT','0.97')
        await save('right',8000,8001,'USDT','0.99')
        await save('future-known',9000,11000,'USDT','1.5')
        await save('wrong-currency',9999,9999,'USDC','1.2')
        provider=RepositoryHistoricalMarketProvider(repo)
        assert await provider._fx('USDT',10000) == '0.99'
        assert await provider._fx('EUR',10000) is None
        assert await provider._fx('USDT',20000) is None
    finally:
        await repo.close()

@pytest.mark.asyncio
async def test_historical_fetch_uses_context_and_actual_completion(monkeypatch):
    from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider
    from diveintocrypto_desktop.shortlab.request_budget import get_current_request_context
    marker=object()
    async def fetch(*args):
        assert get_current_request_context() is marker
        return [dict(t=1760000000000,o='1',h='1',l='1',c='1')]
    monkeypatch.setattr('diveintocrypto_desktop.shortlab.evidence.historical_market.time.time',lambda:1800000000)
    bars=await RepositoryHistoricalMarketProvider(None,price_bars_fn=fetch).read_price_bars('XUSDT',1760000000000,1760003600000,marker)
    assert bars[0].known_at_ms == 1800000000000
    assert get_current_request_context() is None

@pytest.mark.asyncio
async def test_historical_lifecycle_reads_real_schema_at_cutoff(tmp_path):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider
    repo=await ShortLabRepository.open(tmp_path/'life.duckdb')
    await repo.migrate(target_version=5)
    try:
        for at,status in ((1000,'TRADING'),(2000,'DELISTED')):
            await repo.save_contract_lifecycle(dict(futures_symbol='XUSDT',observed_at_ms=at,
                onboard_at_ms=500,first_seen_ms=500,delivery_at_ms=None,
                contract_type='PERPETUAL',exchange_status=status,
                contract_multiplier=1,multiplier_source='native'))
        record=await RepositoryHistoricalMarketProvider(repo).read_lifecycle('XUSDT',1500)
        assert record.exchange_status == 'TRADING'
        assert record.onboard_at_ms == 500
        assert await RepositoryHistoricalMarketProvider(repo).read_lifecycle('XUSDT',100) is None
    finally:
        await repo.close()

@pytest.mark.asyncio
async def test_real_provider_completion_advances_grade_without_moving_due(tmp_path):
    from dataclasses import replace
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from test_shortlab_hedge_evidence import _save_fcs, _save_venue, _complete_fake, NOW, DAY_MS
    repo=await ShortLabRepository.open(tmp_path/'grade-clock.duckdb')
    await repo.migrate(target_version=5)
    try:
        await _save_fcs(repo,'clock-fcs',NOW)
        await _save_venue(repo,'q-entry','bitcoin','BINANCE_SPOT',NOW,'100')
        await _save_venue(repo,'q-exit','bitcoin','BINANCE_SPOT',NOW+7*DAY_MS,'100')
        provider=_complete_fake(NOW,NOW+7*DAY_MS,entry_qty='100')
        grading_time=NOW+8*DAY_MS
        provider.completed_at_ms=grading_time+20
        provider.bars['BTCUSDT']=[replace(b,known_at_ms=grading_time+20) for b in provider.bars['BTCUSDT']]
        outcome=await grader.grade_hedge('clock-fcs','ABSOLUTE_100',7,grading_time,repo,provider,None)
        assert outcome.outcome_status == 'COMPLETE'
        assert outcome.updated_at_ms == grading_time+20
        assert outcome.outcome_json['due_ms'] == NOW+7*DAY_MS
    finally:
        await repo.close()

@pytest.mark.asyncio
async def test_archive_fx_rejects_quote_time_without_actual_fx_source(tmp_path):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider
    repo=await ShortLabRepository.open(tmp_path/'fx-provenance.duckdb')
    await repo.migrate(target_version=5)
    try:
        await repo.save_spot_venue_snapshot(dict(snapshot_id='missing-fx-time',canonical_id='x',venue='BINANCE_SPOT',as_of_ms=9000,fetched_at_ms=9001,reference_notional_usd=10,status='OK',quote_json=dict(quote_currency='USDT',quote_to_usd='.99',source_timestamp_ms=9000,fetched_at_ms=9001)))
        assert await RepositoryHistoricalMarketProvider(repo)._fx('USDT',10000) is None
    finally:
        await repo.close()

@pytest.mark.asyncio
async def test_funding_archive_known_time_is_read_completion_not_settlement():
    from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider
    class Repo:
        async def list_funding_events(self, *args):
            return [SimpleNamespace(symbol='XUSDT',funding_time_ms=10,funding_rate='.001',mark_price='2')]
    provider=RepositoryHistoricalMarketProvider(Repo(),clock_ms=lambda:1000)
    result=await provider.read_settled_funding('XUSDT',0,20)
    assert result[0].known_at_ms == 1000
    assert provider.completed_at_ms == 1000

@pytest.mark.asyncio
async def test_run_due_propagates_sender_budget_context(tmp_path, monkeypatch):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from diveintocrypto_desktop.shortlab.request_budget import get_current_request_context
    from test_shortlab_hedge_evidence import _save_fcs, NOW, DAY_MS
    repo=await ShortLabRepository.open(tmp_path/'context.duckdb')
    await repo.migrate(target_version=5)
    seen=[]
    budget=object()
    async def fake_grade(*args):
        seen.append(get_current_request_context())
        return SimpleNamespace(outcome_status='COMPLETE')
    monkeypatch.setattr(grader,'grade_hedge',fake_grade)
    try:
        await _save_fcs(repo,'context-fcs',NOW)
        await grader.run_due(SimpleNamespace(repository=repo,config=None,clock_ms=lambda:NOW+8*DAY_MS,request_budget=budget,trace_id='budget-test',market_provider=object()))
        assert seen and all(c is not None and c.budget is budget for c in seen)
        assert get_current_request_context() is None
    finally:
        await repo.close()

@pytest.mark.asyncio
async def test_hedge_evidence_requires_frozen_multiplier_provenance(tmp_path):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from test_shortlab_hedge_evidence import _fcs_row, NOW, DAY_MS
    repo=await ShortLabRepository.open(tmp_path/'legacy-identity.duckdb')
    await repo.migrate(target_version=5)
    try:
        row=_fcs_row('legacy-identity');row['risk_json']={}
        await repo.save_funding_capture_snapshot(row)
        result=await grader.grade_hedge('legacy-identity','ABSOLUTE_100',7,NOW+8*DAY_MS,repo,None,None)
        assert result.reason_code == 'LEGACY_IDENTITY_UNVERIFIED'
    finally:
        await repo.close()

@pytest.mark.asyncio
async def test_1000_contract_evidence_uses_canonical_spot_and_native_futures_qty(tmp_path):
    from dataclasses import replace
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from test_shortlab_hedge_evidence import _fcs_row, _complete_fake, NOW, DAY_MS
    repo=await ShortLabRepository.open(tmp_path/'1000-evidence.duckdb')
    await repo.migrate(target_version=5)
    try:
        row=_fcs_row('scaled',symbol='1000PEPEUSDT',canonical='pepe')
        row['risk_json']={'identity':{'contract_multiplier':'1000','multiplier_source':'MANUAL'}}
        await repo.save_funding_capture_snapshot(row)
        for id,at,px in [('entry',NOW,'.1'),('exit',NOW+7*DAY_MS,'.11')]:
            await repo.save_spot_venue_snapshot(dict(snapshot_id=id,canonical_id='pepe',venue='BINANCE_SPOT',as_of_ms=at,fetched_at_ms=at,reference_notional_usd=10000,status='OK',quote_json={'requested_canonical_qty':'100000','buy_vwap':px,'sell_vwap':px,'mid_price':px,'quote_to_usd':'1'}))
        provider=_complete_fake(NOW,NOW+7*DAY_MS)
        provider.bars['1000PEPEUSDT']=[replace(b,symbol='1000PEPEUSDT') for b in provider.bars.pop('BTCUSDT')]
        provider.fundings['1000PEPEUSDT']=[replace(f,symbol='1000PEPEUSDT') for f in provider.fundings.pop('BTCUSDT')]
        provider.find_frozen_quote=repo.find_frozen_quote
        result=await grader.grade_hedge('scaled','ABSOLUTE_100',7,NOW+8*DAY_MS,repo,provider,None)
        assert result.outcome_status == 'COMPLETE', result.reason_code
        entry=result.outcome_json['entry']
        assert entry['futures_qty'] == '100'
        assert entry['canonical_futures_qty'] == '100000'
        assert entry['spot_qty'] == '100000'
        assert result.outcome_json['pnl']['futures_pnl_usd'] == '-1000'
        assert result.outcome_json['pnl']['spot_pnl_usd'] == '1000'
    finally:
        await repo.close()

@pytest.mark.asyncio
async def test_due_exit_bar_is_pending_until_first_full_hour_closes(tmp_path):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from test_shortlab_hedge_evidence import _save_fcs,NOW,DAY_MS
    repo=await ShortLabRepository.open(tmp_path/'pending-exit.duckdb')
    await repo.migrate(target_version=5)
    try:
        await _save_fcs(repo,'pending-exit',NOW)
        result=await grader.grade_hedge('pending-exit','ABSOLUTE_100',7,NOW+7*DAY_MS,repo,None,None)
        assert result.outcome_status == 'PENDING'
        assert result.reason_code == 'PENDING_EXIT_BAR'
        assert result.outcome_json['due_ms'] == NOW+7*DAY_MS
        assert not await repo.list_hedge_outcomes('pending-exit')
    finally:
        await repo.close()

@pytest.mark.asyncio
async def test_real_history_transport_failure_remains_retryable_pending(tmp_path):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider
    from test_shortlab_hedge_evidence import _save_fcs,NOW,DAY_MS
    repo=await ShortLabRepository.open(tmp_path/'retry-history.duckdb')
    await repo.migrate(target_version=5)
    async def failed(*args):
        raise RuntimeError('HTTP 404')
    try:
        await _save_fcs(repo,'retry-history',NOW)
        provider=RepositoryHistoricalMarketProvider(repo,price_bars_fn=failed)
        result=await grader.grade_hedge('retry-history','ABSOLUTE_100',7,NOW+8*DAY_MS,repo,provider,None)
        assert result.outcome_status == 'PENDING'
        assert result.reason_code == 'PENDING_HISTORY_RETRY'
        assert not await repo.list_hedge_outcomes('retry-history')
    finally:
        await repo.close()
