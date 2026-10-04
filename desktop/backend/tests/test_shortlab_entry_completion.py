import asyncio
from diveintocrypto_desktop.shortlab.entry import EntryBudget
from diveintocrypto_desktop.shortlab.request_budget import ObservedCache


def test_entry_cache_records_completion_and_preserves_hit_provenance():
    async def run():
        clock = [1000]
        shared = ObservedCache(clock=lambda: clock[0])
        budget = EntryBudget(clock=lambda: clock[0], shared_cache=shared)
        async def fetch():
            clock[0] = 2000
            return {"price": 2}
        key = ("klines", "BTCUSDT", "1h")
        await budget.guarded(key, fetch)
        observed = shared.get(budget._shared_key(key), 2000)
        assert observed.meta.fetched_at_ms == 2000
        assert observed.meta.known_at_ms == 2000
        clock[0] = 3000
        await budget.guarded(key, fetch)
        assert budget.observation_times[key] == (2000, 2000)
    asyncio.run(run())


def test_finalize_freezes_cutoff_without_rewriting_source_and_invalidates_day():
    from diveintocrypto_desktop.shortlab.entry import EntryResult, finalize_entry_snapshot
    result = EntryResult(symbol="BTCUSDT", as_of_ms=1000, primary_tf="1h",
        entry_score=None, components={}, missing_blocks=(), reason_code=None,
        inputs={}, source_meta={"funding": {"known_at_ms": 2000, "as_of_ms": 1000}},
        dive_weights_hash="w", dive_engine_version="v", dive_config_hash="d",
        shortlab_config_hash="s", snapshot_id="original", fetched_at_ms=2000,
        created_at_ms=2000)
    frozen = finalize_entry_snapshot(result, 3000)
    assert frozen.as_of_ms == 3000
    assert frozen.source_meta["funding"]["as_of_ms"] == 1000
    assert finalize_entry_snapshot(result, 86400000).reason_code == "ENTRY_WINDOW_ROLLOVER"
    import pytest
    with pytest.raises(ValueError):
        finalize_entry_snapshot(result, 1500)


def test_round_budget_counts_final_sends_and_releases_unsent():
    from diveintocrypto_desktop.shortlab.request_budget import RequestBudget, make_request_context, Denied
    shared = RequestBudget(max_sends=100)
    entry = EntryBudget(max_calls=2, request_context=make_request_context(shared))
    sender = entry.request_context.budget
    p1 = sender.try_acquire("fapi", 1, "entry", "klines")
    p2 = sender.try_acquire("fapi", 1, "entry", "klines")
    assert isinstance(sender.try_acquire("fapi", 1, "entry", "klines"), Denied)
    assert entry.used_calls == 0
    p2.release_unsent()
    p1.mark_sent()
    retry = sender.try_acquire("fapi", 1, "entry", "klines")
    retry.mark_sent()
    assert entry.used_calls == 2
    assert isinstance(sender.try_acquire("fapi", 1, "entry", "klines"), Denied)
