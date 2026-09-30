"""Unlock-pressure features, Phase 5 (Task 17, design 8.2 / 11.2).

This module has the *exclusive* ownership of ``unlock_raw_15``:
``features/valuation.py`` keeps the V1 ``valuation_raw_10`` and the FULL
synthesis ``valuation_supply_raw = valuation_raw_10 + unlock_raw_15``
(over 25) lives in ``scoring/ltss.py`` -- never duplicated here.

Fixed bins (design 8.2, raw max 15):

- 90D weighted unlock / circulating: >=30% = 10, >=15% = 7, >=5% = 3,
  else 0.
- 30D weighted unlock / circulating: >=10% = 5, >=5% = 3, else 0.

``weighted_unlock = SUM(amount * allocation_weight) / circulating`` with
the design 11.2 sell-pressure weights. The weights are code constants on
purpose: changing them changes scoring math, so it must ship with a
``feature_version`` bump (recorded in the score snapshot), never as a
silent YAML tweak.
"""

from __future__ import annotations

from typing import Any, Mapping

RAW_MAX = 15

DAY_MS = 86_400_000

# Design 11.2 initial sell-pressure weights by allocation type.
ALLOCATION_WEIGHTS: dict[str, float] = {
    "SEED": 1.0,
    "PRIVATE": 1.0,
    "TEAM": 0.8,
    "ADVISOR": 0.8,
    "TREASURY": 0.4,
    "ECOSYSTEM": 0.3,
    "OTHER": 0.3,
    "COMMUNITY": 0.2,
    "STAKING": 0.2,
}

FEATURE_VERSION_OWNED = "features-v1"


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


def allocation_weight(allocation_type: Any) -> float:
    """Sell-pressure weight for one allocation bucket (unknown -> OTHER)."""
    key = str(allocation_type or "OTHER").upper()
    return ALLOCATION_WEIGHTS.get(key, ALLOCATION_WEIGHTS["OTHER"])


def unlock_pressure(unlock_tokens: Any, circulating_supply: Any) -> float | None:
    """``unlock_tokens / circulating``; ``None`` when either side is unknown
    or the circulating base is not positive (never 0/0 -> 0)."""
    tokens = _finite(unlock_tokens)
    circulating = _finite(circulating_supply)
    if tokens is None or circulating is None or circulating <= 0 or tokens < 0:
        return None
    return tokens / circulating


def score_unlock_90d(ratio: Any) -> tuple[int | None, float | None, str | None]:
    """>=30% = 10, >=15% = 7, >=5% = 3, else 0. Unknown -> null."""
    value = _finite(ratio)
    if value is None:
        return None, None, "UNLOCK_90D_MISSING"
    if value >= 0.30:
        return 10, value, None
    if value >= 0.15:
        return 7, value, None
    if value >= 0.05:
        return 3, value, None
    return 0, value, "UNLOCK_90D_LOW"


def score_unlock_30d(ratio: Any) -> tuple[int | None, float | None, str | None]:
    """>=10% = 5, >=5% = 3, else 0. Unknown -> null."""
    value = _finite(ratio)
    if value is None:
        return None, None, "UNLOCK_30D_MISSING"
    if value >= 0.10:
        return 5, value, None
    if value >= 0.05:
        return 3, value, None
    return 0, value, "UNLOCK_30D_LOW"


def _event_amount(event: Any) -> float | None:
    if isinstance(event, Mapping):
        return _finite(event.get("amount_tokens"))
    return _finite(getattr(event, "amount_tokens", None))


def _event_unlock_at(event: Any) -> int | None:
    if isinstance(event, Mapping):
        value = event.get("unlock_at_ms")
    else:
        value = getattr(event, "unlock_at_ms", None)
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _event_allocation(event: Any) -> str:
    if isinstance(event, Mapping):
        return str(event.get("allocation_type") or "OTHER")
    return str(getattr(event, "allocation_type", "OTHER") or "OTHER")


def window_tokens(
    events: list[Any] | tuple[Any, ...] | None, start_ms: int, end_ms: int
) -> float | None:
    """Weighted unlock tokens vesting in ``[start_ms, end_ms]``.

    Returns ``None`` when ``events`` itself is unknown (provider
    unavailable); a known-empty window is a genuine ``0.0``.
    """
    if events is None:
        return None
    total = 0.0
    for event in events:
        at_ms = _event_unlock_at(event)
        amount = _event_amount(event)
        if at_ms is None or amount is None:
            continue
        if start_ms <= at_ms <= end_ms:
            total += amount * allocation_weight(_event_allocation(event))
    return total


def compute_unlock_raw_15(
    events: list[Any] | tuple[Any, ...] | None,
    circulating_supply: Any,
    as_of_ms: int,
    *,
    weights: Mapping[str, float] | None = None,
) -> tuple[int | None, dict[str, Any], tuple[str, ...]]:
    """Full ``unlock_raw_15`` synthesis (pure).

    Returns ``(raw, details, reasons)``. ``raw`` is ``None`` when the
    unlock schedule or the circulating base is unknown (missing provider
    data contributes 0 downstream with no reweighting); a known schedule
    with no vesting in the windows scores a genuine 0.
    """
    table = dict(ALLOCATION_WEIGHTS)
    if weights is not None:
        for key, value in dict(weights).items():
            number = _finite(value)
            if number is not None and number >= 0:
                table[str(key).upper()] = number
    as_of = int(as_of_ms)
    circulating = _finite(circulating_supply)
    if events is None or circulating is None or circulating <= 0:
        return None, {
            "pressure_30d": None,
            "pressure_90d": None,
            "tokens_30d": None,
            "tokens_90d": None,
        }, ("UNLOCK_INPUT_MISSING",)
    tokens_30d = 0.0
    tokens_90d = 0.0
    for event in events:
        at_ms = _event_unlock_at(event)
        amount = _event_amount(event)
        if at_ms is None or amount is None:
            continue
        weight = table.get(_event_allocation(event).upper(), table["OTHER"])
        if as_of - 90 * DAY_MS <= at_ms <= as_of:
            tokens_90d += amount * weight
        if as_of - 30 * DAY_MS <= at_ms <= as_of:
            tokens_30d += amount * weight
    # NOTE: this backward-looking variant exists for historical pressure
    # analysis; production FULL scoring uses `compute_unlock_raw_15_forward`
    # (design 8.2 bins *future* unlocks).
    pressure_30d = unlock_pressure(tokens_30d, circulating)
    pressure_90d = unlock_pressure(tokens_90d, circulating)
    score_90d, _, reason_90d = score_unlock_90d(pressure_90d)
    score_30d, _, reason_30d = score_unlock_30d(pressure_30d)
    raw = int(score_90d or 0) + int(score_30d or 0)
    reasons = tuple(r for r in (reason_90d, reason_30d) if r is not None)
    return raw, {
        "pressure_30d": pressure_30d,
        "pressure_90d": pressure_90d,
        "tokens_30d": tokens_30d,
        "tokens_90d": tokens_90d,
    }, reasons


def forward_window_tokens(
    events: list[Any] | tuple[Any, ...] | None,
    as_of_ms: int,
    days: int,
    *,
    weights: Mapping[str, float] | None = None,
) -> float | None:
    """Weighted unlock tokens vesting in ``(as_of_ms, as_of_ms + days]``.

    ``None`` when the schedule is unknown; ``0.0`` for a known-empty
    forward window.
    """
    if events is None:
        return None
    table = dict(ALLOCATION_WEIGHTS)
    if weights is not None:
        for key, value in dict(weights).items():
            number = _finite(value)
            if number is not None and number >= 0:
                table[str(key).upper()] = number
    as_of = int(as_of_ms)
    end = as_of + int(days) * DAY_MS
    total = 0.0
    for event in events:
        at_ms = _event_unlock_at(event)
        amount = _event_amount(event)
        if at_ms is None or amount is None:
            continue
        if as_of < at_ms <= end:
            total += amount * table.get(_event_allocation(event).upper(), table["OTHER"])
    return total


def compute_unlock_raw_15_forward(
    events: list[Any] | tuple[Any, ...] | None,
    circulating_supply: Any,
    as_of_ms: int,
    *,
    weights: Mapping[str, float] | None = None,
) -> tuple[int | None, dict[str, Any], tuple[str, ...]]:
    """``unlock_raw_15`` over the *forward* vesting windows.

    Design 8.2 scores future unlocks (``future 90D`` / ``future 30D``);
    this is the production entry point used by the service. ``raw`` is
    ``None`` when the schedule or circulating base is unknown.
    """
    as_of = int(as_of_ms)
    circulating = _finite(circulating_supply)
    if events is None or circulating is None or circulating <= 0:
        return None, {
            "pressure_30d": None,
            "pressure_90d": None,
            "tokens_30d": None,
            "tokens_90d": None,
        }, ("UNLOCK_INPUT_MISSING",)
    tokens_30d = forward_window_tokens(events, as_of, 30, weights=weights)
    tokens_90d = forward_window_tokens(events, as_of, 90, weights=weights)
    pressure_30d = unlock_pressure(tokens_30d, circulating)
    pressure_90d = unlock_pressure(tokens_90d, circulating)
    score_90d, _, reason_90d = score_unlock_90d(pressure_90d)
    score_30d, _, reason_30d = score_unlock_30d(pressure_30d)
    raw = int(score_90d or 0) + int(score_30d or 0)
    reasons = tuple(r for r in (reason_90d, reason_30d) if r is not None)
    return raw, {
        "pressure_30d": pressure_30d,
        "pressure_90d": pressure_90d,
        "tokens_30d": tokens_30d,
        "tokens_90d": tokens_90d,
    }, reasons
