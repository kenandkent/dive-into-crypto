"""F01 red tests: contracts, source persistence and execution gate (Short-Lab F01).

Covers the F01 behaviour assertions (plan L143-148, brief §4 items 1-6):

- 004 schema upgrade to 4, repeat no-op, mid-DDL-failure rollback + replay.
- Restart read-back of base tables with legacy scores still visible.
- Funding conflict keeps the observed version, never UPDATEs history.
- Queue FIFO/aging/capacity, immutable snapshots, RUNNING recovery,
  retention caps, cursor round-trip, evidence/due-score visibility.

Written red-first: every test here fails until repository.py gains the 22
F01 methods, SCHEMA_VERSION 4 and the 004 migration.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab import repository as repo_mod
from diveintocrypto_desktop.shortlab.repository import (
    AssetMappingRecord,
    AssetRecord,
    ConfigSnapshotRecord,
    ContractLifecycleRecord,
    ContractRulesSnapshotRecord,
    EvidencePage,
    FundamentalSnapshotRecord,
    FundingEventRecord,
    FundingObservationRecord,
    IdentitySnapshotRecord,
    LocalWriteBusyError,
    MigrationError,
    ReferenceNotFoundError,
    RetentionStats,
    ShortLabRepository,
    SnapshotImmutableError,
    ValidationError,
)

AS_OF = 1760000000000
FETCHED = AS_OF - 60_000


def _asset(cid: str = "BTC") -> AssetRecord:
    return AssetRecord(
        canonical_id=cid,
        display_symbol="BTC",
        name="Bitcoin",
        categories=("L1",),
        created_at_ms=FETCHED,
        updated_at_ms=FETCHED,
    )


def _mapping(sym: str = "BTCUSDT") -> AssetMappingRecord:
    return AssetMappingRecord(
        futures_symbol=sym,
        canonical_id="BTC",
        spot_symbol="BTCUSDT",
        contract_multiplier=1.0,
        multiplier_source="EXCHANGE",
        coingecko_id="bitcoin",
        unlock_provider_id=None,
        social_provider_id=None,
        mapping_confidence="VERIFIED",
        mapping_source="CONTRACT",
        updated_at_ms=FETCHED,
    )


def _identity(sid: str = "id-1") -> IdentitySnapshotRecord:
    return IdentitySnapshotRecord(
        identity_snapshot_id=sid,
        futures_symbol="BTCUSDT",
        canonical_id="BTC",
        mapping_version="map-v1",
        observed_at_ms=FETCHED,
        identity_json={"canonical_id": "BTC", "confidence": "VERIFIED"},
    )


def _rules(sid: str = "rules-1", known: int = FETCHED) -> ContractRulesSnapshotRecord:
    return ContractRulesSnapshotRecord(
        snapshot_id=sid,
        symbol="BTCUSDT",
        source_as_of_ms=AS_OF,
        known_at_ms=known,
        rules_json={"min_qty": "0.001", "tick": "0.1"},
    )


def _lifecycle(sym: str = "BTCUSDT", observed: int = FETCHED) -> ContractLifecycleRecord:
    return ContractLifecycleRecord(
        futures_symbol=sym,
        observed_at_ms=observed,
        onboard_at_ms=None,
        first_seen_ms=observed,
        delivery_at_ms=None,
        contract_type="PERPETUAL",
        exchange_status="TRADING",
        contract_multiplier=1.0,
        multiplier_source="EXCHANGE",
    )


def _funding(sym: str = "BTCUSDT", ft: int = AS_OF, rate: float = 0.0001) -> FundingEventRecord:
    return FundingEventRecord(symbol=sym, funding_time_ms=ft, funding_rate=rate, mark_price=27000.0)


def _observation(oid: str = "obs-1", known: int = FETCHED) -> FundingObservationRecord:
    return FundingObservationRecord(
        observation_id=oid,
        symbol="BTCUSDT",
        funding_time_ms=AS_OF,
        known_at_ms=known,
        raw_json={"r": "0.0001", "T": AS_OF},
        interval_hours=8.0,
        interval_source="fundingInfo",
        observation_status="OBSERVED",
    )


def _fundamental(sid: str = "fund-1") -> FundamentalSnapshotRecord:
    return FundamentalSnapshotRecord(
        snapshot_id=sid,
        canonical_id="BTC",
        as_of_ms=AS_OF,
        fetched_at_ms=FETCHED,
        market_cap_usd=500_000_000_000.0,
        fdv_usd=600_000_000_000.0,
        circulating_supply=19_000_000.0,
        total_supply=21_000_000.0,
        max_supply=21_000_000.0,
        ath_price=69000.0,
        ath_date_ms=AS_OF - 10_000_000,
        source="unit-test",
    )


def _config(ph: str = "p" * 16) -> ConfigSnapshotRecord:
    return ConfigSnapshotRecord(
        policy_hash=ph,
        config_hash="c" * 16,
        policy_version="policy-v1",
        canonical_json={"profile": "GENERAL_LITE"},
        created_at_ms=FETCHED,
    )


@pytest_asyncio.fixture
async def repo(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "f01.duckdb")
    await handle.migrate()
    yield handle
    await handle.close()


# ---------------------------------------------------------------------------
# 1-3. schema upgrade / repeat / mid-DDL-failure rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f01_schema_upgrades_to_4_with_new_tables_and_indexes(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "up.duckdb")
    try:
        assert await handle.migrate(target_version=1) == 1
        assert await handle.migrate(target_version=3) == 3
        assert await handle.migrate() == 4
        for table in (
            "sl_config_snapshot",
            "sl_identity_snapshot",
            "sl_contract_rules_snapshot",
            "sl_funding_observation",
            "sl_data_cursor",
        ):
            cur = handle._con.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 0
        names = await handle.index_names()
        for index in (
            "idx_sl_identity_time",
            "idx_sl_rules_time",
            "idx_sl_funding_observation_time",
            "idx_sl_contract_seen",
            "idx_sl_fundamental_asset_time",
            "idx_sl_evidence_score_time",
        ):
            assert index in names, index
        cur = handle._con.execute("SELECT max(version) FROM sl_schema_version")
        assert cur.fetchone()[0] == 4
    finally:
        await handle.close()


@pytest.mark.asyncio
async def test_f01_repeat_migrate_is_noop_without_version_rewrite(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "repeat.duckdb")
    try:
        assert await handle.migrate() == 4
        cur = handle._con.execute(
            "SELECT applied_at_ms FROM sl_schema_version WHERE version = 4"
        )
        first = cur.fetchone()[0]
        assert await handle.migrate() == 4
        cur = handle._con.execute(
            "SELECT count(*), max(applied_at_ms) FROM sl_schema_version WHERE version = 4"
        )
        count, latest = cur.fetchone()
        assert count == 1 and latest == first
    finally:
        await handle.close()


@pytest.mark.asyncio
async def test_f01_mid_ddl_failure_rolls_back_and_replays(tmp_path, monkeypatch):
    handle = await ShortLabRepository.open(tmp_path / "fail.duckdb")
    try:
        assert await handle.migrate(target_version=3) == 3
        real_split = repo_mod._split_statements

        def _poisoned(script: str) -> list[str]:
            parts = real_split(script)
            if "sl_config_snapshot" in script:
                return parts + ["THIS IS NOT VALID SQL ("]
            return parts

        monkeypatch.setattr(repo_mod, "_split_statements", _poisoned)
        with pytest.raises(MigrationError):
            await handle.migrate(target_version=4)
        cur = handle._con.execute("SELECT max(version) FROM sl_schema_version")
        assert cur.fetchone()[0] == 3
        cur = handle._con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'sl_config_snapshot'"
        )
        assert cur.fetchone()[0] == 0
        monkeypatch.setattr(repo_mod, "_split_statements", real_split)
        assert await handle.migrate(target_version=4) == 4
    finally:
        await handle.close()


# ---------------------------------------------------------------------------
# 4. restart read-back + legacy scores still visible
# ---------------------------------------------------------------------------


def _feature_meta(names) -> dict:
    return {
        name: {
            "status": "OK",
            "fetched_at_ms": FETCHED,
            "as_of_ms": AS_OF,
            "coverage_fraction": 1.0,
            "reason_code": None,
            "source": "unit-test",
        }
        for name in names
    }


@pytest.mark.asyncio
async def test_f01_restart_readback_keeps_base_tables_and_legacy_scores(tmp_path):
    from diveintocrypto_desktop.shortlab.repository import (
        REQUIRED_ENTRY_META_BLOCKS,
        REQUIRED_FEATURE_META_FIELDS,
        EntrySnapshotRecord,
        FeatureSnapshotRecord,
        ScoreSnapshotRecord,
    )

    db_path = tmp_path / "restart.duckdb"
    handle = await ShortLabRepository.open(db_path)
    try:
        await handle.migrate()
        await handle.upsert_asset(_asset())
        await handle.upsert_asset_mapping(_mapping())
        await handle.save_identity_snapshot(_identity())
        await handle.save_contract_rules_snapshot(_rules())
        await handle.save_contract_lifecycle(_lifecycle())
        await handle.upsert_funding_events([_funding()])
        await handle.save_funding_observation(_observation())
        await handle.save_fundamental_snapshot(_fundamental())
        await handle.save_config_snapshot(_config())
        await handle.save_cursor("funding_backfill", "BTCUSDT", {"last_event_time_ms": AS_OF})
        feature = FeatureSnapshotRecord(
            snapshot_id="feat-r1",
            symbol="BTCUSDT",
            as_of_ms=AS_OF,
            feature_version="feat-v1",
            features={"funding_30d": 0.01},
            source_meta=_feature_meta(REQUIRED_FEATURE_META_FIELDS),
            data_quality=90.0,
        )
        entry = EntrySnapshotRecord(
            snapshot_id="entry-r1",
            symbol="BTCUSDT",
            as_of_ms=AS_OF,
            entry_version="entry-v1",
            dive_weights_hash="w" * 16,
            dive_engine_version="dive-0.3.0",
            dive_config_hash="c" * 16,
            primary_tf="1h",
            inputs={},
            components={},
            source_meta=_feature_meta(REQUIRED_ENTRY_META_BLOCKS),
            entry_score=60.0,
            created_at_ms=FETCHED,
        )
        await handle.save_feature(feature)
        await handle.save_entry(entry)
        score = ScoreSnapshotRecord(
            snapshot_id="score-r1",
            generation_id="gen-r1",
            feature_snapshot_id="feat-r1",
            entry_snapshot_id="entry-r1",
            symbol="BTCUSDT",
            as_of_ms=AS_OF,
            analysis_tier="LITE",
            profile="GENERAL_LITE",
            score_version="ltss-lite-v1",
            entry_version="entry-v1",
            feature_version="feat-v1",
            config_hash="c" * 16,
            ltss=80.0,
            entry_score=60.0,
            data_quality=90.0,
            candidate_status="CANDIDATE",
            execution_status="NOT_READY",
            status="CANDIDATE",
            module_scores={},
            vetoes=(),
            pauses=(),
            reasons=(),
            warnings=(),
        )
        await handle.save_score_batch(
            [score], job_id="gen-r1", started_at_ms=FETCHED, finished_at_ms=AS_OF
        )
    finally:
        await handle.close()

    reopened = await ShortLabRepository.open(db_path)
    try:
        await reopened.migrate()
        assert (await reopened.get_identity_snapshot("id-1")) == _identity()
        assert (await reopened.get_contract_rules_snapshot("rules-1")) == _rules()
        assert (await reopened.get_fundamental_before("BTC", AS_OF)) == _fundamental()
        assert (await reopened.get_config_snapshot("p" * 16)) == _config()
        assert await reopened.load_cursor("funding_backfill", "BTCUSDT") == {
            "last_event_time_ms": AS_OF
        }
        events = await reopened.list_funding_events("BTCUSDT", AS_OF - 1, AS_OF + 1)
        assert len(events) == 1 and events[0].funding_rate == pytest.approx(0.0001)
        assert "BTCUSDT" in await reopened.list_tracked_symbols()
        page = await reopened.list_scores_for_evidence({}, limit=50, offset=0)
        assert page.total == 1 and page.items[0].snapshot_id == "score-r1"
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# 5. funding conflict keeps observed version, never UPDATEs canonical history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f01_funding_conflict_preserves_canonical_and_records_observation(repo):
    await repo.upsert_funding_events([_funding(rate=0.0001)])
    await repo.upsert_funding_events([_funding(rate=0.0009)])
    events = await repo.list_funding_events("BTCUSDT", AS_OF - 1, AS_OF + 1)
    assert len(events) == 1
    assert events[0].funding_rate == pytest.approx(0.0001)
    cur = repo._con.execute(
        "SELECT observation_status, raw_json FROM sl_funding_observation "
        "WHERE symbol = ? AND funding_time_ms = ?",
        ["BTCUSDT", AS_OF],
    )
    rows = cur.fetchall()
    assert rows, "conflicting funding content must leave an observed version"
    assert any(status == "CONFLICT" for status, _ in rows)


@pytest.mark.asyncio
async def test_f01_funding_idempotent_same_content_is_noop(repo):
    await repo.upsert_funding_events([_funding(rate=0.0001)])
    await repo.upsert_funding_events([_funding(rate=0.0001)])
    events = await repo.list_funding_events("BTCUSDT", AS_OF - 1, AS_OF + 1)
    assert len(events) == 1
    cur = repo._con.execute("SELECT count(*) FROM sl_funding_observation")
    assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# immutability / PIT / lifecycle / tracked set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f01_immutable_snapshots_reject_different_content(repo):
    await repo.save_identity_snapshot(_identity("id-x"))
    await repo.save_identity_snapshot(_identity("id-x"))  # same content: idempotent
    import dataclasses

    altered = dataclasses.replace(
        _identity("id-x"), identity_json={"canonical_id": "ETH"}
    )
    with pytest.raises(SnapshotImmutableError):
        await repo.save_identity_snapshot(altered)
    await repo.save_contract_rules_snapshot(_rules("rules-x"))
    with pytest.raises(SnapshotImmutableError):
        await repo.save_contract_rules_snapshot(
            ContractRulesSnapshotRecord(
                snapshot_id="rules-x",
                symbol="BTCUSDT",
                source_as_of_ms=AS_OF,
                known_at_ms=FETCHED,
                rules_json={"min_qty": "9.999"},
            )
        )


@pytest.mark.asyncio
async def test_f01_latest_contract_rules_is_point_in_time(repo):
    await repo.save_contract_rules_snapshot(_rules("rules-old", known=1000))
    await repo.save_contract_rules_snapshot(_rules("rules-new", known=2000))
    assert (await repo.latest_contract_rules("BTCUSDT", 1500)).snapshot_id == "rules-old"
    assert (await repo.latest_contract_rules("BTCUSDT", 2000)).snapshot_id == "rules-new"
    assert await repo.latest_contract_rules("BTCUSDT", 999) is None
    assert await repo.get_contract_rules_snapshot("missing") is None


@pytest.mark.asyncio
async def test_f01_lifecycle_appends_and_latest_wins(repo):
    await repo.save_contract_lifecycle(_lifecycle(observed=1000))
    await repo.save_contract_lifecycle(_lifecycle(observed=2000))
    latest = await repo.latest_contract_lifecycle("BTCUSDT")
    assert latest is not None and latest.observed_at_ms == 2000
    assert await repo.latest_contract_lifecycle("UNKNOWN") is None


@pytest.mark.asyncio
async def test_f01_tracked_symbols_unions_history(repo):
    await repo.upsert_asset_mapping(_mapping("BTCUSDT"))
    life = _lifecycle(sym="ETHUSDT", observed=FETCHED)
    await repo.save_contract_lifecycle(life)
    tracked = await repo.list_tracked_symbols()
    assert "BTCUSDT" in tracked and "ETHUSDT" in tracked
    assert tuple(tracked) == tuple(sorted(tracked))


# ---------------------------------------------------------------------------
# 6. queue / transactions / recovery / retention / cursors / evidence gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f01_write_queue_is_fifo_within_priority(repo):
    started: list[str] = []

    def _slow():
        started.append("blocker")
        time.sleep(0.05)
        return "blocked"

    blocker = asyncio.create_task(repo._run(_slow, priority=1))
    while not started:
        await asyncio.sleep(0.01)
    order: list[str] = []

    def _mark(name: str):
        order.append(name)
        return name

    tasks = [
        asyncio.create_task(repo._run(_mark, f"low-{i}", priority=3)) for i in range(3)
    ]
    urgent = asyncio.create_task(repo._run(_mark, "urgent", priority=1))
    await asyncio.gather(blocker, *tasks, urgent)
    assert order[:1] == ["urgent"]
    assert order[1:] == ["low-0", "low-1", "low-2"]


@pytest.mark.asyncio
async def test_f01_write_queue_aging_promotes_long_waiters(repo):
    fired: list[str] = []
    ticks = {"now": 1000.0}
    repo._now_fn = lambda: ticks["now"]

    def _slow():
        time.sleep(0.1)
        return "slow"

    blocker = asyncio.create_task(repo._run(_slow, priority=1))
    await asyncio.sleep(0.02)

    def _mark(name: str):
        fired.append(name)
        return name

    old = asyncio.create_task(repo._run(_mark, "old-low", priority=4))
    await asyncio.sleep(0.02)
    assert repo.queue_depth() >= 1
    ticks["now"] += repo_mod.QUEUE_AGING_SEC + 1.0
    new = asyncio.create_task(repo._run(_mark, "new-mid", priority=3))
    await asyncio.gather(blocker, old, new)
    assert fired[0] == "old-low"


@pytest.mark.asyncio
async def test_f01_write_queue_full_raises_local_write_busy(repo):
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
    pending_tasks = [
        asyncio.create_task(repo._run(lambda: i, priority=4)) for i in range(256)
    ]
    await asyncio.sleep(0.2)
    assert repo.queue_depth() == 256
    with pytest.raises(LocalWriteBusyError, match="LOCAL_WRITE_BUSY"):
        await repo._run(lambda: "overflow", priority=4)
    try:
        with pytest.raises(LocalWriteBusyError) as exc_info:
            await repo._run(lambda: "overflow", priority=0)
        assert "HTTP503" in str(exc_info.value)
    finally:
        release.set()
        await asyncio.gather(blocker, *pending_tasks)


@pytest.mark.asyncio
async def test_f01_failed_writes_leave_no_partial_records(repo):
    await repo.upsert_asset(_asset())
    with pytest.raises(ValidationError):
        await repo.save_identity_snapshot({"identity_snapshot_id": "bad"})
    assert await repo.get_identity_snapshot("bad") is None
    with pytest.raises(ValidationError):
        await repo.save_score_batch(
            [], job_id="empty", started_at_ms=FETCHED, finished_at_ms=AS_OF
        )


@pytest.mark.asyncio
async def test_f01_restart_recovers_running_jobs_as_failed_interrupted(tmp_path):
    db_path = tmp_path / "crash.duckdb"
    handle = await ShortLabRepository.open(db_path)
    try:
        await handle.migrate()
        await handle.create_job_run("job-crash", "score_refresh", FETCHED)
    finally:
        await handle.close()
    reopened = await ShortLabRepository.open(db_path)
    try:
        await reopened.migrate()
        job = await reopened.get_job_run("job-crash")
        assert job is not None
        assert job["status"] == "FAILED"
        assert job["error_code"] == "PROCESS_INTERRUPTED"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_f01_retention_respects_per_call_limit_and_keeps_conflicts(repo):
    for i in range(3):
        await repo.save_funding_observation(
            FundingObservationRecord(
                observation_id=f"old-{i}",
                symbol="BTCUSDT",
                funding_time_ms=AS_OF - 10_000 - i,
                known_at_ms=1000,
                raw_json={"r": "0.1"},
                interval_hours=None,
                interval_source=None,
                observation_status="OBSERVED",
            )
        )
    await repo.save_funding_observation(
        FundingObservationRecord(
            observation_id="conflict-keep",
            symbol="BTCUSDT",
            funding_time_ms=AS_OF - 20_000,
            known_at_ms=1000,
            raw_json={"r": "0.9"},
            interval_hours=None,
            interval_source=None,
            observation_status="CONFLICT",
        )
    )
    stats = await repo.maintain_retention(
        {"funding_observation_ttl_ms": 60_000}, AS_OF, limit=2
    )
    assert isinstance(stats, RetentionStats)
    assert stats.deleted == 2
    stats2 = await repo.maintain_retention(
        {"funding_observation_ttl_ms": 60_000}, AS_OF, limit=1000
    )
    assert stats2.deleted == 1
    assert stats2.retained >= 1  # the CONFLICT row is evidence and stays
    with pytest.raises(ValidationError):
        await repo.maintain_retention({}, AS_OF, limit=1001)


@pytest.mark.asyncio
async def test_f01_cursor_roundtrip_and_missing(repo):
    assert await repo.load_cursor("funding_backfill", "BTCUSDT") is None
    await repo.save_cursor("funding_backfill", "BTCUSDT", {"last_event_time_ms": 111})
    assert await repo.load_cursor("funding_backfill", "BTCUSDT") == {
        "last_event_time_ms": 111
    }
    await repo.save_cursor("funding_backfill", "BTCUSDT", {"last_event_time_ms": 222})
    assert await repo.load_cursor("funding_backfill", "BTCUSDT") == {
        "last_event_time_ms": 222
    }


@pytest.mark.asyncio
async def test_f01_evidence_lists_only_succeeded_generations(repo):
    from diveintocrypto_desktop.shortlab.repository import (
        REQUIRED_ENTRY_META_BLOCKS,
        REQUIRED_FEATURE_META_FIELDS,
        EntrySnapshotRecord,
        FeatureSnapshotRecord,
        ScoreSnapshotRecord,
    )

    def _feat(sid: str) -> FeatureSnapshotRecord:
        return FeatureSnapshotRecord(
            snapshot_id=sid,
            symbol="BTCUSDT",
            as_of_ms=AS_OF,
            feature_version="feat-v1",
            features={},
            source_meta=_feature_meta(REQUIRED_FEATURE_META_FIELDS),
            data_quality=90.0,
        )

    def _entry(sid: str) -> EntrySnapshotRecord:
        return EntrySnapshotRecord(
            snapshot_id=sid,
            symbol="BTCUSDT",
            as_of_ms=AS_OF,
            entry_version="entry-v1",
            dive_weights_hash="w" * 16,
            dive_engine_version="dive-0.3.0",
            dive_config_hash="c" * 16,
            primary_tf="1h",
            inputs={},
            components={},
            source_meta=_feature_meta(REQUIRED_ENTRY_META_BLOCKS),
            entry_score=None,
            created_at_ms=FETCHED,
        )

    def _score(sid: str, gen: str, feat: str) -> ScoreSnapshotRecord:
        return ScoreSnapshotRecord(
            snapshot_id=sid,
            generation_id=gen,
            feature_snapshot_id=feat,
            entry_snapshot_id=None,
            symbol="BTCUSDT",
            as_of_ms=AS_OF,
            analysis_tier="LITE",
            profile="GENERAL_LITE",
            score_version="ltss-lite-v1",
            entry_version=None,
            feature_version="feat-v1",
            config_hash="c" * 16,
            ltss=10.0,
            entry_score=None,
            data_quality=90.0,
            candidate_status="WATCH",
            execution_status="NOT_READY",
            status="WATCH",
            module_scores={},
            vetoes=(),
            pauses=(),
            reasons=(),
            warnings=(),
        )

    await repo.save_feature(_feat("feat-run"))
    await repo.create_job_run("gen-running", "score_refresh", FETCHED)
    await repo.save_score(_score("score-running", "gen-running", "feat-run"))
    page = await repo.list_scores_for_evidence({}, limit=50, offset=0)
    assert isinstance(page, EvidencePage)
    assert page.total == 0
    await repo.finish_job_run("gen-running", "SUCCEEDED", AS_OF)
    page = await repo.list_scores_for_evidence({}, limit=50, offset=0)
    assert page.total == 1
    filtered = await repo.list_scores_for_evidence({"symbol": "ETHUSDT"}, limit=50, offset=0)
    assert filtered.total == 0
    with pytest.raises(ValidationError):
        await repo.list_scores_for_evidence({"bogus": 1}, limit=50, offset=0)


@pytest.mark.asyncio
async def test_f01_due_scores_ordered_by_due_symbol_score(repo):
    from diveintocrypto_desktop.shortlab.repository import (
        REQUIRED_FEATURE_META_FIELDS,
        FeatureSnapshotRecord,
        OutcomeRecord,
        ScoreSnapshotRecord,
    )

    await repo.save_feature(
        FeatureSnapshotRecord(
            snapshot_id="feat-due",
            symbol="BTCUSDT",
            as_of_ms=AS_OF,
            feature_version="feat-v1",
            features={},
            source_meta=_feature_meta(REQUIRED_FEATURE_META_FIELDS),
            data_quality=90.0,
        )
    )
    await repo.save_score(
        ScoreSnapshotRecord(
            snapshot_id="score-due",
            generation_id="gen-due",
            feature_snapshot_id="feat-due",
            entry_snapshot_id=None,
            symbol="BTCUSDT",
            as_of_ms=AS_OF,
            analysis_tier="LITE",
            profile="GENERAL_LITE",
            score_version="ltss-lite-v1",
            entry_version=None,
            feature_version="feat-v1",
            config_hash="c" * 16,
            ltss=10.0,
            entry_score=None,
            data_quality=90.0,
            candidate_status="WATCH",
            execution_status="NOT_READY",
            status="WATCH",
            module_scores={},
            vetoes=(),
            pauses=(),
            reasons=(),
            warnings=(),
        )
    )

    def _outcome(horizon: str, due: int) -> OutcomeRecord:
        return OutcomeRecord(
            score_snapshot_id="score-due",
            horizon=horizon,
            outcome_status="PENDING",
            reason_code=None,
            entry_ts_ms=AS_OF,
            exit_ts_ms=None,
            horizon_due_ms=due,
            formula_version="grader-v1",
            cost_config_hash="costA",
            funding_event_count=None,
            funding_coverage=None,
            graded_at_ms=AS_OF,
            entry_price=None,
            exit_price=None,
            price_short_return=None,
            funding_carry=None,
            fee_assumption=None,
            slippage_assumption=None,
            net_short_return=None,
            mae=None,
            mfe=None,
        )

    await repo.save_outcome(_outcome("7D", AS_OF + 1_000))
    await repo.save_outcome(_outcome("30D", AS_OF + 2_000))
    early = await repo.list_due_scores(AS_OF + 1_500, 10)
    assert [o.horizon for o in early] == ["7D"]
    everything = await repo.list_due_scores(AS_OF + 3_000, 10)
    assert [o.horizon for o in everything] == ["7D", "30D"]
    assert await repo.list_due_scores(AS_OF, 10) == ()


@pytest.mark.asyncio
async def test_f01_strict_records_reject_unknown_fields(repo):
    with pytest.raises(ValidationError):
        await repo.upsert_asset(
            {"canonical_id": "BTC", "display_symbol": "BTC", "created_at_ms": 1,
             "updated_at_ms": 1, "nope": True}
        )
    assert await repo.upsert_asset(_asset()) == "BTC"
    assert await repo.upsert_asset_mapping(_mapping()) == "BTCUSDT"


def test_f01_004_migration_file_exists_verbatim():
    from diveintocrypto_desktop.shortlab import repository as mod

    sql = (Path(mod.__file__).resolve().parent / "migrations" / "004_core_completion.sql").read_text(
        encoding="utf-8"
    )
    for table in (
        "sl_config_snapshot",
        "sl_identity_snapshot",
        "sl_contract_rules_snapshot",
        "sl_funding_observation",
        "sl_data_cursor",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    for index in (
        "idx_sl_identity_time",
        "idx_sl_rules_time",
        "idx_sl_funding_observation_time",
        "idx_sl_contract_seen",
        "idx_sl_fundamental_asset_time",
        "idx_sl_evidence_score_time",
    ):
        assert index in sql
