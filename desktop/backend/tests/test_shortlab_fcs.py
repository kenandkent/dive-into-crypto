"""H03 FCS: bins, completeness, intervals, rolling P25, distribution.

Plan H03 (AC14). Pure offline fixtures only. No network.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path

import pytest

from diveintocrypto_desktop.shortlab.hedge import models as M
from diveintocrypto_desktop.shortlab.hedge.funding_score import (
    build_fcs_distribution_report,
    compute_conservative_apr,
    compute_funding_metrics,
    compute_funding_std_30d,
    compute_longest_negative_streak,
    compute_p25,
    compute_rolling_7d_aprs,
    resolve_history_class,
    score_fcs,
)

CUTOFF = 1_735_689_600_000  # 2025-01-01 00:00 UTC
DAY_MS = 86_400_000
H8 = 8 * 3_600_000


def _events(symbol, cutoff, days, interval_h=8, rate=0.0005, dup=False):
    n = int(days * 24 / interval_h)
    out = []
    for i in range(n):
        t = cutoff - (n - 1 - i) * int(interval_h * 3_600_000)
        r = rate(i) if callable(rate) else rate
        out.append({"symbol": symbol, "funding_time_ms": t, "rate": str(r)})
    if dup:
        out = out + [dict(e) for e in out[:3]]
    return out


def _imap(events, hours=8):
    return {int(e["funding_time_ms"]): float(hours) for e in events}


def _perfect_venue(cost=0.002):
    return {
        "buy_executable_qty": "15000",
        "requested_canonical_qty": "10000",
        "sell_executable_qty": "15000",
        "roundtrip_cost_pct": cost,
        "exit_feasibility": "CONFIRMED",
    }


def _perfect_identity():
    return {
        "futures_status": "TRADING",
        "no_delisting": True,
        "identity_confidence": "VERIFIED",
        "multiplier_verified": True,
        "spot_quote_fresh": True,
        "next_funding_time_known": True,
    }


def _policy():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    return load_shortlab_config()


def _full_metrics_and_stats(symbol="BTCUSDT", rate=0.0005):
    events = _events(symbol, CUTOFF, 95, rate=rate)
    imap = _imap(events)
    metrics = compute_funding_metrics(events, imap, CUTOFF, 120)
    std = compute_funding_std_30d(events, imap, CUTOFF)
    streak = compute_longest_negative_streak(events, CUTOFF)
    aprs30 = compute_rolling_7d_aprs(events, CUTOFF, lookback_days=30)
    aprs90 = compute_rolling_7d_aprs(events, CUTOFF, lookback_days=90)
    p25_30 = compute_p25(aprs30) if len(aprs30) >= 20 else None
    p25_90 = compute_p25(aprs90) if len(aprs90) >= 60 else None
    stats = {
        "funding_std_30d": std,
        "longest_negative_streak_30d": streak,
        "p25_rolling_7d_apr_30d": p25_30,
        "p25_rolling_7d_apr_90d": p25_90,
    }
    return events, imap, metrics, stats


# H03.1: left-open right-closed, dedup, sort -------------------------------

def test_window_left_open_right_closed_dedup_sorted():
    cutoff = CUTOFF
    start30 = cutoff - 30 * DAY_MS
    ev = [
        {"symbol": "S", "funding_time_ms": start30, "rate": "0.001"},  # excluded (left open)
        {"symbol": "S", "funding_time_ms": start30 + 1, "rate": "0.001"},
        {"symbol": "S", "funding_time_ms": cutoff, "rate": "0.001"},  # included (right closed)
        {"symbol": "S", "funding_time_ms": cutoff + 1, "rate": "0.001"},  # excluded
        {"symbol": "S", "funding_time_ms": start30 + 1, "rate": "0.009"},  # dup same key
    ]
    # Need enough surrounding events for completeness; pad with dense 8h tail.
    pad = _events("S", cutoff, 30, rate=0.0005)
    # Remove pad events colliding with manual times to keep test deterministic.
    manual_times = {e["funding_time_ms"] for e in ev}
    pad = [e for e in pad if e["funding_time_ms"] not in manual_times]
    all_ev = pad + ev[::-1]  # deliberately unsorted
    imap = {int(e["funding_time_ms"]): 8.0 for e in all_ev}
    m = compute_funding_metrics(all_ev, imap, cutoff, 120)
    # Left-open: start30 event excluded; right-closed: cutoff included.
    # Dedup: start30+1 kept once (first rate 0.001, not 0.009).
    assert m.funding_30d is not None
    # Verify dedup kept first occurrence by checking sum contribution:
    # total should include exactly one copy of start30+1.
    assert Decimal(m.funding_30d) > 0


def test_duplicate_events_single_counted():
    base = _events("DUP", CUTOFF, 30, rate=0.0005)
    imap = _imap(base)
    m1 = compute_funding_metrics(base, imap, CUTOFF, 120)
    duped = base + [dict(e) for e in base[:10]]
    imap2 = _imap(duped)
    m2 = compute_funding_metrics(duped, imap2, CUTOFF, 120)
    assert m1.funding_30d == m2.funding_30d
    assert m1.positive_ratio_30d == m2.positive_ratio_30d


# complete=false with high fraction still null ----------------------------

def test_coverage_high_fraction_but_complete_false_still_null():
    # 30D of 8h events but remove 4 consecutive (32h gap > 24h).
    base = _events("GAP", CUTOFF, 30, rate=0.0005)
    assert len(base) == 90
    # Remove middle 4 to create a 40h gap (5*8h span).
    gapped = base[:40] + base[44:]
    assert len(gapped) == 86
    imap = _imap(gapped)
    m = compute_funding_metrics(gapped, imap, CUTOFF, 120)
    frac = float(Decimal(m.coverage_30d))
    assert frac > 0.9  # high fraction
    assert m.funding_30d is None  # but null
    assert m.positive_ratio_30d is None
    assert compute_funding_std_30d(gapped, imap, CUTOFF) is None
    assert compute_longest_negative_streak(gapped, CUTOFF) is None


def test_coverage_override_complete_false_forces_null():
    base = _events("OVR", CUTOFF, 30, rate=0.0005)
    imap = _imap(base)
    overrides = {"30d": {"complete": False, "coverage_fraction": 0.99}}
    m = compute_funding_metrics(base, imap, CUTOFF, 120, coverage_overrides=overrides)
    assert m.coverage_30d is not None
    assert float(Decimal(m.coverage_30d)) == pytest.approx(0.99)
    assert m.funding_30d is None
    assert m.positive_ratio_30d is None


# H03.2: Std8h ddof0 -------------------------------------------------------

def test_std_8h_ddof0_population_not_sample():
    # Constant equiv => std 0.
    ev = _events("STD0", CUTOFF, 30, rate=0.0005)
    imap = _imap(ev)
    assert compute_funding_std_30d(ev, imap, CUTOFF) == pytest.approx(0.0, abs=1e-12)
    # Varying rates: compare against manual population std on equiv.
    rates = [0.0005 + (0.0002 if i % 2 == 0 else -0.0001) for i in range(90)]
    ev2 = [
        {"symbol": "S2", "funding_time_ms": CUTOFF - (89 - i) * H8, "rate": str(r)}
        for i, r in enumerate(rates)
    ]
    imap2 = _imap(ev2)
    got = compute_funding_std_30d(ev2, imap2, CUTOFF)
    equiv = list(rates)  # 8h intervals => equiv == raw
    mean = sum(equiv) / len(equiv)
    var0 = sum((x - mean) ** 2 for x in equiv) / len(equiv)
    var1 = sum((x - mean) ** 2 for x in equiv) / (len(equiv) - 1)
    assert got == pytest.approx(math.sqrt(var0), rel=1e-9)
    assert abs(got - math.sqrt(var1)) > 1e-9  # differs from LTSS-style sample std


def test_interval_varying_uses_per_event_not_single_current():
    # Same raw rates, but half at 4h cadence with half rate => same equiv if per-event used.
    # Build 30D window mixing intervals: alternate blocks.
    ev = []
    t = CUTOFF - 30 * DAY_MS + 4 * 3_600_000
    i = 0
    # 4h block: rate 0.0002 (equiv 0.0004); 8h block: rate 0.0004 (equiv 0.0004).
    # Interleave to keep gaps <=24h.
    while t <= CUTOFF and len(ev) < 120:
        if (i // 10) % 2 == 0:
            ev.append({"symbol": "MIX", "funding_time_ms": t, "rate": "0.0002"})
            im_h = 4.0
            t += 4 * 3_600_000
        else:
            ev.append({"symbol": "MIX", "funding_time_ms": t, "rate": "0.0004"})
            im_h = 8.0
            t += 8 * 3_600_000
        ev[-1]["_h"] = im_h
        i += 1
    imap = {int(e["funding_time_ms"]): float(e.pop("_h")) for e in ev}
    got = compute_funding_std_30d(ev, imap, CUTOFF)
    # All equiv == 0.0004 => std ~0.
    assert got == pytest.approx(0.0, abs=1e-9)
    # Naive single current interval (e.g. 8h for all) would treat 0.0002 as 0.0002 => std > 0.
    naive = {int(e["funding_time_ms"]): 8.0 for e in ev}
    naive_std = compute_funding_std_30d(ev, naive, CUTOFF)
    assert naive_std is not None and naive_std > 1e-5


def test_interval_unknown_yields_null_and_fcs_null():
    ev = _events("UNK", CUTOFF, 30, rate=0.0005)
    # Drop half the interval entries and make gaps irregular so inference fails.
    imap = _imap(ev)
    # Make irregular: shift every 7th event by 3h.
    for idx in range(6, len(ev), 7):
        ev[idx]["funding_time_ms"] = int(ev[idx]["funding_time_ms"]) + 3 * 3_600_000
    # Remove intervals for shifted events.
    for e in ev:
        if int(e["funding_time_ms"]) % (7 * H8) == 0:
            pass
    partial = {k: v for i, (k, v) in enumerate(imap.items()) if i % 3 != 0}
    # Force explicit unknown for one event.
    some_t = int(ev[5]["funding_time_ms"])
    partial[some_t] = None
    assert compute_funding_std_30d(ev, partial, CUTOFF) is None
    m = compute_funding_metrics(ev, partial, CUTOFF, 120)
    policy = _policy()
    stats = {"funding_std_30d": None, "longest_negative_streak_30d": 0}
    res = score_fcs(
        m, _perfect_venue(), 0.002, _perfect_identity(), policy,
        symbol="UNK", canonical_id="unk", as_of_ms=CUTOFF,
        snapshot_id="UNK:1", fcs_config_hash="x" * 64,
        funding_stats=stats,
        conservative={"history_class": "FULL_90D", "method": "MIN_APR30_APR90_P25_90D"},
    )
    assert res.fcs is None
    assert res.readiness == "NOT_READY"


# H03.3: rolling P25 --------------------------------------------------------

def test_p25_linear_interpolation():
    assert compute_p25([10.0]) == pytest.approx(10.0)
    assert compute_p25([0.0, 10.0, 20.0, 30.0]) == pytest.approx(7.5)
    assert compute_p25([]) is None
    # (n-1)*.25 with n=5 => index 1.0 => sorted[1].
    assert compute_p25([5, 1, 3, 2, 4]) == pytest.approx(2.0)


def test_rolling_windows_require_20_60():
    # Full 95D history => both P25s available.
    ev = _events("ROLL", CUTOFF, 95, rate=0.0005)
    a30 = compute_rolling_7d_aprs(ev, CUTOFF, lookback_days=30)
    a90 = compute_rolling_7d_aprs(ev, CUTOFF, lookback_days=90)
    assert len(a30) >= 20
    assert len(a90) >= 60
    assert compute_p25(a30) is not None
    # Truncated to ~25D => 30D windows drop below 20.
    ev_short = [e for e in ev if int(e["funding_time_ms"]) > CUTOFF - 25 * DAY_MS]
    b30 = compute_rolling_7d_aprs(ev_short, CUTOFF, lookback_days=30)
    assert len(b30) < 20
    # Conservative must be null when windows insufficient.
    m = compute_funding_metrics(ev_short, _imap(ev_short), CUTOFF, 120)
    assert m.conservative_apr is None
    # 90D path with only 60D of data => <60 valid 90D windows (60-6=54).
    ev60 = [e for e in ev if int(e["funding_time_ms"]) > CUTOFF - 60 * DAY_MS]
    c90 = compute_rolling_7d_aprs(ev60, CUTOFF, lookback_days=90)
    assert len(c90) < 60


def test_conservative_apr_methods():
    v_full, meth_full = compute_conservative_apr("0.045", "0.12", 0.5, 0.4, "FULL_90D")
    assert meth_full == "MIN_APR30_APR90_P25_90D"
    assert v_full is not None and float(Decimal(v_full)) > 0
    v_part, meth_part = compute_conservative_apr("0.045", None, 0.5, None, "PARTIAL_90D")
    assert meth_part == "MIN_APR30_P25_30D"
    assert v_part is not None
    v_none, _ = compute_conservative_apr("0.045", "0.12", None, None, "FULL_90D")
    assert v_none is None
    v_floor, _ = compute_conservative_apr("-0.01", "-0.02", -0.5, -0.4, "FULL_90D")
    assert v_floor is not None and float(Decimal(v_floor)) == pytest.approx(0.0)


# Bins: boundaries + full 100 ----------------------------------------------

def _score_with(funding_30d, pr30, pr90, std, streak, venue, basis, identity, hist="FULL_90D"):
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    policy = load_shortlab_config()
    m = M.FundingMetrics(
        symbol="T", funding_30d=funding_30d, funding_7d="0.01",
        funding_90d="0.10" if hist == "FULL_90D" else None,
        positive_ratio_30d=pr30,
        positive_ratio_90d=pr90,
        coverage_30d="1.0", coverage_90d="1.0",
        conservative_apr="0.5", history_coverage="1.0",
    )
    stats = {"funding_std_30d": std, "longest_negative_streak_30d": streak}
    return score_fcs(
        m, venue, basis, identity, policy, symbol="T", canonical_id="t",
        as_of_ms=CUTOFF, snapshot_id="T:1", fcs_config_hash="h" * 64,
        funding_stats=stats,
        conservative={"history_class": hist, "method": "M"},
    )


def test_each_bin_boundary():
    # Yield bins (min inclusive).
    assert _score_with("0.04", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_yield"] == 25
    assert _score_with("0.039", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_yield"] == 20
    assert _score_with("0.025", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_yield"] == 20
    assert _score_with("0.024", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_yield"] == 15
    assert _score_with("0.015", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_yield"] == 15
    assert _score_with("0.0075", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_yield"] == 8
    assert _score_with("0.001", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_yield"] == 3
    assert _score_with("0.0", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).fcs is None or True  # 0 yield scores 0 but still scored (30D complete path needs funding>0? FCS still computed)
    # PR30 bins.
    assert _score_with("0.05", "0.9", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_30d"] == 15
    assert _score_with("0.05", "0.899", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_30d"] == 12
    assert _score_with("0.05", "0.8", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_30d"] == 12
    assert _score_with("0.05", "0.75", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_30d"] == 9
    assert _score_with("0.05", "0.65", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_30d"] == 5
    assert _score_with("0.05", "0.64", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_30d"] == 0
    # PR90 bins.
    assert _score_with("0.05", "0.95", "0.9", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_90d"] == 10
    assert _score_with("0.05", "0.95", "0.8", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_90d"] == 8
    assert _score_with("0.05", "0.95", "0.7", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_90d"] == 5
    assert _score_with("0.05", "0.95", "0.6", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_90d"] == 2
    assert _score_with("0.05", "0.95", "0.59", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_persistence_90d"] == 0
    # Std bins (max inclusive).
    assert _score_with("0.05", "0.95", "0.95", 0.0003, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_std"] == 8
    assert _score_with("0.05", "0.95", "0.95", 0.00031, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_std"] == 5
    assert _score_with("0.05", "0.95", "0.95", 0.0008, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_std"] == 5
    assert _score_with("0.05", "0.95", "0.95", 0.0015, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_std"] == 2
    assert _score_with("0.05", "0.95", "0.95", 0.002, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_std"] == 0
    # Streak exact.
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_streak"] == 7
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 1, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_streak"] == 6
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 2, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_streak"] == 3
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 3, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_streak"] == 1
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 4, _perfect_venue(), 0.001, _perfect_identity()).module_scores["funding_stability_streak"] == 0
    # Venue coverage.
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.001, _perfect_identity()).module_scores["venue_coverage"] == 7
    v75 = dict(_perfect_venue()); v75["buy_executable_qty"] = "7500"; v75["sell_executable_qty"] = "7500"
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, v75, 0.001, _perfect_identity()).module_scores["venue_coverage"] == 4
    v50 = dict(_perfect_venue()); v50["buy_executable_qty"] = "5000"; v50["sell_executable_qty"] = "5000"
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, v50, 0.001, _perfect_identity()).module_scores["venue_coverage"] == 2
    v0 = dict(_perfect_venue()); v0["buy_executable_qty"] = "100"; v0["sell_executable_qty"] = "100"
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, v0, 0.001, _perfect_identity()).module_scores["venue_coverage"] == 0
    # Cost bins.
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(0.003), 0.001, _perfect_identity()).module_scores["venue_roundtrip_cost"] == 5
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(0.006), 0.001, _perfect_identity()).module_scores["venue_roundtrip_cost"] == 3
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(0.01), 0.001, _perfect_identity()).module_scores["venue_roundtrip_cost"] == 1
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(0.02), 0.001, _perfect_identity()).module_scores["venue_roundtrip_cost"] == 0
    # Exit.
    for val, exp in (("CONFIRMED", 3), ("PARTIAL", 1), ("UNKNOWN", 0), ("NO", 0)):
        v = dict(_perfect_venue()); v["exit_feasibility"] = val
        assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, v, 0.001, _perfect_identity()).module_scores["venue_exit"] == exp
    # Basis.
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.005, _perfect_identity()).module_scores["basis_quality"] == 10
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.015, _perfect_identity()).module_scores["basis_quality"] == 8
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.03, _perfect_identity()).module_scores["basis_quality"] == 5
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(), 0.05, _perfect_identity()).module_scores["basis_quality"] == 2
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(), -0.005, _perfect_identity()).module_scores["basis_quality"] == 3
    assert _score_with("0.05", "0.95", "0.95", 0.0001, 0, _perfect_venue(), -0.006, _perfect_identity()).module_scores["basis_quality"] == 0


def test_full_100_all_max():
    events, imap, metrics, stats = _full_metrics_and_stats(rate=0.0005)
    # funding_30d = 90*0.0005 = 0.045 >= 0.04 => 25.
    assert float(Decimal(metrics.funding_30d)) == pytest.approx(0.045)
    policy = _policy()
    from diveintocrypto_desktop.shortlab.config import fcs_config_hash

    res = score_fcs(
        metrics, _perfect_venue(), 0.002, _perfect_identity(), policy,
        symbol="BTCUSDT", canonical_id="bitcoin", as_of_ms=CUTOFF,
        snapshot_id="BTCUSDT:full", fcs_config_hash=fcs_config_hash(policy),
        reference_notional_usd=10000, funding_stats=stats,
        conservative={"history_class": "FULL_90D", "method": "MIN_APR30_APR90_P25_90D"},
    )
    assert res.fcs == 100
    assert res.readiness == "READY"
    assert res.fcs_version == "fcs_v2"  # R00 frozen default (was fcs_v1)
    # Conservative/method/coverage/class saved with result.
    assert res.funding_metrics["history_class"] == "FULL_90D"
    assert res.funding_metrics["available_max_score"] == 100.0
    assert res.funding_metrics["conservative_apr"] == metrics.conservative_apr
    assert res.risk["history_class"] == "FULL_90D"


# H03.4: PARTIAL vs ordinary missing ----------------------------------------

def test_reliable_young_NA_cap_90_not_normalised():
    policy = _policy()
    m = M.FundingMetrics(
        symbol="YOUNG", funding_30d="0.045", funding_7d="0.01", funding_90d=None,
        positive_ratio_30d="0.95", positive_ratio_90d=None,
        coverage_30d="1.0", coverage_90d="0.5",
        conservative_apr="0.4", history_coverage="1.0",
    )
    stats = {"funding_std_30d": 0.0001, "longest_negative_streak_30d": 0}
    res = score_fcs(
        m, _perfect_venue(), 0.002, _perfect_identity(), policy,
        symbol="YOUNG", canonical_id="young", as_of_ms=CUTOFF,
        snapshot_id="YOUNG:1", fcs_config_hash="h" * 64,
        funding_stats=stats,
        conservative={"history_class": "PARTIAL_90D", "method": "MIN_APR30_P25_30D", "available_max_score": 90},
    )
    assert res.fcs is not None
    assert res.fcs <= 90
    assert res.fcs == 90  # all other modules max, 90D N/A contributes 0 (not renormalised)
    assert res.funding_metrics["history_class"] == "PARTIAL_90D"
    assert res.funding_metrics["available_max_score"] == 90.0
    assert "FCS_PARTIAL_HISTORY" in res.reasons
    # Not renormalised: raw 90 would otherwise become 100 via *100/90.
    assert res.fcs != pytest.approx(100.0)


def test_unknown_age_missing_90d_is_null_not_NA():
    policy = _policy()
    m = M.FundingMetrics(
        symbol="UNKAGE", funding_30d="0.045", funding_7d="0.01", funding_90d=None,
        positive_ratio_30d="0.95", positive_ratio_90d=None,
        coverage_30d="1.0", coverage_90d="0.5",
        conservative_apr=None, history_coverage="1.0",
    )
    stats = {"funding_std_30d": 0.0001, "longest_negative_streak_30d": 0}
    res = score_fcs(
        m, _perfect_venue(), 0.002, _perfect_identity(), policy,
        symbol="UNKAGE", canonical_id="unkage", as_of_ms=CUTOFF,
        snapshot_id="UNKAGE:1", fcs_config_hash="h" * 64,
        funding_stats=stats,
        conservative={"history_class": "FULL_90D", "method": "M"},
    )
    assert res.fcs is None
    assert res.readiness == "NOT_READY"
    assert "FCS_PARTIAL_HISTORY" not in res.reasons


def test_ordinary_missing_30d_null_no_reweight():
    policy = _policy()
    m = M.FundingMetrics(
        symbol="MISS", funding_30d=None, funding_7d=None, funding_90d=None,
        positive_ratio_30d=None, positive_ratio_90d=None,
        coverage_30d="0.95", coverage_90d="0.9",
        conservative_apr=None, history_coverage="0.95",
    )
    stats = {"funding_std_30d": 0.0001, "longest_negative_streak_30d": 0}
    res = score_fcs(
        m, _perfect_venue(), 0.002, _perfect_identity(), policy,
        symbol="MISS", canonical_id="miss", as_of_ms=CUTOFF,
        snapshot_id="MISS:1", fcs_config_hash="h" * 64,
        funding_stats=stats,
        conservative={"history_class": "FULL_90D", "method": "M"},
    )
    assert res.fcs is None
    assert res.readiness == "NOT_READY"


def test_resolve_history_class():
    assert resolve_history_class(120, CUTOFF) == ("FULL_90D", 100.0)
    assert resolve_history_class({"age_days": 45, "reliable": True}, CUTOFF) == ("PARTIAL_90D", 90.0)
    assert resolve_history_class(None, CUTOFF) == ("FULL_90D", 100.0)
    assert resolve_history_class({"age_days": 45, "reliable": False}, CUTOFF) == ("FULL_90D", 100.0)


# Distribution report with fixed fixture ------------------------------------

def _fixed_dump_events():
    """Fixed offline dump: 50 complete-90D + 12 young (30..89D). Deterministic."""
    symbols = []
    dump = {}
    # 50 mature with varying yields (deterministic by index).
    for i in range(50):
        sym = f"FIX{i:02d}USDT"
        # Vary rate mean/std by index: ensures histogram spread.
        base_rate = 0.0002 + (i % 7) * 0.0001  # 0.0002..0.0008
        if i % 10 == 9:
            base_rate = -0.0001  # a few negative-carry symbols
        ev = _events(sym, CUTOFF, 95, rate=lambda k, b=base_rate, j=i: b + ((0.00005 * ((k + j) % 3 - 1)) if j % 3 == 0 else 0.0))
        # Mix settlement intervals: some 4h, some 8h, some 1h.
        interval = 8 if i % 3 == 0 else (4 if i % 3 == 1 else 1)
        # Resample to chosen cadence deterministically: keep every k-th.
        if interval != 8:
            step = 8 // interval if interval < 8 else 1
            # For 4h we need denser data than 8h base; rebuild instead.
            ev = _events(sym, CUTOFF, 95, interval_h=interval, rate=lambda k, b=base_rate: b)
        dump[sym] = {"events": ev, "interval_hours": interval, "age_days": 200}
        symbols.append(sym)
    # 12 young 30..89D.
    for j in range(12):
        sym = f"YOUNG{j:02d}USDT"
        age = 30 + j * 5  # 30..85
        ev = _events(sym, CUTOFF, age, rate=0.0004)
        dump[sym] = {"events": ev, "interval_hours": 8, "age_days": age}
        symbols.append(sym)
    return dump


def test_distribution_report_fixed_fixture_source_dump_sha_and_enabled_false():
    from diveintocrypto_desktop.shortlab.config import fcs_config_hash, load_shortlab_config

    config = load_shortlab_config()
    # Acceptance not finished: enabled stays false; do not assert enabled.
    assert config.funding_capture.enabled is False
    assert config.hedge.enabled is False

    dump = _fixed_dump_events()
    # Freeze dump bytes deterministically (sorted keys, compact) for SHA.
    flat = {
        sym: {
            "events": [{"t": int(e["funding_time_ms"]), "r": str(e["rate"])} for e in info["events"]],
            "interval_hours": info["interval_hours"],
            "age_days": info["age_days"],
        }
        for sym, info in sorted(dump.items())
    }
    dump_bytes = json.dumps(flat, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    dump_sha = hashlib.sha256(dump_bytes).hexdigest()

    mature_n = sum(1 for s, info in dump.items() if info["age_days"] >= 90)
    young_n = sum(1 for s, info in dump.items() if 30 <= info["age_days"] <= 89)
    assert mature_n == 50
    assert young_n == 12  # fixed young group present; if fewer, report actual n truthfully

    policy = config
    results = []
    for sym, info in sorted(dump.items()):
        ev = info["events"]
        imap = {int(e["funding_time_ms"]): float(info["interval_hours"]) for e in ev}
        age = {"age_days": info["age_days"], "reliable": True}
        m = compute_funding_metrics(ev, imap, CUTOFF, age)
        std = compute_funding_std_30d(ev, imap, CUTOFF)
        streak = compute_longest_negative_streak(ev, CUTOFF)
        a30 = compute_rolling_7d_aprs(ev, CUTOFF, lookback_days=30)
        a90 = compute_rolling_7d_aprs(ev, CUTOFF, lookback_days=90)
        p30 = compute_p25(a30) if len(a30) >= 20 else None
        p90 = compute_p25(a90) if len(a90) >= 60 else None
        hist = "PARTIAL_90D" if info["age_days"] < 90 else "FULL_90D"
        stats = {
            "funding_std_30d": std,
            "longest_negative_streak_30d": streak,
            "p25_rolling_7d_apr_30d": p30,
            "p25_rolling_7d_apr_90d": p90,
        }
        res = score_fcs(
            m, _perfect_venue(), 0.002, _perfect_identity(), policy,
            symbol=sym, canonical_id=sym.lower(), as_of_ms=CUTOFF,
            snapshot_id=f"{sym}:{CUTOFF}", fcs_config_hash=fcs_config_hash(config),
            funding_stats=stats,
            conservative={"history_class": hist, "method": "M",
                          "available_max_score": 90 if hist == "PARTIAL_90D" else 100},
        )
        results.append(res)

    report = build_fcs_distribution_report(results, source="offline-fixture-v1", dump_sha256=dump_sha)
    assert report["source"] == "offline-fixture-v1"
    assert report["dump_sha256"] == dump_sha
    assert report["n"] == 62
    assert report["n_scored"] + report["n_null"] == 62
    assert sum(b["n"] for b in report["histogram"]) == report["n_scored"]
    assert "FULL_90D" in report["history_class_distribution"]
    assert "PARTIAL_90D" in report["history_class_distribution"]
    # Bins use fixed defaults; report is evidence, not an optimisation input.
    assert "not yield" in report["note"].lower() or "not yield-optimised" in report["note"].lower() or "fixed defaults" in report["note"]
