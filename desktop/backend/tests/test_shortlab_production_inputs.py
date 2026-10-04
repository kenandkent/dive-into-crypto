"""F05: frozen production inputs, policy DQ/risk and version markers.

Offline fixture suite (no network, no DB). Covers the F05 scenario table:

- complete funding sequence is no longer None; the 7D risk view reuses the
  identical computed values (never a second fetch or recomputation);
- changing watch/candidate/TTL/veto moves NEW results while the pinned old
  policy still replays the old snapshot bit-identically;
- a successful book is real DQ (never NOT_WIRED) and a single-sided book
  is PARTIAL, never a faked double-sided OK;
- the legacy ``4909ffe7...`` scoring hash and the LTSS/Entry bin math stay
  golden; the fixed input contract is told apart by ``features-v2`` /
  ``entry-v2`` (math versions stay ``ltss-lite-v1`` / ``ltss-full-v1``).

Plus the F05.4 guard (missing legacy policy must not recompute history
with today's config), the R07 future-timestamp rule (v2 invalid, v1
replay keeps the historical clamp) and the canonical-USD unification.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from diveintocrypto_desktop.shortlab import observations as obs_mod
from diveintocrypto_desktop.shortlab.config import (
    config_hash,
    load_shortlab_config,
    policy_hash,
)
from diveintocrypto_desktop.shortlab.inputs import (
    REPLAY_CONFIG_UNAVAILABLE,
    FeatureInputs,
    build_feature_inputs,
    build_field_states,
)
from diveintocrypto_desktop.shortlab.models import ProviderResult
from diveintocrypto_desktop.shortlab.observations import make_observation
from diveintocrypto_desktop.shortlab.quality import (
    QualityPolicy,
    data_quality,
    default_quality_policy,
    quality_policy_from_config,
)
from diveintocrypto_desktop.shortlab.risk.veto import (
    PAUSE_BREAKOUT_7D,
    READY_INPUT_STALE,
    RiskPolicy,
    derive_status,
    evaluate_risks,
    risk_policy_from_config,
)
from diveintocrypto_desktop.shortlab.scoring.ltss import extract_features, score_lite
from diveintocrypto_desktop.shortlab.scoring.versions import (
    ENTRY_VERSION,
    FEATURE_VERSION,
    SCORE_VERSION_FULL,
    SCORE_VERSION_LITE,
)

GOLDEN_SCORING_HASH = "4909ffe7d43c294983d313cff65c6d73125b921944e403a1e1e152b91a869786"

ASOF_MS = int(dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
DAY_MS = 86_400_000
MIDNIGHT = (ASOF_MS // DAY_MS) * DAY_MS
KNOWN_AT = ASOF_MS - 60_000


def _obs(value, source, *, status="OK", reason=None, complete=None,
         coverage=None, window=None, quote="USDT", known_at=KNOWN_AT,
         source_as_of=None):
    start, end = window if window is not None else (None, None)
    return make_observation(
        value,
        source=source,
        source_as_of_ms=source_as_of,
        fetched_at_ms=known_at,
        known_at_ms=known_at,
        status=status,
        reason_code=reason,
        window_start_ms=start,
        window_end_ms=end,
        complete=complete,
        coverage_fraction=coverage,
        units=obs_mod.ObservationUnits(quote_asset=quote),
    )


def _daily_klines(n=80, close_last=31.96, close_8th_ago=34.0):
    candles = []
    for i in range(n):
        open_ms = (MIDNIGHT - (n - i) * DAY_MS) * 1_000_000  # ns, like fetch_klines
        frac = i / (n - 1)
        if i == n - 8:
            close = close_8th_ago
        elif i == n - 1:
            close = close_last
        else:
            close = close_8th_ago + (close_last - close_8th_ago) * frac
        candles.append(
            {"t": open_ms, "o": close, "h": close * 1.01, "l": close * 0.99,
             "c": close, "v": 1000.0, "qv": 35_000_000.0}
        )
    return candles


def _funding_events(days=30, rate=0.0001):
    start = MIDNIGHT - days * DAY_MS
    end = MIDNIGHT
    events = []
    moment = start
    while moment < end:
        events.append({"t": moment, "funding_rate": rate})
        moment += 8 * 3_600_000
    return events, (start, end)


def _oi_points(days=8, first=10_000_000.0, last=10_600_000.0):
    start = MIDNIGHT - days * DAY_MS
    points = []
    steps = days * 24
    for i in range(steps + 1):
        moment_ms = start + i * 3_600_000
        value = first + (last - first) * i / steps
        points.append({"t": moment_ms * 1_000_000, "oi": value, "oi_value": value})
    return points


def _spot_result():
    data = SimpleNamespace(
        spot_volume_30d=60_000_000.0,
        spot_volume_prev_30d=100_000_000.0,
        spot_quote_volume_24h=2_000_000.0,
        spot_price=32.0,
        spot_daily_bars=60,
        futures_spot_volume_ratio_30d=6.0,
        premium=0.5,
    )
    return ProviderResult(
        status="OK", source="binance-spot", fetched_at_ms=KNOWN_AT,
        as_of_ms=ASOF_MS, data=data, stale=False,
        reason_code=None, error_message=None,
    )


def _identity(multiplier=1.0, source="EXCHANGE", confidence="VERIFIED"):
    return SimpleNamespace(
        contract_multiplier=multiplier,
        multiplier_source=source,
        mapping_confidence=confidence,
        binance_spot_symbol="TESTUSDT",
        binance_futures_symbol="TESTUSDT",
        identity_snapshot_id="isl-test",
    )


def _full_observations(*, book=None, multiplier_note=None):
    _ = multiplier_note
    candles = _daily_klines()
    events, window = _funding_events()
    events_90d, window_90d = _funding_events(days=90)
    points = _oi_points()
    if book is None:
        book = {"bid_notional_1pct": 1_500_000.0,
                "ask_notional_1pct": 1_600_000.0, "spread": 0.001}
    return {
        "quote_asset": "USDT",
        "fx_rate": _obs("1", "verified-test-fx"),
        "klines_daily": _obs(candles, "binance-futures-klines",
                             source_as_of=candles[-1]["t"] // 1_000_000,
                             window=((MIDNIGHT - 80 * DAY_MS), MIDNIGHT)),
        "ticker_24h": _obs({"current_price": 31.96, "price_change_24h": 0.01},
                            "binance-ticker"),
        "funding_history": _obs(events, "binance-futures-funding",
                                complete=True, coverage=1.0, window=window,
                                source_as_of=events[-1]["t"]),
        "funding_history_90d": _obs(events_90d, "binance-futures-funding",
                                    complete=True, coverage=1.0, window=window_90d,
                                    source_as_of=events_90d[-1]["t"]),
        "oi_history": _obs(points, "binance-futures-oi",
                           complete=True, coverage=1.0,
                           window=(points[0]["t"] // 1_000_000,
                                   points[-1]["t"] // 1_000_000),
                           source_as_of=points[-1]["t"] // 1_000_000),
        "ath": _obs({"ath_price": 100.0,
                      "ath_date_ms": ASOF_MS - 200 * DAY_MS}, "coingecko"),
        "fundamentals": _obs({"market_cap_usd": 100_000_000.0,
                              "fdv_usd": 600_000_000.0,
                              "circulating_supply": 15_000_000.0,
                              "total_supply": 100_000_000.0}, "coingecko"),
        "spot_history": _obs(_spot_result(), "binance-spot-history",
                             window=(MIDNIGHT - 60 * DAY_MS, MIDNIGHT)),
        "book": _obs(book, "binance-futures-book"),
        "contract": _obs({"status": "TRADING",
                          "onboard_at_ms": ASOF_MS - 200 * DAY_MS,
                          "delivery_at_ms": None,
                          "live_universe_present": True},
                         "binance-exchangeInfo"),
    }


@pytest.fixture(scope="module")
def config():
    return load_shortlab_config()


@pytest.fixture(scope="module")
def inputs(config):
    return build_feature_inputs(
        "TESTUSDT", _full_observations(), _identity(), ASOF_MS, config
    )


# ---------------------------------------------------------------------------
# Complete funding sequence + shared 7D risk values
# ---------------------------------------------------------------------------


class TestCompleteFundingAndShared7D:
    def test_rates_30d_complete_ascending(self, inputs):
        assert isinstance(inputs, FeatureInputs)
        assert inputs.version == "features-v2"
        assert len(inputs.funding_rates_30d) == 90
        assert inputs.funding_rates_30d == tuple([0.0001] * 90)
        assert inputs.funding_30d_complete is True
        assert inputs.funding_30d == pytest.approx(90 * 0.0001)
        assert inputs.funding_30d_coverage == pytest.approx(1.0)
        # Window bounds are canonical and shared.
        assert inputs.funding_window_start_ms == MIDNIGHT - 30 * DAY_MS
        assert inputs.funding_window_end_ms == MIDNIGHT

    def test_funding_7d_from_same_package(self, inputs):
        assert inputs.funding_7d == pytest.approx(21 * 0.0001)
        assert inputs.funding_positive_ratio_30d == pytest.approx(1.0)

    def test_risk_meta_reuses_identical_values(self, inputs):
        meta = inputs.risk_meta()
        assert meta["price_change_7d"] is inputs.price_change_7d
        assert meta["oi_change_7d"] is inputs.oi_change_7d
        assert meta["funding_7d"] is inputs.funding_7d
        assert meta["funding_30d"] is inputs.funding_30d
        assert inputs.price_change_7d == pytest.approx(31.96 / 34.0 - 1.0)
        assert inputs.oi_change_7d == pytest.approx(0.06)
        # Same cutoff-aligned package: OI ends agree with the price window.
        assert inputs.price_window_end_ms == MIDNIGHT
        assert inputs.oi_window_end_ms == MIDNIGHT
        assert inputs.oi_window_reason is None

    def test_shared_7d_drives_risk(self, inputs):
        meta = inputs.risk_meta()
        calm = evaluate_risks({}, dict(meta), 95.0)
        assert PAUSE_BREAKOUT_7D not in calm.pauses
        spiked = dict(meta, price_change_7d=0.80)
        assert PAUSE_BREAKOUT_7D in evaluate_risks({}, spiked, 95.0).pauses

    def test_ltss_scores_on_frozen_inputs(self, config, inputs):
        snap = extract_features(inputs.to_ltss_inputs(), ASOF_MS)
        assert snap.features["carry"]["factors"]["funding_30d"]["score"] == 4
        assert snap.features["carry"]["factors"]["funding_stability"]["score"] == 2
        breakdown = score_lite(snap, "GENERAL_LITE", config)
        assert breakdown.ltss is not None


# ---------------------------------------------------------------------------
# Policy changes move new results; pinned policy replays old snapshots
# ---------------------------------------------------------------------------


class TestPolicyPinning:
    def test_ttl_change_moves_new_dq(self, config, inputs):
        states = build_field_states(inputs, config)
        frozen = quality_policy_from_config(config, policy_hash=policy_hash(config))
        before = data_quality("LITE", states, ASOF_MS, frozen)
        assert before.data_quality == pytest.approx(100.0)
        tightened = QualityPolicy(
            **{**frozen.__dict__,
               "freshness_sec": {**frozen.freshness_sec,
                                 "market_futures": (30, 60)}})
        after = data_quality("LITE", states, ASOF_MS, tightened)
        assert after.data_quality < before.data_quality
        assert after.stale is True

    def test_pinned_policy_replays_snapshot(self, config, inputs):
        states = build_field_states(inputs, config)
        frozen = quality_policy_from_config(config, policy_hash=policy_hash(config))
        first = data_quality("LITE", states, ASOF_MS, frozen)
        for _ in range(5):
            assert data_quality("LITE", states, ASOF_MS, frozen) == first
        # Legacy path replays identically too (v1 tables, untouched).
        assert data_quality("LITE", states, ASOF_MS).data_quality == pytest.approx(
            first.data_quality)

    def test_veto_change_moves_new_risk(self, config, inputs):
        meta = inputs.risk_meta()
        default = risk_policy_from_config(config)
        assert default.breakout_7d == pytest.approx(0.70)
        calm = evaluate_risks({}, dict(meta, price_change_7d=0.10), 95.0,
                              breakout_7d=default.breakout_7d)
        assert PAUSE_BREAKOUT_7D not in calm.pauses
        strict = evaluate_risks({}, dict(meta, price_change_7d=0.10), 95.0,
                                breakout_7d=0.05)
        assert PAUSE_BREAKOUT_7D in strict.pauses
        # Old default call is unchanged by the strict variant existing.
        assert evaluate_risks({}, dict(meta, price_change_7d=0.10), 95.0) == calm

    def test_candidate_thresholds_explicit(self):
        ready = derive_status(85, 75, 90.0, 8.0,
                              {"mapping_confidence": "VERIFIED",
                               "contract_multiplier": 1.0,
                               "has_valid_onboard": True},
                              {"vetoes": (), "pauses": (), "warnings": ()},
                              False, ready_ltss=80)
        assert ready.execution_status == "READY"
        tightened = derive_status(85, 75, 90.0, 8.0,
                                  {"mapping_confidence": "VERIFIED",
                                   "contract_multiplier": 1.0,
                                   "has_valid_onboard": True},
                                  {"vetoes": (), "pauses": (), "warnings": ()},
                                  False, ready_ltss=86)
        assert tightened.execution_status == "NOT_READY"


# ---------------------------------------------------------------------------
# Real book / basis states (never NOT_WIRED, never faked double-sided)
# ---------------------------------------------------------------------------


class TestRealBookStates:
    def test_successful_book_is_real_ok(self, config, inputs):
        states = {s.field_id: s for s in build_field_states(inputs, config)}
        book = states["book_depth"]
        assert book.status == "OK"
        assert book.reason_code is None
        assert "NOT_WIRED" not in (book.source or "")
        basis = states["basis"]
        assert basis.status == "OK"
        assert basis.reason_code is None
        assert "NOT_WIRED" not in (basis.source or "")

    def test_single_sided_book_is_partial_not_faked(self, config):
        observations = _full_observations(
            book={"bid_notional_1pct": 1_500_000.0,
                  "ask_notional_1pct": None, "spread": 0.001})
        one_sided = build_feature_inputs(
            "TESTUSDT", observations, _identity(), ASOF_MS, config)
        states = {s.field_id: s
                  for s in build_field_states(one_sided, config)}
        book = states["book_depth"]
        assert book.status == "PARTIAL"
        assert book.status != "OK"
        dq = data_quality("LITE", list(states.values()), ASOF_MS,
                          default_quality_policy())
        assert dq.group_credits["spot_liquidity"] == pytest.approx(0.90)

    def test_missing_book_is_unavailable(self, config):
        observations = _full_observations(book={})
        empty = build_feature_inputs(
            "TESTUSDT", observations, _identity(), ASOF_MS, config)
        states = {s.field_id: s for s in build_field_states(empty, config)}
        assert states["book_depth"].status == "UNAVAILABLE"
        assert "NOT_WIRED" not in (states["book_depth"].reason_code or "")


# ---------------------------------------------------------------------------
# Legacy hash + math goldens; v2 markers tell the fixed contract apart
# ---------------------------------------------------------------------------


class TestGoldensAndVersions:
    def test_legacy_scoring_hash_unchanged(self, config):
        assert config_hash(config) == GOLDEN_SCORING_HASH

    def test_versions(self):
        assert FEATURE_VERSION == "features-v2"
        assert ENTRY_VERSION == "entry-v2"
        assert SCORE_VERSION_LITE == "ltss-lite-v1"
        assert SCORE_VERSION_FULL == "ltss-full-v1"

    def test_ltss_bins_unchanged(self):
        from diveintocrypto_desktop.shortlab.features import carry as carry_mod
        from diveintocrypto_desktop.shortlab.features import lifecycle as life_mod

        assert carry_mod.score_funding_30d(0.009)[0] == 4
        assert carry_mod.score_funding_stability([0.0001] * 90)[0] == 2
        assert life_mod.score_ath_drawdown((31.96 - 100.0) / 100.0)[0] == 7

    def test_snapshot_carries_v2_with_v1_math(self, config, inputs):
        snap = extract_features(inputs.to_ltss_inputs(), ASOF_MS)
        assert snap.feature_version == "features-v2"
        breakdown = score_lite(snap, "GENERAL_LITE", config)
        assert breakdown.score_version == "ltss-lite-v1"
        assert breakdown.config_hash == GOLDEN_SCORING_HASH


# ---------------------------------------------------------------------------
# F05.4: no policy, no replay -- and the R07 future-timestamp rule
# ---------------------------------------------------------------------------


class TestReplayGuardAndFutureRule:
    def test_missing_policy_never_recomputes(self):
        with pytest.raises(ValueError, match=REPLAY_CONFIG_UNAVAILABLE):
            build_feature_inputs(
                "TESTUSDT", _full_observations(), _identity(), ASOF_MS, None)

    def test_legacy_provenance_leg_rejected_not_scored(self, config):
        observations = _full_observations()
        observations["oi_history"] = [{"t": 1, "oi_value": 5.0}]  # not Observed
        degraded = build_feature_inputs(
            "TESTUSDT", observations, _identity(), ASOF_MS, config)
        assert degraded.oi_change_7d is None
        assert degraded.oi_value_usd is None

    def test_future_timestamp_invalid_under_v2(self, config, inputs):
        states = list(build_field_states(inputs, config))
        future = [
            s if s.field_id != "funding_30d" else
            type(s)(field_id=s.field_id, status=s.status, coverage=s.coverage,
                    valid_count=s.valid_count, required_count=s.required_count,
                    fetched_at_ms=ASOF_MS + 60_000, reason_code=s.reason_code,
                    source=s.source)
            for s in states
        ]
        frozen = default_quality_policy()
        v2 = data_quality("LITE", future, ASOF_MS, frozen)
        detail = v2.field_details["funding_30d"]
        assert detail.freshness == pytest.approx(0.0)
        assert v2.stale is True
        assert READY_INPUT_STALE in derive_status(
            85, 80, v2.data_quality, 8.0,
            {"mapping_confidence": "VERIFIED", "contract_multiplier": 1.0,
             "has_valid_onboard": True},
            {"vetoes": (), "pauses": (), "warnings": ()},
            v2.stale).reasons
        # v1 legacy replay keeps the historical clamp (bit-identical history).
        v1 = data_quality("LITE", future, ASOF_MS)
        assert v1.field_details["funding_30d"].freshness == pytest.approx(1.0)
        assert v1.stale is False


# ---------------------------------------------------------------------------
# Canonical-USD unification + OI/price package alignment
# ---------------------------------------------------------------------------


class TestCanonicalUsdAndAlignment:
    def test_multiplier_unified(self, config):
        base = build_feature_inputs(
            "TESTUSDT", _full_observations(), _identity(), ASOF_MS, config)
        assert base.current_price == pytest.approx(31.96)
        assert base.ath_price == pytest.approx(100.0)
        assert base.unit_reason is None
        # 1000-denomination contract, native legs x1000 -> same canonical USD.
        candles = _daily_klines()
        scaled = [{**c, "c": c["c"] * 1000.0, "o": c["o"] * 1000.0,
                   "h": c["h"] * 1000.0, "l": c["l"] * 1000.0} for c in candles]
        observations = _full_observations()
        observations["klines_daily"] = _obs(
            scaled, "binance-futures-klines", known_at=KNOWN_AT)
        observations["ticker_24h"] = _obs(
            {"current_price": 31.96 * 1000.0, "price_change_24h": 0.01},
            "binance-ticker")
        observations["ath"] = _obs(100.0 * 1000.0, "coingecko")
        scaled_inputs = build_feature_inputs(
            "1000TESTUSDT", observations, _identity(multiplier=1000.0),
            ASOF_MS, config)
        assert scaled_inputs.current_price == pytest.approx(base.current_price)
        assert scaled_inputs.ath_price == pytest.approx(base.ath_price)

    def test_unknown_multiplier_nulls_price_not_oi(self, config):
        degraded = build_feature_inputs(
            "TESTUSDT", _full_observations(),
            _identity(multiplier=None, source=None), ASOF_MS, config)
        assert degraded.current_price is None
        assert degraded.ath_price is None
        assert degraded.unit_reason == "UNIT_UNKNOWN"
        # Nominals are never unit-scaled: OI still resolves.
        assert degraded.oi_value_usd == pytest.approx(10_600_000.0)

    def test_misaligned_oi_window_stays_null(self, config):
        observations = _full_observations()
        late = _oi_points(days=2)  # last-2D only: ends disagree with price
        observations["oi_history"] = _obs(
            late, "binance-futures-oi", complete=True, coverage=1.0,
            window=(late[0]["t"] // 1_000_000, late[-1]["t"] // 1_000_000),
            source_as_of=late[-1]["t"] // 1_000_000)
        skewed = build_feature_inputs(
            "TESTUSDT", observations, _identity(), ASOF_MS, config)
        assert skewed.oi_change_7d is None
        assert skewed.oi_window_reason == "WINDOW_MISALIGNED"
        # The price leg of the same package still resolves.
        assert skewed.price_change_7d == pytest.approx(31.96 / 34.0 - 1.0)


# ---------------------------------------------------------------------------
# RiskPolicy / QualityPolicy construction from config
# ---------------------------------------------------------------------------


class TestPolicyConstruction:
    def test_risk_policy_from_config(self, config):
        policy = risk_policy_from_config(config)
        assert isinstance(policy, RiskPolicy)
        assert policy.breakout_24h == pytest.approx(0.35)
        assert policy.breakout_7d == pytest.approx(0.70)
        assert policy.new_token_days == 45
        assert policy.ready_ltss == 80
        assert policy.ready_dq == 80
        assert policy.veto_dq_threshold == 60

    def test_quality_policy_from_config_matches_defaults(self, config):
        mine = quality_policy_from_config(
            config, policy_hash=policy_hash(config))
        assert mine.version == "quality-policy-v2"
        assert mine.policy_hash == policy_hash(config)
        assert mine == default_quality_policy(policy_hash=policy_hash(config))

    def test_explicit_thresholds_default_to_legacy(self):
        assert RiskPolicy().breakout_24h == pytest.approx(0.35)
        assert RiskPolicy().new_token_days == 45


def test_oi_quote_nominal_uses_fx_without_contract_multiplier(config):
    observations = _full_observations()
    observations["fx_rate"] = _obs("0.98", "verified-test-fx")
    result = build_feature_inputs("TESTUSDT", observations, _identity(1000), ASOF_MS, config)
    assert result.oi_value_usd == pytest.approx(_oi_points()[-1]["oi_value"] * 0.98)
    ratio = result.oi_change_7d
    observations.pop("fx_rate")
    missing_fx = build_feature_inputs("TESTUSDT", observations, _identity(1000), ASOF_MS, config)
    assert missing_fx.oi_value_usd is None
    assert missing_fx.oi_change_7d == ratio
