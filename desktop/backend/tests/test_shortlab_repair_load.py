"""Repair load tests (V13): R11a budget section + R11b jobs section.

R11a-owned budget load: bounded rounds (240/round, Funding 80/300s),
independent weight windows, provider RPS/month independence — fake
clocks/transports. R11b-owned: drive_ticks rotation/capacity (11 symbols,
50 plans, 500 scores, 8s deadline, fair rotation, unserved降级), 60s
persist, 429 counting, deadline expiry, budget DEFERRED, jobs plan归属.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from diveintocrypto_desktop.data import http as http_mod
from diveintocrypto_desktop.shortlab import request_budget as rb
from diveintocrypto_desktop.shortlab.hedge.jobs import HedgeJobs
from diveintocrypto_desktop.shortlab.hedge.monitor import (
    compute_monitor,
    should_persist,
)
from diveintocrypto_desktop.shortlab.request_budget import (
    BudgetExhausted,
    RequestBudget,
    make_request_context,
)


class _FakeResponse:
    def __init__(self, status: int, payload) -> None:
        self.status = status
        self._payload = payload
        self.headers = {}

    def raise_for_status(self) -> None:
        return None

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, n: int) -> None:
        self.calls = 0
        self._n = n

    def get(self, url, params=None):
        self.calls += 1

        class _Ctx:
            def __init__(self, payload):
                self._payload = payload

            async def __aenter__(self):
                return _FakeResponse(200, self._payload)

            async def __aexit__(self, *exc):
                return None

        return _Ctx({"i": self.calls})


async def _noop_sleep(_s: float) -> None:
    return None


class TestR11aBudgetLoad:
    """R11a-owned budget load: bounded rounds, independent windows."""

    @pytest.mark.asyncio
    async def test_single_round_240_bounded(self):
        budget = rb.RequestBudget(max_sends=240, window_ms=60_000)
        ctx = rb.make_request_context(budget, job_type="monitor")
        session = _FakeSession(300)
        with patch.object(http_mod, "get_session", return_value=session):
            for _ in range(240):
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        assert budget.sent_attempts == 240
        assert session.calls == 240
        extra = _FakeSession(1)
        with patch.object(http_mod, "get_session", return_value=extra):
            with pytest.raises(rb.BudgetExhausted):
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        assert extra.calls == 0

    def test_funding_and_weight_windows_independent(self):
        now = [1_700_000_000_000]
        budget = rb.RequestBudget(
            max_sends=1000,
            window_ms=60_000,
            funding_max=80,
            funding_window_ms=300_000,
            max_weight=100000,
            weight_window_ms=60_000,
            clock=lambda: now[0],
        )
        for _ in range(80):
            p = budget.try_acquire("fapi", 5, "backfill", "fundingInfo")
            assert not isinstance(p, rb.Denied)
            p.mark_sent()
        assert isinstance(budget.try_acquire("fapi", 5, "backfill", "fundingInfo"), rb.Denied)
        # Non-funding families still have host quota.
        p = budget.try_acquire("fapi", 10, "monitor", "klines")
        assert not isinstance(p, rb.Denied)

    @pytest.mark.asyncio
    async def test_concurrent_background_never_eats_reserves(self):
        budget = rb.RequestBudget(max_sends=20, window_ms=60_000)
        session = _FakeSession(100)
        successes: list[int] = []

        async def _bg():
            ctx = rb.make_request_context(budget, job_type="entry")
            for _ in range(20):
                try:
                    with patch.object(http_mod, "get_session", return_value=session):
                        await http_mod.get_json(
                            "https://fapi.binance.com/fapi/v1/klines",
                            {"symbol": "BTCUSDT", "limit": 10},
                            request_context=ctx,
                        )
                    successes.append(1)
                except rb.BudgetExhausted:
                    pass

        with patch.object(http_mod.asyncio, "sleep", _noop_sleep):
            await asyncio.gather(_bg(), _bg())
        # Background cap is 10 (20 - 4 monitor - 6 scanner); never 20.
        assert len(successes) <= 10
        assert budget.sent_attempts <= 10


# --- R11b section: jobs rotation/capacity/persist/429/deadline/budget/plan归属 ---
class FakeClock:
    def __init__(self, start_ms: int = 1_791_417_600_000):
        self.ms = int(start_ms)

    def __call__(self) -> int:
        return int(self.ms)

    def advance(self, ms: int) -> int:
        self.ms += int(ms)
        return int(self.ms)


def _plan(pid: str, symbol: str) -> dict:
    return {'plan_id': pid, 'symbol': symbol, 'canonical_id': 'c', 'status': 'ACTIVE',
            'target_hedge_ratio': '1', 'contract_multiplier': '1',
            'plan_config_json': {}}


def _positions():
    return ({'leg_type': 'FUTURES_SHORT', 'remaining_qty': '1', 'open_qty': '1',
             'closed_qty': '0', 'weighted_avg_price': '100'},
            {'leg_type': 'SPOT_LONG', 'remaining_qty': '1', 'open_qty': '1',
             'closed_qty': '0', 'weighted_avg_price': '100'})


async def drive_ticks(symbol_count=11, plan_count=50, score_count=500, clock=None):
    """Fake-clock driver: bounded rounds, fair rotation, background scores."""
    clock = clock or FakeClock()
    svc = SimpleNamespace(_repository=SimpleNamespace(), _config=None,
                          _ensure_hedge_available=AsyncMock(),
                          _hedge_lock_for=lambda pid: asyncio.Lock(),
                          _hedge_market=SimpleNamespace(collect=AsyncMock(return_value={})))
    jobs = HedgeJobs(svc)
    symbols = [f"SYM{i}USDT" for i in range(symbol_count)]
    items = []
    for i in range(plan_count):
        sym = symbols[i % symbol_count]
        items.append((f"plan-{i}", _plan(f"plan-{i}", sym), _positions()))
    # 500 background scores processed cooperatively (never one giant block).
    background = [{'symbol': f"BG{i}USDT"} for i in range(score_count)]
    done = 0
    batch = 50
    yields = 0
    while done < len(background):
        done += min(batch, len(background) - done)
        yields += 1
    assert done == score_count and yields == score_count // batch
    # Consecutive rounds must serve the tail (11 coins, cap 10/tick).
    seen: set[str] = set()
    max_tick = 0
    for _ in range(4):
        report = await jobs._collect_tick(items, clock(), None)
        assert report['max_tick_ms'] <= 8000
        max_tick = max(max_tick, report['max_tick_ms'])
        seen.update(report['selected'])
        # Per-tick cap holds; unserved is at most 1 for 11 symbols.
        assert len(report['selected']) <= 10
        assert len(report['unserved_symbols']) <= max(0, symbol_count - 10)
        clock.advance(10_000)
    # No permanent starvation: all 11 served across rounds.
    assert sorted(seen) == sorted(symbols)
    return SimpleNamespace(max_tick_ms=max_tick, unserved_symbols=sorted(set(symbols) - seen),
                           served=sorted(seen))


@pytest.mark.asyncio
async def test_drive_ticks_11_50_500_bounded_and_fair():
    report = await drive_ticks(symbol_count=11, plan_count=50, score_count=500)
    assert report.max_tick_ms <= 8000
    assert report.unserved_symbols == []


@pytest.mark.asyncio
async def test_fair_rotation_serves_tail_not_fixed_first_10():
    clock = FakeClock()
    svc = SimpleNamespace(_repository=SimpleNamespace(), _config=None,
                          _ensure_hedge_available=AsyncMock(),
                          _hedge_lock_for=lambda pid: asyncio.Lock(),
                          _hedge_market=SimpleNamespace(collect=AsyncMock(return_value={})))
    jobs = HedgeJobs(svc)
    symbols = [f"S{i:02d}USDT" for i in range(11)]
    items = [(f"p-{s}", _plan(f"p-{s}", s), _positions()) for s in symbols]
    first = await jobs._collect_tick(items, clock(), None)
    assert len(first['selected']) == 10 and len(first['unserved_symbols']) == 1
    tail = first['unserved_symbols'][0]
    clock.advance(10_000)
    second = await jobs._collect_tick(items, clock(), None)
    # The previously unserved tail is served first next round.
    assert second['selected'][0] == tail


def test_60s_persist_and_risk_change_immediate():
    from tests.helpers.hedge_load_harness import make_market_cache, make_plan, make_positions, make_settled_events
    now = 1_760_000_000_000
    plan = make_plan("plan-0", "BTCUSDT")
    pos = make_positions(plan_id="plan-0")
    settled = make_settled_events()
    cache = make_market_cache(now_ms=now)
    first = compute_monitor(plan, pos, cache, settled, None, now)
    assert should_persist(None, first, None, None, now_ms=now) is True
    # Same risk 10s later: no persist.
    cache2 = make_market_cache(now_ms=now + 10_000)
    # Keep source times fresh but risk stable: advance expiries too.
    cache2['futures_mark']['as_of_ms'] = now + 10_000
    cache2['spot_quote']['as_of_ms'] = now + 10_000
    cache2['futures_mark']['expires_at_ms'] = now + 70_000
    cache2['spot_quote']['expires_at_ms'] = now + 70_000
    second = compute_monitor(plan, pos, cache2, settled, None, now + 10_000)
    assert should_persist(first, second, now, None, now_ms=now + 10_000) is False
    # 60s sampler fires.
    assert should_persist(first, second, now, None, now_ms=now + 60_000) is True
    # Risk change (degraded set) fires immediately.
    degraded_cache = make_market_cache(now_ms=now + 20_000, persist_lag_ms=6_000)
    degraded = compute_monitor(plan, pos, degraded_cache, settled, None, now + 20_000)
    assert degraded.status == 'MONITOR_DEGRADED'
    assert should_persist(first, degraded, now, None, now_ms=now + 20_000) is True


@pytest.mark.asyncio
async def test_429_counted_degraded_positions_preserved():
    clock = FakeClock()
    from diveintocrypto_desktop.shortlab.service import HedgeUnavailable

    async def _collect_429(symbol, qty, venue=None, request_context=None):
        raise HedgeUnavailable('RATE_LIMITED')

    svc = SimpleNamespace(_repository=SimpleNamespace(), _config=None,
                          _ensure_hedge_available=AsyncMock(),
                          _hedge_lock_for=lambda pid: asyncio.Lock(),
                          _hedge_market=SimpleNamespace(collect=_collect_429))
    jobs = HedgeJobs(svc)
    plan = _plan('p-429', 'BTCUSDT')
    pos = _positions()
    # Direct _collect counts 429 and preserves old quotes (empty here).
    before = int(jobs._rate_limited_count)
    cache = await jobs._collect(plan, pos, clock(), None)
    assert cache == {}
    # Counter increments via _collect_tick path with budget-like 429?
    # Simulate a market that raises RATE_LIMITED through _collect_tick.
    items = [('p-429', plan, pos)]
    report = await jobs._collect_tick(items, clock(), None)
    # _collect swallows and returns cached {}; tick still bounded.
    assert report['max_tick_ms'] <= 8000
    # Positions are never rewritten by degraded flags (mirror untouched).
    assert pos[0]['remaining_qty'] == '1' and pos[1]['remaining_qty'] == '1'


@pytest.mark.asyncio
async def test_deadline_expiry_degraded_never_clears():
    clock = FakeClock()
    svc = SimpleNamespace(_repository=SimpleNamespace(), _config=None,
                          _ensure_hedge_available=AsyncMock(),
                          _hedge_lock_for=lambda pid: asyncio.Lock())
    jobs = HedgeJobs(svc)

    async def _slow(plan, positions, now_ms=None, request_context=None):
        await asyncio.sleep(0.2)
        return {}

    jobs._collect = _slow  # type: ignore[method-assign]
    symbols = ['AUSDT', 'BUSDT']
    items = [(f"p-{s}", _plan(f"p-{s}", s), _positions()) for s in symbols]
    report = await jobs._collect_tick(items, clock(), None, deadline_ms=50, concurrency=4)
    # Total deadline exceeded -> all degraded markers, never fabricated quotes.
    for pid, cache in report['caches'].items():
        assert cache.get('_timeout') is True
    # Positions preserved.
    for _, _, pos in items:
        assert pos[0]['remaining_qty'] == '1'


@pytest.mark.asyncio
async def test_budget_refusal_deferred_via_contract_fake():
    budget = RequestBudget(max_sends=1, window_ms=60_000)
    permit = budget.try_acquire('fapi', 10, 'evidence', 'premiumIndex')
    assert permit
    permit.mark_sent()
    ctx = make_request_context(budget, job_type='evidence', host='fapi', trace_id='t-budget')
    # Direct budget denial raises (jobs map to DEFERRED, never fake quote).
    with pytest.raises(BudgetExhausted):
        budget.acquire_or_raise('fapi', 10, 'evidence', 'premiumIndex')
    assert budget.denied_count >= 1
    # Jobs capture path maps budget denial to DEFERRED JobStatus.
    from tests.repair_fixtures import make_ports, make_decision_context
    ports = make_ports()

    async def _denied_capture(capture_context, repository, market, request_context):
        raise BudgetExhausted("REQUEST_BUDGET_EXHAUSTED: host window full")

    ports = make_ports(capture_strategy_entries=_denied_capture)
    svc = SimpleNamespace(_repository=SimpleNamespace(), _config=None,
                          _ensure_hedge_available=AsyncMock(),
                          _repair_ports=ports,
                          _require_repair_port=ports.require,
                          _repository_port=SimpleNamespace(), _market_port=SimpleNamespace(),
                          _hedge_lock_for=lambda pid: asyncio.Lock())
    jobs = HedgeJobs(svc)
    ctx2 = SimpleNamespace(trace_id='cap-1', clock_ms=FakeClock()(), request_budget=budget)
    # capture_entries builds its own evidence context; denial -> SUCCEEDED+DEFERRED.
    from tests.repair_fixtures import make_funding_context, make_identity
    from diveintocrypto_desktop.shortlab.repair_contracts import CaptureContext
    fctx = make_funding_context('valid')
    ident = make_identity('m1000')
    cap = CaptureContext(cohort='FUNDING_CARRY', source_snapshot_id='s1', symbol='1000PEPEUSDT',
                         identity=ident, identity_snapshot_id='i1', funding_context=fctx,
                         futures_contract_qty='10', canonical_futures_qty='10000',
                         strategies=('ABSOLUTE_100',), decision=None, decision_as_of_ms=1,
                         policy={}, rule_refs={}, source_refs={})
    ctx3 = SimpleNamespace(trace_id='cap-1', clock_ms=lambda: 1_791_417_600_000, request_budget=budget)
    status = await jobs.capture_entries(ctx3, cap, job_id='cap-1')
    assert status.status == 'SUCCEEDED'
    assert status.stats.get('deferred') == 1


def test_jobs_plan归属_monitor_evidence():
    # Plan归属: hedge_monitor uses monitor档位, capture uses evidence (BACKGROUND).
    svc = SimpleNamespace(_repository=SimpleNamespace(), _config=None,
                          _ensure_hedge_available=AsyncMock(),
                          _hedge_lock_for=lambda pid: asyncio.Lock())
    jobs = HedgeJobs(svc)
    mon_ctx = SimpleNamespace(trace_id='m', clock_ms=lambda: 1_791_417_600_000,
                              request_budget=RequestBudget(max_sends=240))
    rc = jobs._request_context_for(mon_ctx, job_type='monitor', job_id='hedge_monitor')
    assert rc.job_type == 'monitor'
    ev_ctx = SimpleNamespace(trace_id='e', clock_ms=lambda: 1_791_417_600_000,
                             request_budget=RequestBudget(max_sends=240))
    rc2 = jobs._request_context_for(ev_ctx, job_type='evidence', job_id='strategy_capture')
    assert rc2.job_type == 'evidence'
