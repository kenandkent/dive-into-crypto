"""Short-Lab profile selection (Task 10, design sections 8 / 8.1).

Priority (fixed): manual override > verified Meme category > verified
low-float condition > GENERAL_ALT. Meme wins when both Meme and low-float
hit, unless a manual override says otherwise. Missing sources never guess:
they fall back to GENERAL_ALT with a recorded reason. No LLM is involved.

``select_profile(identity, fundamentals, overrides) -> Profile`` is pure
(no HTTP / SQL) and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

ProfileBase = Literal["MEME", "LOW_FLOAT_VC", "GENERAL_ALT"]

LITE_PROFILE_NAMES = ("MEME_LITE", "GENERAL_LITE", "LOW_FLOAT_VC_LITE")
FULL_PROFILE_NAMES = ("MEME_FULL", "GENERAL_FULL", "LOW_FLOAT_VC_FULL")

_BASE_TO_LITE: dict[str, str] = {
    "MEME": "MEME_LITE",
    "GENERAL_ALT": "GENERAL_LITE",
    "LOW_FLOAT_VC": "LOW_FLOAT_VC_LITE",
}
_BASE_TO_FULL: dict[str, str] = {
    "MEME": "MEME_FULL",
    "GENERAL_ALT": "GENERAL_FULL",
    "LOW_FLOAT_VC": "LOW_FLOAT_VC_FULL",
}

_VALID_MANUAL = frozenset(
    {
        "MEME",
        "GENERAL",
        "LOW_FLOAT_VC",
        "GENERAL_ALT",
        "MEME_LITE",
        "GENERAL_LITE",
        "LOW_FLOAT_VC_LITE",
        "MEME_FULL",
        "GENERAL_FULL",
        "LOW_FLOAT_VC_FULL",
    }
)


@dataclass(frozen=True)
class Profile:
    """Selected scoring profile.

    ``name`` is the LITE key used by ``score_lite`` (e.g. ``GENERAL_LITE``);
    ``base`` is the tier-independent kind; ``full_name`` is the matching FULL
    key for Task 17; ``reason`` records the classification basis and
    ``is_manual`` whether a human override decided it.
    """

    name: str
    base: ProfileBase
    full_name: str
    reason: str
    is_manual: bool = False


def _normalize_manual(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if text not in _VALID_MANUAL:
        return None
    if text.endswith("_LITE") or text.endswith("_FULL"):
        text = text.rsplit("_", 1)[0]
    # After stripping the tier suffix the remainder must be a known base.
    # "GENERAL" alone is accepted as an alias of "GENERAL_ALT".
    if text == "GENERAL":
        text = "GENERAL_ALT"
    if text in ("MEME", "LOW_FLOAT_VC", "GENERAL_ALT"):
        return text
    return None


def _categories_of(*sources: Any) -> list[str]:
    out: list[str] = []
    for source in sources:
        if source is None:
            continue
        cats = getattr(source, "categories", None)
        if cats is None and isinstance(source, Mapping):
            cats = source.get("categories")
        if not cats:
            continue
        try:
            items = list(cats)
        except TypeError:
            continue
        for item in items:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
    return out


def _is_meme(categories: list[str]) -> bool:
    for item in categories:
        lowered = item.strip().lower()
        if lowered == "meme" or "meme" in lowered:
            return True
    return False


def _as_float(value: Any) -> float | None:
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


def _fundamentals_numbers(fundamentals: Any) -> tuple[float | None, float | None]:
    """Return ``(float_ratio, fdv_mc)`` or Nones when unverifiable."""
    if fundamentals is None:
        return None, None
    if isinstance(fundamentals, Mapping):
        mc = _as_float(fundamentals.get("market_cap_usd"))
        fdv = _as_float(fundamentals.get("fdv_usd"))
        circ = _as_float(fundamentals.get("circulating_supply"))
        total = _as_float(fundamentals.get("total_supply"))
    else:
        mc = _as_float(getattr(fundamentals, "market_cap_usd", None))
        fdv = _as_float(getattr(fundamentals, "fdv_usd", None))
        circ = _as_float(getattr(fundamentals, "circulating_supply", None))
        total = _as_float(getattr(fundamentals, "total_supply", None))
    float_ratio = None
    if circ is not None and total is not None and total > 0:
        float_ratio = circ / total
    fdv_mc = None
    if fdv is not None and mc is not None and mc > 0:
        fdv_mc = fdv / mc
    return float_ratio, fdv_mc


def _manual_from_entry(entry: Any) -> str | None:
    """Extract a normalized manual base from one override entry mapping."""
    if not isinstance(entry, Mapping):
        return None
    for key in ("profile", "manual_profile", "manualProfile"):
        if key in entry:
            manual = _normalize_manual(entry.get(key))
            if manual is not None:
                return manual
    return None


def _extract_symbol_entry(
    overrides: Mapping[str, Any] | None, symbol: str | None
) -> Mapping[str, Any] | None:
    """Return the per-symbol override entry for ``symbol`` when present.

    Accepts the frozen document shape ``{"version":..,"overrides":{sym:entry}}``
    as well as a flat ``{sym: entry}`` mapping. Lookup is exact first, then
    upper-cased (symbols are canonical upper-case like ``BTCUSDT``).
    """
    if not isinstance(overrides, Mapping) or not symbol:
        return None
    text = str(symbol)
    # Frozen document shape.
    inner = overrides.get("overrides")
    if isinstance(inner, Mapping):
        entry = inner.get(text)
        if isinstance(entry, Mapping) and entry:
            return entry
        entry = inner.get(text.upper())
        if isinstance(entry, Mapping) and entry:
            return entry
        entry = inner.get(text.strip().upper())
        if isinstance(entry, Mapping) and entry:
            return entry
    # Flat {symbol: entry} shape.
    entry = overrides.get(text)
    if isinstance(entry, Mapping) and entry:
        # Avoid treating a bare entry ({"profile": ...}) as a symbol map:
        # a bare entry has no nested mapping values for profile keys.
        # If the mapping itself carries a profile key, the caller already
        # handles it as a bare entry; still return it when the key matches
        # a plausible symbol (caller passes symbol explicitly).
        return entry
    entry = overrides.get(text.upper())
    if isinstance(entry, Mapping) and entry:
        return entry
    return None


def select_profile(
    identity: Any,
    fundamentals: Any,
    overrides: Mapping[str, Any] | None,
    symbol: str | None = None,
) -> Profile:
    """Pick the scoring profile (pure, deterministic).

    ``overrides`` may be ``None``, a bare per-symbol entry
    (``{"profile": "MEME", ...}``), the frozen overrides document
    (``{"version":..,"overrides":{symbol: entry}}``), or a flat
    ``{futures_symbol: entry}`` mapping. When ``symbol`` is given the
    entry for that symbol wins (CR08: the unique Profile entry consumes
    the current symbol's entry); otherwise a top-level
    ``profile`` / ``manual_profile`` is honoured for backward
    compatibility. Only an explicit profile string counts as a manual
    override.
    """
    manual: str | None = None
    # CR08: current-symbol entry first (frozen manual priority).
    if symbol is not None and isinstance(overrides, Mapping):
        entry = _extract_symbol_entry(overrides, symbol)
        if entry is not None:
            manual = _manual_from_entry(entry)
    if manual is None and isinstance(overrides, Mapping):
        for key in ("profile", "manual_profile", "manualProfile"):
            if key in overrides:
                manual = _normalize_manual(overrides.get(key))
                if manual is not None:
                    break
    if manual is not None:
        base: ProfileBase = manual  # type: ignore[assignment]
        return Profile(
            name=_BASE_TO_LITE[base],
            base=base,
            full_name=_BASE_TO_FULL[base],
            reason="MANUAL_OVERRIDE",
            is_manual=True,
        )

    categories = _categories_of(fundamentals, identity)
    # R04/D04.2: verified Meme persists even when fundamentals/MC/ATH are
    # temporarily missing -- missing data only changes DQ, never the class.
    # A Meme mark from either side counts; when fundamentals are absent the
    # identity side alone still preserves MEME (transient failure must not
    # flip a verified Meme to GENERAL).
    meme_hit = _is_meme(categories)

    float_ratio, fdv_mc = _fundamentals_numbers(fundamentals)
    low_float_hit = (
        float_ratio is not None
        and fdv_mc is not None
        and float_ratio < 0.35
        and fdv_mc >= 2
    )

    if meme_hit:
        return Profile(
            name=_BASE_TO_LITE["MEME"],
            base="MEME",
            full_name=_BASE_TO_FULL["MEME"],
            reason="MEME_CATEGORY",
        )
    if low_float_hit:
        return Profile(
            name=_BASE_TO_LITE["LOW_FLOAT_VC"],
            base="LOW_FLOAT_VC",
            full_name=_BASE_TO_FULL["LOW_FLOAT_VC"],
            reason="LOW_FLOAT_CONDITION",
        )
    if fundamentals is None:
        reason = "DEFAULT_GENERAL_NO_FUNDAMENTALS"
    elif not categories and (float_ratio is None or fdv_mc is None):
        reason = "DEFAULT_GENERAL_UNVERIFIED"
    else:
        reason = "DEFAULT_GENERAL"
    return Profile(
        name=_BASE_TO_LITE["GENERAL_ALT"],
        base="GENERAL_ALT",
        full_name=_BASE_TO_FULL["GENERAL_ALT"],
        reason=reason,
    )
