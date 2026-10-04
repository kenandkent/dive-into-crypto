"""F02: observation wrapper, units, rule text and UTC decision windows.

Contract (ShortLab_Integrated_Implementation_Plan_CN.md F02, AC02/AC03;
design A4.1/A4.2/A4.3, B16.1):

- ``shortlab/observations.py`` freezes ``ObservationMeta`` / ``Observed[T]``
  (``Observed.value`` is the legacy return object); ``to_legacy`` returns the
  identical object/type; ``validate_observation(observed, cutoff_ms,
  max_future_skew_sec=2)`` rejects a future ``known_at`` (CLOCK_SKEW, no
  negative-age-to-zero freshness fakery); ``canonical_price`` /
  ``canonical_qty`` use Decimal (price ``native/multiplier*fx``, qty
  ``native*multiplier``; nominal OI/qv are never scaled).
- Only the Funding/Kline/OI/Spot chains are wrapped (plus the universe
  exchangeInfo raw store). ``known_at`` is the response-completion time;
  cache hits keep the original time; unknown source time stays null. The
  raw exchangeInfo filters/time are stored verbatim -- no second parser is
  built here. ``qv`` reads raw index 7, aligns to closed UTC days, and a
  missing day is never filled with 0. OI 7D window ends must agree with the
  price window within 5 minutes.
- Legacy data-function signatures and return types are unchanged.

All tests are offline with faked transports; no live requests.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from decimal import Decimal

import pytest

from diveintocrypto_desktop.shortlab import observations as obs

DAY_MS = 86_400_000
T0 = 1_700_000_000_000  # fixed decision cutoff base (ms)


def _meta(known_at_ms=T0, **over) -> obs.ObservationMeta:
    base = dict(
        status="OK",
        source="unit-test",
        source_schema_version="observations-v1",
        reason_code=None,
        source_as_of_ms=T0 - 60_000,
        fetched_at_ms=T0,
        known_at_ms=known_at_ms,
        window_start_ms=None,
        window_end_ms=None,
        complete=None,
        coverage_fraction=None,
        units=None,
        identity_snapshot_id=None,
        raw_snapshot_id=None,
    )
    base.update(over)
    return obs.ObservationMeta(**base)


# ── F02.2 units: Decimal canonical price/qty; nominals never scaled ──────────


def test_canonical_price_native_drawdown_half():
    # native .006 with a 1000x multiplier vs ATH .000012 -> -0.5 drawdown.
    canon = obs.canonical_price(Decimal("0.006"), Decimal("1000"), Decimal("1"))
    assert isinstance(canon, Decimal)
    assert canon == Decimal("0.000006")
    ath = Decimal("0.000012")
    drawdown = (canon - ath) / ath
    assert drawdown == Decimal("-0.5")


def test_canonical_price_applies_fx_in_decimal():
    assert obs.canonical_price(Decimal("100"), Decimal("1"), Decimal("0.5")) == Decimal("50")
    # float inputs are accepted but the result stays Decimal.
    out = obs.canonical_price(0.006, 1000.0, 1.0)
    assert isinstance(out, Decimal)
    assert out == Decimal("0.000006")


def test_canonical_qty_scales_native_not_nominal():
    assert obs.canonical_qty(Decimal("2"), Decimal("1000")) == Decimal("2000")
    # Nominal OI/qv legs must never be routed through the multiplier: the
    # helpers return None for an unknown multiplier instead of guessing.
    assert obs.canonical_price(Decimal("0.006"), None, Decimal("1")) is None
    assert obs.canonical_price(Decimal("0.006"), Decimal("0"), Decimal("1")) is None
    assert obs.canonical_qty(Decimal("2"), None) is None
    assert obs.canonical_qty(None, Decimal("1000")) is None


def test_unknown_dimension_returns_null_with_reason():
    res = obs.resolve_unit_or_null(None, expected="USDT")
    assert res["value"] is None
    assert res["reason_code"] == obs.UNIT_UNKNOWN
    res2 = obs.resolve_unit_or_null(5.0, expected="USDT", actual="MISMATCH")
    assert res2["value"] is None
    assert res2["reason_code"] == obs.UNIT_UNKNOWN
    ok = obs.resolve_unit_or_null(5.0, expected="USDT", actual="USDT")
    assert ok["value"] == 5.0
    assert ok["reason_code"] is None


# ── raw index5 vs 7, duplicate / missing / unclosed days ─────────────────────


def test_kline_raw_index5_and_7_differ_and_qv_uses_7():
    from diveintocrypto_desktop.data import binance_klines as kl

    row = [T0, "100", "110", "90", "105", "1000.0", T0 + DAY_MS - 1,
           "105000.0", 10, "500", "52500", "0"]
    assert float(row[5]) != float(row[7])
    by_ms, _ = kl._quote_volume_by_open_ms([row])
    assert by_ms[T0] == pytest.approx(105000.0)
    assert by_ms[T0] != pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_kline_observed_unclosed_excluded_and_missing_day_not_zero():
    from diveintocrypto_desktop.data import binance_klines as kl

    opens = [T0 + i * DAY_MS for i in range(4)]

    def _row(o, qv):
        return [o, "100", "110", "90", "105", "1000.0", o + DAY_MS - 1,
                str(qv), 10, "500", "52500", "0"]

    raw = [_row(o, 100_000.0 + i) for i, o in enumerate(opens)]
    # End exactly at the last open: that candle is still in progress.
    end_ms = opens[-1]

    async def fake_fetch_raw(symbol, interval, limit, end_time_ms):
        return raw

    import unittest.mock as mock

    with mock.patch.object(kl, "_fetch_raw", fake_fetch_raw):
        observed = await kl.fetch_klines_observed(
            "BTCUSDT", "1d", limit=10, end_ms=end_ms,
            as_of_ms=end_ms, now_ms=end_ms + 1_000,
        )
    assert isinstance(observed, obs.Observed)
    legacy = obs.to_legacy(observed)
    assert [c["t"] // 1_000_000 for c in legacy] == opens[:-1]
    assert all(c["qv"] is not None for c in legacy)
    # Closed-day UTC alignment: window end is the midnight cutoff.
    assert observed.meta.window_end_ms == (end_ms // DAY_MS) * DAY_MS
    # A missing day is never zero-filled: drop one raw day, its candle (if
    # parsed) carries qv=None rather than a neighbour's value or 0.
    raw_gap = [raw[0], raw[1], raw[3]]
    by_ms, _ = kl._quote_volume_by_open_ms(raw_gap)
    assert opens[2] not in by_ms


@pytest.mark.asyncio
async def test_kline_observed_duplicate_open_time_gives_null_qv():
    import logging
    from diveintocrypto_desktop.data import binance_klines as kl
    import unittest.mock as mock

    base = [T0 + i * DAY_MS for i in range(3)]

    def _row(o, qv):
        return [o, "100", "110", "90", "105", "1000.0", o + DAY_MS - 1,
                str(qv), 10, "500", "52500", "0"]

    dupe = _row(base[1], 888888.0)
    raw = [_row(base[0], 1.0), _row(base[1], 2.0), dupe, _row(base[2], 3.0)]

    async def fake_fetch_raw(symbol, interval, limit, end_time_ms):
        return raw

    with mock.patch.object(kl, "_fetch_raw", fake_fetch_raw):
        observed = await kl.fetch_klines_observed(
            "BTCUSDT", "1d", limit=10,
            end_ms=base[-1] + DAY_MS, now_ms=base[-1] + DAY_MS + 1_000,
        )
    dupes = [c for c in obs.to_legacy(observed) if c["t"] // 1_000_000 == base[1]]
    assert len(dupes) == 2
    assert all(c["qv"] is None for c in dupes)


# ── known_at / cache / to_legacy / future rejection ──────────────────────────


def test_to_legacy_preserves_object_and_type():
    payload = [{"t": T0, "qv": 1.0}]
    observed = obs.Observed(value=payload, meta=_meta())
    assert obs.to_legacy(observed) is payload
    assert type(obs.to_legacy(observed)) is type(payload)


def test_validate_accepts_known_at_at_cutoff():
    observed = obs.Observed(value={"a": 1}, meta=_meta(known_at_ms=T0))
    assert obs.validate_observation(observed, T0) is observed


def test_validate_rejects_future_known_at_with_clock_skew():
    observed = obs.Observed(value={"a": 1}, meta=_meta(known_at_ms=T0 + 1_000))
    with pytest.raises(ValueError, match="CLOCK_SKEW"):
        obs.validate_observation(observed, T0)


def test_validate_rejects_source_time_beyond_fetch_skew():
    observed = obs.Observed(
        value={"a": 1},
        meta=_meta(source_as_of_ms=T0 + 60_000, fetched_at_ms=T0),
    )
    with pytest.raises(ValueError, match="CLOCK_SKEW"):
        obs.validate_observation(observed, T0 + 120_000)


def test_negative_age_is_never_clamped_to_zero():
    observed = obs.Observed(value={"a": 1}, meta=_meta(known_at_ms=T0 + 5_000))
    age = obs.observation_age_ms(observed, T0)
    assert age == -5_000  # must stay negative: no max(0, age) freshness fakery
    with pytest.raises(ValueError):
        obs.validate_observation(observed, T0)


def test_source_time_unknown_stays_null():
    observed = obs.Observed(value={"a": 1}, meta=_meta(source_as_of_ms=None))
    assert observed.meta.source_as_of_ms is None
    assert obs.validate_observation(observed, T0) is observed


@pytest.mark.asyncio
async def test_funding_observed_known_at_is_completion_time():
    from diveintocrypto_desktop.data import funding as fm
    import unittest.mock as mock

    rows = [
        {"symbol": "BTCUSDT", "fundingTime": T0, "fundingRate": "0.0001",
         "markPrice": "27000.0"},
        {"symbol": "BTCUSDT", "fundingTime": T0 + 8 * 3_600_000,
         "fundingRate": "0.0001", "markPrice": "27000.0"},
    ]

    async def fake_get_json(url, params=None):
        return rows

    with mock.patch.object(fm, "get_json", fake_get_json):
        observed = await fm.fetch_funding_history_observed(
            "BTCUSDT", T0, T0 + 8 * 3_600_000, now_ms=T0 + 9 * 3_600_000,
        )
    assert observed.meta.known_at_ms == T0 + 9 * 3_600_000
    assert observed.meta.fetched_at_ms == T0 + 9 * 3_600_000
    assert obs.to_legacy(observed) == [
        {"t": T0, "funding_rate": 0.0001, "mark_price": 27000.0},
        {"t": T0 + 8 * 3_600_000, "funding_rate": 0.0001, "mark_price": 27000.0},
    ]


@pytest.mark.asyncio
async def test_premium_index_observed_unknown_source_time_is_null():
    from diveintocrypto_desktop.data import funding as fm
    import unittest.mock as mock

    payload = {"markPrice": "27000.0", "indexPrice": "26990.0",
               "lastFundingRate": "0.0001", "nextFundingTime": 0}
    with mock.patch.object(fm, "get_json", return_value=payload):
        observed = await fm.fetch_premium_index_observed("BTCUSDT", now_ms=T0)
    assert observed.meta.source_as_of_ms is None
    assert observed.meta.known_at_ms == T0
    assert obs.to_legacy(observed)["mark_price"] == 27000.0


@pytest.mark.asyncio
async def test_universe_cache_hit_keeps_original_known_at():
    from diveintocrypto_desktop.data import universe as uv
    import unittest.mock as mock

    uv.reset_universe_cache()
    if hasattr(uv, "reset_observation_cache"):
        uv.reset_observation_cache()
    info = {"symbols": [
        {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING",
         "quoteAsset": "USDT", "baseAsset": "BTC"},
    ]}
    tickers = [{"symbol": "BTCUSDT", "lastPrice": "27000",
                "priceChangePercent": "1.0", "quoteVolume": "100.0"}]

    async def fake_get_json(url, params=None):
        if url.endswith("/exchangeInfo"):
            return info
        return tickers

    with mock.patch.object(uv, "get_json", fake_get_json):
        first = await uv.fetch_universe_observed(now_ms=T0)
        second = await uv.fetch_universe_observed(now_ms=T0 + 60_000)
    assert second is first
    assert second.meta.known_at_ms == T0
    uv.reset_universe_cache()
    if hasattr(uv, "reset_observation_cache"):
        uv.reset_observation_cache()


@pytest.mark.asyncio
async def test_spot_snapshot_cache_hit_keeps_original_known_at():
    from diveintocrypto_desktop.data import spot as sp
    import unittest.mock as mock

    sp.reset_cache()
    if hasattr(sp, "reset_observation_cache"):
        sp.reset_observation_cache()
    closes = [100.0 + i for i in range(49)]

    async def fake_spot_json(path, params):
        if path == "/klines":
            return [[0, 0, 0, 0, str(100.0 + i), 0, 0] for i in range(48)]
        return {"lastPrice": "129.5"}

    with mock.patch.object(sp, "_spot_json", fake_spot_json):
        first = await sp.snapshot_observed(
            "BTCUSDT", 148.0, closes, now_ms=T0)
        second = await sp.snapshot_observed(
            "BTCUSDT", 148.0, closes, now_ms=T0 + 30_000)
    assert second is first
    assert second.meta.known_at_ms == T0
    sp.reset_cache()
    if hasattr(sp, "reset_observation_cache"):
        sp.reset_observation_cache()


# ── exchangeInfo raw filters/time, no second parser ─────────────────────────


@pytest.mark.asyncio
async def test_exchange_info_raw_filters_stored_verbatim():
    from diveintocrypto_desktop.data import universe as uv
    import unittest.mock as mock
    import diveintocrypto_desktop.data.http as http_mod

    uv.reset_universe_cache()
    if hasattr(uv, "reset_observation_cache"):
        uv.reset_observation_cache()
    filters = [{"filterType": "PRICE_FILTER", "tickSize": "0.10"},
               {"filterType": "LOT_SIZE", "stepSize": "0.001"}]
    info = {"serverTime": T0 - 5_000, "symbols": [
        {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING",
         "quoteAsset": "USDT", "baseAsset": "BTC", "filters": filters},
    ]}

    async def fake_get_json(url, params=None):
        return info

    with mock.patch.object(uv, "get_json", fake_get_json):
        observed = await uv.fetch_exchange_info_observed(now_ms=T0)
    raw_symbols = obs.to_legacy(observed)["symbols"]
    assert raw_symbols[0]["filters"] == filters
    assert raw_symbols[0]["filters"] is filters or raw_symbols[0]["filters"] == filters
    # No second parser output is built here: parsed TradingRules fields absent.
    assert "price_rules" not in obs.to_legacy(observed)
    assert "lot_rules" not in obs.to_legacy(observed)
    assert observed.meta.source_as_of_ms == T0 - 5_000
    assert observed.meta.known_at_ms == T0
    uv.reset_universe_cache()
    if hasattr(uv, "reset_observation_cache"):
        uv.reset_observation_cache()


# ── OI / price window alignment (5-minute tolerance) ─────────────────────────


def test_oi_price_window_usable_within_5min():
    ok, reason = obs.oi_price_window_usable(
        T0, T0 + 7 * DAY_MS, T0 + 60_000, T0 + 7 * DAY_MS - 60_000)
    assert ok is True
    assert reason is None


def test_oi_price_window_misaligned_beyond_5min():
    ok, reason = obs.oi_price_window_usable(
        T0, T0 + 7 * DAY_MS, T0 + 6 * 60_000 + 1, T0 + 7 * DAY_MS)
    assert ok is False
    assert reason == obs.WINDOW_MISALIGNED
    ok2, reason2 = obs.oi_price_window_usable(
        T0, T0 + 7 * DAY_MS, T0, T0 + 7 * DAY_MS - (5 * 60_000 + 1))
    assert ok2 is False
    assert reason2 == obs.WINDOW_MISALIGNED


def test_oi_change_7d_null_when_windows_misaligned():
    from diveintocrypto_desktop.data import open_interest as oi

    good = oi.compute_oi_change_7d(
        100.0, 110.0, oi_start_ms=T0, oi_end_ms=T0 + 7 * DAY_MS,
        price_start_ms=T0, price_end_ms=T0 + 7 * DAY_MS)
    assert good["value"] == pytest.approx(0.10)
    assert good["reason_code"] is None
    bad = oi.compute_oi_change_7d(
        100.0, 110.0, oi_start_ms=T0, oi_end_ms=T0 + 7 * DAY_MS,
        price_start_ms=T0 + 10 * 60_000, price_end_ms=T0 + 7 * DAY_MS)
    assert bad["value"] is None
    assert bad["reason_code"] == obs.WINDOW_MISALIGNED


@pytest.mark.asyncio
async def test_oi_observed_wraps_native_and_nominal_separately():
    from diveintocrypto_desktop.data import open_interest as oi
    import unittest.mock as mock

    raw = [{"symbol": "BTCUSDT", "sumOpenInterest": "100.000",
            "sumOpenInterestValue": "6000000.0", "timestamp": T0}]

    async def fake_live(**kwargs):
        return raw

    with mock.patch.object(oi, "_live_fetch_open_interest_hist", fake_live):
        observed = await oi.fetch_oi_hist_observed("BTCUSDT", now_ms=T0)
    points = obs.to_legacy(observed)
    assert points[0]["oi"] == pytest.approx(100.0)
    assert points[0]["oi_value"] == pytest.approx(6_000_000.0)
    assert observed.meta.known_at_ms == T0


# ── cross-day freeze realigns both ends ───────────────────────────────────────


def test_utc_window_freeze_realigns_both_ends():
    start, end = obs.utc_closed_day_window(T0 + 12 * 3_600_000, 60)
    assert end == (T0 + 12 * 3_600_000) // DAY_MS * DAY_MS
    assert (end - start) == 60 * DAY_MS


@pytest.mark.asyncio
async def test_spot_history_observed_wraps_provider_result():
    import datetime as dt
    from diveintocrypto_desktop.data import spot as sp
    from diveintocrypto_desktop.shortlab.models import AssetIdentity
    import unittest.mock as mock

    as_of = int(dt.datetime(2024, 1, 31, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    window_end = int(dt.datetime(2024, 1, 31, tzinfo=dt.timezone.utc).timestamp() * 1000)
    window_start = window_end - 60 * DAY_MS
    ident = AssetIdentity(
        canonical_id="BTC", display_symbol="BTC", binance_futures_symbol="BTCUSDT",
        binance_spot_symbol="BTCUSDT", mapping_confidence="HIGH",
        mapping_source="UNIQUE_SYMBOL",
    )

    def _row(open_ms, close, qv):
        return [open_ms, "100", "110", "90", str(close), "5.0",
                open_ms + DAY_MS - 1, str(qv), 10, "500", "52500", "0"]

    spot_rows = [_row(window_start + i * DAY_MS, 100.0 + i, 1_000_000.0 + i)
                 for i in range(60)]

    async def fake_spot_json(path, params):
        if path == "/exchangeInfo":
            return {"symbols": [{"symbol": "BTCUSDT"}]}
        return spot_rows

    async def fake_futures(symbol, interval, limit=300, end_ms=None):
        return [{"t": (window_start + i * DAY_MS) * 1_000_000,
                 "o": 1.0, "h": 2.0, "l": 0.5, "c": 200.0 + i,
                 "v": 7.0, "qv": 3_000_000.0} for i in range(60)]

    sp.reset_spot_history_cache()
    with mock.patch.object(sp, "_spot_json", fake_spot_json), \
         mock.patch("diveintocrypto_desktop.data.binance_klines.fetch_klines",
                    fake_futures):
        observed = await sp.spot_history_observed(ident, as_of, now_ms=as_of + 5_000)
    legacy = obs.to_legacy(observed)
    assert legacy.status == "OK"
    assert legacy.data.spot_daily_bars == 60
    assert obs.to_legacy(observed) is legacy  # identical object, type kept
    assert isinstance(legacy.data.spot_volume_30d, float)
    assert observed.meta.known_at_ms == as_of + 5_000
    assert observed.meta.source_as_of_ms == window_end
    assert observed.meta.window_start_ms == window_start
    assert observed.meta.window_end_ms == window_end
    assert observed.meta.complete is True
    sp.reset_spot_history_cache()


# ── legacy signatures and types preserved ────────────────────────────────────


def test_legacy_signatures_preserved():
    from diveintocrypto_desktop.data import funding as fm
    from diveintocrypto_desktop.data import binance_klines as kl
    from diveintocrypto_desktop.data import open_interest as oi
    from diveintocrypto_desktop.data import spot as sp
    from diveintocrypto_desktop.data import universe as uv

    for fn in (fm.funding_hist, fm.funding_history_range, fm.premium_index,
               kl.fetch_klines, kl.fetch_klines_range,
               oi.fetch_oi_hist, sp.snapshot, sp.spot_history,
               uv.list_universe, uv.perp_symbols):
        params = set(inspect.signature(fn).parameters)
        assert "now_ms" not in params, fn
        assert "identity_snapshot_id" not in params, fn


@pytest.mark.asyncio
async def test_legacy_return_types_unchanged():
    from diveintocrypto_desktop.data import funding as fm
    import unittest.mock as mock

    rows = [{"symbol": "BTCUSDT", "fundingTime": T0, "fundingRate": "0.0001",
             "markPrice": "27000.0"}]
    with mock.patch.object(fm, "get_json", return_value=rows):
        out = await fm.funding_hist("BTCUSDT", limit=48)
    assert isinstance(out, list)
    assert out == [{"t": T0, "funding_rate": 0.0001}]
