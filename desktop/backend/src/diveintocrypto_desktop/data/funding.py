"""Funding rate + mark/index price from Binance USDT-M public endpoints.

The funding lens (:func:`funding_lens`) turns the raw funding surface into the
planner-facing block: the *predicted* (current-interval, pre-settlement) rate
from ``premiumIndex.lastFundingRate``, the last *settled* rates from the
``fundingRate`` tail, the annualized APR those imply, seconds to the next
settlement, and a crowding ``regime`` label. Every failed/absent input is an
explicit ``None`` — never a zero dressed as data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from aiolimiter import AsyncLimiter

from diveintocrypto_desktop.data.http import FAPI_V1, get_json

# Funding settles three times a day (00:00 / 08:00 / 16:00 UTC) for most pairs.
_SETTLE_SECONDS = 8 * 3600

# ── Short-Lab funding budget (design §10.1) ─────────────────────────────────
# USDⓈ-M /fapi/v1/fundingRate pages (max 1000 rows each, ascending fundingTime)
# share 500 req/5min/IP with /fapi/v1/fundingInfo. Short-Lab caps itself at 80
# req/5min/IP and 80 symbols per 5-minute batch; a first backfill job may span
# several batches and must account requests/remaining queue itself (Task 13).
SHORTLAB_FUNDING_BUDGET_PER_5MIN = 80
SHORTLAB_FUNDING_BATCH_SYMBOLS = 80
SHORTLAB_FUNDING_PAGE_LIMIT = 1000

# ── coverage rule (design §10.1) ────────────────────────────────────────────
# A window is complete only when: earliest/latest events are each within 24h
# of the window edges, every adjacent gap is within 24h, and >= 3 events exist.
# The 24h threshold is conservative gap detection, NOT an assumed fixed
# settlement cadence. Incomplete windows report coverage_fraction and gaps and
# their funding metric must be treated as null by consumers.
FUNDING_MAX_GAP_MS = 24 * 3600 * 1000
FUNDING_MIN_EVENTS = 3
FUNDING_HISTORY_INCOMPLETE = "FUNDING_HISTORY_INCOMPLETE"

_shortlab_funding_limiters: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}


def _shortlab_funding_limiter() -> AsyncLimiter:
    loop = asyncio.get_running_loop()
    limiter = _shortlab_funding_limiters.get(loop)
    if limiter is None:
        limiter = AsyncLimiter(
            max_rate=SHORTLAB_FUNDING_BUDGET_PER_5MIN, time_period=5 * 60
        )
        _shortlab_funding_limiters[loop] = limiter
    return limiter


@dataclass(frozen=True)
class Coverage:
    """Completeness of settled funding history over ``[window_start_ms, window_end_ms]``.

    ``coverage`` is 1 only when the design §10.1 rule holds (edges <= 24h,
    adjacent gaps <= 24h, >= 3 events); otherwise the window metric is null,
    ``reason_code`` is ``FUNDING_HISTORY_INCOMPLETE``, and ``gaps`` lists every
    adjacent/edge shortfall exceeding 24h as ``{start_ms, end_ms, gap_ms}``.
    ``coverage_fraction`` follows the §10.1 formula verbatim (1.0 when complete).
    """

    window_start_ms: int
    window_end_ms: int
    event_count: int
    first_event_ms: int | None
    last_event_ms: int | None
    coverage: int  # 1 = complete, 0 = incomplete
    complete: bool
    coverage_fraction: float
    gaps: tuple[dict[str, int], ...] = field(default_factory=tuple)
    reason_code: str | None = None


def _event_time_ms(event: dict[str, Any]) -> int | None:
    try:
        if "t" in event:
            return int(event["t"])
        return int(event["fundingTime"])
    except (KeyError, TypeError, ValueError):
        return None


async def premium_index(symbol: str) -> dict[str, float]:
    """Current mark price, index price and last funding rate for one symbol.

    ``time_ms`` is the exchange's ``time`` field (``None`` when absent — never
    the local clock); it lets Task 7 verify an OI value against a synchronous
    mark price. ``last_funding_rate`` is the *predicted* (pre-settlement) rate
    and must never be used as settled funding history.
    """
    d: dict[str, Any] = await get_json(f"{FAPI_V1}/premiumIndex", {"symbol": symbol})
    out: dict[str, Any] = {
        "mark_price": float(d["markPrice"]),
        "index_price": float(d["indexPrice"]),
        "last_funding_rate": float(d["lastFundingRate"]),
        "next_funding_time": int(d.get("nextFundingTime", 0)),
    }
    try:
        out["time_ms"] = int(d["time"]) if d.get("time") is not None else None
    except (KeyError, TypeError, ValueError):
        out["time_ms"] = None
    return out


async def premium_index_all() -> dict[str, dict[str, float]]:
    """premiumIndex for EVERY symbol in one call (weight 10) → ``{symbol: row}``.

    Cheaper than per-symbol loops for scan-wide funding/basis annotations.
    Each row carries the same additive ``time_ms`` contract as
    :func:`premium_index` (per-row ``time`` field, ``None`` when absent).
    """
    rows: list[dict[str, Any]] = await get_json(f"{FAPI_V1}/premiumIndex")
    out: dict[str, dict[str, float]] = {}
    for d in rows:
        try:
            row: dict[str, Any] = {
                "mark_price": float(d["markPrice"]),
                "index_price": float(d["indexPrice"]),
                "last_funding_rate": float(d["lastFundingRate"]),
                "next_funding_time": int(d.get("nextFundingTime", 0)),
            }
            try:
                row["time_ms"] = int(d["time"]) if d.get("time") is not None else None
            except (KeyError, TypeError, ValueError):
                row["time_ms"] = None
            out[str(d["symbol"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def funding_hist(symbol: str, limit: int = 48) -> list[dict]:
    """Recent funding events ``[{t, funding_rate}]`` (t in ms).

    Legacy tail reader for existing scan callers; behaviour unchanged.
    Short-Lab windowed backfill must use :func:`funding_history_range`.
    """
    rows: list[dict[str, Any]] = await get_json(
        f"{FAPI_V1}/fundingRate", {"symbol": symbol, "limit": limit}
    )
    return [{"t": int(r["fundingTime"]), "funding_rate": float(r["fundingRate"])} for r in rows]


async def funding_history_range(
    symbol: str, start_ms: int, end_ms: int, limit: int = 1000
) -> list[dict]:
    """Settled funding events over ``[start_ms, end_ms]`` (ascending ``t``).

    Pages ``GET /fapi/v1/fundingRate`` with ``startTime/endTime/limit`` through
    the shared ``data/http.py`` path (429/5xx retried with ``Retry-After``),
    under the Short-Lab 80 req/5min limiter. A full page resumes at
    ``last fundingTime + 1``; repeated times are deduped; an empty (or
    sub-``limit``) page ends pagination. Each event is
    ``{t, funding_rate, mark_price}`` (``mark_price`` is ``None`` when the row
    lacks ``markPrice``). Only the settled ``fundingRate`` endpoint is read —
    the predicted ``premiumIndex.lastFundingRate`` is never history.
    """
    start_ms, end_ms = int(start_ms), int(end_ms)
    if end_ms <= start_ms:
        return []
    limit = max(1, min(int(limit), SHORTLAB_FUNDING_PAGE_LIMIT))
    limiter = _shortlab_funding_limiter()
    events: list[dict[str, Any]] = []
    seen: set[int] = set()
    cursor = start_ms
    while True:
        async with limiter:
            page: list[dict[str, Any]] = await get_json(
                f"{FAPI_V1}/fundingRate",
                {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": limit},
            )
        if not page:
            break
        page_times: list[int] = []
        for row in sorted(page, key=lambda r: int(r.get("fundingTime", 0))):
            try:
                t = int(row["fundingTime"])
            except (KeyError, TypeError, ValueError):
                continue
            page_times.append(t)
            if t < cursor or t > end_ms or t in seen:
                continue
            try:
                rate = float(row["fundingRate"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                mark = float(row["markPrice"]) if row.get("markPrice") is not None else None
            except (TypeError, ValueError):
                mark = None
            seen.add(t)
            events.append({"t": t, "funding_rate": rate, "mark_price": mark})
        if not page_times:
            break
        last_page_t = max(page_times)
        if len(page) < limit or last_page_t >= end_ms:
            break
        nxt = last_page_t + 1
        if nxt <= cursor:  # server is not advancing; avoid an infinite loop
            break
        cursor = nxt
    events.sort(key=lambda e: e["t"])
    return events


def funding_coverage(
    events: list[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    onboard_ms: int | None = None,
) -> Coverage:
    """Completeness of settled funding history over a lookback window.

    ``window_start = max(start_ms, onboard_ms)`` for a valid (positive)
    exchange ``onboardDate``; with no valid listing date the full lookback is
    kept — a locally observed ``first_seen`` must never be passed as
    ``onboard_ms`` to shorten the window. New listings are judged on their
    actual lifetime instead of being penalised for pre-listing history.
    ``end_ms`` (``as_of``) is the window end. Implements design §10.1 verbatim.
    """
    window_end = int(end_ms)
    window_start = int(start_ms)
    if onboard_ms is not None:
        try:
            ob = int(onboard_ms)
        except (TypeError, ValueError):
            ob = None
        if ob is not None and ob > 0:
            window_start = max(window_start, ob)
    window_len = window_end - window_start
    times = sorted({t for e in events if (t := _event_time_ms(e)) is not None})

    gaps: list[dict[str, int]] = []
    if window_len <= 0 or not times:
        if window_len > 0:
            gaps.append(
                {"start_ms": window_start, "end_ms": window_end, "gap_ms": window_len}
            )
        return Coverage(
            window_start_ms=window_start,
            window_end_ms=window_end,
            event_count=len(times),
            first_event_ms=times[0] if times else None,
            last_event_ms=times[-1] if times else None,
            coverage=0,
            complete=False,
            coverage_fraction=0.0,
            gaps=tuple(gaps),
            reason_code=FUNDING_HISTORY_INCOMPLETE,
        )

    adjacent_excess = sum(
        max(0, b - a - FUNDING_MAX_GAP_MS) for a, b in zip(times, times[1:])
    )
    for a, b in zip(times, times[1:]):
        if b - a > FUNDING_MAX_GAP_MS:
            gaps.append({"start_ms": a, "end_ms": b, "gap_ms": b - a})
    head_gap = times[0] - window_start
    if head_gap > FUNDING_MAX_GAP_MS:
        gaps.append({"start_ms": window_start, "end_ms": times[0], "gap_ms": head_gap})
    tail_gap = window_end - times[-1]
    if tail_gap > FUNDING_MAX_GAP_MS:
        gaps.append({"start_ms": times[-1], "end_ms": window_end, "gap_ms": tail_gap})

    complete = (
        len(times) >= FUNDING_MIN_EVENTS
        and head_gap <= FUNDING_MAX_GAP_MS
        and tail_gap <= FUNDING_MAX_GAP_MS
        and all(b - a <= FUNDING_MAX_GAP_MS for a, b in zip(times, times[1:]))
    )
    if complete:
        fraction = 1.0
    else:
        span = min(times[-1], window_end) - max(times[0], window_start)
        covered_ms = max(0, span - adjacent_excess)
        fraction = (
            max(0.0, min(1.0, covered_ms / window_len)) if window_len > 0 else 0.0
        )
    return Coverage(
        window_start_ms=window_start,
        window_end_ms=window_end,
        event_count=len(times),
        first_event_ms=times[0],
        last_event_ms=times[-1],
        coverage=1 if complete else 0,
        complete=complete,
        coverage_fraction=fraction,
        gaps=tuple(gaps),
        reason_code=None if complete else FUNDING_HISTORY_INCOMPLETE,
    )


# ── funding lens (pure math over fetched inputs) ──────────────────────────────
def funding_regime(predicted: float | None) -> str:
    """Crowding label from the predicted (pre-settlement) rate.

    Annualized bands: |rate|·3·365 — ±5% APR is quiet, ±25% crowded, beyond
    that extreme. Sign says which side is paying.
    """
    if predicted is None:
        return "unavailable"
    apr = predicted * 3 * 365
    if apr >= 0.25:
        return "extreme_long_crowding"
    if apr >= 0.05:
        return "long_crowding"
    if apr <= -0.25:
        return "extreme_short_crowding"
    if apr <= -0.05:
        return "short_crowding"
    return "balanced"


def funding_lens(
    predicted_funding: float | None,
    settled_tail: list[float],
    next_funding_time_ms: int | None,
    now_ms: int | None = None,
) -> dict:
    """``funding_lens`` block — pure over already-fetched inputs.

    ``predicted_funding`` comes from premiumIndex ``lastFundingRate`` (the
    current, not-yet-settled interval); ``settled_tail`` is the ``fundingRate``
    history tail (oldest→newest); ``next_funding_time_ms`` is the exchange's
    ``nextFundingTime`` (``None`` when 0/absent — never a fabricated countdown).
    """
    import time as _time

    last_settled = settled_tail[-1] if settled_tail else None
    apr = predicted_funding * 3 * 365 if predicted_funding is not None else None
    seconds_to_funding = None
    if next_funding_time_ms:
        now = int(now_ms if now_ms is not None else _time.time() * 1000)
        delta = (int(next_funding_time_ms) - now) / 1000.0
        if delta > 0:
            seconds_to_funding = round(delta, 1)
    return {
        "predicted_funding": predicted_funding,
        "last_settled": last_settled,
        "apr": round(apr, 6) if apr is not None else None,
        "seconds_to_funding": seconds_to_funding,
        "regime": funding_regime(predicted_funding),
    }
