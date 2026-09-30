"""Task 5: read-only access to the versioned manual identity overrides.

The YAML file is the ONLY human-verified mapping table (design 4.2,
priority 1). It is version-controlled and read at runtime; this module
deliberately exposes no write/save API so production code cannot mutate
verified mappings on the fly.
"""

from __future__ import annotations

import pathlib
from typing import Any, Mapping

import yaml

DEFAULT_PATH = pathlib.Path(__file__).with_name("asset_overrides.yaml")

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
    """
    doc_path = pathlib.Path(path) if path is not None else DEFAULT_PATH
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


def _validate_entry(symbol: Any, entry: Any, doc_path: pathlib.Path) -> None:
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
