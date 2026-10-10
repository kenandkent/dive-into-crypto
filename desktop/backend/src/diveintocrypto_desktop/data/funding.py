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
import time
from dataclasses import dataclass, field
from typing import Any

from aiolimiter import AsyncLimiter

from diveintocrypto_desktop.data.http import FAPI_V1, get_json
from diveintocrypto_desktop.shortlab import observations as _obs
from diveintocrypto_desktop.shortlab.request_budget import (
    RequestContext,
    get_current_request_context,
)

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


async def _budgeted_get_json(
    url: str, params: dict[str, Any] | None, ctx: RequestContext | None
) -> Any:
    """Call ``get_json`` preserving legacy fake signatures (F03 compat).

    Legacy test doubles patch ``funding.get_json`` as
    ``fake(url, params=None)`` without the additive ``request_context`` kwarg.
    Only pass the kwarg when a real budget is present; otherwise use the exact
    legacy call shape.
    """
    if ctx is not None and ctx.budget is not None:
        return await get_json(url, params, request_context=ctx)
    return await get_json(url, params)


async def premium_index(
    symbol: str, *, request_context: RequestContext | None = None
) -> dict[str, float]:
    """Current mark price, index price and last funding rate for one symbol.

    ``time_ms`` is the exchange's ``time`` field (``None`` when absent — never
    the local clock); it lets Task 7 verify an OI value against a synchronous
    mark price. ``last_funding_rate`` is the *predicted* (pre-settlement) rate
    and must never be used as settled funding history.
    """
    ctx = request_context if request_context is not None else get_current_request_context()
    d: dict[str, Any] = await _budgeted_get_json(
        f"{FAPI_V1}/premiumIndex", {"symbol": symbol}, ctx
    )
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


async def premium_index_all(
    *, request_context: RequestContext | None = None
) -> dict[str, dict[str, float]]:
    """premiumIndex for EVERY symbol in one call (weight 10) → ``{symbol: row}``.

    Cheaper than per-symbol loops for scan-wide funding/basis annotations.
    Each row carries the same additive ``time_ms`` contract as
    :func:`premium_index` (per-row ``time`` field, ``None`` when absent).
    """
    ctx = request_context if request_context is not None else get_current_request_context()
    rows: list[dict[str, Any]] = await _budgeted_get_json(
        f"{FAPI_V1}/premiumIndex", None, ctx
    )
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


async def funding_hist(
    symbol: str, limit: int = 48, *, request_context: RequestContext | None = None
) -> list[dict]:
    """Recent funding events ``[{t, funding_rate}]`` (t in ms).

    Legacy tail reader for existing scan callers; behaviour unchanged.
    Short-Lab windowed backfill must use :func:`funding_history_range`.
    """
    ctx = request_context if request_context is not None else get_current_request_context()
    rows: list[dict[str, Any]] = await _budgeted_get_json(
        f"{FAPI_V1}/fundingRate",
        {"symbol": symbol, "limit": limit},
        ctx,
    )
    return [{"t": int(r["fundingTime"]), "funding_rate": float(r["fundingRate"])} for r in rows]


async def funding_history_range(
    symbol: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
    *,
    request_context: RequestContext | None = None,
) -> list[dict]:
    """Settled funding events over ``[start_ms, end_ms]`` (ascending ``t``).

    Pages ``GET /fapi/v1/fundingRate`` with ``startTime/endTime/limit`` through
    the shared ``data/http.py`` path (429/5xx retried with ``Retry-After``).
    Without a budget this uses the legacy Short-Lab 80 req/5min limiter;
    with ``request_context`` (F03) each page — including retries, the
    full-page resume and the terminating empty/sub-limit confirmation — is a
    real attempt counted by the shared ``RequestBudget`` funding window
    (80/300s). A cancelled pagination still counts already-sent pages (sent
    permits are never refunded). A full page resumes at ``last fundingTime +
    1``; repeated times are deduped; an empty (or sub-``limit``) page ends
    pagination. Each event is ``{t, funding_rate, mark_price}``
    (``mark_price`` is ``None`` when the row lacks ``markPrice``). Only the
    settled ``fundingRate`` endpoint is read — the predicted
    ``premiumIndex.lastFundingRate`` is never history.
    """
    start_ms, end_ms = int(start_ms), int(end_ms)
    if end_ms <= start_ms:
        return []
    limit = max(1, min(int(limit), SHORTLAB_FUNDING_PAGE_LIMIT))
    ctx = request_context if request_context is not None else get_current_request_context()
    budgeted = ctx is not None and ctx.budget is not None
    limiter = None if budgeted else _shortlab_funding_limiter()
    events: list[dict[str, Any]] = []
    seen: set[int] = set()
    cursor = start_ms
    while True:
        params = {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": limit}
        if limiter is None:
            # Budgeted path: each page (and each retry inside get_json) is a
            # shared-window attempt; cancellation propagates with sent pages
            # already counted (http permits are never refunded once sent).
            page: list[dict[str, Any]] = await get_json(
                f"{FAPI_V1}/fundingRate", params, request_context=ctx
            )
        else:
            async with limiter:
                page = await get_json(
                    f"{FAPI_V1}/fundingRate",
                    params,
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


# ---------------------------------------------------------------------------
# F02 observation wrappers (design A4.1/A4.3; plan F02.1).
#
# Legacy readers above keep their exact signatures and return types. The
# ``*_observed`` adapters below wrap the same results in
# ``shortlab.observations.Observed``: ``value`` is the legacy return object
# itself, ``meta.known_at_ms`` is the response-completion time (the injected
# ``now_ms`` receive clock in tests, otherwise the wall clock read *after*
# the last page resolves), and an unknown source time stays ``None``.
# Funding history has no result cache, so every call is a fresh observation.
# ---------------------------------------------------------------------------

_FUNDING_SOURCE = "binance-futures-funding"


async def fetch_funding_history_observed(
    symbol: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
    *,
    as_of_ms: int | None = None,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: RequestContext | None = None,
) -> _obs.Observed[list[dict]]:
    """Settled funding events wrapped as an ``Observed`` (F02/R03).

    ``as_of_ms`` is the decision cutoff the caller validates against via
    ``validate_observation``; it is not used to trim the window here.
    R03: ``request_context`` is forwarded to the paged range reader (single
    budget path in ``data/http.py``; no duplicate charge here). The raw
    observation writes its receipt (``known_at``/``source_as_of``); rereading
    the associated record must restore the identical receipt (see
    ``shortlab.observations`` bridge). A complete archive is never patched
    with "now fetched".
    """
    events = await funding_history_range(
        symbol, start_ms, end_ms, limit=limit, request_context=request_context
    )
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    cov = funding_coverage(events, start_ms, end_ms)
    source_as_of = cov.last_event_ms
    ok = bool(cov.complete)
    return _obs.make_observation(
        events,
        source=_FUNDING_SOURCE,
        source_as_of_ms=source_as_of,
        fetched_at_ms=completed,
        known_at_ms=completed,
        status="OK" if ok else "PARTIAL",
        reason_code=None if ok else FUNDING_HISTORY_INCOMPLETE,
        window_start_ms=cov.window_start_ms,
        window_end_ms=cov.window_end_ms,
        complete=ok,
        coverage_fraction=cov.coverage_fraction,
        identity_snapshot_id=identity_snapshot_id,
    )


async def fetch_premium_index_observed(
    symbol: str,
    *,
    as_of_ms: int | None = None,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: RequestContext | None = None,
) -> _obs.Observed[dict]:
    """Current premium-index row wrapped as an ``Observed`` (F02/R03).

    ``meta.source_as_of_ms`` is the exchange ``time`` field (``None`` when
    absent -- never the local clock); ``known_at_ms`` is the completion time.
    R03: ``request_context`` forwarded to the HTTP layer (no duplicate charge).
    """
    _ = as_of_ms  # decision cutoff is enforced downstream via validate_observation
    row = await premium_index(symbol, request_context=request_context)
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    return _obs.make_observation(
        row,
        source=_FUNDING_SOURCE,
        source_as_of_ms=row.get("time_ms"),
        fetched_at_ms=completed,
        known_at_ms=completed,
        identity_snapshot_id=identity_snapshot_id,
    )


# ---------------------------------------------------------------------------
# R03 fundingInfo -> FundingScheduleSegment (D05.1/D18.2).
#
# ``/fapi/v1/fundingInfo`` returns ONLY symbols with an adjusted settlement
# regime (adjustedFundingRateCap/Floor + fundingIntervalHours). A missing
# symbol proves nothing about its history -- it must NOT be inferred as 8h,
# and the current observation's effective_from is the observation boundary
# itself (never extended into unverified history). Without a verifiable old
# regime archive the caller reports HISTORY_BOOTSTRAPPING (history shown, no
# complete entry Gate). Only CONFIRMED segments may grant complete coverage
# (R05 consumes them via RepairPorts).
#
# Family: ``fundingInfo`` (D19.3 local weight 5, shares the Funding 80/300s
# window). Existing ``fundingRate`` family/weights are untouched.
# ---------------------------------------------------------------------------

_FUNDING_INFO_SOURCE = "binance-futures-fundingInfo"
_FUNDING_INFO_FAMILY = "fundingInfo"

#: R03 archive reason when no verifiable old-regime schedule exists.
HISTORY_BOOTSTRAPPING = "HISTORY_BOOTSTRAPPING"
#: R03 schedule-unknown reason (no CONFIRMED segment covers the window).
FUNDING_SCHEDULE_UNKNOWN = "FUNDING_SCHEDULE_UNKNOWN"
#: Legacy funding observation without a receipt.
FUNDING_LEGACY_UNVERIFIED = "UNVERIFIED"


def history_bootstrapping_reasons(has_archive: bool) -> tuple[str, ...]:
    """``(HISTORY_BOOTSTRAPPING,)`` when no verifiable old archive exists."""
    if has_archive:
        return ()
    return (HISTORY_BOOTSTRAPPING,)


def _funding_info_interval_hours(row: Any) -> int | None:
    if not isinstance(row, dict):
        return None
    for key in ("fundingIntervalHours", "fundingInterval", "intervalHours"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


async def fetch_funding_info(
    *, request_context: RequestContext | None = None
) -> list[dict]:
    """Raw ``/fapi/v1/fundingInfo`` rows (adjusted symbols only).

    R03: accepts keyword ``request_context`` forwarded to the shared HTTP
    layer (single charge there; no duplicate budgeting here). Pagination is
    not required (single document); 429/5xx retries honour ``Retry-After``
    via ``data/http.py``.
    """
    ctx = request_context if request_context is not None else get_current_request_context()
    rows: Any = await _budgeted_get_json(f"{FAPI_V1}/fundingInfo", None, ctx)
    if rows is None:
        return []
    if isinstance(rows, dict):
        # Some mirrors wrap the list; keep the raw rows verbatim.
        for key in ("data", "rows", "symbols"):
            if isinstance(rows.get(key), list):
                rows = rows[key]
                break
        else:
            return [rows]
    return list(rows) if isinstance(rows, list) else []


async def fetch_funding_info_observed(
    *,
    as_of_ms: int | None = None,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: RequestContext | None = None,
) -> _obs.Observed[list[dict]]:
    """Current ``fundingInfo`` document wrapped as an ``Observed`` (R03).

    ``fundingInfo`` carries no source time, so ``meta.source_as_of_ms`` stays
    ``None`` (never the local clock); ``known_at`` is the completion time.
    ``value`` is the verbatim row list (adjusted symbols only).
    """
    _ = as_of_ms
    rows = await fetch_funding_info(request_context=request_context)
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    return _obs.make_observation(
        rows,
        source=_FUNDING_INFO_SOURCE,
        source_as_of_ms=None,
        fetched_at_ms=completed,
        known_at_ms=completed,
        identity_snapshot_id=identity_snapshot_id,
    )


def funding_info_to_schedule_segments(
    info_rows: Any,
    *,
    observed_at_ms: int,
    known_at_ms: int,
    source: str = _FUNDING_INFO_SOURCE,
    evidence_ref_prefix: str = "fundingInfo",
) -> tuple[Any, ...]:
    """Build ``FundingScheduleSegment``s from a ``fundingInfo`` snapshot.

    Each adjusted symbol yields one segment with ``effective_from_ms`` set to
    the observation boundary (``observed_at_ms``) -- never extended into
    unverified history -- and ``effective_to_ms=None``. ``anchor_ms`` mirrors
    the boundary; ``verification`` is ``CONFIRMED`` for rows with an explicit
    positive ``fundingIntervalHours``, else ``UNKNOWN``. Symbols absent from
    the snapshot yield no segment (callers report ``HISTORY_BOOTSTRAPPING`` /
    ``FUNDING_SCHEDULE_UNKNOWN`` instead of inferring 8h).
    """
    try:
        from diveintocrypto_desktop.shortlab.repair_contracts import (
            FundingScheduleSegment,
        )
    except Exception:  # pragma: no cover - contract import guard
        FundingScheduleSegment = None  # type: ignore[assignment]

    observed = int(observed_at_ms)
    known = int(known_at_ms)
    segments: list[Any] = []
    rows = list(info_rows) if isinstance(info_rows, (list, tuple)) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        interval = _funding_info_interval_hours(row)
        verification = "CONFIRMED" if interval is not None else "UNKNOWN"
        if interval is None:
            interval = 8
        schedule_id = f"{symbol}:{observed}:{interval}"
        evidence_ref = f"{evidence_ref_prefix}:{symbol}:{known}"
        if FundingScheduleSegment is None:  # fallback dict shape
            segments.append(
                {
                    "schedule_id": schedule_id,
                    "symbol": symbol,
                    "effective_from_ms": observed,
                    "effective_to_ms": None,
                    "interval_hours": interval,
                    "anchor_ms": observed,
                    "known_at_ms": known,
                    "source": source,
                    "evidence_ref": evidence_ref,
                    "verification": verification,
                }
            )
        else:
            segments.append(
                FundingScheduleSegment(
                    schedule_id=schedule_id,
                    symbol=symbol,
                    effective_from_ms=observed,
                    effective_to_ms=None,
                    interval_hours=interval,
                    anchor_ms=observed,
                    known_at_ms=known,
                    source=source,
                    evidence_ref=evidence_ref,
                    verification=verification,
                )
            )
    return tuple(segments)


# ---------------------------------------------------------------------------
# CR03 funding schedule collection (D05.1/D05.2/D18.2).
#
# CR03 gap: ``fetch_funding_info_observed`` /
# ``funding_info_to_schedule_segments`` had definitions only, ``save_funding_schedule``
# had no business caller, and the Collector only read the archive. A fresh
# install therefore stayed UNKNOWN even after 30/90d of event collection.
#
# This section provides the single production collection task
# (:func:`collect_and_archive_funding_schedules`) that wires the four D05.1
# requirements together:
#
# 1.制度响应 (fundingInfo document, adjusted symbols only);
# 2.原始 receipt (``Observed`` known_at/fetched_at preserved verbatim);
# 3.有效区间 (``[effective_from, effective_to)`` with ``effective_from`` equal
#   to the observation boundary, never extended into unverified history);
# 4.档案保存 (``save_funding_schedule`` rows + optional raw ``FUNDING_INFO``
#   market observation for audit).
#
# Default-8h rule (D05.1): a symbol absent from fundingInfo proves nothing by
# itself. It may be archived as CONFIRMED 8h only when ALL of the following
# hold (otherwise UNKNOWN, never inferred):
#
# - (a) the fundingInfo fetch is a complete HTTP200 business success;
# - (b) the symbol status is TRADING and the symbol is not in the adjusted list;
# - (c) the referenced official current default regime is explicitly 8h AND
#   carries a recorded version + receipt (known_at/source).
#
# History rule: ``effective_from`` is always the current observation boundary.
# No backfill into pre-receipt windows is ever written, so
# ``compute_schedule_coverage`` stays UNKNOWN (``FUNDING_SCHEDULE_UNKNOWN``)
# for windows before the first CONFIRMED receipt and only grants coverage for
# post-receipt intervals. Revisions use a new ``schedule_id`` and never mutate
# old rows (D05.2); stable regimes are idempotent (no duplicate open rows);
# regime changes close stale open CONFIRMED rows with a same-``effective_from``
# revision whose ``effective_to`` equals the new observation boundary, so the
# transition does not leave a permanent overlap-UNKNOWN.
#
# Read-only Collector use: :func:`unwrap_repo_schedules_for_coverage` converts
# real ``list_funding_schedules`` rows (which nest the full segment inside
# ``schedule_json``) into coverage-ready segments without touching
# ``shortlab/hedge/market.py``. Production wiring may call the collector's
# existing ``repo.list_funding_schedules`` read plus this helper plus
# ``funding_schedule.compute_schedule_coverage``.
# ---------------------------------------------------------------------------

#: CR03 official default interval (D05.1: current default is 8h when proven).
DEFAULT_FUNDING_INTERVAL_HOURS = 8

#: CR03 TRADING status required for default-8h confirmation (D05.1).
TRADING_STATUS = "TRADING"


def _default_regime_interval_hours(regime: Any) -> int | None:
    if not isinstance(regime, dict):
        # Support Mapping-like objects with .get
        try:
            get = regime.get  # type: ignore[attr-defined]
        except AttributeError:
            return None
        if not callable(get):
            return None
        raw = None
        for key in ("interval_hours", "intervalHours", "fundingIntervalHours"):
            try:
                raw = get(key)
            except Exception:
                raw = None
            if raw is not None:
                break
    else:
        raw = None
        for key in ("interval_hours", "intervalHours", "fundingIntervalHours"):
            if regime.get(key) is not None:
                raw = regime.get(key)
                break
    if raw is None:
        return None
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _default_regime_version(regime: Any) -> str | None:
    try:
        get = regime.get  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if not callable(get):
        return None
    for key in ("version", "regime_version", "revision", "default_version", "regimeVersion"):
        try:
            raw = get(key)
        except Exception:
            continue
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _default_regime_known_at_ms(regime: Any) -> int | None:
    try:
        get = regime.get  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if not callable(get):
        return None
    for key in ("known_at_ms", "knownAt", "known_at", "fetched_at_ms", "receipt_known_at_ms"):
        try:
            raw = get(key)
        except Exception:
            continue
        if raw is None:
            continue
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _default_regime_source(regime: Any) -> str | None:
    try:
        get = regime.get  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if not callable(get):
        return None
    for key in ("source", "origin", "official_source"):
        try:
            raw = get(key)
        except Exception:
            continue
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def validate_default_regime(regime: Any) -> tuple[bool, str | None]:
    """Check an archived official current-default regime (D05.1 predicate c).

    ``True`` only when the regime explicitly states 8h AND carries a recorded
    version + receipt (``known_at`` + ``source``). Returns ``(ok, reason)``
    where ``reason`` is ``None`` on success, else a machine string
    (``DEFAULT_REGIME_*``) for honest UNKNOWN reporting. ``None``/malformed
    regimes are never treated as 8h proof.
    """
    if regime is None:
        return False, "DEFAULT_REGIME_MISSING"
    interval = _default_regime_interval_hours(regime)
    if interval is None:
        return False, "DEFAULT_REGIME_INTERVAL_UNKNOWN"
    if interval != DEFAULT_FUNDING_INTERVAL_HOURS:
        return False, "DEFAULT_REGIME_NOT_8H"
    if _default_regime_version(regime) is None:
        return False, "DEFAULT_REGIME_VERSION_MISSING"
    if _default_regime_known_at_ms(regime) is None:
        return False, "DEFAULT_REGIME_RECEIPT_MISSING"
    if _default_regime_source(regime) is None:
        return False, "DEFAULT_REGIME_SOURCE_MISSING"
    return True, None


def is_default_8h_confirmed(
    *,
    response_ok: bool,
    symbol: str,
    symbol_status: str | None,
    adjusted_symbols: Any,
    default_regime: Any,
) -> bool:
    """D05.1 default-8h CONFIRMED predicate (all four conditions).

    - ``response_ok``: complete HTTP200 business success for this fundingInfo
      fetch (``False`` on any transport/business failure keeps UNKNOWN).
    - ``symbol`` non-empty, ``symbol_status`` exactly ``"TRADING"``.
    - ``symbol`` not in ``adjusted_symbols`` (the fundingInfo adjusted list).
    - ``default_regime`` validates via :func:`validate_default_regime`
      (explicit 8h + recorded version/receipt).

    Missing any predicate returns ``False`` (UNKNOWN). Absence from the
    adjusted list alone never confirms 8h.
    """
    if not response_ok:
        return False
    if not isinstance(symbol, str) or not symbol:
        return False
    if symbol_status != TRADING_STATUS:
        return False
    try:
        if symbol in (adjusted_symbols or ()):  # type: ignore[operator]
            return False
    except TypeError:
        # Un hahable adjusted container: be conservative (UNKNOWN).
        try:
            if symbol in list(adjusted_symbols or ()):
                return False
        except Exception:
            return False
    ok, _ = validate_default_regime(default_regime)
    return bool(ok)


def _adjusted_symbols_of(info_rows: Any) -> set[str]:
    out: set[str] = set()
    rows = list(info_rows) if isinstance(info_rows, (list, tuple)) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        if isinstance(symbol, str) and symbol:
            out.add(symbol)
    return out


def default_8h_segments(
    symbols: Any,
    *,
    observed_at_ms: int,
    known_at_ms: int,
    adjusted_symbols: Any,
    symbol_statuses: Any | None = None,
    default_regime: Any | None = None,
    response_ok: bool = True,
    source: str = _FUNDING_INFO_SOURCE,
    evidence_ref_prefix: str = "fundingInfo",
) -> tuple[Any, ...]:
    """CONFIRMED/UNKNOWN 8h segments for symbols absent from fundingInfo.

    Each candidate yields one segment with ``effective_from`` equal to the
    observation boundary (never backfilled) and ``effective_to=None``.
    ``verification`` is ``CONFIRMED`` only when
    :func:`is_default_8h_confirmed` holds; otherwise ``UNKNOWN`` (callers
    persist only CONFIRMED, leaving history UNKNOWN). ``anchor`` mirrors the
    boundary per R03 (no unverified historical anchor is invented).
    """
    try:
        from diveintocrypto_desktop.shortlab.repair_contracts import (
            FundingScheduleSegment,
        )
    except Exception:  # pragma: no cover - contract import guard
        FundingScheduleSegment = None  # type: ignore[assignment]

    observed = int(observed_at_ms)
    known = int(known_at_ms)
    adjusted: set[str] = set()
    try:
        for s in (adjusted_symbols or ()):
            if isinstance(s, str) and s:
                adjusted.add(s)
    except TypeError:
        pass
    # Dedupe candidates, keep deterministic order.
    candidates: list[str] = []
    seen: set[str] = set()
    for s in (list(symbols) if isinstance(symbols, (list, tuple, set)) else []):
        if not isinstance(s, str) or not s or s in seen:
            continue
        seen.add(s)
        candidates.append(s)
    candidates.sort()
    version = _default_regime_version(default_regime) if default_regime is not None else None
    segments: list[Any] = []
    statuses: Any = symbol_statuses if isinstance(symbol_statuses, dict) else {}
    # Support Mapping-like statuses with .get
    for symbol in candidates:
        if symbol in adjusted:
            continue
        try:
            status = statuses.get(symbol) if hasattr(statuses, "get") else None
        except Exception:
            status = None
        confirmed = is_default_8h_confirmed(
            response_ok=bool(response_ok),
            symbol=symbol,
            symbol_status=status,
            adjusted_symbols=adjusted,
            default_regime=default_regime,
        )
        verification = "CONFIRMED" if confirmed else "UNKNOWN"
        schedule_id = f"{symbol}:{observed}:{DEFAULT_FUNDING_INTERVAL_HOURS}"
        if version:
            evidence_ref = f"{evidence_ref_prefix}:{symbol}:{known}:default:{version}"
        else:
            evidence_ref = f"{evidence_ref_prefix}:{symbol}:{known}"
        if FundingScheduleSegment is None:
            segments.append(
                {
                    "schedule_id": schedule_id,
                    "symbol": symbol,
                    "effective_from_ms": observed,
                    "effective_to_ms": None,
                    "interval_hours": DEFAULT_FUNDING_INTERVAL_HOURS,
                    "anchor_ms": observed,
                    "known_at_ms": known,
                    "source": source,
                    "evidence_ref": evidence_ref,
                    "verification": verification,
                }
            )
        else:
            segments.append(
                FundingScheduleSegment(
                    schedule_id=schedule_id,
                    symbol=symbol,
                    effective_from_ms=observed,
                    effective_to_ms=None,
                    interval_hours=DEFAULT_FUNDING_INTERVAL_HOURS,
                    anchor_ms=observed,
                    known_at_ms=known,
                    source=source,
                    evidence_ref=evidence_ref,
                    verification=verification,
                )
            )
    return tuple(segments)


def build_funding_schedule_segments(
    info_rows: Any,
    *,
    observed_at_ms: int,
    known_at_ms: int,
    symbols: Any | None = None,
    symbol_statuses: Any | None = None,
    default_regime: Any | None = None,
    response_ok: bool = True,
    source: str = _FUNDING_INFO_SOURCE,
    evidence_ref_prefix: str = "fundingInfo",
) -> tuple[Any, ...]:
    """Adjusted + default-8h segments for one fundingInfo snapshot (D05.1).

    Calls :func:`funding_info_to_schedule_segments` for adjusted symbols, then
    :func:`default_8h_segments` for ``symbols`` absent from the snapshot when
    ``symbols`` is not ``None``. With ``symbols=None`` the result is exactly
    the legacy adjusted-only tuple (backward compatible). No ``effective_from``
    is ever earlier than ``observed_at_ms`` (no history backfill).
    """
    adjusted = funding_info_to_schedule_segments(
        info_rows,
        observed_at_ms=observed_at_ms,
        known_at_ms=known_at_ms,
        source=source,
        evidence_ref_prefix=evidence_ref_prefix,
    )
    if symbols is None:
        return adjusted
    adjusted_set = _adjusted_symbols_of(info_rows)
    defaults = default_8h_segments(
        symbols,
        observed_at_ms=observed_at_ms,
        known_at_ms=known_at_ms,
        adjusted_symbols=adjusted_set,
        symbol_statuses=symbol_statuses,
        default_regime=default_regime,
        response_ok=response_ok,
        source=source,
        evidence_ref_prefix=evidence_ref_prefix,
    )
    combined = list(adjusted) + list(defaults)
    # Deterministic order by (symbol, effective_from, schedule_id).
    def _key(seg: Any) -> tuple[str, int, str]:
        try:
            if isinstance(seg, dict):
                return (str(seg.get("symbol", "")), int(seg.get("effective_from_ms", 0)), str(seg.get("schedule_id", "")))
            return (str(getattr(seg, "symbol", "")), int(getattr(seg, "effective_from_ms", 0)), str(getattr(seg, "schedule_id", "")))
        except Exception:
            return ("", 0, "")
    combined.sort(key=_key)
    return tuple(combined)


def schedule_segment_to_record(segment: Any) -> dict[str, Any]:
    """Convert a ``FundingScheduleSegment`` to a ``save_funding_schedule`` record.

    Record shape (R01): ``{schedule_id, symbol, effective_from_ms,
    effective_to_ms, known_at_ms, schedule_json}`` where ``schedule_json`` is
    the complete segment (never a lossy column subset, D18.2). Accepts both
    the dataclass and the fallback dict shape. Raises ``ValueError`` on
    invalid segments (callers keep UNKNOWN instead of persisting garbage).
    """
    if isinstance(segment, dict):
        data = dict(segment)
        try:
            schedule_id = str(data["schedule_id"])
            symbol = str(data["symbol"])
            eff_from = int(data["effective_from_ms"])  # type: ignore[arg-type]
            eff_to = data.get("effective_to_ms")
            eff_to = None if eff_to is None else int(eff_to)  # type: ignore[arg-type]
            known = int(data["known_at_ms"])  # type: ignore[arg-type]
            interval = int(data["interval_hours"])  # type: ignore[arg-type]
            anchor = int(data["anchor_ms"])  # type: ignore[arg-type]
            source = str(data["source"])
            evidence_ref = str(data["evidence_ref"])
            verification = str(data["verification"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid schedule dict: {exc}") from exc
        if not schedule_id or not symbol or not source or not evidence_ref:
            raise ValueError("schedule dict misses required str fields")
        if eff_from < 0 or known < 0 or anchor < 0:
            raise ValueError("schedule times must be >= 0")
        if eff_to is not None and eff_to <= eff_from:
            raise ValueError("effective_to_ms must be > effective_from_ms")
        if interval <= 0:
            raise ValueError("interval_hours must be > 0")
        if verification not in ("CONFIRMED", "INFERRED", "UNKNOWN"):
            raise ValueError(f"verification={verification!r} unknown")
        schedule_json = {
            "schedule_id": schedule_id,
            "symbol": symbol,
            "effective_from_ms": eff_from,
            "effective_to_ms": eff_to,
            "interval_hours": interval,
            "anchor_ms": anchor,
            "known_at_ms": known,
            "source": source,
            "evidence_ref": evidence_ref,
            "verification": verification,
        }
        return {
            "schedule_id": schedule_id,
            "symbol": symbol,
            "effective_from_ms": eff_from,
            "effective_to_ms": eff_to,
            "known_at_ms": known,
            "schedule_json": schedule_json,
        }
    # Dataclass (or compatible object with attributes).
    try:
        schedule_id = str(getattr(segment, "schedule_id"))
        symbol = str(getattr(segment, "symbol"))
        eff_from = int(getattr(segment, "effective_from_ms"))
        eff_to_raw = getattr(segment, "effective_to_ms")
        eff_to = None if eff_to_raw is None else int(eff_to_raw)
        known = int(getattr(segment, "known_at_ms"))
        interval = int(getattr(segment, "interval_hours"))
        anchor = int(getattr(segment, "anchor_ms"))
        source = str(getattr(segment, "source"))
        evidence_ref = str(getattr(segment, "evidence_ref"))
        verification = str(getattr(segment, "verification"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid schedule segment: {exc}") from exc
    if not schedule_id or not symbol or not source or not evidence_ref:
        raise ValueError("schedule misses required str fields")
    if eff_from < 0 or known < 0 or anchor < 0:
        raise ValueError("schedule times must be >= 0")
    if eff_to is not None and eff_to <= eff_from:
        raise ValueError("effective_to_ms must be > effective_from_ms")
    if interval <= 0:
        raise ValueError("interval_hours must be > 0")
    if verification not in ("CONFIRMED", "INFERRED", "UNKNOWN"):
        raise ValueError(f"verification={verification!r} unknown")
    schedule_json = {
        "schedule_id": schedule_id,
        "symbol": symbol,
        "effective_from_ms": eff_from,
        "effective_to_ms": eff_to,
        "interval_hours": interval,
        "anchor_ms": anchor,
        "known_at_ms": known,
        "source": source,
        "evidence_ref": evidence_ref,
        "verification": verification,
    }
    return {
        "schedule_id": schedule_id,
        "symbol": symbol,
        "effective_from_ms": eff_from,
        "effective_to_ms": eff_to,
        "known_at_ms": known,
        "schedule_json": schedule_json,
    }


def _unwrap_one_repo_schedule(row: Any) -> dict[str, Any] | None:
    """Unwrap one ``list_funding_schedules`` row to a coverage-ready mapping.

    Real rows nest the full segment inside ``schedule_json``; the top-level
    columns alone lack ``interval_hours``/``anchor``/``verification`` and are
    skipped by ``compute_schedule_coverage`` (which stays UNKNOWN). This helper
    prefers ``schedule_json`` when it is a mapping, else falls back to the
    top-level columns. Returns ``None`` when neither shape is usable.
    """
    if isinstance(row, dict):
        nested = row.get("schedule_json")
        if isinstance(nested, dict) and nested.get("schedule_id") and nested.get("symbol"):
            # Prefer the nested full segment (D18.2 complete JSON).
            merged = dict(nested)
            # Fill receipt times from top-level when nested omits them.
            for key in ("effective_from_ms", "effective_to_ms", "known_at_ms"):
                if merged.get(key) is None and row.get(key) is not None:
                    merged[key] = row.get(key)
            return merged
        # Fallback: top-level already carries the full DTO keys.
        if row.get("schedule_id") and row.get("symbol"):
            return dict(row)
        return None
    # FundingScheduleSegment dataclass passes through.
    try:
        from diveintocrypto_desktop.shortlab.repair_contracts import FundingScheduleSegment as _Seg
        if isinstance(row, _Seg):
            return row  # type: ignore[return-value]
    except Exception:
        pass
    # Generic attribute object: convert to mapping.
    try:
        return {
            "schedule_id": str(getattr(row, "schedule_id")),
            "symbol": str(getattr(row, "symbol")),
            "effective_from_ms": int(getattr(row, "effective_from_ms")),
            "effective_to_ms": None if getattr(row, "effective_to_ms") is None else int(getattr(row, "effective_to_ms")),
            "interval_hours": int(getattr(row, "interval_hours")),
            "anchor_ms": int(getattr(row, "anchor_ms")),
            "known_at_ms": int(getattr(row, "known_at_ms")),
            "source": str(getattr(row, "source")),
            "evidence_ref": str(getattr(row, "evidence_ref")),
            "verification": str(getattr(row, "verification")),
        }
    except Exception:
        return None


def unwrap_repo_schedules_for_coverage(rows: Any) -> tuple[Any, ...]:
    """Convert ``list_funding_schedules`` rows to coverage-ready segments.

    Read-only helper for the Collector path (``market.py`` is not modified):
    callers keep using ``repo.list_funding_schedules(symbol, as_of)`` for the
    read, then pass the result through here before
    ``funding_schedule.compute_schedule_coverage``. Unusable rows are dropped
    (coverage stays UNKNOWN via absence, never fabricated).
    """
    if rows is None:
        return ()
    try:
        items = list(rows)  # type: ignore[arg-type]
    except TypeError:
        return ()
    out: list[Any] = []
    for row in items:
        unwrapped = _unwrap_one_repo_schedule(row)
        if unwrapped is not None:
            out.append(unwrapped)
    return tuple(out)


def _segment_interval_hours(seg: Any) -> int | None:
    if isinstance(seg, dict):
        nested = seg.get("schedule_json") if "schedule_json" in seg else None
        if isinstance(nested, dict):
            for key in ("interval_hours", "intervalHours"):
                if nested.get(key) is not None:
                    try:
                        value = int(nested.get(key))  # type: ignore[arg-type]
                        return value if value > 0 else None
                    except (TypeError, ValueError):
                        return None
        for key in ("interval_hours", "intervalHours"):
            if seg.get(key) is not None:
                try:
                    value = int(seg.get(key))  # type: ignore[arg-type]
                    return value if value > 0 else None
                except (TypeError, ValueError):
                    return None
        return None
    try:
        value = int(getattr(seg, "interval_hours"))
        return value if value > 0 else None
    except (AttributeError, TypeError, ValueError):
        return None


def _segment_verification(seg: Any) -> str | None:
    if isinstance(seg, dict):
        nested = seg.get("schedule_json") if "schedule_json" in seg else None
        if isinstance(nested, dict) and isinstance(nested.get("verification"), str):
            return nested.get("verification")
        value = seg.get("verification")
        return value if isinstance(value, str) else None
    try:
        value = getattr(seg, "verification")
        return value if isinstance(value, str) else None
    except AttributeError:
        return None


def _segment_effective_from(seg: Any) -> int | None:
    if isinstance(seg, dict):
        nested = seg.get("schedule_json") if "schedule_json" in seg else None
        if isinstance(nested, dict) and nested.get("effective_from_ms") is not None:
            try:
                return int(nested.get("effective_from_ms"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        try:
            return int(seg.get("effective_from_ms"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    try:
        return int(getattr(seg, "effective_from_ms"))
    except (AttributeError, TypeError, ValueError):
        return None


def _segment_effective_to(seg: Any) -> int | None:
    if isinstance(seg, dict):
        nested = seg.get("schedule_json") if "schedule_json" in seg else None
        if isinstance(nested, dict) and "effective_to_ms" in nested:
            raw = nested.get("effective_to_ms")
            if raw is None:
                return None
            try:
                return int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
        raw = seg.get("effective_to_ms")
        if raw is None:
            return None
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    try:
        raw = getattr(seg, "effective_to_ms")
        return None if raw is None else int(raw)
    except (AttributeError, TypeError, ValueError):
        return None


async def collect_and_archive_funding_schedules(
    *,
    repository: Any,
    symbols: Any | None = None,
    symbol_statuses: Any | None = None,
    default_regime: Any | None = None,
    observed_at_ms: int | None = None,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: RequestContext | None = None,
    persist_raw_observation: bool = True,
    persist_only_confirmed: bool = True,
    evidence_ref_prefix: str = "fundingInfo",
) -> dict[str, Any]:
    """Fetch fundingInfo once and archive CONFIRMED schedule segments (D05.1).

    Production collection task (CR03): the single writer for
    ``save_funding_schedule``. Steps:

    1. ``fetch_funding_info_observed`` (single ``fundingInfo`` charge, 429/5xx
       retried via ``data/http.py``). The ``Observed`` receipt
       (``known_at``/``fetched_at``) is preserved verbatim in every saved
       segment (never restamped with save time).
    2. Build adjusted + default-8h segments with ``effective_from`` equal to
       the observation boundary (``known_at`` unless ``observed_at_ms`` is
       explicitly injected for tests). History is never backfilled.
    3. Idempotent archive: a CONFIRMED segment is skipped when a covering
       CONFIRMED row with the same interval already exists
       (``effective_from <= observed`` and open). Stale open CONFIRMED rows
       with a different interval are closed with a same-``effective_from``
       revision (new ``schedule_id``, ``effective_to=observed``) so the
       transition does not leave a permanent overlap-UNKNOWN.
    4. Optionally persist the raw fundingInfo document via
       ``save_market_observation`` (``kind=FUNDING_INFO``) for audit; failures
       there never block schedule writes.

    On transport/business failure no schedule is written and the result keeps
    ``FUNDING_SCHEDULE_UNKNOWN`` (history stays UNKNOWN). Only CONFIRMED
    segments are persisted when ``persist_only_confirmed`` is true (default);
    UNKNOWN candidates are reported but not archived.

    Returns ``{observed_at_ms, known_at_ms, response_ok, adjusted_symbols,
    segments, saved_ids, closed_ids, raw_observation_id, reasons}``.
    ``segments`` are the built dataclass/dict objects (CONFIRMED + UNKNOWN);
    ``saved_ids`` are persisted CONFIRMED ids; ``reasons`` carries
    ``HISTORY_BOOTSTRAPPING`` when no verifiable CONFIRMED archive existed
    before this collection, else ``()``.
    """
    # 1. Fetch (single charge). Failure keeps UNKNOWN without writes.
    try:
        observed = await fetch_funding_info_observed(
            now_ms=now_ms,
            identity_snapshot_id=identity_snapshot_id,
            request_context=request_context,
        )
    except Exception as exc:
        reason = str(getattr(exc, "reason_code", None) or type(exc).__name__)
        return {
            "observed_at_ms": None,
            "known_at_ms": None,
            "response_ok": False,
            "adjusted_symbols": (),
            "segments": (),
            "saved_ids": (),
            "closed_ids": (),
            "raw_observation_id": None,
            "reasons": (FUNDING_SCHEDULE_UNKNOWN,),
            "error": reason[:200],
        }
    try:
        rows = _obs.to_legacy(observed)
    except Exception:
        try:
            rows = getattr(observed, "value")
        except Exception:
            rows = []
    try:
        known_at = int(getattr(getattr(observed, "meta", None), "known_at_ms"))
    except (AttributeError, TypeError, ValueError):
        known_at = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    observed_at = int(observed_at_ms) if observed_at_ms is not None else int(known_at)
    adjusted_set = _adjusted_symbols_of(rows)

    # 2. Build (adjusted + defaults). response_ok=True here (fetch succeeded).
    universe: Any | None = symbols
    if universe is None and isinstance(symbol_statuses, dict):
        universe = tuple(symbol_statuses.keys())
    segments = build_funding_schedule_segments(
        rows,
        observed_at_ms=observed_at,
        known_at_ms=known_at,
        symbols=universe,
        symbol_statuses=symbol_statuses,
        default_regime=default_regime,
        response_ok=True,
        evidence_ref_prefix=evidence_ref_prefix,
    )

    # 3. Archive. Determine pre-existing CONFIRMED cover to report
    # HISTORY_BOOTSTRAPPING honestly (no verifiable old regime before now).
    has_archive = False
    existing_by_symbol: dict[str, list[Any]] = {}
    if repository is not None and hasattr(repository, "list_funding_schedules"):
        # Probe each relevant symbol once (best-effort; failures mean no archive).
        probe_symbols: set[str] = set(adjusted_set)
        if isinstance(universe, (list, tuple, set)):
            for s in universe:
                if isinstance(s, str) and s:
                    probe_symbols.add(s)
        for sym in sorted(probe_symbols):
            try:
                existing = await repository.list_funding_schedules(sym, int(known_at))
            except Exception:
                continue
            existing_by_symbol[sym] = list(existing or ())
            for row in existing_by_symbol[sym]:
                unwrapped = _unwrap_one_repo_schedule(row)
                if unwrapped is None:
                    continue
                ver = _segment_verification(unwrapped)
                if ver != "CONFIRMED":
                    continue
                # Any CONFIRMED known by now counts as a verifiable archive,
                # even when its window does not yet cover the new boundary
                # (it proves an old regime was once confirmed).
                has_archive = True
                break
            if has_archive:
                # Keep probing for idempotency/close decisions below, but the
                # flag is already set. Continue to fill existing_by_symbol for
                # symbols we will actually save (lazy: only those with new
                # CONFIRMED segments).
                pass

    saved_ids: list[str] = []
    closed_ids: list[str] = []
    # 4. Persist raw document for audit (best-effort, never blocks schedules).
    raw_oid: str | None = None
    if persist_raw_observation and repository is not None and hasattr(repository, "save_market_observation"):
        try:
            record = _obs.observation_to_record(
                observed, kind="FUNDING_INFO", symbol="FUNDING_INFO",
            )
            # Deterministic id for idempotent retries (same known_at => same id).
            try:
                record = dict(record)
                record["observation_id"] = f"FUNDING_INFO:{known_at}"
            except Exception:
                pass
            raw_oid = await repository.save_market_observation(record)
        except Exception:
            raw_oid = None

    # 5. Persist CONFIRMED segments (idempotent + close stale opens).
    for seg in segments:
        try:
            ver = seg.get("verification") if isinstance(seg, dict) else getattr(seg, "verification")
        except (AttributeError, TypeError):
            continue
        if persist_only_confirmed and ver != "CONFIRMED":
            continue
        try:
            record = schedule_segment_to_record(seg)
        except ValueError:
            continue
        sym = record["symbol"]
        # Ensure we have existing rows for this symbol (fetch lazily).
        if sym not in existing_by_symbol and repository is not None and hasattr(repository, "list_funding_schedules"):
            try:
                existing_by_symbol[sym] = list(await repository.list_funding_schedules(sym, int(known_at)) or ())
            except Exception:
                existing_by_symbol[sym] = []
        existing = existing_by_symbol.get(sym, [])
        # Idempotency: skip when a covering CONFIRMED with the same interval
        # already exists (stable regime must not create overlapping duplicates
        # on every tick, otherwise D05.2 overlap would force permanent UNKNOWN).
        try:
            new_interval = int(record["schedule_json"]["interval_hours"])
        except (KeyError, TypeError, ValueError):
            continue
        skip = False
        stale_opens: list[Any] = []
        for row in existing:
            unwrapped = _unwrap_one_repo_schedule(row)
            if unwrapped is None:
                continue
            if _segment_verification(unwrapped) != "CONFIRMED":
                continue
            try:
                ex_interval = _segment_interval_hours(unwrapped)
                ex_from = _segment_effective_from(unwrapped)
                ex_to = _segment_effective_to(unwrapped)
            except Exception:
                continue
            if ex_interval is None or ex_from is None:
                continue
            # Covering open row: [ex_from, ex_to or +inf) contains observed.
            covers = ex_from <= observed_at and (ex_to is None or ex_to > observed_at)
            if not covers:
                continue
            if ex_interval == new_interval:
                skip = True
                break
            # Different interval but still covering: stale open to close.
            if ex_to is None:
                stale_opens.append((row, unwrapped))
        if skip:
            continue
        # Close stale opens with same-effective_from revisions (new ids, never
        # mutating old rows). The revision keeps the original interval/anchor
        # but caps the window at the new observation boundary.
        for _row, unwrapped in stale_opens:
            try:
                if isinstance(unwrapped, dict):
                    ex_from = int(unwrapped.get("effective_from_ms"))  # type: ignore[arg-type]
                    ex_interval = int(unwrapped.get("interval_hours"))  # type: ignore[arg-type]
                    ex_anchor = int(unwrapped.get("anchor_ms", ex_from))  # type: ignore[arg-type]
                    ex_source = str(unwrapped.get("source", _FUNDING_INFO_SOURCE))
                else:
                    ex_from = int(getattr(unwrapped, "effective_from_ms"))
                    ex_interval = int(getattr(unwrapped, "interval_hours"))
                    ex_anchor = int(getattr(unwrapped, "anchor_ms"))
                    ex_source = str(getattr(unwrapped, "source"))
            except (TypeError, ValueError, AttributeError):
                continue
            if not (ex_from < observed_at):
                continue
            close_id = f"{sym}:{ex_from}:{ex_interval}:close:{observed_at}:{known_at}"
            close_evidence = f"{evidence_ref_prefix}:{sym}:{known_at}:close:{observed_at}"
            try:
                from diveintocrypto_desktop.shortlab.repair_contracts import FundingScheduleSegment as _Seg2
                close_seg = _Seg2(
                    schedule_id=close_id,
                    symbol=sym,
                    effective_from_ms=ex_from,
                    effective_to_ms=observed_at,
                    interval_hours=ex_interval,
                    anchor_ms=ex_anchor,
                    known_at_ms=known_at,
                    source=ex_source,
                    evidence_ref=close_evidence,
                    verification="CONFIRMED",
                )
            except Exception:
                close_seg = {
                    "schedule_id": close_id,
                    "symbol": sym,
                    "effective_from_ms": ex_from,
                    "effective_to_ms": observed_at,
                    "interval_hours": ex_interval,
                    "anchor_ms": ex_anchor,
                    "known_at_ms": known_at,
                    "source": ex_source,
                    "evidence_ref": close_evidence,
                    "verification": "CONFIRMED",
                }
            try:
                close_record = schedule_segment_to_record(close_seg)
            except ValueError:
                continue
            if repository is not None and hasattr(repository, "save_funding_schedule"):
                try:
                    cid = await repository.save_funding_schedule(close_record)
                    closed_ids.append(str(cid))
                    # Treat the close as now-existing for subsequent segments.
                    existing_by_symbol.setdefault(sym, []).append(close_record)
                except Exception:
                    continue
        # Save the new segment itself.
        if repository is not None and hasattr(repository, "save_funding_schedule"):
            try:
                sid = await repository.save_funding_schedule(record)
                saved_ids.append(str(sid))
                existing_by_symbol.setdefault(sym, []).append(record)
            except Exception:
                continue

    reasons = () if has_archive else (HISTORY_BOOTSTRAPPING,)
    return {
        "observed_at_ms": observed_at,
        "known_at_ms": known_at,
        "response_ok": True,
        "adjusted_symbols": tuple(sorted(adjusted_set)),
        "segments": segments,
        "saved_ids": tuple(saved_ids),
        "closed_ids": tuple(closed_ids),
        "raw_observation_id": raw_oid,
        "reasons": reasons,
    }

