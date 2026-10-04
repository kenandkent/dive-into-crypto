import asyncio
import threading
import pytest
from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
from diveintocrypto_desktop.shortlab.hedge.ledger import aggregate_events
from diveintocrypto_desktop.shortlab.request_budget import RequestBudget, Denied

@pytest.mark.asyncio
async def test_cancelled_queued_operation_does_not_block_following_work(tmp_path):
    repo = ShortLabRepository(tmp_path / 'queue.duckdb')
    entered, release = threading.Event(), threading.Event()
    def block():
        entered.set()
        release.wait(2)
    first = asyncio.create_task(repo._run(block))
    while not entered.is_set():
        await asyncio.sleep(0)
    waiting = asyncio.create_task(repo._run(lambda: 2))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await first
    try:
        assert await asyncio.wait_for(repo._run(lambda: 3), .2) == 3
    finally:
        repo._pending.clear()
        await repo.close()

def test_native_futures_quantity_uses_verified_multiplier():
    positions = aggregate_events([{'event_type':'OPEN', 'leg_type':'FUTURES_SHORT',
        'native_qty':'2', 'native_price':'0.01'}], identity={'contract_multiplier':'1000'})
    assert positions[0].remaining_qty == '2000'

def test_host_budgets_are_independent():
    budget = RequestBudget(max_sends=1)
    budget.acquire_or_raise('fapi', 1, 'monitor', 'ticker').mark_sent()
    assert not isinstance(budget.try_acquire('api', 1, 'monitor', 'ticker'), Denied)

def test_weight_capacity_blocks_before_send():
    budget = RequestBudget(max_sends=100, max_weight=5)
    budget.acquire_or_raise('fapi', 4, 'monitor', 'ticker').mark_sent()
    assert isinstance(budget.try_acquire('fapi', 2, 'monitor', 'ticker'), Denied)

def test_weight_reservation_release_and_expiry_are_per_host():
    clock = [1000]
    budget = RequestBudget(max_sends=100, max_weight=5, weight_window_ms=100,
                           clock=lambda: clock[0])
    permit = budget.acquire_or_raise('fapi', 5, 'monitor', 'ticker')
    assert isinstance(budget.try_acquire('fapi', 1, 'monitor', 'ticker'), Denied)
    permit.release_unsent()
    budget.acquire_or_raise('fapi', 5, 'monitor', 'ticker').mark_sent()
    assert not isinstance(budget.try_acquire('api', 5, 'monitor', 'ticker'), Denied)
    clock[0] += 100
    assert not isinstance(budget.try_acquire('fapi', 5, 'monitor', 'ticker'), Denied)

@pytest.mark.asyncio
async def test_cancelled_running_transaction_keeps_worker_busy_until_finished(tmp_path):
    repo = ShortLabRepository(tmp_path / 'running.duckdb')
    entered, release = threading.Event(), threading.Event()
    def block():
        entered.set()
        release.wait(2)
    first = asyncio.create_task(repo._run(block))
    while not entered.is_set():
        await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert repo._worker_busy
    following = asyncio.create_task(repo._run(lambda: 3))
    await asyncio.sleep(0)
    assert not following.done()
    release.set()
    assert await asyncio.wait_for(following, 1) == 3
    await repo.close()

def test_native_only_spot_quantity_is_not_multiplied():
    positions = aggregate_events([{'event_type':'OPEN', 'leg_type':'SPOT_LONG',
        'native_qty':'2', 'native_price':'0.01'}], identity={'contract_multiplier':'1000'})
    assert positions[1].remaining_qty == '2'

def test_all_exchange_weight_windows_are_enforced():
    budget = RequestBudget(max_sends=100)
    budget.configure_host_limits('fapi', [
        {'rateLimitType':'REQUEST_WEIGHT','interval':'MINUTE','intervalNum':1,'limit':5},
        {'rateLimitType':'REQUEST_WEIGHT','interval':'DAY','intervalNum':1,'limit':100},
    ])
    budget.acquire_or_raise('fapi', 5, 'monitor', 'ticker').mark_sent()
    assert isinstance(budget.try_acquire('fapi', 1, 'monitor', 'ticker'), Denied)
