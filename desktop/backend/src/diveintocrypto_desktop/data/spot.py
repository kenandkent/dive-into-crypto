"""Spot-perp lead/lag (Binance SPOT market data — a DIFFERENT host).

Perp prices lead spot for some pairs and trail for others; this module measures
which. From the spot exchange (``https://api.binance.com``, override with
``DIVE_SPOT_BASE``) it pulls the spot 1h kline tail (48 bars) + 24h ticker and
pairs them with the perp data the caller already has:

  * ``premium_pct``  — perp price premium over spot, percent;
  * ``ret_spread_48h`` — perp 48h return minus spot 48h return;
  * ``lead``         — ``spot`` | ``perp`` | ``mixed`` from comparing lag-1
    cross-correlations (spot→perp vs perp→spot); |diff| inside ``LEAD_MARGIN``
    is honest ``mixed``.

Perp-only listings are the NORMAL state here, not an error: the snapshot is
``{"unavailable": "no_spot_market"}`` (Binance answers -1121 Invalid symbol).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import os
import time
from typing import Any

from diveintocrypto_desktop.data.http import FAPI_V1, get_json
from diveintocrypto_desktop.shortlab.request_budget import (
    RequestContext,
    get_current_request_context,
    scoped_request_context,
)

logger = logging.getLogger("trading_bot.data.spot")

SPOT_BASE = os.environ.get("DIVE_SPOT_BASE", "https://api.binance.com").rstrip("/")
SPOT_V3 = f"{SPOT_BASE}/api/v3"

KLINE_LIMIT = 48
LEAD_MARGIN = 0.05
CACHE_TTL = 60.0

_cache: dict[str, tuple[float, dict]] = {}


def reset_cache() -> None:
    """Drop the snapshot cache (test hook)."""
    _cache.clear()


async def _spot_json(path: str, params: dict[str, Any]) -> Any:
    """GET one Spot document, forwarding the ambient request context.

    ``snapshot`` / ``spot_history`` install their (explicit or ambient)
    ``RequestContext`` for the duration of the fetch via
    :func:`_maybe_scope`, so this two-argument shape -- relied on by legacy
    test doubles -- never changes. ``request_context`` is always forwarded
    (possibly None); budgeted sends resolve the ``spot``/``exchangeInfo``
    families from the shared F03 weights fixture, and a denied send raises
    :class:`BudgetExhausted` for the caller to encapsulate as UNAVAILABLE.
    """
    return await get_json(
        f"{SPOT_V3}{path}", params, request_context=get_current_request_context()
    )


def _resolve_spot_context(request_context: RequestContext | None) -> RequestContext | None:
    """Explicit context wins over ambient; a budgeted context without a
    family defaults to the versioned ``spot`` family (exchangeInfo resolves
    to ``exchangeInfo`` by URL)."""
    ctx = request_context if request_context is not None else get_current_request_context()
    if ctx is not None and ctx.budget is not None and ctx.endpoint_family is None:
        ctx = dataclasses.replace(ctx, endpoint_family="spot")
    return ctx


@contextlib.contextmanager
def _maybe_scope(ctx: RequestContext | None):
    """Install ``ctx`` as the ambient request context (no-op when None)."""
    if ctx is None:
        yield
    else:
        with scoped_request_context(ctx):
            yield


def log_returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    for a, b in zip(closes, closes[1:]):
        if a > 0 and b > 0:
            out.append(math.log(b / a))
    return out


def _corr(a: list[float], b: list[float]) -> float | None:
    m = len(a)
    ma, mb = sum(a) / m, sum(b) / m
    da = [x - ma for x in a]
    db = [x - mb for x in b]
    va = sum(x * x for x in da)
    vb = sum(x * x for x in db)
    if va <= 0 or vb <= 0:
        return None
    return sum(x * y for x, y in zip(da, db)) / (va * vb) ** 0.5


def classify_lead(spot_rets: list[float], perp_rets: list[float],
                  margin: float = LEAD_MARGIN) -> str | None:
    """``spot`` | ``perp`` | ``mixed`` from lag-1 cross-correlation asymmetry.

    corr(spot[t], perp[t+1]) > corr(perp[t], spot[t+1]) + margin ⇒ spot moves
    first. Needs ≥ 10 paired returns; degenerate (zero-variance) series → None.
    """
    n = min(len(spot_rets), len(perp_rets)) - 1
    if n < 10:
        return None
    s, p = spot_rets, perp_rets
    c_sp = _corr(s[:n], p[1:n + 1])
    c_ps = _corr(p[:n], s[1:n + 1])
    if c_sp is None or c_ps is None:
        return None
    if c_sp - c_ps > margin:
        return "spot"
    if c_ps - c_sp > margin:
        return "perp"
    return "mixed"


def parse_kline_closes(rows: list) -> list[float]:
    closes: list[float] = []
    for r in rows or []:
        try:
            v = float(r[4])
        except (IndexError, TypeError, ValueError):
            continue
        if v > 0:
            closes.append(v)
    return closes


def spot_perp_block(spot_closes: list[float], spot_price: float | None,
                    perp_price: float, perp_closes: list[float]) -> dict:
    """Assemble the ``spot_perp`` block (pure)."""
    premium = None
    if spot_price and spot_price > 0 and perp_price > 0:
        premium = round((perp_price - spot_price) / spot_price * 100.0, 4)

    spread = None
    lead = None
    s_rets = log_returns(spot_closes)
    p_rets = log_returns(perp_closes)
    if len(spot_closes) >= 2 and spot_closes[0] > 0 and len(perp_closes) >= 2 and perp_closes[0] > 0:
        spread = round(
            (perp_closes[-1] / perp_closes[0] - 1.0) - (spot_closes[-1] / spot_closes[0] - 1.0), 6
        )
        lead = classify_lead(s_rets, p_rets)
    if premium is None and spread is None and lead is None:
        return {"unavailable": "insufficient_history"}
    return {
        "premium_pct": premium,
        "ret_spread_48h": spread,
        "lead": lead,
    }


async def snapshot(
    symbol: str,
    perp_price: float,
    perp_closes: list[float],
    *,
    request_context: RequestContext | None = None,
) -> dict:
    """Cached ``spot_perp`` block for ``symbol`` (60s TTL on SUCCESS only).

    ``{"unavailable": "no_spot_market"}`` for perp-only listings (normal state);
    other failures report their reason. Errors and unavailable states are never
    cached — every call after a failure re-attempts upstream (so a listing that
    gains a spot market is picked up on the very next call, not an hour later).

    ``request_context`` (F04, additive) travels to ``get_json`` for budget /
    trace propagation; ``None`` preserves the legacy unbounded path.
    """
    now = time.monotonic()
    cached = _cache.get(symbol)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    ctx = _resolve_spot_context(request_context)
    try:
        with _maybe_scope(ctx):
            klines, ticker = await asyncio.gather(
                _spot_json("/klines", {"symbol": symbol, "interval": "1h", "limit": KLINE_LIMIT}),
                _spot_json("/ticker/24hr", {"symbol": symbol}),
            )
        closes = parse_kline_closes(klines)
        spot_price = None
        try:
            spot_price = float(ticker.get("lastPrice"))
        except (KeyError, TypeError, ValueError):
            pass
        block = spot_perp_block(closes, spot_price, perp_price, perp_closes)
    except Exception as e:
        text = str(e)
        if "-1121" in text or "Invalid symbol" in text:
            block = {"unavailable": "no_spot_market"}
        else:
            logger.warning("spot: %s fetch failed — %s", symbol, text[:80])
            block = {"unavailable": f"spot_unreachable: {text[:80]}"}
    if "unavailable" in block:
        return block  # never cached — re-attempted on the next call
    _cache[symbol] = (time.monotonic(), block)
    return block


# ---------------------------------------------------------------------------
# Short-Lab Spot 60D history (Task 6; design §5.1 / §4.2 / §4.4).
#
# Extends this module — no separate Short-Lab spot client — with
# ``spot_history(identity, as_of_ms)``. It fetches Spot ``1d`` klines for the
# 60 closed UTC days ending at ``as_of`` (30 + 30), the same UTC day window a
# futures caller uses, and returns quote-volume (Binance raw index 7, a.k.a.
# Task 4 ``qv``) sums — never base volume (index 5).
#
# Unit rules (design §4.2): volumes and OI are already quote-notional and are
# NEVER scaled by ``contract_multiplier``; only prices are normalised via
# ``canonical_price = futures_close / contract_multiplier`` for ``premium``
# (decimal, §4.4). A missing/unverified multiplier leaves ``premium`` null.
#
# Status rules: the Spot ``exchangeInfo`` symbol list is the ONLY evidence
# that may mark a market NOT_APPLICABLE/``no_spot_market``. Spot 451 / 429 /
# timeout (and an unconfirmed ``-1121``) are UNAVAILABLE and never write the
# negative cache. ``snapshot()`` above is untouched (48 × 1h, 60s TTL).
# ---------------------------------------------------------------------------

import re
from dataclasses import dataclass

from diveintocrypto_desktop.shortlab import observations as _obs
from diveintocrypto_desktop.shortlab.models import ProviderResult, sanitize_error_message

DAY_MS = 86_400_000
SPOT_DAILY_LIMIT = 61  # 60 closed UTC days + headroom for one unclosed bar
SPOT_WINDOW_DAYS = 60
SPOT_HALF_DAYS = 30

EXCHANGE_INFO_POSITIVE_TTL = 3600.0  # successful symbol list, per spot host
EXCHANGE_INFO_NEGATIVE_TTL = 900.0  # confirmed-absent (host, symbol)

NO_SPOT_MARKET = "no_spot_market"
SPOT_UNREACHABLE = "spot_unreachable"
SPOT_SYMBOL_UNCONFIRMED = "spot_symbol_unconfirmed"
SPOT_HISTORY_INCOMPLETE = "spot_history_incomplete"
FUTURES_LEG_UNREACHABLE = "futures_leg_unreachable"

_UNAVAILABLE_HTTP_STATUS = frozenset({451, 429})
_RE_UNKNOWN_SYMBOL = re.compile(r"-1121|invalid symbol", re.IGNORECASE)


@dataclass(frozen=True)
class SpotHistory:
    """60 closed-UTC-day Spot history with the futures leg at the same cutoff.

    All volumes are quote USD/USDT notional (Binance kline raw index 7);
    ``premium`` is a decimal (design §4.4), normalised by the verified
    ``contract_multiplier`` when available. Null means N/A / unknown — never 0.
    """

    spot_symbol: str | None
    futures_symbol: str | None
    as_of_ms: int
    window_start_ms: int  # first covered UTC day open (inclusive)
    window_end_ms: int  # UTC midnight cutoff (exclusive; unclosed day excluded)
    spot_volume_30d: float | None  # last 30 closed UTC days quote volume
    spot_volume_prev_30d: float | None  # the 30 closed UTC days before that
    spot_volume_decay_30d: float | None  # spot_volume_30d / prev (None if N/A)
    spot_quote_volume_24h: float | None  # last closed UTC day qv (not rolling)
    spot_price: float | None  # last closed UTC day close
    futures_volume_30d: float | None  # same last-30-day window, futures qv
    futures_spot_volume_ratio_30d: float | None  # futures_30d / spot_30d
    premium: float | None  # (canonical_futures - spot) / spot, decimal
    canonical_futures_price: float | None
    multiplier_applied: float | None
    spot_daily_bars: int = 0  # closed spot dailies inside the window
    futures_daily_bars: int = 0  # closed futures dailies inside the window


# Full-list cache per spot host: host -> (fetched_monotonic, {SYMBOLS}).
_exchange_info_cache: dict[str, tuple[float, set[str]]] = {}
# Confirmed-absent cache: (host, symbol) -> expiry_monotonic.
_negative_cache: dict[tuple[str, str], float] = {}
# One in-flight exchangeInfo fetch per event loop so a concurrent batch of
# assets shares a single upstream request. Keyed by the loop OBJECT (never
# id()): a collected loop's id may be reused by a new loop, which would hand
# the new loop a lock bound to a dead loop. Closed loops are pruned on entry.
_exchange_info_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def reset_spot_history_cache() -> None:
    """Drop the Task 6 exchangeInfo positive/negative caches (test hook)."""
    _exchange_info_cache.clear()
    _negative_cache.clear()
    _exchange_info_locks.clear()


def _spot_api_root() -> str:
    """Current Spot API root (read per call so host switches are honoured)."""
    return SPOT_V3


def _exchange_info_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    for known in [k for k in _exchange_info_locks]:
        if known is not loop and known.is_closed():
            _exchange_info_locks.pop(known, None)
    lock = _exchange_info_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _exchange_info_locks[loop] = lock
    return lock


def _spot_window_ms(as_of_ms: int) -> tuple[int, int]:
    """``[start, end)`` of the 60 closed UTC days ending at ``as_of_ms``.

    ``end`` is the UTC midnight at/before ``as_of_ms``; the still-open current
    UTC day is always excluded however intraday ``as_of_ms`` is.
    """
    day_start = (int(as_of_ms) // DAY_MS) * DAY_MS
    return (day_start - SPOT_WINDOW_DAYS * DAY_MS, day_start)


def _classify_spot_error(exc: BaseException) -> str:
    """``unknown_symbol`` for -1121/Invalid-symbol, else ``unavailable``.

    Every failure in the Spot path (451, 429, timeout, network, unexpected)
    is encapsulated as UNAVAILABLE; only an *exchangeInfo-confirmed* absence
    may become NOT_APPLICABLE, decided by the caller — never here.
    """
    text = str(exc)
    if _RE_UNKNOWN_SYMBOL.search(text):
        status = getattr(exc, "status", None)
        if status is None or int(status) not in _UNAVAILABLE_HTTP_STATUS:
            return "unknown_symbol"
    return "unavailable"


async def _fetch_spot_exchange_symbols() -> set[str]:
    """Full Spot symbol list for existence confirmation (one upstream call)."""
    payload = await _spot_json("/exchangeInfo", {})
    rows = payload.get("symbols", []) if isinstance(payload, dict) else []
    out: set[str] = set()
    for entry in rows:
        if isinstance(entry, dict):
            sym = entry.get("symbol")
            if isinstance(sym, str) and sym:
                out.add(sym.upper())
    return out


async def _cached_spot_symbols() -> set[str]:
    """exchangeInfo symbols, positive-cached 3600s per host, fetch-shared."""
    host = _spot_api_root()
    now = time.monotonic()
    entry = _exchange_info_cache.get(host)
    if entry is not None and now - entry[0] < EXCHANGE_INFO_POSITIVE_TTL:
        return entry[1]
    lock = _exchange_info_lock()
    async with lock:
        now = time.monotonic()
        entry = _exchange_info_cache.get(host)
        if entry is not None and now - entry[0] < EXCHANGE_INFO_POSITIVE_TTL:
            return entry[1]
        symbols = await _fetch_spot_exchange_symbols()
        _exchange_info_cache[host] = (time.monotonic(), symbols)
        return symbols


def _negative_hit(host: str, symbol: str) -> bool:
    expiry = _negative_cache.get((host, symbol))
    return expiry is not None and time.monotonic() < expiry


def _remember_absent(host: str, symbol: str) -> None:
    _negative_cache[(host, symbol)] = time.monotonic() + EXCHANGE_INFO_NEGATIVE_TTL


def _parse_spot_daily(
    rows: Any, window_start_ms: int, window_end_ms: int
) -> dict[int, dict[str, float | None]]:
    """Raw Spot ``1d`` klines -> ``{open_ms: {close, qv}}`` inside the window.

    Uses quote volume (raw index 7); base volume (index 5) is never read. Rows
    outside ``[start, end)`` or still unclosed at the cutoff are dropped; a
    repeated open time is ambiguous so that day's ``qv`` becomes None.
    """
    by_open: dict[int, dict[str, float | None]] = {}
    for r in rows or []:
        try:
            open_ms = int(r[0])
        except (IndexError, TypeError, ValueError):
            continue
        if not (window_start_ms <= open_ms < window_end_ms):
            continue
        try:
            close_ms = int(r[6])
        except (IndexError, TypeError, ValueError):
            close_ms = None
        if close_ms is not None and close_ms >= window_end_ms:
            continue  # unclosed as of the cutoff (e.g. today's bar)
        try:
            close = float(r[4])
        except (IndexError, TypeError, ValueError):
            close = None
        try:
            qv = float(r[7])
        except (IndexError, TypeError, ValueError):
            qv = None
        if open_ms in by_open:
            by_open[open_ms] = {"close": by_open[open_ms]["close"], "qv": None}
            continue
        by_open[open_ms] = {"close": close, "qv": qv}
    return by_open


def _sum_qv(by_open: dict[int, dict[str, float | None]], opens: list[int]) -> float | None:
    """Quote-volume sum over exact day opens; None unless all 30 legs resolve."""
    total = 0.0
    for open_ms in opens:
        bar = by_open.get(open_ms)
        if bar is None or bar.get("qv") is None:
            return None
        total += float(bar["qv"])
    return total


def _last_close(by_open: dict[int, dict[str, float | None]], opens: list[int]) -> float | None:
    for open_ms in reversed(opens):
        bar = by_open.get(open_ms)
        if bar is not None and bar.get("close") is not None:
            close = float(bar["close"])
            if close > 0:
                return close
    return None


def _verified_multiplier(identity: Any) -> float | None:
    """Multiplier only with an explicit EXCHANGE/MANUAL source (design §4.2)."""
    mult = getattr(identity, "contract_multiplier", None)
    source = getattr(identity, "multiplier_source", None)
    try:
        value = float(mult) if mult is not None else None
    except (TypeError, ValueError):
        return None
    if value is None or not value > 0 or source is None:
        return None
    return value


async def spot_history(
    identity: Any, as_of_ms: int, *, request_context: RequestContext | None = None
) -> ProviderResult[SpotHistory]:
    """60D Spot history for a verified identity at the same UTC-day cutoff.

    ``identity`` is duck-typed (Task 1 ``shortlab.models.AssetIdentity`` or
    Task 5 resolver identity): only ``binance_spot_symbol``,
    ``binance_futures_symbol``, ``contract_multiplier`` / ``multiplier_source``
    are read. The Spot API symbol ALWAYS comes from
    ``identity.binance_spot_symbol`` — the futures symbol is never stripped or
    guessed. ``as_of_ms`` is a UTC epoch-ms truncation point; only the 60 UTC
    days closed before it are used.

    ``request_context`` (F04, additive) travels to ``get_json`` for budget /
    trace propagation; a denied send is encapsulated as UNAVAILABLE (never
    NOT_APPLICABLE, never raised). ``None`` preserves legacy behaviour.
    """
    as_of_ms = int(as_of_ms)
    now_ms = int(time.time() * 1000)
    ctx = _resolve_spot_context(request_context)
    raw_spot = getattr(identity, "binance_spot_symbol", None)
    spot_symbol = raw_spot.strip().upper() if isinstance(raw_spot, str) else None
    if not spot_symbol:
        return ProviderResult(
            status="NOT_APPLICABLE",
            source="binance-spot",
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms,
            data=None,
            stale=False,
            reason_code=NO_SPOT_MARKET,
            error_message="identity carries no verified binance_spot_symbol; "
            "the futures symbol is never guessed as a spot symbol",
        )

    raw_fut = getattr(identity, "binance_futures_symbol", None)
    futures_symbol = raw_fut.strip().upper() if isinstance(raw_fut, str) else None
    window_start, window_end = _spot_window_ms(as_of_ms)
    opens = [window_start + i * DAY_MS for i in range(SPOT_WINDOW_DAYS)]
    prev_opens, last_opens = opens[:SPOT_HALF_DAYS], opens[SPOT_HALF_DAYS:]
    host = _spot_api_root()

    if _negative_hit(host, spot_symbol):
        return ProviderResult(
            status="NOT_APPLICABLE",
            source="binance-spot",
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms,
            data=None,
            stale=False,
            reason_code=NO_SPOT_MARKET,
            error_message=None,
        )

    try:
        with _maybe_scope(ctx):
            symbols = await _cached_spot_symbols()
    except Exception as e:  # noqa: BLE001 — encapsulated as UNAVAILABLE
        return ProviderResult(
            status="UNAVAILABLE",
            source="binance-spot",
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms,
            data=None,
            stale=False,
            reason_code=SPOT_UNREACHABLE,
            error_message=sanitize_error_message(f"spot exchangeInfo failed: {str(e)[:120]}"),
        )
    if spot_symbol not in symbols:
        # The ONLY path to NOT_APPLICABLE for a string symbol: confirmed by
        # Spot exchangeInfo metadata (a lone -1121 never suffices).
        _remember_absent(host, spot_symbol)
        return ProviderResult(
            status="NOT_APPLICABLE",
            source="binance-spot",
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms,
            data=None,
            stale=False,
            reason_code=NO_SPOT_MARKET,
            error_message=None,
        )

    try:
        with _maybe_scope(ctx):
            spot_rows = await _spot_json(
                "/klines",
                {
                    "symbol": spot_symbol,
                    "interval": "1d",
                    "limit": SPOT_DAILY_LIMIT,
                    "endTime": window_end - 1,
                },
            )
    except Exception as e:  # noqa: BLE001 — encapsulated as UNAVAILABLE
        kind = _classify_spot_error(e)
        return ProviderResult(
            status="UNAVAILABLE",
            source="binance-spot",
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms,
            data=None,
            stale=False,
            reason_code=SPOT_SYMBOL_UNCONFIRMED if kind == "unknown_symbol" else SPOT_UNREACHABLE,
            error_message=sanitize_error_message(f"spot daily klines failed: {str(e)[:120]}"),
        )

    spot_bars = _parse_spot_daily(spot_rows, window_start, window_end)
    spot_30d = _sum_qv(spot_bars, last_opens)
    spot_prev_30d = _sum_qv(spot_bars, prev_opens)
    decay = None
    if spot_30d is not None and spot_prev_30d is not None and spot_prev_30d > 0:
        decay = spot_30d / spot_prev_30d
    spot_24h = _sum_qv(spot_bars, opens[-1:])
    spot_price = _last_close(spot_bars, opens)

    futures_30d: float | None = None
    futures_bars: dict[int, dict[str, float | None]] = {}
    futures_ok = True
    futures_error: str | None = None
    if futures_symbol:
        try:
            from diveintocrypto_desktop.data import binance_klines as futures_klines

            # R03: forward the caller's request_context to the futures daily leg
            # (tolerates legacy fakes without the keyword).
            try:
                candles = await futures_klines.fetch_klines(
                    futures_symbol, "1d", limit=SPOT_DAILY_LIMIT, end_ms=window_end,
                    request_context=request_context,
                )
            except TypeError:
                candles = await futures_klines.fetch_klines(
                    futures_symbol, "1d", limit=SPOT_DAILY_LIMIT, end_ms=window_end
                )
            for c in candles or []:
                try:
                    open_ms = int(c["t"]) // 1_000_000
                except (KeyError, TypeError, ValueError):
                    continue
                if window_start <= open_ms < window_end and open_ms not in futures_bars:
                    try:
                        fq = None if c.get("qv") is None else float(c["qv"])
                    except (TypeError, ValueError):
                        fq = None
                    try:
                        fc = None if c.get("c") is None else float(c["c"])
                    except (TypeError, ValueError):
                        fc = None
                    futures_bars[open_ms] = {"close": fc, "qv": fq}
            futures_30d = _sum_qv(futures_bars, last_opens)
        except Exception as e:  # noqa: BLE001 — futures leg degrades to PARTIAL
            futures_ok = False
            futures_error = sanitize_error_message(f"futures daily leg failed: {str(e)[:120]}")
    else:
        futures_ok = False
        futures_error = "identity carries no binance_futures_symbol"

    ratio = None
    if futures_30d is not None and spot_30d is not None and spot_30d > 0:
        ratio = futures_30d / spot_30d

    multiplier = _verified_multiplier(identity)
    canonical: float | None = None
    premium: float | None = None
    futures_close = _last_close(futures_bars, opens) if futures_bars else None
    if multiplier is not None and futures_close is not None and spot_price is not None and spot_price > 0:
        canonical = futures_close / multiplier
        premium = (canonical - spot_price) / spot_price

    history = SpotHistory(
        spot_symbol=spot_symbol,
        futures_symbol=futures_symbol,
        as_of_ms=as_of_ms,
        window_start_ms=window_start,
        window_end_ms=window_end,
        spot_volume_30d=spot_30d,
        spot_volume_prev_30d=spot_prev_30d,
        spot_volume_decay_30d=decay,
        spot_quote_volume_24h=spot_24h,
        spot_price=spot_price,
        futures_volume_30d=futures_30d,
        futures_spot_volume_ratio_30d=ratio,
        premium=premium,
        canonical_futures_price=canonical,
        multiplier_applied=multiplier,
        spot_daily_bars=len(spot_bars),
        futures_daily_bars=len(futures_bars),
    )

    spot_complete = all(
        v is not None
        for v in (spot_30d, spot_prev_30d, decay, spot_24h, spot_price)
    )
    futures_complete = futures_ok and futures_30d is not None and ratio is not None
    if spot_complete and futures_complete:
        return ProviderResult(
            status="OK",
            source="binance-spot",
            fetched_at_ms=now_ms,
            as_of_ms=as_of_ms,
            data=history,
            stale=False,
            reason_code=None,
            error_message=None,
        )
    if not spot_complete:
        reason, message = SPOT_HISTORY_INCOMPLETE, "spot daily window incomplete"
    else:
        reason, message = FUTURES_LEG_UNREACHABLE, futures_error or "futures daily leg incomplete"
    return ProviderResult(
        status="PARTIAL",
        source="binance-spot",
        fetched_at_ms=now_ms,
        as_of_ms=as_of_ms,
        data=history,
        stale=False,
        reason_code=reason,
        error_message=sanitize_error_message(message),
    )


# ---------------------------------------------------------------------------
# F02 observation wrappers (design A4.1/A4.2/A4.3; plan F02.1/F02.2).
#
# Legacy entry points above keep their exact signatures and return types.
# The ``*_observed`` adapters below wrap the same results in
# ``shortlab.observations.Observed``: ``value`` is the legacy return object
# itself (``to_legacy`` returns the identical object/type),
# ``meta.known_at_ms`` is the response-completion time, and an unknown
# source time stays ``None``. Volumes stay quote-notional (Binance raw
# index 7) and are never scaled by ``contract_multiplier`` -- only the
# decimal ``premium`` is multiplier-normalised (see :func:`spot_history`).
# ---------------------------------------------------------------------------

_SPOT_SNAPSHOT_SOURCE = "binance-spot-snapshot"
_SPOT_HISTORY_SOURCE = "binance-spot-history"

# Observed-level mirror of the 60s snapshot cache: symbol -> (mono, Observed).
# A hit returns the identical Observed (original known_at/source_as_of).
_snapshot_observed_cache: dict[str, tuple[float, _obs.Observed[dict]]] = {}


def reset_observation_cache() -> None:
    """Drop the F02 snapshot ``Observed`` cache (test hook)."""
    _snapshot_observed_cache.clear()


async def snapshot_observed(
    symbol: str,
    perp_price: float,
    perp_closes: list[float],
    *,
    as_of_ms: int | None = None,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: RequestContext | None = None,
) -> _obs.Observed[dict]:
    """Cached ``spot_perp`` block wrapped as an ``Observed`` (F02).

    Mirrors the 60s success-only TTL: a cache hit returns the identical
    ``Observed`` with its original ``known_at_ms``. ``as_of_ms`` is
    accepted for the downstream cutoff check only.
    """
    _ = as_of_ms  # decision cutoff is enforced downstream via validate_observation
    now_mono = time.monotonic()
    hit = _snapshot_observed_cache.get(symbol)
    if hit is not None and now_mono - hit[0] < CACHE_TTL:
        return hit[1]
    block = await snapshot(symbol, perp_price, perp_closes, request_context=request_context)
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    observed = _obs.make_observation(
        block,
        source=_SPOT_SNAPSHOT_SOURCE,
        source_as_of_ms=None,
        fetched_at_ms=completed,
        known_at_ms=completed,
        status="OK" if "unavailable" not in block else "NOT_APPLICABLE",
        reason_code=None if "unavailable" not in block else block.get("unavailable"),
        units=_obs.ObservationUnits(quote_asset="USDT"),
        identity_snapshot_id=identity_snapshot_id,
    )
    if "unavailable" not in block:
        _snapshot_observed_cache[symbol] = (time.monotonic(), observed)
    return observed


async def spot_history_observed(
    identity: Any,
    as_of_ms: int,
    *,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: RequestContext | None = None,
) -> _obs.Observed[ProviderResult[SpotHistory]]:
    """60D Spot history wrapped as an ``Observed`` (F02).

    ``value`` is the very ``ProviderResult`` :func:`spot_history` returns
    (status/reason/data preserved); ``meta.known_at_ms`` is this response's
    completion time. History results are not cached -- every call is fresh
    (the underlying exchangeInfo list cache never refreshes a history
    observation's ``known_at``). ``meta.source_as_of_ms`` is the UTC-midnight
    window end; ``complete`` mirrors the ``OK`` window.
    """
    result = await spot_history(identity, as_of_ms, request_context=request_context)
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    data = result.data
    if data is not None:
        window_start = data.window_start_ms
        window_end = data.window_end_ms
    else:
        window_start, window_end = _spot_window_ms(int(as_of_ms))
    return _obs.make_observation(
        result,
        source=_SPOT_HISTORY_SOURCE,
        source_as_of_ms=window_end,
        fetched_at_ms=completed,
        known_at_ms=completed,
        status=result.status,
        reason_code=result.reason_code,
        window_start_ms=window_start,
        window_end_ms=window_end,
        complete=(result.status == "OK"),
        coverage_fraction=1.0 if result.status == "OK" else 0.0,
        units=_obs.ObservationUnits(
            quote_asset="USDT",
            multiplier_source=getattr(identity, "multiplier_source", None),
        ),
        identity_snapshot_id=identity_snapshot_id,
    )


# ---------------------------------------------------------------------------
# H02 Hedge market-data helpers (design B10/B16.1; plan H02.1).
#
# The Hedge venue (`shortlab/hedge/venues/binance_spot.py`) reuses this Spot
# client -- no duplicate Binance Spot client exists. Helpers below expose
# exactly what Hedge needs on top of the historical `snapshot`/`spot_history`
# paths (which stay untouched):
#
# - `fetch_spot_exchange_entry`: one exchangeInfo symbol entry (raw `filters`
#   + `orderTypes` + `status`) for the shared `data/trading_rules.py` parser;
# - `fetch_spot_book_ticker`: best bid/ask for the reference mid;
# - `fetch_spot_depth`: raw bids/asks (price/qty verbatim) for level VWAP.
#
# Identity rule: callers pass the venue instrument ID that came from
# `identity.binance_spot_symbol` (upper-cased, never 1000-prefix-stripped,
# never derived from the futures symbol). Transport failures (451/403/429,
# timeouts, budget denials) propagate to the venue, which maps them to
# UNAVAILABLE (`VENUE_REGION_UNAVAILABLE`/`RATE_LIMITED`) -- never N/A.
# Only an exchangeInfo-confirmed absence (entry None) may become
# NOT_APPLICABLE upstream.
# ---------------------------------------------------------------------------

#: Hedge depth escalation budget (Spot limits 100/500/1000).
SPOT_HEDGE_DEPTH_LIMITS: tuple[int, ...] = (100, 500, 1000)


async def fetch_spot_exchange_entry(
    symbol: str, *, request_context: RequestContext | None = None
) -> dict | None:
    """One Spot exchangeInfo symbol entry, or None when the list lacks it.

    Returns the raw entry dict (with ``filters``/``orderTypes``/``status``)
    for ``symbol`` (case-insensitive match). Raises on transport failure so
    the venue can map 451/403/429 to UNAVAILABLE instead of N/A.
    """
    cleaned = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not cleaned:
        raise ValueError("fetch_spot_exchange_entry requires a non-empty symbol")
    ctx = _resolve_spot_context(request_context)
    with _maybe_scope(ctx):
        payload = await _spot_json("/exchangeInfo", {})
    rows = payload.get("symbols", []) if isinstance(payload, dict) else []
    for entry in rows:
        if isinstance(entry, dict) and str(entry.get("symbol", "")).upper() == cleaned:
            return entry
    return None


async def fetch_spot_book_ticker(
    symbol: str, *, request_context: RequestContext | None = None
) -> dict:
    """Best bid/ask for ``symbol`` (``/ticker/bookTicker``)."""
    cleaned = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not cleaned:
        raise ValueError("fetch_spot_book_ticker requires a non-empty symbol")
    ctx = _resolve_spot_context(request_context)
    with _maybe_scope(ctx):
        return await _spot_json("/ticker/bookTicker", {"symbol": cleaned})


async def fetch_spot_depth(
    symbol: str, limit: int = 100, *, request_context: RequestContext | None = None
) -> dict:
    """Raw Spot depth for ``symbol`` (``/depth``; bids/asks verbatim)."""
    cleaned = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not cleaned:
        raise ValueError("fetch_spot_depth requires a non-empty symbol")
    if int(limit) not in (5, 10, 20, 50, 100, 500, 1000, 5000):
        raise ValueError(f"spot depth limit must be a Binance limit, got {limit!r}")
    ctx = _resolve_spot_context(request_context)
    with _maybe_scope(ctx):
        return await _spot_json("/depth", {"symbol": cleaned, "limit": int(limit)})
