"""Task 11: DQ, VETO/PAUSE/WARN and dual status (design 16/17/18).

Offline fixture suite (no network, no DB). Covers the Task 11 checklist:

- LITE/FULL outer weights sum to 100 and intra-group shares sum to 100%;
- DQ formula ``sum(group_weight * group_credit)`` with
  ``field_credit = coverage * freshness`` (fresh 1 / stale 0.5 / expired 0);
- NOT_APPLICABLE removes only the intra-group denominator (all-N/A group
  contributes 0); UNAVAILABLE stays in the denominator;
- FULL without Catalyst coverage is not enabled;
- PARTIAL ``valid/required`` (market 70, spot 60, supply/float 2, ATH 2,
  two-sided book 2; e.g. 60/70 == 0.857142...), supply 6/12h override vs
  fundamentals 1/6h, funding section-10.1 time coverage;
- status machine verbatim (EXCLUDED/WATCH/CANDIDATE, BLOCKED > NOT_READY >
  MEDIUM > MULTIPLIER > LISTING_AGE > PAUSE > READY, display derivation,
  reasons BLOCK -> PAUSE -> NOT_READY, warnings independent);
- LTSS=25+PAUSE -> EXCLUDED, LTSS=84+PAUSE -> PAUSED, MEDIUM -> CANDIDATE /
  NOT_READY, BLOCK coverage, Entry null never READY, tradeability 7 boundary,
  key-field stale -> top stale + NOT_READY + READY_INPUT_STALE while non-key
  stale stays in details.
"""

from __future__ import annotations

import datetime as dt

import pytest

from diveintocrypto_desktop.shortlab.config import load_shortlab_config
from diveintocrypto_desktop.shortlab.models import CandidateState
from diveintocrypto_desktop.shortlab.quality import (
    FULL_GROUP_WEIGHTS,
    GROUP_FIELD_SHARES,
    LITE_GROUP_WEIGHTS,
    READY_REQUIRED_FIELDS_LITE,
    FieldState,
    data_quality,
)
from diveintocrypto_desktop.shortlab.risk.squeeze import detect_squeeze
from diveintocrypto_desktop.shortlab.risk.veto import (
    ENTRY_BELOW_READY_THRESHOLD,
    ENTRY_NOT_AVAILABLE,
    IDENTITY_REVIEW_REQUIRED,
    LISTING_AGE_UNKNOWN,
    MULTIPLIER_UNVERIFIED,
    PAUSE_BREAKOUT_24H,
    PAUSE_BREAKOUT_7D,
    PAUSE_CONTRACT_STATUS_UNVERIFIED,
    PAUSE_MAJOR_CATALYST,
    PAUSE_NEGATIVE_CARRY,
    PAUSE_NEW_TOKEN,
    PAUSE_SQUEEZE,
    READY_INPUT_STALE,
    VETO_CONTRACT_DELISTING,
    VETO_DATA_IDENTITY,
    VETO_LOW_DATA_QUALITY,
    VETO_LOW_LIQUIDITY,
    WARN_FUNDING_WEAKENING,
    WARN_HIGH_VOLATILITY,
    RiskResult,
    derive_status,
    evaluate_risks,
)

ASOF_MS = int(dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _fetched(age_sec: float) -> int:
    return int(ASOF_MS - age_sec * 1000)


def _fresh_states(**overrides) -> list[FieldState]:
    """All LITE fields fresh (offsets well inside every TTL)."""
    base: dict[str, FieldState] = {
        "market_daily_price": FieldState("market_daily_price", "OK", fetched_at_ms=_fetched(60)),
        "futures_qv_1d": FieldState("futures_qv_1d", "OK", fetched_at_ms=_fetched(60)),
        "oi_usd": FieldState("oi_usd", "OK", fetched_at_ms=_fetched(60)),
        "contract_status": FieldState("contract_status", "OK", fetched_at_ms=_fetched(60)),
        "funding_7d": FieldState("funding_7d", "OK", fetched_at_ms=_fetched(60)),
        "funding_30d": FieldState("funding_30d", "OK", fetched_at_ms=_fetched(60)),
        "funding_90d": FieldState("funding_90d", "OK", fetched_at_ms=_fetched(60)),
        "mc": FieldState("mc", "OK", fetched_at_ms=_fetched(60)),
        "fdv": FieldState("fdv", "OK", fetched_at_ms=_fetched(60)),
        "supply_float": FieldState("supply_float", "OK", fetched_at_ms=_fetched(60)),
        "ath": FieldState("ath", "OK", fetched_at_ms=_fetched(60)),
        "spot_60d_qv": FieldState("spot_60d_qv", "OK", fetched_at_ms=_fetched(60)),
        "spot_24h_qv": FieldState("spot_24h_qv", "OK", fetched_at_ms=_fetched(60)),
        "basis": FieldState("basis", "OK", fetched_at_ms=_fetched(60)),
        "book_depth": FieldState("book_depth", "OK", fetched_at_ms=_fetched(60)),
        "canonical_mapping": FieldState("canonical_mapping", "OK", fetched_at_ms=_fetched(60)),
        "profile_basis": FieldState("profile_basis", "OK", fetched_at_ms=_fetched(60)),
    }
    base.update(overrides)
    return list(base.values())


def _full_fresh_states(**overrides) -> list[FieldState]:
    states = _fresh_states()
    extra = [
        FieldState("unlock_30d", "OK", fetched_at_ms=_fetched(60)),
        FieldState("unlock_90d", "OK", fetched_at_ms=_fetched(60)),
        FieldState("unlock_allocation", "OK", fetched_at_ms=_fetched(60)),
        FieldState("social_volume", "OK", fetched_at_ms=_fetched(60)),
        FieldState("social_contributors", "OK", fetched_at_ms=_fetched(60)),
        FieldState("social_dominance", "OK", fetched_at_ms=_fetched(60)),
        FieldState("social_window", "OK", fetched_at_ms=_fetched(60)),
        FieldState("catalyst_coverage", "OK", fetched_at_ms=_fetched(60)),
        FieldState("catalyst_dedup", "OK", fetched_at_ms=_fetched(60)),
    ]
    by_id = {s.field_id: s for s in states}
    for s in extra:
        by_id[s.field_id] = s
    by_id.update(overrides)
    return list(by_id.values())


def _identity(confidence="VERIFIED", multiplier=1.0, onboard_valid=True):
    return {
        "mapping_confidence": confidence,
        "contract_multiplier": multiplier,
        "has_valid_onboard": onboard_valid,
    }


def _ready_metadata(**overrides):
    meta = {
        "as_of_ms": ASOF_MS,
        "mapping_confidence": "VERIFIED",
        "futures_qv_1d": 35_000_000.0,
        "oi_value_usd": 15_000_000.0,
        "contract_status": "TRADING",
        "exchange_status": "TRADING",
        "live_universe_present": True,
        "previously_seen": True,
        "onboard_at_ms": ASOF_MS - 200 * 86_400_000,
        "price_change_24h": 0.01,
        "price_change_7d": 0.02,
        "oi_change_7d": 0.0,
        "funding_30d": 0.012,
        "funding_7d": 0.003,
        "funding_positive_ratio_30d": 0.8,
        "funding_positive_ratio_7d": 0.8,
    }
    meta.update(overrides)
    return meta


# ---------------------------------------------------------------------------
# Weights / shares
# ---------------------------------------------------------------------------


class TestWeights:
    def test_lite_weights_sum_100(self):
        assert LITE_GROUP_WEIGHTS == {
            "market_futures": 35,
            "funding_history": 20,
            "fundamentals": 20,
            "spot_liquidity": 15,
            "identity_profile": 10,
        }
        assert sum(LITE_GROUP_WEIGHTS.values()) == 100

    def test_full_weights_sum_100(self):
        assert FULL_GROUP_WEIGHTS == {
            "market_futures": 25,
            "funding_history": 15,
            "fundamentals": 15,
            "spot_liquidity": 10,
            "identity_profile": 10,
            "unlock": 10,
            "social": 10,
            "catalyst": 5,
        }
        assert sum(FULL_GROUP_WEIGHTS.values()) == 100

    def test_group_shares_sum_100_percent(self):
        expected = {
            "market_futures": {
                "market_daily_price": 0.35,
                "futures_qv_1d": 0.20,
                "oi_usd": 0.25,
                "contract_status": 0.20,
            },
            "funding_history": {"funding_7d": 0.20, "funding_30d": 0.40, "funding_90d": 0.40},
            "fundamentals": {"mc": 0.30, "fdv": 0.20, "supply_float": 0.25, "ath": 0.25},
            "spot_liquidity": {
                "spot_60d_qv": 0.40,
                "spot_24h_qv": 0.20,
                "basis": 0.20,
                "book_depth": 0.20,
            },
            "identity_profile": {"canonical_mapping": 0.60, "profile_basis": 0.40},
            "unlock": {"unlock_30d": 0.40, "unlock_90d": 0.40, "unlock_allocation": 0.20},
            "social": {
                "social_volume": 0.35,
                "social_contributors": 0.25,
                "social_dominance": 0.25,
                "social_window": 0.15,
            },
            "catalyst": {"catalyst_coverage": 0.60, "catalyst_dedup": 0.40},
        }
        assert GROUP_FIELD_SHARES == expected
        for group, shares in GROUP_FIELD_SHARES.items():
            assert sum(shares.values()) == pytest.approx(1.0), group

    def test_thresholds_match_default_config(self):
        config = load_shortlab_config()
        assert (config.candidate.watch_ltss, config.candidate.candidate_ltss) == (60, 70)
        assert (config.candidate.ready_ltss, config.candidate.ready_entry) == (80, 70)
        assert config.candidate.ready_data_quality == 80
        assert config.candidate.ready_tradeability_score == 7
        assert config.veto.breakout_24h == pytest.approx(0.35)
        assert config.veto.breakout_7d == pytest.approx(0.70)
        assert config.veto.new_token_days == 45

    def test_full_without_catalyst_not_enabled(self):
        with pytest.raises(ValueError, match="CATALYST|FULL_PREREQUISITE"):
            data_quality("FULL", _fresh_states(), ASOF_MS)
        # FULL with catalyst present computes.
        breakdown = data_quality("FULL", _full_fresh_states(), ASOF_MS)
        assert breakdown.tier == "FULL"
        assert breakdown.data_quality == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# DQ formula
# ---------------------------------------------------------------------------


class TestDQFormula:
    def test_all_fresh_is_100(self):
        breakdown = data_quality("LITE", _fresh_states(), ASOF_MS)
        assert breakdown.data_quality == pytest.approx(100.0)
        assert breakdown.stale is False
        assert breakdown.ready_input_stale is False
        assert all(c == pytest.approx(1.0) for c in breakdown.group_credits.values())

    def test_final_rounding_half_up(self):
        # 60/70 market PARTIAL -> market group 0.95 -> DQ 98.25 -> 98.3.
        states = _fresh_states(
            market_daily_price=FieldState(
                "market_daily_price",
                "PARTIAL",
                valid_count=60,
                required_count=70,
                fetched_at_ms=_fetched(60),
            )
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        detail = breakdown.field_details["market_daily_price"]
        assert detail.coverage == pytest.approx(60 / 70)
        assert detail.coverage != pytest.approx(0.0)
        assert detail.coverage != pytest.approx(1.0)
        assert detail.freshness == pytest.approx(1.0)
        assert detail.credit == pytest.approx(60 / 70)
        assert breakdown.group_credits["market_futures"] == pytest.approx(0.95)
        # 35*0.95 + 65 = 98.25 -> half-up 1dp = 98.3.
        assert breakdown.data_quality == pytest.approx(98.3)

    def test_partial_coverages(self):
        states = _fresh_states(
            supply_float=FieldState(
                "supply_float", "PARTIAL", valid_count=1, required_count=2,
                fetched_at_ms=_fetched(60),
            ),
            ath=FieldState(
                "ath", "PARTIAL", valid_count=1, required_count=2,
                fetched_at_ms=_fetched(60),
            ),
            book_depth=FieldState(
                "book_depth", "PARTIAL", valid_count=1, required_count=2,
                fetched_at_ms=_fetched(60),
            ),
            spot_60d_qv=FieldState(
                "spot_60d_qv", "PARTIAL", valid_count=30, required_count=60,
                fetched_at_ms=_fetched(60),
            ),
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        assert breakdown.field_details["supply_float"].coverage == pytest.approx(0.5)
        assert breakdown.field_details["ath"].coverage == pytest.approx(0.5)
        assert breakdown.field_details["book_depth"].coverage == pytest.approx(0.5)
        assert breakdown.field_details["spot_60d_qv"].coverage == pytest.approx(0.5)
        # Fundamentals group: 0.30*1 + 0.20*1 + 0.25*0.5 + 0.25*0.5 = 0.75.
        assert breakdown.group_credits["fundamentals"] == pytest.approx(0.75)
        # Spot group: 0.40*0.5 + 0.20*1 + 0.20*1 + 0.20*0.5 = 0.70.
        assert breakdown.group_credits["spot_liquidity"] == pytest.approx(0.70)

    def test_funding_uses_time_coverage_directly(self):
        states = _fresh_states(
            funding_30d=FieldState(
                "funding_30d", "PARTIAL", coverage=0.6, fetched_at_ms=_fetched(60)
            )
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        assert breakdown.field_details["funding_30d"].coverage == pytest.approx(0.6)
        assert breakdown.field_details["funding_30d"].credit == pytest.approx(0.6)
        # Funding group: 0.2*1 + 0.4*0.6 + 0.4*1 = 0.84.
        assert breakdown.group_credits["funding_history"] == pytest.approx(0.84)

    def test_stale_is_half_and_expired_is_zero(self):
        # funding ttl 1800 / grace 7200: 3600s age -> stale 0.5.
        states = _fresh_states(
            funding_30d=FieldState("funding_30d", "OK", fetched_at_ms=_fetched(3600))
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        detail = breakdown.field_details["funding_30d"]
        assert detail.freshness == pytest.approx(0.5)
        assert detail.freshness_label == "STALE"
        assert detail.credit == pytest.approx(0.5)
        # Beyond grace -> 0.
        states = _fresh_states(
            funding_30d=FieldState("funding_30d", "OK", fetched_at_ms=_fetched(8000))
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        detail = breakdown.field_details["funding_30d"]
        assert detail.freshness == pytest.approx(0.0)
        assert detail.freshness_label == "EXPIRED"
        assert detail.credit == pytest.approx(0.0)

    def test_supply_override_vs_fundamentals_window(self):
        # Supply 6h/12h: 2h age is fresh; fundamentals 1h/6h: 2h age is stale.
        states = _fresh_states(
            supply_float=FieldState("supply_float", "OK", fetched_at_ms=_fetched(7200)),
            mc=FieldState("mc", "OK", fetched_at_ms=_fetched(7200)),
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        assert breakdown.field_details["supply_float"].freshness == pytest.approx(1.0)
        assert breakdown.field_details["mc"].freshness == pytest.approx(0.5)
        # Supply beyond its 12h grace expires while a 5.5h-old MC is stale.
        states = _fresh_states(
            supply_float=FieldState("supply_float", "OK", fetched_at_ms=_fetched(50000)),
            mc=FieldState("mc", "OK", fetched_at_ms=_fetched(20000)),
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        assert breakdown.field_details["supply_float"].freshness_label == "EXPIRED"
        assert breakdown.field_details["mc"].freshness == pytest.approx(0.5)

    def test_not_applicable_removes_only_inner_denominator(self):
        states = _fresh_states(
            spot_60d_qv=FieldState(
                "spot_60d_qv", "NOT_APPLICABLE", fetched_at_ms=_fetched(60),
                reason_code="no_spot_market",
            ),
            spot_24h_qv=FieldState(
                "spot_24h_qv", "NOT_APPLICABLE", fetched_at_ms=_fetched(60),
                reason_code="no_spot_market",
            ),
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        # Spot group keeps basis/book (20%+20%) -> still 1.0, DQ stays 100.
        assert breakdown.group_credits["spot_liquidity"] == pytest.approx(1.0)
        assert breakdown.data_quality == pytest.approx(100.0)

    def test_all_na_group_contributes_zero(self):
        states = _fresh_states(
            **{
                fid: FieldState(fid, "NOT_APPLICABLE", fetched_at_ms=_fetched(60))
                for fid in ("spot_60d_qv", "spot_24h_qv", "basis", "book_depth")
            }
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        assert breakdown.group_credits["spot_liquidity"] == pytest.approx(0.0)
        assert breakdown.group_status["spot_liquidity"] == "NOT_APPLICABLE"
        # Other groups full: 35+20+20+10 = 85.
        assert breakdown.data_quality == pytest.approx(85.0)

    def test_unavailable_stays_in_denominator(self):
        states = _fresh_states(
            book_depth=FieldState("book_depth", "UNAVAILABLE", fetched_at_ms=_fetched(60))
        )
        breakdown = data_quality("LITE", states, ASOF_MS)
        # Spot: 0.40+0.20+0.20+0 = 0.80.
        assert breakdown.group_credits["spot_liquidity"] == pytest.approx(0.80)
        # DQ: 35+20+20+15*0.8+10 = 97.0.
        assert breakdown.data_quality == pytest.approx(97.0)

    def test_key_stale_raises_top_stale_non_key_does_not(self):
        assert "funding_30d" in READY_REQUIRED_FIELDS_LITE
        assert "basis" not in READY_REQUIRED_FIELDS_LITE
        key_stale = data_quality(
            "LITE",
            _fresh_states(
                funding_30d=FieldState("funding_30d", "OK", fetched_at_ms=_fetched(3600))
            ),
            ASOF_MS,
        )
        assert key_stale.stale is True
        assert key_stale.ready_input_stale is True
        assert key_stale.ready_stale_fields == ("funding_30d",)
        non_key_stale = data_quality(
            "LITE",
            _fresh_states(basis=FieldState("basis", "OK", fetched_at_ms=_fetched(7200))),
            ASOF_MS,
        )
        assert non_key_stale.stale is False
        assert non_key_stale.ready_input_stale is False
        assert non_key_stale.field_details["basis"].freshness == pytest.approx(0.5)
        assert non_key_stale.data_quality < 100.0


# ---------------------------------------------------------------------------
# Risk engine
# ---------------------------------------------------------------------------


class TestEvaluateRisks:
    def test_clean_inputs_have_no_risk(self):
        risk = evaluate_risks({}, _ready_metadata(), 95.0)
        assert risk.vetoes == ()
        assert risk.pauses == ()
        assert isinstance(risk, RiskResult)

    def test_deterministic(self):
        meta = _ready_metadata()
        first = evaluate_risks({}, meta, 95.0)
        for _ in range(50):
            assert evaluate_risks({}, dict(meta), 95.0) == first

    def test_veto_low_data_quality(self):
        risk = evaluate_risks({}, _ready_metadata(), 59.9)
        assert VETO_LOW_DATA_QUALITY in risk.vetoes
        assert evaluate_risks({}, _ready_metadata(), 60.0).vetoes == ()

    def test_veto_identity(self):
        for confidence in ("LOW", "UNRESOLVED"):
            risk = evaluate_risks(
                {}, _ready_metadata(mapping_confidence=confidence), 95.0
            )
            assert VETO_DATA_IDENTITY in risk.vetoes
        risk = evaluate_risks({}, _ready_metadata(identity_conflict=True), 95.0)
        assert VETO_DATA_IDENTITY in risk.vetoes

    def test_veto_low_liquidity(self):
        risk = evaluate_risks({}, _ready_metadata(futures_qv_1d=9_999_999.0), 95.0)
        assert VETO_LOW_LIQUIDITY in risk.vetoes
        risk = evaluate_risks({}, _ready_metadata(oi_value_usd=1_999_999.0), 95.0)
        assert VETO_LOW_LIQUIDITY in risk.vetoes
        risk = evaluate_risks({}, _ready_metadata(contract_status="HALT"), 95.0)
        assert VETO_LOW_LIQUIDITY in risk.vetoes

    def test_veto_delisting_vs_unverified_disappearance(self):
        risk = evaluate_risks(
            {}, _ready_metadata(exchange_status="SETTLING", live_universe_present=False), 95.0
        )
        assert VETO_CONTRACT_DELISTING in risk.vetoes
        risk = evaluate_risks(
            {}, _ready_metadata(delivery_at_ms=ASOF_MS + 2 * 86_400_000), 95.0
        )
        assert VETO_CONTRACT_DELISTING in risk.vetoes
        # Far-future placeholder delivery never vetoes.
        risk = evaluate_risks(
            {}, _ready_metadata(delivery_at_ms=ASOF_MS + 400 * 86_400_000), 95.0
        )
        assert VETO_CONTRACT_DELISTING not in risk.vetoes
        # Disappearance without terminal evidence pauses instead.
        risk = evaluate_risks(
            {}, _ready_metadata(live_universe_present=False, exchange_status="TRADING"), 95.0
        )
        assert VETO_CONTRACT_DELISTING not in risk.vetoes
        assert PAUSE_CONTRACT_STATUS_UNVERIFIED in risk.pauses

    def test_pause_breakouts(self):
        risk = evaluate_risks({}, _ready_metadata(price_change_24h=0.35), 95.0)
        assert PAUSE_BREAKOUT_24H in risk.pauses
        risk = evaluate_risks({}, _ready_metadata(price_change_24h=0.349), 95.0)
        assert PAUSE_BREAKOUT_24H not in risk.pauses
        risk = evaluate_risks({}, _ready_metadata(price_change_7d=0.70), 95.0)
        assert PAUSE_BREAKOUT_7D in risk.pauses

    def test_pause_squeeze_needs_confirmation_leg(self):
        assert detect_squeeze(0.12, 0.15, taker_buy_ratio=0.60, micro_score=None) is True
        assert detect_squeeze(0.12, 0.15, taker_buy_ratio=None, micro_score=40.0) is True
        # Price+OI alone never pauses.
        assert detect_squeeze(0.12, 0.15, taker_buy_ratio=None, micro_score=None) is False
        assert detect_squeeze(0.05, 0.15, taker_buy_ratio=0.9, micro_score=None) is False
        risk = evaluate_risks(
            {},
            _ready_metadata(price_change_7d=0.12, oi_change_7d=0.15, taker_buy_ratio=0.6),
            95.0,
        )
        assert PAUSE_SQUEEZE in risk.pauses

    def test_pause_negative_carry(self):
        risk = evaluate_risks({}, _ready_metadata(funding_30d=-0.001), 95.0)
        assert PAUSE_NEGATIVE_CARRY in risk.pauses
        risk = evaluate_risks(
            {}, _ready_metadata(funding_positive_ratio_30d=0.2), 95.0
        )
        assert PAUSE_NEGATIVE_CARRY in risk.pauses

    def test_pause_new_token_only_with_valid_onboard(self):
        young = ASOF_MS - 10 * 86_400_000
        risk = evaluate_risks({}, _ready_metadata(onboard_at_ms=young), 95.0)
        assert PAUSE_NEW_TOKEN in risk.pauses
        old = ASOF_MS - 200 * 86_400_000
        risk = evaluate_risks({}, _ready_metadata(onboard_at_ms=old), 95.0)
        assert PAUSE_NEW_TOKEN not in risk.pauses
        # No valid onboard -> no pause here (derive_status reports UNKNOWN).
        meta = _ready_metadata()
        del meta["onboard_at_ms"]
        assert PAUSE_NEW_TOKEN not in evaluate_risks({}, meta, 95.0).pauses

    def test_pause_major_catalyst(self):
        risk = evaluate_risks({}, _ready_metadata(catalyst_major_event=True), 95.0)
        assert PAUSE_MAJOR_CATALYST in risk.pauses

    def test_warnings_never_block(self):
        risk = evaluate_risks(
            {},
            _ready_metadata(volatility_30d=0.20),
            95.0,
        )
        assert WARN_HIGH_VOLATILITY in risk.warnings
        assert risk.vetoes == () and risk.pauses == ()
        risk = evaluate_risks(
            {},
            _ready_metadata(
                funding_30d=0.01, funding_7d=-0.001,
                funding_positive_ratio_30d=0.8, funding_positive_ratio_7d=0.2,
            ),
            95.0,
        )
        assert WARN_FUNDING_WEAKENING in risk.warnings


# ---------------------------------------------------------------------------
# Status machine (design 17 verbatim)
# ---------------------------------------------------------------------------


def _ready_risk():
    return RiskResult(vetoes=(), pauses=(), warnings=())


class TestDeriveStatus:
    def test_low_ltss_with_pause_stays_excluded(self):
        risk = RiskResult(pauses=(PAUSE_BREAKOUT_24H,))
        state = derive_status(
            25, 80, 95.0, 8.0, _identity(), risk, False,
        )
        assert isinstance(state, CandidateState)
        assert state.candidate_status == "EXCLUDED"
        assert state.execution_status == "NOT_READY"
        assert state.status == "EXCLUDED"
        # Pause still reported even though it cannot upgrade EXCLUDED.
        assert PAUSE_BREAKOUT_24H in state.pauses
        assert PAUSE_BREAKOUT_24H in state.reasons

    def test_high_ltss_with_pause_is_paused(self):
        risk = RiskResult(pauses=(PAUSE_BREAKOUT_24H,))
        state = derive_status(
            84, 80, 95.0, 8.0, _identity(), risk, False,
        )
        assert state.candidate_status == "CANDIDATE"
        assert state.execution_status == "PAUSED"
        assert state.status == "PAUSED"

    def test_candidate_watch_boundaries(self):
        assert derive_status(59.9, 80, 95.0, 8.0, _identity(), _ready_risk(), False).candidate_status == "EXCLUDED"
        assert derive_status(None, 80, 95.0, 8.0, _identity(), _ready_risk(), False).candidate_status == "EXCLUDED"
        assert derive_status(60, 80, 95.0, 8.0, _identity(), _ready_risk(), False).candidate_status == "WATCH"
        assert derive_status(69.9, 80, 95.0, 8.0, _identity(), _ready_risk(), False).candidate_status == "WATCH"
        assert derive_status(70, 80, 95.0, 8.0, _identity(), _ready_risk(), False).candidate_status == "CANDIDATE"

    def test_medium_identity_is_candidate_not_ready(self):
        risk = RiskResult(pauses=(PAUSE_BREAKOUT_24H,))
        state = derive_status(
            84, 80, 95.0, 8.0, _identity(confidence="MEDIUM"), risk, False,
        )
        assert state.candidate_status == "CANDIDATE"
        assert state.execution_status == "NOT_READY"
        assert state.status == "CANDIDATE"
        assert IDENTITY_REVIEW_REQUIRED in state.reasons
        # Pause still in reasons even though MEDIUM outranks PAUSED.
        assert PAUSE_BREAKOUT_24H in state.reasons
        assert state.execution_status != "PAUSED"

    def test_block_covers_everything(self):
        for veto in (
            VETO_DATA_IDENTITY,
            VETO_LOW_DATA_QUALITY,
            VETO_LOW_LIQUIDITY,
            VETO_CONTRACT_DELISTING,
        ):
            risk = RiskResult(vetoes=(veto,), pauses=(PAUSE_BREAKOUT_24H,))
            state = derive_status(95, 95, 95.0, 10.0, _identity(), risk, False)
            assert state.execution_status == "BLOCKED", veto
            assert state.status == "BLOCKED", veto
            assert veto in state.reasons
        # Identity LOW also blocks via the safety net even with empty risk.
        state = derive_status(95, 95, 95.0, 10.0, _identity(confidence="LOW"), _ready_risk(), False)
        assert state.execution_status == "BLOCKED"
        assert VETO_DATA_IDENTITY in state.vetoes

    def test_multiplier_and_listing_age_gates(self):
        state = derive_status(
            85, 80, 95.0, 8.0, _identity(multiplier=None), _ready_risk(), False
        )
        assert state.execution_status == "NOT_READY"
        assert MULTIPLIER_UNVERIFIED in state.reasons
        state = derive_status(
            85, 80, 95.0, 8.0, _identity(onboard_valid=False), _ready_risk(), False
        )
        assert state.execution_status == "NOT_READY"
        assert LISTING_AGE_UNKNOWN in state.reasons

    def test_entry_null_never_ready(self):
        state = derive_status(85, None, 95.0, 8.0, _identity(), _ready_risk(), False)
        assert state.execution_status == "NOT_READY"
        assert state.status == "CANDIDATE"
        assert ENTRY_NOT_AVAILABLE in state.reasons
        state = derive_status(85, 69.9, 95.0, 8.0, _identity(), _ready_risk(), False)
        assert ENTRY_BELOW_READY_THRESHOLD in state.reasons

    def test_tradeability_boundary(self):
        ready = derive_status(85, 80, 95.0, 7.0, _identity(), _ready_risk(), False)
        assert ready.execution_status == "READY"
        assert ready.status == "READY"
        not_ready = derive_status(85, 80, 95.0, 6.9, _identity(), _ready_risk(), False)
        assert not_ready.execution_status == "NOT_READY"
        assert "TRADEABILITY_BELOW_READY" in not_ready.reasons

    def test_ready_gate_all_or_nothing(self):
        # Fully eligible -> READY.
        state = derive_status(85, 75, 90.0, 8.0, _identity(), _ready_risk(), False)
        assert (state.execution_status, state.status) == ("READY", "READY")
        assert state.reasons == ()
        # Each gate alone breaks READY.
        assert derive_status(79.9, 75, 90.0, 8.0, _identity(), _ready_risk(), False).execution_status == "NOT_READY"
        assert derive_status(85, 69.9, 90.0, 8.0, _identity(), _ready_risk(), False).execution_status == "NOT_READY"
        assert derive_status(85, 75, 79.9, 8.0, _identity(), _ready_risk(), False).execution_status == "NOT_READY"
        assert derive_status(85, 75, 90.0, 6.9, _identity(), _ready_risk(), False).execution_status == "NOT_READY"

    def test_key_stale_blocks_ready_non_key_does_not(self):
        key_stale = data_quality(
            "LITE",
            _fresh_states(
                funding_30d=FieldState("funding_30d", "OK", fetched_at_ms=_fetched(3600))
            ),
            ASOF_MS,
        )
        assert key_stale.stale is True
        state = derive_status(
            85, 80, key_stale.data_quality, 8.0, _identity(), _ready_risk(), key_stale.stale
        )
        assert state.execution_status == "NOT_READY"
        assert READY_INPUT_STALE in state.reasons
        assert state.status == "CANDIDATE"

        non_key_stale = data_quality(
            "LITE",
            _fresh_states(basis=FieldState("basis", "OK", fetched_at_ms=_fetched(7200))),
            ASOF_MS,
        )
        assert non_key_stale.stale is False
        assert non_key_stale.field_details["basis"].freshness == pytest.approx(0.5)
        state = derive_status(
            85, 80, non_key_stale.data_quality, 8.0, _identity(), _ready_risk(),
            non_key_stale.stale,
        )
        assert state.execution_status == "READY"
        assert state.status == "READY"

    def test_reasons_deduped_and_ordered_warnings_independent(self):
        risk = RiskResult(
            vetoes=(VETO_LOW_LIQUIDITY, VETO_LOW_LIQUIDITY, VETO_LOW_DATA_QUALITY),
            pauses=(PAUSE_NEGATIVE_CARRY, PAUSE_NEGATIVE_CARRY, PAUSE_SQUEEZE),
            warnings=(WARN_HIGH_VOLATILITY, WARN_FUNDING_WEAKENING),
        )
        state = derive_status(
            75, None, 59.0, 5.0, _identity(confidence="MEDIUM", multiplier=None),
            risk, True,
        )
        assert state.execution_status == "BLOCKED"
        assert state.status == "BLOCKED"
        # Deduped.
        assert len(state.reasons) == len(set(state.reasons))
        # BLOCK before PAUSE before NOT_READY.
        first_veto = min(state.reasons.index(v) for v in state.vetoes)
        first_pause = min(state.reasons.index(p) for p in state.pauses)
        assert first_veto < first_pause
        # Warnings independent: never leak into reasons.
        assert WARN_HIGH_VOLATILITY not in state.reasons
        assert WARN_FUNDING_WEAKENING not in state.reasons
        assert set(state.warnings) == {WARN_HIGH_VOLATILITY, WARN_FUNDING_WEAKENING}

    def test_end_to_end_dq_risk_status(self):
        breakdown = data_quality("LITE", _fresh_states(), ASOF_MS)
        risk = evaluate_risks({}, _ready_metadata(), breakdown.data_quality)
        assert risk.vetoes == () and risk.pauses == ()
        state = derive_status(
            85, 75, breakdown, 8.0, _identity(), risk, breakdown.stale
        )
        assert (state.execution_status, state.status) == ("READY", "READY")

    def test_no_network_or_sql_imports(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "diveintocrypto_desktop" / "shortlab"
        for rel in ("quality.py", "risk/veto.py", "risk/squeeze.py"):
            text = (root / rel).read_text(encoding="utf-8")
            for token in ("aiohttp", "requests", "httpx", "duckdb", "sqlite", "get_json", "fetch_"):
                assert token not in text, f"{rel} must stay pure (found {token!r})"
