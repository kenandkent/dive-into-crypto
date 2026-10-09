"""Order-book snapshot (panel-only) from the public ``/fapi/v1/depth`` endpoint.

``limit=50`` (request weight 2) gives enough depth to measure book pressure at
the 0.5% / 1% / 2% bands around the mid price: each band's imbalance is
``Σbid − Σask`` normalized by the band's total notional (−1 ask-heavy … +1
bid-heavy). ``notional_1pct`` is the summed bid+ask notional (USDT) inside the
1% band — a thin-book honesty read; ``bid_notional_1pct`` /
``ask_notional_1pct`` are the per-side ``Σ(price×qty)`` components (design
§8.2, Tradeability takes ``min`` of the two against the 0.25M/1M single-side
thresholds, ``None`` when either side is missing). A book too thin to fill
even the 0.5% band reports ``{"unavailable": "book_too_thin"}`` instead of
degenerate numbers.

Panel-only by design: served by ``GET /api/symbol/{symbol}``, never attached to
scan rows (a full scan must not multiply depth calls). Cached 10 seconds.

R03 (D04.3/D18.2): raw book sides are retained separately (bids vs asks) so
R06a/R06b/R11a/R11b can compute per-side contract VWAP + coverage (never a
merged total alone). ``fetch_book_observed`` wraps the verbatim sides + panel
as an ``Observed`` with its receipt; legacy :func:`snapshot` keeps its exact
return shape (``to_legacy`` compatible).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from diveintocrypto_desktop.data.http import FAPI_V1, get_json
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

logger = logging.getLogger("trading_bot.data.orderbook")

DEPTH_LIMIT = 50
CACHE_TTL = 10.0
BANDS_PCT = [0.005, 0.01, 0.02]
THIN_BOOK_MIN = 1000.0  # USDT notional inside the 0.5% band below which we refuse to read

_cache: dict[str, tuple[float, dict]] = {}


def reset_cache() -> None:
    """Drop the snapshot cache (test hook)."""
    _cache.clear()


async def _budgeted_get_json(url: str, params: dict[str, Any] | None, ctx: Any | None) -> Any:
    """GET preserving legacy 2-arg fakes (R03). Only forwards the keyword on
    the budgeted path; the HTTP layer charges once (no duplicate budgeting)."""
    if ctx is not None and getattr(ctx, "budget", None) is not None:
        return await get_json(url, params, request_context=ctx)
    if params is None:
        return await get_json(url)
    return await get_json(url, params)


def _resolve_book_context(request_context: Any | None) -> Any | None:
    if request_context is not None:
        return request_context
    try:
        return get_current_request_context()
    except Exception:
        return None


async def fetch_depth(
    symbol: str, limit: int = DEPTH_LIMIT, *, request_context: Any | None = None
) -> dict:
    """Raw depth payload ``{bids: [[p, q]…], asks: [[p, q]…]}`` (sides verbatim).

    R03: accepts keyword ``request_context`` forwarded to the shared HTTP
    layer (``futuresDepth`` family per D19.3; existing families untouched, no
    duplicate charge here).
    """
    ctx = _resolve_book_context(request_context)
    return await _budgeted_get_json(f"{FAPI_V1}/depth", {"symbol": symbol, "limit": limit}, ctx)


def parse_levels(rows: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for r in rows or []:
        try:
            p, q = float(r[0]), float(r[1])
        except (IndexError, TypeError, ValueError):
            continue
        if p > 0 and q > 0:
            out.append((p, q))
    return out


def book_panel(bids: list[tuple[float, float]], asks: list[tuple[float, float]],
               now_ms: int | None = None) -> dict:
    """Imbalance bands + 1% notional from parsed level lists (pure).

    ``mid = (best_bid + best_ask) / 2``; each side is truncated at ``mid ±
    band`` (bids keep ``p >= mid*(1-band)``, asks keep ``p <= mid*(1+band)``)
    and valued as ``Σ(price×qty)``. ``notional_1pct`` is the legacy bilateral
    total (unchanged); ``bid_notional_1pct`` / ``ask_notional_1pct`` are the
    per-side 1% components — ``None`` when that side contributes no level
    inside the band. Either input list empty (or a non-positive best quote)
    is ``{"unavailable": "book_too_thin"}``, as is a sub-1000-USDT 0.5% band.
    """
    if not bids or not asks:
        return {"unavailable": "book_too_thin"}
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid <= 0 or best_ask <= 0:
        return {"unavailable": "book_too_thin"}
    mid = (best_bid + best_ask) / 2.0

    bands: dict[str, float] = {}
    notional_1pct = None
    bid_notional_1pct: float | None = None
    ask_notional_1pct: float | None = None
    total05 = 0.0
    for band in BANDS_PCT:
        bid_levels = [p * q for p, q in bids if p >= mid * (1 - band)]
        ask_levels = [p * q for p, q in asks if p <= mid * (1 + band)]
        bid_sum = sum(bid_levels)
        ask_sum = sum(ask_levels)
        total = bid_sum + ask_sum
        if band == 0.005:
            total05 = total
        if band == 0.01:
            notional_1pct = round(total, 2)
            bid_notional_1pct = round(bid_sum, 2) if bid_levels else None
            ask_notional_1pct = round(ask_sum, 2) if ask_levels else None
        bands[f"{int(band * 1000) / 10:g}%"] = (
            round((bid_sum - ask_sum) / total, 4) if total > 0 else None
        )

    if total05 < THIN_BOOK_MIN:
        return {"unavailable": "book_too_thin"}

    import time as _time

    ts = int(now_ms if now_ms is not None else _time.time() * 1000)
    return {
        "imbalance": bands,
        "notional_1pct": notional_1pct,
        "bid_notional_1pct": bid_notional_1pct,
        "ask_notional_1pct": ask_notional_1pct,
        "mid": mid,
        "ts": ts,
    }


def book_depth_min_1pct(panel: dict) -> float | None:
    """Tradeability input: ``min(bid_notional_1pct, ask_notional_1pct)``.

    ``None`` when the panel is unavailable or either side is missing —
    scoring must treat that factor as null, never as zero.
    """
    if not isinstance(panel, dict) or "unavailable" in panel:
        return None
    bid = panel.get("bid_notional_1pct")
    ask = panel.get("ask_notional_1pct")
    if bid is None or ask is None:
        return None
    try:
        return min(float(bid), float(ask))
    except (TypeError, ValueError):
        return None


async def snapshot(symbol: str, *, request_context: Any | None = None) -> dict:
    """Cached ``book`` block (10s TTL on SUCCESS only); ``{"unavailable": reason}``
    on failure. Errors are never cached — a failed fetch is re-attempted on the
    next call instead of serving the stale failure for the TTL window.
    R03: accepts keyword ``request_context`` (forwarded; legacy callers omit it).
    """
    now = time.monotonic()
    cached = _cache.get(symbol)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    try:
        ctx = _resolve_book_context(request_context)
        if ctx is None:
            raw = await fetch_depth(symbol)
        else:
            raw = await fetch_depth(symbol, request_context=ctx)
        panel = book_panel(parse_levels(raw.get("bids")), parse_levels(raw.get("asks")))
    except Exception as e:
        logger.warning("orderbook: %s fetch failed — %s", symbol, str(e)[:80])
        panel = {"unavailable": str(e)[:120]}
    if "unavailable" in panel:
        return panel  # never cached — re-attempted on the next call
    _cache[symbol] = (time.monotonic(), panel)
    return panel


_BOOK_SOURCE = "binance-futures-book"


async def fetch_book_observed(
    symbol: str,
    limit: int = DEPTH_LIMIT,
    *,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: Any | None = None,
) -> _obs.Observed[dict]:
    """Raw book sides + panel wrapped as an ``Observed`` (R03/D18.2).

    ``value`` retains both sides separately -- ``{"symbol", "bids", "asks",
    "panel"}`` where ``bids``/``asks`` are the verbatim ``[[price, qty]]``
    string pairs from ``/fapi/v1/depth`` (never merged into a single total),
    and ``panel`` is the legacy :func:`book_panel` block. ``meta.source_as_of``
    is the panel ``ts`` (``None`` when unavailable); ``known_at`` is the
    completion time. Cache is not used here (every call is a fresh receipt);
    :func:`snapshot` keeps its 10s panel cache.
    """
    ctx = _resolve_book_context(request_context)
    try:
        if ctx is None:
            raw = await fetch_depth(symbol, limit)
        else:
            raw = await fetch_depth(symbol, limit, request_context=ctx)
    except Exception as exc:
        completed_err = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        unavailable = {"unavailable": str(exc)[:120], "symbol": symbol, "bids": [], "asks": []}
        return _obs.make_observation(
            unavailable,
            source=_BOOK_SOURCE,
            source_as_of_ms=None,
            fetched_at_ms=completed_err,
            known_at_ms=completed_err,
            status="UNAVAILABLE",
            reason_code=str(getattr(exc, "reason_code", "BOOK_UNAVAILABLE") or "BOOK_UNAVAILABLE"),
            identity_snapshot_id=identity_snapshot_id,
        )
    bids_raw = raw.get("bids") if isinstance(raw, dict) else []
    asks_raw = raw.get("asks") if isinstance(raw, dict) else []
    panel = book_panel(parse_levels(bids_raw), parse_levels(asks_raw), now_ms=now_ms)
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    source_as_of = panel.get("ts") if isinstance(panel, dict) else None
    try:
        source_as_of = int(source_as_of) if source_as_of is not None else completed
    except (TypeError, ValueError):
        source_as_of = completed
    value = {
        "symbol": symbol,
        "bids": list(bids_raw) if isinstance(bids_raw, list) else [],
        "asks": list(asks_raw) if isinstance(asks_raw, list) else [],
        "panel": dict(panel) if isinstance(panel, dict) else panel,
    }
    status = "OK" if isinstance(panel, dict) and "unavailable" not in panel else "UNAVAILABLE"
    return _obs.make_observation(
        value,
        source=_BOOK_SOURCE,
        source_as_of_ms=source_as_of,
        fetched_at_ms=completed,
        known_at_ms=completed,
        status=status,
        reason_code=None if status == "OK" else panel.get("unavailable") if isinstance(panel, dict) else "BOOK_UNAVAILABLE",
        units=_obs.ObservationUnits(quote_asset="USDT"),
        identity_snapshot_id=identity_snapshot_id,
    )
