"""H02 Alpha: public market-data + same-quantity dual quote (B11, AC12/AC13).

Fake HTTP fixtures only (no network). Plan-literal checks:

- `code == "000000"` envelope (+ `success == true` when present) with the
  `data` shape; HTTP-200 business failures are honest ERRORs;
- `451`/`403` -> VENUE_REGION_UNAVAILABLE, `429` -> RATE_LIMITED
  (Retry-After kept) -- never N/A, never a region bypass;
- default fullDepth 100, escalate 500/1000 within budget while either side
  cannot cover; truncated books never prove deeper fills;
- Alpha instrument comes from the venue mapping
  (`binance_alpha_symbol`, never a tokenId guess); disabled config sends
  zero requests (fixture + live evidence gate, H02.3).

NOTE on `code000000`/schema (task rule): this suite implements the plan
literal (string `"000000"`). The bundled fixtures below use that literal.
If a captured live fixture shows an integer `0` or a missing `code` with
`success: true`, the implementation (`data/binance_alpha.py:
check_alpha_envelope`) must be updated to the fixture and the deviation
noted here -- fixtures win over the literal.
"""

from __future__ import annotations

import pytest
from decimal import Decimal

from diveintocrypto_desktop.data import binance_alpha as alpha_mod
from diveintocrypto_desktop.shortlab.hedge.venues.binance_alpha import (
    BinanceAlphaVenue,
    ALPHA_BOOK_THIN,
    ALPHA_BUSINESS_ERROR,
    ALPHA_DISABLED,
    TRADING_RULES_UNVERIFIED,
    VENUE_REGION_UNAVAILABLE,
)


NOW = 1_760_000_000_000


def _alpha_filters() -> list[dict]:
    return [
        {"filterType": "PRICE_FILTER", "minPrice": "0.001",
         "maxPrice": "1000", "tickSize": "0.001"},
        {"filterType": "LOT_SIZE", "minQty": "1",
         "maxQty": "1000000", "stepSize": "1"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "1",
         "maxQty": "1000000", "stepSize": "1"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "5",
         "applyToMarket": True, "avgPriceMins": 5},
    ]


def _alpha_entry(symbol="ALPHA1USDT", filters=None) -> dict:
    return {
        "symbol": symbol,
        "status": "TRADING",
        "baseAsset": symbol.replace("USDT", ""),
        "quoteAsset": "USDT",
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": filters if filters is not None else _alpha_filters(),
    }


def _envelope(data) -> dict:
    return {"code": "000000", "msg": "success", "data": data}


def _token_list_payload() -> dict:
    return _envelope([
        {"tokenId": "tok-1", "symbol": "ALPHA1USDT",
         "contractAddress": "0x" + "11" * 20, "decimals": 18, "chainId": 56},
    ])


def _exchange_payload(symbol="ALPHA1USDT", filters=None) -> dict:
    return _envelope({"symbols": [_alpha_entry(symbol, filters)]})


def _ticker_payload(symbol="ALPHA1USDT", bid="1.000", ask="1.010") -> dict:
    return _envelope({"symbol": symbol, "bidPrice": bid, "askPrice": ask,
                      "lastPrice": "1.005"})


def _depth_payload(*, bids=None, asks=None, E=1_760_000_000_000,
                   T=1_760_000_000_001) -> dict:
    if bids is None:
        bids = [["1.000", "100"], ["0.990", "200"]]
    if asks is None:
        asks = [["1.010", "100"], ["1.020", "200"]]
    return _envelope({"E": E, "T": T, "lastUpdateId": 99,
                      "bids": bids, "asks": asks})


class _HttpErr(Exception):
    def __init__(self, status: int, message: str = "http error",
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _identity(symbol="ALPHA1USDT", token_id="tok-1", canonical="alpha1",
              confidence="VERIFIED"):
    from diveintocrypto_desktop.shortlab.models import AssetIdentity

    # AssetIdentity has no alpha fields; the venue reads duck-typed
    # `binance_alpha_symbol` / `binance_alpha_token_id` (mapping table).
    base = AssetIdentity(
        canonical_id=canonical, display_symbol=canonical,
        binance_futures_symbol="ALPHA1USDT",
        binance_spot_symbol=None,
        mapping_confidence=confidence,  # type: ignore[arg-type]
        mapping_source="UNIQUE_SYMBOL",  # type: ignore[arg-type]
    )
    object.__setattr__(base, "binance_alpha_symbol", symbol)
    object.__setattr__(base, "binance_alpha_token_id", token_id)
    return base


def _config(*, alpha_enabled=True):
    class _Costs(dict):
        pass

    class _Hedge:
        def __init__(self):
            self.execution = {"max_price_impact_bps": 100000}
            self.costs = {"alpha_entry_fee_rate": 0.001,
                          "alpha_exit_fee_rate": 0.001}
            self.providers = {"binance_alpha": {"enabled": alpha_enabled}}

    class _Config:
        def __init__(self):
            self.hedge = _Hedge()

    return _Config()


def _venue_with_fakes(*, symbol="ALPHA1USDT", ticker=None, depths=None,
                      exchange_filters=None, token_payload=None,
                      exchange_payload=None, alpha_enabled=True,
                      depth_err=None, ticker_err=None,
                      token_err=None, exchange_err=None,
                      now=NOW):
    calls = {"token": 0, "exchange": 0, "ticker": 0, "depth": 0,
             "depth_limits": []}
    token_doc = token_payload if token_payload is not None else _token_list_payload()
    exchange_doc = (exchange_payload if exchange_payload is not None
                    else _exchange_payload(symbol, exchange_filters))
    ticker_doc = ticker if ticker is not None else _ticker_payload(symbol)
    depth_docs = depths if depths is not None else [_depth_payload()]

    async def token_fn():
        calls["token"] += 1
        if token_err is not None:
            raise token_err
        return alpha_mod.check_alpha_envelope(token_doc)

    async def exchange_fn():
        calls["exchange"] += 1
        if exchange_err is not None:
            raise exchange_err
        return alpha_mod.check_alpha_envelope(exchange_doc)

    async def ticker_fn(sym):
        calls["ticker"] += 1
        calls["ticker_symbol"] = sym
        if ticker_err is not None:
            raise ticker_err
        doc = ticker_doc
        if isinstance(doc, Exception):
            raise doc
        return alpha_mod.check_alpha_envelope(doc)

    async def depth_fn(sym, limit):
        calls["depth"] += 1
        calls["depth_limits"].append(int(limit))
        calls["depth_symbol"] = sym
        if depth_err is not None:
            raise depth_err
        # Serve per-limit docs when a list is given, else repeat the single.
        if isinstance(depth_docs, list) and all(isinstance(d, dict) for d in depth_docs):
            idx = {100: 0, 500: 1, 1000: 2}.get(int(limit), 0)
            doc = depth_docs[min(idx, len(depth_docs) - 1)]
        else:
            doc = depth_docs
        if isinstance(doc, Exception):
            raise doc
        return alpha_mod.check_alpha_envelope(doc)

    venue = BinanceAlphaVenue(
        _config(alpha_enabled=alpha_enabled),
        token_list_fn=token_fn,
        exchange_info_fn=exchange_fn,
        ticker_fn=ticker_fn,
        depth_fn=depth_fn,
        now_ms_fn=lambda: now,
    )
    return venue, calls


# ---------------------------------------------------------------------------
# data/binance_alpha: envelope + fetchers over fake http_get
# ---------------------------------------------------------------------------

def test_envelope_requires_code_000000_and_success_true():
    assert alpha_mod.check_alpha_envelope(_envelope({"a": 1})) == {"a": 1}
    with pytest.raises(alpha_mod.AlphaBusinessError):
        alpha_mod.check_alpha_envelope({"code": "123456", "data": {}})
    with pytest.raises(alpha_mod.AlphaBusinessError):
        alpha_mod.check_alpha_envelope({"code": "000000", "success": False,
                                        "data": {}})
    with pytest.raises(alpha_mod.AlphaBusinessError):
        alpha_mod.check_alpha_envelope({"code": "000000"})
    with pytest.raises(alpha_mod.AlphaBusinessError):
        alpha_mod.check_alpha_envelope(["not", "a", "mapping"])
    # Integer 0 is NOT accepted per the plan literal (fixture wins if live
    # shows otherwise -- currently no live fixture, so strict stays).
    with pytest.raises(alpha_mod.AlphaBusinessError):
        alpha_mod.check_alpha_envelope({"code": 0, "data": {}})


@pytest.mark.asyncio
async def test_alpha_fetchers_use_fixed_paths_and_validate_shapes():
    seen: list[tuple[str, dict]] = []

    async def fake_http(url: str, params: dict | None):
        seen.append((url, dict(params or {})))
        if url.endswith("/all/token/list"):
            return _token_list_payload()
        if url.endswith("/get-exchange-info"):
            return _exchange_payload()
        if url.endswith("/ticker"):
            assert params and params.get("symbol") == "ALPHA1USDT"
            return _ticker_payload()
        if url.endswith("/fullDepth"):
            assert params and params.get("symbol") == "ALPHA1USDT"
            assert params.get("limit") in (100, 500, 1000)
            return _depth_payload()
        raise AssertionError(f"unexpected alpha url {url!r}")

    alpha_mod.reset_alpha_cache()
    tokens = await alpha_mod.fetch_token_list(http_get=fake_http)
    assert isinstance(tokens, list) and tokens[0]["tokenId"] == "tok-1"
    info = await alpha_mod.fetch_exchange_info(http_get=fake_http)
    assert info["symbols"][0]["symbol"] == "ALPHA1USDT"
    ticker = await alpha_mod.fetch_ticker("ALPHA1USDT", http_get=fake_http)
    assert ticker["bidPrice"] == "1.000"
    depth = await alpha_mod.fetch_full_depth("ALPHA1USDT", 100, http_get=fake_http)
    assert depth["bids"][0][0] == "1.000"
    # Fixed host is www.binance.com (no FAPI host leak).
    assert all(url.startswith("https://www.binance.com/bapi/defi/v1/public/")
               for url, _ in seen)
    assert any(url.endswith("/alpha-trade/ticker") for url, _ in seen)
    assert any(url.endswith("/alpha-trade/fullDepth") for url, _ in seen)
    # Cached tokenList/exchangeInfo: second round sends no new list/info.
    n = len(seen)
    await alpha_mod.fetch_token_list(http_get=fake_http)
    await alpha_mod.fetch_exchange_info(http_get=fake_http)
    assert len(seen) == n
    alpha_mod.reset_alpha_cache()


@pytest.mark.asyncio
async def test_alpha_http_status_mapping_never_na():
    async def fake_451(url, params):
        raise _HttpErr(451, "unavailable for legal reasons")

    async def fake_403(url, params):
        raise _HttpErr(403, "forbidden")

    async def fake_429(url, params):
        raise _HttpErr(429, "too many", retry_after=2.0)

    alpha_mod.reset_alpha_cache()
    with pytest.raises(alpha_mod.AlphaRegionError) as exc_451:
        await alpha_mod.fetch_ticker("ALPHA1USDT", http_get=fake_451)
    assert exc_451.value.status == 451
    with pytest.raises(alpha_mod.AlphaRegionError):
        await alpha_mod.fetch_full_depth("A", 100, http_get=fake_403)
    with pytest.raises(alpha_mod.AlphaRateLimited) as exc_429:
        await alpha_mod.fetch_ticker("ALPHA1USDT", http_get=fake_429)
    assert exc_429.value.retry_after == 2.0
    alpha_mod.reset_alpha_cache()


def test_alpha_enabled_gate_reads_config_only():
    assert alpha_mod.is_alpha_enabled(_config(alpha_enabled=True)) is True
    assert alpha_mod.is_alpha_enabled(_config(alpha_enabled=False)) is False
    assert alpha_mod.is_alpha_enabled(None) is False
    assert alpha_mod.is_alpha_enabled(object()) is False


# ---------------------------------------------------------------------------
# Alpha venue: same-quantity dual quote
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alpha_same_quantity_dual_quote():
    venue, calls = _venue_with_fakes()
    result = await venue.quote(_identity(), "100")
    assert result.status == "OK"
    quote = result.data
    assert quote.venue == "BINANCE_ALPHA"
    assert quote.symbol == "ALPHA1USDT"
    assert quote.requested_canonical_qty == "100"
    # Buy from asks, sell to bids, same net quantity.
    # Asks 100@1.010 + 200@1.020: VWAP(100) = 1.010.
    # Bids 100@1.000 + 200@0.990: VWAP(100) = 1.000.
    assert Decimal(quote.buy_vwap) == Decimal("1.010")
    assert Decimal(quote.sell_vwap) == Decimal("1.000")
    assert quote.buy_executable_qty == "100"
    assert quote.sell_executable_qty == "100"
    assert quote.entry_feasible and quote.exit_feasible
    assert quote.exit_feasibility == "CONFIRMED"
    assert Decimal(quote.mid_price) == Decimal("1.005")
    assert quote.source_timestamp_ms == 1_760_000_000_000
    assert calls["ticker_symbol"] == "ALPHA1USDT"
    assert calls["depth_symbol"] == "ALPHA1USDT"
    assert calls["depth_limits"][0] == 100


@pytest.mark.asyncio
async def test_alpha_quantity_cache_and_depth_escalation():
    # Depth 100 covers 100 but not 250; 500 covers 250.
    thin_100 = _depth_payload(bids=[["1.000", "100"]], asks=[["1.010", "100"]])
    deep_500 = _depth_payload(bids=[["1.000", "100"], ["0.990", "200"]],
                              asks=[["1.010", "100"], ["1.020", "200"]])
    venue, calls = _venue_with_fakes(depths=[thin_100, deep_500, deep_500])
    small = await venue.quote(_identity(), "50")
    assert small.status == "OK"
    n_after_small = calls["depth"]
    again = await venue.quote(_identity(), "50")
    assert again.data.buy_vwap == small.data.buy_vwap
    assert calls["depth"] == n_after_small  # cache hit: no new send
    large = await venue.quote(_identity(), "250")
    assert large.status in ("OK", "PARTIAL")
    # Escalation happened within the 100/500/1000 budget.
    assert 500 in calls["depth_limits"]
    assert Decimal(large.data.buy_executable_qty) == Decimal("250")
    # A still-larger quantity cannot reuse the 250 quote.
    huge = await venue.quote(_identity(), "10000")
    assert huge.status == "PARTIAL" and huge.reason_code == ALPHA_BOOK_THIN
    assert Decimal(huge.data.buy_executable_qty) < Decimal("10000")


@pytest.mark.asyncio
async def test_alpha_illegal_filters_reject_each_kind():
    for filters in (
        [{k: v for k, v in f.items()} for f in _alpha_filters()[:1]]
        + [{"filterType": "LOT_SIZE", "minQty": "abc",
            "maxQty": "1000000", "stepSize": "1"}]
        + _alpha_filters()[2:],
        _alpha_filters()[:2]
        + [{"filterType": "MARKET_LOT_SIZE", "minQty": "10",
            "maxQty": "1", "stepSize": "1"}]
        + _alpha_filters()[3:],
        _alpha_filters()[:3]
        + [{"filterType": "MIN_NOTIONAL", "minNotional": "-5",
            "applyToMarket": True, "avgPriceMins": 5}],
        [{"filterType": "PRICE_FILTER", "minPrice": "0.001",
          "maxPrice": "1000", "tickSize": "abc"}]
        + _alpha_filters()[1:],
    ):
        venue, _ = _venue_with_fakes(exchange_filters=filters)
        result = await venue.quote(_identity(), "10")
        assert result.status == "ERROR"
        assert result.reason_code == TRADING_RULES_UNVERIFIED


@pytest.mark.asyncio
async def test_alpha_200_business_region_and_thin_are_honest():
    # HTTP-200 business failure (code != 000000) is an honest ERROR.
    venue_b, _ = _venue_with_fakes(
        exchange_payload={"code": "400001", "msg": "fail", "data": {}})
    res_b = await venue_b.quote(_identity(), "10")
    assert res_b.status == "ERROR" and res_b.reason_code == ALPHA_BUSINESS_ERROR
    # success=false is also a business failure.
    # Build the failing ticker envelope manually (success false).
    bad_ticker = {"code": "000000", "success": False,
                  "data": {"symbol": "ALPHA1USDT"}}
    venue_s2, _ = _venue_with_fakes(ticker=bad_ticker)
    res_s = await venue_s2.quote(_identity(), "10")
    assert res_s.status == "ERROR" and res_s.reason_code == ALPHA_BUSINESS_ERROR
    # Region blocks are UNAVAILABLE, never N/A.
    for code in (451, 403):
        venue_r, _ = _venue_with_fakes(ticker_err=_HttpErr(code, "blocked"))
        res_r = await venue_r.quote(_identity(), "10")
        assert res_r.status == "UNAVAILABLE"
        assert res_r.reason_code == VENUE_REGION_UNAVAILABLE
    # 429 keeps RATE_LIMITED.
    venue_429, _ = _venue_with_fakes(depth_err=_HttpErr(429, "slow down"))
    res_429 = await venue_429.quote(_identity(), "10")
    assert res_429.status == "UNAVAILABLE" and res_429.reason_code == "RATE_LIMITED"
    # Single-side thin book is PARTIAL with honest flags.
    thin = _depth_payload(bids=[], asks=[["1.010", "500"]])
    venue_t, _ = _venue_with_fakes(depths=[thin])
    res_t = await venue_t.quote(_identity(), "10")
    assert res_t.status == "PARTIAL" and res_t.reason_code == ALPHA_BOOK_THIN
    assert res_t.data.exit_feasible is False


@pytest.mark.asyncio
async def test_alpha_disabled_sends_zero_and_identity_verbatim():
    venue, calls = _venue_with_fakes(alpha_enabled=False)
    res = await venue.quote(_identity(), "10")
    assert res.status == "UNAVAILABLE" and res.reason_code == ALPHA_DISABLED
    assert calls == {"token": 0, "exchange": 0, "ticker": 0, "depth": 0,
                     "depth_limits": []}
    # Identity symbol used verbatim (tokenId never guessed as a symbol).
    venue2, calls2 = _venue_with_fakes(symbol="1000ALPHAUSDT",
                                       ticker=_ticker_payload("1000ALPHAUSDT"),
                                       depths=[_depth_payload()])
    identity = _identity(symbol="1000ALPHAUSDT", token_id="tok-1",
                         canonical="alpha1000")
    res2 = await venue2.quote(identity, "10")
    assert res2.status in ("OK", "PARTIAL")
    assert res2.data.symbol == "1000ALPHAUSDT"
    assert calls2["ticker_symbol"] == "1000ALPHAUSDT"
    assert calls2["depth_symbol"] == "1000ALPHAUSDT"
    # Confirmed-absent Alpha market is the N/A path.
    venue3, _ = _venue_with_fakes(
        exchange_payload=_envelope({"symbols": [_alpha_entry("OTHER")]}))
    res3 = await venue3.quote(_identity(symbol="MISSING"), "10")
    assert res3.status == "NOT_APPLICABLE"
