"""H01 repository: 005 migration, idempotent plans, references, retention pins.

Covers plan H01 scenes 004→005 / POST-idempotency / references:
- 004→005 fault rollback + repeat: eleven tables, no partial schema5;
- POST duplicate client_request_id after simulation expiry returns plan;
- client_request_id idempotency precedes version/expiry checks;
- references written in the same worker transaction;
- monitor/alerts/FCS/venue/outcomes round-trip;
- retention pins referenced simulations/quotes (30d/1d) with invalid-graph guard.
"""

from __future__ import annotations

import time

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab.repository import (
    HedgeIdempotencyError,
    HedgeReferenceInvalidError,
    HedgeVersionConflictError,
    MigrationError,
    ShortLabRepository,
    ValidationError,
)

DAY_MS = 86_400_000
NOW = 1_760_000_000_000

ELEVEN_TABLES = (
    "sl_hedge_simulation_snapshot",
    "sl_hedge_venue_mapping",
    "sl_hedge_fill_event",
    "sl_hedge_snapshot_reference",
    "sl_hedge_outcome",
    "sl_funding_capture_snapshot",
    "sl_spot_venue_snapshot",
    "sl_hedge_plan",
    "sl_hedge_leg",
    "sl_hedge_monitor_snapshot",
    "sl_hedge_alert",
)


def _sim(sim_id: str = "sim-1", generated: int = NOW - 1_000,
         expires: int = NOW + 3_600_000) -> dict:
    return {
        "simulation_id": sim_id,
        "symbol": "BTCUSDT",
        "generated_at_ms": generated,
        "expires_at_ms": expires,
        "formula_version": "hedge_v1",
        "policy_hash": "ff7adb8e321486bfc4c5d8a8c5645b26874be15577bebe63d336ba81980c8f08",
        "input_json": {"symbol": "BTCUSDT", "mode": "ABSOLUTE"},
        "result_json": {"target_spot_qty": "1.5"},
        "source_meta_json": {"schema_version": "hedge-source-v1"},
    }


def _plan(plan_id: str = "plan-1", sim_id: str = "sim-1",
          client_id: str = "client-1", created: int = NOW) -> dict:
    return {
        "plan_id": plan_id,
        "symbol": "BTCUSDT",
        "canonical_id": "bitcoin",
        "mode": "ABSOLUTE",
        "status": "DRAFT",
        "simulation_id": sim_id,
        "client_request_id": client_id,
        "plan_config_json": {
            "target_spot_qty": "1.5",
            "futures_notional_usd": "10000",
            "formula_version": "hedge_v1",
        },
        "created_at_ms": created,
        "updated_at_ms": created,
        "target_hedge_ratio": 1.0,
        "futures_notional_usd": 10000.0,
        "futures_contract_qty": 0.15,
        "canonical_futures_qty": 0.15,
        "spot_venue": "BINANCE_SPOT",
        "target_spot_qty": 1.5,
    }


def _open_spot(qty: str = "1.5", price: str = "67000") -> dict:
    return {
        "schema_version": "hedge-event-v1",
        "leg_type": "SPOT_LONG",
        "event_type": "OPEN_SPOT_LONG",
        "native_qty": qty,
        "canonical_qty": qty,
        "native_price": price,
        "price_currency": "USDT",
        "fee_currency": "BNB",
        "fee_amount": "0.001",
        "fee_usd": "0.5",
        "gas_usd": None,
        "source": "USER_ENTERED",
        "executed_at_ms": NOW,
        "gross_qty": qty,
        "net_qty": qty,
    }


@pytest_asyncio.fixture
async def repo(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "h01.duckdb")
    await handle.migrate(target_version=4)
    await handle.migrate(target_version=5)
    yield handle
    await handle.close()


@pytest.mark.asyncio
async def test_005_applies_eleven_tables_idempotently(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "tables.duckdb")
    try:
        assert await handle.migrate(target_version=4) == 4
        assert await handle.migrate(target_version=5) == 5
        for table in ELEVEN_TABLES:
            cur = handle._con.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 0, table
        names = await handle.index_names()
        for index in (
            "idx_sl_hedge_reference_target",
            "idx_sl_hedge_fill_time",
            "idx_sl_fcs_symbol_time",
            "idx_sl_hedge_plan_status",
            "idx_sl_hedge_leg_plan_type",
            "idx_sl_spot_venue_lookup",
            "idx_sl_hedge_monitor_plan_time",
            "idx_sl_hedge_alert_plan_state",
        ):
            assert index in names, index
        # Repeat is a no-op without version rewrite.
        assert await handle.migrate(target_version=5) == 5
        cur = handle._con.execute(
            "SELECT count(*), max(applied_at_ms) FROM sl_schema_version WHERE version = 5")
        count, _ = cur.fetchone()
        assert count == 1
    finally:
        await handle.close()


@pytest.mark.asyncio
async def test_004_to_005_fault_rolls_back_without_partial_schema5(tmp_path, monkeypatch):
    from diveintocrypto_desktop.shortlab import repository as repo_mod
    handle = await ShortLabRepository.open(tmp_path / "fault005.duckdb")
    try:
        assert await handle.migrate(target_version=4) == 4
        real_split = repo_mod._split_statements

        def _poisoned(script: str) -> list[str]:
            parts = real_split(script)
            if "sl_hedge_plan" in script:
                return parts + ["THIS IS NOT VALID SQL ("]
            return parts

        monkeypatch.setattr(repo_mod, "_split_statements", _poisoned)
        with pytest.raises(MigrationError):
            await handle.migrate(target_version=5)
        cur = handle._con.execute("SELECT max(version) FROM sl_schema_version")
        assert cur.fetchone()[0] == 4
        cur = handle._con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'sl_hedge_plan'")
        assert cur.fetchone()[0] == 0
        # No partial 005 tables leak through the failed file txn.
        for table in ELEVEN_TABLES:
            cur = handle._con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                [table])
            assert cur.fetchone()[0] == 0, table
        monkeypatch.setattr(repo_mod, "_split_statements", real_split)
        assert await handle.migrate(target_version=5) == 5
        for table in ELEVEN_TABLES:
            cur = handle._con.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 0
    finally:
        await handle.close()


@pytest.mark.asyncio
async def test_post_duplicate_client_id_after_sim_expiry_returns_plan(repo):
    # Fresh simulation covers plan creation; then it expires.
    await repo.save_hedge_simulation(_sim("sim-exp", NOW - 10_000, NOW + 60_000))
    plan = _plan("plan-exp", "sim-exp", "client-exp", NOW - 5_000)
    assert await repo.create_hedge_plan(plan) == "plan-exp"
    # Expire the simulation afterwards (update the row directly to simulate
    # time passing; reads still work, new plans would be rejected).
    repo._con.execute(
        "UPDATE sl_hedge_simulation_snapshot SET expires_at_ms = ? WHERE simulation_id = ?",
        [NOW - 1_000, "sim-exp"])
    # Duplicate POST with the SAME payload bypasses expiry and returns plan.
    again = _plan("plan-exp-other-id", "sim-exp", "client-exp", NOW)
    assert await repo.create_hedge_plan(again) == "plan-exp"
    # Different payload with the same client id is 409 (checked before expiry).
    different = _plan("plan-exp-new", "sim-exp", "client-exp", NOW)
    different["plan_config_json"] = {"target_spot_qty": "9.9"}
    with pytest.raises(HedgeIdempotencyError):
        await repo.create_hedge_plan(different)


@pytest.mark.asyncio
async def test_apply_event_idempotent_before_version_check(repo):
    await repo.save_hedge_simulation(_sim())
    await repo.create_hedge_plan(_plan())
    first = await repo.apply_hedge_event("plan-1", "evt-1", 1, _open_spot("1.5"))
    assert first["plan_version"] == 2
    # Same payload replays to the same event even with a stale version.
    replay = await repo.apply_hedge_event("plan-1", "evt-1", 1, _open_spot("1.5"))
    assert replay["event_id"] == first["event_id"]
    # Different payload with the same client id is 409, not a version error.
    with pytest.raises(HedgeIdempotencyError):
        await repo.apply_hedge_event("plan-1", "evt-1", 2, _open_spot("0.5"))
    # Stale version with a fresh client id is 409 PLAN_VERSION_CONFLICT.
    with pytest.raises(HedgeVersionConflictError):
        await repo.apply_hedge_event("plan-1", "evt-fresh", 1, _open_spot("0.1"))
    # Over-close beyond the Decimal remainder is rejected (no epsilon).
    with pytest.raises(ValidationError):
        await repo.apply_hedge_event("plan-1", "evt-over", 2, {
            **_open_spot("9.0"),
            "event_type": "CLOSE_SPOT_LONG",
        })


@pytest.mark.asyncio
async def test_references_written_in_same_transaction(repo):
    await repo.save_hedge_simulation(_sim("sim-ref"))
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "venue-1", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT", "as_of_ms": NOW, "fetched_at_ms": NOW,
        "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "1.5",
                       "requestedCanonicalQty": "1.5"},
        "status": "OK",
    })
    await repo.create_hedge_plan(
        _plan("plan-ref", "sim-ref", "client-ref"),
        references=[("VENUE_QUOTE", "venue-1", "plan-quote")],
    )
    refs = await repo.list_snapshot_references("PLAN", "plan-ref")
    kinds = {(r["referenced_type"], r["referenced_id"]) for r in refs}
    assert ("SIMULATION", "sim-ref") in kinds
    assert ("VENUE_QUOTE", "venue-1") in kinds
    # Referenced IDs must exist; no silent JSON-only pins.
    with pytest.raises(Exception):
        await repo.create_hedge_plan(
            _plan("plan-bad", "sim-ref", "client-bad"),
            references=[("VENUE_QUOTE", "missing-quote", "pin")],
        )


@pytest.mark.asyncio
async def test_monitor_alerts_fcs_venue_outcomes_round_trip(repo):
    await repo.save_hedge_simulation(_sim("sim-m"))
    await repo.create_hedge_plan(_plan("plan-m", "sim-m", "client-m"))
    await repo.save_funding_capture_snapshot({
        "snapshot_id": "fcs-1", "symbol": "BTCUSDT", "canonical_id": "bitcoin",
        "as_of_ms": NOW, "fcs_version": "fcs_v1",
        "fcs_config_hash": "72eca2e7214ac6dfdf908541d1dd185c6b6ce882e30de3297b495c8f1d61c136",
        "reference_notional_usd": 10000.0, "fcs": 82.5,
        "module_scores_json": {}, "funding_metrics_json": {"funding_30d": "0.012"},
        "venue_summary_json": {}, "risk_json": {}, "readiness": "READY",
        "reasons_json": [], "created_at_ms": NOW,
    })
    assert len(await repo.list_fcs("BTCUSDT")) == 1
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "venue-m", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT", "as_of_ms": NOW, "fetched_at_ms": NOW,
        "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "1.5"}, "status": "OK",
    })
    assert len(await repo.list_spot_venues("bitcoin")) == 1
    await repo.save_hedge_monitor_snapshot({
        "snapshot_id": "mon-1", "plan_id": "plan-m", "as_of_ms": NOW,
        "source_meta_json": {}, "quality_json": {}, "metrics_json": {},
        "status": "OK", "created_at_ms": NOW,
    })
    assert (await repo.latest_hedge_monitor("plan-m"))["snapshot_id"] == "mon-1"
    alert = await repo.upsert_hedge_alert(
        "plan-m", "SPOT_EXIT_CAPACITY", "CRITICAL", "PAIR_EXIT", {"q": 1}, NOW)
    assert alert["state"] == "OPEN"
    # Same condition only bumps last_seen (no new episode).
    same = await repo.upsert_hedge_alert(
        "plan-m", "SPOT_EXIT_CAPACITY", "CRITICAL", "PAIR_EXIT", {"q": 2}, NOW + 1)
    assert same["alert_id"] == alert["alert_id"]
    assert len(await repo.list_hedge_alerts("plan-m")) == 1
    await repo.ack_hedge_alert(alert["alert_id"], NOW + 2)
    assert (await repo.list_hedge_alerts("plan-m", "ACKNOWLEDGED"))[0]["alert_id"] == alert["alert_id"]
    await repo.resolve_hedge_alert(alert["alert_id"], NOW + 3)
    assert await repo.list_hedge_alerts("plan-m", "OPEN") == ()
    # Re-trigger after RESOLVED opens a new episode atomically.
    second = await repo.upsert_hedge_alert(
        "plan-m", "SPOT_EXIT_CAPACITY", "CRITICAL", "PAIR_EXIT", {}, NOW + 4)
    assert second["episode"] == 2
    await repo.save_hedge_outcome({
        "outcome_id": "out-1", "fcs_snapshot_id": "fcs-1",
        "strategy": "ABSOLUTE_100", "horizon_days": 30,
        "outcome_status": "PENDING", "evidence_version": "hedge_evidence_v1",
        "cost_config_hash": "9915b1468e5d0e4fc02ee71b4883c5445351928b0e7509f0dc4e6727fcca37dc",
        "outcome_json": {}, "updated_at_ms": NOW,
    })
    assert len(await repo.list_hedge_outcomes("fcs-1")) == 1


@pytest.mark.asyncio
async def test_retention_pins_referenced_simulation_and_quote(repo):
    # Pinned simulation + quote (referenced by a plan) survive past TTL.
    await repo.save_hedge_simulation(_sim("sim-pin", NOW - 31 * DAY_MS, NOW - 30 * DAY_MS))
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "quote-pin", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT", "as_of_ms": NOW - 10 * DAY_MS,
        "fetched_at_ms": NOW - 10 * DAY_MS, "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "1.5"}, "status": "OK",
    })
    await repo.save_snapshot_references(
        "SIMULATION", "sim-pin", [("VENUE_QUOTE", "quote-pin", "sim-quote")], NOW)
    await repo.create_hedge_plan(
        _plan("plan-pin", "sim-pin", "client-pin", NOW - 31 * DAY_MS),
    )
    # Isolated simulation (31d, unreferenced) + ordinary quote (2d, unreferenced).
    await repo.save_hedge_simulation(_sim("sim-old", NOW - 31 * DAY_MS, NOW - 31 * DAY_MS + 1000))
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "quote-old", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT", "as_of_ms": NOW - 2 * DAY_MS,
        "fetched_at_ms": NOW - 2 * DAY_MS, "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "0.1"}, "status": "OK",
    })
    stats = await repo.maintain_retention({}, NOW, 1000)
    assert stats.errors == 0
    assert await repo.get_hedge_simulation("sim-pin") is not None
    assert await repo.get_hedge_simulation("sim-old") is None
    venues = {r["snapshot_id"] for r in await repo.list_spot_venues()}
    assert "quote-pin" in venues
    assert "quote-old" not in venues


@pytest.mark.asyncio
async def test_retention_invalid_graph_stops_batch_conservatively(repo):
    await repo.save_hedge_simulation(_sim("sim-ring"))
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "quote-ring", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT", "as_of_ms": NOW, "fetched_at_ms": NOW,
        "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "1.0"}, "status": "OK",
    })
    await repo.save_snapshot_references(
        "SIMULATION", "sim-ring", [("VENUE_QUOTE", "quote-ring", "pin")], NOW)
    # Forge a two-node ring directly (bypasses the validated writer).
    repo._con.execute(
        "INSERT INTO sl_hedge_snapshot_reference (referrer_type, referrer_id, "
        "referenced_type, referenced_id, purpose, created_at_ms) "
        "VALUES ('VENUE_QUOTE', 'quote-ring', 'SIMULATION', 'sim-ring', 'ring', ?)",
        [NOW],
    )
    stats = await repo.maintain_retention({}, NOW, 1000)
    assert stats.errors == 1
    # Conservative retain: nothing in the ring was swept.
    assert await repo.get_hedge_simulation("sim-ring") is not None


def test_005_resource_manifest_contains_migration_via_resources_and_spec():
    from diveintocrypto_desktop.resources import read_resource_text
    # Frozen bundle reads 005 without a source checkout (F09 review).
    sql = read_resource_text("shortlab/migrations/005_hedge_advisor.sql")
    for table in ELEVEN_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, table
    # Spec collects exactly 001-005 (H01 change, F09 review; no placeholder).
    import ast
    from pathlib import Path
    spec_path = Path(__file__).resolve().parent.parent / "short-lab.spec"
    assert spec_path.is_file()
    tree = ast.parse(spec_path.read_text(encoding="utf-8"))
    sql_literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.endswith(".sql")
    }
    assert sql_literals == {
        "001_init.sql", "002_unlock_social.sql", "003_catalyst.sql",
        "004_core_completion.sql", "005_hedge_advisor.sql",
    }


def test_hedge_enabled_schema_target_is_5_while_base_stays_4():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.repository import schema_target_for_config
    base = load_shortlab_config()
    assert schema_target_for_config(base) == 4
    # Hedge-enabled config targets 5 (same helper, no second repository).
    import dataclasses
    hedge_on = dataclasses.replace(
        base, hedge=dataclasses.replace(base.hedge, enabled=True))
    assert schema_target_for_config(hedge_on) == 5
