"""Task 4: K-line quote-volume (qv) retention (Short-Lab Phase 1).

Contract (ShortLab_Detailed_Design_CN.md §5.1, ShortLab_Implementation_Plan_CN.md Task 4):

- The kline DTO gains ``qv: float | None`` (Binance raw quote asset volume,
  raw row index 7). ``v`` keeps its base-volume semantics (raw index 5).
- ``qv`` is aligned by open time ``t`` (never by array position), because the
  anti-repaint trim drops unfinished candles and raw/parsed order may differ.
- Duplicate open times in raw, or a parsed record whose time is missing from
  raw, yield ``qv=None`` plus a logged data-quality reason.
- ``to_dataframe`` input semantics are unchanged (60 indicators still read
  base volume ``v`` only).

All tests are offline: the raw Binance page is faked, the real Crypcodile
``parse_klines_page`` is used except where a missing-timestamp parse result
is explicitly simulated.
"""

from __future__ import annotations

import json
import logging
import pathlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from diveintocrypto_desktop.data import binance_klines as kl

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "shortlab"
DAY_MS = 86_400_000


def _fixture_row() -> list:
    return json.loads((FIXTURES / "futures_1d_kline.json").read_text())


def _row(open_ms: int, base_vol: str | float, quote_vol: str | float,
         *, close_ms: int | None = None, price: str = "100") -> list:
    """Full 12-field Binance kline row (index 5 = base vol, index 7 = quote vol)."""
    return [open_ms, price, "110", "90", "105", str(base_vol),
            close_ms if close_ms is not None else open_ms + DAY_MS - 1,
            str(quote_vol), 10, "500", "52500", "0"]


def _raw_page(n: int, *, base_open_ms: int, base_vol: float = 1000.0,
              quote_vol: float = 105000.0) -> list[list]:
    return [_row(base_open_ms + i * DAY_MS, base_vol + i, quote_vol + 1000 * i)
            for i in range(n)]


def _finished_end_ms(raw: list[list]) -> int:
    """An ``end_ms`` as-of which every row in ``raw`` is a finished candle."""
    return max(int(r[0]) for r in raw) + DAY_MS


# ---------------------------------------------------------------------------
# v vs qv semantics (Task 0 fixture)
# ---------------------------------------------------------------------------


def test_fixture_base_vs_quote_volume_differ():
    row = _fixture_row()
    assert float(row[5]) != float(row[7])


@pytest.mark.asyncio
async def test_fetch_klines_retains_qv_and_keeps_base_volume_v():
    raw = _raw_page(5, base_open_ms=1699920000000)
    end_ms = _finished_end_ms(raw)

    async def fake_fetch_raw(symbol, interval, limit, end_time_ms):
        assert limit == 3 + 1  # one extra row for the anti-repaint trim
        return raw

    with patch.object(kl, "_fetch_raw", fake_fetch_raw):
        candles = await kl.fetch_klines("BTCUSDT", "1d", limit=3, end_ms=end_ms)

    assert len(candles) == 3
    for candle, row in zip(candles, raw[-3:]):
        assert candle["v"] == float(row[5])  # base volume unchanged
        assert candle["qv"] == float(row[7])  # quote volume retained
        assert candle["v"] != candle["qv"]
        # Old {t,o,h,l,c,v} values untouched.
        assert candle["t"] == int(row[0]) * 1_000_000
        assert (candle["o"], candle["h"], candle["l"], candle["c"]) == (
            float(row[1]), float(row[2]), float(row[3]), float(row[4]))


@pytest.mark.asyncio
async def test_fetch_klines_range_retains_qv():
    raw = _raw_page(4, base_open_ms=1699920000000)

    async def fake_fetch_raw_range(symbol, interval, start_ms, end_ms, limit):
        return raw

    with patch.object(kl, "_fetch_raw_range", fake_fetch_raw_range):
        candles = await kl.fetch_klines_range("BTCUSDT", "1d",
                                              start_ms=1699920000000,
                                              end_ms=1699920000000 + 4 * DAY_MS)

    assert len(candles) == 4
    for candle, row in zip(candles, raw):
        assert candle["v"] == float(row[5])
        assert candle["qv"] == float(row[7])
        assert candle["v"] != candle["qv"]


# ---------------------------------------------------------------------------
# Unfinished-candle trim keeps qv aligned (head and tail)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trailing_unfinished_candle_dropped_qv_kept():
    raw = _raw_page(4, base_open_ms=1699920000000)
    # Last row opens exactly at end_ms -> still in progress -> dropped.
    end_ms = int(raw[-1][0])
    assert int(raw[-2][0]) + DAY_MS <= end_ms  # second-to-last is finished

    async def fake_fetch_raw(symbol, interval, limit, end_time_ms):
        return raw

    with patch.object(kl, "_fetch_raw", fake_fetch_raw):
        candles = await kl.fetch_klines("BTCUSDT", "1d", limit=10, end_ms=end_ms)

    assert [c["t"] // 1_000_000 for c in candles] == [int(r[0]) for r in raw[:-1]]
    for candle, row in zip(candles, raw[:-1]):
        assert candle["qv"] == float(row[7])


@pytest.mark.asyncio
async def test_unfinished_candle_dropped_when_not_trailing_shuffled():
    """Out-of-order page: the unfinished row is at the head; qv still aligns by t."""
    raw = _raw_page(4, base_open_ms=1699920000000)
    end_ms = int(raw[-1][0])  # last chronological row unfinished
    shuffled = [raw[-1], raw[0], raw[2], raw[1]]

    async def fake_fetch_raw(symbol, interval, limit, end_time_ms):
        return shuffled

    with patch.object(kl, "_fetch_raw", fake_fetch_raw):
        candles = await kl.fetch_klines("BTCUSDT", "1d", limit=10, end_ms=end_ms)

    # The unfinished open time is gone wherever it was positioned...
    assert int(raw[-1][0]) not in {c["t"] // 1_000_000 for c in candles}
    # ...and every surviving candle carries its OWN quote volume.
    by_t = {c["t"] // 1_000_000: c for c in candles}
    assert len(by_t) == 3
    for row in raw[:-1]:
        assert by_t[int(row[0])]["qv"] == float(row[7])


# ---------------------------------------------------------------------------
# Duplicate / missing timestamps -> qv=None + data-quality reason
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_open_time_gives_null_qv_and_logs_reason(caplog):
    base = _raw_page(3, base_open_ms=1699920000000)
    dupe = _row(int(base[1][0]), 9999.0, 888888.0)  # same open, different qv
    raw = [base[0], base[1], dupe, base[2]]
    end_ms = _finished_end_ms(raw)

    async def fake_fetch_raw(symbol, interval, limit, end_time_ms):
        return raw

    with patch.object(kl, "_fetch_raw", fake_fetch_raw):
        with caplog.at_level(logging.WARNING, logger=kl.__name__):
            candles = await kl.fetch_klines("BTCUSDT", "1d", limit=10, end_ms=end_ms)

    dup_t = int(base[1][0])
    dupes = [c for c in candles if c["t"] // 1_000_000 == dup_t]
    assert len(dupes) == 2
    assert all(c["qv"] is None for c in dupes)
    assert any("QV_DUPLICATE_OPEN_TIME" in r.message for r in caplog.records)
    # Non-duplicated times are unaffected.
    singles = [c for c in candles if c["t"] // 1_000_000 != dup_t]
    assert singles and all(c["qv"] is not None for c in singles)


@pytest.mark.asyncio
async def test_parsed_time_missing_from_raw_gives_null_qv_position_independent(caplog):
    """Parsed order differs from raw order and one parsed time has no raw row."""
    raw = _raw_page(3, base_open_ms=1699920000000)
    recs = [
        SimpleNamespace(exchange_ts=int(raw[2][0]) * 1_000_000, open=1.0,
                        high=2.0, low=0.5, close=1.5, volume=float(raw[2][5])),
        # Parsed time with no matching raw row (must NOT inherit a neighbour's qv).
        SimpleNamespace(exchange_ts=(int(raw[0][0]) - DAY_MS) * 1_000_000, open=1.0,
                        high=2.0, low=0.5, close=1.5, volume=42.0),
        SimpleNamespace(exchange_ts=int(raw[0][0]) * 1_000_000, open=1.0,
                        high=2.0, low=0.5, close=1.5, volume=float(raw[0][5])),
        # Parsed record without any timestamp.
        SimpleNamespace(exchange_ts=None, open=1.0,
                        high=2.0, low=0.5, close=1.5, volume=7.0),
    ]

    async def fake_fetch_raw(symbol, interval, limit, end_time_ms):
        return raw

    def fake_parse(raw_arg, venue, symbol, interval, local_ts):
        return list(recs)

    with patch.object(kl, "_fetch_raw", fake_fetch_raw), \
         patch.object(kl, "parse_klines_page", fake_parse):
        with caplog.at_level(logging.WARNING, logger=kl.__name__):
            candles = await kl.fetch_klines("BTCUSDT", "1d", limit=10,
                                            end_ms=_finished_end_ms(raw))

    by_t = {c["t"]: c for c in candles if c["t"] is not None}
    # Position-independent alignment: each parsed time got its own raw qv.
    assert by_t[int(raw[2][0]) * 1_000_000]["qv"] == float(raw[2][7])
    assert by_t[int(raw[0][0]) * 1_000_000]["qv"] == float(raw[0][7])
    # Unmatched / missing timestamps -> null + reason, never a neighbour's qv.
    assert by_t[(int(raw[0][0]) - DAY_MS) * 1_000_000]["qv"] is None
    null_ts = [c for c in candles if c["t"] is None]
    assert len(null_ts) == 1 and null_ts[0]["qv"] is None
    messages = [r.message for r in caplog.records]
    assert any("QV_UNMATCHED_TIMESTAMP" in m for m in messages)
    assert any("QV_MISSING_TIMESTAMP" in m for m in messages)


# ---------------------------------------------------------------------------
# Indicator input semantics unchanged
# ---------------------------------------------------------------------------

def test_to_dataframe_ignores_qv():
    candles = [{"t": 1, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5,
                "v": 10.0, "qv": 105.0},
               {"t": 2, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5,
                "v": 11.0, "qv": None}]
    df = kl.to_dataframe(candles)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["volume"].tolist() == [10.0, 11.0]
