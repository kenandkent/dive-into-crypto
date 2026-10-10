"""F04: production identity directory + safe address mapping (plan F04).

Contract (ShortLab_Integrated_Implementation_Plan_CN.md F04, AC02/AC11/AC12;
design A6.3):

- Three layers: ``identity/verified_assets.yaml`` (human-verified 1x + 1000
  sets) -> CoinGecko ``/coins/list?include_platform=false`` directory cached
  24h / grace 72h / 32MiB cap -> ``SHORTLAB_IDENTITY_OVERRIDES_PATH`` overlay
  (wins, same schema + chain validation, syntax errors fail loud).
- DEMO/PRO host + key param strictly paired; the same key is never sent to
  the other host. Keyless refresh performs zero sends (UNCONFIGURED).
- At most 50 bound assets get platform/address enrichment per refresh.
- ``normalize_chain_address``: EVM EIP-55 via Ethereum Keccak (never NIST
  SHA3); Solana base58 keeps case; unknown chains are never lowered.
- Conflicts never auto-match; 1000-prefix never guesses a multiplier.
- Directory failure keeps the old ``known_at``; overlay errors fail loud.
- FULL skeletons (``.example`` / no-fetcher) are never READY-capable.

All network access is faked; no live CoinGecko requests.
"""

from __future__ import annotations

import pytest

CATALOG = "diveintocrypto_desktop.shortlab.identity.catalog"
RESOLVER = "diveintocrypto_desktop.shortlab.identity.resolver"
OVERRIDES = "diveintocrypto_desktop.shortlab.identity.overrides"
CG = "diveintocrypto_desktop.shortlab.providers.coingecko"
BASE = "diveintocrypto_desktop.shortlab.providers.base"

# EIP-55 spec vectors (https://eips.ethereum.org/EIPS/eip-55).
EIP55_GOOD = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
EIP55_GOOD_2 = "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359"
# Same address with one nibble case flipped -> wrong checksum.
EIP55_BAD = "0x5aaeb6053F3E94C9b9A09f33669435E7Ef1BeAed"

# Solana USDC mint (32 bytes base58) + a first-letter-lowered twin that still
# decodes to 32 bytes with different content.
SOL_GOOD = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_TWIN = "Epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v"

NOW_MS = 1_750_000_000_000


def _load(modname):
    import importlib

    return importlib.import_module(modname)


def _clock(start_ms: int = NOW_MS):
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


@pytest.fixture(autouse=True)
def _scrub_overlay_env(monkeypatch):
    monkeypatch.delenv("SHORTLAB_IDENTITY_OVERRIDES_PATH", raising=False)


# ---------------------------------------------------------------------------
# 1. normalize_chain_address: EIP-55 / Solana / unknown chain
# ---------------------------------------------------------------------------


def test_eip55_correct_checksum_verifies_with_lower_key():
    m = _load(RESOLVER)
    norm = m.normalize_chain_address("ethereum", EIP55_GOOD)
    assert norm.validation_status == m.CHECKSUM_VERIFIED
    assert norm.canonical_key == EIP55_GOOD.lower()
    # Display keeps the checksum; the comparison key is lowercase.
    assert norm.display == EIP55_GOOD


def test_eip55_all_lower_has_no_checksum_but_same_key():
    m = _load(RESOLVER)
    norm = m.normalize_chain_address("ethereum", EIP55_GOOD.lower())
    assert norm.validation_status == m.NO_CHECKSUM
    assert norm.canonical_key == EIP55_GOOD.lower()


def test_eip55_wrong_checksum_does_not_merge_with_correct():
    m = _load(RESOLVER)
    good = m.normalize_chain_address("ethereum", EIP55_GOOD)
    bad = m.normalize_chain_address("ethereum", EIP55_BAD)
    assert bad.validation_status == m.BAD_CHECKSUM
    assert bad.canonical_key != good.canonical_key


def test_eip55_uses_ethereum_keccak_not_nist_sha3():
    """NIST SHA3-256 of the same input differs; only Ethereum Keccak verifies."""
    import hashlib

    m = _load(RESOLVER)
    body = EIP55_GOOD[2:].lower().encode("ascii")
    nist = hashlib.sha3_256(body).hexdigest()
    eth = m._keccak_256(body).hex()
    assert nist != eth  # guards against swapping in hashlib.sha3_256
    assert m.normalize_chain_address("ethereum", EIP55_GOOD).validation_status == (
        m.CHECKSUM_VERIFIED
    )


def test_eip55_malformed_is_invalid():
    m = _load(RESOLVER)
    for bad in ("0xZZZ", "0x1234", "not-an-address", "", "0x" + "ab" * 19):
        norm = m.normalize_chain_address("ethereum", bad)
        assert norm.validation_status == m.INVALID_ADDRESS, bad


def test_solana_case_preserved_and_twins_do_not_merge():
    m = _load(RESOLVER)
    a = m.normalize_chain_address("solana", SOL_GOOD)
    b = m.normalize_chain_address("solana", SOL_TWIN)
    assert a.validation_status == m.CHAIN_ADDRESS_OK
    assert b.validation_status == m.CHAIN_ADDRESS_OK
    assert a.canonical_key == SOL_GOOD  # case preserved, never lowered
    assert b.canonical_key == SOL_TWIN
    assert a.canonical_key != b.canonical_key


def test_solana_bad_length_is_invalid():
    m = _load(RESOLVER)
    norm = m.normalize_chain_address("solana", "1111")
    assert norm.validation_status == m.INVALID_ADDRESS


def test_unknown_chain_is_never_lowered():
    m = _load(RESOLVER)
    norm = m.normalize_chain_address("mychain-xyz", "AbC123xYz")
    assert norm.validation_status == m.ADDRESS_CHAIN_UNSUPPORTED
    assert norm.canonical_key == "AbC123xYz"


def test_resolver_contract_match_uses_normalized_keys():
    """EVM mixed-case (correct checksum) still matches a lowercase candidate;
    a Solana address mangled to lowercase must NOT contract-match."""
    m = _load(RESOLVER)
    # EVM: same address, different case -> CONTRACT VERIFIED.
    ident = m.resolve_identity(
        futures_symbol="TESTUSDT",
        exchange_meta={"contract_address": EIP55_GOOD, "chain": "ethereum"},
        provider_candidates=[
            {
                "provider_symbol": "TESTUSDT",
                "coingecko_id": "test",
                "contract_address": EIP55_GOOD.lower(),
                "chain": "ethereum",
            },
        ],
        overrides={},
    )
    assert ident.mapping_confidence == "VERIFIED"
    assert ident.mapping_source == "CONTRACT"
    # Solana: candidate keeps case; a lowercased meta address disagrees.
    ident_sol = m.resolve_identity(
        futures_symbol="SOLUSDT",
        exchange_meta={"contract_address": SOL_GOOD.lower(), "chain": "solana"},
        provider_candidates=[
            {
                "provider_symbol": "SOLUSDT",
                "coingecko_id": "solana",
                "contract_address": SOL_GOOD,
                "chain": "solana",
            },
        ],
        overrides={},
    )
    assert ident_sol.mapping_source != "CONTRACT"


# ---------------------------------------------------------------------------
# 2. verified_assets.yaml: 1x + 1000 sets, consistent with overrides
# ---------------------------------------------------------------------------


def test_load_verified_assets_has_1x_and_1000_sets():
    mod = _load(CATALOG)
    doc = mod.load_verified_assets()
    assert isinstance(doc.get("version"), int)
    assets = doc["assets"]
    assert "BTCUSDT" in assets and "ETHUSDT" in assets  # ordinary 1x
    assert "1000PEPEUSDT" in assets and "1000SHIBUSDT" in assets  # 1000 set
    for symbol, entry in assets.items():
        assert entry.get("multiplier_source") in ("MANUAL", "EXCHANGE"), symbol
        assert entry.get("contract_multiplier", 1) > 0, symbol


def test_verified_assets_agree_with_manual_overrides_on_overlap():
    cat = _load(CATALOG)
    om = _load(OVERRIDES)
    assets = cat.load_verified_assets()["assets"]
    table = om.load_overrides()["overrides"]
    overlap = set(assets) & set(table)
    assert overlap, "expected overlap between verified assets and overrides"
    for symbol in overlap:
        for key in (
            "canonical_id",
            "coingecko_id",
            "binance_spot_symbol",
            "contract_multiplier",
            "chain",
            "contract_address",
        ):
            assert assets[symbol].get(key) == table[symbol].get(key), (symbol, key)


# ---------------------------------------------------------------------------
# 3. overlay: priority, version record, loud failure
# ---------------------------------------------------------------------------


def test_overlay_wins_and_records_source_priority(tmp_path):
    om = _load(OVERRIDES)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "version: 2\noverrides:\n"
        '  "BTCUSDT":\n    canonical_id: "manual-btc"\n'
        '    display_symbol: "BTC"\n    coingecko_id: "manual-bitcoin"\n',
        encoding="utf-8",
    )
    effective = om.load_effective_overrides(
        overlay, {"version": 1, "overrides": {"BTCUSDT": {"canonical_id": "x"}}}
    )
    assert effective["overrides"]["BTCUSDT"]["canonical_id"] == "manual-btc"
    src = effective["sources"]["BTCUSDT"]
    assert src["source"] == "overlay" and src["overridden"] is True
    assert effective["builtin_version"] == 1 and effective["overlay_version"] == 2


def test_overlay_respects_env_path(tmp_path, monkeypatch):
    om = _load(OVERRIDES)
    overlay = tmp_path / "env-overlay.yaml"
    overlay.write_text(
        "version: 9\noverrides:\n"
        '  "ETHUSDT":\n    canonical_id: "ethereum"\n    coingecko_id: "ethereum"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SHORTLAB_IDENTITY_OVERRIDES_PATH", str(overlay))
    effective = om.load_effective_overrides(None, {"version": 1, "overrides": {}})
    assert effective["overrides"]["ETHUSDT"]["canonical_id"] == "ethereum"
    assert effective["overlay_path"] == str(overlay)


def test_overlay_syntax_error_fails_loud_not_empty(tmp_path):
    om = _load(OVERRIDES)
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: [unclosed\n  overrides: {", encoding="utf-8")
    with pytest.raises(ValueError):
        om.load_effective_overrides(bad, {"version": 1, "overrides": {}})


def test_overlay_schema_and_checksum_errors_fail_loud(tmp_path):
    om = _load(OVERRIDES)
    bad_keys = tmp_path / "bad-keys.yaml"
    bad_keys.write_text(
        "version: 1\noverrides:\n  \"BTCUSDT\":\n    bogus_key: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        om.load_effective_overrides(bad_keys, {"version": 1, "overrides": {}})
    bad_addr = tmp_path / "bad-addr.yaml"
    bad_addr.write_text(
        "version: 1\noverrides:\n  \"BTCUSDT\":\n"
        "    chain: \"ethereum\"\n    contract_address: \"0xZZZ\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        om.load_effective_overrides(bad_addr, {"version": 1, "overrides": {}})
    bad_checksum = tmp_path / "bad-checksum.yaml"
    bad_checksum.write_text(
        "version: 1\noverrides:\n  \"BTCUSDT\":\n"
        f"    chain: \"ethereum\"\n    contract_address: \"{EIP55_BAD}\"\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        om.load_effective_overrides(bad_checksum, {"version": 1, "overrides": {}})


def test_overlay_missing_file_fails_loud(tmp_path):
    om = _load(OVERRIDES)
    with pytest.raises(ValueError):
        om.load_effective_overrides(
            tmp_path / "does-not-exist.yaml", {"version": 1, "overrides": {}}
        )


# ---------------------------------------------------------------------------
# 4. candidates(): priority, conflicts, 1000-prefix, STALE
# ---------------------------------------------------------------------------


def test_override_candidate_dominates_directory_conflicts():
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(
        http=http,
        overrides_doc={
            "version": 1,
            "overrides": {
                "PEPEUSDT": {
                    "canonical_id": "pepe",
                    "display_symbol": "PEPE",
                    "coingecko_id": "pepe",
                    "binance_spot_symbol": "PEPEUSDT",
                }
            },
        },
    )
    got = cat.candidates("PEPEUSDT", NOW_MS)
    assert len(got) == 1
    assert got[0]["coingecko_id"] == "pepe"
    assert got[0]["catalog_source"] == "override"


def test_verified_candidate_returned_single():
    cat = _catalog(http=FakeDirHttp(payload=[]))
    got = cat.candidates("BTCUSDT", NOW_MS)
    assert len(got) == 1
    assert got[0]["coingecko_id"] == "bitcoin"
    assert got[0]["catalog_source"] == "verified-assets"


@pytest.mark.asyncio
async def test_homonym_conflict_returns_all_candidates_for_unresolved():
    """pepe/pepe-kebab share a normalized symbol: every candidate is
    returned so the resolver stays UNRESOLVED (never positional-picked)."""
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http)
    res = await cat.refresh(_ctx())
    assert res.status == "OK"
    got = cat.candidates("PEPEUSDT", NOW_MS)
    assert {c["coingecko_id"] for c in got} == {"pepe", "pepe-kebab"}
    ident = rmod.resolve_identity("PEPEUSDT", {}, list(got), {})
    assert ident.mapping_confidence == "UNRESOLVED"
    assert ident.coingecko_id is None


@pytest.mark.asyncio
async def test_1000_prefix_never_guesses_multiplier():
    from diveintocrypto_desktop.shortlab.identity import resolver as rmod

    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http)
    await cat.refresh(_ctx())
    got = cat.candidates("1000PEPEUSDT", NOW_MS)
    # Only the conflicting directory family matches; nothing verified.
    assert {c["coingecko_id"] for c in got} == {"pepe", "pepe-kebab"}
    ident = rmod.resolve_identity("1000PEPEUSDT", {}, list(got), {})
    assert ident.mapping_confidence == "UNRESOLVED"
    assert ident.contract_multiplier is None
    assert ident.binance_spot_symbol is None


@pytest.mark.asyncio
async def test_unique_directory_symbol_is_single_candidate():
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, verified={"version": 3, "assets": {}})
    await cat.refresh(_ctx())
    got = cat.candidates("BTCUSDT", NOW_MS)
    assert [c["coingecko_id"] for c in got] == ["bitcoin"]
    assert got[0]["catalog_source"] == "coingecko-directory"


@pytest.mark.asyncio
async def test_stale_directory_within_grace_is_marked_not_dropped():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock, verified={"version": 3, "assets": {}})
    await cat.refresh(_ctx())
    clock.advance((86400 + 3600) * 1000)  # past TTL, inside 72h grace
    got = cat.candidates("SOLUSDT", NOW_MS + (86400 + 3600) * 1000)
    assert len(got) == 1 and got[0]["catalog_stale"] is True


@pytest.mark.asyncio
async def test_expired_directory_beyond_grace_drops_layer():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock, verified={"version": 3, "assets": {}})
    await cat.refresh(_ctx())
    clock.advance((86400 + 259200 + 10) * 1000)
    got = cat.candidates("SOLUSDT", NOW_MS + (86400 + 259200 + 10) * 1000)
    assert got == ()


@pytest.mark.asyncio
async def test_directory_known_after_cutoff_is_excluded():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock, verified={"version": 3, "assets": {}})
    await cat.refresh(_ctx())  # known_at == NOW_MS
    assert cat.candidates("SOLUSDT", NOW_MS - 1) == ()


# ---------------------------------------------------------------------------
# 5. refresh(): routes, TTL, failure keeps known_at, backoff, caps, redaction
# ---------------------------------------------------------------------------


def _ctx(budget=None, **kwargs):
    from diveintocrypto_desktop.shortlab.request_budget import make_request_context

    return make_request_context(budget, **kwargs)


@pytest.mark.asyncio
async def test_refresh_demo_route_and_params():
    from diveintocrypto_desktop.shortlab.request_budget import RequestBudget

    http = FakeDirHttp(payload=list(DIR_LIST))
    budget = RequestBudget()
    cat = _catalog(http=http)
    ctx = _ctx(budget=budget, job_type="backfill", trace_id="trace-1")
    res = await cat.refresh(ctx)
    assert res.status == "OK"
    assert res.data["entries"] == len(DIR_LIST)
    assert res.data["known_at_ms"] == NOW_MS
    assert len(res.data["checksum"]) == 64
    assert len(http.calls) == 1
    url, params, got_ctx = http.calls[0]
    assert url.startswith("https://api.coingecko.com/api/v3/coins/list")
    assert params["include_platform"] == "false"
    assert params["x_cg_demo_api_key"] == "DEMO-KEY-1"
    assert "x_cg_pro_api_key" not in params
    # Traceability and the shared host-send budget travel with the CoinGecko
    # host/family context; the monthly ledger is charged by the same sender.
    assert got_ctx.trace_id == "trace-1"
    assert got_ctx.host == "api.coingecko.com"
    assert got_ctx.endpoint_family == "cgDirectory"
    assert got_ctx.budget is budget
    assert budget.sent_attempts == 1


@pytest.mark.asyncio
async def test_refresh_pro_route_never_touches_demo_host():
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, api_plan="pro", api_key="PRO-KEY-9")
    res = await cat.refresh(_ctx(trace_id="t"))
    assert res.status == "OK"
    (url, params, _), = http.calls
    assert url.startswith("https://pro-api.coingecko.com/api/v3/coins/list")
    assert params["x_cg_pro_api_key"] == "PRO-KEY-9"
    assert "demo" not in url and "x_cg_demo_api_key" not in params
    assert http.hosts() == ["pro-api.coingecko.com"]


@pytest.mark.asyncio
async def test_refresh_unknown_plan_raises_without_send():
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, api_plan="ultra", api_key="K")
    with pytest.raises(ValueError):
        await cat.refresh(_ctx())
    assert http.calls == []


@pytest.mark.asyncio
async def test_refresh_without_key_sends_nothing_and_marks_unconfigured():
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, api_key=None)
    res = await cat.refresh(_ctx(trace_id="t"))
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == "UNCONFIGURED"
    assert http.calls == []  # zero sends to either host
    assert res.data is None or res.data.get("entries", 0) == 0


@pytest.mark.asyncio
async def test_refresh_within_ttl_skips_fetch():
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http)
    first = await cat.refresh(_ctx())
    assert first.status == "OK"
    second = await cat.refresh(_ctx())
    assert second.status == "OK"
    assert len(http.calls) == 1  # cached directory reused


@pytest.mark.asyncio
async def test_refresh_failure_keeps_old_known_at_and_entries():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock)
    ok = await cat.refresh(_ctx())
    assert ok.status == "OK"
    clock.advance(86500 * 1000)  # expire the 24h TTL
    from diveintocrypto_desktop.data.http import TransientUpstreamError

    http.error = TransientUpstreamError(500, None)
    res = await cat.refresh(_ctx())
    assert res.status in ("UNAVAILABLE", "ERROR")
    assert cat.known_at_ms == NOW_MS  # old receipt kept
    assert cat.entry_count == len(DIR_LIST)
    # NOTE: BTCUSDT is covered by the verified layer in this catalog; use the
    # directory-only SOLUSDT to prove the old directory receipt still serves.
    got = cat.candidates("SOLUSDT", clock())
    assert [c["coingecko_id"] for c in got] == ["solana"]
    assert got[0]["catalog_stale"] is True


@pytest.mark.asyncio
async def test_refresh_429_backs_off_without_retry_storm():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock)
    await cat.refresh(_ctx())
    clock.advance(86500 * 1000)
    from diveintocrypto_desktop.data.http import TransientUpstreamError

    http.error = TransientUpstreamError(429, 7.0)
    res = await cat.refresh(_ctx())
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == "COINGECKO_RATE_LIMITED"
    assert cat.next_allowed_at_ms == NOW_MS + 86500 * 1000 + 7000
    sends = len(http.calls)
    # Still inside backoff: no further send.
    again = await cat.refresh(_ctx())
    assert again.status == "UNAVAILABLE"
    assert len(http.calls) == sends


@pytest.mark.asyncio
async def test_refresh_bad_schema_and_oversize_keep_old_cache():
    clock = _clock()
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, clock=clock)
    await cat.refresh(_ctx())
    clock.advance(86500 * 1000)
    http.error = None
    http.payload = {"id": "not-a-list"}
    res = await cat.refresh(_ctx())
    assert res.status == "ERROR"
    assert res.reason_code == "COINGECKO_BAD_RESPONSE"
    assert cat.entry_count == len(DIR_LIST)
    # Oversized expanded payload is rejected the same way.
    tiny = _catalog(
        http=FakeDirHttp(payload=list(DIR_LIST)),
        clock=clock,
        api_key="K",
        max_list_bytes=10,
    )
    res2 = await tiny.refresh(_ctx())
    assert res2.status == "ERROR"
    assert tiny.entry_count == 0


@pytest.mark.asyncio
async def test_refresh_key_never_persisted_or_logged():
    http = FakeDirHttp(payload=list(DIR_LIST))
    cat = _catalog(http=http, api_key="SECRET-KEY-999")
    res = await cat.refresh(_ctx())
    assert res.status == "OK"
    blob = repr(cat.snapshot()) + repr(res.data) + (res.error_message or "")
    assert "SECRET-KEY-999" not in blob
    assert "SECRET-KEY-999" not in res.data["source_url_redacted"]


@pytest.mark.asyncio
async def test_platform_detail_capped_at_50_and_enriches_candidates():
    http = FakeDirHttp(payload=list(DIR_LIST))

    def _coin_payload(url, params):
        slug = url.rsplit("/", 1)[-1]
        return {
            "id": slug,
            "platforms": {
                "ethereum": "0x6982508145454ce325ddbe47a25d4ec3d2311933",
            },
        }

    http.payload = None
    http.error = None
    calls: list = []

    async def _router(url, params, request_context=None):
        calls.append((url, dict(params), request_context))
        if url.endswith("/coins/list"):
            return list(DIR_LIST)
        return _coin_payload(url, params)

    cat = _catalog(http=_router)
    await cat.refresh(_ctx())
    ids = [f"coin-{i:03d}" for i in range(60)]
    res = await cat.refresh_platform_details(ids, _ctx(trace_id="p"))
    assert res.status == "OK"
    assert res.data["fetched"] == 50
    assert res.data["skipped_over_cap"] == 10
    detail_calls = [c for c in calls if not c[0].endswith("/coins/list")]
    assert len(detail_calls) == 50
    for url, params, got_ctx in detail_calls:
        assert "SECRET" not in url
        assert got_ctx.trace_id == "p"


@pytest.mark.asyncio
async def test_platform_detail_without_key_sends_nothing():
    http = FakeDirHttp(payload=[])
    cat = _catalog(http=http, api_key=None)
    res = await cat.refresh_platform_details(["bitcoin"], _ctx())
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == "UNCONFIGURED"
    assert http.calls == []


# ---------------------------------------------------------------------------
# 6. identity snapshot IDs: deterministic content addressing for F05
# ---------------------------------------------------------------------------


def test_snapshot_id_is_deterministic_and_version_sensitive():
    mod = _load(CATALOG)
    cat = _catalog(http=FakeDirHttp(payload=[]))
    ident = {
        "canonical_id": "bitcoin",
        "coingecko_id": "bitcoin",
        "binance_spot_symbol": "BTCUSDT",
        "contract_multiplier": 1,
        "multiplier_source": "MANUAL",
        "chain": "ethereum",
        "contract_address": None,
        "mapping_confidence": "VERIFIED",
        "mapping_source": "MANUAL",
    }
    a = cat.snapshot_id_for("BTCUSDT", ident, NOW_MS)
    b = cat.snapshot_id_for("BTCUSDT", dict(ident), NOW_MS)
    assert a == b and a.startswith("isl-")
    assert cat.snapshot_id_for("ETHUSDT", ident, NOW_MS) != a
    assert cat.snapshot_id_for("BTCUSDT", ident, NOW_MS + 1) != a
    changed = dict(ident, coingecko_id="wrapped-bitcoin")
    assert cat.snapshot_id_for("BTCUSDT", changed, NOW_MS) != a


def test_mapping_version_covers_all_layers():
    cat = _catalog(http=FakeDirHttp(payload=[]))
    version = cat.mapping_version
    assert "verified-v3" in version
    assert "overrides-v7" in version
    assert "dir-none" in version


# ---------------------------------------------------------------------------
# 7. FULL skeletons are never READY-capable
# ---------------------------------------------------------------------------


def test_null_provider_capability_is_not_ready():
    mod = _load(BASE)
    cap = mod.provider_capability(mod.NullProvider())
    assert cap.ready is False
    assert cap.reason_code == mod.PROVIDER_NOT_CONFIGURED


def test_skeleton_and_example_providers_are_not_ready():
    import types

    mod = _load(BASE)
    skeleton = types.SimpleNamespace(name="unlock")
    cap = mod.provider_capability(skeleton)
    assert cap.ready is False
    assert cap.reason_code == mod.PROVIDER_TRANSPORT_UNVERIFIED
    example = types.SimpleNamespace(name="coingecko.example", transport_verified=True)
    assert mod.provider_capability(example).ready is False


@pytest.mark.asyncio
async def test_coingecko_fake_fetcher_is_not_verified_transport():
    cg = _load(CG)

    async def _fake(url, params):
        return {}

    provider = cg.CoinGeckoProvider(fetcher=_fake)
    base = _load(BASE)
    assert base.provider_capability(provider).ready is False
    verified = cg.CoinGeckoProvider(fetcher=_fake, transport_verified=True)
    assert base.provider_capability(verified).ready is True
    default_transport = cg.CoinGeckoProvider()
    assert base.provider_capability(default_transport).ready is True
