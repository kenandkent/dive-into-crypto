"""Lifecycle / structural-decay factors (Task 10, design sections 8.2 / 8.3 / 9.1).

Pure functions over already-closed data. Every scorer returns
``(score, value, reason)`` where ``score`` is ``int | None`` (``None`` means
missing / unavailable and contributes 0 downstream) and ``value`` is the raw
observable. No HTTP, no SQL, no clock reads.
"""

from __future__ import annotations

from typing import Any

DAY_MS = 86_400_000

RAW_MAX = 25


def score_ath_drawdown(drawdown: Any) -> tuple[int | None, float | None, str | None]:
    """Design 9.1 bins over the decimal drawdown ``(current-ath)/ath`` (<=0).

    Bins (left-closed on the lower edge, last bin inclusive)::
        d > -0.20              -> 0
        d > -0.40              -> 3
        d > -0.70              -> 7
        d > -0.85              -> 4
        d >= -0.95             -> 2
        else (d < -0.95)       -> 0
    A non-negative ``d`` (at/above ATH) scores 0. ``None`` stays null.
    """
    if drawdown is None or isinstance(drawdown, bool):
        return None, None, "ATH_MISSING"
    try:
        d = float(drawdown)
    except (TypeError, ValueError):
        return None, None, "ATH_MISSING"
    import math as _math

    if not _math.isfinite(d):
        return None, None, "ATH_MISSING"
    if d > 0:
        return 0, d, "ATH_AT_HIGH"
    if d > -0.20:
        return 0, d, "ATH_SHALLOW"
    if d > -0.40:
        return 3, d, None
    if d > -0.70:
        return 7, d, None
    if d > -0.85:
        return 4, d, None
    if d >= -0.95:
        return 2, d, None
    return 0, d, "ATH_NEAR_ZERO"


def score_ath_age(age_days: Any) -> tuple[int | None, float | None, str | None]:
    """``<90d=0, 90-179d=1, >=180d=3``. ``None`` stays null."""
    if age_days is None or isinstance(age_days, bool):
        return None, None, "ATH_AGE_MISSING"
    try:
        age = float(age_days)
    except (TypeError, ValueError):
        return None, None, "ATH_AGE_MISSING"
    import math as _math

    if not _math.isfinite(age) or age < 0:
        return None, None, "ATH_AGE_MISSING"
    if age < 90:
        return 0, age, "ATH_AGE_YOUNG"
    if age < 180:
        return 1, age, None
    return 3, age, None


def score_30d_drop(ret_30d: Any) -> tuple[int | None, float | None, str | None]:
    """``>=0=0, (-10%,0)=1, (-30%,-10%]=3, else=1``. ``None`` stays null."""
    if ret_30d is None or isinstance(ret_30d, bool):
        return None, None, "TREND_30D_MISSING"
    try:
        r = float(ret_30d)
    except (TypeError, ValueError):
        return None, None, "TREND_30D_MISSING"
    import math as _math

    if not _math.isfinite(r):
        return None, None, "TREND_30D_MISSING"
    if r >= 0:
        return 0, r, "TREND_30D_UP"
    if r > -0.10:
        return 1, r, None
    if r > -0.30:
        return 3, r, None
    return 1, r, None


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def score_ma_structure(
    closes: Any,
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """MA structure 0-4 (design 8.2/8.3).

    One point each for: last close < MA60, MA30 < MA60, MA60 below its value
    10 days ago, mean(last 5 closes) < MA30. Requires >= 70 closed dailies
    (MA60 plus the 10-day slope lookback); otherwise ``(None, None,
    "MA_INSUFFICIENT_HISTORY")``.
    """
    if not isinstance(closes, (list, tuple)):
        return None, None, "MA_INSUFFICIENT_HISTORY"
    clean: list[float] = []
    for item in closes:
        if item is None or isinstance(item, bool):
            return None, None, "MA_INSUFFICIENT_HISTORY"
        try:
            value = float(item)
        except (TypeError, ValueError):
            return None, None, "MA_INSUFFICIENT_HISTORY"
        import math as _math

        if not _math.isfinite(value):
            return None, None, "MA_INSUFFICIENT_HISTORY"
        clean.append(value)
    if len(clean) < 70:
        return None, None, "MA_INSUFFICIENT_HISTORY"
    ma60 = _mean(clean[-60:])
    ma30 = _mean(clean[-30:])
    ma60_ago = _mean(clean[-70:-10])
    last5 = _mean(clean[-5:])
    last = clean[-1]
    assert ma60 is not None and ma30 is not None and ma60_ago is not None
    assert last5 is not None
    conds = {
        "close_below_ma60": last < ma60,
        "ma30_below_ma60": ma30 < ma60,
        "ma60_declining": ma60 < ma60_ago,
        "last5_below_ma30": last5 < ma30,
    }
    score = sum(1 for hit in conds.values() if hit)
    value = {
        "close": last,
        "ma30": ma30,
        "ma60": ma60,
        "ma60_10d_ago": ma60_ago,
        "last5_avg": last5,
        **conds,
    }
    reason = None if score == 4 else "MA_STRUCTURE_PARTIAL"
    return score, value, reason


def find_confirmed_pivot_highs(highs: list[float]) -> list[tuple[int, float]]:
    """Confirmed pivot highs: center strictly above the 3 highs on each side.

    Only indices ``3..n-4`` can be centers (the right 3 bars must already be
    closed). Returns ``[(index, high)]`` oldest first.
    """
    out: list[tuple[int, float]] = []
    n = len(highs)
    for center in range(3, n - 3):
        pivot = highs[center]
        left = highs[center - 3 : center]
        right = highs[center + 1 : center + 4]
        if all(pivot > item for item in left) and all(pivot > item for item in right):
            out.append((center, pivot))
    return out


def score_lower_high(highs: Any) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """Lower High 0/3: last two *confirmed* pivot highs strictly decreasing.

    Fewer than 7 closed dailies cannot confirm any pivot -> ``None`` with
    ``PIVOT_INSUFFICIENT_HISTORY``. With enough history but fewer than two
    confirmed pivots the structure is absent -> ``0`` (not null), so an
    *unconfirmed* spike inside the last 3 bars can never score.
    """
    if not isinstance(highs, (list, tuple)):
        return None, None, "PIVOT_INSUFFICIENT_HISTORY"
    clean: list[float] = []
    for item in highs:
        if item is None or isinstance(item, bool):
            return None, None, "PIVOT_INSUFFICIENT_HISTORY"
        try:
            value = float(item)
        except (TypeError, ValueError):
            return None, None, "PIVOT_INSUFFICIENT_HISTORY"
        import math as _math

        if not _math.isfinite(value) or value <= 0:
            return None, None, "PIVOT_INSUFFICIENT_HISTORY"
        clean.append(value)
    if len(clean) < 7:
        return None, None, "PIVOT_INSUFFICIENT_HISTORY"
    pivots = find_confirmed_pivot_highs(clean)
    value = {"pivots": [[idx, price] for idx, price in pivots]}
    if len(pivots) < 2:
        return 0, value, "NO_CONFIRMED_LOWER_HIGH"
    if pivots[-1][1] < pivots[-2][1]:
        return 3, value, None
    return 0, value, "NO_CONFIRMED_LOWER_HIGH"


def score_spot_decay(
    spot_30d: Any, spot_prev_30d: Any, *, applicable: bool = True
) -> tuple[int | None, float | None, str | None]:
    """Spot Volume Decay 0-3 over ``rear30/prev30``.

    ``<=0.6=3, <=0.85=2, <1=1, else 0``. When there is verifiably no spot
    market (``applicable=False``) the factor is N/A -> ``None`` with
    ``NO_SPOT_MARKET`` (Task 11 removes it from the DQ denominator; scoring
    never treats it as 0 volume). Missing volumes or ``prev <= 0`` are null.
    """
    if not applicable:
        return None, None, "NO_SPOT_MARKET"
    if (
        spot_30d is None
        or spot_prev_30d is None
        or isinstance(spot_30d, bool)
        or isinstance(spot_prev_30d, bool)
    ):
        return None, None, "SPOT_DECAY_MISSING"
    try:
        cur = float(spot_30d)
        prev = float(spot_prev_30d)
    except (TypeError, ValueError):
        return None, None, "SPOT_DECAY_MISSING"
    import math as _math

    if not (_math.isfinite(cur) and _math.isfinite(prev)) or prev <= 0 or cur < 0:
        return None, None, "SPOT_DECAY_MISSING"
    ratio = cur / prev
    if ratio <= 0.6:
        return 3, ratio, None
    if ratio <= 0.85:
        return 2, ratio, None
    if ratio < 1:
        return 1, ratio, None
    return 0, ratio, "SPOT_DECAY_NONE"


def _ma_series(closes: list[float], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    acc = 0.0
    for idx, price in enumerate(closes):
        acc += price
        if idx >= window:
            acc -= closes[idx - window]
        if idx >= window - 1:
            out[idx] = acc / window
    return out


def score_failed_bounce(
    closes: Any, highs: Any
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """Failed Bounce 0/2 (design 8.3).

    Within the last 20 closed dailies: bounce >= 10% off the local low, the
    bounce's highest *close* stays below the latest confirmed pivot high that
    precedes it, then 2 consecutive closes print below MA10. Fewer than 20
    closes, no computable MA10, or no confirmed pivot -> ``None`` with a
    specific reason (``FAILED_BOUNCE_INSUFFICIENT_HISTORY`` /
    ``FAILED_BOUNCE_NO_PIVOT``).
    """
    if not isinstance(closes, (list, tuple)) or not isinstance(highs, (list, tuple)):
        return None, None, "FAILED_BOUNCE_INSUFFICIENT_HISTORY"
    clean_close: list[float] = []
    for item in closes:
        if item is None or isinstance(item, bool):
            return None, None, "FAILED_BOUNCE_INSUFFICIENT_HISTORY"
        try:
            value = float(item)
        except (TypeError, ValueError):
            return None, None, "FAILED_BOUNCE_INSUFFICIENT_HISTORY"
        import math as _math

        if not _math.isfinite(value) or value <= 0:
            return None, None, "FAILED_BOUNCE_INSUFFICIENT_HISTORY"
        clean_close.append(value)
    clean_high: list[float] = []
    for item in highs:
        if item is None or isinstance(item, bool):
            return None, None, "FAILED_BOUNCE_INSUFFICIENT_HISTORY"
        try:
            value = float(item)
        except (TypeError, ValueError):
            return None, None, "FAILED_BOUNCE_INSUFFICIENT_HISTORY"
        import math as _math

        if not _math.isfinite(value) or value <= 0:
            return None, None, "FAILED_BOUNCE_INSUFFICIENT_HISTORY"
        clean_high.append(value)
    if len(clean_close) < 20 or len(clean_high) < 7:
        return None, None, "FAILED_BOUNCE_INSUFFICIENT_HISTORY"
    if len(clean_close) != len(clean_high):
        # Histories must align day-for-day; callers pass same-length series.
        # Truncate to the shared tail deterministically.
        shared = min(len(clean_close), len(clean_high))
        if shared < 20:
            return None, None, "FAILED_BOUNCE_INSUFFICIENT_HISTORY"
        clean_close = clean_close[-shared:]
        clean_high = clean_high[-shared:]

    window = clean_close[-20:]
    base = len(clean_close) - 20
    low_idx_rel = min(range(len(window)), key=lambda i: window[i])
    low = window[low_idx_rel]
    after_low = window[low_idx_rel:]
    bounce_high = max(after_low)
    bounce_rel = low_idx_rel + after_low.index(bounce_high)
    bounce_abs = base + bounce_rel
    if low <= 0 or (bounce_high - low) / low < 0.10:
        return 0, {"rebound": (bounce_high - low) / low if low > 0 else None}, "NO_FAILED_BOUNCE"

    pivots = find_confirmed_pivot_highs(clean_high)
    prior = [price for idx, price in pivots if idx < bounce_abs]
    if not prior:
        return None, None, "FAILED_BOUNCE_NO_PIVOT"
    ref = prior[-1]
    if not bounce_high < ref:
        return 0, {"bounce_high": bounce_high, "pivot_high": ref}, "NO_FAILED_BOUNCE"

    ma10 = _ma_series(clean_close, 10)
    for idx in range(bounce_abs + 1, len(clean_close) - 1):
        first = ma10[idx]
        second = ma10[idx + 1]
        if first is None or second is None:
            continue
        if clean_close[idx] < first and clean_close[idx + 1] < second:
            return 2, {"bounce_high": bounce_high, "pivot_high": ref}, None
    return 0, {"bounce_high": bounce_high, "pivot_high": ref}, "NO_FAILED_BOUNCE"
