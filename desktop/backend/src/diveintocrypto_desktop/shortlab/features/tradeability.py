"""Tradeability factors (Task 10, design sections 8.2 / 13 / 13.1).

The 24h futures quote-volume leg and the 10M hard gate use ONLY the last
**closed UTC 1d K-line** ``qv`` (quote asset volume). The rolling 24h ticker
(``universe.quote_volume``) is a cheap prefilter and must never enter the
score, the gate, DQ, or the snapshot field: when the daily ``qv`` is missing
the factor is null with no ticker fallback.
"""

from __future__ import annotations

from typing import Any

RAW_MAX = 10


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    import math as _math

    if not _math.isfinite(result):
        return None
    return result


def score_futures_qv(qv_1d: Any) -> tuple[int | None, float | None, str | None]:
    """>=30M=3, >=20M=2, >=10M=1, else 0. ``None`` stays null (no fallback)."""
    value = _finite(qv_1d)
    if value is None or value < 0:
        return None, value, "FUTURES_QV_MISSING"
    if value >= 30_000_000:
        return 3, value, None
    if value >= 20_000_000:
        return 2, value, None
    if value >= 10_000_000:
        return 1, value, None
    return 0, value, "FUTURES_QV_LOW"


def score_oi(oi_usd: Any) -> tuple[int | None, float | None, str | None]:
    """>=5M=2, >=2M=1, else 0. Unknown unit / missing -> null."""
    value = _finite(oi_usd)
    if value is None or value < 0:
        return None, value, "OI_MISSING"
    if value >= 5_000_000:
        return 2, value, None
    if value >= 2_000_000:
        return 1, value, None
    return 0, value, "OI_LOW"


def score_spread(spread: Any) -> tuple[int | None, float | None, str | None]:
    """Decimal spread ``(ask-bid)/mid``: ``<=0.15%=2, <=0.3%=1, else 0``."""
    value = _finite(spread)
    if value is None or value < 0:
        return None, value, "SPREAD_MISSING"
    if value <= 0.0015:
        return 2, value, None
    if value <= 0.003:
        return 1, value, None
    return 0, value, "SPREAD_WIDE"


def score_depth(depth_min_1pct: Any) -> tuple[int | None, float | None, str | None]:
    """``min(bid, ask)`` 1% notional: ``>=1M=2, >=0.25M=1, else 0``.

    Callers pass ``book_depth_min_1pct(panel)`` (``None`` when either side is
    missing); a missing side is null, never 0.
    """
    value = _finite(depth_min_1pct)
    if value is None or value < 0:
        return None, value, "DEPTH_MISSING"
    if value >= 1_000_000:
        return 2, value, None
    if value >= 250_000:
        return 1, value, None
    return 0, value, "DEPTH_THIN"


def score_contract(
    status: Any, has_settlement: Any
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """TRADING + settlement record -> 1, known-negative -> 0, unknown -> null."""
    if status is None or has_settlement is None:
        return None, None, "CONTRACT_STATUS_MISSING"
    if not isinstance(status, str) or not status:
        return None, None, "CONTRACT_STATUS_MISSING"
    if not isinstance(has_settlement, bool):
        return None, None, "CONTRACT_STATUS_MISSING"
    value = {"status": status, "has_settlement_record": has_settlement}
    if status == "TRADING" and has_settlement:
        return 1, value, None
    return 0, value, "CONTRACT_NOT_READY"
