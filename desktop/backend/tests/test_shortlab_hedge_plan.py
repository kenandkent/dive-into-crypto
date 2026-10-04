"""H06 immutable manual-fill ledger + state machine (B19/B21/B28.7.1).

Covers plan H06 scenes (AC15):

- concurrent same-version single commit; duplicate client_event_id
  idempotent on identical payload, 409 on different payload;
- 0.1+0.2 exact, tiny dust never epsilon-swallowed, over-close rejected;
- partial open/close/correction restart replay identical, futures
  liquidation leaves an orphan spot leg;
- unknown data never blocks recording a real fill nor rewrites real state;
- ACTIVE needs both legs positive with drift <= 5pts; single leg is
  PARTIALLY_FILLED with an independent exit action; CLOSED needs each
  leg 0-or-rule-dust (RELATIVE never requires equality); actual funding
  receipts never add to estimates.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab.hedge.ledger import (
    Ledger,
    aggregate_events,
    canonical_event_type,
    evaluate_position_state,
    is_dust,
    normalize_event_for_repo,
    recommend_exit_action,
    sum_funding_receipts,
    sum_qty_strs,
)
from diveintocrypto_desktop.shortlab.hedge.models import HedgePosition
from diveintocrypto_desktop.shortlab.repository import (
    HedgeIdempotencyError,
    HedgeVersionConflictError,
    ShortLabRepository,
)

NOW = 1_760_000_000_000


def _sim(sim_id: str = "sim-h06") -> dict:
    return {
        "simulation_id": sim_id,
        "symbol": "BTCUSDT",
        "generated_at_ms": NOW - 1_000,
        "expires_at_ms": NOW + 3_600_000,
        "formula_version": "hedge_v1",
        "policy_hash": "p" * 64,
        "input_json": {},
        "result_json": {},
        "source_meta_json": {},
    }


def _plan(
    plan_id: str = "plan-h06",
    sim_id: str = "sim-h06",
    client_id: str = "client-h06",
    mode: str = "ABSOLUTE",
) -> dict:
    return {
        "plan_id": plan_id,
        "symbol": "BTCUSDT",
        "canonical_id": "bitcoin",
        "mode": mode,
        "status": "DRAFT",
        "simulation_id": sim_id,
        "client_request_id": client_id,
        "plan_config_json": {"target_hedge_ratio": "1"},
        "created_at_ms": NOW,
        "updated_at_ms": NOW,
        "target_hedge_ratio": 1.0,
        "futures_notional_usd": 10000.0,
        "futures_contract_qty": 0.15,
        "canonical_futures_qty": 0.15,
        "spot_venue": "BINANCE_SPOT",
        "target_spot_qty": 0.3,
    }


def _evt(
    leg: str,
    etype: str,
    qty: str | None = "1.0",
    client_extra: dict | None = None,
    **overrides,
) -> dict:
    base: dict = {
        "schema_version": "hedge-event-v1",
        "leg_type": leg,
        "event_type": etype,
        "native_qty": qty,
        "canonical_qty": qty,
        "native_price": "67000",
        "price_currency": "USDT",
        "fee_currency": None,
        "fee_amount": None,
        "fee_usd": None,
        "gas_usd": None,
        "source": "USER_ENTERED",
        "executed_at_ms": NOW,
        "gross_qty": qty,
        "net_qty": qty,
    }
    if etype == "FUNDING_RECEIPT":
        base = {
            "schema_version": "hedge-event-v1",
            "leg_type": "FUNDING",
            "event_type": "FUNDING_RECEIPT",
            "native_qty": None,
            "canonical_qty": None,
            "native_price": None,
            "price_currency": None,
            "fee_currency": None,
            "fee_amount": None,
            "fee_usd": None,
            "gas_usd": None,
            "source": "USER_ENTERED",
            "executed_at_ms": NOW,
            "amount": "5",
            "currency": "USDT",
            "public_funding_event_id": "pub-1",
        }
    base.update(overrides)
    if client_extra:
        base.update(client_extra)
    # Drop qty keys for funding receipts when qty is None.
    if etype == "FUNDING_RECEIPT":
        for key in ("native_qty", "canonical_qty", "native_price",
                    "gross_qty", "net_qty"):
            base.pop(key, None)
    return base


@pytest_asyncio.fixture
async def repo(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "h06.duckdb")
    await handle.migrate(target_version=4)
    await handle.migrate(target_version=5)
    yield handle
    await handle.close()


async def _new_plan(repo, plan_id="plan-h06", sim_id="sim-h06",
                    client_id="client-h06", mode="ABSOLUTE"):
    await repo.save_hedge_simulation(_sim(sim_id))
    await repo.create_hedge_plan(_plan(plan_id, sim_id, client_id, mode))
    return Ledger(repo)


# ---------------------------------------------------------------------------
# Pure decimal maths: 0.1 + 0.2, dust, no DOUBLE.
# ---------------------------------------------------------------------------


def test_sum_qty_strs_point_one_plus_point_two():
    assert sum_qty_strs(["0.1", "0.2"]) == "0.3"
    assert sum_qty_strs(["0.1", "0.2", "-0.3"]) == "0"
    # 40-digit dust survives the default 28-digit context.
    tiny = "0." + "0" * 35 + "1"
    assert Decimal(sum_qty_strs([tiny, tiny])) == Decimal(tiny) * 2


def test_aggregate_point_one_plus_point_two_exact():
    events = [
        {"event_id": "e1", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "executed_at_ms": NOW,
         "canonical_qty": "0.1"},
        {"event_id": "e2", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "executed_at_ms": NOW + 1,
         "canonical_qty": "0.2"},
    ]
    (fut, spot) = aggregate_events(events, plan_id="p")
    assert spot.open_qty == "0.3"
    assert spot.remaining_qty == "0.3"


def test_dust_never_swallowed_and_is_dust_needs_rules():
    dust = "0.000000000000000001"
    events = [
        {"event_id": "d1", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "executed_at_ms": NOW,
         "canonical_qty": dust},
    ]
    (_, spot) = aggregate_events(events, plan_id="p")
    assert spot.remaining_qty == dust
    assert spot.remaining_qty != "0"
    # Without rules only exact zero is closed.
    assert is_dust(dust, None) is False
    assert is_dust("0", None) is False
    # With a step rule the same remainder is explicit dust.
    rules = {"lot_rules": {"step_size": "0.00001", "min_qty": "0.0001"}}
    assert is_dust(dust, rules) is True
    assert is_dust("0.001", rules) is False


def test_fee_net_qty_wins_and_no_double_count():
    # Base-token fee / transfer tax already deducted in net_qty: the
    # balance counts net, the fee stays a cost line (never subtracted
    # a second time).
    events = [
        {"event_id": "f1", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "executed_at_ms": NOW,
         "gross_qty": "1.0", "canonical_qty": "0.999",
         "net_qty": "0.999",
         "fee_amount": "0.001", "fee_currency": "BTC"},
    ]
    (_, spot) = aggregate_events(events, plan_id="p")
    assert spot.open_qty == "0.999"
    assert spot.remaining_qty == "0.999"


def test_generic_alias_translation():
    assert canonical_event_type("OPEN", "SPOT_LONG") == "OPEN_SPOT_LONG"
    assert canonical_event_type("OPEN", "FUTURES_SHORT") == "OPEN_FUTURES_SHORT"
    assert canonical_event_type("CLOSE", "SPOT_LONG") == "CLOSE_SPOT_LONG"
    assert canonical_event_type(
        "ADJUSTMENT", "SPOT_LONG",
        has_reverses=True, has_supersedes=False,
    ) == "CORRECT_REVERSAL"
    assert canonical_event_type(
        "ADJUSTMENT", "SPOT_LONG",
        has_reverses=False, has_supersedes=True,
    ) == "CORRECT_SUPERSEDE"
    normalised = normalize_event_for_repo({
        "leg_type": "SPOT_LONG", "event_type": "OPEN",
        "native_qty": "1.0", "executed_at_ms": NOW,
    })
    assert normalised["event_type"] == "OPEN_SPOT_LONG"
    assert normalised["schema_version"] == "hedge-event-v1"
    assert normalised["source"] == "USER_ENTERED"


def test_over_close_rejected_pure():
    events = [
        {"event_id": "o1", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "executed_at_ms": NOW,
         "canonical_qty": "1.0"},
        {"event_id": "c1", "leg_type": "SPOT_LONG",
         "event_type": "CLOSE_SPOT_LONG", "executed_at_ms": NOW + 1,
         "canonical_qty": "9.0"},
    ]
    with pytest.raises(ValueError, match="remaining"):
        aggregate_events(events, plan_id="p")


def test_correction_double_reverse_rejected_pure():
    events = [
        {"event_id": "o1", "plan_id": "p", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "executed_at_ms": NOW,
         "canonical_qty": "1.0"},
        {"event_id": "r1", "plan_id": "p", "leg_type": "SPOT_LONG",
         "event_type": "CORRECT_REVERSAL", "executed_at_ms": NOW + 1,
         "canonical_qty": "1.0", "reverses_event_id": "o1"},
        {"event_id": "r2", "plan_id": "p", "leg_type": "SPOT_LONG",
         "event_type": "CORRECT_REVERSAL", "executed_at_ms": NOW + 2,
         "canonical_qty": "1.0", "reverses_event_id": "o1"},
    ]
    with pytest.raises(ValueError, match="only be reversed once"):
        aggregate_events(events, plan_id="p")


def test_funding_receipt_never_enters_qty_and_never_adds_to_estimate():
    events = [
        {"event_id": "o1", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "executed_at_ms": NOW,
         "canonical_qty": "1.0"},
        {"event_id": "f1", "leg_type": "FUNDING",
         "event_type": "FUNDING_RECEIPT", "executed_at_ms": NOW + 1,
         "amount": "5", "currency": "USDT",
         "public_funding_event_id": "pub-1"},
    ]
    (fut, spot) = aggregate_events(events, plan_id="p")
    assert spot.open_qty == "1"
    assert spot.remaining_qty == "1"
    assert sum_funding_receipts(events) == {"USDT": "5"}
    # Actual receipts stay separate from any estimated number.
    assert sum_funding_receipts(events)["USDT"] != "105"


# ---------------------------------------------------------------------------
# State machine: ACTIVE / PARTIALLY_FILLED / CLOSING / CLOSED.
# ---------------------------------------------------------------------------


def _positions(fut_open, fut_rem, spot_open, spot_rem):
    return (
        HedgePosition(plan_id="p", leg_type="FUTURES_SHORT",
                      open_qty=fut_open, closed_qty="0",
                      remaining_qty=fut_rem, gross_qty=fut_open,
                      net_qty=fut_rem, event_ids=("a",)),
        HedgePosition(plan_id="p", leg_type="SPOT_LONG",
                      open_qty=spot_open, closed_qty="0",
                      remaining_qty=spot_rem, gross_qty=spot_open,
                      net_qty=spot_rem, event_ids=("b",)),
    )


def test_active_needs_both_legs_and_drift_within_5pts():
    state = evaluate_position_state(
        _positions("2", "2", "2", "2"), "1",
        mode="ABSOLUTE", plan_id="p", plan_version=3, updated_at_ms=NOW,
    )
    assert state.status == "ACTIVE"
    code, action = recommend_exit_action(_positions("2", "2", "2", "2"), "1")
    assert (code, action) == ("NONE", "NONE")


def test_single_leg_is_partially_filled_with_independent_action():
    state = evaluate_position_state(
        _positions("0", "0", "1", "1"), "1",
        mode="ABSOLUTE", plan_id="p", plan_version=2, updated_at_ms=NOW,
    )
    assert state.status == "PARTIALLY_FILLED"
    # The exit suggestion is independent: it never rewrites the state.
    code, action = recommend_exit_action(
        _positions("0", "0", "1", "1"), "1")
    assert code == "ORPHAN_LEG_WARNING"
    assert action == "REVIEW"
    assert state.status == "PARTIALLY_FILLED"


def test_drift_beyond_5pts_blocks_active():
    # Actual 0.9 vs target 1.0 -> 10pts drift.
    state = evaluate_position_state(
        _positions("1", "1", "0.9", "0.9"), "1",
        mode="ABSOLUTE", plan_id="p", plan_version=2, updated_at_ms=NOW,
    )
    assert state.status == "PARTIALLY_FILLED"
    code, action = recommend_exit_action(
        _positions("1", "1", "0.9", "0.9"), "1")
    assert (code, action) == ("RATIO_DRIFT_WARN", "REVIEW")


def test_closed_allows_dust_per_leg_and_relative_unequal():
    rules = {
        "FUTURES_SHORT": {"lot_rules": {"step_size": "0.01", "min_qty": "0.01"}},
        "SPOT_LONG": {"lot_rules": {"step_size": "0.01", "min_qty": "0.01"}},
    }
    tiny = "0.001"
    state = evaluate_position_state(
        _positions("2", tiny, "1", tiny), "0.5",
        mode="RELATIVE", plan_id="p", plan_version=5, updated_at_ms=NOW,
        rules=rules,
    )
    # Each leg holds only rule dust; RELATIVE legs were never equal.
    assert state.status == "CLOSED"
    assert recommend_exit_action(
        _positions("2", tiny, "1", tiny), "0.5", rules=rules,
    ) == ("NONE", "NONE")


def test_closed_requires_exact_zero_without_rules():
    tiny = "0.000000000000000001"
    state = evaluate_position_state(
        _positions("2", tiny, "1", tiny), "0.5",
        mode="RELATIVE", plan_id="p", plan_version=5, updated_at_ms=NOW,
    )
    assert state.status != "CLOSED"


def test_liquidation_orphan_spot_is_closing_with_urgent_action():
    positions = aggregate_events([
        {"event_id": "fo", "plan_id": "p", "leg_type": "FUTURES_SHORT",
         "event_type": "OPEN_FUTURES_SHORT", "executed_at_ms": NOW,
         "canonical_qty": "2.0"},
        {"event_id": "so", "plan_id": "p", "leg_type": "SPOT_LONG",
         "event_type": "OPEN_SPOT_LONG", "executed_at_ms": NOW,
         "canonical_qty": "2.0"},
        {"event_id": "liq", "plan_id": "p", "leg_type": "FUTURES_SHORT",
         "event_type": "LIQUIDATION", "executed_at_ms": NOW + 1,
         "canonical_qty": "2.0"},
    ], plan_id="p")
    by_leg = {p.leg_type: p for p in positions}
    assert by_leg["FUTURES_SHORT"].remaining_qty == "0"
    assert by_leg["SPOT_LONG"].remaining_qty == "2"
    state = evaluate_position_state(
        positions, "1", mode="ABSOLUTE", plan_id="p",
        plan_version=4, updated_at_ms=NOW)
    assert state.status == "CLOSING"
    code, action = recommend_exit_action(
        positions, "1", has_liquidation=True)
    assert code == "CRITICAL_ORPHAN_SPOT_LEG"
    assert action == "URGENT_PAIR_EXIT"


# ---------------------------------------------------------------------------
# Ledger over the H01 single-worker transaction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_version_single_commit(repo):
    ledger = await _new_plan(repo)
    await ledger.apply_event(
        "plan-h06", "base-fut", 1,
        _evt("FUTURES_SHORT", "OPEN_FUTURES_SHORT", "2.0"))
    await ledger.apply_event(
        "plan-h06", "base-spot", 2,
        _evt("SPOT_LONG", "OPEN_SPOT_LONG", "2.0"))

    async def _submit(tag: str):
        return await ledger.apply_event(
            "plan-h06", f"race-{tag}", 3,
            _evt("SPOT_LONG", "CLOSE_SPOT_LONG", "0.1"))

    results = await asyncio.gather(
        *[_submit(str(i)) for i in range(5)], return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results
                 if isinstance(r, HedgeVersionConflictError)]
    assert len(ok) == 1
    assert len(conflicts) == 4
    assert ok[0].plan_version == 4


@pytest.mark.asyncio
async def test_duplicate_client_event_id_idempotent_or_409(repo):
    ledger = await _new_plan(repo)
    payload = _evt("SPOT_LONG", "OPEN_SPOT_LONG", "0.1")
    first = await ledger.apply_event("plan-h06", "evt-1", 1, payload)
    # Same payload replays to the same event even with a stale version.
    replay = await ledger.apply_event("plan-h06", "evt-1", 1, dict(payload))
    assert replay.event_id == first.event_id
    assert replay.plan_version == first.plan_version
    # Different payload with the same client id is 409.
    with pytest.raises(HedgeIdempotencyError):
        await ledger.apply_event(
            "plan-h06", "evt-1", 2, _evt("SPOT_LONG", "OPEN_SPOT_LONG", "0.5"))


@pytest.mark.asyncio
async def test_point_one_plus_point_two_via_ledger(repo):
    ledger = await _new_plan(repo)
    await ledger.apply_event(
        "plan-h06", "o1", 1, _evt("SPOT_LONG", "OPEN_SPOT_LONG", "0.1"))
    out = await ledger.apply_event(
        "plan-h06", "o2", 2, _evt("SPOT_LONG", "OPEN_SPOT_LONG", "0.2"))
    spot = next(p for p in out.positions if p.leg_type == "SPOT_LONG")
    assert spot.open_qty == "0.3"
    assert spot.remaining_qty == "0.3"
    assert out.balance_source == "CONFIRMED"
    assert out.estimated is False
    # Closing exactly 0.3 leaves a true zero (no 1e-17 residue).
    out = await ledger.apply_event(
        "plan-h06", "c1", 3, _evt("SPOT_LONG", "CLOSE_SPOT_LONG", "0.3"))
    spot = next(p for p in out.positions if p.leg_type == "SPOT_LONG")
    assert spot.remaining_qty == "0"


@pytest.mark.asyncio
async def test_dust_preserved_and_over_close_rejected_via_ledger(repo):
    ledger = await _new_plan(repo)
    dust = "0.000000000000000001"
    await ledger.apply_event(
        "plan-h06", "d1", 1, _evt("SPOT_LONG", "OPEN_SPOT_LONG", dust))
    positions = await ledger.read_positions("plan-h06")
    spot = next(p for p in positions if p.leg_type == "SPOT_LONG")
    assert spot.remaining_qty == dust
    # Over-close beyond the exact Decimal remainder is rejected.
    with pytest.raises(ValueError, match="remaining"):
        await ledger.apply_event(
            "plan-h06", "over", 2,
            _evt("SPOT_LONG", "CLOSE_SPOT_LONG", "0.000000000000000002"))


@pytest.mark.asyncio
async def test_partial_open_close_correction_restart_replay(repo, tmp_path):
    db = tmp_path / "h06-restart.duckdb"
    handle = await ShortLabRepository.open(db)
    try:
        await handle.migrate(target_version=4)
        await handle.migrate(target_version=5)
        ledger = Ledger(handle)
        await handle.save_hedge_simulation(_sim())
        await handle.create_hedge_plan(_plan())
        await ledger.apply_event(
            "plan-h06", "o1", 1, _evt("SPOT_LONG", "OPEN_SPOT_LONG", "1.0"))
        await ledger.apply_event(
            "plan-h06", "o2", 2, _evt("SPOT_LONG", "OPEN_SPOT_LONG", "0.5"))
        # Correct the second open (partial-fill typo): reverse 0.5.
        await ledger.apply_event("plan-h06", "r1", 3, {
            **_evt("SPOT_LONG", "CORRECT_REVERSAL", "0.5"),
            "reverses_event_id": "plan-h06#o2",
        })
        await ledger.apply_event(
            "plan-h06", "c1", 4, _evt("SPOT_LONG", "CLOSE_SPOT_LONG", "0.4"))
        before = await ledger.read_positions("plan-h06")
    finally:
        await handle.close()
    reopened = await ShortLabRepository.open(db)
    try:
        await reopened.migrate(target_version=5)
        after = await Ledger(reopened).read_positions("plan-h06")
        assert [(p.leg_type, p.open_qty, p.closed_qty, p.remaining_qty)
                for p in after] == [
            (p.leg_type, p.open_qty, p.closed_qty, p.remaining_qty)
            for p in before
        ]
        spot = next(p for p in after if p.leg_type == "SPOT_LONG")
        # 1.5 opened, 0.5 corrected, 0.4 closed -> 0.6 remains.
        assert spot.open_qty == "1.5"
        assert spot.closed_qty == "0.9"
        assert spot.remaining_qty == "0.6"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_futures_liquidation_leaves_orphan_spot(repo):
    ledger = await _new_plan(repo, plan_id="plan-liq", sim_id="sim-liq",
                             client_id="client-liq")
    await ledger.apply_event(
        "plan-liq", "fo", 1, _evt("FUTURES_SHORT", "OPEN_FUTURES_SHORT", "2.0"))
    await ledger.apply_event(
        "plan-liq", "so", 2, _evt("SPOT_LONG", "OPEN_SPOT_LONG", "2.0"))
    out = await ledger.apply_event(
        "plan-liq", "liq", 3,
        _evt("FUTURES_SHORT", "LIQUIDATION", "2.0"))
    by_leg = {p.leg_type: p for p in out.positions}
    assert by_leg["FUTURES_SHORT"].remaining_qty == "0"
    assert by_leg["SPOT_LONG"].remaining_qty == "2"
    state = evaluate_position_state(
        out.positions, "1", mode="ABSOLUTE", plan_id="plan-liq",
        plan_version=out.plan_version, updated_at_ms=NOW)
    assert state.status == "CLOSING"
    code, action = recommend_exit_action(
        out.positions, "1", has_liquidation=True)
    assert code == "CRITICAL_ORPHAN_SPOT_LEG"
    assert action == "URGENT_PAIR_EXIT"


@pytest.mark.asyncio
async def test_adjustment_alias_via_ledger(repo):
    ledger = await _new_plan(repo, plan_id="plan-adj", sim_id="sim-adj",
                             client_id="client-adj")
    await ledger.apply_event(
        "plan-adj", "o1", 1, _evt("SPOT_LONG", "OPEN_SPOT_LONG", "1.0"))
    # B19 shorthand ADJUSTMENT maps to CORRECT_SUPERSEDE and closes the
    # overstated 0.2 delta.
    out = await ledger.apply_event("plan-adj", "a1", 2, {
        **_evt("SPOT_LONG", "ADJUSTMENT", "0.2"),
        "supersedes_event_id": "plan-adj#o1",
    })
    spot = next(p for p in out.positions if p.leg_type == "SPOT_LONG")
    assert spot.open_qty == "1"
    assert spot.closed_qty == "0.2"
    assert spot.remaining_qty == "0.8"


@pytest.mark.asyncio
async def test_unknown_data_never_blocks_real_fill(repo):
    ledger = await _new_plan(repo, plan_id="plan-unk", sim_id="sim-unk",
                             client_id="client-unk")
    exotic = _evt("SPOT_LONG", "OPEN_SPOT_LONG", "1.5",
                  native_price=None,
                  client_extra={
                      "unknown_provider_field": {"weird": [1, 2]},
                      "quote_to_usd": None,
                      "mark_price": None,
                  })
    out = await ledger.apply_event("plan-unk", "u1", 1, exotic)
    spot = next(p for p in out.positions if p.leg_type == "SPOT_LONG")
    assert spot.remaining_qty == "1.5"
    # Unknown target never promotes to ACTIVE but never corrupts state.
    state = evaluate_position_state(
        out.positions, None, mode="ABSOLUTE", plan_id="plan-unk",
        plan_version=out.plan_version, updated_at_ms=NOW)
    assert state.status == "PARTIALLY_FILLED"


@pytest.mark.asyncio
async def test_per_plan_locks_are_loop_bound_and_submit_only(repo):
    ledger = await _new_plan(repo)
    first = ledger._lock_for("plan-h06")
    assert ledger._lock_for("plan-h06") is first
    assert ledger._lock_for("plan-other") is not first
    # The lock is free after a submit (submit-only, no provider hold).
    await ledger.apply_event(
        "plan-h06", "lk1", 1, _evt("SPOT_LONG", "OPEN_SPOT_LONG", "0.1"))
    assert not first.locked()
