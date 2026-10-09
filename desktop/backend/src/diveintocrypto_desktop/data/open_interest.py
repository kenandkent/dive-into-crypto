"""Open-interest history via Crypcodile's Binance OI parser.

Raw Binance ``/futures/data/openInterestHist`` rows carry
``{symbol, sumOpenInterest, sumOpenInterestValue, timestamp}``:

- ``sumOpenInterest`` (``oi``) is a **quantity** in base-asset units
  (e.g. BTC for BTCUSDT) — never a USD amount.
- ``sumOpenInterestValue`` (``oi_value``) is the **nominal value** in quote
  units (USDT for USDT-M) — the only field that may approximate USD.

Crypcodile ``parse_open_interest_hist`` maps these verbatim to
``open_interest`` / ``open_interest_value``. This module adds the Short-Lab
unit assertion (design §8.2): only a quote-USDT nominal that reproduces
``sumOpenInterest × premiumIndex.markPrice`` within 5% — with the mark
response no more than 5 minutes away from the OI point — may be exposed as
``oi_value_usd``. Anything else is ``None`` + ``OI_UNIT_UNVERIFIED``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from crypcodile.exchanges.binance.backfill import _live_fetch_open_interest_hist, parse_open_interest_hist

from diveintocrypto_desktop.data.http import FAPI_DATA, TransientUpstreamError, run_with_retries
from diveintocrypto_desktop.shortlab import observations as _obs

try:  # pragma: no cover - import guard
    from diveintocrypto_desktop.shortlab.request_budget import (
        RequestContext,
        get_current_request_context,
    )
except Exception:  # pragma: no cover
    from typing import Any as _Any

    RequestContext = _Any  # type: ignore[assignment,misc]
    def get_current_request_context():  # type: ignore[no-redef]
        return None

_VENUE = "binance-usdm"

# Binance publishes openInterestHist for these periods only.
OI_PERIODS = ["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"]

# ── Short-Lab OI unit assertion (design §8.2) ───────────────────────────────
# The mark must come from premiumIndex.markPrice with a response time within
# 5 minutes of the nearest OI point; the nominal must reproduce
# oi_quantity × mark_price within 5% relative error. Any failure (missing
# sync time, stale mark, unit mismatch, error above tolerance) yields
# oi_value_usd=None + OI_UNIT_UNVERIFIED — never a base-quantity-as-USD.
OI_UNIT_UNVERIFIED = "OI_UNIT_UNVERIFIED"
OI_MAX_SYNC_MS = 5 * 60 * 1000
OI_VERIFY_TOLERANCE = 0.05
USD_QUOTE_UNITS = ("USD", "USDT")
_UNKNOWN_QUOTE_UNITS = ("", "UNKNOWN", "UNVERIFIED", "NULL", "N/A", "NA", "?", "-")


def _to_ms(ts: Any) -> int | None:
    """Normalise an OI/mark timestamp to ms.

    ``fetch_oi_hist`` points carry ``t`` in **ns** while ``premiumIndex``
    ``time`` is in **ms**; accept either scale (ns values are >= 1e14).
    """
    if ts is None:
        return None
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return None
    if v >= 10**14:  # nanoseconds → milliseconds
        v //= 1_000_000
    return v


def _normalise_quote_unit(unit: Any) -> str | None:
    """Upper-cased quote unit, or ``None`` when unknown/unusable."""
    if unit is None:
        return None
    u = str(unit).strip().upper()
    if not u or u in _UNKNOWN_QUOTE_UNITS:
        return None
    return u


def resolve_oi_value_usd(
    oi_quantity: float | None,
    oi_value: float | None,
    *,
    mark_price: float | None,
    oi_time_ms: int | None,
    mark_time_ms: int | None,
    quote_unit: str | None = "USDT",
) -> dict:
    """Verify a raw OI nominal and expose it as USD (pure).

    ``oi_quantity`` is ``sumOpenInterest`` (base units, e.g. BTC);
    ``oi_value`` is ``sumOpenInterestValue`` (quote nominal, USDT on USDT-M);
    ``mark_price`` must be ``premiumIndex.markPrice`` and ``mark_time_ms`` its
    response ``time``. ``oi_time_ms``/``mark_time_ms`` accept ms or ns.

    Returns ``{oi_value_usd, oi_value_source, reason_code, relative_error,
    sync_ms}``. On success ``reason_code`` is ``None`` and ``oi_value_source``
    records the conversion path. On any failure ``oi_value_usd`` is ``None``
    with ``reason_code == OI_UNIT_UNVERIFIED``:

    - unknown/empty ``quote_unit`` → null (no guessing);
    - ``USD``/``USDT`` quote → ``oi_value`` used directly as the USD
      approximation (``USDT≈USD``);
    - any other known quote unit → explicit ``oi_value × mark_price``
      conversion, sourced as
      ``sumOpenInterestValue(<UNIT>)×premiumIndex.markPrice``;
    - missing mark price, missing sync time, >5min staleness, or relative
      error ``abs(candidate - oi_quantity×mark)/max(candidate,1) > 0.05``
      → null.
    """
    oi_ms = _to_ms(oi_time_ms)
    mk_ms = _to_ms(mark_time_ms)
    unit = _normalise_quote_unit(quote_unit)

    def _fail(rel_err: float | None, sync: int | None) -> dict:
        return {
            "oi_value_usd": None,
            "oi_value_source": None,
            "reason_code": OI_UNIT_UNVERIFIED,
            "relative_error": rel_err,
            "sync_ms": sync,
        }

    try:
        qty = float(oi_quantity) if oi_quantity is not None else None
        val = float(oi_value) if oi_value is not None else None
        mark = float(mark_price) if mark_price is not None else None
    except (TypeError, ValueError):
        return _fail(None, None)
    if qty is None or val is None or mark is None:
        return _fail(None, None)
    if not (qty > 0 and val > 0 and mark > 0):
        return _fail(None, None)
    if unit is None:
        return _fail(None, None)
    if oi_ms is None or mk_ms is None:
        return _fail(None, None)
    sync_ms = abs(mk_ms - oi_ms)
    if sync_ms > OI_MAX_SYNC_MS:
        return _fail(None, sync_ms)

    if unit in USD_QUOTE_UNITS:
        candidate = val
        source = "sumOpenInterestValue(USDT≈USD)"
    else:
        candidate = val * mark
        source = f"sumOpenInterestValue({unit})×premiumIndex.markPrice"
    expected = qty * mark
    rel_err = abs(candidate - expected) / max(candidate, 1.0)
    if rel_err > OI_VERIFY_TOLERANCE:
        return _fail(rel_err, sync_ms)
    return {
        "oi_value_usd": candidate,
        "oi_value_source": source,
        "reason_code": None,
        "relative_error": rel_err,
        "sync_ms": sync_ms,
    }


# The plan names the interface ``oi_value_usd``; keep it as an alias so both
# the field name and the resolver are importable under that contract.
oi_value_usd = resolve_oi_value_usd


def enrich_oi_hist_with_usd(
    points: list[dict],
    *,
    mark_price: float | None,
    mark_time_ms: int | None,
    quote_unit: str | None = "USDT",
) -> list[dict]:
    """Attach the §8.2 USD assertion to each ``fetch_oi_hist`` point.

    Input points keep their ``{t (ns), oi, oi_value}`` shape; each output row
    additionally carries ``oi_value_usd`` (float or ``None``),
    ``oi_value_source`` and ``reason_code`` resolved against the **same**
    synchronous mark. History rows keep their own ``t`` — only a currently
    verified endpoint unit may be referenced downstream (design §8.2).
    """
    out: list[dict] = []
    for p in points or []:
        res = resolve_oi_value_usd(
            p.get("oi"),
            p.get("oi_value"),
            mark_price=mark_price,
            oi_time_ms=p.get("t"),
            mark_time_ms=mark_time_ms,
            quote_unit=quote_unit,
        )
        row = dict(p)
        row["oi_value_usd"] = res["oi_value_usd"]
        row["oi_value_source"] = res["oi_value_source"]
        row["reason_code"] = res["reason_code"]
        out.append(row)
    return out


def compute_oi_change_7d(
    first_oi_usd: float | None,
    last_oi_usd: float | None,
    *,
    oi_start_ms: int | None,
    oi_end_ms: int | None,
    price_start_ms: int | None,
    price_end_ms: int | None,
    tolerance_ms: int = _obs.OI_PRICE_SYNC_TOLERANCE_MS,
) -> dict:
    """7D OI change ``last/first - 1`` gated on price-window alignment (F02).

    Both OI-window ends must agree with the price window within
    ``tolerance_ms`` (one 5m period); a misaligned or incomplete window is
    ``{"value": None, "reason_code": WINDOW_MISALIGNED}`` -- never a
    zero-filled ratio. Non-positive/unverified USD legs are
    ``OI_UNIT_UNVERIFIED`` (native quantity and USD nominal stay separated;
    see :func:`resolve_oi_value_usd`).
    """
    usable, reason = _obs.oi_price_window_usable(
        oi_start_ms, oi_end_ms, price_start_ms, price_end_ms, tolerance_ms
    )
    if not usable:
        return {"value": None, "reason_code": reason}
    try:
        first = float(first_oi_usd) if first_oi_usd is not None else None
        last = float(last_oi_usd) if last_oi_usd is not None else None
    except (TypeError, ValueError):
        return {"value": None, "reason_code": OI_UNIT_UNVERIFIED}
    if first is None or last is None or not (first > 0 and last > 0):
        return {"value": None, "reason_code": OI_UNIT_UNVERIFIED}
    return {"value": last / first - 1.0, "reason_code": None}


async def fetch_oi_hist_observed(
    symbol: str,
    period: str = "5m",
    limit: int = 48,
    *,
    as_of_ms: int | None = None,
    now_ms: int | None = None,
    identity_snapshot_id: str | None = None,
    request_context: Any | None = None,
) -> _obs.Observed[list[dict]]:
    """Recent open-interest points wrapped as an ``Observed`` (F02/R03).

    Legacy :func:`fetch_oi_hist` keeps its signature and return type; the
    wrapper only adds the PIT envelope (completion ``known_at``; native
    ``oi`` quantity and quote ``oi_value`` nominal stay separated, with the
    dollar-unit source retained via :func:`resolve_oi_value_usd`). OI
    carries no result cache, so every call is fresh. ``as_of_ms`` is
    accepted for the downstream cutoff check.
    R03: accepts keyword ``request_context`` (no duplicate budgeting here).
    """
    _ = as_of_ms  # decision cutoff is enforced downstream via validate_observation
    points = await fetch_oi_hist(symbol, period, limit, request_context=request_context)
    completed = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    times = sorted(_to_ms(p.get("t")) for p in points or [])
    times = [t for t in times if t is not None]
    return _obs.make_observation(
        points,
        source="binance-futures-oi",
        source_as_of_ms=max(times) if times else None,
        fetched_at_ms=completed,
        known_at_ms=completed,
        window_start_ms=min(times) if times else None,
        window_end_ms=max(times) if times else None,
        complete=bool(points),
        coverage_fraction=1.0 if points else 0.0,
        units=_obs.ObservationUnits(qty_unit="BASE", quote_asset="USDT"),
        identity_snapshot_id=identity_snapshot_id,
    )


async def fetch_oi_hist(
    symbol: str,
    period: str = "5m",
    limit: int = 48,
    *,
    request_context: Any | None = None,
) -> list[dict]:
    """Return recent open-interest points ``[{t, oi, oi_value}]`` (t in ns).

    R03: ``oi`` stays a base-asset quantity, ``oi_value`` stays the quote
    nominal (the only USD-approximable leg, via :func:`resolve_oi_value_usd`
    with its ``oi_value_source`` retained). Both time (``t``/``source_as_of``)
    and the dollar source are preserved; unit mismatches stay
    ``OI_UNIT_UNVERIFIED`` (never a quantity-as-USD). Accepts keyword
    ``request_context`` (no duplicate budgeting here).
    """
    _ = request_context  # R03 API uniformity; OI transport charges via its own limiter
    if period not in OI_PERIODS:
        period = "5m"
    now_ms = int(time.time() * 1000)

    async def send() -> Any:
        return await _live_fetch_open_interest_hist(
            symbol=symbol,
            period=period,
            start_time_ms=None,
            end_time_ms=now_ms,
            limit=limit,
            rest_base=FAPI_DATA,
        )

    raw = await run_with_retries(
        send,
        should_retry=lambda e: isinstance(e, TransientUpstreamError) or isinstance(e, asyncio.TimeoutError),
    )
    local_ts = now_ms * 1_000_000
    recs = parse_open_interest_hist(raw, _VENUE, symbol, local_ts)
    return [
        {"t": r.exchange_ts, "oi": r.open_interest, "oi_value": r.open_interest_value}
        for r in recs
    ]
