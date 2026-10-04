"""F06a+F06b: base-run contract + default callback wiring (plan F06, design A7.3/A8/A9).

Covers the F06a delivery (base-run contract) plus the F06b final wiring
(same F06 owner, after F07/F09 deliver callbacks):

- default Runtime wires IdentityCatalog/RequestBudget/ObservedCache/
  Repository/QualityPolicy/clock; JobContext(repository,config,clock_ms,
  request_budget,trace_id,data_dir) is the only context consumers take.
- job adapters share ``async run(context) -> JobStatus``
  (score_refresh/funding_backfill/metadata/contract_refresh/grader/
  maintenance); funding page queue/cursor/nextAllowed persist via F01
  save/load_cursor; cache hits survive a denied budget (never cleared);
  tracked = live + history; per-coin frozen decision cutoff then score;
  base tables first with ID refs, score batch + SUCCEEDED atomically; stop
  awaits scheduler/service before DB close.
- F06b wires the defaults on the single shared service instance:
  ``evidence.jobs.run_due`` via ``register_grader_callback`` and
  ``maintenance.maintain`` via ``register_retention_callback``; scheduler
  exposes score_refresh + grader + maintenance and each runs once
  successfully under fake HTTP without a second service.

All network access is faked; no live requests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from diveintocrypto_desktop.shortlab.config import load_shortlab_config
from diveintocrypto_desktop.shortlab.models import ProviderResult
from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
from diveintocrypto_desktop.shortlab.request_budget import ObservedCache, RequestBudget
from diveintocrypto_desktop.shortlab.runtime import ShortLabRuntime
from diveintocrypto_desktop.shortlab.scheduler import ShortLabScheduler
from diveintocrypto_desktop.shortlab.service import (
    JOB_TYPE_CONTRACT_REFRESH,
    JOB_TYPE_FUNDING_BACKFILL,
    JOB_TYPE_GRADER,
    JOB_TYPE_MAINTENANCE,
    JOB_TYPE_METADATA,
    JOB_TYPE_SCORE_REFRESH,
    JobContext,
    ShortLabService,
    UnknownJobType,
)

DAY_MS = 86_400_000
NOW = 1_750_000_000_000


class FakeClock:
    def __init__(self, start_ms: int = NOW) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return int(self.ms)

    def advance(self, ms: int) -> None:
        self.ms += ms


def make_universe(n: int) -> list[dict]:
    return [
        {"s": f"T{i:03d}USDT", "price": 4.0, "ch": 1.0, "quote_volume": float(1_000_000_000 - i)}
        for i in range(n)
    ]


def make_metadata(symbols: list[str], clock_ms: int) -> dict[str, dict]:
    out = {}
    for symbol in symbols:
        out[symbol] = {
            "symbol": symbol,
            "onboard_at_ms": clock_ms - 400 * DAY_MS,
            "first_seen_ms": clock_ms - 400 * DAY_MS,
            "delivery_at_ms": None,
            "status": "TRADING",
            "contract_type": "PERPETUAL",
            "observed_at_ms": clock_ms,
            "contract_multiplier": None,
            "multiplier_source": None,
        }
    return out


def make_overrides(symbols: list[str]) -> dict:
    return {
        s: {"canonical_id": s.lower(), "display_symbol": s,
            "contract_multiplier": 1.0, "multiplier_source": "MANUAL"}
        for s in symbols
    }


def funding_events_light(as_of_ms: int, n: int = 5, rate: float = 0.0001) -> list[dict]:
    step = 8 * 3_600_000
    return [{"t": as_of_ms - i * step, "funding_rate": rate, "mark_price": 4.0}
            for i in range(n)]


def funding_events_90d(as_of_ms: int, rate: float = 0.0001) -> list[dict]:
    step = 8 * 3_600_000
    start = as_of_ms - 90 * DAY_MS
    out, t = [], start - (start % step)
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
    name = "coingecko"

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[Any] = []

    async def fetch(self, identity: Any) -> ProviderResult[dict]:
        self.calls.append(getattr(identity, "binance_futures_symbol", None))
        return ProviderResult(
            status="OK", source="coingecko", fetched_at_ms=self._clock(),
            as_of_ms=self._clock(),
            data={"market_cap_usd": 500_000_000.0, "fdv_usd": 2_000_000_000.0,
                  "ath_usd": 10.0, "ath_date_ms": NOW - 300 * DAY_MS,
                  "circulating_supply": 10_000_000.0, "total_supply": 50_000_000.0,
                  "categories": []},
            stale=False, reason_code=None, error_message=None,
        )


class FakeEntryRunner:
    def __init__(self, clock_ms: int = NOW) -> None:
        from diveintocrypto_desktop.shortlab.entry import EntryBatchResult

        self._batch_cls = EntryBatchResult
        self._clock_ms = clock_ms

    async def __call__(self, symbols: list[str], *, budget: Any, max_symbols: int,
                       as_of_ms: int, now_ms: int) -> Any:
        from diveintocrypto_desktop.shortlab.repository import EntrySnapshotRecord

        items = []
        for s in symbols[:max_symbols]:
            meta = {b: {"status": "OK", "fetched_at_ms": as_of_ms, "as_of_ms": as_of_ms,
                        "coverage_fraction": 1.0, "reason_code": None, "source": "fake"}
                    for b in ("consensus", "mtf", "micro", "regime", "failed_bounce", "funding")}
            rec = EntrySnapshotRecord(
                snapshot_id=f"entry-{s}-{as_of_ms}-entry-v1", symbol=s, as_of_ms=as_of_ms,
                entry_version="entry-v1", dive_weights_hash="w", dive_engine_version="e",
                dive_config_hash="c", primary_tf="1h",
                inputs={"consensus": {"finalSignal": "SELL"}}, components={"total": 75.0},
                source_meta=meta, entry_score=75.0, created_at_ms=as_of_ms,
            )

            class _R:
                def __init__(self, r: Any) -> None:
                    self._r, self.symbol, self.entry_score = r, r.symbol, r.entry_score
                    self.reason_code, self.snapshot_id = None, r.snapshot_id

                def to_record(self) -> Any:
                    return self._r

            items.append(_R(rec))
        return self._batch_cls(items=tuple(items), queued_symbols=(), stats={})


async def open_repo(tmp_path: Path) -> ShortLabRepository:
    repo = await ShortLabRepository.open(db_path=tmp_path / "shortlab.duckdb")
    await repo.migrate()
    return repo


def make_service(repo: Any, clock: FakeClock, rows: list[dict], *,
                 funding_fn: Any = None, registry: Any | None = None,
                 budget: Any | None = None, cache: Any | None = None) -> ShortLabService:
    symbols = [r["s"] for r in rows]
    meta = make_metadata(symbols, clock())

    async def universe_fn(limit: int | None = None) -> list[dict]:
        return list(rows[:limit] if limit else rows)

    async def metadata_fn() -> dict:
        return dict(meta)

    if funding_fn is None:
        async def funding_fn_inner(symbol: str, a: int, b: int) -> list[dict]:
            return funding_events_light(b)
    else:
        funding_fn_inner = funding_fn

    from diveintocrypto_desktop.data import funding as funding_mod

    return ShortLabService(
        config=load_shortlab_config(), repository=repo,
        registry=registry or ProviderRegistry(), clock=clock,
        universe_fn=universe_fn, metadata_fn=metadata_fn,
        funding_history_fn=funding_fn_inner,
        funding_coverage_fn=funding_mod.funding_coverage,
        spot_history_fn=fake_spot_unavailable, market_inputs_fn=fake_market,
        identity_candidates_fn=lambda s: [], identity_overrides=make_overrides(symbols),
        entry_runner=FakeEntryRunner(clock()), request_budget=budget, observed_cache=cache,
    )


# ---------------------------------------------------------------------------
# 1. Default Runtime wiring + full base tables with ID references
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_runtime_wires_shared_handles(tmp_path) -> None:
    clock = FakeClock()
    runtime = ShortLabRuntime(db_path=tmp_path / "rt.duckdb", clock=clock)
    await runtime.start()
    try:
        assert runtime.available is True
        assert runtime.request_budget is not None
        assert runtime.observed_cache is not None
        assert runtime.identity_catalog is not None
        assert runtime.quality_policy is not None
        assert runtime.service.request_budget is runtime.request_budget
        assert runtime.service.observed_cache is runtime.observed_cache
        assert runtime.service.identity_catalog is runtime.identity_catalog
        ctx = runtime.make_context(JOB_TYPE_SCORE_REFRESH)
        assert isinstance(ctx, JobContext)
        assert ctx.repository is runtime.repository
        assert ctx.config is runtime.config
        assert ctx.request_budget is runtime.request_budget
        assert callable(ctx.clock_ms)
        assert isinstance(ctx.trace_id, str) and ctx.trace_id
        # JobContext required fields per F06 contract.
        import dataclasses as _dc

        names = {f.name for f in _dc.fields(JobContext)}
        assert {"repository", "config", "clock_ms", "request_budget", "trace_id", "data_dir"} <= names
        # F06b default wiring: base-run + grader/retention callbacks registered
        # on the single shared service (no second service).
        job_types = set(runtime.scheduler.job_types())
        assert {JOB_TYPE_SCORE_REFRESH, JOB_TYPE_FUNDING_BACKFILL,
                JOB_TYPE_CONTRACT_REFRESH, JOB_TYPE_METADATA,
                JOB_TYPE_GRADER, JOB_TYPE_MAINTENANCE} <= job_types
        from diveintocrypto_desktop.shortlab.evidence.jobs import run_due as _expected_grader
        from diveintocrypto_desktop.shortlab.maintenance import maintain as _expected_maintain

        assert runtime.service.grader_callback is _expected_grader
        assert runtime.service.retention_callback is _expected_maintain
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_default_runtime_writes_all_base_tables_with_refs(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    registry = ProviderRegistry()
    registry.register("coingecko", FakeCoinGecko(clock))
    rows = make_universe(2)
    symbols = [r["s"] for r in rows]

    async def funding_fn(symbol: str, a: int, b: int) -> list[dict]:
        return funding_events_90d(b)

    service = make_service(repo, clock, rows, funding_fn=funding_fn, registry=registry,
                           budget=RequestBudget(clock=clock), cache=ObservedCache(clock=clock))
    # Bind CoinGecko IDs so fundamentals persist (verified manual overrides).
    service._identity_overrides = {
        s: {"canonical_id": s.lower(), "display_symbol": s,
            "contract_multiplier": 1.0, "multiplier_source": "MANUAL",
            "coingecko_id": "bitcoin"}
        for s in symbols
    }
    # Simulate default Runtime injection (service built with fakes + default handles).
    runtime = ShortLabRuntime(repository=repo, service=service, clock=clock,
                              request_budget=service.request_budget,
                              observed_cache=service.observed_cache,
                              db_path=tmp_path / "unused.duckdb")
    # Bypass start (repository already open); wire minimal scheduler for stop-order check.
    runtime._config = load_shortlab_config()
    runtime._available = True
    runtime._started = True
    status = await service.run_refresh(JOB_TYPE_SCORE_REFRESH)
    assert status.status == "SUCCEEDED"
    symbols = [r["s"] for r in rows]
    # Base tables populated.
    tracked = await repo.list_tracked_symbols()
    for s in symbols:
        assert s in tracked
    for s in symbols:
        assert await repo.latest_contract_lifecycle(s) is not None
        assert len(await repo.list_funding_events(s, NOW - 90 * DAY_MS, NOW)) > 0
        life = await repo.latest_contract_lifecycle(s)
        assert life is not None and life.futures_symbol == s
    # Config snapshot + funding cursor persisted.
    from diveintocrypto_desktop.shortlab.config import policy_hash as _phash

    assert await repo.get_config_snapshot(_phash(service.config)) is not None
    assert await repo.load_cursor(JOB_TYPE_FUNDING_BACKFILL, "main") is not None
    # Feature/score linkage: feature references fundamental, score batch atomic.
    page = await service.candidates()
    assert page.total == 2
    for item in page.items:
        feature = await repo.get_feature(item.feature_snapshot_id)
        assert feature is not None
        assert feature.fundamental_snapshot_id is not None
        # The referenced fundamental row exists (canonical + cutoff lookup).
        canonical = item.symbol.lower()
        fund = await repo.get_fundamental_before(canonical, NOW + DAY_MS)
        assert fund is not None
        assert fund.snapshot_id == feature.fundamental_snapshot_id
        assert item.feature_snapshot_id == feature.snapshot_id
    # Score batch + SUCCEEDED committed together; previous generation still serves.
    gen = await repo.latest_completed_generation()
    assert gen == status.job_id
    await repo.close()


# ---------------------------------------------------------------------------
# 2. 500 symbols / 80-per-batch fair rotation, restart resumes, gap incremental
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_funding_fair_rotation_500_restart_resumes(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    rows = make_universe(500)
    fetched: list[str] = []

    async def funding_fn(symbol: str, a: int, b: int) -> list[dict]:
        fetched.append(symbol)
        return funding_events_light(b, n=3)

    service = make_service(repo, clock, rows, funding_fn=funding_fn)
    s1 = await service.run_refresh(JOB_TYPE_SCORE_REFRESH)
    assert s1.status == "SUCCEEDED"
    assert s1.stats["funding_requested"] == 80
    first = list(fetched)
    assert len(set(first)) == 80
    persisted = await repo.load_cursor(JOB_TYPE_FUNDING_BACKFILL, "main")
    assert persisted is not None and int(persisted["cursor"]) == 80 % 500

    # Restart: fresh service on the SAME db must resume, not re-head.
    fetched.clear()
    clock.advance(301_000)  # next 5-min window + new generation id (avoids job-id reuse)
    service2 = make_service(repo, clock, rows, funding_fn=funding_fn)
    # Fresh in-memory cursor proves persistence (not memory) drives resume.
    assert service2._funding_cursor == 0
    s2 = await service2.run_refresh(JOB_TYPE_SCORE_REFRESH)
    assert s2.status == "SUCCEEDED"
    second = list(fetched)
    assert len(set(second)) == 80
    assert set(first).isdisjoint(set(second))  # continued, not restarted
    assert set(second) == {f"T{i:03d}USDT" for i in range(80, 160)}
    persisted2 = await repo.load_cursor(JOB_TYPE_FUNDING_BACKFILL, "main")
    assert int(persisted2["cursor"]) == 160 % 500
    await repo.close()


@pytest.mark.asyncio
async def test_funding_backfill_incremental_gap_no_90d_refetch(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    rows = make_universe(3)
    calls: list[tuple[str, int, int]] = []

    async def funding_fn(symbol: str, a: int, b: int) -> list[dict]:
        calls.append((symbol, a, b))
        return funding_events_light(b, n=3)

    service = make_service(repo, clock, rows, funding_fn=funding_fn,
                           budget=RequestBudget(clock=clock), cache=ObservedCache(clock=clock))
    ctx = service.make_job_context(JOB_TYPE_FUNDING_BACKFILL, trace_id="funding_backfill-1")
    first = await service.run_funding_backfill(ctx, job_id="funding_backfill-1")
    assert first.status == "SUCCEEDED"
    assert first.stats["requested"] == 3
    n_calls = len(calls)
    # Second backfill at the same cutoff: gap already filled -> no resend.
    ctx2 = service.make_job_context(JOB_TYPE_FUNDING_BACKFILL, trace_id="funding_backfill-2")
    second = await service.run_funding_backfill(ctx2, job_id="funding_backfill-2")
    assert second.status == "SUCCEEDED"
    assert len(calls) == n_calls  # incremental: no blind 90D refetch
    await repo.close()


@pytest.mark.asyncio
async def test_cache_survives_budget_denial(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    rows = make_universe(2)
    cache = ObservedCache(clock=clock)
    budget = RequestBudget(max_sends=1, window_ms=60_000, clock=clock)
    service = make_service(repo, clock, rows, budget=budget, cache=cache)
    ctx = service.make_job_context(JOB_TYPE_FUNDING_BACKFILL, trace_id="funding_backfill-1")
    first = await service.run_funding_backfill(ctx, job_id="funding_backfill-1")
    assert first.status == "SUCCEEDED"
    assert len(cache) >= 1
    before = len(cache)
    # Exhaust the budget, then deny the next symbol: cache must stay intact.
    ctx2 = service.make_job_context(JOB_TYPE_FUNDING_BACKFILL, trace_id="funding_backfill-2")
    second = await service.run_funding_backfill(ctx2, job_id="funding_backfill-2")
    assert second.status == "SUCCEEDED"
    assert len(cache) == before  # TTL hit, denied budget never clears
    await repo.close()


# ---------------------------------------------------------------------------
# 3. Per-coin frozen cutoff, cross-day realignment, tracked set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_coin_cutoff_cross_day_and_tracked(tmp_path) -> None:
    clock = FakeClock(start_ms=(NOW // DAY_MS) * DAY_MS - 60_000)  # 1 min before UTC midnight
    repo = await open_repo(tmp_path)
    rows = make_universe(2)
    service = make_service(repo, clock, rows)
    cutoffs = []

    orig_score = service._score_symbol

    async def spy(symbol: str, row: Any, live: Any, meta: Any, as_of: int, tier: str) -> Any:
        cutoffs.append((symbol, int(as_of)))
        out = await orig_score(symbol, row, live, meta, as_of, tier)
        if symbol == rows[0]["s"]:
            clock.advance(120_000)  # cross UTC midnight before the next coin freezes
        return out

    service._score_symbol = spy  # type: ignore[method-assign]
    status = await service.run_refresh(JOB_TYPE_SCORE_REFRESH)
    assert status.status == "SUCCEEDED"
    assert len(cutoffs) == 2
    # Per-coin cutoffs differ across the midnight boundary.
    assert cutoffs[0][1] < cutoffs[1][1]
    assert (cutoffs[0][1] // DAY_MS) != (cutoffs[1][1] // DAY_MS)
    # Scores carry their own frozen cutoff (no stale global day reuse).
    page = await service.candidates()
    as_ofs = sorted(item.as_of_ms for item in page.items)
    assert as_ofs[0] != as_ofs[1]
    # Tracked = live + history.
    live = {r["s"] for r in rows}
    tracked = set(await service.tracked_symbols(live))
    assert live <= tracked
    # F02 UTC closed-day windows realign per cutoff (open day excluded).
    from diveintocrypto_desktop.shortlab.observations import utc_closed_day_window

    for _sym, cutoff in cutoffs:
        start, end = utc_closed_day_window(cutoff, 7)
        assert end == (cutoff // DAY_MS) * DAY_MS
        assert end - start == 7 * DAY_MS
        assert end <= cutoff  # still-open day never scored
    await repo.close()


@pytest.mark.asyncio
async def test_feature_inputs_require_observed_and_frozen_policy() -> None:
    from diveintocrypto_desktop.shortlab import observations as _obs
    from diveintocrypto_desktop.shortlab.inputs import build_feature_inputs

    config = load_shortlab_config()
    cutoff = NOW
    klines = [{"t": cutoff - (i + 1) * DAY_MS, "c": 100.0 - i, "h": 101.0, "qv": 1_000_000.0}
              for i in range(40)]

    def _wrap(value: Any, source: str) -> Any:
        return _obs.make_observation(
            value, source=source, source_as_of_ms=cutoff - 1000,
            fetched_at_ms=cutoff - 500, known_at_ms=cutoff - 500,
        )

    observations = {
        "klines_daily": _wrap(klines, "binance-klines"),
        "ticker_24h": _wrap({"current_price": 95.0, "price_change_24h": 0.01}, "binance-ticker"),
        "funding_history": _wrap([], "binance-funding"),
        "funding_history_90d": _wrap([], "binance-funding"),
        "oi_history": _wrap([], "binance-oi"),
        "ath": _wrap({"ath_price": 200.0, "ath_date_ms": cutoff - 300 * DAY_MS}, "coingecko"),
        "fundamentals": _wrap({"market_cap_usd": 1e9}, "coingecko"),
        "spot_history": _wrap({"status": "UNAVAILABLE"}, "binance-spot"),
        "book": _wrap({"spread": 0.001}, "binance-book"),
        "contract": _wrap({"status": "TRADING", "live_universe_present": True}, "binance-exchangeInfo"),
        "fx_rate": _wrap(1.0, "fx"),
    }

    class _Ident:
        contract_multiplier = 1.0
        multiplier_source = "MANUAL"
        mapping_confidence = "VERIFIED"
        identity_snapshot_id = "isl-test"

    inputs = build_feature_inputs("T000USDT", observations, _Ident(), cutoff, config)
    assert inputs.symbol == "T000USDT" and inputs.decision_as_of_ms == cutoff
    # Legacy dict (non-Observed) is rejected, never scored.
    bad = dict(observations)
    bad["klines_daily"] = klines  # type: ignore[dict-item]
    degraded = build_feature_inputs("T000USDT", bad, _Ident(), cutoff, config)
    assert degraded.daily_closes == ()
    # Missing frozen policy never falls back to today's config.
    import pytest as _pt

    with _pt.raises(ValueError):
        build_feature_inputs("T000USDT", observations, _Ident(), cutoff, None)


# ---------------------------------------------------------------------------
# 4. Callback slots on a bare service (no runtime wiring); no second service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_slots_unregistered_no_extra_service(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    rows = make_universe(1)
    service = make_service(repo, clock, rows)
    assert service.grader_callback is None
    assert service.retention_callback is None
    ctx = service.make_job_context(JOB_TYPE_SCORE_REFRESH, trace_id="trace-1")
    with pytest.raises(UnknownJobType):
        await service.run_due(ctx)
    with pytest.raises(UnknownJobType):
        await service.maintain(ctx)
    # Registering is explicit (F07/F09 own the impl); default wiring adds no job.
    async def _fake_grader(c: JobContext):  # pragma: no cover - slot shape only
        return await service.run_refresh(JOB_TYPE_SCORE_REFRESH)

    service.register_grader_callback(_fake_grader)
    assert service.grader_callback is _fake_grader
    service.register_grader_callback(None)
    assert service.grader_callback is None
    await repo.close()


@pytest.mark.asyncio
async def test_runtime_stop_awaits_service_before_db_close(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    rows = make_universe(2)
    gate = asyncio.Event()

    async def gated_universe(limit: int | None = None) -> list[dict]:
        await gate.wait()
        return list(rows)

    service = make_service(repo, clock, rows)
    service._universe_fn = gated_universe  # type: ignore[assignment]
    sched = ShortLabScheduler(jitter_fn=lambda m: 0.0)
    runtime = ShortLabRuntime(repository=repo, service=service, scheduler=sched, clock=clock)
    runtime._config = load_shortlab_config()
    runtime._available = True
    runtime._started = True
    # Runtime only closes repositories it owns; this test owns the repo, so
    # claim ownership to exercise the stop→service→DB ordering.
    runtime._owns_repository = True
    ref = await service.refresh(JOB_TYPE_SCORE_REFRESH)
    await asyncio.sleep(0.05)
    assert (await service.job_status(ref.job_id)).status == "RUNNING"
    gate.set()
    await runtime.stop()  # must await the in-flight job, then close the DB
    assert repo._closed is True
    assert runtime.available is False


@pytest.mark.asyncio
async def test_contract_and_metadata_adapters_run(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    rows = make_universe(3)
    service = make_service(repo, clock, rows)
    ctx = service.make_job_context(JOB_TYPE_CONTRACT_REFRESH, trace_id="contract_refresh-1")
    cstat = await service.run_contract_refresh(ctx, job_id="contract_refresh-1")
    assert cstat.status == "SUCCEEDED" and cstat.stats["tracked"] >= 3
    mctx = service.make_job_context(JOB_TYPE_METADATA, trace_id="metadata-1")
    mstat = await service.run_metadata_refresh(mctx, job_id="metadata-1")
    assert mstat.status == "SUCCEEDED" and mstat.stats["live"] == 3
    assert await repo.load_cursor(JOB_TYPE_CONTRACT_REFRESH, "main") is not None
    await repo.close()


# ---------------------------------------------------------------------------
# 5. F06b: default Runtime wires grader + retention (same owner, no new service)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f06b_default_wiring_registers_callbacks_without_extra_service(tmp_path) -> None:
    """F06b: default Runtime registers run_due + maintain on one service."""
    from diveintocrypto_desktop.shortlab.evidence.jobs import run_due as _expected_grader
    from diveintocrypto_desktop.shortlab.maintenance import maintain as _expected_maintain

    clock = FakeClock()
    runtime = ShortLabRuntime(db_path=tmp_path / "f06b-reg.duckdb", clock=clock)
    service_before = runtime._service
    await runtime.start()
    try:
        assert runtime.available is True
        # Callbacks wired to the F07/F09 owners (exact function identity).
        assert runtime.service.grader_callback is _expected_grader
        assert runtime.service.retention_callback is _expected_maintain
        # Aliases expose the same slots.
        assert runtime.service.grader_callback is not None
        assert runtime.service.retention_callback is not None
        # Scheduler exposes score + grader + maintenance (F06b steady state).
        job_types = set(runtime.scheduler.job_types())
        assert {JOB_TYPE_SCORE_REFRESH, JOB_TYPE_GRADER, JOB_TYPE_MAINTENANCE} <= job_types
        assert runtime.scheduler.has_job(JOB_TYPE_GRADER)
        assert runtime.scheduler.has_job(JOB_TYPE_MAINTENANCE)
        # Cadence comes from config (grader_sec / retention_sec), jitter shared.
        assert runtime.scheduler._jobs[JOB_TYPE_GRADER].interval_sec == float(
            runtime.config.refresh.grader_sec
        )
        assert runtime.scheduler._jobs[JOB_TYPE_MAINTENANCE].interval_sec == float(
            runtime.config.maintenance.retention_sec
        )
        # No second service: the runtime kept (or created once) a single instance.
        assert runtime.service is not None
        if service_before is not None:
            assert runtime.service is service_before
        # Callbacks share the runtime JobContext (same repository, no new DB).
        ctx = runtime.make_context(JOB_TYPE_GRADER)
        assert ctx.repository is runtime.repository
        assert ctx.config is runtime.config
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_f06b_grader_and_maintain_each_succeed(tmp_path) -> None:
    """F06b: wired grader + maintain each run once successfully (empty DB, no net)."""
    clock = FakeClock()
    runtime = ShortLabRuntime(db_path=tmp_path / "f06b-each.duckdb", clock=clock)
    await runtime.start()
    try:
        service = runtime.service
        # Direct slot entry points.
        gctx = service.make_job_context(JOB_TYPE_GRADER, trace_id="grader-f06b-1")
        gstatus = await service.run_due(gctx)
        assert gstatus.status == "SUCCEEDED" and gstatus.job_type == JOB_TYPE_GRADER
        mctx = service.make_job_context(JOB_TYPE_MAINTENANCE, trace_id="maintenance-f06b-1")
        mstatus = await service.maintain(mctx)
        assert mstatus.status == "SUCCEEDED" and mstatus.job_type == JOB_TYPE_MAINTENANCE
        # Unified adapter dispatch on the same service instance.
        gctx2 = service.make_job_context(JOB_TYPE_GRADER, trace_id="grader-f06b-2")
        g2 = await service.run_job_adapter(JOB_TYPE_GRADER, gctx2)
        assert g2.status == "SUCCEEDED"
        mctx2 = service.make_job_context(JOB_TYPE_MAINTENANCE, trace_id="maintenance-f06b-2")
        m2 = await service.run_job_adapter(JOB_TYPE_MAINTENANCE, mctx2)
        assert m2.status == "SUCCEEDED"
        # Refresh path (per-job locks + job_status) also succeeds.
        gr = await service.run_refresh(JOB_TYPE_GRADER)
        assert gr.status == "SUCCEEDED"
        mr = await service.run_refresh(JOB_TYPE_MAINTENANCE)
        assert mr.status == "SUCCEEDED"
        # Scheduler wiring runs the same callbacks (no second service).
        sg = await runtime.scheduler.trigger_now(JOB_TYPE_GRADER)
        assert getattr(sg, "status", None) == "SUCCEEDED"
        sm = await runtime.scheduler.trigger_now(JOB_TYPE_MAINTENANCE)
        assert getattr(sm, "status", None) == "SUCCEEDED"
        assert runtime.service is service
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_f06b_default_wiring_end_to_end_fake_http(tmp_path) -> None:
    """F06b: fake-HTTP score_refresh + grader + maintain all succeed, one service."""
    from diveintocrypto_desktop.shortlab.evidence.jobs import run_due as _expected_grader
    from diveintocrypto_desktop.shortlab.maintenance import maintain as _expected_maintain

    clock = FakeClock()
    repo = await open_repo(tmp_path)
    registry = ProviderRegistry()
    registry.register("coingecko", FakeCoinGecko(clock))
    rows = make_universe(2)
    symbols = [r["s"] for r in rows]

    async def funding_fn(symbol: str, a: int, b: int) -> list[dict]:
        return funding_events_90d(b)

    service = make_service(repo, clock, rows, funding_fn=funding_fn, registry=registry,
                           budget=RequestBudget(clock=clock), cache=ObservedCache(clock=clock))
    service._identity_overrides = {
        s: {"canonical_id": s.lower(), "display_symbol": s,
            "contract_multiplier": 1.0, "multiplier_source": "MANUAL",
            "coingecko_id": "bitcoin"}
        for s in symbols
    }
    runtime = ShortLabRuntime(repository=repo, service=service, clock=clock,
                              request_budget=service.request_budget,
                              observed_cache=service.observed_cache,
                              db_path=tmp_path / "unused.duckdb")
    service_id = id(service)
    await runtime.start()
    try:
        # No second service: runtime kept the injected instance.
        assert runtime.service is service
        assert id(runtime.service) == service_id
        assert service._repository is repo
        # Callbacks wired to the domain owners.
        assert service.grader_callback is _expected_grader
        assert service.retention_callback is _expected_maintain
        # Scheduler exposes all three.
        assert runtime.scheduler.has_job(JOB_TYPE_SCORE_REFRESH)
        assert runtime.scheduler.has_job(JOB_TYPE_GRADER)
        assert runtime.scheduler.has_job(JOB_TYPE_MAINTENANCE)
        # Park background ticks so manual runs are deterministic.
        await runtime.scheduler.stop()

        score_status = await service.run_refresh(JOB_TYPE_SCORE_REFRESH)
        assert score_status.status == "SUCCEEDED"
        assert score_status.stats.get("succeeded", 0) >= 1

        gctx = service.make_job_context(JOB_TYPE_GRADER, trace_id="grader-e2e-1")
        gstatus = await service.run_due(gctx)
        assert gstatus.status == "SUCCEEDED" and gstatus.job_type == JOB_TYPE_GRADER

        mctx = service.make_job_context(JOB_TYPE_MAINTENANCE, trace_id="maintenance-e2e-1")
        mstatus = await service.maintain(mctx)
        assert mstatus.status == "SUCCEEDED" and mstatus.job_type == JOB_TYPE_MAINTENANCE

        # Scheduler trigger path runs the same single-service callbacks.
        sg = await runtime.scheduler.trigger_now(JOB_TYPE_GRADER)
        assert getattr(sg, "status", None) == "SUCCEEDED"
        sm = await runtime.scheduler.trigger_now(JOB_TYPE_MAINTENANCE)
        assert getattr(sm, "status", None) == "SUCCEEDED"
        assert runtime.service is service
    finally:
        try:
            await runtime.stop()
        finally:
            await repo.close()


@pytest.mark.asyncio
async def test_f06b_funding_cursor_survives_restart_with_callbacks(tmp_path) -> None:
    """F06b: fair rotation + restart resume keep working after callback wiring."""
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    rows = make_universe(100)
    fetched: list[str] = []

    async def funding_fn(symbol: str, a: int, b: int) -> list[dict]:
        fetched.append(symbol)
        return funding_events_light(b, n=3)

    service = make_service(repo, clock, rows, funding_fn=funding_fn)
    runtime = ShortLabRuntime(repository=repo, service=service, clock=clock,
                              request_budget=service.request_budget,
                              observed_cache=service.observed_cache,
                              db_path=tmp_path / "unused.duckdb")
    await runtime.start()
    try:
        assert service.grader_callback is not None
        assert service.retention_callback is not None
        await runtime.scheduler.stop()  # park background ticks
        s1 = await service.run_refresh(JOB_TYPE_SCORE_REFRESH)
        assert s1.status == "SUCCEEDED"
        assert s1.stats["funding_requested"] == 80
        first = set(fetched)
        assert len(first) == 80
    finally:
        try:
            await runtime.stop()
        except Exception:
            pass

    # Restart: fresh service on the SAME db resumes, callbacks re-wire.
    fetched.clear()
    clock.advance(301_000)
    service2 = make_service(repo, clock, rows, funding_fn=funding_fn)
    assert service2._funding_cursor == 0
    assert service2.grader_callback is None  # bare service starts unwired
    runtime2 = ShortLabRuntime(repository=repo, service=service2, clock=clock,
                               request_budget=service2.request_budget,
                               observed_cache=service2.observed_cache,
                               db_path=tmp_path / "unused2.duckdb")
    await runtime2.start()
    try:
        assert service2.grader_callback is not None
        assert service2.retention_callback is not None
        await runtime2.scheduler.stop()
        s2 = await service2.run_refresh(JOB_TYPE_SCORE_REFRESH)
        assert s2.status == "SUCCEEDED"
        second = set(fetched)
        assert len(second) == 80
        deferred = {f"T{i:03d}USDT" for i in range(100)} - first
        assert deferred <= second  # continued, not restarted
    finally:
        try:
            await runtime2.stop()
        finally:
            await repo.close()


@pytest.mark.asyncio
async def test_f06b_graceful_stop_with_callbacks_closes_db(tmp_path) -> None:
    """F06b: stop() still awaits scheduler/service before DB close (6 jobs)."""
    clock = FakeClock()
    runtime = ShortLabRuntime(db_path=tmp_path / "f06b-stop.duckdb", clock=clock)
    await runtime.start()
    try:
        assert runtime.available is True
        assert set(runtime.scheduler.job_types()) >= {
            JOB_TYPE_SCORE_REFRESH, JOB_TYPE_FUNDING_BACKFILL,
            JOB_TYPE_CONTRACT_REFRESH, JOB_TYPE_METADATA,
            JOB_TYPE_GRADER, JOB_TYPE_MAINTENANCE,
        }
        owned = runtime.repository
        assert owned is not None
        await runtime.stop()
        assert runtime.scheduler.pending_tasks() == ()
        assert owned._closed is True
        assert runtime.available is False
    finally:
        try:
            await runtime.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# H08: default Runtime production wiring (not just an injected service)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_h08_default_runtime_hedge_production_wiring(tmp_path) -> None:
    """H08: production Runtime wires hedge jobs jitter 0 + combined grader."""
    import dataclasses as _dc

    from diveintocrypto_desktop.shortlab.service import (
        JOB_TYPE_HEDGE_MONITOR,
        JOB_TYPE_HEDGE_SETTLEMENT,
    )

    clock = FakeClock()
    base = load_shortlab_config()
    hedge_cfg = _dc.replace(base.hedge, enabled=True)
    config = _dc.replace(base, hedge=hedge_cfg)
    runtime = ShortLabRuntime(
        config=config, db_path=tmp_path / "h08-prod-wiring.duckdb",
        clock=clock, jitter_fn=lambda m: 0.0,
    )
    await runtime.start()
    try:
        assert runtime.available is True
        assert runtime.hedge_available is True
        job_types = set(runtime.scheduler.job_types())
        assert JOB_TYPE_SCORE_REFRESH in job_types
        assert JOB_TYPE_HEDGE_MONITOR in job_types
        assert JOB_TYPE_HEDGE_SETTLEMENT in job_types
        # Active pair never inherits the ordinary 300s jitter.
        assert runtime.scheduler._jobs[JOB_TYPE_HEDGE_MONITOR].jitter_max_sec == 0.0
        assert runtime.scheduler._jobs[JOB_TYPE_HEDGE_SETTLEMENT].jitter_max_sec == 0.0
        # Directional grader stays the F06b wiring; hedge is lazy/combined.
        from diveintocrypto_desktop.shortlab.evidence.jobs import run_due as _expected_grader

        assert runtime.service.grader_callback is _expected_grader
        ctx = runtime.service.make_job_context(JOB_TYPE_GRADER, trace_id="grader-h08-wiring")
        status = await runtime.service.run_due(ctx)
        assert status.status == "SUCCEEDED"
        assert "directional" in status.stats and "hedge" in status.stats
        caps = runtime.hedge_capabilities
        assert caps["hedge"] is True
        assert isinstance(caps["binanceSpot"], bool)
    finally:
        await runtime.stop()
    assert runtime.available is False
    assert runtime.scheduler.pending_tasks() == ()


@pytest.mark.asyncio
async def test_h08_hedge_005_failure_keeps_base_via_runtime(tmp_path, monkeypatch) -> None:
    """H08: poisoned 005 only bans Hedge (production Runtime, base 004 serves)."""
    import dataclasses as _dc

    from diveintocrypto_desktop.shortlab import repository as repo_mod
    from diveintocrypto_desktop.shortlab.service import (
        JOB_TYPE_HEDGE_MONITOR,
    )

    clock = FakeClock()
    base = load_shortlab_config()
    hedge_cfg = _dc.replace(base.hedge, enabled=True)
    config = _dc.replace(base, hedge=hedge_cfg)
    real_split = repo_mod._split_statements

    def _poisoned(script: str) -> list[str]:
        parts = real_split(script)
        if "sl_hedge_plan" in script:
            return parts + ["THIS IS NOT VALID SQL ("]
        return parts

    monkeypatch.setattr(repo_mod, "_split_statements", _poisoned)
    runtime = ShortLabRuntime(
        config=config, db_path=tmp_path / "h08-005-poison.duckdb", clock=clock,
    )
    await runtime.start()
    try:
        # Base stays available; hedge is honestly disabled.
        assert runtime.available is True
        assert runtime.hedge_available is False
        assert JOB_TYPE_HEDGE_MONITOR not in set(runtime.scheduler.job_types())
        assert JOB_TYPE_SCORE_REFRESH in set(runtime.scheduler.job_types())
    finally:
        try:
            await runtime.stop()
        except Exception:
            pass
    monkeypatch.setattr(repo_mod, "_split_statements", real_split)
    # Retry with a clean migration recovers hedge on the same DB path.
    runtime2 = ShortLabRuntime(
        config=config, db_path=tmp_path / "h08-005-poison.duckdb", clock=clock,
        jitter_fn=lambda m: 0.0,
    )
    await runtime2.start()
    try:
        assert runtime2.available is True
        assert runtime2.hedge_available is True
        assert JOB_TYPE_HEDGE_MONITOR in set(runtime2.scheduler.job_types())
    finally:
        try:
            await runtime2.stop()
        except Exception:
            pass
