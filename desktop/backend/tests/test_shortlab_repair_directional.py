"""R04 冻结输入与做空风险/分类 (D04/D05.2).

Red->Green: covers plan R04 checklist:
- 价格D/D-7收盘与OI对应时点,每端向前5min,8D拒绝
- Ticker现价/24h同Observation,缺风险输入NOT_READY
- Micro/Taker同轮冻结,迟到不入历史, SQUEEZE_CHECK_UNVERIFIED / UNKNOWN
- watch/candidate/ready全生效,人工Profile优先,MC缺失不改类
- 共识threshold .4/范围(0,.5],严重冲突中性+0置信保留计数
- Carry复用R05覆盖
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
import yaml

from diveintocrypto_desktop.engine.consensus.engine import ConsensusEngine
from diveintocrypto_desktop.engine.indicators.base import IndicatorResult, Signal
from diveintocrypto_desktop.shortlab import observations as obs_mod
from diveintocrypto_desktop.shortlab.config import load_shortlab_config
from diveintocrypto_desktop.shortlab.features import carry as carry_mod
from diveintocrypto_desktop.shortlab.inputs import build_feature_inputs
from diveintocrypto_desktop.shortlab.models import ProviderResult
from diveintocrypto_desktop.shortlab.observations import make_observation
from diveintocrypto_desktop.shortlab.risk import squeeze as squeeze_mod
from diveintocrypto_desktop.shortlab.risk.veto import (
    RiskResult,
    derive_status,
    evaluate_risks,
)
from diveintocrypto_desktop.shortlab.scoring.profiles import select_profile

ASOF_MS = int(dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
DAY_MS = 86_400_000
MIDNIGHT = (ASOF_MS // DAY_MS) * DAY_MS
KNOWN_AT = ASOF_MS - 60_000

# Re-resolve reason codes from module (fallback to literal when not yet added).
try:
    from diveintocrypto_desktop.shortlab.risk.veto import RISK_INPUT_UNVERIFIED as _RISK_MISSING
except ImportError:
    _RISK_MISSING = "RISK_INPUT_UNVERIFIED"
try:
    from diveintocrypto_desktop.shortlab.risk.veto import SQUEEZE_CHECK_UNVERIFIED as _SQ_UNVERIFIED
except ImportError:
    _SQ_UNVERIFIED = "SQUEEZE_CHECK_UNVERIFIED"


def _obs(value, source, *, known_at=KNOWN_AT, source_as_of=None, complete=None, coverage=None, coverage_fraction=None, window=None, quote="USDT", **kw):
    cov = coverage_fraction if coverage_fraction is not None else coverage
    start, end = (window if window is not None else (None, None)) if isinstance(window, (list, tuple)) else (None, None)
    units = None
    try:
        units = obs_mod.ObservationUnits(quote_asset=quote)
    except Exception:
        units = None
    return make_observation(
        value, source=source, source_as_of_ms=source_as_of,
        fetched_at_ms=known_at, known_at_ms=known_at,
        complete=complete, coverage_fraction=cov,
        window_start_ms=start, window_end_ms=end,
        units=units, **kw,
    )


def _identity(multiplier=1.0, source="EXCHANGE", confidence="VERIFIED"):
    return SimpleNamespace(
        contract_multiplier=multiplier, multiplier_source=source,
        mapping_confidence=confidence, identity_snapshot_id="isl-r04",
    )


def _daily_klines(n=10, close_last=100.0, close_8th_ago=80.0):
    candles = []
    for i in range(n):
        open_ms = (MIDNIGHT - (n - i) * DAY_MS) * 1_000_000
        if i == n - 8:
            close = close_8th_ago
        elif i == n - 1:
            close = close_last
        else:
            frac = i / max(1, n - 1)
            close = close_8th_ago + (close_last - close_8th_ago) * frac
        candles.append({"t": open_ms, "o": close, "h": close * 1.01, "l": close * 0.99, "c": close, "v": 1000.0, "qv": 35_000_000.0})
    return candles


def _oi_at(moment_ms, value):
    return {"t": int(moment_ms) * 1_000_000, "oi": value, "oi_value": value}


def _full_observations(*, price_last=100.0, price_d7=80.0, oi_first=10_000_000.0, oi_last=12_000_000.0):
    candles = _daily_klines(close_last=price_last, close_8th_ago=price_d7)
    # OI at the two corresponding CLOSE moments (7d apart), exact.
    close_d7 = (candles[-8]["t"] // 1_000_000) + DAY_MS
    close_d = (candles[-1]["t"] // 1_000_000) + DAY_MS
    points = [_oi_at(close_d7, oi_first), _oi_at(close_d, oi_last)]
    events = [{"t": MIDNIGHT - 30 * DAY_MS + i * 8 * 3_600_000, "funding_rate": 0.0001} for i in range(90)]
    events_90 = [{"t": MIDNIGHT - 90 * DAY_MS + i * 8 * 3_600_000, "funding_rate": 0.0001} for i in range(270)]
    spot_data = SimpleNamespace(spot_volume_30d=60_000_000.0, spot_volume_prev_30d=100_000_000.0, spot_quote_volume_24h=2_000_000.0, spot_price=99.0, spot_daily_bars=60, futures_spot_volume_ratio_30d=6.0, premium=0.5)
    spot_result = ProviderResult(status="OK", source="binance-spot", fetched_at_ms=KNOWN_AT, as_of_ms=ASOF_MS, data=spot_data, stale=False, reason_code=None, error_message=None)
    return {
        "quote_asset": "USDT",
        "fx_rate": _obs("1", "verified-test-fx"),
        "klines_daily": _obs(candles, "binance-futures-klines", source_as_of=candles[-1]["t"] // 1_000_000),
        "ticker_24h": _obs({"current_price": price_last, "price_change_24h": 0.01}, "binance-ticker"),
        "funding_history": _obs(events, "binance-futures-funding", complete=True, coverage=1.0),
        "funding_history_90d": _obs(events_90, "binance-futures-funding", complete=True, coverage=1.0),
        "oi_history": _obs(points, "binance-futures-oi"),
        "ath": _obs({"ath_price": 200.0, "ath_date_ms": ASOF_MS - 200 * DAY_MS}, "coingecko"),
        "fundamentals": _obs({"market_cap_usd": 100_000_000.0, "fdv_usd": 600_000_000.0, "circulating_supply": 15_000_000.0, "total_supply": 100_000_000.0}, "coingecko"),
        "spot_history": _obs(spot_result, "binance-spot-history"),
        "book": _obs({"bid_notional_1pct": 1_500_000.0, "ask_notional_1pct": 1_600_000.0, "spread": 0.001}, "binance-futures-book"),
        "contract": _obs({"status": "TRADING", "onboard_at_ms": ASOF_MS - 200 * DAY_MS, "live_universe_present": True}, "binance-exchangeInfo"),
    }


def _equal_conflict_fixture():
    return [
        IndicatorResult(name="rsi", signal=Signal.BUY, score=1, reason="buy"),
        IndicatorResult(name="macd", signal=Signal.SELL, score=-1, reason="sell"),
    ]


def _ready_identity():
    return {"mapping_confidence": "VERIFIED", "contract_multiplier": 1.0, "has_valid_onboard": True}


def _ready_meta(**over):
    meta = {"as_of_ms": ASOF_MS, "mapping_confidence": "VERIFIED", "futures_qv_1d": 35_000_000.0, "oi_value_usd": 15_000_000.0, "contract_status": "TRADING", "exchange_status": "TRADING", "live_universe_present": True, "previously_seen": True, "onboard_at_ms": ASOF_MS - 200 * DAY_MS, "price_change_24h": 0.01, "price_change_7d": 0.02, "oi_change_7d": 0.0, "funding_30d": 0.012, "funding_7d": 0.003, "funding_positive_ratio_30d": 0.8}
    meta.update(over)
    return meta


# ------------------------------------------------------------------
# Consensus: severe conflict neutral + 0 confidence, threshold .4/(0,.5]
# ------------------------------------------------------------------

def test_severe_equal_conflict_is_neutral():
    config = {"consensus": {"conflict_ratio_threshold": 0.4}}
    result = ConsensusEngine(config).evaluate(_equal_conflict_fixture())
    assert result["final_signal"] == "NEUTRAL"
    assert result["confidence"] == 0
    # retain true conflict metrics for UI
    assert result["score_data"]["buy_count"] == 1
    assert result["score_data"]["sell_count"] == 1
    assert result["score_data"]["active_signals"] == 2
    assert any("conflict" in f.lower() for f in result["risk_data"]["risk_factors"])


def test_conflict_default_is_04_and_range_enforced():
    import pathlib

    text = pathlib.Path(__file__).resolve().parent.parent / "src" / "diveintocrypto_desktop" / "engine" / "config" / "default.yaml"
    doc = yaml.safe_load(text.read_text(encoding="utf-8"))
    assert doc["consensus"]["conflict_ratio_threshold"] == pytest.approx(0.4)
    # range (0, 0.5]: 0, negative, >0.5 rejected
    for bad in (0, -0.1, 0.5 + 1e-9, 0.6, 1.0):
        with pytest.raises(ValueError):
            ConsensusEngine({"consensus": {"conflict_ratio_threshold": bad}}).evaluate(_equal_conflict_fixture())
    # edges 0.5 and small positive accepted
    out = ConsensusEngine({"consensus": {"conflict_ratio_threshold": 0.5}}).evaluate(_equal_conflict_fixture())
    assert out["final_signal"] == "NEUTRAL" and out["confidence"] == 0
    out2 = ConsensusEngine({"consensus": {"conflict_ratio_threshold": 0.01}}).evaluate(_equal_conflict_fixture())
    assert out2["final_signal"] == "NEUTRAL"


def test_conflict_at_threshold_is_neutral_not_greater_only():
    # ratio exactly 0.4 (2 vs 3 active => minority 2/5=0.4) must be neutral at threshold 0.4
    results = [
        IndicatorResult(name="a", signal=Signal.BUY, score=1, reason="b"),
        IndicatorResult(name="b", signal=Signal.BUY, score=1, reason="b"),
        IndicatorResult(name="c", signal=Signal.BUY, score=1, reason="b"),
        IndicatorResult(name="d", signal=Signal.SELL, score=-1, reason="s"),
        IndicatorResult(name="e", signal=Signal.SELL, score=-1, reason="s"),
    ]
    out = ConsensusEngine({"consensus": {"conflict_ratio_threshold": 0.4}}).evaluate(results)
    assert out["final_signal"] == "NEUTRAL"
    assert out["confidence"] == 0


def test_no_bilateral_votes_ratio_zero_and_all_missing_unknown():
    # only buys: minority 0 => ratio 0, no forced neutral
    only_buy = [IndicatorResult(name="a", signal=Signal.BUY, score=1, reason="b")]
    out = ConsensusEngine({"consensus": {"conflict_ratio_threshold": 0.4}}).evaluate(only_buy)
    assert out["score_data"]["buy_count"] == 1 and out["score_data"]["sell_count"] == 0
    # all neutral: active 0 => ratio 0 path, must not crash
    all_neut = [IndicatorResult(name="a", signal=Signal.NEUTRAL, score=0, reason="n")]
    out2 = ConsensusEngine({"consensus": {"conflict_ratio_threshold": 0.4}}).evaluate(all_neut)
    assert out2["score_data"]["active_signals"] == 0


# ------------------------------------------------------------------
# 7D price D/D-7 + OI per-end 5min, 8D reject, future not used
# ------------------------------------------------------------------

def test_price_d_d7_with_oi_per_end_5min():
    config = load_shortlab_config()
    fit = build_feature_inputs("TSTUSDT", _full_observations(price_last=120.0, price_d7=100.0, oi_first=10_000_000.0, oi_last=12_000_000.0), _identity(), ASOF_MS, config)
    assert fit.price_change_7d == pytest.approx(0.20)
    assert fit.oi_change_7d == pytest.approx(0.20)
    # 7x24h window, not 8D
    assert fit.price_window_end_ms - fit.price_window_start_ms == 7 * DAY_MS
    assert fit.oi_window_reason is None


def test_oi_each_end_only_looks_back_5min():
    config = load_shortlab_config()
    base = _full_observations(price_last=120.0, price_d7=100.0)
    candles = base["klines_daily"].value
    close_d7 = (candles[-8]["t"] // 1_000_000) + DAY_MS
    close_d = (candles[-1]["t"] // 1_000_000) + DAY_MS
    # start 4min early is usable (within 5min backward)
    pts_ok = [_oi_at(close_d7 - 4 * 60_000, 10_000_000.0), _oi_at(close_d, 12_000_000.0)]
    base["oi_history"] = _obs(pts_ok, "binance-futures-oi")
    fit = build_feature_inputs("TSTUSDT", base, _identity(), ASOF_MS, config)
    assert fit.oi_change_7d == pytest.approx(0.20)
    # start 6min early is outside 5min => reject
    pts_bad = [_oi_at(close_d7 - 6 * 60_000, 10_000_000.0), _oi_at(close_d, 12_000_000.0)]
    base["oi_history"] = _obs(pts_bad, "binance-futures-oi")
    fit2 = build_feature_inputs("TSTUSDT", base, _identity(), ASOF_MS, config)
    assert fit2.oi_change_7d is None
    assert fit2.oi_window_reason == "WINDOW_MISALIGNED"


def test_8d_span_rejected():
    config = load_shortlab_config()
    base = _full_observations(price_last=120.0, price_d7=100.0)
    candles = base["klines_daily"].value
    open_d7 = candles[-8]["t"] // 1_000_000
    close_d = (candles[-1]["t"] // 1_000_000) + DAY_MS
    pts = [_oi_at(open_d7, 10_000_000.0), _oi_at(close_d, 12_000_000.0)]
    base["oi_history"] = _obs(pts, "binance-futures-oi")
    fit = build_feature_inputs("TSTUSDT", base, _identity(), ASOF_MS, config)
    assert fit.oi_change_7d is None
    assert fit.oi_window_reason == "WINDOW_MISALIGNED"


def test_future_oi_sample_never_used():
    config = load_shortlab_config()
    base = _full_observations(price_last=120.0, price_d7=100.0)
    candles = base["klines_daily"].value
    close_d7 = (candles[-8]["t"] // 1_000_000) + DAY_MS
    close_d = (candles[-1]["t"] // 1_000_000) + DAY_MS
    pts = [_oi_at(close_d7, 10_000_000.0), _oi_at(close_d, 12_000_000.0), _oi_at(ASOF_MS + 60_000, 99_000_000.0)]
    base["oi_history"] = _obs(pts, "binance-futures-oi")
    fit = build_feature_inputs("TSTUSDT", base, _identity(), ASOF_MS, config)
    assert fit.oi_change_7d == pytest.approx(0.20)


# ------------------------------------------------------------------
# Ticker same Observation + missing risk input NOT_READY
# ------------------------------------------------------------------

def test_ticker_current_and_24h_from_same_observation():
    config = load_shortlab_config()
    fit = build_feature_inputs("TSTUSDT", _full_observations(price_last=31.96), _identity(), ASOF_MS, config)
    assert fit.current_price == pytest.approx(31.96)
    assert fit.current_price_source == "ticker_24h"
    assert fit.price_change_24h == pytest.approx(0.01)
    meta = fit.risk_meta()
    assert meta["price_change_24h"] is fit.price_change_24h


def test_missing_risk_input_is_not_ready():
    meta = _ready_meta()
    del meta["price_change_24h"]
    state = derive_status(85, 80, 95.0, 8.0, _ready_identity(), evaluate_risks({}, meta, 95.0), False)
    assert state.execution_status == "NOT_READY"
    assert _RISK_MISSING in state.reasons


def test_v02_late_observation_rejected_and_micro_late_not_in_history():
    config = load_shortlab_config()
    base = _full_observations()
    # ticker known after cutoff must degrade (not score future)
    base["ticker_24h"] = _obs({"current_price": 31.96, "price_change_24h": 0.99}, "binance-ticker", known_at=ASOF_MS + 60_000, source_as_of=ASOF_MS + 60_000)
    fit = build_feature_inputs("TSTUSDT", base, _identity(), ASOF_MS, config)
    # falls back to daily close, 24h from future ticker must not leak
    assert fit.price_change_24h != pytest.approx(0.99)
    # squeeze late micro must not enter history: craft squeeze with late confirm
    feats = {"price_change_7d": 0.20, "oi_change_7d": 0.20}
    late_micro = {"value": 80.0, "known_at_ms": ASOF_MS + 10_000, "as_of_ms": ASOF_MS}
    assert squeeze_mod.evaluate_squeeze(feats, {"micro_score": late_micro, "as_of_ms": ASOF_MS}) is False


# ------------------------------------------------------------------
# Squeeze frozen tri-state
# ------------------------------------------------------------------

def test_squeeze_plus20_with_micro_confirms_pause():
    feats = {"price_change_7d": 0.20, "oi_change_7d": 0.20}
    meta = _ready_meta(price_change_7d=0.20, oi_change_7d=0.20, micro_score=40.0)
    risk = evaluate_risks(feats, meta, 95.0)
    assert "PAUSE_SQUEEZE" in risk.pauses


def test_squeeze_plus20_without_confirm_is_not_ready():
    feats = {"price_change_7d": 0.20, "oi_change_7d": 0.20}
    meta = _ready_meta(price_change_7d=0.20, oi_change_7d=0.20)
    meta.pop("taker_buy_ratio", None)
    # ensure no confirm legs
    for k in ("taker_buy_ratio", "taker_buy_volume_ratio", "micro_score", "microstructure_score"):
        meta.pop(k, None)
        feats.pop(k, None)
    risk = evaluate_risks(dict(feats), dict(meta), 95.0)
    assert "PAUSE_SQUEEZE" not in risk.pauses
    state = derive_status(85, 80, 95.0, 8.0, _ready_identity(), risk, False)
    assert state.execution_status == "NOT_READY"
    assert _SQ_UNVERIFIED in state.reasons


def test_squeeze_no_extra_confirm_when_prereq_clearly_not_met():
    feats = {"price_change_7d": 0.02, "oi_change_7d": 0.01}
    meta = _ready_meta(price_change_7d=0.02, oi_change_7d=0.01)
    for k in ("taker_buy_ratio", "micro_score"):
        meta.pop(k, None)
    risk = evaluate_risks(feats, meta, 95.0)
    assert "PAUSE_SQUEEZE" not in risk.pauses
    state = derive_status(85, 80, 95.0, 8.0, _ready_identity(), risk, False)
    # prereq clearly not met => must not demand SQUEEZE_CHECK_UNVERIFIED
    assert _SQ_UNVERIFIED not in state.reasons


def test_squeeze_prereq_unknown_is_unknown_not_pause():
    feats = {}
    meta = _ready_meta()
    for k in ("price_change_7d", "oi_change_7d", "taker_buy_ratio", "micro_score"):
        feats.pop(k, None)
        meta.pop(k, None)
    risk = evaluate_risks(feats, meta, 95.0)
    assert "PAUSE_SQUEEZE" not in risk.pauses


def test_entry_total_never_substituted_for_micro():
    # high entry-like total must not count as micro confirmation
    assert squeeze_mod.detect_squeeze(0.20, 0.20, taker_buy_ratio=None, micro_score=None) is False
    feats = {"price_change_7d": 0.20, "oi_change_7d": 0.20, "entry_score": 95.0, "entry_total": 95.0}
    meta = _ready_meta(price_change_7d=0.20, oi_change_7d=0.20, entry_score=95.0)
    assert squeeze_mod.evaluate_squeeze(feats, meta) is False


# ------------------------------------------------------------------
# watch/candidate/ready all effective + order validation
# ------------------------------------------------------------------

def test_thresholds_20_30_ltss50_is_candidate():
    state = derive_status(50, 80, 95.0, 8.0, _ready_identity(), RiskResult(vetoes=(), pauses=(), warnings=()), False, watch_ltss=20, candidate_ltss=30, ready_ltss=80)
    assert state.candidate_status == "CANDIDATE"
    # defaults would exclude 50
    state2 = derive_status(50, 80, 95.0, 8.0, _ready_identity(), RiskResult(vetoes=(), pauses=(), warnings=()), False)
    assert state2.candidate_status == "EXCLUDED"


def test_candidate_threshold_order_validated():
    with pytest.raises(ValueError):
        derive_status(85, 80, 95.0, 8.0, _ready_identity(), RiskResult(vetoes=(), pauses=(), warnings=()), False, watch_ltss=70, candidate_ltss=60, ready_ltss=80)


def test_risk_policy_carries_watch_candidate():
    from diveintocrypto_desktop.shortlab.risk.veto import risk_policy_from_config

    policy = risk_policy_from_config(load_shortlab_config())
    assert policy.watch_ltss == 60 and policy.candidate_ltss == 70 and policy.ready_ltss == 80


# ------------------------------------------------------------------
# Profile: manual priority, meme persists when MC missing
# ------------------------------------------------------------------

class _Fund:
    def __init__(self, categories=(), market_cap_usd=None, fdv_usd=None, circulating_supply=None, total_supply=None):
        self.categories = tuple(categories)
        self.market_cap_usd = market_cap_usd
        self.fdv_usd = fdv_usd
        self.circulating_supply = circulating_supply
        self.total_supply = total_supply


class _Ident:
    def __init__(self, categories=()):
        self.categories = tuple(categories)


def test_manual_profile_priority():
    fund = _Fund(categories=("meme",), market_cap_usd=100.0, fdv_usd=600.0, circulating_supply=10.0, total_supply=100.0)
    p = select_profile(None, fund, {"profile": "GENERAL"})
    assert p.base == "GENERAL_ALT" and p.is_manual is True
    p2 = select_profile(None, fund, {"profile": "MEME"})
    assert p2.base == "MEME" and p2.is_manual is True


def test_meme_persists_when_mc_missing_and_fundamentals_failed():
    # verified meme on identity must survive missing MC and missing fundamentals object
    ident = _Ident(categories=("meme",))
    fund_missing_mc = _Fund(categories=("meme",), market_cap_usd=None, fdv_usd=None, circulating_supply=None, total_supply=None)
    assert select_profile(ident, fund_missing_mc, None).base == "MEME"
    assert select_profile(ident, None, None).base == "MEME"


# ------------------------------------------------------------------
# Carry reuses R05 coverage
# ------------------------------------------------------------------

def test_carry_reuses_r05_coverage_not_max24h():
    # R05 coverage says incomplete => carry null even though value positive
    cov = SimpleNamespace(complete=False, coverage_fraction=0.5)
    score, _, reason = carry_mod.score_funding_30d(0.05, complete=True, coverage=cov)
    assert score is None
    assert reason == "FUNDING_HISTORY_INCOMPLETE"
    # R05 complete => normal bins
    cov_ok = SimpleNamespace(complete=True, coverage_fraction=1.0)
    score2, _, _ = carry_mod.score_funding_30d(0.05, complete=True, coverage=cov_ok)
    assert score2 == 8
    # explicit complete=False still null without coverage
    assert carry_mod.score_funding_30d(0.05, complete=False)[0] is None


def test_carry_positive_ratio_reuses_r05_coverage():
    cov = SimpleNamespace(complete=False, coverage_fraction=0.2)
    assert carry_mod.score_positive_ratio(0.9, complete=True, coverage=cov)[0] is None
