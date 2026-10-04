"""H05: Ethereum/0x read-only price adapter + independent PoC (plan H05, AC12).

Contract (ShortLab_Integrated_Implementation_Plan_CN.md H05 + design B12):

- `hedge/venues/onchain.py` holds `OnchainQuoteProvider` with
  `quote_buy`/`quote_sell`/`health`, `quote_kind="INDICATIVE"`.
- `hedge/venues/ethereum_0x.py` pins chain 1 / `https://api.0x.org` /
  `/swap/allowance-holder/price/v2`, header `0x-version: v2`, key from
  `SHORTLAB_0X_API_KEY`; no key means zero network + `UNCONFIGURED` health;
  1 RPS / concurrency 1; `buyAmount`/`sellAmount` are mutually exclusive
  atomic-unit strings for the same net quantity; gas may be null; missing
  provider TTL falls back to local 30s; never fetches getQuote/calldata/
  approve.

All network access here is faked with fixed fixtures (no live sends). The
no-key branch is really executed (never skipped) and asserts zero sends.
EIP-55 vectors need no network. Gas-missing / no-route / 403 / 429 /
expired never backfill "0". The with-key PoC is an independent report: in
a keyless environment the suite records NOT_VERIFIED and never claims
completion.
"""

from __future__ import annotations

import os
from decimal import Decimal

import pytest

NOW = 1_760_000_000_000

# EIP-55 spec vectors (same as F04 suite; Ethereum Keccak, never NIST SHA3).
EIP55_GOOD = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"
EIP55_BAD = "0x5aaeb6053F3E94C9b9A09f33669435E7Ef1BeAed"  # one nibble flipped
USDC_ADDRESS = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDC_DECIMALS = 6
TOKEN_DECIMALS = 18
QTY = "1.5"
ATOMIC_QTY = "1500000000000000000"  # 1.5 * 10**18, integer string, no float
USDC_ATOMIC = "4500000000"  # 4500.00 USDC * 10**6

PRICE_URL = "https://api.0x.org/swap/allowance-holder/price/v2"


@pytest.fixture(autouse=True)
def _scrub_0x_key(monkeypatch):
    # Every test starts keyless; configured tests inject `api_key=` or
    # `env=` explicitly so a developer-machine key can never leak in.
    monkeypatch.delenv("SHORTLAB_0X_API_KEY", raising=False)


def _ident(chain="ethereum", address=EIP55_GOOD, confidence="VERIFIED"):
    from diveintocrypto_desktop.shortlab.models import AssetIdentity

    return AssetIdentity(
        canonical_id="ethereum-test-token",
        display_symbol="ETK",
        binance_futures_symbol="ETKUSDT",
        binance_spot_symbol=None,
        chain=chain,
        contract_address=address,
        mapping_confidence=confidence,  # type: ignore[arg-type]
        mapping_source="CONTRACT",  # type: ignore[arg-type]
    )


class _HttpErr(Exception):
    def __init__(self, status: int, message: str = "http error",
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class FakePriceHttp:
    """Counting fake for the 0x price endpoint (never real network)."""

    def __init__(self, payload=None, error=None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    async def __call__(self, url, params, headers, request_context=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        if self.error is not None:
            raise self.error
        if callable(self.payload):
            return self.payload(dict(params or {}))
        if isinstance(self.payload, dict):
            return dict(self.payload)
        return self.payload


def _venue(http, *, api_key="TEST-0X-KEY", now=NOW, **over):
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as m

    kwargs = dict(
        api_key=api_key,
        price_fn=http,
        now_ms_fn=lambda: now,
        decimals_by_address={EIP55_GOOD.lower(): TOKEN_DECIMALS},
        # No real waiting in the suite; one dedicated test covers throttling.
        min_interval_sec=0.0,
    )
    kwargs.update(over)
    return m.Ethereum0xPriceVenue(**kwargs)


def _ok_payload(*, with_gas=True, expiry=NOW + 25_000):
    payload = {
        "blockNumber": "21600000",
        "buyAmount": ATOMIC_QTY,
        "sellAmount": USDC_ATOMIC,
        "buyToken": EIP55_GOOD,
        "sellToken": USDC_ADDRESS,
    }
    if with_gas:
        payload["gas"] = "120000"
        payload["gasPrice"] = "15000000000"
    if expiry is not None:
        payload["expiration"] = int(expiry)
    return payload


# ---------------------------------------------------------------------------
# Protocol shape (onchain.py): quote_buy/quote_sell/health, INDICATIVE only
# ---------------------------------------------------------------------------

def test_onchain_provider_protocol_shape_is_indicative_only():
    from diveintocrypto_desktop.shortlab.hedge.venues import onchain as m

    assert m.ONCHAIN_QUOTE_KIND == "INDICATIVE"
    for method in ("quote_buy", "quote_sell", "health"):
        assert hasattr(m.OnchainQuoteProvider, method), method
    # The frozen DTO stays INDICATIVE + simulation_verified=False (H01).
    from diveintocrypto_desktop.shortlab.hedge.models import OnchainQuote

    quote = OnchainQuote(
        venue="ONCHAIN_DEX", canonical_id="ethereum-test-token", chain="ethereum",
        contract_address=EIP55_GOOD, as_of_ms=NOW, expires_at_ms=NOW + 30_000,
        requested_canonical_qty=QTY, provider_id="ETHEREUM_0X_PRICE_V2",
        api_version="v2", chain_id=1, block_number=21600000,
        atomic_amounts={"buyAmount": ATOMIC_QTY, "sellAmount": USDC_ATOMIC},
        decimals=TOKEN_DECIMALS, quote_kind="INDICATIVE",
        buy_vwap="3000", sell_vwap=None,
        buy_executable_qty=QTY, sell_executable_qty=None,
        gas_units=None, gas_price=None, native_gas_fx=None,
        estimated_gas_usd=None, estimated_fee_usd=None,
        token_tax_status="UNKNOWN", route_complete=True,
        simulation_verified=False, quote_currency="USDC",
        quote_to_usd=None, fetched_at_ms=NOW, status="OK", reason_code=None,
    )
    assert quote.quote_kind == "INDICATIVE"
    assert quote.simulation_verified is False
    assert quote.venue == "ONCHAIN_DEX"


def test_ethereum_0x_pins_chain_host_endpoint_version_and_key_env():
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as m

    assert m.CHAIN_ID == 1
    assert m.BASE_URL == "https://api.0x.org"
    assert m.PRICE_PATH == "/swap/allowance-holder/price/v2"
    assert m.PRICE_URL == PRICE_URL
    assert m.API_VERSION == "v2"
    assert m.API_KEY_ENV == "SHORTLAB_0X_API_KEY"
    assert m.PROVIDER_ID == "ETHEREUM_0X_PRICE_V2"
    assert m.REQUESTS_PER_SEC == 1
    assert m.CONCURRENCY == 1
    # Official docs path uses `evm-ap-is` verbatim (B12.2 freeze).
    assert "evm-ap-is" in m.DOC_PRICE_URL
    assert "api-overview" in m.DOC_AUTH_URL


# ---------------------------------------------------------------------------
# No key: really executed, UNCONFIGURED, zero sends (never skipped)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_without_key_health_and_quotes_return_unconfigured_with_zero_sends():
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as m

    http = FakePriceHttp(payload=_ok_payload())
    venue = m.Ethereum0xPriceVenue(
        api_key=None, env={}, price_fn=http, now_ms_fn=lambda: NOW,
        decimals_by_address={EIP55_GOOD.lower(): TOKEN_DECIMALS},
        min_interval_sec=0.0,
    )
    # Health: no key -> zero network, UNCONFIGURED (this branch is executed,
    # not skipped, so CI proves the guard without a secret).
    health = await venue.health(None)
    assert health.status == "UNAVAILABLE"
    assert health.reason_code == "UNCONFIGURED"
    assert health.data is not None and health.data.get("enabled") is False
    # Quotes: same guard, zero sends, never a zero-price fabrication.
    buy = await venue.quote_buy(_ident(), QTY, "USDC", NOW, None)
    sell = await venue.quote_sell(_ident(), QTY, "USDC", NOW, None)
    assert buy.status == "UNAVAILABLE" and buy.reason_code == "UNCONFIGURED"
    assert sell.status == "UNAVAILABLE" and sell.reason_code == "UNCONFIGURED"
    assert buy.data is None and sell.data is None
    assert http.calls == []


@pytest.mark.asyncio
async def test_without_key_env_truly_absent_still_zero_sends():
    # Belt-and-braces: even when the process env has no key at all, the
    # adapter opens zero connections (the autouse fixture scrubs the env).
    assert os.environ.get("SHORTLAB_0X_API_KEY") in (None, "")
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as m

    http = FakePriceHttp(payload=_ok_payload())
    venue = m.Ethereum0xPriceVenue(
        api_key=None, price_fn=http, now_ms_fn=lambda: NOW,
        decimals_by_address={EIP55_GOOD.lower(): TOKEN_DECIMALS},
        min_interval_sec=0.0,
    )
    res = await venue.quote_buy(_ident(), QTY, "USDC", NOW, None)
    assert res.reason_code == "UNCONFIGURED"
    assert http.calls == []


# ---------------------------------------------------------------------------
# EIP-55 correct/wrong/unknown: no network for validation itself
# ---------------------------------------------------------------------------

def test_eip55_vectors_need_no_network():
    # Pure F04 validation: correct / wrong / unknown are decided locally with
    # zero HTTP (this test never constructs a venue transport).
    from diveintocrypto_desktop.shortlab.identity import resolver as r

    good = r.normalize_chain_address("ethereum", EIP55_GOOD)
    assert good.validation_status == r.CHECKSUM_VERIFIED
    assert good.canonical_key == EIP55_GOOD.lower()
    assert good.display == EIP55_GOOD
    bad = r.normalize_chain_address("ethereum", EIP55_BAD)
    assert bad.validation_status == r.BAD_CHECKSUM
    assert bad.canonical_key != good.canonical_key
    unknown = r.normalize_chain_address("mychain-xyz", "AbC123xYz")
    assert unknown.validation_status == r.ADDRESS_CHAIN_UNSUPPORTED
    assert unknown.canonical_key == "AbC123xYz"  # never lowered


@pytest.mark.asyncio
async def test_bad_checksum_and_unknown_chain_never_send():
    http = FakePriceHttp(payload=_ok_payload())
    venue = _venue(http)
    bad = await venue.quote_buy(_ident(address=EIP55_BAD), QTY, "USDC", NOW, None)
    assert bad.status == "UNAVAILABLE"
    assert bad.reason_code == "BAD_CHECKSUM"
    assert bad.data is None
    unknown = await venue.quote_sell(
        _ident(chain="mychain-xyz", address="AbC123xYz"), QTY, "USDC", NOW, None
    )
    assert unknown.status == "UNAVAILABLE"
    assert unknown.reason_code in ("ADDRESS_CHAIN_UNSUPPORTED", "CHAIN_PROVIDER_UNCONFIGURED")
    assert unknown.data is None
    # Other EVM chains are explicitly out of scope (chain 1 only).
    other = await venue.quote_buy(_ident(chain="polygon", address=EIP55_GOOD), QTY, "USDC", NOW, None)
    assert other.status == "UNAVAILABLE"
    assert other.reason_code == "CHAIN_PROVIDER_UNCONFIGURED"
    assert other.data is None
    assert http.calls == []


@pytest.mark.asyncio
async def test_lowercase_no_checksum_and_unverified_identity_never_send():
    http = FakePriceHttp(payload=_ok_payload())
    venue = _venue(http)
    # All-lowercase carries no checksum proof: inspectable but not quotable.
    lower = await venue.quote_buy(
        _ident(address=EIP55_GOOD.lower()), QTY, "USDC", NOW, None
    )
    assert lower.status == "UNAVAILABLE"
    assert lower.data is None
    assert lower.reason_code in ("NO_CHECKSUM", "ONCHAIN_IDENTITY_UNVERIFIED")
    # UNRESOLVED identity never reaches the network either.
    unresolved = await venue.quote_buy(
        _ident(confidence="UNRESOLVED"), QTY, "USDC", NOW, None
    )
    assert unresolved.status == "UNAVAILABLE"
    assert unresolved.data is None
    # Missing decimals cannot be guessed from the symbol.
    bare = _venue(http, decimals_by_address={})
    missing = await bare.quote_buy(_ident(), QTY, "USDC", NOW, None)
    assert missing.status == "UNAVAILABLE"
    assert missing.reason_code == "ONCHAIN_DECIMALS_UNVERIFIED"
    assert missing.data is None
    assert http.calls == []


# ---------------------------------------------------------------------------
# Fixed endpoint / headers; buy/sellAmount mutually exclusive, same net qty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_endpoint_headers_and_mutually_exclusive_atomic_amounts():
    def _directional(params):
        # Mirror a real price service: echo the requested atomic leg and
        # price the other side in USDC (same net token quantity both ways).
        base = {"blockNumber": "21600000", "gas": "120000",
                "gasPrice": "15000000000", "expiration": NOW + 25_000}
        if "buyAmount" in params:
            return {**base, "buyAmount": params["buyAmount"],
                    "sellAmount": USDC_ATOMIC,
                    "buyToken": EIP55_GOOD, "sellToken": USDC_ADDRESS}
        return {**base, "buyAmount": USDC_ATOMIC,
                "sellAmount": params["sellAmount"],
                "buyToken": USDC_ADDRESS, "sellToken": EIP55_GOOD}

    http = FakePriceHttp(payload=_directional)
    venue = _venue(http)
    buy = await venue.quote_buy(_ident(), QTY, "USDC", NOW, None)
    sell = await venue.quote_sell(_ident(), QTY, "USDC", NOW, None)
    assert buy.status in ("OK", "PARTIAL") and sell.status in ("OK", "PARTIAL")
    assert len(http.calls) == 2
    for call in http.calls:
        # Fixed chain-1 price endpoint only (never getQuote/calldata/approve).
        assert call["url"] == PRICE_URL
        assert call["headers"].get("0x-version") == "v2"
        assert "0x-api-key" in call["headers"]
        params = call["params"]
        assert params.get("chainId") in ("1", 1)
        # No wallet / tx material ever leaves the adapter.
        for forbidden in ("taker", "recipient", "txOrigin", "tx_origin",
                          "calldata", "data", "transaction", "approve",
                          "permit2", "getQuote"):
            assert forbidden not in params, forbidden
    buy_params, sell_params = http.calls[0]["params"], http.calls[1]["params"]
    # Buy uses buyAmount, sell uses the same net token sellAmount; never both.
    assert buy_params.get("buyAmount") == ATOMIC_QTY
    assert "sellAmount" not in buy_params
    assert sell_params.get("sellAmount") == ATOMIC_QTY
    assert "buyAmount" not in sell_params
    # Atomic strings: integer text, no float 10**decimals, same net quantity.
    assert buy_params["buyAmount"].isdigit()
    assert sell_params["sellAmount"].isdigit()
    assert buy_params["buyAmount"] == sell_params["sellAmount"] == ATOMIC_QTY
    # Frozen DTO carries the same requested qty + integer atomics both sides.
    assert buy.data is not None and sell.data is not None
    assert buy.data.requested_canonical_qty == QTY
    assert sell.data.requested_canonical_qty == QTY
    assert buy.data.atomic_amounts["buyAmount"] == ATOMIC_QTY
    assert sell.data.atomic_amounts["sellAmount"] == ATOMIC_QTY
    assert buy.data.quote_kind == "INDICATIVE"
    assert sell.data.simulation_verified is False
    # VWAPs are decimal strings (USDC per token), never float math.
    Decimal(buy.data.buy_vwap)  # type: ignore[arg-type]
    Decimal(sell.data.sell_vwap)  # type: ignore[arg-type]
    assert buy.data.buy_executable_qty == QTY
    assert sell.data.sell_executable_qty == QTY


@pytest.mark.asyncio
async def test_transaction_payload_in_response_is_rejected_without_storing():
    http = FakePriceHttp(payload={
        **_ok_payload(),
        "transaction": {"to": "0x1234", "data": "0xdeadbeef1234"},
        "calldata": "0xdeadbeef1234",
    })
    venue = _venue(http)
    res = await venue.quote_buy(_ident(), QTY, "USDC", NOW, None)
    assert res.status == "ERROR"
    assert res.reason_code == "PRICE_SCHEMA_REJECTED"
    assert res.data is None
    # The adapter exposes no tx-building surface at all.
    assert not hasattr(venue, "get_quote")
    assert not hasattr(venue, "build_calldata")
    assert not hasattr(venue, "build_approve")


# ---------------------------------------------------------------------------
# Gas missing / no route / 403 / 429 / expired: never backfill "0"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gas_missing_keeps_null_not_zero():
    http = FakePriceHttp(payload=_ok_payload(with_gas=False, expiry=None))
    venue = _venue(http)
    res = await venue.quote_buy(_ident(), QTY, "USDC", NOW, None)
    assert res.status == "PARTIAL"
    assert res.data is not None
    quote = res.data
    assert quote.gas_units is None
    assert quote.gas_price is None
    assert quote.estimated_gas_usd is None
    assert quote.estimated_fee_usd is None
    assert quote.quote_to_usd is None
    for field in (quote.gas_units, quote.gas_price, quote.estimated_gas_usd,
                  quote.estimated_fee_usd, quote.quote_to_usd):
        assert field != "0"
    assert quote.route_complete is True
    # Missing provider TTL falls back to local fetched+30s with clear source.
    assert quote.expires_at_ms == NOW + 30_000
    assert quote.fetched_at_ms == NOW


@pytest.mark.asyncio
async def test_no_sell_route_is_unavailable_without_zero_price():
    http = FakePriceHttp(payload={"blockNumber": "21600000"})
    venue = _venue(http)
    res = await venue.quote_sell(_ident(), QTY, "USDC", NOW, None)
    assert res.status == "UNAVAILABLE"
    assert res.reason_code in ("NO_ROUTE", "ROUTE_INCOMPLETE")
    assert res.data is None


@pytest.mark.asyncio
async def test_403_429_are_unavailable_never_na_and_never_zero():
    for status, reason in ((403, "VENUE_REGION_UNAVAILABLE"), (429, "RATE_LIMITED")):
        http = FakePriceHttp(error=_HttpErr(status, "blocked"))
        venue = _venue(http)
        res = await venue.quote_buy(_ident(), QTY, "USDC", NOW, None)
        assert res.status == "UNAVAILABLE", status
        assert res.reason_code == reason, status
        assert res.status != "NOT_APPLICABLE"
        assert res.data is None


@pytest.mark.asyncio
async def test_expired_provider_quote_is_not_served_as_zero():
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as m

    http = FakePriceHttp(payload=_ok_payload(expiry=NOW - 1_000))
    venue = _venue(http)
    res = await venue.quote_buy(_ident(), QTY, "USDC", NOW, None)
    assert res.status == "UNAVAILABLE"
    assert res.reason_code == "QUOTE_EXPIRED"
    assert res.data is None
    # The expiry helper itself never synthesises a price either.
    fresh = await _venue(FakePriceHttp(payload=_ok_payload(expiry=None))).quote_buy(
        _ident(), QTY, "USDC", NOW, None
    )
    assert fresh.data is not None
    assert m.is_onchain_quote_expired(fresh.data, NOW + 30_001) is True
    assert m.is_onchain_quote_expired(fresh.data, NOW + 29_999) is False


# ---------------------------------------------------------------------------
# Limits: 1 RPS / concurrency 1; error messages never carry the key
# ---------------------------------------------------------------------------

def test_rate_limit_and_concurrency_are_pinned_to_one():
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as m

    venue = _venue(FakePriceHttp(payload=_ok_payload()))
    assert venue.requests_per_sec == 1
    assert venue.concurrency == 1
    assert m.REQUESTS_PER_SEC == 1 and m.CONCURRENCY == 1


@pytest.mark.asyncio
async def test_throttle_serialises_sends_without_leaking_the_key():
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as m

    sleeps: list[float] = []
    clock = {"mono": 100.0}

    async def _sleep(delay: float) -> None:
        sleeps.append(float(delay))
        clock["mono"] += float(delay)

    http = FakePriceHttp(payload=_ok_payload())
    venue = m.Ethereum0xPriceVenue(
        api_key="SUPER-SECRET-KEY",
        price_fn=http,
        now_ms_fn=lambda: NOW,
        decimals_by_address={EIP55_GOOD.lower(): TOKEN_DECIMALS},
        min_interval_sec=1.0,
        monotonic_fn=lambda: clock["mono"],
        sleep_fn=_sleep,
    )
    first = await venue.quote_buy(_ident(), QTY, "USDC", NOW, None)
    # Advance less than 1s: the second send must wait out the 1 RPS window.
    clock["mono"] += 0.2
    second = await venue.quote_sell(_ident(), QTY, "USDC", NOW, None)
    assert first.status in ("OK", "PARTIAL")
    assert second.status in ("OK", "PARTIAL")
    assert sleeps and sleeps[0] > 0
    assert len(http.calls) == 2
    for call in http.calls:
        assert "SUPER-SECRET-KEY" not in str(call["params"])
    assert "SUPER-SECRET-KEY" not in str(first.error_message or "")
    assert "SUPER-SECRET-KEY" not in str(second.error_message or "")


# ---------------------------------------------------------------------------
# Independent PoC report: keyless records NOT_VERIFIED, never claims complete
# ---------------------------------------------------------------------------

def test_poc_report_without_key_records_not_verified():
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as m

    report = m.build_poc_report(api_key_present=False)
    assert report["live_verified"] is False
    assert report["live_status"] == "NOT_VERIFIED"
    assert report["reason_code"] == "UNCONFIGURED"
    assert report["endpoint"] == PRICE_URL
    assert "evm-ap-is" in report["docs_url"]
    # The offline suite must never present fixture success as live proof.
    assert "do not claim" in report["note"].lower() or "not" in report["note"].lower()


def test_poc_report_with_fake_transport_stays_not_verified_without_live():
    from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as m

    report = m.build_poc_report(api_key_present=True, live_response=None)
    assert report["live_verified"] is False
    assert report["live_status"] == "NOT_VERIFIED"
    assert report["endpoint"] == PRICE_URL
