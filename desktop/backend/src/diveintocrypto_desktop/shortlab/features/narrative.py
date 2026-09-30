"""Narrative-decay features, Phase 5 (Task 17, design 8.2 / section 12).

Attention decay, not sentiment as an LTSS driver. Fixed bins (raw max 15):

- 30D social-volume decay 5, contributors decay 3, dominance decay 3:
  ``ratio = recent_30d / prev_30d``; ``<= 0.5`` scores full, ``0.5 ~ 1``
  decays linearly to 0, ``>= 1`` scores 0.
- price/social divergence 2 and spot-volume/social divergence 2: the
  aligned price (spot volume) rises ``>= 10%`` while the social metric
  falls ``>= 30%`` for full marks, else 0.

Missing windows contribute 0 with a reason; a fully unknown social feed
(provider unavailable) yields ``raw=None`` so FULL scoring adds no points
and DQ drops instead of faking attention.
"""

from __future__ import annotations

from typing import Any

RAW_MAX = 15


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


def decay_score(
    recent: Any, previous: Any, full: int, *, field: str
) -> tuple[int | None, float | None, str | None]:
    """Linear decay bin: ``ratio <= 0.5`` -> full, ``>= 1`` -> 0, between ->
    ``round(full * (1 - ratio) / 0.5)``. Unknown/non-positive base -> null."""
    recent_v = _finite(recent)
    previous_v = _finite(previous)
    if recent_v is None or previous_v is None or previous_v <= 0 or recent_v < 0:
        return None, None, f"{field}_MISSING"
    ratio = recent_v / previous_v
    if ratio <= 0.5:
        return int(full), ratio, None
    if ratio >= 1:
        return 0, ratio, f"{field}_NO_DECAY"
    return int(round(full * (1.0 - ratio) / 0.5)), ratio, f"{field}_PARTIAL_DECAY"


def divergence_score(
    price_change: Any, metric_change: Any, full: int, *, field: str
) -> tuple[int | None, float | None, str | None]:
    """Full marks when price rises ``>= 10%`` while the aligned metric falls
    ``>= 30%`` (unconfirmed bounce); else 0. Either side unknown -> null."""
    price = _finite(price_change)
    metric = _finite(metric_change)
    if price is None or metric is None:
        return None, None, f"{field}_MISSING"
    if price >= 0.10 and metric <= -0.30:
        return int(full), metric, None
    return 0, metric, f"{field}_ABSENT"


def _social_change(recent: Any, previous: Any) -> float | None:
    recent_v = _finite(recent)
    previous_v = _finite(previous)
    if recent_v is None or previous_v is None or previous_v <= 0 or recent_v < 0:
        return None
    return recent_v / previous_v - 1.0


def compute_narrative_raw_15(
    volume_prev_30d: Any,
    volume_30d: Any,
    contributors_prev_30d: Any,
    contributors_30d: Any,
    dominance_prev_30d: Any,
    dominance_30d: Any,
    price_change_30d: Any = None,
    spot_volume_change_30d: Any = None,
    *,
    social_volume_change_30d: Any = None,
) -> tuple[int | None, dict[str, Any], tuple[str, ...]]:
    """Full ``narrative_raw_15`` synthesis (pure).

    Returns ``(raw, details, reasons)``. ``raw`` is ``None`` only when every
    window is unknown; partially known feeds score the known factors and
    contribute 0 for the missing ones.
    """
    volume = decay_score(volume_30d, volume_prev_30d, 5, field="SOCIAL_VOLUME")
    contributors = decay_score(
        contributors_30d, contributors_prev_30d, 3, field="SOCIAL_CONTRIBUTORS"
    )
    dominance = decay_score(
        dominance_30d, dominance_prev_30d, 3, field="SOCIAL_DOMINANCE"
    )
    social_change = _social_change(volume_30d, volume_prev_30d)
    if social_volume_change_30d is not None:
        override = _finite(social_volume_change_30d)
        if override is not None:
            social_change = override
    price_div = divergence_score(
        price_change_30d, social_change, 2, field="PRICE_SOCIAL_DIVERGENCE"
    )
    spot_div = divergence_score(
        spot_volume_change_30d, social_change, 2, field="SPOT_SOCIAL_DIVERGENCE"
    )
    parts = (volume, contributors, dominance, price_div, spot_div)
    if all(score is None for score, _, _ in parts):
        return None, {
            "social_volume_decay": volume,
            "social_contributors_decay": contributors,
            "social_dominance_decay": dominance,
            "price_social_divergence": price_div,
            "spot_social_divergence": spot_div,
        }, ("NARRATIVE_INPUT_MISSING",)
    raw = sum(score or 0 for score, _, _ in parts)
    reasons = tuple(
        reason
        for _, _, reason in parts
        if reason is not None
    )
    return int(raw), {
        "social_volume_decay": volume,
        "social_contributors_decay": contributors,
        "social_dominance_decay": dominance,
        "price_social_divergence": price_div,
        "spot_social_divergence": spot_div,
    }, reasons
