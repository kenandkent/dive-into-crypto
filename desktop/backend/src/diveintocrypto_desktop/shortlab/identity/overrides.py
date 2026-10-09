"""Task 5: read-only access to the versioned manual identity overrides.

The YAML file is the ONLY human-verified mapping table (design 4.2,
priority 1). It is version-controlled and read at runtime; this module
deliberately exposes no write/save API so production code cannot mutate
verified mappings on the fly.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Mapping

import yaml

from diveintocrypto_desktop.shortlab.identity import resolver as _resolver

DEFAULT_PATH = pathlib.Path(__file__).with_name("asset_overrides.yaml")

#: Packaged resource name for the built-in table (F09, design A9.3).
_BUILTIN_RESOURCE = "shortlab/identity/asset_overrides.yaml"

#: Environment variable pointing at the user-supplied identity overlay file.
#: The overlay wins over the built-in table but must pass the same schema
#: and chain/address validation; syntax errors fail loud (the identity
#: capability becomes unavailable) instead of silently degrading to an
#: empty directory.
IDENTITY_OVERRIDES_ENV = "SHORTLAB_IDENTITY_OVERRIDES_PATH"

_ENTRY_KEYS = {
    "canonical_id",
    "display_symbol",
    "name",
    "binance_spot_symbol",
    "contract_multiplier",
    "multiplier_source",
    "coingecko_id",
    "unlock_provider_id",
    "social_provider_id",
    "chain",
    "contract_address",
    "categories",
    # R02a/D04.2: optional human-verified scoring profile (no LITE/FULL suffix;
    # the tier suffix is decided by analysis_tier, not stored here).
    "profile",
}

#: R02a/D04.2: allowed manual profile values (tier-independent base only).
ALLOWED_PROFILES = frozenset({"MEME", "GENERAL", "LOW_FLOAT_VC"})


def load_overrides(path: str | pathlib.Path | None = None) -> dict:
    """Load and validate the versioned overrides document.

    Returns the full document ``{"version": int, "overrides": {...}}``.
    Raises ``ValueError`` on any schema violation (fail loud: a corrupt
    manual table must never silently degrade to auto-guessing).

    The built-in table (``path is None``) reads via
    :func:`resources.read_resource_text` so frozen builds need no source
    checkout; explicit overlay paths still read from the filesystem and
    domain validation is unchanged.
    """
    if path is None:
        from diveintocrypto_desktop.resources import read_resource_text as _read_res

        try:
            data = yaml.safe_load(_read_res(_BUILTIN_RESOURCE))
        except FileNotFoundError as exc:
            raise ValueError(f"overrides document not found: {exc}") from exc
        doc_path: Any = _BUILTIN_RESOURCE
    else:
        doc_path = pathlib.Path(path)
        with open(doc_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"overrides document must be a mapping: {doc_path}")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"overrides 'version' must be an int: {doc_path}")
    table = data.get("overrides")
    if not isinstance(table, dict):
        raise ValueError(f"overrides 'overrides' must be a mapping: {doc_path}")
    for symbol, entry in table.items():
        _validate_entry(symbol, entry, doc_path)
    return {"version": version, "overrides": dict(table)}


def normalize_profile(value: Any) -> str | None:
    """Normalize one profile value to its tier-independent base (R02a/D04.2).

    Accepts only ``MEME``/``GENERAL``/``LOW_FLOAT_VC`` (case-insensitive,
    surrounding whitespace ignored); ``LITE``/``FULL`` suffixed forms and any
    other string return ``None`` (caller rejects them as schema violations).
    ``GENERAL_ALT`` is accepted as an alias of ``GENERAL`` for scorer
    compatibility but normalizes to ``GENERAL``.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    if text == "GENERAL_ALT":
        return "GENERAL"
    if text in ALLOWED_PROFILES:
        return text
    return None


def validate_override_entry(symbol: Any, entry: Any) -> None:
    """Validate one manual mapping entry against the frozen schema.

    Covers keys, types, multiplier provenance and the optional R02a
    ``profile`` (``MEME``/``GENERAL``/``LOW_FLOAT_VC`` only, no LITE/FULL
    suffix). Chain/address *structure* is checked separately by
    :func:`validate_override_address`: the Task-5 built-in table is frozen
    ground truth (e.g. it carries a 41-hex SHIB address that F04 may neither
    silently rewrite nor guess a replacement for), so structural rejection
    applies to NEW human input (overlay documents) while the resolver still
    case-safely normalizes every address at comparison time.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError(f"override key must be a non-empty symbol: {symbol!r}")
    if not isinstance(entry, dict):
        raise ValueError(f"override for {symbol!r} must be a mapping")
    unknown = set(entry) - _ENTRY_KEYS
    if unknown:
        raise ValueError(f"override for {symbol!r} has unknown keys: {sorted(unknown)}")
    mult = entry.get("contract_multiplier")
    if mult is not None:
        if isinstance(mult, bool) or not isinstance(mult, (int, float)) or mult <= 0:
            raise ValueError(
                f"override for {symbol!r}: contract_multiplier must be > 0"
            )
        src = entry.get("multiplier_source", "MANUAL")
        if src != "MANUAL":
            raise ValueError(
                f"override for {symbol!r}: multiplier_source must be MANUAL"
            )
    spot = entry.get("binance_spot_symbol")
    if spot is not None and (not isinstance(spot, str) or not spot.strip()):
        raise ValueError(
            f"override for {symbol!r}: binance_spot_symbol must be a string"
        )
    cats = entry.get("categories", [])
    if not isinstance(cats, list) or any(not isinstance(c, str) for c in cats):
        raise ValueError(f"override for {symbol!r}: categories must be a list[str]")
    if "profile" in entry:
        if normalize_profile(entry.get("profile")) is None:
            raise ValueError(
                f"override for {symbol!r}: profile must be one of "
                f"{sorted(ALLOWED_PROFILES)} (no LITE/FULL suffix)"
            )


def validate_override_address(symbol: Any, entry: Any) -> None:
    """Structurally validate one entry's chain/address (new human input).

    Raises ``ValueError`` for malformed (``INVALID_ADDRESS``) or
    wrong-checksum (``BAD_CHECKSUM``) EVM addresses; unknown chains are kept
    case-intact (``ADDRESS_CHAIN_UNSUPPORTED``) and pass. Entries without a
    ``contract_address`` (e.g. native BTC) trivially pass.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"override for {symbol!r} must be a mapping")
    address = entry.get("contract_address")
    if address is None:
        return
    if not isinstance(address, str) or not address.strip():
        raise ValueError(f"override for {symbol!r}: contract_address must be a string")
    norm = _resolver.normalize_chain_address(entry.get("chain") or "", address)
    if norm.validation_status in (
        _resolver.INVALID_ADDRESS,
        _resolver.BAD_CHECKSUM,
    ):
        raise ValueError(
            f"override for {symbol!r}: contract_address fails chain "
            f"validation ({norm.validation_status})"
        )


def _validate_entry(symbol: Any, entry: Any, doc_path: pathlib.Path) -> None:
    try:
        validate_override_entry(symbol, entry)
    except ValueError as exc:
        raise ValueError(f"{doc_path}: {exc}") from exc


def get_override(
    futures_symbol: str, overrides: Mapping[str, Any] | None
) -> dict | None:
    """Return the manual entry for a futures symbol, or None.

    Accepts either the full versioned document or a bare symbol->entry
    mapping. Exact (case-normalized) key match only -- no prefix stripping,
    no fuzzy matching.
    """
    if not overrides:
        return None
    table: Mapping[str, Any] = overrides
    inner = overrides.get("overrides")
    if isinstance(inner, Mapping):
        table = inner
    entry = table.get(futures_symbol)
    if entry is None:
        entry = table.get(futures_symbol.upper())
    return dict(entry) if isinstance(entry, Mapping) else None


def _categories_of(*sources: Any) -> list[str]:
    """Collect category strings from identity/fundamentals mappings or objects."""
    out: list[str] = []
    for source in sources:
        if source is None:
            continue
        cats: Any = None
        if isinstance(source, Mapping):
            cats = source.get("categories")
        else:
            cats = getattr(source, "categories", None)
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


def _is_meme_category(categories: list[str]) -> bool:
    """True when any category marks a Meme asset (case-insensitive)."""
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


def _low_float_hit(fundamentals: Any) -> bool:
    """True for the verified low-float condition (float<0.35, FDV/MC>=2)."""
    if fundamentals is None:
        return False
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
    float_ratio = circ / total if circ is not None and total not in (None, 0) and total > 0 else None
    fdv_mc = fdv / mc if fdv is not None and mc is not None and mc > 0 else None
    return (
        float_ratio is not None
        and fdv_mc is not None
        and float_ratio < 0.35
        and fdv_mc >= 2
    )


def get_manual_profile(override_entry: Mapping[str, Any] | None) -> str | None:
    """Manual profile from one override entry, or ``None`` (R02a/D04.2).

    Only the tier-independent ``MEME``/``GENERAL``/``LOW_FLOAT_VC`` values
    count; anything else (including LITE/FULL suffixes) is not a manual
    override.
    """
    if not isinstance(override_entry, Mapping):
        return None
    return normalize_profile(override_entry.get("profile"))


def effective_profile(
    override_entry: Mapping[str, Any] | None = None,
    identity: Any = None,
    fundamentals: Any = None,
) -> tuple[str, str, bool]:
    """Resolve the tier-independent profile (R02a/D04.2).

    Priority: manual ``profile`` in the override entry > verified Meme
    category on the identity (or fundamentals) > verified low-float
    condition > ``GENERAL``. A known Meme never degrades to ``GENERAL``
    merely because fundamentals/MC/ATH are temporarily missing -- missing
    data only changes DQ, never the class. Returns
    ``(profile, reason, is_manual)`` with ``profile`` in
    ``MEME``/``GENERAL``/``LOW_FLOAT_VC`` and ``reason`` in
    ``MANUAL_OVERRIDE``/``MEME_CATEGORY``/``LOW_FLOAT_CONDITION``/
    ``DEFAULT_GENERAL*``.
    """
    manual = get_manual_profile(override_entry)
    if manual is not None:
        return manual, "MANUAL_OVERRIDE", True
    categories = _categories_of(identity, fundamentals)
    # Verified Meme persists even when fundamentals are missing/failed.
    if _is_meme_category(categories):
        # Require the Meme mark to come from the identity side when
        # fundamentals are absent, so a transient fundamentals failure
        # cannot flip a verified Meme to GENERAL.
        identity_cats = _categories_of(identity)
        fund_cats = _categories_of(fundamentals)
        if _is_meme_category(identity_cats) or (
            fundamentals is not None and _is_meme_category(fund_cats)
        ):
            return "MEME", "MEME_CATEGORY", False
        # Fallthrough safety: any meme string still counts as Meme.
        return "MEME", "MEME_CATEGORY", False
    if _low_float_hit(fundamentals):
        return "LOW_FLOAT_VC", "LOW_FLOAT_CONDITION", False
    if fundamentals is None:
        return "GENERAL", "DEFAULT_GENERAL_NO_FUNDAMENTALS", False
    if not categories:
        return "GENERAL", "DEFAULT_GENERAL_UNVERIFIED", False
    return "GENERAL", "DEFAULT_GENERAL", False


def _load_overlay_document(path: str | pathlib.Path) -> dict:
    """Read + validate one overlay file; ``ValueError`` on any problem.

    A missing file, YAML syntax error or schema violation fails loud: the
    caller must treat the identity capability as unavailable, never as an
    empty directory.
    """
    doc_path = pathlib.Path(path)
    if not doc_path.exists():
        raise ValueError(f"identity overlay not found: {doc_path}")
    try:
        with open(doc_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"identity overlay syntax error at {doc_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"identity overlay must be a mapping: {doc_path}")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"identity overlay 'version' must be an int: {doc_path}")
    table = data.get("overrides")
    if not isinstance(table, dict):
        raise ValueError(f"identity overlay 'overrides' must be a mapping: {doc_path}")
    for symbol, entry in table.items():
        _validate_entry(symbol, entry, doc_path)
        try:
            validate_override_address(symbol, entry)
        except ValueError as exc:
            raise ValueError(f"{doc_path}: {exc}") from exc
    return {"version": version, "overrides": dict(table)}


def load_effective_overrides(
    path: str | pathlib.Path | None = None,
    embedded: Mapping[str, Any] | None = None,
) -> dict:
    """Merge the built-in table with the user overlay (design A6.3 layer 3).

    ``path`` wins over the ``SHORTLAB_IDENTITY_OVERRIDES_PATH`` environment
    variable; when neither is set only the built-in table applies.
    ``embedded`` replaces the built-in document on disk (tests) and must be
    a versioned ``{"version": int, "overrides": {...}}`` document.

    Returns a versioned mapping ``{"version", "overrides", "sources",
    "builtin_version", "overlay_version", "overlay_path"}``. The overlay
    wins per symbol; ``sources[symbol]`` records ``source``
    (``"overlay"``/``"builtin"``), whether it ``overridden`` a built-in
    entry, and both layer versions -- so a change only affects new
    snapshots. Any overlay problem raises ``ValueError`` (fail loud).
    """
    if embedded is None:
        builtin = load_overrides()
    elif (
        isinstance(embedded, Mapping)
        and isinstance(embedded.get("version"), int)
        and not isinstance(embedded.get("version"), bool)
        and isinstance(embedded.get("overrides"), Mapping)
    ):
        for symbol, entry in embedded["overrides"].items():
            _validate_entry(symbol, entry, pathlib.Path("<embedded>"))
        builtin = {"version": embedded["version"], "overrides": dict(embedded["overrides"])}
    else:
        raise ValueError("embedded must be a versioned overrides document")

    overlay_path = str(path) if path is not None else os.environ.get(IDENTITY_OVERRIDES_ENV)
    if not overlay_path:
        table = dict(builtin["overrides"])
        return {
            "version": builtin["version"],
            "overrides": table,
            "sources": {
                symbol: {
                    "source": "builtin",
                    "overridden": False,
                    "builtin_version": builtin["version"],
                    "overlay_version": None,
                }
                for symbol in table
            },
            "builtin_version": builtin["version"],
            "overlay_version": None,
            "overlay_path": None,
        }
    overlay = _load_overlay_document(overlay_path)
    merged = {**builtin["overrides"], **overlay["overrides"]}
    return {
        "version": overlay["version"],
        "overrides": merged,
        "sources": {
            symbol: {
                "source": "overlay" if symbol in overlay["overrides"] else "builtin",
                "overridden": symbol in overlay["overrides"]
                and symbol in builtin["overrides"],
                "builtin_version": builtin["version"],
                "overlay_version": overlay["version"],
            }
            for symbol in merged
        },
        "builtin_version": builtin["version"],
        "overlay_version": overlay["version"],
        "overlay_path": str(overlay_path),
    }
