"""R11a endpoint weights v2 registry (D19.3).

    - Version is endpoint-weights-v3; every Mark/FundingInfo/Spot/Alpha/
  CoinGecko/0x allowed URL registers a family; unknown host/path/limit
  refuses as UNBUDGETED without transport.
- FAPI/Spot local conservative tokens are versioned and limit-aware;
  Alpha/CoinGecko/0x use independent host-local limits (never Binance
  official IP buckets).
- Alpha five paths frozen from data/binance_alpha.py PATH constants;
  0x only read-only allowance-holder price paths (never trade/approve/
  calldata).
"""

from __future__ import annotations

from diveintocrypto_desktop.data import binance_alpha as alpha_mod
from diveintocrypto_desktop.shortlab import request_budget as rb
from diveintocrypto_desktop.shortlab.hedge.venues import ethereum_0x as zerox_mod


class TestEndpointWeightsV2:
    def test_version(self):
        assert rb.ENDPOINT_WEIGHTS_VERSION == "endpoint-weights-v3"

    def test_fapi_weights(self):
        # fundingInfo 5, still funding-shared.
        assert rb.endpoint_weight("fundingInfo", None) == 5
        assert "fundingInfo" in rb.FUNDING_FAMILIES
        # markKlines shares klines buckets, 1..1500.
        assert rb.endpoint_weight("markKlines", {"limit": 10}) == 10
        assert rb.endpoint_weight("markKlines", {"limit": 500}) == 20
        assert rb.endpoint_weight("markKlines", {"limit": 1000}) == 30
        assert rb.endpoint_weight("markKlines", {"limit": 1500}) == 30
        assert rb.endpoint_weight("markKlines", {"limit": 0}) is None
        assert rb.endpoint_weight("markKlines", {"limit": 1501}) is None
        # futuresDepth buckets.
        assert rb.endpoint_weight("futuresDepth", {"limit": 100}) == 20
        assert rb.endpoint_weight("futuresDepth", {"limit": 500}) == 30
        assert rb.endpoint_weight("futuresDepth", {"limit": 1000}) == 50
        assert rb.endpoint_weight("futuresDepth", None) is None
        assert rb.endpoint_weight("futuresDepth", {"limit": 5000}) is None

    def test_spot_weights(self):
        assert rb.endpoint_weight("spotKlines", {"limit": 10}) == 10
        assert rb.endpoint_weight("spotKlines", {"limit": 1000}) == 30
        assert rb.endpoint_weight("spotKlines", {"limit": 1001}) is None
        assert rb.endpoint_weight("spotDepth", {"limit": 100}) == 20
        assert rb.endpoint_weight("spotDepth", {"limit": 1000}) == 50
        assert rb.endpoint_weight("spotTicker", {"symbol": "BTCUSDT"}) == 2
        assert rb.endpoint_weight("spotTicker", {"symbols": '["BTCUSDT"]'}) == 40
        assert rb.endpoint_weight("spotTicker", {"symbols": '["BTCUSDT", "ETHUSDT"]'}) == 40
        assert rb.endpoint_weight("spotTicker", {"symbols": '[' + ','.join('"S' + str(i) + '"' for i in range(20)) + ']'}) == 40
        assert rb.endpoint_weight("spotTicker", {"symbols": '[' + ','.join('"S' + str(i) + '"' for i in range(21)) + ']'}) == 40
        assert rb.endpoint_weight("spotTicker", {"symbols": '[' + ','.join('"S' + str(i) + '"' for i in range(100)) + ']'}) == 40
        assert rb.endpoint_weight("spotTicker", {"symbols": '[' + ','.join('"S' + str(i) + '"' for i in range(101)) + ']'}) == 80
        assert rb.endpoint_weight("spotTicker", {"symbols": '[' + ','.join('"S' + str(i) + '"' for i in range(1000)) + ']'}) == 80
        assert rb.endpoint_weight("spotTicker", None) == 80
        assert rb.endpoint_weight("spotTicker", {"symbol": "BTCUSDT", "symbols": '["BTCUSDT"]'}) is None
        assert rb.endpoint_weight("spotTicker", {"symbols": "not-json"}) is None
        assert rb.endpoint_weight("spotTicker", {"symbols": "{}"}) is None

    def test_alpha_paths_frozen(self):
        # Five PATH constants frozen to families, each weight 10.
        assert alpha_mod.PATH_TOKEN_LIST.endswith("/token/list")
        mapping = {
            alpha_mod.PATH_TOKEN_LIST: "alphaTokenList",
            alpha_mod.PATH_EXCHANGE_INFO: "alphaExchangeInfo",
            alpha_mod.PATH_TICKER: "alphaTicker",
            alpha_mod.PATH_FULL_DEPTH: "alphaDepth",
            alpha_mod.PATH_KLINES: "alphaKlines",
        }
        for path, family in mapping.items():
            url = f"https://www.binance.com{path}"
            assert rb.endpoint_family_for_url(url) == family, path
            assert rb.endpoint_weight(family, {"symbol": "X", "limit": 100}) == 10, family
        # Unknown alpha path refuses.
        assert rb.endpoint_family_for_url("https://www.binance.com/bapi/defi/v1/public/unknown") is None

    def test_coingecko_weights(self):
        for family in ("cgDirectory", "cgMarkets", "cgCoin", "cgFx"):
            assert rb.endpoint_weight(family, None) == 1, family
        # Priority: list/markets beat dynamic {id}.
        assert rb.endpoint_family_for_url("https://api.coingecko.com/api/v3/coins/list") == "cgDirectory"
        assert rb.endpoint_family_for_url("https://api.coingecko.com/api/v3/coins/markets") == "cgMarkets"
        assert rb.endpoint_family_for_url("https://api.coingecko.com/api/v3/coins/bitcoin") == "cgCoin"
        # Nested dynamic path refuses (single id segment only).
        assert rb.endpoint_family_for_url("https://api.coingecko.com/api/v3/coins/bitcoin/extra") is None

    def test_0x_weights(self):
        assert rb.endpoint_weight("onchainPrice", None) == 1
        assert rb.endpoint_family_for_url(zerox_mod.PRICE_URL) == "onchainPrice"
        # Header no-suffix path also allowed; trade paths never.
        assert (
            rb.endpoint_family_for_url("https://api.0x.org/swap/allowance-holder/price")
            == "onchainPrice"
        )
        assert rb.endpoint_family_for_url("https://api.0x.org/swap/allowance-holder/build") is None
        assert rb.endpoint_family_for_url("https://api.0x.org/tx/calldata") is None

    def test_unknown_registry(self):
        assert rb.endpoint_family_for_url("https://evil.com/fapi/v1/klines") is None
        assert rb.endpoint_family_for_url("https://fapi.binance.com/fapi/v1/tradeOrder") is None
        assert rb.endpoint_weight("no-such-family", {}) is None
        assert rb.endpoint_weight(None, {}) is None
        assert rb.endpoint_weight("klines", {"limit": 0}) is None
        assert rb.endpoint_weight("klines", {"limit": 5000}) is None
