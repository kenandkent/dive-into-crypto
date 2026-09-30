"""Valuation factors V1 (Task 10, design sections 8.2 / 11.1).

``valuation_raw_10`` only. Unlock pressure (``unlock_raw_15``) belongs to
Task 17 ``features/supply.py``; the FULL synthesis
``valuation_supply_raw = valuation_raw_10 + unlock_raw_15`` lives in
``scoring/ltss.py`` and must not be duplicated here.
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


def score_fdv_mc(fdv: Any, market_cap: Any) -> tuple[int | None, float | None, str | None]:
    """>=5=4, >=3=3, >=2=2, else 0. Missing/non-positive MC -> null."""
    fdv_v = _finite(fdv)
    mc_v = _finite(market_cap)
    if fdv_v is None or mc_v is None or mc_v <= 0 or fdv_v < 0:
        return None, None, "FDV_MC_MISSING"
    ratio = fdv_v / mc_v
    if ratio >= 5:
        return 4, ratio, None
    if ratio >= 3:
        return 3, ratio, None
    if ratio >= 2:
        return 2, ratio, None
    return 0, ratio, "FDV_MC_LOW"


def score_float_ratio(
    circulating: Any, total: Any
) -> tuple[int | None, float | None, str | None]:
    """``<0.2=4, <0.35=3, <0.5=1, else 0``. Missing/non-positive total -> null."""
    circ = _finite(circulating)
    total_v = _finite(total)
    if circ is None or total_v is None or total_v <= 0 or circ < 0:
        return None, None, "FLOAT_RATIO_MISSING"
    ratio = circ / total_v
    if ratio < 0.2:
        return 4, ratio, None
    if ratio < 0.35:
        return 3, ratio, None
    if ratio < 0.5:
        return 1, ratio, None
    return 0, ratio, "FLOAT_RATIO_HIGH"


def score_combo(
    fdv_mc: float | None, float_ratio: float | None
) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """``FDV/MC >= 3 and float < 0.35`` -> 2, else 0. Either input missing -> null."""
    if fdv_mc is None or float_ratio is None:
        return None, None, "VALUATION_COMBO_MISSING"
    import math as _math

    if not (_math.isfinite(fdv_mc) and _math.isfinite(float_ratio)):
        return None, None, "VALUATION_COMBO_MISSING"
    value = {"fdv_mc": fdv_mc, "float_ratio": float_ratio}
    if fdv_mc >= 3 and float_ratio < 0.35:
        return 2, value, None
    return 0, value, "VALUATION_COMBO_ABSENT"
