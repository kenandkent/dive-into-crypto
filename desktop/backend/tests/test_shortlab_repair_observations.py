"""R03 Provider来源时间与存量恢复 (D03/D04.3/D05.1/D18.2).

Covers:
- receipt重启不变 (persist/restore preserves known_at/source_as_of)
- Universe Ticker 44.744/+40% + 31.96日线 (closeTime/receipt, to_legacy兼容)
- cache hit返回同一Observed
- receipt晚于cutoff拒绝, 源时钟快3s拒绝 (2s容差)
- 缺index7 -> qv None (不补零)
- 451 -> UNAVAILABLE (不标N/A)
- 分页/endTime/去重/429走预算
- fundingInfo -> ScheduleSegment (无历史生效点不前延, 缺档案HISTORY_BOOTSTRAPPING)
- Mark端点真OHLC (禁拿TRADE冒充)
- OI时间/美元来源保留, book两侧分开保存
- 新增生产读取接keyword request_context透传 (不重复扣费)
- legacy缺receipt标UNVERIFIED
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from diveintocrypto_desktop.shortlab import observations as obs

T0 = 1_700_000_000_000
DAY_MS = 86_400_000


# ---------------------------------------------------------------------------
# Fake repo bridge (calls actual parser + repository protocol shape)
# ---------------------------------------------------------------------------

class FakeMarketRepo:
    """Minimal in-memory market-observation store using R03 record helpers."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    async def save_market_observation(self, record: dict) -> str:
        oid = str(record.get("observation_id") or f"obs-{len(self._store)}")
        stored = dict(record)
        stored["observation_id"] = oid
        self._store[oid] = stored
        return oid

    async def get_market_observation(self, oid: str):
        return self._store.get(oid)

    async def list_market_observations(self, symbol, kind, start_ms, end_ms, known_by_ms):
        out = []
        for rec in self._store.values():
            if rec.get("symbol") != symbol or rec.get("kind") != kind:
                continue
            known = rec.get("known_at_ms")
            src = rec.get("source_as_of_ms")
            if known is not None and int(known) > int(known_by_ms):
                continue
            if src is not None and not (int(start_ms) <= int(src) <= int(end_ms)):
                # allow receipt-only (src None) to pass time filter
                pass
            out.append(rec)
        return tuple(out)


class FundingCollector:
    """Test bridge: calls actual data parser + repo protocol (R15b验真实Runtime)."""

    def __init__(self, symbol: str = "BTCUSDT") -> None:
        self.symbol = symbol
        self._last_observed = None
        self._last_oid = None

    async def collect_funding(self, symbol: str | None = None):
        from diveintocrypto_desktop.data import funding as fm

        sym = symbol or self.symbol
        # small window; transport is faked by callers
        observed = await fm.fetch_funding_history_observed(
            sym, T0 - 8 * 3_600_000, T0, now_ms=T0
        )
        self._last_observed = observed
        return observed

    async def persist(self, observed, repo: FakeMarketRepo):
        record = obs.observation_to_record(
            observed, kind="FUNDING_INFO", symbol=self.symbol
        )
        oid = await repo.save_market_observation(record)
        self._last_oid = oid
        return oid

    async def restore(self, repo: FakeMarketRepo):
        assert self._last_oid is not None
        record = await repo.get_market_observation(self._last_oid)
        assert record is not None
        return obs.observation_from_record(record)


def _row(t_ms: int, rate: float = 0.0001, mark: float = 27000.0) -> dict:
    return {
        "symbol": "BTCUSDT",
        "fundingTime": t_ms,
        "fundingRate": f"{rate:.8f}",
        "markPrice": f"{mark:.1f}",
    }


# ---------------------------------------------------------------------------
# 1. receipt重启不变
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_archive_restart_preserves_receipt():
    from diveintocrypto_desktop.data import funding as fm

    rows = [_row(T0 - 8 * 3_600_000), _row(T0)]
    from unittest.mock import patch as _patch

    async def fake_get_json(url, params=None):
        return rows

    repo = FakeMarketRepo()
    collector = FundingCollector("BTCUSDT")
    with _patch.object(fm, "get_json", fake_get_json):
        observed = await collector.collect_funding()
        oid = await collector.persist(observed, repo)
        assert oid is not None
        restored = await collector.restore(repo)
    assert restored.meta.known_at_ms == observed.meta.known_at_ms
    assert restored.meta.source_as_of_ms == observed.meta.source_as_of_ms
    assert restored.meta.fetched_at_ms == observed.meta.fetched_at_ms
    # value preserved (no "now fetched" patch)
    assert obs.to_legacy(restored) == obs.to_legacy(observed)


# ---------------------------------------------------------------------------
# 2. 44.744/+40% 与 31.96 日线 (Universe Ticker + Kline close保留)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_universe_ticker_44744_plus40_retains_close_time_and_receipt():
    from diveintocrypto_desktop.data import universe as uv

    uv.reset_universe_cache()
    if hasattr(uv, "reset_observation_cache"):
        uv.reset_observation_cache()
    try:
        info = {"symbols": [
            {"symbol": "PEPEUSDT", "contractType": "PERPETUAL", "status": "TRADING",
             "quoteAsset": "USDT", "baseAsset": "PEPE"},
        ]}
        close_ms = T0 - 60_000
        tickers = [{"symbol": "PEPEUSDT", "lastPrice": "44.744",
                    "priceChangePercent": "40.0", "quoteVolume": "123456.0",
                    "closeTime": close_ms}]
        recv = T0

        async def fake_get_json(url, params=None):
            if url.endswith("/exchangeInfo"):
                return info
            return tickers

        with patch.object(uv, "get_json", fake_get_json):
            observed = await uv.fetch_universe_observed(now_ms=recv)
        legacy = obs.to_legacy(observed)
        assert isinstance(legacy, list) and len(legacy) == 1
        row = legacy[0]
        assert float(row["price"]) == pytest.approx(44.744)
        assert float(row["ch"]) == pytest.approx(40.0)
        # closeTime retained (not dropped)
        assert int(row.get("closeTime")) == close_ms
        # receipt retained
        assert observed.meta.known_at_ms == recv
        assert observed.meta.fetched_at_ms == recv
        assert observed.meta.source_as_of_ms == close_ms
    finally:
        uv.reset_universe_cache()
        if hasattr(uv, "reset_observation_cache"):
            uv.reset_observation_cache()


@pytest.mark.asyncio
async def test_kline_daily_close_3196_retained_and_unclosed_excluded():
    from diveintocrypto_desktop.data import binance_klines as kl

    base = 1_699_900_000_000 - (1_699_900_000_000 % DAY_MS)

    def _row_1d(open_ms: int, close: str, qv: str) -> list:
        return [open_ms, "30.0", "32.0", "29.0", close, "1000.0",
                open_ms + DAY_MS - 1, qv, 10, "500", "52500", "0"]

    raw = [_row_1d(base + i * DAY_MS, "31.96" if i == 1 else "30.0", "5000.0")
           for i in range(3)]

    async def fake_fetch_raw(symbol, interval, limit, end_time_ms):
        return raw

    end_ms = base + 3 * DAY_MS  # all three finished as of end_ms
    with patch.object(kl, "_fetch_raw", fake_fetch_raw):
        candles = await kl.fetch_klines("BTCUSDT", "1d", limit=3, end_ms=end_ms)
    assert len(candles) == 3
    # source close retained (31.96 present)
    closes = [c["c"] for c in candles]
    assert 31.96 in [float(x) for x in closes]
    # raw index7 retained
    assert all(c["qv"] == pytest.approx(5000.0) for c in candles)
    # unclosed excluded: end_ms exactly at last open -> last still in progress
    with patch.object(kl, "_fetch_raw", fake_fetch_raw):
        candles2 = await kl.fetch_klines("BTCUSDT", "1d", limit=10, end_ms=base + 2 * DAY_MS)
    assert all((c["t"] // 1_000_000) != (base + 2 * DAY_MS) for c in candles2)


# ---------------------------------------------------------------------------
# 3. cache hit返回同一Observed (原始receipt)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_hit_returns_identical_observed():
    from diveintocrypto_desktop.data import universe as uv

    uv.reset_universe_cache()
    if hasattr(uv, "reset_observation_cache"):
        uv.reset_observation_cache()
    try:
        info = {"symbols": [
            {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING",
             "quoteAsset": "USDT", "baseAsset": "BTC"},
        ]}
        tickers = [{"symbol": "BTCUSDT", "lastPrice": "27000",
                    "priceChangePercent": "1.0", "quoteVolume": "100.0",
                    "closeTime": T0 - 10_000}]

        async def fake_get_json(url, params=None):
            if url.endswith("/exchangeInfo"):
                return info
            return tickers

        with patch.object(uv, "get_json", fake_get_json):
            first = await uv.fetch_universe_observed(now_ms=T0)
            second = await uv.fetch_universe_observed(now_ms=T0 + 60_000)
        assert second is first or (
            second.meta.known_at_ms == first.meta.known_at_ms
            and second.meta.source_as_of_ms == first.meta.source_as_of_ms
        )
        assert second.meta.known_at_ms == T0
    finally:
        uv.reset_universe_cache()
        if hasattr(uv, "reset_observation_cache"):
            uv.reset_observation_cache()


# ---------------------------------------------------------------------------
# 4. receipt晚于cutoff拒绝 / 源时钟快3s拒绝 (容差2s)
# ---------------------------------------------------------------------------

def test_receipt_later_than_cutoff_rejected():
    observed = obs.make_observation(
        {"price": 1.0}, source="unit-test",
        source_as_of_ms=T0 - 1_000, fetched_at_ms=T0 + 5_000, known_at_ms=T0 + 5_000,
    )
    with pytest.raises(obs.FutureKnownAtError):
        obs.validate_observation(observed, T0)
    # age negative, never clamped
    assert obs.observation_age_ms(observed, T0) < 0


def test_source_clock_fast_3s_rejected():
    observed = obs.make_observation(
        {"price": 1.0}, source="unit-test",
        source_as_of_ms=T0 + 3_000, fetched_at_ms=T0, known_at_ms=T0,
    )
    with pytest.raises(ValueError, match="CLOCK_SKEW"):
        obs.validate_observation(observed, T0, max_future_skew_sec=2)
    # 2s内允许
    ok = obs.make_observation(
        {"price": 1.0}, source="unit-test",
        source_as_of_ms=T0 + 2_000, fetched_at_ms=T0, known_at_ms=T0,
    )
    assert obs.validate_observation(ok, T0, max_future_skew_sec=2) is ok


# ---------------------------------------------------------------------------
# 5. 缺index7 -> qv None (不补零)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_index7_gives_null_qv():
    from diveintocrypto_desktop.data import binance_klines as kl

    base = 1_699_900_000_000 - (1_699_900_000_000 % DAY_MS)
    # row without index7
    raw = [[base, "100", "110", "90", "105", "1000.0", base + DAY_MS - 1]]
    async def fake_fetch_raw(symbol, interval, limit, end_time_ms):
        return raw
    with patch.object(kl, "_fetch_raw", fake_fetch_raw):
        candles = await kl.fetch_klines("BTCUSDT", "1d", limit=1, end_ms=base + DAY_MS)
    assert len(candles) == 1
    assert candles[0]["qv"] is None
    assert candles[0]["v"] == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# 6. 451不N/A (Spot 451 -> UNAVAILABLE)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_451_is_unavailable_not_na():
    from diveintocrypto_desktop.data import spot as sp
    from diveintocrypto_desktop.shortlab.models import AssetIdentity

    sp.reset_spot_history_cache()
    try:
        ident = AssetIdentity(
            canonical_id="BTC", display_symbol="BTC",
            binance_futures_symbol="BTCUSDT", binance_spot_symbol="BTCUSDT",
            mapping_confidence="HIGH", mapping_source="UNIQUE_SYMBOL",
        )
        as_of = T0

        async def fake_spot_json(path, params):
            if path == "/exchangeInfo":
                return {"symbols": [{"symbol": "BTCUSDT"}]}
            raise RuntimeError("451 Client Error: restricted location")

        with patch.object(sp, "_spot_json", fake_spot_json):
            res = await sp.spot_history(ident, as_of)
        assert res.status == "UNAVAILABLE"
        assert res.status != "NOT_APPLICABLE"
        assert res.data is None
        assert sp._negative_cache == {}
    finally:
        sp.reset_spot_history_cache()


# ---------------------------------------------------------------------------
# 7. 分页/endTime/去重/429走预算 (funding_history_range)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_funding_pagination_endtime_dedup():
    from diveintocrypto_desktop.data import funding as fm

    t0 = T0 - 4 * 3_600_000
    rows = [_row(t0 + k * 3_600_000) for k in range(5)]

    class _Paged:
        def __init__(self):
            self.calls = []

        async def __call__(self, url, params=None):
            self.calls.append({"url": url, "params": dict(params or {})})
            out = [r for r in rows
                   if (params.get("startTime") is None or r["fundingTime"] >= params["startTime"])
                   and (params.get("endTime") is None or r["fundingTime"] <= params["endTime"])]
            return out[: params.get("limit", 1000)]

    fake = _Paged()
    with patch.object(fm, "get_json", fake):
        out = await fm.funding_history_range("BTCUSDT", t0, t0 + 4 * 3_600_000, limit=2)
    assert [e["t"] for e in out] == [r["fundingTime"] for r in rows]
    # endTime透传
    assert fake.calls[0]["params"]["endTime"] == t0 + 4 * 3_600_000
    # 去重: 重复边界行不重复
    pages = [[rows[0], rows[1]], [rows[1], rows[2]], []]
    async def overlapping(url, params=None):
        return pages.pop(0)
    with patch.object(fm, "get_json", overlapping):
        out2 = await fm.funding_history_range("BTCUSDT", t0, t0 + 2 * 3_600_000, limit=2)
    assert [e["t"] for e in out2] == [rows[0]["fundingTime"], rows[1]["fundingTime"], rows[2]["fundingTime"]]


@pytest.mark.asyncio
async def test_funding_429_goes_through_shared_http_budget():
    from diveintocrypto_desktop.data import funding as fm
    from diveintocrypto_desktop.data import http as http_mod

    t0 = T0 - 8 * 3_600_000
    rows = [_row(t0), _row(t0 + 8 * 3_600_000)]

    class _FakeResp:
        def __init__(self, status, payload, retry_after=None):
            self.status = status
            self._payload = payload
            self.headers = {} if retry_after is None else {"Retry-After": retry_after}

        def raise_for_status(self):
            if self.status >= 400:
                import aiohttp
                raise aiohttp.ClientResponseError(request_info=None, history=(), status=self.status, message="err")

        async def json(self):
            return self._payload

    class _FakeSession:
        def __init__(self, resps):
            self.responses = list(resps)

        def get(self, url, params=None):
            resp = self.responses.pop(0)
            class _Ctx:
                async def __aenter__(self):
                    return resp
                async def __aexit__(self, *exc):
                    return None
            return _Ctx()

    session = _FakeSession([_FakeResp(429, None, retry_after="1"), _FakeResp(200, rows)])
    sleeps = []
    async def fake_sleep(s):
        sleeps.append(s)
    with patch.object(http_mod, "get_session", return_value=session), patch.object(http_mod.asyncio, "sleep", fake_sleep):
        out = await fm.funding_history_range("BTCUSDT", t0, t0 + 8 * 3_600_000)
    assert sleeps == [1.0]
    assert [e["t"] for e in out] == [t0, t0 + 8 * 3_600_000]


# ---------------------------------------------------------------------------
# 8. fundingInfo -> ScheduleSegment (不前延, 缺档案HISTORY_BOOTSTRAPPING)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_funding_info_schedule_no_forward_extension():
    from diveintocrypto_desktop.data import funding as fm

    observed_at = T0
    info_rows = [{"symbol": "BTCUSDT", "adjustedFundingRateCap": "0.02",
                  "adjustedFundingRateFloor": "-0.02", "fundingIntervalHours": 8}]
    with patch.object(fm, "get_json", return_value=info_rows):
        observed = await fm.fetch_funding_info_observed(now_ms=observed_at)
    assert obs.to_legacy(observed) == info_rows
    segments = fm.funding_info_to_schedule_segments(
        obs.to_legacy(observed), observed_at_ms=observed_at, known_at_ms=observed_at
    )
    assert len(segments) == 1
    seg = segments[0]
    # 无历史生效点不前延: effective_from == 本次观察边界
    assert int(getattr(seg, "effective_from_ms")) == observed_at
    assert int(getattr(seg, "interval_hours")) == 8


def test_missing_archive_reports_history_bootstrapping():
    from diveintocrypto_desktop.data import funding as fm

    reasons = fm.history_bootstrapping_reasons(has_archive=False)
    assert "HISTORY_BOOTSTRAPPING" in tuple(reasons)
    reasons2 = fm.history_bootstrapping_reasons(has_archive=True)
    assert "HISTORY_BOOTSTRAPPING" not in tuple(reasons2)


# ---------------------------------------------------------------------------
# 9. Mark端点真OHLC (禁拿TRADE冒充)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mark_klines_uses_mark_endpoint_not_trade():
    from diveintocrypto_desktop.data import binance_klines as kl

    start = T0 - 2 * DAY_MS
    end = T0
    mark_rows = [
        [start, "100.0", "110.0", "90.0", "105.0", "1000.0", start + DAY_MS - 1, "105000.0", 10, "500", "52500", "0"],
        [start + DAY_MS, "105.0", "115.0", "95.0", "110.0", "1100.0", start + 2 * DAY_MS - 1, "115000.0", 10, "500", "53500", "0"],
    ]
    calls = []

    async def fake_get_json(url, params=None, request_context=None):
        calls.append({"url": url, "params": dict(params or {})})
        assert "markPriceKlines" in url, f"must hit Mark endpoint, got {url!r}"
        assert "lines" in url.lower()
        return mark_rows

    with patch("diveintocrypto_desktop.data.binance_klines.get_json", fake_get_json):
        observed = await kl.fetch_mark_klines_range("BTCUSDT", "1d", start, end)
    assert calls and all("markPriceKlines" in c["url"] for c in calls)
    # 真OHLC/close保留 (非TRADE冒充: close来自mark行index4)
    candles = obs.to_legacy(observed)
    assert [float(c["c"]) for c in candles] == [105.0, 110.0]
    assert observed.meta.source_as_of_ms is not None


# ---------------------------------------------------------------------------
# 10. OI时间/美元来源保留
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oi_time_and_usd_source_retained():
    from diveintocrypto_desktop.data import open_interest as oi_mod

    pts = [
        {"t": (T0 - 60_000) * 1_000_000, "oi": 100.0, "oi_value": 2_700_000.0},
        {"t": T0 * 1_000_000, "oi": 110.0, "oi_value": 2_970_000.0},
    ]
    with patch.object(oi_mod, "fetch_oi_hist", return_value=pts):
        observed = await oi_mod.fetch_oi_hist_observed("BTCUSDT", limit=2, now_ms=T0)
    assert observed.meta.source_as_of_ms == T0
    assert observed.meta.known_at_ms == T0
    legacy = obs.to_legacy(observed)
    assert legacy[0]["oi"] == pytest.approx(100.0)
    assert legacy[0]["oi_value"] == pytest.approx(2_700_000.0)
    # 美元来源: USDT名义可直接作USD近似, 来源字符串保留
    res = oi_mod.resolve_oi_value_usd(100.0, 2_700_000.0, mark_price=27000.0,
                                      oi_time_ms=T0 - 60_000, mark_time_ms=T0 - 30_000,
                                      quote_unit="USDT")
    assert res["oi_value_usd"] == pytest.approx(2_700_000.0)
    assert res["oi_value_source"] is not None and res["reason_code"] is None


# ---------------------------------------------------------------------------
# 11. book两侧分开保存
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_book_sides_saved_separately():
    from diveintocrypto_desktop.data import orderbook as ob

    raw = {"bids": [["100.0", "1.0"], ["99.0", "2.0"]],
           "asks": [["101.0", "1.5"], ["102.0", "1.0"]]}
    with patch.object(ob, "fetch_depth", return_value=raw):
        observed = await ob.fetch_book_observed("BTCUSDT", now_ms=T0)
    value = obs.to_legacy(observed)
    assert "bids" in value and "asks" in value
    assert value["bids"] != value["asks"]
    assert len(value["bids"]) == 2 and len(value["asks"]) == 2
    # 两侧可各自算VWAP (不只剩总量)
    assert observed.meta.known_at_ms == T0


# ---------------------------------------------------------------------------
# 12. request_context透传 (不重复扣费) + family不动
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_context_passthrough_no_double_charge():
    from diveintocrypto_desktop.data import funding as fm
    from diveintocrypto_desktop.shortlab.request_budget import RequestBudget, make_request_context

    t0 = T0 - 8 * 3_600_000
    rows = [_row(t0)]
    seen = {}

    async def fake_get_json(url, params=None, request_context=None):
        seen["ctx"] = request_context
        return rows

    budget = RequestBudget(max_sends=240, window_ms=60_000)
    ctx = make_request_context(budget, job_type="entry", host="fapi", trace_id="trace-r03-1")
    with patch.object(fm, "get_json", fake_get_json):
        out = await fm.funding_history_range("BTCUSDT", t0, t0 + 8 * 3_600_000, request_context=ctx)
    assert out and seen["ctx"] is ctx
    assert seen["ctx"].trace_id == "trace-r03-1"
    # 数据模块不重复扣预算: 未经HTTP层不产生sent计数 (fake未走真实transport)
    assert budget.sent_attempts == 0

    # 已有family不动: klines/fundingRate权重仍为v1 fixture值
    from diveintocrypto_desktop.shortlab.request_budget import ENDPOINT_WEIGHTS, endpoint_weight
    assert endpoint_weight("klines", {"limit": 10}) == ENDPOINT_WEIGHTS["klines"]["buckets"][0][1]
    assert endpoint_weight("fundingRate", {"limit": 1000}) is not None


@pytest.mark.asyncio
async def test_new_reads_accept_request_context_keyword():
    import inspect

    from diveintocrypto_desktop.data import binance_klines as kl
    from diveintocrypto_desktop.data import funding as fm
    from diveintocrypto_desktop.data import open_interest as oi_mod
    from diveintocrypto_desktop.data import orderbook as ob
    from diveintocrypto_desktop.data import spot as sp
    from diveintocrypto_desktop.data import universe as uv

    assert "request_context" in inspect.signature(fm.funding_history_range).parameters
    assert "request_context" in inspect.signature(fm.fetch_funding_history_observed).parameters
    assert "request_context" in inspect.signature(fm.fetch_funding_info_observed).parameters
    assert "request_context" in inspect.signature(kl.fetch_klines).parameters
    assert "request_context" in inspect.signature(kl.fetch_mark_klines_range).parameters
    assert "request_context" in inspect.signature(oi_mod.fetch_oi_hist).parameters
    assert "request_context" in inspect.signature(oi_mod.fetch_oi_hist_observed).parameters
    assert "request_context" in inspect.signature(sp.spot_history).parameters
    assert "request_context" in inspect.signature(ob.fetch_depth).parameters
    assert "request_context" in inspect.signature(ob.fetch_book_observed).parameters
    assert "request_context" in inspect.signature(uv.fetch_universe_observed).parameters


# ---------------------------------------------------------------------------
# 13. legacy缺receipt标UNVERIFIED
# ---------------------------------------------------------------------------

def test_legacy_missing_receipt_marked_unverified():
    legacy_record = {
        "observation_id": "legacy-1",
        "symbol": "BTCUSDT",
        "kind": "FUNDING_INFO",
        "source_as_of_ms": T0 - 60_000,
        "known_at_ms": None,
        "value_json": [{"t": T0 - 60_000, "funding_rate": 0.0001}],
        "meta_json": {"status": "OK", "source": "legacy", "known_at_ms": None},
        "raw_sha256": "x",
    }
    restored = obs.observation_from_record(legacy_record)
    assert restored.meta.known_at_ms is None
    assert restored.meta.reason_code == obs.UNVERIFIED
    with pytest.raises(ValueError):
        obs.validate_observation(restored, T0)


# ---------------------------------------------------------------------------
# 14. CR03 funding schedule collection (D05.1/D05.2, empty DB -> archive ->
#     only post-receipt coverage; default-8h CONFIRMED predicate; no backfill).
# ---------------------------------------------------------------------------

H8_MS = 8 * 3_600_000
H4_MS = 4 * 3_600_000


class _FakeScheduleRepo:
    """In-memory schedule + market store (same read shape as real repo)."""

    def __init__(self) -> None:
        self.schedules: dict[str, dict] = {}
        self.markets: dict[str, dict] = {}

    async def save_funding_schedule(self, record: dict) -> str:
        sid = str(record["schedule_id"])
        if sid not in self.schedules:
            self.schedules[sid] = dict(record)
        return sid

    async def list_funding_schedules(self, symbol, known_by_ms):
        out = [
            r for r in self.schedules.values()
            if r.get("symbol") == symbol and int(r.get("known_at_ms", 0)) <= int(known_by_ms)
        ]
        out.sort(key=lambda r: (int(r["effective_from_ms"]), int(r["known_at_ms"]), str(r["schedule_id"])))
        return tuple(out)

    async def save_market_observation(self, record: dict) -> str:
        oid = str(record["observation_id"])
        if oid not in self.markets:
            self.markets[oid] = dict(record)
        return oid


def _valid_default_regime(known_at: int = T0) -> dict:
    return {
        "interval_hours": 8,
        "version": "official-default-v2026-10-01",
        "known_at_ms": known_at,
        "source": "binance:fapi/fundingInfo:default",
    }


def _slots_from(anchor: int, interval_ms: int, start_exclusive: int, end_inclusive: int) -> list[int]:
    """Slots ``anchor + k*interval`` inside ``(start, end]`` (D05.2 left-open)."""
    import math as _math

    k_min = _math.ceil((start_exclusive + 1 - anchor) / interval_ms)
    k_max = _math.floor((end_inclusive - anchor) / interval_ms)
    out = []
    for k in range(k_min, k_max + 1):
        t = anchor + k * interval_ms
        if t > start_exclusive and t <= end_inclusive:
            out.append(t)
    return sorted(out)


def _funding_event_for(t: int, known_at: int, rate: str = "0.0005") -> dict:
    return {"symbol": "BTCUSDT", "funding_time_ms": t, "rate": rate, "known_at_ms": known_at}


def test_cr03_default_8h_predicate_requires_all_conditions():
    from diveintocrypto_desktop.data import funding as fm

    regime = _valid_default_regime(T0)
    assert fm.is_default_8h_confirmed(
        response_ok=True, symbol="ETHUSDT", symbol_status="TRADING",
        adjusted_symbols={"BTCUSDT"}, default_regime=regime,
    ) is True
    # (a) incomplete/error response keeps UNKNOWN.
    assert fm.is_default_8h_confirmed(
        response_ok=False, symbol="ETHUSDT", symbol_status="TRADING",
        adjusted_symbols=set(), default_regime=regime,
    ) is False
    # (b) non-TRADING / missing status never confirms.
    assert fm.is_default_8h_confirmed(
        response_ok=True, symbol="ETHUSDT", symbol_status="SETTLING",
        adjusted_symbols=set(), default_regime=regime,
    ) is False
    assert fm.is_default_8h_confirmed(
        response_ok=True, symbol="ETHUSDT", symbol_status=None,
        adjusted_symbols=set(), default_regime=regime,
    ) is False
    # (b) adjusted symbol never takes the default path.
    assert fm.is_default_8h_confirmed(
        response_ok=True, symbol="BTCUSDT", symbol_status="TRADING",
        adjusted_symbols={"BTCUSDT"}, default_regime=regime,
    ) is False
    # (c) absence alone never confirms: no regime / wrong interval / no version / no receipt.
    assert fm.is_default_8h_confirmed(
        response_ok=True, symbol="ETHUSDT", symbol_status="TRADING",
        adjusted_symbols=set(), default_regime=None,
    ) is False
    assert fm.is_default_8h_confirmed(
        response_ok=True, symbol="ETHUSDT", symbol_status="TRADING",
        adjusted_symbols=set(), default_regime={**regime, "interval_hours": 4},
    ) is False
    assert fm.is_default_8h_confirmed(
        response_ok=True, symbol="ETHUSDT", symbol_status="TRADING",
        adjusted_symbols=set(), default_regime={k: v for k, v in regime.items() if k != "version"},
    ) is False
    assert fm.is_default_8h_confirmed(
        response_ok=True, symbol="ETHUSDT", symbol_status="TRADING",
        adjusted_symbols=set(), default_regime={k: v for k, v in regime.items() if k != "known_at_ms"},
    ) is False
    ok, reason = fm.validate_default_regime(None)
    assert ok is False and reason is not None
    ok2, _ = fm.validate_default_regime(regime)
    assert ok2 is True


def test_cr03_adjusted_segments_never_backfill():
    from diveintocrypto_desktop.data import funding as fm

    rows = [{"symbol": "BTCUSDT", "fundingIntervalHours": 8}]
    segs = fm.funding_info_to_schedule_segments(rows, observed_at_ms=T0, known_at_ms=T0)
    assert len(segs) == 1
    seg = segs[0]
    assert int(getattr(seg, "effective_from_ms")) == T0
    assert getattr(seg, "effective_to_ms") is None
    assert int(getattr(seg, "anchor_ms")) == T0
    assert int(getattr(seg, "known_at_ms")) == T0
    assert getattr(seg, "verification") == "CONFIRMED"
    # Missing interval stays UNKNOWN (never assumed 8h CONFIRMED).
    segs2 = fm.funding_info_to_schedule_segments(
        [{"symbol": "BTCUSDT"}], observed_at_ms=T0, known_at_ms=T0
    )
    assert getattr(segs2[0], "verification") == "UNKNOWN"
    # Absent symbol yields no segment (no inference).
    assert fm.funding_info_to_schedule_segments([], observed_at_ms=T0, known_at_ms=T0) == ()


@pytest.mark.asyncio
async def test_cr03_collect_empty_db_writes_and_covers_only_post_receipt():
    from diveintocrypto_desktop.data import funding as fm

    repo = _FakeScheduleRepo()
    # Empty start: no archive -> HISTORY_BOOTSTRAPPING expected on first write.
    assert await repo.list_funding_schedules("BTCUSDT", T0 - 1) == ()
    assert await repo.list_funding_schedules("ETHUSDT", T0 - 1) == ()

    adjusted_rows = [{"symbol": "BTCUSDT", "fundingIntervalHours": 8}]
    statuses = {"BTCUSDT": "TRADING", "ETHUSDT": "TRADING"}
    regime = _valid_default_regime(T0)

    async def _fake_fetch_info(*, request_context=None):
        return list(adjusted_rows)

    with patch.object(fm, "fetch_funding_info", _fake_fetch_info):
        res = await fm.collect_and_archive_funding_schedules(
            repository=repo,
            symbols=["BTCUSDT", "ETHUSDT"],
            symbol_statuses=statuses,
            default_regime=regime,
            now_ms=T0,
        )
    assert res["response_ok"] is True
    assert res["observed_at_ms"] == T0 and res["known_at_ms"] == T0
    assert set(res["adjusted_symbols"]) == {"BTCUSDT"}
    # Both symbols archived as CONFIRMED (adjusted 8h + default 8h with valid predicate).
    saved = res["saved_ids"]
    assert len(saved) == 2
    assert res["reasons"] == ("HISTORY_BOOTSTRAPPING",)
    assert res["raw_observation_id"] is not None

    # Receipt preserved: effective_from == observation boundary, known_at == receipt.
    for sym in ("BTCUSDT", "ETHUSDT"):
        rows = await repo.list_funding_schedules(sym, T0)
        assert len(rows) == 1
        rec = rows[0]
        assert int(rec["effective_from_ms"]) == T0
        assert rec["effective_to_ms"] is None
        assert int(rec["known_at_ms"]) == T0
        sched = rec["schedule_json"]
        assert sched["verification"] == "CONFIRMED"
        assert int(sched["interval_hours"]) == 8
        assert int(sched["anchor_ms"]) == T0
    # Default evidence references the official version (audit trail).
    eth_sched = (await repo.list_funding_schedules("ETHUSDT", T0))[0]["schedule_json"]
    assert "official-default-v2026-10-01" in str(eth_sched["evidence_ref"])

    # Coverage: Collector read-only path (same repo read + unwrap, market.py untouched).
    from diveintocrypto_desktop.shortlab.funding_schedule import compute_schedule_coverage

    # Pre-receipt window stays UNKNOWN even with dense 8h events (no backfill).
    pre_start, pre_end = T0 - 30 * DAY_MS, T0
    pre_slots = _slots_from(T0 - 30 * DAY_MS, H8_MS, pre_start - 1, pre_end)
    # Build dense pre events aligned to their own grid (they cannot match post anchor).
    pre_events = [_funding_event_for(t, T0 - 1_000) for t in pre_slots]
    for sym in ("BTCUSDT", "ETHUSDT"):
        raw = await repo.list_funding_schedules(sym, pre_end)
        cov_pre = compute_schedule_coverage(
            pre_events, fm.unwrap_repo_schedules_for_coverage(raw), pre_start, pre_end, pre_end
        )
        assert cov_pre.coverage_fraction is None
        assert "FUNDING_SCHEDULE_UNKNOWN" in cov_pre.reasons

    # Post-receipt 7d window with anchor-aligned events grants coverage.
    post_start, post_end = T0, T0 + 7 * DAY_MS
    post_slots = _slots_from(T0, H8_MS, post_start, post_end)
    assert len(post_slots) == 21
    post_events = [_funding_event_for(t, T0 + 1_000) for t in post_slots]
    for sym in ("BTCUSDT", "ETHUSDT"):
        raw = await repo.list_funding_schedules(sym, post_end)
        cov = compute_schedule_coverage(
            post_events, fm.unwrap_repo_schedules_for_coverage(raw), post_start, post_end, post_end
        )
        assert cov.expected_count == 21
        assert cov.received_count == 21
        assert cov.coverage_fraction == "1"
        assert cov.reasons == ()


@pytest.mark.asyncio
async def test_cr03_default_without_valid_regime_stays_unknown():
    from diveintocrypto_desktop.data import funding as fm
    from diveintocrypto_desktop.shortlab.funding_schedule import compute_schedule_coverage

    repo = _FakeScheduleRepo()

    async def _fake_fetch_info(*, request_context=None):
        return []

    # No official default archive: unadjusted TRADING symbol must stay UNKNOWN.
    with patch.object(fm, "fetch_funding_info", _fake_fetch_info):
        res = await fm.collect_and_archive_funding_schedules(
            repository=repo,
            symbols=["ETHUSDT"],
            symbol_statuses={"ETHUSDT": "TRADING"},
            default_regime=None,
            now_ms=T0,
        )
    assert res["saved_ids"] == ()
    assert len(res["segments"]) == 1
    assert getattr(res["segments"][0], "verification") == "UNKNOWN"
    raw = await repo.list_funding_schedules("ETHUSDT", T0)
    assert raw == ()
    cov = compute_schedule_coverage([], fm.unwrap_repo_schedules_for_coverage(raw), T0, T0 + 7 * DAY_MS, T0 + 7 * DAY_MS)
    assert cov.coverage_fraction is None
    assert "FUNDING_SCHEDULE_UNKNOWN" in cov.reasons


@pytest.mark.asyncio
async def test_cr03_incomplete_response_writes_nothing():
    from diveintocrypto_desktop.data import funding as fm

    repo = _FakeScheduleRepo()

    async def _boom(*, request_context=None):
        raise RuntimeError("fapi down")

    with patch.object(fm, "fetch_funding_info", _boom):
        res = await fm.collect_and_archive_funding_schedules(
            repository=repo, symbols=["BTCUSDT"],
            symbol_statuses={"BTCUSDT": "TRADING"},
            default_regime=_valid_default_regime(T0), now_ms=T0,
        )
    assert res["response_ok"] is False
    assert res["saved_ids"] == () and res["closed_ids"] == ()
    assert "FUNDING_SCHEDULE_UNKNOWN" in res["reasons"]
    assert await repo.list_funding_schedules("BTCUSDT", T0) == ()


@pytest.mark.asyncio
async def test_cr03_continuous_collect_is_idempotent():
    from diveintocrypto_desktop.data import funding as fm

    repo = _FakeScheduleRepo()
    rows = [{"symbol": "BTCUSDT", "fundingIntervalHours": 8}]
    statuses = {"BTCUSDT": "TRADING"}
    regime = _valid_default_regime(T0)

    async def _fake_fetch_info(*, request_context=None):
        return list(rows)

    with patch.object(fm, "fetch_funding_info", _fake_fetch_info):
        first = await fm.collect_and_archive_funding_schedules(
            repository=repo, symbols=["BTCUSDT"], symbol_statuses=statuses,
            default_regime=regime, now_ms=T0,
        )
        second = await fm.collect_and_archive_funding_schedules(
            repository=repo, symbols=["BTCUSDT"], symbol_statuses=statuses,
            default_regime={**regime, "known_at_ms": T0 + 3_600_000},
            now_ms=T0 + 3_600_000,
        )
    assert len(first["saved_ids"]) == 1
    # Stable regime: second tick writes nothing (no overlapping duplicates).
    assert second["saved_ids"] == () and second["closed_ids"] == ()
    rows_now = await repo.list_funding_schedules("BTCUSDT", T0 + 3_600_000)
    assert len(rows_now) == 1


@pytest.mark.asyncio
async def test_cr03_regime_change_closes_stale_and_covers_new():
    from diveintocrypto_desktop.data import funding as fm
    from diveintocrypto_desktop.shortlab.funding_schedule import compute_schedule_coverage

    repo = _FakeScheduleRepo()
    statuses = {"BTCUSDT": "TRADING"}
    regime = _valid_default_regime(T0)
    t1 = T0 + 7 * DAY_MS

    async def _fetch_8h(*, request_context=None):
        return [{"symbol": "BTCUSDT", "fundingIntervalHours": 8}]

    async def _fetch_4h(*, request_context=None):
        return [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}]

    with patch.object(fm, "fetch_funding_info", _fetch_8h):
        r1 = await fm.collect_and_archive_funding_schedules(
            repository=repo, symbols=["BTCUSDT"], symbol_statuses=statuses,
            default_regime=regime, now_ms=T0,
        )
    assert len(r1["saved_ids"]) == 1
    with patch.object(fm, "fetch_funding_info", _fetch_4h):
        r2 = await fm.collect_and_archive_funding_schedules(
            repository=repo, symbols=["BTCUSDT"], symbol_statuses=statuses,
            default_regime=regime, now_ms=t1,
        )
    assert len(r2["saved_ids"]) == 1
    assert len(r2["closed_ids"]) == 1
    # Old 8h window still covered by the closed [T0, t1) revision.
    old_slots = _slots_from(T0, H8_MS, T0, t1)
    old_events = [_funding_event_for(t, t1) for t in old_slots]
    raw = await repo.list_funding_schedules("BTCUSDT", t1)
    cov_old = compute_schedule_coverage(
        old_events, fm.unwrap_repo_schedules_for_coverage(raw), T0, t1, t1
    )
    assert cov_old.coverage_fraction == "1"
    # New 4h window covered by the new [t1, None) segment.
    new_slots = _slots_from(t1, H4_MS, t1, t1 + 7 * DAY_MS)
    assert len(new_slots) == 42
    new_events = [_funding_event_for(t, t1 + 1_000) for t in new_slots]
    cov_new = compute_schedule_coverage(
        new_events, fm.unwrap_repo_schedules_for_coverage(raw), t1, t1 + 7 * DAY_MS, t1 + 7 * DAY_MS
    )
    assert cov_new.expected_count == 42 and cov_new.received_count == 42
    assert cov_new.coverage_fraction == "1"


@pytest.mark.asyncio
async def test_cr03_restart_preserves_receipt_and_coverage():
    import tempfile
    from pathlib import Path

    from diveintocrypto_desktop.data import funding as fm
    from diveintocrypto_desktop.shortlab.funding_schedule import compute_schedule_coverage
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    tmp = tempfile.mkdtemp()
    db = Path(tmp) / "cr03.duckdb"
    repo = await ShortLabRepository.open(db)
    try:
        await repo.migrate(6)
        # Empty start.
        assert await repo.list_funding_schedules("BTCUSDT", T0) == ()

        async def _fake_fetch_info(*, request_context=None):
            return [{"symbol": "BTCUSDT", "fundingIntervalHours": 8}]

        with patch.object(fm, "fetch_funding_info", _fake_fetch_info):
            res = await fm.collect_and_archive_funding_schedules(
                repository=repo, symbols=["BTCUSDT"],
                symbol_statuses={"BTCUSDT": "TRADING"},
                default_regime=_valid_default_regime(T0), now_ms=T0,
            )
        assert len(res["saved_ids"]) == 1
    finally:
        await repo.close()
    # Restart: reopen the same DB file, receipt must survive verbatim.
    repo2 = await ShortLabRepository.open(db)
    try:
        rows = await repo2.list_funding_schedules("BTCUSDT", T0)
        assert len(rows) == 1
        assert int(rows[0]["known_at_ms"]) == T0
        assert int(rows[0]["effective_from_ms"]) == T0
        sched = rows[0]["schedule_json"]
        assert sched["verification"] == "CONFIRMED" and int(sched["interval_hours"]) == 8
        post_slots = _slots_from(T0, H8_MS, T0, T0 + 7 * DAY_MS)
        post_events = [
            {"symbol": "BTCUSDT", "funding_time_ms": t, "rate": "0.0005", "known_at_ms": T0 + 1_000}
            for t in post_slots
        ]
        cov = compute_schedule_coverage(
            post_events, fm.unwrap_repo_schedules_for_coverage(rows), T0, T0 + 7 * DAY_MS, T0 + 7 * DAY_MS
        )
        assert cov.coverage_fraction == "1"
    finally:
        await repo2.close()




class _TFakeClock:
    def __init__(self, start_ms: int) -> None:
        self.ms = int(start_ms)

    def __call__(self) -> int:
        return int(self.ms)


@pytest.mark.asyncio
async def test_backfill_run_archives_funding_schedules(tmp_path) -> None:
    """CR03 wiring: run_funding_backfill archives CONFIRMED schedules (D05.1)."""
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from diveintocrypto_desktop.shortlab import observations as _obs
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.data import funding as fm
    from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from diveintocrypto_desktop.shortlab.request_budget import ObservedCache, RequestBudget
    from diveintocrypto_desktop.shortlab.service import (
        JOB_TYPE_FUNDING_BACKFILL,
        ShortLabService,
    )

    db = Path(tempfile.mkdtemp()) / "cr03w.duckdb"
    repo = await ShortLabRepository.open(db)
    try:
        await repo.migrate(6)
        assert await repo.list_funding_schedules("BTCUSDT", T0) == ()

        async def _universe_fn(limit: int | None = None):
            return [{"s": "BTCUSDT"}]

        async def _funding_fn(symbol: str, a: int, b: int):
            return []

        async def _fake_info_observed(*, request_context=None, **kwargs):
            return _obs.make_observation(
                [{"symbol": "BTCUSDT", "fundingIntervalHours": 8}],
                source="binance:fapi/fundingInfo",
                source_as_of_ms=None,
                fetched_at_ms=T0,
                known_at_ms=T0,
            )

        clock = _TFakeClock(T0)
        service = ShortLabService(
            config=load_shortlab_config(), repository=repo,
            registry=ProviderRegistry(), clock=clock,
            universe_fn=_universe_fn, funding_history_fn=_funding_fn,
            request_budget=RequestBudget(max_sends=240, window_ms=60_000),
            observed_cache=ObservedCache(),
        )
        with patch.object(fm, "fetch_funding_info_observed", _fake_info_observed):
            ctx = service.make_job_context(JOB_TYPE_FUNDING_BACKFILL, trace_id="cr03w-1")
            status = await service.run_funding_backfill(ctx, job_id="cr03w-1")
        assert status.status == "SUCCEEDED"
        assert int(status.stats.get("schedules_saved", 0)) >= 1
        rows = await repo.list_funding_schedules("BTCUSDT", T0)
        assert len(rows) == 1
        sched = rows[0]["schedule_json"]
        assert sched["verification"] == "CONFIRMED"
        assert int(sched["interval_hours"]) == 8
        assert int(rows[0]["effective_from_ms"]) == T0
    finally:
        await repo.close()
