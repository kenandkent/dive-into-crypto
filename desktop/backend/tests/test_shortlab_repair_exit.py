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


# ---------------------------------------------------------------------------
# CR06 (D09/D12): real DTO-compatible exit — dataclass rules, per-holding
# venue Futures BUY / Spot SELL, true rounding + Dust + coverage/refs/earliest.
# ---------------------------------------------------------------------------


def _cr06_trs(venue: str, step: str) -> Any:
    from diveintocrypto_desktop.shortlab.hedge.models import TradingRulesSnapshot

    return TradingRulesSnapshot(
        venue=venue,
        instrument_id="1000PEPEUSDT",
        source_as_of_ms=FIXTURE_NOW - 60_000,
        known_at_ms=FIXTURE_NOW - 50_000,
        rule_version="rules-cr06",
        raw_filters={},
        order_types={"LIMIT": True, "MARKET": True, "STOP": True},
        price_rules={"tick_size": "0.000001"},
        lot_rules={"step_size": step, "min_qty": "0.001", "max_qty": "1000000"},
        notional_rules={"min_notional": "5"},
        stop_orders_supported=True,
        conditional_orders_source_ref="rules:1000PEPEUSDT:cr06",
    )


def _cr06_events(native: str) -> Any:
    now = FIXTURE_NOW
    return (
        {"event_id": "o-fut", "leg_type": "FUTURES_SHORT",
         "event_type": "OPEN_FUTURES_SHORT", "native_qty": native,
         "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED",
         "executed_at_ms": now - 1000},
        {"event_id": "o-spot", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "native_qty": native,
         "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED",
         "executed_at_ms": now - 1000},
    )


def test_cr06_dataclass_rules_rounding_1234_to_123() -> None:
    # Native 1.234 step .01 -> floored 1.23 (never raw 1.234).
    positions = aggregate_events(_cr06_events("1.234"), make_identity(), plan_id="p-cr06-round")
    rules = {"FUTURES_SHORT": _cr06_trs("BINANCE_SPOT", "0.01"),
             "SPOT_LONG": _cr06_trs("BINANCE_SPOT", "0.01")}
    market = make_market_context()
    g = build_pair_exit_guidance(
        _plan(plan_id="p-cr06-round"), positions, market, rules, FIXTURE_NOW,
    )
    by_leg = {str(leg["leg_type"]): leg for leg in g.legs}
    assert str(by_leg["FUTURES_SHORT"]["native_qty"]) == "1.23"
    assert str(by_leg["SPOT_LONG"]["native_qty"]) == "1.23"
    assert "STEP_FLOORED" in list(by_leg["FUTURES_SHORT"]["reasons"])
    assert not g.unexecutable_dust


def test_cr06_venue_futures_buy_spot_sell_per_holding() -> None:
    # Futures BUY never BINANCE_SPOT; spot SELL keeps holding venue ALPHA.
    positions = aggregate_events(_cr06_events("6"), make_identity(), plan_id="p-cr06-venue")
    rules = {"FUTURES_SHORT": _cr06_trs("BINANCE_SPOT", "1"),
             "SPOT_LONG": _cr06_trs("BINANCE_ALPHA", "1")}
    plan = _plan(plan_id="p-cr06-venue", spot_venue="BINANCE_ALPHA")
    g = build_pair_exit_guidance(plan, positions, make_market_context(), rules, FIXTURE_NOW)
    by_leg = {str(leg["leg_type"]): leg for leg in g.legs}
    assert by_leg["FUTURES_SHORT"]["side"] == "BUY"
    assert by_leg["FUTURES_SHORT"]["venue"] != "BINANCE_SPOT"
    assert by_leg["SPOT_LONG"]["side"] == "SELL"
    assert by_leg["SPOT_LONG"]["venue"] == "BINANCE_ALPHA"


def test_cr06_dust_below_step_separate_column() -> None:
    # 0.005 with step .01 -> dust only, never zeroed leg.
    positions = aggregate_events(_cr06_events("0.005"), make_identity(), plan_id="p-cr06-dust")
    rules = {"FUTURES_SHORT": _cr06_trs("BINANCE_SPOT", "0.01"),
             "SPOT_LONG": _cr06_trs("BINANCE_SPOT", "0.01")}
    g = build_pair_exit_guidance(
        _plan(plan_id="p-cr06-dust"), positions, make_market_context(), rules, FIXTURE_NOW,
    )
    assert len(g.legs) == 0
    assert str(g.unexecutable_dust["FUTURES_SHORT"]) == "0.005"
    assert str(g.unexecutable_dust["SPOT_LONG"]) == "0.005"
    assert "DUST_BELOW_STEP" in list(g.reasons)


def test_cr06_coverage_refs_earliest_expiry() -> None:
    # Coverage/refs flow through; earliest expiry wins (1s not extended to 20s).
    positions = aggregate_events(_cr06_events("6"), make_identity(), plan_id="p-cr06-cov")
    market = make_market_context(
        futures_buy_vwap_native="100",
        spot_sell_vwap_native="110",
        futures_exit_coverage="1",
        spot_exit_coverage="0.5",
        exit_quote_refs={"futures": "fq-early", "spot": "sq-early"},
        expires_at_ms=FIXTURE_NOW + 1_000,
    )
    # Earlier per-leg expiry must win over the top-level 1s.
    market = dict(market)
    market["futures_expires_at_ms"] = FIXTURE_NOW + 5_000
    market["spot_expires_at_ms"] = FIXTURE_NOW + 1_000
    g = build_pair_exit_guidance(
        _plan(plan_id="p-cr06-cov"), positions, market,
        _rules(step="1"), FIXTURE_NOW,
    )
    assert g.expires_at_ms == FIXTURE_NOW + 1_000
    by_leg = {str(leg["leg_type"]): leg for leg in g.legs}
    assert by_leg["FUTURES_SHORT"]["capability"] == "CONFIRMED"
    assert by_leg["FUTURES_SHORT"]["coverage"] == "1"
    assert by_leg["SPOT_LONG"]["capability"] == "PARTIAL"
    assert by_leg["SPOT_LONG"]["coverage"] == "0.5"
    assert by_leg["FUTURES_SHORT"]["source_refs"]["exit_quote"] == "fq-early"
    assert by_leg["SPOT_LONG"]["source_refs"]["exit_quote"] == "sq-early"


# ---------------------------------------------------------------------------
# CR06 service wiring: real SpotVenueQuote / TradingRulesSnapshot dataclasses
# are read (not discarded); holding venue Futures BUY / Spot SELL with exact
# remaining qty; true rounding + coverage/refs/earliest expiry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cr06_service_exit_dataclass_venue_qty_expiry(tmp_path) -> None:
    import dataclasses

    from diveintocrypto_desktop.shortlab.hedge.models import (
        SpotVenueQuote,
        TradingRulesSnapshot,
    )

    NOW_SVC = FIXTURE_NOW

    class _Clock:
        def __init__(self) -> None:
            self.ms = NOW_SVC

        def __call__(self) -> int:
            return int(self.ms)

    clock = _Clock()

    async def _quote_dataclass(symbol: str, qty: str, venue: Any = None) -> SpotVenueQuote:
        _v = str(venue or "BINANCE_SPOT")
        return SpotVenueQuote(
            venue=_v,
            canonical_id="pepe",
            symbol=str(symbol).upper(),
            chain=None,
            contract_address=None,
            as_of_ms=NOW_SVC,
            expires_at_ms=NOW_SVC + 1_000,  # 1s quote: must not become 20s.
            reference_notional_usd="10000",
            mid_price="100",
            buy_vwap="100",
            sell_vwap="110",
            buy_executable_qty="1000000",
            sell_executable_qty="1000000",
            buy_slippage_bps=5.0,
            sell_slippage_bps=5.0,
            estimated_fee_usd=None,
            estimated_gas_usd=None,
            direction_costs={},
            entry_feasible=True,
            exit_feasible=True,
            exit_feasibility="CONFIRMED",
            quote_currency="USDT",
            quote_to_usd="1",
            source_timestamp_ms=NOW_SVC - 1_000,
            fetched_at_ms=NOW_SVC,
            requested_canonical_qty=str(qty),
            trading_rules={},
            capabilities={},
            identity_confidence="VERIFIED",
            status="OK",
            reason_code=None,
        )

    async def _mark(symbol: str) -> dict[str, Any]:
        return {
            "mark_price": "100", "native_price": "100",
            "quote_currency": "USDT", "quote_to_usd": "1",
            "symbol": str(symbol).upper(),
            "as_of_ms": NOW_SVC, "fetched_at_ms": NOW_SVC,
            "known_at_ms": NOW_SVC, "expires_at_ms": NOW_SVC + 60_000,
        }

    def _trs(venue: str) -> TradingRulesSnapshot:
        return TradingRulesSnapshot(
            venue=venue, instrument_id="1000PEPEUSDT",
            source_as_of_ms=NOW_SVC - 60_000, known_at_ms=NOW_SVC - 50_000,
            rule_version="rules-cr06-svc", raw_filters={},
            order_types={"LIMIT": True, "MARKET": True, "STOP": True},
            price_rules={"tick_size": "0.000001"},
            lot_rules={"step_size": "0.01", "min_qty": "0.001", "max_qty": "1000000"},
            notional_rules={"min_notional": "5"},
            stop_orders_supported=True,
            conditional_orders_source_ref="rules:1000PEPEUSDT:cr06",
        )

    def _rules_fn(venue: str):
        def _fn() -> TradingRulesSnapshot:
            return _trs(venue)

        return _fn

    from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
    from diveintocrypto_desktop.shortlab.service import ShortLabService
    from diveintocrypto_desktop.shortlab.service import build_default_repair_ports
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    base = load_shortlab_config()
    try:
        hedge = dataclasses.replace(base.hedge, enabled=True)
        funding = dataclasses.replace(base.funding_capture, enabled=True)
        cfg = dataclasses.replace(base, hedge=hedge, funding_capture=funding)
    except Exception:
        cfg = base
    repo = await ShortLabRepository.open(tmp_path / "cr06-svc.duckdb")
    await repo.migrate(target_version=6)
    try:
        svc = ShortLabService(
            config=cfg, repository=repo, registry=ProviderRegistry(), clock=clock,
            identity_overrides={
                "1000PEPEUSDT": {
                    "canonical_id": "pepe", "display_symbol": "1000PEPEUSDT",
                    "contract_multiplier": 1000, "multiplier_source": "EXCHANGE",
                    "mapping_confidence": "VERIFIED", "mapping_source": "MANUAL",
                    "binance_spot_symbol": "1000PEPEUSDT", "coingecko_id": "pepe",
                }
            },
            hedge_mark_fn=_mark,
            hedge_quote_fn=_quote_dataclass,
            hedge_funding_fn=None,
            hedge_futures_rules_fn=_rules_fn("BINANCE_SPOT"),
            hedge_spot_rules_fn=_rules_fn("BINANCE_ALPHA"),
            hedge_available=True,
            repair_ports=build_default_repair_ports(),
        )
        sim = await svc.simulate({
            "symbol": "1000PEPEUSDT", "mode": "ABSOLUTE",
            "futuresNotionalUsd": "10000", "preferredSpotVenue": "AUTO",
        })
        plan = await svc.save_plan({"simulation_id": sim["simulationId"], "client_request_id": "cr06-svc-1"})
        pid = plan["planId"]

        def _evt(leg: str, typ: str, qty: str) -> dict[str, Any]:
            return {
                "schema_version": "hedge-event-v1", "leg_type": leg, "event_type": typ,
                "native_qty": qty, "canonical_qty": qty, "native_price": "100",
                "price_currency": "USDT", "fee_currency": None, "fee_amount": None,
                "fee_usd": None, "gas_usd": None, "source": "USER_ENTERED",
                "executed_at_ms": NOW_SVC, "gross_qty": qty, "net_qty": qty,
            }

        r1 = await svc.apply_leg_event(pid, {"event": _evt("FUTURES_SHORT", "OPEN_FUTURES_SHORT", "1.234"), "client_event_id": "e-f-1", "expected_version": 1})
        await svc.apply_leg_event(pid, {"event": _evt("SPOT_LONG", "OPEN_SPOT_LONG", "1.234"), "client_event_id": "e-s-1", "expected_version": r1["planVersion"]})
        out = await svc.repair_exit_guidance(pid)
        # Quantity rounded to step .01 (1.234 -> 1.23), venue per holding, 1s expiry kept.
        by_leg = {str(leg["leg_type"]): leg for leg in out["legs"]}
        assert str(by_leg["FUTURES_SHORT"]["native_qty"]) == "1.23"
        assert str(by_leg["SPOT_LONG"]["native_qty"]) == "1.23"
        assert by_leg["FUTURES_SHORT"]["side"] == "BUY"
        assert by_leg["SPOT_LONG"]["side"] == "SELL"
        assert by_leg["FUTURES_SHORT"]["venue"] == "BINANCE_FUTURES"
        # Spot holding venue comes from plan (AUTO -> BINANCE_SPOT here); dataclass venue preserved via rules.
        assert by_leg["SPOT_LONG"]["venue"] in ("BINANCE_SPOT", "BINANCE_ALPHA")
        assert out["expiresAtMs"] == NOW_SVC + 1_000
        # Coverage + refs present (never empty VWAP/coverage).
        assert by_leg["FUTURES_SHORT"]["coverage"] is not None
        assert by_leg["SPOT_LONG"]["coverage"] is not None
        assert by_leg["FUTURES_SHORT"]["vwap_native"] is not None
        assert by_leg["SPOT_LONG"]["vwap_native"] is not None
    finally:
        await repo.close()
