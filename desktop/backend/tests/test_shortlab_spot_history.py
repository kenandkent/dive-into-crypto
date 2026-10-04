"""Task 6: Spot 60D history and futures-spot units (Short-Lab Phase 1).

Contract (ShortLab_Detailed_Design_CN.md §5.1/§4.2/§4.4,
ShortLab_Implementation_Plan_CN.md Task 6):

- ``spot.snapshot()`` keeps its 48 × 1h behaviour ( untouched ).
- ``spot_history(identity, as_of_ms)`` fetches Spot ``1d`` klines (limit 61)
  for the 60 closed UTC days ending at ``as_of`` (30 + 30, unclosed current
  UTC day excluded) and the futures ``1d`` leg at the SAME cutoff via
  ``binance_klines.fetch_klines`` (Task 4 ``qv``).
- Volumes are quote volume (raw index 7); base volume (index 5) is never read.
  Volumes/OI are never scaled by ``contract_multiplier``; only ``premium``
  (decimal) is multiplier-normalised.
- The Spot API symbol comes ONLY from ``identity.binance_spot_symbol``; the
  futures symbol is never stripped/guessed.
- Spot 451/429/timeout — and a ``-1121`` without exchangeInfo confirmation —
  are UNAVAILABLE. Only a Spot exchangeInfo list that provably lacks the
  symbol is NOT_APPLICABLE/``no_spot_market``.
- exchangeInfo list: positive cache 3600s per host; confirmed-absent symbols:
  negative cache 900s keyed (host, symbol); concurrent assets share one
  fetch; 451/429/timeout never write the negative cache.

All network access is faked; no live requests.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import time as _time

import pytest

from diveintocrypto_desktop.data import spot as spot_mod
from diveintocrypto_desktop.data.http import TransientUpstreamError
from diveintocrypto_desktop.shortlab.models import AssetIdentity

DAY_MS = 86_400_000
AS_OF_MS = int(dt.datetime(2024, 1, 31, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
WINDOW_END_MS = int(dt.datetime(2024, 1, 31, tzinfo=dt.timezone.utc).timestamp() * 1000)
WINDOW_START_MS = WINDOW_END_MS - 60 * DAY_MS


def _ident(futures: str = "BTCUSDT", spot: str | None = "BTCUSDT",
           mult: float | None = None, msrc: str | None = None) -> AssetIdentity:
    return AssetIdentity(
        canonical_id="BTC",
        display_symbol="BTC",
        binance_futures_symbol=futures,
        binance_spot_symbol=spot,
        contract_multiplier=mult,
        multiplier_source=msrc,
        mapping_confidence="HIGH",
        mapping_source="UNIQUE_SYMBOL",
    )


def _spot_row(open_ms: int, close: float, base_vol: float, quote_vol: float) -> list:
    """Full 12-field Binance spot kline row (idx 5 = base vol, idx 7 = quote vol)."""
    return [open_ms, "100", "110", "90", str(close), str(base_vol),
            open_ms + DAY_MS - 1, str(quote_vol), 10, "500", "52500", "0"]


def _window_rows(*, close0: float = 100.0, close_step: float = 0.5,
                 qv0: float = 1_000_000.0, qv_step: float = 10_000.0,
                 base0: float = 5.0) -> list[list]:
    return [_spot_row(WINDOW_START_MS + i * DAY_MS, close0 + i * close_step,
                      base0 + i, qv0 + i * qv_step) for i in range(60)]


def _qv(i: int, qv0: float = 1_000_000.0, qv_step: float = 10_000.0) -> float:
    return qv0 + i * qv_step


class _FakeSpot:
    """Fake ``spot_mod._spot_json``; counts exchangeInfo vs klines calls."""

    def __init__(self, *, symbols: list[str] | None = None, klines: list | None = None,
                 exchange_error: BaseException | None = None,
                 kline_error: BaseException | None = None,
                 hourly_rows: list | None = None) -> None:
        self.symbols = symbols if symbols is not None else ["BTCUSDT"]
        self.klines = klines if klines is not None else []
        self.exchange_error = exchange_error
        self.kline_error = kline_error
        self.hourly_rows = hourly_rows
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, path: str, params: dict) -> object:
        self.calls.append((path, dict(params)))
        if path == "/exchangeInfo":
            if self.exchange_error is not None:
                raise self.exchange_error
            return {"symbols": [{"symbol": s} for s in self.symbols]}
        if path == "/klines":
            if params.get("interval") == "1h" and self.hourly_rows is not None:
                return self.hourly_rows
            if self.kline_error is not None:
                raise self.kline_error
            return self.klines
        if path == "/ticker/24hr":
            return {"lastPrice": "129.5"}
        raise AssertionError(f"unexpected spot path {path!r}")

    def exchange_calls(self) -> int:
        return sum(1 for p, _ in self.calls if p == "/exchangeInfo")

    def kline_calls(self) -> int:
        return sum(1 for p, _ in self.calls if p == "/klines")


class _FakeFutures:
    """Fake ``binance_klines.fetch_klines`` returning Task-4-style candles."""

    def __init__(self, *, qv_mult: float = 3.0, close0: float = 200.0,
                 error: BaseException | None = None,
                 flat_spot_qv: float | None = None) -> None:
        self.qv_mult = qv_mult
        self.close0 = close0
        self.error = error
        self.flat_spot_qv = flat_spot_qv
        self.calls: list[tuple] = []

    async def __call__(self, symbol: str, interval: str, limit: int = 300,
                       end_ms: int | None = None) -> list[dict]:
        self.calls.append((symbol, interval, limit, end_ms))
        if self.error is not None:
            raise self.error
        base = (lambda i: self.flat_spot_qv) if self.flat_spot_qv is not None else _qv
        return [{"t": (WINDOW_START_MS + i * DAY_MS) * 1_000_000,
                 "o": 1.0, "h": 2.0, "l": 0.5, "c": self.close0 + i,
                 "v": 7.0 + i,
                 "qv": self.qv_mult * base(i)}
                for i in range(60)]


@pytest.fixture(autouse=True)
def _clean_caches():
    spot_mod.reset_cache()
    spot_mod.reset_spot_history_cache()
    yield
    spot_mod.reset_cache()
    spot_mod.reset_spot_history_cache()


@pytest.fixture()
def fake_futures(monkeypatch):
    fake = _FakeFutures()
    monkeypatch.setattr(
        "diveintocrypto_desktop.data.binance_klines.fetch_klines", fake)
    return fake


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def monotonic(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def clock(monkeypatch):
    clk = _Clock()
    monkeypatch.setattr(spot_mod.time, "monotonic", clk.monotonic)
    return clk


# ---------------------------------------------------------------------------
# 1. Original 48h snapshot behaviour is unchanged
# ---------------------------------------------------------------------------

def test_snapshot_constants_untouched():
    assert spot_mod.KLINE_LIMIT == 48
    assert spot_mod.CACHE_TTL == 60.0


@pytest.mark.asyncio
async def test_snapshot_48h_still_works(monkeypatch):
    rows_1h = [[0, 0, 0, 0, str(100.0 + i), 0, 0] for i in range(48)]
    fake = _FakeSpot(hourly_rows=[[0, 0, 0, 0, r[4], 0, 0] for r in rows_1h])
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    out = await spot_mod.snapshot(
        "BTCUSDT", 148.0, [100.0 + i for i in range(49)])
    assert out["premium_pct"] == pytest.approx((148.0 - 129.5) / 129.5 * 100)
    assert out["ret_spread_48h"] is not None
    assert fake.calls[0][1] == {"symbol": "BTCUSDT", "interval": "1h", "limit": 48}


@pytest.mark.asyncio
async def test_snapshot_1121_still_no_spot_market(monkeypatch):
    async def boom(path, params):
        raise RuntimeError('400 code=-1121, msg="Invalid symbol."')

    monkeypatch.setattr(spot_mod, "_spot_json", boom)
    out = await spot_mod.snapshot("ODDUSDT", 1.0, [1.0, 1.1])
    assert out == {"unavailable": "no_spot_market"}


# ---------------------------------------------------------------------------
# 2. 61 daily bars, UTC-day alignment, unclosed day excluded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_window_alignment_and_61_bar_fetch(monkeypatch, fake_futures):
    start, end = spot_mod._spot_window_ms(AS_OF_MS)
    assert (start, end) == (WINDOW_START_MS, WINDOW_END_MS)

    rows = _window_rows()
    today_row = _spot_row(WINDOW_END_MS, 999.0, 1.0, 999_999_999.0)  # unclosed today
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=rows + [today_row])
    monkeypatch.setattr(spot_mod, "_spot_json", fake)

    res = await spot_mod.spot_history(_ident(), AS_OF_MS)

    assert res.status == "OK", res
    assert res.reason_code is None
    assert res.as_of_ms == AS_OF_MS and res.stale is False
    data = res.data
    assert data.window_start_ms == WINDOW_START_MS
    assert data.window_end_ms == WINDOW_END_MS
    # Spot fetch: 1d, limit 61, cutoff excludes today.
    (path, params) = next(c for c in fake.calls if c[0] == "/klines")
    assert params["symbol"] == "BTCUSDT" and params["interval"] == "1d"
    assert params["limit"] == 61 and params["endTime"] == WINDOW_END_MS - 1
    # Futures leg at the SAME cutoff.
    assert fake_futures.calls == [("BTCUSDT", "1d", 61, WINDOW_END_MS)]
    # Today's monster bar is excluded from every sum/price.
    assert data.spot_quote_volume_24h == pytest.approx(_qv(59))
    assert data.spot_price == pytest.approx(100.0 + 59 * 0.5)
    assert data.spot_daily_bars == 60 and data.futures_daily_bars == 60
    assert 999_999_999.0 not in (data.spot_volume_30d, data.spot_quote_volume_24h)


@pytest.mark.asyncio
async def test_thirty_plus_thirty_sums_and_decay(monkeypatch, fake_futures):
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=_window_rows())
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert res.status == "OK", res
    data = res.data
    prev = sum(_qv(i) for i in range(30))
    last = sum(_qv(i) for i in range(30, 60))
    assert data.spot_volume_prev_30d == pytest.approx(prev)
    assert data.spot_volume_30d == pytest.approx(last)
    assert data.spot_volume_decay_30d == pytest.approx(last / prev)


@pytest.mark.asyncio
async def test_quote_volume_used_not_base(monkeypatch, fake_futures):
    """Base vols (~5) and quote vols (~1e6) differ hugely: sums must follow qv."""
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=_window_rows())
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)
    data = res.data
    base_last = sum(5.0 + i for i in range(30, 60))
    assert data.spot_volume_30d != pytest.approx(base_last)
    assert data.spot_volume_30d == pytest.approx(sum(_qv(i) for i in range(30, 60)))


@pytest.mark.asyncio
async def test_futures_spot_ratio_same_window(monkeypatch):
    fut = _FakeFutures(qv_mult=3.0)
    monkeypatch.setattr(
        "diveintocrypto_desktop.data.binance_klines.fetch_klines", fut)
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=_window_rows())
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert res.status == "OK", res
    data = res.data
    spot_last = sum(_qv(i) for i in range(30, 60))
    assert data.futures_volume_30d == pytest.approx(3.0 * spot_last)
    assert data.futures_spot_volume_ratio_30d == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# 3. Multiplier-normalised premium (decimal); volumes never scaled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_premium_normalised_by_verified_multiplier(monkeypatch):
    fut = _FakeFutures(qv_mult=3.0, close0=6.0, flat_spot_qv=1_000_000.0)
    monkeypatch.setattr(
        "diveintocrypto_desktop.data.binance_klines.fetch_klines", fut)
    rows = [_spot_row(WINDOW_START_MS + i * DAY_MS, 0.005, 5.0, 1_000_000.0)
            for i in range(60)]
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=rows)
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    ident = _ident(mult=1000.0, msrc="MANUAL")
    res = await spot_mod.spot_history(ident, AS_OF_MS)
    assert res.status == "OK", res
    data = res.data
    assert data.multiplier_applied == pytest.approx(1000.0)
    assert data.canonical_futures_price == pytest.approx((6.0 + 59) / 1000.0)
    assert data.premium == pytest.approx(((6.0 + 59) / 1000.0 - 0.005) / 0.005)
    # Volumes stay quote-notional: no 1000x scaling on either leg.
    assert data.spot_volume_30d == pytest.approx(30 * 1_000_000.0)
    assert data.futures_spot_volume_ratio_30d == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_premium_null_without_verified_multiplier(monkeypatch, fake_futures):
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=_window_rows())
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)  # mult None
    assert res.status == "OK", res  # premium N/A does not fail the window
    assert res.data.premium is None
    assert res.data.canonical_futures_price is None
    assert res.data.multiplier_applied is None


# ---------------------------------------------------------------------------
# 4. No spot market -> NOT_APPLICABLE; never guess from the futures symbol
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_spot_symbol_is_not_applicable_without_http(monkeypatch, fake_futures):
    async def boom(path, params):
        raise AssertionError("no HTTP may happen without a verified spot symbol")

    monkeypatch.setattr(spot_mod, "_spot_json", boom)
    res = await spot_mod.spot_history(_ident(spot=None), AS_OF_MS)
    assert res.status == "NOT_APPLICABLE" and res.data is None
    assert res.reason_code == "no_spot_market"
    assert fake_futures.calls == []


@pytest.mark.asyncio
async def test_exchange_info_absent_is_not_applicable_with_negative_cache(
        monkeypatch, fake_futures, clock):
    fake = _FakeSpot(symbols=["ETHUSDT"], klines=_window_rows())  # no BTCUSDT
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    first = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert first.status == "NOT_APPLICABLE" and first.data is None
    assert first.reason_code == "no_spot_market"
    assert fake.exchange_calls() == 1 and fake.kline_calls() == 0
    # Negative cache: no second upstream call.
    second = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert second.status == "NOT_APPLICABLE"
    assert fake.exchange_calls() == 1
    # After 900s the negative entry expires; absence is re-derived from the
    # still-fresh (3600s) positive list, so still no upstream call.
    clock.advance(901.0)
    third = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert third.status == "NOT_APPLICABLE"
    assert fake.exchange_calls() == 1
    # Past the 3600s positive TTL the list itself is re-fetched upstream.
    clock.advance(2701.0)
    fourth = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert fourth.status == "NOT_APPLICABLE"
    assert fake.exchange_calls() == 2


@pytest.mark.asyncio
async def test_no_guessing_futures_symbol(monkeypatch):
    seen_spot: list[str] = []

    async def spy(path, params):
        if path == "/exchangeInfo":
            return {"symbols": [{"symbol": "PEPEUSDT"}]}
        seen_spot.append(params["symbol"])
        return [_spot_row(WINDOW_START_MS + i * DAY_MS, 0.001, 5.0, 500_000.0)
                for i in range(60)]

    monkeypatch.setattr(spot_mod, "_spot_json", spy)
    fut = _FakeFutures()
    monkeypatch.setattr(
        "diveintocrypto_desktop.data.binance_klines.fetch_klines", fut)
    ident = _ident(futures="1000PEPEUSDT", spot="PEPEUSDT",
                   mult=1000.0, msrc="MANUAL")
    res = await spot_mod.spot_history(ident, AS_OF_MS)
    assert res.status in ("OK", "PARTIAL"), res
    assert seen_spot and all(s == "PEPEUSDT" for s in seen_spot)
    assert "1000PEPEUSDT" not in seen_spot
    assert fut.calls and all(c[0] == "1000PEPEUSDT" for c in fut.calls)


# ---------------------------------------------------------------------------
# 5. 451/429/timeout and unconfirmed -1121 are UNAVAILABLE (no neg. cache)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exchange_info_unreachable_but_pair_exists_is_unavailable(
        monkeypatch, fake_futures):
    """The pair exists, but metadata is unreachable: UNAVAILABLE, not N/A."""
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=_window_rows(),
                     exchange_error=RuntimeError("451 Client Error: restricted location"))
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert res.status == "UNAVAILABLE" and res.data is None
    assert res.reason_code == "spot_unreachable"
    assert spot_mod._negative_cache == {}  # 451 never writes negative cache
    # Recovery on the next call (no 15-minute penalty).
    fake.exchange_error = None
    ok = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert ok.status == "OK", ok


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    TransientUpstreamError(429, None),
    asyncio.TimeoutError(),
    RuntimeError("429 Too Many Requests"),
    RuntimeError("spot read timeout"),
])
async def test_spot_klines_451_429_timeout_are_unavailable(
        monkeypatch, fake_futures, error):
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=_window_rows(), kline_error=error)
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert res.status == "UNAVAILABLE" and res.data is None
    assert res.reason_code == "spot_unreachable"
    assert spot_mod._negative_cache == {}


@pytest.mark.asyncio
async def test_klines_1121_after_confirmation_is_unavailable(
        monkeypatch, fake_futures):
    """exchangeInfo confirms the market, klines answer -1121: still UNAVAILABLE."""
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=[],
                     kline_error=RuntimeError('400 code=-1121, msg="Invalid symbol."'))
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert res.status == "UNAVAILABLE" and res.data is None
    assert res.reason_code == "spot_symbol_unconfirmed"
    assert spot_mod._negative_cache == {}


# ---------------------------------------------------------------------------
# 6. exchangeInfo caching: 3600s positive, host-keyed, concurrently shared
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_positive_cache_3600_host_switch_and_expiry(
        monkeypatch, fake_futures, clock):
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=_window_rows())
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    assert (await spot_mod.spot_history(_ident(), AS_OF_MS)).status == "OK"
    assert (await spot_mod.spot_history(_ident(), AS_OF_MS)).status == "OK"
    assert fake.exchange_calls() == 1  # positive hit
    clock.advance(3599.0)
    assert (await spot_mod.spot_history(_ident(), AS_OF_MS)).status == "OK"
    assert fake.exchange_calls() == 1  # still inside the 3600s TTL
    clock.advance(2.0)
    assert (await spot_mod.spot_history(_ident(), AS_OF_MS)).status == "OK"
    assert fake.exchange_calls() == 2  # expired -> one refetch
    # Host switch misses the cache even though the symbol list is identical.
    monkeypatch.setattr(spot_mod, "SPOT_V3", "https://other-spot-host.example/api/v3")
    assert (await spot_mod.spot_history(_ident(), AS_OF_MS)).status == "OK"
    assert fake.exchange_calls() == 3


@pytest.mark.asyncio
async def test_concurrent_histories_share_one_exchange_info(monkeypatch):
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT"]
    fake = _FakeSpot(symbols=symbols, klines=_window_rows())
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    fut = _FakeFutures()
    monkeypatch.setattr(
        "diveintocrypto_desktop.data.binance_klines.fetch_klines", fut)
    idents = [_ident(futures=s, spot=s) for s in symbols]
    results = await asyncio.gather(
        *(spot_mod.spot_history(i, AS_OF_MS) for i in idents))
    assert all(r.status == "OK" for r in results)
    assert fake.exchange_calls() == 1  # one shared fetch for the whole batch


# ---------------------------------------------------------------------------
# 7. PARTIAL: short history on the spot leg, failed futures leg
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_on_short_spot_history(monkeypatch, fake_futures):
    rows = _window_rows()[20:]  # most recent 40 of the 60 window days
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=rows)
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert res.status == "PARTIAL" and res.data is not None
    assert res.reason_code == "spot_history_incomplete"
    data = res.data
    assert data.spot_daily_bars == 40
    assert data.spot_quote_volume_24h == pytest.approx(_qv(59))
    assert data.spot_price == pytest.approx(100.0 + 59 * 0.5)
    assert data.spot_volume_30d == pytest.approx(sum(_qv(i) for i in range(30, 60)))
    assert data.spot_volume_prev_30d is None and data.spot_volume_decay_30d is None


@pytest.mark.asyncio
async def test_futures_leg_failure_is_partial(monkeypatch):
    fut = _FakeFutures(error=RuntimeError("futures fapi down"))
    monkeypatch.setattr(
        "diveintocrypto_desktop.data.binance_klines.fetch_klines", fut)
    fake = _FakeSpot(symbols=["BTCUSDT"], klines=_window_rows())
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert res.status == "PARTIAL" and res.data is not None
    assert res.reason_code == spot_mod.FUTURES_LEG_UNREACHABLE
    assert res.data.spot_volume_30d is not None  # spot leg intact
    assert res.data.futures_volume_30d is None
    assert res.data.futures_spot_volume_ratio_30d is None
    assert res.data.premium is None


# ---------------------------------------------------------------------------
# 8. F04: request_context threading (F03 handoff) + 403 is UNAVAILABLE, not N/A
# ---------------------------------------------------------------------------


class _CtxSpotJson:
    """Fake ``get_json`` replacement capturing (url, params, request_context)."""

    def __init__(self, *, symbols=("BTCUSDT",), klines=None) -> None:
        self.symbols = list(symbols)
        self.klines = klines
        self.calls: list[tuple] = []

    async def __call__(self, url, params=None, request_context=None):
        self.calls.append((url, dict(params or {}), request_context))
        if url.endswith("/exchangeInfo"):
            return {"symbols": [{"symbol": s} for s in self.symbols]}
        if "/klines" in url:
            assert self.klines is not None
            return self.klines
        raise AssertionError(f"unexpected spot url {url!r}")


@pytest.mark.asyncio
async def test_spot_history_threads_request_context(monkeypatch, fake_futures):
    from diveintocrypto_desktop.shortlab.request_budget import make_request_context

    fake = _CtxSpotJson(klines=_window_rows())
    monkeypatch.setattr(spot_mod, "get_json", fake)
    ctx = make_request_context(
        None, job_type="entry", host="spot", trace_id="spot-trace-1",
        identity_snapshot_id="isl-abc",
    )
    res = await spot_mod.spot_history(_ident(), AS_OF_MS, request_context=ctx)
    assert res.status == "OK", res
    assert len(fake.calls) == 2  # exchangeInfo + klines, zero real sends
    for url, params, got_ctx in fake.calls:
        assert got_ctx is not None
        assert got_ctx.trace_id == "spot-trace-1"
        assert got_ctx.identity_snapshot_id == "isl-abc"


@pytest.mark.asyncio
async def test_spot_history_budget_exhaustion_is_unavailable_not_raise(
        monkeypatch, fake_futures):
    """A denied send surfaces as UNAVAILABLE (queued), never an exception."""
    from diveintocrypto_desktop.shortlab.request_budget import (
        RequestBudget,
        make_request_context,
    )

    class _DeadSession:
        """Fake session failing fast: proves budget accounting, zero network."""

        class _CM:
            async def __aenter__(self):
                raise OSError("no network in tests")

            async def __aexit__(self, *exc):
                return False

        def get(self, url, params=None):
            return _DeadSession._CM()

    async def _dead_session():
        return _DeadSession()

    monkeypatch.setattr(
        "diveintocrypto_desktop.data.http.get_session", _dead_session)
    monkeypatch.setattr(
        "diveintocrypto_desktop.data.binance_klines.fetch_klines", _FakeFutures())
    budget = RequestBudget(max_sends=1, window_ms=3_600_000)
    ctx = make_request_context(budget, job_type="entry", host="spot",
                               endpoint_family="spot")
    first = await spot_mod.spot_history(_ident(), AS_OF_MS, request_context=ctx)
    assert first.status == "UNAVAILABLE"
    assert budget.sent_attempts == 1
    second = await spot_mod.spot_history(_ident(), AS_OF_MS, request_context=ctx)
    assert second.status == "UNAVAILABLE"
    assert budget.sent_attempts == 1  # no hidden sends past the denial
    assert spot_mod._negative_cache == {}  # budget denial never proves absence


@pytest.mark.asyncio
async def test_spot_403_is_unavailable_never_not_applicable(monkeypatch, fake_futures):
    class _Forbidden(Exception):
        status = 403

    fake = _FakeSpot(symbols=["BTCUSDT"], klines=_window_rows(),
                     exchange_error=_Forbidden("403 Client Error: Forbidden"))
    monkeypatch.setattr(spot_mod, "_spot_json", fake)
    res = await spot_mod.spot_history(_ident(), AS_OF_MS)
    assert res.status == "UNAVAILABLE" and res.data is None
    assert res.reason_code == "spot_unreachable"
    assert spot_mod._negative_cache == {}
