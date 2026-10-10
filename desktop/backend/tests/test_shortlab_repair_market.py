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


# ---------------------------------------------------------------------------
# CR02 (D03/D05): FundingEventRecord compat + persisted receipt recovery.
#
# list_funding_events returns FundingEventRecord dataclass (no .get); the
# collector must recover the persisted sl_funding_observation receipt known
# no later than as_of (cutoff) and must never restamp an old event with
# current as_of. Repository already provides list_funding_observations, so
# no repository change is required (verified below).
# ---------------------------------------------------------------------------

DAY_MS_CR02 = 86_400_000
H8_CR02 = 8 * 3_600_000


def _slots_8h_cr02(end_ms: int, days: int = 90) -> list[int]:
    start = end_ms - days * DAY_MS_CR02
    out: list[int] = []
    cur = end_ms
    while cur > start:
        out.append(cur)
        cur -= H8_CR02
    return sorted(out)


def _confirmed_seg_cr02(start_ms: int, anchor_ms: int, known_at_ms: int):
    from diveintocrypto_desktop.shortlab.repair_contracts import FundingScheduleSegment

    return FundingScheduleSegment(
        schedule_id="sched-1",
        symbol="1000PEPEUSDT",
        effective_from_ms=start_ms,
        effective_to_ms=None,
        interval_hours=8,
        anchor_ms=anchor_ms,
        known_at_ms=known_at_ms,
        source="binance:fapi/fundingInfo",
        evidence_ref="ev-1",
        verification="CONFIRMED",
    )


@pytest.mark.asyncio
async def test_cr02_dataclass_events_recover_receipt_and_gate_pass():
    """90D positive + CONFIRMED + fresh current + coverage=1 => not UNKNOWN via last."""
    import tempfile
    from pathlib import Path

    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    from tests.repair_fixtures import FIXTURE_NOW

    tmp = tempfile.mkdtemp()
    repo = await ShortLabRepository.open(Path(tmp) / "cr02.duckdb")
    try:
        await repo.migrate(6)
        start = FIXTURE_NOW - 90 * DAY_MS_CR02
        slots = _slots_8h_cr02(FIXTURE_NOW)
        assert len(slots) == 270
        await repo.upsert_funding_events(
            [{"symbol": "1000PEPEUSDT", "funding_time_ms": s, "funding_rate": 0.0005} for s in slots]
        )
        latest = slots[-1]
        # Real repo returns FundingEventRecord (no .get); sanity check the type.
        seeded = await repo.list_funding_events("1000PEPEUSDT", FIXTURE_NOW - 90 * DAY_MS_CR02, FIXTURE_NOW)
        assert seeded and type(seeded[0]).__name__ == "FundingEventRecord"
        assert not hasattr(seeded[0], "get")
        await repo.save_funding_observation(
            {
                "observation_id": f"obs-1000PEPEUSDT-{FIXTURE_NOW}",
                "symbol": "1000PEPEUSDT",
                "funding_time_ms": latest,
                "known_at_ms": FIXTURE_NOW,
                "raw_json": {"event_count": len(slots), "window": "90d"},
                "interval_hours": 8.0,
                "interval_source": "fundingRate",
                "observation_status": "OBSERVED",
            }
        )
        service = SimpleNamespace(
            _hedge_identity_for=AsyncMock(return_value={"contract_multiplier": "1000"})
        )
        m = ProductionHedgeMarket(service, None, repo, None, lambda: FIXTURE_NOW)

        async def _fake_mark(symbol, request_context=None):
            return {
                "mark_price": "0.01",
                "time_ms": FIXTURE_NOW - 3_000,
                "last_funding_rate": "0.0005",
                "next_funding_time": FIXTURE_NOW + H8_CR02,
                "as_of_ms": FIXTURE_NOW - 3_000,
                "fetched_at_ms": FIXTURE_NOW - 2_000,
                "quote_currency": "USDT",
            }

        m.mark = _fake_mark
        m._exchange_info = AsyncMock(
            return_value={"symbols": [{"symbol": "1000PEPEUSDT", "onboardDate": start - 10 * DAY_MS_CR02}]}
        )
        seg = _confirmed_seg_cr02(start, FIXTURE_NOW, FIXTURE_NOW - 1_000)
        m.repo.list_funding_schedules = AsyncMock(return_value=[seg])
        ctx = make_request_context(None, job_type="evidence", host="fapi", trace_id="t-cr02-pass")
        fctx = await m.collect_funding("1000PEPEUSDT", FIXTURE_NOW, ctx)
        last = fctx.last_settled_observation
        assert last is not None, "dataclass events must not lose last observation via e.get"
        assert last.meta.source_as_of_ms == latest
        assert last.meta.known_at_ms == FIXTURE_NOW
        assert str(fctx.metrics.last_settled_rate) == "0.0005"
        assert fctx.coverage_90d.coverage_fraction == "1"
        from diveintocrypto_desktop.shortlab.config import load_shortlab_config
        from diveintocrypto_desktop.shortlab.hedge.entry_gate import evaluate_funding_entry_gate

        gate = evaluate_funding_entry_gate(fctx, load_shortlab_config(), FIXTURE_NOW)
        assert gate.status == "PASS", f"expected PASS, got {gate.status} {gate.reasons}"
        assert "FUNDING_SCHEDULE_UNKNOWN" not in gate.reasons
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr02_old_receipt_not_reset_to_as_of():
    """Persisted known/fetched for an old settlement must survive verbatim."""
    import tempfile
    from pathlib import Path

    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    from tests.repair_fixtures import FIXTURE_NOW

    tmp = tempfile.mkdtemp()
    repo = await ShortLabRepository.open(Path(tmp) / "cr02-old.duckdb")
    try:
        await repo.migrate(6)
        start = FIXTURE_NOW - 90 * DAY_MS_CR02
        end_settled = FIXTURE_NOW - H8_CR02
        slots: list[int] = []
        cur = end_settled
        while cur > start:
            slots.append(cur)
            cur -= H8_CR02
        slots = sorted(slots)
        await repo.upsert_funding_events(
            [{"symbol": "1000PEPEUSDT", "funding_time_ms": s, "funding_rate": 0.0005} for s in slots]
        )
        latest = slots[-1]
        persisted_known = latest + 5_000
        assert persisted_known < FIXTURE_NOW
        await repo.save_funding_observation(
            {
                "observation_id": f"obs-1000PEPEUSDT-{persisted_known}",
                "symbol": "1000PEPEUSDT",
                "funding_time_ms": latest,
                "known_at_ms": persisted_known,
                "raw_json": {"event_count": len(slots)},
                "interval_hours": 8.0,
                "interval_source": "fundingRate",
                "observation_status": "OBSERVED",
            }
        )
        service = SimpleNamespace(
            _hedge_identity_for=AsyncMock(return_value={"contract_multiplier": "1000"})
        )
        m = ProductionHedgeMarket(service, None, repo, None, lambda: FIXTURE_NOW)

        async def _fake_mark(symbol, request_context=None):
            return {
                "mark_price": "0.01",
                "time_ms": FIXTURE_NOW - 3_000,
                "last_funding_rate": "0.0005",
                "next_funding_time": FIXTURE_NOW + H8_CR02,
                "as_of_ms": FIXTURE_NOW - 3_000,
                "fetched_at_ms": FIXTURE_NOW - 2_000,
                "quote_currency": "USDT",
            }

        m.mark = _fake_mark
        m._exchange_info = AsyncMock(
            return_value={"symbols": [{"symbol": "1000PEPEUSDT", "onboardDate": start - 10 * DAY_MS_CR02}]}
        )
        seg = _confirmed_seg_cr02(start, end_settled, start)
        m.repo.list_funding_schedules = AsyncMock(return_value=[seg])
        ctx = make_request_context(None, job_type="evidence", host="fapi", trace_id="t-cr02-old")
        fctx = await m.collect_funding("1000PEPEUSDT", FIXTURE_NOW, ctx)
        last = fctx.last_settled_observation
        assert last is not None
        assert last.meta.source_as_of_ms == latest
        assert last.meta.known_at_ms == persisted_known
        assert last.meta.fetched_at_ms == persisted_known
        assert last.meta.known_at_ms != FIXTURE_NOW, "old receipt must not be restamped with as_of"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr02_missing_receipt_stays_unknown_not_fabricated():
    """Canonical events without an observation receipt must stay None (UNKNOWN)."""
    import tempfile
    from pathlib import Path

    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    from tests.repair_fixtures import FIXTURE_NOW

    tmp = tempfile.mkdtemp()
    repo = await ShortLabRepository.open(Path(tmp) / "cr02-miss.duckdb")
    try:
        await repo.migrate(6)
        start = FIXTURE_NOW - 90 * DAY_MS_CR02
        slots = _slots_8h_cr02(FIXTURE_NOW)
        await repo.upsert_funding_events(
            [{"symbol": "1000PEPEUSDT", "funding_time_ms": s, "funding_rate": 0.0005} for s in slots]
        )
        service = SimpleNamespace(
            _hedge_identity_for=AsyncMock(return_value={"contract_multiplier": "1000"})
        )
        m = ProductionHedgeMarket(service, None, repo, None, lambda: FIXTURE_NOW)

        async def _fake_mark(symbol, request_context=None):
            return {
                "mark_price": "0.01",
                "time_ms": FIXTURE_NOW - 3_000,
                "last_funding_rate": "0.0005",
                "next_funding_time": FIXTURE_NOW + H8_CR02,
                "as_of_ms": FIXTURE_NOW - 3_000,
                "fetched_at_ms": FIXTURE_NOW - 2_000,
                "quote_currency": "USDT",
            }

        m.mark = _fake_mark
        m._exchange_info = AsyncMock(
            return_value={"symbols": [{"symbol": "1000PEPEUSDT", "onboardDate": start - 10 * DAY_MS_CR02}]}
        )
        seg = _confirmed_seg_cr02(start, FIXTURE_NOW, FIXTURE_NOW - 1_000)
        m.repo.list_funding_schedules = AsyncMock(return_value=[seg])
        ctx = make_request_context(None, job_type="evidence", host="fapi", trace_id="t-cr02-miss")
        fctx = await m.collect_funding("1000PEPEUSDT", FIXTURE_NOW, ctx)
        assert fctx.last_settled_observation is None
        from diveintocrypto_desktop.shortlab.config import load_shortlab_config
        from diveintocrypto_desktop.shortlab.hedge.entry_gate import evaluate_funding_entry_gate

        gate = evaluate_funding_entry_gate(fctx, load_shortlab_config(), FIXTURE_NOW)
        assert gate.status == "UNKNOWN"
        assert "FUNDING_SCHEDULE_UNKNOWN" in gate.reasons
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr02_restart_preserves_known_time():
    """Real DB存入 -> 重启 -> 读回: observation known_at不变, collector恢复一致."""
    import tempfile
    from pathlib import Path

    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    from tests.repair_fixtures import FIXTURE_NOW

    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "cr02-restart.duckdb"
    latest = FIXTURE_NOW - H8_CR02
    persisted_known = latest + 5_000
    repo = await ShortLabRepository.open(db_path)
    try:
        await repo.migrate(6)
        await repo.upsert_funding_events(
            [{"symbol": "1000PEPEUSDT", "funding_time_ms": latest, "funding_rate": 0.0005}]
        )
        await repo.save_funding_observation(
            {
                "observation_id": "obs-restart-1",
                "symbol": "1000PEPEUSDT",
                "funding_time_ms": latest,
                "known_at_ms": persisted_known,
                "raw_json": {"event_count": 1},
                "interval_hours": 8.0,
                "interval_source": "fundingRate",
                "observation_status": "OBSERVED",
            }
        )
    finally:
        await repo.close()
    repo2 = await ShortLabRepository.open(db_path)
    try:
        await repo2.migrate(6)
        rows = await repo2.list_funding_observations("1000PEPEUSDT", latest, latest, FIXTURE_NOW)
        assert len(rows) == 1
        assert int(rows[0]["known_at_ms"]) == persisted_known
        service = SimpleNamespace(
            _hedge_identity_for=AsyncMock(return_value={"contract_multiplier": "1000"})
        )
        m = ProductionHedgeMarket(service, None, repo2, None, lambda: FIXTURE_NOW)

        async def _fake_mark(symbol, request_context=None):
            return {
                "mark_price": "0.01",
                "time_ms": FIXTURE_NOW - 3_000,
                "last_funding_rate": "0.0005",
                "next_funding_time": FIXTURE_NOW + H8_CR02,
                "as_of_ms": FIXTURE_NOW - 3_000,
                "fetched_at_ms": FIXTURE_NOW - 2_000,
                "quote_currency": "USDT",
            }

        m.mark = _fake_mark
        m._exchange_info = AsyncMock(return_value={"symbols": []})
        m.repo.list_funding_schedules = AsyncMock(return_value=[])
        ctx = make_request_context(None, job_type="evidence", host="fapi", trace_id="t-cr02-restart")
        fctx = await m.collect_funding("1000PEPEUSDT", FIXTURE_NOW, ctx)
        last = fctx.last_settled_observation
        assert last is not None
        assert last.meta.known_at_ms == persisted_known
        assert last.meta.fetched_at_ms == persisted_known
    finally:
        await repo2.close()


@pytest.mark.asyncio
async def test_cr02_compat_mapping_and_dataclass_receipts():
    """Mapping(t/fundingRate)与dataclass事件/收据均兼容, 未来receipt不可见."""
    from diveintocrypto_desktop.shortlab.repository import (
        FundingEventRecord,
        FundingObservationRecord,
    )

    from tests.repair_fixtures import FIXTURE_NOW

    latest = FIXTURE_NOW - H8_CR02
    mapping_events = [
        {"symbol": "1000PEPEUSDT", "funding_time_ms": latest - H8_CR02, "funding_rate": "0.0005"},
        {"symbol": "1000PEPEUSDT", "t": latest, "fundingRate": "0.0006"},
    ]
    mapping_rows = [
        {
            "observation_id": "o1",
            "symbol": "1000PEPEUSDT",
            "funding_time_ms": latest,
            "known_at_ms": latest + 7_000,
            "raw_json": {},
            "observation_status": "OBSERVED",
        }
    ]
    service = SimpleNamespace(
        _hedge_identity_for=AsyncMock(return_value={"contract_multiplier": "1000"})
    )
    repo = SimpleNamespace(
        list_funding_events=AsyncMock(return_value=mapping_events),
        list_funding_observations=AsyncMock(return_value=mapping_rows),
        list_funding_schedules=AsyncMock(return_value=[]),
    )
    m = ProductionHedgeMarket(service, None, repo, None, lambda: FIXTURE_NOW)
    m.mark = AsyncMock(
        return_value={
            "mark_price": "0.01",
            "time_ms": FIXTURE_NOW - 3_000,
            "last_funding_rate": "0.0006",
            "as_of_ms": FIXTURE_NOW - 3_000,
            "fetched_at_ms": FIXTURE_NOW - 2_000,
            "quote_currency": "USDT",
        }
    )
    m._exchange_info = AsyncMock(return_value={"symbols": []})
    ctx = make_request_context(None, job_type="evidence", host="fapi", trace_id="t-cr02-compat")
    fctx = await m.collect_funding("1000PEPEUSDT", FIXTURE_NOW, ctx)
    assert fctx.last_settled_observation is not None
    assert fctx.last_settled_observation.meta.known_at_ms == latest + 7_000
    assert fctx.last_settled_observation.meta.source_as_of_ms == latest

    dataclass_events = [FundingEventRecord(symbol="1000PEPEUSDT", funding_time_ms=latest, funding_rate=0.0007)]
    dataclass_rows = [
        FundingObservationRecord(
            observation_id="o1",
            symbol="1000PEPEUSDT",
            funding_time_ms=latest,
            known_at_ms=latest + 8_000,
            raw_json={},
            observation_status="OBSERVED",
        )
    ]
    repo2 = SimpleNamespace(
        list_funding_events=AsyncMock(return_value=dataclass_events),
        list_funding_observations=AsyncMock(return_value=dataclass_rows),
        list_funding_schedules=AsyncMock(return_value=[]),
    )
    m2 = ProductionHedgeMarket(service, None, repo2, None, lambda: FIXTURE_NOW)
    m2.mark = AsyncMock(
        return_value={
            "mark_price": "0.01",
            "time_ms": FIXTURE_NOW - 3_000,
            "last_funding_rate": "0.0007",
            "as_of_ms": FIXTURE_NOW - 3_000,
            "fetched_at_ms": FIXTURE_NOW - 2_000,
            "quote_currency": "USDT",
        }
    )
    m2._exchange_info = AsyncMock(return_value={"symbols": []})
    fctx2 = await m2.collect_funding("1000PEPEUSDT", FIXTURE_NOW, ctx)
    assert fctx2.last_settled_observation is not None
    assert fctx2.last_settled_observation.meta.known_at_ms == latest + 8_000

    # Future receipt (known_at > as_of) must stay invisible => None, never fabricated.
    future_rows = [
        {
            "observation_id": "o-future",
            "symbol": "1000PEPEUSDT",
            "funding_time_ms": latest,
            "known_at_ms": FIXTURE_NOW + 60_000,
            "raw_json": {},
            "observation_status": "OBSERVED",
        }
    ]
    repo3 = SimpleNamespace(
        list_funding_events=AsyncMock(return_value=dataclass_events),
        list_funding_observations=AsyncMock(return_value=future_rows),
        list_funding_schedules=AsyncMock(return_value=[]),
    )
    # Real repo would already filter known_at > known_by, but the collector
    # must also defensively ignore a future receipt passed by a fake.
    m3 = ProductionHedgeMarket(service, None, repo3, None, lambda: FIXTURE_NOW)
    m3.mark = AsyncMock(
        return_value={
            "mark_price": "0.01",
            "time_ms": FIXTURE_NOW - 3_000,
            "last_funding_rate": "0.0007",
            "as_of_ms": FIXTURE_NOW - 3_000,
            "fetched_at_ms": FIXTURE_NOW - 2_000,
            "quote_currency": "USDT",
        }
    )
    m3._exchange_info = AsyncMock(return_value={"symbols": []})
    fctx3 = await m3.collect_funding("1000PEPEUSDT", FIXTURE_NOW, ctx)
    # Our collector filters known > as_of, so a future-only receipt yields None.
    # (If the repo had correctly filtered, rows would be empty with the same result.)
    assert fctx3.last_settled_observation is None
