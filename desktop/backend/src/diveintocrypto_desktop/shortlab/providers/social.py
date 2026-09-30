"""Social attention-decay provider (Task 17, design 5.4 / section 12).

Measures *attention decay*, never raw sentiment as an LTSS driver: the
provider returns two aligned 30-day windows (previous / recent) for social
volume, contributors and dominance, plus the aligned price and spot-volume
changes used for divergence checks. Scoring lives in
``features/narrative.py``; this module only fetches, caches and
point-in-time filters.

Status mapping mirrors :mod:`unlock`: ``NOT_APPLICABLE`` without a
verified ``social_provider_id`` (no HTTP), ``UNAVAILABLE`` on 429 /
timeout / connection errors (stale cache attached when available),
``ERROR`` on 404 / other 4xx / 5xx / unparseable payloads. Freshness TTL
defaults to 10800s (design 18.1 ``social``). The key travels as a header
only and never appears in ``request_url`` / logs / ``error_message``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from aiolimiter import AsyncLimiter

from diveintocrypto_desktop.shortlab.models import ProviderResult

log = logging.getLogger(__name__)

PROVIDER_NAME = "social"

SOCIAL_MAX_PER_MIN = 10
SOCIAL_PERIOD_SEC = 60

# Design 18.1: social ttl 3h / grace 12h.
SOCIAL_TTL_SEC = 10800

IDENTITY_NOT_MAPPED = "IDENTITY_NOT_MAPPED"
SOCIAL_RATE_LIMITED = "SOCIAL_RATE_LIMITED"
SOCIAL_UPSTREAM_ERROR = "SOCIAL_UPSTREAM_ERROR"
SOCIAL_UNKNOWN_ID = "SOCIAL_UNKNOWN_ID"
SOCIAL_CLIENT_ERROR = "SOCIAL_CLIENT_ERROR"
SOCIAL_TIMEOUT = "SOCIAL_TIMEOUT"
SOCIAL_NETWORK_ERROR = "SOCIAL_NETWORK_ERROR"
SOCIAL_BAD_RESPONSE = "SOCIAL_BAD_RESPONSE"

_Fetcher = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
_Clock = Callable[[], int]

_limiters: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}


def _limiter() -> AsyncLimiter:
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = AsyncLimiter(
            max_rate=SOCIAL_MAX_PER_MIN, time_period=SOCIAL_PERIOD_SEC
        )
        _limiters[loop] = limiter
    return limiter


class SocialNotFound(Exception):
    """The ``social_provider_id`` does not exist upstream (HTTP 404)."""


class SocialBadResponse(Exception):
    """Upstream 2xx payload is not a parseable social document."""


class _RetryableSocialError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class SocialSeries:
    """Two aligned 30-day attention windows for one canonical asset.

    ``None`` means unknown (never 0): missing windows contribute no
    narrative score and lower DQ instead of faking decay.
    """

    provider_id: str
    as_of_ms: int
    fetched_at_ms: int
    volume_prev_30d: float | None = None
    volume_30d: float | None = None
    contributors_prev_30d: float | None = None
    contributors_30d: float | None = None
    dominance_prev_30d: float | None = None
    dominance_30d: float | None = None
    price_change_30d: float | None = None
    spot_volume_change_30d: float | None = None
    request_url: str = ""


@dataclass
class _CacheEntry:
    data: SocialSeries
    fetched_at_ms: int


def _provider_id_of(identity: Any) -> str | None:
    value = getattr(identity, "social_provider_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _finite_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    import math as _math

    if not _math.isfinite(result) or result < 0:
        return None
    return result


def _signed_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    import math as _math

    if not _math.isfinite(result):
        return None
    return result


class SocialProvider:
    """Vendor-neutral social attention provider (fake-HTTP injectable)."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.lunarcrush.example",
        social_ttl_sec: int = SOCIAL_TTL_SEC,
        clock: _Clock | None = None,
        fetcher: _Fetcher | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = str(base_url).rstrip("/")
        self._ttl_ms = max(1, int(social_ttl_sec)) * 1000
        self._clock: _Clock = clock or (lambda: time.time_ns() // 1_000_000)
        self._fetcher = fetcher
        self._cache: dict[str, _CacheEntry] = {}

    def _request_url(self, provider_id: str) -> str:
        return f"{self._base_url}/v1/social/{urllib.parse.quote(provider_id)}/decay"

    async def fetch(
        self, identity: Any, as_of_ms: int | None = None
    ) -> ProviderResult[SocialSeries]:
        provider_id = _provider_id_of(identity)
        now_ms = int(self._clock())
        if provider_id is None:
            return ProviderResult(
                status="NOT_APPLICABLE",
                source=self.name,
                fetched_at_ms=now_ms,
                as_of_ms=None,
                data=None,
                stale=False,
                reason_code=IDENTITY_NOT_MAPPED,
                error_message=None,
            )
        cutoff = now_ms if as_of_ms is None else int(as_of_ms)
        cached = self._cache.get(provider_id)
        if (
            cached is not None
            and now_ms - cached.fetched_at_ms <= self._ttl_ms
            and cached.data.as_of_ms <= cutoff
        ):
            return ProviderResult(
                status="OK",
                source=self.name,
                fetched_at_ms=cached.fetched_at_ms,
                as_of_ms=cached.data.as_of_ms,
                data=cached.data,
                stale=False,
                reason_code=None,
                error_message=None,
            )
        url = self._request_url(provider_id)
        headers: dict[str, Any] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with _limiter():
                payload = await self._fetch_payload(url, headers)
        except SocialNotFound as exc:
            return self._failure(
                provider_id, now_ms, cutoff, url, "ERROR",
                SOCIAL_UNKNOWN_ID, type(exc).__name__, cached,
            )
        except _RetryableSocialError as exc:
            code = (
                SOCIAL_RATE_LIMITED
                if exc.kind == "rate_limited"
                else SOCIAL_TIMEOUT
                if exc.kind == "timeout"
                else SOCIAL_NETWORK_ERROR
            )
            return self._failure(
                provider_id, now_ms, cutoff, url, "UNAVAILABLE",
                code, type(exc).__name__, cached,
            )
        except SocialBadResponse as exc:
            return self._failure(
                provider_id, now_ms, cutoff, url, "ERROR",
                SOCIAL_BAD_RESPONSE, type(exc).__name__, cached,
            )
        except Exception as exc:  # noqa: BLE001 - providers never raise
            return self._failure(
                provider_id, now_ms, cutoff, url, "UNAVAILABLE",
                SOCIAL_UPSTREAM_ERROR, type(exc).__name__, cached,
            )
        try:
            series = self._parse_payload(
                payload, provider_id=provider_id, now_ms=now_ms, cutoff=cutoff
            )
        except SocialBadResponse as exc:
            return self._failure(
                provider_id, now_ms, cutoff, url, "ERROR",
                SOCIAL_BAD_RESPONSE, type(exc).__name__, cached,
            )
        self._cache[provider_id] = _CacheEntry(data=series, fetched_at_ms=now_ms)
        return ProviderResult(
            status="OK",
            source=self.name,
            fetched_at_ms=now_ms,
            as_of_ms=series.as_of_ms,
            data=series,
            stale=False,
            reason_code=None,
            error_message=None,
        )

    async def _fetch_payload(self, url: str, headers: Mapping[str, Any]) -> Any:
        if self._fetcher is not None:
            return await self._fetcher(url, headers)
        raise _RetryableSocialError("network", "SocialProvider has no HTTP fetcher wired")

    def _parse_payload(
        self, payload: Any, *, provider_id: str, now_ms: int, cutoff: int
    ) -> SocialSeries:
        if not isinstance(payload, Mapping):
            raise SocialBadResponse(f"unexpected payload shape {type(payload).__name__}")
        as_of_ms = payload.get("as_of_ms")
        try:
            as_of = int(as_of_ms) if as_of_ms is not None else cutoff  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise SocialBadResponse("as_of_ms is not an int") from None
        if as_of > cutoff:
            # A snapshot from the future must never feed a past decision.
            raise SocialBadResponse("social snapshot as_of_ms is after the cutoff")
        windows = payload.get("windows")
        if not isinstance(windows, Mapping):
            raise SocialBadResponse("windows is not a mapping")
        prev = windows.get("prev_30d")
        recent = windows.get("recent_30d")
        if not isinstance(prev, Mapping) or not isinstance(recent, Mapping):
            raise SocialBadResponse("30d windows are not mappings")
        return SocialSeries(
            provider_id=provider_id,
            as_of_ms=as_of,
            fetched_at_ms=now_ms,
            volume_prev_30d=_finite_or_none(prev.get("volume")),
            volume_30d=_finite_or_none(recent.get("volume")),
            contributors_prev_30d=_finite_or_none(prev.get("contributors")),
            contributors_30d=_finite_or_none(recent.get("contributors")),
            dominance_prev_30d=_finite_or_none(prev.get("dominance")),
            dominance_30d=_finite_or_none(recent.get("dominance")),
            price_change_30d=_signed_or_none(payload.get("price_change_30d")),
            spot_volume_change_30d=_signed_or_none(payload.get("spot_volume_change_30d")),
            request_url=self._request_url(provider_id),
        )

    def _failure(
        self,
        provider_id: str,
        now_ms: int,
        cutoff: int,
        url: str,
        status: str,
        reason_code: str,
        error_name: str,
        cached: _CacheEntry | None,
    ) -> ProviderResult[SocialSeries]:
        if cached is not None and cached.data.as_of_ms <= cutoff:
            return ProviderResult(
                status=status,  # type: ignore[arg-type]
                source=self.name,
                fetched_at_ms=cached.fetched_at_ms,
                as_of_ms=cached.data.as_of_ms,
                data=cached.data,
                stale=True,
                reason_code=reason_code,
                error_message=error_name,
            )
        return ProviderResult(
            status=status,  # type: ignore[arg-type]
            source=self.name,
            fetched_at_ms=now_ms,
            as_of_ms=None,
            data=None,
            stale=False,
            reason_code=reason_code,
            error_message=error_name,
        )
