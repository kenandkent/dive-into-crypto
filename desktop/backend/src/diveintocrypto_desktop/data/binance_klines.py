"""Multi-timeframe OHLCV via Crypcodile's Binance kline parser.

OHLCV is fetched from Binance's official USDT-M ``/fapi/v1/klines`` (official bars,
including the taker-buy volume split) and normalised through Crypcodile's
``parse_klines_page`` so the whole data path stays Crypcodile-centric.

Anti-repaint: Binance returns the current UNFINISHED candle as the last row when
``end_time`` is "now". Every indicator reads ``iloc[-1]``, so a trailing
in-progress candle makes signals flip before the close. We therefore request one
extra row and drop any candle whose close time is still in the future.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pandas as pd
from crypcodile.exchanges.binance.backfill import _live_fetch_klines, parse_klines_page

from diveintocrypto_desktop.data.http import FAPI_V1, TransientUpstreamError, get_json, run_with_retries
from diveintocrypto_desktop.shortlab import observations as _obs

try:  # pragma: no cover - import guard
    from diveintocrypto_desktop.shortlab.request_budget import (
        RequestContext,
        get_current_request_context,
    )
except Exception:  # pragma: no cover
    RequestContext = Any  # type: ignore[assignment,misc]
    def get_current_request_context():  # type: ignore[no-redef]
        return None

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000
_KLINE_SOURCE = "binance-futures-klines"
_MARK_KLINE_SOURCE = "binance-futures-mark-klines"
# R03 family (D19.3): markKlines shares the klines limit buckets (1..1500).
_MARK_KLINE_FAMILY = "markKlines"

# The canonical 12 timeframes of the Dive Into Crypto scanner.
TF_LIST = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"]

_VENUE = "binance-usdm"


def period_ns(interval: str) -> int:
    """Candle period in nanoseconds for a Binance interval like ``1m``/``4h``/``1d``."""
    unit_seconds = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    return int(interval[:-1]) * unit_seconds.get(interval[-1], 60) * 1_000_000_000


async def _fetch_raw(symbol: str, interval: str, limit: int, end_time_ms: int) -> Any:
    async def send() -> Any:
        return await _live_fetch_klines(
            symbol=symbol,
            interval=interval,
            start_time_ms=None,
            end_time_ms=end_time_ms,
            limit=limit,
            rest_base=FAPI_V1,
        )

    return await run_with_retries(
        send,
        should_retry=lambda e: isinstance(e, TransientUpstreamError) or isinstance(e, asyncio.TimeoutError),
    )


async def _fetch_raw_range(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int) -> Any:
    async def send() -> Any:
        return await _live_fetch_klines(
            symbol=symbol,
            interval=interval,
            start_time_ms=start_ms,
            end_time_ms=end_ms,
            limit=limit,
            rest_base=FAPI_V1,
        )

    return await run_with_retries(
        send,
        should_retry=lambda e: isinstance(e, TransientUpstreamError) or isinstance(e, asyncio.TimeoutError),
    )


# Data-quality reasons for a null ``qv`` (logged, never fabricated as 0).
QV_DUPLICATE_OPEN_TIME = "QV_DUPLICATE_OPEN_TIME"
QV_MALFORMED_ROW = "QV_MALFORMED_ROW"
QV_MISSING_TIMESTAMP = "QV_MISSING_TIMESTAMP"
QV_UNMATCHED_TIMESTAMP = "QV_UNMATCHED_TIMESTAMP"


def _quote_volume_by_open_ms(raw: Any) -> tuple[dict[int, float], dict[int, str]]:
    """Map raw Binance open-time (ms) to quote asset volume (raw index 7).

    Crypcodile's ``OHLCV`` drops the quote leg, so ``qv`` is recovered here
    from the untouched raw page. A repeated open time is ambiguous: every
    candle with that time gets ``qv=None`` (no neighbour's value is reused).
    Returns ``(qv_by_open_ms, null_reason_by_open_ms)``.
    """
    qv_by_ms: dict[int, float] = {}
    null_reason: dict[int, str] = {}
    if not isinstance(raw, (list, tuple)):
        return qv_by_ms, null_reason
    seen: set[int] = set()
    for row in raw:
        try:
            open_ms = int(row[0])
        except (IndexError, TypeError, ValueError):
            continue
        if open_ms in seen:
            null_reason[open_ms] = QV_DUPLICATE_OPEN_TIME
            qv_by_ms.pop(open_ms, None)
            continue
        seen.add(open_ms)
        try:
            qv_by_ms[open_ms] = float(row[7])
        except (IndexError, TypeError, ValueError):
            null_reason[open_ms] = QV_MALFORMED_ROW
    return qv_by_ms, null_reason


def _attach_qv(recs: list, raw: Any) -> list[dict]:
    """Build ``{t,o,h,l,c,v,qv}`` candles, aligning ``qv`` by open time ``t``.

    Alignment is strictly by timestamp (``exchange_ts // 1_000_000`` back to
    raw open ms): never by array position, so the anti-repaint trim, page
    reordering, or a parser that reorders rows cannot misattribute a quote
    volume. Unresolvable legs become ``qv=None`` with a logged reason.
    """
    qv_by_ms, null_reason = _quote_volume_by_open_ms(raw)
    out = []
    for r in recs:
        ts = r.exchange_ts
        qv: float | None = None
        if ts is None:
            logger.warning("kline qv=null: parsed record has no timestamp: %s",
                           QV_MISSING_TIMESTAMP)
        else:
            open_ms: int | None = None
            try:
                open_ms = int(ts) // 1_000_000
            except (TypeError, ValueError):
                logger.warning("kline qv=null: unusable parsed timestamp %r: %s",
                               ts, QV_MISSING_TIMESTAMP)
            if open_ms is not None:
                if open_ms in qv_by_ms:
                    qv = qv_by_ms[open_ms]
                else:
                    reason = null_reason.get(open_ms, QV_UNMATCHED_TIMESTAMP)
                    logger.warning("kline qv=null for open_ms=%s: %s", open_ms, reason)
        out.append({"t": ts, "o": r.open, "h": r.high, "l": r.low,
                    "c": r.close, "v": r.volume, "qv": qv})
    return out


def _drop_unfinished(recs: list, interval: str, now_ns: int) -> list:
    """Drop trailing candles whose close time is in the future (they still repaint).

    If a skewed clock marks every candle unfinished, degrade to the newest candle
    rather than returning nothing downstream.
    """
    period = period_ns(interval)
    out = [r for r in recs
           if r.exchange_ts is None or r.exchange_ts + period <= now_ns]
    return out if out else recs[-1:]


def _pad_raw_for_parser(raw: Any) -> Any:
    """Pad short Binance rows so the Crypcodile parser does not crash (R03).

    Real rows have 12 fields; a row missing index 7 (quote volume) must yield
    ``qv=None`` -- never an ``IndexError`` from the parser's index-9 read.
    Padding is parser-only: ``_quote_volume_by_open_ms`` still reads the
    original untouched ``raw`` so the missing leg stays null with a logged
    reason.
    """
    if not isinstance(raw, (list, tuple)):
        return raw
    out: list[Any] = []
    for row in raw:
        if isinstance(row, (list, tuple)) and len(row) < 12:
            padded = list(row) + ["0"] * (12 - len(row))
            out.append(padded)
        else:
            out.append(row)
    return out


def _safe_parse_klines_page(raw: Any, symbol: str, interval: str, local_ts: int) -> list:
    """Parse with tolerance for short rows (missing index 7 -> qv None)."""
    try:
        return list(parse_klines_page(raw, _VENUE, symbol, interval, local_ts))
    except (IndexError, TypeError, ValueError):
        padded = _pad_raw_for_parser(raw)
        try:
            return list(parse_klines_page(padded, _VENUE, symbol, interval, local_ts))
        except Exception:
            # Last resort: minimal manual records for rows with valid OHLC.
            from types import SimpleNamespace as _NS

            recs = []
            for row in raw if isinstance(raw, (list, tuple)) else []:
                try:
                    open_ms = int(row[0])  # type: ignore[index]
                    recs.append(
                        _NS(
                            exchange_ts=open_ms * 1_000_000,
                            open=float(row[1]),  # type: ignore[index]
                            high=float(row[2]),  # type: ignore[index]
                            low=float(row[3]),  # type: ignore[index]
                            close=float(row[4]),  # type: ignore[index]
                            volume=float(row[5]) if len(row) > 5 else 0.0,  # type: ignore[index]
                        )
                    )
                except (IndexError, TypeError, ValueError):
                    continue
            return recs


async def _budgeted_mark_get_json(url: str, params: dict[str, Any] | None, ctx: Any | None) -> Any:
    """GET via shared HTTP, preserving legacy 2-arg fakes (R03)."""
    if ctx is not None and getattr(ctx, "budget", None) is not None:
        return await get_json(url, params, request_context=ctx)
    if params is None:
        return await get_json(url)
    return await get_json(url, params)


async def fetch_klines(
    symbol: str,
    interval: str,
    limit: int = 300,
    end_ms: int | None = None,
    *,
    request_context: Any | None = None,
) -> list[dict]:
    """Return the most recent ``limit`` FINISHED candles for ``symbol`` at ``interval``.

    Each candle is ``{t, o, h, l, c, v, qv}`` with ``t`` in nanoseconds UTC:
    ``v`` is base volume, ``qv`` is quote asset volume (Binance raw index 7,
    ``None`` with a logged data-quality reason when unresolvable -- a missing
    index 7 never becomes 0 and never crashes the parser). The
    current in-progress candle is never returned. ``end_ms`` fetches the window
    ENDING at that instant (historical views); the anti-repaint rule then drops
    candles that were still unfinished *as of ``end_ms``*.
    R03: accepts keyword ``request_context`` (forwarded when the HTTP path
    supports it; TRADE klines via Crypcodile charge no duplicate budget here).
    """
    _ = request_context  # accepted for R03 API uniformity; see mark path for budgeted HTTP
    now_ms = int(end_ms if end_ms is not None else time.time() * 1000)
    raw = await _fetch_raw(symbol, interval, limit + 1, now_ms)
    local_ts = now_ms * 1_000_000
    recs = _safe_parse_klines_page(raw, symbol, interval, local_ts)
    recs = _drop_unfinished(list(recs), interval, local_ts)
    return _attach_qv(recs[-limit:], raw)


async def fetch_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
    *,
    request_context: Any | None = None,
) -> list[dict]:
    """FINISHED candles for ``symbol`` at ``interval`` covering ``[start_ms, end_ms]``.

    Used by the evidence grader to backfill forward returns after an archived
    verdict. Same anti-repaint rule as :func:`fetch_klines` (the trailing
    unfinished candle is dropped). Each candle carries ``qv`` like
    :func:`fetch_klines` (raw index 7 retained, source close retained,
    unclosed daily excluded, missing leg null).
    R03: accepts keyword ``request_context`` (no duplicate budgeting here).
    """
    _ = request_context
    now_ms = int(time.time() * 1000)
    raw = await _fetch_raw_range(symbol, interval, start_ms, min(end_ms, now_ms), limit)
    local_ts = now_ms * 1_000_000
    recs = _safe_parse_klines_page(raw, symbol, interval, local_ts)
    recs = _drop_unfinished(list(recs), interval, local_ts)
    return _attach_qv(recs, raw)


async def fetch_all_tf(
    symbol: str,
    limit: int = 300,
    intervals: list[str] | None = None,
    end_ms: int | None = None,
    *,
    request_context: Any | None = None,
) -> dict[str, list[dict]]:
    """Fetch all 12 timeframes for ``symbol`` concurrently (optionally as-of ``end_ms``)."""
    tfs = intervals or TF_LIST
    results = await asyncio.gather(
        *(fetch_klines(symbol, tf, limit, end_ms=end_ms, request_context=request_context) for tf in tfs)
    )
    return dict(zip(tfs, results))


def _observed_for_candles(
    candles: list[dict],
    *,
    cutoff_ms: int,
    completed_ms: int,
    identity_snapshot_id: str | None,
) -> _obs.Observed[list[dict]]:
    """Wrap legacy candles: completion ``known_at``, UTC-midnight window end."""
    opens = sorted(
        int(c["t"]) // 1_000_000 for c in candles or [] if c.get("t") is not None
    )
    resolved = sum(1 for c in candles or [] if c.get("qv") is not None)
    total = len(candles or [])
    return _obs.make_observation(
        candles,
        source=_KLINE_SOURCE,
        source_as_of_ms=max(opens) if opens else None,
        fetched_at_ms=completed_ms,
        known_at_ms=completed_ms,
        window_start_ms=min(opens) if opens else None,
        window_end_ms=(int(cutoff_ms) // DAY_MS) * DAY_MS,
        complete=bool(total) and resolved == total,
        coverage_fraction=(resolved / total) if total else 0.0,
        identity_snapshot_id=identity_snapshot_id,
    )


async def fetch_klines_observed(
    symbol: str,
    interval: str,
    limit: int = 300,
    end_ms: int | None = None,
    *,
    as_of_ms: int | None = None,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: Any | None = None,
) -> _obs.Observed[list[dict]]:
    """Most recent FINISHED candles wrapped as an ``Observed`` (F02/R03).

    Legacy :func:`fetch_klines` keeps its signature and return type; the
    wrapper only adds the PIT envelope. ``known_at_ms`` is the
    response-completion time (``qv`` still reads raw index 7, aligned by open
    time; missing/unclosed legs stay ``None``, never 0). Klines carry no
    result cache, so every call is fresh. ``as_of_ms`` is accepted for the
    downstream cutoff check and defaults to ``end_ms``.
    R03: accepts keyword ``request_context`` (no duplicate budgeting here).
    """
    candles = await fetch_klines(symbol, interval, limit, end_ms=end_ms, request_context=request_context)
    effective_end = end_ms if end_ms is not None else (
        int(as_of_ms) if as_of_ms is not None else int(time.time() * 1000)
    )
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    return _observed_for_candles(
        candles,
        cutoff_ms=effective_end,
        completed_ms=completed,
        identity_snapshot_id=identity_snapshot_id,
    )


async def fetch_klines_range_observed(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
    *,
    as_of_ms: int | None = None,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: Any | None = None,
) -> _obs.Observed[list[dict]]:
    """FINISHED candles over ``[start_ms, end_ms]`` wrapped as ``Observed`` (R03)."""
    candles = await fetch_klines_range(
        symbol, interval, start_ms, end_ms, limit, request_context=request_context
    )
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    return _observed_for_candles(
        candles,
        cutoff_ms=end_ms,
        completed_ms=completed,
        identity_snapshot_id=identity_snapshot_id,
    )


# ---------------------------------------------------------------------------
# R03 Mark Price Klines (D18.1): true Mark OHLC for evidence archiving.
#
# ``fetch_mark_klines_range`` uses the public ``/fapi/v1/markPriceKlines``
# endpoint with genuine Mark OHLC + close time. TRADE klines (``/fapi/v1/klines``)
# must NEVER be substituted as Mark history -- the URL, source tag and per-candle
# ``price_basis="MARK"`` prove the provenance. Unclosed bars as of ``end_ms``
# are excluded; missing quote legs stay ``None`` (never 0).
# Family: ``markKlines`` (D19.3, klines limit buckets, 1..1500). Existing
# ``klines`` family/weights are untouched.
# ---------------------------------------------------------------------------


def _parse_mark_row(row: Any) -> dict[str, Any] | None:
    """Parse one raw ``markPriceKlines`` row to a Mark candle (R03).

    Raw layout mirrors klines: index 0 openTime, 1..4 OHLC (Mark), 5 volume,
    6 closeTime, 7 quote volume (may be absent -> None). Returns ``None`` for
    malformed rows (never a fabricated zero).
    """
    try:
        open_ms = int(row[0])  # type: ignore[index]
        close_ms = int(row[6])  # type: ignore[index]
    except (IndexError, TypeError, ValueError):
        return None
    try:
        o = float(row[1])  # type: ignore[index]
        h = float(row[2])  # type: ignore[index]
        l = float(row[3])  # type: ignore[index]
        c = float(row[4])  # type: ignore[index]
    except (IndexError, TypeError, ValueError):
        return None
    try:
        v_raw = row[5]  # type: ignore[index]
        v = float(v_raw) if v_raw is not None else None
    except (IndexError, TypeError, ValueError):
        v = None
    try:
        qv_raw = row[7]  # type: ignore[index]
        qv = float(qv_raw) if qv_raw is not None else None
    except (IndexError, TypeError, ValueError):
        qv = None
        logger.warning("mark kline qv=null for open_ms=%s: %s", open_ms, QV_MALFORMED_ROW)
    if not (open_ms > 0 and close_ms > 0):
        return None
    return {
        "t": open_ms * 1_000_000,
        "openTime": open_ms,
        "closeTime": close_ms,
        "o": o,
        "h": h,
        "l": l,
        "c": c,
        "v": v,
        "qv": qv,
        "price_basis": "MARK",
    }


async def fetch_mark_klines_range(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    request_context: Any | None = None,
    limit: int = 1000,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
) -> _obs.Observed[list[dict]]:
    """True Mark OHLC over ``[start_ms, end_ms]`` wrapped as ``Observed`` (R03/D18.1).

    Uses the public Mark Price Kline endpoint (``/fapi/v1/markPriceKlines``);
    TRADE klines are never substituted. Each candle carries genuine Mark
    ``o/h/l/c`` + ``closeTime`` + ``price_basis="MARK"``; ``qv`` reads raw
    index 7 (``None`` when missing, never 0). Bars unclosed as of ``end_ms``
    are excluded. ``meta.source_as_of_ms`` is the max Mark ``closeTime``;
    ``known_at`` is the completion time. ``request_context`` is forwarded to
    the shared HTTP layer (single charge there; no duplicate budgeting here).
    """
    start_ms, end_ms = int(start_ms), int(end_ms)
    if end_ms <= start_ms:
        completed_empty = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        return _obs.make_observation(
            [],
            source=_MARK_KLINE_SOURCE,
            source_as_of_ms=None,
            fetched_at_ms=completed_empty,
            known_at_ms=completed_empty,
            window_start_ms=start_ms,
            window_end_ms=end_ms,
            complete=True,
            coverage_fraction=1.0,
            identity_snapshot_id=identity_snapshot_id,
        )
    lim = max(1, min(int(limit), 1500))
    ctx = request_context if request_context is not None else get_current_request_context()
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": lim,
    }
    url = f"{FAPI_V1}/markPriceKlines"
    raw: Any = await _budgeted_mark_get_json(url, params, ctx)
    candles: list[dict[str, Any]] = []
    if isinstance(raw, (list, tuple)):
        for row in raw:
            parsed = _parse_mark_row(row)
            if parsed is None:
                continue
            # Exclude bars unclosed as of end_ms (closeTime beyond the window).
            if int(parsed["closeTime"]) >= int(end_ms) and int(parsed["openTime"]) + (
                period_ns(interval) // 1_000_000
            ) > int(end_ms):
                continue
            # Strict window containment on open time.
            if not (int(start_ms) <= int(parsed["openTime"]) < int(end_ms)):
                # Keep rows whose open is inside [start, end); out-of-window
                # rows (e.g. server over-fetch) are dropped, never re-dated.
                continue
            candles.append(parsed)
    candles.sort(key=lambda c: int(c["openTime"]))
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    closes = [int(c["closeTime"]) for c in candles]
    opens = [int(c["openTime"]) for c in candles]
    resolved = sum(1 for c in candles if c.get("qv") is not None)
    total = len(candles)
    return _obs.make_observation(
        candles,
        source=_MARK_KLINE_SOURCE,
        source_as_of_ms=max(closes) if closes else None,
        fetched_at_ms=completed,
        known_at_ms=completed,
        window_start_ms=min(opens) if opens else start_ms,
        window_end_ms=(int(end_ms) // DAY_MS) * DAY_MS if interval == "1d" else end_ms,
        complete=bool(total) and resolved == total,
        coverage_fraction=(resolved / total) if total else 1.0,
        identity_snapshot_id=identity_snapshot_id,
    )


def to_dataframe(candles: list[dict]) -> pd.DataFrame:
    """Convert ``[{t,o,h,l,c,v}]`` to the OHLCV DataFrame the engine consumes.

    Indicator input semantics are unchanged: only base volume ``v`` feeds
    ``volume``; a present ``qv`` is ignored here (Short-Lab reads it off the
    candle DTOs directly).
    """
    return pd.DataFrame(
        [{"open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"], "volume": c["v"]} for c in candles]
    )
