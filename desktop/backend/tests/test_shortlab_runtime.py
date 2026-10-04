"""Task 13: Short-Lab service / runtime / scheduler (Phase 2).

Covers ``ShortLab_Implementation_Plan_CN.md`` Task 13 and
``ShortLab_Detailed_Design_CN.md`` sections 6 / 19 / 22 / 23:

- DB migration failure only marks Short-Lab unavailable (legacy Dive --
  ``create_app`` / ``/api/scan`` cache / engine loader -- is untouched).
- Same-``job_type`` manual + scheduled refreshes reuse the in-flight
  ``job_id`` (single instance); shutdown awaits/cancels background tasks
  and closes the DB worker.
- Provider failures degrade symbols to null + reason, never modify the
  legacy scan cache, and never stop the service from serving.
- Inject ``ProviderRegistry`` + effective-tier selection default LITE;
  future provider modules are never imported; ``evidence_summary`` is an
  explicit ``Unavailable`` until Task 16 wires metrics.
- Pipeline order universe -> cheap filter -> identity -> features ->
  LTSS -> risk -> top-N Entry -> save_entry -> save_feature ->
  save_score(entry_snapshot_id); funding batches of 80 symbols per
  5 minutes with cross-batch continuation; one Entry round of 240 calls;
  job stats count succeeded / failed / deferred.
- Risk authority is Task 11 ``evaluate_risks`` / ``derive_status`` alone:
  HALT/BREAK-style non-terminal states veto as ``VETO_LOW_LIQUIDITY``,
  never ``VETO_CONTRACT_DELISTING``.
- Scheduler jitter is injected per execution (fixed sources pin the 0s
  and 300s boundaries); manual refresh skips jitter but shares the lock.
- CoinGecko market (3600s) / supply (21600s): one market response
  refreshes both clocks, no duplicate supply request.

All network access is faked; no live requests.
"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from diveintocrypto_desktop.shortlab.config import load_shortlab_config
from diveintocrypto_desktop.shortlab.models import ProviderResult
from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
from diveintocrypto_desktop.shortlab.repository import (
    EntrySnapshotRecord,
    MigrationError,
    ShortLabRepository,
)
from diveintocrypto_desktop.shortlab.runtime import ShortLabRuntime
from diveintocrypto_desktop.shortlab.scheduler import (
    ShortLabScheduler,
    UnknownJobError,
    default_jitter_fn,
)
from diveintocrypto_desktop.shortlab.service import (
    EVIDENCE_UNAVAILABLE_REASON,
    JOB_TYPE_SCORE_REFRESH,
    CandidateQuery,
    EvidenceSummary,
    JobNotFound,
    ShortLabService,
    ShortLabUnavailable,
    SymbolNotFound,
    Unavailable,
    UnknownJobType,
)

DAY_MS = 86_400_000
NOW = 1_750_000_000_000

ENTRY_BLOCKS = ("consensus", "mtf", "micro", "regime", "failed_bounce", "funding")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start_ms: int = NOW) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return int(self.ms)

    def advance(self, ms: int) -> None:
        self.ms += ms


def make_universe(n: int, *, price: float = 4.0, ch: float = 1.0) -> list[dict]:
    return [
        {"s": f"T{i:03d}USDT", "price": price, "ch": ch, "quote_volume": float(1_000_000_000 - i)}
        for i in range(n)
    ]


def make_metadata(symbols: list[str], clock_ms: int, *, status: str = "TRADING",
                  onboard_ms: int | None = None) -> dict[str, dict]:
    out = {}
    for symbol in symbols:
        out[symbol] = {
            "symbol": symbol,
            "onboard_at_ms": onboard_ms if onboard_ms is not None else clock_ms - 400 * DAY_MS,
            "first_seen_ms": clock_ms - 400 * DAY_MS,
            "delivery_at_ms": None,
            "status": status,
            "contract_type": "PERPETUAL",
            "observed_at_ms": clock_ms,
            "quote_to_usd": "1",  # Explicit verified FX in this offline happy-path fixture.
            "contract_multiplier": None,
            "multiplier_source": None,
        }
    return out


def make_overrides(symbols: list[str], *, coingecko_id: str | None = None) -> dict:
    return {
        symbol: {
            "canonical_id": symbol.lower(),
            "display_symbol": symbol,
            "contract_multiplier": 1.0,
            "multiplier_source": "MANUAL",
            **({"coingecko_id": coingecko_id} if coingecko_id else {}),
        }
        for symbol in symbols
    }


def funding_events(as_of_ms: int, days: int = 90, rate: float = 0.0001) -> list[dict]:
    step = 8 * 3_600_000
    start = as_of_ms - days * DAY_MS
    out = []
    t = start - (start % step)
    while t <= as_of_ms:
        if t >= start:
            out.append({"t": t, "funding_rate": rate, "mark_price": 4.0})
        t += step
    return out


async def fake_market(symbol: str, as_of_ms: int) -> dict:
    closes = [100.0 - i * 0.5 for i in range(100)]
    highs = [c * 1.01 for c in closes]
    return {
        "fetched_at_ms": as_of_ms,
        "daily_closes": closes,
        "daily_highs": highs,
        "futures_qv_1d": 50_000_000.0,
        "oi_value_usd": 10_000_000.0,
        "spread": 0.001,
        "best_bid": 3.999,
        "best_ask": 4.001,
        "bid_notional_1pct": 2_000_000.0,
        "ask_notional_1pct": 2_000_000.0,
    }


def fake_spot_unavailable(identity: Any, as_of_ms: int) -> ProviderResult[None]:
    return ProviderResult(
        status="UNAVAILABLE", source="spot", fetched_at_ms=as_of_ms, as_of_ms=None,
        data=None, stale=False, reason_code="SPOT_FETCH_FAILED", error_message=None,
    )


class FakeCoinGecko:
    """Counting fake fundamentals provider (market response carries supply)."""

    name = "coingecko"

    def __init__(self, clock: FakeClock, *, with_supply: bool = True) -> None:
        self._clock = clock
        self.calls: list[Any] = []
        self.with_supply = with_supply

    async def fetch(self, identity: Any) -> ProviderResult[dict]:
        self.calls.append(getattr(identity, "binance_futures_symbol", None))
        data = {
            "market_cap_usd": 500_000_000.0,
            "fdv_usd": 2_000_000_000.0,
            "ath_usd": 10.0,
            "ath_date_ms": NOW - 300 * DAY_MS,
            "categories": [],
        }
        if self.with_supply:
            data.update({"circulating_supply": 10_000_000.0, "total_supply": 50_000_000.0})
        return ProviderResult(
            status="OK", source="coingecko", fetched_at_ms=self._clock(),
            as_of_ms=self._clock(), data=data, stale=False,
            reason_code=None, error_message=None,
        )


def make_entry_record(symbol: str, as_of_ms: int, score: float | None = 75.0,
                      reason: str | None = None) -> EntrySnapshotRecord:
    meta = {
        block: {"status": "OK" if score is not None else "UNAVAILABLE",
                "fetched_at_ms": as_of_ms, "as_of_ms": as_of_ms,
                "coverage_fraction": 1.0, "reason_code": reason, "source": "fake-entry"}
        for block in ENTRY_BLOCKS
    }
    return EntrySnapshotRecord(
        snapshot_id=f"entry-{symbol}-{as_of_ms}-entry-v1", symbol=symbol, as_of_ms=as_of_ms,
        entry_version="entry-v1", dive_weights_hash="w", dive_engine_version="e",
        dive_config_hash="c", primary_tf="1h", inputs={"consensus": {"finalSignal": "SELL"}},
        components={"total": score}, source_meta=meta, entry_score=score,
        created_at_ms=as_of_ms,
    )


class FakeEntryResult:
    def __init__(self, record: EntrySnapshotRecord, reason: str | None = None) -> None:
        self._record = record
        self.symbol = record.symbol
        self.entry_score = record.entry_score
        self.reason_code = reason
        self.snapshot_id = record.snapshot_id

    def to_record(self) -> EntrySnapshotRecord:
        return self._record


class FakeEntryRunner:
    """Stand-in for ``run_entry_batch``; captures the budget it is given."""

    def __init__(self, clock_ms: int = NOW, *, score: float | None = 75.0,
                 queued: tuple[str, ...] = ()) -> None:
        from diveintocrypto_desktop.shortlab.entry import EntryBatchResult

        self._batch_cls = EntryBatchResult
        self._clock_ms = clock_ms
        self._score = score
        self._queued = tuple(queued)
        self.captured: dict = {}

    async def __call__(self, symbols: list[str], *, budget: Any, max_symbols: int,
                       as_of_ms: int, now_ms: int) -> Any:
        self.captured = {
            "symbols": list(symbols),
            "max_calls": budget.max_calls,
            "concurrency": budget.concurrency,
            "ttl_sec": budget.ttl_sec,
            "max_symbols": max_symbols,
        }
        items = tuple(
            FakeEntryResult(make_entry_record(s, as_of_ms, self._score))
            for s in symbols[:max_symbols] if s not in self._queued
        )
        return self._batch_cls(items=items, queued_symbols=self._queued,
                               stats={"calls_made": 18 * len(items), "cache_hits": 0})


async def open_repo(tmp_path, clock: FakeClock) -> ShortLabRepository:
    repo = await ShortLabRepository.open(db_path=tmp_path / "shortlab.duckdb")
    await repo.migrate()
    return repo


def make_service(repo: Any, clock: FakeClock, n: int = 2, *,
                 registry: Any | None = None, entry_runner: Any | None = None,
                 overrides: Any = "default", metadata: Any = "default",
                 funding_fn: Any = "default", status: str = "TRADING",
                 metrics_provider: Any = None) -> tuple[ShortLabService, dict]:
    rows = make_universe(n)
    symbols = [r["s"] for r in rows]
    observed = {
        "fetched_symbols": [],
    }

    async def universe_fn(limit: int | None = None) -> list[dict]:
        return list(rows[:limit] if limit else rows)

    meta = make_metadata(symbols, clock(), status=status) if metadata == "default" else metadata

    async def metadata_fn() -> dict:
        return dict(meta)

    if funding_fn == "default":
        async def funding_fn_inner(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
            observed["fetched_symbols"].append(symbol)
            return funding_events(end_ms)
    else:
        funding_fn_inner = funding_fn

    from diveintocrypto_desktop.data import funding as funding_mod

    service = ShortLabService(
        config=load_shortlab_config(),
        repository=repo,
        registry=registry or ProviderRegistry(),
        clock=clock,
        universe_fn=universe_fn,
        metadata_fn=metadata_fn,
        funding_history_fn=funding_fn_inner,
        funding_coverage_fn=funding_mod.funding_coverage,
        spot_history_fn=fake_spot_unavailable,
        market_inputs_fn=fake_market,
        identity_candidates_fn=lambda symbol: [],
        identity_overrides=make_overrides(symbols) if overrides == "default" else overrides,
        entry_runner=entry_runner or FakeEntryRunner(clock()),
        metrics_provider=metrics_provider,
    )
    return service, observed


# ---------------------------------------------------------------------------
# Scheduler: injected jitter, boundaries, manual path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_jitter_zero_boundary() -> None:
    seen_max: list[float] = []
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def jitter_fn(max_sec: float) -> float:
        seen_max.append(max_sec)
        return 0.0

    sched = ShortLabScheduler(jitter_fn=jitter_fn, sleep=fake_sleep,
                              default_jitter_max_sec=300.0)
    runs: list[int] = []

    async def tick() -> None:
        runs.append(1)
        if len(runs) >= 2:
            sched._shutdown.set()

    sched.register("score_refresh", 60.0, tick)
    await asyncio.wait_for(asyncio.create_task(sched._run_loop(sched._jobs["score_refresh"])), timeout=5)
    assert seen_max == [300.0, 300.0]  # independently drawn before every execution
    assert sleeps == [60.0]  # zero jitter never sleeps
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_scheduler_jitter_300_boundary() -> None:
    sleeps: list[float] = []
    jitter_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    sched = ShortLabScheduler(jitter_fn=lambda m: (jitter_calls.append(m), 300.0)[1],
                              sleep=fake_sleep, default_jitter_max_sec=300.0)
    runs = 0

    async def tick() -> None:
        nonlocal runs
        runs += 1
        if runs >= 2:
            sched._shutdown.set()

    sched.register("score_refresh", 60.0, tick)
    await asyncio.wait_for(asyncio.create_task(sched._run_loop(sched._jobs["score_refresh"])), timeout=5)
    assert jitter_calls == [300.0, 300.0]
    assert sleeps == [300.0, 60.0, 300.0]


@pytest.mark.asyncio
async def test_manual_trigger_skips_jitter_but_shares_lock() -> None:
    jitter_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:  # must never be awaited on the manual path
        raise AssertionError("manual refresh must not sleep for jitter")

    sched = ShortLabScheduler(jitter_fn=lambda m: jitter_calls.append(m) or 0.0,
                              sleep=fake_sleep, default_jitter_max_sec=300.0)
    calls: list[str] = []

    async def tick() -> str:
        calls.append("ran")
        await asyncio.sleep(0.01)
        return "ok"

    sched.register("score_refresh", 60.0, tick)
    # Hold the job lock: a concurrent manual trigger must wait on the same lock.
    lock = sched.lock_for("score_refresh")
    await lock.acquire()
    task = asyncio.create_task(sched.trigger_now("score_refresh"))
    await asyncio.sleep(0.05)
    assert not task.done()
    assert jitter_calls == []
    lock.release()
    assert await asyncio.wait_for(task, timeout=5) == "ok"
    assert calls == ["ran"] and jitter_calls == []


@pytest.mark.asyncio
async def test_scheduler_tick_failure_does_not_kill_loop() -> None:
    sched = ShortLabScheduler(jitter_fn=lambda m: 0.0, sleep=lambda s: asyncio.sleep(0))
    attempts = 0

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        sched._shutdown.set()

    sched.register("score_refresh", 0.0, flaky)
    await asyncio.wait_for(asyncio.create_task(sched._run_loop(sched._jobs["score_refresh"])), timeout=5)
    assert attempts == 2
    assert sched._jobs["score_refresh"].runs == 1


def test_scheduler_rejects_unknown_job() -> None:
    sched = ShortLabScheduler()
    with pytest.raises(UnknownJobError):
        sched.lock_for("nope")


# ---------------------------------------------------------------------------
# Runtime: migration failure isolates Short-Lab; stop() shuts everything down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_failure_only_marks_shortlab_unavailable(tmp_path, monkeypatch) -> None:
    async def fail_open(cls, *args: Any, **kwargs: Any) -> Any:
        raise MigrationError("disk is on fire")

    monkeypatch.setattr(ShortLabRepository, "open", classmethod(fail_open))
    runtime = ShortLabRuntime()
    await runtime.start()  # must not raise
    assert runtime.available is False

    with pytest.raises(ShortLabUnavailable):
        runtime.service
    service = ShortLabService(config=load_shortlab_config(), repository=None)
    with pytest.raises(ShortLabUnavailable):
        await service.candidates()

    # Legacy Dive service is unaffected: app builds, scan cache untouched.
    from diveintocrypto_desktop.api import app as app_mod

    app_mod._scan_cache["13:sentinel"] = (time.monotonic(), {"sentinel": True})
    app = app_mod.create_app()
    routes = {getattr(r, "path", "") for r in app.routes}
    assert "/api/scan" in routes
    assert app_mod._scan_cache["13:sentinel"][1] == {"sentinel": True}
    del app_mod._scan_cache["13:sentinel"]

    from diveintocrypto_desktop.engine.loader import load_config as load_engine_config

    assert isinstance(load_engine_config(), dict)
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_stop_cancels_tasks_and_closes_db(tmp_path) -> None:
    clock = FakeClock()
    ticks: list[int] = []

    async def noop() -> dict:
        ticks.append(1)
        return {"noop": True}

    sched = ShortLabScheduler(jitter_fn=lambda m: 0.0, sleep=lambda s: asyncio.sleep(0))
    sched.register(JOB_TYPE_SCORE_REFRESH, 3600.0, noop)
    # Runtime-owned repository (the Task 14 lifespan path): stop() must
    # cancel/await the background task and close the DB worker.
    runtime = ShortLabRuntime(db_path=tmp_path / "rt.duckdb", scheduler=sched, clock=clock)
    await runtime.start()
    assert runtime.available is True
    assert sched.running
    await asyncio.sleep(0.05)
    assert ticks  # the periodic job really ran in the background
    owned = runtime.repository
    await runtime.stop()
    assert sched.pending_tasks() == ()
    assert owned._closed is True
    assert runtime.available is False


# ---------------------------------------------------------------------------
# Refresh: single instance per job_type, reuse of job id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_job_type_reuses_inflight_job_id(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)
    gate = asyncio.Event()
    real_universe = make_universe(1)

    async def gated_universe(limit: int | None = None) -> list[dict]:
        await gate.wait()
        return list(real_universe)

    service = ShortLabService(
        config=load_shortlab_config(), repository=repo, clock=clock,
        universe_fn=gated_universe,
        metadata_fn=lambda: make_metadata(["T000USDT"], clock()),
        funding_history_fn=lambda s, a, b: funding_events(b),
        spot_history_fn=fake_spot_unavailable, market_inputs_fn=fake_market,
        identity_overrides=make_overrides(["T000USDT"]),
        entry_runner=FakeEntryRunner(),
    )
    from diveintocrypto_desktop.data import funding as funding_mod
    service._funding_coverage_fn = funding_mod.funding_coverage

    first = await service.refresh()
    assert first.existing is False
    second = await service.refresh()
    assert second.existing is True and second.job_id == first.job_id
    reused = await service.run_refresh()
    assert reused.existing is True
    status = await service.job_status(first.job_id)
    assert status.status == "RUNNING"
    gate.set()
    final = await service.await_job(first.job_id)
    assert final.status == "SUCCEEDED"
    assert final.stats["succeeded"] == 1
    await repo.close()


@pytest.mark.asyncio
async def test_refresh_rejects_unknown_job_type(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)
    service = ShortLabService(config=load_shortlab_config(), repository=repo, clock=clock)
    with pytest.raises(UnknownJobType):
        await service.refresh("nope")
    with pytest.raises(JobNotFound):
        await service.job_status("missing-id")
    await repo.close()


# ---------------------------------------------------------------------------
# Provider failure isolation: old scan cache untouched, service still serves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_providers_failing_leaves_legacy_scan_cache(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)

    async def boom_history(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
        raise RuntimeError("binance down")

    async def boom_spot(identity: Any, as_of_ms: int) -> Any:
        raise RuntimeError("spot down")

    async def boom_market(symbol: str, as_of_ms: int) -> dict:
        raise RuntimeError("klines down")

    service = ShortLabService(
        config=load_shortlab_config(), repository=repo, clock=clock,
        universe_fn=lambda limit=None: make_universe(2),
        metadata_fn=lambda: make_metadata(["T000USDT", "T001USDT"], clock()),
        funding_history_fn=boom_history,
        spot_history_fn=boom_spot, market_inputs_fn=boom_market,
        identity_overrides=make_overrides(["T000USDT", "T001USDT"]),
        entry_runner=FakeEntryRunner(score=None),
    )
    from diveintocrypto_desktop.data import funding as funding_mod
    service._funding_coverage_fn = funding_mod.funding_coverage

    from diveintocrypto_desktop.api import app as app_mod
    app_mod._scan_cache["13:legacy"] = (time.monotonic(), {"survivors": [1, 2, 3]})

    status = await service.run_refresh()
    assert status.status == "SUCCEEDED"  # honest all-null scores, job completes
    assert status.stats["succeeded"] == 2 and status.stats["failed"] == 0
    assert app_mod._scan_cache["13:legacy"][1] == {"survivors": [1, 2, 3]}
    del app_mod._scan_cache["13:legacy"]

    page = await service.candidates()
    assert page.total == 2
    assert all(item.ltss is None for item in page.items)
    detail = await service.detail("T000USDT")
    assert detail.score.symbol == "T000USDT" and detail.feature is not None
    await repo.close()


# ---------------------------------------------------------------------------
# Pipeline order + persistence + read paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_order_and_persistence(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)
    registry = ProviderRegistry()
    fake_cg = FakeCoinGecko(clock)
    registry.register("coingecko", fake_cg)
    service, _ = make_service(repo, clock, n=2, registry=registry,
                              overrides="default")
    # Rebuild with coingecko ids so the market/supply clocks are exercised.
    symbols = ["T000USDT", "T001USDT"]
    service._identity_overrides = make_overrides(symbols, coingecko_id="bitcoin")

    calls: list[str] = []
    orig_entry, orig_feature = repo.save_entry, repo.save_feature
    orig_batch = repo.save_score_batch

    async def logged_entry(snapshot: Any) -> str:
        calls.append(f"save_entry:{snapshot.symbol}")
        return await orig_entry(snapshot)

    async def logged_feature(snapshot: Any) -> str:
        calls.append(f"save_feature:{snapshot.symbol}")
        return await orig_feature(snapshot)

    async def logged_batch(scores: Any, **kwargs: Any) -> str:
        calls.append(f"save_score_batch:{len(scores)}")
        return await orig_batch(scores, **kwargs)

    repo.save_entry, repo.save_feature, repo.save_score_batch = logged_entry, logged_feature, logged_batch  # type: ignore[method-assign]

    status = await service.run_refresh()
    assert status.status == "SUCCEEDED"
    assert status.stats["succeeded"] == 2
    assert set(status.stats) >= {"succeeded", "failed", "deferred", "funding_requested",
                                 "entry_calls_made", "duration_ms", "effective_tier"}
    assert status.stats["effective_tier"] == "LITE"
    # Per symbol: save_entry -> save_feature; the score batch commits last.
    assert calls[-1].startswith("save_score_batch:2")
    for symbol in symbols:
        assert calls.index(f"save_entry:{symbol}") < calls.index(f"save_feature:{symbol}")
        assert calls.index(f"save_feature:{symbol}") < calls.index("save_score_batch:2")

    page = await service.candidates(CandidateQuery(limit=10))
    assert page.total == 2 and len(page.items) == 2
    for item in page.items:
        assert item.ltss is not None  # happy fakes carry complete critical inputs
        assert item.entry_snapshot_id is not None  # top-2 are inside entry_depth_top=10
        entry = await repo.get_entry(item.entry_snapshot_id)
        assert entry is not None and entry.symbol == item.symbol
        assert entry.entry_score == 75.0

    detail = await service.detail("T001USDT")
    assert detail.generation_id == page.generation_id == status.job_id
    assert detail.entry is not None and detail.feature is not None
    with pytest.raises(SymbolNotFound):
        await service.detail("NOPEUSDT")
    await repo.close()


# ---------------------------------------------------------------------------
# Funding: 80 symbols per 5-minute batch, continuation across batches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_funding_batches_80_per_5min_with_continuation(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)
    service, observed = make_service(repo, clock, n=100)
    status1 = await service.run_refresh()
    assert status1.status == "SUCCEEDED"
    assert status1.stats["funding_requested"] == 80
    assert status1.stats["funding_deferred"] == 20
    first_batch = set(observed["fetched_symbols"])
    assert len(first_batch) == 80

    clock.advance(301_000)  # next 5-minute window
    observed["fetched_symbols"].clear()
    status2 = await service.run_refresh()
    # Complete archives from the first batch are reused; only the missing
    # 20 symbols consume sends in the second window.
    assert status2.stats["funding_requested"] == 20
    assert status2.stats["funding_deferred"] == 0
    second_batch = set(observed["fetched_symbols"])
    # The 20 symbols deferred in run 1 are picked up first in run 2.
    deferred = {f"T{i:03d}USDT" for i in range(100)} - first_batch
    assert deferred <= second_batch
    await repo.close()


# ---------------------------------------------------------------------------
# Entry: one 240-call round; CoinGecko market/supply clocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_round_uses_240_call_budget(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)
    runner = FakeEntryRunner()
    service, _ = make_service(repo, clock, n=3, entry_runner=runner)
    status = await service.run_refresh()
    assert status.status == "SUCCEEDED"
    assert runner.captured["max_calls"] == 240
    assert runner.captured["concurrency"] == 2
    assert runner.captured["ttl_sec"] == 3600
    assert runner.captured["max_symbols"] == 10
    assert status.stats["entry_calls_made"] == 18 * 3
    await repo.close()


@pytest.mark.asyncio
async def test_coingecko_market_response_refreshes_supply_without_extra_call(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)
    registry = ProviderRegistry()
    fake_cg = FakeCoinGecko(clock)
    registry.register("coingecko", fake_cg)
    service, _ = make_service(repo, clock, n=2, registry=registry)
    service._identity_overrides = make_overrides(["T000USDT", "T001USDT"], coingecko_id="bitcoin")

    await service.run_refresh()
    assert len(fake_cg.calls) == 1  # one shared coingecko_id, one request
    await service.run_refresh()
    assert len(fake_cg.calls) == 1  # market clock (3600s) still fresh: no request
    clock.advance(3_601_000)
    await service.run_refresh()
    # Market stale -> exactly one refetch; the supply in that response
    # refreshes the 21600s supply clock, so no second supply request.
    assert len(fake_cg.calls) == 2
    clock.advance(3_601_000)
    await service.run_refresh()
    assert len(fake_cg.calls) == 3
    await repo.close()


@pytest.mark.asyncio
async def test_supply_unseen_triggers_independent_refresh(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)
    registry = ProviderRegistry()
    fake_cg = FakeCoinGecko(clock, with_supply=False)  # market never carries supply
    registry.register("coingecko", fake_cg)
    service, _ = make_service(repo, clock, n=1, registry=registry)
    service._identity_overrides = make_overrides(["T000USDT"], coingecko_id="bitcoin")

    await service.run_refresh()
    await service.run_refresh()  # supply never seen -> independent refresh each run
    assert len(fake_cg.calls) == 2
    await repo.close()


# ---------------------------------------------------------------------------
# Risk authority: Task 11 alone decides; HALT is low-liquidity, not delisting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halt_state_vetoes_low_liquidity_not_delisting(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)
    registry = ProviderRegistry()
    registry.register("coingecko", FakeCoinGecko(clock))
    symbols = ["T000USDT", "T001USDT"]
    meta = make_metadata(symbols, clock(), status="TRADING")
    meta["T001USDT"] = dict(meta["T001USDT"], status="HALT")
    service, _ = make_service(repo, clock, n=2, registry=registry, metadata=meta)
    service._identity_overrides = make_overrides(symbols, coingecko_id="bitcoin")

    status = await service.run_refresh()
    assert status.status == "SUCCEEDED"
    halted = await service.detail("T001USDT")
    assert "VETO_LOW_LIQUIDITY" in halted.score.vetoes
    assert "VETO_CONTRACT_DELISTING" not in halted.score.vetoes
    assert halted.score.execution_status == "BLOCKED"
    await repo.close()


# ---------------------------------------------------------------------------
# Tier selection without importing future modules; evidence Unavailable
# ---------------------------------------------------------------------------


def test_effective_tier_defaults_lite_without_full_imports(tmp_path) -> None:
    clock = FakeClock()
    service = ShortLabService(config=load_shortlab_config(), repository=None, clock=clock)
    assert service.effective_tier() == ("LITE", ())
    tier, warnings = service.effective_tier("FULL")
    assert tier == "LITE" and warnings == ("FULL_PREREQUISITE_MISSING",)
    for module in ("unlock", "social", "catalyst"):
        assert f"diveintocrypto_desktop.shortlab.providers.{module}" not in sys.modules

    registry = ProviderRegistry()
    for name in ("unlock", "social", "catalyst"):
        registry.register(name, ProviderRegistry().get(name))
    service_full = ShortLabService(config=load_shortlab_config(), repository=None,
                                   registry=registry, clock=clock)
    assert service_full.effective_tier("FULL") == ("FULL", ())


@pytest.mark.asyncio
async def test_evidence_summary_unavailable_until_metrics_wired(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path, clock)
    service, _ = make_service(repo, clock, n=1)
    missing = await service.evidence_summary({"horizon": "30D"})
    assert isinstance(missing, Unavailable)
    assert missing.reason == EVIDENCE_UNAVAILABLE_REASON

    async def metrics(filters: Any) -> EvidenceSummary:
        return EvidenceSummary(filters=dict(filters), horizons={"30D": {"n": 5}},
                               total=5, generated_at_ms=clock())

    service._metrics_provider = metrics
    summary = await service.evidence_summary({"horizon": "30D"})
    assert isinstance(summary, EvidenceSummary) and summary.total == 5
    await repo.close()
