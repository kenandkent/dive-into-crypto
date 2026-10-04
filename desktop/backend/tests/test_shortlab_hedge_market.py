from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from diveintocrypto_desktop.shortlab.hedge.market import ProductionHedgeMarket


def market():
    service = SimpleNamespace(_hedge_identity_for=AsyncMock(return_value={'contract_multiplier':1000,'binance_spot_symbol':'PEPEUSDT'}))
    m = ProductionHedgeMarket(service, None, SimpleNamespace(), None, lambda: 100000)
    m._exchange_info = AsyncMock(return_value={'symbols':[]})
    return m


@pytest.mark.asyncio
async def test_native_and_canonical_mark_and_non_par_fx(monkeypatch):
    m = market()
    m.fx_provider.fetch = AsyncMock(return_value=SimpleNamespace(status='OK', stale=False, as_of_ms=99000, data=SimpleNamespace(price_usd=.98)))
    from diveintocrypto_desktop.data import funding
    fetch = AsyncMock(return_value={'mark_price':'.01','time_ms':99000,'last_funding_rate':.001})
    monkeypatch.setattr(funding, 'premium_index', fetch)
    result = await m.mark('1000PEPEUSDT')
    assert result['mark_price'] == '.01' or result['mark_price'] == '0.01'
    assert result['price'] == '0.00001'
    assert result['canonical_price_usd'] == '0.0000098'
    assert result['quote_to_usd'] == '0.98'
    assert result['as_of_ms'] == 99000
    await m.mark('1000PEPEUSDT')
    assert fetch.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('ts', [None, 0, 1, 200000])
async def test_missing_old_future_source_time_never_replaced_by_now(monkeypatch, ts):
    m = market()
    from diveintocrypto_desktop.data import funding
    monkeypatch.setattr(funding, 'premium_index', AsyncMock(return_value={'mark_price':'.01','time_ms':ts}))
    with pytest.raises(RuntimeError, match='MARK_'):
        await m.mark('1000PEPEUSDT')


@pytest.mark.asyncio
async def test_fx_failure_never_assumes_dollar_parity():
    m = market()
    m.fx_provider.fetch = AsyncMock(return_value=SimpleNamespace(status='UNAVAILABLE',stale=False,as_of_ms=99000,data=None))
    with pytest.raises(RuntimeError, match='QUOTE_FX_UNAVAILABLE'):
        await m._fx('USDT')


@pytest.mark.asyncio
async def test_funding_reads_persisted_history_and_current_predicted_rate():
    m = market()
    m.mark = AsyncMock(return_value={'last_funding_rate':'.0002'})
    m.repo.list_funding_events = AsyncMock(return_value=[])
    result = await m.funding('PEPEUSDT')
    assert str(result.current_rate) in ('0.0002', '.0002')
    assert result.conservative_apr is None
    m.repo.list_funding_events.assert_awaited_once()


def spot_quote(qty='2'):
    from diveintocrypto_desktop.shortlab.hedge.models import SpotVenueQuote
    return SpotVenueQuote(venue='BINANCE_SPOT', canonical_id='pepe', symbol='PEPEUSDT',
        chain=None, contract_address=None, as_of_ms=99000, expires_at_ms=119000,
        reference_notional_usd='20', mid_price='10', buy_vwap='10.1',sell_vwap='9.9',
        buy_executable_qty=qty,sell_executable_qty=qty,buy_slippage_bps=10,
        sell_slippage_bps=10,estimated_fee_usd='.02',estimated_gas_usd=None,
        requested_canonical_qty=qty,quote_currency='USDT',quote_to_usd='1',
        direction_costs={'buy_fee_usd':'.01'},status='OK')


@pytest.mark.asyncio
async def test_quote_fx_converts_costs_once_and_keeps_native_vwap():
    m = market()
    m._fx = AsyncMock(return_value='.98')
    m.spot.quote = AsyncMock(return_value=SimpleNamespace(status='OK',data=spot_quote(),stale=False))
    q = await m.quote('1000PEPEUSDT','2')
    assert q.buy_vwap == '10.1'
    assert q.quote_to_usd == '.98'
    assert q.reference_notional_usd == '19.60'
    assert q.estimated_fee_usd == '0.0196'
    assert q.direction_costs['buy_fee_usd'] == '0.0098'


@pytest.mark.asyncio
async def test_quote_quantity_and_provider_failure_preserved():
    m = market()
    m.spot.quote = AsyncMock(return_value=SimpleNamespace(status='OK',data=spot_quote('3'),stale=False))
    with pytest.raises(RuntimeError, match='QUOTE_QUANTITY_MISMATCH'):
        await m.quote('1000PEPEUSDT','2')
    m.spot.quote = AsyncMock(return_value=SimpleNamespace(status='UNAVAILABLE',data=None,stale=False,reason_code='VENUE_REGION_UNAVAILABLE'))
    with pytest.raises(RuntimeError, match='VENUE_REGION_UNAVAILABLE'):
        await m.quote('1000PEPEUSDT','2')


@pytest.mark.asyncio
async def test_missing_chain_key_not_silently_ready():
    m = market()
    m.service._hedge_identity_for = AsyncMock(return_value={'chain':'ethereum','contract_address':'0x0000000000000000000000000000000000000001','canonical_id':'x','contract_multiplier':1})
    m.chain._env = {}
    with pytest.raises(RuntimeError, match='UNCONFIGURED|DISABLED'):
        await m.quote('XUSDT','2','ONCHAIN_DEX')


@pytest.mark.asyncio
async def test_funding_repairs_gaps_at_most_every_300_seconds_and_caches_30_seconds():
    m = market()
    ticks = [100000]
    m.clock = lambda: ticks[0]
    m.mark = AsyncMock(return_value={'last_funding_rate':'.0002'})
    m.repo.list_funding_events = AsyncMock(return_value=[])
    m.service._fetch_funding = AsyncMock(return_value={'events':[]})
    await m.funding('PEPEUSDT')
    await m.funding('PEPEUSDT')
    assert m.repo.list_funding_events.await_count == 1
    assert m.service._fetch_funding.await_count == 1
    ticks[0] += 31000
    await m.funding('PEPEUSDT')
    assert m.repo.list_funding_events.await_count == 2
    assert m.service._fetch_funding.await_count == 1
    ticks[0] += 300000
    await m.funding('PEPEUSDT')
    assert m.service._fetch_funding.await_count == 2
    assert m.service._fetch_funding.await_args.args[1] == {'onboard_at_ms':None}


@pytest.mark.asyncio
async def test_exchange_rate_limits_configure_shared_host_once(monkeypatch):
    from unittest.mock import Mock
    from diveintocrypto_desktop.data import http
    budget = Mock()
    m = ProductionHedgeMarket(SimpleNamespace(), None, None, budget, lambda:100000)
    raw = {'symbols':[], 'rateLimits':[{'rateLimitType':'REQUEST_WEIGHT','interval':'MINUTE','intervalNum':1,'limit':2400}]}
    fetch = AsyncMock(return_value=raw)
    monkeypatch.setattr(http,'get_json',fetch)
    await m._exchange_info()
    await m._exchange_info()
    budget.configure_host_limits.assert_called_once_with('fapi',raw['rateLimits'])
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_fx_source_time_is_required_and_cannot_be_refreshed_locally():
    m = market()
    for ts in (None, 1, 200000):
        m.fx_provider.fetch = AsyncMock(return_value=SimpleNamespace(status='OK',stale=False,as_of_ms=ts,data=SimpleNamespace(price_usd=1)))
        with pytest.raises(RuntimeError,match='QUOTE_FX_STALE_OR_TIME_UNKNOWN'):
            await m._fx('USDT')

@pytest.mark.asyncio
async def test_fx_cached_quote_keeps_first_known_time():
    m=market()
    now=[100000]; m.clock=lambda:now[0]
    m.fx_provider.fetch=AsyncMock(return_value=SimpleNamespace(status='OK',stale=False,as_of_ms=99000,data=SimpleNamespace(price_usd='.98')))
    m.spot.quote=AsyncMock(return_value=SimpleNamespace(status='OK',data=spot_quote(),stale=False))
    first=await m.quote('1000PEPEUSDT','2')
    now[0]=110000
    second=await m.quote('1000PEPEUSDT','2')
    assert first.capabilities['fx']==second.capabilities['fx']=={'source_as_of_ms':99000,'known_at_ms':100000,'currency':'USDT'}
    assert m.fx_provider.fetch.await_count==1

@pytest.mark.asyncio
async def test_spot_outage_keeps_actual_mark_available_for_orphan_leg():
    m=market()
    m.mark=AsyncMock(return_value={'price':'0.001','as_of_ms':100000})
    m.quote=AsyncMock(side_effect=RuntimeError('SPOT_UNAVAILABLE'))
    m.funding=AsyncMock(side_effect=RuntimeError('FUNDING_UNAVAILABLE'))
    cache=await m.collect('1000PEPEUSDT','0')
    assert cache['futures_mark']['price']=='0.001'
    assert 'spot_quote' not in cache
    assert cache['collection_errors']['spot_quote']=='SPOT_UNAVAILABLE'
