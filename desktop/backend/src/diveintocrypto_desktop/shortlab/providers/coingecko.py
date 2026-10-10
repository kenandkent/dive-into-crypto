"""CoinGecko fundamentals provider (Task 9, design 5.2).

Fetches market cap, FDV, circulating/total/max supply, ATH price, ATH date
and categories for one canonical asset. The request key is ALWAYS
``identity.coingecko_id`` -- a missing id yields ``NOT_APPLICABLE`` with
``IDENTITY_NOT_MAPPED`` and performs no HTTP; symbol/name guessing is
forbidden (design 4.2 rule 5).

Endpoint: ``GET {base}/api/v3/coins/{id}`` (single atomic document carrying
both market fields and supply, so one response refreshes both the 60-minute
market cache and the 6-hour supply view per design 23 -- no separate supply
request is ever issued).

Status mapping:
  OK            parsed successfully (absent fields stay ``None``, never 0)
  NOT_APPLICABLE  identity carries no verified ``coingecko_id``
  UNAVAILABLE   429 / timeout / connection error (after retries for 429);
                carries stale cache with ``stale=True`` when one exists
  ERROR         404 unknown id / other 4xx / 5xx (after retries) /
                unparseable payload; stale cache attached when available

Shared plumbing (``data.http``): process-wide aiohttp session,
``run_with_retries`` with jittered exponential backoff honoring
``Retry-After``, and ``TransientUpstreamError`` classification. CoinGecko
gets its own independent per-loop rate limiter
(``COINGECKO_MAX_PER_MIN`` / minute), separate from the Binance limiters.

Key hygiene: V1 uses the keyless public endpoint -- no API key is read,
sent, logged, or embedded in ``request_url`` / ``error_message`` (the
latter is additionally passed through ``models.sanitize_error_message`` by
the ``ProviderResult`` constructor).

Snapshot / PIT: ``Fundamentals`` carries ``request_url``, ``fetched_at_ms``
and ``as_of_ms`` (CoinGecko ``last_updated`` when present, else fetch
time), so Task 10 can persist point-in-time snapshots; stale serves keep
the ORIGINAL ``fetched_at_ms`` so DQ aging stays truthful.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import aiohttp
from aiolimiter import AsyncLimiter

from diveintocrypto_desktop.data.http import (
    TransientUpstreamError,
    check_deadline_or_raise,
    get_session,
    handle_month_rollover,
    resolve_family_or_raise,
    run_with_retries,
)
from diveintocrypto_desktop.shortlab.models import ProviderResult
from diveintocrypto_desktop.shortlab.request_budget import (
    JOB_TYPE_UNKNOWN,
    BudgetExhausted,
    Denied,
    UnbudgetedEndpointError,
    budget_class,
    get_current_request_context,
    host_from_url,
)

log = logging.getLogger(__name__)

PROVIDER_NAME = "coingecko"
COINGECKO_BASE_URL = "https://api.coingecko.com"

# R11a budget (D11/D19.4): monthly + local RPM + reserve. Defaults mirror
# shortlab/default.yaml optimization.providers.coingecko.
ACCOUNT_MONTHLY_LIMIT = 10000
LOCAL_REQUESTS_PER_MINUTE = 30
COINGECKO_RESERVE_FRACTION = 0.1
BUDGET_LIMITED = "BUDGET_LIMITED"
#: FX observations are valid for 60s; never guess USDT=1 when stale (D11).
FX_MAX_AGE_MS = 60_000

# Independent CoinGecko budget (R11a: 30/min local, separate from Binance
# 80/5min and futures/data limiters; platform IP quota stays honest unknown).
COINGECKO_MAX_PER_MIN = 30
COINGECKO_PERIOD_SEC = 60

# Design 23 / quality_freshness_sec.fundamentals + supply_float override.
MARKET_TTL_SEC = 3600
SUPPLY_TTL_SEC = 21600
SUPPLY_GRACE_SEC = 43200

# Retryable upstream statuses, mirroring the shared ``data.http`` policy.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

IDENTITY_NOT_MAPPED = "IDENTITY_NOT_MAPPED"
COINGECKO_RATE_LIMITED = "COINGECKO_RATE_LIMITED"
COINGECKO_UPSTREAM_ERROR = "COINGECKO_UPSTREAM_ERROR"
COINGECKO_UNKNOWN_ID = "COINGECKO_UNKNOWN_ID"
COINGECKO_CLIENT_ERROR = "COINGECKO_CLIENT_ERROR"
COINGECKO_TIMEOUT = "COINGECKO_TIMEOUT"
COINGECKO_NETWORK_ERROR = "COINGECKO_NETWORK_ERROR"
COINGECKO_BAD_RESPONSE = "COINGECKO_BAD_RESPONSE"

_Fetcher = Callable[[str, Mapping[str, Any]], Awaitable[Any]]
_Clock = Callable[[], int]
_Sleeper = Callable[[float], Awaitable[None]]

_limiters: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}


def _limiter() -> AsyncLimiter:
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = AsyncLimiter(
            max_rate=COINGECKO_MAX_PER_MIN, time_period=COINGECKO_PERIOD_SEC
        )
        _limiters[loop] = limiter
    return limiter


class CoinGeckoNotFound(Exception):
    """The ``coingecko_id`` does not exist upstream (HTTP 404). Not retried."""


class CoinGeckoBadResponse(Exception):
    """Upstream 2xx payload is not a parseable coin document. Not retried."""


class CoinGeckoUnconfigured(Exception):
    """No API key / bad plan: the network directory is unavailable (UNCONFIGURED)."""


class _MonthlyExhausted(Exception):
    """Monthly quota exhausted (maps to BUDGET_LIMITED UNAVAILABLE)."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


# ---------------------------------------------------------------------------
# F04: identity-directory routes and parsing (design A6.3).
#
# The ``/coins/list`` directory is fetched with ``include_platform=false``
# (id/symbol/name only); platform/address enrichment for at most 50 bound
# assets goes through per-coin documents. DEMO and PRO each pin a host AND
# its key parameter together -- :func:`coingecko_route` returns both from
# the single ``api_plan`` so the same key can never be sent to the other
# host. Keys travel as query parameters because the shared ``data.http``
# layer carries no header support (F03-frozen); every stored/logged URL is
# passed through :func:`redact_coins_list_url` first.
# ---------------------------------------------------------------------------

#: Strict host + key-parameter pairs selected by ``api_plan``.
COINGECKO_DEMO_BASE_URL = "https://api.coingecko.com"
COINGECKO_PRO_BASE_URL = "https://pro-api.coingecko.com"
COINGECKO_DEMO_KEY_PARAM = "x_cg_demo_api_key"
COINGECKO_PRO_KEY_PARAM = "x_cg_pro_api_key"

#: Expanded-response ceiling for the directory payload (design A6.3).
COINGECKO_LIST_MAX_BYTES = 32 * 1024 * 1024

#: Directory freshness (design A7.3 / default.yaml identity_profile).
COINGECKO_DIRECTORY_TTL_SEC = 86400
COINGECKO_DIRECTORY_GRACE_SEC = 259200

#: Max bound assets enriched with platform/address per refresh (A6.3).
COINGECKO_MAX_PLATFORM_DETAIL = 50

IDENTITY_DIRECTORY_UNCONFIGURED = "UNCONFIGURED"


@dataclass(frozen=True)
class CoinDirectoryEntry:
    """One validated ``/coins/list`` row (id/symbol/name only)."""

    coin_id: str
    symbol: str
    name: str | None = None


def coingecko_route(api_plan: str | None, api_key: str | None) -> tuple[str, str]:
    """Return ``(base_url, key_param)`` for ``api_plan`` (``"demo"``/``"pro"``).

    Raises :class:`CoinGeckoUnconfigured` (UNCONFIGURED, key-free message)
    when the key is missing/blank, and ``ValueError`` for an unknown plan --
    both before any HTTP is attempted.
    """
    plan = str(api_plan or "demo").strip().lower()
    key = api_key.strip() if isinstance(api_key, str) else ""
    if plan == "demo":
        route = (COINGECKO_DEMO_BASE_URL, COINGECKO_DEMO_KEY_PARAM)
    elif plan == "pro":
        route = (COINGECKO_PRO_BASE_URL, COINGECKO_PRO_KEY_PARAM)
    else:
        raise ValueError(f"unknown coingecko api_plan: {api_plan!r}")
    if not key:
        raise CoinGeckoUnconfigured(
            f"{IDENTITY_DIRECTORY_UNCONFIGURED}: no CoinGecko API key for "
            f"plan={plan!r}; only the local verified set is available"
        )
    return route


def build_coins_list_request(
    base_url: str,
    api_key: str,
    api_plan: str | None,
    *,
    include_platform: bool = False,
) -> tuple[str, dict[str, str]]:
    """Build the ``(url, params)`` pair for the directory list.

    Pure function (no I/O): the key parameter name always matches the host
    implied by ``api_plan`` (strict pairing enforced by
    :func:`coingecko_route`).
    """
    route_base, key_param = coingecko_route(api_plan, api_key)
    base = (base_url or route_base).rstrip("/")
    if base != route_base:
        # A custom base must still pair with the plan's host family; refuse
        # to send a demo key at the pro host and vice versa.
        raise ValueError(
            f"coingecko base_url {base_url!r} does not match api_plan={api_plan!r}"
        )
    url = f"{base}/api/v3/coins/list"
    params = {
        "include_platform": "true" if include_platform else "false",
        key_param: api_key,
    }
    return url, params


def build_coin_detail_request(
    base_url: str, api_key: str, api_plan: str | None, coin_id: str
) -> tuple[str, dict[str, str]]:
    """Build the ``(url, params)`` pair for one per-coin platform document."""
    route_base, key_param = coingecko_route(api_plan, api_key)
    base = (base_url or route_base).rstrip("/")
    if base != route_base:
        raise ValueError(
            f"coingecko base_url {base_url!r} does not match api_plan={api_plan!r}"
        )
    slug = urllib.parse.quote(str(coin_id).strip(), safe="")
    return f"{base}/api/v3/coins/{slug}", {
        "localization": "false",
        "tickers": "false",
        "market_data": "false",
        "community_data": "false",
        "developer_data": "false",
        "sparkline": "false",
        key_param: api_key,
    }


def redact_coins_list_url(url: str, params: Mapping[str, Any] | None = None) -> str:
    """Render a log-safe URL with key-parameter values replaced by ``***``."""
    text = str(url)
    for param in (COINGECKO_DEMO_KEY_PARAM, COINGECKO_PRO_KEY_PARAM):
        text = re.sub(rf"([?&]{param}=)[^&\s]*", r"\1***", text)
    if params:
        query = urllib.parse.urlencode(
            sorted(
                (k, ("***" if k in (COINGECKO_DEMO_KEY_PARAM, COINGECKO_PRO_KEY_PARAM) else v))
                for k, v in params.items()
            )
        )
        text = f"{text}?{query}" if query else text
    return text


def coins_list_size_bytes(payload: Any) -> int:
    """Expanded size of a directory payload (canonical JSON, UTF-8)."""
    try:
        return len(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    except (TypeError, ValueError):
        return COINGECKO_LIST_MAX_BYTES + 1


def parse_coins_list(
    payload: Any, *, max_bytes: int = COINGECKO_LIST_MAX_BYTES
) -> tuple[CoinDirectoryEntry, ...]:
    """Validate a ``/coins/list`` payload into directory entries.

    Raises :class:`CoinGeckoBadResponse` when the payload is not a list of
    ``{id, symbol, name}`` objects or exceeds ``max_bytes`` -- the caller
    keeps the previous cache on any such failure (atomic replace only after
    this check passes).
    """
    if coins_list_size_bytes(payload) > max_bytes:
        raise CoinGeckoBadResponse(
            f"coins/list payload exceeds {max_bytes} bytes; refusing to cache"
        )
    if not isinstance(payload, list):
        raise CoinGeckoBadResponse("coins/list payload must be a list")
    entries: list[CoinDirectoryEntry] = []
    for row in payload:
        if not isinstance(row, Mapping):
            raise CoinGeckoBadResponse("coins/list row must be an object")
        coin_id = row.get("id")
        symbol = row.get("symbol")
        name = row.get("name")
        if not isinstance(coin_id, str) or not coin_id.strip():
            raise CoinGeckoBadResponse("coins/list row misses string id")
        if not isinstance(symbol, str) or not symbol.strip():
            raise CoinGeckoBadResponse("coins/list row misses string symbol")
        if name is not None and not isinstance(name, str):
            raise CoinGeckoBadResponse("coins/list row has non-string name")
        entries.append(
            CoinDirectoryEntry(coin_id=coin_id.strip(), symbol=symbol.strip(), name=name)
        )
    return tuple(entries)


def parse_coin_platforms(payload: Any) -> dict[str, str]:
    """Extract ``{platform: address}`` from one per-coin document.

    Non-empty string addresses only; unknown/missing ``platforms`` yields
    ``{}`` (the coin simply carries no chain binding yet).
    """
    if not isinstance(payload, Mapping):
        return {}
    platforms = payload.get("platforms")
    if not isinstance(platforms, Mapping):
        return {}
    out: dict[str, str] = {}
    for platform, address in platforms.items():
        if (
            isinstance(platform, str)
            and platform.strip()
            and isinstance(address, str)
            and address.strip()
        ):
            out[platform.strip().lower()] = address.strip()
    return out


@dataclass(frozen=True)
class Fundamentals:
    """Point-in-time fundamental snapshot for one canonical asset.

    Ratios follow the repo-wide decimal convention (``ath_change=-0.5``
    means -50 %); amounts are USD notional. Every numeric field is ``None``
    when upstream omits it -- ``None`` is never coerced to 0.
    """

    coingecko_id: str
    symbol: str | None = None  # upstream echo, informational only
    name: str | None = None  # upstream echo, informational only
    price_usd: float | None = None
    market_cap_usd: float | None = None
    fdv_usd: float | None = None
    circulating_supply: float | None = None
    total_supply: float | None = None
    max_supply: float | None = None
    ath_usd: float | None = None
    ath_change: float | None = None  # decimal fraction, CoinGecko pct / 100
    ath_date_ms: int | None = None
    categories: tuple[str, ...] = ()
    last_updated_ms: int | None = None  # upstream ``last_updated``, else None
    request_url: str = ""  # sanitized request URL; never carries a key
    fetched_at_ms: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "categories",
            tuple(c for c in (self.categories or ()) if isinstance(c, str)),
        )


@dataclass
class _CacheEntry:
    data: Fundamentals
    fetched_at_ms: int
    as_of_ms: int | None


def build_coin_request(
    coingecko_id: str, base_url: str = COINGECKO_BASE_URL
) -> tuple[str, dict[str, str]]:
    """Build the ``(url, params)`` pair for one coin document.

    Pure function (no I/O) so tests can assert the request shape -- in
    particular that the path carries the verified ``coingecko_id`` and that
    no key material appears anywhere.
    """
    slug = urllib.parse.quote(str(coingecko_id).strip(), safe="")
    url = f"{base_url.rstrip('/')}/api/v3/coins/{slug}"
    params = {
        "localization": "false",
        "tickers": "false",
        "market_data": "true",
        "community_data": "false",
        "developer_data": "false",
        "sparkline": "false",
    }
    return url, params


def _request_url_for_snapshot(url: str, params: Mapping[str, Any]) -> str:
    query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params.items()))
    return f"{url}?{query}" if query else url


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result == result else None  # drop NaN, keep the None
    if isinstance(value, str):
        try:
            result = float(value.strip().replace(",", ""))
        except ValueError:
            return None
        return result if result == result else None
    return None


def _as_usd(value: Any) -> float | None:
    if isinstance(value, Mapping):
        return _as_float(value.get("usd"))
    return None


def _as_time_ms(value: Any) -> int | None:
    """Parse CoinGecko timestamps (ISO-8601 strings or epoch numbers)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Mapping):  # e.g. {"usd": "<iso>"}
        return _as_time_ms(value.get("usd"))
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number <= 0:
            return None
        # Heuristic: >= 1e12 is already ms, smaller values are seconds.
        return int(number) if number >= 1e12 else int(number * 1000)
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00") if value.strip() else ""
        if not text:
            return None
        try:
            parsed = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp() * 1000)
    return None


def parse_coin_document(
    coingecko_id: str,
    payload: Any,
    *,
    request_url: str,
    fetched_at_ms: int,
) -> Fundamentals:
    """Parse one ``/coins/{id}`` document into :class:`Fundamentals`.

    Raises :class:`CoinGeckoBadResponse` when the payload is not a coin
    document (missing/non-object ``market_data``). Individual absent fields
    become ``None``.
    """
    if not isinstance(payload, Mapping):
        raise CoinGeckoBadResponse(f"expected object payload for id={coingecko_id}")
    market = payload.get("market_data")
    if not isinstance(market, Mapping):
        raise CoinGeckoBadResponse(f"missing market_data for id={coingecko_id}")

    ath_change_pct = _as_usd(market.get("ath_change_percentage"))
    last_updated_ms = _as_time_ms(market.get("last_updated"))
    categories = payload.get("categories") or ()

    return Fundamentals(
        coingecko_id=coingecko_id,
        symbol=payload.get("symbol") if isinstance(payload.get("symbol"), str) else None,
        name=payload.get("name") if isinstance(payload.get("name"), str) else None,
        price_usd=_as_usd(market.get("current_price")),
        market_cap_usd=_as_usd(market.get("market_cap")),
        fdv_usd=_as_usd(market.get("fully_diluted_valuation")),
        circulating_supply=_as_float(market.get("circulating_supply")),
        total_supply=_as_float(market.get("total_supply")),
        max_supply=_as_float(market.get("max_supply")),
        ath_usd=_as_usd(market.get("ath")),
        ath_change=(ath_change_pct / 100.0) if ath_change_pct is not None else None,
        ath_date_ms=_as_time_ms(market.get("ath_date")),
        categories=tuple(categories) if isinstance(categories, (list, tuple)) else (),
        last_updated_ms=last_updated_ms,
        request_url=request_url,
        fetched_at_ms=fetched_at_ms,
    )


def _retry_after_seconds(resp: aiohttp.ClientResponse) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# R11a budget helpers (D11/D19.4): monthly effective limit, UTC month key,
# FX freshness. Totals never reset on restart (persistent BUDGET_COUNTER via
# R01); lowering the configured limit never clears history.
# ---------------------------------------------------------------------------


def coingecko_effective_limit(
    account_monthly_limit: int = ACCOUNT_MONTHLY_LIMIT,
    reserve_fraction: float = COINGECKO_RESERVE_FRACTION,
) -> int:
    """Effective monthly sends: ``floor(limit * (1 - reserve))`` (D11)."""
    import math as _math

    limit = int(account_monthly_limit)
    reserve = float(reserve_fraction)
    if limit <= 0:
        raise ValueError(f"account_monthly_limit must be > 0, got {account_monthly_limit!r}")
    if not 0.0 <= reserve < 1.0:
        raise ValueError(f"reserve_fraction must be in [0,1), got {reserve_fraction!r}")
    return int(_math.floor(limit * (1.0 - reserve)))


def coingecko_month_key(as_of_ms: int) -> str:
    """UTC ``YYYY-MM`` for ``as_of_ms`` (D19.4, same as R01/request_budget)."""
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(int(as_of_ms) / 1000.0, tz=_dt.timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


def is_fx_fresh(source_as_of_ms: int | None, now_ms: int, max_age_ms: int = FX_MAX_AGE_MS) -> bool:
    """True when an FX observation is fresh enough to reuse (D11).

    ``None`` source time or age > ``max_age_ms`` (default 60s) is stale;
    callers must keep ``UNKNOWN`` and never guess ``USDT=1`` to save quota.
    """
    if source_as_of_ms is None:
        return False
    try:
        age = int(now_ms) - int(source_as_of_ms)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= int(max_age_ms)


def build_markets_request(
    coin_ids: list[str] | tuple[str, ...],
    base_url: str = COINGECKO_BASE_URL,
    *,
    vs_currency: str = "usd",
) -> tuple[str, dict[str, str]]:
    """Build the ``(url, params)`` pair for the batch markets endpoint.

    Pure function (no I/O): ``GET {base}/api/v3/coins/markets`` with
    ``vs_currency`` + comma-joined ``ids``. Batch is preferred over per-coin
    ``/coins/{id}`` for Fundamentals refresh (D11); per-coin documents remain
    the fallback for supply detail.
    """
    ids = [str(c).strip() for c in (coin_ids or []) if str(c).strip()]
    if not ids:
        raise ValueError("build_markets_request requires at least one coin id")
    url = f"{base_url.rstrip('/')}/api/v3/coins/markets"
    params = {"vs_currency": str(vs_currency or "usd"), "ids": ",".join(ids)}
    return url, params


def build_fx_request(
    coin_ids: list[str] | tuple[str, ...] | str,
    base_url: str = COINGECKO_BASE_URL,
    *,
    vs_currencies: str = "usd",
) -> tuple[str, dict[str, str]]:
    """Build the ``(url, params)`` pair for ``/simple/price`` FX reuse.

    Pure function (no I/O): batch FX for the given ids without forcing a
    per-coin ``/coins/{id}`` fetch every minute. Callers reuse a fresh
    (<=60s) observation of the same currency instead of re-pulling.
    """
    if isinstance(coin_ids, str):
        ids = [coin_ids.strip()] if coin_ids.strip() else []
    else:
        ids = [str(c).strip() for c in (coin_ids or []) if str(c).strip()]
    if not ids:
        raise ValueError("build_fx_request requires at least one coin id")
    url = f"{base_url.rstrip('/')}/api/v3/simple/price"
    return url, {"ids": ",".join(ids), "vs_currencies": str(vs_currencies or "usd")}


def parse_markets_payload(
    payload: Any,
    *,
    request_url: str,
    fetched_at_ms: int,
) -> tuple[Fundamentals, ...]:
    """Parse a ``/coins/markets`` list into :class:`Fundamentals` tuples.

    Each row must carry ``id``; absent numeric fields stay ``None`` (never
    0). Raises :class:`CoinGeckoBadResponse` for non-list payloads.
    """
    if not isinstance(payload, list):
        raise CoinGeckoBadResponse("coins/markets payload must be a list")
    out: list[Fundamentals] = []
    for row in payload:
        if not isinstance(row, Mapping):
            raise CoinGeckoBadResponse("coins/markets row must be an object")
        coin_id = row.get("id")
        if not isinstance(coin_id, str) or not coin_id.strip():
            raise CoinGeckoBadResponse("coins/markets row misses string id")
        market_cap = row.get("market_cap")
        fdv = row.get("fully_diluted_valuation", row.get("fdv"))
        circulating = row.get("circulating_supply")
        total = row.get("total_supply")
        maximum = row.get("max_supply")
        ath = row.get("ath")
        ath_change_pct = row.get("ath_change_percentage")
        ath_date_raw = row.get("ath_date")
        last_updated_raw = row.get("last_updated")
        out.append(
            Fundamentals(
                coingecko_id=coin_id.strip(),
                symbol=row.get("symbol") if isinstance(row.get("symbol"), str) else None,
                name=row.get("name") if isinstance(row.get("name"), str) else None,
                price_usd=_as_float(row.get("current_price")),
                market_cap_usd=_as_float(market_cap),
                fdv_usd=_as_float(fdv),
                circulating_supply=_as_float(circulating),
                total_supply=_as_float(total),
                max_supply=_as_float(maximum),
                ath_usd=_as_float(ath),
                ath_change=(float(ath_change_pct) / 100.0)
                if isinstance(ath_change_pct, (int, float)) and not isinstance(ath_change_pct, bool)
                else None,
                ath_date_ms=_as_time_ms(ath_date_raw),
                categories=tuple(row.get("categories") or ())
                if isinstance(row.get("categories"), (list, tuple))
                else (),
                last_updated_ms=_as_time_ms(last_updated_raw),
                request_url=request_url,
                fetched_at_ms=fetched_at_ms,
            )
        )
    return tuple(out)


class CoinGeckoProvider:
    """``CoinGeckoProvider.fetch(identity) -> ProviderResult[Fundamentals]``.

    ``identity`` is duck-typed: only ``identity.coingecko_id`` is read, so
    both ``shortlab.models.AssetIdentity`` (frozen DTO) and the Task 5
    resolver record work. Identity-confidence gating (LOW/UNRESOLVED blocking)
    belongs to the Task 11 risk layer, not here.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str = COINGECKO_BASE_URL,
        timeout_sec: float = 10.0,
        max_retries: int = 2,
        market_ttl_sec: int = MARKET_TTL_SEC,
        clock: _Clock | None = None,
        sleep: _Sleeper | None = None,
        limiter: AsyncLimiter | None = None,
        fetcher: _Fetcher | None = None,
        transport_verified: bool | None = None,
        account_monthly_limit: int = ACCOUNT_MONTHLY_LIMIT,
        reserve_fraction: float = COINGECKO_RESERVE_FRACTION,
        repository: Any | None = None,
        supply_ttl_sec: int = SUPPLY_TTL_SEC,
        uuid_fn: Callable[[], str] | None = None,
    ) -> None:
        self._base_url = base_url
        self._timeout_sec = timeout_sec
        self._max_retries = max_retries
        self._market_ttl_ms = market_ttl_sec * 1000
        self._supply_ttl_ms = int(supply_ttl_sec) * 1000
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._sleep = sleep
        self._limiter_override = limiter
        self._fetcher = fetcher or self._default_fetcher
        # Only a real network transport counts as verified (F04.4): injected
        # test doubles stay unverified unless explicitly flagged, so FULL
        # skeletons can never register as READY-capable.
        self.transport_verified = (fetcher is None) if transport_verified is None else bool(
            transport_verified
        )
        self._cache: dict[str, _CacheEntry] = {}
        # R11a monthly + supply/FX reuse (D11/D19.4).
        self._account_monthly_limit = int(account_monthly_limit)
        self._reserve_fraction = float(reserve_fraction)
        self._repository = repository
        self._supply_cache: dict[str, _CacheEntry] = {}
        self._fx_cache: dict[str, tuple[int, str, int]] = {}
        self._uuid_fn = uuid_fn

    @property
    def _active_limiter(self) -> AsyncLimiter:
        return self._limiter_override if self._limiter_override is not None else _limiter()

    def cache_entry(self, coingecko_id: str) -> Fundamentals | None:
        """Return the cached snapshot for ``coingecko_id`` (if any)."""
        entry = self._cache.get(coingecko_id)
        return entry.data if entry is not None else None

    def clear_cache(self) -> None:
        self._cache.clear()
        self._supply_cache.clear()
        self._fx_cache.clear()

    def effective_monthly_limit(self) -> int:
        """Effective monthly sends after reserve (D11/D19.4, default 9000)."""
        return coingecko_effective_limit(
            self._account_monthly_limit, self._reserve_fraction
        )

    def get_cached_fx(self, currency: str, now_ms: int | None = None) -> str | None:
        """Reuse a fresh (<=60s) FX observation for ``currency`` (D11).

        Returns the cached rate string when fresh, else ``None`` (caller
        keeps ``UNKNOWN``; never guesses ``USDT=1`` to save quota). ``USD``
        is the only implied 1 (no fetch needed).
        """
        if str(currency or "").upper() == "USD":
            return "1"
        now = int(now_ms) if now_ms is not None else int(self._clock())
        row = self._fx_cache.get(str(currency or "").upper())
        if row is None:
            return None
        as_of_ms, rate, _known_ms = row
        if not is_fx_fresh(as_of_ms, now):
            return None
        return rate

    def put_fx(self, currency: str, rate: str, source_as_of_ms: int) -> None:
        """Record an FX observation for later 60s reuse (D11)."""
        now = int(self._clock())
        self._fx_cache[str(currency or "").upper()] = (
            int(source_as_of_ms),
            str(rate),
            int(now),
        )

    async def _check_monthly(self, now_ms: int) -> tuple[str, str] | None:
        """Reserve one monthly send via the repository when wired (D19.4).

        Returns ``(request_id, month_key)`` on admission, ``None`` when no
        repository is wired (in-memory RPM only). On exhaustion returns a
        ``BUDGET_LIMITED``-style failure via exception? Callers check
        ``None`` vs tuple; exhaustion raises a typed error handled as
        ``BUDGET_LIMITED`` UNAVAILABLE (never counted as sent).
        """
        if self._repository is None:
            return None
        import uuid as _uuid_mod

        month_key = coingecko_month_key(int(now_ms))
        request_id = self._uuid_fn() if self._uuid_fn is not None else str(_uuid_mod.uuid4())
        result = await self._repository.reserve_provider_request(
            PROVIDER_NAME,
            month_key,
            request_id,
            self.effective_monthly_limit(),
            int(now_ms),
        )
        if not bool(result.get("admitted")):
            raise _MonthlyExhausted(str(result.get("reason_code") or "BUDGET_MONTHLY_EXHAUSTED"))
        return request_id, month_key

    async def _finish_monthly(self, request_id: str | None, sent: bool, now_ms: int) -> None:
        if request_id is None or self._repository is None:
            return
        try:
            await self._repository.finish_provider_request(request_id, sent, int(now_ms))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # CR13 unified pre-send (D11/D19.4): every real transport reserves
    # monthly + host budget per attempt, checks deadline, handles midnight
    # rollover, bills per send; cache never reserves.
    # ------------------------------------------------------------------

    def _effective_ctx(self, request_context: Any | None) -> Any | None:
        """Explicit ``request_context`` wins, else the ambient scheduler ctx."""
        if request_context is not None:
            return request_context
        try:
            return get_current_request_context()
        except Exception:
            return None

    def _budget_and_job(self, ctx: Any | None) -> tuple[Any | None, str]:
        budget = getattr(ctx, "budget", None) if ctx is not None else None
        try:
            jt = str(getattr(ctx, "job_type", "entry") or "entry")
        except Exception:
            jt = "entry"
        return budget, jt

    async def _transport_with_budget(
        self, url: str, params: Mapping[str, Any], ctx: Any | None
    ) -> Any:
        """One budgeted transport with retries (per-send reserve/billing).

        Unified pre-send per attempt (D19.3/D19.4): family/weight from URL
        only, unknown job refuses, deadline checked before reserve and again
        before transport, monthly reserve with fresh UUID per attempt, host
        permit per attempt, UTC month re-read before transport entry with
        old-ID cancel + new-UUID reserve on rollover. Billing point is first
        transport entry: ``finish(sent=True)`` + ``permit.mark_sent()`` for
        every entered transport (success or fetcher exception); pre-transport
        denials cancel (``finish(False)`` + ``release_unsent``) and never
        send. Cache callers never reach here. Retries use new IDs via
        ``run_with_retries`` (only ``TransientUpstreamError`` retried).
        """
        # Strict family/weight from URL only (no ambient fallback).
        family, weight = resolve_family_or_raise(url, dict(params) if isinstance(params, Mapping) else params)
        host = host_from_url(url)
        budget, job_type = self._budget_and_job(ctx)
        # Unknown job_type refuses before any monthly reservation (D19.3).
        if budget is not None and budget_class(job_type) == JOB_TYPE_UNKNOWN:
            raise BudgetExhausted(
                f"{JOB_TYPE_UNKNOWN}: job_type={job_type!r} is not a known tier; refusing to send",
                reason_code=JOB_TYPE_UNKNOWN,
                next_allowed_at_ms=None,
                job_type=job_type,
                endpoint_family=family,
            )

        async def _one_send() -> Any:
            # Pre-attempt deadline (before any reservation).
            check_deadline_or_raise(ctx, int(self._clock()))
            now0 = int(self._clock())
            monthly_hold: tuple[str, str] | None = None
            try:
                monthly_hold = await self._check_monthly(now0)
            except _MonthlyExhausted:
                raise
            except Exception:
                # Reserve validation errors (month mismatch etc.) never send.
                raise
            permit: Any | None = None
            if budget is not None:
                res = budget.try_acquire(host, weight, job_type, family)
                if isinstance(res, Denied) or res is False:
                    denied = res if isinstance(res, Denied) else Denied()
                    if monthly_hold is not None:
                        try:
                            await self._finish_monthly(monthly_hold[0], False, int(self._clock()))
                        except Exception:
                            pass
                    raise BudgetExhausted(
                        getattr(denied, "message", "budget exhausted"),
                        reason_code=getattr(denied, "reason_code", "REQUEST_BUDGET_EXHAUSTED"),
                        next_allowed_at_ms=getattr(denied, "next_allowed_at_ms", None),
                        job_type=getattr(denied, "job_type", None) or job_type,
                        endpoint_family=getattr(denied, "endpoint_family", None) or family,
                    )
                permit = res
            # Limiter wait ends here; re-read clock before transport entry.
            async with self._active_limiter:
                now_pre = int(self._clock())
                try:
                    check_deadline_or_raise(ctx, now_pre)
                except BudgetExhausted:
                    if monthly_hold is not None:
                        try:
                            await self._finish_monthly(monthly_hold[0], False, now_pre)
                        except Exception:
                            pass
                    if permit is not None:
                        try:
                            permit.release_unsent()
                        except Exception:
                            pass
                    raise
                if monthly_hold is not None and self._repository is not None:
                    fresh_month = coingecko_month_key(now_pre)
                    if fresh_month != monthly_hold[1]:
                        # Midnight: cancel un-sent old ID, reserve new month.
                        try:
                            new_id, new_res = await handle_month_rollover(
                                self._repository,
                                provider=PROVIDER_NAME,
                                old_request_id=str(monthly_hold[0]),
                                old_month_key=str(monthly_hold[1]),
                                new_month_key=str(fresh_month),
                                monthly_limit=int(self.effective_monthly_limit()),
                                as_of_ms=int(now_pre),
                                uuid_fn=self._uuid_fn,
                            )
                        except Exception as exc:
                            if permit is not None:
                                try:
                                    permit.release_unsent()
                                except Exception:
                                    pass
                            raise exc
                        if not bool(new_res.get("admitted")):
                            if permit is not None:
                                try:
                                    permit.release_unsent()
                                except Exception:
                                    pass
                            raise _MonthlyExhausted(
                                str(new_res.get("reason_code") or "BUDGET_MONTHLY_EXHAUSTED")
                            )
                        monthly_hold = (str(new_id), str(fresh_month))
                # Final deadline immediately before transport entry.
                try:
                    check_deadline_or_raise(ctx, int(self._clock()))
                except BudgetExhausted:
                    if monthly_hold is not None:
                        try:
                            await self._finish_monthly(monthly_hold[0], False, int(self._clock()))
                        except Exception:
                            pass
                    if permit is not None:
                        try:
                            permit.release_unsent()
                        except Exception:
                            pass
                    raise
                # Transport entry (billing point): mark + finish(true) for
                # every entered send, success or fetcher exception.
                try:
                    payload = await self._fetcher(url, params)
                except TransientUpstreamError:
                    if permit is not None:
                        try:
                            permit.mark_sent()
                        except Exception:
                            pass
                    if monthly_hold is not None:
                        await self._finish_monthly(monthly_hold[0], True, int(self._clock()))
                    raise
                except asyncio.CancelledError:
                    # Cancelled before entry completed: release, never bill.
                    # If already marked (not possible here), mark path above
                    # would have billed; this is pre-entry cancellation.
                    if permit is not None:
                        try:
                            permit.release_unsent()
                        except Exception:
                            pass
                    if monthly_hold is not None:
                        try:
                            await self._finish_monthly(monthly_hold[0], False, int(self._clock()))
                        except Exception:
                            pass
                    raise
                except Exception:
                    if permit is not None:
                        try:
                            permit.mark_sent()
                        except Exception:
                            pass
                    if monthly_hold is not None:
                        await self._finish_monthly(monthly_hold[0], True, int(self._clock()))
                    raise
                # Success: bill then return.
                if permit is not None:
                    try:
                        permit.mark_sent()
                    except Exception:
                        pass
                if monthly_hold is not None:
                    await self._finish_monthly(monthly_hold[0], True, int(self._clock()))
                return payload

        return await run_with_retries(
            _one_send,
            sleep=self._sleep or asyncio.sleep,
            max_retries=self._max_retries,
        )

    def _budget_limited(
        self,
        cached: _CacheEntry | None,
        now_ms: int,
        detail: str,
    ) -> ProviderResult[Fundamentals]:
        """UNAVAILABLE ``BUDGET_LIMITED`` (stale attached when cached)."""
        log.info("coingecko budget-limited: %s", detail[:150])
        return ProviderResult(
            status="UNAVAILABLE",  # type: ignore[arg-type]
            source=PROVIDER_NAME,
            fetched_at_ms=int(now_ms),
            as_of_ms=(cached.as_of_ms if cached is not None else None),
            data=(cached.data if cached is not None else None),
            stale=bool(cached is not None),
            reason_code=BUDGET_LIMITED,
            error_message=str(detail)[:200],
        )

    async def _default_fetcher(self, url: str, params: Mapping[str, Any]) -> Any:
        session = await get_session()
        timeout = aiohttp.ClientTimeout(total=self._timeout_sec)
        # No auth headers, no API key: V1 uses the keyless public endpoint.
        async with session.get(url, params=dict(params), timeout=timeout) as resp:
            if resp.status == 404:
                raise CoinGeckoNotFound(f"unknown coingecko id at {url}")
            if resp.status in _RETRYABLE_STATUSES:
                raise TransientUpstreamError(resp.status, _retry_after_seconds(resp))
            try:
                resp.raise_for_status()
            except aiohttp.ClientResponseError as exc:
                raise CoinGeckoBadResponse(
                    f"unexpected status {resp.status} at {url}"
                ) from exc
            try:
                return await resp.json()
            except (aiohttp.ContentTypeError, ValueError) as exc:
                raise CoinGeckoBadResponse(f"invalid JSON at {url}") from exc

    def _stale_result(
        self,
        entry: _CacheEntry,
        *,
        status: str,
        reason_code: str,
        error_message: str | None,
    ) -> ProviderResult[Fundamentals]:
        log.info(
            "coingecko stale id=%s status=%s reason=%s fetched_at_ms=%d",
            entry.data.coingecko_id,
            status,
            reason_code,
            entry.fetched_at_ms,
        )
        return ProviderResult(
            status=status,  # type: ignore[arg-type]
            source=PROVIDER_NAME,
            fetched_at_ms=entry.fetched_at_ms,
            as_of_ms=entry.as_of_ms,
            data=entry.data,
            stale=True,
            reason_code=reason_code,
            error_message=error_message,
        )

    async def fetch(
        self, identity: Any, *, request_context: Any | None = None
    ) -> ProviderResult[Fundamentals]:
        """Fetch one coin document (CR13 unified budgeting, D11/D19.4).

        Cache hits never reserve/bill; every real transport reserves monthly
        + host budget per attempt (retry = new ID/permit), checks deadline
        before reserve and before transport, rolls month on midnight, bills
        per entered send. ``request_context`` (explicit else ambient) carries
        the shared budget/job/deadline; legacy ``None`` stays unbounded
        except for the wired monthly gate.
        """
        ctx = self._effective_ctx(request_context)
        now_ms = int(self._clock())
        raw_id = getattr(identity, "coingecko_id", None)
        coingecko_id = str(raw_id).strip() if raw_id is not None else ""
        if not coingecko_id:
            log.info("coingecko skip: identity has no verified coingecko_id")
            return ProviderResult(
                status="NOT_APPLICABLE",
                source=PROVIDER_NAME,
                fetched_at_ms=now_ms,
                as_of_ms=None,
                data=None,
                stale=False,
                reason_code=IDENTITY_NOT_MAPPED,
                error_message=(
                    "identity has no verified coingecko_id; "
                    "refusing to guess by symbol"
                ),
            )

        cached = self._cache.get(coingecko_id)
        if cached is not None and now_ms - cached.fetched_at_ms <= self._market_ttl_ms:
            return ProviderResult(
                status="OK",
                source=PROVIDER_NAME,
                fetched_at_ms=cached.fetched_at_ms,
                as_of_ms=cached.as_of_ms,
                data=cached.data,
                stale=False,
                reason_code=None,
                error_message=None,
            )

        url, params = build_coin_request(coingecko_id, self._base_url)
        request_url = _request_url_for_snapshot(url, params)
        # CR13: unified pre-send per attempt inside _transport_with_budget.
        # Cache hits above never reserve; monthly/host billed per entered
        # transport (retry = new ID), deadline + month rollover handled.
        try:
            payload = await self._transport_with_budget(url, params, ctx)
        except _MonthlyExhausted as exc:
            return self._budget_limited(
                cached, now_ms, f"coingecko monthly quota exhausted ({exc.reason_code})"
            )
        except BudgetExhausted as exc:
            return self._budget_limited(
                cached, now_ms, f"coingecko send refused ({exc.reason_code})"
            )
        except UnbudgetedEndpointError as exc:
            return self._failure(cached, COINGECKO_CLIENT_ERROR, "ERROR", str(exc), now_ms)
        except CoinGeckoNotFound as exc:
            return self._failure(
                cached, COINGECKO_UNKNOWN_ID, "ERROR", str(exc), now_ms
            )
        except TransientUpstreamError as exc:
            if exc.status == 429:
                return self._failure(
                    cached,
                    COINGECKO_RATE_LIMITED,
                    "UNAVAILABLE",
                    f"rate limited (status 429) for id={coingecko_id}",
                    now_ms,
                )
            return self._failure(
                cached,
                COINGECKO_UPSTREAM_ERROR,
                "ERROR",
                f"upstream status {exc.status} for id={coingecko_id}",
                now_ms,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return self._failure(
                cached,
                COINGECKO_TIMEOUT,
                "UNAVAILABLE",
                f"request timed out for id={coingecko_id}",
                now_ms,
            )
        except aiohttp.ClientError as exc:
            return self._failure(
                cached,
                COINGECKO_NETWORK_ERROR,
                "UNAVAILABLE",
                f"network error for id={coingecko_id}: {type(exc).__name__}",
                now_ms,
            )
        except CoinGeckoBadResponse as exc:
            # Fetcher-level bad response already billed per send.
            return self._failure(cached, COINGECKO_CLIENT_ERROR, "ERROR", str(exc), now_ms)
        except Exception as exc:  # noqa: BLE001 - encapsulated, never raised
            # Raw text is truncated and redacted by ProviderResult's
            # sanitize_error_message; it never reaches the public API.
            detail = str(exc)[:200]
            suffix = f": {detail}" if detail else ""
            return self._failure(
                cached,
                COINGECKO_NETWORK_ERROR,
                "UNAVAILABLE",
                f"unexpected {type(exc).__name__}{suffix} for id={coingecko_id}",
                now_ms,
            )

        try:
            fundamentals = parse_coin_document(
                coingecko_id,
                payload,
                request_url=request_url,
                fetched_at_ms=now_ms,
            )
        except CoinGeckoBadResponse as exc:
            # Transport already billed per send; parse failure bills nothing extra.
            return self._failure(
                cached, COINGECKO_BAD_RESPONSE, "ERROR", str(exc), now_ms
            )

        as_of_ms = (
            fundamentals.last_updated_ms
            if fundamentals.last_updated_ms is not None
            else now_ms
        )
        self._cache[coingecko_id] = _CacheEntry(
            data=fundamentals, fetched_at_ms=now_ms, as_of_ms=as_of_ms
        )
        # Supply view shares the same atomic document but has an independent
        # 6h TTL (D11): refresh the supply cache alongside the market cache.
        self._supply_cache[coingecko_id] = _CacheEntry(
            data=fundamentals, fetched_at_ms=now_ms, as_of_ms=as_of_ms
        )
        log.info("coingecko ok id=%s as_of_ms=%s", coingecko_id, as_of_ms)
        return ProviderResult(
            status="OK",
            source=PROVIDER_NAME,
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms,
            data=fundamentals,
            stale=False,
            reason_code=None,
            error_message=None,
        )

    async def fetch_many(
        self, identities: list[Any] | tuple[Any, ...], *, request_context: Any | None = None
    ) -> dict[str, ProviderResult[Fundamentals]]:
        """Batch Fundamentals refresh preferring ``/coins/markets`` (D11).

        Cache hits (market TTL) never send; misses are fetched in one batch
        markets call (per-send monthly + RPM + host budget via the unified
        pre-send check, retry = new ID) with per-coin fallback to :meth:`fetch`
        on partial failure. Supply uses the independent 6h TTL via
        ``_supply_cache``. ``request_context`` (explicit else ambient) is
        honoured for budget/deadline; cache never double-charges.
        """
        ctx = self._effective_ctx(request_context)
        now_ms = int(self._clock())
        ids: list[str] = []
        by_id: dict[str, Any] = {}
        for ident in identities or []:
            raw = getattr(ident, "coingecko_id", None)
            cid = str(raw).strip() if raw is not None else ""
            if cid and cid not in by_id:
                by_id[cid] = ident
                ids.append(cid)
        out: dict[str, ProviderResult[Fundamentals]] = {}
        misses: list[str] = []
        for cid in ids:
            cached = self._cache.get(cid)
            if cached is not None and now_ms - cached.fetched_at_ms <= self._market_ttl_ms:
                out[cid] = ProviderResult(
                    status="OK",
                    source=PROVIDER_NAME,
                    fetched_at_ms=cached.fetched_at_ms,
                    as_of_ms=cached.as_of_ms,
                    data=cached.data,
                    stale=False,
                    reason_code=None,
                    error_message=None,
                )
            else:
                misses.append(cid)
        if not misses:
            return out
        # One batch markets transport (unified per-send budgeting inside).
        url, params = build_markets_request(misses, self._base_url)
        request_url = _request_url_for_snapshot(url, params)
        try:
            payload = await self._transport_with_budget(url, params, ctx)
        except (_MonthlyExhausted, BudgetExhausted) as exc:
            for cid in misses:
                cached = self._cache.get(cid)
                out[cid] = ProviderResult(
                    status="UNAVAILABLE",  # type: ignore[arg-type]
                    source=PROVIDER_NAME,
                    fetched_at_ms=now_ms,
                    as_of_ms=(cached.as_of_ms if cached is not None else None),
                    data=(cached.data if cached is not None else None),
                    stale=bool(cached is not None),
                    reason_code=BUDGET_LIMITED,
                    error_message=f"coingecko monthly/budget exhausted ({getattr(exc, 'reason_code', exc)})",
                )
            return out
        except UnbudgetedEndpointError as exc:
            for cid in misses:
                # Batch family unknown: per-coin fallback keeps honest mapping.
                out[cid] = await self.fetch(by_id[cid], request_context=request_context)
            _ = exc
            return out
        except Exception:  # noqa: BLE001 - per-coin fallback below (already billed per send)
            for cid in misses:
                # Fall back to single fetch (preserves per-coin error mapping).
                out[cid] = await self.fetch(by_id[cid], request_context=request_context)
            return out
        try:
            parsed = parse_markets_payload(payload, request_url=request_url, fetched_at_ms=now_ms)
        except CoinGeckoBadResponse as exc:
            # Batch transport already billed per send; fallback bills per coin.
            for cid in misses:
                out[cid] = await self.fetch(by_id[cid], request_context=request_context)
            _ = exc
            return out
        by_parsed: dict[str, Fundamentals] = {f.coingecko_id: f for f in parsed}
        for cid in misses:
            fund = by_parsed.get(cid)
            if fund is None:
                # Not in batch response: per-coin fallback (honest, no guess).
                out[cid] = await self.fetch(by_id[cid], request_context=request_context)
                continue
            as_of = fund.last_updated_ms if fund.last_updated_ms is not None else now_ms
            self._cache[cid] = _CacheEntry(data=fund, fetched_at_ms=now_ms, as_of_ms=as_of)
            self._supply_cache[cid] = _CacheEntry(data=fund, fetched_at_ms=now_ms, as_of_ms=as_of)
            out[cid] = ProviderResult(
                status="OK",
                source=PROVIDER_NAME,
                fetched_at_ms=now_ms,
                as_of_ms=as_of,
                data=fund,
                stale=False,
                reason_code=None,
                error_message=None,
            )
        return out

    async def fetch_fx(
        self, coin_ids: list[str] | tuple[str, ...] | str, *, request_context: Any | None = None
    ) -> dict[str, str | None]:
        """Batch FX via ``/simple/price`` with 60s reuse (D11).

        Fresh (<=60s) cached FX for the same currency is reused without a new
        CoinGecko send; missing/stale FX returns ``None`` (caller keeps
        ``UNKNOWN``, never guesses ``USDT=1``). Unified pre-send per attempt
        (monthly + host + deadline + midnight) applies to the single batch
        transport; cache reuse never reserves.
        """
        ctx = self._effective_ctx(request_context)
        now_ms = int(self._clock())
        if isinstance(coin_ids, str):
            wanted = [coin_ids.strip()] if coin_ids.strip() else []
        else:
            wanted = [str(c).strip() for c in (coin_ids or []) if str(c).strip()]
        out: dict[str, str | None] = {}
        need: list[str] = []
        for cid in wanted:
            # FX cache is keyed by coin id upper; reuse when fresh.
            row = self._fx_cache.get(cid.upper())
            if row is not None and is_fx_fresh(row[0], now_ms):
                out[cid] = row[1]
            else:
                need.append(cid)
                out[cid] = None
        if not need:
            return out
        url, params = build_fx_request(need, self._base_url)
        try:
            payload = await self._transport_with_budget(url, params, ctx)
        except (_MonthlyExhausted, BudgetExhausted, UnbudgetedEndpointError, Exception):
            # Monthly/host/deadline exhausted or transport failed: no FX,
            # caller keeps UNKNOWN (never USDT=1). Already billed/cancelled
            # per send inside the helper; cache reuse above never reserved.
            return out
        if isinstance(payload, Mapping):
            for cid in need:
                row = payload.get(cid) or payload.get(cid.lower()) or payload.get(cid.upper())
                price: str | None = None
                if isinstance(row, Mapping):
                    raw = row.get("usd")
                    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                        try:
                            value = float(raw)
                        except (TypeError, ValueError):
                            value = None
                        if value is not None and value == value and value > 0:
                            price = str(raw)
                if price is not None:
                    out[cid] = price
                    self._fx_cache[cid.upper()] = (now_ms, price, now_ms)
        return out

    def _failure(
        self,
        cached: _CacheEntry | None,
        reason_code: str,
        status: str,
        detail: str,
        now_ms: int,
    ) -> ProviderResult[Fundamentals]:
        if cached is not None:
            return self._stale_result(
                cached, status=status, reason_code=reason_code, error_message=detail
            )
        log.info("coingecko %s reason=%s", status.lower(), reason_code)
        return ProviderResult(
            status=status,  # type: ignore[arg-type]
            source=PROVIDER_NAME,
            fetched_at_ms=now_ms,
            as_of_ms=None,
            data=None,
            stale=False,
            reason_code=reason_code,
            error_message=detail,
        )


__all__ = [
    "ACCOUNT_MONTHLY_LIMIT",
    "BUDGET_LIMITED",
    "COINGECKO_BAD_RESPONSE",
    "COINGECKO_BASE_URL",
    "COINGECKO_CLIENT_ERROR",
    "COINGECKO_DEMO_BASE_URL",
    "COINGECKO_DEMO_KEY_PARAM",
    "COINGECKO_DIRECTORY_GRACE_SEC",
    "COINGECKO_DIRECTORY_TTL_SEC",
    "COINGECKO_LIST_MAX_BYTES",
    "COINGECKO_MAX_PER_MIN",
    "COINGECKO_MAX_PLATFORM_DETAIL",
    "COINGECKO_NETWORK_ERROR",
    "COINGECKO_PERIOD_SEC",
    "COINGECKO_PRO_BASE_URL",
    "COINGECKO_PRO_KEY_PARAM",
    "COINGECKO_RATE_LIMITED",
    "COINGECKO_RESERVE_FRACTION",
    "COINGECKO_TIMEOUT",
    "COINGECKO_UNKNOWN_ID",
    "COINGECKO_UPSTREAM_ERROR",
    "FX_MAX_AGE_MS",
    "IDENTITY_DIRECTORY_UNCONFIGURED",
    "IDENTITY_NOT_MAPPED",
    "LOCAL_REQUESTS_PER_MINUTE",
    "MARKET_TTL_SEC",
    "PROVIDER_NAME",
    "SUPPLY_GRACE_SEC",
    "SUPPLY_TTL_SEC",
    "CoinDirectoryEntry",
    "CoinGeckoBadResponse",
    "CoinGeckoNotFound",
    "CoinGeckoProvider",
    "CoinGeckoUnconfigured",
    "Fundamentals",
    "build_coin_detail_request",
    "build_coin_request",
    "build_coins_list_request",
    "build_fx_request",
    "build_markets_request",
    "coingecko_effective_limit",
    "coingecko_month_key",
    "coingecko_route",
    "coins_list_size_bytes",
    "is_fx_fresh",
    "parse_coin_document",
    "parse_coin_platforms",
    "parse_coins_list",
    "parse_markets_payload",
    "redact_coins_list_url",
]
