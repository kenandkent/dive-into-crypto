"""Task 7: OI and order-book notional unit assertions (Short-Lab Phase 1).

Contract (``ShortLab_Implementation_Plan_CN.md`` Task 7,
``ShortLab_Detailed_Design_CN.md`` §8.2):

- Raw Binance ``openInterestHist`` rows hold ``sumOpenInterest`` (quantity in
  base units, e.g. BTC) and ``sumOpenInterestValue`` (nominal in quote units,
  USDT on USDT-M). Crypcodile maps them to ``open_interest`` /
  ``open_interest_value``; only a quote-USDT nominal verified against
  ``sumOpenInterest × premiumIndex.markPrice`` (5% tolerance, mark response
  within 5 minutes of the OI point) may be exposed as ``oi_value_usd``.
  Anything else is ``None`` + ``OI_UNIT_UNVERIFIED`` — a base quantity must
  never pose as USD.
- ``book_panel()`` keeps the legacy bilateral ``notional_1pct`` and adds
  ``bid_notional_1pct`` / ``ask_notional_1pct`` (per-side ``Σ(price×qty)``
  inside ``mid ± 1%`` where ``mid = (best_bid + best_ask) / 2``).
  Tradeability consumes ``min(bid, ask)`` against the 0.25M/1M single-side
  thresholds; a missing side is ``None``, never zero.

All tests are offline with fixed fixtures; no live requests.
"""

from __future__ import annotations

import asyncio

import pytest
from crypcodile.exchanges.binance.backfill import parse_open_interest_hist

from diveintocrypto_desktop.data import open_interest as oi_mod
from diveintocrypto_desktop.data import orderbook as ob

BTC_TS_MS = 1_700_000_000_000
MARK_PRICE = 60_000.0
OI_QTY = 100.0  # BTC
OI_VALUE = OI_QTY * MARK_PRICE  # USDT nominal: 6_000_000


def _raw_oi_row(
    qty: float = OI_QTY,
    value: float = OI_VALUE,
    ts_ms: int = BTC_TS_MS,
    symbol: str = "BTCUSDT",
) -> dict:
    """Raw ``/futures/data/openInterestHist`` row (exchange field names)."""
    return {
        "symbol": symbol,
        "sumOpenInterest": f"{qty:.3f}",
        "sumOpenInterestValue": f"{value:.1f}",
        "timestamp": ts_ms,
    }


# ── OI: raw fixture vs parser units ──────────────────────────────────────────


def test_raw_oi_parser_separates_quantity_from_nominal_value():
    raw = [_raw_oi_row()]
    recs = parse_open_interest_hist(raw, "binance-usdm", "BTCUSDT", local_ts=0)
    assert len(recs) == 1
    rec = recs[0]
    # Quantity is base units (BTC), value is quote nominal (USDT) — different
    # units, different magnitudes; the price falls out of their ratio.
    assert rec.open_interest == pytest.approx(OI_QTY)
    assert rec.open_interest_value == pytest.approx(OI_VALUE)
    assert rec.open_interest != pytest.approx(rec.open_interest_value)
    assert rec.open_interest_value / rec.open_interest == pytest.approx(MARK_PRICE)


def test_fetch_oi_hist_preserves_parser_units(monkeypatch):
    raw = [_raw_oi_row(), _raw_oi_row(qty=101.0, value=101.0 * MARK_PRICE,
                                      ts_ms=BTC_TS_MS + 300_000)]

    async def fake_live(**kwargs):
        assert kwargs["symbol"] == "BTCUSDT"
        return raw

    monkeypatch.setattr(oi_mod, "_live_fetch_open_interest_hist", fake_live)
    points = asyncio.run(oi_mod.fetch_oi_hist("BTCUSDT", "5m", limit=2))
    assert [p["oi"] for p in points] == pytest.approx([100.0, 101.0])
    assert [p["oi_value"] for p in points] == pytest.approx(
        [6_000_000.0, 6_060_000.0]
    )


# ── OI: USD verification (design §8.2) ───────────────────────────────────────


def test_oi_value_usd_accepts_synced_usdt_nominal():
    res = oi_mod.resolve_oi_value_usd(
        OI_QTY, OI_VALUE,
        mark_price=MARK_PRICE,
        oi_time_ms=BTC_TS_MS,
        mark_time_ms=BTC_TS_MS + 60_000,  # 1 min later — within the 5-min window
        quote_unit="USDT",
    )
    assert res["oi_value_usd"] == pytest.approx(OI_VALUE)
    assert res["reason_code"] is None
    assert "sumOpenInterestValue" in res["oi_value_source"]


def test_oi_value_usd_accepts_ns_oi_timestamps():
    # fetch_oi_hist points carry t in ns; the resolver must handle both scales.
    res = oi_mod.resolve_oi_value_usd(
        OI_QTY, OI_VALUE,
        mark_price=MARK_PRICE,
        oi_time_ms=BTC_TS_MS * 1_000_000,  # ns scale
        mark_time_ms=BTC_TS_MS + 60_000,
        quote_unit="USDT",
    )
    assert res["oi_value_usd"] == pytest.approx(OI_VALUE)
    assert res["reason_code"] is None


def test_oi_value_usd_rejects_error_above_5pct():
    bad_value = OI_VALUE * 1.20  # 20% above oi_qty × mark
    res = oi_mod.resolve_oi_value_usd(
        OI_QTY, bad_value,
        mark_price=MARK_PRICE,
        oi_time_ms=BTC_TS_MS,
        mark_time_ms=BTC_TS_MS + 60_000,
        quote_unit="USDT",
    )
    assert res["oi_value_usd"] is None
    assert res["reason_code"] == oi_mod.OI_UNIT_UNVERIFIED
    # Design §8.2: abs(value - qty×mark)/max(value,1); 1.20× → 0.20/1.20.
    assert res["relative_error"] == pytest.approx(0.20 / 1.20, abs=1e-9)


def test_oi_value_usd_boundary_tolerance():
    ok_value = OI_VALUE * 1.04  # 0.04/1.04 ≈ 0.038 < 0.05 → pass
    res_ok = oi_mod.resolve_oi_value_usd(
        OI_QTY, ok_value, mark_price=MARK_PRICE,
        oi_time_ms=BTC_TS_MS, mark_time_ms=BTC_TS_MS, quote_unit="USDT",
    )
    assert res_ok["reason_code"] is None
    bad_value = OI_VALUE * 1.10  # 0.10/1.10 ≈ 0.091 > 0.05 → fail
    res_bad = oi_mod.resolve_oi_value_usd(
        OI_QTY, bad_value, mark_price=MARK_PRICE,
        oi_time_ms=BTC_TS_MS, mark_time_ms=BTC_TS_MS, quote_unit="USDT",
    )
    assert res_bad["oi_value_usd"] is None
    assert res_bad["reason_code"] == oi_mod.OI_UNIT_UNVERIFIED


def test_oi_value_usd_rejects_stale_mark():
    res = oi_mod.resolve_oi_value_usd(
        OI_QTY, OI_VALUE,
        mark_price=MARK_PRICE,
        oi_time_ms=BTC_TS_MS,
        mark_time_ms=BTC_TS_MS + 6 * 60_000,  # 6 min — beyond the window
        quote_unit="USDT",
    )
    assert res["oi_value_usd"] is None
    assert res["reason_code"] == oi_mod.OI_UNIT_UNVERIFIED


def test_oi_value_usd_rejects_missing_sync_time():
    for kwargs in (
        {"oi_time_ms": None, "mark_time_ms": BTC_TS_MS},
        {"oi_time_ms": BTC_TS_MS, "mark_time_ms": None},
    ):
        res = oi_mod.resolve_oi_value_usd(
            OI_QTY, OI_VALUE, mark_price=MARK_PRICE,
            quote_unit="USDT", **kwargs,
        )
        assert res["oi_value_usd"] is None
        assert res["reason_code"] == oi_mod.OI_UNIT_UNVERIFIED


def test_oi_value_usd_rejects_missing_mark_price():
    res = oi_mod.resolve_oi_value_usd(
        OI_QTY, OI_VALUE, mark_price=None,
        oi_time_ms=BTC_TS_MS, mark_time_ms=BTC_TS_MS, quote_unit="USDT",
    )
    assert res["oi_value_usd"] is None
    assert res["reason_code"] == oi_mod.OI_UNIT_UNVERIFIED


def test_oi_value_usd_unknown_unit_is_null():
    for unit in (None, "", "UNKNOWN", "unverified"):
        res = oi_mod.resolve_oi_value_usd(
            OI_QTY, OI_VALUE, mark_price=MARK_PRICE,
            oi_time_ms=BTC_TS_MS, mark_time_ms=BTC_TS_MS, quote_unit=unit,
        )
        assert res["oi_value_usd"] is None, unit
        assert res["reason_code"] == oi_mod.OI_UNIT_UNVERIFIED


def test_oi_value_usd_non_usd_unit_converts_explicitly():
    # A base-denominated nominal (e.g. BTC) is converted via the sync mark
    # and the source records the conversion path.
    res = oi_mod.resolve_oi_value_usd(
        OI_QTY, OI_QTY,  # oi_value quoted in BTC, numerically == quantity
        mark_price=MARK_PRICE,
        oi_time_ms=BTC_TS_MS, mark_time_ms=BTC_TS_MS, quote_unit="BTC",
    )
    assert res["oi_value_usd"] == pytest.approx(OI_VALUE)
    assert res["reason_code"] is None
    assert "BTC" in res["oi_value_source"]
    assert "markPrice" in res["oi_value_source"]


def test_oi_value_usd_alias_matches_resolver():
    assert oi_mod.oi_value_usd is oi_mod.resolve_oi_value_usd


def test_enrich_oi_hist_with_usd_keeps_point_times():
    points = [
        {"t": BTC_TS_MS * 1_000_000, "oi": OI_QTY, "oi_value": OI_VALUE},
        {"t": (BTC_TS_MS + 300_000) * 1_000_000, "oi": 101.0,
         "oi_value": 101.0 * MARK_PRICE},
    ]
    enriched = oi_mod.enrich_oi_hist_with_usd(
        points, mark_price=MARK_PRICE, mark_time_ms=BTC_TS_MS + 60_000,
    )
    assert [r["t"] for r in enriched] == [p["t"] for p in points]
    assert [r["oi_value_usd"] for r in enriched] == pytest.approx(
        [OI_VALUE, 101.0 * MARK_PRICE]
    )
    assert all(r["reason_code"] is None for r in enriched)


def test_enrich_oi_hist_with_usd_marks_failed_points():
    points = [{"t": BTC_TS_MS * 1_000_000, "oi": OI_QTY,
               "oi_value": OI_VALUE * 1.5}]
    enriched = oi_mod.enrich_oi_hist_with_usd(
        points, mark_price=MARK_PRICE, mark_time_ms=BTC_TS_MS,
    )
    assert enriched[0]["oi_value_usd"] is None
    assert enriched[0]["reason_code"] == oi_mod.OI_UNIT_UNVERIFIED


# ── order book: bilateral 1% notionals ───────────────────────────────────────


def _asymmetric_book():
    # mid = (100.0 + 100.2) / 2 = 100.1; every level below sits inside ±1%.
    bids = [(100.0, 200.0), (99.9, 100.0)]   # 20000 + 9990 = 29990
    asks = [(100.2, 1.0), (100.3, 2.0)]      # 100.2 + 200.6 = 300.8
    return bids, asks


def test_book_panel_adds_bilateral_1pct_and_keeps_legacy_total():
    bids, asks = _asymmetric_book()
    panel = ob.book_panel(bids, asks, now_ms=BTC_TS_MS)
    bid_exp = 100.0 * 200.0 + 99.9 * 100.0
    ask_exp = 100.2 * 1.0 + 100.3 * 2.0
    assert panel["bid_notional_1pct"] == pytest.approx(round(bid_exp, 2))
    assert panel["ask_notional_1pct"] == pytest.approx(round(ask_exp, 2))
    # Legacy bilateral total is unchanged: bid + ask.
    assert panel["notional_1pct"] == pytest.approx(round(bid_exp + ask_exp, 2))
    assert panel["mid"] == pytest.approx((100.0 + 100.2) / 2.0)


def test_book_asymmetric_min_differs_from_legacy_total():
    bids, asks = _asymmetric_book()
    panel = ob.book_panel(bids, asks, now_ms=BTC_TS_MS)
    depth_min = ob.book_depth_min_1pct(panel)
    assert depth_min == pytest.approx(panel["ask_notional_1pct"])
    assert depth_min != pytest.approx(panel["notional_1pct"])
    assert panel["bid_notional_1pct"] != pytest.approx(panel["ask_notional_1pct"])


def test_book_panel_truncates_at_mid_plus_minus_1pct():
    bids = [(100.0, 50.0), (98.0, 100.0)]   # 98.0 falls below mid×0.99
    asks = [(100.2, 50.0), (102.0, 100.0)]  # 102.0 sits above mid×1.01
    panel = ob.book_panel(bids, asks, now_ms=BTC_TS_MS)
    mid = (100.0 + 100.2) / 2.0
    assert panel["mid"] == pytest.approx(mid)
    assert panel["bid_notional_1pct"] == pytest.approx(round(100.0 * 50.0, 2))
    assert panel["ask_notional_1pct"] == pytest.approx(round(100.2 * 50.0, 2))
    assert panel["notional_1pct"] == pytest.approx(
        round(100.0 * 50.0 + 100.2 * 50.0, 2)
    )


def test_book_depth_min_needs_both_sides():
    assert ob.book_depth_min_1pct(
        {"bid_notional_1pct": 300.0, "ask_notional_1pct": 100.0}
    ) == pytest.approx(100.0)
    assert ob.book_depth_min_1pct(
        {"bid_notional_1pct": 300.0, "ask_notional_1pct": None}
    ) is None
    assert ob.book_depth_min_1pct(
        {"bid_notional_1pct": None, "ask_notional_1pct": 100.0}
    ) is None
    assert ob.book_depth_min_1pct({"unavailable": "book_too_thin"}) is None


def test_book_panel_empty_side_is_unavailable():
    bids, asks = _asymmetric_book()
    assert ob.book_panel([], asks) == {"unavailable": "book_too_thin"}
    assert ob.book_panel(bids, []) == {"unavailable": "book_too_thin"}
    assert ob.book_panel([], []) == {"unavailable": "book_too_thin"}
    # Scoring input for a missing side is null, never zero.
    assert ob.book_depth_min_1pct(ob.book_panel([], asks)) is None
    assert ob.book_depth_min_1pct(ob.book_panel(bids, [])) is None
