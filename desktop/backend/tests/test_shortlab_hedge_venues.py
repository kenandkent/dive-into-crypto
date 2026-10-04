"""H02 venues: Spot dual quote + unified rules (plan H02.1-H02.3, AC12/AC13).

Covers the H02 minimal scenes with fake HTTP fixtures only (no network):

- same-quantity buy/sell dual quote; a different quantity never reuses a
  small-amount quote (cache key = venue/identity/rules/quantity);
- LOT_SIZE / MARKET_LOT_SIZE / MIN_NOTIONAL / PRICE_FILTER illegal rejects
  (LIMIT/MARKET checked separately; precision never substitutes tick/step);
- Spot 200-shape business failure, region 403/451, 429 and single-side thin
  book honesty (451/403/429 never N/A);
- 1000-denomination Spot symbol comes from identity verbatim (no prefix
  strip, no futures-symbol guess); level VWAP (never 1%-total inference).

Raw filter shapes for Futures/Spot/Alpha share `data/trading_rules.py`.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from decimal import Decimal

from diveintocrypto_desktop.data import trading_rules as rules_mod
from diveintocrypto_desktop.shortlab.hedge.venues import base as base_mod
from diveintocrypto_desktop.shortlab.hedge.venues.binance_spot import (
    BinanceSpotVenue,
    NO_SPOT_MARKET,
    SPOT_BOOK_THIN,
    TRADING_RULES_UNVERIFIED,
    VENUE_REGION_UNAVAILABLE,
)
from diveintocrypto_desktop.shortlab.models import AssetIdentity


NOW = 1_760_000_000_000


def _spot_filters(*, min_qty="0.001", max_qty="1000", step="0.001",
                  min_price="0.01", max_price="1000000", tick="0.01",
                  min_notional="10") -> list[dict]:
    return [
        {"filterType": "PRICE_FILTER", "minPrice": min_price,
         "maxPrice": max_price, "tickSize": tick},
        {"filterType": "LOT_SIZE", "minQty": min_qty,
         "maxQty": max_qty, "stepSize": step},
        {"filterType": "MARKET_LOT_SIZE", "minQty": min_qty,
         "maxQty": max_qty, "stepSize": step},
        {"filterType": "MIN_NOTIONAL", "minNotional": min_notional,
         "applyToMarket": True, "avgPriceMins": 5},
    ]


def _spot_entry(symbol="BTCUSDT", filters=None, order_types=None) -> dict:
    return {
        "symbol": symbol,
        "status": "TRADING",
        "baseAsset": symbol.replace("USDT", ""),
        "quoteAsset": "USDT",
        "orderTypes": order_types if order_types is not None else ["LIMIT", "MARKET"],
        "filters": filters if filters is not None else _spot_filters(),
    }


def _ident(spot: str | None = "BTCUSDT", futures: str = "BTCUSDT",
           canonical: str = "bitcoin", confidence: str = "VERIFIED") -> AssetIdentity:
    return AssetIdentity(
        canonical_id=canonical,
        display_symbol=canonical,
        binance_futures_symbol=futures,
        binance_spot_symbol=spot,
        mapping_confidence=confidence,  # type: ignore[arg-type]
        mapping_source="UNIQUE_SYMBOL",  # type: ignore[arg-type]
    )


def _book(*, bid="66990.00", ask="67000.00") -> dict:
    return {"symbol": "BTCUSDT", "bidPrice": bid, "askPrice": ask}


def _depth(*, bids=None, asks=None) -> dict:
    if bids is None:
        bids = [["66990.00", "0.100"], ["66980.00", "0.200"], ["66970.00", "0.300"]]
    if asks is None:
        asks = [["67000.00", "0.100"], ["67010.00", "0.200"], ["67020.00", "0.300"]]
    return {"lastUpdateId": 12345, "bids": bids, "asks": asks}


class _HttpErr(Exception):
    def __init__(self, status: int, message: str = "http error",
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _venue(*, entry=None, book=None, depth=None, entry_err=None,
           book_err=None, depth_err=None, now=NOW):
    calls = {"entry": 0, "book": 0, "depth": 0, "depth_limits": []}

    async def entry_fn(symbol, _ctx=None):
        calls["entry"] += 1
        calls["entry_symbol"] = symbol
        if entry_err is not None:
            raise entry_err
        return entry if entry is not None else _spot_entry(symbol)

    async def book_fn(symbol, _ctx=None):
        calls["book"] += 1
        calls["book_symbol"] = symbol
        if book_err is not None:
            raise book_err
        return dict(book if book is not None else _book())

    async def depth_fn(symbol, limit, _ctx=None):
        calls["depth"] += 1
        calls["depth_limits"].append(int(limit))
        calls["depth_symbol"] = symbol
        if depth_err is not None:
            raise depth_err
        payload = depth if depth is not None else _depth()
        # Return fresh copies so callers cannot mutate the fixture.
        return {"lastUpdateId": payload.get("lastUpdateId", 1),
                "bids": [list(r) for r in payload["bids"]],
                "asks": [list(r) for r in payload["asks"]]}

    venue = BinanceSpotVenue(
        config=None,
        exchange_entry_fn=entry_fn,
        book_ticker_fn=book_fn,
        depth_fn=depth_fn,
        now_ms_fn=lambda: now,
    )
    return venue, calls


# ---------------------------------------------------------------------------
# trading_rules: three shapes share one parser; precision ban; 0 = disabled
# ---------------------------------------------------------------------------

def test_three_filter_shapes_share_parser():
    spot_raw = _spot_entry("BTCUSDT")
    snap = rules_mod.parse_trading_rules(
        spot_raw, "BINANCE_SPOT", "BTCUSDT",
        {"source_as_of_ms": None, "known_at_ms": NOW})
    assert snap.order_types == {"LIMIT": True, "MARKET": True}
    assert snap.price_rules["tick_size"] == "0.01"
    assert snap.lot_rules["step_size"] == "0.001"
    assert snap.lot_rules["market_step_size"] == "0.001"
    assert snap.notional_rules["min_notional"] == "10"
    # Futures-shaped raw (NOTIONAL + PERCENT_PRICE) through the same parser.
    fut_raw = {
        "symbol": "BTCUSDT", "status": "TRADING",
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.10",
             "maxPrice": "100000", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "minQty": "0.001",
             "maxQty": "1000", "stepSize": "0.001"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.002",
             "maxQty": "500", "stepSize": "0.002"},
            {"filterType": "NOTIONAL", "minNotional": "20", "maxNotional": "0",
             "applyToMarket": True},
            {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05",
             "multiplierDown": "0.95", "avgPriceMins": 5},
        ],
    }
    fut = rules_mod.parse_trading_rules(
        fut_raw, "BINANCE_SPOT", "BTCUSDT", {"known_at_ms": NOW})
    assert fut.order_types == {"LIMIT": True, "MARKET": True}
    assert fut.lot_rules["market_min_qty"] == "0.002"
    assert fut.price_rules["multiplier_up"] == "1.05"
    # Alpha-shaped raw (same filterTypes, Alpha venue label).
    alpha_raw = _spot_entry("ALPHA1USDT")
    alpha = rules_mod.parse_trading_rules(
        alpha_raw, "BINANCE_ALPHA", "ALPHA1USDT", {"known_at_ms": NOW})
    assert alpha.order_types == {"LIMIT": True, "MARKET": True}


def test_precision_never_substitutes_tick_step():
    # Only precisions, no filters list content: LIMIT/MARKET unsupported.
    raw = {"symbol": "BTCUSDT", "status": "TRADING",
           "pricePrecision": 2, "quantityPrecision": 3,
           "baseAssetPrecision": 8, "quotePrecision": 8,
           "orderTypes": ["LIMIT", "MARKET"],
           "filters": []}
    snap = rules_mod.parse_trading_rules(
        raw, "BINANCE_SPOT", "BTCUSDT", {"known_at_ms": NOW})
    assert snap.order_types == {"LIMIT": False, "MARKET": False}
    assert snap.price_rules == {}
    assert snap.lot_rules == {}
    # Missing filters list entirely is illegal (precision ban fails loud).
    with pytest.raises(rules_mod.TradingRulesError):
        rules_mod.parse_trading_rules(
            {"symbol": "BTCUSDT", "pricePrecision": 2},
            "BINANCE_SPOT", "BTCUSDT", {"known_at_ms": NOW})


def test_zero_step_is_disabled_not_legal_step():
    raw = _spot_entry("BTCUSDT", filters=[
        {"filterType": "PRICE_FILTER", "minPrice": "0.00",
         "maxPrice": "0.00", "tickSize": "0.00"},
        {"filterType": "LOT_SIZE", "minQty": "0.00",
         "maxQty": "0.00", "stepSize": "0.00"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "0.00",
         "applyToMarket": True, "avgPriceMins": 5},
    ])
    snap = rules_mod.parse_trading_rules(
        raw, "BINANCE_SPOT", "BTCUSDT", {"known_at_ms": NOW})
    # Disabled bounds impose no constraint: any positive qty/price passes.
    ok, _ = rules_mod.validate_qty(snap, "123456.789", "LIMIT")
    assert ok is True
    ok, _ = rules_mod.validate_price(snap, "99999.99", "LIMIT")
    assert ok is True
    ok, _ = rules_mod.validate_notional(snap, "0.01", "LIMIT")
    assert ok is True


@pytest.mark.parametrize("case", ["lot", "market_lot", "min_notional", "price"])
def test_illegal_filters_rejected(case):
    if case == "lot":
        filters = _spot_filters()
        filters[1] = {"filterType": "LOT_SIZE", "minQty": "abc",
                      "maxQty": "1000", "stepSize": "0.001"}
    elif case == "market_lot":
        filters = _spot_filters()
        filters[2] = {"filterType": "MARKET_LOT_SIZE", "minQty": "10",
                      "maxQty": "1", "stepSize": "0.001"}
    elif case == "min_notional":
        filters = _spot_filters()
        filters[3] = {"filterType": "MIN_NOTIONAL", "minNotional": "-5",
                      "applyToMarket": True, "avgPriceMins": 5}
    else:
        filters = _spot_filters(tick="abc")
    with pytest.raises(rules_mod.TradingRulesError):
        rules_mod.parse_trading_rules(
            _spot_entry("BTCUSDT", filters=filters),
            "BINANCE_SPOT", "BTCUSDT", {"known_at_ms": NOW})


def test_limit_market_checked_separately():
    snap = rules_mod.parse_trading_rules(
        _spot_entry("BTCUSDT"), "BINANCE_SPOT", "BTCUSDT", {"known_at_ms": NOW})
    # Step is 0.001: 0.0015 fails both LIMIT and MARKET.
    ok_limit, _ = rules_mod.validate_qty(snap, "0.0015", "LIMIT")
    ok_market, _ = rules_mod.validate_qty(snap, "0.0015", "MARKET")
    assert ok_limit is False and ok_market is False
    # Unknown order type is unsupported (TRADING_RULES_UNVERIFIED downstream).
    ok, reason = rules_mod.validate_qty(snap, "0.01", "STOP")
    assert ok is False and rules_mod.TRADING_RULES_UNVERIFIED in reason


def test_rules_cache_keeps_version():
    cache = rules_mod.TradingRulesCache()
    snap = rules_mod.parse_trading_rules(
        _spot_entry("BTCUSDT"), "BINANCE_SPOT", "BTCUSDT",
        {"known_at_ms": NOW, "rule_version": "rules-v1-test"})
    cache.put(snap)
    hit = cache.get("BINANCE_SPOT", "BTCUSDT")
    assert hit is not None and hit.rule_version == "rules-v1-test"
    assert hit.known_at_ms == NOW
    assert cache.get("BINANCE_SPOT", "OTHER") is None


# ---------------------------------------------------------------------------
# base: DepthSnapshot strings + level VWAP + quantity-keyed cache
# ---------------------------------------------------------------------------

def test_depth_snapshot_keeps_strings_and_source_interval():
    snap = base_mod.parse_depth_snapshot(
        venue="BINANCE_SPOT", instrument_id="BTCUSDT",
        raw_bids=[["66990.00", "0.100"]], raw_asks=[["67000.00", "0.100"]],
        source="binance-spot-depth", source_timestamp_ms=None,
        fetched_at_ms=NOW, received_at_ms=NOW + 5)
    assert snap.bids[0] == ("66990.00", "0.100")
    assert snap.asks[0] == ("67000.00", "0.100")
    assert snap.source == "binance-spot-depth"
    assert snap.fetched_at_ms == NOW and snap.received_at_ms == NOW + 5


def test_level_vwap_never_from_1pct_total():
    # 3 asks: VWAP(2.0) = (100*1 + 101*1)/2 = 100.5 -- a 1% total cannot
    # produce this without walking levels.
    levels = (("100.00", "1"), ("101.00", "1"), ("102.00", "1"))
    vwap, executable = base_mod.vwap_for_qty(levels, Decimal("2"))
    assert executable == Decimal("2")
    assert vwap == Decimal("100.50")
    # Insufficient depth returns the available prefix honestly.
    vwap2, executable2 = base_mod.vwap_for_qty(levels, Decimal("10"))
    assert executable2 == Decimal("3")
    assert vwap2 == Decimal("101.00")
    # Empty side: no fabrication.
    vwap3, executable3 = base_mod.vwap_for_qty((), Decimal("1"))
    assert executable3 == Decimal("0") and vwap3 is None


def test_quote_cache_key_includes_quantity():
    k_small = base_mod.build_quote_cache_key(
        "BINANCE_SPOT", "bitcoin", "BTCUSDT", "rules-v1-x", "0.200")
    k_large = base_mod.build_quote_cache_key(
        "BINANCE_SPOT", "bitcoin", "BTCUSDT", "rules-v1-x", "10.000")
    assert k_small != k_large
    k_same = base_mod.build_quote_cache_key(
        "BINANCE_SPOT", "bitcoin", "BTCUSDT", "rules-v1-x", "0.2")
    assert k_same == k_small  # Decimal-normalised, not string-compared
    k_other_rules = base_mod.build_quote_cache_key(
        "BINANCE_SPOT", "bitcoin", "BTCUSDT", "rules-v1-y", "0.200")
    assert k_other_rules != k_small


# ---------------------------------------------------------------------------
# Spot venue: same-quantity dual quote
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_same_quantity_dual_quote_with_level_vwap():
    venue, _ = _venue()
    result = await venue.quote(_ident(), "0.200")
    assert result.status == "OK"
    assert result.reason_code is None
    quote = result.data
    # Same net quantity both sides (buy target first, sell same net).
    assert quote.requested_canonical_qty == "0.200"
    assert Decimal(quote.buy_executable_qty) == Decimal("0.2")
    assert Decimal(quote.sell_executable_qty) == Decimal("0.2")
    assert quote.entry_feasible is True and quote.exit_feasible is True
    assert quote.exit_feasibility == "CONFIRMED"
    # Level VWAP: buy (67000*0.1 + 67010*0.1)/0.2 = 67005 (asks);
    # sell (66990*0.1 + 66980*0.1)/0.2 = 66985 (bids).
    assert Decimal(quote.buy_vwap) == Decimal("67005.00")
    assert Decimal(quote.sell_vwap) == Decimal("66985.00")
    # Mid from best bid/ask, Decimal strings everywhere.
    assert Decimal(quote.mid_price) == Decimal("66995.00")
    Decimal(quote.reference_notional_usd)
    Decimal(quote.estimated_fee_usd)
    assert quote.symbol == "BTCUSDT"  # venue instrument ID, not canonical
    assert quote.venue == "BINANCE_SPOT"
    assert quote.trading_rules["rule_version"].startswith("rules-v1-")
    assert quote.capabilities["depth_limit"] == 100


@pytest.mark.asyncio
async def test_spot_different_quantity_never_reuses_small_quote():
    venue, calls = _venue()
    small = await venue.quote(_ident(), "0.200")
    assert small.status == "OK"
    depth_calls_after_small = calls["depth"]
    # Same quantity hits the cache: no new depth send.
    again = await venue.quote(_ident(), "0.200")
    assert again.data.buy_vwap == small.data.buy_vwap
    assert calls["depth"] == depth_calls_after_small
    # Larger quantity (10.0 > 0.6 total depth) cannot reuse the small quote.
    large = await venue.quote(_ident(), "10.000")
    assert large.status == "PARTIAL"
    assert large.reason_code == SPOT_BOOK_THIN
    assert Decimal(large.data.buy_executable_qty) < Decimal("10.000")
    assert large.data.exit_feasibility in ("PARTIAL", "NO")
    assert large.data.entry_feasible is False or large.data.exit_feasible is False
    assert calls["depth"] > depth_calls_after_small


@pytest.mark.asyncio
async def test_spot_illegal_filters_reject_each_kind():
    # LOT_SIZE illegal (non-decimal step).
    bad_lot = _spot_filters()
    bad_lot[1] = {"filterType": "LOT_SIZE", "minQty": "0.001",
                  "maxQty": "1000", "stepSize": "abc"}
    venue, _ = _venue(entry=_spot_entry("BTCUSDT", filters=bad_lot))
    result = await venue.quote(_ident(), "0.010")
    assert result.status == "ERROR" and result.reason_code == TRADING_RULES_UNVERIFIED
    # MARKET_LOT_SIZE illegal (min > max).
    bad_market = _spot_filters()
    bad_market[2] = {"filterType": "MARKET_LOT_SIZE", "minQty": "10",
                     "maxQty": "1", "stepSize": "0.001"}
    venue2, _ = _venue(entry=_spot_entry("BTCUSDT", filters=bad_market))
    result2 = await venue2.quote(_ident(), "0.010")
    assert result2.status == "ERROR" and result2.reason_code == TRADING_RULES_UNVERIFIED
    # MIN_NOTIONAL illegal (negative).
    bad_notional = _spot_filters()
    bad_notional[3] = {"filterType": "MIN_NOTIONAL", "minNotional": "-5",
                       "applyToMarket": True, "avgPriceMins": 5}
    venue3, _ = _venue(entry=_spot_entry("BTCUSDT", filters=bad_notional))
    result3 = await venue3.quote(_ident(), "0.010")
    assert result3.status == "ERROR" and result3.reason_code == TRADING_RULES_UNVERIFIED
    # PRICE_FILTER illegal (bad tick).
    bad_price = _spot_filters(tick="abc")
    venue4, _ = _venue(entry=_spot_entry("BTCUSDT", filters=bad_price))
    result4 = await venue4.quote(_ident(), "0.010")
    assert result4.status == "ERROR" and result4.reason_code == TRADING_RULES_UNVERIFIED
    # Quantity violating step is rejected even with legal filters.
    venue5, _ = _venue()
    result5 = await venue5.quote(_ident(), "0.0015")
    assert result5.status == "ERROR" and result5.reason_code == TRADING_RULES_UNVERIFIED


@pytest.mark.asyncio
async def test_spot_200_shape_failure_region_and_thin_are_honest():
    # 200-shape failure: bookTicker without bid/ask is an honest ERROR.
    venue, _ = _venue(book={})
    result = await venue.quote(_ident(), "0.010")
    assert result.status == "ERROR" and result.data is None
    # Region blocks are UNAVAILABLE, never N/A.
    for status_code in (451, 403):
        venue_r, _ = _venue(depth_err=_HttpErr(status_code, "geo blocked"))
        res_r = await venue_r.quote(_ident(), "0.010")
        assert res_r.status == "UNAVAILABLE"
        assert res_r.reason_code == VENUE_REGION_UNAVAILABLE
        assert res_r.status != "NOT_APPLICABLE"
    # 429 keeps RATE_LIMITED (Retry-After preserved in the transport).
    venue_429, _ = _venue(depth_err=_HttpErr(429, "too many", retry_after=2.0))
    res_429 = await venue_429.quote(_ident(), "0.010")
    assert res_429.status == "UNAVAILABLE" and res_429.reason_code == "RATE_LIMITED"
    # Single-side thin book: PARTIAL with honest feasible flags.
    thin = _depth(bids=[], asks=[["67000.00", "1.000"]])
    venue_t, _ = _venue(depth=thin)
    res_t = await venue_t.quote(_ident(), "0.010")
    assert res_t.status == "PARTIAL" and res_t.reason_code == SPOT_BOOK_THIN
    assert res_t.data.exit_feasible is False
    assert res_t.data.exit_feasibility in ("PARTIAL", "NO")
    # Confirmed-absent symbol is the ONLY N/A path here.
    venue_na, _ = _venue(entry=None, book=None, depth=None,
                         entry_err=None)
    async def _absent_entry(symbol, _ctx=None):
        return None
    venue_na._exchange_entry_fn = _absent_entry
    res_na = await venue_na.quote(_ident(), "0.010")
    assert res_na.status == "NOT_APPLICABLE" and res_na.reason_code == NO_SPOT_MARKET


@pytest.mark.asyncio
async def test_spot_1000_symbol_uses_identity_verbatim():
    filters_1000 = [
        {"filterType": "PRICE_FILTER", "minPrice": "0.00001",
         "maxPrice": "1000", "tickSize": "0.00001"},
        {"filterType": "LOT_SIZE", "minQty": "1",
         "maxQty": "100000000", "stepSize": "1"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "1",
         "maxQty": "100000000", "stepSize": "1"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "5",
         "applyToMarket": True, "avgPriceMins": 5},
    ]
    entry_1000 = _spot_entry("1000PEPEUSDT", filters=filters_1000)
    venue, calls = _venue(entry=entry_1000,
                           book={"symbol": "1000PEPEUSDT",
                                 "bidPrice": "0.01234", "askPrice": "0.01236"},
                           depth={"lastUpdateId": 7,
                                  "bids": [["0.01234", "1000"], ["0.01230", "2000"]],
                                  "asks": [["0.01236", "1000"], ["0.01240", "2000"]]})
    identity = _ident(spot="1000PEPEUSDT", futures="1000PEPEUSDT",
                      canonical="pepe", confidence="VERIFIED")
    result = await venue.quote(identity, "500")
    assert result.status in ("OK", "PARTIAL")
    assert result.data is not None
    # Venue instrument ID is the identity symbol verbatim (no 1000 strip).
    assert result.data.symbol == "1000PEPEUSDT"
    assert calls.get("entry_symbol") == "1000PEPEUSDT"
    assert calls.get("book_symbol") == "1000PEPEUSDT"
    assert calls.get("depth_symbol") == "1000PEPEUSDT"
    assert result.data.symbol != "PEPEUSDT"
    # Missing identity symbol is N/A (futures symbol never guessed).
    venue2, _ = _venue()
    res2 = await venue2.quote(_ident(spot=None), "0.010")
    assert res2.status == "NOT_APPLICABLE" and res2.reason_code == NO_SPOT_MARKET
