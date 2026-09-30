"""Short-Lab DuckDB repository (design section 19).

Embedded, in-process persistence: one DuckDB file, one single-worker
``ThreadPoolExecutor`` so every public method can be ``async`` without
blocking the FastAPI event loop. Writes of one ``score_refresh``
generation (all score rows plus the job's ``SUCCEEDED``/``finished_at_ms``)
commit in a single transaction; Entry/feature snapshots persist first as
immutable facts and may stay orphaned when a later batch rolls back.

Point-in-time rules enforced here (not in business scoring code):

- ``snapshot_id`` is immutable: rewriting the same id with different
  content raises :class:`SnapshotImmutableError`.
- The same ``symbol``/``as_of_ms`` may carry many score rows (different
  profile/config/re-run); rows never overwrite each other.
- ``save_score`` requires the referenced feature snapshot to exist with
  matching ``symbol``/``as_of_ms``; a non-null ``entry_snapshot_id``
  additionally requires that Entry snapshot with matching
  ``symbol``/``as_of_ms``. A null ``entry_snapshot_id`` requires
  ``entry_score``/``entry_version`` to be null as well.
- ``save_feature``/``save_entry`` reject snapshots whose ``source_meta``
  misses a READY/DQ-required field; legacy rows read back without that
  metadata report ``UNAVAILABLE`` and must never be projected as READY.
- ``save_outcome`` requires the referenced score snapshot; outcomes are
  keyed ``(score_snapshot_id, horizon, formula_version, cost_config_hash)``
  so cost variants coexist.
- Candidate pagination only reads generations whose ``sl_job_run`` is
  ``SUCCEEDED``; running/failed/half-written batches stay invisible.

Migration failure raises :class:`MigrationError` (a plain exception the
FastAPI lifespan maps to "Short-Lab unavailable"); it never terminates
the host process.

``ProviderResult``/``CandidateState`` are imported from Task 1's frozen
``models.py`` and never redefined here.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from typing import Self

from diveintocrypto_desktop.shortlab.models import CandidateState
from diveintocrypto_desktop.shortlab.paths import DB_FILENAME, resolve_db_path

try:  # DuckDB is a hard dependency (pyproject); keep module importable without it.
    import duckdb as _duckdb
except Exception as _duckdb_import_error:  # pragma: no cover - import-time guard
    _duckdb = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
SCHEMA_MIGRATION = "001_init.sql"

_JOB_TYPE_SCORE_REFRESH = "score_refresh"

_REQUIRED_META_KEYS = frozenset(
    {"status", "fetched_at_ms", "as_of_ms", "coverage_fraction", "reason_code", "source"}
)
_ALLOWED_SOURCE_STATUS = frozenset({"OK", "PARTIAL", "NOT_APPLICABLE", "UNAVAILABLE", "ERROR"})

# READY/DQ-required feature source fields (design section 18, LITE groups).
# Presence is enforced on write; UNAVAILABLE entries are allowed (they lower
# DQ downstream) but missing keys are rejected.
REQUIRED_FEATURE_META_FIELDS = frozenset(
    {
        "market_daily",
        "futures_volume_24h",
        "oi_usd",
        "contract_status",
        "funding_7d",
        "funding_30d",
        "funding_90d",
        "market_cap",
        "fdv",
        "supply_float",
        "ath",
        "spot_volume_60d",
        "spot_volume_24h",
        "basis",
        "orderbook_depth",
        "identity",
        "profile",
    }
)

# Entry building blocks (design sections 15/24: entry_required_blocks).
REQUIRED_ENTRY_META_BLOCKS = frozenset(
    {"consensus", "mtf", "micro", "regime", "failed_bounce", "funding"}
)

ALLOWED_OUTCOME_STATUS = frozenset({"PENDING", "COMPLETE", "CENSORED", "UNAVAILABLE"})
ALLOWED_HORIZONS = frozenset({"7D", "30D", "90D"})
ALLOWED_ANALYSIS_TIERS = frozenset({"LITE", "FULL"})
ALLOWED_JOB_STATUS = frozenset({"RUNNING", "SUCCEEDED", "FAILED"})
_ALLOWED_DISPLAY_STATUS = frozenset({"EXCLUDED", "WATCH", "CANDIDATE", "READY", "PAUSED", "BLOCKED"})
_ALLOWED_CANDIDATE_SORTS = frozenset({"ltss", "entry_score", "data_quality"})

_STATUS_RANK = {"READY": 0, "CANDIDATE": 1, "WATCH": 2, "PAUSED": 3, "BLOCKED": 4, "EXCLUDED": 5}


class RepositoryError(Exception):
    """Base Short-Lab repository failure; never fatal to the host process."""


class MigrationError(RepositoryError):
    """Schema migration failed; Short-Lab stays unavailable, app keeps running."""


class ValidationError(RepositoryError, ValueError):
    """Caller-side payload error (bad meta, bad enum, bad pagination, ...)."""


class SnapshotImmutableError(RepositoryError):
    """Same snapshot_id rewritten with different content."""


class ReferenceNotFoundError(RepositoryError, LookupError):
    """A referenced feature/entry/score snapshot (or generation) is missing."""


class GenerationNotFoundError(ReferenceNotFoundError):
    """No visible (SUCCEEDED) score generation for the requested id."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _parse_json_dict(raw: Any, column: str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"column {column} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError(f"column {column} must hold a JSON object")
    return parsed


def _parse_json_tuple(raw: Any, column: str) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple)):
        items = tuple(raw)
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"column {column} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValidationError(f"column {column} must hold a JSON array")
        items = tuple(parsed)
    for item in items:
        if not isinstance(item, str):
            raise ValidationError(f"column {column} must hold an array of str")
    return items


def _check_source_meta(
    source_meta: Mapping[str, Any], required: frozenset[str], *, what: str
) -> None:
    if not isinstance(source_meta, Mapping):
        raise ValidationError(f"{what}: source_meta must be a field-keyed mapping")
    missing = sorted(required - set(source_meta.keys()))
    if missing:
        raise ValidationError(f"{what}: source_meta misses READY/DQ fields: {missing}")
    for field_name in sorted(required):
        entry = source_meta[field_name]
        if not isinstance(entry, Mapping):
            raise ValidationError(f"{what}: source_meta[{field_name!r}] must be a mapping")
        absent = sorted(_REQUIRED_META_KEYS - set(entry.keys()))
        if absent:
            raise ValidationError(
                f"{what}: source_meta[{field_name!r}] misses keys: {absent}"
            )
        status = entry["status"]
        if status not in _ALLOWED_SOURCE_STATUS:
            raise ValidationError(
                f"{what}: source_meta[{field_name!r}].status={status!r} is not a "
                "ProviderResult status"
            )
        if not isinstance(entry["source"], str) or not entry["source"]:
            raise ValidationError(
                f"{what}: source_meta[{field_name!r}].source must be a non-empty str"
            )


def _source_meta_complete(source_meta: Mapping[str, Any], required: frozenset[str]) -> bool:
    try:
        _check_source_meta(source_meta, required, what="read")
    except ValidationError:
        return False
    return True


# ---------------------------------------------------------------------------
# Immutable snapshot records (frozen dataclasses; raw values only, no API JSON)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureSnapshotRecord:
    snapshot_id: str
    symbol: str
    as_of_ms: int
    feature_version: str
    features: Mapping[str, Any]
    source_meta: Mapping[str, Mapping[str, Any]]
    data_quality: float
    fundamental_snapshot_id: str | None = None


@dataclass(frozen=True)
class EntrySnapshotRecord:
    snapshot_id: str
    symbol: str
    as_of_ms: int
    entry_version: str
    dive_weights_hash: str
    dive_engine_version: str
    dive_config_hash: str
    primary_tf: str
    inputs: Mapping[str, Any]
    components: Mapping[str, Any]
    source_meta: Mapping[str, Mapping[str, Any]]
    entry_score: float | None
    created_at_ms: int


@dataclass(frozen=True)
class ScoreSnapshotRecord:
    snapshot_id: str
    generation_id: str
    feature_snapshot_id: str
    entry_snapshot_id: str | None
    symbol: str
    as_of_ms: int
    analysis_tier: str
    profile: str
    score_version: str
    entry_version: str | None
    feature_version: str
    config_hash: str
    ltss: float | None
    entry_score: float | None
    data_quality: float
    candidate_status: str
    execution_status: str
    status: str
    module_scores: Mapping[str, Any]
    vetoes: tuple[str, ...]
    pauses: tuple[str, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class OutcomeRecord:
    score_snapshot_id: str
    horizon: str
    outcome_status: str
    reason_code: str | None
    entry_ts_ms: int | None
    exit_ts_ms: int | None
    horizon_due_ms: int
    formula_version: str
    cost_config_hash: str
    funding_event_count: int | None
    funding_coverage: float | None
    graded_at_ms: int
    entry_price: float | None
    exit_price: float | None
    price_short_return: float | None
    funding_carry: float | None
    fee_assumption: float | None
    slippage_assumption: float | None
    net_short_return: float | None
    mae: float | None
    mfe: float | None


@dataclass(frozen=True)
class CandidatePage:
    generation_id: str
    total: int
    items: tuple[ScoreSnapshotRecord, ...]
    limit: int
    offset: int


def _split_statements(script: str) -> list[str]:
    cleaned_lines = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        cleaned_lines.append(line)
    return [part.strip() for part in "\n".join(cleaned_lines).split(";") if part.strip()]


class ShortLabRepository:
    """Async DuckDB access; all SQL runs on one single-worker executor."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path).expanduser()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="shortlab-db"
        )
        self._con: Any = None
        self._closed = False

    # -- lifecycle ------------------------------------------------------
    @classmethod
    async def open(
        cls,
        db_path: Path | str | None = None,
        *,
        data_dir: Path | str | None = None,
        frozen: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> Self:
        """Connect (creating parent dirs). Raises RepositoryError when unavailable."""
        if db_path is not None:
            resolved = Path(db_path).expanduser()
        elif data_dir is not None:
            resolved = Path(data_dir).expanduser() / DB_FILENAME
        else:
            resolved = resolve_db_path(frozen=frozen, env=env)
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RepositoryError(f"cannot create Short-Lab db directory: {exc}") from exc
        repo = cls(resolved)
        try:
            await repo._run(repo._connect_sync)
        except Exception:
            repo._executor.shutdown(wait=False)
            repo._closed = True
            raise
        return repo

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._run(self._close_sync)
        finally:
            self._executor.shutdown(wait=True)
            self._closed = True

    async def _run(self, fn: Any, *args: Any) -> Any:
        if self._closed:
            raise RepositoryError("ShortLabRepository is closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    def _require_con(self) -> Any:
        if self._con is None:
            raise RepositoryError("ShortLabRepository is not open")
        return self._con

    def _connect_sync(self) -> None:
        if _duckdb is None:
            raise RepositoryError(
                f"duckdb is unavailable ({_duckdb_import_error}); Short-Lab is offline"
            )
        try:
            self._con = _duckdb.connect(str(self._db_path))
        except Exception as exc:
            raise RepositoryError(
                f"cannot open Short-Lab database at {self._db_path}: {exc}"
            ) from exc

    def _close_sync(self) -> None:
        con, self._con = self._con, None
        if con is not None:
            try:
                con.close()
            except Exception:
                pass

    # -- low-level helpers (executor thread only) ------------------------
    @staticmethod
    def _rows_to_dicts(cur: Any) -> list[dict[str, Any]]:
        names = [col[0] for col in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]

    def _fetch_raw(
        self, con: Any, table: str, where: str, params: Sequence[Any]
    ) -> dict[str, Any] | None:
        cur = con.execute(f"SELECT * FROM {table} WHERE {where} LIMIT 1", list(params))
        rows = self._rows_to_dicts(cur)
        return rows[0] if rows else None

    def _insert_immutable(
        self, con: Any, table: str, where: str, params: Sequence[Any], raw: dict[str, Any]
    ) -> None:
        existing = self._fetch_raw(con, table, where, params)
        if existing is None:
            columns = ", ".join(raw.keys())
            placeholders = ", ".join(["?"] * len(raw))
            con.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                list(raw.values()),
            )
            return
        if existing != raw:
            raise SnapshotImmutableError(
                f"{table} snapshot is immutable and already stored with "
                "different content"
            )

    # -- migrate ----------------------------------------------------------
    async def migrate(self) -> int:
        """Apply 001_init.sql once; returns the schema version. Never exits."""
        return await self._run(self._migrate_sync)

    def _migrate_sync(self) -> int:
        con = self._require_con()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS sl_schema_version ("
                "version INTEGER PRIMARY KEY, applied_at_ms BIGINT NOT NULL)"
            )
            cur = con.execute("SELECT max(version) AS v FROM sl_schema_version")
            current = cur.fetchone()[0]
            if current is not None and int(current) >= SCHEMA_VERSION:
                return int(current)
            migration_file = Path(__file__).resolve().parent / "migrations" / SCHEMA_MIGRATION
            try:
                script = migration_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise MigrationError(
                    f"cannot read migration {SCHEMA_MIGRATION}: {exc}"
                ) from exc
            con.execute("BEGIN TRANSACTION")
            try:
                for statement in _split_statements(script):
                    con.execute(statement)
                applied_at_ms = time.time_ns() // 1_000_000
                con.execute(
                    "INSERT INTO sl_schema_version(version, applied_at_ms) "
                    "SELECT 1, ? WHERE NOT EXISTS "
                    "(SELECT 1 FROM sl_schema_version WHERE version = 1)",
                    [applied_at_ms],
                )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            return SCHEMA_VERSION
        except RepositoryError:
            raise
        except Exception as exc:
            raise MigrationError(f"Short-Lab migration failed: {exc}") from exc

    # -- feature snapshots -------------------------------------------------
    async def save_feature(self, snapshot: FeatureSnapshotRecord) -> str:
        return await self._run(self._save_feature_sync, snapshot)

    def _save_feature_sync(self, snapshot: FeatureSnapshotRecord) -> str:
        _check_source_meta(
            snapshot.source_meta, REQUIRED_FEATURE_META_FIELDS, what="save_feature"
        )
        con = self._require_con()
        raw = {
            "snapshot_id": snapshot.snapshot_id,
            "symbol": snapshot.symbol,
            "as_of_ms": snapshot.as_of_ms,
            "fundamental_snapshot_id": snapshot.fundamental_snapshot_id,
            "feature_version": snapshot.feature_version,
            "features_json": _canonical_json(dict(snapshot.features)),
            "source_meta_json": _canonical_json(
                {k: dict(v) for k, v in snapshot.source_meta.items()}
            ),
            "data_quality": snapshot.data_quality,
        }
        self._insert_immutable(
            con, "sl_feature_snapshot", "snapshot_id = ?", [snapshot.snapshot_id], raw
        )
        return snapshot.snapshot_id

    async def get_feature(self, snapshot_id: str) -> FeatureSnapshotRecord | None:
        return await self._run(self._get_feature_sync, snapshot_id)

    def _get_feature_sync(self, snapshot_id: str) -> FeatureSnapshotRecord | None:
        con = self._require_con()
        raw = self._fetch_raw(
            con, "sl_feature_snapshot", "snapshot_id = ?", [snapshot_id]
        )
        if raw is None:
            return None
        return FeatureSnapshotRecord(
            snapshot_id=raw["snapshot_id"],
            symbol=raw["symbol"],
            as_of_ms=raw["as_of_ms"],
            feature_version=raw["feature_version"],
            features=_parse_json_dict(raw["features_json"], "features_json"),
            source_meta={
                k: v
                for k, v in _parse_json_dict(
                    raw["source_meta_json"], "source_meta_json"
                ).items()
            },
            data_quality=raw["data_quality"],
            fundamental_snapshot_id=raw["fundamental_snapshot_id"],
        )

    async def feature_source_status(self, snapshot_id: str) -> str:
        """'OK' when READY/DQ metadata is complete, else 'UNAVAILABLE'.

        Legacy rows stored without field-level metadata must never be
        projected as READY; callers gate READY display on this helper.
        """
        record = await self.get_feature(snapshot_id)
        if record is None:
            raise ReferenceNotFoundError(f"feature snapshot {snapshot_id!r} not found")
        if _source_meta_complete(record.source_meta, REQUIRED_FEATURE_META_FIELDS):
            return "OK"
        return "UNAVAILABLE"

    # -- entry snapshots ----------------------------------------------------
    async def save_entry(self, snapshot: EntrySnapshotRecord) -> str:
        return await self._run(self._save_entry_sync, snapshot)

    def _save_entry_sync(self, snapshot: EntrySnapshotRecord) -> str:
        _check_source_meta(
            snapshot.source_meta, REQUIRED_ENTRY_META_BLOCKS, what="save_entry"
        )
        con = self._require_con()
        raw = {
            "snapshot_id": snapshot.snapshot_id,
            "symbol": snapshot.symbol,
            "as_of_ms": snapshot.as_of_ms,
            "entry_version": snapshot.entry_version,
            "dive_weights_hash": snapshot.dive_weights_hash,
            "dive_engine_version": snapshot.dive_engine_version,
            "dive_config_hash": snapshot.dive_config_hash,
            "primary_tf": snapshot.primary_tf,
            "inputs_json": _canonical_json(dict(snapshot.inputs)),
            "components_json": _canonical_json(dict(snapshot.components)),
            "source_meta_json": _canonical_json(
                {k: dict(v) for k, v in snapshot.source_meta.items()}
            ),
            "entry_score": snapshot.entry_score,
            "created_at_ms": snapshot.created_at_ms,
        }
        self._insert_immutable(
            con, "sl_entry_snapshot", "snapshot_id = ?", [snapshot.snapshot_id], raw
        )
        return snapshot.snapshot_id

    async def get_entry(self, snapshot_id: str) -> EntrySnapshotRecord | None:
        return await self._run(self._get_entry_sync, snapshot_id)

    def _get_entry_sync(self, snapshot_id: str) -> EntrySnapshotRecord | None:
        con = self._require_con()
        raw = self._fetch_raw(
            con, "sl_entry_snapshot", "snapshot_id = ?", [snapshot_id]
        )
        if raw is None:
            return None
        return EntrySnapshotRecord(
            snapshot_id=raw["snapshot_id"],
            symbol=raw["symbol"],
            as_of_ms=raw["as_of_ms"],
            entry_version=raw["entry_version"],
            dive_weights_hash=raw["dive_weights_hash"],
            dive_engine_version=raw["dive_engine_version"],
            dive_config_hash=raw["dive_config_hash"],
            primary_tf=raw["primary_tf"],
            inputs=_parse_json_dict(raw["inputs_json"], "inputs_json"),
            components=_parse_json_dict(raw["components_json"], "components_json"),
            source_meta=_parse_json_dict(raw["source_meta_json"], "source_meta_json"),
            entry_score=raw["entry_score"],
            created_at_ms=raw["created_at_ms"],
        )

    async def entry_source_status(self, snapshot_id: str) -> str:
        """'OK' when entry block metadata is complete, else 'UNAVAILABLE'."""
        record = await self.get_entry(snapshot_id)
        if record is None:
            raise ReferenceNotFoundError(f"entry snapshot {snapshot_id!r} not found")
        if _source_meta_complete(record.source_meta, REQUIRED_ENTRY_META_BLOCKS):
            return "OK"
        return "UNAVAILABLE"

    # -- score snapshots ------------------------------------------------------
    @staticmethod
    def _validate_score_state(snapshot: ScoreSnapshotRecord) -> None:
        if snapshot.analysis_tier not in ALLOWED_ANALYSIS_TIERS:
            raise ValidationError(
                f"analysis_tier={snapshot.analysis_tier!r} must be LITE or FULL"
            )
        try:
            CandidateState(
                candidate_status=snapshot.candidate_status,  # type: ignore[arg-type]
                execution_status=snapshot.execution_status,  # type: ignore[arg-type]
                status=snapshot.status,  # type: ignore[arg-type]
                reasons=tuple(snapshot.reasons),
                vetoes=tuple(snapshot.vetoes),
                pauses=tuple(snapshot.pauses),
                warnings=tuple(snapshot.warnings),
            )
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"invalid candidate state: {exc}") from exc
        if snapshot.entry_snapshot_id is None:
            if snapshot.entry_version is not None or snapshot.entry_score is not None:
                raise ValidationError(
                    "entry_snapshot_id is null so entry_version/entry_score "
                    "must also be null"
                )

    def _score_raw(self, snapshot: ScoreSnapshotRecord) -> dict[str, Any]:
        return {
            "snapshot_id": snapshot.snapshot_id,
            "generation_id": snapshot.generation_id,
            "feature_snapshot_id": snapshot.feature_snapshot_id,
            "entry_snapshot_id": snapshot.entry_snapshot_id,
            "symbol": snapshot.symbol,
            "as_of_ms": snapshot.as_of_ms,
            "analysis_tier": snapshot.analysis_tier,
            "profile": snapshot.profile,
            "score_version": snapshot.score_version,
            "entry_version": snapshot.entry_version,
            "feature_version": snapshot.feature_version,
            "config_hash": snapshot.config_hash,
            "ltss": snapshot.ltss,
            "entry_score": snapshot.entry_score,
            "data_quality": snapshot.data_quality,
            "candidate_status": snapshot.candidate_status,
            "execution_status": snapshot.execution_status,
            "status": snapshot.status,
            "module_scores_json": _canonical_json(dict(snapshot.module_scores)),
            "vetoes_json": _canonical_json(list(snapshot.vetoes)),
            "pauses_json": _canonical_json(list(snapshot.pauses)),
            "reasons_json": _canonical_json(list(snapshot.reasons)),
            "warnings_json": _canonical_json(list(snapshot.warnings)),
        }

    def _insert_score_row_sync(self, con: Any, snapshot: ScoreSnapshotRecord) -> None:
        """Validate references and insert one score row (no commit).

        Exposed as an instance method so tests can inject faults around it.
        """
        self._validate_score_state(snapshot)
        feature = self._fetch_raw(
            con, "sl_feature_snapshot", "snapshot_id = ?", [snapshot.feature_snapshot_id]
        )
        if feature is None:
            raise ReferenceNotFoundError(
                f"feature snapshot {snapshot.feature_snapshot_id!r} not found"
            )
        if feature["symbol"] != snapshot.symbol or feature["as_of_ms"] != snapshot.as_of_ms:
            raise ValidationError(
                "score symbol/as_of must match the referenced feature snapshot"
            )
        if snapshot.entry_snapshot_id is not None:
            entry = self._fetch_raw(
                con, "sl_entry_snapshot", "snapshot_id = ?", [snapshot.entry_snapshot_id]
            )
            if entry is None:
                raise ReferenceNotFoundError(
                    f"entry snapshot {snapshot.entry_snapshot_id!r} not found"
                )
            if entry["symbol"] != snapshot.symbol or entry["as_of_ms"] != snapshot.as_of_ms:
                raise ValidationError(
                    "score symbol/as_of must match the referenced entry snapshot"
                )
        raw = self._score_raw(snapshot)
        self._insert_immutable(
            con, "sl_score_snapshot", "snapshot_id = ?", [snapshot.snapshot_id], raw
        )

    async def save_score(
        self,
        snapshot: ScoreSnapshotRecord,
        entry_snapshot_id: str | None = None,
    ) -> str:
        """Persist one score row (autocommit; does not touch job state).

        ``entry_snapshot_id`` mirrors the fixed cross-task interface: when
        given it must agree with ``snapshot.entry_snapshot_id``.
        """
        return await self._run(self._save_score_sync, snapshot, entry_snapshot_id)

    def _save_score_sync(
        self, snapshot: ScoreSnapshotRecord, entry_snapshot_id: str | None
    ) -> str:
        if entry_snapshot_id is not None and (
            snapshot.entry_snapshot_id is not None
            and snapshot.entry_snapshot_id != entry_snapshot_id
        ):
            raise ValidationError(
                "entry_snapshot_id argument disagrees with the score snapshot"
            )
        effective = snapshot
        if entry_snapshot_id is not None and snapshot.entry_snapshot_id is None:
            import dataclasses

            effective = dataclasses.replace(
                snapshot, entry_snapshot_id=entry_snapshot_id
            )
        con = self._require_con()
        self._insert_score_row_sync(con, effective)
        return snapshot.snapshot_id

    async def save_score_batch(
        self,
        scores: Sequence[ScoreSnapshotRecord],
        *,
        job_id: str,
        job_type: str = _JOB_TYPE_SCORE_REFRESH,
        started_at_ms: int,
        finished_at_ms: int,
        stats: Mapping[str, Any] | None = None,
    ) -> str:
        """Commit a full generation plus job SUCCEEDED in one transaction.

        Any failure rolls back every score row and the job success marker;
        the previous complete generation keeps serving reads.
        """
        return await self._run(
            self._save_score_batch_sync,
            list(scores),
            job_id,
            job_type,
            started_at_ms,
            finished_at_ms,
            dict(stats) if stats is not None else None,
        )

    def _save_score_batch_sync(
        self,
        scores: list[ScoreSnapshotRecord],
        job_id: str,
        job_type: str,
        started_at_ms: int,
        finished_at_ms: int,
        stats: dict[str, Any] | None,
    ) -> str:
        if not scores:
            raise ValidationError("save_score_batch requires at least one score")
        con = self._require_con()
        con.execute("BEGIN TRANSACTION")
        try:
            for snapshot in scores:
                self._insert_score_row_sync(con, snapshot)
            stats_json = _canonical_json(stats) if stats is not None else None
            existing = self._fetch_raw(con, "sl_job_run", "job_id = ?", [job_id])
            if existing is None:
                con.execute(
                    "INSERT INTO sl_job_run (job_id, job_type, started_at_ms, "
                    "finished_at_ms, status, stats_json, error_code) "
                    "VALUES (?, ?, ?, ?, 'SUCCEEDED', ?, NULL)",
                    [job_id, job_type, started_at_ms, finished_at_ms, stats_json],
                )
            else:
                con.execute(
                    "UPDATE sl_job_run SET job_type = ?, started_at_ms = ?, "
                    "finished_at_ms = ?, status = 'SUCCEEDED', stats_json = ?, "
                    "error_code = NULL WHERE job_id = ?",
                    [job_type, started_at_ms, finished_at_ms, stats_json, job_id],
                )
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return job_id

    @staticmethod
    def _score_from_raw(raw: Mapping[str, Any]) -> ScoreSnapshotRecord:
        return ScoreSnapshotRecord(
            snapshot_id=raw["snapshot_id"],
            generation_id=raw["generation_id"],
            feature_snapshot_id=raw["feature_snapshot_id"],
            entry_snapshot_id=raw["entry_snapshot_id"],
            symbol=raw["symbol"],
            as_of_ms=raw["as_of_ms"],
            analysis_tier=raw["analysis_tier"],
            profile=raw["profile"],
            score_version=raw["score_version"],
            entry_version=raw["entry_version"],
            feature_version=raw["feature_version"],
            config_hash=raw["config_hash"],
            ltss=raw["ltss"],
            entry_score=raw["entry_score"],
            data_quality=raw["data_quality"],
            candidate_status=raw["candidate_status"],
            execution_status=raw["execution_status"],
            status=raw["status"],
            module_scores=_parse_json_dict(raw["module_scores_json"], "module_scores_json"),
            vetoes=_parse_json_tuple(raw["vetoes_json"], "vetoes_json"),
            pauses=_parse_json_tuple(raw["pauses_json"], "pauses_json"),
            reasons=_parse_json_tuple(raw["reasons_json"], "reasons_json"),
            warnings=_parse_json_tuple(raw["warnings_json"], "warnings_json"),
        )

    async def get_score(self, snapshot_id: str) -> ScoreSnapshotRecord | None:
        return await self._run(self._get_score_sync, snapshot_id)

    def _get_score_sync(self, snapshot_id: str) -> ScoreSnapshotRecord | None:
        con = self._require_con()
        raw = self._fetch_raw(con, "sl_score_snapshot", "snapshot_id = ?", [snapshot_id])
        if raw is None:
            return None
        return self._score_from_raw(raw)

    # -- jobs -------------------------------------------------------------------
    async def create_job_run(
        self,
        job_id: str,
        job_type: str,
        started_at_ms: int,
        *,
        stats: Mapping[str, Any] | None = None,
    ) -> str:
        return await self._run(
            self._create_job_run_sync, job_id, job_type, started_at_ms,
            dict(stats) if stats is not None else None,
        )

    def _create_job_run_sync(
        self, job_id: str, job_type: str, started_at_ms: int, stats: dict | None
    ) -> str:
        con = self._require_con()
        if self._fetch_raw(con, "sl_job_run", "job_id = ?", [job_id]) is not None:
            raise ValidationError(f"job {job_id!r} already exists")
        stats_json = _canonical_json(stats) if stats is not None else None
        con.execute(
            "INSERT INTO sl_job_run (job_id, job_type, started_at_ms, finished_at_ms,"
            " status, stats_json, error_code) VALUES (?, ?, ?, NULL, 'RUNNING', ?, NULL)",
            [job_id, job_type, started_at_ms, stats_json],
        )
        return job_id

    async def finish_job_run(
        self,
        job_id: str,
        status: str,
        finished_at_ms: int,
        *,
        stats: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._run(
            self._finish_job_run_sync, job_id, status, finished_at_ms,
            dict(stats) if stats is not None else None, error_code,
        )

    def _finish_job_run_sync(
        self,
        job_id: str,
        status: str,
        finished_at_ms: int,
        stats: dict | None,
        error_code: str | None,
    ) -> None:
        if status not in ALLOWED_JOB_STATUS:
            raise ValidationError(f"job status={status!r} must be RUNNING/SUCCEEDED/FAILED")
        con = self._require_con()
        if self._fetch_raw(con, "sl_job_run", "job_id = ?", [job_id]) is None:
            raise ReferenceNotFoundError(f"job {job_id!r} not found")
        stats_json = _canonical_json(stats) if stats is not None else None
        con.execute(
            "UPDATE sl_job_run SET status = ?, finished_at_ms = ?, stats_json = ?, "
            "error_code = ? WHERE job_id = ?",
            [status, finished_at_ms, stats_json, error_code, job_id],
        )

    async def get_job_run(self, job_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_job_run_sync, job_id)

    def _get_job_run_sync(self, job_id: str) -> dict[str, Any] | None:
        con = self._require_con()
        raw = self._fetch_raw(con, "sl_job_run", "job_id = ?", [job_id])
        if raw is None:
            return None
        if raw.get("stats_json") is not None:
            raw["stats_json"] = _parse_json_dict(raw["stats_json"], "stats_json")
        return raw

    async def latest_completed_generation(
        self, job_type: str = _JOB_TYPE_SCORE_REFRESH
    ) -> str | None:
        return await self._run(self._latest_completed_generation_sync, job_type)

    def _latest_completed_generation_sync(self, job_type: str) -> str | None:
        con = self._require_con()
        cur = con.execute(
            "SELECT job_id FROM sl_job_run WHERE job_type = ? AND status = 'SUCCEEDED'"
            " AND finished_at_ms IS NOT NULL ORDER BY finished_at_ms DESC, job_id ASC"
            " LIMIT 1",
            [job_type],
        )
        row = cur.fetchone()
        return row[0] if row else None

    # -- candidate pagination -----------------------------------------------------
    async def list_candidates(
        self,
        *,
        generation_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        sort: str | None = None,
        order: str = "desc",
    ) -> CandidatePage:
        return await self._run(
            self._list_candidates_sync, generation_id, limit, offset, status, sort, order
        )

    def _list_candidates_sync(
        self,
        generation_id: str | None,
        limit: int,
        offset: int,
        status: str | None,
        sort: str | None,
        order: str,
    ) -> CandidatePage:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("limit must be an int in 1..200")
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an int >= 0")
        if status is not None and status not in _ALLOWED_DISPLAY_STATUS:
            raise ValidationError(f"status={status!r} is not a display status")
        if sort is not None and sort not in _ALLOWED_CANDIDATE_SORTS:
            raise ValidationError(
                f"sort={sort!r} must be one of {sorted(_ALLOWED_CANDIDATE_SORTS)}"
            )
        if order not in ("asc", "desc"):
            raise ValidationError("order must be 'asc' or 'desc'")
        con = self._require_con()
        if generation_id is None:
            resolved = self._latest_completed_generation_sync(_JOB_TYPE_SCORE_REFRESH)
            if resolved is None:
                raise GenerationNotFoundError("no completed score_refresh generation")
        else:
            job = self._fetch_raw(con, "sl_job_run", "job_id = ?", [generation_id])
            if (
                job is None
                or job["status"] != "SUCCEEDED"
                or job["job_type"] != _JOB_TYPE_SCORE_REFRESH
            ):
                raise GenerationNotFoundError(
                    f"generation {generation_id!r} is not a completed score_refresh job"
                )
            resolved = generation_id
        clauses = ["generation_id = ?"]
        params: list[Any] = [resolved]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = " AND ".join(clauses)
        total = con.execute(
            f"SELECT count(*) FROM sl_score_snapshot WHERE {where}", params
        ).fetchone()[0]
        if sort is None:
            order_by = (
                "CASE status WHEN 'READY' THEN 0 WHEN 'CANDIDATE' THEN 1 "
                "WHEN 'WATCH' THEN 2 WHEN 'PAUSED' THEN 3 WHEN 'BLOCKED' THEN 4 "
                "ELSE 5 END, ltss DESC NULLS LAST, entry_score DESC NULLS LAST, "
                "data_quality DESC, symbol ASC, snapshot_id ASC"
            )
        else:
            direction = "ASC" if order == "asc" else "DESC"
            order_by = (
                f"{sort} {direction} NULLS LAST, symbol ASC, snapshot_id ASC"
            )
        cur = con.execute(
            f"SELECT * FROM sl_score_snapshot WHERE {where} ORDER BY {order_by} "
            "LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        items = tuple(self._score_from_raw(row) for row in self._rows_to_dicts(cur))
        return CandidatePage(
            generation_id=resolved, total=int(total), items=items,
            limit=limit, offset=offset,
        )

    # -- forward outcomes ------------------------------------------------------------
    async def save_outcome(self, outcome: OutcomeRecord) -> str:
        return await self._run(self._save_outcome_sync, outcome)

    def _save_outcome_sync(self, outcome: OutcomeRecord) -> str:
        if outcome.horizon not in ALLOWED_HORIZONS:
            raise ValidationError(f"horizon={outcome.horizon!r} must be 7D/30D/90D")
        if outcome.outcome_status not in ALLOWED_OUTCOME_STATUS:
            raise ValidationError(
                f"outcome_status={outcome.outcome_status!r} must be "
                "PENDING/COMPLETE/CENSORED/UNAVAILABLE"
            )
        con = self._require_con()
        score = self._fetch_raw(
            con, "sl_score_snapshot", "snapshot_id = ?", [outcome.score_snapshot_id]
        )
        if score is None:
            raise ReferenceNotFoundError(
                f"score snapshot {outcome.score_snapshot_id!r} not found"
            )
        raw = {
            "score_snapshot_id": outcome.score_snapshot_id,
            "horizon": outcome.horizon,
            "outcome_status": outcome.outcome_status,
            "reason_code": outcome.reason_code,
            "entry_ts_ms": outcome.entry_ts_ms,
            "exit_ts_ms": outcome.exit_ts_ms,
            "horizon_due_ms": outcome.horizon_due_ms,
            "formula_version": outcome.formula_version,
            "cost_config_hash": outcome.cost_config_hash,
            "funding_event_count": outcome.funding_event_count,
            "funding_coverage": outcome.funding_coverage,
            "graded_at_ms": outcome.graded_at_ms,
            "entry_price": outcome.entry_price,
            "exit_price": outcome.exit_price,
            "price_short_return": outcome.price_short_return,
            "funding_carry": outcome.funding_carry,
            "fee_assumption": outcome.fee_assumption,
            "slippage_assumption": outcome.slippage_assumption,
            "net_short_return": outcome.net_short_return,
            "mae": outcome.mae,
            "mfe": outcome.mfe,
        }
        where = (
            "score_snapshot_id = ? AND horizon = ? AND formula_version = ? "
            "AND cost_config_hash = ?"
        )
        params = [
            outcome.score_snapshot_id, outcome.horizon,
            outcome.formula_version, outcome.cost_config_hash,
        ]
        self._insert_immutable(con, "sl_forward_outcome", where, params, raw)
        return outcome.score_snapshot_id

    async def get_outcome(
        self,
        score_snapshot_id: str,
        horizon: str,
        formula_version: str,
        cost_config_hash: str,
    ) -> OutcomeRecord | None:
        return await self._run(
            self._get_outcome_sync, score_snapshot_id, horizon,
            formula_version, cost_config_hash,
        )

    def _get_outcome_sync(
        self, score_snapshot_id: str, horizon: str, formula_version: str,
        cost_config_hash: str,
    ) -> OutcomeRecord | None:
        con = self._require_con()
        raw = self._fetch_raw(
            con, "sl_forward_outcome",
            "score_snapshot_id = ? AND horizon = ? AND formula_version = ? "
            "AND cost_config_hash = ?",
            [score_snapshot_id, horizon, formula_version, cost_config_hash],
        )
        if raw is None:
            return None
        return OutcomeRecord(**raw)

    async def list_outcomes_for_score(self, score_snapshot_id: str) -> tuple[OutcomeRecord, ...]:
        return await self._run(self._list_outcomes_for_score_sync, score_snapshot_id)

    def _list_outcomes_for_score_sync(
        self, score_snapshot_id: str
    ) -> tuple[OutcomeRecord, ...]:
        con = self._require_con()
        cur = con.execute(
            "SELECT * FROM sl_forward_outcome WHERE score_snapshot_id = ? "
            "ORDER BY horizon ASC, formula_version ASC, cost_config_hash ASC",
            [score_snapshot_id],
        )
        return tuple(OutcomeRecord(**row) for row in self._rows_to_dicts(cur))

    # -- introspection ------------------------------------------------------------------
    async def index_names(self) -> list[str]:
        return await self._run(self._index_names_sync)

    def _index_names_sync(self) -> list[str]:
        con = self._require_con()
        cur = con.execute("SELECT index_name FROM duckdb_indexes()")
        return sorted(row[0] for row in cur.fetchall())
