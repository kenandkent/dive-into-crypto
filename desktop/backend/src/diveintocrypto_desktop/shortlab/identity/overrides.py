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
}


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


def validate_override_entry(symbol: Any, entry: Any) -> None:
    """Validate one manual mapping entry against the frozen schema.

    Covers keys, types and multiplier provenance only. Chain/address
    *structure* is checked separately by :func:`validate_override_address`:
    the Task-5 built-in table is frozen ground truth (e.g. it carries a
    41-hex SHIB address that F04 may neither silently rewrite nor guess a
    replacement for), so structural rejection applies to NEW human input
    (overlay documents) while the resolver still case-safely normalizes
    every address at comparison time.
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
