"""Task 5: canonical asset identity resolver (design 4.1/4.2).

Security boundary: no fundamental/unlock/social datum may enter scoring
until its futures symbol is bound to an explicit canonical asset. Symbol
strings alone are never enough.

Mapping priority (design 4.2):

1. MANUAL override in the versioned ``asset_overrides.yaml`` -> VERIFIED.
2. Exact contract-address + chain match -> VERIFIED.
3. Unique provider candidate with an exactly (prefix-normalized) matching
   symbol -> HIGH. Only when there is a single candidate.
4. ``1000``/``k``-style prefixed symbols that still yield several
   candidates -> UNRESOLVED. Never guessed.
5. Fuzzy/looks-like name matching is forbidden and is not implemented:
   names are never compared, only exact normalized symbols.

Unit rule: ``contract_multiplier`` / ``multiplier_source`` come ONLY from
explicit exchange unit metadata (``exchange_meta``) or from a manually
verified YAML entry. A bare ``1000`` prefix or a close spot price never
verifies a multiplier. Quote volumes and OI are already USD-notional and
are NEVER scaled by the multiplier -- only prices go through
:func:`canonical_price`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

VERIFIED = "VERIFIED"
HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"
UNRESOLVED = "UNRESOLVED"

MANUAL = "MANUAL"
CONTRACT = "CONTRACT"
UNIQUE_SYMBOL = "UNIQUE_SYMBOL"
OTHER = "OTHER"

EXCHANGE = "EXCHANGE"

# Quote-asset suffixes stripped before symbol comparison. Order matters:
# longer suffixes first so "USDT" wins over "USD".
_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "USD")

# Binance leveraged denominations: a leading "1" followed by 3+ zeros
# (1000PEPE, 10000..., 1000000MOG...). Narrow on purpose: a generic
# leading-digit strip would mangle real bases such as "1INCH".
_PREFIX_RE = re.compile(r"^10{3,}")

# Reason / status codes consumed by Task 11 (risk/DQ). Defined here so the
# caller gets stable machine codes without importing future modules.
VETO_DATA_IDENTITY = "VETO_DATA_IDENTITY"
IDENTITY_REVIEW_REQUIRED = "IDENTITY_REVIEW_REQUIRED"
MULTIPLIER_UNVERIFIED = "MULTIPLIER_UNVERIFIED"


@dataclass(frozen=True)
class AssetIdentity:
    """Design 4.1 identity record (snake_case; Task 14 maps to camelCase)."""

    canonical_id: str
    display_symbol: str
    name: str | None
    binance_futures_symbol: str
    # Set ONLY from a VERIFIED/HIGH binding. Never prefix-guessed, so the
    # spot client (Task 6) can never receive an unverified symbol.
    binance_spot_symbol: str | None
    contract_multiplier: float | None
    multiplier_source: str | None  # EXCHANGE | MANUAL | None
    coingecko_id: str | None
    unlock_provider_id: str | None
    social_provider_id: str | None
    chain: str | None
    contract_address: str | None
    categories: list[str] = field(default_factory=list)
    mapping_confidence: str = UNRESOLVED  # VERIFIED|HIGH|MEDIUM|LOW|UNRESOLVED
    mapping_source: str = OTHER  # MANUAL|CONTRACT|UNIQUE_SYMBOL|OTHER


def _strip_quote(symbol: str) -> str:
    s = symbol.upper()
    for suffix in _QUOTE_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def normalize_base(symbol: str) -> str:
    """Normalize for EXACT comparison only: upper, drop quote suffix, drop
    a leading 1000-style denomination. No fuzzy logic anywhere."""
    base = _strip_quote(symbol.strip())
    return _PREFIX_RE.sub("", base)


def _symbols_equal(a: str, b: str) -> bool:
    return normalize_base(a) == normalize_base(b)


def _valid_multiplier(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


def _extract_multiplier(
    override: Mapping[str, Any] | None,
    exchange_meta: Mapping[str, Any],
) -> tuple[float | None, str | None]:
    """Manual override wins; otherwise explicit exchange unit metadata.

    ``exchange_meta`` must carry an explicit ``multiplier_source`` of
    EXCHANGE (or MANUAL for already-merged metadata); a bare number without
    provenance is ignored so a ``1000`` prefix can never sneak in.
    """
    if override:
        m = _valid_multiplier(override.get("contract_multiplier"))
        if m is not None:
            src = override.get("multiplier_source", MANUAL)
            if src not in (EXCHANGE, MANUAL):
                src = MANUAL  # YAML entries are human-verified by definition
            return m, src
    m = _valid_multiplier(exchange_meta.get("contract_multiplier"))
    if m is not None and exchange_meta.get("multiplier_source") in (
        EXCHANGE,
        MANUAL,
    ):
        return m, exchange_meta["multiplier_source"]
    return None, None


def _norm_addr(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _contract_pair(meta: Mapping[str, Any]) -> tuple[str | None, str | None]:
    return (
        _norm_addr(meta.get("contract_address")),
        _norm_addr(meta.get("chain")),
    )


def _contract_conflicts(
    exchange_meta: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    """True when both sides state address+chain and they disagree.

    A sole candidate that contradicts known on-chain identity must not be
    promoted to HIGH on symbol equality alone.
    """
    meta_addr, meta_chain = _contract_pair(exchange_meta)
    if meta_addr is None or meta_chain is None:
        return False
    cand_addr, cand_chain = _contract_pair(candidate)
    if cand_addr is None or cand_chain is None:
        return False
    return (cand_addr, cand_chain) != (meta_addr, meta_chain)


def _unresolved(
    futures_symbol: str,
    multiplier: float | None,
    multiplier_source: str | None,
    source: str = OTHER,
) -> AssetIdentity:
    return AssetIdentity(
        canonical_id=futures_symbol.lower(),
        display_symbol=futures_symbol,
        name=None,
        binance_futures_symbol=futures_symbol,
        binance_spot_symbol=None,
        contract_multiplier=multiplier,
        multiplier_source=multiplier_source,
        coingecko_id=None,
        unlock_provider_id=None,
        social_provider_id=None,
        chain=None,
        contract_address=None,
        categories=[],
        mapping_confidence=UNRESOLVED,
        mapping_source=source,
    )


def resolve_identity(
    futures_symbol: str,
    exchange_meta: Mapping[str, Any] | None,
    provider_candidates: Sequence[Mapping[str, Any]] | None,
    overrides: Mapping[str, Any] | None,
) -> AssetIdentity:
    """Bind a Binance futures symbol to its canonical asset.

    Priority: MANUAL override > CONTRACT address+chain > UNIQUE_SYMBOL
    (single exact-matching candidate). Multi-candidate and empty sets are
    UNRESOLVED; names are never fuzzily compared.
    """
    meta: Mapping[str, Any] = exchange_meta or {}
    candidates = list(provider_candidates or [])
    table = overrides or {}

    # Accept both the full versioned doc {"version":..,"overrides":{..}}
    # and a bare {futures_symbol: entry} mapping.
    if isinstance(table.get("overrides"), Mapping):
        table = table["overrides"]
    entry = table.get(futures_symbol)
    if entry is None:
        entry = table.get(futures_symbol.upper())

    if isinstance(entry, Mapping):
        multiplier, mult_src = _extract_multiplier(entry, meta)
        spot = entry.get("binance_spot_symbol")
        return AssetIdentity(
            canonical_id=str(entry.get("canonical_id", futures_symbol.lower())),
            display_symbol=str(entry.get("display_symbol", futures_symbol)),
            name=entry.get("name"),
            binance_futures_symbol=futures_symbol,
            binance_spot_symbol=str(spot) if isinstance(spot, str) else None,
            contract_multiplier=multiplier,
            multiplier_source=mult_src,
            coingecko_id=entry.get("coingecko_id"),
            unlock_provider_id=entry.get("unlock_provider_id"),
            social_provider_id=entry.get("social_provider_id"),
            chain=entry.get("chain") or _norm_addr(meta.get("chain")),
            contract_address=entry.get("contract_address")
            or _norm_addr(meta.get("contract_address")),
            categories=list(entry.get("categories", [])),
            mapping_confidence=VERIFIED,
            mapping_source=MANUAL,
        )

    multiplier, mult_src = _extract_multiplier(None, meta)

    # Priority 2: exact contract address + chain.
    meta_addr, meta_chain = _contract_pair(meta)
    if meta_addr is not None and meta_chain is not None:
        matched = [
            c
            for c in candidates
            if _contract_pair(c) == (meta_addr, meta_chain)
        ]
        if len(matched) == 1:
            c = matched[0]
            return AssetIdentity(
                canonical_id=str(
                    c.get("coingecko_id")
                    or c.get("provider_symbol", futures_symbol).lower()
                ),
                display_symbol=str(
                    c.get("display_symbol")
                    or c.get("provider_symbol", futures_symbol)
                ),
                name=c.get("name"),
                binance_futures_symbol=futures_symbol,
                binance_spot_symbol=c.get("binance_spot_symbol")
                if isinstance(c.get("binance_spot_symbol"), str)
                else None,
                contract_multiplier=multiplier,
                multiplier_source=mult_src,
                coingecko_id=c.get("coingecko_id"),
                unlock_provider_id=c.get("unlock_provider_id"),
                social_provider_id=c.get("social_provider_id"),
                chain=meta_chain,
                contract_address=meta_addr,
                categories=list(c.get("categories", [])),
                mapping_confidence=VERIFIED,
                mapping_source=CONTRACT,
            )
        if len(matched) > 1:
            return _unresolved(futures_symbol, multiplier, mult_src)
        # Zero matches: fall through to the unique-symbol rule, unless the
        # lone candidate actively contradicts the known contract pair.

    # Priority 3: exactly one candidate, exact normalized symbol only.
    if len(candidates) == 1:
        c = candidates[0]
        if c.get("requires_review"):
            return AssetIdentity(
                canonical_id=str(
                    c.get("coingecko_id")
                    or c.get("provider_symbol", futures_symbol).lower()
                ),
                display_symbol=str(
                    c.get("display_symbol")
                    or c.get("provider_symbol", futures_symbol)
                ),
                name=c.get("name"),
                binance_futures_symbol=futures_symbol,
                binance_spot_symbol=None,
                contract_multiplier=multiplier,
                multiplier_source=mult_src,
                coingecko_id=c.get("coingecko_id"),
                unlock_provider_id=c.get("unlock_provider_id"),
                social_provider_id=c.get("social_provider_id"),
                chain=_norm_addr(c.get("chain")),
                contract_address=_norm_addr(c.get("contract_address")),
                categories=list(c.get("categories", [])),
                mapping_confidence=MEDIUM,
                mapping_source=OTHER,
            )
        provider_symbol = c.get("provider_symbol", "")
        if isinstance(provider_symbol, str) and _symbols_equal(
            futures_symbol, provider_symbol
        ):
            if _contract_conflicts(meta, c):
                return AssetIdentity(
                    canonical_id=futures_symbol.lower(),
                    display_symbol=futures_symbol,
                    name=None,
                    binance_futures_symbol=futures_symbol,
                    binance_spot_symbol=None,
                    contract_multiplier=multiplier,
                    multiplier_source=mult_src,
                    coingecko_id=None,
                    unlock_provider_id=None,
                    social_provider_id=None,
                    chain=_norm_addr(meta.get("chain")),
                    contract_address=_norm_addr(meta.get("contract_address")),
                    categories=[],
                    mapping_confidence=LOW,
                    mapping_source=OTHER,
                )
            return AssetIdentity(
                canonical_id=str(
                    c.get("coingecko_id") or provider_symbol.lower()
                ),
                display_symbol=str(
                    c.get("display_symbol") or provider_symbol
                ),
                name=c.get("name"),
                binance_futures_symbol=futures_symbol,
                binance_spot_symbol=c.get("binance_spot_symbol")
                if isinstance(c.get("binance_spot_symbol"), str)
                else None,
                contract_multiplier=multiplier,
                multiplier_source=mult_src,
                coingecko_id=c.get("coingecko_id"),
                unlock_provider_id=c.get("unlock_provider_id"),
                social_provider_id=c.get("social_provider_id"),
                chain=_norm_addr(c.get("chain")),
                contract_address=_norm_addr(c.get("contract_address")),
                categories=list(c.get("categories", [])),
                mapping_confidence=HIGH,
                mapping_source=UNIQUE_SYMBOL,
            )
        # Unique candidate whose symbol does not match: weak evidence.
        return AssetIdentity(
            canonical_id=futures_symbol.lower(),
            display_symbol=futures_symbol,
            name=None,
            binance_futures_symbol=futures_symbol,
            binance_spot_symbol=None,
            contract_multiplier=multiplier,
            multiplier_source=mult_src,
            coingecko_id=None,
            unlock_provider_id=None,
            social_provider_id=None,
            chain=None,
            contract_address=None,
            categories=[],
            mapping_confidence=LOW,
            mapping_source=OTHER,
        )

    # Priority 4: zero or several candidates (incl. 1000/k-prefix families).
    return _unresolved(futures_symbol, multiplier, mult_src)


def identity_execution_hint(
    identity: AssetIdentity,
) -> tuple[str, tuple[str, ...]]:
    """Identity-level execution hint for the caller (Task 11 owns the full
    status machine; this helper only encodes design 4.2 identity/multiplier
    semantics and never returns a bare READY).

    - LOW / UNRESOLVED -> ("BLOCKED", (VETO_DATA_IDENTITY, ...)).
    - MEDIUM -> ("NOT_READY", (IDENTITY_REVIEW_REQUIRED, ...)).
    - Missing trusted multiplier -> NOT_READY + MULTIPLIER_UNVERIFIED.
    - Otherwise -> ("READY_ELIGIBLE", ()) -- still needs DQ/entry gates.
    """
    reasons: list[str] = []
    if identity.mapping_confidence in (LOW, UNRESOLVED):
        reasons.append(VETO_DATA_IDENTITY)
        if identity.contract_multiplier is None:
            reasons.append(MULTIPLIER_UNVERIFIED)
        return "BLOCKED", tuple(reasons)
    if identity.mapping_confidence == MEDIUM:
        reasons.append(IDENTITY_REVIEW_REQUIRED)
        if identity.contract_multiplier is None:
            reasons.append(MULTIPLIER_UNVERIFIED)
        return "NOT_READY", tuple(reasons)
    if identity.contract_multiplier is None:
        return "NOT_READY", (MULTIPLIER_UNVERIFIED,)
    return "READY_ELIGIBLE", ()


def canonical_price(
    futures_price: float | None, multiplier: float | None
) -> float | None:
    """Convert a leveraged-contract quote to a per-coin canonical price.

    ``canonical_price = futures_price / multiplier``. Unknown, zero or
    non-finite multipliers yield None (caller treats dependent cross-source
    metrics as null + MULTIPLIER_UNVERIFIED). Volumes and OI must never be
    passed through here.
    """
    if futures_price is None or multiplier is None:
        return None
    try:
        p = float(futures_price)
        m = float(multiplier)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or not math.isfinite(m) or m <= 0:
        return None
    return p / m
