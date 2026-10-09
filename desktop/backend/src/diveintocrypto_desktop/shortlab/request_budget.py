"""F03: real HTTP budget, unique retry accounting and cross-round cache.

Design A7.1/A7.2; plan F03 (AC03/AC04/AC07).

- :class:`RequestBudget` is the final-send layer budget. Every real HTTP
  attempt (first try, retry, pagination page, termination-confirm page)
  atomically reserves via :meth:`RequestBudget.try_acquire` (synchronous, no
  ``await`` between check and increment so concurrent jobs never overshoot)
  and is recorded only by :meth:`Permit.mark_sent` when the send actually
  goes out. Cancellation before the send releases via
  :meth:`Permit.release_unsent`; an already-sent attempt is never refunded.
- Request count, exchange request weight and the special funding / OI-ratio
  windows are tracked separately. Connection-pool size never substitutes for
  rate limit. The ratio/OI 40/60s and funding 80/300s protections stay
  enforced (see :mod:`diveintocrypto_desktop.data.http` and
  :mod:`diveintocrypto_desktop.data.funding`); priority reserves never bypass
  them.
- Priority: host quota reserves 20% for ``monitor`` and 30% for ``scanner``;
  background jobs (``entry``/``backfill``/``retention``/others) may only use
  the remaining ~50%. High priority may consume unused quota; background
  must never eat the reserves. Sustained shortage surfaces as an explicit
  denial/queue, never an invisible extra budget.
- :data:`ENDPOINT_WEIGHTS_VERSION` versions the endpoint-weight fixture.
  Weights come from this fixed fixture (per endpoint + limit bucket), never
  inferred from ``exchangeInfo.rateLimits`` alone. An endpoint without a
  weight is ``UNBUDGETED_ENDPOINT`` and must not be sent.
- :class:`ObservedCache` is the long-lived shared cache (held by F06 at
  runtime, injected into :class:`shortlab.entry.EntryBudget`). ``get`` returns
  the identical ``Observed`` (original ``known_at``/``source_as_of``), never a
  hit-time restamp. ``EntryBudget`` resets counts per round but reuses the
  shared instance across rounds.
"""

from __future__ import annotations

import time
import urllib.parse
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Hashable

from diveintocrypto_desktop.shortlab import observations as _obs

__all__ = [
    "ENDPOINT_WEIGHTS_VERSION",
    "ENDPOINT_WEIGHTS",
    "UNBUDGETED_ENDPOINT",
    "BUDGET_EXHAUSTED",
    "JOB_TYPE_UNKNOWN",
    "DEADLINE_EXCEEDED",
    "MONITOR_RESERVE_FRAC",
    "SCANNER_RESERVE_FRAC",
    "FUNDING_MAX_SENDS",
    "FUNDING_WINDOW_MS",
    "RATIO_MAX_SENDS",
    "RATIO_WINDOW_MS",
    "FUNDING_FAMILIES",
    "RATIO_FAMILIES",
    "BudgetExhausted",
    "UnbudgetedEndpointError",
    "Permit",
    "Denied",
    "RequestBudget",
    "RequestContext",
    "ObservedCache",
    "make_request_context",
    "endpoint_family_for_url",
    "endpoint_weight",
    "budget_class",
    "utc_month_key",
    "monthly_effective_limit",
    "is_deadline_exceeded",
    "get_current_request_context",
    "set_current_request_context",
    "scoped_request_context",
]

#: Versioned endpoint-weight fixture. Bump when any weight changes.
ENDPOINT_WEIGHTS_VERSION = "endpoint-weights-v2"

UNBUDGETED_ENDPOINT = "UNBUDGETED_ENDPOINT"
BUDGET_EXHAUSTED = "REQUEST_BUDGET_EXHAUSTED"
#: Unknown job_type rejection code (D19.3: not silent background).
JOB_TYPE_UNKNOWN = "JOB_TYPE_UNKNOWN"
#: Deadline exceeded before transport (R11a: send-time deadline check).
DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"

MONITOR_RESERVE_FRAC = 0.20
SCANNER_RESERVE_FRAC = 0.30

#: Funding dedicated window: 80 real attempts / 300s shared (design A7.1.1).
FUNDING_MAX_SENDS = 80
FUNDING_WINDOW_MS = 300_000

#: OI / futures-data dedicated window: 40 / 60s (existing http protection).
RATIO_MAX_SENDS = 40
RATIO_WINDOW_MS = 60_000

FUNDING_FAMILIES = frozenset({"fundingRate", "fundingInfo"})
RATIO_FAMILIES = frozenset({"oi", "ratio"})

#: D19.3 job_type -> budget tier (frozen). Old names retained.
#: monitor/active_monitor/critical -> MONITOR (100%);
#: scanner/user_scanner/interactive -> SCANNER (total - floor(total*0.2));
#: opportunity/evidence/entry/backfill/funding_backfill/retention/background
#:   -> BACKGROUND (total - floor(total*0.2) - floor(total*0.3));
#: anything else -> JOB_TYPE_UNKNOWN (refuse to send, never silent background).
BACKGROUND_JOB_TYPES = frozenset(
    {
        "opportunity",
        "evidence",
        "entry",
        "backfill",
        "funding_backfill",
        "retention",
        "background",
    }
)
MONITOR_JOB_TYPES = frozenset({"monitor", "active_monitor", "critical"})
SCANNER_JOB_TYPES = frozenset({"scanner", "user_scanner", "interactive"})

#: R11a scheduler compatibility: legacy scheduler/service job_types map to
#: the nearest D19.3 tier so existing periodic jobs keep working. Truly
#: unknown values (not in D19.3 nor this map) still reject as UNKNOWN.
_SCHEDULER_JOB_TIER_FALLBACK: dict[str, str] = {
    "score_refresh": "SCANNER",
    "contract_refresh": "SCANNER",
    "metadata": "SCANNER",
    "funding_capture_refresh": "SCANNER",
    "hedge_venue_refresh": "SCANNER",
    "hedge_monitor": "MONITOR",
    "hedge_settlement_check": "MONITOR",
    "grader": "BACKGROUND",
    "maintenance": "BACKGROUND",
}


def budget_class(job_type: str | None) -> str:
    """Map ``job_type`` to its D19.3 budget tier.

    Returns ``"MONITOR"`` / ``"SCANNER"`` / ``"BACKGROUND"`` for known
    values (case-insensitive, ``interactive`` is SCANNER), else
    ``JOB_TYPE_UNKNOWN``. Callers must refuse to send UNKNOWN (never fall
    back to background).
    """
    jt = str(job_type or "").strip().lower()
    if jt in MONITOR_JOB_TYPES:
        return "MONITOR"
    if jt in SCANNER_JOB_TYPES:
        return "SCANNER"
    if jt in BACKGROUND_JOB_TYPES:
        return "BACKGROUND"
    fallback = _SCHEDULER_JOB_TIER_FALLBACK.get(jt)
    if fallback is not None:
        return fallback
    return JOB_TYPE_UNKNOWN


def utc_month_key(as_of_ms: int) -> str:
    """UTC ``YYYY-MM`` month key for ``as_of_ms`` (D19.4, matches R01)."""
    import datetime as _dt

    moment = _dt.datetime.fromtimestamp(int(as_of_ms) / 1000.0, tz=_dt.timezone.utc)
    return f"{moment.year:04d}-{moment.month:02d}"


def monthly_effective_limit(account_monthly_limit: int, reserve_fraction: float) -> int:
    """Effective monthly sends: ``floor(limit * (1 - reserve))`` (D11/D19.4)."""
    limit = int(account_monthly_limit)
    reserve = float(reserve_fraction)
    if limit <= 0:
        raise ValueError(f"account_monthly_limit must be > 0, got {account_monthly_limit!r}")
    if not 0.0 <= reserve < 1.0:
        raise ValueError(f"reserve_fraction must be in [0,1), got {reserve_fraction!r}")
    import math as _math

    return int(_math.floor(limit * (1.0 - reserve)))


def is_deadline_exceeded(deadline_ms: int | None, now_ms: int) -> bool:
    """True when ``deadline_ms`` is set and ``now_ms`` is past it."""
    if deadline_ms is None:
        return False
    try:
        return int(now_ms) > int(deadline_ms)
    except (TypeError, ValueError):
        return False


def _default_clock_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Endpoint family + weight fixture (per endpoint + limit bucket)
# ---------------------------------------------------------------------------

#: Static fixture: family -> weight or limit-bucket table. Weights are
#: illustrative-but-fixed Binance public weights; the point is they are
#: versioned and limit-aware, not guessed from rateLimits at runtime.
#: R11a (endpoint-weights-v2): existing families/buckets retained, plus
#: markKlines/fundingInfo/Spot/Alpha/CoinGecko/0x per D19.3. FAPI/Spot final
#: weight is max(local token, Fixture frozen official IP weight) elsewhere;
#: Alpha/CoinGecko/0x use independent host-local RPS/token limits.
ENDPOINT_WEIGHTS: dict[str, Any] = {
    "klines": {"default": 10, "buckets": [(100, 10), (500, 20), (1000, 30)], "min": 1, "max": 1000},
    "markKlines": {"default": 10, "buckets": [(100, 10), (500, 20), (1000, 30), (1500, 30)], "min": 1, "max": 1500},
    "fundingRate": {"default": 10, "buckets": [(100, 5), (500, 10), (1000, 20)]},
    "fundingInfo": {"default": 5},
    "premiumIndex": {"default": 10},
    "premiumIndexAll": {"default": 10},
    "futuresDepth": {"default": 20, "buckets": [(100, 20), (500, 30), (1000, 50)], "min": 1, "max": 1000, "require_limit": True},
    "oi": {"default": 10},
    "ratio": {"default": 10},
    "exchangeInfo": {"default": 20},
    "ticker": {"default": 5},
    "universe": {"default": 5},
    "spot": {"default": 10},
    "spotKlines": {"default": 10, "buckets": [(100, 10), (500, 20), (1000, 30)], "min": 1, "max": 1000},
    "spotDepth": {"default": 20, "buckets": [(100, 20), (500, 30), (1000, 50)], "min": 1, "max": 1000, "require_limit": True},
    "spotTicker": {"default": 40},
    "alphaTokenList": {"default": 10},
    "alphaExchangeInfo": {"default": 10},
    "alphaTicker": {"default": 10},
    "alphaDepth": {"default": 10},
    "alphaKlines": {"default": 10},
    "cgDirectory": {"default": 1},
    "cgMarkets": {"default": 1},
    "cgCoin": {"default": 1},
    "cgFx": {"default": 1},
    "onchainPrice": {"default": 1},
}

#: R11a frozen Alpha paths (from data/binance_alpha.py five PATH constants).
_ALPHA_PATH_TO_FAMILY: dict[str, str] = {
    "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list": "alphaTokenList",
    "/bapi/defi/v1/public/alpha-trade/get-exchange-info": "alphaExchangeInfo",
    "/bapi/defi/v1/public/alpha-trade/ticker": "alphaTicker",
    "/bapi/defi/v1/public/alpha-trade/fullDepth": "alphaDepth",
    "/bapi/defi/v1/public/alpha-trade/klines": "alphaKlines",
}

#: R11a frozen 0x read-only price paths (adapter PRICE_PATH + public v2
#: header no-suffix path). No trade/approve/calldata paths are ever allowed.
_0X_PRICE_PATHS = frozenset(
    {"/swap/allowance-holder/price/v2", "/swap/allowance-holder/price"}
)


def _fapi_mirror_host() -> str | None:
    """HTTPS origin host of ``DIVE_FAPI_BASE`` when explicitly configured."""
    import os as _os

    raw = (_os.environ.get("DIVE_FAPI_BASE") or "").strip().rstrip("/")
    if not raw:
        return None
    try:
        host = urllib.parse.urlparse(raw).netloc.lower()
    except Exception:
        return None
    return host or None


def _spot_mirror_host() -> str | None:
    import os as _os

    raw = (_os.environ.get("DIVE_SPOT_BASE") or "").strip().rstrip("/")
    if not raw:
        return None
    try:
        host = urllib.parse.urlparse(raw).netloc.lower()
    except Exception:
        return None
    return host or None


def endpoint_family_for_url(url: str) -> str | None:
    """Map a request URL to its budget family (``None`` = unknown).

    R11a: host + full path must both match (D19.3). A matching path on a
    non-allowlisted host is still unknown. ``DIVE_FAPI_BASE`` mirror host is
    the only fapi alias and shares the fapi budget; any other host never
    inherits fapi rights. Unknown host/path/limit must refuse to send.
    """
    try:
        parts = urllib.parse.urlparse(str(url))
        path = parts.path or str(url)
        host = (parts.netloc or "").lower()
    except Exception:
        path = str(url)
        host = ""
    # Normalise fapi mirror alias for family resolution (shares fapi budget).
    mirror = _fapi_mirror_host()
    is_fapi = host in ("fapi.binance.com", "fapi") or (mirror is not None and host == mirror)
    # Bare-host legacy ("fapi") without netloc: treat as fapi for compat.
    if not host:
        is_fapi = True
    if is_fapi:
        if path == "/fapi/v1/klines":
            return "klines"
        if path == "/fapi/v1/markPriceKlines":
            return "markKlines"
        if path == "/fapi/v1/fundingRate":
            return "fundingRate"
        if path == "/fapi/v1/fundingInfo":
            return "fundingInfo"
        if path == "/fapi/v1/premiumIndex":
            return "premiumIndex"
        if path == "/fapi/v1/depth":
            return "futuresDepth"
        if path == "/fapi/v1/exchangeInfo":
            return "exchangeInfo"
        if path == "/fapi/v1/ticker/24hr" or path == "/fapi/v1/ticker":
            return "ticker"
        if path.startswith("/futures/data/openInterestHist"):
            return "oi"
        if path.startswith("/futures/data/"):
            return "ratio"
        return None
    spot_mirror = _spot_mirror_host()
    is_spot = host in ("api.binance.com", "api") or (
        spot_mirror is not None and host == spot_mirror
    )
    if is_spot:
        if path == "/api/v3/klines":
            return "spotKlines"
        if path == "/api/v3/depth":
            return "spotDepth"
        if path == "/api/v3/ticker/24hr":
            return "spotTicker"
        if path == "/api/v3/exchangeInfo":
            return "exchangeInfo"
        # Legacy generic spot family for other /api/v3/* market paths is NOT
        # allowed: unknown path must refuse (D19.3).
        return None
    if host in ("www.binance.com",):
        family = _ALPHA_PATH_TO_FAMILY.get(path)
        if family is not None:
            return family
        return None
    if host in ("api.coingecko.com", "pro-api.coingecko.com"):
        if path == "/api/v3/coins/list":
            return "cgDirectory"
        if path == "/api/v3/coins/markets":
            return "cgMarkets"
        if path == "/api/v3/simple/price":
            return "cgFx"
        # Dynamic /api/v3/coins/{id} after exact matches (list/markets first).
        if path.startswith("/api/v3/coins/") and len(path) > len("/api/v3/coins/"):
            remainder = path[len("/api/v3/coins/"):]
            # Single id segment only (no extra slashes); query is not in path.
            if remainder and "/" not in remainder:
                return "cgCoin"
        return None
    if host in ("api.0x.org",):
        if path in _0X_PRICE_PATHS:
            return "onchainPrice"
        # Trade/approve/calldata paths are never allowed even on this host.
        return None
    return None


def _limit_param(params: dict[str, Any] | None) -> int | None:
    if not isinstance(params, dict):
        return None
    for key in ("limit", "Limit", "LIMIT"):
        if key in params:
            try:
                return int(params[key])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
    return None


def endpoint_weight(
    family: str | None, params: dict[str, Any] | None = None
) -> int | None:
    """Weight for ``(family, limit)``; ``None`` when the endpoint is unknown.

    Unknown families are ``UNBUDGETED_ENDPOINT`` and must not be sent under a
    budget. Limit-bucketed families reject out-of-range/missing limits
    (unknown limit => ``None``); spotTicker distinguishes single (2) vs full
    list (40) by ``symbol`` presence.
    """
    if family is None:
        return None
    # premiumIndex single vs all share the same fixture weight.
    if family == "premiumIndex" and isinstance(params, dict) and params.get("symbol") is None:
        family = "premiumIndexAll"
    # spotTicker: single symbol 2, full list 40 (D19.3).
    if family == "spotTicker":
        if isinstance(params, dict) and params.get("symbol"):
            return 2
        return 40
    spec = ENDPOINT_WEIGHTS.get(family)
    if spec is None:
        return None
    if isinstance(spec, int):
        return spec
    limit = _limit_param(params)
    buckets = spec.get("buckets") if isinstance(spec, dict) else None
    require_limit = bool(spec.get("require_limit")) if isinstance(spec, dict) else False
    lo = spec.get("min") if isinstance(spec, dict) else None
    hi = spec.get("max") if isinstance(spec, dict) else None
    if buckets:
        # Depth families require an explicit limit; klines families allow
        # default when no limit is given but reject out-of-range values.
        if limit is None:
            if require_limit:
                return None
            default = spec.get("default", 10) if isinstance(spec, dict) else 10
            return int(default)
        # Non-positive or non-finite limits are unknown.
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            return None
        if lo is not None and lim < int(lo):
            return None
        if hi is not None and lim > int(hi):
            return None
        for ceiling, weight in buckets:
            if lim <= int(ceiling):
                return int(weight)
        return None
    default = spec.get("default", 10) if isinstance(spec, dict) else 10
    return int(default)


def host_from_url(url: str) -> str:
    """Normalise the host part of a URL for budget accounting.

    R11a: ``fapi.binance.com`` (+ explicit ``DIVE_FAPI_BASE`` mirror) share
    the ``fapi`` budget; ``api.binance.com`` (+ ``DIVE_SPOT_BASE`` mirror)
    share ``api``; Alpha/CoinGecko/0x keep independent host buckets. Any
    other host keeps its own lowercased bucket but its paths still reject
    as unknown at the family layer.
    """
    try:
        netloc = urllib.parse.urlparse(str(url)).netloc
    except Exception:
        netloc = ""
    host = (netloc or "fapi").lower()
    if host in ("fapi.binance.com", "fapi"):
        return "fapi"
    if host in ("api.binance.com", "api"):
        return "api"
    mirror = _fapi_mirror_host()
    if mirror is not None and host == mirror:
        return "fapi"
    spot_mirror = _spot_mirror_host()
    if spot_mirror is not None and host == spot_mirror:
        return "api"
    return host


# ---------------------------------------------------------------------------
# Errors / permits
# ---------------------------------------------------------------------------


class BudgetExhausted(RuntimeError):
    """A send was denied: no budget left (explicit queue, no hidden sends)."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = BUDGET_EXHAUSTED,
        next_allowed_at_ms: int | None = None,
        job_type: str | None = None,
        endpoint_family: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.next_allowed_at_ms = next_allowed_at_ms
        self.job_type = job_type
        self.endpoint_family = endpoint_family


class UnbudgetedEndpointError(ValueError):
    """An endpoint without a weight fixture must not be sent under a budget."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason_code = UNBUDGETED_ENDPOINT


@dataclass
class Denied:
    """A rejected reservation: caller must queue, never send invisibly."""

    reason_code: str = BUDGET_EXHAUSTED
    message: str = "budget exhausted"
    next_allowed_at_ms: int | None = None
    job_type: str | None = None
    endpoint_family: str | None = None

    def __bool__(self) -> bool:  # explicit falsiness so `if permit:` works
        return False


@dataclass
class Permit:
    """An atomic reservation; only :meth:`mark_sent` records the send."""

    _budget: Any = field(repr=False)
    host: str = ""
    weight: int = 0
    job_type: str = "entry"
    endpoint_family: str = ""
    _state: str = field(default="reserved", repr=False)

    def mark_sent(self) -> None:
        """Record the actual send. Idempotent; sent sends are never refunded."""
        if self._state == "sent":
            return
        if self._state != "reserved":
            raise RuntimeError(f"permit already {self._state}, cannot mark_sent")
        self._state = "sent"
        self._budget._record_sent(self)

    def release_unsent(self) -> None:
        """Release a reservation cancelled before the send (reserved only)."""
        if self._state == "sent":
            return  # already sent: never refunded
        if self._state != "reserved":
            return
        self._state = "released"
        self._budget._release(self)

    def __bool__(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# RequestBudget
# ---------------------------------------------------------------------------


class RequestBudget:
    """Final-send layer budget shared across jobs (A7.1).

    ``max_sends``/``window_ms`` bound the host (default 240/60s — the Entry
    hard cap; construct smaller budgets in tests). The funding (80/300s) and
    ratio/OI (40/60s) dedicated windows are enforced in addition, with no
    priority bypass. :meth:`try_acquire` is synchronous so concurrent
    coroutines can never overshoot.
    """

    def __init__(
        self,
        max_sends: int = 240,
        window_ms: int = 60_000,
        *,
        funding_max: int = FUNDING_MAX_SENDS,
        funding_window_ms: int = FUNDING_WINDOW_MS,
        ratio_max: int = RATIO_MAX_SENDS,
        ratio_window_ms: int = RATIO_WINDOW_MS,
        monitor_reserve_frac: float = MONITOR_RESERVE_FRAC,
        scanner_reserve_frac: float = SCANNER_RESERVE_FRAC,
        clock: Callable[[], int] | None = None,
        host: str = "fapi",
        weights_version: str = ENDPOINT_WEIGHTS_VERSION,
        max_weight: int = 2400,
        weight_window_ms: int = 60_000,
        host_limits: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        if isinstance(max_sends, bool) or int(max_sends) < 1:
            raise ValueError(f"max_sends must be an int >= 1, got {max_sends!r}")
        if isinstance(window_ms, bool) or int(window_ms) < 1:
            raise ValueError(f"window_ms must be an int >= 1, got {window_ms!r}")
        self.max_sends = int(max_sends)
        self.window_ms = int(window_ms)
        self.funding_max = int(funding_max)
        self.funding_window_ms = int(funding_window_ms)
        self.ratio_max = int(ratio_max)
        self.ratio_window_ms = int(ratio_window_ms)
        self.monitor_reserve_frac = float(monitor_reserve_frac)
        self.scanner_reserve_frac = float(scanner_reserve_frac)
        self._clock = clock or _default_clock_ms
        self.host = str(host)
        self.weights_version = str(weights_version)
        self._host_sends: deque[int] = deque()
        self._funding_sends: deque[int] = deque()
        self._ratio_sends: deque[int] = deque()
        self._reserved_host = 0
        self._reserved_funding = 0
        self._reserved_ratio = 0
        self.sent_attempts = 0
        self.weight_used = 0
        self.denied_count = 0
        if max_weight < 1 or weight_window_ms < 1:
            raise ValueError("weight capacity and window must be positive")
        self.max_weight = int(max_weight)
        self.weight_window_ms = int(weight_window_ms)
        self._host_limits = dict(host_limits or {})
        self._hosts: dict[str, dict[str, Any]] = {}
        self._active_host = self.host
        self._weights: deque[tuple[int, int]] = deque()
        self._reserved_weight = 0
        self._save_host()

    def _save_host(self) -> None:
        self._hosts[self._active_host] = {
            "sends": self._host_sends, "funding": self._funding_sends,
            "ratio": self._ratio_sends, "reserved": self._reserved_host,
            "reserved_funding": self._reserved_funding,
            "reserved_ratio": self._reserved_ratio,
            "weights": self._weights, "reserved_weight": self._reserved_weight,
        }

    def _select_host(self, host: str) -> None:
        self._save_host()
        self._active_host = str(host or self.host).lower()
        state = self._hosts.setdefault(self._active_host, {
            "sends": deque(), "funding": deque(), "ratio": deque(),
            "reserved": 0, "reserved_funding": 0, "reserved_ratio": 0,
            "weights": deque(), "reserved_weight": 0,
        })
        self._host_sends, self._funding_sends, self._ratio_sends = state["sends"], state["funding"], state["ratio"]
        self._reserved_host = state["reserved"]
        self._reserved_funding, self._reserved_ratio = state["reserved_funding"], state["reserved_ratio"]
        self._weights, self._reserved_weight = state["weights"], state["reserved_weight"]

    def configure_host_limits(self, host: str, rate_limits: list[dict[str, Any]]) -> None:
        """Freeze a verified exchangeInfo REQUEST_WEIGHT limit per host.

        Only explicit REQUEST_WEIGHT contracts are accepted; endpoint weights
        still come from the versioned endpoint fixtures.
        """
        units = {"SECOND": 1000, "MINUTE": 60000, "HOUR": 3600000, "DAY": 86400000}
        limits = []
        for item in rate_limits:
            if item.get("rateLimitType") == "REQUEST_WEIGHT":
                duration = units.get(str(item.get("interval")), 0) * int(item.get("intervalNum", 1))
                limit = int(item.get("limit", 0))
                if duration > 0 and limit > 0:
                    limits.append((limit, duration))
        if not limits:
            raise ValueError("verified REQUEST_WEIGHT rate limit required")
        self._host_limits[str(host).lower()] = limits

    def _weight_limits(self) -> list[tuple[int, int]]:
        value = self._host_limits.get(self._active_host)
        if value is None:
            return [(self.max_weight, self.weight_window_ms)]
        return value if isinstance(value, list) else [value]

    # -- clock / windows ----------------------------------------------------
    def now_ms(self) -> int:
        return int(self._clock())

    @staticmethod
    def _prune(times: deque[int], now: int, window_ms: int) -> None:
        cutoff = now - int(window_ms)
        while times and times[0] <= cutoff:
            times.popleft()

    def _prune_all(self, now: int) -> None:
        self._prune(self._host_sends, now, self.window_ms)
        self._prune(self._funding_sends, now, self.funding_window_ms)
        self._prune(self._ratio_sends, now, self.ratio_window_ms)
        window = max(duration for _, duration in self._weight_limits())
        while self._weights and self._weights[0][0] <= now - window:
            self._weights.popleft()

    def _allowed_for_job(self, job_type: str) -> int:
        total = self.max_sends
        # Floor keeps tiny test budgets usable (max_sends=1 still sends once);
        # production 240 gives 48 monitor + 72 scanner + 120 background.
        monitor_reserve = int(total * self.monitor_reserve_frac)
        scanner_reserve = int(total * self.scanner_reserve_frac)
        tier = budget_class(job_type)
        if tier == "MONITOR":
            return total
        if tier == "SCANNER":
            return max(0, total - monitor_reserve)
        if tier == "BACKGROUND":
            return max(0, total - monitor_reserve - scanner_reserve)
        # JOB_TYPE_UNKNOWN: no quota (refuse, never silent background).
        return 0

    def _next_allowed_at(self, now: int, family: str, reason: str) -> int | None:
        """Next ms when the limiting window slides (denial-specific)."""
        if reason == "funding" and self._funding_sends:
            return int(self._funding_sends[0]) + self.funding_window_ms
        if reason == "ratio" and self._ratio_sends:
            return int(self._ratio_sends[0]) + self.ratio_window_ms
        if self._host_sends:
            return int(self._host_sends[0]) + self.window_ms
        return now

    # -- acquire / record ----------------------------------------------------
    def try_acquire(
        self,
        host: str,
        weight: int | None,
        job_type: str,
        endpoint_family: str,
    ) -> Permit | Denied:
        """Atomically reserve one send; ``Denied`` when the window is full.

        ``weight=None`` (unknown endpoint) is denied as
        ``UNBUDGETED_ENDPOINT`` without sending. Unknown ``job_type``
        (D19.3) is denied as ``JOB_TYPE_UNKNOWN`` without sending. No
        ``await`` happens between the check and the increment, so concurrent
        jobs cannot overshoot.
        """
        self._select_host(host)
        now = self.now_ms()
        family = str(endpoint_family or "unknown")
        jt = str(job_type or "entry")
        # R11a: unknown job_type must refuse, never silent background.
        if budget_class(jt) == JOB_TYPE_UNKNOWN:
            self.denied_count += 1
            return Denied(
                reason_code=JOB_TYPE_UNKNOWN,
                message=f"{JOB_TYPE_UNKNOWN}: job_type={jt!r} is not a known tier "
                "(monitor/scanner/interactive/background); refusing to send",
                next_allowed_at_ms=None,
                job_type=jt,
                endpoint_family=family,
            )
        if weight is None:
            self.denied_count += 1
            return Denied(
                reason_code=UNBUDGETED_ENDPOINT,
                message=f"{UNBUDGETED_ENDPOINT}: no weight fixture for family={family!r} "
                f"(weights {self.weights_version}); refusing to send",
                next_allowed_at_ms=None,
                job_type=jt,
                endpoint_family=family,
            )
        try:
            w = int(weight)
        except (TypeError, ValueError):
            self.denied_count += 1
            return Denied(
                reason_code=UNBUDGETED_ENDPOINT,
                message=f"{UNBUDGETED_ENDPOINT}: bad weight {weight!r} for {family!r}",
                next_allowed_at_ms=None,
                job_type=jt,
                endpoint_family=family,
            )
        if w < 1:
            self.denied_count += 1
            return Denied(
                reason_code=UNBUDGETED_ENDPOINT,
                message=f"{UNBUDGETED_ENDPOINT}: non-positive weight for {family!r}",
                next_allowed_at_ms=None,
                job_type=jt,
                endpoint_family=family,
            )
        self._prune_all(now)
        allowed = self._allowed_for_job(jt)
        if len(self._host_sends) + self._reserved_host >= allowed:
            self.denied_count += 1
            return Denied(
                reason_code=BUDGET_EXHAUSTED,
                message=(
                    f"{BUDGET_EXHAUSTED}: host window full "
                    f"(sent={len(self._host_sends)} reserved={self._reserved_host} "
                    f"allowed={allowed}/{self.max_sends} job={jt} family={family})"
                ),
                next_allowed_at_ms=self._next_allowed_at(now, family, "host"),
                job_type=jt,
                endpoint_family=family,
            )
        if family in FUNDING_FAMILIES and (
            len(self._funding_sends) + self._reserved_funding >= self.funding_max
        ):
            self.denied_count += 1
            return Denied(
                reason_code=BUDGET_EXHAUSTED,
                message=(
                    f"{BUDGET_EXHAUSTED}: funding window full "
                    f"(sent={len(self._funding_sends)}/{self.funding_max}/300s "
                    f"job={jt})"
                ),
                next_allowed_at_ms=self._next_allowed_at(now, family, "funding"),
                job_type=jt,
                endpoint_family=family,
            )
        if family in RATIO_FAMILIES and (
            len(self._ratio_sends) + self._reserved_ratio >= self.ratio_max
        ):
            self.denied_count += 1
            return Denied(
                reason_code=BUDGET_EXHAUSTED,
                message=(
                    f"{BUDGET_EXHAUSTED}: ratio/OI window full "
                    f"(sent={len(self._ratio_sends)}/{self.ratio_max}/60s job={jt})"
                ),
                next_allowed_at_ms=self._next_allowed_at(now, family, "ratio"),
                job_type=jt,
                endpoint_family=family,
            )
        for weight_limit, weight_window in self._weight_limits():
            entries = [(at, value) for at, value in self._weights if at > now - weight_window]
            used_weight = sum(value for _, value in entries) + self._reserved_weight
            if used_weight + w > weight_limit:
                self.denied_count += 1
                return Denied(reason_code=BUDGET_EXHAUSTED,
                    message=f"{BUDGET_EXHAUSTED}: request weight window full ({used_weight}+{w}>{weight_limit})",
                    next_allowed_at_ms=(entries[0][0] + weight_window if entries else None),
                    job_type=jt, endpoint_family=family)
        self._reserved_weight += w
        self._reserved_host += 1
        if family in FUNDING_FAMILIES:
            self._reserved_funding += 1
        if family in RATIO_FAMILIES:
            self._reserved_ratio += 1
        return Permit(
            self,
            host=str(host or self.host),
            weight=w,
            job_type=jt,
            endpoint_family=family,
        )

    def acquire_or_raise(
        self,
        host: str,
        weight: int | None,
        job_type: str,
        endpoint_family: str,
    ) -> Permit:
        """Like :meth:`try_acquire` but raises on denial."""
        res = self.try_acquire(host, weight, job_type, endpoint_family)
        if isinstance(res, Denied):
            if res.reason_code == UNBUDGETED_ENDPOINT:
                raise UnbudgetedEndpointError(res.message)
            raise BudgetExhausted(
                res.message,
                reason_code=res.reason_code,
                next_allowed_at_ms=res.next_allowed_at_ms,
                job_type=res.job_type,
                endpoint_family=res.endpoint_family,
            )
        return res

    def _record_sent(self, permit: Permit) -> None:
        self._select_host(permit.host)
        self._reserved_weight -= permit.weight
        now = self.now_ms()
        self._prune_all(now)
        if self._reserved_host > 0:
            self._reserved_host -= 1
        if permit.endpoint_family in FUNDING_FAMILIES and self._reserved_funding > 0:
            self._reserved_funding -= 1
        if permit.endpoint_family in RATIO_FAMILIES and self._reserved_ratio > 0:
            self._reserved_ratio -= 1
        self._host_sends.append(now)
        self._weights.append((now, permit.weight))
        if permit.endpoint_family in FUNDING_FAMILIES:
            self._funding_sends.append(now)
        if permit.endpoint_family in RATIO_FAMILIES:
            self._ratio_sends.append(now)
        self.sent_attempts += 1
        try:
            self.weight_used += int(permit.weight)
        except (TypeError, ValueError):
            pass

    def _release(self, permit: Permit) -> None:
        self._select_host(permit.host)
        self._reserved_weight -= permit.weight
        if self._reserved_host > 0:
            self._reserved_host -= 1
        if permit.endpoint_family in FUNDING_FAMILIES and self._reserved_funding > 0:
            self._reserved_funding -= 1
        if permit.endpoint_family in RATIO_FAMILIES and self._reserved_ratio > 0:
            self._reserved_ratio -= 1

    # -- introspection ---------------------------------------------------------
    def remaining(self, job_type: str = "entry", host: str | None = None) -> int:
        """Remaining host sends visible to ``job_type`` in the current window."""
        self._select_host(host or self.host)
        now = self.now_ms()
        self._prune_all(now)
        return max(0, self._allowed_for_job(job_type) - len(self._host_sends) - self._reserved_host)

    def stats(self, job_type: str = "entry", host: str | None = None) -> dict[str, Any]:
        self._select_host(host or self.host)
        now = self.now_ms()
        self._prune_all(now)
        return {
            "sent_attempts": self.sent_attempts,
            "weight_used": self.weight_used,
            "denied": self.denied_count,
            "host_sent_in_window": len(self._host_sends),
            "host_reserved": self._reserved_host,
            "funding_sent_in_window": len(self._funding_sends),
            "ratio_sent_in_window": len(self._ratio_sends),
            "remaining_for_job": self.remaining(job_type, host),
            "host": self._active_host,
            "weight_in_window": sum(v for _, v in self._weights),
            "weight_reserved": self._reserved_weight,
            "max_sends": self.max_sends,
            "window_ms": self.window_ms,
            "weights_version": self.weights_version,
        }

    def reset(self) -> None:
        """Drop all window state and counters (test hook)."""
        self._hosts.clear()
        self._weights.clear()
        self._reserved_weight = 0
        self._host_sends.clear()
        self._funding_sends.clear()
        self._ratio_sends.clear()
        self._reserved_host = 0
        self._reserved_funding = 0
        self._reserved_ratio = 0
        self.sent_attempts = 0
        self.weight_used = 0
        self.denied_count = 0


# ---------------------------------------------------------------------------
# RequestContext (+ ambient ContextVar for legacy signatures)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestContext:
    """Explicit per-request budget wiring (additive; legacy callers omit it).

    ``budget`` is the shared :class:`RequestBudget`; ``job_type`` drives the
    monitor(20%)/scanner(30%) reserves (``entry``/``backfill``/``retention``
    are background); ``host``/``endpoint_family`` scope the reservation;
    ``trace_id``/``identity_snapshot_id`` propagate provenance into cache
    keys and job stats. R11a appends optional ``job_id``/``deadline_ms``
    (default None) at the end; position/order of the R00 six fields is
    preserved.
    """

    budget: RequestBudget | None = None
    host: str = "fapi"
    job_type: str = "entry"
    endpoint_family: str | None = None
    trace_id: str | None = None
    identity_snapshot_id: str | None = None
    job_id: str | None = None
    deadline_ms: int | None = None


def make_request_context(
    budget: RequestBudget | None,
    *,
    job_type: str = "entry",
    host: str = "fapi",
    endpoint_family: str | None = None,
    trace_id: str | None = None,
    identity_snapshot_id: str | None = None,
    job_id: str | None = None,
    deadline_ms: int | None = None,
) -> RequestContext:
    """Build a :class:`RequestContext` (F04 handoff: use this constructor).

    Pass the resulting context as ``request_context=...`` to
    ``data.http.get_json`` / ``data.funding.funding_history_range`` (and via
    ``EntryBudget(request_context=...)`` for Entry rounds). ``budget=None``
    means unbounded legacy behaviour (tests only — production always wires
    the shared budget). R11a appends ``job_id``/``deadline_ms`` keywords;
    ``deadline_ms`` is checked before each transport send (expired => deny,
    never send).
    """
    return RequestContext(
        budget=budget,
        host=str(host or "fapi"),
        job_type=str(job_type or "entry"),
        endpoint_family=endpoint_family,
        trace_id=trace_id,
        identity_snapshot_id=identity_snapshot_id,
        job_id=job_id,
        deadline_ms=int(deadline_ms) if deadline_ms is not None else None,
    )


async def reserve_monthly_budget(
    repository: Any,
    *,
    provider: str,
    month_key: str,
    request_id: str,
    monthly_limit: int,
    as_of_ms: int,
) -> Mapping[str, Any]:
    """Reserve one monthly send via the R01 repository (D19.4).

    Thin wrapper over ``RepositoryPort.reserve_provider_request`` so callers
    never bypass the single-worker transaction (count/compare/insert atomic).
    """
    return await repository.reserve_provider_request(
        provider, month_key, request_id, monthly_limit, as_of_ms
    )


async def finish_monthly_budget(
    repository: Any, *, request_id: str, sent: bool, as_of_ms: int
) -> None:
    """Finish one monthly reservation via the R01 repository (D19.4)."""
    await repository.finish_provider_request(request_id, sent, as_of_ms)


_current_request_context: ContextVar[RequestContext | None] = ContextVar(
    "shortlab_request_context", default=None
)


def get_current_request_context() -> RequestContext | None:
    """Ambient context for legacy signatures (propagates via ``copy_context``)."""
    return _current_request_context.get()


def set_current_request_context(ctx: RequestContext | None):  # type: ignore[no-untyped-def]
    """Set the ambient context; returns the token for later reset."""
    return _current_request_context.set(ctx)


class scoped_request_context:
    """Temporarily install ``ctx`` as the ambient request context.

    Exits restore the previous value, so nested scopes compose. Threads need
    ``copy_context`` propagation (see design A7.1).
    """

    def __init__(self, ctx: RequestContext | None) -> None:
        self._ctx = ctx
        self._token = None

    def __enter__(self):  # type: ignore[no-untyped-def]
        self._token = _current_request_context.set(self._ctx)
        return self._ctx

    def __exit__(self, *exc):  # type: ignore[no-untyped-def]
        if self._token is not None:
            _current_request_context.reset(self._token)
            self._token = None
        return False


# ---------------------------------------------------------------------------
# ObservedCache: long-lived shared cache (F06 holds the instance)
# ---------------------------------------------------------------------------


class ObservedCache:
    """Shared ``Observed`` cache keyed by ``(symbol, endpoint, window, ...)``.

    ``get(key, cutoff_ms)`` returns the identical ``Observed`` on hit
    (original ``known_at``/``source_as_of`` — never restamped); ``None`` on
    expiry or when the stored ``known_at`` is after ``cutoff_ms`` (future
    data must not score a past decision). ``put`` stores the identical
    object with an absolute ``expires_at_ms``.
    """

    def __init__(self, *, clock: Callable[[], int] | None = None) -> None:
        self._clock = clock or _default_clock_ms
        self._store: dict[Hashable, tuple[_obs.Observed[Any], int]] = {}
        self.hits = 0
        self.misses = 0

    def now_ms(self) -> int:
        return int(self._clock())

    def get(self, key: Hashable, cutoff_ms: int | None) -> _obs.Observed[Any] | None:
        row = self._store.get(key)
        if row is None:
            self.misses += 1
            return None
        observed, expires_at_ms = row
        now = self.now_ms()
        if int(expires_at_ms) <= now:
            self._store.pop(key, None)
            self.misses += 1
            return None
        if cutoff_ms is not None:
            known = observed.meta.known_at_ms
            if known is not None and int(known) > int(cutoff_ms):
                self.misses += 1
                return None
        self.hits += 1
        return observed

    def put(
        self, key: Hashable, observed: _obs.Observed[Any], expires_at_ms: int
    ) -> None:
        self._store[key] = (observed, int(expires_at_ms))

    def invalidate(self, key: Hashable) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._store)

    def stats(self) -> dict[str, Any]:
        return {"entries": len(self._store), "hits": self.hits, "misses": self.misses}
