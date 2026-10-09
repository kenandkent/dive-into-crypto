"""Binance USDT-M perpetual symbol universe, ranked by 24h quote volume.

Short-Lab Task 8 (design 10.2) additionally retains full in-memory contract
lifecycle metadata for every futures symbol ever seen in ``exchangeInfo`` --
including contracts that stop TRADING. The scannable universe
(:func:`list_universe` / :func:`perp_symbols`) still only exposes TRADING
perpetuals; :func:`contract_metadata_all` exposes everything ever observed so
risk gates and the forward grader can still find retired contracts. Durable
history belongs to Task 2's repository; this store is process memory only and
never touches the DB.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from diveintocrypto_desktop.data.http import FAPI_V1, LoopBoundLock, get_json
from diveintocrypto_desktop.shortlab import observations as _obs

# R03: additive request-context passthrough (D19). Legacy fakes patch
# ``universe.get_json`` as ``fake(url, params=None)`` without the keyword, so
# the keyword is only forwarded on the budgeted path (mirrors funding).
try:  # pragma: no cover - import guard for offline tooling
    from diveintocrypto_desktop.shortlab.request_budget import (
        RequestContext,
        get_current_request_context,
    )
except Exception:  # pragma: no cover
    RequestContext = Any  # type: ignore[assignment,misc]
    def get_current_request_context():  # type: ignore[no-redef]
        return None

# Stablecoin / fiat bases excluded from the scan (no directional edge).
_SKIP_BASES = {"USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "EUR", "GBP", "USTC"}

# exchangeInfo + all-symbol ticker costs ~41 request weight; cache the full
# universe briefly so /api/universe, /api/leaders AND scans share one fetch.
# The RAW ticker payload + perp map are cached alongside so dependent surfaces
# (sentiment/stablecoin proxy) reuse them with ZERO extra upstream calls.
_UNIVERSE_TTL = 30.0
_cache: list[dict] | None = None
_cache_ts: float = 0.0
_cache_lock = LoopBoundLock()

_raw_tickers: list[dict] | None = None
_raw_tickers_ts: float = 0.0
_perps: dict[str, dict] | None = None
_perps_ts: float = 0.0

# ── Task 8: in-memory contract lifecycle retention (design 10.2) ──────────
# Populated as a side effect of every successful exchangeInfo fetch; never
# pruned when a contract leaves the live universe. ``_metadata_history``
# keeps one snapshot per material change (status/contractType/onboard/
# delivery) so callers can see *when* a contract stopped trading.

DAY_MS = 86_400_000
NEW_TOKEN_DAYS_DEFAULT = 45  # design 24: veto.new_token_days
DELIVERY_VETO_WINDOW_MS = 7 * DAY_MS  # "deliveryDate ... 接近当前时间"
_METADATA_HISTORY_LIMIT = 64

# Reason codes consumed by Task 11 (risk/DQ). Stable machine strings.
LISTING_AGE_UNKNOWN = "LISTING_AGE_UNKNOWN"
PAUSE_NEW_TOKEN = "PAUSE_NEW_TOKEN"
VETO_CONTRACT_DELISTING = "VETO_CONTRACT_DELISTING"
PAUSE_CONTRACT_STATUS_UNVERIFIED = "PAUSE_CONTRACT_STATUS_UNVERIFIED"
# Design 10.2 body text spells the same pause without the PAUSE_ prefix.
CONTRACT_STATUS_UNVERIFIED = PAUSE_CONTRACT_STATUS_UNVERIFIED

# Exchange statuses that confirm a contract is leaving/has left trading
# (SETTLING/CLOSE/...) are a subset of "not TRADING". Per the Task 8 plan any
# explicit non-TRADING status is veto evidence; only the listing-flow family
# below is excepted (a pending listing is the opposite of a delisting and
# must never be misreported as one).
TRADABLE_STATUS = "TRADING"
# Listing-flow statuses: not tradable *yet*, but NOT evidence of delisting,
# so they must never produce VETO_CONTRACT_DELISTING.
PRE_TRADING_STATUSES = frozenset({"PENDING_TRADING", "PRE_TRADING"})
_NON_VETO_STATUSES = frozenset({TRADABLE_STATUS}) | PRE_TRADING_STATUSES

# Multiplier provenance accepted without a manual override (mirrors the
# Task 5 resolver rule: a bare "1000" prefix or a close spot price never
# verifies a multiplier). Anything else -> (None, None).
_TRUSTED_MULTIPLIER_SOURCES = ("EXCHANGE", "MANUAL")


@dataclass(frozen=True)
class ContractMetadata:
    """Point-in-time lifecycle record for one futures symbol (design 10.2).

    ``onboard_at_ms`` is the exchange ``onboardDate`` (None when missing or
    invalid); ``first_seen_ms`` is only the local first-observation time and
    must never be used to prove listing age. ``delivery_at_ms`` keeps the raw
    ``deliveryDate`` -- including far-future placeholders, which callers must
    ignore (see :func:`contract_termination_status`).
    """

    symbol: str
    onboard_at_ms: int | None
    first_seen_ms: int | None
    delivery_at_ms: int | None
    status: str | None
    contract_type: str | None
    observed_at_ms: int | None
    contract_multiplier: float | None
    multiplier_source: str | None  # EXCHANGE | MANUAL | None


_contract_metadata: dict[str, ContractMetadata] = {}
_metadata_history: dict[str, list[ContractMetadata]] = {}


def _to_valid_ms(value: Any) -> int | None:
    """Exchange ms timestamps: positive ints only (0/missing/garbage -> None)."""
    if isinstance(value, bool):
        return None
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    return iv if iv > 0 else None


def _extract_multiplier(entry: Mapping[str, Any]) -> tuple[float | None, str | None]:
    raw = entry.get("contract_multiplier")
    src = entry.get("multiplier_source")
    if src not in _TRUSTED_MULTIPLIER_SOURCES:
        return None, None
    try:
        m = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, None
    if not math.isfinite(m) or m <= 0:
        return None, None
    return m, src


def update_contract_metadata(
    symbols: Collection[Mapping[str, Any]], observed_at_ms: int
) -> dict[str, ContractMetadata]:
    """Merge one exchangeInfo ``symbols`` snapshot into the retention store.

    ``first_seen_ms`` is set to ``observed_at_ms`` on first sight and keeps
    the earliest value afterwards -- status changes never reset it. Returns a
    copy of the full store (including contracts no longer TRADING).
    """
    global _contract_metadata, _metadata_history
    for entry in symbols or []:
        if not isinstance(entry, Mapping):
            continue
        sym = entry.get("symbol")
        if not isinstance(sym, str) or not sym:
            continue
        onboard = _to_valid_ms(entry.get("onboardDate"))
        delivery = _to_valid_ms(entry.get("deliveryDate"))
        status = entry.get("status")
        status = status if isinstance(status, str) and status else None
        ctype = entry.get("contractType")
        ctype = ctype if isinstance(ctype, str) and ctype else None
        mult, msrc = _extract_multiplier(entry)
        prev = _contract_metadata.get(sym)
        if prev is not None and prev.first_seen_ms is not None:
            first_seen = min(prev.first_seen_ms, observed_at_ms)
        else:
            first_seen = observed_at_ms
        meta = ContractMetadata(
            symbol=sym,
            onboard_at_ms=onboard,
            first_seen_ms=first_seen,
            delivery_at_ms=delivery,
            status=status,
            contract_type=ctype,
            observed_at_ms=observed_at_ms,
            contract_multiplier=mult,
            multiplier_source=msrc,
        )
        if prev is None or (
            prev.status,
            prev.contract_type,
            prev.onboard_at_ms,
            prev.delivery_at_ms,
        ) != (status, ctype, onboard, delivery):
            hist = _metadata_history.setdefault(sym, [])
            hist.append(meta)
            del hist[: -_METADATA_HISTORY_LIMIT]
        _contract_metadata[sym] = meta
    return dict(_contract_metadata)


def contract_metadata_all() -> dict[str, ContractMetadata]:
    """Full retained metadata ``{symbol: ContractMetadata}`` (design 10.2).

    Includes stopped/non-TRADING contracts. This is the "all contracts"
    view; :func:`list_universe` remains the "scannable trading universe"
    view. Memory-only; durable history is Task 2's repository job.
    """
    return dict(_contract_metadata)


def contract_history(symbol: str) -> list[ContractMetadata]:
    """Material-change snapshots for one symbol, oldest first (memory-only)."""
    return list(_metadata_history.get(symbol, []))


def reset_contract_metadata() -> None:
    """Drop retained metadata + history (test hook)."""
    global _contract_metadata, _metadata_history
    _contract_metadata = {}
    _metadata_history = {}


def listing_age_status(
    meta: ContractMetadata | None,
    now_ms: int,
    new_token_days: int = NEW_TOKEN_DAYS_DEFAULT,
) -> tuple[str, tuple[str, ...]]:
    """Listing-age gate (design 10.2/17): ``PAUSE_NEW_TOKEN`` fires only on a
    valid ``onboardDate`` younger than ``new_token_days``. Without a valid
    onboard date the caller gets ``NOT_READY`` + ``LISTING_AGE_UNKNOWN`` --
    ``first_seen_ms`` never clears it (it only bounds local observation).
    """
    if meta is None or meta.onboard_at_ms is None:
        return "NOT_READY", (LISTING_AGE_UNKNOWN,)
    if now_ms - meta.onboard_at_ms < new_token_days * DAY_MS:
        return "PAUSED", (PAUSE_NEW_TOKEN,)
    return "OK", ()


def _is_near_delivery(delivery_at_ms: int | None, now_ms: int, window_ms: int) -> bool:
    if delivery_at_ms is None or window_ms < 0:
        return False
    # Far-future placeholders (perps use ~year-2100 sentinels) are years away
    # and never fall inside the window; only a delivery near *now* counts.
    return abs(delivery_at_ms - now_ms) <= window_ms


def contract_termination_status(
    symbol: str,
    live_symbols: Collection[str] | None,
    now_ms: int,
    metadata_all: Mapping[str, ContractMetadata] | None = None,
    delivery_window_ms: int = DELIVERY_VETO_WINDOW_MS,
) -> tuple[str, tuple[str, ...]]:
    """Delisting-vs-disappearance gate (design 10.2/16.2).

    - Explicit terminal ``status`` or a ``deliveryDate`` near ``now_ms`` ->
      ``BLOCKED`` + ``VETO_CONTRACT_DELISTING`` (confirmed).
    - Previously seen but missing from ``live_symbols`` with no confirmed
      terminal evidence -> ``PAUSED`` + ``PAUSE_CONTRACT_STATUS_UNVERIFIED``
      (never misreported as delisted). Missing exchange ``status`` also lands
      here -- it is never defaulted to TRADING.
    - Never seen and missing -> ``UNKNOWN`` + ``()`` (no claim at all).
    - In ``live_symbols`` with no veto evidence -> ``OK`` + ``()``.
    """
    store = metadata_all if metadata_all is not None else _contract_metadata
    meta = store.get(symbol)
    if meta is not None:
        if meta.status is not None and meta.status not in _NON_VETO_STATUSES:
            return "BLOCKED", (VETO_CONTRACT_DELISTING,)
        if _is_near_delivery(meta.delivery_at_ms, now_ms, delivery_window_ms):
            return "BLOCKED", (VETO_CONTRACT_DELISTING,)
    if live_symbols is not None and symbol in live_symbols:
        return "OK", ()
    if meta is not None:
        return "PAUSED", (PAUSE_CONTRACT_STATUS_UNVERIFIED,)
    return "UNKNOWN", ()


def reset_universe_cache() -> None:
    """Drop the cached universe + shared raw payload (test hook)."""
    global _cache, _cache_ts, _raw_tickers, _raw_tickers_ts, _perps, _perps_ts
    _cache = None
    _cache_ts = 0.0
    _raw_tickers = None
    _raw_tickers_ts = 0.0
    _perps = None
    _perps_ts = 0.0
    reset_contract_metadata()


async def _budgeted_get_json(
    url: str, params: dict[str, Any] | None, ctx: Any | None
) -> Any:
    """Call ``get_json`` preserving legacy fake signatures (R03 compat).

    Legacy test doubles patch ``universe.get_json`` as
    ``fake(url, params=None)`` without the additive ``request_context`` kwarg.
    Only pass the kwarg when a real budget is present; otherwise use the exact
    legacy call shape. The context (incl. future job_id/deadline_ms) is
    forwarded verbatim to the HTTP layer -- never re-charged here.
    """
    if ctx is not None and getattr(ctx, "budget", None) is not None:
        return await get_json(url, params, request_context=ctx)
    if params is None:
        return await get_json(url)
    return await get_json(url, params)


def _resolve_universe_context(request_context: Any | None) -> Any | None:
    if request_context is not None:
        return request_context
    try:
        return get_current_request_context()
    except Exception:
        return None


def _ticker_close_time_ms(ticker: Any) -> int | None:
    """Source ``closeTime`` of one ``ticker/24hr`` row (R03/D04.3).

    ``None`` when absent/invalid -- never the local clock.
    """
    if not isinstance(ticker, dict):
        return None
    for key in ("closeTime", "close_time", "close_time_ms"):
        raw = ticker.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _universe_source_as_of(tickers: Any) -> int | None:
    """Max retained ``closeTime`` across tickers (receipt source time)."""
    best: int | None = None
    if isinstance(tickers, (list, tuple)):
        for t in tickers:
            ms = _ticker_close_time_ms(t)
            if ms is not None and (best is None or ms > best):
                best = ms
    return best


async def perp_symbols(
    *, request_context: Any | None = None
) -> dict[str, dict[str, Any]]:
    """Tradable USDT perps ``{symbol: {base, quote}}`` (30s TTL cache)."""
    global _perps, _perps_ts
    now = time.monotonic()
    if _perps is None or now - _perps_ts >= _UNIVERSE_TTL:
        async with _cache_lock:
            now = time.monotonic()
            if _perps is None or now - _perps_ts >= _UNIVERSE_TTL:
                _perps = await _perp_symbols(request_context=request_context)
                _perps_ts = now
    return _perps or {}


async def all_tickers(
    *, request_context: Any | None = None
) -> list[dict]:
    """RAW ``ticker/24hr`` payload (30s TTL cache, shared with the universe)."""
    global _raw_tickers, _raw_tickers_ts
    now = time.monotonic()
    if _raw_tickers is None or now - _raw_tickers_ts >= _UNIVERSE_TTL:
        async with _cache_lock:
            now = time.monotonic()
            if _raw_tickers is None or now - _raw_tickers_ts >= _UNIVERSE_TTL:
                ctx = _resolve_universe_context(request_context)
                _raw_tickers = await _budgeted_get_json(f"{FAPI_V1}/ticker/24hr", None, ctx)
                _raw_tickers_ts = now
    return _raw_tickers or []


async def _perp_symbols(
    *, request_context: Any | None = None
) -> dict[str, dict[str, Any]]:
    ctx = _resolve_universe_context(request_context)
    info: dict[str, Any] = await _budgeted_get_json(f"{FAPI_V1}/exchangeInfo", None, ctx)
    raw = info.get("symbols") or []
    # Retain FULL lifecycle metadata (all statuses/contract types) before the
    # TRADING-perp filter below narrows the scannable universe.
    update_contract_metadata(raw, int(time.time() * 1000))
    out: dict[str, dict[str, Any]] = {}
    for s in raw:
        if (
            s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("baseAsset") not in _SKIP_BASES
        ):
            out[s["symbol"]] = {"base": s["baseAsset"], "quote": s["quoteAsset"]}
    return out


async def _fetch_universe(
    *, request_context: Any | None = None
) -> list[dict]:
    ctx = _resolve_universe_context(request_context)
    # Forward the same context to both legs; the HTTP layer charges once per
    # real send (no duplicate budgeting here). Legacy fakes without the kwarg
    # are preserved via _budgeted_get_json.
    perps_coro = _perp_symbols(request_context=ctx)
    ticker_coro = _budgeted_get_json(f"{FAPI_V1}/ticker/24hr", None, ctx)
    perps, tickers = await asyncio.gather(perps_coro, ticker_coro)
    # stash the raw payload + perp map for dependent surfaces (0 extra calls)
    global _raw_tickers, _raw_tickers_ts, _perps, _perps_ts
    _raw_tickers, _raw_tickers_ts = tickers, time.monotonic()
    _perps, _perps_ts = perps, time.monotonic()

    rows: list[dict] = []
    for t in tickers:
        if not isinstance(t, dict):
            continue
        sym = t.get("symbol")
        if sym not in perps:
            continue
        # R03/D04.3: retain source closeTime per ticker (receipt source time).
        # Legacy keys unchanged; closeTime is additive for to_legacy compat.
        close_ms = _ticker_close_time_ms(t)
        rows.append(
            {
                "s": sym,
                "name": perps[sym]["base"],
                "price": float(t["lastPrice"]),
                "ch": float(t["priceChangePercent"]),
                "quote_volume": float(t["quoteVolume"]),
                "closeTime": close_ms,
            }
        )
    rows.sort(key=lambda r: r["quote_volume"], reverse=True)
    return rows


async def list_universe(
    limit: int | None = None, *, request_context: Any | None = None
) -> list[dict]:
    """Return ``[{symbol, name, price, ch, quote_volume}]`` sorted by 24h quote
    volume (desc), served from a 30s TTL cache. ``name`` falls back to the base
    asset. Errors are never cached — a failed refresh propagates and the next
    call retries.

    R03: rows additionally carry source ``closeTime`` (additive); legacy
    callers ignore it via :func:`to_legacy` compat.
    """
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache is None or now - _cache_ts >= _UNIVERSE_TTL:
        async with _cache_lock:
            now = time.monotonic()
            if _cache is None or now - _cache_ts >= _UNIVERSE_TTL:
                # Only forward the context when explicitly provided; legacy
                # callers (and their _fetch_universe mocks) keep the old shape.
                if request_context is None:
                    _cache = await _fetch_universe()  # raises propagate; cache untouched
                else:
                    _cache = await _fetch_universe(request_context=request_context)
                _cache_ts = now
    rows = _cache or []
    return rows[:limit] if limit else rows


# ---------------------------------------------------------------------------
# F02 observation wrappers (design A4.1/A4.3, B16.1; plan F02.1).
#
# Legacy readers above keep their exact signatures and return types. The
# ``*_observed`` adapters below wrap the same results in
# ``shortlab.observations.Observed`` with ``known_at_ms`` set to the
# response-completion time; a cache hit returns the identical ``Observed``
# (original ``known_at``/``source_as_of``), never a hit-time restamp.
# The raw exchangeInfo payload (original ``filters``/``time``) is stored
# verbatim in the observation value -- no second parser is built here
# (rule parsing stays H02's job).
# ---------------------------------------------------------------------------

# Raw exchangeInfo kept 30 minutes per B16.1 (rules-snapshot refresh cadence).
_EXCHANGE_INFO_OBSERVED_TTL = 1800.0
_UNIVERSE_SOURCE = "binance-futures-universe"
_EXCHANGE_INFO_SOURCE = "binance-futures-exchangeInfo"

_exchange_info_observed: _obs.Observed[dict] | None = None
_exchange_info_observed_mono: float = 0.0
_universe_observed: _obs.Observed[list[dict]] | None = None
_universe_observed_mono: float = 0.0


def reset_observation_cache() -> None:
    """Drop the F02 ``Observed`` caches (test hook; legacy caches untouched)."""
    global _exchange_info_observed, _exchange_info_observed_mono
    global _universe_observed, _universe_observed_mono
    _exchange_info_observed = None
    _exchange_info_observed_mono = 0.0
    _universe_observed = None
    _universe_observed_mono = 0.0


def _exchange_server_time(payload: Any) -> int | None:
    try:
        raw = payload.get("serverTime") if isinstance(payload, dict) else None
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def fetch_exchange_info_observed(
    *,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: Any | None = None,
) -> _obs.Observed[dict]:
    """Raw ``exchangeInfo`` payload wrapped as an ``Observed`` (F02/B16.1).

    ``value`` is the verbatim payload (original per-symbol ``filters`` and
    ``serverTime`` preserved); ``meta.source_as_of_ms`` is ``serverTime``
    (``None`` when the venue omits it -- never the local clock).
    R03: accepts keyword ``request_context`` forwarded to the HTTP layer
    (no duplicate budgeting here).
    """
    global _exchange_info_observed, _exchange_info_observed_mono
    now_mono = time.monotonic()
    if (
        _exchange_info_observed is not None
        and now_mono - _exchange_info_observed_mono < _EXCHANGE_INFO_OBSERVED_TTL
    ):
        return _exchange_info_observed
    async with _cache_lock:
        now_mono = time.monotonic()
        if (
            _exchange_info_observed is not None
            and now_mono - _exchange_info_observed_mono < _EXCHANGE_INFO_OBSERVED_TTL
        ):
            return _exchange_info_observed
        ctx = _resolve_universe_context(request_context)
        payload: dict[str, Any] = await _budgeted_get_json(f"{FAPI_V1}/exchangeInfo", None, ctx)
        completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        raw_symbols = payload.get("symbols") or []
        update_contract_metadata(raw_symbols, completed)
        observed = _obs.make_observation(
            payload,
            source=_EXCHANGE_INFO_SOURCE,
            source_as_of_ms=_exchange_server_time(payload),
            fetched_at_ms=completed,
            known_at_ms=completed,
            units=_obs.ObservationUnits(quote_asset="USDT"),
            identity_snapshot_id=identity_snapshot_id,
        )
        _exchange_info_observed = observed
        _exchange_info_observed_mono = time.monotonic()
        return observed


async def fetch_universe_observed(
    limit: int | None = None,
    *,
    as_of_ms: int | None = None,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: Any | None = None,
) -> _obs.Observed[list[dict]]:
    """Ranked universe rows wrapped as an ``Observed`` (F02/R03).

    Mirrors the 30s legacy TTL: a cache hit returns the identical
    ``Observed`` with its original ``known_at_ms``. ``as_of_ms`` is
    accepted for the downstream cutoff check only.
    R03/D04.3: each row retains source ``closeTime``; ``meta.source_as_of_ms``
    is the max retained ``closeTime`` (``None`` when the venue omits it).
    ``value`` via :func:`to_legacy` stays API-compatible.
    """
    _ = as_of_ms  # decision cutoff is enforced downstream via validate_observation
    global _universe_observed, _universe_observed_mono
    now_mono = time.monotonic()
    if (
        _universe_observed is not None
        and now_mono - _universe_observed_mono < _UNIVERSE_TTL
    ):
        return _slice_universe_observed(_universe_observed, limit)
    async with _cache_lock:
        now_mono = time.monotonic()
        if (
            _universe_observed is not None
            and now_mono - _universe_observed_mono < _UNIVERSE_TTL
        ):
            return _slice_universe_observed(_universe_observed, limit)
        ctx = _resolve_universe_context(request_context)
        if ctx is None:
            rows = await _fetch_universe()  # raises propagate; observed cache untouched
        else:
            rows = await _fetch_universe(request_context=ctx)
        completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        # R03: retain per-ticker closeTime as the source time (receipt).
        source_as_of = _universe_source_as_of(rows)
        if source_as_of is None and _raw_tickers:
            source_as_of = _universe_source_as_of(_raw_tickers)
        _universe_observed = _obs.make_observation(
            rows,
            source=_UNIVERSE_SOURCE,
            source_as_of_ms=source_as_of,
            fetched_at_ms=completed,
            known_at_ms=completed,
            units=_obs.ObservationUnits(quote_asset="USDT"),
            identity_snapshot_id=identity_snapshot_id,
        )
        _universe_observed_mono = time.monotonic()
        return _slice_universe_observed(_universe_observed, limit)


def _slice_universe_observed(
    observed: _obs.Observed[list[dict]], limit: int | None
) -> _obs.Observed[list[dict]]:
    """Apply ``limit`` without restamping: slices share the cached ``meta``."""
    if not limit:
        return observed
    return _obs.Observed(value=observed.value[:limit], meta=observed.meta)
