"""Task 12: lightweight Entry builder + budget (Short-Lab Phase 2).

Covers ``ShortLab_Implementation_Plan_CN.md`` Task 12 and
``ShortLab_Detailed_Design_CN.md`` sections 6.1 / 15 / 15.1:

- Six-block mapping (consensus 30 / MTF 20 / micro 20 / regime 10 /
  failed-bounce 10 / 30D-funding 10 = 100); any missing block, an inactive
  microstructure bundle or an incomplete 30D funding window forces
  ``entryScore=None`` (never 0).
- The 12-request timeframe list reuses ``data/binance_klines.TF_LIST``
  (identical to ``scan/constants.ALL_TFS``); per-TF counting with fakes; a
  missing timeframe makes the Entry unavailable; no second hardcoded list.
- Cold-cache cost is 12 klines + 1 OI + 4 ratios + 1 funding = 18 calls per
  symbol; the CVD / ticker / divergence / panel-only extras (book, L/S term
  structure, basis, spot) are never fetched and the full ``build_symbol``
  chain is never entered.
- One round: 10 symbols, concurrency 2, budget 240, cache TTL 3600s.
  Faked batches cover 10 coins (<= 180 base requests), 30 coins (the round
  stops at 240 with the rest queued and no further network) and a 429 retry
  batch (every real attempt spends budget through the shared limiter path).
  Exhaustion yields ``ENTRY_BUDGET_EXHAUSTED`` + null.
- ``build_symbol(end_ms=old)`` with live OI / ratio / funding fixtures is
  shown to mix past candles with present positioning and is rejected for
  Short-Lab history; replay reads only the stored snapshot, offline.
- Snapshots persist through the Task 2 ``save_entry`` interface (real tmp
  DuckDB, no network) with weights hash, engine version / config hash,
  primary TF, six frozen inputs / contributions and per-block
  fetched-at / as-of; a DB read-back recomputes the identical Entry with
  all network fakes armed to raise, and the ``entry_snapshot_id`` links a
  score row. Two as-of instants yield two independent snapshots.

All network access is faked; no live requests.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import re
from unittest.mock import patch

import pytest

from diveintocrypto_desktop.data import binance_klines as klines_mod
from diveintocrypto_desktop.data import funding as funding_mod
from diveintocrypto_desktop.data import open_interest as oi_mod
from diveintocrypto_desktop.data import ratios as ratios_mod
from diveintocrypto_desktop.data.http import TransientUpstreamError
from diveintocrypto_desktop.scan import constants as scan_constants
from diveintocrypto_desktop.scan import symbol_builder as symbol_builder_mod
from diveintocrypto_desktop.shortlab import entry as entry_mod
from diveintocrypto_desktop.shortlab.config import config_hash, load_shortlab_config
from diveintocrypto_desktop.shortlab.entry import (
    ENTRY_BUDGET_EXHAUSTED,
    ENTRY_REQUIRED_BLOCKS,
    EntryBudget,
    HistoricalReplayError,
    build_entry_snapshot,
    build_historical_entry,
    recompute_entry_from_record,
    run_entry_batch,
    save_entry_snapshot,
    score_entry,
    score_entry_detailed,
)
from diveintocrypto_desktop.shortlab.repository import (
    REQUIRED_ENTRY_META_BLOCKS,
    FeatureSnapshotRecord,
    ScoreSnapshotRecord,
    ShortLabRepository,
)
from diveintocrypto_desktop.shortlab.scoring.versions import ENTRY_VERSION

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
ASOF_MS = 1_720_000_000_000
FETCHED_MS = ASOF_MS - 60_000

ENTRY_SOURCE = pathlib.Path(entry_mod.__file__).read_text(encoding="utf-8")


def _full_snapshot(**overrides):
    base = {
        "finalSignal": "SELL",
        "confidence": 80,
        "mtfConfluence": {"score": -60.0, "direction": -1, "gate": True},
        "microstructure": {"score": -50.0, "active": 3, "label": "SELL"},
        "regime": {"regime": "TREND", "adaptive_score": -12.5},
    }
    base.update(overrides)
    return base


def _full_funding(**overrides):
    base = {"positive_ratio_30d": 1.0, "complete": True}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Six-block mapping (design 15.1)
# ---------------------------------------------------------------------------


class TestScoreMapping:
    def test_all_max_scores_100(self):
        total, components, missing = score_entry_detailed(
            _full_snapshot(
                finalSignal="STRONG_SELL",
                confidence=100,
                mtfConfluence={"score": -100.0, "direction": -1, "gate": True},
                microstructure={"score": -100.0, "active": 5, "label": "STRONG_SELL"},
                regime={"regime": "TREND", "adaptive_score": -3.0},
            ),
            _full_funding(),
            True,
        )
        assert missing == ()
        assert components == {
            "consensus": 30.0,  # 30 * 1.0 + 3 clamped to 30
            "mtf": 20.0,
            "micro": 20.0,
            "regime": 10.0,
            "failed_bounce": 10.0,
            "funding": 10.0,
        }
        assert total == 100.0
        assert score_entry(_full_snapshot(finalSignal="STRONG_SELL", confidence=100,
                                          mtfConfluence={"score": -100.0, "direction": -1, "gate": True},
                                          microstructure={"score": -100.0, "active": 5, "label": "STRONG_SELL"},
                                          regime={"regime": "TREND", "adaptive_score": -3.0}),
                           _full_funding(), True) == 100.0

    def test_consensus_only_sell_contributes(self):
        assert score_entry_detailed(
            _full_snapshot(finalSignal="BUY", confidence=90), _full_funding(), False
        )[1]["consensus"] == 0.0
        assert score_entry_detailed(
            _full_snapshot(finalSignal="NEUTRAL", confidence=50), _full_funding(), False
        )[1]["consensus"] == 0.0
        # 30 * 80 / 100 = 24
        assert score_entry_detailed(
            _full_snapshot(finalSignal="SELL", confidence=80), _full_funding(), False
        )[1]["consensus"] == 24.0
        # STRONG_SELL adds 3: 30 * 50 / 100 + 3 = 18
        assert score_entry_detailed(
            _full_snapshot(finalSignal="STRONG_SELL", confidence=50), _full_funding(), False
        )[1]["consensus"] == 18.0

    def test_mtf_direction_gate(self):
        # direction != -1 forces 0 even with a deeply negative score.
        _, components, _ = score_entry_detailed(
            _full_snapshot(mtfConfluence={"score": -90.0, "direction": 1, "gate": True}),
            _full_funding(), False,
        )
        assert components["mtf"] == 0.0
        _, components, _ = score_entry_detailed(
            _full_snapshot(mtfConfluence={"score": -90.0, "direction": 0, "gate": True}),
            _full_funding(), False,
        )
        assert components["mtf"] == 0.0
        # Failed gate caps at 10: raw 20 * 0.9 = 18 -> 10.
        _, components, _ = score_entry_detailed(
            _full_snapshot(mtfConfluence={"score": -90.0, "direction": -1, "gate": False}),
            _full_funding(), False,
        )
        assert components["mtf"] == 10.0
        # Passing gate keeps the raw value: 20 * 0.6 = 12.
        _, components, _ = score_entry_detailed(
            _full_snapshot(mtfConfluence={"score": -60.0, "direction": -1, "gate": True}),
            _full_funding(), False,
        )
        assert components["mtf"] == 12.0

    def test_micro_inactive_is_unavailable(self):
        total, components, missing = score_entry_detailed(
            _full_snapshot(microstructure={"score": -80.0, "active": 0, "label": "NEUTRAL"}),
            _full_funding(), False,
        )
        assert total is None
        assert components["micro"] is None
        assert "micro" in missing
        # Active bundle scales: 20 * 0.5 = 10.
        _, components, _ = score_entry_detailed(
            _full_snapshot(microstructure={"score": -50.0, "active": 2, "label": "SELL"}),
            _full_funding(), False,
        )
        assert components["micro"] == 10.0

    def test_regime_mapping(self):
        cases = [
            ({"regime": "TREND", "adaptive_score": -0.5}, 10.0),
            ({"regime": "MIXED", "adaptive_score": -0.5}, 5.0),
            ({"regime": "RANGE", "adaptive_score": -0.5}, 0.0),
            ({"regime": "TREND", "adaptive_score": 2.0}, 0.0),
            ({"regime": "MIXED", "adaptive_score": 0.0}, 0.0),
        ]
        for regime_block, expected in cases:
            _, components, _ = score_entry_detailed(
                _full_snapshot(regime=regime_block), _full_funding(), False
            )
            assert components["regime"] == expected, regime_block

    def test_failed_bounce_and_funding(self):
        # 24 + 12 + 10 + 10 + 10 + 10 = 76 with the bounce, 66 without.
        assert score_entry(_full_snapshot(), _full_funding(), True) == 76.0
        assert score_entry(_full_snapshot(), _full_funding(), False) == 66.0
        assert score_entry(_full_snapshot(), _full_funding(), None) is None
        # Funding scales with the positive ratio: 10 * 0.7 = 7.
        assert score_entry(
            _full_snapshot(), _full_funding(positive_ratio_30d=0.7), False
        ) == 66.0 - 10.0 + 7.0
        # Incomplete 30D funding is unavailable, never a partial ratio.
        assert score_entry(
            _full_snapshot(), _full_funding(positive_ratio_30d=0.9, complete=False), False
        ) is None

    @pytest.mark.parametrize(
        "drop",
        ["consensus", "mtf", "micro", "regime", "failed_bounce", "funding"],
    )
    def test_any_missing_block_forces_null(self, drop):
        snapshot = _full_snapshot()
        funding = _full_funding()
        bounce: bool | None = False
        if drop == "consensus":
            snapshot = _full_snapshot(finalSignal=None)
        elif drop == "mtf":
            snapshot = _full_snapshot(mtfConfluence=None)
        elif drop == "micro":
            snapshot = _full_snapshot(microstructure={"score": -10.0, "active": 0})
        elif drop == "regime":
            snapshot = _full_snapshot(regime=None)
        elif drop == "failed_bounce":
            bounce = None
        elif drop == "funding":
            funding = _full_funding(complete=False)
        total, _, missing = score_entry_detailed(snapshot, funding, bounce)
        assert total is None
        assert drop in missing

    def test_deterministic(self):
        first = score_entry(_full_snapshot(), _full_funding(), True)
        for _ in range(50):
            assert score_entry(_full_snapshot(), _full_funding(), True) == first


# ---------------------------------------------------------------------------
# Fake market plumbing
# ---------------------------------------------------------------------------


def _candle(moment_ms: int, price: float) -> dict:
    return {
        "t": moment_ms * 1_000_000,
        "o": price,
        "h": price * 1.01,
        "l": price * 0.99,
        "c": price,
        "v": 10.0,
        "qv": 1000.0,
    }


def _daily_candles(count: int, start_price: float = 100.0, step: float = -0.4) -> list[dict]:
    start = ASOF_MS - count * DAY_MS
    return [_candle(start + i * DAY_MS, start_price + i * step) for i in range(count)]


def _tf_candles(count_per_tf: int = 25) -> dict[str, list[dict]]:
    return {tf: _daily_candles(count_per_tf) for tf in klines_mod.TF_LIST}


def _funding_events(count: int = 90, rate: float = 0.0001) -> list[dict]:
    start = ASOF_MS - 30 * DAY_MS
    step = (30 * DAY_MS) // count
    return [
        {"t": start + i * step, "funding_rate": rate, "mark_price": 50000.0}
        for i in range(count)
    ]


def _oi_points(count: int = 48) -> list[dict]:
    base = (ASOF_MS - count * 5 * 60_000) * 1_000_000
    return [
        {"t": base + i * 5 * 60_000 * 1_000_000, "oi": 1000.0 + i, "oi_value": 50_000_000.0}
        for i in range(count)
    ]


class _CallLog(dict):
    def bump(self, key):
        self[key] = self.get(key, 0) + 1


def _install_fakes(
    monkeypatch,
    calls: _CallLog,
    candles_by_tf: dict[str, list[dict]] | None = None,
    funding_events: list[dict] | None = None,
    assembled: dict | None = None,
    klines_fail_once: set[str] | None = None,
    missing_tfs: set[str] | None = None,
):
    candles_by_tf = candles_by_tf if candles_by_tf is not None else _tf_candles()
    funding_events = funding_events if funding_events is not None else _funding_events()
    klines_fail_once = set(klines_fail_once or set())
    missing_tfs = set(missing_tfs or set())
    failed_once: set[tuple[str, str]] = set()

    async def fake_fetch_klines(symbol, interval, limit=300, end_ms=None):
        calls.bump(("klines", symbol, interval))
        if interval in missing_tfs:
            return []
        if interval in klines_fail_once and (symbol, interval) not in failed_once:
            failed_once.add((symbol, interval))
            raise TransientUpstreamError(429, 0.0)
        return [dict(c) for c in candles_by_tf[interval]]

    async def fake_oi(symbol, period="5m", limit=48):
        calls.bump(("oi", symbol))
        return [dict(p) for p in _oi_points()]

    def fake_ratio(kind):
        async def _fetch(symbol, period="5m", limit=48):
            calls.bump(("ratio", symbol, kind))
            return [1.2] * 48
        return _fetch

    async def fake_funding_range(symbol, start_ms, end_ms, limit=1000):
        calls.bump(("funding", symbol))
        return [dict(e) for e in funding_events]

    monkeypatch.setattr(klines_mod, "fetch_klines", fake_fetch_klines)
    monkeypatch.setattr(oi_mod, "fetch_oi_hist", fake_oi)
    monkeypatch.setattr(ratios_mod, "global_account_ls", fake_ratio("glob"))
    monkeypatch.setattr(ratios_mod, "top_account_ls", fake_ratio("acc"))
    monkeypatch.setattr(ratios_mod, "top_position_ls", fake_ratio("pos"))
    monkeypatch.setattr(ratios_mod, "taker_ls", fake_ratio("taker"))
    monkeypatch.setattr(funding_mod, "funding_history_range", fake_funding_range)
    if assembled is not None:
        async def fake_to_thread(fn, *args, **kwargs):
            calls.bump(("assemble", args[0] if args else ""))
            return dict(assembled)
        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)


def _canned_assembled(**overrides) -> dict:
    base = {
        "finalSignal": "SELL",
        "confidence": 80,
        "mtfConfluence": {"score": -60.0, "direction": -1, "gate": True},
        "microstructure": {"score": -50.0, "active": 3, "label": "SELL"},
        "regime": {"regime": "TREND", "adaptive_score": -12.5},
        "weights_hash": "canned",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 2. Timeframe list reuse + forbidden extras
# ---------------------------------------------------------------------------


class TestTimeframeContract:
    def test_tf_list_is_the_canonical_twelve(self):
        assert klines_mod.TF_LIST == scan_constants.ALL_TFS
        assert klines_mod.TF_LIST == [
            "1m", "3m", "5m", "15m", "30m", "1h",
            "2h", "4h", "6h", "8h", "12h", "1d",
        ]

    def test_no_second_hardcoded_timeframe_list(self):
        literals = set(re.findall(r"""["'](\d+[mhdw])["']""", ENTRY_SOURCE))
        # Only documented single-period accesses may appear: the 5m cadence
        # shared with build_symbol series, the 1h primary default from the
        # Task 12 interface, and the single daily read for the 8.3
        # failed-bounce rule. The 12-way fan-out iterates TF_LIST itself.
        assert literals <= {"5m", "1h", "1d"}, literals
        assert "TF_LIST" in ENTRY_SOURCE

    def test_forbidden_fetches_are_never_called(self):
        forbidden = [
            "cvd", "ticker", "divergence", "ls_term", "funding_lens",
            "premium_index", "orderbook", "basis", "spot", "panel", "book",
        ]
        lowered = ENTRY_SOURCE.lower()
        for token in forbidden:
            assert re.search(r"(?<![a-z_])" + re.escape(token) + r"(?![a-z_])", lowered) is None, token

    def test_build_symbol_is_never_imported_or_called(self):
        tree = ast.parse(ENTRY_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in getattr(node, "names", [])]
                assert "build_symbol" not in names
            if isinstance(node, ast.Call):
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else "")
                assert called != "build_symbol"

    @pytest.mark.asyncio
    async def test_per_tf_counting_and_missing_tf_unavailable(self, monkeypatch):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        budget = EntryBudget()
        result = await build_entry_snapshot(
            "BTCUSDT", budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        requested = {key[2] for key in calls if key[0] == "klines"}
        assert requested == set(klines_mod.TF_LIST)
        assert sum(1 for key in calls if key[0] == "klines") == 12
        assert result.entry_score == 66.0

        calls2: _CallLog = _CallLog()
        _install_fakes(
            monkeypatch, calls2, assembled=_canned_assembled(), missing_tfs={"12h"}
        )
        result2 = await build_entry_snapshot(
            "BTCUSDT", budget=EntryBudget(), as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        assert result2.entry_score is None
        assert "consensus" in result2.missing_blocks
        assert result2.source_meta["consensus"]["reason_code"] == "CONSENSUS_TIMEFRAME_MISSING"

    @pytest.mark.asyncio
    async def test_cold_cost_is_18_calls_and_no_full_chain(self, monkeypatch):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        with patch.object(
            symbol_builder_mod, "build_symbol",
            side_effect=AssertionError("full chain must not run"),
        ), patch.object(
            ratios_mod, "ratio_term_structure",
            side_effect=AssertionError("panel-only L/S term must not run"),
        ), patch.object(
            ratios_mod, "position_ls_timeseries",
            side_effect=AssertionError("divergence input must not run"),
        ):
            budget = EntryBudget()
            result = await build_entry_snapshot(
                "ETHUSDT", budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS
            )
        kinds = [key[0] for key in calls]
        assert kinds.count("klines") == 12
        assert kinds.count("oi") == 1
        assert kinds.count("ratio") == 4
        assert kinds.count("funding") == 1
        assert budget.used_calls == 18
        assert result.entry_score == 66.0


# ---------------------------------------------------------------------------
# 3. Budget rounds: 10 coins, 30 coins, 429 retries, exhaustion
# ---------------------------------------------------------------------------


class TestEntryBudget:
    def test_defaults_match_plan(self):
        budget = EntryBudget()
        assert (budget.max_calls, budget.concurrency, budget.ttl_sec) == (240, 2, 3600)

    def test_invalid_params_rejected(self):
        with pytest.raises(ValueError):
            EntryBudget(max_calls=0)
        with pytest.raises(ValueError):
            EntryBudget(concurrency=0)
        with pytest.raises(ValueError):
            EntryBudget(ttl_sec=-1)

    @pytest.mark.asyncio
    async def test_ten_symbols_cost_180(self, monkeypatch):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        budget = EntryBudget()
        symbols = [f"COIN{i}USDT" for i in range(10)]
        batch = await run_entry_batch(
            symbols, budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        assert batch.stats["calls_made"] == 180
        assert batch.stats["attempted"] == 10
        assert batch.stats["queued"] == 0
        assert batch.queued_symbols == ()
        assert all(item.entry_score == 66.0 for item in batch.items)
        assert all(item.reason_code is None for item in batch.items)

    @pytest.mark.asyncio
    async def test_thirty_symbols_stop_at_240_and_queue(self, monkeypatch):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        budget = EntryBudget()
        symbols = [f"COIN{i}USDT" for i in range(30)]
        batch = await run_entry_batch(
            symbols, budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS,
            max_symbols=30,
        )
        assert batch.stats["calls_made"] == 240
        assert budget.used_calls == 240
        assert budget.exhausted
        attempted = {item.symbol for item in batch.items}
        queued = set(batch.queued_symbols)
        assert len(queued) > 0
        assert attempted.isdisjoint(queued)
        assert attempted | queued == set(symbols)
        # Queued symbols never touched the network.
        touched = {key[1] for key in calls}
        assert queued.isdisjoint(touched)
        exhausted_items = [
            item for item in batch.items if item.reason_code == ENTRY_BUDGET_EXHAUSTED
        ]
        assert exhausted_items
        assert all(item.entry_score is None for item in exhausted_items)

    @pytest.mark.asyncio
    async def test_429_retry_counts_every_attempt(self, monkeypatch):
        calls: _CallLog = _CallLog()
        _install_fakes(
            monkeypatch, calls, assembled=_canned_assembled(), klines_fail_once={"1h"}
        )
        budget = EntryBudget()
        result = await build_entry_snapshot(
            "BTCUSDT", budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        assert result.entry_score == 66.0
        assert result.reason_code is None
        # 18 base calls + 1 transient retry, all spent through the budget.
        assert budget.used_calls == 19

    @pytest.mark.asyncio
    async def test_retries_stay_on_shared_limiter_path(self, monkeypatch):
        get_json_calls: list[dict] = []

        async def fake_get_json(url, params=None, *, rate_limited=False):
            get_json_calls.append({"url": url, "rate_limited": rate_limited})
            if "topLongShortPositionRatio" in url:
                return [
                    {"longShortRatio": "1.3", "timestamp": ASOF_MS - i * HOUR_MS}
                    for i in range(48)
                ]
            raise AssertionError(f"unexpected upstream url: {url}")

        calls: _CallLog = _CallLog()
        candles = _tf_candles()

        async def fake_fetch_klines(symbol, interval, limit=300, end_ms=None):
            calls.bump(("klines", symbol, interval))
            return [dict(c) for c in candles[interval]]

        async def fake_oi(symbol, period="5m", limit=48):
            return [dict(p) for p in _oi_points()]

        def fake_leaf(value):
            async def _fetch(symbol, period="5m", limit=48):
                return [value] * 48
            return _fetch

        async def _coro(value):
            return value

        monkeypatch.setattr(klines_mod, "fetch_klines", fake_fetch_klines)
        monkeypatch.setattr(oi_mod, "fetch_oi_hist", fake_oi)
        monkeypatch.setattr(ratios_mod, "global_account_ls", fake_leaf(1.1))
        monkeypatch.setattr(ratios_mod, "top_account_ls", fake_leaf(1.2))
        # top_position_ls stays real: it must travel get_json(rate_limited=True).
        # NOTE: ratios.py binds get_json at import time, so patch it where it
        # is looked up (ratios_mod), not at its definition site (http_mod).
        monkeypatch.setattr(ratios_mod, "taker_ls", fake_leaf(1.0))
        monkeypatch.setattr(ratios_mod, "get_json", fake_get_json)
        monkeypatch.setattr(
            funding_mod, "funding_history_range",
            lambda symbol, start_ms, end_ms, limit=1000: _coro(_funding_events()),
        )

        result = await build_entry_snapshot(
            "BTCUSDT", budget=EntryBudget(), as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        ratio_calls = [c for c in get_json_calls if "/futures/data/" in c["url"]]
        assert ratio_calls
        assert all(c["rate_limited"] is True for c in ratio_calls)
        assert result.source_meta["micro"]["status"] == "OK"

    @pytest.mark.asyncio
    async def test_exhausted_budget_makes_no_network(self, monkeypatch):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        budget = EntryBudget(max_calls=1)
        result = await build_entry_snapshot(
            "BTCUSDT", budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        assert result.entry_score is None
        assert result.reason_code == ENTRY_BUDGET_EXHAUSTED
        assert result.missing_blocks == ENTRY_REQUIRED_BLOCKS
        assert budget.used_calls == 1
        assert sum(calls.values()) == 1

    @pytest.mark.asyncio
    async def test_concurrency_cap_and_cache_ttl(self, monkeypatch):
        calls: _CallLog = _CallLog()
        candles = _tf_candles()

        async def slow_fetch_klines(symbol, interval, limit=300, end_ms=None):
            calls.bump(("klines", symbol, interval))
            await asyncio.sleep(0.01)
            return [dict(c) for c in candles[interval]]

        async def slow_oi(symbol, period="5m", limit=48):
            calls.bump(("oi", symbol))
            await asyncio.sleep(0.01)
            return [dict(p) for p in _oi_points()]

        def slow_ratio(kind):
            async def _fetch(symbol, period="5m", limit=48):
                calls.bump(("ratio", symbol, kind))
                await asyncio.sleep(0.01)
                return [1.2] * 48
            return _fetch

        async def slow_funding(symbol, start_ms, end_ms, limit=1000):
            calls.bump(("funding", symbol))
            await asyncio.sleep(0.01)
            return _funding_events()

        monkeypatch.setattr(klines_mod, "fetch_klines", slow_fetch_klines)
        monkeypatch.setattr(oi_mod, "fetch_oi_hist", slow_oi)
        monkeypatch.setattr(ratios_mod, "global_account_ls", slow_ratio("glob"))
        monkeypatch.setattr(ratios_mod, "top_account_ls", slow_ratio("acc"))
        monkeypatch.setattr(ratios_mod, "top_position_ls", slow_ratio("pos"))
        monkeypatch.setattr(ratios_mod, "taker_ls", slow_ratio("taker"))
        monkeypatch.setattr(funding_mod, "funding_history_range", slow_funding)

        now = [FETCHED_MS]
        budget = EntryBudget(clock=lambda: now[0])
        symbols = [f"COIN{i}USDT" for i in range(4)]
        first = await run_entry_batch(
            symbols, budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS,
            max_symbols=4,
        )
        assert budget.peak_inflight == 2
        assert first.stats["calls_made"] == 4 * 18

        # Second round inside the TTL is fully cached: no new upstream calls.
        second = await run_entry_batch(
            symbols, budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS,
            max_symbols=4,
        )
        assert second.stats["calls_made"] == 0
        assert second.stats["cache_hits"] == 4 * 18
        assert [item.entry_score for item in second.items] == [
            item.entry_score for item in first.items
        ]

        # Past the 3600s TTL the same budget's cache misses again.
        now[0] = FETCHED_MS + 3600_000 + 1
        third = await run_entry_batch(
            symbols[:1], budget=budget,
            as_of_ms=ASOF_MS, now_ms=now[0], max_symbols=1,
        )
        assert third.stats["calls_made"] == 18

    @pytest.mark.asyncio
    async def test_default_round_caps_ten_symbols(self, monkeypatch):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        budget = EntryBudget()
        symbols = [f"COIN{i}USDT" for i in range(12)]
        batch = await run_entry_batch(
            symbols, budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        assert batch.stats["attempted"] == 10
        assert batch.queued_symbols == ("COIN10USDT", "COIN11USDT")
        assert batch.stats["calls_made"] == 180


# ---------------------------------------------------------------------------
# 4. Historical replay rejection
# ---------------------------------------------------------------------------


class TestHistoricalReplay:
    @pytest.mark.asyncio
    async def test_build_symbol_end_ms_mixes_live_funding_and_is_rejected(self, monkeypatch):
        old_ms = ASOF_MS - 60 * DAY_MS
        old_candles = {
            tf: [_candle(old_ms - (24 - i) * DAY_MS, 100.0 - i) for i in range(24)]
            for tf in klines_mod.TF_LIST
        }

        async def fake_all_tf(symbol, limit=300, intervals=None, end_ms=None):
            assert end_ms == old_ms
            return {tf: list(rows) for tf, rows in old_candles.items()}

        live_oi = [{"t": ASOF_MS * 1_000_000, "oi": 999.0, "oi_value": 49_000_000.0}]
        live_funding = [
            {"t": ASOF_MS - i * 8 * HOUR_MS, "funding_rate": 0.0002} for i in range(48)
        ]

        async def fake_oi(symbol, period="5m", limit=48):
            return [dict(p) for p in live_oi]

        async def fake_ratios(symbol, period="5m", limit=48):
            return {"glob": [1.1] * 48, "acc": [1.2] * 48,
                    "pos": [1.3] * 48, "taker": [1.0] * 48}

        async def fake_hist(symbol, limit=48):
            return [dict(r) for r in live_funding]

        async def fake_pos_ts(symbol, period, limit=60):
            return {"t": [], "v": []}

        monkeypatch.setattr(klines_mod, "fetch_all_tf", fake_all_tf)
        monkeypatch.setattr(oi_mod, "fetch_oi_hist", fake_oi)
        monkeypatch.setattr(ratios_mod, "fetch_ratio_series", fake_ratios)
        monkeypatch.setattr(funding_mod, "funding_hist", fake_hist)
        monkeypatch.setattr(ratios_mod, "position_ls_timeseries", fake_pos_ts)

        obj = await symbol_builder_mod.build_symbol("BTCUSDT", end_ms=old_ms)
        tail_times = [r["t"] for r in live_funding]
        assert max(tail_times) > old_ms
        assert obj["series"]["funding"] == [r["funding_rate"] for r in live_funding]

        with pytest.raises(HistoricalReplayError):
            build_historical_entry("BTCUSDT", end_ms=old_ms)
        with pytest.raises(HistoricalReplayError):
            build_historical_entry("BTCUSDT")

    @pytest.mark.asyncio
    async def test_replay_reads_only_the_stored_snapshot(self, monkeypatch, tmp_path):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        budget = EntryBudget()
        result = await build_entry_snapshot(
            "BTCUSDT", budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        repo = await ShortLabRepository.open(db_path=tmp_path / "replay.duckdb")
        try:
            await repo.migrate()
            await save_entry_snapshot(repo, result)
            stored = await repo.get_entry(result.snapshot_id)
        finally:
            await repo.close()

        def _raise(*args, **kwargs):
            raise AssertionError("replay must not touch the network")

        monkeypatch.setattr(klines_mod, "fetch_klines", _raise)
        monkeypatch.setattr(oi_mod, "fetch_oi_hist", _raise)
        monkeypatch.setattr(ratios_mod, "global_account_ls", _raise)
        monkeypatch.setattr(funding_mod, "funding_history_range", _raise)

        replayed = recompute_entry_from_record(stored)
        assert replayed["symbol"] == "BTCUSDT"
        assert replayed["as_of_ms"] == ASOF_MS
        assert replayed["entry_score"] == result.entry_score == 66.0
        assert replayed["components"] == {
            name: result.components[name] for name in replayed["components"]
        }
        assert replayed["missing_blocks"] == ()


# ---------------------------------------------------------------------------
# 5. Persistence: save_entry, provenance, score linkage, two as-of instants
# ---------------------------------------------------------------------------


def _feature_meta():
    from diveintocrypto_desktop.shortlab.repository import REQUIRED_FEATURE_META_FIELDS

    def one():
        return {
            "status": "OK",
            "fetched_at_ms": FETCHED_MS,
            "as_of_ms": ASOF_MS,
            "coverage_fraction": 1.0,
            "reason_code": None,
            "source": "unit-test",
        }

    return {name: one() for name in REQUIRED_FEATURE_META_FIELDS}


class TestEntryPersistence:
    @pytest.mark.asyncio
    async def test_save_entry_and_recompute_offline(self, monkeypatch, tmp_path):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        result = await build_entry_snapshot(
            "BTCUSDT", budget=EntryBudget(), as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        assert result.snapshot_id == f"entry-BTCUSDT-{ASOF_MS}-{ENTRY_VERSION}"
        assert set(result.source_meta) == set(REQUIRED_ENTRY_META_BLOCKS)
        assert set(result.inputs) == set(REQUIRED_ENTRY_META_BLOCKS) | {"_meta"}
        for block, meta in result.source_meta.items():
            assert set(meta) == {
                "status", "fetched_at_ms", "as_of_ms",
                "coverage_fraction", "reason_code", "source",
            }
            assert meta["fetched_at_ms"] == FETCHED_MS
            assert meta["as_of_ms"] == ASOF_MS

        repo = await ShortLabRepository.open(db_path=tmp_path / "entry.duckdb")
        try:
            await repo.migrate()
            snapshot_id = await save_entry_snapshot(repo, result)
            assert snapshot_id == result.snapshot_id
            assert await repo.entry_source_status(snapshot_id) == "OK"
            stored = await repo.get_entry(snapshot_id)
            assert stored is not None
            assert stored.entry_version == ENTRY_VERSION
            assert stored.primary_tf == "1h"
            assert stored.symbol == "BTCUSDT"
            assert stored.as_of_ms == ASOF_MS
            assert stored.dive_engine_version
            assert stored.dive_weights_hash == result.dive_weights_hash
            assert stored.dive_config_hash == result.dive_config_hash
            assert stored.inputs["_meta"]["shortlab_config_hash"] == config_hash(
                load_shortlab_config()
            )
            assert stored.entry_score == 66.0
        finally:
            await repo.close()

        def _raise(*args, **kwargs):
            raise AssertionError("recompute must not touch the network")

        monkeypatch.setattr(klines_mod, "fetch_klines", _raise)
        monkeypatch.setattr(oi_mod, "fetch_oi_hist", _raise)
        monkeypatch.setattr(ratios_mod, "global_account_ls", _raise)
        monkeypatch.setattr(ratios_mod, "top_account_ls", _raise)
        monkeypatch.setattr(ratios_mod, "top_position_ls", _raise)
        monkeypatch.setattr(ratios_mod, "taker_ls", _raise)
        monkeypatch.setattr(funding_mod, "funding_history_range", _raise)

        replayed = recompute_entry_from_record(stored)
        assert replayed["entry_score"] == stored.entry_score == 66.0
        for block in ENTRY_REQUIRED_BLOCKS:
            assert replayed["components"][block] == stored.components[block]

    @pytest.mark.asyncio
    async def test_entry_snapshot_id_links_score(self, monkeypatch, tmp_path):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        result = await build_entry_snapshot(
            "BTCUSDT", budget=EntryBudget(), as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        repo = await ShortLabRepository.open(db_path=tmp_path / "link.duckdb")
        try:
            await repo.migrate()
            entry_id = await save_entry_snapshot(repo, result)
            feature = FeatureSnapshotRecord(
                snapshot_id="feat-link-1",
                symbol="BTCUSDT",
                as_of_ms=ASOF_MS,
                feature_version="features-v1",
                features={"ltss": 81.0},
                source_meta=_feature_meta(),
                data_quality=85.0,
            )
            await repo.save_feature(feature)
            score = ScoreSnapshotRecord(
                snapshot_id="score-link-1",
                generation_id="gen-link-1",
                feature_snapshot_id="feat-link-1",
                entry_snapshot_id=entry_id,
                symbol="BTCUSDT",
                as_of_ms=ASOF_MS,
                analysis_tier="LITE",
                profile="GENERAL_LITE",
                score_version="ltss-lite-v1",
                entry_version=ENTRY_VERSION,
                feature_version="features-v1",
                config_hash=config_hash(load_shortlab_config()),
                ltss=81.0,
                entry_score=result.entry_score,
                data_quality=85.0,
                candidate_status="CANDIDATE",
                execution_status="NOT_READY",
                status="CANDIDATE",
                module_scores={},
                vetoes=(),
                pauses=(),
                reasons=("ENTRY_BELOW_READY",),
                warnings=(),
            )
            returned = await repo.save_score(score, entry_snapshot_id=entry_id)
            assert returned == "score-link-1"
            read_back = await repo.get_score("score-link-1")
            assert read_back is not None
            assert read_back.entry_snapshot_id == entry_id
            assert read_back.entry_score == 66.0
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_two_asof_instants_are_independent(self, monkeypatch, tmp_path):
        calls: _CallLog = _CallLog()
        _install_fakes(monkeypatch, calls, assembled=_canned_assembled())
        first = await build_entry_snapshot(
            "BTCUSDT", budget=EntryBudget(), as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        second = await build_entry_snapshot(
            "BTCUSDT",
            budget=EntryBudget(),
            as_of_ms=ASOF_MS + DAY_MS,
            now_ms=FETCHED_MS + DAY_MS,
        )
        assert first.snapshot_id != second.snapshot_id
        repo = await ShortLabRepository.open(db_path=tmp_path / "two.duckdb")
        try:
            await repo.migrate()
            await save_entry_snapshot(repo, first)
            await save_entry_snapshot(repo, second)
            assert await repo.get_entry(first.snapshot_id) is not None
            assert await repo.get_entry(second.snapshot_id) is not None
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_incomplete_funding_persists_null(self, monkeypatch, tmp_path):
        calls: _CallLog = _CallLog()
        thin = [
            {"t": ASOF_MS - i * 10 * DAY_MS, "funding_rate": 0.0001, "mark_price": 1.0}
            for i in range(5)
        ]
        _install_fakes(
            monkeypatch, calls, assembled=_canned_assembled(), funding_events=thin
        )
        result = await build_entry_snapshot(
            "BTCUSDT", budget=EntryBudget(), as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        assert result.entry_score is None
        assert "funding" in result.missing_blocks
        assert (
            result.source_meta["funding"]["reason_code"]
            == funding_mod.FUNDING_HISTORY_INCOMPLETE
        )
        repo = await ShortLabRepository.open(db_path=tmp_path / "thin.duckdb")
        try:
            await repo.migrate()
            snapshot_id = await save_entry_snapshot(repo, result)
            stored = await repo.get_entry(snapshot_id)
            assert stored is not None
            assert stored.entry_score is None
            assert recompute_entry_from_record(stored)["entry_score"] is None
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_real_assemble_pipeline_shape(self, monkeypatch):
        """One symbol through the real pure assemble (no network, real math)."""
        calls: _CallLog = _CallLog()
        candles = {tf: _daily_candles(70) for tf in klines_mod.TF_LIST}

        async def fake_fetch_klines(symbol, interval, limit=300, end_ms=None):
            calls.bump(("klines", symbol, interval))
            return [dict(c) for c in candles[interval]]

        async def fake_oi(symbol, period="5m", limit=48):
            return [dict(p) for p in _oi_points()]

        async def fake_series(symbol, period="5m", limit=48):
            return [1.1] * 48

        monkeypatch.setattr(klines_mod, "fetch_klines", fake_fetch_klines)
        monkeypatch.setattr(oi_mod, "fetch_oi_hist", fake_oi)
        monkeypatch.setattr(ratios_mod, "global_account_ls", fake_series)
        monkeypatch.setattr(ratios_mod, "top_account_ls", fake_series)
        monkeypatch.setattr(ratios_mod, "top_position_ls", fake_series)
        monkeypatch.setattr(ratios_mod, "taker_ls", fake_series)
        monkeypatch.setattr(
            funding_mod, "funding_history_range",
            lambda symbol, start_ms, end_ms, limit=1000: _coro(_funding_events()),
        )

        async def _coro(value):
            return value

        result = await build_entry_snapshot(
            "BTCUSDT", budget=EntryBudget(), as_of_ms=ASOF_MS, now_ms=FETCHED_MS
        )
        assert result.snapshot_id == f"entry-BTCUSDT-{ASOF_MS}-{ENTRY_VERSION}"
        assert set(result.source_meta) == set(REQUIRED_ENTRY_META_BLOCKS)
        assert result.dive_weights_hash and result.dive_config_hash
        assert result.dive_engine_version
        replayed = recompute_entry_from_record(result.to_record())
        assert replayed["entry_score"] == result.entry_score
