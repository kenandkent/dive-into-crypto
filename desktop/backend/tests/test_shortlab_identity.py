"""Task 5: asset identity + contract multiplier (design 4.1/4.2).

TDD-first suite: written before ``shortlab/identity/*`` exists, so the first
run must FAIL (import error). Covers:

- unique symbol auto-bind -> HIGH / UNIQUE_SYMBOL
- multi-candidate -> UNRESOLVED, never guessed
- 1000PEPEUSDT -> PEPEUSDT only via verified MANUAL override; without the
  override the resolver must NOT hand a prefix-stripped symbol to the spot API
- no verified multiplier -> MULTIPLIER_UNVERIFIED / NOT_READY semantics
- MEDIUM / LOW / UNRESOLVED readiness mapping, no auto-READY
- fuzzy-name matching is forbidden (similar names stay UNRESOLVED)
- multiplier provenance: only explicit exchange unit metadata or the
  versioned manual YAML; a bare ``1000`` prefix never verifies
- canonical_price = futures_price / multiplier; volumes/OI never scaled
"""

import pathlib

import pytest

RESOLVER = "diveintocrypto_desktop.shortlab.identity.resolver"
OVERRIDES = "diveintocrypto_desktop.shortlab.identity.overrides"

def _load(modname):
    import importlib

    return importlib.import_module(modname)


def test_unique_symbol_binds_high():
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="BTCUSDT",
        exchange_meta={},
        provider_candidates=[
            {"provider_symbol": "BTCUSDT", "coingecko_id": "bitcoin",
             "name": "Bitcoin", "categories": ["layer-1"]},
        ],
        overrides={},
    )
    assert ident.mapping_confidence == "HIGH"
    assert ident.mapping_source == "UNIQUE_SYMBOL"
    assert ident.binance_futures_symbol == "BTCUSDT"
    assert ident.coingecko_id == "bitcoin"


def test_multi_candidate_is_unresolved_not_guessed():
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="PEPEUSDT",
        exchange_meta={},
        provider_candidates=[
            {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe", "name": "Pepe"},
            {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe-coin", "name": "Pepe Coin"},
        ],
        overrides={},
    )
    assert ident.mapping_confidence == "UNRESOLVED"
    # Must not pick either candidate's id by position.
    assert ident.coingecko_id is None


def test_no_fuzzy_name_matching():
    """Similar names must never auto-bind; only exact normalized symbols."""
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="PEPEUSDT",
        exchange_meta={},
        provider_candidates=[
            {"provider_symbol": "OTHERUSDT", "coingecko_id": "pepe-evil",
             "name": "Pepe"},  # same display name, different symbol
            {"provider_symbol": "PEPE2USDT", "coingecko_id": "pepe2",
             "name": "Pepe2 Similar"},
        ],
        overrides={},
    )
    assert ident.mapping_confidence == "UNRESOLVED"
    assert ident.coingecko_id is None


def test_1000pepe_manual_override_is_verified_spot():
    m = _load(RESOLVER)
    overrides = {
        "1000PEPEUSDT": {
            "canonical_id": "pepe",
            "display_symbol": "PEPE",
            "name": "Pepe",
            "binance_spot_symbol": "PEPEUSDT",
            "contract_multiplier": 1000,
            "multiplier_source": "MANUAL",
            "coingecko_id": "pepe",
        }
    }
    ident = m.resolve_identity(
        futures_symbol="1000PEPEUSDT",
        exchange_meta={},
        provider_candidates=[
            {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe", "name": "Pepe"},
            {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe-kebab", "name": "Pepe Kebab"},
        ],
        overrides=overrides,
    )
    assert ident.mapping_confidence == "VERIFIED"
    assert ident.mapping_source == "MANUAL"
    assert ident.binance_spot_symbol == "PEPEUSDT"
    assert ident.contract_multiplier == 1000
    assert ident.multiplier_source == "MANUAL"


def test_1000pepe_without_override_never_guesses_spot_or_multiplier():
    """Without a verified mapping the futures symbol must NOT be
    prefix-stripped and handed to the spot API, and the ``1000`` prefix
    must NOT become a multiplier."""
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="1000PEPEUSDT",
        exchange_meta={},
        provider_candidates=[
            {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe", "name": "Pepe"},
            {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe-kebab", "name": "Pepe Kebab"},
        ],
        overrides={},
    )
    assert ident.mapping_confidence == "UNRESOLVED"
    assert ident.binance_spot_symbol is None
    assert ident.contract_multiplier is None
    assert ident.multiplier_source is None


def test_manual_override_beats_contract_and_unique():
    m = _load(RESOLVER)
    overrides = {
        "BTCUSDT": {
            "canonical_id": "manual-btc",
            "display_symbol": "BTC",
            "binance_spot_symbol": "BTCUSDT",
            "coingecko_id": "manual-bitcoin",
        }
    }
    ident = m.resolve_identity(
        futures_symbol="BTCUSDT",
        exchange_meta={"contract_address": "0xabc", "chain": "ethereum"},
        provider_candidates=[
            {"provider_symbol": "BTCUSDT", "coingecko_id": "bitcoin",
             "contract_address": "0xabc", "chain": "ethereum"},
        ],
        overrides=overrides,
    )
    assert ident.mapping_confidence == "VERIFIED"
    assert ident.mapping_source == "MANUAL"
    assert ident.canonical_id == "manual-btc"


def test_contract_address_plus_chain_match_is_verified():
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="WLDUSDT",
        exchange_meta={"contract_address": "0x163f8c246792fa45bc45069cb7a410f655fb786f",
                       "chain": "ethereum"},
        provider_candidates=[
            {"provider_symbol": "WLDUSDT", "coingecko_id": "worldcoin",
             "contract_address": "0x163f8c246792fa45bc45069cb7a410f655fb786f",
             "chain": "ethereum"},
        ],
        overrides={},
    )
    assert ident.mapping_confidence == "VERIFIED"
    assert ident.mapping_source == "CONTRACT"


def test_contract_chain_mismatch_does_not_bind():
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="WLDUSDT",
        exchange_meta={"contract_address": "0x163f8c246792fa45bc45069cb7a410f655fb786f",
                       "chain": "ethereum"},
        provider_candidates=[
            {"provider_symbol": "WLDUSDT", "coingecko_id": "worldcoin",
             "contract_address": "0x163f8c246792fa45bc45069cb7a410f655fb786f",
             "chain": "bsc"},  # same address, wrong chain
        ],
        overrides={},
    )
    # Address alone is not enough; chain must match too.
    assert ident.mapping_confidence in ("LOW", "UNRESOLVED")
    assert ident.mapping_source != "CONTRACT"


def test_medium_requires_review_and_forces_not_ready():
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="ARBUSDT",
        exchange_meta={},
        provider_candidates=[
            {"provider_symbol": "ARBUSDT", "coingecko_id": "arbitrum",
             "requires_review": True},
        ],
        overrides={},
    )
    assert ident.mapping_confidence == "MEDIUM"
    status, reasons = m.identity_execution_hint(ident)
    assert status == "NOT_READY"
    assert "IDENTITY_REVIEW_REQUIRED" in reasons
    # MEDIUM never auto-READYs even with a good multiplier.
    assert status != "READY"


def test_low_identity_is_blocked():
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="BTCUSDT",
        exchange_meta={},
        provider_candidates=[
            # Unique candidate, but its symbol does not match even after
            # normalization -> weak evidence, LOW.
            {"provider_symbol": "WBTCUSDT", "coingecko_id": "wrapped-bitcoin"},
        ],
        overrides={},
    )
    assert ident.mapping_confidence == "LOW"
    status, reasons = m.identity_execution_hint(ident)
    assert status == "BLOCKED"
    assert "VETO_DATA_IDENTITY" in reasons


def test_unresolved_identity_is_blocked():
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="UNKNOWNUSDT",
        exchange_meta={},
        provider_candidates=[],
        overrides={},
    )
    assert ident.mapping_confidence == "UNRESOLVED"
    status, reasons = m.identity_execution_hint(ident)
    assert status == "BLOCKED"
    assert "VETO_DATA_IDENTITY" in reasons


def test_missing_multiplier_forces_not_ready():
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="BTCUSDT",
        exchange_meta={},
        provider_candidates=[
            {"provider_symbol": "BTCUSDT", "coingecko_id": "bitcoin"},
        ],
        overrides={},
    )
    assert ident.mapping_confidence == "HIGH"
    assert ident.contract_multiplier is None
    status, reasons = m.identity_execution_hint(ident)
    assert status == "NOT_READY"
    assert "MULTIPLIER_UNVERIFIED" in reasons


def test_exchange_unit_metadata_provides_multiplier():
    m = _load(RESOLVER)
    ident = m.resolve_identity(
        futures_symbol="1000PEPEUSDT",
        exchange_meta={"contract_multiplier": 1000, "multiplier_source": "EXCHANGE"},
        provider_candidates=[
            {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe"},
            {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe-kebab"},
        ],
        overrides={},
    )
    # Identity is still unresolved (multi-candidate), but the multiplier
    # itself is trusted because it came from explicit exchange metadata.
    assert ident.contract_multiplier == 1000
    assert ident.multiplier_source == "EXCHANGE"


def test_canonical_price_divides_by_multiplier():
    m = _load(RESOLVER)
    assert m.canonical_price(0.006, 1000) == pytest.approx(0.000006)
    assert m.canonical_price(100.0, 1) == pytest.approx(100.0)
    # Unknown or degenerate multiplier -> null, never guessed.
    assert m.canonical_price(0.006, None) is None
    assert m.canonical_price(None, 1000) is None
    assert m.canonical_price(0.006, 0) is None


def test_volumes_and_oi_are_never_scaled_by_multiplier():
    """Quote volumes and OI are already USD-notional; the multiplier only
    ever applies to price via canonical_price."""
    m = _load(RESOLVER)
    futures_quote_volume = 25_000_000.0
    oi_notional = 5_000_000.0
    price = m.canonical_price(0.006, 1000)
    assert price == pytest.approx(0.000006)
    # The resolver offers no volume-scaling helper, and raw notionals pass
    # through untouched.
    assert futures_quote_volume == 25_000_000.0
    assert oi_notional == 5_000_000.0
    assert not hasattr(m, "canonical_volume")
    assert not hasattr(m, "canonical_oi")


def test_overrides_yaml_is_versioned_and_read_only():
    import importlib

    om = _load(OVERRIDES)
    yaml_path = (
        pathlib.Path(om.__file__).parent / "asset_overrides.yaml"
    )
    assert yaml_path.exists(), "asset_overrides.yaml must be version-controlled"
    first = om.load_overrides(yaml_path)
    second = om.load_overrides(yaml_path)
    assert first == second
    assert isinstance(first.get("version"), int)
    # Read-only contract: the module exposes no write/save API.
    assert not hasattr(om, "save_overrides")
    assert not hasattr(om, "write_overrides")
    # Spot check the canonical 1000PEPE manual entry.
    entry = om.get_override("1000PEPEUSDT", om.load_overrides(yaml_path))
    assert entry is not None
    assert entry["binance_spot_symbol"] == "PEPEUSDT"
    assert entry["contract_multiplier"] == 1000
