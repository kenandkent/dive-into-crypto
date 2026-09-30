"""Carry & derivatives-crowding factors (Task 10, design sections 8.2 / 10.3).

Pure functions over settled history and verified USD notionals. ``None``
means missing and contributes 0 downstream; an incomplete 30D/90D funding
window is missing (never a partial sum masquerading as a full window).
"""

from __future__ import annotations

from typing import Any

RAW_MAX = 25


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


def score_funding_30d(
    funding_30d: Any, *, complete: bool = True
) -> tuple[int | None, float | None, str | None]:
    """Design 10.3 bins (decimal fractions, left-closed, last bin unbounded)::

        <= 0            -> 0
        [0, 0.002)      -> 1
        [0.002, 0.005)  -> 2
        [0.005, 0.01)   -> 4
        [0.01, 0.02)    -> 6
        >= 0.02         -> 8
    ``complete=False`` (funding gap) forces ``None``/``FUNDING_HISTORY_INCOMPLETE``.
    """
    value = _finite(funding_30d)
    if value is None or not complete:
        return None, value, "FUNDING_HISTORY_INCOMPLETE"
    if value <= 0:
        return 0, value, "FUNDING_NON_POSITIVE"
    if value < 0.002:
        return 1, value, None
    if value < 0.005:
        return 2, value, None
    if value < 0.01:
        return 4, value, None
    if value < 0.02:
        return 6, value, None
    return 8, value, None


def score_positive_ratio(
    ratio: Any, *, complete: bool = True, field: str = "POSITIVE_RATIO"
) -> tuple[int | None, float | None, str | None]:
    """>=0.7=3, >=0.55=2, >=0.4=1, else 0. Incomplete window -> null."""
    value = _finite(ratio)
    if value is None or not complete:
        return None, value, "FUNDING_HISTORY_INCOMPLETE"
    if value >= 0.7:
        return 3, value, None
    if value >= 0.55:
        return 2, value, None
    if value >= 0.4:
        return 1, value, None
    return 0, value, f"{field}_LOW"


def score_funding_stability(
    rates_30d: Any,
) -> tuple[int | None, float | None, str | None]:
    """Stability 0-2 over the population stdev of settled 30D rates.

    ``<=0.0003=2, <=0.0008=1, else 0``. Empty / non-numeric input is null.
    """
    if not isinstance(rates_30d, (list, tuple)) or len(rates_30d) == 0:
        return None, None, "FUNDING_STABILITY_MISSING"
    clean: list[float] = []
    for item in rates_30d:
        value = _finite(item)
        if value is None:
            return None, None, "FUNDING_STABILITY_MISSING"
        clean.append(value)
    mean = sum(clean) / len(clean)
    var = sum((item - mean) ** 2 for item in clean) / len(clean)
    stdev = var**0.5
    if stdev <= 0.0003:
        return 2, stdev, None
    if stdev <= 0.0008:
        return 1, stdev, None
    return 0, stdev, "FUNDING_UNSTABLE"


def score_oi_mc(oi_usd: Any, market_cap: Any) -> tuple[int | None, float | None, str | None]:
    """>=0.15=3, >=0.08=2, >=0.03=1, else 0. Unknown unit / missing -> null."""
    oi = _finite(oi_usd)
    mc = _finite(market_cap)
    if oi is None or mc is None or mc <= 0 or oi < 0:
        return None, None, "OI_MC_MISSING"
    ratio = oi / mc
    if ratio >= 0.15:
        return 3, ratio, None
    if ratio >= 0.08:
        return 2, ratio, None
    if ratio >= 0.03:
        return 1, ratio, None
    return 0, ratio, "OI_MC_LOW"


def score_price_oi_divergence(
    price_change_7d: Any, oi_change_7d: Any
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """Price down >5% while OI up >5% -> 3, else 0. Either leg missing -> null."""
    price = _finite(price_change_7d)
    oi = _finite(oi_change_7d)
    if price is None or oi is None:
        return None, None, "DIVERGENCE_MISSING"
    value = {"price_change_7d": price, "oi_change_7d": oi}
    if price < -0.05 and oi > 0.05:
        return 3, value, None
    return 0, value, "NO_DIVERGENCE"


def score_futures_spot_ratio(
    ratio: Any, *, applicable: bool = True
) -> tuple[int | None, float | None, str | None]:
    """>=5=2, >=2=1, else 0. No-spot denominator (N/A) -> null, never 0."""
    if not applicable:
        return None, None, "NO_SPOT_MARKET"
    value = _finite(ratio)
    if value is None or value < 0:
        return None, None, "FUTURES_SPOT_RATIO_MISSING"
    if value >= 5:
        return 2, value, None
    if value >= 2:
        return 1, value, None
    return 0, value, "FUTURES_SPOT_RATIO_LOW"


def score_crowding(ls_ratio: Any) -> tuple[int | None, float | None, str | None]:
    """Top-position L/S > 1.5 -> 1, else 0. Missing -> null."""
    value = _finite(ls_ratio)
    if value is None:
        return None, None, "CROWDING_MISSING"
    if value > 1.5:
        return 1, value, None
    return 0, value, "CROWDING_LOW"
