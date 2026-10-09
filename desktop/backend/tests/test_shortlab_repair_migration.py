"""R01 migration: 006 applies seven tables + eight indexes, rollback keeps v5.

Real DuckDB temp files (never sqlite). Helpers defined here per plan:
repository_v5 builds actual 001-005; inject_bad_006 only replaces the test
resource read with legal-first-DDL + illegal-second; read_schema_version and
original_event_count query via the repo single worker.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab.repository import (
    MigrationError,
    ShortLabRepository,
)

NOW = 1_760_000_000_000

SEVEN_TABLES = (
    "sl_market_observation",
    "sl_funding_schedule",
    "sl_fx_observation",
    "sl_hedge_decision_snapshot",
    "sl_hedge_protection_confirmation",
    "sl_strategy_entry_snapshot",
    "sl_strategy_quote_task",
)

EIGHT_INDEXES = (
    "idx_sl_market_observation_cutoff",
    "idx_sl_funding_schedule_cutoff",
    "idx_sl_fx_observation_cutoff",
    "idx_sl_hedge_decision_symbol",
    "idx_sl_protection_plan",
    "idx_sl_strategy_entry_time",
    "idx_sl_quote_task_due",
    "idx_sl_fcs_current",
)


@pytest_asyncio.fixture
async def repository_v5(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "r01v5.duckdb")
    assert await handle.migrate(target_version=5) == 5
    # Three canonical funding events as the pre-006 checksum baseline.
    await handle.upsert_funding_events([
        {"symbol": "BTCUSDT", "funding_time_ms": NOW - 3_600_000 * i,
         "funding_rate": 0.0001 + i * 0.00001}
        for i in range(3)
    ])
    yield handle
    await handle.close()


async def read_schema_version(repo: ShortLabRepository) -> int:
    cur = repo._con.execute("SELECT max(version) FROM sl_schema_version")
    return int(cur.fetchone()[0])


async def original_event_count(repo: ShortLabRepository) -> int:
    cur = repo._con.execute("SELECT count(*) FROM sl_funding_event")
    return int(cur.fetchone()[0])


def inject_bad_006(monkeypatch):
    from diveintocrypto_desktop.shortlab import repository as repo_mod
    from diveintocrypto_desktop import resources as res_mod
    real_read = res_mod.read_resource_text

    def _patched(name: str) -> str:
        if name == "shortlab/migrations/006_optimization_repair.sql":
            return (
                "CREATE TABLE IF NOT EXISTS sl_tmp_r01_probe "
                "(id VARCHAR PRIMARY KEY);\n"
                "THIS IS NOT VALID SQL ("
            )
        return real_read(name)

    monkeypatch.setattr(res_mod, "read_resource_text", _patched)
    # repository imports read_resource_text lazily, so patching resources suffices.
    _ = repo_mod


@pytest.mark.asyncio
async def test_migration_rolls_back(repository_v5, monkeypatch):
    inject_bad_006(monkeypatch)
    with pytest.raises(MigrationError):
        await repository_v5.migrate(target_version=6)
    assert await read_schema_version(repository_v5) == 5
    assert await original_event_count(repository_v5) == 3
    # No partial 006 tables leak through the failed file transaction.
    for table in SEVEN_TABLES:
        cur = repository_v5._con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [table],
        )
        assert cur.fetchone()[0] == 0, table
    # Replay with the real 006 succeeds.
    monkeypatch.undo()
    assert await repository_v5.migrate(target_version=6) == 6
    assert await read_schema_version(repository_v5) == 6
    assert await original_event_count(repository_v5) == 3


@pytest.mark.asyncio
async def test_migration_repeat_006_is_noop_without_rewrite(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "r01repeat.duckdb")
    try:
        assert await handle.migrate(target_version=6) == 6
        cur = handle._con.execute(
            "SELECT applied_at_ms FROM sl_schema_version WHERE version = 6"
        )
        first = cur.fetchone()[0]
        assert await handle.migrate(target_version=6) == 6
        cur = handle._con.execute(
            "SELECT count(*), max(applied_at_ms) FROM sl_schema_version WHERE version = 6"
        )
        count, latest = cur.fetchone()
        assert count == 1 and latest == first
    finally:
        await handle.close()


@pytest.mark.asyncio
async def test_006_applies_seven_tables_eight_indexes_idempotently(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "r01tables.duckdb")
    try:
        assert await handle.migrate(target_version=5) == 5
        assert await handle.migrate(target_version=6) == 6
        for table in SEVEN_TABLES:
            cur = handle._con.execute(f"SELECT count(*) FROM {table}")
            assert cur.fetchone()[0] == 0, table
        names = await handle.index_names()
        for index in EIGHT_INDEXES:
            assert index in names, index
        assert await handle.migrate(target_version=6) == 6
    finally:
        await handle.close()


def test_006_resource_and_spec_collect_real_file():
    from diveintocrypto_desktop.resources import read_resource_text
    sql = read_resource_text("shortlab/migrations/006_optimization_repair.sql")
    for table in SEVEN_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, table
    for index in EIGHT_INDEXES:
        assert index in sql, index
    # IF NOT EXISTS everywhere; single-file transaction is applied by the repo.
    assert sql.count("IF NOT EXISTS") >= 15
    spec_path = Path(__file__).resolve().parent.parent / "short-lab.spec"
    assert spec_path.is_file()
    tree = ast.parse(spec_path.read_text(encoding="utf-8"))
    sql_literals = {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.endswith(".sql")
    }
    assert "006_optimization_repair.sql" in sql_literals
    assert sql_literals == {
        "001_init.sql", "002_unlock_social.sql", "003_catalyst.sql",
        "004_core_completion.sql", "005_hedge_advisor.sql",
        "006_optimization_repair.sql",
    }


@pytest.mark.asyncio
async def test_schema_target_is_6_for_enabled_config():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.repository import schema_target_for_config
    base = load_shortlab_config()
    assert schema_target_for_config(base) == 6
    import dataclasses
    hedge_on = dataclasses.replace(
        base, hedge=dataclasses.replace(base.hedge, enabled=True))
    assert schema_target_for_config(hedge_on) == 6
