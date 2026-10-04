"""H03 HedgeDQ + opportunity ranking (pure, offline).

Plan H03.5 / B38 / B8.4. No network.
"""

from __future__ import annotations

import pytest

from diveintocrypto_desktop.shortlab.hedge.funding_score import score_fcs
from diveintocrypto_desktop.shortlab.hedge.models import FCSResult, FundingMetrics
from diveintocrypto_desktop.shortlab.hedge.opportunity import (
    compute_hedge_quality,
    rank_opportunities,
)

CUTOFF = 1_735_689_600_000


def _policy():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    return load_shortlab_config()


def _full_states(**over):
    base = {
        "funding.current_rate_time": True,
        "funding.next_time_interval": True,
        "funding.history_7d": {"complete": True, "coverage_fraction": 1.0},
        "funding.history_30d": {"complete": True, "coverage_fraction": 1.0},
        "funding.history_90d": {"complete": True, "coverage_fraction": 1.0},
        "funding.std_rolling": True,
        "futures.mark_time": True,
        "futures.exit_depth_vwap": True,
        "futures.trading_rules": True,
        "futures.symbol_status": True,
        "spot_venue.buy_quote": True,
        "spot_venue.sell_quote": True,
        "spot_venue.rules_decimals": True,
        "spot_venue.cost_fee_gas": True,
        "identity_units.asset_identity": True,
        "identity_units.multiplier": True,
        "identity_units.quote_usd_fx": True,
        "contract_time.lifecycle": True,
        "contract_time.source_time": True,
        "contract_time.time_alignment": True,
        "contract_time.heartbeat": True,
    }
    base.update(over)
    return base


def test_quality_full_verified_scores_100_ready():
    res = compute_hedge_quality(_full_states(), _policy(), CUTOFF)
    assert res.score == pytest.approx(100.0)
    assert res.readiness == "READY"
    assert res.group_scores["funding"] == pytest.approx(100.0)


def test_quality_funding_history_needs_complete_despite_high_fraction():
    states = _full_states(**{
        "funding.history_30d": {"complete": False, "coverage_fraction": 0.99},
    })
    res = compute_hedge_quality(states, _policy(), CUTOFF)
    # 30D share is 30/100 of funding group (weight 30): loss = 30*0.99*0.3?
    assert res.score < 100.0
    assert res.field_credits["funding.history_30d"] == pytest.approx(0.0)
    # Fraction variant honoured when complete.
    states2 = _full_states(**{
        "funding.history_30d": {"complete": True, "coverage_fraction": 0.5},
    })
    res2 = compute_hedge_quality(states2, _policy(), CUTOFF)
    assert res2.field_credits["funding.history_30d"] == pytest.approx(0.5)
    assert res2.score > res.score


def test_quality_90d_NA_excluded_and_renormalised():
    states = _full_states(**{
        "funding.history_90d": {"applicable": False, "reason": "INSUFFICIENT_ASSET_AGE"},
    })
    res = compute_hedge_quality(states, _policy(), CUTOFF)
    # Funding group denominator drops 20 share: other 80 must still give 100%.
    assert res.group_scores["funding"] == pytest.approx(100.0)
    assert res.score == pytest.approx(100.0)
    assert any("N/A" in r or "INSUFFICIENT" in r for r in res.reasons)


def test_quality_ordinary_missing_90d_scores_zero_not_NA():
    states = _full_states(**{
        "funding.history_90d": {"complete": False, "coverage_fraction": 0.0},
    })
    res = compute_hedge_quality(states, _policy(), CUTOFF)
    assert res.field_credits["funding.history_90d"] == pytest.approx(0.0)
    # Funding group: (100-20)/100 = 80% => total 30*0.8 + 70 = 94.
    assert res.group_scores["funding"] == pytest.approx(80.0)
    assert res.score == pytest.approx(94.0)


def test_quality_identity_and_exit_never_NA():
    states = _full_states(**{
        "identity_units.asset_identity": {"applicable": False, "reason": "INSUFFICIENT_ASSET_AGE"},
        "spot_venue.sell_quote": {"applicable": False, "reason": "INSUFFICIENT_ASSET_AGE"},
    })
    res = compute_hedge_quality(states, _policy(), CUTOFF)
    # Never excluded: credits 0 with denominator intact.
    assert res.field_credits["identity_units.asset_identity"] == pytest.approx(0.0)
    assert res.field_credits["spot_venue.sell_quote"] == pytest.approx(0.0)
    assert res.score < 100.0


def test_quality_reads_weights_from_policy():
    import copy

    policy = _policy()
    res_base = compute_hedge_quality(_full_states(), policy, CUTOFF)
    assert res_base.score == pytest.approx(100.0)
    # Drop one spot field: loss depends on policy shares verbatim.
    states = _full_states(**{"spot_venue.sell_quote": False})
    res = compute_hedge_quality(states, policy, CUTOFF)
    # sell share 35/100 of spot group (weight 25): loss = 25*0.35 = 8.75.
    assert res.score == pytest.approx(100.0 - 8.75)
    # Custom policy with different group weight changes the same loss (sum stays 100).
    import copy as _copy

    full_q = _copy.deepcopy(dict(policy.hedge.quality))
    full_q["group_weights"] = dict(full_q["group_weights"])
    # Move 5 points funding->spot (30->25, 25->30): same missing field loses 30*0.35=10.5.
    full_q["group_weights"]["spot_venue"] = 30
    full_q["group_weights"]["funding"] = 25
    custom = {"quality": full_q}
    res_custom = compute_hedge_quality(states, custom, CUTOFF)
    assert res_custom.score == pytest.approx(100.0 - 10.5)


def _fcs(sym, snap, fcs, funding_30d="0.04", pr30="0.9", be=None):
    fm = {"funding_30d": funding_30d, "positive_ratio_30d": pr30}
    risk = {"break_even_days": be} if be is not None else {}
    return FCSResult(
        snapshot_id=snap, symbol=sym, canonical_id=sym.lower(), as_of_ms=CUTOFF,
        fcs_version="fcs_v1", fcs_config_hash="h" * 64,
        reference_notional_usd="10000", fcs=fcs,
        module_scores={}, funding_metrics=dict(fm), venue_summary={},
        basis=None, risk=dict(risk), readiness="READY" if fcs is not None else "NOT_READY",
        reasons=(), created_at_ms=CUTOFF,
    )


def test_rank_numeric_nulls_last_symbol_id():
    a = _fcs("B", "id2", 80.0)
    b = _fcs("A", "id1", 80.0)
    c = _fcs("C", "id0", None)
    d = _fcs("A", "id0", 80.0)
    out = rank_opportunities([a, b, c, d], sort="fcs", order="desc")
    assert [r.snapshot_id for r in out] == ["id0", "id1", "id2", "id0"]
    assert [r.symbol for r in out] == ["A", "A", "B", "C"]
    # asc keeps nulls last as well.
    out_asc = rank_opportunities([c, a, b], sort="fcs", order="asc")
    assert out_asc[-1].fcs is None


def test_rank_does_not_normalise_by_available_max():
    # PARTIAL 85 (max 90) must not outrank FULL 86 via 85/90 normalisation.
    partial = _fcs("AAA", "p1", 85.0)
    full = _fcs("BBB", "f1", 86.0)
    out = rank_opportunities([partial, full], sort="fcs", order="desc")
    assert [r.snapshot_id for r in out] == ["f1", "p1"]
    out_asc = rank_opportunities([partial, full], sort="fcs", order="asc")
    assert [r.snapshot_id for r in out_asc] == ["p1", "f1"]


def test_rank_each_sort_key():
    r1 = _fcs("A", "s1", 70.0, funding_30d="0.05", pr30="0.9", be=2.0)
    r2 = _fcs("B", "s2", 80.0, funding_30d="0.03", pr30="0.95", be=5.0)
    r3 = _fcs("C", "s3", None, funding_30d="0.09", pr30=None, be=None)
    assert [r.symbol for r in rank_opportunities([r1, r2, r3], sort="fcs", order="desc")] == ["B", "A", "C"]
    assert [r.symbol for r in rank_opportunities([r1, r2, r3], sort="funding30d", order="desc")] == ["C", "A", "B"]
    assert [r.symbol for r in rank_opportunities([r1, r2, r3], sort="positiveRatio30d", order="desc")] == ["B", "A", "C"]
    assert [r.symbol for r in rank_opportunities([r1, r2, r3], sort="breakEvenDays", order="asc")] == ["A", "B", "C"]


def test_rank_duplicate_events_do_not_double_count_inputs():
    # Ranking inputs are already-scored snapshots; duplicates by (symbol,time)
    # are deduped upstream. Here assert stable tie-break on exact ties.
    r1 = _fcs("SAME", "id-a", 75.0)
    r2 = _fcs("SAME", "id-b", 75.0)
    out = rank_opportunities([r2, r1], sort="fcs", order="desc")
    assert [r.snapshot_id for r in out] == ["id-a", "id-b"]
