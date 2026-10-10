"""Shared HTTP plumbing for Binance USDT-M public endpoints (R11a budget/HTTP).

All endpoints used here are PUBLIC market data — no API key, no auth, nothing to
sign. A single shared aiohttp session + a conservative rate limiter keep the
full-universe scan inside Binance's weight budget. Transient failures (429/5xx)
are retried with jittered exponential backoff, honoring ``Retry-After``.

Geo-restriction insurance: the host defaults to ``https://fapi.binance.com`` and
can be repointed (e.g. to a reachable mirror) via ``DIVE_FAPI_BASE`` — the single
place the base URL is defined. Only that explicit mirror origin shares the
fapi budget; any other host never inherits fapi rights (D19.3).

R11a (D11/D19.3/D19.4): budgeted sends resolve ``host + full path`` to a
versioned family (``endpoint-weights-v3``), validate limit buckets, deny
unknown host/path/limit as ``UNBUDGETED_ENDPOINT`` without transport, check
``deadline_ms`` before each transport, count one real transport per permit
(retry = new permit/ID, cache never reserves), and re-read the UTC month
immediately before transport for midnight rollover (old ID cancel + new
UUID/month reserve; in-flight completion stays on the original month).
"""

from __future__ import annotations

import asyncio
import os
import random
import time
import uuid as _uuid
from typing import Any, Awaitable, Callable, TypeVar

import aiohttp
from aiolimiter import AsyncLimiter

from diveintocrypto_desktop.shortlab.request_budget import (
    DEADLINE_EXCEEDED,
    ENDPOINT_WEIGHTS_VERSION,
    JOB_TYPE_UNKNOWN,
    UNBUDGETED_ENDPOINT,
    BudgetExhausted,
    Denied,
    RequestContext,
    UnbudgetedEndpointError,
    budget_class,
    endpoint_family_for_url,
    endpoint_weight,
    get_current_request_context,
    host_from_url,
    is_deadline_exceeded,
    utc_month_key,
)

__all__ = [
    "FAPI_BASE",
    "FAPI_V1",
    "FAPI_DATA",
    "TransientUpstreamError",
    "BudgetExhausted",
    "UnbudgetedEndpointError",
    "UNBUDGETED_ENDPOINT",
    "ENDPOINT_WEIGHTS_VERSION",
    "JOB_TYPE_UNKNOWN",
    "DEADLINE_EXCEEDED",
    "retry_delay",
    "run_with_retries",
    "get_json",
    "get_session",
    "close_session",
    "endpoint_family_for_url",
    "endpoint_weight",
    "resolve_family_or_raise",
    "check_deadline_or_raise",
    "month_key_for_ms",
]

# USDT-M futures REST roots (override the host via DIVE_FAPI_BASE if needed).
FAPI_BASE = os.environ.get("DIVE_FAPI_BASE", "https://fapi.binance.com").rstrip("/")
FAPI_V1 = f"{FAPI_BASE}/fapi/v1"
FAPI_DATA = FAPI_BASE  # /futures/data/* lives directly under the host

# Binance futures/data ("long-short", OI hist) endpoints are weight-limited far
# more tightly than klines; 40 requests / 60s is a safe shared ceiling.
_RATIO_LIMITERS: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}

# Retry policy for transient upstream failures (429 / 5xx).
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2  # retries AFTER the first attempt (3 attempts total)
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 8.0

T = TypeVar("T")


class LoopBoundLock:
    def __init__(self) -> None:
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if loop not in self._locks:
            self._locks[loop] = asyncio.Lock()
        return self._locks[loop]

    async def __aenter__(self) -> None:
        await self._get_lock().__aenter__()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self._get_lock().__aexit__(exc_type, exc_val, exc_tb)


class TransientUpstreamError(Exception):
    """A retryable upstream failure (429/5xx), carrying ``Retry-After`` seconds."""

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"upstream {status}")


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Backoff before retry ``attempt`` (0-based). Honors Retry-After when present."""
    if retry_after is not None and retry_after > 0:
        return min(float(retry_after), 60.0)
    jittered = _BACKOFF_BASE * (2**attempt) * (0.5 + random.random())
    return min(jittered, _BACKOFF_CAP)


async def run_with_retries(
    send: Callable[[], Awaitable[T]],
    *,
    should_retry: Callable[[Exception], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_retries: int = _MAX_RETRIES,
) -> T:
    """Run ``send()`` retrying transient failures with jittered exponential backoff.

    ``should_retry`` decides which exceptions are transient (default: only
    :class:`TransientUpstreamError`). The last exception propagates when retries
    are exhausted.
    """
    should_retry = should_retry or (lambda e: isinstance(e, TransientUpstreamError))
    sleep = sleep or asyncio.sleep  # late-bound so tests can intercept it
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await send()
        except Exception as e:  # noqa: BLE001 - classified by should_retry
            if not should_retry(e) or attempt >= max_retries:
                raise
            last = e
            await sleep(retry_delay(attempt, getattr(e, "retry_after", None)))
    raise last  # pragma: no cover - loop always returns or raises


def _retry_after_seconds(resp: aiohttp.ClientResponse) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None  # HTTP-date form: fall back to exponential backoff


def month_key_for_ms(now_ms: int) -> str:
    """UTC ``YYYY-MM`` for ``now_ms`` (D19.4, same as R01/request_budget)."""
    return utc_month_key(int(now_ms))


def resolve_family_or_raise(url: str, params: dict[str, Any] | None) -> tuple[str, int]:
    """Resolve ``(family, weight)`` for ``url/params`` or raise UNBUDGETED.

    R11a strict registry: host + full path must match; unknown host, path
    or limit bucket raises :class:`UnbudgetedEndpointError` without sending.
    Never falls back to an ambient ``endpoint_family``.
    """
    family = endpoint_family_for_url(url)
    if family is None:
        raise UnbudgetedEndpointError(
            f"{UNBUDGETED_ENDPOINT}: no family for url={url!r} "
            f"(weights {ENDPOINT_WEIGHTS_VERSION}); refusing to send"
        )
    weight = endpoint_weight(family, params)
    if weight is None:
        raise UnbudgetedEndpointError(
            f"{UNBUDGETED_ENDPOINT}: no weight fixture for family={family!r} "
            f"params={params!r} (weights {ENDPOINT_WEIGHTS_VERSION}); refusing to send"
        )
    return family, int(weight)


def check_deadline_or_raise(ctx: RequestContext | None, now_ms: int) -> None:
    """Raise ``BudgetExhausted(DEADLINE_EXCEEDED)`` when the deadline passed.

    Must be called immediately before each transport entry (after all
    budget/queue awaits); expired contexts never send.
    """
    if ctx is None or ctx.deadline_ms is None:
        return
    if is_deadline_exceeded(ctx.deadline_ms, now_ms):
        raise BudgetExhausted(
            f"{DEADLINE_EXCEEDED}: deadline_ms={ctx.deadline_ms} < now_ms={now_ms}; refusing to send",
            reason_code=DEADLINE_EXCEEDED,
            next_allowed_at_ms=None,
            job_type=ctx.job_type,
            endpoint_family=ctx.endpoint_family,
        )


async def _monthly_reserve(
    repository: Any,
    *,
    provider: str,
    month_key: str,
    request_id: str,
    monthly_limit: int,
    as_of_ms: int,
) -> Any:
    """Reserve via ``RepositoryPort.reserve_provider_request`` (D19.4)."""
    return await repository.reserve_provider_request(
        provider, month_key, request_id, monthly_limit, as_of_ms
    )


async def _monthly_finish(
    repository: Any, *, request_id: str, sent: bool, as_of_ms: int
) -> None:
    """Finish via ``RepositoryPort.finish_provider_request`` (D19.4)."""
    await repository.finish_provider_request(request_id, sent, as_of_ms)


async def handle_month_rollover(
    repository: Any,
    *,
    provider: str,
    old_request_id: str,
    old_month_key: str,
    new_month_key: str,
    monthly_limit: int,
    as_of_ms: int,
    uuid_fn: Callable[[], str] | None = None,
) -> tuple[str, Any]:
    """Cancel the old-month reservation and reserve in the new month (D19.4).

    Used when the UTC month changed between queueing and transport entry:
    the un-sent old ID is finished ``sent=false`` (only when it was still
    RESERVED; already-terminal states stay idempotent), then a fresh UUID is
    reserved in the new month. Returns ``(new_request_id, reserve_result)``;
    when the new reserve is not admitted the caller must cancel its host
    permit and never send.
    """
    try:
        await _monthly_finish(
            repository, request_id=old_request_id, sent=False, as_of_ms=as_of_ms
        )
    except Exception:
        # finish(false) on missing/terminal states raises ValidationError;
        # midnight cancel is best-effort (unknown IDs stay NOT_FOUND, inverse
        # terminal stays conflict) — the new-month reserve still decides.
        pass
    new_id = uuid_fn() if uuid_fn is not None else str(_uuid.uuid4())
    result = await _monthly_reserve(
        repository,
        provider=provider,
        month_key=new_month_key,
        request_id=new_id,
        monthly_limit=monthly_limit,
        as_of_ms=as_of_ms,
    )
    _ = old_month_key
    return new_id, result


async def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    rate_limited: bool = False,
    request_context: RequestContext | None = None,
    repository: Any | None = None,
    monthly_provider: str | None = None,
    monthly_limit: int | None = None,
    clock: Callable[[], int] | None = None,
    uuid_fn: Callable[[], str] | None = None,
) -> Any:
    """GET a JSON document with transient-failure retries.

    Set ``rate_limited`` for futures/data endpoints (shared 40/60s limiter).

    ``request_context`` is additive (F03): when it carries a
    :class:`RequestBudget`, every real attempt (first try, retry, pagination
    page) atomically reserves via ``try_acquire`` and is recorded by
    ``Permit.mark_sent``; cancellation before the send releases, an
    already-sent attempt is never refunded. Unknown host/path/limit
    (``endpoint-weights-v3``) raises :class:`UnbudgetedEndpointError`
    without sending; unknown ``job_type`` raises ``BudgetExhausted`` with
    ``JOB_TYPE_UNKNOWN``; past-``deadline_ms`` raises ``BudgetExhausted``
    with ``DEADLINE_EXCEEDED`` before transport. ``None`` preserves the
    legacy unbounded path (existing callers/tests).

    R11a monthly (D19.4, CoinGecko): pass ``repository`` +
    ``monthly_provider`` + ``monthly_limit`` to persist month counts via
    ``RepositoryPort``. Each attempt uses a fresh request ID (retry = new
    ID, cache never reserves here); the UTC month is re-read immediately
    before transport entry — a changed month cancels the old ID
    (``finish(false)``) and reserves a new UUID/month, re-checking deadline
    and quota; without a new permit the host permit is cancelled and nothing
    is sent. Once transport is entered the completion (or unknown result)
    is finished ``sent=true`` to the original reservation month even across
    a midnight return; callers never re-reserve.
    """

    ctx = request_context if request_context is not None else get_current_request_context()
    budget = ctx.budget if ctx is not None else None
    if budget is None:
        async def send() -> Any:
            session = await get_session()
            if rate_limited:
                loop = asyncio.get_running_loop()
                if loop not in _RATIO_LIMITERS:
                    _RATIO_LIMITERS[loop] = AsyncLimiter(max_rate=40, time_period=60)
                limiter = _RATIO_LIMITERS[loop]
                async with limiter:
                    return await _read_json(session, url, params)
            return await _read_json(session, url, params)

        return await run_with_retries(send)

    # -- budgeted path: unique send point, one retry loop (F03.1/F03.2) -------
    # R11a strict: family/weight come from URL only (no ambient fallback).
    family, weight = resolve_family_or_raise(url, params)
    host = host_from_url(url)
    job_type = ctx.job_type if ctx is not None else "entry"
    # Unknown job_type refuses before any transport (D19.3).
    if budget_class(job_type) == JOB_TYPE_UNKNOWN:
        raise BudgetExhausted(
            f"{JOB_TYPE_UNKNOWN}: job_type={job_type!r} is not a known tier; refusing to send",
            reason_code=JOB_TYPE_UNKNOWN,
            next_allowed_at_ms=None,
            job_type=job_type,
            endpoint_family=family,
        )

    def _now_ms() -> int:
        if clock is not None:
            try:
                return int(clock())
            except Exception:
                pass
        try:
            return int(budget.now_ms())
        except Exception:
            return int(time.time() * 1000)

    monthly_enabled = (
        repository is not None and monthly_provider is not None and monthly_limit is not None
    )

    last: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        # Pre-attempt deadline check (before any reservation).
        check_deadline_or_raise(ctx, _now_ms())
        monthly_request_id: str | None = None
        monthly_month: str | None = None
        if monthly_enabled:
            assert repository is not None and monthly_provider is not None and monthly_limit is not None
            now0 = _now_ms()
            monthly_month = month_key_for_ms(now0)
            monthly_request_id = uuid_fn() if uuid_fn is not None else str(_uuid.uuid4())
            # Retry uses a fresh ID (D19.4); cache never reserves (checked by caller).
            mres = await _monthly_reserve(
                repository,
                provider=str(monthly_provider),
                month_key=str(monthly_month),
                request_id=str(monthly_request_id),
                monthly_limit=int(monthly_limit),
                as_of_ms=int(now0),
            )
            if not bool(mres.get("admitted")):
                raise BudgetExhausted(
                    str(mres.get("reason_code") or "BUDGET_MONTHLY_EXHAUSTED"),
                    reason_code=str(mres.get("reason_code") or "BUDGET_MONTHLY_EXHAUSTED"),
                    next_allowed_at_ms=None,
                    job_type=job_type,
                    endpoint_family=family,
                )
        permit = budget.try_acquire(host, weight, job_type, family)
        if isinstance(permit, Denied) or permit is False:
            denied = permit if isinstance(permit, Denied) else Denied()
            if monthly_enabled and monthly_request_id is not None and monthly_month is not None:
                # Host denied before transport: cancel the monthly hold.
                try:
                    await _monthly_finish(
                        repository,
                        request_id=str(monthly_request_id),
                        sent=False,
                        as_of_ms=_now_ms(),
                    )
                except Exception:
                    pass
            raise BudgetExhausted(
                denied.message,
                reason_code=denied.reason_code,
                next_allowed_at_ms=denied.next_allowed_at_ms,
                job_type=denied.job_type or job_type,
                endpoint_family=denied.endpoint_family or family,
            )
        marked = False

        def _mark() -> None:
            nonlocal marked
            if not marked:
                permit.mark_sent()
                marked = True

        try:
            session = await get_session()
            # Host/RPM queue ends here; re-read clock immediately before
            # transport entry (D19.4 midnight + deadline re-check). No old-month
            # reservation may send after the wait.
            now_pre = _now_ms()
            check_deadline_or_raise(ctx, now_pre)
            if monthly_enabled and monthly_month is not None and monthly_request_id is not None:
                assert repository is not None and monthly_provider is not None and monthly_limit is not None
                fresh_month = month_key_for_ms(now_pre)
                if fresh_month != monthly_month:
                    # Midnight rollover for an un-sent hold: cancel old, reserve new.
                    new_id, new_res = await handle_month_rollover(
                        repository,
                        provider=str(monthly_provider),
                        old_request_id=str(monthly_request_id),
                        old_month_key=str(monthly_month),
                        new_month_key=str(fresh_month),
                        monthly_limit=int(monthly_limit),
                        as_of_ms=int(now_pre),
                        uuid_fn=uuid_fn,
                    )
                    if not bool(new_res.get("admitted")):
                        permit.release_unsent()
                        raise BudgetExhausted(
                            str(new_res.get("reason_code") or "BUDGET_MONTHLY_EXHAUSTED"),
                            reason_code=str(new_res.get("reason_code") or "BUDGET_MONTHLY_EXHAUSTED"),
                            next_allowed_at_ms=None,
                            job_type=job_type,
                            endpoint_family=family,
                        )
                    monthly_request_id = str(new_id)
                    monthly_month = str(fresh_month)
                    # Re-check deadline/quota under the new month already done;
                    # host permit is still ours (not yet dispatched).
            if rate_limited:
                loop = asyncio.get_running_loop()
                if loop not in _RATIO_LIMITERS:
                    _RATIO_LIMITERS[loop] = AsyncLimiter(max_rate=40, time_period=60)
                limiter = _RATIO_LIMITERS[loop]
                async with limiter:
                    # Any await above re-checks already; limiter wait ends here.
                    # Re-read once more before the actual transport entry.
                    check_deadline_or_raise(ctx, _now_ms())
                    try:
                        cm = session.get(url, params=params)
                    except asyncio.CancelledError:
                        permit.release_unsent()
                        if monthly_enabled and monthly_request_id is not None:
                            try:
                                await _monthly_finish(
                                    repository, request_id=str(monthly_request_id),
                                    sent=False, as_of_ms=_now_ms(),
                                )
                            except Exception:
                                pass
                        raise
                    except Exception:
                        # get() itself failed before any send: release.
                        permit.release_unsent()
                        if monthly_enabled and monthly_request_id is not None:
                            try:
                                await _monthly_finish(
                                    repository, request_id=str(monthly_request_id),
                                    sent=False, as_of_ms=_now_ms(),
                                )
                            except Exception:
                                pass
                        raise
                    try:
                        async with cm as resp:
                            _mark()
                            # Billing point is first transport entry: finish
                            # monthly to the reservation month even across a
                            # midnight return (D19.4); unknown results also bill.
                            if monthly_enabled and monthly_request_id is not None:
                                try:
                                    await _monthly_finish(
                                        repository, request_id=str(monthly_request_id),
                                        sent=True, as_of_ms=_now_ms(),
                                    )
                                except Exception:
                                    pass
                            if resp.status in _RETRYABLE_STATUSES:
                                raise TransientUpstreamError(
                                    resp.status, _retry_after_seconds(resp)
                                )
                            resp.raise_for_status()
                            return await resp.json()
                    except asyncio.CancelledError:
                        if not marked:
                            permit.release_unsent()
                            if monthly_enabled and monthly_request_id is not None:
                                try:
                                    await _monthly_finish(
                                        repository, request_id=str(monthly_request_id),
                                        sent=False, as_of_ms=_now_ms(),
                                    )
                                except Exception:
                                    pass
                        raise
                    except TransientUpstreamError:
                        if not marked:
                            _mark()
                            if monthly_enabled and monthly_request_id is not None:
                                try:
                                    await _monthly_finish(
                                        repository, request_id=str(monthly_request_id),
                                        sent=True, as_of_ms=_now_ms(),
                                    )
                                except Exception:
                                    pass
                        raise
                    except Exception:
                        if not marked:
                            _mark()
                            if monthly_enabled and monthly_request_id is not None:
                                try:
                                    await _monthly_finish(
                                        repository, request_id=str(monthly_request_id),
                                        sent=True, as_of_ms=_now_ms(),
                                    )
                                except Exception:
                                    pass
                        raise
            else:
                check_deadline_or_raise(ctx, _now_ms())
                try:
                    cm = session.get(url, params=params)
                except asyncio.CancelledError:
                    permit.release_unsent()
                    if monthly_enabled and monthly_request_id is not None:
                        try:
                            await _monthly_finish(
                                repository, request_id=str(monthly_request_id),
                                sent=False, as_of_ms=_now_ms(),
                            )
                        except Exception:
                            pass
                    raise
                except Exception:
                    permit.release_unsent()
                    if monthly_enabled and monthly_request_id is not None:
                        try:
                            await _monthly_finish(
                                repository, request_id=str(monthly_request_id),
                                sent=False, as_of_ms=_now_ms(),
                            )
                        except Exception:
                            pass
                    raise
                try:
                    async with cm as resp:
                        _mark()
                        if monthly_enabled and monthly_request_id is not None:
                            try:
                                await _monthly_finish(
                                    repository, request_id=str(monthly_request_id),
                                    sent=True, as_of_ms=_now_ms(),
                                )
                            except Exception:
                                pass
                        if resp.status in _RETRYABLE_STATUSES:
                            raise TransientUpstreamError(
                                resp.status, _retry_after_seconds(resp)
                            )
                        resp.raise_for_status()
                        return await resp.json()
                except asyncio.CancelledError:
                    if not marked:
                        permit.release_unsent()
                        if monthly_enabled and monthly_request_id is not None:
                            try:
                                await _monthly_finish(
                                    repository, request_id=str(monthly_request_id),
                                    sent=False, as_of_ms=_now_ms(),
                                )
                            except Exception:
                                pass
                    raise
                except TransientUpstreamError:
                    if not marked:
                        _mark()
                        if monthly_enabled and monthly_request_id is not None:
                            try:
                                await _monthly_finish(
                                    repository, request_id=str(monthly_request_id),
                                    sent=True, as_of_ms=_now_ms(),
                                )
                            except Exception:
                                pass
                    raise
                except Exception:
                    if not marked:
                        _mark()
                        if monthly_enabled and monthly_request_id is not None:
                            try:
                                await _monthly_finish(
                                    repository, request_id=str(monthly_request_id),
                                    sent=True, as_of_ms=_now_ms(),
                                )
                            except Exception:
                                pass
                    raise
        except BudgetExhausted:
            raise
        except UnbudgetedEndpointError:
            raise
        except asyncio.CancelledError:
            raise
        except TransientUpstreamError as exc:
            last = exc
            if attempt >= _MAX_RETRIES:
                raise
            await asyncio.sleep(retry_delay(attempt, getattr(exc, "retry_after", None)))
            continue
        except Exception:
            raise
    raise last  # pragma: no cover - loop always returns or raises


async def _read_json(session: aiohttp.ClientSession, url: str, params: dict[str, Any] | None) -> Any:
    async with session.get(url, params=params) as resp:
        if resp.status in _RETRYABLE_STATUSES:
            raise TransientUpstreamError(resp.status, _retry_after_seconds(resp))
        resp.raise_for_status()
        return await resp.json()


_session: aiohttp.ClientSession | None = None
_session_lock = LoopBoundLock()


async def get_session() -> aiohttp.ClientSession:
    """Return a lazily-created process-wide aiohttp session."""
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                timeout = aiohttp.ClientTimeout(total=30, sock_connect=10)
                connector = aiohttp.TCPConnector(limit=24, ttl_dns_cache=300)
                _session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _session


async def close_session() -> None:
    global _session
    async with _session_lock:
        if _session is not None and not _session.closed:
            await _session.close()
        _session = None
