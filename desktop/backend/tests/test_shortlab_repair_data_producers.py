"""Regression tests for same-response DATA producers (offline only)."""

from __future__ import annotations

import pytest

from diveintocrypto_desktop.shortlab.observations import ObservationMeta, Observed


class _FundingObservationRepo:
    def __init__(self) -> None:
        self.records = []

    async def save_funding_observation(self, record):
        self.records.append(dict(record))
        return str(record["observation_id"])


@pytest.mark.asyncio
async def test_funding_receipt_helper_persists_each_event_with_original_response_times():
    from diveintocrypto_desktop.data.funding import persist_funding_event_receipts

    receipt = Observed(
        value=[
            {"symbol": "BTCUSDT", "fundingTime": 1000, "fundingRate": "0.001"},
            {"symbol": "BTCUSDT", "fundingTime": 2000, "fundingRate": "0.002"},
        ],
        meta=ObservationMeta(
            status="OK", source="binance-futures-funding",
            source_as_of_ms=2000, fetched_at_ms=5000, known_at_ms=5000,
            window_start_ms=1000, window_end_ms=2000,
        ),
    )
    repo = _FundingObservationRepo()

    ids = await persist_funding_event_receipts(receipt, repo)

    assert len(ids) == len(repo.records) == 2
    assert [row["funding_time_ms"] for row in repo.records] == [1000, 2000]
    assert all(row["known_at_ms"] == 5000 for row in repo.records)
    assert all(row["interval_hours"] is None for row in repo.records)
    assert repo.records[0]["raw_json"]["event"]["fundingRate"] == "0.001"
    assert repo.records[0]["raw_json"]["receipt"]["source_as_of_ms"] == 2000


@pytest.mark.asyncio
async def test_taker_ratio_observation_preserves_latest_source_time_and_request_context(monkeypatch):
    from diveintocrypto_desktop.data import ratios

    calls = []

    async def get_json(url, params, *, request_context=None, **kwargs):
        calls.append((url, params, request_context))
        return [
            {"buySellRatio": "1.1", "timestamp": 3000},
            {"buySellRatio": "0.8", "timestamp": 4000},
        ]

    monkeypatch.setattr(ratios, "get_json", get_json)
    ctx = object()

    observed = await ratios.fetch_taker_ratio_observed(
        "BTCUSDT", period="5m", limit=2, now_ms=5000,
        request_context=ctx,
    )

    assert observed.value == pytest.approx(0.8)
    assert observed.meta.source_as_of_ms == 4000
    assert observed.meta.known_at_ms == observed.meta.fetched_at_ms == 5000
    assert observed.meta.source == "binance-futures-data:takerlongshortRatio"
    assert calls[0][2] is ctx


@pytest.mark.asyncio
async def test_funding_and_taker_receipts_use_clock_after_slow_transport(monkeypatch):
    from diveintocrypto_desktop.data import funding, ratios

    class Clock:
        now = 1000

    clock = Clock()

    async def funding_response(*args, **kwargs):
        clock.now += 5000
        return [{"symbol": "BTCUSDT", "t": 2000, "funding_rate": "0.001"}]

    async def taker_response(*args, **kwargs):
        clock.now += 5000
        return [{"buySellRatio": "0.8", "timestamp": clock.now - 1000}]

    monkeypatch.setattr(funding, "funding_history_range", funding_response)
    monkeypatch.setattr(ratios, "get_json", taker_response)

    funding_observed = await funding.fetch_funding_history_observed(
        "BTCUSDT", 1, 10_000, clock_ms=lambda: clock.now
    )
    taker_observed = await ratios.fetch_taker_ratio_observed(
        "BTCUSDT", clock_ms=lambda: clock.now
    )

    assert funding_observed.meta.known_at_ms == 6000
    assert funding_observed.meta.fetched_at_ms == 6000
    assert taker_observed.meta.known_at_ms == 11_000
    assert taker_observed.meta.fetched_at_ms == 11_000


@pytest.mark.asyncio
async def test_funding_schedule_effective_start_comes_from_response_completion(monkeypatch):
    from diveintocrypto_desktop.data import funding

    class Clock:
        now = 1000

    class ScheduleRepo:
        def __init__(self):
            self.schedules = []
            self.market_observations = []

        async def list_funding_schedules(self, symbol, known_by_ms):
            return tuple(row for row in self.schedules if row["symbol"] == symbol)

        async def save_funding_schedule(self, record):
            self.schedules.append(dict(record))
            return str(record["schedule_id"])

        async def save_market_observation(self, record):
            self.market_observations.append(dict(record))
            return str(record["observation_id"])

    clock = Clock()
    repo = ScheduleRepo()

    async def slow_info(*, request_context=None):
        clock.now += 5000
        return [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}]

    monkeypatch.setattr(funding, "fetch_funding_info", slow_info)

    result = await funding.collect_and_archive_funding_schedules(
        repository=repo,
        symbols=["BTCUSDT"],
        observed_at_ms=1000,
        clock_ms=lambda: clock.now,
    )

    assert result["response_ok"] is True
    assert result["known_at_ms"] == result["observed_at_ms"] == 6000
    assert repo.schedules[0]["effective_from_ms"] == 6000
    assert repo.schedules[0]["known_at_ms"] == 6000


@pytest.mark.asyncio
async def test_default_regime_receipt_is_bound_to_completed_funding_info(monkeypatch):
    from diveintocrypto_desktop.data import funding

    class Clock:
        now = 1000

    class ScheduleRepo:
        def __init__(self):
            self.schedules = []

        async def list_funding_schedules(self, symbol, known_by_ms):
            return tuple(row for row in self.schedules if row["symbol"] == symbol)

        async def save_funding_schedule(self, record):
            self.schedules.append(dict(record))
            return str(record["schedule_id"])

        async def save_market_observation(self, record):
            return str(record["observation_id"])

    clock = Clock()
    repo = ScheduleRepo()

    async def slow_info(*, request_context=None):
        clock.now += 5000
        return []

    monkeypatch.setattr(funding, "fetch_funding_info", slow_info)
    result = await funding.collect_and_archive_funding_schedules(
        repository=repo,
        symbols=["ETHUSDT"],
        symbol_statuses={"ETHUSDT": "TRADING"},
        default_regime={
            "interval_hours": 8,
            "version": "binance-faq-360033525031@2026-10-09",
            "source": "binance-faq:360033525031",
            "known_at_ms": 1000,
        },
        now_ms=None,
        clock_ms=lambda: clock.now,
    )

    assert result["response_ok"] is True
    assert result["known_at_ms"] == result["observed_at_ms"] == 6000
    saved = repo.schedules[0]
    assert saved["effective_from_ms"] == saved["known_at_ms"] == 6000
    assert saved["schedule_json"]["verification"] == "CONFIRMED"
    assert saved["schedule_json"]["source"] == "binance-faq:360033525031"
    assert "binance-faq-360033525031@2026-10-09" in saved["schedule_json"]["evidence_ref"]


@pytest.mark.asyncio
async def test_identity_catalog_monthly_budget_counts_each_send_and_cache_is_free():
    from diveintocrypto_desktop.data.http import TransientUpstreamError
    from diveintocrypto_desktop.shortlab.identity.catalog import IdentityCatalog
    from diveintocrypto_desktop.shortlab.request_budget import (
        RequestBudget,
        make_request_context,
    )

    class MonthlyRepo:
        def __init__(self):
            self.reservations = []
            self.finished = []

        async def reserve_provider_request(
            self, provider, month_key, request_id, monthly_limit, as_of_ms
        ):
            self.reservations.append((provider, month_key, request_id, monthly_limit, as_of_ms))
            return {"admitted": True}

        async def finish_provider_request(self, request_id, sent, as_of_ms):
            self.finished.append((request_id, sent, as_of_ms))

    repo = MonthlyRepo()
    sends = []
    directory_attempts = 0

    async def http_get(url, params, context):
        nonlocal directory_attempts
        sends.append((url, context))
        if url.endswith("/coins/list"):
            directory_attempts += 1
            if directory_attempts == 1:
                raise TransientUpstreamError(503, retry_after=0.001)
            return [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}]
        return {"id": "bitcoin", "platforms": {"ethereum": "0xabc"}}

    cat = IdentityCatalog(
        api_plan="demo",
        api_key="test-key",
        verified={"version": 3, "assets": {}},
        overrides_doc={"version": 7, "overrides": {}},
        clock=lambda: 1_800_000_000_000,
        http_get=http_get,
        repository=repo,
        account_monthly_limit=10,
        reserve_fraction=0.1,
    )
    ctx = make_request_context(
        RequestBudget(max_sends=10, window_ms=60_000),
        job_type="metadata",
        trace_id="catalog-budget",
    )

    first = await cat.refresh(ctx)
    cached = await cat.refresh(ctx)
    detail = await cat.refresh_platform_details(["bitcoin"], ctx)

    assert first.status == cached.status == detail.status == "OK"
    assert directory_attempts == 2
    assert len(sends) == len(repo.reservations) == len(repo.finished) == 3
    assert len({row[2] for row in repo.reservations}) == 3
    assert all(row[3] == 9 for row in repo.reservations)
    assert all(row[1] is True for row in repo.finished)
    assert ctx.budget.sent_attempts == 3
    # The CG-host permit and monthly reservation both charge every entered
    # transport; a cached directory refresh has no second reservation.
    assert all(context.budget is not None for _, context in sends)


@pytest.mark.asyncio
async def test_identity_catalog_monthly_exhaustion_blocks_detail_send():
    from diveintocrypto_desktop.shortlab.identity.catalog import IdentityCatalog
    from diveintocrypto_desktop.shortlab.providers.coingecko import BUDGET_LIMITED

    class MonthlyRepo:
        def __init__(self):
            self.reservations = []
            self.finished = []

        async def reserve_provider_request(
            self, provider, month_key, request_id, monthly_limit, as_of_ms
        ):
            admitted = len(self.reservations) < monthly_limit
            self.reservations.append((request_id, monthly_limit, admitted))
            return {"admitted": admitted, "reason_code": "BUDGET_MONTHLY_EXHAUSTED"}

        async def finish_provider_request(self, request_id, sent, as_of_ms):
            self.finished.append((request_id, sent))

    repo = MonthlyRepo()
    sends = []

    async def http_get(url, params, context):
        sends.append(url)
        if url.endswith("/coins/list"):
            return [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}]
        return {"id": "bitcoin", "platforms": {"ethereum": "0xabc"}}

    cat = IdentityCatalog(
        api_plan="demo",
        api_key="test-key",
        verified={"version": 3, "assets": {}},
        overrides_doc={"version": 7, "overrides": {}},
        clock=lambda: 1_800_000_000_000,
        http_get=http_get,
        repository=repo,
        account_monthly_limit=2,
        reserve_fraction=0.5,
    )
    await cat.refresh()

    detail = await cat.refresh_platform_details(["bitcoin"])

    assert detail.status == "UNAVAILABLE"
    assert detail.reason_code == BUDGET_LIMITED
    assert len(repo.reservations) == 2
    assert repo.reservations[0][1:] == (1, True)
    assert repo.reservations[1][1:] == (1, False)
    assert len(sends) == 1
    assert len(repo.finished) == 1 and repo.finished[0][1] is True
