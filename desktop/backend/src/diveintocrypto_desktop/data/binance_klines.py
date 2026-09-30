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

from diveintocrypto_desktop.data.http import FAPI_V1, TransientUpstreamError, run_with_retries

logger = logging.getLogger(__name__)

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


async def fetch_klines(symbol: str, interval: str, limit: int = 300,
                       end_ms: int | None = None) -> list[dict]:
    """Return the most recent ``limit`` FINISHED candles for ``symbol`` at ``interval``.

    Each candle is ``{t, o, h, l, c, v, qv}`` with ``t`` in nanoseconds UTC:
    ``v`` is base volume, ``qv`` is quote asset volume (Binance raw index 7,
    ``None`` with a logged data-quality reason when unresolvable). The
    current in-progress candle is never returned. ``end_ms`` fetches the window
    ENDING at that instant (historical views); the anti-repaint rule then drops
    candles that were still unfinished *as of ``end_ms``*.
    """
    now_ms = int(end_ms if end_ms is not None else time.time() * 1000)
    raw = await _fetch_raw(symbol, interval, limit + 1, now_ms)
    local_ts = now_ms * 1_000_000
    recs = parse_klines_page(raw, _VENUE, symbol, interval, local_ts)
    recs = _drop_unfinished(list(recs), interval, local_ts)
    return _attach_qv(recs[-limit:], raw)


async def fetch_klines_range(
    symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 1000
) -> list[dict]:
    """FINISHED candles for ``symbol`` at ``interval`` covering ``[start_ms, end_ms]``.

    Used by the evidence grader to backfill forward returns after an archived
    verdict. Same anti-repaint rule as :func:`fetch_klines` (the trailing
    unfinished candle is dropped). Each candle carries ``qv`` like
    :func:`fetch_klines`.
    """
    now_ms = int(time.time() * 1000)
    raw = await _fetch_raw_range(symbol, interval, start_ms, min(end_ms, now_ms), limit)
    local_ts = now_ms * 1_000_000
    recs = parse_klines_page(raw, _VENUE, symbol, interval, local_ts)
    recs = _drop_unfinished(list(recs), interval, local_ts)
    return _attach_qv(recs, raw)


async def fetch_all_tf(symbol: str, limit: int = 300, intervals: list[str] | None = None,
                       end_ms: int | None = None) -> dict[str, list[dict]]:
    """Fetch all 12 timeframes for ``symbol`` concurrently (optionally as-of ``end_ms``)."""
    tfs = intervals or TF_LIST
    results = await asyncio.gather(*(fetch_klines(symbol, tf, limit, end_ms=end_ms) for tf in tfs))
    return dict(zip(tfs, results))


def to_dataframe(candles: list[dict]) -> pd.DataFrame:
    """Convert ``[{t,o,h,l,c,v}]`` to the OHLCV DataFrame the engine consumes.

    Indicator input semantics are unchanged: only base volume ``v`` feeds
    ``volume``; a present ``qv`` is ignored here (Short-Lab reads it off the
    candle DTOs directly).
    """
    return pd.DataFrame(
        [{"open": c["o"], "high": c["h"], "low": c["l"], "close": c["c"], "volume": c["v"]} for c in candles]
    )
