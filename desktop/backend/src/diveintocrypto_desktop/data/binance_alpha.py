"""H02 Binance Alpha public market-data adapter (design B11; plan H02.2).

Fixed host ``https://www.binance.com`` only. Public paths (verbatim)::

    /bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list
    /bapi/defi/v1/public/alpha-trade/get-exchange-info
    /bapi/defi/v1/public/alpha-trade/ticker
    /bapi/defi/v1/public/alpha-trade/fullDepth
    /bapi/defi/v1/public/alpha-trade/klines

Contract (B11.2.1, literal implementation):

- HTTP 200 still checks the business envelope: ``code == "000000"`` and the
  ``data`` shape; when a ``success`` field exists it must be ``true``.
  Schema notes: real fixtures rule -- this module first implements the plan
  literal (string ``"000000"``). If a captured fixture shows an integer
  ``0`` or a missing ``code`` with ``success: true``, the fixture wins and
  the adjustment is noted in the venue test (see
  ``test_shortlab_binance_alpha.py``). Current implementation accepts string
  ``"000000"`` only; integer codes are treated as business failures so a
  shape drift fails loud instead of silently passing.
- Source time prefers ``depth.E``/``depth.T`` (millis); missing source time
  only records ``VERIFIED_INGEST`` provenance downstream, never fabricates
  the current time.
- ``451``/``403`` map to ``VENUE_REGION_UNAVAILABLE``; ``429`` maps to
  ``RATE_LIMITED`` (``Retry-After`` preserved in the error); never ``N/A``,
  never a region bypass.
- Independent conservative limiter: 5 sends / 10 s, concurrency 1 for this
  host. Binance FAPI weight budget is never borrowed.
- ``tokenList``/``exchangeInfo`` cache 30 minutes; ``ticker``/``depth``
  freshness follows B31 (``alpha_quote`` 20 s / grace 60 s for ticker,
  ``spot_depth`` 60 s / grace 180 s for depth).
- Only versioned raw fixtures plus target-deployment live evidence together
  may set ``providers.binance_alpha.enabled=true``. This module never flips
  the flag; :func:`is_alpha_enabled` only reads it.

All network goes through injectable ``http_get`` so the suite stays offline:
``http_get(url, params) -> payload | raises``. The default real sender uses
the shared aiohttp session with the Alpha limiter and raises
:class:`AlphaHttpError` subclasses carrying ``status``/``retry_after``.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Awaitable, Callable

__all__ = [
    "ALPHA_HOST",
    "PATH_TOKEN_LIST",
    "PATH_EXCHANGE_INFO",
    "PATH_TICKER",
    "PATH_FULL_DEPTH",
    "PATH_KLINES",
    "VENUE_REGION_UNAVAILABLE",
    "ALPHA_RATE_LIMITED",
    "ALPHA_BUSINESS_ERROR",
    "ALPHA_DISABLED",
    "ALPHA_NO_MARKET",
    "TOKEN_LIST_TTL_SEC",
    "EXCHANGE_INFO_TTL_SEC",
    "AlphaError",
    "AlphaBusinessError",
    "AlphaRegionError",
    "AlphaRateLimited",
    "AlphaHttpError",
    "AlphaLimiter",
    "check_alpha_envelope",
    "is_alpha_enabled",
    "fetch_token_list",
    "fetch_exchange_info",
    "fetch_ticker",
    "fetch_full_depth",
    "reset_alpha_cache",
    "reset_alpha_limiter",
]

ALPHA_HOST = "https://www.binance.com"

PATH_TOKEN_LIST = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
PATH_EXCHANGE_INFO = "/bapi/defi/v1/public/alpha-trade/get-exchange-info"
PATH_TICKER = "/bapi/defi/v1/public/alpha-trade/ticker"
PATH_FULL_DEPTH = "/bapi/defi/v1/public/alpha-trade/fullDepth"
PATH_KLINES = "/bapi/defi/v1/public/alpha-trade/klines"

VENUE_REGION_UNAVAILABLE = "VENUE_REGION_UNAVAILABLE"
ALPHA_RATE_LIMITED = "RATE_LIMITED"
ALPHA_BUSINESS_ERROR = "ALPHA_BUSINESS_ERROR"
ALPHA_DISABLED = "VENUE_DISABLED"
ALPHA_NO_MARKET = "no_alpha_market"

TOKEN_LIST_TTL_SEC = 1800.0
EXCHANGE_INFO_TTL_SEC = 1800.0

#: Conservative host budget (B11.2.1): 5 sends per 10 s, concurrency 1.
ALPHA_MAX_SENDS = 5
ALPHA_WINDOW_SEC = 10.0


class AlphaError(Exception):
    """Base Alpha transport/business failure."""

    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class AlphaHttpError(AlphaError):
    """Non-2xx HTTP status from the Alpha host."""


class AlphaRegionError(AlphaHttpError):
    """451/403 region block (never N/A, never bypassed)."""


class AlphaRateLimited(AlphaHttpError):
    """429 rate limit (Retry-After preserved)."""


class AlphaBusinessError(AlphaError):
    """HTTP 200 with a failing business envelope or bad data shape."""


class AlphaLimiter:
    """Independent 5/10 s + concurrency-1 limiter for the Alpha host."""

    def __init__(self, *, max_sends: int = ALPHA_MAX_SENDS,
                 window_sec: float = ALPHA_WINDOW_SEC) -> None:
        self.max_sends = int(max_sends)
        self.window_sec = float(window_sec)
        self._sends: deque[float] = deque()
        self._sem = asyncio.Semaphore(1)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._sends and self._sends[0] <= cutoff:
            self._sends.popleft()

    async def acquire(self) -> None:
        async with self._sem:
            now = time.monotonic()
            self._prune(now)
            if len(self._sends) >= self.max_sends:
                oldest = self._sends[0]
                delay = max(0.0, (oldest + self.window_sec) - now)
                if delay > 0:
                    await asyncio.sleep(delay)
                    now = time.monotonic()
                    self._prune(now)
            self._sends.append(time.monotonic())

    def reset(self) -> None:
        self._sends.clear()

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        self._prune(now)
        return {"sends_in_window": len(self._sends), "max_sends": self.max_sends,
                "window_sec": self.window_sec}


_limiter = AlphaLimiter()


def reset_alpha_limiter() -> None:
    """Drop Alpha limiter state (test hook)."""
    _limiter.reset()


# -- envelope ---------------------------------------------------------------


def check_alpha_envelope(payload: Any) -> Any:
    """Validate the Alpha business envelope, returning ``data``.

    Plan literal: ``payload`` must be a mapping with ``code == "000000"``;
    when ``success`` is present it must be ``True``; ``data`` must exist.
    Anything else raises :class:`AlphaBusinessError` (HTTP-200 business
    failure -- honest, never treated as success).

    Fixture rule: if a captured raw fixture shows a different code spelling
    (e.g. integer ``0``), this function must be updated to the fixture and
    the deviation noted in the Alpha test. The current suite fixtures use
    string ``"000000"`` per the plan literal.
    """
    if not isinstance(payload, dict):
        raise AlphaBusinessError(f"alpha envelope must be a mapping, got {type(payload).__name__}")
    code = payload.get("code")
    if code != "000000":
        raise AlphaBusinessError(f"alpha business code != 000000: {code!r}")
    if "success" in payload and payload.get("success") is not True:
        raise AlphaBusinessError(f"alpha success != true: {payload.get('success')!r}")
    if "data" not in payload:
        raise AlphaBusinessError("alpha envelope missing data")
    return payload["data"]


def _classify_http_status(status: int, retry_after: float | None = None,
                          body: Any = None) -> AlphaError:
    message = f"alpha http {status}"
    if body is not None:
        message += f": {str(body)[:120]}"
    if status in (451, 403):
        return AlphaRegionError(message, status=status, retry_after=retry_after)
    if status == 429:
        return AlphaRateLimited(message, status=status, retry_after=retry_after)
    return AlphaHttpError(message, status=status, retry_after=retry_after)


def _retry_after_from_headers(headers: Any) -> float | None:
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def _default_http_get(url: str, params: dict[str, Any] | None) -> Any:
    """Real Alpha sender (host limiter + status mapping, no FAPI budget)."""
    from diveintocrypto_desktop.data.http import get_session

    await _limiter.acquire()
    session = await get_session()
    async with session.get(url, params=params) as resp:
        retry_after = _retry_after_from_headers(resp.headers)
        if resp.status != 200:
            try:
                body = await resp.json()
            except Exception:
                body = None
            raise _classify_http_status(resp.status, retry_after, body)
        return await resp.json()


HttpGet = Callable[[str, dict[str, Any] | None], Awaitable[Any]]


async def _alpha_get(
    path: str,
    params: dict[str, Any] | None,
    *,
    http_get: Any | None = None,
) -> Any:
    """GET one Alpha document, mapping HTTP failures to :class:`AlphaError`.

    ``http_get`` is ``async (url, params) -> json`` (fake-injectable). Fake
    HTTP errors must carry ``status`` (and optional ``retry_after``) like the
    real sender: ``aiohttp.ClientResponseError`` or any exception with a
    ``status`` attribute is classified; anything else propagates.
    """
    url = f"{ALPHA_HOST}{path}"
    params = dict(params or {})
    if http_get is not None:
        try:
            return await http_get(url, params)
        except AlphaError:
            raise
        except Exception as exc:  # noqa: BLE001 -- classify fake transport errors
            status = getattr(exc, "status", None)
            retry_after = getattr(exc, "retry_after", None)
            if status is not None:
                try:
                    status_int = int(status)
                except (TypeError, ValueError):
                    raise
                raise _classify_http_status(status_int, retry_after, str(exc)[:120]) from exc
            raise
    return await _default_http_get(url, params)


# -- raw caches (tokenList / exchangeInfo, 30 min) ---------------------------

_token_cache: dict[str, tuple[float, Any]] = {}
_exchange_cache: dict[str, tuple[float, Any]] = {}


def reset_alpha_cache() -> None:
    """Drop Alpha tokenList/exchangeInfo caches (test hook)."""
    _token_cache.clear()
    _exchange_cache.clear()


def _cache_get(store: dict, key: str, ttl: float) -> Any | None:
    row = store.get(key)
    if row is None:
        return None
    fetched_mono, payload = row
    if time.monotonic() - fetched_mono >= ttl:
        store.pop(key, None)
        return None
    return payload


def _cache_put(store: dict, key: str, payload: Any) -> None:
    store[key] = (time.monotonic(), payload)


# -- public fetchers (raw, envelope-checked) ----------------------------------


async def fetch_token_list(*, http_get: Any | None = None) -> list[dict[str, Any]]:
    """Token list / identity metadata (cached 30 min)."""
    cached = _cache_get(_token_cache, "token_list", TOKEN_LIST_TTL_SEC)
    if cached is not None:
        return cached
    payload = await _alpha_get(PATH_TOKEN_LIST, {}, http_get=http_get)
    data = check_alpha_envelope(payload)
    if not isinstance(data, list):
        # Some hosts wrap the list as {"tokens": [...]}; accept verbatim.
        if isinstance(data, dict) and isinstance(data.get("tokens"), list):
            data = data["tokens"]
        else:
            raise AlphaBusinessError("alpha token list data must be a list")
    _cache_put(_token_cache, "token_list", data)
    return data


async def fetch_exchange_info(*, http_get: Any | None = None) -> dict[str, Any]:
    """Exchange info / trading rules (cached 30 min)."""
    cached = _cache_get(_exchange_cache, "exchange_info", EXCHANGE_INFO_TTL_SEC)
    if cached is not None:
        return cached
    payload = await _alpha_get(PATH_EXCHANGE_INFO, {}, http_get=http_get)
    data = check_alpha_envelope(payload)
    if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
        raise AlphaBusinessError("alpha exchangeInfo data must hold symbols list")
    _cache_put(_exchange_cache, "exchange_info", data)
    return data


async def fetch_ticker(symbol: str, *, http_get: Any | None = None) -> dict[str, Any]:
    """Current price / 24h stats for one legal Alpha instrument ID."""
    if not symbol or not isinstance(symbol, str):
        raise AlphaBusinessError("alpha ticker requires a non-empty symbol")
    payload = await _alpha_get(PATH_TICKER, {"symbol": symbol}, http_get=http_get)
    data = check_alpha_envelope(payload)
    if not isinstance(data, dict):
        raise AlphaBusinessError("alpha ticker data must be a mapping")
    return data


async def fetch_full_depth(
    symbol: str,
    limit: int = 100,
    *,
    http_get: Any | None = None,
) -> dict[str, Any]:
    """Depth / fullDepth for one instrument (limit 100/500/1000)."""
    if not symbol or not isinstance(symbol, str):
        raise AlphaBusinessError("alpha fullDepth requires a non-empty symbol")
    if int(limit) not in (100, 500, 1000):
        raise AlphaBusinessError(f"alpha fullDepth limit must be 100/500/1000, got {limit!r}")
    payload = await _alpha_get(
        PATH_FULL_DEPTH, {"symbol": symbol, "limit": int(limit)}, http_get=http_get)
    data = check_alpha_envelope(payload)
    if not isinstance(data, dict) or not isinstance(data.get("bids"), list) \
            or not isinstance(data.get("asks"), list):
        raise AlphaBusinessError("alpha fullDepth data must hold bids/asks lists")
    return data


def is_alpha_enabled(config: Any) -> bool:
    """True only when ``providers.binance_alpha.enabled`` is explicitly true.

    Fixture + live evidence are both required before operators flip the
    flag (H02.3); this helper never flips it. Missing config sections mean
    disabled. No network is sent here.
    """
    try:
        hedge = getattr(config, "hedge", None)
        providers = getattr(hedge, "providers", None) if hedge is not None else None
        if providers is None and isinstance(hedge, dict):
            providers = hedge.get("providers")
        if providers is None:
            return False
        if isinstance(providers, dict):
            entry = providers.get("binance_alpha")
        else:
            entry = getattr(providers, "binance_alpha", None)
            if entry is None and hasattr(providers, "get"):
                try:
                    entry = providers.get("binance_alpha")  # type: ignore[union-attr]
                except Exception:
                    entry = None
        if entry is None:
            return False
        if isinstance(entry, dict):
            return bool(entry.get("enabled"))
        return bool(getattr(entry, "enabled", False))
    except Exception:
        return False
