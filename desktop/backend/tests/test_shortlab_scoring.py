"""Task 10: features, profiles and LTSS-LITE math (design 8.2/8.3/9.1/10.3/11.1/13/14).

Fixed-fixture suite (no network, no DB). Every bin boundary in design 8.2 is
asserted at the exact threshold; the three LITE profiles and the three FULL
profiles reach exactly 100 on all-max fixtures; missing critical inputs force
``ltss=None`` with no reweighting; the rolling ticker never enters
Tradeability; and the same inputs repeated 100x are bit-identical.
"""

from __future__ import annotations

import datetime as dt

import pytest

from diveintocrypto_desktop.shortlab.config import config_hash, load_shortlab_config
from diveintocrypto_desktop.shortlab.features import carry as carry_mod
from diveintocrypto_desktop.shortlab.features import lifecycle as life_mod
from diveintocrypto_desktop.shortlab.features import tradeability as trade_mod
from diveintocrypto_desktop.shortlab.features import valuation as val_mod
from diveintocrypto_desktop.shortlab.scoring import ltss as ltss_mod
from diveintocrypto_desktop.shortlab.scoring.ltss import (
    extract_features,
    round_half_up_1,
    score_full,
    score_lite,
)
from diveintocrypto_desktop.shortlab.scoring.profiles import Profile, select_profile
from diveintocrypto_desktop.shortlab.scoring.versions import (
    FEATURE_VERSION,
    SCORE_VERSION_FULL,
    SCORE_VERSION_LITE,
)

ASOF_MS = int(
    dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000
)
DAY_MS = 86_400_000


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _max_series() -> tuple[list[float], list[float]]:
    base = [100 - i * 0.9 for i in range(60)]
    tail = [40, 39, 38, 37, 36, 35, 34, 33, 32, 30, 34, 34.5, 35, 34, 33, 32.5, 32, 31.5, 31, 32]
    closes = base + tail
    highs = [c * 1.02 for c in closes]
    highs[20] = 95.0
    highs[40] = 75.0
    for idx, val in ((20, 95.0), (40, 75.0)):
        for d in (-3, -2, -1, 1, 2, 3):
            if highs[idx + d] >= val:
                highs[idx + d] = val - 5.0
    return closes, highs


def _max_inputs(**overrides) -> dict:
    closes, highs = _max_series()
    inputs: dict = {
        "symbol": "TESTUSDT",
        "ath_price": 100.0,
        "current_price": closes[-1],
        "ath_date_ms": ASOF_MS - 200 * DAY_MS,
        "daily_closes": list(closes),
        "daily_highs": list(highs),
        "close_30d_ago": 40.0,
        "spot_volume_30d": 60.0,
        "spot_volume_prev_30d": 100.0,
        "spot_applicable": True,
        "funding_30d": 0.025,
        "funding_30d_complete": True,
        "funding_positive_ratio_30d": 0.75,
        "funding_30d_ratio_complete": True,
        "funding_positive_ratio_90d": 0.80,
        "funding_90d_complete": True,
        "funding_rates_30d": [0.0001] * 90,
        "market_cap_usd": 100_000_000.0,
        "fdv_usd": 600_000_000.0,
        "circulating_supply": 15_000_000.0,
        "total_supply": 100_000_000.0,
        "oi_value_usd": 15_000_000.0,
        "price_change_7d": -0.06,
        "oi_change_7d": 0.06,
        "futures_spot_volume_ratio": 6.0,
        "ls_ratio": 2.0,
        "futures_qv_1d": 35_000_000.0,
        "universe_quote_volume": 1_000_000.0,  # rolling ticker: must be ignored
        "spread": 0.001,
        "book_depth_min_1pct": 1_500_000.0,
        "contract_status": "TRADING",
        "has_settlement_record": True,
        "unlock_raw_15": 15.0,
        "narrative_raw_15": 15.0,
    }
    inputs.update(overrides)
    return inputs


@pytest.fixture(scope="module")
def config():
    return load_shortlab_config()


# ---------------------------------------------------------------------------
# 9.1 ATH drawdown bins (max 7)
# ---------------------------------------------------------------------------


class TestAthDrawdown:
    @pytest.mark.parametrize(
        "drawdown,expected",
        [
            (0.0, 0),
            (-0.05, 0),
            (-0.199, 0),
            (-0.20, 3),
            (-0.30, 3),
            (-0.399, 3),
            (-0.40, 7),
            (-0.50, 7),
            (-0.699, 7),
            (-0.70, 4),
            (-0.80, 4),
            (-0.849, 4),
            (-0.85, 2),
            (-0.90, 2),
            (-0.95, 2),
            (-0.951, 0),
            (-0.99, 0),
            (0.05, 0),  # above ATH: no drawdown premium
        ],
    )
    def test_bins(self, drawdown, expected):
        score, value, _ = life_mod.score_ath_drawdown(drawdown)
        assert score == expected
        assert value == pytest.approx(drawdown)

    def test_missing_is_null(self):
        for bad in (None, "nan", float("nan"), float("inf")):
            score, value, reason = life_mod.score_ath_drawdown(bad)
            assert score is None and value is None
            assert reason == "ATH_MISSING"


class TestAthAge:
    @pytest.mark.parametrize(
        "age,expected",
        [(0, 0), (89.9, 0), (90, 1), (179.9, 1), (180, 3), (400, 3)],
    )
    def test_bins(self, age, expected):
        assert life_mod.score_ath_age(age)[0] == expected

    def test_missing(self):
        assert life_mod.score_ath_age(None)[0] is None
        assert life_mod.score_ath_age(-1)[0] is None


class TestDrop30d:
    @pytest.mark.parametrize(
        "ret,expected",
        [
            (0.05, 0),
            (0.0, 0),
            (-0.05, 1),
            (-0.099, 1),
            (-0.10, 3),
            (-0.20, 3),
            (-0.299, 3),
            (-0.30, 1),
            (-0.60, 1),
        ],
    )
    def test_bins(self, ret, expected):
        assert life_mod.score_30d_drop(ret)[0] == expected

    def test_missing(self):
        assert life_mod.score_30d_drop(None)[0] is None


# ---------------------------------------------------------------------------
# 8.3 MA / pivot / failed bounce
# ---------------------------------------------------------------------------


class TestMaStructure:
    def test_needs_70_bars(self):
        closes, _ = _max_series()
        assert life_mod.score_ma_structure(closes[:69])[0] is None
        assert life_mod.score_ma_structure(closes[:69])[2] == "MA_INSUFFICIENT_HISTORY"
        assert life_mod.score_ma_structure(closes)[0] == 4

    def test_bullish_scores_zero(self):
        closes = [30 + i * 0.9 for i in range(80)]  # steadily rising
        score, value, _ = life_mod.score_ma_structure(closes)
        assert score == 0
        assert value["close_below_ma60"] is False

    def test_non_numeric_is_null(self):
        assert life_mod.score_ma_structure(None)[0] is None
        assert life_mod.score_ma_structure([1.0] * 69 + [None])[0] is None


class TestLowerHigh:
    def test_needs_7_bars(self):
        assert life_mod.score_lower_high([10.0] * 6)[0] is None

    def test_max_fixture_scores_3(self):
        _, highs = _max_series()
        assert life_mod.score_lower_high(highs)[0] == 3

    def test_rising_pivots_score_0(self):
        highs = [10.0] * 30
        highs[10] = 20.0  # first pivot
        highs[20] = 30.0  # second pivot, higher -> no lower high
        for idx in (10, 20):
            for d in (-3, -2, -1, 1, 2, 3):
                if highs[idx + d] >= highs[idx]:
                    highs[idx + d] = highs[idx] - 1.0
        assert life_mod.score_lower_high(highs)[0] == 0

    def test_unconfirmed_spike_never_scores(self):
        # A spike inside the last 3 bars has no right-hand window and must not
        # count as a confirmed pivot.
        highs = [10.0] * 20
        highs[10] = 25.0  # one confirmed pivot only
        for d in (-3, -2, -1, 1, 2, 3):
            if highs[10 + d] >= 25.0:
                highs[10 + d] = 20.0
        highs[-1] = 99.0  # unconfirmed spike at the edge
        highs[-2] = 98.0
        score, value, _ = life_mod.score_lower_high(highs)
        assert score == 0
        assert len(value["pivots"]) <= 1

    def test_window_is_strictly_3_each_side(self):
        # Center higher than 2 neighbours but not the 3rd -> not a pivot.
        highs = [10.0] * 15
        highs[7] = 20.0
        highs[4] = 20.0  # equal 3rd left neighbour blocks the pivot (strict >)
        score, value, _ = life_mod.score_lower_high(highs)
        assert value["pivots"] == []
        assert score == 0


class TestFailedBounce:
    def test_max_fixture_scores_2(self):
        closes, highs = _max_series()
        assert life_mod.score_failed_bounce(closes, highs)[0] == 2

    def test_no_bounce_scores_0(self):
        closes = [100 - i * 0.5 for i in range(30)]  # steady slide, no 10% bounce
        highs = [c * 1.01 for c in closes]
        highs[10] = max(highs) + 5.0
        score, _, _ = life_mod.score_failed_bounce(closes, highs)
        assert score == 0

    def test_insufficient_history_is_null(self):
        closes = [10.0] * 19
        highs = [10.0] * 19
        score, _, reason = life_mod.score_failed_bounce(closes, highs)
        assert score is None
        assert reason == "FAILED_BOUNCE_INSUFFICIENT_HISTORY"

    def test_no_prior_pivot_is_null(self):
        closes, _ = _max_series()
        flat_highs = [50.0] * len(closes)  # no strict pivot possible
        score, _, reason = life_mod.score_failed_bounce(closes, flat_highs)
        assert score is None
        assert reason == "FAILED_BOUNCE_NO_PIVOT"


class TestSpotDecay:
    @pytest.mark.parametrize(
        "ratio,expected",
        [(0.6, 3), (0.61, 2), (0.85, 2), (0.86, 1), (0.99, 1), (1.0, 0), (1.5, 0)],
    )
    def test_bins(self, ratio, expected):
        score, value, _ = life_mod.score_spot_decay(100 * ratio, 100.0)
        assert score == expected
        assert value == pytest.approx(ratio)

    def test_missing_is_null(self):
        assert life_mod.score_spot_decay(None, 100.0)[0] is None
        assert life_mod.score_spot_decay(50.0, None)[0] is None
        assert life_mod.score_spot_decay(50.0, 0.0)[0] is None

    def test_no_spot_market_is_na(self):
        score, value, reason = life_mod.score_spot_decay(50.0, 100.0, applicable=False)
        assert score is None and value is None
        assert reason == "NO_SPOT_MARKET"


# ---------------------------------------------------------------------------
# 10.3 carry bins
# ---------------------------------------------------------------------------


class TestFunding30d:
    @pytest.mark.parametrize(
        "funding,expected",
        [
            (-0.001, 0),
            (0.0, 0),
            (0.001, 1),
            (0.00199, 1),
            (0.002, 2),
            (0.004, 2),
            (0.00499, 2),
            (0.005, 4),
            (0.009, 4),
            (0.00999, 4),
            (0.01, 6),
            (0.019, 6),
            (0.01999, 6),
            (0.02, 8),
            (0.05, 8),
        ],
    )
    def test_bins(self, funding, expected):
        assert carry_mod.score_funding_30d(funding)[0] == expected

    def test_gap_is_null(self):
        score, _, reason = carry_mod.score_funding_30d(0.05, complete=False)
        assert score is None
        assert reason == "FUNDING_HISTORY_INCOMPLETE"
        assert carry_mod.score_funding_30d(None)[0] is None


class TestPositiveRatio:
    @pytest.mark.parametrize(
        "ratio,expected",
        [(0.7, 3), (0.699, 2), (0.55, 2), (0.549, 1), (0.4, 1), (0.399, 0), (0.0, 0)],
    )
    def test_bins(self, ratio, expected):
        assert carry_mod.score_positive_ratio(ratio)[0] == expected
        assert carry_mod.score_positive_ratio(ratio)[0] == expected

    def test_incomplete_window_is_null(self):
        assert carry_mod.score_positive_ratio(0.9, complete=False)[0] is None


class TestFundingStability:
    def test_flat_is_2(self):
        assert carry_mod.score_funding_stability([0.0001] * 90)[0] == 2

    def test_boundary_1(self):
        # population stdev exactly ~0.0005 -> 1 point band
        rates = [0.0005, -0.0005] * 45
        score, value, _ = carry_mod.score_funding_stability(rates)
        assert value == pytest.approx(0.0005)
        assert score == 1

    def test_noisy_is_0(self):
        assert carry_mod.score_funding_stability([0.002, -0.002] * 45)[0] == 0

    def test_empty_is_null(self):
        assert carry_mod.score_funding_stability([])[0] is None
        assert carry_mod.score_funding_stability(None)[0] is None


class TestOiMc:
    @pytest.mark.parametrize(
        "ratio,expected",
        [(0.15, 3), (0.149, 2), (0.08, 2), (0.079, 1), (0.03, 1), (0.029, 0)],
    )
    def test_bins(self, ratio, expected):
        assert carry_mod.score_oi_mc(100 * ratio, 100.0)[0] == expected

    def test_unknown_unit_is_null(self):
        assert carry_mod.score_oi_mc(None, 100.0)[0] is None
        assert carry_mod.score_oi_mc(10.0, None)[0] is None


class TestDivergence:
    def test_hit(self):
        assert carry_mod.score_price_oi_divergence(-0.06, 0.06)[0] == 3

    @pytest.mark.parametrize(
        "price,oi",
        [(-0.04, 0.06), (-0.06, 0.04), (-0.05, 0.05), (0.01, 0.10)],
    )
    def test_miss(self, price, oi):
        assert carry_mod.score_price_oi_divergence(price, oi)[0] == 0

    def test_missing(self):
        assert carry_mod.score_price_oi_divergence(None, 0.1)[0] is None


class TestFuturesSpotRatio:
    @pytest.mark.parametrize(
        "ratio,expected", [(5.0, 2), (4.9, 1), (2.0, 1), (1.9, 0), (0.5, 0)]
    )
    def test_bins(self, ratio, expected):
        assert carry_mod.score_futures_spot_ratio(ratio)[0] == expected

    def test_no_spot_is_null(self):
        score, _, reason = carry_mod.score_futures_spot_ratio(99.0, applicable=False)
        assert score is None and reason == "NO_SPOT_MARKET"


class TestCrowding:
    def test_bins(self):
        assert carry_mod.score_crowding(2.0)[0] == 1
        assert carry_mod.score_crowding(1.51)[0] == 1
        assert carry_mod.score_crowding(1.5)[0] == 0
        assert carry_mod.score_crowding(None)[0] is None


# ---------------------------------------------------------------------------
# 11.1 valuation bins (max 10)
# ---------------------------------------------------------------------------


class TestValuation:
    @pytest.mark.parametrize(
        "ratio,expected",
        [(6.0, 4), (5.0, 4), (4.9, 3), (3.0, 3), (2.9, 2), (2.0, 2), (1.9, 0)],
    )
    def test_fdv_mc(self, ratio, expected):
        assert val_mod.score_fdv_mc(100 * ratio, 100.0)[0] == expected

    @pytest.mark.parametrize(
        "ratio,expected",
        [(0.15, 4), (0.199, 4), (0.20, 3), (0.34, 3), (0.35, 1), (0.49, 1), (0.50, 0), (0.9, 0)],
    )
    def test_float(self, ratio, expected):
        assert val_mod.score_float_ratio(100 * ratio, 100.0)[0] == expected

    def test_combo(self):
        assert val_mod.score_combo(6.0, 0.15)[0] == 2
        assert val_mod.score_combo(3.0, 0.349)[0] == 2
        assert val_mod.score_combo(2.9, 0.15)[0] == 0
        assert val_mod.score_combo(6.0, 0.36)[0] == 0
        assert val_mod.score_combo(None, 0.15)[0] is None

    def test_missing(self):
        assert val_mod.score_fdv_mc(None, 100.0)[0] is None
        assert val_mod.score_float_ratio(10.0, None)[0] is None


# ---------------------------------------------------------------------------
# 13 tradeability bins (max 10, daily qv only)
# ---------------------------------------------------------------------------


class TestTradeability:
    @pytest.mark.parametrize(
        "qv,expected",
        [
            (35_000_000.0, 3),
            (30_000_000.0, 3),
            (29_999_999.0, 2),
            (20_000_000.0, 2),
            (19_999_999.0, 1),
            (10_000_000.0, 1),
            (9_999_999.0, 0),
        ],
    )
    def test_qv(self, qv, expected):
        assert trade_mod.score_futures_qv(qv)[0] == expected

    @pytest.mark.parametrize(
        "oi,expected",
        [(10_000_000.0, 2), (5_000_000.0, 2), (4_999_999.0, 1), (2_000_000.0, 1), (1_999_999.0, 0)],
    )
    def test_oi(self, oi, expected):
        assert trade_mod.score_oi(oi)[0] == expected

    @pytest.mark.parametrize(
        "spread,expected",
        [(0.001, 2), (0.0015, 2), (0.0016, 1), (0.003, 1), (0.0031, 0)],
    )
    def test_spread(self, spread, expected):
        assert trade_mod.score_spread(spread)[0] == expected

    @pytest.mark.parametrize(
        "depth,expected",
        [(1_500_000.0, 2), (1_000_000.0, 2), (999_999.0, 1), (250_000.0, 1), (249_999.0, 0)],
    )
    def test_depth(self, depth, expected):
        assert trade_mod.score_depth(depth)[0] == expected

    def test_contract(self):
        assert trade_mod.score_contract("TRADING", True)[0] == 1
        assert trade_mod.score_contract("TRADING", False)[0] == 0
        assert trade_mod.score_contract("HALT", True)[0] == 0
        assert trade_mod.score_contract(None, True)[0] is None
        assert trade_mod.score_contract("TRADING", None)[0] is None

    def test_missing_is_null(self):
        assert trade_mod.score_futures_qv(None)[0] is None
        assert trade_mod.score_oi(None)[0] is None
        assert trade_mod.score_spread(None)[0] is None
        assert trade_mod.score_depth(None)[0] is None


# ---------------------------------------------------------------------------
# Profiles (8.1)
# ---------------------------------------------------------------------------


class _Fundamentals:
    def __init__(self, categories=(), market_cap_usd=None, fdv_usd=None,
                 circulating_supply=None, total_supply=None):
        self.categories = tuple(categories)
        self.market_cap_usd = market_cap_usd
        self.fdv_usd = fdv_usd
        self.circulating_supply = circulating_supply
        self.total_supply = total_supply


class TestSelectProfile:
    def test_manual_override_wins(self):
        fund = _Fundamentals(
            categories=("meme",), market_cap_usd=100.0, fdv_usd=600.0,
            circulating_supply=10.0, total_supply=100.0,
        )
        profile = select_profile(None, fund, {"profile": "GENERAL_ALT"})
        assert isinstance(profile, Profile)
        assert profile.name == "GENERAL_LITE"
        assert profile.base == "GENERAL_ALT"
        assert profile.is_manual is True

    def test_meme_beats_low_float(self):
        fund = _Fundamentals(
            categories=("Meme",), market_cap_usd=100.0, fdv_usd=600.0,
            circulating_supply=10.0, total_supply=100.0,
        )
        profile = select_profile(None, fund, None)
        assert profile.base == "MEME"
        assert profile.name == "MEME_LITE"
        assert profile.full_name == "MEME_FULL"

    def test_low_float_condition(self):
        fund = _Fundamentals(
            categories=("defi",), market_cap_usd=100.0, fdv_usd=200.0,
            circulating_supply=30.0, total_supply=100.0,
        )
        assert select_profile(None, fund, None).base == "LOW_FLOAT_VC"

    def test_low_float_boundaries(self):
        # float_ratio == 0.35 is NOT low-float; fdv/mc == 2 is the lower edge.
        at_edge = _Fundamentals(
            categories=(), market_cap_usd=100.0, fdv_usd=200.0,
            circulating_supply=35.0, total_supply=100.0,
        )
        assert select_profile(None, at_edge, None).base == "GENERAL_ALT"
        just_inside = _Fundamentals(
            categories=(), market_cap_usd=100.0, fdv_usd=200.0,
            circulating_supply=34.0, total_supply=100.0,
        )
        assert select_profile(None, just_inside, None).base == "LOW_FLOAT_VC"

    def test_missing_fundamentals_defaults_general(self):
        profile = select_profile(None, None, None)
        assert profile.base == "GENERAL_ALT"
        assert profile.reason == "DEFAULT_GENERAL_NO_FUNDAMENTALS"


# ---------------------------------------------------------------------------
# score_lite: three LITE profiles hit 100, module math, rounding
# ---------------------------------------------------------------------------


class TestScoreLite:
    @pytest.mark.parametrize("profile", ["MEME_LITE", "GENERAL_LITE", "LOW_FLOAT_VC_LITE"])
    def test_all_max_is_100(self, config, profile):
        snap = extract_features(_max_inputs(), ASOF_MS)
        assert snap.features["lifecycle"]["raw"] == 25
        assert snap.features["carry"]["raw"] == 25
        assert snap.features["valuation"]["raw"] == 10
        assert snap.features["tradeability"]["raw"] == 10
        breakdown = score_lite(snap, profile, config)
        assert breakdown.ltss == 100.0
        assert breakdown.profile == profile
        assert breakdown.score_version == SCORE_VERSION_LITE
        assert breakdown.feature_version == FEATURE_VERSION
        assert breakdown.config_hash == config_hash(config)
        assert sum(breakdown.module_scores.values()) == pytest.approx(100.0)

    def test_module_score_is_raw_over_max_times_weight(self, config):
        snap = extract_features(_max_inputs(), ASOF_MS)
        breakdown = score_lite(snap, "GENERAL_LITE", config)
        weights = dict(config.score_weights["GENERAL_LITE"])
        assert breakdown.module_scores["lifecycle"] == pytest.approx(25 / 25 * weights["lifecycle"])
        assert breakdown.module_scores["carry"] == pytest.approx(25 / 25 * weights["carry"])
        assert breakdown.module_scores["valuation"] == pytest.approx(10 / 10 * weights["valuation"])
        assert breakdown.module_scores["tradeability"] == pytest.approx(10 / 10 * weights["tradeability"])

    def test_rounding_is_half_up(self):
        assert round_half_up_1(2.25) == pytest.approx(2.3)
        assert round_half_up_1(2.35) == pytest.approx(2.4)
        assert round_half_up_1(99.95) == pytest.approx(100.0)

    def test_breakdown_carries_raws_reasons_profile_version(self, config):
        snap = extract_features(_max_inputs(), ASOF_MS)
        breakdown = score_lite(snap, "GENERAL_LITE", config)
        assert set(breakdown.module_raws) == {"lifecycle", "carry", "valuation", "tradeability"}
        assert "lifecycle.ath_drawdown" in breakdown.factor_scores
        assert "carry.funding_30d" in breakdown.raw_values
        assert isinstance(breakdown.reasons, tuple)
        assert breakdown.null_reason is None
        assert breakdown.symbol == "TESTUSDT"
        assert breakdown.as_of_ms == ASOF_MS

    def test_profile_object_accepted(self, config):
        snap = extract_features(_max_inputs(), ASOF_MS)
        profile = select_profile(None, None, None)
        breakdown = score_lite(snap, profile, config)
        assert breakdown.profile == "GENERAL_LITE"
        assert breakdown.ltss == 100.0


# ---------------------------------------------------------------------------
# Null semantics: missing -> 0, critical -> ltss None without reweighting
# ---------------------------------------------------------------------------


class TestNullSemantics:
    @pytest.mark.parametrize(
        "override,critical",
        [
            ({"funding_30d": None}, ["funding30d"]),
            ({"funding_30d_complete": False}, ["funding30d"]),
            ({"ath_price": None}, ["ATH"]),
            ({"oi_value_usd": None}, ["OI"]),
            ({"market_cap_usd": None}, ["MC"]),
        ],
    )
    def test_critical_missing_forces_null(self, config, override, critical):
        snap = extract_features(_max_inputs(**override), ASOF_MS)
        breakdown = score_lite(snap, "GENERAL_LITE", config)
        assert breakdown.ltss is None
        assert breakdown.null_reason is not None
        for item in critical:
            assert item in breakdown.null_reason
        # No reweighting: maxima stay fixed and missing raws stay low.
        assert breakdown.module_max == {"lifecycle": 25, "carry": 25, "valuation": 10, "tradeability": 10}

    def test_non_critical_missing_stays_scored(self, config):
        snap = extract_features(_max_inputs(spread=None), ASOF_MS)
        assert snap.features["tradeability"]["factors"]["spread"]["score"] is None
        breakdown = score_lite(snap, "GENERAL_LITE", config)
        assert breakdown.ltss is not None
        assert breakdown.module_raws["tradeability"] == 8  # 10 - 2 spread points
        assert breakdown.module_scores["tradeability"] == pytest.approx(8 / 10 * 10)

    def test_spot_60d_missing_is_null_but_not_critical(self, config):
        snap = extract_features(
            _max_inputs(spot_volume_30d=None, spot_volume_prev_30d=None,
                        futures_spot_volume_ratio=None),
            ASOF_MS,
        )
        assert snap.features["lifecycle"]["factors"]["spot_decay"]["score"] is None
        assert snap.features["carry"]["factors"]["futures_spot_ratio"]["score"] is None
        breakdown = score_lite(snap, "GENERAL_LITE", config)
        assert breakdown.ltss is not None

    def test_oi_unit_unknown_is_critical_null(self, config):
        # Task 7 OI_UNIT_UNVERIFIED surfaces as oi_value_usd=None here.
        snap = extract_features(_max_inputs(oi_value_usd=None), ASOF_MS)
        assert snap.features["carry"]["factors"]["oi_mc"]["score"] is None
        assert snap.features["tradeability"]["factors"]["oi"]["score"] is None
        breakdown = score_lite(snap, "GENERAL_LITE", config)
        assert breakdown.ltss is None
        assert "OI" in (breakdown.null_reason or "")

    def test_no_spot_market_is_na_not_zero(self, config):
        snap = extract_features(
            _max_inputs(spot_applicable=False, spot_volume_30d=None,
                        spot_volume_prev_30d=None, futures_spot_volume_ratio=None),
            ASOF_MS,
        )
        decay = snap.features["lifecycle"]["factors"]["spot_decay"]
        assert decay["score"] is None and decay["reason"] == "NO_SPOT_MARKET"
        ratio = snap.features["carry"]["factors"]["futures_spot_ratio"]
        assert ratio["score"] is None and ratio["reason"] == "NO_SPOT_MARKET"
        assert score_lite(snap, "GENERAL_LITE", config).ltss is not None


# ---------------------------------------------------------------------------
# Tradeability discriminating test: daily qv only, never the rolling ticker
# ---------------------------------------------------------------------------


class TestTradeabilityUsesDailyQvOnly:
    def test_daily_qv_wins_over_rolling(self, config):
        low_daily = extract_features(
            _max_inputs(futures_qv_1d=5_000_000.0, universe_quote_volume=500_000_000.0),
            ASOF_MS,
        )
        assert low_daily.features["tradeability"]["factors"]["futures_qv_1d"]["score"] == 0
        high_daily = extract_features(
            _max_inputs(futures_qv_1d=35_000_000.0, universe_quote_volume=1_000.0),
            ASOF_MS,
        )
        assert high_daily.features["tradeability"]["factors"]["futures_qv_1d"]["score"] == 3
        # The rolling ticker must not leak into the snapshot field either.
        assert "universe_quote_volume" not in high_daily.features["tradeability"]["factors"]["futures_qv_1d"].get("value", {}) \
            if isinstance(high_daily.features["tradeability"]["factors"]["futures_qv_1d"].get("value"), dict) else True
        assert high_daily.features["tradeability"]["factors"]["futures_qv_1d"]["value"] == pytest.approx(35_000_000.0)

    def test_daily_missing_never_falls_back_to_ticker(self, config):
        snap = extract_features(
            _max_inputs(futures_qv_1d=None, universe_quote_volume=500_000_000.0),
            ASOF_MS,
        )
        factor = snap.features["tradeability"]["factors"]["futures_qv_1d"]
        assert factor["score"] is None
        assert factor["reason"] == "FUTURES_QV_MISSING"

    def test_hard_min_consistency(self, config):
        # 10M matches the config hard_min gate: below it the factor is 0 and
        # Task 11 will BLOCK; scoring itself never invents volume.
        snap = extract_features(_max_inputs(futures_qv_1d=9_999_999.0), ASOF_MS)
        assert snap.features["tradeability"]["factors"]["futures_qv_1d"]["score"] == 0


# ---------------------------------------------------------------------------
# FULL pre-buried synthesis for Task 17
# ---------------------------------------------------------------------------


class TestFullSynthesis:
    @pytest.mark.parametrize("profile", ["MEME_FULL", "GENERAL_FULL", "LOW_FLOAT_VC_FULL"])
    def test_all_max_full_is_100(self, config, profile):
        snap = extract_features(_max_inputs(), ASOF_MS)
        breakdown = score_full(snap, profile, config)
        assert breakdown.raw_values["valuation_supply_raw"] == 10 + 15
        assert breakdown.profile == profile
        assert breakdown.score_version == SCORE_VERSION_FULL
        assert breakdown.ltss == 100.0

    def test_valuation_supply_math(self, config):
        snap = extract_features(_max_inputs(), ASOF_MS)
        breakdown = score_full(snap, "GENERAL_FULL", config)
        weights = dict(config.score_weights["GENERAL_FULL"])
        assert breakdown.module_raws["valuation"] == 25
        assert breakdown.module_scores["valuation"] == pytest.approx(25 / 25 * weights["valuation"])
        assert breakdown.module_raws["narrative"] == 15
        assert breakdown.module_scores["narrative"] == pytest.approx(15 / 15 * weights["narrative"])

    def test_unlock_missing_is_zero_without_reweight(self, config):
        snap = extract_features(_max_inputs(unlock_raw_15=None, narrative_raw_15=None), ASOF_MS)
        breakdown = score_full(snap, "GENERAL_FULL", config)
        assert breakdown.module_raws["valuation"] == 10  # valuation 10 + unlock 0
        assert breakdown.module_scores["valuation"] == pytest.approx(10 / 25 * 20)
        assert breakdown.module_raws["narrative"] == 0
        assert breakdown.ltss is not None and breakdown.ltss < 100.0


# ---------------------------------------------------------------------------
# Purity, versions, determinism
# ---------------------------------------------------------------------------


class TestPurityAndContract:
    def test_no_network_or_sql_imports(self):
        import pathlib

        root = pathlib.Path(ltss_mod.__file__).parent.parent
        checked = [
            root / "features" / "lifecycle.py",
            root / "features" / "carry.py",
            root / "features" / "valuation.py",
            root / "features" / "tradeability.py",
            root / "scoring" / "ltss.py",
            root / "scoring" / "profiles.py",
            root / "scoring" / "versions.py",
        ]
        banned = ("aiohttp", "requests", "httpx", "duckdb", "sqlite", "get_json", "fetch_")
        for path in checked:
            text = path.read_text(encoding="utf-8")
            for token in banned:
                assert token not in text, f"{path.name} must stay pure (found {token!r})"

    def test_no_min_age_days_consumer(self):
        snap = extract_features(_max_inputs(), ASOF_MS)
        assert "min_age_days" not in snap.features
        assert "min_age_days" not in snap.source_meta
        for module in ("lifecycle", "carry", "valuation", "tradeability"):
            assert "min_age_days" not in snap.features[module].get("factors", {})

    def test_determinism_100x(self, config):
        inputs = _max_inputs()
        first_snap = extract_features(inputs, ASOF_MS)
        first_score = score_lite(first_snap, "GENERAL_LITE", config)
        for _ in range(100):
            snap = extract_features(dict(inputs), ASOF_MS)
            assert snap.features == first_snap.features
            assert snap.source_meta == first_snap.source_meta
            assert snap.snapshot_id == first_snap.snapshot_id
            score = score_lite(snap, "GENERAL_LITE", config)
            assert score.ltss == first_score.ltss
            assert score.module_scores == first_score.module_scores
            assert score.reasons == first_score.reasons
