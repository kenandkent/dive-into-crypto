"""Task 11: short-squeeze pause detector (design 16.2 ``PAUSE_SQUEEZE``).

Pure function. ``PAUSE_SQUEEZE`` fires only on the full conjunction
``price-up + OI-up + (taker-buy dominance or bullish microstructure)``; price
and OI rising alone never pause. Thresholds are fixed and documented so the
same inputs always yield the same verdict.

R04/D04.3: squeeze inputs are frozen at the same decision cutoff as price/OI
(taker/micro with ``known_at`` after the cutoff never participate and never
enter history). ``evaluate_squeeze_state`` is the frozen tri-state authority:
``PAUSE`` (full conjunction), ``NEED_CONFIRM`` (prereq met but confirm
unknown => execution NOT_READY ``SQUEEZE_CHECK_UNVERIFIED``), ``OK`` (prereq
clearly not met => no extra confirm required), ``UNKNOWN`` (prereq inputs
themselves unknown => risk UNKNOWN). Entry totals are never substituted for
Micro (only ``micro_score``-family keys count).
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "SQUEEZE_PRICE_7D",
    "SQUEEZE_OI_7D",
    "SQUEEZE_TAKER_BUY_RATIO",
    "SQUEEZE_MICRO_BULLISH",
    "SQUEEZE_CHECK_UNVERIFIED",
    "detect_squeeze",
    "evaluate_squeeze",
    "evaluate_squeeze_state",
    "is_micro_late",
]

#: R04: frozen-check reason when prereq is met but confirm is unknown.
SQUEEZE_CHECK_UNVERIFIED = "SQUEEZE_CHECK_UNVERIFIED"

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


def _cutoff_of(metadata: Mapping[str, Any] | None) -> int | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in ("as_of_ms", "asOfMs", "decision_as_of_ms", "cutoff_ms"):
        raw = metadata.get(key)
        if raw is not None:
            try:
                return int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    return None


def _known_at_of(value: Any) -> int | None:
    """Extract ``known_at`` from a frozen envelope value, or None when plain."""
    if value is None or isinstance(value, (int, float, str, bool)):
        return None
    if isinstance(value, Mapping):
        for key in ("known_at_ms", "knownAt", "known_at", "fetched_at_ms"):
            raw = value.get(key)
            if raw is not None:
                try:
                    return int(raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
        meta = value.get("meta")
        if isinstance(meta, Mapping):
            for key in ("known_at_ms", "knownAt", "known_at", "fetched_at_ms"):
                raw = meta.get(key)
                if raw is not None:
                    try:
                        return int(raw)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        continue
        # {"value": v, "known_at_ms": ...} envelope: unwrap for finiteness below.
        if "value" in value:
            return _known_at_of(value.get("value"))
        return None
    for attr in ("known_at_ms", "known_at", "fetched_at_ms"):
        try:
            raw = getattr(value, attr, None)
        except Exception:
            continue
        if raw is not None:
            try:
                return int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
    meta = getattr(value, "meta", None)
    if meta is not None:
        for attr in ("known_at_ms", "known_at", "fetched_at_ms"):
            try:
                raw = getattr(meta, attr, None)
            except Exception:
                continue
            if raw is not None:
                try:
                    return int(raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
    return None


def _unwrap(value: Any) -> Any:
    """Unwrap ``{"value": v}`` envelopes to the inner scalar for finiteness."""
    if isinstance(value, Mapping) and "value" in value and len(value) <= 4:
        inner = value.get("value")
        # Only unwrap when the inner looks scalar (avoid swallowing feature maps).
        if inner is None or isinstance(inner, (int, float, str, bool)):
            return inner
    return value


def is_micro_late(value: Any, cutoff_ms: int | None) -> bool:
    """True when a confirm leg is frozen-out (known after the cutoff)."""
    if cutoff_ms is None:
        return False
    known = _known_at_of(value)
    if known is None:
        return False
    try:
        return int(known) > int(cutoff_ms)
    except (TypeError, ValueError):
        return False


def _frozen_finite(value: Any, cutoff_ms: int | None) -> float | None:
    """Finite value respecting the freeze (late confirms are unknown)."""
    if is_micro_late(value, cutoff_ms):
        return None
    return _finite(_unwrap(value))


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

    R04: taker/micro envelopes with ``known_at`` after the decision cutoff
    (``as_of_ms``) are frozen-out (treated as unknown, never history).
    Entry totals (``entry_score``/``entry_total``) are never read as Micro.
    """
    state = evaluate_squeeze_state(features, metadata)
    return state == "PAUSE"


def evaluate_squeeze_state(
    features: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> str:
    """Frozen tri-state squeeze verdict (R04/D04.3, pure).

    Returns ``PAUSE`` (full conjunction), ``NEED_CONFIRM`` (price+OI prereq
    met but both confirm legs unknown => caller reports execution NOT_READY
    ``SQUEEZE_CHECK_UNVERIFIED``), ``OK`` (prereq clearly not met => no
    extra confirm required), or ``UNKNOWN`` (prereq inputs themselves
    unknown => risk UNKNOWN, no pause and no confirm demand).
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

    cutoff = _cutoff_of(meta)
    # Price/OI are already frozen by inputs.py; still respect late envelopes.
    price_raw = _pick("price_change_7d", "ret_7d", "return_7d")
    oi_raw = _pick("oi_change_7d", "oi_chg_7d")
    price = _frozen_finite(price_raw, cutoff)
    oi = _frozen_finite(oi_raw, cutoff)
    # Price/OI late envelopes are also unknown (never future samples).
    if is_micro_late(price_raw, cutoff) or is_micro_late(oi_raw, cutoff):
        return "UNKNOWN"
    taker_raw = _pick("taker_buy_ratio", "taker_buy_volume_ratio", "taker_buy_share")
    micro_raw = _pick("micro_score", "microstructure_score", "microstructure_bullish_score")
    # R04: never substitute Entry totals for Micro (only micro-family keys).
    taker = _frozen_finite(taker_raw, cutoff)
    micro = _frozen_finite(micro_raw, cutoff)
    if price is None or oi is None:
        return "UNKNOWN"
    prereq = price >= SQUEEZE_PRICE_7D and oi >= SQUEEZE_OI_7D
    if not prereq:
        return "OK"
    if (taker is not None and taker >= SQUEEZE_TAKER_BUY_RATIO) or (
        micro is not None and micro >= SQUEEZE_MICRO_BULLISH
    ):
        return "PAUSE"
    return "NEED_CONFIRM"
