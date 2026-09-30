"""Task 11: short-squeeze pause detector (design 16.2 ``PAUSE_SQUEEZE``).

Pure function. ``PAUSE_SQUEEZE`` fires only on the full conjunction
``price-up + OI-up + (taker-buy dominance or bullish microstructure)``; price
and OI rising alone never pause. Thresholds are fixed and documented so the
same inputs always yield the same verdict.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "SQUEEZE_PRICE_7D",
    "SQUEEZE_OI_7D",
    "SQUEEZE_TAKER_BUY_RATIO",
    "SQUEEZE_MICRO_BULLISH",
    "detect_squeeze",
    "evaluate_squeeze",
]

# Fixed trigger levels (design 16.2 initial values, configuration of the exact
# cut is owned by Task 11 until Evidence recalibrates them under a new score
# version).
SQUEEZE_PRICE_7D = 0.10
SQUEEZE_OI_7D = 0.10
SQUEEZE_TAKER_BUY_RATIO = 0.55
SQUEEZE_MICRO_BULLISH = 30.0


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _get(source: Mapping[str, Any] | None, *names: str) -> Any:
    if not isinstance(source, Mapping):
        return None
    for name in names:
        if name in source:
            return source[name]
    return None


def detect_squeeze(
    price_change_7d: Any,
    oi_change_7d: Any,
    taker_buy_ratio: Any = None,
    micro_score: Any = None,
) -> bool:
    """Return True iff the squeeze conjunction holds.

    - ``price_change_7d >= 0.10`` and ``oi_change_7d >= 0.10`` are both
      required (decimals, e.g. 0.12 = +12%).
    - At least one confirmation leg is required: ``taker_buy_ratio >= 0.55``
      or bullish microstructure ``micro_score >= 30``.
    - Missing legs never count as confirmation; non-finite inputs are null.
    """
    price = _finite(price_change_7d)
    oi = _finite(oi_change_7d)
    if price is None or oi is None:
        return False
    if price < SQUEEZE_PRICE_7D or oi < SQUEEZE_OI_7D:
        return False
    taker = _finite(taker_buy_ratio)
    micro = _finite(micro_score)
    if taker is not None and taker >= SQUEEZE_TAKER_BUY_RATIO:
        return True
    if micro is not None and micro >= SQUEEZE_MICRO_BULLISH:
        return True
    return False


def evaluate_squeeze(
    features: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> bool:
    """Extract squeeze inputs from ``features``/``metadata`` deterministically.

    ``metadata`` wins over ``features`` when both carry the same key so tests
    and the runtime can override frozen feature values with fresher risk
    inputs without ambiguity. Recognised keys: ``price_change_7d`` /
    ``ret_7d``, ``oi_change_7d``, ``taker_buy_ratio`` / ``taker_buy_volume_ratio``,
    ``micro_score`` / ``microstructure_score``.
    """
    meta = metadata if isinstance(metadata, Mapping) else {}
    feats = features if isinstance(features, Mapping) else {}
    # FeatureSnapshot nests raw inputs under "_inputs" in Task 10; support both
    # the nested view and a flat mapping.
    nested: Mapping[str, Any] | None = None
    if isinstance(feats.get("_inputs"), Mapping):
        nested = feats["_inputs"]  # type: ignore[assignment]
    elif isinstance(feats.get("features"), Mapping):
        inner = feats["features"]
        if isinstance(inner, Mapping) and isinstance(inner.get("_inputs"), Mapping):
            nested = inner["_inputs"]

    def _pick(*names: str) -> Any:
        for source in (meta, feats, nested):
            value = _get(source, *names)
            if value is not None:
                return value
        return None

    price = _pick("price_change_7d", "ret_7d", "return_7d")
    oi = _pick("oi_change_7d", "oi_chg_7d")
    taker = _pick("taker_buy_ratio", "taker_buy_volume_ratio", "taker_buy_share")
    micro = _pick("micro_score", "microstructure_score", "microstructure_bullish_score")
    return detect_squeeze(price, oi, taker, micro)
