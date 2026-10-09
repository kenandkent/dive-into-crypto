"""R11b MarketPort production wiring (D11/D18/D19).

Covers ProductionHedgeMarket.collect_futures/collect_spot/collect_funding:
true BOOK two-sided depth (SELL entry / BUY exit), Mark never posing as
execution price, budget refusal as BudgetExhausted (DEFERRED upstream),
immutable RequestContext pass-through, and R14 capture task interface
shapes. Budget semantics follow R00/D19 contracts (Fake budget for denial);
R11a details are never assumed.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from diveintocrypto_desktop.shortlab.hedge.market import ProductionHedgeMarket
from diveintocrypto_desktop.shortlab.request_budget import RequestBudget, make_request_context


def _market(clock_ms: int = 100_000, budget=None):
    service = SimpleNamespace(
        _hedge_identity_for=AsyncMock(return_value={
            'contract_multiplier': '1000',
            'binance_spot_symbol': '1000PEPEUSDT',
            'binance_futures_symbol': '1000PEPEUSDT',
            'canonical_id': 'pepe',
        })
    )
    repo = SimpleNamespace(
        list_funding_events=AsyncMock(return_value=[]),
        list_funding_schedules=AsyncMock(return_value=[]),
    )
    clock = lambda: clock_ms
    m = ProductionHedgeMarket(service, None, repo, budget, clock)
    return m


def _book_fixture():
    # True BOOK: bids descending, asks ascending, verbatim string pairs.
    return SimpleNamespace(
        value={
            'symbol': '1000PEPEUSDT',
            'bids': [['0.0099', '600'], ['0.0098', '600']],
            'asks': [['0.0101', '600'], ['0.0102', '600']],
        },
        meta=SimpleNamespace(source_as_of_ms=99_000, known_at_ms=99_500, fetched_at_ms=99_500),
    )


@pytest.mark.asyncio
async def test_collect_futures_true_book_sell_entry_buy_exit(monkeypatch):
    m = _market()
    m._fx = AsyncMock(return_value='1')
    from diveintocrypto_desktop.data import funding as _funding
    from diveintocrypto_desktop.data import orderbook as _book

    async def _fake_premium(symbol, request_context=None):
        assert request_context is not None
        # Immutable pass-through: real job_type preserved, never forced monitor.
        assert getattr(request_context, 'job_type', None) == 'evidence'
        return {'mark_price': '0.01', 'time_ms': 99_000, 'last_funding_rate': '0.0005'}

    async def _fake_book(symbol, request_context=None):
        assert request_context is not None
        return _book_fixture()

    monkeypatch.setattr(_funding, 'premium_index', _fake_premium)
    monkeypatch.setattr(_book, 'fetch_book_observed', _fake_book)
    m._exchange_info = AsyncMock(return_value={'symbols': [{'symbol': '1000PEPEUSDT'}], 'rateLimits': []})
    # Minimal futures rules entry so parse succeeds.
    from diveintocrypto_desktop.data import trading_rules as _rules
    real_parse = _rules.parse_trading_rules
    monkeypatch.setattr(_rules, 'parse_trading_rules',
                        lambda entry, venue, inst, meta: real_parse(
                            {'symbol': '1000PEPEUSDT', 'status': 'TRADING',
                             'filters': [{'filterType': 'PRICE_FILTER', 'minPrice': '0.000001',
                                          'maxPrice': '1000000', 'tickSize': '0.000001'},
                                         {'filterType': 'LOT_SIZE', 'minQty': '1', 'maxQty': '1000000', 'stepSize': '1'},
                                         {'filterType': 'MIN_NOTIONAL', 'notional': '5'}]},
                            venue, inst, meta))

    ctx = make_request_context(None, job_type='evidence', host='fapi', trace_id='t1')
    out = await m.collect_futures('1000PEPEUSDT', '100', ctx)
    quote = out['futures_quote']
    # SELL entry from bids, BUY exit from asks (never merged total).
    assert quote.requested_contract_qty == '100'
    assert quote.sell_vwap_native is not None and quote.buy_vwap_native is not None
    assert Decimal(quote.sell_vwap_native) < Decimal(quote.buy_vwap_native)
    assert quote.sell_executable_qty == '100' and quote.buy_executable_qty == '100'
    assert quote.book_observation_id.startswith('1000PEPEUSDT:book:')
    # Mark never poses as execution price: quote VWAPs differ from Mark.
    mark = out['futures_mark']
    assert mark['mark_price'] == '0.01'
    assert quote.sell_vwap_native != mark['mark_price'] or quote.buy_vwap_native != mark['mark_price']
    assert quote.as_of_ms == mark['as_of_ms']
    assert out['book_observation_id'] == quote.book_observation_id


@pytest.mark.asyncio
async def test_collect_futures_budget_denial_raises_for_deferred():
    budget = RequestBudget(max_sends=1, window_ms=60_000)
    m = _market(budget=budget)
    m._fx = AsyncMock(return_value='1')
    # Exhaust the single send with a direct acquire.
    permit = budget.try_acquire('fapi', 10, 'evidence', 'premiumIndex')
    assert permit
    permit.mark_sent()
    from diveintocrypto_desktop.shortlab.request_budget import BudgetExhausted
    ctx = make_request_context(budget, job_type='evidence', host='fapi', trace_id='t-defer')
    with pytest.raises(BudgetExhausted):
        await m.collect_futures('1000PEPEUSDT', '10', ctx)


@pytest.mark.asyncio
async def test_collect_spot_forwards_context_and_checks_qty():
    m = _market()
    m._fx = AsyncMock(return_value='1')
    seen: dict = {}

    async def _fake_quote(identity, qty, request_context=None):
        seen['job_type'] = getattr(request_context, 'job_type', None)
        seen['qty'] = qty
        from diveintocrypto_desktop.shortlab.models import ProviderResult
        from tests.repair_fixtures import make_decision_context
        q = make_decision_context().venue_quotes[0]
        return SimpleNamespace(status='OK', data=q, stale=False, reason_code=None)

    m.spot.quote = _fake_quote
    ctx = make_request_context(None, job_type='opportunity', host='spot', trace_id='t-spot')
    identity = SimpleNamespace(binance_spot_symbol='1000PEPEUSDT', canonical_id='pepe',
                               mapping_confidence='VERIFIED', contract_multiplier=1000)
    quote = await m.collect_spot(identity, 'BINANCE_SPOT', '100000', ctx)
    assert seen['job_type'] == 'opportunity'
    assert seen['qty'] == '100000'
    assert quote.venue == 'BINANCE_SPOT'
    with pytest.raises(ValueError):
        await m.collect_spot(identity, 'BINANCE_SPOT', '0', ctx)


@pytest.mark.asyncio
async def test_collect_funding_returns_funding_context_with_source_times():
    m = _market(clock_ms=1791417600000)
    m._fx = AsyncMock(return_value='1')
    from diveintocrypto_desktop.data import funding as _funding
    from tests.repair_fixtures import FIXTURE_NOW

    async def _fake_premium(symbol, request_context=None):
        return {'mark_price': '0.01', 'time_ms': FIXTURE_NOW - 3_000, 'last_funding_rate': '0.0005'}

    import diveintocrypto_desktop.data.funding as _fmod
    orig = _fmod.premium_index
    _fmod.premium_index = _fake_premium
    try:
        m._exchange_info = AsyncMock(return_value={'symbols': []})
        m.repo.list_funding_events = AsyncMock(return_value=[])
        ctx = make_request_context(None, job_type='evidence', host='fapi', trace_id='t-fund')
        fctx = await m.collect_funding('1000PEPEUSDT', FIXTURE_NOW, ctx)
    finally:
        _fmod.premium_index = orig
    from diveintocrypto_desktop.shortlab.repair_contracts import FundingContext
    assert isinstance(fctx, FundingContext)
    assert fctx.metrics.symbol == '1000PEPEUSDT'
    # Source times preserved (current observation carries Mark time, never now-fill).
    assert fctx.current_observation.meta.source_as_of_ms == FIXTURE_NOW - 3_000
    assert fctx.history_class in ('FULL_90D', 'PARTIAL_90D', 'INSUFFICIENT', 'HISTORY_CLASS_UNKNOWN')
