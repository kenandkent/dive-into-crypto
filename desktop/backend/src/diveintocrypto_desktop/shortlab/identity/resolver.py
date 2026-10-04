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

from eth_hash.auto import keccak as _eth_keccak

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

# ---------------------------------------------------------------------------
# F04: safe chain-address normalization (design A6.3; plan F04.1-F04.4).
#
# ``normalize_chain_address`` is the ONLY place that interprets the case of
# an on-chain address. EVM addresses are validated as 20-byte hex and
# checksummed with Ethereum Keccak (``eth_hash``, never NIST SHA3 from
# hashlib); the comparison key is lowercase while ``display`` keeps the
# checksum. Solana addresses are base58-decoded to exactly 32 bytes and keep
# their case byte-for-byte. Unknown chains are never lowered.
# ---------------------------------------------------------------------------

#: Validation statuses returned by :func:`normalize_chain_address`.
CHAIN_ADDRESS_OK = "OK"
CHECKSUM_VERIFIED = "CHECKSUM_VERIFIED"
NO_CHECKSUM = "NO_CHECKSUM"
BAD_CHECKSUM = "BAD_CHECKSUM"
INVALID_ADDRESS = "INVALID_ADDRESS"
ADDRESS_CHAIN_UNSUPPORTED = "ADDRESS_CHAIN_UNSUPPORTED"

#: Chain labels whose addresses are 20-byte EVM hex (checked EIP-55).
EVM_CHAINS = frozenset(
    {
        "ethereum",
        "bsc",
        "binance-smart-chain",
        "arbitrum",
        "arbitrum-one",
        "arbitrum-nova",
        "polygon",
        "polygon-pos",
        "optimism",
        "optimistic-ethereum",
        "avalanche",
        "avalanche-c-chain",
        "base",
        "linea",
        "scroll",
        "zksync",
        "era",
        "mantle",
        "blast",
        "fantom",
        "gnosis",
        "celo",
        "moonbeam",
        "moonriver",
        "manta-pacific",
        "op-bnb",
    }
)

#: Chain labels whose addresses are case-sensitive base58 (never lowered).
SOLANA_CHAINS = frozenset({"solana"})

_EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {ch: i for i, ch in enumerate(_B58_ALPHABET)}


@dataclass(frozen=True)
class NormalizedAddress:
    """Case-safe view of one on-chain address.

    ``canonical_key`` is the comparison key (lowercase for EVM, case-kept
    for Solana/unknown chains so differently-cased keys never merge);
    ``display`` keeps the checksum (EIP-55 for EVM, raw for the rest);
    ``validation_status`` is one of the ``*_ADDRESS`` / ``CHECKSUM_*`` /
    ``NO_CHECKSUM`` / ``OK`` codes above; ``chain`` is the normalized
    chain label (or None when none was given).
    """

    canonical_key: str
    display: str
    validation_status: str
    chain: str | None = None


def _keccak_256(data: bytes) -> bytes:
    """Ethereum Keccak-256 (NOT NIST SHA3-256: different padding/domain)."""
    return _eth_keccak(data)


def _eip55_encode(lower_body: str) -> str:
    """EIP-55 checksum-encode 40 lowercase hex chars (no ``0x`` prefix)."""
    digest = _keccak_256(lower_body.encode("ascii"))
    out: list[str] = []
    for i, ch in enumerate(lower_body):
        if ch in "0123456789":
            out.append(ch)
        else:
            nibble = (digest[i // 2] >> (4 if i % 2 == 0 else 0)) & 0xF
            out.append(ch.upper() if nibble >= 8 else ch)
    return "".join(out)


def to_checksum_address(address: str) -> str:
    """EIP-55 checksum-encode an EVM address (raises ``ValueError`` unless
    it is 20-byte hex)."""
    raw = address.strip() if isinstance(address, str) else ""
    if not _EVM_ADDRESS_RE.match(raw):
        raise ValueError(f"not a 20-byte EVM hex address: {address!r}")
    return "0x" + _eip55_encode(raw[2:].lower())


def _base58_decode_32(text: str) -> bytes | None:
    """Base58-decode to exactly 32 bytes; ``None`` on any malformation."""
    if not text or any(ch not in _B58_INDEX for ch in text):
        return None
    number = 0
    for ch in text:
        number = number * 58 + _B58_INDEX[ch]
    hexed = format(number, "x")
    if len(hexed) % 2:
        hexed = "0" + hexed
    body = bytes.fromhex(hexed)
    pad = len(text) - len(text.lstrip("1"))
    result = b"\x00" * pad + body.lstrip(b"\x00")
    return result if len(result) == 32 else None


def normalize_chain_address(chain_id: Any, address: Any) -> NormalizedAddress:
    """Normalize one on-chain address without ever guessing across chains.

    - Solana (``chain_id`` in :data:`SOLANA_CHAINS`): base58 must decode to
      exactly 32 bytes; the key keeps the original case.
    - EVM-looking (``0x`` + 40 hex): mixed case is EIP-55 verified via
      Ethereum Keccak (``CHECKSUM_VERIFIED`` / ``BAD_CHECKSUM``); uniform
      case is accepted as ``NO_CHECKSUM``. The key is always lowercase.
    - EVM chain label but malformed: ``INVALID_ADDRESS`` (lowercase key
      for backward-compatible comparison only -- never a verification).
    - Anything else on an unknown/empty chain: ``ADDRESS_CHAIN_UNSUPPORTED``
      with the raw, never-lowered key.
    """
    raw = address.strip() if isinstance(address, str) else ""
    chain = chain_id.strip().lower() if isinstance(chain_id, str) else ""
    chain = chain or ""
    label = chain or None
    if chain in SOLANA_CHAINS:
        decoded = _base58_decode_32(raw)
        if decoded is None or len(decoded) != 32:
            return NormalizedAddress(raw, raw, INVALID_ADDRESS, label)
        return NormalizedAddress(raw, raw, CHAIN_ADDRESS_OK, label)
    if _EVM_ADDRESS_RE.match(raw):
        body = raw[2:]
        if body == body.lower() or body == body.upper():
            return NormalizedAddress(
                "0x" + body.lower(),
                "0x" + _eip55_encode(body.lower()),
                NO_CHECKSUM,
                label,
            )
        if body == _eip55_encode(body.lower()):
            return NormalizedAddress("0x" + body.lower(), raw, CHECKSUM_VERIFIED, label)
        return NormalizedAddress(raw, raw, BAD_CHECKSUM, label)
    if chain in EVM_CHAINS:
        return NormalizedAddress(raw.lower(), raw, INVALID_ADDRESS, label)
    return NormalizedAddress(raw, raw, ADDRESS_CHAIN_UNSUPPORTED, label)


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


def _norm_chain(value: Any) -> str | None:
    """Normalized chain label (lowercase); chain labels carry no case."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _norm_address(chain_norm: str | None, value: Any) -> str | None:
    """Comparison key for one contract address via
    :func:`normalize_chain_address` (EVM lowercase, Solana/unknown raw)."""
    if not isinstance(value, str) or not value.strip():
        return None
    return normalize_chain_address(chain_norm or "", value).canonical_key


def _contract_pair(meta: Mapping[str, Any]) -> tuple[str | None, str | None]:
    chain = _norm_chain(meta.get("chain"))
    return (_norm_address(chain, meta.get("contract_address")), chain)


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
        manual_chain = _norm_chain(entry.get("chain")) or _norm_chain(meta.get("chain"))
        manual_addr_raw = entry.get("contract_address") or meta.get("contract_address")
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
            chain=manual_chain,
            contract_address=_norm_address(manual_chain, manual_addr_raw),
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
        cand_chain = _norm_chain(c.get("chain"))
        cand_address = _norm_address(cand_chain, c.get("contract_address"))
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
                chain=cand_chain,
                contract_address=cand_address,
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
                    chain=_norm_chain(meta.get("chain")),
                    contract_address=_norm_address(
                        _norm_chain(meta.get("chain")), meta.get("contract_address")
                    ),
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
                chain=cand_chain,
                contract_address=cand_address,
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
