"""R01 repository: 006 queries, sources, budget atomics, protection CAS, retention pins.

Real DuckDB temp files (never sqlite). Covers D13.1 all 20 methods incl. the
2 month-budget methods, target-6 wiring, pin-first same-transaction retention
(BOOK 3d / MARK 14d / OI 180d / Entry 365d / unreferenced Decision 30d),
quote-task atomic claim/finish + restart recovery, and 503 queue semantics.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import time

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab.repair_contracts import (
    OpportunityQuery,
)
from diveintocrypto_desktop.shortlab.repair_ports import RepositoryPort
from diveintocrypto_desktop.shortlab.repository import (
    HedgeIdempotencyError,
    HedgeVersionConflictError,
    LocalWriteBusyError,
    ReferenceNotFoundError,
    ShortLabRepository,
    ValidationError,
)

DAY_MS = 86_400_000
NOW = 1_760_000_000_000
BUDGET_AS_OF = int(_dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)
BUDGET_MONTH = "2026-10"


@pytest_asyncio.fixture
async def repo(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "r01.duckdb")
    await handle.migrate(target_version=6)
    yield handle
    await handle.close()


def _market(sym="BTCUSDT", kind="MARK", oid="m-1", src=None, known=None):
    return {
        "observation_id": oid,
        "symbol": sym,
        "kind": kind,
        "source_as_of_ms": NOW - 60_000 if src is None else src,
        "known_at_ms": NOW - 50_000 if known is None else known,
        "value_json": {"price": "100"},
        "meta_json": {"status": "OK", "repair_schema_version": "repair-contract-v1"},
        "raw_sha256": f"sha-{oid}",
    }


def _schedule(sid="sched-1", sym="BTCUSDT", eff=None, known=None):
    return {
        "schedule_id": sid,
        "symbol": sym,
        "effective_from_ms": NOW - 90 * DAY_MS if eff is None else eff,
        "effective_to_ms": None,
        "known_at_ms": NOW - 1_000 if known is None else known,
        "schedule_json": {
            "schedule_id": sid, "symbol": sym,
            "effective_from_ms": NOW - 90 * DAY_MS if eff is None else eff,
            "effective_to_ms": None,
            "interval_hours": 8, "anchor_ms": NOW - 90 * DAY_MS,
            "known_at_ms": NOW - 1_000 if known is None else known,
            "source": "binance:fapi/fundingInfo", "evidence_ref": "ev-1",
            "verification": "CONFIRMED",
        },
    }


def _fx(fxid="fx-1", cur="USDT", src=None, known=None, rate="1"):
    return {
        "fx_id": fxid, "currency": cur,
        "source_as_of_ms": NOW - 5_000 if src is None else src,
        "known_at_ms": NOW - 4_000 if known is None else known,
        "rate_str": rate, "source_json": {"source": "coingecko"},
    }


def _decision(did="dec-1", sym="BTCUSDT", gen=None, exp=None):
    g = NOW if gen is None else gen
    e = NOW + 20_000 if exp is None else exp
    return {
        "decision_id": did, "symbol": sym,
        "generated_at_ms": g, "expires_at_ms": e,
        "decision_policy_hash": "ab" * 32,
        "decision_json": {
            "request": {"symbol": sym, "goal": "CARRY_CAPTURE"},
            "context_refs": {"identity": "id-1", "fcs": "fcs-1"},
            "recommendation": "FULL_HEDGE",
        },
    }


def _sim(sim_id="sim-r01", gen=None, exp=None):
    return {
        "simulation_id": sim_id, "symbol": "BTCUSDT",
        "generated_at_ms": NOW - 1_000 if gen is None else gen,
        "expires_at_ms": NOW + 3_600_000 if exp is None else exp,
        "formula_version": "hedge_v1", "policy_hash": "ff" * 32,
        "input_json": {"symbol": "BTCUSDT"}, "result_json": {},
        "source_meta_json": {},
    }


def _plan(plan_id="plan-r01", sim_id="sim-r01", client="client-r01", created=None):
    c = NOW if created is None else created
    return {
        "plan_id": plan_id, "symbol": "BTCUSDT", "canonical_id": "bitcoin",
        "mode": "ABSOLUTE", "status": "DRAFT", "simulation_id": sim_id,
        "client_request_id": client, "plan_config_json": {},
        "created_at_ms": c, "updated_at_ms": c,
    }


def _fcs(sid="fcs-r01", sym="BTCUSDT", asof=None, created=None, proj=None, readiness="READY"):
    a = NOW if asof is None else asof
    c = NOW if created is None else created
    base_proj = {
        "snapshot_id": sid, "symbol": sym, "canonical_id": "bitcoin",
        "as_of_ms": a, "expires_at_ms": a + 1_800_000, "stale": False,
        "fcs": 80.0, "fcs_config_hash": "00" * 32,
        "funding_7d": "0.01", "funding_30d": "0.02",
        "positive_ratio_30d": "0.9", "history_class": "FULL_90D",
        "best_venue": "BINANCE_SPOT", "break_even_days": "10",
        "conservative_apr": "0.2",
        "readiness_breakdown": {"readiness": readiness},
        "readiness": readiness, "reasons": [],
    }
    if proj is not None:
        base_proj.update(proj)
    return {
        "snapshot_id": sid, "symbol": sym, "canonical_id": "bitcoin",
        "as_of_ms": a, "fcs_version": "fcs_v2", "fcs_config_hash": "00" * 32,
        "reference_notional_usd": 10000.0, "fcs": 80.0,
        "module_scores_json": {}, "funding_metrics_json": {},
        "venue_summary_json": {}, "risk_json": {"projection_v2": base_proj},
        "readiness": readiness, "reasons_json": [], "created_at_ms": c,
    }


def test_repository_port_has_all_20_methods():
    expected = {
        'save_market_observation', 'get_market_observation',
        'list_market_observations', 'list_funding_observations',
        'save_funding_schedule', 'list_funding_schedules',
        'save_fx_observation', 'get_fx_at',
        'save_hedge_decision', 'get_hedge_decision',
        'save_protection_confirmation', 'get_protection_confirmation',
        'reserve_provider_request', 'finish_provider_request',
        'list_current_funding_opportunities',
        'save_strategy_entry', 'list_strategy_entries',
        'save_strategy_quote_task', 'claim_due_quote_tasks', 'finish_quote_task',
    }
    assert len(expected) == 20
    for name in expected:
        assert callable(getattr(ShortLabRepository, name)), name
    for name in expected:
        assert name in dir(RepositoryPort), name


@pytest.mark.asyncio
async def test_market_observation_roundtrip_and_known_cutoff(repo):
    await repo.save_market_observation(_market(oid="m-a", src=NOW - 10_000, known=NOW - 9_000))
    await repo.save_market_observation(_market(oid="m-b", src=NOW - 5_000, known=NOW - 1_000))
    got = await repo.get_market_observation("m-a")
    assert got is not None and got["symbol"] == "BTCUSDT"
    assert await repo.get_market_observation("missing") is None
    # known_by excludes the later receipt.
    early = await repo.list_market_observations("BTCUSDT", "MARK", NOW - 20_000, NOW, NOW - 5_000)
    assert {r["observation_id"] for r in early} == {"m-a"}
    all_rows = await repo.list_market_observations("BTCUSDT", "MARK", NOW - 20_000, NOW, NOW)
    assert {r["observation_id"] for r in all_rows} == {"m-a", "m-b"}
    with pytest.raises(ValidationError):
        await repo.save_market_observation({"observation_id": "bad"})
    with pytest.raises(ValidationError):
        await repo.save_market_observation({**_market(oid="m-c"), "kind": "NOPE"})
    with pytest.raises(ValidationError):
        await repo.list_market_observations("BTCUSDT", "MARK", NOW, NOW - 1, NOW)


@pytest.mark.asyncio
async def test_funding_observations_select_latest_receipt(repo):
    await repo.save_funding_observation({
        "observation_id": "fobs-1", "symbol": "BTCUSDT",
        "funding_time_ms": NOW - 8 * 3_600_000, "known_at_ms": NOW - 7_000,
        "raw_json": {"rate": "0.0005"}, "observation_status": "OBSERVED",
    })
    await repo.save_funding_observation({
        "observation_id": "fobs-2", "symbol": "BTCUSDT",
        "funding_time_ms": NOW - 8 * 3_600_000, "known_at_ms": NOW - 1_000,
        "raw_json": {"rate": "0.0006"}, "observation_status": "OBSERVED",
    })
    rows = await repo.list_funding_observations("BTCUSDT", NOW - 9 * 3_600_000, NOW, NOW - 500)
    assert len(rows) == 1
    assert rows[0]["observation_id"] == "fobs-2"
    # Point-in-time: older cutoff sees the older receipt.
    rows_old = await repo.list_funding_observations("BTCUSDT", NOW - 9 * 3_600_000, NOW, NOW - 5_000)
    assert [r["observation_id"] for r in rows_old] == ["fobs-1"]


@pytest.mark.asyncio
async def test_funding_schedule_and_fx_roundtrip(repo):
    await repo.save_funding_schedule(_schedule())
    rows = await repo.list_funding_schedules("BTCUSDT", NOW)
    assert len(rows) == 1 and rows[0]["schedule_id"] == "sched-1"
    assert rows[0]["schedule_json"]["interval_hours"] == 8
    assert await repo.list_funding_schedules("BTCUSDT", NOW - 5_000) == ()
    with pytest.raises(ValidationError):
        await repo.save_funding_schedule({**_schedule(), "schedule_json": {"interval_hours": 0}})
    await repo.save_fx_observation(_fx())
    got = await repo.get_fx_at("USDT", NOW, NOW)
    assert got is not None and got["rate_str"] == "1"
    assert await repo.get_fx_at("USDT", NOW - 120_000, NOW) is None  # older than 60s age
    assert await repo.get_fx_at("EUR", NOW, NOW) is None
    with pytest.raises(ValidationError):
        await repo.save_fx_observation({**_fx(fxid="fx-bad"), "rate_str": "0"})


@pytest.mark.asyncio
async def test_decision_save_get_and_reference_rollback(repo):
    await repo.save_market_observation(_market(oid="m-dec", kind="BOOK"))
    did = await repo.save_hedge_decision(
        _decision("dec-ok"),
        references=[{"referenced_type": "MARKET_OBSERVATION", "referenced_id": "m-dec", "purpose": "pin"}],
    )
    assert did == "dec-ok"
    got = await repo.get_hedge_decision("dec-ok")
    assert got is not None and got["symbol"] == "BTCUSDT"
    assert await repo.get_hedge_decision("missing") is None
    # Bad reference rolls back the whole transaction: no decision row leaks.
    with pytest.raises(Exception):
        await repo.save_hedge_decision(
            _decision("dec-bad"),
            references=[{"referenced_type": "MARKET_OBSERVATION", "referenced_id": "nope", "purpose": "pin"}],
        )
    assert await repo.get_hedge_decision("dec-bad") is None
    refs = await repo.list_snapshot_references("DECISION", "dec-ok")
    assert {(r["referenced_type"], r["referenced_id"]) for r in refs} == {("MARKET_OBSERVATION", "m-dec")}
    with pytest.raises(ValidationError):
        await repo.save_hedge_decision(
            {"decision_id": "x", "symbol": "BTCUSDT"}, references=())


@pytest.mark.asyncio
async def test_protection_cas_idempotent_and_conflicts(repo):
    await repo.save_hedge_simulation(_sim())
    await repo.create_hedge_plan(_plan())
    rec = {
        "client_request_id": "prot-1", "confirmed_at_ms": NOW,
        "expires_at_ms": NOW + 86_400_000,
        "confirmation_json": {"futures": {"status": "CONFIRMED"}, "spot": {"status": "CONFIRMED"}},
    }
    first = await repo.save_protection_confirmation("plan-r01", 1, rec)
    assert first["plan_version"] == 2
    assert first["confirmation_json"]["protected_position_hash"]
    assert first["confirmation_json"]["resulting_plan_version"] == 2
    # Same payload replays idempotently even with the old expected version.
    replay = await repo.save_protection_confirmation("plan-r01", 1, rec)
    assert replay["confirmation_id"] == first["confirmation_id"]
    plan = await repo.get_hedge_plan("plan-r01")
    assert int(plan["plan_version"]) == 2
    # Same key, different payload -> 409.
    with pytest.raises(HedgeIdempotencyError):
        await repo.save_protection_confirmation("plan-r01", 2, {
            **rec, "confirmation_json": {"futures": {}, "spot": {}, "protected_position_hash": "other"},
        })
    # Stale version with a fresh key -> 409 version conflict.
    with pytest.raises(HedgeVersionConflictError):
        await repo.save_protection_confirmation("plan-r01", 1, {
            "client_request_id": "prot-2", "confirmed_at_ms": NOW,
            "expires_at_ms": NOW + 86_400_000, "confirmation_json": {},
        })
    latest = await repo.get_protection_confirmation("plan-r01")
    assert latest is not None and latest["client_request_id"] == "prot-1"
    assert await repo.get_protection_confirmation("no-plan") is None


@pytest.mark.asyncio
async def test_budget_full_state_table(repo):
    # Unknown finish -> NOT_FOUND, nothing written.
    with pytest.raises(ValidationError, match="BUDGET_REQUEST_NOT_FOUND"):
        await repo.finish_provider_request("ghost", True, BUDGET_AS_OF)
    r1 = await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "b-1", 2, BUDGET_AS_OF)
    assert r1 == {"admitted": True, "request_id": "b-1", "month_key": BUDGET_MONTH,
                  "sent_count": 0, "reserved_count": 1, "remaining": 1, "reason_code": None}
    # Duplicate reserve does not re-admit and does not change counts.
    r1dup = await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "b-1", 2, BUDGET_AS_OF)
    assert r1dup["admitted"] is False and r1dup["reason_code"] == "BUDGET_RESERVATION_ALREADY_HELD"
    assert (r1dup["sent_count"], r1dup["reserved_count"]) == (0, 1)
    r2 = await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "b-2", 2, BUDGET_AS_OF)
    assert r2["admitted"] is True and r2["remaining"] == 0
    # Exhausted: no new row.
    r3 = await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "b-3", 2, BUDGET_AS_OF)
    assert r3["admitted"] is False and r3["reason_code"] == "BUDGET_MONTHLY_EXHAUSTED"
    # Same ID, different provider/month/limit -> conflict, no write.
    with pytest.raises(ValidationError, match="BUDGET_REQUEST_ID_CONFLICT"):
        await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "b-1", 99, BUDGET_AS_OF)
    with pytest.raises(ValidationError, match="MONTH_KEY_MISMATCH"):
        await repo.reserve_provider_request("coingecko", "2026-09", "b-9", 2, BUDGET_AS_OF)
    # Finish RESERVED->SENT, then repeat finish is idempotent.
    await repo.finish_provider_request("b-1", True, BUDGET_AS_OF + 1_000)
    await repo.finish_provider_request("b-1", True, BUDGET_AS_OF + 2_000)
    # Reverse terminal transition -> conflict.
    with pytest.raises(ValidationError, match="BUDGET_STATE_CONFLICT"):
        await repo.finish_provider_request("b-1", False, BUDGET_AS_OF + 3_000)
    # Cancel the other reservation, then re-reserve same ID stays cancelled.
    await repo.finish_provider_request("b-2", False, BUDGET_AS_OF + 1_000)
    cancelled = await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "b-2", 2, BUDGET_AS_OF)
    assert cancelled["reason_code"] == "BUDGET_REQUEST_CANCELLED"
    # Clock skew: finish before reservation.
    await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "b-4", 10, BUDGET_AS_OF)
    with pytest.raises(ValidationError, match="CLOCK_SKEW"):
        await repo.finish_provider_request("b-4", True, BUDGET_AS_OF - 1_000)
    # SENT request re-reserve reports already-sent.
    sent_again = await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "b-1", 2, BUDGET_AS_OF)
    assert sent_again["reason_code"] == "BUDGET_REQUEST_ALREADY_SENT"


@pytest.mark.asyncio
async def test_budget_restart_keeps_reserved_as_sent(tmp_path):
    db = tmp_path / "budget-restart.duckdb"
    h1 = await ShortLabRepository.open(db)
    try:
        await h1.migrate(target_version=6)
        await h1.reserve_provider_request("coingecko", BUDGET_MONTH, "br-1", 10, BUDGET_AS_OF)
    finally:
        await h1.close()
    h2 = await ShortLabRepository.open(db)
    try:
        await h2.migrate(target_version=6)
        # Recovery ran on open: RESERVED conservatively became SENT.
        dup = await h2.reserve_provider_request("coingecko", BUDGET_MONTH, "br-1", 10, BUDGET_AS_OF)
        assert dup["reason_code"] == "BUDGET_REQUEST_ALREADY_SENT"
    finally:
        await h2.close()


@pytest.mark.asyncio
async def test_opportunity_latest_row_number_and_stale(repo):
    # Old READY + newer NOT_READY for the same symbol: latest wins, READY hidden.
    await repo.save_funding_capture_snapshot(_fcs("fcs-old", "PEPEUSDT", asof=NOW - 10_000, created=NOW - 9_000,
        proj={"expires_at_ms": NOW + 1_800_000, "readiness": "READY",
              "readiness_breakdown": {"readiness": "READY"}}))
    await repo.save_funding_capture_snapshot(_fcs("fcs-new", "PEPEUSDT", asof=NOW, created=NOW,
        proj={"expires_at_ms": NOW + 1_800_000, "readiness": "NOT_READY",
              "readiness_breakdown": {"readiness": "NOT_READY"}, "reasons": ["FUNDING_CURRENT_NON_POSITIVE"]}))
    page = await repo.list_current_funding_opportunities(OpportunityQuery(readiness="READY"), as_of_ms=NOW + 1_000)
    assert all(item["symbol"] != "PEPEUSDT" for item in page.items)
    assert page.total == 0 and page.as_of_ms is None
    # Unfiltered latest is the newer NOT_READY row (not the old READY, not both).
    page_all = await repo.list_current_funding_opportunities(OpportunityQuery(), as_of_ms=NOW + 1_000)
    pepe = [i for i in page_all.items if i["symbol"] == "PEPEUSDT"]
    assert len(pepe) == 1 and pepe[0]["snapshot_id"] == "fcs-new"
    # Stale: query past expiry excludes by default, includes with NOT_READY when asked.
    await repo.save_funding_capture_snapshot(_fcs("fcs-exp", "DOGEUSDT", asof=NOW - 5_000, created=NOW - 4_000,
        proj={"expires_at_ms": NOW + 500}))
    fresh = await repo.list_current_funding_opportunities(OpportunityQuery(), as_of_ms=NOW + 1_000)
    assert all(i["symbol"] != "DOGEUSDT" for i in fresh.items)
    stale = await repo.list_current_funding_opportunities(OpportunityQuery(include_stale=True), as_of_ms=NOW + 1_000)
    doge = [i for i in stale.items if i["symbol"] == "DOGEUSDT"]
    assert len(doge) == 1 and doge[0]["stale"] is True and doge[0]["readiness"] == "NOT_READY"
    # Filters / sort / page total consistency (no 200-row pre-truncation).
    for k in range(3):
        await repo.save_funding_capture_snapshot(_fcs(f"s{k}", f"C{k}USDT", asof=NOW, created=NOW + k,
            proj={"fcs": float(10 + k), "funding_30d": f"0.0{k+1}", "positive_ratio_30d": "0.9"}))
    sorted_page = await repo.list_current_funding_opportunities(
        OpportunityQuery(sort="fcs", order="desc", limit=2, offset=0), as_of_ms=NOW + 1_000)
    assert sorted_page.total >= 3
    assert len(sorted_page.items) == 2
    assert float(sorted_page.items[0]["fcs"]) >= float(sorted_page.items[1]["fcs"])
    with pytest.raises(ValidationError):
        await repo.list_current_funding_opportunities({"limit": 500}, as_of_ms=NOW)


@pytest.mark.asyncio
async def test_strategy_entry_and_quote_task_lifecycle(repo):
    await repo.save_market_observation(_market(oid="m-e1", kind="BOOK"))
    await repo.save_hedge_decision(_decision("dec-e1"), references=[
        {"referenced_type": "MARKET_OBSERVATION", "referenced_id": "m-e1", "purpose": "pin"}])
    eid = await repo.save_strategy_entry({
        "entry_id": "entry-1", "cohort": "USER_DECISION", "symbol": "BTCUSDT",
        "source_snapshot_id": "dec-e1", "strategy": "ABSOLUTE_100",
        "decision_as_of_ms": NOW, "entry_json": {"status": "ENTRY_COMPLETE"},
    }, references=[{"referenced_type": "DECISION", "referenced_id": "dec-e1", "purpose": "pin"}])
    assert eid == "entry-1"
    rows = await repo.list_strategy_entries("USER_DECISION", NOW - 1_000, NOW + 1_000)
    assert len(rows) == 1 and rows[0]["entry_id"] == "entry-1"
    assert await repo.list_strategy_entries("USER_DECISION", NOW + 1_000, NOW + 2_000) == ()
    with pytest.raises(ValidationError):
        await repo.save_strategy_entry({
            "entry_id": "entry-2", "cohort": "USER_DECISION", "symbol": "BTCUSDT",
            "source_snapshot_id": "dec-e1", "strategy": "ABSOLUTE_100",
            "decision_as_of_ms": NOW, "entry_json": {"status": "ENTRY_COMPLETE"},
        }, references=())
    # Quote tasks: atomic claim, deadline blocks reclaim, finish transitions.
    await repo.save_strategy_quote_task({
        "task_id": "q-1", "entry_id": "entry-1", "horizon_days": 30,
        "purpose": "EXIT", "due_ms": NOW, "status": "PENDING",
        "task_json": {"deadline_ms": NOW + 100_000, "attempt_count": 0},
        "updated_at_ms": NOW,
    })
    await repo.save_strategy_quote_task({
        "task_id": "q-exp", "entry_id": "entry-1", "horizon_days": 7,
        "purpose": "EXIT", "due_ms": NOW - 200_000, "status": "PENDING",
        "task_json": {"deadline_ms": NOW - 100_000, "attempt_count": 0},
        "updated_at_ms": NOW - 200_000,
    })
    claimed = await repo.claim_due_quote_tasks(NOW + 1_000, limit=20)
    ids = {c["task_id"] for c in claimed}
    assert "q-1" in ids and "q-exp" not in ids
    assert all(c["status"] == "RUNNING" for c in claimed if c["task_id"] == "q-1")
    await repo.finish_quote_task("q-1", "COMPLETE", {"quote_refs": {"futures": "fq-1"}})
    with pytest.raises(ValidationError):
        await repo.finish_quote_task("q-1", "COMPLETE", {})
    with pytest.raises((ValidationError, ReferenceNotFoundError)):
        await repo.finish_quote_task("missing", "COMPLETE", {})
    with pytest.raises((ValidationError, ReferenceNotFoundError)):
        await repo.save_strategy_quote_task({
            "task_id": "q-bad", "entry_id": "missing-entry", "horizon_days": 30,
            "purpose": "EXIT", "due_ms": NOW, "status": "PENDING",
            "task_json": {}, "updated_at_ms": NOW,
        })


@pytest.mark.asyncio
async def test_quote_running_recovers_to_pending_with_interrupted(tmp_path):
    db = tmp_path / "quote-restart.duckdb"
    h1 = await ShortLabRepository.open(db)
    try:
        await h1.migrate(target_version=6)
        await h1.save_market_observation(_market(oid="m-q", kind="BOOK"))
        await h1.save_hedge_decision(_decision("dec-q"), references=[
            {"referenced_type": "MARKET_OBSERVATION", "referenced_id": "m-q", "purpose": "pin"}])
        await h1.save_strategy_entry({
            "entry_id": "entry-q", "cohort": "USER_DECISION", "symbol": "BTCUSDT",
            "source_snapshot_id": "dec-q", "strategy": "ABSOLUTE_100",
            "decision_as_of_ms": NOW, "entry_json": {"status": "ENTRY_COMPLETE"},
        }, references=[])
        await h1.save_strategy_quote_task({
            "task_id": "q-run", "entry_id": "entry-q", "horizon_days": 30,
            "purpose": "EXIT", "due_ms": NOW, "status": "PENDING",
            "task_json": {"deadline_ms": NOW + 1_000_000, "attempt_count": 0},
            "updated_at_ms": NOW,
        })
        claimed = await h1.claim_due_quote_tasks(NOW + 1_000)
        assert any(c["task_id"] == "q-run" and c["status"] == "RUNNING" for c in claimed)
    finally:
        await h1.close()
    h2 = await ShortLabRepository.open(db)
    try:
        await h2.migrate(target_version=6)
        cur = h2._con.execute("SELECT status, task_json FROM sl_strategy_quote_task WHERE task_id = 'q-run'")
        status, tj = cur.fetchone()
        assert status == "PENDING"
        assert "PROCESS_INTERRUPTED" in tj
        # Recovered task is claimable again before its deadline.
        again = await h2.claim_due_quote_tasks(NOW + 2_000)
        assert any(c["task_id"] == "q-run" for c in again)
    finally:
        await h2.close()


@pytest.mark.asyncio
async def test_queue_full_reports_503_without_commit(repo):
    import threading
    started = threading.Event()
    release = threading.Event()

    def _slow():
        started.set()
        release.wait(5.0)
        return "slow"

    blocker = asyncio.create_task(repo._run(_slow, priority=1))
    for _ in range(500):
        if started.is_set():
            break
        await asyncio.sleep(0.01)
    assert started.is_set()
    pending = [asyncio.create_task(repo._run(lambda: 1, priority=4)) for _ in range(256)]
    await asyncio.sleep(0.2)
    assert repo.queue_depth() == 256
    with pytest.raises(LocalWriteBusyError, match="LOCAL_WRITE_BUSY") as exc_info:
        await repo.save_market_observation(_market(oid="overflow"))
    assert exc_info.value.status_code == 503
    assert exc_info.value.error_code == "LOCAL_WRITE_BUSY"
    release.set()
    await asyncio.gather(blocker, *pending)
    # Drained: the overflow write never committed.
    assert await repo.get_market_observation("overflow") is None


@pytest.mark.asyncio
async def test_retention_pin_first_and_per_kind_ttls(repo):
    # Pinned BOOK (referenced by a decision) survives past its 3d TTL.
    await repo.save_market_observation(_market(oid="book-pin", kind="BOOK", src=NOW - 10 * DAY_MS, known=NOW - 10 * DAY_MS))
    await repo.save_market_observation(_market(oid="book-old", kind="BOOK", src=NOW - 10 * DAY_MS, known=NOW - 10 * DAY_MS))
    await repo.save_hedge_decision(_decision("dec-pin"), references=[
        {"referenced_type": "MARKET_OBSERVATION", "referenced_id": "book-pin", "purpose": "pin"}])
    # MARK young (5d) retained, MARK old (20d) swept; OI 100d retained, 200d swept.
    await repo.save_market_observation(_market(oid="mark-young", kind="MARK", src=NOW - 5 * DAY_MS, known=NOW - 5 * DAY_MS))
    await repo.save_market_observation(_market(oid="mark-old", kind="MARK", src=NOW - 20 * DAY_MS, known=NOW - 20 * DAY_MS))
    await repo.save_market_observation(_market(oid="oi-keep", kind="OI", src=NOW - 100 * DAY_MS, known=NOW - 100 * DAY_MS))
    await repo.save_market_observation(_market(oid="oi-drop", kind="OI", src=NOW - 200 * DAY_MS, known=NOW - 200 * DAY_MS))
    # MARK_BAR_1H 200d retained (365d floor); Entry 200d retained.
    await repo.save_market_observation(_market(oid="bar-keep", kind="MARK_BAR_1H", src=NOW - 200 * DAY_MS, known=NOW - 200 * DAY_MS))
    await repo.save_hedge_decision(_decision("dec-old", gen=NOW - 40 * DAY_MS, exp=NOW - 40 * DAY_MS + 20_000), references=())
    await repo.save_hedge_decision(_decision("dec-young", gen=NOW - 5 * DAY_MS, exp=NOW - 5 * DAY_MS + 20_000), references=())
    await repo.save_strategy_entry({
        "entry_id": "entry-keep", "cohort": "USER_DECISION", "symbol": "BTCUSDT",
        "source_snapshot_id": "dec-young", "strategy": "ABSOLUTE_100",
        "decision_as_of_ms": NOW - 200 * DAY_MS, "entry_json": {"status": "ENTRY_COMPLETE"},
    }, references=[])
    # Plans/ledger never auto-deleted: create a plan that stays.
    await repo.save_hedge_simulation(_sim("sim-keep"))
    await repo.create_hedge_plan(_plan("plan-keep", "sim-keep", "client-keep"))
    stats = await repo.maintain_retention({}, NOW, 1000)
    assert stats.errors == 0
    assert await repo.get_market_observation("book-pin") is not None
    assert await repo.get_market_observation("book-old") is None
    assert await repo.get_market_observation("mark-young") is not None
    assert await repo.get_market_observation("mark-old") is None
    assert await repo.get_market_observation("oi-keep") is not None
    assert await repo.get_market_observation("oi-drop") is None
    assert await repo.get_market_observation("bar-keep") is not None
    assert await repo.get_hedge_decision("dec-pin") is not None
    assert await repo.get_hedge_decision("dec-old") is None
    assert await repo.get_hedge_decision("dec-young") is not None
    entries = await repo.list_strategy_entries("USER_DECISION", NOW - 300 * DAY_MS, NOW)
    assert any(e["entry_id"] == "entry-keep" for e in entries)
    assert await repo.get_hedge_plan("plan-keep") is not None


@pytest.mark.asyncio
async def test_event_fx_uses_market_observation_mapping(repo):
    await repo.save_market_observation({
        "observation_id": "evfx-1", "symbol": "BTCUSDT", "kind": "EVENT_FX",
        "source_as_of_ms": NOW - 1_000, "known_at_ms": NOW - 500,
        "value_json": {"event_id": "e-1", "price_fx": "1", "fee_fx": "1"},
        "meta_json": {}, "raw_sha256": "evfx",
    })
    got = await repo.get_market_observation("evfx-1")
    assert got is not None and got["value_json"]["event_id"] == "e-1"
    with pytest.raises(ValidationError):
        await repo.save_market_observation({
            "observation_id": "evfx-bad", "symbol": "BTCUSDT", "kind": "EVENT_FX",
            "source_as_of_ms": NOW, "known_at_ms": NOW,
            "value_json": {"no_event": True}, "meta_json": {}, "raw_sha256": "x",
        })
