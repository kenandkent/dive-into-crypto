"""R05 Funding coverage, entry gate and FCS semantics (D05/D03.3).

Red->green: before R05 ``funding_schedule.compute_schedule_coverage`` and
``hedge.entry_gate.evaluate_funding_entry_gate`` did not exist
(ImportError); ``funding_score`` rejected ``fcs_v2`` policies. This file
fails on the frozen R00 baseline and passes after R05.

Pure offline only: fixed ``FIXTURE_NOW`` clock, D15 merged config, no
network/DB/clock reads.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from diveintocrypto_desktop.shortlab.config import load_shortlab_config
from diveintocrypto_desktop.shortlab.funding_schedule import (
    compute_priced_event_coverage,
    compute_schedule_coverage,
)
from diveintocrypto_desktop.shortlab.hedge.entry_gate import (
    evaluate_funding_entry_gate,
)
from diveintocrypto_desktop.shortlab.hedge.funding_score import (
    build_funding_context,
    build_funding_readiness_breakdown,
    compute_funding_metrics,
    score_fcs,
)
from diveintocrypto_desktop.shortlab.observations import ObservationMeta, Observed
from diveintocrypto_desktop.shortlab.repair_contracts import (
    FundingContext,
    FundingCoverage,
    FundingScheduleSegment,
    GateResult,
)
from tests.repair_fixtures import FIXTURE_NOW, make_funding_context

DAY_MS = 86_400_000
H8 = 8 * 3_600_000
H4 = 4 * 3_600_000

NOW = FIXTURE_NOW


def _default_policy():
    return load_shortlab_config()


def _observed(value: Any, *, source_as_of: int | None, known_at: int) -> Observed:
    return Observed(
        value=value,
        meta=ObservationMeta(
            status="OK",
            source="binance:fapi/fundingRate",
            source_as_of_ms=source_as_of,
            fetched_at_ms=known_at,
            known_at_ms=known_at,
        ),
    )


def _segment(
    seg_id: str = "sched-1",
    *,
    start: int,
    end: int | None,
    interval_hours: int = 8,
    anchor: int,
    known_at: int,
    verification: str = "CONFIRMED",
) -> FundingScheduleSegment:
    return FundingScheduleSegment(
        schedule_id=seg_id,
        symbol="1000PEPEUSDT",
        effective_from_ms=start,
        effective_to_ms=end,
        interval_hours=interval_hours,
        anchor_ms=anchor,
        known_at_ms=known_at,
        source="binance:fapi/fundingInfo",
        evidence_ref="ev-1",
        verification=verification,
    )


def _funding_event(t: int, rate: str, *, known_at: int, mark: Any = "100") -> dict:
    ev: dict[str, Any] = {
        "symbol": "1000PEPEUSDT",
        "funding_time_ms": t,
        "rate": rate,
        "known_at_ms": known_at,
    }
    if mark is not None:
        ev["mark_price"] = mark
    return ev


def _slots_8h_90d(end: int) -> list[int]:
    start = end - 90 * DAY_MS
    out = []
    t = end
    while t > start:
        out.append(t)
        t -= H8
    return sorted(out)


# ---------------------------------------------------------------------------
# Plan smoke: recent double-negative blocks high history
# ---------------------------------------------------------------------------

def test_recent_negative_blocks_high_history():
    policy = _default_policy()
    ctx = make_funding_context(current_rate="-0.0005", last_settled_rate="-0.0005")
    # Patch observations to carry the negative rates with fresh provenance.
    cur = _observed({"rate": "-0.0005", "funding_time_ms": NOW - 60_000},
                    source_as_of=NOW - 60_000, known_at=NOW - 50_000)
    last = _observed({"rate": "-0.0005", "funding_time_ms": NOW - H8},
                     source_as_of=NOW - H8, known_at=NOW - H8 + 5000)
    ctx = make_funding_context(
        current_rate="-0.0005",
        last_settled_rate="-0.0005",
        current_observation=cur,
        last_settled_observation=last,
    )
    result = evaluate_funding_entry_gate(ctx, policy, NOW)
    assert result.status == "FAIL"
    assert "FUNDING_CURRENT_NON_POSITIVE" in result.reasons


# ---------------------------------------------------------------------------
# 16-cross current x last matrix (pos/zero/neg/unknown)
# ---------------------------------------------------------------------------

_RATE_CASES = {
    "pos": ("0.0005", True),
    "zero": ("0", True),
    "neg": ("-0.0005", True),
    "unknown": (None, False),
}


def _matrix_context(cur_key: str, last_key: str) -> FundingContext:
    cur_rate, cur_known = _RATE_CASES[cur_key]
    last_rate, last_known = _RATE_CASES[last_key]
    if cur_key == "unknown":
        cur_obs = _observed(None, source_as_of=None, known_at=NOW - 50_000)
    else:
        assert cur_rate is not None
        cur_obs = _observed({"rate": cur_rate, "funding_time_ms": NOW - 60_000},
                            source_as_of=NOW - 60_000, known_at=NOW - 50_000)
    if last_key == "unknown":
        last_obs = None
    else:
        assert last_rate is not None
        last_obs = _observed({"rate": last_rate, "funding_time_ms": NOW - H8},
                             source_as_of=NOW - H8, known_at=NOW - H8 + 5000)
    kwargs: dict[str, Any] = {}
    if cur_rate is None:
        kwargs["current_rate"] = None
    else:
        kwargs["current_rate"] = cur_rate
    if last_rate is None:
        kwargs["last_settled_rate"] = None
    else:
        kwargs["last_settled_rate"] = last_rate
    kwargs["current_observation"] = cur_obs
    kwargs["last_settled_observation"] = last_obs
    return make_funding_context(**kwargs)


@pytest.mark.parametrize("cur_key", ["pos", "zero", "neg", "unknown"])
@pytest.mark.parametrize("last_key", ["pos", "zero", "neg", "unknown"])
def test_current_last_16_cross(cur_key: str, last_key: str):
    policy = _default_policy()
    ctx = _matrix_context(cur_key, last_key)
    res = evaluate_funding_entry_gate(ctx, policy, NOW)
    if cur_key == "unknown" or last_key == "unknown":
        assert res.status == "UNKNOWN"
        assert res.status != "PASS"
    elif cur_key == "pos" and last_key == "pos":
        assert res.status == "PASS"
    else:
        assert res.status == "FAIL"
        assert res.status != "PASS"
        if cur_key in ("zero", "neg"):
            assert "FUNDING_CURRENT_NON_POSITIVE" in res.reasons
        if last_key in ("zero", "neg"):
            assert "FUNDING_LAST_NON_POSITIVE" in res.reasons


def test_require_switches_disable_positivity():
    policy = _default_policy()
    ctx = _matrix_context("neg", "neg")
    # Both switches off: negativity no longer fails (history still valid).
    relaxed = {
        "require_current_positive": False,
        "require_last_settled_positive": False,
        "min_funding_7d": -100.0,
        "min_funding_30d": -100.0,
        "min_positive_ratio_30d": 0.0,
        "min_positive_ratio_90d": 0.0,
        "min_30d_coverage": 0.0,
        "min_90d_coverage": 0.0,
    }
    res = evaluate_funding_entry_gate(ctx, {"entry_gate": relaxed}, NOW)
    assert res.status == "PASS"
    # Only current switch off: last still fails.
    only_current_off = dict(relaxed)
    only_current_off["require_last_settled_positive"] = True
    only_current_off["min_positive_ratio_30d"] = 0.0
    res2 = evaluate_funding_entry_gate(ctx, {"entry_gate": only_current_off}, NOW)
    assert res2.status == "FAIL"
    assert "FUNDING_LAST_NON_POSITIVE" in res2.reasons
    assert "FUNDING_CURRENT_NON_POSITIVE" not in res2.reasons


def test_all_eight_entry_gate_keys_consumed():
    policy = _default_policy()
    ctx = _matrix_context("pos", "pos")
    base = evaluate_funding_entry_gate(ctx, policy, NOW)
    assert base.status == "PASS"
    # Each numeric threshold tightened just above the fixture value must flip
    # to FAIL with the matching reason family (proves consumption).
    # Fixture: funding_7d 0.004, funding_30d 0.018, pr30 0.85, pr90 0.8,
    # cov30 0.97777778, cov90 1. For 90D coverage (perfect 1.0) use an
    # imperfect 0.95 context so a 0.96 gate can fail inside [0,1].
    cases = [
        ("min_funding_7d", 0.005, "FUNDING_HISTORY_BELOW_MIN", None),
        ("min_funding_30d", 0.02, "FUNDING_HISTORY_BELOW_MIN", None),
        ("min_positive_ratio_30d", 0.86, "FUNDING_POSITIVE_RATIO_LOW", None),
        ("min_positive_ratio_90d", 0.81, "FUNDING_POSITIVE_RATIO_LOW", None),
        ("min_30d_coverage", 0.99, "FUNDING_COVERAGE_LOW", None),
        ("min_90d_coverage", 0.96, "FUNDING_COVERAGE_LOW", "imperfect90"),
    ]
    for key, tight, reason, variant in cases:
        use_ctx = ctx
        if variant == "imperfect90":
            # R00 make_funding_context conflates metrics.coverage_90d (str)
            # with context.coverage_90d (DTO); build the imperfect context
            # via the R05 unified assembler instead of the frozen helper.
            cov90 = FundingCoverage(
                NOW - 90 * DAY_MS, NOW, 270, 257, "0.95185185", "1", (), ()
            )
            use_ctx = build_funding_context(
                ctx.metrics, ctx.coverage_7d, ctx.coverage_30d, cov90,
                history_class=ctx.history_class,
                listing_age_days=ctx.listing_age_days,
                conservative_apr=ctx.conservative_apr,
                conservative_method=ctx.conservative_method,
                current_observation=ctx.current_observation,
                last_settled_observation=ctx.last_settled_observation,
                schedule_refs=ctx.schedule_refs,
                input_refs=dict(ctx.input_refs),
            )
        gate = dict(policy.funding_capture.entry_gate)
        gate[key] = tight
        res = evaluate_funding_entry_gate(use_ctx, {"entry_gate": gate}, NOW)
        assert res.status == "FAIL", key
        assert reason in res.reasons, key
    # Missing key must raise (no silent default).
    gate2 = dict(policy.funding_capture.entry_gate)
    del gate2["min_funding_7d"]
    with pytest.raises(ValueError):
        evaluate_funding_entry_gate(ctx, {"entry_gate": gate2}, NOW)


def test_threshold_boundaries_equal_passes():
    ctx = _matrix_context("pos", "pos")
    # Fixture: funding_7d 0.004, funding_30d 0.018, pr30 0.85, pr90 0.8,
    # cov30 0.97777778, cov90 1.
    gate = {
        "require_current_positive": True,
        "require_last_settled_positive": True,
        "min_funding_7d": 0.004,
        "min_funding_30d": 0.018,
        "min_positive_ratio_30d": 0.85,
        "min_positive_ratio_90d": 0.8,
        "min_30d_coverage": 0.97777778,
        "min_90d_coverage": 1.0,
    }
    assert evaluate_funding_entry_gate(ctx, {"entry_gate": gate}, NOW).status == "PASS"
    # One epsilon below on any single threshold flips to FAIL.
    below = dict(gate)
    below["min_positive_ratio_30d"] = 0.85000001
    res = evaluate_funding_entry_gate(ctx, {"entry_gate": below}, NOW)
    assert res.status == "FAIL"
    assert "FUNDING_POSITIVE_RATIO_LOW" in res.reasons


def test_current_stale_beyond_120s_fails():
    policy = _default_policy()
    stale_cur = _observed({"rate": "0.0005", "funding_time_ms": NOW - 121_000},
                          source_as_of=NOW - 121_000, known_at=NOW - 120_000)
    last = _observed({"rate": "0.0004", "funding_time_ms": NOW - H8},
                     source_as_of=NOW - H8, known_at=NOW - H8 + 5000)
    ctx = make_funding_context(
        current_rate="0.0005", last_settled_rate="0.0004",
        current_observation=stale_cur, last_settled_observation=last,
    )
    res = evaluate_funding_entry_gate(ctx, policy, NOW)
    assert res.status == "FAIL"
    assert "FUNDING_CURRENT_STALE" in res.reasons
    # Exactly 120s is still fresh (strictly greater fails).
    fresh_cur = _observed({"rate": "0.0005", "funding_time_ms": NOW - 120_000},
                          source_as_of=NOW - 120_000, known_at=NOW - 119_000)
    ctx2 = make_funding_context(
        current_rate="0.0005", last_settled_rate="0.0004",
        current_observation=fresh_cur, last_settled_observation=last,
    )
    assert evaluate_funding_entry_gate(ctx2, policy, NOW).status == "PASS"
    # Future known_at can never PASS.
    future_cur = _observed({"rate": "0.0005", "funding_time_ms": NOW - 60_000},
                           source_as_of=NOW - 60_000, known_at=NOW + 1000)
    ctx3 = make_funding_context(
        current_rate="0.0005", last_settled_rate="0.0004",
        current_observation=future_cur, last_settled_observation=last,
    )
    assert evaluate_funding_entry_gate(ctx3, policy, NOW).status != "PASS"


def test_last_uses_slot_match_not_120s_ttl():
    policy = _default_policy()
    # Last settled 8h old must still PASS (120s TTL must NOT apply to it).
    ctx = _matrix_context("pos", "pos")
    assert evaluate_funding_entry_gate(ctx, policy, NOW).status == "PASS"
    # Last value time disagreeing with its source time beyond 60s is UNKNOWN.
    bad_last = Observed(
        value={"rate": "0.0004", "funding_time_ms": NOW - H8},
        meta=ObservationMeta(
            status="OK", source="binance:fapi/fundingRate",
            source_as_of_ms=NOW - H8 - 120_000,
            fetched_at_ms=NOW - H8 + 5000, known_at_ms=NOW - H8 + 5000,
        ),
    )
    cur = _observed({"rate": "0.0005", "funding_time_ms": NOW - 60_000},
                    source_as_of=NOW - 60_000, known_at=NOW - 50_000)
    ctx2 = make_funding_context(
        current_rate="0.0005", last_settled_rate="0.0004",
        current_observation=cur, last_settled_observation=bad_last,
    )
    res = evaluate_funding_entry_gate(ctx2, policy, NOW)
    assert res.status == "UNKNOWN"
    assert "FUNDING_SCHEDULE_UNKNOWN" in res.reasons


def test_partial_90d_skips_90d_gates_with_na():
    policy = _default_policy()
    cur = _observed({"rate": "0.0005", "funding_time_ms": NOW - 60_000},
                    source_as_of=NOW - 60_000, known_at=NOW - 50_000)
    last = _observed({"rate": "0.0004", "funding_time_ms": NOW - H8},
                     source_as_of=NOW - H8, known_at=NOW - H8 + 5000)
    # PARTIAL with terrible 90D persistence/coverage still PASSES on 30D.
    # Coverage DTOs must stay FundingCoverage (R00 frozen): low 90D metric
    # ratios plus an unknown-schedule 90D coverage are N/A for PARTIAL.
    # NOTE: R00 make_funding_context cannot take a FundingCoverage object
    # for coverage_90d (it would also patch metrics.coverage_90d); build via
    # the R05 unified assembler.
    cov90_unknown = FundingCoverage(
        NOW - 90 * DAY_MS, NOW, None, 0, None, "0",
        (), ("FUNDING_SCHEDULE_UNKNOWN",),
    )
    import dataclasses as _dc

    _base = make_funding_context(
        current_rate="0.0005", last_settled_rate="0.0004",
        current_observation=cur, last_settled_observation=last,
    )
    _patched_metrics = _dc.replace(_base.metrics, positive_ratio_90d="0.05")
    ctx = build_funding_context(
        _patched_metrics, _base.coverage_7d, _base.coverage_30d, cov90_unknown,
        history_class="PARTIAL_90D", listing_age_days=45,
        conservative_apr=_base.conservative_apr,
        conservative_method=_base.conservative_method,
        current_observation=cur, last_settled_observation=last,
        schedule_refs=_base.schedule_refs, input_refs=dict(_base.input_refs),
    )
    res = evaluate_funding_entry_gate(ctx, policy, NOW)
    assert res.status == "PASS"
    assert "FUNDING_90D_NA" in res.reasons
    # Same 90D values with FULL_90D must FAIL/UNKNOWN (90D enforced).
    ctx_full = build_funding_context(
        _patched_metrics, _base.coverage_7d, _base.coverage_30d, cov90_unknown,
        history_class="FULL_90D", listing_age_days=120,
        conservative_apr=_base.conservative_apr,
        conservative_method=_base.conservative_method,
        current_observation=cur, last_settled_observation=last,
        schedule_refs=_base.schedule_refs, input_refs=dict(_base.input_refs),
    )
    res_full = evaluate_funding_entry_gate(ctx_full, policy, NOW)
    assert res_full.status in ("FAIL", "UNKNOWN")
    assert "FUNDING_90D_NA" not in res_full.reasons


def test_unknown_listing_is_unknown_not_first_seen():
    policy = _default_policy()
    ctx = make_funding_context("unknown")
    res = evaluate_funding_entry_gate(ctx, policy, NOW)
    assert res.status == "UNKNOWN"
    assert "HISTORY_CLASS_UNKNOWN" in res.reasons or "FUNDING_SCHEDULE_UNKNOWN" in res.reasons
    assert res.status != "PASS"


def test_fcs_high_never_overrides_fail():
    policy = _default_policy()
    # Perfect FCS inputs score 100, but the gate still FAILs on negativity.
    from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics
    metrics = FundingMetrics(
        symbol="T", funding_30d="0.05", funding_7d="0.01", funding_90d="0.10",
        positive_ratio_30d="0.95", positive_ratio_90d="0.95",
        coverage_30d="1.0", coverage_90d="1.0",
        conservative_apr="0.5", history_coverage="1.0",
    )
    venue = {
        "buy_executable_qty": "15000", "requested_canonical_qty": "10000",
        "sell_executable_qty": "15000", "roundtrip_cost_pct": 0.002,
        "exit_feasibility": "CONFIRMED",
    }
    identity = {
        "futures_status": "TRADING", "no_delisting": True,
        "identity_confidence": "VERIFIED", "multiplier_verified": True,
        "spot_quote_fresh": True, "next_funding_time_known": True,
    }
    stats = {"funding_std_30d": 0.0001, "longest_negative_streak_30d": 0}
    res = score_fcs(
        metrics, venue, 0.002, identity, policy, symbol="T", canonical_id="t",
        as_of_ms=NOW, snapshot_id="T:1", fcs_config_hash="h" * 64,
        funding_stats=stats, conservative={"history_class": "FULL_90D", "method": "M"},
    )
    assert res.fcs == 100
    gate_ctx = _matrix_context("neg", "neg")
    gate = evaluate_funding_entry_gate(gate_ctx, policy, NOW)
    assert gate.status == "FAIL"


# ---------------------------------------------------------------------------
# Schedule coverage
# ---------------------------------------------------------------------------

def test_schedule_head_tail_missing_counted():
    end = NOW
    start = end - 90 * DAY_MS
    seg = _segment(start=start, end=None, anchor=end, known_at=end - 1000)
    slots = _slots_8h_90d(end)
    assert len(slots) == 270
    events = [
        _funding_event(t, "0.0005", known_at=end - 500) for t in slots[1:-1]
    ]
    cov = compute_schedule_coverage(events, [seg], start, end, end)
    assert cov.expected_count == 270
    assert cov.received_count == 268
    assert cov.coverage_fraction == "0.99259259"
    assert cov.schedule_coverage_fraction == "1"
    assert tuple(sorted(cov.missing_slots)) == (slots[0], slots[-1])
    assert "COVERAGE_GAP" in cov.reasons


def test_schedule_complete_90d():
    end = NOW
    start = end - 90 * DAY_MS
    seg = _segment(start=start, end=None, anchor=end, known_at=end - 1000)
    slots = _slots_8h_90d(end)
    events = [_funding_event(t, "0.0005", known_at=end - 500) for t in slots]
    cov = compute_schedule_coverage(events, [seg], start, end, end)
    assert cov.expected_count == 270
    assert cov.received_count == 270
    assert cov.coverage_fraction == "1"
    assert cov.missing_slots == ()


def test_schedule_unknown_yields_null():
    end = NOW
    start = end - 30 * DAY_MS
    cov = compute_schedule_coverage([], [], start, end, end)
    assert cov.expected_count is None
    assert cov.received_count == 0
    assert cov.coverage_fraction is None
    assert cov.schedule_coverage_fraction == "0"
    assert "FUNDING_SCHEDULE_UNKNOWN" in cov.reasons
    # INFERRED segments never grant coverage either.
    seg = _segment(start=start, end=None, anchor=end, known_at=end - 1000,
                   verification="INFERRED")
    cov2 = compute_schedule_coverage([], [seg], start, end, end)
    assert cov2.coverage_fraction is None
    assert "FUNDING_SCHEDULE_UNKNOWN" in cov2.reasons


def test_schedule_legal_4h_not_assumed_8h():
    end = NOW
    start = end - 30 * DAY_MS
    seg = _segment(start=start, end=None, interval_hours=4, anchor=end,
                   known_at=end - 1000)
    # 30D at 4h cadence: 180 slots.
    slots = []
    t = end
    while t > start:
        slots.append(t)
        t -= H4
    slots = sorted(slots)
    assert len(slots) == 180
    events = [_funding_event(s, "0.0002", known_at=end - 500) for s in slots]
    cov = compute_schedule_coverage(events, [seg], start, end, end)
    assert cov.expected_count == 180
    assert cov.received_count == 180
    assert cov.coverage_fraction == "1"


def test_schedule_tolerance_60s():
    end = NOW
    start = end - 7 * DAY_MS
    seg = _segment(start=start, end=None, anchor=end, known_at=end - 1000)
    slots = []
    t = end
    while t > start:
        slots.append(t)
        t -= H8
    slots = sorted(slots)
    # One slot matched 30s off (accepted), one 90s off (missed).
    events = []
    for i, s in enumerate(slots):
        if i == 0:
            events.append(_funding_event(s + 30_000, "0.0005", known_at=end - 500))
        elif i == 1:
            events.append(_funding_event(s + 90_000, "0.0005", known_at=end - 500))
        else:
            events.append(_funding_event(s, "0.0005", known_at=end - 500))
    cov = compute_schedule_coverage(events, [seg], start, end, end)
    assert cov.expected_count == len(slots)
    assert cov.received_count == len(slots) - 1
    assert slots[1] in cov.missing_slots
    assert slots[0] not in cov.missing_slots


def test_schedule_dedup_picks_known_by_valid():
    end = NOW
    start = end - 7 * DAY_MS
    seg = _segment(start=start, end=None, anchor=end, known_at=end - 1000)
    slots = []
    t = end
    while t > start:
        slots.append(t)
        t -= H8
    slots = sorted(slots)
    target = slots[5]
    events = [_funding_event(s, "0.0005", known_at=end - 500) for s in slots if s != target]
    # Duplicate at target: stale receipt (valid) + future receipt (invisible).
    events.append(_funding_event(target, "0.0005", known_at=end - 500))
    events.append(_funding_event(target, "0.0099", known_at=end + 5000))
    cov = compute_schedule_coverage(events, [seg], start, end, end)
    assert cov.received_count == len(slots)
    assert target not in cov.missing_slots


def test_schedule_revision_picks_latest_known():
    end = NOW
    start = end - 30 * DAY_MS
    old = _segment("sched-a", start=start, end=None, interval_hours=8,
                   anchor=end, known_at=end - 2000)
    new = _segment("sched-a", start=start, end=None, interval_hours=4,
                   anchor=end, known_at=end - 1000)
    slots4 = []
    t = end
    while t > start:
        slots4.append(t)
        t -= H4
    slots4 = sorted(slots4)
    events = [_funding_event(s, "0.0002", known_at=end - 500) for s in slots4]
    cov = compute_schedule_coverage(events, [old, new], start, end, end)
    assert cov.expected_count == 180
    # At the older cutoff only the 8h revision is visible (90 slots).
    cov_old = compute_schedule_coverage(events, [old, new], start, end, end - 1500)
    assert cov_old.expected_count == 90


def test_schedule_overlap_is_unknown():
    end = NOW
    start = end - 30 * DAY_MS
    mid = start + 15 * DAY_MS
    a = _segment("sched-a", start=start, end=mid + H8, anchor=end, known_at=end - 1000)
    b = _segment("sched-b", start=mid, end=None, anchor=end, known_at=end - 1000)
    cov = compute_schedule_coverage([], [a, b], start, end, end)
    assert cov.coverage_fraction is None
    assert "FUNDING_SCHEDULE_UNKNOWN" in cov.reasons


def test_missing_mark_only_affects_usd_carry():
    end = NOW
    start = end - 30 * DAY_MS
    n = 90
    full = [
        {"symbol": "S", "funding_time_ms": end - (n - 1 - i) * H8,
         "rate": "0.0005", "mark_price": "100"}
        for i in range(n)
    ]
    imap = {int(e["funding_time_ms"]): 8.0 for e in full}
    m_full = compute_funding_metrics(full, imap, end, 120)
    half = [dict(e) for e in full]
    for e in half[::2]:
        e.pop("mark_price", None)
    m_half = compute_funding_metrics(half, imap, end, 120)
    assert m_full.funding_30d == m_half.funding_30d
    assert m_full.positive_ratio_30d == m_half.positive_ratio_30d
    assert compute_priced_event_coverage(full, start, end) == "1"
    priced_half = compute_priced_event_coverage(half, start, end)
    assert priced_half is not None and Decimal(priced_half) < Decimal("1")
    assert priced_half == "0.5"


def test_young_partial_builders():
    from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics
    metrics = FundingMetrics(
        symbol="YOUNG", current_rate="0.0005", last_settled_rate="0.0004",
        funding_7d="0.004", funding_30d="0.018", funding_90d=None,
        positive_ratio_30d="0.85", positive_ratio_90d=None,
        coverage_30d="0.95", coverage_90d="0.5",
        conservative_apr="0.2", history_coverage="0.95",
    )
    cov7 = FundingCoverage(NOW - 7 * DAY_MS, NOW, 21, 21, "1", "1", (), ())
    cov30 = FundingCoverage(NOW - 30 * DAY_MS, NOW, 90, 88, "0.97777778", "1", (), ())
    cov90 = FundingCoverage(NOW - 90 * DAY_MS, NOW, None, 0, None, "0",
                            (), ("FUNDING_SCHEDULE_UNKNOWN",))
    cur = _observed({"rate": "0.0005"}, source_as_of=NOW - 60_000, known_at=NOW - 50_000)
    last = _observed({"rate": "0.0004"}, source_as_of=NOW - H8, known_at=NOW - H8 + 5000)
    ctx = build_funding_context(
        metrics, cov7, cov30, cov90, history_class="PARTIAL_90D",
        listing_age_days=45, conservative_apr="0.2",
        conservative_method="MIN_APR30_P25_30D",
        current_observation=cur, last_settled_observation=last,
        schedule_refs=("sched-1",), input_refs={"funding": "fcs-young"},
    )
    assert ctx.history_class == "PARTIAL_90D"
    assert ctx.listing_age_days == 45
    gate = evaluate_funding_entry_gate(ctx, _default_policy(), NOW)
    # 90D unknowns are N/A for PARTIAL; the 30D-valid young coin passes.
    # (cov30 0.977 >= 0.9, pr30 0.85 >= 0.75, sums above minima.)
    assert gate.status == "PASS"
    assert "FUNDING_90D_NA" in gate.reasons


def test_unified_readiness_breakdown():
    ctx = _matrix_context("pos", "pos")
    gate = evaluate_funding_entry_gate(ctx, _default_policy(), NOW)
    assert gate.status == "PASS"
    rb = build_funding_readiness_breakdown(gate)
    assert rb.funding_gate.status == "PASS"
    assert rb.readiness == "NOT_READY"  # execution/economic UNKNOWN by default
    assert rb.data_complete is False
    ok_exec = GateResult("PASS", (), NOW, {})
    ok_econ = GateResult("PASS", (), NOW, {})
    rb2 = build_funding_readiness_breakdown(gate, execution_gate=ok_exec, economic_gate=ok_econ)
    assert rb2.readiness == "READY"
    assert rb2.data_complete is True
    # Funding FAIL never becomes READY even with other gates PASS.
    bad = evaluate_funding_entry_gate(_matrix_context("neg", "neg"), _default_policy(), NOW)
    rb3 = build_funding_readiness_breakdown(bad, execution_gate=ok_exec, economic_gate=ok_econ)
    assert rb3.readiness == "NOT_READY"
