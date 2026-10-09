"""R02a Resolver/Override + R02b Catalog (shared file; plan R02; design D04.1/D04.2/D19).

R02a ownership: resolver.py / overrides.py / asset_overrides.yaml only
(catalog.py untouched). Contract: resolve_asset_context(symbol,
exchange_meta, catalog_candidates, overrides) -> AssetIdentity;
resolve_identity stays as compat wrapper. Catalog input is the R00 candidate
fixture via make_verified_candidate (reads the packaged verified set, never
Catalog privates). Covers: verified BTC m1/MANUAL, 1000, candidate
conflict (UNKNOWN + IDENTITY_UNIT_CONFLICT, never guessed), Decimal
multipliers, mapping_confidence single field + Hedge adaptation, EIP-55/
Solana rules, and profile (MEME/GENERAL/LOW_FLOAT_VC, manual-first, Meme
missing-data stays Meme).

R02b: Catalog candidates + source stability (plan R02b; design D04.1/D19).

Ownership: R02b (``shortlab/identity/catalog.py`` only). Read-only consumption:
R00 ``repair_fixtures`` identity helpers + ``FIXTURE_NOW``, F04
``IdentityCatalog``/``normalize_chain_address`` behaviour, the packaged
``verified_assets.yaml`` / ``asset_overrides.yaml`` files.

Contract under test (R02b): ``IdentityCatalog.candidates(symbol, as_of_ms)``
keeps its signature and returns R00-compatible candidate mappings. The
resolver stays a contract-only consumer: ambiguous families are all retained
so the resolver stays UNRESOLVED instead of position-picking the first entry.

Sections:
  R02a-*  Resolver/Override (this task; on top per shared-file note)
  R02b-1  single-symbol overlay (per-symbol MANUAL win, case-insensitive)
  R02b-2  verified-directory metadata validation (fail loud, never guessed)
  R02b-3  real directory file reading (packaged YAML, multiplier provenance)
  R02b-4  directory failure behaviour (keyless / failure-keeps-receipt /
           429 backoff / bad schema / loud overlay / expiry / ambiguity)
  R02b-5  Service default binding + Hedge adaptation seam (documents R10b work,
           asserts current wiring without modifying Service)

NOTE (R02b agent, 2026-10-09): shared file now carries R02a on top and R02b
below with both headers preserved.
"""

from __future__ import annotations

import pytest

CATALOG = "diveintocrypto_desktop.shortlab.identity.catalog"

# Same frozen instant as R00 repair_fixtures.FIXTURE_NOW (hard-coded to avoid
# importing tests helpers into collection; value verified against
# desktop/backend/tests/repair_fixtures.py).
FIXTURE_NOW = 1791417600000


def _load(modname):
    import importlib

    return importlib.import_module(modname)


def _clock(start_ms: int = FIXTURE_NOW - 60000):
    state = {"ms": start_ms}

    def _now() -> int:
        return int(state["ms"])

    _now.advance = lambda ms: state.update(ms=state["ms"] + ms)  # type: ignore[attr-defined]
    return _now


class _NoopLimiter:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeDirHttp:
    """Fake catalog HTTP layer: records (url, params, ctx), zero real sends."""

    def __init__(self, payload=None, error=None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple] = []

    async def __call__(self, url, params, request_context=None):
        self.calls.append((url, dict(params), request_context))
        if isinstance(self.error, BaseException):
            raise self.error
        if callable(self.error):
            raise self.error()
        return self.payload

    def hosts(self) -> list[str]:
        import urllib.parse

        return [urllib.parse.urlparse(u).netloc for u, _, _ in self.calls]


DIR_LIST = [
    {"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"},
    {"id": "pepe", "symbol": "pepe", "name": "Pepe"},
    # Same normalized symbol as ``pepe`` -> conflict fixture, never guessed.
    {"id": "pepe-kebab", "symbol": "pepe", "name": "Pepe Kebab"},
    {"id": "solana", "symbol": "sol", "name": "Solana"},
]


def _verified_doc():
    return {
        "version": 3,
        "assets": {
            "BTCUSDT": {
                "canonical_id": "bitcoin",
                "display_symbol": "BTC",
                "name": "Bitcoin",
                "binance_spot_symbol": "BTCUSDT",
                "contract_multiplier": 1,
                "multiplier_source": "MANUAL",
                "coingecko_id": "bitcoin",
                "categories": ["layer-1"],
            },
            "ETHUSDT": {
                "canonical_id": "ethereum",
                "display_symbol": "ETH",
                "name": "Ethereum",
                "binance_spot_symbol": "ETHUSDT",
                "contract_multiplier": 1,
                "multiplier_source": "MANUAL",
                "coingecko_id": "ethereum",
                "chain": "ethereum",
                "categories": ["layer-1"],
            },
        },
    }


def _overrides_doc():
    return {"version": 7, "overrides": {}}


def _catalog(http=None, clock=None, **kwargs):
    mod = _load(CATALOG)
    kwargs.setdefault("api_plan", "demo")
    kwargs.setdefault("api_key", "DEMO-KEY-1")
    kwargs.setdefault("verified", _verified_doc())
    kwargs.setdefault("overrides_doc", _overrides_doc())
    kwargs.setdefault("clock", clock or _clock())
    kwargs.setdefault("http_get", http or FakeDirHttp(payload=list(DIR_LIST)))
    kwargs.setdefault("rate_limiter", _NoopLimiter())
    return mod.IdentityCatalog(**kwargs)


def _ctx(budget=None, **kwargs):
    from diveintocrypto_desktop.shortlab.request_budget import make_request_context

    return make_request_context(budget, **kwargs)


def make_verified_candidate(symbol: str = "BTCUSDT") -> dict:
    """R00-style frozen helper: read the packaged verified set for real.

    Returns the exact candidate mapping ``IdentityCatalog.candidates`` serves
    for a verified symbol (single MANUAL row + catalog provenance). Used to
    pin R02b output to the frozen R00 identity fixture values without
    importing R00 test helpers into production.
    """
    mod = _load(CATALOG)
    assets = mod.load_verified_assets()["assets"]
    key = symbol.strip().upper()
    assert key in assets, f"frozen verified set has no {symbol!r}"
    entry = dict(assets[key])
    entry.update(
        {
            "provider_symbol": key,
            "catalog_source": "verified-assets",
            "mapping_source": "MANUAL",
        }
    )
    return entry


@pytest.fixture(autouse=True)
def _scrub_overlay_env(monkeypatch):
    monkeypatch.delenv("SHORTLAB_IDENTITY_OVERRIDES_PATH", raising=False)


def _write_single_symbol_overlay(path, symbol: str = "BTCUSDT") -> str:
    path.write_text(
        'version: 2\noverrides:\n  "'
        + symbol
        + '":\n    canonical_id: "manual-btc"\n    display_symbol: "BTC"\n    name: "Manual Bitcoin"\n    binance_spot_symbol: "BTCUSDT"\n    contract_multiplier: 1\n    multiplier_source: "MANUAL"\n    coingecko_id: "manual-bitcoin"\n',
        encoding="utf-8",
    )
    return str(path)


# ---------------------------------------------------------------------------
# R02a Resolver/Override (plan R02 R02a; design D04.1/D04.2)
#
# Ownership: R02a (resolver.py / overrides.py / asset_overrides.yaml only).
# Contract: resolve_asset_context(symbol, exchange_meta, catalog_candidates,
# overrides) -> AssetIdentity; resolve_identity stays as compat wrapper.
# Catalog input is the R00 candidate fixture via make_verified_candidate
# (reads the packaged verified set, never Catalog privates).
# ---------------------------------------------------------------------------


def test_r02a_plan_snippet_btc_verified_manual():
    """Plan R02a snippet: BTC single verified candidate -> VERIFIED/MANUAL/1."""
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    result = rmod.resolve_asset_context("BTCUSDT", {}, [make_verified_candidate("BTCUSDT")], {})
    assert result.contract_multiplier == 1
    assert result.multiplier_source == "MANUAL"
    assert result.mapping_confidence == "VERIFIED"


def test_r02a_btc_multiplier_is_decimal_and_manual():
    """Verified BTC m1: Decimal multiplier, MANUAL source, VERIFIED confidence."""
    from decimal import Decimal

    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    result = rmod.resolve_asset_context("BTCUSDT", {}, [make_verified_candidate("BTCUSDT")], {})
    assert result.mapping_confidence == "VERIFIED"
    assert result.mapping_source == "MANUAL"
    assert result.multiplier_source == "MANUAL"
    assert isinstance(result.contract_multiplier, Decimal)
    assert result.contract_multiplier == Decimal("1")
    assert result.binance_futures_symbol == "BTCUSDT"
    # mapping_confidence stays the single confidence field (no duplicate).
    assert not hasattr(result, "identity_confidence")
    assert "identity_confidence" not in vars(result)


def test_r02a_1000pepe_verified_manual_decimal():
    """1000PEPE via verified row/MANUAL override -> VERIFIED, Decimal 1000."""
    from decimal import Decimal

    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    cand = make_verified_candidate("1000PEPEUSDT")
    assert cand["contract_multiplier"] == 1000
    assert cand["multiplier_source"] == "MANUAL"
    result = rmod.resolve_asset_context("1000PEPEUSDT", {}, [cand], {})
    assert result.mapping_confidence == "VERIFIED"
    assert result.multiplier_source == "MANUAL"
    assert isinstance(result.contract_multiplier, Decimal)
    assert result.contract_multiplier == Decimal("1000")
    # Manual spot binding is preserved, never prefix-guessed.
    assert result.binance_spot_symbol in ("PEPEUSDT", "1000PEPEUSDT", cand.get("binance_spot_symbol"))


def test_r02a_1000_override_wins_over_ambiguous_candidates():
    """MANUAL override wins even when catalog candidates are ambiguous."""
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

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
    ambiguous = [
        {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe", "name": "Pepe"},
        {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe-kebab", "name": "Pepe Kebab"},
    ]
    result = rmod.resolve_asset_context("1000PEPEUSDT", {}, ambiguous, overrides)
    assert result.mapping_confidence == "VERIFIED"
    assert result.mapping_source == "MANUAL"
    assert result.contract_multiplier == 1000
    assert result.multiplier_source == "MANUAL"
    assert result.binance_spot_symbol == "PEPEUSDT"


def test_r02a_1000_prefix_without_provenance_never_guesses():
    """Bare 1000 prefix without MANUAL/EXCHANGE provenance never verifies."""
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    ambiguous = [
        {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe"},
        {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe-kebab"},
    ]
    result = rmod.resolve_asset_context("1000PEPEUSDT", {}, ambiguous, {})
    assert result.mapping_confidence == "UNRESOLVED"
    assert result.contract_multiplier is None
    assert result.multiplier_source is None
    assert result.binance_spot_symbol is None
    # A bare number without source in exchange_meta is also ignored.
    bare = rmod.resolve_asset_context(
        "1000PEPEUSDT", {"contract_multiplier": 1000}, ambiguous, {}
    )
    assert bare.contract_multiplier is None


def test_r02a_candidate_conflict_is_unresolved_with_unit_conflict():
    """Conflicting trusted candidate multipliers -> UNRESOLVED + conflict reason."""
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    assert rmod.IDENTITY_UNIT_CONFLICT == "IDENTITY_UNIT_CONFLICT"
    cands = [
        {
            "provider_symbol": "BTCUSDT",
            "coingecko_id": "bitcoin",
            "contract_multiplier": 1,
            "multiplier_source": "MANUAL",
            "catalog_source": "verified-assets",
            "mapping_source": "MANUAL",
        },
        {
            "provider_symbol": "BTCUSDT",
            "coingecko_id": "bitcoin-alt",
            "contract_multiplier": 1000,
            "multiplier_source": "MANUAL",
            "catalog_source": "verified-assets",
            "mapping_source": "MANUAL",
        },
    ]
    result = rmod.resolve_asset_context("BTCUSDT", {}, cands, {})
    assert result.mapping_confidence == "UNRESOLVED"
    # Never silently pick one side's unit.
    assert result.contract_multiplier is None
    assert result.multiplier_source is None
    assert result.coingecko_id is None
    assert rmod.has_unit_conflict("BTCUSDT", {}, cands, {}) is True
    assert rmod.asset_context_reasons("BTCUSDT", {}, cands, {}) == ("IDENTITY_UNIT_CONFLICT",)
    # Non-conflicting single verified row has no conflict reason.
    single = [make_verified_candidate("BTCUSDT")]
    assert rmod.has_unit_conflict("BTCUSDT", {}, single, {}) is False
    assert rmod.asset_context_reasons("BTCUSDT", {}, single, {}) == ()


def test_r02a_override_exchange_multiplier_conflict_is_unresolved():
    """MANUAL override vs disagreeing EXCHANGE metadata -> UNRESOLVED conflict."""
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    overrides = {
        "BTCUSDT": {
            "canonical_id": "bitcoin",
            "display_symbol": "BTC",
            "contract_multiplier": 1,
            "multiplier_source": "MANUAL",
            "coingecko_id": "bitcoin",
        }
    }
    exchange_meta = {"contract_multiplier": 1000, "multiplier_source": "EXCHANGE"}
    result = rmod.resolve_asset_context("BTCUSDT", exchange_meta, [], overrides)
    assert result.mapping_confidence == "UNRESOLVED"
    assert result.contract_multiplier is None
    assert rmod.has_unit_conflict("BTCUSDT", exchange_meta, [], overrides) is True
    # Agreeing values are not a conflict.
    agreeing_meta = {"contract_multiplier": 1, "multiplier_source": "EXCHANGE"}
    ok = rmod.resolve_asset_context("BTCUSDT", agreeing_meta, [], overrides)
    assert ok.mapping_confidence == "VERIFIED"
    assert ok.contract_multiplier == 1
    assert rmod.has_unit_conflict("BTCUSDT", agreeing_meta, [], overrides) is False


def test_r02a_resolve_identity_is_compat_wrapper():
    """resolve_identity keeps the Task-5 signature and delegates (same result)."""
    import inspect

    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    sig_old = inspect.signature(rmod.resolve_identity)
    assert list(sig_old.parameters) == [
        "futures_symbol",
        "exchange_meta",
        "provider_candidates",
        "overrides",
    ]
    sig_new = inspect.signature(rmod.resolve_asset_context)
    assert list(sig_new.parameters) == [
        "symbol",
        "exchange_meta",
        "catalog_candidates",
        "overrides",
    ]
    cands = [make_verified_candidate("BTCUSDT")]
    via_new = rmod.resolve_asset_context("BTCUSDT", {}, cands, {})
    via_old = rmod.resolve_identity("BTCUSDT", {}, cands, {})
    assert via_old == via_new
    assert via_old.mapping_confidence == "VERIFIED"


def test_r02a_mapping_confidence_single_field_and_hedge_adaptation():
    """mapping_confidence is unique; Hedge adaptation stays conflict-free."""
    from diveintocrypto_desktop.shortlab.hedge import models as hmod
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    # Same 1:1 vocabulary as the Hedge boundary (D04.1).
    assert set(hmod.ALLOWED_IDENTITY_CONFIDENCE) == {
        rmod.VERIFIED,
        rmod.HIGH,
        rmod.MEDIUM,
        rmod.LOW,
        rmod.UNRESOLVED,
    }
    result = rmod.resolve_asset_context("BTCUSDT", {}, [make_verified_candidate("BTCUSDT")], {})
    assert rmod.hedge_identity_confidence(result) == result.mapping_confidence == "VERIFIED"
    view = rmod.as_hedge_identity(result)
    assert view["mapping_confidence"] == view["identity_confidence"] == "VERIFIED"
    # Mapping and dict inputs adapt identically.
    assert rmod.hedge_identity_confidence({"mapping_confidence": "HIGH"}) == "HIGH"
    assert rmod.hedge_identity_confidence({"identity_confidence": "LOW"}) == "LOW"
    assert rmod.hedge_identity_confidence({}) == "UNRESOLVED"


def test_r02a_contract_multiplier_and_source_enter_result():
    """Verified catalog multiplier/source enter the result (D04.1, no first-pick)."""
    from decimal import Decimal

    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    # Single verified row contributes its MANUAL unit.
    cand = make_verified_candidate("ETHUSDT")
    result = rmod.resolve_asset_context("ETHUSDT", {}, [cand], {})
    assert result.mapping_confidence == "VERIFIED"
    assert result.contract_multiplier == Decimal(str(cand["contract_multiplier"]))
    assert result.multiplier_source == cand["multiplier_source"] == "MANUAL"
    # Ambiguous directory rows without provenance contribute no unit.
    ambiguous = [
        {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe"},
        {"provider_symbol": "PEPEUSDT", "coingecko_id": "pepe-kebab"},
    ]
    unresolved = rmod.resolve_asset_context("PEPEUSDT", {}, ambiguous, {})
    assert unresolved.mapping_confidence == "UNRESOLVED"
    assert unresolved.contract_multiplier is None


def test_r02a_eip55_and_solana_rules_preserved():
    """EIP-55 checksum and Solana case rules still govern contract binding."""
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    good = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
    ident = rmod.resolve_identity(
        "TESTUSDT",
        {"contract_address": good, "chain": "ethereum"},
        [
            {
                "provider_symbol": "TESTUSDT",
                "coingecko_id": "test",
                "contract_address": good.lower(),
                "chain": "ethereum",
            },
        ],
        {},
    )
    assert ident.mapping_confidence == "VERIFIED"
    assert ident.mapping_source == "CONTRACT"
    sol_good = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    ident_sol = rmod.resolve_identity(
        "SOLUSDT",
        {"contract_address": sol_good.lower(), "chain": "solana"},
        [
            {
                "provider_symbol": "SOLUSDT",
                "coingecko_id": "solana",
                "contract_address": sol_good,
                "chain": "solana",
            },
        ],
        {},
    )
    assert ident_sol.mapping_source != "CONTRACT"


def test_r02a_decimal_amount_path_never_float():
    """Decimal multiplier + Decimal canonical price (no float-derived amounts)."""
    from decimal import Decimal

    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    result = rmod.resolve_asset_context("BTCUSDT", {}, [make_verified_candidate("BTCUSDT")], {})
    assert isinstance(result.contract_multiplier, Decimal)
    dec = rmod.canonical_price_decimal("0.006", result.contract_multiplier)
    assert isinstance(dec, Decimal)
    # 0.006 / 1 == 0.006 exactly in Decimal (float would be 0.006000...4).
    assert dec == Decimal("0.006")
    dec1000 = rmod.canonical_price_decimal(Decimal("0.006"), Decimal("1000"))
    assert dec1000 == Decimal("0.000006")
    assert rmod.canonical_price_decimal("0.006", None) is None
    assert rmod.canonical_price_decimal("0.006", 0) is None
    # Float-compat entry still returns a float close to the Decimal value.
    assert rmod.canonical_price(0.006, 1000) == 0.006 / 1000


def test_r02a_overrides_accept_three_profiles_reject_others():
    """Override validation accepts MEME/GENERAL/LOW_FLOAT_VC, rejects the rest."""
    from diveintocrypto_desktop.shortlab.identity import overrides as omod

    assert omod.ALLOWED_PROFILES == frozenset({"MEME", "GENERAL", "LOW_FLOAT_VC"})
    for profile in ("MEME", "GENERAL", "LOW_FLOAT_VC", "meme", " general "):
        omod.validate_override_entry("BTCUSDT", {"canonical_id": "x", "profile": profile})
    for bad in ("LITE", "MEME_LITE", "GENERAL_FULL", "FULL", "BULL", "", 123, None):
        try:
            omod.validate_override_entry("BTCUSDT", {"canonical_id": "x", "profile": bad})
        except ValueError:
            pass
        else:
            raise AssertionError(f"profile {bad!r} should be rejected")
    # profile stays optional: entries without it still validate.
    omod.validate_override_entry("BTCUSDT", {"canonical_id": "x"})


def test_r02a_manual_profile_priority():
    """Manual profile outranks verified categories (D04.2)."""
    from diveintocrypto_desktop.shortlab.identity import overrides as omod

    identity = {"categories": ["meme"]}
    profile, reason, is_manual = omod.effective_profile({"profile": "GENERAL"}, identity, None)
    assert (profile, reason, is_manual) == ("GENERAL", "MANUAL_OVERRIDE", True)
    profile2, _, is_manual2 = omod.effective_profile({"profile": "LOW_FLOAT_VC"}, identity, None)
    assert profile2 == "LOW_FLOAT_VC" and is_manual2 is True
    # Case/whitespace-insensitive manual input normalizes to the base.
    profile3, _, _ = omod.effective_profile({"profile": " meme "}, identity, None)
    assert profile3 == "MEME"


def test_r02a_meme_missing_fundamentals_stays_meme():
    """Known Meme never degrades to GENERAL on transient missing data (D04.2)."""
    from diveintocrypto_desktop.shortlab.identity import overrides as omod

    meme_identity = {"categories": ["meme"]}
    # Fundamentals failed / missing entirely: class stays MEME (only DQ changes).
    for fundamentals in (None, {}, {"market_cap_usd": None, "fdv_usd": None}):
        profile, reason, is_manual = omod.effective_profile(None, meme_identity, fundamentals)
        assert profile == "MEME", fundamentals
        assert reason == "MEME_CATEGORY"
        assert is_manual is False
    # Non-meme without fundamentals falls back to GENERAL (never guessed).
    profile_g, _, _ = omod.effective_profile(None, {"categories": []}, None)
    assert profile_g == "GENERAL"


def test_r02a_builtin_yaml_profiles_are_meme():
    """Packaged asset_overrides.yaml carries MANUAL units + MEME profiles (R02a)."""
    from diveintocrypto_desktop.shortlab.identity import overrides as omod

    doc = omod.load_overrides()
    assert isinstance(doc.get("version"), int) and doc["version"] >= 2
    for symbol in ("1000PEPEUSDT", "1000SHIBUSDT"):
        entry = omod.get_override(symbol, doc)
        assert entry is not None, symbol
        assert entry["contract_multiplier"] == 1000
        assert entry["multiplier_source"] == "MANUAL"
        assert entry["profile"] == "MEME"
        profile, reason, is_manual = omod.effective_profile(entry, entry, None)
        assert (profile, reason, is_manual) == ("MEME", "MANUAL_OVERRIDE", True)


# ---------------------------------------------------------------------------
# R02b-1 single-symbol overlay
# ---------------------------------------------------------------------------


def test_r02b_single_symbol_overlay_wins_as_single_manual(tmp_path):
    """One-symbol overlay file wins per symbol as a single MANUAL mapping."""
    overlay = _write_single_symbol_overlay(tmp_path / "single.yaml")
    cat = _catalog(overlay_path=overlay)
    got = cat.candidates("BTCUSDT", FIXTURE_NOW)
    assert len(got) == 1
    cand = got[0]
    assert cand["catalog_source"] == "override"
    assert cand["mapping_source"] == "MANUAL"
    assert cand["contract_multiplier"] == 1
    assert cand["multiplier_source"] == "MANUAL"
    assert cand["coingecko_id"] == "manual-bitcoin"
    assert cand["catalog_stale"] is False
    assert isinstance(cand["catalog_known_at_ms"], int)


def test_r02b_single_symbol_overlay_is_per_symbol_not_table(tmp_path):
    """A single-symbol overlay only shadows its own symbol (ETH stays verified)."""
    overlay = _write_single_symbol_overlay(tmp_path / "single.yaml")
    cat = _catalog(overlay_path=overlay)
    eth = cat.candidates("ETHUSDT", FIXTURE_NOW)
    assert len(eth) == 1
    assert eth[0]["catalog_source"] == "verified-assets"
    assert eth[0]["coingecko_id"] == "ethereum"


def test_r02b_single_symbol_overlay_lookup_is_case_insensitive(tmp_path):
    overlay = _write_single_symbol_overlay(tmp_path / "single.yaml")
    cat = _catalog(overlay_path=overlay)
    got = cat.candidates("btcusdt", FIXTURE_NOW)
    assert len(got) == 1
    assert got[0]["catalog_source"] == "override"


@pytest.mark.asyncio
async def test_r02b_single_symbol_overlay_beats_directory(tmp_path):
    """Overlay wins even when the network directory holds the same symbol."""
    overlay = _write_single_symbol_overlay(tmp_path / "single.yaml", "SOLUSDT")
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock, overlay_path=overlay, verified={"version": 3, "assets": {}})
    await cat.refresh(_ctx())
    got = cat.candidates("SOLUSDT", FIXTURE_NOW)
    assert len(got) == 1
    assert got[0]["catalog_source"] == "override"
    assert got[0]["mapping_source"] == "MANUAL"


# ---------------------------------------------------------------------------
# R02b-2 verified-directory metadata validation (fail loud, never guessed)
# ---------------------------------------------------------------------------


def test_r02b_verified_entry_missing_canonical_id_fails_loud():
    mod = _load(CATALOG)
    bad = {
        "version": 3,
        "assets": {
            "BTCUSDT": {
                "display_symbol": "BTC",
                "contract_multiplier": 1,
                "multiplier_source": "MANUAL",
            }
        },
    }
    with pytest.raises(ValueError):
        mod.IdentityCatalog(
            verified=bad,
            overrides_doc=_overrides_doc(),
            clock=_clock(),
            http_get=FakeDirHttp(payload=[]),
            rate_limiter=_NoopLimiter(),
        )


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"multiplier_source": "MANUAL"},
        {"contract_multiplier": 0, "multiplier_source": "MANUAL"},
        {"contract_multiplier": 1},
    ],
)
def test_r02b_verified_entry_missing_multiplier_provenance_fails_loud(entry):
    """Verified rows must carry explicit multiplier provenance (D04.1).

    A bare symbol (or a non-positive / unsourced multiplier) must fail loud at
    load instead of serving a candidate the resolver could mistake for a
    verified 1x binding.
    """
    mod = _load(CATALOG)
    doc = {
        "version": 3,
        "assets": {"BTCUSDT": {"canonical_id": "bitcoin", **entry}},
    }
    with pytest.raises(ValueError):
        mod.IdentityCatalog(
            verified=doc,
            overrides_doc=_overrides_doc(),
            clock=_clock(),
            http_get=FakeDirHttp(payload=[]),
            rate_limiter=_NoopLimiter(),
        )


def test_r02b_verified_address_without_chain_fails_loud():
    mod = _load(CATALOG)
    doc = {
        "version": 3,
        "assets": {
            "PEPEUSDT": {
                "canonical_id": "pepe",
                "contract_multiplier": 1,
                "multiplier_source": "MANUAL",
                "contract_address": "0x6982508145454ce325ddbe47a25d4ec3d2311933",
            }
        },
    }
    with pytest.raises(ValueError):
        mod.IdentityCatalog(
            verified=doc,
            overrides_doc=_overrides_doc(),
            clock=_clock(),
            http_get=FakeDirHttp(payload=[]),
            rate_limiter=_NoopLimiter(),
        )


def test_r02b_verified_chain_without_address_is_allowed():
    """Native/settlement chain rows without an address stay loadable (ETH-style)."""
    mod = _load(CATALOG)
    cat = mod.IdentityCatalog(
        verified=_verified_doc(),
        overrides_doc=_overrides_doc(),
        clock=_clock(),
        http_get=FakeDirHttp(payload=[]),
        rate_limiter=_NoopLimiter(),
    )
    got = cat.candidates("ETHUSDT", FIXTURE_NOW)
    assert len(got) == 1
    assert got[0]["chain"] == "ethereum"
    assert got[0].get("contract_address") is None


# ---------------------------------------------------------------------------
# R02b-3 real directory file reading
# ---------------------------------------------------------------------------


def test_r02b_real_verified_file_loads_1x_and_1000_sets():
    """Packaged verified file reads for real: 1x set + explicit 1000 set."""
    mod = _load(CATALOG)
    doc = mod.load_verified_assets()
    assert isinstance(doc.get("version"), int)
    assets = doc["assets"]
    for symbol in ("BTCUSDT", "ETHUSDT", "1000PEPEUSDT", "1000SHIBUSDT"):
        assert symbol in assets, symbol
        entry = assets[symbol]
        assert entry["multiplier_source"] == "MANUAL"
        assert entry["contract_multiplier"] > 0


def test_r02b_real_file_candidates_match_frozen_helper():
    """Default catalog (real files, no network) serves frozen verified rows."""
    mod = _load(CATALOG)
    cat = mod.IdentityCatalog(
        clock=_clock(),
        http_get=FakeDirHttp(payload=[]),
        rate_limiter=_NoopLimiter(),
    )
    expected = make_verified_candidate("BTCUSDT")
    got = cat.candidates("BTCUSDT", FIXTURE_NOW)
    assert len(got) == 1
    for key in (
        "canonical_id",
        "coingecko_id",
        "binance_spot_symbol",
        "contract_multiplier",
        "multiplier_source",
        "catalog_source",
        "mapping_source",
    ):
        assert got[0][key] == expected[key], key
    assert got[0]["catalog_stale"] is False


def test_r02b_real_file_1000_symbol_carries_explicit_multiplier():
    # 1000PEPEUSDT overlaps verified-assets and the built-in manual override
    # (field-for-field identical per F04). Priority is manual override >
    # verified, so either MANUAL layer may serve -- but provenance must stay
    # explicit MANUAL/1000 and never a guessed prefix multiplier.
    mod = _load(CATALOG)
    cat = mod.IdentityCatalog(
        clock=_clock(),
        http_get=FakeDirHttp(payload=[]),
        rate_limiter=_NoopLimiter(),
    )
    got = cat.candidates("1000PEPEUSDT", FIXTURE_NOW)
    assert len(got) == 1
    assert got[0]["catalog_source"] in ("override", "verified-assets")
    assert got[0]["mapping_source"] == "MANUAL"
    assert got[0]["contract_multiplier"] == 1000
    assert got[0]["multiplier_source"] == "MANUAL"


def test_r02b_plan_snippet_btc_single_manual():
    """Plan R02b snippet shape: single MANUAL candidate for BTCUSDT."""
    cat = _catalog(http=FakeDirHttp(payload=[]))
    candidates = cat.candidates("BTCUSDT", FIXTURE_NOW)
    assert len(candidates) == 1
    assert candidates[0]["multiplier_source"] == "MANUAL"
    assert candidates[0]["contract_multiplier"] == 1


# ---------------------------------------------------------------------------
# R02b-4 directory failure behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_r02b_keyless_zero_sends_verified_serves():
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, api_key=None)
    res = await cat.refresh(_ctx())
    assert res.status == "UNAVAILABLE"
    assert http.calls == []
    got = cat.candidates("BTCUSDT", FIXTURE_NOW)
    assert len(got) == 1
    assert got[0]["catalog_source"] == "verified-assets"


@pytest.mark.asyncio
async def test_r02b_refresh_failure_keeps_old_receipt():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock, verified={"version": 3, "assets": {}})
    ok = await cat.refresh(_ctx())
    assert ok.status == "OK"
    clock.advance(86500000)
    from diveintocrypto_desktop.data.http import TransientUpstreamError

    http.error = TransientUpstreamError(500, None)
    res = await cat.refresh(_ctx())
    assert res.status in ("UNAVAILABLE", "ERROR")
    got = cat.candidates("SOLUSDT", clock())
    assert [c["coingecko_id"] for c in got] == ["solana"]
    assert got[0]["catalog_stale"] is True


@pytest.mark.asyncio
async def test_r02b_429_backs_off_without_retry_storm():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock)
    await cat.refresh(_ctx())
    clock.advance(86500000)
    from diveintocrypto_desktop.data.http import TransientUpstreamError

    http.error = TransientUpstreamError(429, 7.0)
    res = await cat.refresh(_ctx())
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == "COINGECKO_RATE_LIMITED"
    sends = len(http.calls)
    again = await cat.refresh(_ctx())
    assert again.status == "UNAVAILABLE"
    assert len(http.calls) == sends


@pytest.mark.asyncio
async def test_r02b_bad_schema_keeps_old_cache():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock)
    await cat.refresh(_ctx())
    clock.advance(86500000)
    http.error = None
    http.payload = {"id": "not-a-list"}
    res = await cat.refresh(_ctx())
    assert res.status == "ERROR"
    assert res.reason_code == "COINGECKO_BAD_RESPONSE"
    assert cat.entry_count == len(DIR_LIST)


@pytest.mark.parametrize(
    "filename, body",
    [
        ("bad-syntax.yaml", "version: [unclosed\n  overrides: {"),
        ("bad-keys.yaml", 'version: 1\noverrides:\n  "BTCUSDT":\n    bogus_key: 1\n'),
    ],
)
def test_r02b_overlay_errors_fail_loud(tmp_path, filename, body):
    """Overlay problems fail loud: the identity capability is unavailable,
    never an empty directory."""
    mod = _load(CATALOG)
    bad = tmp_path / filename
    bad.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        mod.IdentityCatalog(
            verified=_verified_doc(),
            overrides_doc=_overrides_doc(),
            overlay_path=str(bad),
            clock=_clock(),
            http_get=FakeDirHttp(payload=[]),
            rate_limiter=_NoopLimiter(),
        )


def test_r02b_overlay_missing_file_fails_loud(tmp_path):
    mod = _load(CATALOG)
    with pytest.raises(ValueError):
        mod.IdentityCatalog(
            verified=_verified_doc(),
            overrides_doc=_overrides_doc(),
            overlay_path=str(tmp_path / "does-not-exist.yaml"),
            clock=_clock(),
            http_get=FakeDirHttp(payload=[]),
            rate_limiter=_NoopLimiter(),
        )


@pytest.mark.asyncio
async def test_r02b_expired_directory_drops_but_verified_serves():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock)
    await cat.refresh(_ctx())
    clock.advance(345610000)
    assert cat.candidates("SOLUSDT", clock()) == ()
    btc = cat.candidates("BTCUSDT", clock())
    assert len(btc) == 1
    assert btc[0]["catalog_source"] == "verified-assets"


@pytest.mark.asyncio
async def test_r02b_ambiguous_candidates_all_retained_no_first_pick():
    """Ambiguous families stay multi-candidate; resolver stays UNRESOLVED.

    D04.1 forbids binding the directory first entry directly as a Hedge
    Identity: the full set must reach the resolver, which leaves conflicts
    UNRESOLVED. Directory candidates also carry no invented multiplier.
    """
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    cat = _catalog(http=FakeDirHttp(payload=list(DIR_LIST)))
    await cat.refresh(_ctx())
    got = cat.candidates("PEPEUSDT", FIXTURE_NOW)
    assert {c["coingecko_id"] for c in got} == {"pepe", "pepe-kebab"}
    for cand in got:
        assert "contract_multiplier" not in cand
        assert cand["catalog_source"] == "coingecko-directory"
        assert isinstance(cand["catalog_known_at_ms"], int)
    ident = rmod.resolve_identity("PEPEUSDT", {}, list(got), {})
    assert ident.mapping_confidence == "UNRESOLVED"
    assert ident.coingecko_id is None


# ---------------------------------------------------------------------------
# R02b-5 Service default binding + Hedge adaptation seam
# ---------------------------------------------------------------------------


def test_r02b_catalog_candidates_signature_is_r10b_binding_point():
    """R10b binds Service defaults to this exact contract (no Service change here)."""
    import inspect

    mod = _load(CATALOG)
    sig = inspect.signature(mod.IdentityCatalog.candidates)
    params = list(sig.parameters)
    assert params[0] == "self"
    assert params[1] == "symbol"
    assert params[2] in ("cutoff_ms", "as_of_ms")
    cat = _catalog(http=FakeDirHttp(payload=[]))
    out = cat.candidates("BTCUSDT", FIXTURE_NOW)
    assert isinstance(out, tuple)
    for key in ("provider_symbol", "catalog_source", "catalog_known_at_ms", "catalog_stale"):
        assert key in out[0], key


def test_r02b_service_default_candidates_fn_is_empty_without_catalog():
    """Current Service default is an empty candidates fn; the injected Catalog
    slot exists for R10b to bind (this test only observes, never modifies)."""
    import inspect

    from diveintocrypto_desktop.shortlab import service as svc

    params = inspect.signature(svc.ShortLabService.__init__).parameters
    assert "identity_candidates_fn" in params
    assert "identity_catalog" in params
    src = inspect.getsource(svc.ShortLabService.__init__)
    assert "lambda symbol: []" in src


def test_r02b_hedge_confidence_adaptation_vocab_matches():
    """Hedge ``identity_confidence`` accepts the resolver ``mapping_confidence``
    vocabulary 1:1 (D04.1); persistence must keep both conflict-free."""
    from diveintocrypto_desktop.shortlab.hedge import models as hmod
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    assert set(hmod.ALLOWED_IDENTITY_CONFIDENCE) == {
        rmod.VERIFIED,
        rmod.HIGH,
        rmod.MEDIUM,
        rmod.LOW,
        rmod.UNRESOLVED,
    }
