"""Token-unlock provider (Task 17, design 5.3 / 11.2).

Business code depends only on the :class:`UnlockProvider` protocol surface
(``fetch`` returning the frozen :class:`ProviderResult`); any vendor
(Tokenomist first) sits behind the injectable ``fetcher``. No API key is
used when the identity carries no verified ``unlock_provider_id`` -- that
path returns ``NOT_APPLICABLE`` + ``IDENTITY_NOT_MAPPED`` and performs no
HTTP, so symbol/name guessing is impossible.

Status mapping:
  OK              parsed schedule (empty event list is a genuine zero)
  NOT_APPLICABLE  identity carries no verified ``unlock_provider_id``
  UNAVAILABLE     429 / timeout / connection error; carries the stale cache
                  with ``stale=True`` when one exists
  ERROR           404 unknown id / other 4xx / 5xx / unparseable payload;
                  stale cache attached when available

Plumbing: per-event-loop ``AsyncLimiter`` (independent from the Binance /
CoinGecko limiters), TTL cache (``unlock_ttl_sec``, default 43200s per
design 18.1), dedup by ``event_id`` (latest ``known_at_ms`` wins) and a
point-in-time cutoff -- events with ``known_at_ms`` later than the
requested ``as_of_ms`` are excluded from ``data`` (the repository enforces
the same rule on reads).

Key hygiene: the key travels only as an ``Authorization`` header, never in
``request_url`` / logs / ``error_message`` (the latter is additionally
sanitized by the ``ProviderResult`` constructor).
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

PROVIDER_NAME = "unlock"

# Independent unlock budget; deliberately separate from the Binance 80/5min
# and CoinGecko limiters. Unlock schedules change slowly (design 23: 6-12h).
UNLOCK_MAX_PER_MIN = 10
UNLOCK_PERIOD_SEC = 60

# Design 18.1: unlock ttl 12h / grace 36h.
UNLOCK_TTL_SEC = 43200

IDENTITY_NOT_MAPPED = "IDENTITY_NOT_MAPPED"
UNLOCK_RATE_LIMITED = "UNLOCK_RATE_LIMITED"
UNLOCK_UPSTREAM_ERROR = "UNLOCK_UPSTREAM_ERROR"
UNLOCK_UNKNOWN_ID = "UNLOCK_UNKNOWN_ID"
UNLOCK_CLIENT_ERROR = "UNLOCK_CLIENT_ERROR"
UNLOCK_TIMEOUT = "UNLOCK_TIMEOUT"
UNLOCK_NETWORK_ERROR = "UNLOCK_NETWORK_ERROR"
UNLOCK_BAD_RESPONSE = "UNLOCK_BAD_RESPONSE"

ALLOWED_ALLOCATIONS = frozenset(
    {
        "TEAM",
        "SEED",
        "PRIVATE",
        "ADVISOR",
        "ECOSYSTEM",
        "TREASURY",
        "COMMUNITY",
        "STAKING",
        "OTHER",
    }
)

_Fetcher = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
_Clock = Callable[[], int]

_limiters: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}


def _limiter() -> AsyncLimiter:
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = AsyncLimiter(
            max_rate=UNLOCK_MAX_PER_MIN, time_period=UNLOCK_PERIOD_SEC
        )
        _limiters[loop] = limiter
    return limiter


class UnlockNotFound(Exception):
    """The ``unlock_provider_id`` does not exist upstream (HTTP 404)."""


class UnlockBadResponse(Exception):
    """Upstream 2xx payload is not a parseable unlock document."""


@dataclass(frozen=True)
class UnlockEvent:
    """One vesting release (design 5.3 contract)."""

    event_id: str
    canonical_id: str
    unlock_at_ms: int
    known_at_ms: int
    amount_tokens: float
    allocation_type: str = "OTHER"
    percent_of_current_float: float | None = None
    source: str = PROVIDER_NAME


@dataclass(frozen=True)
class UnlockSchedule:
    """Point-in-time unlock schedule for one canonical asset."""

    provider_id: str
    events: tuple[UnlockEvent, ...] = ()
    fetched_at_ms: int = 0
    request_url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events or ()))


@dataclass
class _CacheEntry:
    data: UnlockSchedule
    fetched_at_ms: int


def _provider_id_of(identity: Any) -> str | None:
    value = getattr(identity, "unlock_provider_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _canonical_of(identity: Any) -> str:
    for name in ("canonical_id", "coingecko_id"):
        value = getattr(identity, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _parse_event(
    raw: Mapping[str, Any], *, provider_id: str, canonical_id: str, fetched_at_ms: int
) -> UnlockEvent | None:
    try:
        event_id = str(raw.get("event_id") or raw.get("id") or "")
        unlock_at_ms = int(raw.get("unlock_at_ms") or raw.get("ts_ms") or 0)  # type: ignore[arg-type]
        amount = float(raw.get("amount_tokens"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not event_id or unlock_at_ms <= 0 or not amount > 0:
        return None
    allocation = str(raw.get("allocation_type") or "OTHER").upper()
    if allocation not in ALLOWED_ALLOCATIONS:
        allocation = "OTHER"
    try:
        known_at_ms = int(raw.get("known_at_ms") or fetched_at_ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        known_at_ms = fetched_at_ms
    percent = raw.get("percent_of_current_float")
    try:
        percent_f = None if percent is None else float(percent)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        percent_f = None
    return UnlockEvent(
        event_id=event_id,
        canonical_id=canonical_id,
        unlock_at_ms=unlock_at_ms,
        known_at_ms=known_at_ms,
        amount_tokens=amount,
        allocation_type=allocation,
        percent_of_current_float=percent_f,
        source=str(raw.get("source") or PROVIDER_NAME),
    )


def dedup_events(
    events: list[UnlockEvent], *, as_of_ms: int | None = None
) -> tuple[UnlockEvent, ...]:
    """Dedup by ``event_id`` (latest ``known_at_ms`` wins) and apply the
    point-in-time cutoff (``known_at_ms > as_of_ms`` excluded)."""
    best: dict[str, UnlockEvent] = {}
    for event in events:
        if as_of_ms is not None and event.known_at_ms > int(as_of_ms):
            continue
        current = best.get(event.event_id)
        if current is None or event.known_at_ms >= current.known_at_ms:
            best[event.event_id] = event
    return tuple(sorted(best.values(), key=lambda e: (e.unlock_at_ms, e.event_id)))


class UnlockProvider:
    """Vendor-neutral unlock schedule provider (fake-HTTP injectable)."""

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.tokenomist.example",
        unlock_ttl_sec: int = UNLOCK_TTL_SEC,
        clock: _Clock | None = None,
        fetcher: _Fetcher | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = str(base_url).rstrip("/")
        self._ttl_ms = max(1, int(unlock_ttl_sec)) * 1000
        self._clock: _Clock = clock or (lambda: time.time_ns() // 1_000_000)
        self._fetcher = fetcher
        self._cache: dict[str, _CacheEntry] = {}

    def _request_url(self, provider_id: str) -> str:
        # The key is never embedded in the URL (header transport only).
        return (
            f"{self._base_url}/v1/unlocks/{urllib.parse.quote(provider_id)}"
        )

    async def fetch(
        self, identity: Any, as_of_ms: int | None = None
    ) -> ProviderResult[UnlockSchedule]:
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
        cached = self._cache.get(provider_id)
        if cached is not None and now_ms - cached.fetched_at_ms <= self._ttl_ms:
            events = dedup_events(
                list(cached.data.events),
                as_of_ms=as_of_ms,
            )
            return ProviderResult(
                status="OK",
                source=self.name,
                fetched_at_ms=cached.fetched_at_ms,
                as_of_ms=as_of_ms if as_of_ms is not None else cached.data.fetched_at_ms,
                data=UnlockSchedule(
                    provider_id=provider_id,
                    events=events,
                    fetched_at_ms=cached.fetched_at_ms,
                    request_url=cached.data.request_url,
                ),
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
        except UnlockNotFound as exc:
            return self._failure(
                provider_id, now_ms, as_of_ms, url, "ERROR",
                UNLOCK_UNKNOWN_ID, type(exc).__name__, cached,
            )
        except _RetryableUnlockError as exc:
            code = (
                UNLOCK_RATE_LIMITED
                if exc.kind == "rate_limited"
                else UNLOCK_TIMEOUT
                if exc.kind == "timeout"
                else UNLOCK_NETWORK_ERROR
            )
            return self._failure(
                provider_id, now_ms, as_of_ms, url, "UNAVAILABLE",
                code, type(exc).__name__, cached,
            )
        except UnlockBadResponse as exc:
            return self._failure(
                provider_id, now_ms, as_of_ms, url, "ERROR",
                UNLOCK_BAD_RESPONSE, type(exc).__name__, cached,
            )
        except Exception as exc:  # noqa: BLE001 - providers never raise
            return self._failure(
                provider_id, now_ms, as_of_ms, url, "UNAVAILABLE",
                UNLOCK_UPSTREAM_ERROR, type(exc).__name__, cached,
            )
        try:
            events = self._parse_payload(
                payload, provider_id=provider_id, fetched_at_ms=now_ms
            )
        except UnlockBadResponse as exc:
            return self._failure(
                provider_id, now_ms, as_of_ms, url, "ERROR",
                UNLOCK_BAD_RESPONSE, type(exc).__name__, cached,
            )
        schedule = UnlockSchedule(
            provider_id=provider_id,
            events=events,
            fetched_at_ms=now_ms,
            request_url=url,
        )
        self._cache[provider_id] = _CacheEntry(data=schedule, fetched_at_ms=now_ms)
        return ProviderResult(
            status="OK",
            source=self.name,
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms if as_of_ms is not None else now_ms,
            data=UnlockSchedule(
                provider_id=provider_id,
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
        raise _RetryableUnlockError("network", "UnlockProvider has no HTTP fetcher wired")

    def _parse_payload(
        self, payload: Any, *, provider_id: str, fetched_at_ms: int
    ) -> tuple[UnlockEvent, ...]:
        if isinstance(payload, Mapping) and "events" in payload:
            raw_events = payload.get("events")
        elif isinstance(payload, (list, tuple)):
            raw_events = payload
        else:
            raise UnlockBadResponse(f"unexpected payload shape {type(payload).__name__}")
        if not isinstance(raw_events, (list, tuple)):
            raise UnlockBadResponse("events is not a list")
        canonical_id = _canonical_of_fetcher_payload(payload, provider_id)
        events: list[UnlockEvent] = []
        for raw in raw_events:
            if not isinstance(raw, Mapping):
                raise UnlockBadResponse("event entry is not a mapping")
            event = _parse_event(
                raw,
                provider_id=provider_id,
                canonical_id=canonical_id,
                fetched_at_ms=fetched_at_ms,
            )
            if event is not None:
                events.append(event)
        return tuple(events)

    def _failure(
        self,
        provider_id: str,
        now_ms: int,
        as_of_ms: int | None,
        url: str,
        status: str,
        reason_code: str,
        error_name: str,
        cached: _CacheEntry | None,
    ) -> ProviderResult[UnlockSchedule]:
        if cached is not None:
            events = dedup_events(list(cached.data.events), as_of_ms=as_of_ms)
            return ProviderResult(
                status=status,  # type: ignore[arg-type]
                source=self.name,
                fetched_at_ms=cached.fetched_at_ms,
                as_of_ms=as_of_ms,
                data=UnlockSchedule(
                    provider_id=provider_id,
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


class _RetryableUnlockError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def _canonical_of_fetcher_payload(payload: Any, provider_id: str) -> str:
    if isinstance(payload, Mapping):
        for key in ("canonical_id", "asset_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return provider_id
