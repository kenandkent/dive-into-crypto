"""Task 2: DuckDB paths, schema and repository (Short-Lab Phase 1).

Covers ``ShortLab_Implementation_Plan_CN.md`` Task 2 and
``ShortLab_Detailed_Design_CN.md`` section 19 / 19.1-19.4:

- ``resolve_data_dir``: source default ``desktop/backend/runtime``,
  packaged Windows ``%LOCALAPPDATA%/short-lab`` (with ``sys._MEIPASS``
  simulated), ``SHORTLAB_DATA_DIR`` override with writability check and
  no ``_internal`` fallback.
- Empty-db / repeated migration, immutable ``snapshot_id``, same
  symbol/as-of scores under different configs coexisting, score references
  to feature + standalone Entry snapshots, four outcome states.
- ``source_meta_json`` per-field keys; writes missing READY/DQ metadata
  are rejected, legacy rows without it read back as UNAVAILABLE only.
- Same-``generation_id`` batch visible only after a complete write, with
  pagination; the section-19 query indexes exist; dangling
  feature/entry/score references are rejected; same score/horizon with a
  different cost hash coexists.
- Fault injection: a failing Nth score insert rolls back the whole score
  batch and the job SUCCEEDED marker (orphan Entry/feature snapshots may
  stay); success commits scores and job ``finished_at_ms``/``status``
  atomically.
- DDL matches design 19.2 field-by-field; migration failure surfaces as
  ``MigrationError``/``RepositoryError`` and never kills FastAPI.

All DB access is offline (tmp DuckDB files); no network.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab import paths as paths_mod
from diveintocrypto_desktop.shortlab.paths import (
    DataDirNotWritableError,
    resolve_data_dir,
)
from diveintocrypto_desktop.shortlab.repository import (
    REQUIRED_ENTRY_META_BLOCKS,
    REQUIRED_FEATURE_META_FIELDS,
    CandidatePage,
    EntrySnapshotRecord,
    FeatureSnapshotRecord,
    GenerationNotFoundError,
    MigrationError,
    OutcomeRecord,
    ReferenceNotFoundError,
    RepositoryError,
    ScoreSnapshotRecord,
    ShortLabRepository,
    SnapshotImmutableError,
    ValidationError,
)

AS_OF = 1760000000000
FETCHED = AS_OF - 60_000


# ---------------------------------------------------------------------------
# factories
# ---------------------------------------------------------------------------


def _meta_entry(status: str = "OK", source: str = "unit-test") -> dict:
    return {
        "status": status,
        "fetched_at_ms": FETCHED,
        "as_of_ms": AS_OF,
        "coverage_fraction": 1.0,
        "reason_code": None,
        "source": source,
    }


def _feature_meta(**overrides) -> dict:
    meta = {name: _meta_entry() for name in REQUIRED_FEATURE_META_FIELDS}
    meta.update(overrides)
    return meta


def _entry_meta(**overrides) -> dict:
    meta = {name: _meta_entry() for name in REQUIRED_ENTRY_META_BLOCKS}
    meta.update(overrides)
    return meta


def _feature(
    sid: str = "feat-1",
    symbol: str = "BTCUSDT",
    as_of: int = AS_OF,
    meta=None,
    dq: float = 88.5,
) -> FeatureSnapshotRecord:
    return FeatureSnapshotRecord(
        snapshot_id=sid,
        symbol=symbol,
        as_of_ms=as_of,
        feature_version="feat-v1",
        features={"funding_30d": 0.012, "ath_drawdown": -0.58},
        source_meta=meta if meta is not None else _feature_meta(),
        data_quality=dq,
    )


def _entry(
    sid: str = "entry-1",
    symbol: str = "BTCUSDT",
    as_of: int = AS_OF,
    score: float | None = 63.1,
    meta=None,
) -> EntrySnapshotRecord:
    return EntrySnapshotRecord(
        snapshot_id=sid,
        symbol=symbol,
        as_of_ms=as_of,
        entry_version="entry-v1",
        dive_weights_hash="w" * 16,
        dive_engine_version="dive-0.3.0",
        dive_config_hash="c" * 16,
        primary_tf="1h",
        inputs={"finalSignal": "SELL", "confidence": 70},
        components={"consensus": 21.0, "funding": 8.0},
        source_meta=meta if meta is not None else _entry_meta(),
        entry_score=score,
        created_at_ms=FETCHED,
    )


def _score(
    sid: str = "score-1",
    gen: str = "gen-1",
    feat: str = "feat-1",
    entry: str | None = "entry-1",
    symbol: str = "BTCUSDT",
    as_of: int = AS_OF,
    status: str = "CANDIDATE",
    candidate_status: str = "CANDIDATE",
    execution_status: str = "NOT_READY",
    ltss: float | None = 84.2,
    entry_score: float | None = 63.1,
    config_hash: str = "cfghash1",
    profile: str = "GENERAL_LITE",
) -> ScoreSnapshotRecord:
    return ScoreSnapshotRecord(
        snapshot_id=sid,
        generation_id=gen,
        feature_snapshot_id=feat,
        entry_snapshot_id=entry,
        symbol=symbol,
        as_of_ms=as_of,
        analysis_tier="LITE",
        profile=profile,
        score_version="ltss-lite-v1",
        entry_version="entry-v1" if entry is not None else None,
        feature_version="feat-v1",
        config_hash=config_hash,
        ltss=ltss,
        entry_score=entry_score,
        data_quality=88.5,
        candidate_status=candidate_status,
        execution_status=execution_status,
        status=status,
        module_scores={"lifecycle": 30.0, "carry": 38.2},
        vetoes=(),
        pauses=(),
        reasons=("ENTRY_BELOW_READY_THRESHOLD",),
        warnings=(),
    )


def _outcome(
    score_id: str = "score-1",
    horizon: str = "30D",
    status: str = "COMPLETE",
    cost: str = "costA",
) -> OutcomeRecord:
    return OutcomeRecord(
        score_snapshot_id=score_id,
        horizon=horizon,
        outcome_status=status,
        reason_code=None,
        entry_ts_ms=AS_OF,
        exit_ts_ms=AS_OF + 30 * 86_400_000,
        horizon_due_ms=AS_OF + 30 * 86_400_000,
        formula_version="grader-v1",
        cost_config_hash=cost,
        funding_event_count=90,
        funding_coverage=1.0,
        graded_at_ms=AS_OF + 31 * 86_400_000,
        entry_price=27000.0,
        exit_price=24000.0,
        price_short_return=0.1111,
        funding_carry=0.012,
        fee_assumption=0.001,
        slippage_assumption=0.002,
        net_short_return=0.1201,
        mae=0.05,
        mfe=0.14,
    )


@pytest_asyncio.fixture
async def repo(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "shortlab.duckdb")
    await handle.migrate()
    yield handle
    await handle.close()


# ---------------------------------------------------------------------------
# 1. paths
# ---------------------------------------------------------------------------


class TestResolveDataDir:
    def test_source_default_is_backend_runtime(self):
        resolved = resolve_data_dir(frozen=False, env={})
        expected = (
            Path(paths_mod.__file__).resolve().parent.parent.parent.parent / "runtime"
        )
        assert resolved == expected
        assert "_internal" not in resolved.parts

    def test_frozen_windows_uses_localappdata(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "_MEIPASS", r"C:\app\_internal", raising=False)
        env = {"LOCALAPPDATA": str(tmp_path / "Local")}
        resolved = resolve_data_dir(frozen=True, env=env)
        assert resolved == Path(env["LOCALAPPDATA"]) / "short-lab"
        assert "_internal" not in resolved.parts
        assert resolved.is_dir()

    def test_meipass_alone_counts_as_packaged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "_MEIPASS", r"C:\readonly\_internal", raising=False)
        env = {"LOCALAPPDATA": str(tmp_path / "Local")}
        resolved = resolve_data_dir(frozen=False, env=env)
        assert resolved == Path(env["LOCALAPPDATA"]) / "short-lab"

    def test_env_override_wins_and_is_writable(self, tmp_path):
        target = tmp_path / "custom-data"
        resolved = resolve_data_dir(
            frozen=True, env={"SHORTLAB_DATA_DIR": str(target)}
        )
        assert resolved == target
        assert target.is_dir()
        assert "_internal" not in resolved.parts
        # probe file must be cleaned up
        assert list(target.iterdir()) == []

    def test_env_override_inside_internal_rejected(self, tmp_path):
        bad = tmp_path / "_internal" / "data"
        with pytest.raises(DataDirNotWritableError):
            resolve_data_dir(frozen=False, env={"SHORTLAB_DATA_DIR": str(bad)})

    def test_env_override_unwritable_rejected(self, tmp_path):
        blocker = tmp_path / "file-not-dir"
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(DataDirNotWritableError):
            resolve_data_dir(frozen=False, env={"SHORTLAB_DATA_DIR": str(blocker)})

    def test_frozen_posix_stays_in_user_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        if hasattr(sys, "_MEIPASS"):
            monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        if getattr(sys, "frozen", False):
            monkeypatch.delattr(sys, "frozen", raising=False)
        env = {"HOME": str(tmp_path / "home")}
        resolved = resolve_data_dir(frozen=True, env=env)
        assert resolved == Path(env["HOME"]) / "Library" / "Application Support" / "short-lab"
        assert resolved.is_dir()


# ---------------------------------------------------------------------------
# frozen DTOs are consumed, not redefined
# ---------------------------------------------------------------------------


def test_repository_consumes_frozen_dtos_without_redefining():
    import diveintocrypto_desktop.shortlab.repository as repo_mod
    from diveintocrypto_desktop.shortlab import models

    # Consumed by import (identical objects), never redefined in repository.py.
    assert repo_mod.CandidateState is models.CandidateState
    assert "class CandidateState" not in Path(repo_mod.__file__).read_text()
    assert "class ProviderResult" not in Path(repo_mod.__file__).read_text()


def test_public_db_methods_are_async_and_single_worker(repo):
    public = [
        "open", "close", "migrate", "save_feature", "save_entry", "save_score",
        "save_score_batch", "list_candidates", "save_outcome", "get_feature",
        "get_entry", "get_score", "get_outcome", "create_job_run",
        "finish_job_run", "get_job_run",
    ]
    for name in public:
        assert asyncio.iscoroutinefunction(getattr(ShortLabRepository, name)), name
    assert repo._executor._max_workers == 1


# ---------------------------------------------------------------------------
# 2. migrate / empty db / repeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migrate_empty_then_repeat_preserves_data(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "fresh.duckdb")
    try:
        assert await handle.migrate() == 1
        await handle.save_feature(_feature())
        assert await handle.migrate() == 1  # repeat: no-op, no wipe
        stored = await handle.get_feature("feat-1")
        assert stored is not None and stored.symbol == "BTCUSDT"
    finally:
        await handle.close()


@pytest.mark.asyncio
async def test_migration_failure_never_kills_fastapi(tmp_path):
    # A directory is not a DuckDB file: open must fail as a plain error.
    with pytest.raises(RepositoryError):
        await ShortLabRepository.open(tmp_path)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def lifespan(app: FastAPI):
        try:
            app.state.repo = await ShortLabRepository.open(tmp_path)
        except RepositoryError:
            app.state.repo = None
            app.state.shortlab_available = False
        else:  # pragma: no cover - open above already proved failure
            app.state.shortlab_available = True
        yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"shortlab_available": app.state.shortlab_available}

    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"shortlab_available": False}


# ---------------------------------------------------------------------------
# immutability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_id_immutable(repo):
    await repo.save_feature(_feature())
    await repo.save_feature(_feature())  # identical rewrite: idempotent
    altered = _feature(dq=10.0)
    with pytest.raises(SnapshotImmutableError):
        await repo.save_feature(altered)
    await repo.save_entry(_entry())
    with pytest.raises(SnapshotImmutableError):
        await repo.save_entry(_entry(score=1.0))
    await repo.save_score(_score())
    with pytest.raises(SnapshotImmutableError):
        await repo.save_score(_score(ltss=1.0))


# ---------------------------------------------------------------------------
# score references
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_score_rejects_dangling_references(repo):
    with pytest.raises(ReferenceNotFoundError):
        await repo.save_score(_score())  # no feature yet
    await repo.save_feature(_feature())
    with pytest.raises(ReferenceNotFoundError):
        await repo.save_score(_score())  # entry-1 missing
    with pytest.raises(ReferenceNotFoundError):
        await repo.save_score(_score(entry="no-such-entry"), entry_snapshot_id=None)
    await repo.save_entry(_entry())
    # symbol mismatch against the referenced feature
    with pytest.raises(ValidationError):
        await repo.save_score(_score(symbol="ETHUSDT"))
    # as_of mismatch against the referenced entry
    other_entry = _entry(sid="entry-2", as_of=AS_OF - 1)
    await repo.save_entry(other_entry)
    with pytest.raises(ValidationError):
        await repo.save_score(_score(entry="entry-2"))
    # null entry reference requires null entry_score/entry_version
    with pytest.raises(ValidationError):
        await repo.save_score(_score(entry=None, entry_score=63.1))
    with pytest.raises(ValidationError):
        await repo.save_score(_score(entry=None))
    ok_id = await repo.save_score(
        _score(entry=None, entry_score=None), entry_snapshot_id=None
    )
    assert ok_id == "score-1"


@pytest.mark.asyncio
async def test_save_score_entry_param_must_agree(repo):
    await repo.save_feature(_feature())
    await repo.save_entry(_entry())
    with pytest.raises(ValidationError):
        await repo.save_score(_score(entry="entry-1"), entry_snapshot_id="entry-X")
    assert await repo.save_score(_score(entry="entry-1"), entry_snapshot_id="entry-1")


@pytest.mark.asyncio
async def test_same_symbol_asof_different_configs_coexist(repo):
    await repo.save_feature(_feature())
    await repo.save_entry(_entry())
    scores = [
        _score(sid="score-a", gen="gen-multi", config_hash="cfg-1", profile="MEME_LITE"),
        _score(sid="score-b", gen="gen-multi", config_hash="cfg-2", profile="GENERAL_LITE"),
    ]
    await repo.save_score_batch(
        scores, job_id="gen-multi", started_at_ms=FETCHED, finished_at_ms=AS_OF
    )
    page = await repo.list_candidates(generation_id="gen-multi")
    assert page.total == 2
    assert {item.snapshot_id for item in page.items} == {"score-a", "score-b"}


# ---------------------------------------------------------------------------
# source_meta discipline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_write_rejects_missing_required_meta(repo):
    meta = _feature_meta()
    del meta["funding_30d"]
    with pytest.raises(ValidationError):
        await repo.save_feature(_feature(meta=meta))
    meta = _feature_meta()
    del meta["oi_usd"]["coverage_fraction"]
    with pytest.raises(ValidationError):
        await repo.save_feature(_feature(sid="feat-x", meta=meta))
    meta = _feature_meta()
    meta["market_cap"]["status"] = "BROKEN"
    with pytest.raises(ValidationError):
        await repo.save_feature(_feature(sid="feat-y", meta=meta))


@pytest.mark.asyncio
async def test_entry_write_rejects_missing_block_meta(repo):
    meta = _entry_meta()
    del meta["micro"]
    with pytest.raises(ValidationError):
        await repo.save_entry(_entry(meta=meta))
    meta = _entry_meta()
    meta["funding"]["source"] = ""
    with pytest.raises(ValidationError):
        await repo.save_entry(_entry(sid="entry-x", meta=meta))


@pytest.mark.asyncio
async def test_legacy_row_without_meta_reads_unavailable_only(tmp_path):
    import duckdb

    db_path = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "CREATE TABLE sl_feature_snapshot (snapshot_id VARCHAR PRIMARY KEY,"
            " symbol VARCHAR NOT NULL, as_of_ms BIGINT NOT NULL,"
            " fundamental_snapshot_id VARCHAR, feature_version VARCHAR NOT NULL,"
            " features_json VARCHAR NOT NULL, source_meta_json VARCHAR NOT NULL,"
            " data_quality DOUBLE NOT NULL)"
        )
        con.execute(
            "INSERT INTO sl_feature_snapshot VALUES "
            "('legacy-1', 'BTCUSDT', ?, NULL, 'feat-v0', '{\"a\":1}', '{}', 50.0)",
            [AS_OF],
        )
    finally:
        con.close()
    repo = await ShortLabRepository.open(db_path)
    try:
        await repo.migrate()
        stored = await repo.get_feature("legacy-1")
        assert stored is not None
        assert await repo.feature_source_status("legacy-1") == "UNAVAILABLE"
        assert await repo.save_feature(_feature()) == "feat-1"
        assert await repo.feature_source_status("feat-1") == "OK"
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# outcomes: four states + cost-hash coexistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcome_four_states_and_cost_variants(repo):
    await repo.save_feature(_feature())
    await repo.save_entry(_entry())
    await repo.save_score(_score(entry=None, entry_score=None), entry_snapshot_id=None)
    # fix: score-1 above has no entry; outcomes key on the score only
    await repo.save_outcome(_outcome(horizon="7D", status="PENDING", cost="costA"))
    await repo.save_outcome(_outcome(horizon="30D", status="COMPLETE", cost="costA"))
    await repo.save_outcome(_outcome(horizon="90D", status="CENSORED", cost="costA"))
    await repo.save_outcome(_outcome(horizon="7D", status="UNAVAILABLE", cost="costB"))
    got = await repo.get_outcome("score-1", "7D", "grader-v1", "costB")
    assert got is not None and got.outcome_status == "UNAVAILABLE"
    assert got.reason_code is None
    all_rows = await repo.list_outcomes_for_score("score-1")
    assert len(all_rows) == 4
    assert {row.outcome_status for row in all_rows} == {
        "PENDING", "COMPLETE", "CENSORED", "UNAVAILABLE",
    }
    with pytest.raises(ReferenceNotFoundError):
        await repo.save_outcome(_outcome(score_id="no-such-score"))
    with pytest.raises(ValidationError):
        await repo.save_outcome(_outcome(status="DONE"))
    with pytest.raises(ValidationError):
        await repo.save_outcome(_outcome(horizon="1D"))
    # same full key rewritten differently is immutable
    with pytest.raises(SnapshotImmutableError):
        await repo.save_outcome(_outcome(horizon="7D", status="COMPLETE", cost="costA"))


# ---------------------------------------------------------------------------
# generation visibility, pagination, ordering
# ---------------------------------------------------------------------------


async def _seed_generation(repo, gen: str, symbols: list[str], finished: int) -> None:
    scores = []
    for i, symbol in enumerate(symbols):
        feat_id, entry_id = f"{gen}-feat-{i}", f"{gen}-entry-{i}"
        await repo.save_feature(_feature(sid=feat_id, symbol=symbol))
        await repo.save_entry(_entry(sid=entry_id, symbol=symbol))
        scores.append(_score(sid=f"{gen}-score-{i}", gen=gen, feat=feat_id, entry=entry_id, symbol=symbol))
    await repo.save_score_batch(
        scores, job_id=gen, started_at_ms=FETCHED, finished_at_ms=finished,
        stats={"symbols": len(symbols)},
    )


@pytest.mark.asyncio
async def test_batch_visible_only_when_complete_and_paginates(repo):
    await _seed_generation(repo, "gen-1", ["BTCUSDT", "ETHUSDT", "SOLUSDT"], AS_OF)
    page = await repo.list_candidates(generation_id="gen-1", limit=2, offset=0)
    assert isinstance(page, CandidatePage)
    assert page.total == 3 and page.generation_id == "gen-1"
    assert len(page.items) == 2
    page2 = await repo.list_candidates(generation_id="gen-1", limit=2, offset=2)
    assert len(page2.items) == 1
    assert page2.generation_id == "gen-1"
    # incomplete batch (single rows, no SUCCEEDED job) never leaks
    await repo.save_feature(_feature(sid="feat-half", symbol="DOGEUSDT"))
    await repo.save_entry(_entry(sid="entry-half", symbol="DOGEUSDT"))
    await repo.save_score(
        _score(sid="score-half", gen="gen-half", feat="feat-half", entry="entry-half", symbol="DOGEUSDT")
    )
    latest = await repo.list_candidates()
    assert latest.generation_id == "gen-1"
    assert all(item.symbol != "DOGEUSDT" for item in latest.items)
    with pytest.raises(GenerationNotFoundError):
        await repo.list_candidates(generation_id="gen-half")
    with pytest.raises(GenerationNotFoundError):
        await repo.list_candidates(generation_id="no-such-gen")


@pytest.mark.asyncio
async def test_default_order_and_explicit_sort(repo):
    await repo.save_feature(_feature(sid="f-b", symbol="BBB"))
    await repo.save_feature(_feature(sid="f-a", symbol="AAA"))
    await repo.save_feature(_feature(sid="f-c", symbol="CCC"))
    await repo.save_feature(_feature(sid="f-n", symbol="NNN"))
    await repo.save_entry(_entry(sid="e-b", symbol="BBB"))
    await repo.save_entry(_entry(sid="e-a", symbol="AAA"))
    await repo.save_entry(_entry(sid="e-c", symbol="CCC"))
    await repo.save_entry(_entry(sid="e-n", symbol="NNN"))
    scores = [
        _score(sid="s-watch", gen="gen-ord", feat="f-b", entry="e-b", symbol="BBB",
               status="WATCH", candidate_status="WATCH", ltss=65.0),
        _score(sid="s-ready", gen="gen-ord", feat="f-a", entry="e-a", symbol="AAA",
               status="READY", candidate_status="CANDIDATE", execution_status="READY", ltss=90.0),
        _score(sid="s-excl", gen="gen-ord", feat="f-c", entry="e-c", symbol="CCC",
               status="EXCLUDED", candidate_status="EXCLUDED", ltss=10.0),
        _score(sid="s-null", gen="gen-ord", feat="f-n", entry="e-n", symbol="NNN",
               status="CANDIDATE", candidate_status="CANDIDATE", ltss=None, entry_score=None),
    ]
    await repo.save_score_batch(
        scores, job_id="gen-ord", started_at_ms=FETCHED, finished_at_ms=AS_OF
    )
    page = await repo.list_candidates(generation_id="gen-ord", limit=10)
    assert [item.symbol for item in page.items] == ["AAA", "NNN", "BBB", "CCC"]
    asc = await repo.list_candidates(
        generation_id="gen-ord", limit=10, sort="ltss", order="asc"
    )
    assert [item.symbol for item in asc.items][-1] == "NNN"  # nulls last
    assert asc.items[0].symbol == "CCC"
    only_ready = await repo.list_candidates(generation_id="gen-ord", status="READY")
    assert only_ready.total == 1 and only_ready.items[0].symbol == "AAA"
    with pytest.raises(ValidationError):
        await repo.list_candidates(generation_id="gen-ord", limit=0)
    with pytest.raises(ValidationError):
        await repo.list_candidates(generation_id="gen-ord", offset=-1)
    with pytest.raises(ValidationError):
        await repo.list_candidates(generation_id="gen-ord", sort="funding30d")
    with pytest.raises(ValidationError):
        await repo.list_candidates(generation_id="gen-ord", status="STALE")


@pytest.mark.asyncio
async def test_latest_generation_prefers_newest_then_smallest_job_id(repo):
    await _seed_generation(repo, "gen-old", ["BTCUSDT"], AS_OF - 1000)
    await _seed_generation(repo, "gen-new", ["ETHUSDT"], AS_OF)
    assert await repo.latest_completed_generation() == "gen-new"
    # same finished_at_ms: smaller job_id wins (finished_at_ms DESC, job_id ASC)
    await repo.create_job_run("job-b", "score_refresh", FETCHED)
    await repo.finish_job_run("job-b", "SUCCEEDED", AS_OF)
    await repo.create_job_run("job-a", "score_refresh", FETCHED)
    await repo.finish_job_run("job-a", "SUCCEEDED", AS_OF)
    assert await repo.latest_completed_generation() == "gen-new"  # newer ts still wins
    await repo.finish_job_run("gen-old", "SUCCEEDED", AS_OF)  # tie at AS_OF now
    assert await repo.latest_completed_generation() == "gen-new"  # 'gen-new' < 'job-*'


# ---------------------------------------------------------------------------
# transaction fault injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_batch_rolls_back_scores_and_job(repo, monkeypatch):
    await _seed_generation(repo, "gen-good", ["BTCUSDT"], AS_OF - 2000)
    await repo.save_feature(_feature(sid="n-feat-0", symbol="AA"))
    await repo.save_feature(_feature(sid="n-feat-1", symbol="BB"))
    await repo.save_feature(_feature(sid="n-feat-2", symbol="CC"))
    await repo.save_entry(_entry(sid="n-entry-0", symbol="AA"))
    await repo.save_entry(_entry(sid="n-entry-1", symbol="BB"))
    await repo.save_entry(_entry(sid="n-entry-2", symbol="CC"))
    batch = [
        _score(sid="n-score-0", gen="gen-bad", feat="n-feat-0", entry="n-entry-0", symbol="AA"),
        _score(sid="n-score-1", gen="gen-bad", feat="n-feat-1", entry="n-entry-1", symbol="BB"),
        _score(sid="n-score-2", gen="gen-bad", feat="n-feat-2", entry="n-entry-2", symbol="CC"),
    ]
    calls = {"n": 0}
    original = repo._insert_score_row_sync

    def flaky(con, snapshot):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("injected score write failure")
        return original(con, snapshot)

    monkeypatch.setattr(repo, "_insert_score_row_sync", flaky)
    with pytest.raises(RuntimeError, match="injected score write failure"):
        await repo.save_score_batch(
            batch, job_id="gen-bad", started_at_ms=FETCHED, finished_at_ms=AS_OF
        )
    # whole batch + SUCCEEDED marker rolled back; old generation still serves
    assert await repo.get_score("n-score-0") is None
    assert await repo.get_score("n-score-1") is None
    job = await repo.get_job_run("gen-bad")
    assert job is None or job["status"] != "SUCCEEDED"
    latest = await repo.list_candidates()
    assert latest.generation_id == "gen-good"
    # orphan Entry/feature snapshots may stay (not candidates)
    assert await repo.get_feature("n-feat-0") is not None
    assert await repo.get_entry("n-entry-0") is not None


@pytest.mark.asyncio
async def test_successful_batch_commits_scores_and_job_atomically(repo):
    await repo.save_feature(_feature(sid="s-feat", symbol="BTCUSDT"))
    await repo.save_entry(_entry(sid="s-entry", symbol="BTCUSDT"))
    scores = [
        _score(sid="s-score-0", gen="gen-ok", feat="s-feat", entry="s-entry"),
        _score(sid="s-score-1", gen="gen-ok", feat="s-feat", entry="s-entry"),
    ]
    returned = await repo.save_score_batch(
        scores, job_id="gen-ok", started_at_ms=FETCHED, finished_at_ms=AS_OF,
        stats={"ok": 2},
    )
    assert returned == "gen-ok"
    job = await repo.get_job_run("gen-ok")
    assert job is not None
    assert job["status"] == "SUCCEEDED" and job["finished_at_ms"] == AS_OF
    page = await repo.list_candidates(generation_id="gen-ok")
    assert page.total == 2
    with pytest.raises(ValidationError):
        await repo.save_score_batch(
            [], job_id="gen-empty", started_at_ms=FETCHED, finished_at_ms=AS_OF
        )


# ---------------------------------------------------------------------------
# concurrent serialisation on the single worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_writes_serialize_on_single_worker(repo):
    ids = await asyncio.gather(
        *(repo.save_feature(_feature(sid=f"c-feat-{i}", symbol=f"S{i}")) for i in range(20))
    )
    assert sorted(ids) == sorted(f"c-feat-{i}" for i in range(20))
    assert await repo.get_feature("c-feat-19") is not None


# ---------------------------------------------------------------------------
# DDL vs design 19.2 + index inventory
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS = {
    "sl_schema_version": ["version", "applied_at_ms"],
    "sl_asset": ["canonical_id", "display_symbol", "name", "categories_json",
                 "created_at_ms", "updated_at_ms"],
    "sl_asset_mapping": ["futures_symbol", "canonical_id", "spot_symbol",
                         "contract_multiplier", "multiplier_source", "coingecko_id",
                         "unlock_provider_id", "social_provider_id",
                         "mapping_confidence", "mapping_source", "updated_at_ms"],
    "sl_contract_lifecycle": ["futures_symbol", "observed_at_ms", "onboard_at_ms",
                              "first_seen_ms", "delivery_at_ms", "contract_type",
                              "exchange_status", "contract_multiplier",
                              "multiplier_source"],
    "sl_funding_event": ["symbol", "funding_time_ms", "funding_rate", "mark_price"],
    "sl_fundamental_snapshot": ["snapshot_id", "canonical_id", "as_of_ms",
                                "fetched_at_ms", "market_cap_usd", "fdv_usd",
                                "circulating_supply", "total_supply", "max_supply",
                                "ath_price", "ath_date_ms", "source"],
    "sl_feature_snapshot": ["snapshot_id", "symbol", "as_of_ms",
                            "fundamental_snapshot_id", "feature_version",
                            "features_json", "source_meta_json", "data_quality"],
    "sl_entry_snapshot": ["snapshot_id", "symbol", "as_of_ms", "entry_version",
                          "dive_weights_hash", "dive_engine_version",
                          "dive_config_hash", "primary_tf", "inputs_json",
                          "components_json", "source_meta_json", "entry_score",
                          "created_at_ms"],
    "sl_score_snapshot": ["snapshot_id", "generation_id", "feature_snapshot_id",
                          "entry_snapshot_id", "symbol", "as_of_ms",
                          "analysis_tier", "profile", "score_version",
                          "entry_version", "feature_version", "config_hash", "ltss",
                          "entry_score", "data_quality", "candidate_status",
                          "execution_status", "status", "module_scores_json",
                          "vetoes_json", "pauses_json", "reasons_json",
                          "warnings_json"],
    "sl_forward_outcome": ["score_snapshot_id", "horizon", "outcome_status",
                           "reason_code", "entry_ts_ms", "exit_ts_ms",
                           "horizon_due_ms", "formula_version", "cost_config_hash",
                           "funding_event_count", "funding_coverage",
                           "graded_at_ms", "entry_price", "exit_price",
                           "price_short_return", "funding_carry", "fee_assumption",
                           "slippage_assumption", "net_short_return", "mae", "mfe"],
    "sl_job_run": ["job_id", "job_type", "started_at_ms", "finished_at_ms",
                   "status", "stats_json", "error_code"],
}

EXPECTED_INDEXES = [
    "idx_sl_score_generation",
    "idx_sl_score_symbol_time",
    "idx_sl_feature_symbol_time",
    "idx_sl_entry_symbol_time",
    "idx_sl_funding_symbol_time",
    "idx_sl_outcome_status_due",
]


def _migration_sql() -> str:
    from diveintocrypto_desktop.shortlab import repository as repo_mod

    path = Path(repo_mod.__file__).resolve().parent / "migrations" / "001_init.sql"
    return path.read_text(encoding="utf-8")


def test_migration_sql_matches_design_19_2_field_by_field():
    sql = _migration_sql()
    found: dict[str, list[str]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\);", sql, re.S
    ):
        table, body = match.group(1), match.group(2)
        columns = []
        for part in re.split(r",\s*\n", body):
            name = part.strip().split()[0]
            if name in ("PRIMARY", "FOREIGN", "CONSTRAINT", "UNIQUE", "CHECK"):
                continue
            columns.append(name)
        found[table] = columns
    assert set(found) == set(EXPECTED_COLUMNS), set(found) ^ set(EXPECTED_COLUMNS)
    for table, columns in EXPECTED_COLUMNS.items():
        assert found[table] == columns, table
    # composite primary keys from design 19.2
    assert "PRIMARY KEY(futures_symbol, observed_at_ms)" in sql
    assert "PRIMARY KEY(symbol, funding_time_ms)" in sql
    assert (
        "PRIMARY KEY(score_snapshot_id, horizon, formula_version, cost_config_hash)"
        in sql
    )
    for index in EXPECTED_INDEXES:
        assert index in sql, index


@pytest.mark.asyncio
async def test_runtime_tables_and_indexes_match_design(repo):
    names = await repo.index_names()
    for index in EXPECTED_INDEXES:
        assert index in names, index
