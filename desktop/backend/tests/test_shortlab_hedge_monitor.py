"""H07 monitor: cached PnL, basis, funding carry and freshness (AC16/AC17).

Uses only the frozen H01 DTOs plus the H07 harness builders; no network,
no DB, no real waiting. Every row of the H07.1/H07.2 scene table is
asserted here (load cadence lives in test_shortlab_hedge_load.py).
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from diveintocrypto_desktop.shortlab.hedge.alerts import evaluate_alerts
from diveintocrypto_desktop.shortlab.hedge.monitor import (
    HIGH_FREQUENCY_STATUSES,
    basis_readiness_for_new_plan,
    compute_monitor,
    is_high_frequency_status,
    risk_key,
    should_persist,
)

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "helpers"))
from hedge_load_harness import (  # noqa: E402
    InjectedClock,
    make_market_cache,
    make_plan,
    make_positions,
    make_settled_events,
)

NOW = 1_760_000_000_000


def _with_metrics(snapshot, **updates):
    metrics = dict(snapshot.metrics_json)
    metrics.update(updates)
    return dataclasses.replace(snapshot, metrics_json=metrics)


# ---------------------------------------------------------------------------
# Funding: then-qty * native mark * rate with then-FX; unknown stays null.
# ---------------------------------------------------------------------------


def test_partial_qty_uses_then_qty_not_current_qty():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions(fut_qty="1.0", spot_qty="1.0")
    cache = make_market_cache(now_ms=clock.now_ms)
    # Two settlements: first with 2.0 short (before a partial close), second
    # with the current 1.0 short. The monitor must honour each event's
    # then-qty instead of current_qty * full history (1.0 * both).
    events = (
        {"funding_time_ms": NOW - 16 * 3_600_000, "rate": "0.0001",
         "mark_price": "67000", "quote_asset": "USDT", "fx_to_usd": "1",
         "short_qty": "2.0"},
        {"funding_time_ms": NOW - 8 * 3_600_000, "rate": "0.0001",
         "mark_price": "67000", "quote_asset": "USDT", "fx_to_usd": "1",
         "short_qty": "1.0"},
    )
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    # 2*67000*0.0001 + 1*67000*0.0001 = 13.4 + 6.7 = 20.1
    assert snap.estimated_settled_funding_usd == "20.1"
    # Naive current-qty * history would be 6.7 + 6.7 = 13.4.
    assert snap.estimated_settled_funding_usd != "13.4"


def test_negative_funding_is_a_deduction():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms)
    events = make_settled_events(rate="-0.0002")
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    assert Decimal(snap.estimated_settled_funding_usd or "0") < 0
    # 3 * 1 * 67000 * -0.0002 = -40.2
    assert snap.estimated_settled_funding_usd == "-40.2"


def test_missing_mark_or_fx_or_boundary_forces_null_with_known_subtotal():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms)
    events = (
        {"funding_time_ms": NOW - 16 * 3_600_000, "rate": "0.0001",
         "mark_price": "67000", "quote_asset": "USDT", "fx_to_usd": "1",
         "short_qty": "1.0"},
        # Missing native mark: unknown bucket.
        {"funding_time_ms": NOW - 8 * 3_600_000, "rate": "0.0001",
         "mark_price": None, "quote_asset": "USDT", "fx_to_usd": "1",
         "short_qty": "1.0"},
        # Settlement-boundary fill: uncertain even with all fields.
        {"funding_time_ms": NOW, "rate": "0.0001",
         "mark_price": "67000", "quote_asset": "USDT", "fx_to_usd": "1",
         "short_qty": "1.0", "boundary_uncertain": True},
    )
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    assert snap.estimated_settled_funding_usd is None
    assert snap.metrics_json["funding_known_subtotal_usd"] == "6.7"
    assert snap.metrics_json["funding_coverage"] == "1/3"
    assert snap.metrics_json["carry_comparison"] == "CARRY_COMPARISON_UNAVAILABLE"
    # The known subtotal is honest; no 0 is filled into the total.
    assert snap.metrics_json["funding_complete"] is False


def test_missing_settlement_fx_nulls_usd_total():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms)
    events = (
        {"funding_time_ms": NOW - 8 * 3_600_000, "rate": "0.0001",
         "mark_price": "67000", "quote_asset": "USDT", "fx_to_usd": None,
         "short_qty": "1.0"},
    )
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    assert snap.estimated_settled_funding_usd is None
    assert snap.metrics_json["funding_known_subtotal_usd"] == "0"


def test_actual_receipts_never_added_and_projected_never_accrued():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms, projected_next="0.5")
    cache["actual_funding_receipts_usd"] = {"USDT": "5"}
    events = make_settled_events()
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    assert snap.estimated_settled_funding_usd == "20.1"
    assert snap.projected_next_funding_usd == "0.5"
    # Actual user receipts stay a separate calibre, never added.
    assert snap.metrics_json["actual_funding_receipts_usd"] == {"USDT": "5"}
    assert snap.metrics_json["actual_funding_receipts_separate"] is True
    assert snap.metrics_json["projected_excluded_from_accrued"] is True
    assert Decimal(snap.estimated_settled_funding_usd or "0") != Decimal("25.1")


# ---------------------------------------------------------------------------
# Price PnL: matched basis + unmatched directional == two-leg total.
# ---------------------------------------------------------------------------


def test_basis_already_inside_two_legs_never_added_twice():
    clock = InjectedClock(NOW)
    plan = make_plan()
    # Futures 2.0 short, spot 1.0 long; entries equal, marks diverge.
    pos = make_positions(fut_qty="2.0", spot_qty="1.0",
                         fut_entry="67000", spot_entry="67000")
    cache = make_market_cache(now_ms=clock.now_ms, mark="68000", spot="67500")
    events = make_settled_events()
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    assert snap.futures_pnl_usd == "-2000"
    assert snap.spot_pnl_usd == "500"
    assert snap.basis_pnl_usd == "-500"
    total = Decimal(snap.futures_pnl_usd or "0") + Decimal(snap.spot_pnl_usd or "0")
    # Matched 1.0 basis (-500) + unmatched futures 1.0 * (67000-68000) = -1500.
    assert total == Decimal("-1500")
    assert total == Decimal(snap.basis_pnl_usd or "0") + Decimal("-1000")
    assert snap.metrics_json["basis_not_double_added"] is True


def test_partial_close_without_exit_snapshot_nulls_leg_pnl():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions(fut_qty="1.0", spot_qty="1.0",
                         fut_closed="0.4", spot_closed="0.4")
    cache = make_market_cache(now_ms=clock.now_ms)
    events = make_settled_events()
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    # Realized exit prices are unknown: the complete leg stays null, never 0.
    assert snap.spot_pnl_usd is None
    assert snap.futures_pnl_usd is None
    assert snap.net_pnl_before_exit_usd is None
    assert snap.metrics_json["realized_spot_unknown"] is True


def test_fx_residual_independent_and_missing_fx_nulls_net():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms, mark="68000", spot="67500")
    cache["futures_mark"]["quote_currency"] = "USDT"
    cache["spot_quote"]["quote_currency"] = "USDC"
    cache["spot_quote"]["quote_to_usd"] = "0.999"
    events = make_settled_events()
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    # Cross-currency: no single-spread basis; residual is independent.
    assert snap.basis_pnl_usd is None
    assert snap.metrics_json["fx_pnl_adjustment_usd"] is not None
    assert snap.metrics_json["fx_residual"] == "CROSS_CURRENCY_SEPARATE"

    # Current-FX missing nulls the complete USD net (USDT != 1 by default).
    cache2 = make_market_cache(now_ms=clock.now_ms)
    cache2["futures_mark"]["quote_to_usd"] = None
    cache2["spot_quote"]["quote_to_usd"] = None
    snap2 = compute_monitor(plan, pos, cache2, events, None, clock.now_ms)
    assert snap2.spot_pnl_usd is None
    assert snap2.net_pnl_before_exit_usd is None
    assert snap2.metrics_json["fx_pnl_adjustment_usd"] is None


def test_known_cost_and_exit_cost_counted_once():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms, known_cost_usd="5",
                              exit_cost_usd="3")
    events = make_settled_events()  # 20.1 carry
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    # Price PnL is 0/0; net = 0 + 0 + 20.1 - 5 = 15.1; after = 15.1 - 3.
    assert snap.net_pnl_before_exit_usd == "15.1"
    assert snap.estimated_net_pnl_after_exit_usd == "12.1"


# ---------------------------------------------------------------------------
# Basis skew: 5s fresh, 5..30s reference-only, >30s null; READY gate.
# ---------------------------------------------------------------------------


def test_basis_skew_reference_but_new_ready_must_not_pass():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms, skew_sec=10)
    events = make_settled_events()
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    # Reference basis is kept (positions stay visible) but flagged.
    assert snap.current_basis_pct is not None
    assert snap.metrics_json["basis_status"] == "BASIS_ASYNC_STALE"
    assert snap.metrics_json["time_alignment_credit"] == 0
    assert snap.status == "MONITOR_DEGRADED"
    assert snap.metrics_json["new_plan_ready_on_basis"] is False
    ready, reason, skew = basis_readiness_for_new_plan(
        NOW - 10_000, NOW, policy=None)
    assert ready is False
    assert reason == "BASIS_ASYNC_STALE"
    assert skew == 10

    ready_fresh, _, skew_fresh = basis_readiness_for_new_plan(
        NOW, NOW, policy=None)
    assert ready_fresh is True
    assert skew_fresh == 0

    cache_late = make_market_cache(now_ms=clock.now_ms, skew_sec=40)
    snap_late = compute_monitor(plan, pos, cache_late, events, None, clock.now_ms)
    assert snap_late.current_basis_pct is None
    assert snap_late.metrics_json["basis_status"] == "BASIS_STALE_NULL"


def test_zero_accrued_uses_fixed_basis_thresholds():
    # Zero accrued carry still uses the fixed USD/notional gates (B23):
    # WARN needs adverse >= max(10, 0.1% notional); EXIT >= max(20, 0.2%).
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms)
    snap = compute_monitor(plan, pos, cache, (), None, clock.now_ms)
    snap = _with_metrics(
        snap, target_hedge_ratio="1", futures_entry_notional_usd="10000",
        funding_complete=True)
    snap = dataclasses.replace(
        snap, basis_pnl_usd="-15", estimated_settled_funding_usd="0",
        current_basis_pct="-0.001")
    snap = _with_metrics(
        snap, adverse_basis_loss_usd="15", funding_known_subtotal_usd="0")
    changes = {c["code"]: c for c in evaluate_alerts(None, snap, None)}
    assert changes["BASIS_CONSUMING_CARRY"]["active"] is True
    assert changes["BASIS_CONSUMED_CARRY"]["active"] is False
    # A dust-size adverse loss never fires on fixed gates.
    snap_small = dataclasses.replace(snap, basis_pnl_usd="-1")
    snap_small = _with_metrics(snap_small, adverse_basis_loss_usd="1")
    small = {c["code"]: c for c in evaluate_alerts(None, snap_small, None)}
    assert small["BASIS_CONSUMING_CARRY"]["active"] is False
    assert small["BASIS_CONSUMED_CARRY"]["active"] is False


# ---------------------------------------------------------------------------
# Liquidation: mark over user price is unconfirmed only.
# ---------------------------------------------------------------------------


def test_mark_over_user_liq_is_unconfirmed_only():
    clock = InjectedClock(NOW)
    plan = make_plan(liquidation_price="90000")
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms, mark="95000")
    events = make_settled_events()
    snap = compute_monitor(plan, pos, cache, events, None, clock.now_ms)
    assert snap.metrics_json["liq_state"] == "LIQUIDATION_POSSIBLE_UNCONFIRMED"
    # Positions are never zeroed by the monitor.
    assert snap.mark_price == "95000"
    changes = {c["code"]: c for c in evaluate_alerts(None, snap, None)}
    assert changes["LIQUIDATION_POSSIBLE_UNCONFIRMED"]["active"] is True
    assert changes["LIQUIDATION_POSSIBLE_UNCONFIRMED"]["severity"] == "CRITICAL"
    # No auto-LIQUIDATION code is ever emitted by the monitor path.
    assert "LIQUIDATION" not in [c for c in changes if c == "LIQUIDATION"]


def test_liq_distance_bands_and_stale_flag():
    clock = InjectedClock(NOW)
    plan = make_plan(liquidation_price="80000",
                     liquidation_price_updated_at_ms=NOW - 25 * 3_600_000)
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms, mark="70000")
    snap = compute_monitor(plan, pos, cache, (), None, clock.now_ms)
    # (80000-70000)/70000 = 14.28% -> WARNING band.
    assert snap.metrics_json["liq_state"] == "WARNING"
    assert snap.metrics_json["liq_price_stale"] is True


# ---------------------------------------------------------------------------
# Persist cadence + eligibility (H08 executes; H07 decides latest-only).
# ---------------------------------------------------------------------------


def test_should_persist_60s_or_risk_change():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms)
    first = compute_monitor(plan, pos, cache, (), None, clock.now_ms)
    assert should_persist(None, first, None, None) is True
    # Same risk 10s later: no persist.
    clock.advance(10)
    cache["futures_mark"]["as_of_ms"] = clock.now_ms
    cache["spot_quote"]["as_of_ms"] = clock.now_ms
    second = compute_monitor(plan, pos, cache, (), None, clock.now_ms)
    assert should_persist(first, second, NOW, None, now_ms=clock.now_ms) is False
    # 60s sampler fires even with identical risk.
    clock.advance(50)
    cache["futures_mark"]["as_of_ms"] = clock.now_ms
    cache["spot_quote"]["as_of_ms"] = clock.now_ms
    third = compute_monitor(plan, pos, cache, (), None, clock.now_ms)
    assert should_persist(first, third, NOW, None, now_ms=clock.now_ms) is True
    # Risk change (liq band flip) persists out-of-cycle.
    cache_risk = make_market_cache(now_ms=clock.now_ms, mark="79000")
    risky = compute_monitor(plan, pos, cache_risk, (), None, clock.now_ms)
    assert risk_key(first) != risk_key(risky)
    assert should_persist(first, risky, clock.now_ms - 10_000, None,
                          now_ms=clock.now_ms) is True


def test_only_partial_active_closing_get_high_frequency_ticks():
    assert is_high_frequency_status("PARTIALLY_FILLED") is True
    assert is_high_frequency_status("ACTIVE") is True
    assert is_high_frequency_status("CLOSING") is True
    for status in ("DRAFT", "READY", "CLOSED", "INVALID"):
        assert is_high_frequency_status(status) is False
    assert HIGH_FREQUENCY_STATUSES == frozenset(
        {"PARTIALLY_FILLED", "ACTIVE", "CLOSING"})
