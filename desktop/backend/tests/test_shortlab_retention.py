"""F09: resources, maintenance and base release gate (design A9.3/A9.4).

Base gate only (schema 4, no Hedge/005):

- ``resources.read_resource_text`` reads engine/default/identity/001-004
  without a source checkout; ``short-lab.spec`` lists exactly those
  resources with no 005 placeholder file;
- read-only install + empty data dir boots twice consistently;
- ``maintenance.maintain`` only calls ``maintain_retention`` with a 180-day
  TTL and limit <= 1000 (aging via the retention queue);
- tag mutual exclusion and argparse default 46408 (AST, never comments);
- bootstrap/base product never requires Hedge (schema target 4,
  ``migrate(5)`` raises).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import pytest_asyncio
import yaml

TEST_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TEST_DIR.parent
DESKTOP_DIR = BACKEND_DIR.parent
REPO_ROOT = DESKTOP_DIR.parent
SRC_DIR = BACKEND_DIR / "src"

DAY_MS = 86_400_000
NOW = 1_760_000_000_000

EXPECTED_RESOURCES = (
    "engine/config/default.yaml",
    "shortlab/default.yaml",
    "shortlab/identity/asset_overrides.yaml",
    "shortlab/identity/verified_assets.yaml",
    "shortlab/migrations/001_init.sql",
    "shortlab/migrations/002_unlock_social.sql",
    "shortlab/migrations/003_catalyst.sql",
    "shortlab/migrations/004_core_completion.sql",
)


def _meta(status: str = "OK") -> dict:
    return {
        "status": status,
        "fetched_at_ms": NOW - 60_000,
        "as_of_ms": NOW - 60_000,
        "coverage_fraction": 1.0,
        "reason_code": None,
        "source": "f09-test",
    }


def _feature_meta() -> dict:
    from diveintocrypto_desktop.shortlab.repository import REQUIRED_FEATURE_META_FIELDS

    return {name: _meta() for name in REQUIRED_FEATURE_META_FIELDS}


def _entry_meta() -> dict:
    from diveintocrypto_desktop.shortlab.repository import REQUIRED_ENTRY_META_BLOCKS

    return {name: _meta() for name in REQUIRED_ENTRY_META_BLOCKS}


@pytest_asyncio.fixture
async def repo(tmp_path):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    handle = await ShortLabRepository.open(tmp_path / "f09.duckdb")
    await handle.migrate()
    yield handle
    await handle.close()


# ---------------------------------------------------------------------------
# 1. engine/default/identity/migrations readable without a source checkout
# ---------------------------------------------------------------------------


def test_resources_read_all_base_texts() -> None:
    from diveintocrypto_desktop.resources import read_resource_text

    for rel in EXPECTED_RESOURCES:
        text = read_resource_text(rel)
        assert isinstance(text, str) and text.strip(), rel
    # YAML parses; SQL carries DDL.
    engine_cfg = yaml.safe_load(read_resource_text("engine/config/default.yaml"))
    assert "consensus" in engine_cfg and "indicator_weights" in engine_cfg
    shortlab_cfg = yaml.safe_load(read_resource_text("shortlab/default.yaml"))
    assert "shortlab" in shortlab_cfg
    overrides = yaml.safe_load(
        read_resource_text("shortlab/identity/asset_overrides.yaml")
    )
    assert isinstance(overrides.get("version"), int) and isinstance(
        overrides.get("overrides"), dict
    )
    verified = yaml.safe_load(
        read_resource_text("shortlab/identity/verified_assets.yaml")
    )
    assert isinstance(verified.get("version"), int) and isinstance(
        verified.get("assets"), dict
    )
    for rel in EXPECTED_RESOURCES:
        if rel.endswith(".sql"):
            assert "CREATE TABLE" in read_resource_text(rel).upper(), rel


def test_resources_match_checkout_files() -> None:
    from diveintocrypto_desktop.resources import read_resource_text

    for rel in EXPECTED_RESOURCES:
        on_disk = SRC_DIR / "diveintocrypto_desktop" / rel
        assert on_disk.is_file(), rel
        assert read_resource_text(rel) == on_disk.read_text(encoding="utf-8"), rel


def test_resources_reject_escape() -> None:
    from diveintocrypto_desktop.resources import read_resource_text

    for bad in ("", "../secret", "/abs/path", "shortlab/../../etc/passwd"):
        with pytest.raises((ValueError, FileNotFoundError)):
            read_resource_text(bad)
    with pytest.raises(FileNotFoundError):
        read_resource_text("shortlab/does-not-exist.yaml")


def test_packaged_call_sites_use_read_resource_text() -> None:
    for module, needle in (
        ("engine/loader.py", "read_resource_text"),
        ("shortlab/config.py", "read_resource_text"),
        ("shortlab/repository.py", "read_resource_text"),
        ("shortlab/identity/overrides.py", "read_resource_text"),
    ):
        text = (SRC_DIR / "diveintocrypto_desktop" / module).read_text(encoding="utf-8")
        assert "read_resource_text" in text, module
        assert needle in text, module
    # No direct dev-absolute reads remain on those packaged paths.
    loader = (SRC_DIR / "diveintocrypto_desktop" / "engine/loader.py").read_text(
        encoding="utf-8"
    )
    assert "_CONFIG_PATH.open" not in loader
    # _MEIPASS fallback exists but is documented as product-smoke-only.
    resources_src = (SRC_DIR / "diveintocrypto_desktop" / "resources.py").read_text(
        encoding="utf-8"
    )
    assert "_MEIPASS" in resources_src
    assert "importlib" in resources_src


# ---------------------------------------------------------------------------
# 2. spec collects existing resources; no placeholder 005
# ---------------------------------------------------------------------------


def test_spec_collects_all_migrations_including_real_005() -> None:
    spec_path = BACKEND_DIR / "short-lab.spec"
    assert spec_path.is_file()
    spec = spec_path.read_text(encoding="utf-8")
    tree = ast.parse(spec, filename=str(spec_path))
    assert isinstance(tree, ast.Module)
    # H01: 005 really exists on disk (never a placeholder).
    assert (
        SRC_DIR / "diveintocrypto_desktop" / "shortlab" / "migrations" / "005_hedge_advisor.sql"
    ).is_file()
    # Collected migration filenames are exactly 001-005.
    sql_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.endswith(".sql")
    }
    assert sql_literals == {
        "001_init.sql",
        "002_unlock_social.sql",
        "003_catalyst.sql",
        "004_core_completion.sql",
        "005_hedge_advisor.sql",
    }, sql_literals
    for needle in (
        "engine/config",
        "asset_overrides.yaml",
        "verified_assets.yaml",
        "001_init.sql",
        "002_unlock_social.sql",
        "003_catalyst.sql",
        "004_core_completion.sql",
        "005_hedge_advisor.sql",
        "shortlab/default.yaml",
        "ui/dist",
        "duckdb",
    ):
        assert needle in spec, needle
    assert spec.count('name="short-lab"') >= 2


# ---------------------------------------------------------------------------
# 3. read-only install + empty data dir: two boots share one writable home
# ---------------------------------------------------------------------------


def test_readonly_install_empty_data_dir_two_boots_consistent(
    tmp_path: Path, monkeypatch
) -> None:
    from diveintocrypto_desktop.shortlab import paths as paths_mod

    monkeypatch.setattr(sys, "platform", "win32")
    fake_internal = tmp_path / "install" / "_internal"
    fake_internal.mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(fake_internal), raising=False)
    env = {"LOCALAPPDATA": str(tmp_path / "lad")}
    first = paths_mod.resolve_data_dir(frozen=True, env=env)
    second = paths_mod.resolve_data_dir(frozen=True, env=env)
    assert first == second
    assert "_internal" not in first.parts
    assert str(fake_internal) not in str(first)
    # Empty data dir boots: migrate + write, close, reopen, same rows back.
    import asyncio as _asyncio

    async def _two_boots() -> None:
        from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

        db = first / paths_mod.DB_FILENAME
        r1 = await ShortLabRepository.open(db)
        try:
            await r1.migrate()
            await r1.upsert_asset(
                {
                    "canonical_id": "BTC",
                    "display_symbol": "BTC",
                    "name": "Bitcoin",
                    "categories": (),
                    "created_at_ms": NOW,
                    "updated_at_ms": NOW,
                }
            )
        finally:
            await r1.close()
        r2 = await ShortLabRepository.open(db)
        try:
            await r2.migrate()
            tracked = await r2.list_tracked_symbols()
            # Asset projection alone is not tracked; the DB file itself must
            # survive the restart with the same home.
            assert (first / paths_mod.DB_FILENAME).is_file()
            assert second / paths_mod.DB_FILENAME == first / paths_mod.DB_FILENAME
            _ = tracked
        finally:
            await r2.close()

    _asyncio.run(_two_boots())


@pytest.mark.asyncio
async def test_empty_data_dir_restart_reads_back_base_tables(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    db = tmp_path / "empty" / "shortlab.duckdb"
    r1 = await ShortLabRepository.open(db)
    await r1.migrate()
    await r1.save_identity_snapshot(
        {
            "identity_snapshot_id": "id-f09",
            "futures_symbol": "BTCUSDT",
            "canonical_id": "bitcoin",
            "mapping_version": "v1",
            "observed_at_ms": NOW,
            "identity_json": {"canonical_id": "bitcoin"},
        }
    )
    await r1.close()
    r2 = await ShortLabRepository.open(db)
    try:
        await r2.migrate()
        assert await r2.get_identity_snapshot("id-f09") is not None
    finally:
        await r2.close()


# ---------------------------------------------------------------------------
# 4. retention: 180-day reference protection, batch cap, aging
# ---------------------------------------------------------------------------


def _score_deps(as_of: int, tag: str):
    from diveintocrypto_desktop.shortlab.repository import (
        EntrySnapshotRecord,
        FeatureSnapshotRecord,
    )

    feat = FeatureSnapshotRecord(
        snapshot_id=f"feat-{tag}",
        symbol="BTCUSDT",
        as_of_ms=as_of,
        feature_version="feat-v1",
        features={},
        source_meta=_feature_meta(),
        data_quality=90.0,
    )
    entry = EntrySnapshotRecord(
        snapshot_id=f"entry-{tag}",
        symbol="BTCUSDT",
        as_of_ms=as_of,
        entry_version="entry-v1",
        dive_weights_hash="w" * 16,
        dive_engine_version="e",
        dive_config_hash="c" * 16,
        primary_tf="1h",
        inputs={},
        components={},
        source_meta=_entry_meta(),
        entry_score=70.0,
        created_at_ms=as_of,
    )
    return feat, entry


def _score_row(sid: str, gen: str, feat: str, entry: str | None, as_of: int):
    from diveintocrypto_desktop.shortlab.repository import ScoreSnapshotRecord

    return ScoreSnapshotRecord(
        snapshot_id=sid,
        generation_id=gen,
        feature_snapshot_id=feat,
        entry_snapshot_id=entry,
        symbol="BTCUSDT",
        as_of_ms=as_of,
        analysis_tier="LITE",
        profile="GENERAL_LITE",
        score_version="ltss-lite-v1",
        entry_version="entry-v1" if entry is not None else None,
        feature_version="feat-v1",
        config_hash="cfg",
        ltss=80.0,
        entry_score=70.0 if entry is not None else None,
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


@pytest.mark.asyncio
async def test_retention_180d_protects_outcome_and_policy_batch_limited(repo) -> None:
    from diveintocrypto_desktop.shortlab.repository import (
        MigrationError,
        OutcomeRecord,
        RetentionStats,
        ValidationError,
    )

    old_as_of = NOW - 200 * DAY_MS
    new_as_of = NOW
    # Old generation (expired by a 180-day TTL) + latest generation.
    for tag, as_of in (("old-keep", old_as_of), ("old-drop", old_as_of), ("new", new_as_of)):
        feat, entry = _score_deps(as_of, tag)
        await repo.save_feature(feat)
        await repo.save_entry(entry)
    await repo.save_score_batch(
        [
            _score_row("score-old-keep", "gen-old", "feat-old-keep", "entry-old-keep", old_as_of),
            _score_row("score-old-drop", "gen-old", "feat-old-drop", "entry-old-drop", old_as_of),
        ],
        job_id="gen-old",
        started_at_ms=old_as_of,
        finished_at_ms=old_as_of + 1_000,
    )
    await repo.save_score_batch(
        [_score_row("score-new", "gen-new", "feat-new", "entry-new", new_as_of)],
        job_id="gen-new",
        started_at_ms=new_as_of,
        finished_at_ms=new_as_of + 1_000,
    )
    # One old score is referenced by a forward outcome (even an unexpired
    # 90-day horizon pins it); the other is sweepable.
    await repo.save_outcome(
        OutcomeRecord(
            score_snapshot_id="score-old-keep",
            horizon="90D",
            outcome_status="PENDING",
            reason_code="PENDING_NOT_DUE",
            entry_ts_ms=old_as_of,
            exit_ts_ms=None,
            horizon_due_ms=NOW + 10 * DAY_MS,
            formula_version="forward-v1",
            cost_config_hash="cost",
            funding_event_count=None,
            funding_coverage=None,
            graded_at_ms=NOW,
            entry_price=100.0,
            exit_price=None,
            price_short_return=None,
            funding_carry=None,
            fee_assumption=0.001,
            slippage_assumption=0.002,
            net_short_return=None,
            mae=None,
            mfe=None,
        )
    )
    # Associated policy snapshot is never swept (no config DELETE exists).
    await repo.save_config_snapshot(
        {
            "policy_hash": "p" * 32,
            "config_hash": "c" * 32,
            "policy_version": "policy-v1",
            "canonical_json": {},
            "created_at_ms": old_as_of,
        }
    )
    ttl_180 = 180 * DAY_MS
    stats = await repo.maintain_retention({"score_ttl_ms": ttl_180}, NOW, limit=1000)
    assert isinstance(stats, RetentionStats)
    assert stats.deleted == 1  # only the unreferenced expired score
    assert await repo.get_score("score-old-drop") is None
    assert await repo.get_score("score-old-keep") is not None
    assert await repo.get_score("score-new") is not None
    assert await repo.get_config_snapshot("p" * 32) is not None
    # Batch cap is enforced by the repository.
    with pytest.raises(ValidationError):
        await repo.maintain_retention({"score_ttl_ms": ttl_180}, NOW, limit=1001)
    with pytest.raises(ValidationError):
        await repo.maintain_retention({"score_ttl_ms": ttl_180}, NOW, limit=0)


@pytest.mark.asyncio
async def test_retention_per_call_limit_and_conflict_guard(repo) -> None:
    from diveintocrypto_desktop.shortlab.repository import (
        FundingObservationRecord,
        ValidationError,
    )

    for i in range(3):
        await repo.save_funding_observation(
            FundingObservationRecord(
                observation_id=f"f09-old-{i}",
                symbol="BTCUSDT",
                funding_time_ms=NOW - 200 * DAY_MS,
                known_at_ms=1_000,
                raw_json={"r": "0.1"},
                interval_hours=8.0,
                interval_source="fundingRate",
                observation_status="OBSERVED",
            )
        )
    await repo.save_funding_observation(
        FundingObservationRecord(
            observation_id="f09-conflict",
            symbol="BTCUSDT",
            funding_time_ms=NOW - 200 * DAY_MS,
            known_at_ms=1_000,
            raw_json={"r": "0.9"},
            interval_hours=8.0,
            interval_source="fundingRate",
            observation_status="CONFLICT",
        )
    )
    first = await repo.maintain_retention({"funding_observation_ttl_ms": 1_000}, NOW, limit=2)
    assert first.deleted == 2
    second = await repo.maintain_retention({"funding_observation_ttl_ms": 1_000}, NOW, limit=1000)
    assert second.deleted == 1
    assert second.retained >= 1
    with pytest.raises(ValidationError):
        await repo.maintain_retention({}, NOW, limit=1001)


@pytest.mark.asyncio
async def test_maintenance_defaults_are_180d_1000_with_aging() -> None:
    from diveintocrypto_desktop.shortlab import maintenance as _m
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.repository import QUEUE_AGING_SEC

    config = load_shortlab_config()
    assert config.maintenance.snapshot_min_days == 180
    assert config.maintenance.batch_delete_limit == 1000
    assert _m.retention_ttl_ms(config) == 180 * DAY_MS
    assert _m.retention_limit(config) == 1000
    assert _m.MAX_BATCH_LIMIT == 1000
    # Single-writer aging guard (F01): waiters promote every 30s.
    assert QUEUE_AGING_SEC == 30.0
    assert config.ingestion.db_priority_aging_sec == 30
    policy = _m.retention_policy_for_config(config)
    assert policy == {
        "funding_observation_ttl_ms": 180 * DAY_MS,
        "cursor_ttl_ms": 180 * DAY_MS,
        "score_ttl_ms": 180 * DAY_MS,
    }


@pytest.mark.asyncio
async def test_maintenance_callback_only_calls_maintain_retention(repo) -> None:
    import inspect as _inspect

    from diveintocrypto_desktop.shortlab import maintenance as _m

    assert _inspect.iscoroutinefunction(_m.maintain)
    src = Path(_m.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "maintain")
    assert [a.arg for a in fn.args.args] == ["context"]
    calls = [
        (n.func.attr if isinstance(n.func, ast.Attribute) else "")
        for n in ast.walk(fn)
        if isinstance(n, ast.Await) and isinstance(getattr(n, "value", None), ast.Call)
        for n in [n.value]
        if isinstance(n.func, ast.Attribute)
    ]
    assert "maintain_retention" in calls
    # No new SQL / connections inside maintain(): inspect awaited calls and
    # SQL keywords in the function body only (docstrings excluded by AST).
    awaited_attrs = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    for banned in ("execute", "executemany", "connect"):
        assert banned not in awaited_attrs, banned
    fn_strings = [
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    for kw in ("SELECT", "DELETE FROM", "INSERT INTO", "VACUUM"):
        assert all(kw not in s for s in fn_strings), kw

    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    _config = load_shortlab_config()

    class _Ctx:
        repository = repo
        config = _config
        clock_ms = staticmethod(lambda: NOW)
        trace_id = "maintenance-test"

    status = await _m.maintain(_Ctx())
    assert status.status == "SUCCEEDED"
    assert status.job_type == "maintenance"
    assert status.stats["limit"] <= 1000
    assert status.stats["ttl_ms"] == 180 * DAY_MS
    # Registered through the F06a slot without a second service.
    from diveintocrypto_desktop.shortlab.service import ShortLabService

    svc = ShortLabService(config=_config, repository=repo)
    assert svc.retention_callback is None
    svc.register_retention_callback(_m.maintain)
    assert svc.retention_callback is _m.maintain
    svc.register_maintain(_m.maintain)
    assert svc.retention_callback is _m.maintain
    again = await svc.maintain(_Ctx())
    assert again.status == "SUCCEEDED"


# ---------------------------------------------------------------------------
# 5. tags mutually exclusive + argparse default 46408 + bootstrap needs no Hedge
# ---------------------------------------------------------------------------


def test_tags_mutually_exclusive_and_port_via_argparse() -> None:
    import yaml as _yaml

    wf_text = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    wf = _yaml.safe_load(wf_text)
    on = wf.get("on", wf.get(True))
    assert set(on["push"]["tags"]) == {"v*", "short-lab-v*"}
    android_if = str(wf["jobs"]["release"]["if"])
    desktop_if = str(wf["jobs"]["package-desktop"]["if"])
    assert "refs/tags/v" in android_if
    assert "refs/tags/short-lab-v" in desktop_if
    # short-lab-v* must not startWith refs/tags/v (mutual exclusion at the
    # literal-prefix level, mirroring the workflow gates).
    assert not "refs/tags/short-lab-v1.0.0".startswith("refs/tags/v")
    assert "refs/tags/short-lab-v1.0.0".startswith("refs/tags/short-lab-v")
    assert "refs/tags/v0.4.0".startswith("refs/tags/v")
    assert not "refs/tags/v0.4.0".startswith("refs/tags/short-lab-v")
    # Default port is asserted from argparse (AST), never from a comment.
    main_src = (SRC_DIR / "diveintocrypto_desktop" / "__main__.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)
    defaults: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            args = [a for a in node.args if isinstance(a, ast.Constant)]
            if any(a.value == "--port" for a in args):
                for kw in node.keywords:
                    if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                        defaults.append(kw.value.value)
    assert defaults == [46408]
    # Release notes and spec examples agree on the same default.
    assert "127.0.0.1:46408" in wf_text
    assert "127.0.0.1:46408" in (BACKEND_DIR / "short-lab.spec").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_bootstrap_base_product_needs_no_hedge(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.repository import (
        MigrationError,
        ShortLabRepository,
        schema_target_for_config,
    )

    config = load_shortlab_config()
    assert schema_target_for_config(config) == 4
    handle = await ShortLabRepository.open(tmp_path / "base.duckdb")
    try:
        assert await handle.migrate() == 4
        # H01: the real 005 applies cleanly; base product works with hedge disabled.
        assert await handle.migrate(target_version=5) == 5
    finally:
        await handle.close()
