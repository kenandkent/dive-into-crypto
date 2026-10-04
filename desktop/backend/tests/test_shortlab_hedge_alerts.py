"""H07 alerts: severity/action split, episode dedup, ACK, recovery (AC18).

Pure unit layer over :func:`evaluate_alerts` + :class:`AlertManager`; the
H01 repository transaction (dedup+episode in one txn) is covered by
test_shortlab_hedge_repository.py and wired by H08.
"""

from __future__ import annotations

import dataclasses
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

from diveintocrypto_desktop.shortlab.hedge.alerts import (  # noqa: E402
    AlertManager,
    evaluate_alerts,
)
from diveintocrypto_desktop.shortlab.hedge.monitor import compute_monitor  # noqa: E402

NOW = 1_760_000_000_000


def _snap_with(**metric_updates):
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms)
    snap = compute_monitor(plan, pos, cache, make_settled_events(),
                           None, clock.now_ms)
    metrics = dict(snap.metrics_json)
    metrics.update(metric_updates)
    basis = metric_updates.get("basis_override", None)
    if basis is not None:
        snap = dataclasses.replace(snap, basis_pnl_usd=basis)
    return dataclasses.replace(snap, metrics_json=metrics)


def _by_code(changes):
    return {c["code"]: c for c in changes}


# ---------------------------------------------------------------------------
# Severity / action separation.
# ---------------------------------------------------------------------------


def test_severity_and_action_are_independent():
    snap = _snap_with(target_hedge_ratio="1",
                      futures_entry_notional_usd="10000",
                      funding_complete=True,
                      funding_known_subtotal_usd="20.1",
                      adverse_basis_loss_usd="50",
                      basis_override="-50")
    snap = dataclasses.replace(snap, estimated_settled_funding_usd="20.1")
    changes = _by_code(evaluate_alerts(None, snap, None))
    consumed = changes["BASIS_CONSUMED_CARRY"]
    assert consumed["severity"] == "CRITICAL"
    assert consumed["recommended_action"] == "PAIR_EXIT"
    # EXIT_RECOMMENDED-style wording is an action, never a severity.
    for change in evaluate_alerts(None, snap, None):
        assert change["severity"] in ("INFO", "WARN", "CRITICAL")
        assert change["recommended_action"] in (
            "NONE", "REVIEW", "PAIR_EXIT", "URGENT_PAIR_EXIT")


def test_ratio_drift_warn_vs_critical_thresholds():
    snap = _snap_with(target_hedge_ratio="1")
    snap = dataclasses.replace(snap, actual_hedge_ratio="1.03")
    warn = _by_code(evaluate_alerts(None, snap, None))
    assert warn["HEDGE_RATIO_DRIFT"]["active"] is True
    assert warn["HEDGE_RATIO_DRIFT"]["severity"] == "WARN"
    assert warn["HEDGE_RATIO_CRITICAL"]["active"] is False

    snap_crit = dataclasses.replace(snap, actual_hedge_ratio="1.10")
    crit = _by_code(evaluate_alerts(None, snap_crit, None))
    assert crit["HEDGE_RATIO_CRITICAL"]["active"] is True
    assert crit["HEDGE_RATIO_CRITICAL"]["severity"] == "CRITICAL"
    assert crit["HEDGE_RATIO_CRITICAL"]["recommended_action"] == "PAIR_EXIT"


def test_orphan_legs_fire_independently():
    snap = _snap_with(orphan_hint="CRITICAL_ORPHAN_SPOT_LEG")
    changes = _by_code(evaluate_alerts(None, snap, None))
    assert changes["ORPHAN_SPOT_LEG"]["active"] is True
    assert changes["ORPHAN_SPOT_LEG"]["severity"] == "CRITICAL"
    assert changes["ORPHAN_SPOT_LEG"]["recommended_action"] == "URGENT_PAIR_EXIT"


# ---------------------------------------------------------------------------
# Episode dedup + ACK + failure + 2-tick recovery.
# ---------------------------------------------------------------------------


def _basis_firing_snapshot():
    return _snap_with(target_hedge_ratio="1",
                      futures_entry_notional_usd="10000",
                      funding_complete=True,
                      funding_known_subtotal_usd="20.1",
                      adverse_basis_loss_usd="15",
                      basis_override="-15",
                      actual_hedge_ratio="1.0")


def _basis_clear_snapshot():
    snap = _basis_firing_snapshot()
    metrics = dict(snap.metrics_json)
    metrics["adverse_basis_loss_usd"] = "1"
    snap = dataclasses.replace(snap, basis_pnl_usd="-1")
    return dataclasses.replace(snap, metrics_json=metrics)


def test_same_condition_does_not_open_a_new_episode():
    mgr = AlertManager()
    firing = evaluate_alerts(None, _basis_firing_snapshot(), None)
    first = mgr.apply(firing, NOW)
    opened = [d for d in first if d["dedup_key"] == "plan-0:BASIS_CONSUMING_CARRY:"]
    assert opened and opened[0]["notify"] is True
    assert opened[0]["episode"] == 1
    # Same condition 10s later: last_seen bump only, no new episode.
    second = mgr.apply(firing, NOW + 10_000)
    repeat = [d for d in second if d["dedup_key"] == "plan-0:BASIS_CONSUMING_CARRY:"]
    assert repeat[0]["episode"] == 1
    assert repeat[0]["notify"] is False
    assert repeat[0]["db_op"] == "upsert"
    assert len(mgr.all_alerts()) == len(
        [d for d in mgr.all_alerts() if d["state"] in ("OPEN", "ACKNOWLEDGED")])


def test_ack_does_not_renotify_on_refire():
    mgr = AlertManager()
    firing = evaluate_alerts(None, _basis_firing_snapshot(), None)
    mgr.apply(firing, NOW)
    mgr.ack("plan-0:BASIS_CONSUMING_CARRY:", NOW + 1_000)
    refire = mgr.apply(firing, NOW + 10_000)
    row = [d for d in refire if d["dedup_key"] == "plan-0:BASIS_CONSUMING_CARRY:"]
    assert row[0]["state"] == "ACKNOWLEDGED"
    assert row[0]["notify"] is False
    assert row[0]["episode"] == 1


def test_fetch_failure_never_resolves():
    mgr = AlertManager()
    firing = evaluate_alerts(None, _basis_firing_snapshot(), None)
    mgr.apply(firing, NOW)
    # Stale/failed evaluation of the same condition: fresh=False.
    failed = [dict(c, active=False, fresh=False) for c in firing
              if c["code"] == "BASIS_CONSUMING_CARRY"]
    out = mgr.apply(failed, NOW + 10_000)
    row = [d for d in out if d["dedup_key"] == "plan-0:BASIS_CONSUMING_CARRY:"]
    assert row[0]["state"] == "OPEN"
    assert row[0]["db_op"] == "noop"
    # A single fresh clear after a failure still needs a second tick.
    clear = evaluate_alerts(None, _basis_clear_snapshot(), None)
    mgr.apply(clear, NOW + 20_000)
    record = dict(mgr._alerts["plan-0:BASIS_CONSUMING_CARRY:"])
    assert record["state"] == "OPEN"
    mgr.apply(clear, NOW + 30_000)
    assert mgr._alerts["plan-0:BASIS_CONSUMING_CARRY:"]["state"] == "RESOLVED"


def test_recovery_needs_two_fresh_ticks():
    mgr = AlertManager()
    firing = evaluate_alerts(None, _basis_firing_snapshot(), None)
    mgr.apply(firing, NOW)
    clear = evaluate_alerts(None, _basis_clear_snapshot(), None)
    first = mgr.apply(clear, NOW + 10_000)
    row = [d for d in first if d["dedup_key"] == "plan-0:BASIS_CONSUMING_CARRY:"]
    assert row[0]["state"] == "OPEN"
    assert row[0]["notify"] is False
    second = mgr.apply(clear, NOW + 20_000)
    row2 = [d for d in second if d["dedup_key"] == "plan-0:BASIS_CONSUMING_CARRY:"]
    assert row2[0]["state"] == "RESOLVED"
    assert row2[0]["notify"] is True
    # Re-fire after RESOLVED opens episode 2.
    refire = mgr.apply(firing, NOW + 30_000)
    row3 = [d for d in refire if d["dedup_key"] == "plan-0:BASIS_CONSUMING_CARRY:"]
    assert row3[0]["episode"] == 2
    assert row3[0]["state"] == "OPEN"


def test_mark_over_user_price_only_unconfirmed():
    clock = InjectedClock(NOW)
    plan = make_plan(liquidation_price="90000")
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms, mark="95000")
    snap = compute_monitor(plan, pos, cache, make_settled_events(),
                           None, clock.now_ms)
    changes = _by_code(evaluate_alerts(None, snap, None))
    assert changes["LIQUIDATION_POSSIBLE_UNCONFIRMED"]["active"] is True
    assert "LIQUIDATION" not in changes
    mgr = AlertManager()
    dispositions = mgr.apply(evaluate_alerts(None, snap, None), clock.now_ms)
    unconfirmed = [d for d in dispositions
                   if d["code"] == "LIQUIDATION_POSSIBLE_UNCONFIRMED"]
    assert unconfirmed and unconfirmed[0]["notify"] is True


def test_db_lag_degraded_keeps_positions_and_criticals():
    clock = InjectedClock(NOW)
    plan = make_plan()
    pos = make_positions()
    cache = make_market_cache(now_ms=clock.now_ms, persist_lag_ms=6_000)
    snap = compute_monitor(plan, pos, cache, make_settled_events(),
                           None, clock.now_ms)
    assert snap.status == "MONITOR_DEGRADED"
    assert "PERSISTENCE_LAG" in snap.metrics_json["degraded_reasons"]
    # Real marks/positions are untouched by the lag flag.
    assert snap.mark_price == "67000"
    changes = _by_code(evaluate_alerts(None, snap, None))
    assert changes["PERSISTENCE_LAG"]["active"] is True
    mgr = AlertManager()
    # A critical open before the lag survives the degraded window.
    crit_snap = _basis_firing_snapshot()
    mgr.apply(evaluate_alerts(None, crit_snap, None), clock.now_ms)
    mgr.apply(evaluate_alerts(None, snap, None), clock.now_ms + 10_000)
    open_codes = {r["code"] for r in mgr.open_alerts()}
    assert "BASIS_CONSUMING_CARRY" in open_codes
