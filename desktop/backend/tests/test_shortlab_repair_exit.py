"""R08b Monitor/Exit/Alerts wiring (D10/D12, V06/V11).

Red->green: before R08b ``hedge.exit_guidance`` did not exist
(ModuleNotFoundError). Pure offline only: fixed clocks, no network/DB/clock
reads except explicit repository persistence for EVENT_FX / ACTIVATION_CHECK.
Amounts are exact Decimal strings (no tolerance masking).

Covers (plan R08b):
- build_pair_exit_guidance fields/sides/step/dust/orphan/POSSIBLE (D10/D18.1);
- Dust single-column, never zeroed;
- orphan SPOT URGENT_PAIR_EXIT, Mark breach POSSIBLE unconfirmed;
- FUNDED_PENDING_ACTIVATION high-frequency monitoring;
- LedgerPnl actual costs into Monitor, unknown not zero, no double-count;
- event currency/FX persistence (EVENT_FX);
- protection confirmation state machine + expiry degrade;
- ACK never resolves risk;
- ACTIVATION_CHECK observation persistence.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab.hedge.ledger import (
    aggregate_events,
    build_event_fx_records,
    evaluate_position_state,
)
from diveintocrypto_desktop.shortlab.hedge.exit_guidance import (
    build_pair_exit_guidance,
)
from diveintocrypto_desktop.shortlab.hedge.monitor import (
    build_activation_check_record,
    compute_monitor,
    is_high_frequency_status,
)
from diveintocrypto_desktop.shortlab.hedge.alerts import (
    AlertManager,
    evaluate_alerts,
)
from diveintocrypto_desktop.shortlab.hedge.pnl import compute_ledger_pnl
from diveintocrypto_desktop.shortlab.hedge.protection import (
    compute_protected_position_hash,
    validate_protection_confirmation,
)
from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
from tests.repair_fixtures import (
    FIXTURE_NOW,
    make_event_fx,
    make_events,
    make_identity,
    make_market_context,
)


def _plan(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "plan_id": "plan-MEME_FULL_VALID",
        "plan_version": 2,
        "symbol": "1000PEPEUSDT",
        "mode": "ABSOLUTE",
        "status": "FUNDED_PENDING_ACTIVATION",
        "target_hedge_ratio": "1",
        "liquidation_price": "0.02",
        "liquidation_price_source": "USER_EXCHANGE",
        "liquidation_price_updated_at_ms": FIXTURE_NOW - 1_000,
    }
    base.update(over)
    return base


def _rules(step: str = "1", min_qty: str = "1") -> dict[str, Any]:
    return {
        "FUTURES_SHORT": {
            "lot_rules": {"step_size": step, "min_qty": min_qty},
            "venue": "BINANCE_SPOT",
            "price_rules": {"tick_size": "0.000001"},
        },
        "SPOT_LONG": {
            "lot_rules": {"step_size": step, "min_qty": min_qty},
            "venue": "BINANCE_SPOT",
            "price_rules": {"tick_size": "0.000001"},
        },
    }


def _remaining_by_leg(positions: Any) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for p in positions:
        if isinstance(p, dict):
            leg = str(p.get("leg_type"))
            rem = Decimal(str(p.get("remaining_qty")))
        else:
            leg = str(getattr(p, "leg_type"))
            rem = Decimal(str(getattr(p, "remaining_qty")))
        out[leg] = rem
    return out


def _monitor_cache(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "futures_mark": {
            "price": "100",
            "quote_currency": "USDT",
            "quote_to_usd": "1",
            "as_of_ms": FIXTURE_NOW,
            "fetched_at_ms": FIXTURE_NOW,
            "expires_at_ms": FIXTURE_NOW + 60_000,
        },
        "spot_quote": {
            "sell_vwap": "110",
            "mid_price": "110",
            "quote_currency": "USDT",
            "quote_to_usd": "1",
            "as_of_ms": FIXTURE_NOW,
            "fetched_at_ms": FIXTURE_NOW,
            "expires_at_ms": FIXTURE_NOW + 60_000,
            "sell_executable_qty": "100",
            "sell_slippage_bps": 5.0,
        },
        "known_cost_usd": "2",
        "estimated_exit_cost_usd": "1",
        "projected_next_funding_usd": "0.5",
        "persist_lag_ms": 0,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Exit guidance fields / sides / step.
# ---------------------------------------------------------------------------


def test_exit_guidance_fields_sides_and_step_floor() -> None:
    positions = aggregate_events(make_events(), make_identity(), plan_id="plan-MEME_FULL_VALID")
    remaining = _remaining_by_leg(positions)
    assert remaining["FUTURES_SHORT"] == Decimal("6")
    assert remaining["SPOT_LONG"] == Decimal("6")
    market = make_market_context()
    guidance = build_pair_exit_guidance(
        _plan(), positions, market, _rules(step="1"), FIXTURE_NOW
    )
    assert guidance.plan_id == "plan-MEME_FULL_VALID"
    assert guidance.plan_version == 2
    assert guidance.generated_at_ms == FIXTURE_NOW
    assert guidance.expires_at_ms > guidance.generated_at_ms
    assert guidance.confirmation_required is True
    assert len(guidance.legs) == 2
    by_leg = {str(leg["leg_type"]): dict(leg) for leg in guidance.legs}
    fut = by_leg["FUTURES_SHORT"]
    spot = by_leg["SPOT_LONG"]
    # D10: futures BUY reduceOnly, spot SELL.
    assert fut["side"] == "BUY"
    assert fut["reduce_only"] is True
    assert spot["side"] == "SELL"
    assert spot["reduce_only"] is False
    # Native qty never exceeds true remaining, floored to Step.
    for leg_type, leg in by_leg.items():
        assert Decimal(str(leg["native_qty"])) <= remaining[leg_type]
        assert Decimal(str(leg["native_qty"])) > 0
    # Required per-leg fields (D10).
    for leg in guidance.legs:
        for key in (
            "leg_type",
            "venue",
            "side",
            "native_qty",
            "qty_currency",
            "price_currency",
            "vwap_native",
            "reduce_only",
            "coverage",
            "capability",
            "source_refs",
            "reasons",
        ):
            assert key in leg, f"leg misses {key}"
        assert leg["capability"] in ("CONFIRMED", "PARTIAL", "UNKNOWN", "NO")
    # Missing quote never hides remaining: capability UNKNOWN, qty still visible.
    market_noquote = dict(market)
    market_noquote["futures_buy_vwap_native"] = None
    market_noquote["spot_sell_vwap_native"] = None
    g2 = build_pair_exit_guidance(
        _plan(), positions, market_noquote, _rules(step="1"), FIXTURE_NOW
    )
    assert len(g2.legs) == 2
    assert all(leg["capability"] == "UNKNOWN" for leg in g2.legs)


def test_exit_guidance_dust_single_column_never_zeroed() -> None:
    now = FIXTURE_NOW
    events = (
        {"event_id": "o-fut", "leg_type": "FUTURES_SHORT",
         "event_type": "OPEN_FUTURES_SHORT", "native_qty": "6",
         "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED",
         "executed_at_ms": now - 1000},
        {"event_id": "o-spot", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "native_qty": "0.05",
         "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED",
         "executed_at_ms": now - 1000},
    )
    positions = aggregate_events(events, make_identity(), plan_id="p-dust")
    remaining = _remaining_by_leg(positions)
    assert remaining["SPOT_LONG"] == Decimal("0.05")
    guidance = build_pair_exit_guidance(
        _plan(plan_id="p-dust"), positions, make_market_context(),
        _rules(step="1", min_qty="1"), FIXTURE_NOW,
    )
    # Dust is listed separately, never claimed as closed/zero.
    assert "SPOT_LONG" in dict(guidance.unexecutable_dust)
    assert Decimal(str(guidance.unexecutable_dust["SPOT_LONG"])) == Decimal("0.05")
    assert all(str(leg["leg_type"]) != "SPOT_LONG" for leg in guidance.legs)
    # Futures leg still executable.
    assert any(str(leg["leg_type"]) == "FUTURES_SHORT" for leg in guidance.legs)
    assert "DUST_BELOW_STEP" in list(guidance.reasons)


def test_exit_guidance_orphan_spot_urgent() -> None:
    now = FIXTURE_NOW
    events = (
        {"event_id": "o-fut", "leg_type": "FUTURES_SHORT",
         "event_type": "OPEN_FUTURES_SHORT", "native_qty": "6",
         "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED",
         "executed_at_ms": now - 2000},
        {"event_id": "o-spot", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "native_qty": "6",
         "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED",
         "executed_at_ms": now - 2000},
        # Manual liquidation closes only the true futures quantity.
        {"event_id": "liq-fut", "leg_type": "FUTURES_SHORT",
         "event_type": "LIQUIDATION", "native_qty": "6",
         "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED",
         "executed_at_ms": now - 1000},
    )
    positions = aggregate_events(events, make_identity(), plan_id="p-orphan")
    remaining = _remaining_by_leg(positions)
    assert remaining["FUTURES_SHORT"] == Decimal("0")
    assert remaining["SPOT_LONG"] == Decimal("6")
    guidance = build_pair_exit_guidance(
        _plan(plan_id="p-orphan"), positions, make_market_context(),
        _rules(step="1"), FIXTURE_NOW,
    )
    assert len(guidance.legs) == 1
    assert str(guidance.legs[0]["leg_type"]) == "SPOT_LONG"
    assert str(guidance.legs[0]["side"]) == "SELL"
    assert "URGENT_PAIR_EXIT" in list(guidance.reasons)
    assert "CRITICAL_ORPHAN_SPOT_LEG" in list(guidance.reasons)


def test_exit_guidance_mark_breach_possible_unconfirmed_only() -> None:
    positions = aggregate_events(make_events(), make_identity(), plan_id="plan-MEME_FULL_VALID")
    before = {str(leg["leg_type"]): str(leg["native_qty"]) for leg in
              build_pair_exit_guidance(
                  _plan(), positions, make_market_context(),
                  _rules(step="1"), FIXTURE_NOW).legs}
    # Mark touches the user liquidation threshold: POSSIBLE only, no auto qty change.
    market = make_market_context(futures_mark_native="0.02")
    plan = _plan(liquidation_price="0.02")
    guidance = build_pair_exit_guidance(
        plan, positions, market, _rules(step="1"), FIXTURE_NOW
    )
    assert "POSSIBLE_LIQUIDATION_UNCONFIRMED" in list(guidance.reasons)
    after = {str(leg["leg_type"]): str(leg["native_qty"]) for leg in guidance.legs}
    assert after == before
    # No auto-LIQUIDATION leg rewrite.
    assert all(str(leg.get("event_type", "")) != "LIQUIDATION" for leg in guidance.legs)


# ---------------------------------------------------------------------------
# Ledger: FUNDED_PENDING_ACTIVATION (no auto ACTIVE on the R08b path).
# ---------------------------------------------------------------------------


def test_ledger_funded_pending_not_auto_active() -> None:
    now = FIXTURE_NOW
    open_only = (
        {"event_id": "o-fut", "leg_type": "FUTURES_SHORT",
         "event_type": "OPEN_FUTURES_SHORT", "native_qty": "6",
         "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED",
         "executed_at_ms": now - 1000},
        {"event_id": "o-spot", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "native_qty": "6",
         "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED",
         "executed_at_ms": now - 1000},
    )
    positions = aggregate_events(open_only, make_identity(), plan_id="p")
    # Legacy default preserves ACTIVE for pre-repair callers (regression green).
    legacy = evaluate_position_state(
        positions, "1", mode="ABSOLUTE", plan_id="p",
        plan_version=2, updated_at_ms=FIXTURE_NOW,
    )
    assert legacy.status == "ACTIVE"
    # R08b path: both legs filled but not explicitly activated -> FUNDED.
    funded = evaluate_position_state(
        positions, "1", mode="ABSOLUTE", plan_id="p",
        plan_version=2, updated_at_ms=FIXTURE_NOW,
        activated=False,
    )
    assert funded.status == "FUNDED_PENDING_ACTIVATION"
    explicit = evaluate_position_state(
        positions, "1", mode="ABSOLUTE", plan_id="p",
        plan_version=2, updated_at_ms=FIXTURE_NOW,
        activated=True,
    )
    assert explicit.status == "ACTIVE"


def test_monitor_high_frequency_includes_funded_pending() -> None:
    assert is_high_frequency_status("PARTIALLY_FILLED") is True
    assert is_high_frequency_status("ACTIVE") is True
    assert is_high_frequency_status("CLOSING") is True
    assert is_high_frequency_status("FUNDED_PENDING_ACTIVATION") is True
    assert is_high_frequency_status("DRAFT") is False
    assert is_high_frequency_status("CLOSED") is False


# ---------------------------------------------------------------------------
# Monitor: actual LedgerPnl costs in, unknown not zero, no double-count.
# ---------------------------------------------------------------------------


def test_monitor_consumes_ledger_pnl_costs_and_funding_basis() -> None:
    pnl = compute_ledger_pnl(
        make_events(), make_identity(), make_event_fx(), make_market_context()
    )
    assert pnl.realized_futures_usd == "40"
    assert pnl.realized_spot_usd == "40"
    positions = aggregate_events(make_events(), make_identity(), plan_id="plan-MEME_FULL_VALID")
    plan = {"plan_id": "plan-MEME_FULL_VALID", "status": "FUNDED_PENDING_ACTIVATION",
            "target_hedge_ratio": "1", "liquidation_price": "90000",
            "liquidation_price_source": "USER_EXCHANGE",
            "liquidation_price_updated_at_ms": FIXTURE_NOW}
    snap = compute_monitor(
        plan, positions, _monitor_cache(), (), None, FIXTURE_NOW,
        ledger_pnl=pnl,
    )
    # Actual costs flow into the snapshot (not hand-filled cache values).
    assert snap.known_cost_usd == pnl.known_cost_usd
    assert snap.estimated_exit_cost_usd == pnl.estimated_exit_cost_usd
    assert snap.metrics_json["ledger_funding_basis"] == pnl.funding_basis
    assert snap.metrics_json["ledger_actual_funding_usd"] == pnl.actual_funding_usd
    assert snap.metrics_json["funding_double_count_prevented"] is True
    # Unknown stays null, never zero-filled.
    events = list(make_events())
    broken = dict(events[0])
    broken["fee_amount"] = None
    broken["fee_usd"] = None
    broken["gas_usd"] = None
    events[0] = broken
    pnl_unknown = compute_ledger_pnl(
        events, make_identity(), make_event_fx(), make_market_context()
    )
    assert pnl_unknown.net_before_exit_usd is None
    snap2 = compute_monitor(
        plan, positions, _monitor_cache(), (), None, FIXTURE_NOW,
        ledger_pnl=pnl_unknown,
    )
    assert snap2.net_pnl_before_exit_usd is None
    assert "UNKNOWN_FEE" in list(snap2.metrics_json.get("ledger_unknown_components", []))


def test_monitor_estimated_and_actual_funding_never_double_counted() -> None:
    now = FIXTURE_NOW
    events = tuple(make_events()) + (
        {"event_id": "f1", "leg_type": "FUNDING", "event_type": "FUNDING_RECEIPT",
         "amount": "5", "currency": "USDT", "public_funding_event_id": "pub-1",
         "source": "USER_ENTERED", "executed_at_ms": now},
    )
    fx = dict(make_event_fx())
    fx["f1"] = {"price_fx_id": "x", "price_fx": "1", "funding_fx_id": "y",
                "funding_fx": "1"}
    pnl = compute_ledger_pnl(events, make_identity(), fx, make_market_context())
    assert pnl.actual_funding_usd == "5"
    assert pnl.funding_basis == "ACTUAL_RECEIPTS_ONLY"
    positions = aggregate_events(make_events(), make_identity(), plan_id="p")
    plan = {"plan_id": "p", "status": "ACTIVE", "target_hedge_ratio": "1"}
    snap = compute_monitor(
        plan, positions, _monitor_cache(), (), None, FIXTURE_NOW,
        ledger_pnl=pnl,
    )
    assert snap.metrics_json["ledger_actual_funding_usd"] == "5"
    assert snap.metrics_json["ledger_estimated_unconfirmed_funding_usd"] is None


# ---------------------------------------------------------------------------
# Protection state machine + expiry degrade + ACK never resolves.
# ---------------------------------------------------------------------------


def _protection_record_funded() -> tuple[dict[str, Any], dict[str, Any], Any]:
    positions = aggregate_events(make_events(), make_identity(), plan_id="plan-MEME_FULL_VALID")
    by_leg = {str(p.leg_type): str(p.remaining_qty) for p in positions}
    liq = "0.02"
    stop_px = "0.019"
    rule_ids = ("rule-v1",)
    h = compute_protected_position_hash(
        futures_remaining=by_leg["FUTURES_SHORT"],
        spot_remaining=by_leg["SPOT_LONG"],
        liquidation_price=liq,
        stop_trigger_price=stop_px,
        stop_trigger_basis="MARK_PRICE",
        rule_ids=rule_ids,
    )
    plan = _plan(liquidation_price=liq, stop_trigger_price=stop_px,
                 stop_trigger_basis="MARK_PRICE", rule_ids=list(rule_ids))
    conf = {
        "confirmation_id": "conf-1",
        "client_request_id": "cli-1",
        "confirmed_at_ms": FIXTURE_NOW - 1_000,
        "expires_at_ms": FIXTURE_NOW + 86_400_000,
        "protected_position_hash": h,
        "source": "USER_CONFIRMED",
        "futures": {"status": "CONFIRMED", "nativeQty": by_leg["FUTURES_SHORT"],
                    "triggerPrice": stop_px, "triggerBasis": "MARK_PRICE",
                    "orderReference": "fut-stop-1"},
        "spot": {"status": "CONFIRMED", "nativeQty": by_leg["SPOT_LONG"],
                 "exitMode": "PLATFORM_ORDER", "orderReference": "spot-1"},
    }
    gate = validate_protection_confirmation(conf, plan, positions, FIXTURE_NOW)
    assert gate.status == "PASS"
    return plan, positions, gate


def test_monitor_protection_expiry_degrades_and_alerts() -> None:
    plan, positions, gate = _protection_record_funded()
    snap_ok = compute_monitor(
        plan, positions, _monitor_cache(), (), None, FIXTURE_NOW,
        protection=gate,
    )
    assert snap_ok.metrics_json["protection_status"] == "PASS"
    # Expired confirmation fails closed and degrades the snapshot.
    expired_gate = validate_protection_confirmation(
        {"confirmation_id": "conf-1", "client_request_id": "cli-1",
         "confirmed_at_ms": FIXTURE_NOW - 100_000,
         "expires_at_ms": FIXTURE_NOW - 1_000,
         "protected_position_hash": "x", "source": "USER_CONFIRMED",
         "futures": {"status": "CONFIRMED", "nativeQty": "6"},
         "spot": {"status": "CONFIRMED", "nativeQty": "6",
                  "exitMode": "PLATFORM_ORDER"}},
        plan, positions, FIXTURE_NOW,
    )
    assert expired_gate.status == "FAIL"
    snap_bad = compute_monitor(
        plan, positions, _monitor_cache(), (), None, FIXTURE_NOW,
        protection=expired_gate,
    )
    assert snap_bad.status == "MONITOR_DEGRADED"
    assert "PROTECTION_INVALID" in list(snap_bad.metrics_json["degraded_reasons"])
    changes = {c["code"]: c for c in evaluate_alerts(None, snap_bad, None)}
    assert changes["PROTECTION_EXPIRED"]["active"] is True
    assert changes["PROTECTION_EXPIRED"]["fresh"] is True


def test_ack_never_resolves_risk() -> None:
    plan, positions, gate = _protection_record_funded()
    expired_gate = validate_protection_confirmation(
        {"confirmation_id": "conf-1", "client_request_id": "cli-1",
         "confirmed_at_ms": FIXTURE_NOW - 100_000,
         "expires_at_ms": FIXTURE_NOW - 1_000,
         "protected_position_hash": "x", "source": "USER_CONFIRMED",
         "futures": {"status": "CONFIRMED", "nativeQty": "6"},
         "spot": {"status": "CONFIRMED", "nativeQty": "6",
                  "exitMode": "PLATFORM_ORDER"}},
        plan, positions, FIXTURE_NOW,
    )
    snap_bad = compute_monitor(
        plan, positions, _monitor_cache(), (), None, FIXTURE_NOW,
        protection=expired_gate,
    )
    mgr = AlertManager()
    firing = evaluate_alerts(None, snap_bad, None)
    mgr.apply(firing, FIXTURE_NOW)
    key = "plan-MEME_FULL_VALID:PROTECTION_EXPIRED:"
    assert mgr._alerts[key]["state"] == "OPEN"
    mgr.ack(key, FIXTURE_NOW + 1_000)
    assert mgr._alerts[key]["state"] == "ACKNOWLEDGED"
    # Re-fire while ACKNOWLEDGED never re-notifies and never resolves.
    refire = mgr.apply(firing, FIXTURE_NOW + 10_000)
    row = [d for d in refire if d["dedup_key"] == key][0]
    assert row["state"] == "ACKNOWLEDGED"
    assert row["notify"] is False
    assert mgr._alerts[key]["state"] == "ACKNOWLEDGED"
    assert key in {r["dedup_key"] for r in mgr.open_alerts()}


# ---------------------------------------------------------------------------
# Persistence: EVENT_FX + ACTIVATION_CHECK.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repo(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "r08b.duckdb")
    await handle.migrate(target_version=6)
    yield handle
    await handle.close()


@pytest.mark.asyncio
async def test_event_fx_records_persist(repo) -> None:
    events = make_events()
    event_fx = make_event_fx()
    records = build_event_fx_records(
        events, event_fx, "1000PEPEUSDT", FIXTURE_NOW
    )
    assert len(records) == 4
    for rec in records:
        assert rec["kind"] == "EVENT_FX"
        assert rec["value_json"]["event_id"] in {
            "e-open-fut", "e-close-fut", "e-open-spot", "e-close-spot"}
        assert "price_fx" in rec["value_json"]
        oid = await repo.save_market_observation(rec)
        assert oid == rec["observation_id"]
    got = await repo.get_market_observation(records[0]["observation_id"])
    assert got is not None
    assert got["value_json"]["event_id"] == records[0]["value_json"]["event_id"]


@pytest.mark.asyncio
async def test_activation_check_saved(repo) -> None:
    positions = aggregate_events(make_events(), make_identity(), plan_id="plan-MEME_FULL_VALID")
    _, _, gate = _protection_record_funded()
    record = build_activation_check_record(
        plan_id="plan-MEME_FULL_VALID",
        symbol="1000PEPEUSDT",
        cutoff_ms=FIXTURE_NOW,
        known_at_ms=FIXTURE_NOW,
        checks={
            "identity_ok": True,
            "mark_vs_liquidation_ok": True,
            "depth_ok": True,
            "funding_gate": "PASS",
            "economics_ok": True,
            "protection_status": gate.status,
            "protected_position_hash": "hash-stub",
        },
        source_refs={"protection": "conf-1", "market": "m-1"},
    )
    assert record["kind"] == "ACTIVATION_CHECK"
    assert record["symbol"] == "1000PEPEUSDT"
    assert record["value_json"]["plan_id"] == "plan-MEME_FULL_VALID"
    assert "protected_position_hash" in record["value_json"]["checks"]
    oid = await repo.save_market_observation(record)
    assert oid == record["observation_id"]
    got = await repo.get_market_observation(oid)
    assert got is not None
    assert got["value_json"]["checks"]["funding_gate"] == "PASS"
    listed = await repo.list_market_observations(
        "1000PEPEUSDT", "ACTIVATION_CHECK",
        FIXTURE_NOW - 60_000, FIXTURE_NOW + 60_000, FIXTURE_NOW + 1_000,
    )
    assert any(r["observation_id"] == oid for r in listed)
