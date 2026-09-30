"""Task 9: CoinGecko fundamentals provider + registry (Short-Lab Phase 1).

Contract (ShortLab_Detailed_Design_CN.md 4.2/4.3/5.2,
ShortLab_Implementation_Plan_CN.md Task 9):

- Requests are keyed ONLY by ``identity.coingecko_id``; a missing id never
  triggers HTTP (same-name different coins must not be guessed).
- Captures MC, FDV, circulating/total/max supply, ATH price, ATH date and
  categories; absent fields stay ``None``, never 0.
- 429/5xx/timeout map to UNAVAILABLE/ERROR + stable ``reason_code``; a
  previous cache entry is served back with ``stale=True``.
- The raw request URL and fetch time travel with the snapshot for Task 10
  PIT use; logs/responses/errors never carry an API key.
- ``ProviderRegistry`` resolves by name and falls back to ``NullProvider``
  (Task 13 runtime, Task 17 FULL extension); DTOs come from
  ``shortlab.models`` and are never redefined here.

All network access is faked; no live CoinGecko requests.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import aiohttp
import pytest

from diveintocrypto_desktop.data.http import TransientUpstreamError
from diveintocrypto_desktop.shortlab.models import AssetIdentity, ProviderResult
from diveintocrypto_desktop.shortlab.providers import base as base_mod
from diveintocrypto_desktop.shortlab.providers import coingecko as cg
from diveintocrypto_desktop.shortlab.providers.base import (
    NullProvider,
    ProviderRegistry,
)
from diveintocrypto_desktop.shortlab.providers.coingecko import (
    CoinGeckoBadResponse,
    CoinGeckoNotFound,
    CoinGeckoProvider,
)

NOW_MS = 1_716_720_000_000  # 2024-06-01T00:00:00Z
ATH_ISO = "2024-05-27T12:00:00.000Z"
ATH_MS = int(dt.datetime(2024, 5, 27, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
LAST_UPDATED_ISO = "2024-06-01T00:00:00.000Z"


def _ident(coingecko_id: str | None = "pepe",
           futures: str = "1000PEPEUSDT") -> AssetIdentity:
    return AssetIdentity(
        canonical_id="PEPE",
        display_symbol="PEPE",
        name="Pepe",
        binance_futures_symbol=futures,
        binance_spot_symbol="PEPEUSDT",
        coingecko_id=coingecko_id,
        mapping_confidence="HIGH",
        mapping_source="UNIQUE_SYMBOL",
    )


def _coin_payload(**overrides) -> dict:
    doc = {
        "id": "pepe",
        "symbol": "pepe",
        "name": "Pepe",
        "categories": ["Meme", "Ethereum Ecosystem"],
        "market_data": {
            "current_price": {"usd": 0.000012},
            "market_cap": {"usd": 5_000_000_000},
            "fully_diluted_valuation": {"usd": 5_000_000_000},
            "circulating_supply": 420_690_000_000_000,
            "total_supply": 420_690_000_000_000,
            "max_supply": 420_690_000_000_000,
            "ath": {"usd": 0.000028},
            "ath_change_percentage": {"usd": -57.0},
            "ath_date": {"usd": ATH_ISO},
            "last_updated": LAST_UPDATED_ISO,
        },
    }
    for key, value in overrides.items():
        if key.startswith("market_"):
            doc["market_data"][key[len("market_"):]] = value
        else:
            doc[key] = value
    return doc


class _FakeClock:
    def __init__(self, now_ms: int = NOW_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


class _FakeHttp:
    """Scripted single-attempt fetcher replacing the aiohttp layer."""

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url: str, params) -> object:
        self.calls.append((url, dict(params)))
        action = self.behaviour
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(url, dict(params))
        return action


class _FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _provider(http: _FakeHttp, clock: _FakeClock | None = None,
              sleep: _FakeSleep | None = None, **kwargs) -> CoinGeckoProvider:
    return CoinGeckoProvider(
        clock=clock or _FakeClock(),
        sleep=sleep or _FakeSleep(),
        fetcher=http,
        **kwargs,
    )


# ── request identity: coingecko_id only, never guessed ──────────────────────

def test_request_uses_verified_coingecko_id_not_symbol():
    url, params = cg.build_coin_request("pepe")
    assert "/api/v3/coins/pepe" in url
    assert "1000PEPE" not in url
    assert params["market_data"] == "true"


def test_request_carries_no_key_material():
    url, params = cg.build_coin_request("bitcoin")
    blob = (url + str(sorted(params.items()))).lower()
    assert "key" not in blob
    assert "token" not in blob
    assert "auth" not in blob


@pytest.mark.asyncio
async def test_fetch_url_embeds_coingecko_id():
    http = _FakeHttp(_coin_payload())
    res = await _provider(http).fetch(_ident("pepe", futures="1000PEPEUSDT"))
    assert res.status == "OK"
    assert len(http.calls) == 1
    url, _ = http.calls[0]
    assert "/api/v3/coins/pepe" in url
    assert "1000PEPE" not in url
    assert "PEPEUSDT" not in url


@pytest.mark.asyncio
async def test_missing_coingecko_id_never_fetches():
    http = _FakeHttp(_coin_payload())
    res = await _provider(http).fetch(_ident(None))
    assert res.status == "NOT_APPLICABLE"
    assert res.reason_code == cg.IDENTITY_NOT_MAPPED
    assert res.data is None
    assert res.stale is False
    assert http.calls == []  # same-name coins must not be guessed by symbol


@pytest.mark.asyncio
async def test_blank_coingecko_id_never_fetches():
    http = _FakeHttp(_coin_payload())
    res = await _provider(http).fetch(_ident("   "))
    assert res.status == "NOT_APPLICABLE"
    assert http.calls == []


# ── OK parsing: MC/FDV/supply/ATH/date/categories + snapshot ───────────────

@pytest.mark.asyncio
async def test_ok_parses_all_fundamental_fields():
    res = await _provider(_FakeHttp(_coin_payload())).fetch(_ident())
    assert res.status == "OK"
    assert isinstance(res, ProviderResult)
    assert res.source == "coingecko"
    assert res.reason_code is None
    assert res.stale is False
    assert res.fetched_at_ms == NOW_MS
    assert res.as_of_ms == res.data.last_updated_ms
    data = res.data
    assert data.coingecko_id == "pepe"
    assert data.market_cap_usd == 5_000_000_000
    assert data.fdv_usd == 5_000_000_000
    assert data.circulating_supply == 420_690_000_000_000
    assert data.total_supply == 420_690_000_000_000
    assert data.max_supply == 420_690_000_000_000
    assert data.ath_usd == pytest.approx(0.000028)
    assert data.ath_change == pytest.approx(-0.57)  # decimal, not percent
    assert data.ath_date_ms == ATH_MS
    assert data.categories == ("Meme", "Ethereum Ecosystem")
    # Snapshot for Task 10 PIT: raw request + time travel with the data.
    assert "/api/v3/coins/pepe" in data.request_url
    assert "key" not in data.request_url.lower()
    assert data.fetched_at_ms == NOW_MS


@pytest.mark.asyncio
async def test_absent_fields_stay_none_never_zero():
    payload = _coin_payload(market_max_supply=None,
                            market_fully_diluted_valuation={"usd": None})
    payload["market_data"]["ath"] = {"usd": None}
    payload["categories"] = []
    res = await _provider(_FakeHttp(payload)).fetch(_ident())
    assert res.status == "OK"
    assert res.data.max_supply is None
    assert res.data.fdv_usd is None
    assert res.data.ath_usd is None
    assert res.data.categories == ()
    assert res.data.market_cap_usd == 5_000_000_000  # present fields intact


@pytest.mark.asyncio
async def test_as_of_falls_back_to_fetch_time_without_last_updated():
    payload = _coin_payload()
    payload["market_data"]["last_updated"] = None
    res = await _provider(_FakeHttp(payload)).fetch(_ident())
    assert res.status == "OK"
    assert res.data.last_updated_ms is None
    assert res.as_of_ms == NOW_MS


# ── failure mapping: 429 / 5xx / timeout / 404 / bad payload ───────────────

@pytest.mark.asyncio
async def test_429_is_unavailable_with_retries_and_retry_after():
    http = _FakeHttp(TransientUpstreamError(429, 7.0))
    sleep = _FakeSleep()
    res = await _provider(http, sleep=sleep).fetch(_ident())
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == cg.COINGECKO_RATE_LIMITED
    assert res.data is None
    assert res.stale is False
    assert len(http.calls) == 3  # first attempt + 2 retries
    assert sleep.delays == [7.0, 7.0]  # Retry-After honored, no real waiting


@pytest.mark.asyncio
async def test_500_is_error():
    http = _FakeHttp(TransientUpstreamError(500, None))
    res = await _provider(http).fetch(_ident())
    assert res.status == "ERROR"
    assert res.reason_code == cg.COINGECKO_UPSTREAM_ERROR
    assert res.data is None
    assert len(http.calls) == 3


@pytest.mark.asyncio
async def test_timeout_is_unavailable():
    http = _FakeHttp(asyncio.TimeoutError())
    res = await _provider(http).fetch(_ident())
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == cg.COINGECKO_TIMEOUT
    assert res.data is None
    assert len(http.calls) == 1  # timeouts are not retried (shared style)


@pytest.mark.asyncio
async def test_connection_error_is_unavailable():
    http = _FakeHttp(aiohttp.ClientError("connection refused"))
    res = await _provider(http).fetch(_ident())
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == cg.COINGECKO_NETWORK_ERROR
    assert res.data is None


@pytest.mark.asyncio
async def test_404_unknown_id_is_error_not_retried():
    http = _FakeHttp(CoinGeckoNotFound("unknown coingecko id"))
    res = await _provider(http).fetch(_ident("no-such-coin"))
    assert res.status == "ERROR"
    assert res.reason_code == cg.COINGECKO_UNKNOWN_ID
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_unparseable_payload_is_error():
    http = _FakeHttp({"id": "pepe"})  # market_data missing
    res = await _provider(http).fetch(_ident())
    assert res.status == "ERROR"
    assert res.reason_code == cg.COINGECKO_BAD_RESPONSE
    assert res.data is None


@pytest.mark.asyncio
async def test_parse_rejects_non_object_payload():
    with pytest.raises(CoinGeckoBadResponse):
        cg.parse_coin_document("pepe", [1, 2], request_url="u", fetched_at_ms=1)


# ── stale cache ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failure_serves_stale_cache_with_original_time():
    clock = _FakeClock()
    http = _FakeHttp(_coin_payload())
    provider = _provider(http, clock=clock)
    fresh = await provider.fetch(_ident())
    assert fresh.status == "OK"

    clock.now_ms += (cg.MARKET_TTL_SEC + 60) * 1000  # expire the market TTL
    http.behaviour = TransientUpstreamError(429, None)
    res = await provider.fetch(_ident())
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == cg.COINGECKO_RATE_LIMITED
    assert res.stale is True
    assert res.data == fresh.data
    assert res.fetched_at_ms == NOW_MS  # original time preserved for DQ aging


@pytest.mark.asyncio
async def test_error_serves_stale_cache():
    clock = _FakeClock()
    http = _FakeHttp(_coin_payload())
    provider = _provider(http, clock=clock)
    fresh = await provider.fetch(_ident())
    clock.now_ms += (cg.MARKET_TTL_SEC + 60) * 1000
    http.behaviour = TransientUpstreamError(500, None)
    res = await provider.fetch(_ident())
    assert res.status == "ERROR"
    assert res.reason_code == cg.COINGECKO_UPSTREAM_ERROR
    assert res.stale is True
    assert res.data == fresh.data


# ── TTL cache behaviour ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cache_hit_within_ttl_issues_no_http():
    clock = _FakeClock()
    http = _FakeHttp(_coin_payload())
    provider = _provider(http, clock=clock)
    first = await provider.fetch(_ident())
    clock.now_ms += 60 * 1000
    second = await provider.fetch(_ident())
    assert second.status == "OK"
    assert second.stale is False
    assert second.data == first.data
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_cache_expiry_triggers_refetch():
    clock = _FakeClock()
    http = _FakeHttp(_coin_payload())
    provider = _provider(http, clock=clock)
    await provider.fetch(_ident())
    clock.now_ms += (cg.MARKET_TTL_SEC + 1) * 1000
    res = await provider.fetch(_ident())
    assert res.status == "OK"
    assert len(http.calls) == 2


# ── key hygiene in errors/logs ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_message_redacts_key_material(caplog):
    secret = "TOPSECRET123"
    http = _FakeHttp(RuntimeError(f"api_key={secret} upstream blew up"))
    with caplog.at_level("INFO"):
        res = await _provider(http).fetch(_ident())
    assert res.status == "UNAVAILABLE"
    assert res.error_message is not None
    assert secret not in res.error_message
    assert secret not in caplog.text


# ── independent limiter ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coingecko_has_independent_limiter():
    provider = CoinGeckoProvider(fetcher=_FakeHttp(_coin_payload()))
    limiter = provider._active_limiter
    assert limiter.max_rate == cg.COINGECKO_MAX_PER_MIN
    assert limiter.time_period == cg.COINGECKO_PERIOD_SEC
    # Shared per-loop singleton across provider instances (not per-call).
    other = CoinGeckoProvider(fetcher=_FakeHttp(_coin_payload()))
    assert other._active_limiter is limiter


# ── registry + NullProvider ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_registry_resolves_registered_provider():
    provider = CoinGeckoProvider(fetcher=_FakeHttp(_coin_payload()))
    registry = ProviderRegistry()
    registry.register("coingecko", provider)
    assert registry.has("coingecko")
    assert registry.get("coingecko") is provider
    assert registry.names() == ("coingecko",)
    res = await registry.get("coingecko").fetch(_ident())
    assert res.status == "OK"


@pytest.mark.asyncio
async def test_registry_unknown_name_returns_null_provider():
    registry = ProviderRegistry()
    assert not registry.has("unlock")
    fallback = registry.get("unlock")
    assert isinstance(fallback, NullProvider)
    res = await fallback.fetch(_ident())
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == base_mod.PROVIDER_NOT_CONFIGURED
    assert res.data is None
    assert res.stale is False


@pytest.mark.asyncio
async def test_null_provider_needs_no_io_or_key():
    res = await NullProvider(clock=lambda: NOW_MS).fetch(_ident())
    assert res.status == "UNAVAILABLE"
    assert res.fetched_at_ms == NOW_MS


@pytest.mark.asyncio
async def test_registry_allows_task17_upgrade_from_null():
    registry = ProviderRegistry()
    assert isinstance(registry.get("social"), NullProvider)
    real = CoinGeckoProvider(fetcher=_FakeHttp(_coin_payload()))
    registry.register("social", real)  # future Task 17 provider takes over
    assert registry.get("social") is real
