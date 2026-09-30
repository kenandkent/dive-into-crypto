"""Structured catalyst-event provider (Task 17, design 5.5).

A catalyst feed only produces *structured events* -- whether they pause or
block execution is decided solely by the Task 11 risk engine
(``PAUSE_MAJOR_CATALYST`` on a MAJOR severity inside its window). A future
LLM may act as an event extractor upstream; it must never output LTSS.

Status mapping mirrors :mod:`unlock`: ``NOT_APPLICABLE`` without a
verifiable asset id (``canonical_id`` / ``coingecko_id`` -- no HTTP),
``UNAVAILABLE`` on 429 / timeout / connection errors (stale cache attached
when available), ``ERROR`` on 404 / other 4xx / 5xx / unparseable payloads.
Freshness TTL defaults to 1800s (design 18.1 ``catalyst``). Events are
deduped by ``event_id`` (latest ``known_at_ms`` wins) and filtered to
``known_at_ms <= as_of_ms`` so replay never sees the future.
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

PROVIDER_NAME = "catalyst"

CATALYST_MAX_PER_MIN = 20
CATALYST_PERIOD_SEC = 60

# Design 18.1: catalyst ttl 30min / grace 120min (event-type data).
CATALYST_TTL_SEC = 1800

IDENTITY_NOT_MAPPED = "IDENTITY_NOT_MAPPED"
CATALYST_RATE_LIMITED = "CATALYST_RATE_LIMITED"
CATALYST_UPSTREAM_ERROR = "CATALYST_UPSTREAM_ERROR"
CATALYST_UNKNOWN_ID = "CATALYST_UNKNOWN_ID"
CATALYST_CLIENT_ERROR = "CATALYST_CLIENT_ERROR"
CATALYST_TIMEOUT = "CATALYST_TIMEOUT"
CATALYST_NETWORK_ERROR = "CATALYST_NETWORK_ERROR"
CATALYST_BAD_RESPONSE = "CATALYST_BAD_RESPONSE"

ALLOWED_TYPES = frozenset(
    {
        "CEX_LISTING",
        "DELISTING",
        "MAINNET",
        "BURN",
        "BUYBACK",
        "AIRDROP",
        "PARTNERSHIP",
        "PRODUCT",
        "OTHER",
    }
)

ALLOWED_SEVERITIES = ("INFO", "MATERIAL", "MAJOR")

_Fetcher = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
_Clock = Callable[[], int]

_limiters: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}


def _limiter() -> AsyncLimiter:
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = AsyncLimiter(
            max_rate=CATALYST_MAX_PER_MIN, time_period=CATALYST_PERIOD_SEC
        )
        _limiters[loop] = limiter
    return limiter


class CatalystNotFound(Exception):
    """The asset id does not exist upstream (HTTP 404)."""


class CatalystBadResponse(Exception):
    """Upstream 2xx payload is not a parseable catalyst document."""


class _RetryableCatalystError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class CatalystEvent:
    """One structured catalyst event (design 5.5 contract)."""

    event_id: str
    canonical_id: str
    announced_at_ms: int
    known_at_ms: int
    event_type: str = "OTHER"
    severity: str = "INFO"
    confidence: float = 0.0
    effective_at_ms: int | None = None
    source_url: str | None = None
    title: str = ""


@dataclass(frozen=True)
class CatalystFeed:
    """Point-in-time catalyst feed for one canonical asset."""

    asset_id: str
    events: tuple[CatalystEvent, ...] = ()
    fetched_at_ms: int = 0
    request_url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events or ()))

    def major_in_window(self, as_of_ms: int, window_ms: int) -> CatalystEvent | None:
        """Latest MAJOR event announced within ``window_ms`` before ``as_of``."""
        best: CatalystEvent | None = None
        for event in self.events:
            if event.severity != "MAJOR":
                continue
            if not (int(as_of_ms) - int(window_ms) <= event.announced_at_ms <= int(as_of_ms)):
                continue
            if best is None or event.announced_at_ms > best.announced_at_ms:
                best = event
        return best


@dataclass
class _CacheEntry:
    data: CatalystFeed
    fetched_at_ms: int


def _asset_id_of(identity: Any) -> str | None:
    for name in ("canonical_id", "coingecko_id"):
        value = getattr(identity, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_event(
    raw: Mapping[str, Any], *, canonical_id: str, fetched_at_ms: int
) -> CatalystEvent | None:
    try:
        event_id = str(raw.get("event_id") or raw.get("id") or "")
        announced_at_ms = int(raw.get("announced_at_ms") or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not event_id or announced_at_ms <= 0:
        return None
    event_type = str(raw.get("event_type") or raw.get("type") or "OTHER").upper()
    if event_type not in ALLOWED_TYPES:
        event_type = "OTHER"
    severity = str(raw.get("severity") or "INFO").upper()
    if severity not in ALLOWED_SEVERITIES:
        severity = "INFO"
    try:
        confidence = float(raw.get("confidence", 0.0))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        confidence = 0.0
    import math as _math

    if not _math.isfinite(confidence):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    effective = raw.get("effective_at_ms")
    try:
        effective_ms = None if effective is None else int(effective)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        effective_ms = None
    try:
        known_at_ms = int(raw.get("known_at_ms") or fetched_at_ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        known_at_ms = fetched_at_ms
    source_url = raw.get("source_url")
    return CatalystEvent(
        event_id=event_id,
        canonical_id=canonical_id,
        announced_at_ms=announced_at_ms,
        known_at_ms=known_at_ms,
        event_type=event_type,
        severity=severity,
        confidence=confidence,
        effective_at_ms=effective_ms,
        source_url=str(source_url) if isinstance(source_url, str) else None,
        title=str(raw.get("title") or ""),
    )


def dedup_events(
    events: list[CatalystEvent], *, as_of_ms: int | None = None
) -> tuple[CatalystEvent, ...]:
    """Dedup by ``event_id`` (latest ``known_at_ms`` wins) and apply the
    point-in-time cutoff (``known_at_ms > as_of_ms`` excluded)."""
    best: dict[str, CatalystEvent] = {}
    for event in events:
        if as_of_ms is not None and event.known_at_ms > int(as_of_ms):
            continue
        current = best.get(event.event_id)
        if current is None or event.known_at_ms >= current.known_at_ms:
            best[event.event_id] = event
    return tuple(sorted(best.values(), key=lambda e: (e.announced_at_ms, e.event_id)))


class CatalystProvider:
    """Structured catalyst-event provider (fake-HTTP injectable)."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.catalyst.example",
        catalyst_ttl_sec: int = CATALYST_TTL_SEC,
        clock: _Clock | None = None,
        fetcher: _Fetcher | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = str(base_url).rstrip("/")
        self._ttl_ms = max(1, int(catalyst_ttl_sec)) * 1000
        self._clock: _Clock = clock or (lambda: time.time_ns() // 1_000_000)
        self._fetcher = fetcher
        self._cache: dict[str, _CacheEntry] = {}

    def _request_url(self, asset_id: str) -> str:
        return f"{self._base_url}/v1/catalysts/{urllib.parse.quote(asset_id)}"

    async def fetch(
        self, identity: Any, as_of_ms: int | None = None
    ) -> ProviderResult[CatalystFeed]:
        asset_id = _asset_id_of(identity)
        now_ms = int(self._clock())
        if asset_id is None:
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
        cached = self._cache.get(asset_id)
        if cached is not None and now_ms - cached.fetched_at_ms <= self._ttl_ms:
            events = dedup_events(list(cached.data.events), as_of_ms=as_of_ms)
            return ProviderResult(
                status="OK",
                source=self.name,
                fetched_at_ms=cached.fetched_at_ms,
                as_of_ms=as_of_ms if as_of_ms is not None else cached.data.fetched_at_ms,
                data=CatalystFeed(
                    asset_id=asset_id,
                    events=events,
                    fetched_at_ms=cached.fetched_at_ms,
                    request_url=cached.data.request_url,
                ),
                stale=False,
                reason_code=None,
                error_message=None,
            )
        url = self._request_url(asset_id)
        headers: dict[str, Any] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with _limiter():
                payload = await self._fetch_payload(url, headers)
        except CatalystNotFound as exc:
            return self._failure(
                asset_id, now_ms, as_of_ms, url, "ERROR",
                CATALYST_UNKNOWN_ID, type(exc).__name__, cached,
            )
        except _RetryableCatalystError as exc:
            code = (
                CATALYST_RATE_LIMITED
                if exc.kind == "rate_limited"
                else CATALYST_TIMEOUT
                if exc.kind == "timeout"
                else CATALYST_NETWORK_ERROR
            )
            return self._failure(
                asset_id, now_ms, as_of_ms, url, "UNAVAILABLE",
                code, type(exc).__name__, cached,
            )
        except CatalystBadResponse as exc:
            return self._failure(
                asset_id, now_ms, as_of_ms, url, "ERROR",
                CATALYST_BAD_RESPONSE, type(exc).__name__, cached,
            )
        except Exception as exc:  # noqa: BLE001 - providers never raise
            return self._failure(
                asset_id, now_ms, as_of_ms, url, "UNAVAILABLE",
                CATALYST_UPSTREAM_ERROR, type(exc).__name__, cached,
            )
        try:
            events = self._parse_payload(
                payload, asset_id=asset_id, fetched_at_ms=now_ms
            )
        except CatalystBadResponse as exc:
            return self._failure(
                asset_id, now_ms, as_of_ms, url, "ERROR",
                CATALYST_BAD_RESPONSE, type(exc).__name__, cached,
            )
        feed = CatalystFeed(
            asset_id=asset_id,
            events=events,
            fetched_at_ms=now_ms,
            request_url=url,
        )
        self._cache[asset_id] = _CacheEntry(data=feed, fetched_at_ms=now_ms)
        return ProviderResult(
            status="OK",
            source=self.name,
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms if as_of_ms is not None else now_ms,
            data=CatalystFeed(
                asset_id=asset_id,
                events=dedup_events(list(events), as_of_ms=as_of_ms),
                fetched_at_ms=now_ms,
                request_url=url,
            ),
            stale=False,
            reason_code=None,
            error_message=None,
        )

    async def _fetch_payload(self, url: str, headers: Mapping[str, Any]) -> Any:
        if self._fetcher is not None:
            return await self._fetcher(url, headers)
        raise _RetryableCatalystError(
            "network", "CatalystProvider has no HTTP fetcher wired"
        )

    def _parse_payload(
        self, payload: Any, *, asset_id: str, fetched_at_ms: int
    ) -> tuple[CatalystEvent, ...]:
        if isinstance(payload, Mapping) and "events" in payload:
            raw_events = payload.get("events")
        elif isinstance(payload, (list, tuple)):
            raw_events = payload
        else:
            raise CatalystBadResponse(
                f"unexpected payload shape {type(payload).__name__}"
            )
        if not isinstance(raw_events, (list, tuple)):
            raise CatalystBadResponse("events is not a list")
        events: list[CatalystEvent] = []
        for raw in raw_events:
            if not isinstance(raw, Mapping):
                raise CatalystBadResponse("event entry is not a mapping")
            event = _parse_event(
                raw, canonical_id=asset_id, fetched_at_ms=fetched_at_ms
            )
            if event is not None:
                events.append(event)
        return tuple(events)

    def _failure(
        self,
        asset_id: str,
        now_ms: int,
        as_of_ms: int | None,
        url: str,
        status: str,
        reason_code: str,
        error_name: str,
        cached: _CacheEntry | None,
    ) -> ProviderResult[CatalystFeed]:
        if cached is not None:
            events = dedup_events(list(cached.data.events), as_of_ms=as_of_ms)
            return ProviderResult(
                status=status,  # type: ignore[arg-type]
                source=self.name,
                fetched_at_ms=cached.fetched_at_ms,
                as_of_ms=as_of_ms,
                data=CatalystFeed(
                    asset_id=asset_id,
                    events=events,
                    fetched_at_ms=cached.fetched_at_ms,
                    request_url=url,
                ),
                stale=True,
                reason_code=reason_code,
                error_message=error_name,
            )
        return ProviderResult(
            status=status,  # type: ignore[arg-type]
            source=self.name,
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms,
            data=None,
            stale=False,
            reason_code=reason_code,
            error_message=error_name,
        )
