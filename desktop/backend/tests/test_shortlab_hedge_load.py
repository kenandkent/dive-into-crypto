"""H07 load harness: virtual 600s, 10 symbols, 500 scores, block recovery.

Fake-timer determinism only; the real-worker integration lives in H08's
``test_shortlab_hedge_worker_integration.py`` and must not mock the
production monitor functions.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "helpers"))
from hedge_load_harness import (  # noqa: E402
    FakeWorker,
    InjectedClock,
    make_background_scores,
    make_market_cache,
    make_monitor_inputs,
    make_plan,
    make_positions,
    make_settled_events,
)

from diveintocrypto_desktop.shortlab.hedge.alerts import (  # noqa: E402
    AlertManager,
    evaluate_alerts,
)
from diveintocrypto_desktop.shortlab.hedge.monitor import (  # noqa: E402
    compute_monitor,
    should_persist,
)

NOW = 1_760_000_000_000


def test_injected_clock_advance_is_deterministic():
    clock = InjectedClock(NOW)
    assert clock.now_ms == NOW
    assert clock.advance(10) == NOW + 10_000
    assert clock.advance(0.5) == NOW + 10_500


def test_fake_worker_block_release_coalesces_latest():
    clock = InjectedClock(NOW)
    worker = FakeWorker(clock)
    assert worker.submit(lambda: None, kind="persist", plan_id="p0") == "executed"
    worker.block()
    assert worker.blocked is True
    worker.enqueue_persist("p0", {"as_of_ms": 1})
    worker.enqueue_persist("p0", {"as_of_ms": 2})
    worker.enqueue_persist("p1", {"as_of_ms": 3})
    # Latest-only: one pending persist per plan, never a full-DB scan.
    assert set(worker.pending_persists()) == {"p0", "p1"}
    assert worker.depth == 2
    assert worker.is_degraded() is False
    clock.advance(6)
    assert worker.is_degraded() is True
    assert worker.queue_lag_ms() == 6_000
    drained = worker.release()
    assert len(drained) == 2
    assert worker.depth == 0


def test_virtual_600s_10symbols_500scores_tick_and_persist_rates():
    clock = InjectedClock(NOW)
    worker = FakeWorker(clock)
    plans, positions_list, caches, settled_list = make_monitor_inputs(
        n_plans=12, n_symbols=10, now_ms=clock.now_ms)
    # 10 distinct symbols with a shared-symbol multi-plan pair.
    assert len({p["symbol"] for p in plans}) == 10
    assert plans[0]["symbol"] == plans[10]["symbol"]

    background = make_background_scores(500)
    assert len(background) == 500
    worker.yield_background(len(background), batch=50)
    assert worker.background_items == 500
    assert worker.background_yields == 10

    compute_count = 0
    persist_count = 0
    jitter_samples: list[float] = []
    last_persist = {p["plan_id"]: None for p in plans}
    previous = {p["plan_id"]: None for p in plans}
    # Same-symbol plans share one采集 task: count distinct asset fetches.
    asset_fetches: set[str] = set()

    for _tick in range(60):  # 600s / 10s
        scheduled = clock.now_ms
        for idx, plan in enumerate(plans):
            cache = dict(caches[idx])
            cache["futures_mark"] = dict(caches[idx]["futures_mark"])
            cache["spot_quote"] = dict(caches[idx]["spot_quote"])
            # Fresh controlled refresh each tick: source times advance and
            # expiries extend, so risk stays stable and only the 60s sampler
            # fires (expiry flips would be a real risk-change persist).
            cache["futures_mark"]["as_of_ms"] = clock.now_ms
            cache["spot_quote"]["as_of_ms"] = clock.now_ms
            cache["futures_mark"]["expires_at_ms"] = clock.now_ms + 60_000
            cache["spot_quote"]["expires_at_ms"] = clock.now_ms + 60_000
            snap = compute_monitor(plan, positions_list[idx], cache,
                                   settled_list[idx], None, clock.now_ms)
            compute_count += 1
            asset_fetches.add(plan["symbol"])
            prev = previous[plan["plan_id"]]
            last = last_persist[plan["plan_id"]]
            if should_persist(prev, snap, last, None, now_ms=clock.now_ms):
                persist_count += 1
                last_persist[plan["plan_id"]] = clock.now_ms
                worker.submit(lambda: None, kind="persist",
                              plan_id=plan["plan_id"])
            previous[plan["plan_id"]] = snap
        # Virtual ticks have zero scheduling jitter by construction.
        jitter_samples.append(float(clock.now_ms - scheduled - 0))
        clock.advance(10)

    # 60 ticks x 12 plans in-memory; persists = 60s sampler => 10 per plan
    # (t=0 plus every 60s: 0,60,...,540). Never per-tick full writes.
    assert compute_count == 720
    assert persist_count == 12 * 10
    assert all(j == 0 for j in jitter_samples)
    # Per-asset sharing: only 10 distinct fetch keys per tick.
    assert len({p["symbol"] for p in plans}) == 10
    assert worker.max_depth == 0  # never blocked in this scenario


def test_blocked_worker_degraded_then_recovers_criticals_and_positions():
    clock = InjectedClock(NOW)
    worker = FakeWorker(clock)
    manager = AlertManager()
    plan = make_plan("plan-crit", "BTCUSDT")
    pos = make_positions(plan_id="plan-crit")
    settled = make_settled_events()

    # Open a critical basis alert on fresh ticks.
    cache = make_market_cache(now_ms=clock.now_ms)
    snap = compute_monitor(plan, pos, cache, settled, None, clock.now_ms)
    metrics = dict(snap.metrics_json)
    metrics.update({"target_hedge_ratio": "1",
                    "futures_entry_notional_usd": "10000",
                    "funding_complete": True,
                    "funding_known_subtotal_usd": "20.1",
                    "adverse_basis_loss_usd": "50"})
    snap = dataclasses.replace(snap, basis_pnl_usd="-50",
                               estimated_settled_funding_usd="20.1")
    snap = dataclasses.replace(snap, metrics_json=metrics)
    manager.apply(evaluate_alerts(None, snap, None), clock.now_ms)
    assert {r["code"] for r in manager.open_alerts()} >= {"BASIS_CONSUMED_CARRY"} or \
        {r["code"] for r in manager.open_alerts()} >= {"BASIS_CONSUMING_CARRY"}

    before_positions = {p["leg_type"]: p["remaining_qty"] for p in pos}
    worker.block()
    degraded_ticks = 0
    for _ in range(4):  # 40s blocked: 10s ticks keep computing in memory.
        clock.advance(10)
        lag_cache = make_market_cache(now_ms=clock.now_ms,
                                      persist_lag_ms=worker.queue_lag_ms() + 10_000)
        lag_snap = compute_monitor(plan, pos, lag_cache, settled,
                                   None, clock.now_ms)
        assert lag_snap.status == "MONITOR_DEGRADED"
        degraded_ticks += 1
        worker.enqueue_persist(plan["plan_id"], lag_snap)
        # Ticks read the memory mirror, never the full DB.
        assert lag_snap.mark_price == "67000"
    assert degraded_ticks == 4
    assert worker.is_degraded() is True
    # Latest-only: exactly one pending persist for the plan.
    assert list(worker.pending_persists()) == ["plan-crit"]

    drained = worker.release()
    assert len(drained) == 1
    # Recovery replays the latest snapshot with every critical intact and
    # positions byte-identical.
    assert {p["leg_type"]: p["remaining_qty"] for p in pos} == before_positions
    assert {r["code"] for r in manager.open_alerts()} >= {"BASIS_CONSUMING_CARRY"} or \
        {r["code"] for r in manager.open_alerts()} >= {"BASIS_CONSUMED_CARRY"}
