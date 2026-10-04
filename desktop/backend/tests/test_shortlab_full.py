"""Task 17: Unlock / Social / Catalyst providers and FULL wiring (Phase 5-6).

Covers ``ShortLab_Implementation_Plan_CN.md`` Task 17 and
``ShortLab_Detailed_Design_CN.md`` sections 5.3-5.5, 11.2, 12, 14, 18
(FULL groups), 19.2 (002/003 DDL) and 23/24:

- Phase 5: NullProvider / keyless fixtures keep LITE intact; the real
  providers gate on verified identity ids, rate-limit + cache, enforce
  point-in-time cutoffs and dedup.
- 002 (``sl_unlock_event`` / ``sl_social_snapshot``) and 003
  (``sl_catalyst_event``) migrations match the design DDL field by field;
  the repository saves idempotently and reads with ``known_at`` cutoffs.
  Phase 5 never runs 003.
- ``supply.py`` alone owns ``unlock_raw_15`` (``valuation.py`` keeps the
  V1 ``valuation_raw_10``); FULL fuses them in ``scoring/ltss.py``.
- Phase 6 enables FULL with Catalyst: three profiles hit 100, no weight
  rebalancing on missing data, DQ thresholds and event PAUSE.
- The three providers register on the Task 13 ``ProviderRegistry``;
  ``runtime`` / ``service`` pick LITE/FULL by requested/effective tier,
  verified end-to-end through the frozen Task 14 API (TestClient).
- FULL requested but keyless/disabled -> requested FULL, effective LITE,
  ``FULL_PREREQUISITE_MISSING``; transient 429 under FULL stays FULL
  with lower DQ and NOT_READY, never mixing LITE rows in.
- Regression: Task 10/11 suites are run alongside (see CI command).

All network access is faked (injected fetchers); no live requests.

NOTE: provider modules are imported lazily inside the tests and unloaded
afterwards so ``test_shortlab_runtime.py``'s "future modules are never
imported by the service layer" assertion keeps passing in shared
pytest processes.
"""

from __future__ import annotations

import dataclasses
import sys
import types

import pytest
from fastapi.testclient import TestClient

from diveintocrypto_desktop.shortlab.config import load_shortlab_config
from diveintocrypto_desktop.shortlab.features import narrative as narrative_mod
from diveintocrypto_desktop.shortlab.features import supply as supply_mod
from diveintocrypto_desktop.shortlab.models import ProviderResult
from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
from diveintocrypto_desktop.shortlab.quality import (
    FULL_GROUP_WEIGHTS,
    FULL_PREREQUISITE_MISSING,
    LITE_GROUP_WEIGHTS,
    FieldState,
    data_quality,
)
from diveintocrypto_desktop.shortlab.repository import (
    CatalystEventRecord,
    ShortLabRepository,
    SocialSnapshotRecord,
    UnlockEventRecord,
    schema_target_for_config,
)
from diveintocrypto_desktop.shortlab.runtime import build_default_registry
from diveintocrypto_desktop.shortlab.scoring.ltss import (
    extract_features,
    score_full,
)
from diveintocrypto_desktop.shortlab.scoring.versions import SCORE_VERSION_FULL
from diveintocrypto_desktop.shortlab.service import ShortLabService

DAY_MS = 86_400_000
NOW = 1_750_000_000_000

_FUTURE_MODULES = (
    "diveintocrypto_desktop.shortlab.providers.unlock",
    "diveintocrypto_desktop.shortlab.providers.social",
    "diveintocrypto_desktop.shortlab.providers.catalyst",
)


@pytest.fixture(autouse=True)
def _unload_future_providers():
    yield
    for name in _FUTURE_MODULES:
        sys.modules.pop(name, None)


def _unlock_mod():
    from diveintocrypto_desktop.shortlab.providers import unlock as mod

    return mod


def _social_mod():
    from diveintocrypto_desktop.shortlab.providers import social as mod

    return mod


def _catalyst_mod():
    from diveintocrypto_desktop.shortlab.providers import catalyst as mod

    return mod


# ---------------------------------------------------------------------------
# Clocks / identities / service fakes
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start_ms: int = NOW) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return int(self.ms)

    def advance(self, ms: int) -> None:
        self.ms += ms


def make_identity(
    *,
    canonical_id: str = "testcoin",
    coingecko_id: str | None = "testcoin",
    unlock_provider_id: str | None = "testcoin-unlock",
    social_provider_id: str | None = "testcoin-social",
    confidence: str = "HIGH",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        canonical_id=canonical_id,
        coingecko_id=coingecko_id,
        unlock_provider_id=unlock_provider_id,
        social_provider_id=social_provider_id,
        mapping_confidence=confidence,
        binance_futures_symbol="TESTUSDT",
    )


def make_universe(n: int) -> list[dict]:
    return [
        {"s": f"T{i:03d}USDT", "price": 4.0, "ch": 1.0,
         "quote_volume": float(1_000_000_000 - i)}
        for i in range(n)
    ]


def make_metadata(symbols: list[str], clock_ms: int) -> dict[str, dict]:
    return {
        symbol: {
            "symbol": symbol,
            "onboard_at_ms": clock_ms - 400 * DAY_MS,
            "first_seen_ms": clock_ms - 400 * DAY_MS,
            "delivery_at_ms": None,
            "status": "TRADING",
            "contract_type": "PERPETUAL",
            "observed_at_ms": clock_ms,
            "quote_to_usd": "1",  # Explicit FX for this offline happy-path fixture.
            "contract_multiplier": None,
            "multiplier_source": None,
        }
        for symbol in symbols
    }


def make_overrides(symbols: list[str]) -> dict:
    return {
        symbol: {
            "canonical_id": symbol.lower().replace("usdt", ""),
            "display_symbol": symbol,
            "contract_multiplier": 1.0,
            "multiplier_source": "MANUAL",
            "coingecko_id": symbol.lower().replace("usdt", ""),
            "unlock_provider_id": f"{symbol.lower().replace('usdt', '')}-unlock",
            "social_provider_id": f"{symbol.lower().replace('usdt', '')}-social",
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


def fake_spot_unavailable(identity: object, as_of_ms: int) -> ProviderResult[None]:
    return ProviderResult(
        status="UNAVAILABLE", source="spot", fetched_at_ms=as_of_ms, as_of_ms=None,
        data=None, stale=False, reason_code="SPOT_FETCH_FAILED", error_message=None,
    )


class FakeCoinGecko:
    name = "coingecko"

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.calls: list[object] = []

    async def fetch(self, identity: object) -> ProviderResult[dict]:
        self.calls.append(identity)
        return ProviderResult(
            status="OK", source="coingecko", fetched_at_ms=self._clock(),
            as_of_ms=self._clock(),
            data={
                "market_cap_usd": 500_000_000.0,
                "fdv_usd": 2_000_000_000.0,
                "circulating_supply": 10_000_000.0,
                "total_supply": 50_000_000.0,
                "ath_usd": 10.0,
                "ath_date_ms": NOW - 300 * DAY_MS,
                "categories": [],
            },
            stale=False, reason_code=None, error_message=None,
        )


ENTRY_BLOCKS = ("consensus", "mtf", "micro", "regime", "failed_bounce", "funding")


def make_entry_record(symbol: str, as_of_ms: int, score: float | None = 75.0):
    from diveintocrypto_desktop.shortlab.repository import EntrySnapshotRecord

    meta = {
        block: {"status": "OK" if score is not None else "UNAVAILABLE",
                "fetched_at_ms": as_of_ms, "as_of_ms": as_of_ms,
                "coverage_fraction": 1.0, "reason_code": None, "source": "fake-entry"}
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
    def __init__(self, record) -> None:
        self._record = record
        self.symbol = record.symbol
        self.entry_score = record.entry_score
        self.reason_code = None
        self.snapshot_id = record.snapshot_id

    def to_record(self):
        return self._record


class FakeEntryRunner:
    def __init__(self, clock_ms: int = NOW) -> None:
        from diveintocrypto_desktop.shortlab.entry import EntryBatchResult

        self._batch_cls = EntryBatchResult
        self._clock_ms = clock_ms

    async def __call__(self, symbols, *, budget, max_symbols, as_of_ms, now_ms):
        items = tuple(
            FakeEntryResult(make_entry_record(s, as_of_ms, 75.0))
            for s in symbols[:max_symbols]
        )
        return self._batch_cls(items=items, queued_symbols=(),
                               stats={"calls_made": 18 * len(items), "cache_hits": 0})


# ---------------------------------------------------------------------------
# Fake HTTP payloads for the three real providers
# ---------------------------------------------------------------------------


def unlock_payload(as_of_ms: int, *, known_at_ms: int | None = None) -> dict:
    known = known_at_ms if known_at_ms is not None else as_of_ms - DAY_MS
    return {
        "canonical_id": "t000",
        "events": [
            {"event_id": "u-30d", "unlock_at_ms": as_of_ms + 10 * DAY_MS,
             "amount_tokens": 1_200_000.0, "allocation_type": "TEAM",
             "known_at_ms": known, "source": "tokenomist"},
            {"event_id": "u-90d", "unlock_at_ms": as_of_ms + 60 * DAY_MS,
             "amount_tokens": 3_000_000.0, "allocation_type": "SEED",
             "known_at_ms": known, "source": "tokenomist"},
            # Duplicate id, older known_at: dedup keeps the newer one.
            {"event_id": "u-30d", "unlock_at_ms": as_of_ms + 10 * DAY_MS,
             "amount_tokens": 100.0, "allocation_type": "TEAM",
             "known_at_ms": known - 10 * DAY_MS, "source": "tokenomist"},
        ],
    }


def social_payload(as_of_ms: int) -> dict:
    return {
        "as_of_ms": as_of_ms,
        "windows": {
            "prev_30d": {"volume": 100.0, "contributors": 100.0, "dominance": 100.0},
            "recent_30d": {"volume": 40.0, "contributors": 60.0, "dominance": 90.0},
        },
        "price_change_30d": 0.15,
        "spot_volume_change_30d": -0.50,
    }


def catalyst_payload(as_of_ms: int, *, severity: str = "INFO") -> dict:
    return {
        "asset_id": "t000",
        "events": [
            {"event_id": "c-major", "announced_at_ms": as_of_ms - 5 * DAY_MS,
             "effective_at_ms": as_of_ms + 2 * DAY_MS, "event_type": "CEX_LISTING",
             "severity": severity, "confidence": 0.9,
             "source_url": "https://example.com/list", "title": "Listing",
             "known_at_ms": as_of_ms - 5 * DAY_MS},
            {"event_id": "c-info", "announced_at_ms": as_of_ms - 60 * DAY_MS,
             "effective_at_ms": None, "event_type": "PRODUCT",
             "severity": "INFO", "confidence": 0.5,
             "source_url": None, "title": "Update",
             "known_at_ms": as_of_ms - 60 * DAY_MS},
        ],
    }


class FakeHttp:
    """Counting fake HTTP layer: ``mode`` flips to failure kinds.

    ``retryable_factory`` / ``notfound_factory`` build the *provider's
    own* error types so status mapping is exercised end to end.
    """

    def __init__(
        self,
        payload: dict,
        *,
        retryable_factory=None,
        notfound_factory=None,
    ) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []
        self.mode = "ok"
        self._retryable_factory = retryable_factory
        self._notfound_factory = notfound_factory

    async def __call__(self, url: str, headers: dict) -> dict:
        self.calls.append((url, dict(headers)))
        if self.mode == "rate_limited":
            raise self._retryable_factory("rate_limited", "429 fake")
        if self.mode == "timeout":
            raise self._retryable_factory("timeout", "timeout fake")
        if self.mode == "not_found":
            raise self._notfound_factory("404 fake")
        return self.payload


def _fake_http(mod, payload: dict) -> FakeHttp:
    retryable = next(
        getattr(mod, name) for name in dir(mod) if name.startswith("_Retryable")
    )
    notfound = next(
        getattr(mod, name)
        for name in dir(mod)
        if name.endswith("NotFound") and name != "ReferenceNotFoundError"
    )
    return FakeHttp(payload, retryable_factory=retryable,
                    notfound_factory=notfound)


# ---------------------------------------------------------------------------
# Service assembly
# ---------------------------------------------------------------------------


def full_config():
    return dataclasses.replace(load_shortlab_config(), analysis_tier="FULL")


def make_full_registry(    clock: FakeClock,
    unlock_http: FakeHttp,
    social_http: FakeHttp,
    catalyst_http: FakeHttp,
) -> ProviderRegistry:
    unlock_mod = _unlock_mod()
    social_mod = _social_mod()
    catalyst_mod = _catalyst_mod()
    registry = ProviderRegistry()
    registry.register("coingecko", FakeCoinGecko(clock))
    registry.register(
        "unlock",
        unlock_mod.UnlockProvider(api_key="test-unlock-key", clock=clock,
                                  fetcher=unlock_http),
    )
    registry.register(
        "social",
        social_mod.SocialProvider(api_key="test-social-key", clock=clock,
                                  fetcher=social_http),
    )
    registry.register(
        "catalyst",
        catalyst_mod.CatalystProvider(clock=clock, fetcher=catalyst_http),
    )
    return registry


def make_lite_registry(clock: FakeClock) -> ProviderRegistry:
    """LITE registry: fundamentals only, no Phase 5/6 providers."""
    registry = ProviderRegistry()
    registry.register("coingecko", FakeCoinGecko(clock))
    return registry


async def open_repo(tmp_path, target: int = 3) -> ShortLabRepository:
    repo = await ShortLabRepository.open(db_path=tmp_path / "shortlab.duckdb")
    await repo.migrate(target_version=target)
    return repo


def make_service(
    repo: ShortLabRepository,
    clock: FakeClock,
    registry: ProviderRegistry,
    *,
    config=None,
    n: int = 2,
) -> tuple[ShortLabService, dict]:
    rows = make_universe(n)
    symbols = [r["s"] for r in rows]
    observed: dict = {"fetched_symbols": []}

    async def universe_fn(limit=None):
        return list(rows[:limit] if limit else rows)

    meta = make_metadata(symbols, clock())

    async def metadata_fn():
        return dict(meta)

    async def funding_fn(symbol: str, start_ms: int, end_ms: int):
        observed["fetched_symbols"].append(symbol)
        return funding_events(end_ms)

    from diveintocrypto_desktop.data import funding as funding_mod

    service = ShortLabService(
        config=config or full_config(),
        repository=repo,
        registry=registry,
        clock=clock,
        universe_fn=universe_fn,
        metadata_fn=metadata_fn,
        funding_history_fn=funding_fn,
        funding_coverage_fn=funding_mod.funding_coverage,
        spot_history_fn=fake_spot_unavailable,
        market_inputs_fn=fake_market,
        identity_candidates_fn=lambda symbol: [],
        identity_overrides=make_overrides(symbols),
        entry_runner=FakeEntryRunner(clock()),
    )
    return service, observed


def _stub_app(service: ShortLabService):
    from diveintocrypto_desktop.api.app import create_app

    app = create_app()

    class _StubRuntime:
        available = True
        unavailable_reason = "shortlab_unavailable"

        @property
        def service(self):
            return service

        @property
        def config(self):
            return service.config

        @property
        def registry(self):
            return service.registry

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    app.state.shortlab_runtime = _StubRuntime()
    return app


# ---------------------------------------------------------------------------
# 1. Phase 5: NullProvider / keyless fixtures keep LITE intact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase5_null_providers_keep_lite_intact(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        # No FULL provider registered at all: FULL requested -> LITE fallback.
        service, _ = make_service(repo, clock, make_lite_registry(clock),
                                  config=full_config(), n=2)
        tier, warnings = service.effective_tier("FULL")
        assert tier == "LITE"
        assert warnings == (FULL_PREREQUISITE_MISSING,)
        status = await service.run_refresh()
        assert status.status == "SUCCEEDED"
        assert status.stats["effective_tier"] == "LITE"
        assert FULL_PREREQUISITE_MISSING in status.stats["tier_warnings"]
        page = await repo.list_candidates(generation_id=status.job_id)
        assert page.total == 2
        for item in page.items:
            assert item.analysis_tier == "LITE"
            assert item.profile.endswith("_LITE")
            assert item.ltss is not None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_phase5_real_providers_without_ids_stay_lite(tmp_path) -> None:
    """Registered providers with unmapped identities: NOT_APPLICABLE, no HTTP."""
    clock = FakeClock()
    unlock_mod = _unlock_mod()
    social_mod = _social_mod()
    catalyst_mod = _catalyst_mod()
    unlock_http = _fake_http(unlock_mod, unlock_payload(NOW))
    social_http = _fake_http(social_mod, social_payload(NOW))
    catalyst_http = _fake_http(catalyst_mod, catalyst_payload(NOW))
    repo = await open_repo(tmp_path)
    try:
        registry = make_full_registry(clock, unlock_http, social_http, catalyst_http)
        service, _ = make_service(repo, clock, registry, config=full_config(), n=1)
        # Identity without provider ids: every FULL fetch is NOT_APPLICABLE.
        bare = make_identity(unlock_provider_id=None, social_provider_id=None,
                             coingecko_id=None, canonical_id="")
        assert (await registry.get("unlock").fetch(bare)).status == "NOT_APPLICABLE"
        assert (await registry.get("social").fetch(bare)).status == "NOT_APPLICABLE"
        assert (await registry.get("catalyst").fetch(bare)).status == "NOT_APPLICABLE"
        assert unlock_http.calls == [] and social_http.calls == []
        assert catalyst_http.calls == []
        status = await service.run_refresh()
        assert status.status == "SUCCEEDED"
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# 2. Provider units: identity gating, limiter cache, cutoff, dedup, hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unlock_identity_gating_no_http_without_id() -> None:
    unlock_mod = _unlock_mod()
    http = _fake_http(unlock_mod, unlock_payload(NOW))
    provider = unlock_mod.UnlockProvider(api_key="k", clock=FakeClock(), fetcher=http)
    result = await provider.fetch(make_identity(unlock_provider_id=None))
    assert result.status == "NOT_APPLICABLE"
    assert result.reason_code == "IDENTITY_NOT_MAPPED"
    assert result.data is None
    assert http.calls == []


@pytest.mark.asyncio
async def test_unlock_events_cutoff_and_dedup() -> None:
    unlock_mod = _unlock_mod()
    http = _fake_http(unlock_mod, unlock_payload(NOW))
    provider = unlock_mod.UnlockProvider(api_key="k", clock=FakeClock(), fetcher=http)
    identity = make_identity()
    result = await provider.fetch(identity, NOW)
    assert result.status == "OK"
    assert result.reason_code is None
    ids = sorted(e.event_id for e in result.data.events)
    assert ids == ["u-30d", "u-90d"]  # duplicate id deduped to the newer known_at
    kept = next(e for e in result.data.events if e.event_id == "u-30d")
    assert kept.amount_tokens == 1_200_000.0
    # Future-known event versions are invisible at an earlier cutoff.
    early = await provider.fetch(identity, NOW - 20 * DAY_MS)
    assert early.status == "OK"
    assert early.data.events == ()
    # Second fetch inside the TTL performs no HTTP.
    await provider.fetch(identity, NOW)
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_unlock_rate_limit_then_stale() -> None:
    unlock_mod = _unlock_mod()
    clock = FakeClock()
    http = _fake_http(unlock_mod, unlock_payload(NOW))
    provider = unlock_mod.UnlockProvider(api_key="secret-key-123", clock=clock,
                                         fetcher=http)
    identity = make_identity()
    ok_result = await provider.fetch(identity, NOW)
    assert ok_result.status == "OK" and not ok_result.stale
    clock.advance(43_200_000 + 1)  # expire the 12h TTL
    http.mode = "rate_limited"
    limited = await provider.fetch(identity, NOW)
    assert limited.status == "UNAVAILABLE"
    assert limited.reason_code == "UNLOCK_RATE_LIMITED"
    assert limited.stale is True
    assert limited.data is not None and len(limited.data.events) == 2
    # No key material in the URL or the surfaced diagnostics.
    assert "secret-key-123" not in limited.data.request_url
    assert limited.data.request_url.startswith("https://")
    assert "secret-key-123" not in (limited.error_message or "")
    assert http.calls[0][1].get("Authorization") == "Bearer secret-key-123"
    # Fresh 404 with no cache is an ERROR with no data.
    http2 = _fake_http(unlock_mod, unlock_payload(NOW))
    http2.mode = "not_found"
    fresh = unlock_mod.UnlockProvider(api_key="k", clock=FakeClock(), fetcher=http2)
    missing = await fresh.fetch(identity, NOW)
    assert missing.status == "ERROR"
    assert missing.reason_code == "UNLOCK_UNKNOWN_ID"
    assert missing.data is None


@pytest.mark.asyncio
async def test_social_windows_and_cache() -> None:
    social_mod = _social_mod()
    http = _fake_http(social_mod, social_payload(NOW))
    provider = social_mod.SocialProvider(api_key="k", clock=FakeClock(), fetcher=http)
    gated = await provider.fetch(make_identity(social_provider_id=None))
    assert gated.status == "NOT_APPLICABLE" and http.calls == []
    result = await provider.fetch(make_identity(), NOW)
    assert result.status == "OK"
    assert result.data.volume_30d == 40.0
    assert result.data.contributors_prev_30d == 100.0
    assert result.data.price_change_30d == 0.15
    await provider.fetch(make_identity(), NOW)
    assert len(http.calls) == 1
    assert "k" not in result.data.request_url


@pytest.mark.asyncio
async def test_catalyst_events_and_major_window() -> None:
    catalyst_mod = _catalyst_mod()
    http = _fake_http(catalyst_mod, catalyst_payload(NOW, severity="MAJOR"))
    provider = catalyst_mod.CatalystProvider(clock=FakeClock(), fetcher=http)
    gated = await provider.fetch(make_identity(canonical_id="", coingecko_id=None))
    assert gated.status == "NOT_APPLICABLE" and http.calls == []
    result = await provider.fetch(make_identity(), NOW)
    assert result.status == "OK"
    assert {e.event_id for e in result.data.events} == {"c-major", "c-info"}
    major = result.data.major_in_window(NOW, 30 * DAY_MS)
    assert major is not None and major.event_id == "c-major"
    assert result.data.major_in_window(NOW - 60 * DAY_MS, 30 * DAY_MS) is None


# ---------------------------------------------------------------------------
# 3. supply.py: 30/90D unlock bins, allocation weights, unknowns
# ---------------------------------------------------------------------------


class TestSupplyFeatures:
    def test_pressure_math(self) -> None:
        assert supply_mod.unlock_pressure(3_000_000.0, 10_000_000.0) == 0.3
        assert supply_mod.unlock_pressure(0.0, 10_000_000.0) == 0.0
        assert supply_mod.unlock_pressure(1.0, 0.0) is None
        assert supply_mod.unlock_pressure(None, 10_000_000.0) is None

    def test_allocation_weights(self) -> None:
        assert supply_mod.allocation_weight("SEED") == 1.0
        assert supply_mod.allocation_weight("TEAM") == 0.8
        assert supply_mod.allocation_weight("ECOSYSTEM") == 0.3
        assert supply_mod.allocation_weight("whatever") == 0.3  # OTHER

    def test_unlock_bins(self) -> None:
        assert supply_mod.score_unlock_90d(0.30)[0] == 10
        assert supply_mod.score_unlock_90d(0.15)[0] == 7
        assert supply_mod.score_unlock_90d(0.05)[0] == 3
        assert supply_mod.score_unlock_90d(0.049)[0] == 0
        assert supply_mod.score_unlock_90d(None)[0] is None
        assert supply_mod.score_unlock_30d(0.10)[0] == 5
        assert supply_mod.score_unlock_30d(0.05)[0] == 3
        assert supply_mod.score_unlock_30d(0.049)[0] == 0
        assert supply_mod.score_unlock_30d(None)[0] is None

    def test_forward_raw_15_weights_and_bins(self) -> None:
        events = [
            {"event_id": "a", "unlock_at_ms": NOW + 10 * DAY_MS,
             "amount_tokens": 1_200_000.0, "allocation_type": "TEAM"},
            {"event_id": "b", "unlock_at_ms": NOW + 60 * DAY_MS,
             "amount_tokens": 3_000_000.0, "allocation_type": "SEED"},
        ]
        raw, details, _ = supply_mod.compute_unlock_raw_15_forward(
            events, 10_000_000.0, NOW
        )
        # 30D: 1.2M * 0.8 / 10M = 0.096 -> 3; 90D: +3M * 1.0 -> 0.396 -> 10.
        assert details["pressure_30d"] == pytest.approx(0.096)
        assert details["pressure_90d"] == pytest.approx(0.396)
        assert raw == 13

    def test_forward_raw_unknown_inputs(self) -> None:
        raw, _, reasons = supply_mod.compute_unlock_raw_15_forward(
            None, 10_000_000.0, NOW
        )
        assert raw is None and reasons == ("UNLOCK_INPUT_MISSING",)
        raw, _, _ = supply_mod.compute_unlock_raw_15_forward([], None, NOW)
        assert raw is None

    def test_forward_raw_known_empty_is_zero(self) -> None:
        raw, details, _ = supply_mod.compute_unlock_raw_15_forward([], 10_000_000.0, NOW)
        assert raw == 0
        assert details["pressure_30d"] == 0.0


# ---------------------------------------------------------------------------
# 4. narrative.py: two 30D windows, divergences, severity inputs
# ---------------------------------------------------------------------------


class TestNarrativeFeatures:
    def test_full_decay(self) -> None:
        score, ratio, _ = narrative_mod.decay_score(40.0, 100.0, 5, field="X")
        assert (score, ratio) == (5, 0.4)

    def test_linear_decay(self) -> None:
        score, ratio, reason = narrative_mod.decay_score(75.0, 100.0, 5, field="X")
        assert ratio == 0.75
        assert score == 2  # round(5 * 0.25 / 0.5)
        assert reason == "X_PARTIAL_DECAY"

    def test_no_decay_and_missing(self) -> None:
        assert narrative_mod.decay_score(120.0, 100.0, 5, field="X")[0] == 0
        assert narrative_mod.decay_score(None, 100.0, 5, field="X")[0] is None
        assert narrative_mod.decay_score(50.0, 0.0, 5, field="X")[0] is None

    def test_divergence(self) -> None:
        assert narrative_mod.divergence_score(0.15, -0.45, 2, field="D")[0] == 2
        assert narrative_mod.divergence_score(0.05, -0.45, 2, field="D")[0] == 0
        assert narrative_mod.divergence_score(0.15, -0.10, 2, field="D")[0] == 0
        assert narrative_mod.divergence_score(None, -0.45, 2, field="D")[0] is None

    def test_narrative_raw_15_synthesis(self) -> None:
        raw, details, _ = narrative_mod.compute_narrative_raw_15(
            100.0, 40.0, 100.0, 60.0, 100.0, 90.0,
            price_change_30d=0.15, spot_volume_change_30d=0.20,
        )
        # volume 0.4 -> 5; contributors 0.6 -> 2; dominance 0.9 -> 1;
        # price +15% vs social -60% -> 2; spot +20% vs social -60% -> 2.
        assert raw == 12
        assert details["social_volume_decay"][0] == 5

    def test_narrative_raw_all_missing_is_none(self) -> None:
        raw, _, reasons = narrative_mod.compute_narrative_raw_15(
            None, None, None, None, None, None
        )
        assert raw is None and reasons == ("NARRATIVE_INPUT_MISSING",)


# ---------------------------------------------------------------------------
# 5. score_full: three profiles at 100, no reweighting, critical nulls
# ---------------------------------------------------------------------------


def _max_inputs(**overrides) -> dict:
    closes = [100 - i * 0.9 for i in range(60)]
    tail = [40, 39, 38, 37, 36, 35, 34, 33, 32, 30, 34, 34.5, 35, 34, 33, 32.5,
            32, 31.5, 31, 32]
    closes = closes + tail
    highs = [c * 1.02 for c in closes]
    highs[20] = 95.0
    highs[40] = 75.0
    for idx in (20, 40):
        for d in (-3, -2, -1, 1, 2, 3):
            if highs[idx + d] >= highs[idx]:
                highs[idx + d] = highs[idx] - 5.0
    inputs: dict = {
        "symbol": "TESTUSDT",
        "ath_price": 100.0,
        "current_price": closes[-1],
        "ath_date_ms": NOW - 200 * DAY_MS,
        "daily_closes": list(closes),
        "daily_highs": list(highs),
        "close_30d_ago": 40.0,
        "spot_volume_30d": 60.0,
        "spot_volume_prev_30d": 100.0,
        "spot_applicable": True,
        "funding_30d": 0.025,
        "funding_30d_complete": True,
        "funding_positive_ratio_30d": 0.75,
        "funding_30d_ratio_complete": True,
        "funding_positive_ratio_90d": 0.80,
        "funding_90d_complete": True,
        "funding_rates_30d": [0.0001] * 90,
        "market_cap_usd": 100_000_000.0,
        "fdv_usd": 600_000_000.0,
        "circulating_supply": 15_000_000.0,
        "total_supply": 100_000_000.0,
        "oi_value_usd": 15_000_000.0,
        "price_change_7d": -0.06,
        "oi_change_7d": 0.06,
        "futures_spot_volume_ratio": 6.0,
        "ls_ratio": 2.0,
        "futures_qv_1d": 35_000_000.0,
        "spread": 0.001,
        "book_depth_min_1pct": 1_500_000.0,
        "contract_status": "TRADING",
        "has_settlement_record": True,
        "unlock_raw_15": 15.0,
        "narrative_raw_15": 15.0,
    }
    inputs.update(overrides)
    return inputs


class TestScoreFull:
    @pytest.mark.parametrize(
        "profile", ["MEME_FULL", "GENERAL_FULL", "LOW_FLOAT_VC_FULL"]
    )
    def test_all_max_full_is_100(self, profile) -> None:
        config = load_shortlab_config()
        snap = extract_features(_max_inputs(), NOW)
        breakdown = score_full(snap, profile, config)
        assert breakdown.score_version == SCORE_VERSION_FULL
        assert breakdown.profile == profile
        assert breakdown.module_raws["valuation"] == 25
        assert breakdown.module_raws["narrative"] == 15
        assert breakdown.ltss == 100.0

    def test_missing_unlock_never_reweights(self) -> None:
        config = load_shortlab_config()
        snap = extract_features(
            _max_inputs(unlock_raw_15=None, narrative_raw_15=None), NOW
        )
        breakdown = score_full(snap, "GENERAL_FULL", config)
        assert breakdown.module_raws["valuation"] == 10  # 10 + 0, still over 25
        assert breakdown.module_scores["valuation"] == pytest.approx(10 / 25 * 20)
        assert breakdown.module_raws["narrative"] == 0
        assert breakdown.ltss is not None and breakdown.ltss < 100.0

    def test_critical_missing_nulls_full(self) -> None:
        config = load_shortlab_config()
        snap = extract_features(_max_inputs(market_cap_usd=None), NOW)
        breakdown = score_full(snap, "GENERAL_FULL", config)
        assert breakdown.ltss is None
        assert breakdown.null_reason is not None and "MC" in breakdown.null_reason


# ---------------------------------------------------------------------------
# 6. Migrations 002/003: DDL fidelity, forward motion, replay cutoffs
# ---------------------------------------------------------------------------


async def _columns(repo: ShortLabRepository, table: str) -> list[str]:
    rows = await repo._run(_pragma_table_info, repo, table)
    return rows


def _pragma_table_info(repo: ShortLabRepository, table: str) -> list[str]:
    con = repo._require_con()
    cur = con.execute(f"PRAGMA table_info('{table}')")
    return [row[1] for row in cur.fetchall()]


@pytest.mark.asyncio
async def test_migration_ddl_matches_design_19_2(tmp_path) -> None:
    repo = await ShortLabRepository.open(db_path=tmp_path / "ddl.duckdb")
    try:
        assert await repo.migrate(target_version=3) == 3
        assert await _columns(repo, "sl_unlock_event") == [
            "event_id", "canonical_id", "known_at_ms", "unlock_at_ms",
            "amount_tokens", "allocation_type", "source", "fetched_at_ms",
        ]
        assert await _columns(repo, "sl_social_snapshot") == [
            "snapshot_id", "canonical_id", "as_of_ms", "fetched_at_ms",
            "source", "metrics_json",
        ]
        assert await _columns(repo, "sl_catalyst_event") == [
            "event_id", "canonical_id", "known_at_ms", "announced_at_ms",
            "effective_at_ms", "event_type", "severity", "confidence",
            "source_url", "title",
        ]
        indexes = await repo.index_names()
        assert "idx_sl_unlock_asset_time" in indexes
        assert "idx_sl_social_asset_time" in indexes
        assert "idx_sl_catalyst_asset_time" in indexes
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_phase5_cap_never_runs_003(tmp_path) -> None:
    repo = await ShortLabRepository.open(db_path=tmp_path / "phase5.duckdb")
    try:
        assert await repo.migrate(target_version=2) == 2
        assert await _columns(repo, "sl_unlock_event") is not None
        assert await _columns(repo, "sl_social_snapshot") is not None
        with pytest.raises(Exception):
            await repo.save_catalyst_events([
                CatalystEventRecord(
                    event_id="c", canonical_id="t", known_at_ms=NOW,
                    announced_at_ms=NOW, effective_at_ms=None,
                    event_type="PRODUCT", severity="INFO", confidence=0.5,
                    source_url=None, title="t",
                )
            ])
        # Phase 6 upgrade is forward-only from the Phase 5 state.
        assert await repo.migrate(target_version=3) == 3
        assert await _columns(repo, "sl_catalyst_event") is not None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_forward_migration_from_v1_and_repeat(tmp_path) -> None:
    repo = await ShortLabRepository.open(db_path=tmp_path / "fwd.duckdb")
    try:
        assert await repo.migrate(target_version=1) == 1  # explicit stepwise path
        assert await repo.migrate(target_version=2) == 2
        saved = await repo.save_unlock_events([
            UnlockEventRecord(
                event_id="u", canonical_id="t", known_at_ms=NOW,
                unlock_at_ms=NOW + DAY_MS, amount_tokens=5.0,
                allocation_type="SEED", source="tokenomist", fetched_at_ms=NOW,
            )
        ])
        assert saved == 1
        assert await repo.migrate(target_version=3) == 3
        assert await repo.migrate(target_version=3) == 3  # repeat: no-op
        events = await repo.list_unlock_events("t", NOW, NOW + 2 * DAY_MS, NOW)
        assert len(events) == 1 and events[0].amount_tokens == 5.0
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_t0_replay_excludes_future_known_events(tmp_path) -> None:
    repo = await open_repo(tmp_path)
    try:
        t0 = NOW
        await repo.save_unlock_events([
            UnlockEventRecord(
                event_id="u1", canonical_id="t", known_at_ms=t0 - 100,
                unlock_at_ms=t0 + 10 * DAY_MS, amount_tokens=100.0,
                allocation_type="TEAM", source="tokenomist", fetched_at_ms=t0 - 100,
            ),
            # Same event id, revised upstream: visible only after t0.
            UnlockEventRecord(
                event_id="u1", canonical_id="t", known_at_ms=t0 + 50,
                unlock_at_ms=t0 + 10 * DAY_MS, amount_tokens=200.0,
                allocation_type="TEAM", source="tokenomist", fetched_at_ms=t0 + 50,
            ),
            # Outside the vesting window: never listed.
            UnlockEventRecord(
                event_id="u2", canonical_id="t", known_at_ms=t0 - 100,
                unlock_at_ms=t0 + 400 * DAY_MS, amount_tokens=999.0,
                allocation_type="SEED", source="tokenomist", fetched_at_ms=t0 - 100,
            ),
        ])
        # Idempotent re-save of identical rows.
        await repo.save_unlock_events([
            UnlockEventRecord(
                event_id="u1", canonical_id="t", known_at_ms=t0 - 100,
                unlock_at_ms=t0 + 10 * DAY_MS, amount_tokens=100.0,
                allocation_type="TEAM", source="tokenomist", fetched_at_ms=t0 - 100,
            )
        ])
        at_t0 = await repo.list_unlock_events("t", t0, t0 + 90 * DAY_MS, t0)
        assert [e.event_id for e in at_t0] == ["u1"]
        assert at_t0[0].amount_tokens == 100.0
        later = await repo.list_unlock_events("t", t0, t0 + 90 * DAY_MS, t0 + 100)
        assert [e.event_id for e in later] == ["u1"]
        assert later[0].amount_tokens == 200.0  # dedup: latest known wins
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_social_snapshot_point_in_time(tmp_path) -> None:
    repo = await open_repo(tmp_path)
    try:
        old = SocialSnapshotRecord(
            snapshot_id="soc-old", canonical_id="t", as_of_ms=NOW - 10 * DAY_MS,
            fetched_at_ms=NOW - 10 * DAY_MS, source="social",
            metrics={"volume_30d": 10.0},
        )
        new = SocialSnapshotRecord(
            snapshot_id="soc-new", canonical_id="t", as_of_ms=NOW - DAY_MS,
            fetched_at_ms=NOW - DAY_MS, source="social",
            metrics={"volume_30d": 40.0},
        )
        await repo.save_social_snapshot(old)
        await repo.save_social_snapshot(new)
        await repo.save_social_snapshot(old)  # idempotent re-save
        assert (await repo.get_social_before("t", NOW - 5 * DAY_MS)).snapshot_id == "soc-old"
        assert (await repo.get_social_before("t", NOW)).snapshot_id == "soc-new"
        assert await repo.get_social_before("t", NOW - 30 * DAY_MS) is None
        assert await repo.get_social_before("nope", NOW) is None
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_catalyst_point_in_time_and_dedup(tmp_path) -> None:
    repo = await open_repo(tmp_path)
    try:
        await repo.save_catalyst_events([
            CatalystEventRecord(
                event_id="c1", canonical_id="t", known_at_ms=NOW - 100,
                announced_at_ms=NOW - 5 * DAY_MS, effective_at_ms=None,
                event_type="BURN", severity="MATERIAL", confidence=0.7,
                source_url=None, title="burn",
            ),
            CatalystEventRecord(
                event_id="c1", canonical_id="t", known_at_ms=NOW + 100,
                announced_at_ms=NOW - 5 * DAY_MS, effective_at_ms=None,
                event_type="BURN", severity="MAJOR", confidence=0.9,
                source_url=None, title="burn-up",
            ),
        ])
        at_t0 = await repo.list_catalyst_events("t", NOW)
        assert len(at_t0) == 1 and at_t0[0].severity == "MATERIAL"
        later = await repo.list_catalyst_events("t", NOW + 200)
        assert len(later) == 1 and later[0].severity == "MAJOR"
    finally:
        await repo.close()


def test_schema_target_for_config_phasing() -> None:
    from diveintocrypto_desktop.shortlab.config import ProviderConfig

    base = load_shortlab_config()
    # F01: schema target decoupled from provider flags (always 4; hedge -> 5 in H01)
    assert schema_target_for_config(base) == 4
    providers = dict(base.providers)
    providers["unlock"] = ProviderConfig(enabled=True, api_key_env="DIVE_TOKENOMIST_API_KEY")
    assert schema_target_for_config(dataclasses.replace(base, providers=providers)) == 4
    providers["social"] = ProviderConfig(enabled=True, api_key_env="DIVE_LUNARCRUSH_API_KEY")
    assert schema_target_for_config(dataclasses.replace(base, providers=providers)) == 4
    providers["catalyst"] = ProviderConfig(enabled=True, api_key_env=None)
    assert schema_target_for_config(dataclasses.replace(base, providers=providers)) == 4


# ---------------------------------------------------------------------------
# 7. FULL DQ: fixed groups, catalyst gate, thresholds, event PAUSE
# ---------------------------------------------------------------------------


def _all_ok_states(as_of_ms: int) -> list[FieldState]:
    from diveintocrypto_desktop.shortlab.quality import GROUP_FIELD_SHARES

    return [
        FieldState(field_id=field_id, status="OK", fetched_at_ms=as_of_ms,
                   source="test")
        for group_fields in GROUP_FIELD_SHARES.values()
        for field_id in group_fields
    ]


class TestFullDQ:
    def test_group_weights_sum_100(self) -> None:
        assert sum(LITE_GROUP_WEIGHTS.values()) == 100
        assert sum(FULL_GROUP_WEIGHTS.values()) == 100
        assert FULL_GROUP_WEIGHTS["unlock"] == 10
        assert FULL_GROUP_WEIGHTS["social"] == 10
        assert FULL_GROUP_WEIGHTS["catalyst"] == 5

    def test_full_without_catalyst_refuses(self) -> None:
        states = [s for s in _all_ok_states(NOW) if not s.field_id.startswith("catalyst")]
        with pytest.raises(ValueError, match=FULL_PREREQUISITE_MISSING):
            data_quality("FULL", states, NOW)

    def test_full_all_ok_is_100(self) -> None:
        assert data_quality("FULL", _all_ok_states(NOW), NOW).data_quality == 100.0

    def test_transient_catalyst_outage_lowers_dq_without_reweight(self) -> None:
        states = _all_ok_states(NOW)
        degraded = [
            dataclasses.replace(s, status="UNAVAILABLE",
                                reason_code="CATALYST_RATE_LIMITED")
            if s.field_id.startswith("catalyst") else s
            for s in states
        ]
        breakdown = data_quality("FULL", degraded, NOW)
        assert breakdown.data_quality == 95.0  # 5-point group lost, nothing moved
        assert breakdown.group_credits["catalyst"] == 0.0

    def test_dq_ready_threshold_from_config(self) -> None:
        assert load_shortlab_config().candidate.ready_data_quality == 80

    def test_major_catalyst_pauses(self) -> None:
        from diveintocrypto_desktop.shortlab.risk.veto import evaluate_risks

        paused = evaluate_risks(
            {"catalyst_major_event": True, "catalyst_severity": "MAJOR"},
            {"catalyst_major_event": True, "catalyst_severity": "MAJOR",
             "as_of_ms": NOW},
            95.0,
        )
        assert "PAUSE_MAJOR_CATALYST" in paused.pauses
        quiet = evaluate_risks(
            {"catalyst_major_event": False, "catalyst_severity": "INFO"},
            {"catalyst_major_event": False, "catalyst_severity": "INFO",
             "as_of_ms": NOW},
            95.0,
        )
        assert "PAUSE_MAJOR_CATALYST" not in quiet.pauses


# ---------------------------------------------------------------------------
# 8. Registry: three providers register; keyless stays Null
# ---------------------------------------------------------------------------


def test_runtime_registers_full_providers_with_keys() -> None:
    from diveintocrypto_desktop.shortlab.config import ProviderConfig

    base = load_shortlab_config()
    providers = dict(base.providers)
    providers["unlock"] = ProviderConfig(enabled=True, api_key_env="DIVE_TOKENOMIST_API_KEY")
    providers["social"] = ProviderConfig(enabled=True, api_key_env="DIVE_LUNARCRUSH_API_KEY")
    providers["catalyst"] = ProviderConfig(enabled=True, api_key_env=None)
    config = dataclasses.replace(base, providers=providers)
    registry = build_default_registry(
        config,
        env={"DIVE_TOKENOMIST_API_KEY": "k1", "DIVE_LUNARCRUSH_API_KEY": "k2"},
    )
    assert registry.has("unlock") and registry.has("social") and registry.has("catalyst")
    assert registry.get("unlock").name == "unlock"
    assert registry.get("social").name == "social"
    assert registry.get("catalyst").name == "catalyst"


def test_runtime_skips_keyless_providers() -> None:
    from diveintocrypto_desktop.shortlab.config import ProviderConfig

    base = load_shortlab_config()
    providers = dict(base.providers)
    providers["unlock"] = ProviderConfig(enabled=True, api_key_env="DIVE_TOKENOMIST_API_KEY")
    providers["social"] = ProviderConfig(enabled=False, api_key_env="DIVE_LUNARCRUSH_API_KEY")
    providers["catalyst"] = ProviderConfig(enabled=True, api_key_env=None)
    config = dataclasses.replace(base, providers=providers)
    registry = build_default_registry(config, env={})
    assert not registry.has("unlock")  # enabled but key missing
    assert not registry.has("social")  # disabled
    assert registry.has("catalyst")  # enabled, no key required
    assert registry.get("unlock").name == "null"  # NullProvider fallback
    service = ShortLabService(config=dataclasses.replace(base, analysis_tier="FULL"),
                              repository=None, registry=registry, clock=FakeClock())
    assert service.effective_tier() == ("LITE", (FULL_PREREQUISITE_MISSING,))


def test_default_config_registers_no_future_provider() -> None:
    registry = build_default_registry(load_shortlab_config())
    assert not registry.has("unlock")
    assert not registry.has("social")
    assert not registry.has("catalyst")


# ---------------------------------------------------------------------------
# 9. FULL end to end through the frozen Task 14 API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_end_to_end_changes_candidates(tmp_path) -> None:
    clock = FakeClock()
    unlock_mod = _unlock_mod()
    social_mod = _social_mod()
    catalyst_mod = _catalyst_mod()
    full_repo = await ShortLabRepository.open(db_path=tmp_path / "full.duckdb")
    lite_repo = await ShortLabRepository.open(db_path=tmp_path / "lite.duckdb")
    try:
        await full_repo.migrate(target_version=3)
        await lite_repo.migrate()
        full_service, _ = make_service(
            full_repo, clock,
            make_full_registry(
                clock,
                _fake_http(unlock_mod, unlock_payload(NOW)),
                _fake_http(social_mod, social_payload(NOW)),
                _fake_http(catalyst_mod, catalyst_payload(NOW)),
            ),
            n=2,
        )
        lite_service, _ = make_service(
            lite_repo, clock, ProviderRegistry(), config=load_shortlab_config(), n=2
        )
        full_status = await full_service.run_refresh()
        lite_status = await lite_service.run_refresh()
        assert full_status.status == "SUCCEEDED"
        assert full_status.stats["effective_tier"] == "FULL"
        assert lite_status.stats["effective_tier"] == "LITE"

        with TestClient(_stub_app(full_service)) as client:
            body = client.get("/api/short/candidates").json()
            assert body["analysisTier"] == "FULL"
            assert body["scoreVersion"] == "ltss-full-v1"
            assert body["total"] == 2
            for item in body["items"]:
                assert item["analysisTier"] == "FULL"
                assert item["profile"].endswith("_FULL")
                assert "narrative" in item["moduleScores"]
                assert "valuation" in item["moduleScores"]
        with TestClient(_stub_app(lite_service)) as client:
            lite_body = client.get("/api/short/candidates").json()
            assert lite_body["analysisTier"] == "LITE"
            for item in lite_body["items"]:
                assert item["profile"].endswith("_LITE")

        full_ltss = {i["symbol"]: i["ltss"] for i in body["items"]}
        lite_ltss = {i["symbol"]: i["ltss"] for i in lite_body["items"]}
        assert set(full_ltss) == set(lite_ltss)
        assert any(full_ltss[s] != lite_ltss[s] for s in full_ltss)
    finally:
        await full_repo.close()
        await lite_repo.close()


@pytest.mark.asyncio
async def test_full_requested_but_keyless_stays_lite(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        # Configured FULL, but the registry has no FULL provider: the
        # router-visible requested tier is FULL while rows stay LITE.
        service, _ = make_service(repo, clock, make_lite_registry(clock),
                                  config=full_config(), n=1)
        assert service.config.analysis_tier == "FULL"  # requestedTier
        status = await service.run_refresh()
        assert status.stats["effective_tier"] == "LITE"
        assert FULL_PREREQUISITE_MISSING in status.stats["tier_warnings"]
        with TestClient(_stub_app(service)) as client:
            health = client.get("/api/short/health").json()
            assert health["analysisTier"] == "FULL"
            body = client.get("/api/short/candidates").json()
            assert body["analysisTier"] == "LITE"
            assert body["items"][0]["analysisTier"] == "LITE"
            assert FULL_PREREQUISITE_MISSING in status.stats["tier_warnings"]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_full_transient_429_stays_full_with_lower_dq(tmp_path) -> None:
    clock = FakeClock()
    unlock_mod = _unlock_mod()
    social_mod = _social_mod()
    catalyst_mod = _catalyst_mod()
    catalyst_http = _fake_http(catalyst_mod, catalyst_payload(NOW))
    repo = await open_repo(tmp_path)
    try:
        service, _ = make_service(
            repo, clock,
            make_full_registry(
                clock,
                _fake_http(unlock_mod, unlock_payload(NOW)),
                _fake_http(social_mod, social_payload(NOW)),
                catalyst_http,
            ),
            n=1,
        )
        first = await service.run_refresh()
        assert first.stats["effective_tier"] == "FULL"
        page1 = await repo.list_candidates(generation_id=first.job_id)
        dq1 = page1.items[0].data_quality

        # Catalyst starts 429ing after its 30min TTL: same generation shape,
        # still FULL, lower DQ, NOT_READY -- never a LITE mix.
        clock.advance(1_800_001)
        catalyst_http.mode = "rate_limited"
        second = await service.run_refresh()
        assert second.status == "SUCCEEDED"
        assert second.stats["effective_tier"] == "FULL"
        page2 = await repo.list_candidates(generation_id=second.job_id)
        assert page2.total == 1
        item = page2.items[0]
        assert item.analysis_tier == "FULL"
        assert item.data_quality < dq1
        assert item.execution_status == "NOT_READY"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_full_major_catalyst_pauses_candidate(tmp_path) -> None:
    clock = FakeClock()
    unlock_mod = _unlock_mod()
    social_mod = _social_mod()
    catalyst_mod = _catalyst_mod()
    repo = await open_repo(tmp_path)
    try:
        service, _ = make_service(
            repo, clock,
            make_full_registry(
                clock,
                _fake_http(unlock_mod, unlock_payload(NOW)),
                _fake_http(social_mod, social_payload(NOW)),
                _fake_http(catalyst_mod,
                           catalyst_payload(NOW, severity="MAJOR")),
            ),
            n=1,
        )
        status = await service.run_refresh()
        assert status.stats["effective_tier"] == "FULL"
        with TestClient(_stub_app(service)) as client:
            body = client.get("/api/short/candidates").json()
            assert body["analysisTier"] == "FULL"
            pauses = body["items"][0]["pauses"]
            assert "PAUSE_MAJOR_CATALYST" in pauses
            detail = client.get("/api/short/symbol/T000USDT").json()
            assert "PAUSE_MAJOR_CATALYST" in detail["pauses"]
    finally:
        await repo.close()
