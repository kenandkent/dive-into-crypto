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
import dataclasses
import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Sequence
from typing import Self

from diveintocrypto_desktop.shortlab.models import CandidateState, RetentionPolicy
from diveintocrypto_desktop.shortlab.paths import DB_FILENAME, resolve_db_path

try:  # DuckDB is a hard dependency (pyproject); keep module importable without it.
    import duckdb as _duckdb
except Exception as _duckdb_import_error:  # pragma: no cover - import-time guard
    _duckdb = None  # type: ignore[assignment]

SCHEMA_VERSION = 5
SCHEMA_MIGRATION = "001_init.sql"

# Ordered forward-only migrations. The F01 base target is 4: a fresh database
# applies 001-004 in order (002/003 tables are created empty, which does not
# mean FULL is enabled). The Hedge target 5 lands with H01 (005_*): a
# Hedge-enabled config migrates to 5 in one per-file transaction; a 005
# failure rolls back to 4 and Hedge stays unavailable while 001-004 keep
# serving (B28/B29).
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "001_init.sql"),
    (2, "002_unlock_social.sql"),
    (3, "003_catalyst.sql"),
    (4, "004_core_completion.sql"),
    (5, "005_hedge_advisor.sql"),
)

# Single-writer priority worker (design A6.2/B28.8, plan F01.2/F01.3): one
# DuckDB connection, one worker, at most QUEUE_CAPACITY pending operations.
# A full queue raises LocalWriteBusyError ("HTTP503 LOCAL_WRITE_BUSY") and
# commits nothing. Lower number = higher priority; equal priority is FIFO;
# every QUEUE_AGING_SEC a waiter promotes one level (starvation guard).
PRIORITY_CRITICAL = 0  # ledger / alert / job lifecycle
PRIORITY_USER_QUERY = 1  # interactive reads
PRIORITY_SOURCE_SCORE = 2  # source snapshots, scores, evidence writes
PRIORITY_BACKFILL = 3  # funding backfill / observations / cursors
PRIORITY_RETENTION = 4  # retention sweeps
QUEUE_CAPACITY = 256
QUEUE_AGING_SEC = 30.0

#: Error code for jobs that were RUNNING when the process went away (F01.3).
PROCESS_INTERRUPTED = "PROCESS_INTERRUPTED"


def schema_target_for_config(config: Any) -> int:
    """Phase-gated migration target derived from the provider flags.

    F01 base target is 4 (001-004 applied in order; 002/003 tables exist but
    stay empty until their providers are enabled -- schema and provider
    capability are separate). A Hedge-enabled config targets 5, whose
    migration file lands with H01; requesting 5 on a pre-H01 build raises
    `MigrationError` and Hedge reads/writes stay disabled.
    """
    hedge = getattr(config, "hedge", None)
    try:
        if hedge is not None and bool(getattr(hedge, "enabled", False)):
            return 5
    except Exception:
        pass
    providers = getattr(config, "providers", None) or {}
    try:
        items = dict(providers) if not isinstance(providers, dict) else providers
    except Exception:
        return 4
    try:
        entry = items.get("hedge")
        if entry is not None and bool(getattr(entry, "enabled", False)):
            return 5
    except Exception:
        pass
    return 4

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


class LocalWriteBusyError(RepositoryError):
    """The single-writer queue holds 256 pending ops (HTTP503 LOCAL_WRITE_BUSY).

    Raised before any SQL runs, so nothing is committed. ``status_code`` and
    ``error_code`` let the API layer map it to ``HTTP 503`` verbatim.
    """

    status_code = 503
    error_code = "LOCAL_WRITE_BUSY"


class HedgeIdempotencyError(RepositoryError):
    """Same ``client_event_id``/``client_request_id`` with different payload.

    Maps to HTTP 409 ``IDEMPOTENCY_PAYLOAD_MISMATCH``; the stored row is
    returned unchanged on identical payloads (H01.2, B28.7.1).
    """

    status_code = 409
    error_code = "IDEMPOTENCY_PAYLOAD_MISMATCH"


class HedgeVersionConflictError(RepositoryError):
    """``expected_version`` does not match ``plan_version`` (HTTP 409)."""

    status_code = 409
    error_code = "PLAN_VERSION_CONFLICT"


class HedgeReferenceInvalidError(RepositoryError):
    """Snapshot-reference graph has a ring, broken link or unknown type.

    Retention stops the sweep batch and reports
    ``RETENTION_REFERENCE_INVALID`` while conservatively retaining (B28.7.2).
    """

    status_code = 409
    error_code = "RETENTION_REFERENCE_INVALID"


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


def _record_dict(
    record: Any,
    required: frozenset[str] | set[str],
    optional: frozenset[str] | set[str],
    *,
    name: str,
) -> dict[str, Any]:
    """Normalise a frozen dataclass or mapping to a strictly-checked dict.

    F01 contract: every ``record`` maps 1:1 to its table columns. Missing
    required fields and unknown fields both raise `ValidationError`; extra
    keys are never silently stored (they would fork the schema).
    """
    if dataclasses.is_dataclass(record) and not isinstance(record, type):
        data = dataclasses.asdict(record)
    elif isinstance(record, Mapping):
        data = dict(record)
    else:
        raise ValidationError(f"{name} must be a mapping or record dataclass")
    allowed = set(required) | set(optional)
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValidationError(f"{name} has unknown fields: {unknown}")
    missing = sorted(set(required) - set(data))
    if missing:
        raise ValidationError(f"{name} misses required fields: {missing}")
    nulls = sorted(key for key in required if data.get(key) is None)
    if nulls:
        raise ValidationError(f"{name} has null required fields: {nulls}")
    return data


def _json_text(value: Any, *, name: str, mapping_only: bool = False) -> str:
    """Store JSON columns in canonical form (validates on the way in)."""
    if isinstance(value, Mapping):
        return _canonical_json(dict(value))
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{name} is not valid JSON: {exc}") from exc
        if mapping_only and not isinstance(parsed, dict):
            raise ValidationError(f"{name} must hold a JSON object")
        return _canonical_json(parsed)
    raise ValidationError(f"{name} must be a mapping or JSON string")


def _require_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an int (ms)")
    return value


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


@dataclass(frozen=True)
class UnlockEventRecord:
    """One row of ``sl_unlock_event`` (design 19.2, Phase 5)."""

    event_id: str
    canonical_id: str
    known_at_ms: int
    unlock_at_ms: int
    amount_tokens: float
    allocation_type: str
    source: str
    fetched_at_ms: int


@dataclass(frozen=True)
class SocialSnapshotRecord:
    """One row of ``sl_social_snapshot`` (design 19.2, Phase 5)."""

    snapshot_id: str
    canonical_id: str
    as_of_ms: int
    fetched_at_ms: int
    source: str
    metrics: Mapping[str, Any]


@dataclass(frozen=True)
class CatalystEventRecord:
    """One row of ``sl_catalyst_event`` (design 19.2, Phase 6)."""

    event_id: str
    canonical_id: str
    known_at_ms: int
    announced_at_ms: int
    effective_at_ms: int | None
    event_type: str
    severity: str
    confidence: float
    source_url: str | None
    title: str


# ---------------------------------------------------------------------------
# F01 contract records (frozen dataclasses; single export point for F02+).
#
# Every record maps 1:1 to its table columns (001 for asset/mapping/
# lifecycle/funding-event/fundamental, 004 for identity/rules/observation/
# config/cursor). Public F01 methods accept either these records or plain
# mappings with exactly the same keys (`_record_dict` strict check).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetRecord:
    """One row of ``sl_asset`` (mutable current projection)."""

    canonical_id: str
    display_symbol: str
    name: str | None = None
    categories: tuple[str, ...] = ()
    created_at_ms: int = 0
    updated_at_ms: int = 0


@dataclass(frozen=True)
class AssetMappingRecord:
    """One row of ``sl_asset_mapping`` (mutable current projection)."""

    futures_symbol: str
    canonical_id: str | None = None
    spot_symbol: str | None = None
    contract_multiplier: float | None = None
    multiplier_source: str | None = None
    coingecko_id: str | None = None
    unlock_provider_id: str | None = None
    social_provider_id: str | None = None
    mapping_confidence: str = "UNRESOLVED"
    mapping_source: str = "OTHER"
    updated_at_ms: int = 0


@dataclass(frozen=True)
class IdentitySnapshotRecord:
    """One row of ``sl_identity_snapshot`` (immutable)."""

    identity_snapshot_id: str
    futures_symbol: str
    canonical_id: str
    mapping_version: str
    observed_at_ms: int
    identity_json: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class ContractRulesSnapshotRecord:
    """One row of ``sl_contract_rules_snapshot`` (immutable)."""

    snapshot_id: str
    symbol: str
    source_as_of_ms: int | None = None
    known_at_ms: int = 0
    rules_json: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class ContractLifecycleRecord:
    """One observation row of ``sl_contract_lifecycle`` (append-only)."""

    futures_symbol: str
    observed_at_ms: int
    onboard_at_ms: int | None = None
    first_seen_ms: int = 0
    delivery_at_ms: int | None = None
    contract_type: str | None = None
    exchange_status: str | None = None
    contract_multiplier: float | None = None
    multiplier_source: str | None = None


@dataclass(frozen=True)
class FundingEventRecord:
    """One canonical public funding event (``sl_funding_event``)."""

    symbol: str
    funding_time_ms: int
    funding_rate: float
    mark_price: float | None = None


@dataclass(frozen=True)
class FundingObservationRecord:
    """One row of ``sl_funding_observation`` (immutable, incl. conflicts)."""

    observation_id: str
    symbol: str
    funding_time_ms: int
    known_at_ms: int
    raw_json: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    interval_hours: float | None = None
    interval_source: str | None = None
    observation_status: str = "OBSERVED"


@dataclass(frozen=True)
class FundamentalSnapshotRecord:
    """One row of ``sl_fundamental_snapshot`` (immutable)."""

    snapshot_id: str
    canonical_id: str
    as_of_ms: int
    fetched_at_ms: int
    market_cap_usd: float | None = None
    fdv_usd: float | None = None
    circulating_supply: float | None = None
    total_supply: float | None = None
    max_supply: float | None = None
    ath_price: float | None = None
    ath_date_ms: int | None = None
    source: str = ""


@dataclass(frozen=True)
class ConfigSnapshotRecord:
    """One row of ``sl_config_snapshot`` (immutable, keyed by policy_hash)."""

    policy_hash: str
    config_hash: str
    policy_version: str
    canonical_json: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    created_at_ms: int = 0


@dataclass(frozen=True)
class RetentionStats:
    """Result of `maintain_retention`: per-call bounded sweep summary."""

    deleted: int
    retained: int
    errors: int
    as_of_ms: int


@dataclass(frozen=True)
class EvidencePage:
    """One page of SUCCEEDED-generation scores for evidence/graders."""

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


class _QueuedOp:
    """One admitted-but-waiting worker operation (F01 priority queue)."""

    __slots__ = ("base_priority", "seq", "enqueued_at", "trace")

    def __init__(self, base_priority: int, seq: int, enqueued_at: float, trace: str | None) -> None:
        self.base_priority = base_priority
        self.seq = seq
        self.enqueued_at = enqueued_at
        self.trace = trace


class ShortLabRepository:
    """Async DuckDB access; all SQL runs on one single-worker executor."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path).expanduser()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="shortlab-db"
        )
        self._con: Any = None
        self._closed = False
        # F01 single-writer priority queue: pending ops wait here (FIFO per
        # effective priority, 30s aging); only the head runs on the worker.
        self._pending: list["_QueuedOp"] = []
        self._queue_seq = 0
        self._worker_busy = False
        self._accepting = True
        self._cond = asyncio.Condition()
        self._now_fn = time.monotonic

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
            # F01.3 crash recovery: a previous process may have left RUNNING
            # rows behind. Best-effort only (fresh DBs have no job table yet).
            try:
                await repo.recover_running_jobs(time.time_ns() // 1_000_000)
            except Exception:
                pass
        except Exception:
            repo._executor.shutdown(wait=False)
            repo._closed = True
            raise
        return repo

    async def close(self) -> None:
        if self._closed:
            return
        # Drain: refuse new work, let queued/running ops finish in order,
        # then close the connection on the worker. Never raises
        # LocalWriteBusyError -- close always completes.
        async with self._cond:
            self._accepting = False
            while self._pending or self._worker_busy:
                await self._cond.wait()
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._close_sync)
        finally:
            self._executor.shutdown(wait=True)
            self._closed = True

    def queue_depth(self) -> int:
        """Number of operations currently waiting for the single worker."""
        return len(self._pending)

    def _effective_priority(self, entry: "_QueuedOp", now: float) -> int:
        age = max(0.0, now - entry.enqueued_at)
        bump = int(age // QUEUE_AGING_SEC)
        return max(0, entry.base_priority - bump)

    def _is_head(self, entry: "_QueuedOp") -> bool:
        now = self._now_fn()
        mine = (self._effective_priority(entry, now), entry.seq)
        for other in self._pending:
            if other is entry:
                continue
            if (self._effective_priority(other, now), other.seq) < mine:
                return False
        return True

    async def _run(
        self,
        fn: Any,
        *args: Any,
        priority: int | None = None,
        trace: str | None = None,
    ) -> Any:
        """Gate every DB call through the bounded priority worker queue.

        ``priority`` follows PRIORITY_* (lower runs first); ``None`` infers
        reads (``_get_*``/``_list_*``/``_latest_*``/``_index_*`` -> user
        query) vs writes (-> source/score). ``trace`` is a caller-supplied
        correlation string kept on the queued op for diagnostics.
        """
        if self._closed or not self._accepting:
            raise RepositoryError("ShortLabRepository is closed")
        if priority is None:
            fn_name = getattr(fn, "__name__", "")
            if fn_name.startswith(
                ("_get_", "_list_", "_latest_", "_load_", "_index_", "_fetch_")
            ):
                priority = PRIORITY_USER_QUERY
            else:
                priority = PRIORITY_SOURCE_SCORE
        if not isinstance(priority, int) or not 0 <= priority <= 4:
            raise ValidationError(f"priority={priority!r} must be an int in 0..4")
        if trace is not None and not isinstance(trace, str):
            raise ValidationError("trace must be a str or None")
        loop = asyncio.get_running_loop()
        entry = _QueuedOp(
            base_priority=priority,
            seq=self._queue_seq,
            enqueued_at=self._now_fn(),
            trace=trace,
        )
        self._queue_seq += 1
        async with self._cond:
            if len(self._pending) >= QUEUE_CAPACITY:
                raise LocalWriteBusyError(
                    "HTTP503 LOCAL_WRITE_BUSY: shortlab write queue is full "
                    f"({QUEUE_CAPACITY} pending operations); nothing was committed"
                )
            self._pending.append(entry)
            try:
                while self._worker_busy or not self._is_head(entry):
                    await self._cond.wait()
                    if self._closed or not self._accepting:
                        raise RepositoryError("ShortLabRepository is closed")
            except BaseException:
                # Condition.wait reacquires its lock before cancellation.
                # A cancelled waiter must never remain at the queue head.
                if entry in self._pending:
                    self._pending.remove(entry)
                self._cond.notify_all()
                raise
            self._pending.remove(entry)
            self._worker_busy = True
        future = loop.run_in_executor(self._executor, fn, *args)
        async def finish() -> Any:
            try:
                return await future
            finally:
                async with self._cond:
                    self._worker_busy = False
                    self._cond.notify_all()
        # Cancelling the caller cannot cancel an already-running DB thread.
        # Keep the worker occupied until its transaction actually finishes.
        return await asyncio.shield(asyncio.create_task(finish()))

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
    async def migrate(self, target_version: int = 4) -> int:
        """Apply pending migrations up to ``target_version`` (inclusive).

        The F01 default (``4``) brings a fresh database to the base schema
        (004 core completion). Explicit lower targets still work for
        staged upgrades (``migrate(1)``/``migrate(2)``/``migrate(3)``);
        repeat calls are a no-op returning the current version; failures
        raise :class:`MigrationError` and never exit the process.
        """
        return await self._run(self._migrate_sync, int(target_version))

    def _migrate_sync(self, target_version: int = 4) -> int:
        con = self._require_con()
        want = int(target_version)
        if want < 1 or want > SCHEMA_VERSION:
            raise MigrationError(
                f"target_version={target_version!r} is outside 1..{SCHEMA_VERSION}"
            )
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS sl_schema_version ("
                "version INTEGER PRIMARY KEY, applied_at_ms BIGINT NOT NULL)"
            )
            cur = con.execute("SELECT max(version) AS v FROM sl_schema_version")
            current = cur.fetchone()[0]
            current_n = int(current) if current is not None else 0
            if current_n >= want:
                return current_n
            # F09: migrations resolve via resources.read_resource_text so the
            # frozen bundle needs no source checkout; domain DDL is unchanged.
            from diveintocrypto_desktop.resources import read_resource_text as _read_res

            for version, filename in MIGRATIONS:
                if version <= current_n or version > want:
                    continue
                try:
                    script = _read_res(f"shortlab/migrations/{filename}")
                except (OSError, FileNotFoundError) as exc:
                    raise MigrationError(
                        f"cannot read migration {filename}: {exc}"
                    ) from exc
                con.execute("BEGIN TRANSACTION")
                try:
                    for statement in _split_statements(script):
                        con.execute(statement)
                    applied_at_ms = time.time_ns() // 1_000_000
                    con.execute(
                        "INSERT INTO sl_schema_version(version, applied_at_ms) "
                        "SELECT ?, ? WHERE NOT EXISTS "
                        "(SELECT 1 FROM sl_schema_version WHERE version = ?)",
                        [version, applied_at_ms, version],
                    )
                    con.execute("COMMIT")
                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass
                    raise
            return want
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
            priority=PRIORITY_CRITICAL,
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
            priority=PRIORITY_CRITICAL,
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

    # -- unlock / social / catalyst (Task 17, design 19.2) -----------------------
    @staticmethod
    def _unlock_raw(record: UnlockEventRecord) -> dict[str, Any]:
        return {
            "event_id": record.event_id,
            "canonical_id": record.canonical_id,
            "known_at_ms": record.known_at_ms,
            "unlock_at_ms": record.unlock_at_ms,
            "amount_tokens": record.amount_tokens,
            "allocation_type": record.allocation_type,
            "source": record.source,
            "fetched_at_ms": record.fetched_at_ms,
        }

    @staticmethod
    def _catalyst_raw(record: CatalystEventRecord) -> dict[str, Any]:
        return {
            "event_id": record.event_id,
            "canonical_id": record.canonical_id,
            "known_at_ms": record.known_at_ms,
            "announced_at_ms": record.announced_at_ms,
            "effective_at_ms": record.effective_at_ms,
            "event_type": record.event_type,
            "severity": record.severity,
            "confidence": record.confidence,
            "source_url": record.source_url,
            "title": record.title,
        }

    async def save_unlock_events(
        self, events: Sequence[UnlockEventRecord]
    ) -> int:
        """Idempotent unlock-event save (re-saving identical rows is a no-op)."""
        return await self._run(self._save_unlock_events_sync, list(events))

    def _save_unlock_events_sync(self, events: list[UnlockEventRecord]) -> int:
        con = self._require_con()
        for event in events:
            if not event.event_id or not event.canonical_id:
                raise ValidationError("unlock event requires event_id/canonical_id")
            if event.unlock_at_ms <= 0 or event.known_at_ms < 0:
                raise ValidationError("unlock event has a bad unlock_at_ms/known_at_ms")
            if not event.amount_tokens > 0:
                raise ValidationError("unlock event amount_tokens must be positive")
            raw = self._unlock_raw(event)
            self._insert_immutable(
                con, "sl_unlock_event", "event_id = ? AND known_at_ms = ?",
                [event.event_id, event.known_at_ms], raw,
            )
        return len(events)

    async def list_unlock_events(
        self,
        canonical_id: str,
        start_ms: int,
        end_ms: int,
        known_at_ms: int,
    ) -> tuple[UnlockEventRecord, ...]:
        """Unlock events vesting in ``[start_ms, end_ms]`` known no later
        than ``known_at_ms`` (point-in-time replay excludes the future),
        deduped by ``event_id`` keeping the latest visible ``known_at_ms``.
        """
        return await self._run(
            self._list_unlock_events_sync, canonical_id, start_ms, end_ms, known_at_ms
        )

    def _list_unlock_events_sync(
        self, canonical_id: str, start_ms: int, end_ms: int, known_at_ms: int
    ) -> tuple[UnlockEventRecord, ...]:
        con = self._require_con()
        cur = con.execute(
            "SELECT * FROM sl_unlock_event WHERE canonical_id = ? "
            "AND unlock_at_ms >= ? AND unlock_at_ms <= ? AND known_at_ms <= ? "
            "ORDER BY unlock_at_ms ASC, known_at_ms ASC",
            [canonical_id, start_ms, end_ms, known_at_ms],
        )
        best: dict[str, dict[str, Any]] = {}
        for row in self._rows_to_dicts(cur):
            best[row["event_id"]] = row  # ascending known_at: last wins
        return tuple(
            UnlockEventRecord(
                event_id=row["event_id"],
                canonical_id=row["canonical_id"],
                known_at_ms=row["known_at_ms"],
                unlock_at_ms=row["unlock_at_ms"],
                amount_tokens=row["amount_tokens"],
                allocation_type=row["allocation_type"],
                source=row["source"],
                fetched_at_ms=row["fetched_at_ms"],
            )
            for row in sorted(best.values(), key=lambda r: (r["unlock_at_ms"], r["event_id"]))
        )

    async def save_social_snapshot(self, snapshot: SocialSnapshotRecord) -> str:
        return await self._run(self._save_social_snapshot_sync, snapshot)

    def _save_social_snapshot_sync(self, snapshot: SocialSnapshotRecord) -> str:
        con = self._require_con()
        if not snapshot.snapshot_id or not snapshot.canonical_id:
            raise ValidationError("social snapshot requires snapshot_id/canonical_id")
        raw = {
            "snapshot_id": snapshot.snapshot_id,
            "canonical_id": snapshot.canonical_id,
            "as_of_ms": snapshot.as_of_ms,
            "fetched_at_ms": snapshot.fetched_at_ms,
            "source": snapshot.source,
            "metrics_json": _canonical_json(dict(snapshot.metrics)),
        }
        self._insert_immutable(
            con, "sl_social_snapshot", "snapshot_id = ?", [snapshot.snapshot_id], raw
        )
        return snapshot.snapshot_id

    async def get_social_before(
        self, canonical_id: str, as_of_ms: int
    ) -> SocialSnapshotRecord | None:
        """Latest social snapshot with ``as_of_ms`` at or before the cutoff
        (point-in-time replay never reads the future)."""
        return await self._run(self._get_social_before_sync, canonical_id, as_of_ms)

    def _get_social_before_sync(
        self, canonical_id: str, as_of_ms: int
    ) -> SocialSnapshotRecord | None:
        con = self._require_con()
        cur = con.execute(
            "SELECT * FROM sl_social_snapshot WHERE canonical_id = ? "
            "AND as_of_ms <= ? ORDER BY as_of_ms DESC LIMIT 1",
            [canonical_id, as_of_ms],
        )
        rows = self._rows_to_dicts(cur)
        if not rows:
            return None
        raw = rows[0]
        return SocialSnapshotRecord(
            snapshot_id=raw["snapshot_id"],
            canonical_id=raw["canonical_id"],
            as_of_ms=raw["as_of_ms"],
            fetched_at_ms=raw["fetched_at_ms"],
            source=raw["source"],
            metrics=_parse_json_dict(raw["metrics_json"], "metrics_json"),
        )

    async def save_catalyst_events(
        self, events: Sequence[CatalystEventRecord]
    ) -> int:
        """Idempotent catalyst-event save (re-saving identical rows is a no-op)."""
        return await self._run(self._save_catalyst_events_sync, list(events))

    def _save_catalyst_events_sync(self, events: list[CatalystEventRecord]) -> int:
        con = self._require_con()
        for event in events:
            if not event.event_id or not event.canonical_id:
                raise ValidationError("catalyst event requires event_id/canonical_id")
            if event.announced_at_ms <= 0 or event.known_at_ms < 0:
                raise ValidationError(
                    "catalyst event has a bad announced_at_ms/known_at_ms"
                )
            raw = self._catalyst_raw(event)
            self._insert_immutable(
                con, "sl_catalyst_event", "event_id = ? AND known_at_ms = ?",
                [event.event_id, event.known_at_ms], raw,
            )
        return len(events)

    async def list_catalyst_events(
        self, canonical_id: str, known_at_ms: int
    ) -> tuple[CatalystEventRecord, ...]:
        """Catalyst events known no later than ``known_at_ms``, deduped by
        ``event_id`` keeping the latest visible ``known_at_ms``."""
        return await self._run(
            self._list_catalyst_events_sync, canonical_id, known_at_ms
        )

    def _list_catalyst_events_sync(
        self, canonical_id: str, known_at_ms: int
    ) -> tuple[CatalystEventRecord, ...]:
        con = self._require_con()
        cur = con.execute(
            "SELECT * FROM sl_catalyst_event WHERE canonical_id = ? "
            "AND known_at_ms <= ? ORDER BY announced_at_ms ASC, known_at_ms ASC",
            [canonical_id, known_at_ms],
        )
        best: dict[str, dict[str, Any]] = {}
        for row in self._rows_to_dicts(cur):
            best[row["event_id"]] = row  # ascending known_at: last wins
        return tuple(
            CatalystEventRecord(
                event_id=row["event_id"],
                canonical_id=row["canonical_id"],
                known_at_ms=row["known_at_ms"],
                announced_at_ms=row["announced_at_ms"],
                effective_at_ms=row["effective_at_ms"],
                event_type=row["event_type"],
                severity=row["severity"],
                confidence=row["confidence"],
                source_url=row["source_url"],
                title=row["title"],
            )
            for row in sorted(
                best.values(), key=lambda r: (r["announced_at_ms"], r["event_id"])
            )
        )

    # -- F01 contracts / source persistence (design A6, plan F01) -------------
    _ASSET_REQUIRED = frozenset(
        {"canonical_id", "display_symbol", "created_at_ms", "updated_at_ms"}
    )
    _ASSET_OPTIONAL = frozenset({"name", "categories"})
    _MAPPING_REQUIRED = frozenset(
        {"futures_symbol", "mapping_confidence", "mapping_source", "updated_at_ms"}
    )
    _MAPPING_OPTIONAL = frozenset(
        {
            "canonical_id", "spot_symbol", "contract_multiplier",
            "multiplier_source", "coingecko_id", "unlock_provider_id",
            "social_provider_id",
        }
    )
    _IDENTITY_REQUIRED = frozenset(
        {"identity_snapshot_id", "futures_symbol", "canonical_id",
         "mapping_version", "observed_at_ms"}
    )
    _IDENTITY_OPTIONAL = frozenset({"identity_json"})
    _RULES_REQUIRED = frozenset({"snapshot_id", "symbol", "known_at_ms"})
    _RULES_OPTIONAL = frozenset({"source_as_of_ms", "rules_json"})
    _LIFECYCLE_REQUIRED = frozenset(
        {"futures_symbol", "observed_at_ms", "first_seen_ms"}
    )
    _LIFECYCLE_OPTIONAL = frozenset(
        {"onboard_at_ms", "delivery_at_ms", "contract_type", "exchange_status",
         "contract_multiplier", "multiplier_source"}
    )
    _FUNDING_REQUIRED = frozenset({"symbol", "funding_time_ms", "funding_rate"})
    _FUNDING_OPTIONAL = frozenset({"mark_price"})
    _OBS_REQUIRED = frozenset(
        {"observation_id", "symbol", "funding_time_ms", "known_at_ms",
         "observation_status"}
    )
    _OBS_OPTIONAL = frozenset({"raw_json", "interval_hours", "interval_source"})
    _FUNDAMENTAL_REQUIRED = frozenset(
        {"snapshot_id", "canonical_id", "as_of_ms", "fetched_at_ms", "source"}
    )
    _FUNDAMENTAL_OPTIONAL = frozenset(
        {"market_cap_usd", "fdv_usd", "circulating_supply", "total_supply",
         "max_supply", "ath_price", "ath_date_ms"}
    )
    _CONFIG_REQUIRED = frozenset(
        {"policy_hash", "config_hash", "policy_version", "created_at_ms"}
    )
    _CONFIG_OPTIONAL = frozenset({"canonical_json"})
    _EVIDENCE_FILTERS = frozenset(
        {"symbol", "generation_id", "as_of_from_ms", "as_of_to_ms", "status"}
    )
    _RETENTION_KEYS = frozenset(
        {"funding_observation_ttl_ms", "cursor_ttl_ms", "score_ttl_ms"}
    )

    # -- assets ------------------------------------------------------------
    async def upsert_asset(
        self, record: Any, *, priority: int | None = None, trace: str | None = None
    ) -> str:
        """Insert or refresh the mutable ``sl_asset`` projection."""
        return await self._run(
            self._upsert_asset_sync, record, priority=priority, trace=trace
        )

    def _upsert_asset_sync(self, record: Any) -> str:
        data = _record_dict(record, self._ASSET_REQUIRED, self._ASSET_OPTIONAL, name="asset")
        categories = data.get("categories", ())
        if isinstance(categories, Mapping):
            raise ValidationError("asset.categories must be a tuple/list of str")
        cats = tuple(categories or ())
        for item in cats:
            if not isinstance(item, str):
                raise ValidationError("asset.categories must hold str")
        con = self._require_con()
        cid = data["canonical_id"]
        existing = self._fetch_raw(con, "sl_asset", "canonical_id = ?", [cid])
        if existing is None:
            con.execute(
                "INSERT INTO sl_asset (canonical_id, display_symbol, name, "
                "categories_json, created_at_ms, updated_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [cid, data["display_symbol"], data.get("name"),
                 _canonical_json(list(cats)), data["created_at_ms"],
                 data["updated_at_ms"]],
            )
        else:
            con.execute(
                "UPDATE sl_asset SET display_symbol = ?, name = ?, "
                "categories_json = ?, updated_at_ms = ? WHERE canonical_id = ?",
                [data["display_symbol"], data.get("name"),
                 _canonical_json(list(cats)), data["updated_at_ms"], cid],
            )
        return cid

    async def upsert_asset_mapping(
        self, record: Any, *, priority: int | None = None, trace: str | None = None
    ) -> str:
        """Insert or refresh the mutable ``sl_asset_mapping`` projection."""
        return await self._run(
            self._upsert_asset_mapping_sync, record, priority=priority, trace=trace
        )

    def _upsert_asset_mapping_sync(self, record: Any) -> str:
        data = _record_dict(
            record, self._MAPPING_REQUIRED, self._MAPPING_OPTIONAL, name="asset_mapping"
        )
        con = self._require_con()
        sym = data["futures_symbol"]
        cols = ["futures_symbol", "canonical_id", "spot_symbol",
                "contract_multiplier", "multiplier_source", "coingecko_id",
                "unlock_provider_id", "social_provider_id",
                "mapping_confidence", "mapping_source", "updated_at_ms"]
        values = [data.get(col) for col in cols]
        values[0] = sym
        if self._fetch_raw(con, "sl_asset_mapping", "futures_symbol = ?", [sym]) is None:
            con.execute(
                f"INSERT INTO sl_asset_mapping ({', '.join(cols)}) "
                f"VALUES ({', '.join(['?'] * len(cols))})",
                values,
            )
        else:
            sets = ", ".join(f"{col} = ?" for col in cols[1:])
            con.execute(
                f"UPDATE sl_asset_mapping SET {sets} WHERE futures_symbol = ?",
                values[1:] + [sym],
            )
        return sym

    # -- identity ----------------------------------------------------------
    @staticmethod
    def _identity_raw(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "identity_snapshot_id": data["identity_snapshot_id"],
            "futures_symbol": data["futures_symbol"],
            "canonical_id": data["canonical_id"],
            "mapping_version": data["mapping_version"],
            "observed_at_ms": data["observed_at_ms"],
            "identity_json": _json_text(data.get("identity_json", {}), name="identity_json"),
        }

    @staticmethod
    def _identity_from_row(raw: Mapping[str, Any]) -> IdentitySnapshotRecord:
        return IdentitySnapshotRecord(
            identity_snapshot_id=raw["identity_snapshot_id"],
            futures_symbol=raw["futures_symbol"],
            canonical_id=raw["canonical_id"],
            mapping_version=raw["mapping_version"],
            observed_at_ms=raw["observed_at_ms"],
            identity_json=_parse_json_dict(raw["identity_json"], "identity_json"),
        )

    async def save_identity_snapshot(
        self, record: Any, *, priority: int | None = None, trace: str | None = None
    ) -> str:
        """Store an immutable identity snapshot; same id + other content fails."""
        return await self._run(
            self._save_identity_snapshot_sync, record, priority=priority, trace=trace
        )

    def _save_identity_snapshot_sync(self, record: Any) -> str:
        data = _record_dict(
            record, self._IDENTITY_REQUIRED, self._IDENTITY_OPTIONAL, name="identity_snapshot"
        )
        raw = self._identity_raw(data)
        self._insert_immutable(
            self._require_con(), "sl_identity_snapshot",
            "identity_snapshot_id = ?", [raw["identity_snapshot_id"]], raw,
        )
        return raw["identity_snapshot_id"]

    async def get_identity_snapshot(
        self, snapshot_id: str, *, priority: int | None = None, trace: str | None = None
    ) -> IdentitySnapshotRecord | None:
        return await self._run(
            self._get_identity_snapshot_sync, snapshot_id, priority=priority, trace=trace
        )

    def _get_identity_snapshot_sync(self, snapshot_id: str) -> IdentitySnapshotRecord | None:
        raw = self._fetch_raw(
            self._require_con(), "sl_identity_snapshot",
            "identity_snapshot_id = ?", [snapshot_id],
        )
        return None if raw is None else self._identity_from_row(raw)

    # -- contract rules (immutable three-piece set) -------------------------
    @staticmethod
    def _rules_raw(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "snapshot_id": data["snapshot_id"],
            "symbol": data["symbol"],
            "source_as_of_ms": data.get("source_as_of_ms"),
            "known_at_ms": data["known_at_ms"],
            "rules_json": _json_text(data.get("rules_json", {}), name="rules_json"),
        }

    @staticmethod
    def _rules_from_row(raw: Mapping[str, Any]) -> ContractRulesSnapshotRecord:
        return ContractRulesSnapshotRecord(
            snapshot_id=raw["snapshot_id"],
            symbol=raw["symbol"],
            source_as_of_ms=raw["source_as_of_ms"],
            known_at_ms=raw["known_at_ms"],
            rules_json=_parse_json_dict(raw["rules_json"], "rules_json"),
        )

    async def save_contract_rules_snapshot(
        self, record: Any, *, priority: int | None = None, trace: str | None = None
    ) -> str:
        return await self._run(
            self._save_contract_rules_snapshot_sync, record,
            priority=priority, trace=trace,
        )

    def _save_contract_rules_snapshot_sync(self, record: Any) -> str:
        data = _record_dict(
            record, self._RULES_REQUIRED, self._RULES_OPTIONAL, name="contract_rules_snapshot"
        )
        raw = self._rules_raw(data)
        self._insert_immutable(
            self._require_con(), "sl_contract_rules_snapshot",
            "snapshot_id = ?", [raw["snapshot_id"]], raw,
        )
        return raw["snapshot_id"]

    async def get_contract_rules_snapshot(
        self, snapshot_id: str, *, priority: int | None = None, trace: str | None = None
    ) -> ContractRulesSnapshotRecord | None:
        return await self._run(
            self._get_contract_rules_snapshot_sync, snapshot_id,
            priority=priority, trace=trace,
        )

    def _get_contract_rules_snapshot_sync(
        self, snapshot_id: str
    ) -> ContractRulesSnapshotRecord | None:
        raw = self._fetch_raw(
            self._require_con(), "sl_contract_rules_snapshot",
            "snapshot_id = ?", [snapshot_id],
        )
        return None if raw is None else self._rules_from_row(raw)

    async def latest_contract_rules(
        self, symbol: str, cutoff_ms: int,
        *, priority: int | None = None, trace: str | None = None,
    ) -> ContractRulesSnapshotRecord | None:
        """Newest rules with ``known_at_ms <= cutoff_ms`` (point-in-time)."""
        return await self._run(
            self._latest_contract_rules_sync, symbol, _require_int(cutoff_ms, name="cutoff_ms"),
            priority=priority, trace=trace,
        )

    def _latest_contract_rules_sync(
        self, symbol: str, cutoff_ms: int
    ) -> ContractRulesSnapshotRecord | None:
        con = self._require_con()
        cur = con.execute(
            "SELECT * FROM sl_contract_rules_snapshot WHERE symbol = ? "
            "AND known_at_ms <= ? ORDER BY known_at_ms DESC LIMIT 1",
            [symbol, cutoff_ms],
        )
        rows = self._rows_to_dicts(cur)
        return None if not rows else self._rules_from_row(rows[0])

    # -- contract lifecycle --------------------------------------------------
    @staticmethod
    def _lifecycle_raw(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "futures_symbol": data["futures_symbol"],
            "observed_at_ms": data["observed_at_ms"],
            "onboard_at_ms": data.get("onboard_at_ms"),
            "first_seen_ms": data["first_seen_ms"],
            "delivery_at_ms": data.get("delivery_at_ms"),
            "contract_type": data.get("contract_type"),
            "exchange_status": data.get("exchange_status"),
            "contract_multiplier": data.get("contract_multiplier"),
            "multiplier_source": data.get("multiplier_source"),
        }

    @staticmethod
    def _lifecycle_from_row(raw: Mapping[str, Any]) -> ContractLifecycleRecord:
        return ContractLifecycleRecord(**{k: raw[k] for k in (
            "futures_symbol", "observed_at_ms", "onboard_at_ms",
            "first_seen_ms", "delivery_at_ms", "contract_type",
            "exchange_status", "contract_multiplier", "multiplier_source")})

    async def save_contract_lifecycle(
        self, record: Any, *, priority: int | None = None, trace: str | None = None
    ) -> None:
        """Append one lifecycle observation row (same key + other content fails)."""
        await self._run(
            self._save_contract_lifecycle_sync, record, priority=priority, trace=trace
        )

    def _save_contract_lifecycle_sync(self, record: Any) -> None:
        data = _record_dict(
            record, self._LIFECYCLE_REQUIRED, self._LIFECYCLE_OPTIONAL,
            name="contract_lifecycle",
        )
        raw = self._lifecycle_raw(data)
        self._insert_immutable(
            self._require_con(), "sl_contract_lifecycle",
            "futures_symbol = ? AND observed_at_ms = ?",
            [raw["futures_symbol"], raw["observed_at_ms"]], raw,
        )

    async def latest_contract_lifecycle(
        self, symbol: str, *, priority: int | None = None, trace: str | None = None
    ) -> ContractLifecycleRecord | None:
        return await self._run(
            self._latest_contract_lifecycle_sync, symbol, priority=priority, trace=trace
        )

    def _latest_contract_lifecycle_sync(self, symbol: str) -> ContractLifecycleRecord | None:
        con = self._require_con()
        cur = con.execute(
            "SELECT * FROM sl_contract_lifecycle WHERE futures_symbol = ? "
            "ORDER BY observed_at_ms DESC LIMIT 1",
            [symbol],
        )
        rows = self._rows_to_dicts(cur)
        return None if not rows else self._lifecycle_from_row(rows[0])

    async def list_tracked_symbols(
        self, *, priority: int | None = None, trace: str | None = None
    ) -> tuple[str, ...]:
        """Live universe + scored history + lifecycle symbols (sorted).

        Disappearing from the live list alone never delists a symbol here;
        F02 (universe input) and H01 (open Hedge legs) extend this union.
        """
        return await self._run(
            self._list_tracked_symbols_sync, priority=priority, trace=trace
        )

    def _list_tracked_symbols_sync(self) -> tuple[str, ...]:
        con = self._require_con()
        cur = con.execute(
            "SELECT futures_symbol AS s FROM sl_asset_mapping "
            "UNION SELECT symbol FROM sl_feature_snapshot "
            "UNION SELECT symbol FROM sl_score_snapshot "
            "UNION SELECT futures_symbol FROM sl_contract_lifecycle"
        )
        return tuple(sorted(row[0] for row in cur.fetchall()))

    # -- funding events + observations ----------------------------------------
    @staticmethod
    def _funding_raw(data: dict[str, Any]) -> dict[str, Any]:
        rate = data["funding_rate"]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)):
            raise ValidationError("funding_rate must be a number")
        return {
            "symbol": data["symbol"],
            "funding_time_ms": _require_int(data["funding_time_ms"], name="funding_time_ms"),
            "funding_rate": float(rate),
            "mark_price": data.get("mark_price"),
        }

    @staticmethod
    def _funding_from_row(raw: Mapping[str, Any]) -> FundingEventRecord:
        return FundingEventRecord(
            symbol=raw["symbol"],
            funding_time_ms=raw["funding_time_ms"],
            funding_rate=raw["funding_rate"],
            mark_price=raw["mark_price"],
        )

    async def upsert_funding_events(
        self, events: Sequence[Any],
        *, priority: int | None = PRIORITY_BACKFILL, trace: str | None = None,
    ) -> int:
        """Store canonical funding events; conflicts keep history + log observed.

        Same ``(symbol, funding_time)`` + same content is a no-op. Same key +
        different content keeps the stored canonical row and appends a
        ``CONFLICT`` row to ``sl_funding_observation`` -- canonical history
        is never UPDATE-overwritten.
        """
        return await self._run(
            self._upsert_funding_events_sync, list(events),
            priority=priority, trace=trace,
        )

    def _upsert_funding_events_sync(self, events: list[Any]) -> int:
        con = self._require_con()
        now_ms = time.time_ns() // 1_000_000
        for event in events:
            data = _record_dict(
                event, self._FUNDING_REQUIRED, self._FUNDING_OPTIONAL, name="funding_event"
            )
            raw = self._funding_raw(data)
            existing = self._fetch_raw(
                con, "sl_funding_event", "symbol = ? AND funding_time_ms = ?",
                [raw["symbol"], raw["funding_time_ms"]],
            )
            if existing is None:
                con.execute(
                    "INSERT INTO sl_funding_event (symbol, funding_time_ms, "
                    "funding_rate, mark_price) VALUES (?, ?, ?, ?)",
                    [raw["symbol"], raw["funding_time_ms"], raw["funding_rate"],
                     raw["mark_price"]],
                )
                continue
            if (existing["funding_rate"] == raw["funding_rate"]
                    and existing["mark_price"] == raw["mark_price"]):
                continue
            base = f"conflict:{raw['symbol']}:{raw['funding_time_ms']}:{now_ms}"
            observation_id, suffix = base, 0
            while self._fetch_raw(
                con, "sl_funding_observation", "observation_id = ?", [observation_id]
            ) is not None:
                suffix += 1
                observation_id = f"{base}#{suffix}"
            con.execute(
                "INSERT INTO sl_funding_observation (observation_id, symbol, "
                "funding_time_ms, known_at_ms, raw_json, interval_hours, "
                "interval_source, observation_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFLICT')",
                [observation_id, raw["symbol"], raw["funding_time_ms"], now_ms,
                 _canonical_json({"observed": raw,
                                  "reason": "FUNDING_CONFLICT_CANONICAL_KEPT"}),
                 None, None],
            )
        return len(events)

    async def list_funding_events(
        self, symbol: str, start_ms: int, end_ms: int,
        *, priority: int | None = None, trace: str | None = None,
    ) -> tuple[FundingEventRecord, ...]:
        return await self._run(
            self._list_funding_events_sync, symbol, start_ms, end_ms,
            priority=priority, trace=trace,
        )

    def _list_funding_events_sync(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> tuple[FundingEventRecord, ...]:
        con = self._require_con()
        cur = con.execute(
            "SELECT * FROM sl_funding_event WHERE symbol = ? "
            "AND funding_time_ms >= ? AND funding_time_ms <= ? "
            "ORDER BY funding_time_ms ASC",
            [symbol, start_ms, end_ms],
        )
        return tuple(self._funding_from_row(row) for row in self._rows_to_dicts(cur))

    @staticmethod
    def _observation_raw(data: dict[str, Any]) -> dict[str, Any]:
        interval = data.get("interval_hours")
        if interval is not None and (
            isinstance(interval, bool) or not isinstance(interval, (int, float))
        ):
            raise ValidationError("interval_hours must be a number or None")
        return {
            "observation_id": data["observation_id"],
            "symbol": data["symbol"],
            "funding_time_ms": _require_int(data["funding_time_ms"], name="funding_time_ms"),
            "known_at_ms": _require_int(data["known_at_ms"], name="known_at_ms"),
            "raw_json": _json_text(data.get("raw_json", {}), name="raw_json"),
            "interval_hours": None if interval is None else float(interval),
            "interval_source": data.get("interval_source"),
            "observation_status": data["observation_status"],
        }

    @staticmethod
    def _observation_from_row(raw: Mapping[str, Any]) -> FundingObservationRecord:
        parsed = json.loads(raw["raw_json"]) if isinstance(raw["raw_json"], str) else raw["raw_json"]
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        return FundingObservationRecord(
            observation_id=raw["observation_id"],
            symbol=raw["symbol"],
            funding_time_ms=raw["funding_time_ms"],
            known_at_ms=raw["known_at_ms"],
            raw_json=parsed,
            interval_hours=raw["interval_hours"],
            interval_source=raw["interval_source"],
            observation_status=raw["observation_status"],
        )

    async def save_funding_observation(
        self, record: Any, *, priority: int | None = PRIORITY_BACKFILL,
        trace: str | None = None,
    ) -> str:
        return await self._run(
            self._save_funding_observation_sync, record, priority=priority, trace=trace
        )

    def _save_funding_observation_sync(self, record: Any) -> str:
        data = _record_dict(
            record, self._OBS_REQUIRED, self._OBS_OPTIONAL, name="funding_observation"
        )
        raw = self._observation_raw(data)
        self._insert_immutable(
            self._require_con(), "sl_funding_observation",
            "observation_id = ?", [raw["observation_id"]], raw,
        )
        return raw["observation_id"]

    # -- fundamental -----------------------------------------------------------
    @staticmethod
    def _fundamental_raw(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "snapshot_id": data["snapshot_id"],
            "canonical_id": data["canonical_id"],
            "as_of_ms": _require_int(data["as_of_ms"], name="as_of_ms"),
            "fetched_at_ms": _require_int(data["fetched_at_ms"], name="fetched_at_ms"),
            "market_cap_usd": data.get("market_cap_usd"),
            "fdv_usd": data.get("fdv_usd"),
            "circulating_supply": data.get("circulating_supply"),
            "total_supply": data.get("total_supply"),
            "max_supply": data.get("max_supply"),
            "ath_price": data.get("ath_price"),
            "ath_date_ms": data.get("ath_date_ms"),
            "source": data["source"],
        }

    @staticmethod
    def _fundamental_from_row(raw: Mapping[str, Any]) -> FundamentalSnapshotRecord:
        return FundamentalSnapshotRecord(**{k: raw[k] for k in (
            "snapshot_id", "canonical_id", "as_of_ms", "fetched_at_ms",
            "market_cap_usd", "fdv_usd", "circulating_supply", "total_supply",
            "max_supply", "ath_price", "ath_date_ms", "source")})

    async def save_fundamental_snapshot(
        self, record: Any, *, priority: int | None = None, trace: str | None = None
    ) -> str:
        return await self._run(
            self._save_fundamental_snapshot_sync, record, priority=priority, trace=trace
        )

    def _save_fundamental_snapshot_sync(self, record: Any) -> str:
        data = _record_dict(
            record, self._FUNDAMENTAL_REQUIRED, self._FUNDAMENTAL_OPTIONAL,
            name="fundamental_snapshot",
        )
        raw = self._fundamental_raw(data)
        self._insert_immutable(
            self._require_con(), "sl_fundamental_snapshot",
            "snapshot_id = ?", [raw["snapshot_id"]], raw,
        )
        return raw["snapshot_id"]

    async def get_fundamental_before(
        self, canonical_id: str, cutoff_ms: int,
        *, priority: int | None = None, trace: str | None = None,
    ) -> FundamentalSnapshotRecord | None:
        """Newest fundamental snapshot with ``as_of_ms <= cutoff_ms``."""
        return await self._run(
            self._get_fundamental_before_sync, canonical_id,
            _require_int(cutoff_ms, name="cutoff_ms"),
            priority=priority, trace=trace,
        )

    def _get_fundamental_before_sync(
        self, canonical_id: str, cutoff_ms: int
    ) -> FundamentalSnapshotRecord | None:
        con = self._require_con()
        cur = con.execute(
            "SELECT * FROM sl_fundamental_snapshot WHERE canonical_id = ? "
            "AND as_of_ms <= ? ORDER BY as_of_ms DESC LIMIT 1",
            [canonical_id, cutoff_ms],
        )
        rows = self._rows_to_dicts(cur)
        return None if not rows else self._fundamental_from_row(rows[0])

    # -- config snapshots ------------------------------------------------------
    @staticmethod
    def _config_raw(data: dict[str, Any]) -> dict[str, Any]:
        return {
            "policy_hash": data["policy_hash"],
            "config_hash": data["config_hash"],
            "policy_version": data["policy_version"],
            "canonical_json": _json_text(
                data.get("canonical_json", {}), name="canonical_json"),
            "created_at_ms": _require_int(data["created_at_ms"], name="created_at_ms"),
        }

    @staticmethod
    def _config_from_row(raw: Mapping[str, Any]) -> ConfigSnapshotRecord:
        return ConfigSnapshotRecord(
            policy_hash=raw["policy_hash"],
            config_hash=raw["config_hash"],
            policy_version=raw["policy_version"],
            canonical_json=_parse_json_dict(raw["canonical_json"], "canonical_json"),
            created_at_ms=raw["created_at_ms"],
        )

    async def save_config_snapshot(
        self, record: Any, *, priority: int | None = None, trace: str | None = None
    ) -> str:
        return await self._run(
            self._save_config_snapshot_sync, record, priority=priority, trace=trace
        )

    def _save_config_snapshot_sync(self, record: Any) -> str:
        data = _record_dict(
            record, self._CONFIG_REQUIRED, self._CONFIG_OPTIONAL, name="config_snapshot"
        )
        raw = self._config_raw(data)
        self._insert_immutable(
            self._require_con(), "sl_config_snapshot",
            "policy_hash = ?", [raw["policy_hash"]], raw,
        )
        return raw["policy_hash"]

    async def get_config_snapshot(
        self, policy_hash: str, *, priority: int | None = None, trace: str | None = None
    ) -> ConfigSnapshotRecord | None:
        return await self._run(
            self._get_config_snapshot_sync, policy_hash, priority=priority, trace=trace
        )

    def _get_config_snapshot_sync(self, policy_hash: str) -> ConfigSnapshotRecord | None:
        raw = self._fetch_raw(
            self._require_con(), "sl_config_snapshot", "policy_hash = ?", [policy_hash]
        )
        return None if raw is None else self._config_from_row(raw)

    # -- cursors ---------------------------------------------------------------
    async def save_cursor(
        self, job_type: str, key: str, value: dict[str, Any],
        *, priority: int | None = PRIORITY_BACKFILL, trace: str | None = None,
    ) -> None:
        """Persist a restart-recoverable cursor (mutable, keyed PK)."""
        await self._run(
            self._save_cursor_sync, job_type, key, value, priority=priority, trace=trace
        )

    def _save_cursor_sync(
        self, job_type: str, key: str, value: dict[str, Any]
    ) -> None:
        if not job_type or not key:
            raise ValidationError("cursor requires job_type and key")
        if not isinstance(value, dict):
            raise ValidationError("cursor value must be a dict")
        con = self._require_con()
        now_ms = time.time_ns() // 1_000_000
        payload = _canonical_json(value)
        if self._fetch_raw(
            con, "sl_data_cursor", "job_type = ? AND cursor_key = ?", [job_type, key]
        ) is None:
            con.execute(
                "INSERT INTO sl_data_cursor (job_type, cursor_key, cursor_json, "
                "updated_at_ms) VALUES (?, ?, ?, ?)",
                [job_type, key, payload, now_ms],
            )
        else:
            con.execute(
                "UPDATE sl_data_cursor SET cursor_json = ?, updated_at_ms = ? "
                "WHERE job_type = ? AND cursor_key = ?",
                [payload, now_ms, job_type, key],
            )

    async def load_cursor(
        self, job_type: str, key: str,
        *, priority: int | None = None, trace: str | None = None,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._load_cursor_sync, job_type, key, priority=priority, trace=trace
        )

    def _load_cursor_sync(self, job_type: str, key: str) -> dict[str, Any] | None:
        raw = self._fetch_raw(
            self._require_con(), "sl_data_cursor",
            "job_type = ? AND cursor_key = ?", [job_type, key],
        )
        if raw is None:
            return None
        return _parse_json_dict(raw["cursor_json"], "cursor_json")

    # -- evidence / due scores ---------------------------------------------------
    async def list_scores_for_evidence(
        self, filters: Mapping[str, Any], limit: int, offset: int,
        *, priority: int | None = None, trace: str | None = None,
    ) -> EvidencePage:
        """Score page over SUCCEEDED generations only (running batches hidden)."""
        return await self._run(
            self._list_scores_for_evidence_sync, dict(filters), limit, offset,
            priority=priority, trace=trace,
        )

    def _list_scores_for_evidence_sync(
        self, filters: dict[str, Any], limit: int, offset: int
    ) -> EvidencePage:
        unknown = sorted(set(filters) - self._EVIDENCE_FILTERS)
        if unknown:
            raise ValidationError(f"evidence filters unknown: {unknown}")
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("limit must be an int in 1..200")
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an int >= 0")
        clauses = [
            "s.generation_id IN (SELECT job_id FROM sl_job_run "
            "WHERE job_type = 'score_refresh' AND status = 'SUCCEEDED')"
        ]
        params: list[Any] = []
        if filters.get("symbol") is not None:
            clauses.append("s.symbol = ?")
            params.append(filters["symbol"])
        if filters.get("generation_id") is not None:
            clauses.append("s.generation_id = ?")
            params.append(filters["generation_id"])
        if filters.get("as_of_from_ms") is not None:
            clauses.append("s.as_of_ms >= ?")
            params.append(filters["as_of_from_ms"])
        if filters.get("as_of_to_ms") is not None:
            clauses.append("s.as_of_ms <= ?")
            params.append(filters["as_of_to_ms"])
        if filters.get("status") is not None:
            if filters["status"] not in _ALLOWED_DISPLAY_STATUS:
                raise ValidationError(
                    f"status={filters['status']!r} is not a display status"
                )
            clauses.append("s.status = ?")
            params.append(filters["status"])
        where = " AND ".join(clauses)
        con = self._require_con()
        total = con.execute(
            f"SELECT count(*) FROM sl_score_snapshot s WHERE {where}", params
        ).fetchone()[0]
        cur = con.execute(
            f"SELECT s.* FROM sl_score_snapshot s WHERE {where} "
            "ORDER BY s.as_of_ms DESC, s.symbol ASC, s.snapshot_id ASC "
            "LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        items = tuple(self._score_from_raw(row) for row in self._rows_to_dicts(cur))
        return EvidencePage(total=int(total), items=items, limit=limit, offset=offset)

    async def list_due_scores(
        self, now_ms: int, limit: int = 200,
        *, priority: int | None = None, trace: str | None = None,
    ) -> tuple[OutcomeRecord, ...]:
        """PENDING outcomes with ``horizon_due_ms <= now`` for the grader.

        Ordered by due time, then symbol, then score id (deterministic).
        """
        return await self._run(
            self._list_due_scores_sync, _require_int(now_ms, name="now_ms"), limit,
            priority=priority, trace=trace,
        )

    def _list_due_scores_sync(
        self, now_ms: int, limit: int
    ) -> tuple[OutcomeRecord, ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationError("limit must be an int in 1..1000")
        con = self._require_con()
        cur = con.execute(
            "SELECT o.* FROM sl_forward_outcome o "
            "JOIN sl_score_snapshot s ON s.snapshot_id = o.score_snapshot_id "
            "WHERE o.outcome_status = 'PENDING' AND o.horizon_due_ms <= ? "
            "ORDER BY o.horizon_due_ms ASC, s.symbol ASC, o.score_snapshot_id ASC, "
            "o.horizon ASC, o.formula_version ASC, o.cost_config_hash ASC "
            "LIMIT ?",
            [now_ms, limit],
        )
        return tuple(OutcomeRecord(**row) for row in self._rows_to_dicts(cur))

    # -- retention ---------------------------------------------------------------
    @staticmethod
    def _retention_ttls(policy: Any) -> dict[str, int | None]:
        if isinstance(policy, RetentionPolicy):
            return {
                "funding_observation_ttl_ms": policy.funding_observation_ttl_ms,
                "cursor_ttl_ms": policy.cursor_ttl_ms,
                "score_ttl_ms": policy.score_ttl_ms,
            }
        if not isinstance(policy, Mapping):
            raise ValidationError("retention policy must be a mapping or RetentionPolicy")
        unknown = sorted(set(policy) - ShortLabRepository._RETENTION_KEYS)
        if unknown:
            raise ValidationError(f"retention policy has unknown keys: {unknown}")
        ttls: dict[str, int | None] = {}
        for key in ShortLabRepository._RETENTION_KEYS:
            value = policy.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValidationError(f"retention policy[{key!r}] must be a non-negative int or None")
            ttls[key] = value
        return ttls

    async def maintain_retention(
        self, policy: Any, now_ms: int, limit: int = 1000,
        *, priority: int | None = PRIORITY_RETENTION, trace: str | None = None,
    ) -> RetentionStats:
        """Delete at most ``limit`` expired rows in one transaction (<=1000).

        Only swept when the table TTL is set. Evidence is never deleted:
        ``CONFLICT`` observations, the latest SUCCEEDED generation and any
        score referenced by a forward outcome are always retained.
        """
        return await self._run(
            self._maintain_retention_sync, policy,
            _require_int(now_ms, name="now_ms"), limit,
            priority=priority, trace=trace,
        )

    def _maintain_retention_sync(
        self, policy: Any, now_ms: int, limit: int
    ) -> RetentionStats:
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValidationError("retention limit must be an int in 1..1000")
        ttls = self._retention_ttls(policy)
        con = self._require_con()
        deleted = 0
        errors = 0
        con.execute("BEGIN TRANSACTION")
        try:
            # H01 pin set (same worker txn, re-checked before every delete).
            # Roots: every plan (even CLOSED), every FCS/outcome evidence row
            # and every unresolved-alert plan. Traverse reference edges to
            # find pinned simulations/quotes/identity/config. Rings, broken
            # links or unknown types stop the hedge sweep with
            # RETENTION_REFERENCE_INVALID and conservative retain.
            pinned: set[tuple[str, str]] = set()
            hedge_valid = True
            hedge_tables_exist = False
            try:
                cur = con.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_name = 'sl_hedge_snapshot_reference'"
                )
                if cur.fetchone()[0] == 1:
                    cur = con.execute(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name = 'sl_hedge_simulation_snapshot'"
                    )
                    if cur.fetchone()[0] == 1:
                        hedge_tables_exist = True
            except Exception:
                hedge_tables_exist = False
            if hedge_tables_exist:
                try:
                    pinned = self._hedge_pinned_sync(con)
                except HedgeReferenceInvalidError:
                    hedge_valid = False
                    errors = 1
                except ReferenceNotFoundError:
                    hedge_valid = False
                    errors = 1
            pinned_funding_obs = {
                rid for (rtype, rid) in pinned if rtype == "FUNDING_OBSERVATION"
            }
            ttl = ttls["funding_observation_ttl_ms"]
            if ttl is not None and deleted < limit:
                cur = con.execute(
                    "SELECT observation_id FROM sl_funding_observation "
                    "WHERE observation_status = 'OBSERVED' AND known_at_ms < ? "
                    "ORDER BY known_at_ms ASC LIMIT ?",
                    [now_ms - ttl, limit - deleted],
                )
                for (observation_id,) in cur.fetchall():
                    if observation_id in pinned_funding_obs:
                        continue
                    # Re-check pin under the same txn (no cross-txn DELETE).
                    if hedge_tables_exist:
                        cur2 = con.execute(
                            "SELECT count(*) FROM sl_hedge_snapshot_reference "
                            "WHERE referenced_type = 'FUNDING_OBSERVATION' "
                            "AND referenced_id = ?",
                            [observation_id],
                        )
                        if cur2.fetchone()[0] > 0:
                            continue
                    con.execute(
                        "DELETE FROM sl_funding_observation WHERE observation_id = ?",
                        [observation_id],
                    )
                    deleted += 1
            ttl = ttls["cursor_ttl_ms"]
            if ttl is not None and deleted < limit:
                cur = con.execute(
                    "SELECT job_type, cursor_key FROM sl_data_cursor "
                    "WHERE updated_at_ms < ? ORDER BY updated_at_ms ASC LIMIT ?",
                    [now_ms - ttl, limit - deleted],
                )
                for job_type, cursor_key in cur.fetchall():
                    con.execute(
                        "DELETE FROM sl_data_cursor WHERE job_type = ? AND cursor_key = ?",
                        [job_type, cursor_key],
                    )
                    deleted += 1
            ttl = ttls["score_ttl_ms"]
            if ttl is not None and deleted < limit:
                cur = con.execute(
                    "SELECT job_id FROM sl_job_run WHERE job_type = 'score_refresh' "
                    "AND status = 'SUCCEEDED' AND finished_at_ms IS NOT NULL "
                    "ORDER BY finished_at_ms DESC, job_id ASC LIMIT 1"
                )
                row = cur.fetchone()
                if row is not None:
                    latest_gen = row[0]
                    cur = con.execute(
                        "SELECT snapshot_id FROM sl_score_snapshot "
                        "WHERE as_of_ms < ? AND generation_id <> ? "
                        "AND NOT EXISTS (SELECT 1 FROM sl_forward_outcome o "
                        "WHERE o.score_snapshot_id = sl_score_snapshot.snapshot_id) "
                        "ORDER BY as_of_ms ASC LIMIT ?",
                        [now_ms - ttl, latest_gen, limit - deleted],
                    )
                    for (snapshot_id,) in cur.fetchall():
                        con.execute(
                            "DELETE FROM sl_score_snapshot WHERE snapshot_id = ?",
                            [snapshot_id],
                        )
                        deleted += 1
            # H01 hedge sweep: isolated simulations 30d, ordinary quotes 1d,
            # plans/FCS/outcomes/alerts never auto-deleted here.
            if hedge_tables_exist and hedge_valid and deleted < limit:
                deleted = self._hedge_sweep_sync(con, now_ms, limit, deleted, pinned)
            retained = 0
            for table in ("sl_funding_observation", "sl_data_cursor", "sl_score_snapshot"):
                retained += con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if hedge_tables_exist:
                for table in ("sl_hedge_simulation_snapshot", "sl_spot_venue_snapshot",
                              "sl_hedge_snapshot_reference", "sl_hedge_plan",
                              "sl_hedge_fill_event", "sl_hedge_outcome",
                              "sl_funding_capture_snapshot", "sl_hedge_monitor_snapshot",
                              "sl_hedge_alert"):
                    try:
                        retained += con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    except Exception:
                        pass
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return RetentionStats(
            deleted=deleted, retained=int(retained), errors=int(errors), as_of_ms=now_ms
        )

    def _hedge_pinned_sync(self, con: Any) -> set[tuple[str, str]]:
        """Transitive pin set from plans/FCS/outcomes/unresolved-alert plans."""
        # Collect roots.
        roots: list[tuple[str, str]] = []
        for table, col, rtype in (
            ("sl_hedge_plan", "plan_id", "PLAN"),
            ("sl_funding_capture_snapshot", "snapshot_id", "FCS"),
            ("sl_hedge_outcome", "outcome_id", "OUTCOME"),
        ):
            try:
                cur = con.execute(f"SELECT {col} FROM {table}")
            except Exception:
                continue
            for (rid,) in cur.fetchall():
                roots.append((rtype, str(rid)))
        try:
            cur = con.execute(
                "SELECT DISTINCT plan_id FROM sl_hedge_alert "
                "WHERE state IN ('OPEN', 'ACKNOWLEDGED')"
            )
            for (pid,) in cur.fetchall():
                roots.append(("PLAN", str(pid)))
        except Exception:
            pass
        # BFS over reference edges; detect rings/broken/unknown.
        pinned: set[tuple[str, str]] = set(roots)
        queue: list[tuple[str, str]] = list(roots)
        visited_referrer: set[tuple[str, str]] = set()
        while queue:
            rtype, rid = queue.pop(0)
            if (rtype, rid) in visited_referrer:
                # Revisiting a referrer means a cycle in the pin graph.
                raise HedgeReferenceInvalidError(
                    "RETENTION_REFERENCE_INVALID: reference cycle")
            visited_referrer.add((rtype, rid))
            try:
                cur = con.execute(
                    "SELECT referenced_type, referenced_id FROM "
                    "sl_hedge_snapshot_reference WHERE referrer_type = ? "
                    "AND referrer_id = ?",
                    [rtype, rid],
                )
                edges = cur.fetchall()
            except Exception:
                continue
            for target_type, target_id in edges:
                if target_type not in self._HEDGE_REF_TYPES:
                    raise HedgeReferenceInvalidError(
                        "RETENTION_REFERENCE_INVALID: unknown reference type")
                if not self._hedge_ref_exists_sync(con, str(target_type), str(target_id)):
                    raise HedgeReferenceInvalidError(
                        "RETENTION_REFERENCE_INVALID: broken reference link")
                key = (str(target_type), str(target_id))
                if key not in pinned:
                    pinned.add(key)
                    queue.append(key)
                elif key in visited_referrer and (rtype, rid) != key:
                    # Back-edge forming a ring (A references B, B references A).
                    # Self-pins are allowed; cross-cycles are invalid.
                    pass
        # Explicit ring check: any edge whose target already expands to its
        # own referrer (two-hop cycle) is invalid. Single-pass BFS above
        # already guards revisits; do a cheap pairwise cycle probe for
        # two-node rings which BFS alone would otherwise retain.
        try:
            cur = con.execute(
                "SELECT referrer_type, referrer_id, referenced_type, referenced_id "
                "FROM sl_hedge_snapshot_reference"
            )
            edge_set = {
                ((str(a), str(b)), (str(c), str(d))) for a, b, c, d in cur.fetchall()
            }
            for (src, dst) in edge_set:
                if (dst, src) in edge_set and src != dst:
                    raise HedgeReferenceInvalidError(
                        "RETENTION_REFERENCE_INVALID: reference cycle")
        except HedgeReferenceInvalidError:
            raise
        except Exception:
            pass
        return pinned

    def _hedge_sweep_sync(
        self, con: Any, now_ms: int, limit: int, deleted: int,
        pinned: set[tuple[str, str]],
    ) -> int:
        day_ms = 86_400_000
        sim_ttl = 30 * day_ms
        quote_ttl = 1 * day_ms
        pinned_sims = {rid for (rtype, rid) in pinned if rtype == "SIMULATION"}
        pinned_quotes = {rid for (rtype, rid) in pinned if rtype == "VENUE_QUOTE"}
        # Isolated-but-still-retained simulations (age < 30d) pin their quotes
        # for at least 30d even though those quotes are not reachable from a
        # retained root.
        isolated_quote_ids: set[str] = set()
        try:
            cur = con.execute(
                "SELECT simulation_id, generated_at_ms FROM sl_hedge_simulation_snapshot"
            )
            sim_rows = cur.fetchall()
        except Exception:
            return deleted
        isolated_sim_ids: set[str] = set()
        for sim_id, gen_ms in sim_rows:
            if str(sim_id) not in pinned_sims and int(gen_ms) >= now_ms - sim_ttl:
                isolated_sim_ids.add(str(sim_id))
        if isolated_sim_ids:
            try:
                cur = con.execute(
                    "SELECT referrer_id, referenced_id FROM sl_hedge_snapshot_reference "
                    "WHERE referrer_type = 'SIMULATION' AND referenced_type = 'VENUE_QUOTE'"
                )
                for referrer_id, referenced_id in cur.fetchall():
                    if str(referrer_id) in isolated_sim_ids:
                        isolated_quote_ids.add(str(referenced_id))
            except Exception:
                pass
        # 1. Expired unpinned simulations (30d).
        if deleted < limit:
            cur = con.execute(
                "SELECT simulation_id FROM sl_hedge_simulation_snapshot "
                "WHERE generated_at_ms < ? ORDER BY generated_at_ms ASC LIMIT ?",
                [now_ms - sim_ttl, limit - deleted + 50],
            )
            for (sim_id,) in cur.fetchall():
                if deleted >= limit:
                    break
                if str(sim_id) in pinned_sims:
                    continue
                # Re-check pin inside the same txn before deleting.
                cur2 = con.execute(
                    "SELECT count(*) FROM sl_hedge_snapshot_reference "
                    "WHERE referenced_type = 'SIMULATION' AND referenced_id = ?",
                    [str(sim_id)],
                )
                # Only retained-root references pin; dangling edges from
                # already-deleted referrers do not. Re-derive: if any
                # remaining referrer is itself pinned/retained, skip.
                still_pinned = False
                cur3 = con.execute(
                    "SELECT referrer_type, referrer_id FROM sl_hedge_snapshot_reference "
                    "WHERE referenced_type = 'SIMULATION' AND referenced_id = ?",
                    [str(sim_id)],
                )
                for rtype, rid in cur3.fetchall():
                    if (str(rtype), str(rid)) in pinned:
                        still_pinned = True
                        break
                    # A plan/FCS/outcome referrer always pins (roots are
                    # never deleted here), even if the BFS seed missed it
                    # due to a concurrent edge write.
                    if str(rtype) in ("PLAN", "FCS", "OUTCOME"):
                        still_pinned = True
                        break
                if still_pinned:
                    continue
                con.execute(
                    "DELETE FROM sl_hedge_snapshot_reference WHERE "
                    "(referrer_type = 'SIMULATION' AND referrer_id = ?) OR "
                    "(referenced_type = 'SIMULATION' AND referenced_id = ?)",
                    [str(sim_id), str(sim_id)],
                )
                con.execute(
                    "DELETE FROM sl_hedge_simulation_snapshot WHERE simulation_id = ?",
                    [str(sim_id)],
                )
                deleted += 1
        # 2. Ordinary unreferenced quotes (1d); isolated-simulation quotes
        # keep their 30d cover via the isolated set above.
        if deleted < limit:
            try:
                cur = con.execute(
                    "SELECT snapshot_id FROM sl_spot_venue_snapshot "
                    "WHERE as_of_ms < ? ORDER BY as_of_ms ASC LIMIT ?",
                    [now_ms - quote_ttl, limit - deleted + 50],
                )
                for (snap_id,) in cur.fetchall():
                    if deleted >= limit:
                        break
                    sid = str(snap_id)
                    if sid in pinned_quotes or sid in isolated_quote_ids:
                        continue
                    cur2 = con.execute(
                        "SELECT referrer_type, referrer_id FROM sl_hedge_snapshot_reference "
                        "WHERE referenced_type = 'VENUE_QUOTE' AND referenced_id = ?",
                        [sid],
                    )
                    skip = False
                    for rtype, rid in cur2.fetchall():
                        if (str(rtype), str(rid)) in pinned or str(rid) in isolated_sim_ids:
                            skip = True
                            break
                        if str(rtype) in ("PLAN", "FCS", "OUTCOME", "SIMULATION"):
                            # Any live referrer pins the quote; the BFS pin
                            # set already covers retained roots, so a live
                            # edge here means an isolated (30d) simulation.
                            skip = True
                            break
                    if skip:
                        continue
                    con.execute(
                        "DELETE FROM sl_hedge_snapshot_reference WHERE "
                        "referenced_type = 'VENUE_QUOTE' AND referenced_id = ?",
                        [sid],
                    )
                    con.execute(
                        "DELETE FROM sl_spot_venue_snapshot WHERE snapshot_id = ?",
                        [sid],
                    )
                    deleted += 1
            except Exception:
                pass
        return deleted

    # -- hedge (H01, design B28/B29) ---------------------------------------
    # All Hedge writes run on the same single-worker queue; ledger mutation
    # (`apply_hedge_event`) is one indivisible worker op (BEGIN→checks→
    # insert→leg rebuild→version CAS→COMMIT, ROLLBACK on any error).
    # Balances accumulate from immutable `event_json` decimal strings with
    # integer coefficient/exponent alignment (B28.0); DOUBLE projections in
    # `sl_hedge_plan`/`sl_hedge_leg` are never read back for balances.

    _HEDGE_REF_TYPES = frozenset(
        {"FCS", "SIMULATION", "PLAN", "OUTCOME", "VENUE_QUOTE", "IDENTITY",
         "CONFIG", "VENUE_MAPPING", "CONTRACT_RULES", "FUNDING_OBSERVATION"}
    )
    _HEDGE_PLAN_STATUS = frozenset(
        {"DRAFT", "READY", "PARTIALLY_FILLED", "ACTIVE", "CLOSING", "CLOSED", "INVALID"}
    )
    _HEDGE_MODES = frozenset({"ABSOLUTE", "RELATIVE"})
    _HEDGE_EVENT_TYPES = frozenset(
        {"OPEN_FUTURES_SHORT", "OPEN_SPOT_LONG", "CLOSE_FUTURES_SHORT",
         "CLOSE_SPOT_LONG", "CORRECT_REVERSAL", "CORRECT_SUPERSEDE",
         "FUNDING_RECEIPT", "LIQUIDATION"}
    )
    _HEDGE_LEG_TYPES = frozenset({"FUTURES_SHORT", "SPOT_LONG", "FUNDING"})
    _HEDGE_ALERT_SEV = frozenset({"INFO", "WARN", "CRITICAL"})
    _HEDGE_ALERT_STATE = frozenset({"OPEN", "ACKNOWLEDGED", "RESOLVED"})
    _HEDGE_READINESS = frozenset({"READY", "NOT_READY", "BLOCKED"})

    @staticmethod
    def _hedge_parse_decimal(name: str, value: Any) -> Decimal:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{name} must be a non-empty decimal string")
        try:
            parsed = Decimal(value.strip())
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise ValidationError(f"{name} is not a decimal string: {value!r}") from exc
        if not parsed.is_finite():
            raise ValidationError(f"{name} must be finite")
        return parsed

    @staticmethod
    def _hedge_json_text(value: Any, *, name: str) -> str:
        """Canonical JSON for hedge columns accepting objects or arrays."""
        if isinstance(value, Mapping):
            return _canonical_json(dict(value))
        if isinstance(value, (list, tuple)):
            return _canonical_json(list(value))
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"{name} is not valid JSON: {exc}") from exc
            return _canonical_json(parsed)
        raise ValidationError(f"{name} must be a mapping, list or JSON string")

    @staticmethod
    def _hedge_sum_strs(values: Sequence[str]) -> str:
        """Exact sum via integer coefficient/exponent alignment (B28.0).

        Immune to the default 28-digit context: every input is scaled to
        the minimum exponent as an integer, summed as Python ints, then
        re-encoded. ``0.1+0.2`` is exactly ``0.3``; dust is never rounded
        to zero by an epsilon.
        """
        if not values:
            return "0"
        decoded = [ShortLabRepository._hedge_parse_decimal("qty", v) for v in values]
        min_exp = min(d.as_tuple().exponent for d in decoded)
        total = 0
        for d in decoded:
            sign, digits, exp = d.as_tuple()
            coeff = 0
            for digit in digits:
                coeff = coeff * 10 + digit
            if sign:
                coeff = -coeff
            total += coeff * (10 ** (exp - min_exp))
        if total == 0:
            return "0"
        negative = total < 0
        total = abs(total)
        digits_str = str(total)
        if min_exp >= 0:
            out = digits_str + ("0" * min_exp)
        else:
            point = len(digits_str) + min_exp
            if point > 0:
                out = digits_str[:point] + "." + digits_str[point:]
            else:
                out = "0." + ("0" * (-point)) + digits_str
            out = out.rstrip("0").rstrip(".")
            if out == "-0" or out == "":
                out = "0"
        return ("-" if negative else "") + out

    @staticmethod
    def _hedge_require_tables(con: Any, tables: Sequence[str]) -> None:
        for table in tables:
            cur = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                [table],
            )
            if cur.fetchone()[0] == 0:
                raise ReferenceNotFoundError(
                    f"hedge table {table} is unavailable (migrate to schema 5)"
                )

    def _hedge_ref_exists_sync(self, con: Any, ref_type: str, ref_id: str) -> bool:
        if ref_type == "FCS":
            return self._fetch_raw(con, "sl_funding_capture_snapshot",
                                   "snapshot_id = ?", [ref_id]) is not None
        if ref_type == "SIMULATION":
            return self._fetch_raw(con, "sl_hedge_simulation_snapshot",
                                   "simulation_id = ?", [ref_id]) is not None
        if ref_type == "PLAN":
            return self._fetch_raw(con, "sl_hedge_plan", "plan_id = ?", [ref_id]) is not None
        if ref_type == "OUTCOME":
            return self._fetch_raw(con, "sl_hedge_outcome",
                                   "outcome_id = ?", [ref_id]) is not None
        if ref_type == "VENUE_QUOTE":
            return self._fetch_raw(con, "sl_spot_venue_snapshot",
                                   "snapshot_id = ?", [ref_id]) is not None
        if ref_type == "IDENTITY":
            return self._fetch_raw(con, "sl_identity_snapshot",
                                   "identity_snapshot_id = ?", [ref_id]) is not None
        if ref_type == "CONFIG":
            return self._fetch_raw(con, "sl_config_snapshot",
                                   "policy_hash = ?", [ref_id]) is not None
        if ref_type == "VENUE_MAPPING":
            return self._fetch_raw(con, "sl_hedge_venue_mapping",
                                   "mapping_id = ?", [ref_id]) is not None
        if ref_type == "CONTRACT_RULES":
            return self._fetch_raw(con, "sl_contract_rules_snapshot",
                                   "snapshot_id = ?", [ref_id]) is not None
        if ref_type == "FUNDING_OBSERVATION":
            return self._fetch_raw(con, "sl_funding_observation",
                                   "observation_id = ?", [ref_id]) is not None
        raise HedgeReferenceInvalidError(
            f"RETENTION_REFERENCE_INVALID: unknown reference type {ref_type!r}"
        )

    @staticmethod
    def _hedge_event_to_json(event: Any) -> dict[str, Any]:
        if dataclasses.is_dataclass(event) and not isinstance(event, type):
            data = dataclasses.asdict(event)
        elif isinstance(event, Mapping):
            data = dict(event)
        else:
            raise ValidationError("hedge event must be a mapping or frozen DTO")
        # Normalise tuples to lists for JSON storage.
        for key, value in list(data.items()):
            if isinstance(value, tuple):
                data[key] = list(value)
        return data

    def _hedge_validate_event(self, event_json: dict[str, Any]) -> dict[str, Any]:
        if event_json.get("schema_version") != "hedge-event-v1":
            raise ValidationError("event_json.schema_version must be hedge-event-v1")
        leg_type = event_json.get("leg_type")
        event_type = event_json.get("event_type")
        if leg_type not in self._HEDGE_LEG_TYPES:
            raise ValidationError(f"leg_type={leg_type!r} is not a hedge leg")
        if event_type not in self._HEDGE_EVENT_TYPES:
            raise ValidationError(f"event_type={event_type!r} is not a hedge event")
        if event_json.get("source") != "USER_ENTERED":
            raise ValidationError('event source must be "USER_ENTERED"')
        if event_type == "FUNDING_RECEIPT":
            amount = event_json.get("amount")
            if amount is None:
                raise ValidationError("FUNDING_RECEIPT requires amount")
            self._hedge_parse_decimal("amount", amount)
            for key in ("native_qty", "canonical_qty", "native_price",
                        "gross_qty", "net_qty"):
                if event_json.get(key) is not None:
                    raise ValidationError(f"FUNDING_RECEIPT must leave {key} null")
        else:
            for key in ("native_qty", "canonical_qty", "native_price",
                        "fee_amount", "fee_usd", "gas_usd", "gross_qty", "net_qty"):
                value = event_json.get(key)
                if value is not None:
                    parsed = self._hedge_parse_decimal(key, value)
                    if key in ("native_qty", "canonical_qty", "gross_qty", "net_qty"):
                        if parsed < 0:
                            raise ValidationError(f"{key} must be non-negative")
            if event_json.get("native_qty") is None and event_json.get("canonical_qty") is None:
                raise ValidationError("non-funding events require a quantity")
        return event_json

    def _hedge_effective_qty_sync(self, con: Any, plan_id: str, payload: dict[str, Any]) -> str | None:
        # Zero is a real value, so never use truthiness to choose a quantity.
        for key in ("net_qty", "canonical_qty"):
            if payload.get(key) is not None:
                return str(payload[key])
        native = payload.get("native_qty")
        if native is None:
            return None
        if payload.get("leg_type") != "FUTURES_SHORT":
            return str(native)
        multiplier = self._hedge_multiplier_sync(con, plan_id)
        if multiplier is None:
            raise ValidationError("native futures quantity requires a verified contract multiplier")
        with localcontext() as ctx:
            ctx.prec = 80
            return format(self._hedge_parse_decimal("native_qty", str(native)) * multiplier, "f")

    def _hedge_multiplier_sync(self, con: Any, plan_id: str) -> Decimal | None:
        plan = self._fetch_raw(con, "sl_hedge_plan", "plan_id = ?", [plan_id])
        config = json.loads(plan["plan_config_json"]) if plan else {}
        identity = config.get("identity", {})
        raw = config.get("contract_multiplier") or identity.get("contract_multiplier")
        if raw is None:
            return None
        multiplier = self._hedge_parse_decimal("contract_multiplier", str(raw))
        if multiplier <= 0:
            raise ValidationError("contract_multiplier must be positive")
        return multiplier

    def _hedge_aggregate_sync(self, con: Any, plan_id: str) -> list[dict[str, Any]]:
        """Rebuild per-leg positions from immutable events only (B28.0).

        Never reads ``sl_hedge_leg`` DOUBLEs or ``SQL SUM(qty)``; never
        compares floats. FUNDING_RECEIPT never enters quantity maths.
        """
        cur = con.execute(
            "SELECT event_id, event_json FROM sl_hedge_fill_event "
            "WHERE plan_id = ? ORDER BY executed_at_ms ASC, event_id ASC",
            [plan_id],
        )
        per_leg: dict[str, dict[str, Any]] = {}
        for event_id, event_json_raw in cur.fetchall():
            try:
                payload = json.loads(event_json_raw) if isinstance(event_json_raw, str) else dict(event_json_raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("event_type") == "FUNDING_RECEIPT":
                continue
            leg = payload.get("leg_type")
            if leg not in ("FUTURES_SHORT", "SPOT_LONG"):
                continue
            # Prefer net_qty (base-fee deducted) then canonical then native.
            qty_str = self._hedge_effective_qty_sync(con, plan_id, payload)
            if qty_str is None:
                continue
            entry = per_leg.setdefault(leg, {"open": [], "closed": [], "ids": [], "prices": []})
            entry["ids"].append(event_id)
            etype = payload.get("event_type")
            if etype in ("OPEN_FUTURES_SHORT", "OPEN_SPOT_LONG"):
                entry["open"].append(str(qty_str))
                if payload.get("native_price") is not None:
                    price = self._hedge_parse_decimal("native_price", str(payload["native_price"]))
                    multiplier = self._hedge_multiplier_sync(con, plan_id) if leg == "FUTURES_SHORT" else None
                    with localcontext() as ctx:
                        ctx.prec = 80
                        canonical_price = price / multiplier if multiplier is not None else price
                    entry["prices"].append((str(qty_str), str(canonical_price)))
            elif etype in ("CLOSE_FUTURES_SHORT", "CLOSE_SPOT_LONG"):
                entry["closed"].append(str(qty_str))
            elif etype in ("CORRECT_REVERSAL", "CORRECT_SUPERSEDE", "LIQUIDATION"):
                # Corrections close the referenced quantity; treat as close
                # unless the payload explicitly re-opens (net positive open).
                entry["closed"].append(str(qty_str))
        positions: list[dict[str, Any]] = []
        for leg in ("FUTURES_SHORT", "SPOT_LONG"):
            entry = per_leg.get(leg, {"open": [], "closed": [], "ids": [], "prices": []})
            open_qty = self._hedge_sum_strs(entry["open"]) if entry["open"] else "0"
            closed_qty = self._hedge_sum_strs(entry["closed"]) if entry["closed"] else "0"
            # remaining = open - closed via integer maths (negate closed).
            remaining = self._hedge_sum_strs(
                entry["open"] + [f"-{q}" if not str(q).startswith("-") else str(q)[1:]
                                 for q in entry["closed"]]
            ) if (entry["open"] or entry["closed"]) else "0"
            # Weighted avg entry price with 80-digit context (display only
            # for the position; zero/over-close checks never use it).
            wavg: str | None = None
            if entry["prices"]:
                try:
                    with localcontext() as ctx:
                        ctx.prec = 80
                        num = sum((Decimal(q) * Decimal(p) for q, p in entry["prices"]),
                                  Decimal(0))
                        den = sum((Decimal(q) for q, _ in entry["prices"]), Decimal(0))
                        wavg = str(num / den) if den != 0 else None
                except (InvalidOperation, ValueError, ArithmeticError):
                    wavg = None
            positions.append({
                "plan_id": plan_id,
                "leg_type": leg,
                "open_qty": open_qty,
                "closed_qty": closed_qty,
                "remaining_qty": remaining,
                "gross_qty": open_qty,
                "net_qty": remaining,
                "weighted_avg_price": wavg,
                "event_ids": list(entry["ids"]),
            })
        return positions

    # -- simulations ------------------------------------------------------
    async def save_hedge_simulation(
        self, record: Any, references: Sequence[Any] | None = None,
        *, priority: int | None = PRIORITY_SOURCE_SCORE, trace: str | None = None,
    ) -> str:
        return await self._run(
            self._save_hedge_simulation_sync, record, list(references or []),
            priority=priority, trace=trace,
        )

    def _save_hedge_simulation_sync(self, record: Any, references: list[Any]) -> str:
        data = _record_dict(
            record,
            {"simulation_id", "symbol", "generated_at_ms", "expires_at_ms",
             "formula_version", "policy_hash"},
            {"input_json", "result_json", "source_meta_json"},
            name="hedge_simulation",
        )
        sim_id = data["simulation_id"]
        if not isinstance(sim_id, str) or not sim_id:
            raise ValidationError("simulation_id must be a non-empty str")
        gen = _require_int(data["generated_at_ms"], name="generated_at_ms")
        exp = _require_int(data["expires_at_ms"], name="expires_at_ms")
        if exp <= gen:
            raise ValidationError("expires_at_ms must be > generated_at_ms")
        if not data["formula_version"] or not data["policy_hash"]:
            raise ValidationError("formula_version/policy_hash must be non-empty")
        input_json = _json_text(data.get("input_json", {}), name="input_json")
        result_json = _json_text(data.get("result_json", {}), name="result_json")
        source_meta = _json_text(data.get("source_meta_json", {}), name="source_meta_json")
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_simulation_snapshot",
                                         "sl_hedge_snapshot_reference"))
        raw = {
            "simulation_id": sim_id,
            "symbol": data["symbol"],
            "generated_at_ms": gen,
            "expires_at_ms": exp,
            "formula_version": data["formula_version"],
            "policy_hash": data["policy_hash"],
            "input_json": input_json,
            "result_json": result_json,
            "source_meta_json": source_meta,
        }
        con.execute("BEGIN TRANSACTION")
        try:
            self._insert_immutable(
                con, "sl_hedge_simulation_snapshot", "simulation_id = ?", [sim_id], raw)
            if references:
                self._hedge_insert_refs_sync(con, "SIMULATION", sim_id, references, gen)
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return sim_id

    async def get_hedge_simulation(self, simulation_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_hedge_simulation_sync, simulation_id)

    def _get_hedge_simulation_sync(self, simulation_id: str) -> dict[str, Any] | None:
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_hedge_simulation_snapshot",))
        except ReferenceNotFoundError:
            return None
        raw = self._fetch_raw(con, "sl_hedge_simulation_snapshot",
                              "simulation_id = ?", [simulation_id])
        if raw is None:
            return None
        out = dict(raw)
        for key in ("input_json", "result_json", "source_meta_json"):
            try:
                out[key] = json.loads(out[key]) if isinstance(out[key], str) else out[key]
            except (TypeError, ValueError):
                pass
        return out

    # -- snapshot references (same-transaction integrity, B28.7.2) ---------
    def _hedge_insert_refs_sync(
        self, con: Any, referrer_type: str, referrer_id: str,
        references: Sequence[Any], created_at_ms: int,
    ) -> None:
        if referrer_type not in self._HEDGE_REF_TYPES:
            raise HedgeReferenceInvalidError(
                f"RETENTION_REFERENCE_INVALID: unknown referrer type {referrer_type!r}")
        for ref in references:
            if isinstance(ref, Mapping):
                rtype = ref.get("referenced_type")
                rid = ref.get("referenced_id")
                purpose = ref.get("purpose", "pin")
            elif isinstance(ref, (list, tuple)) and len(ref) >= 2:
                rtype, rid = ref[0], ref[1]
                purpose = ref[2] if len(ref) > 2 else "pin"
            else:
                raise ValidationError("reference must be a mapping or (type, id[, purpose])")
            if rtype not in self._HEDGE_REF_TYPES:
                raise HedgeReferenceInvalidError(
                    f"RETENTION_REFERENCE_INVALID: unknown referenced type {rtype!r}")
            if not isinstance(rid, str) or not rid:
                raise ValidationError("referenced_id must be a non-empty str")
            if not self._hedge_ref_exists_sync(con, rtype, rid):
                raise ReferenceNotFoundError(f"referenced {rtype}:{rid} not found")
            # Idempotent edge insert (same referrer+target+purpose twice is a no-op).
            existing = self._fetch_raw(
                con, "sl_hedge_snapshot_reference",
                "referrer_type = ? AND referrer_id = ? AND referenced_type = ? "
                "AND referenced_id = ? AND purpose = ?",
                [referrer_type, referrer_id, rtype, rid, purpose],
            )
            if existing is None:
                con.execute(
                    "INSERT INTO sl_hedge_snapshot_reference (referrer_type, referrer_id, "
                    "referenced_type, referenced_id, purpose, created_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [referrer_type, referrer_id, rtype, rid, purpose, created_at_ms],
                )

    async def save_snapshot_references(
        self, referrer_type: str, referrer_id: str, references: Sequence[Any],
        created_at_ms: int, *, priority: int | None = PRIORITY_CRITICAL,
        trace: str | None = None,
    ) -> int:
        return await self._run(
            self._save_snapshot_references_sync, referrer_type, referrer_id,
            list(references), _require_int(created_at_ms, name="created_at_ms"),
            priority=priority, trace=trace,
        )

    def _save_snapshot_references_sync(
        self, referrer_type: str, referrer_id: str,
        references: list[Any], created_at_ms: int,
    ) -> int:
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_snapshot_reference",))
        if not self._hedge_ref_exists_sync(con, referrer_type, referrer_id):
            # Referrer rows for SIMULATION/PLAN/FCS/OUTCOME must exist; for
            # transient callers the edge write still validates targets.
            pass
        con.execute("BEGIN TRANSACTION")
        try:
            self._hedge_insert_refs_sync(con, referrer_type, referrer_id,
                                         references, created_at_ms)
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return len(references)

    async def list_snapshot_references(
        self, referrer_type: str | None = None, referrer_id: str | None = None,
        referenced_type: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        return await self._run(
            self._list_snapshot_references_sync,
            referrer_type, referrer_id, referenced_type,
        )

    def _list_snapshot_references_sync(
        self, referrer_type: str | None, referrer_id: str | None,
        referenced_type: str | None,
    ) -> tuple[dict[str, Any], ...]:
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_hedge_snapshot_reference",))
        except ReferenceNotFoundError:
            return ()
        clauses: list[str] = []
        params: list[Any] = []
        if referrer_type is not None:
            clauses.append("referrer_type = ?")
            params.append(referrer_type)
        if referrer_id is not None:
            clauses.append("referrer_id = ?")
            params.append(referrer_id)
        if referenced_type is not None:
            clauses.append("referenced_type = ?")
            params.append(referenced_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = con.execute(
            f"SELECT * FROM sl_hedge_snapshot_reference{where} "
            "ORDER BY created_at_ms ASC, referrer_id ASC, referenced_id ASC",
            params,
        )
        return tuple(self._rows_to_dicts(cur))

    # -- plans (idempotent client_request_id first, then version/expiry) --
    async def create_hedge_plan(
        self, record: Any, references: Sequence[Any] | None = None,
        *, priority: int | None = PRIORITY_CRITICAL, trace: str | None = None,
    ) -> str:
        return await self._run(
            self._create_hedge_plan_sync, record, list(references or []),
            priority=priority, trace=trace,
        )

    def _create_hedge_plan_sync(self, record: Any, references: list[Any]) -> str:
        data = _record_dict(
            record,
            {"plan_id", "symbol", "canonical_id", "mode", "status",
             "simulation_id", "client_request_id", "plan_config_json",
             "created_at_ms", "updated_at_ms"},
            {"target_hedge_ratio", "futures_notional_usd", "futures_contract_qty",
             "canonical_futures_qty", "spot_venue", "spot_symbol", "spot_chain",
             "spot_contract", "target_spot_qty", "target_spot_notional_usd",
             "leverage", "margin_mode", "margin_usd", "liquidation_price",
             "liquidation_price_source", "planned_hold_days", "fcs_snapshot_id",
             "plan_safety_score", "activated_at_ms", "closed_at_ms", "plan_version"},
            name="hedge_plan",
        )
        plan_id = data["plan_id"]
        client_request_id = data["client_request_id"]
        simulation_id = data["simulation_id"]
        if data["mode"] not in self._HEDGE_MODES:
            raise ValidationError(f"mode={data['mode']!r} must be ABSOLUTE/RELATIVE")
        if data["status"] not in self._HEDGE_PLAN_STATUS:
            raise ValidationError(f"status={data['status']!r} is not a plan status")
        created = _require_int(data["created_at_ms"], name="created_at_ms")
        updated = _require_int(data["updated_at_ms"], name="updated_at_ms")
        plan_config_json = _json_text(data["plan_config_json"], name="plan_config_json")
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_plan", "sl_hedge_simulation_snapshot",
                                         "sl_hedge_snapshot_reference"))
        con.execute("BEGIN TRANSACTION")
        try:
            # Idempotency first (H01.2): same client_request_id with identical
            # simulation+config returns the existing plan even when the
            # simulation has since expired; different payload is 409.
            existing = self._fetch_raw(con, "sl_hedge_plan",
                                       "client_request_id = ?", [client_request_id])
            if existing is not None:
                same_sim = existing["simulation_id"] == simulation_id
                same_cfg = existing["plan_config_json"] == plan_config_json
                if same_sim and same_cfg:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass
                    return str(existing["plan_id"])
                raise HedgeIdempotencyError(
                    "IDEMPOTENCY_PAYLOAD_MISMATCH: client_request_id reuses a "
                    "different simulation/config payload")
            # New plan: simulation must exist and cover creation time.
            sim = self._fetch_raw(con, "sl_hedge_simulation_snapshot",
                                  "simulation_id = ?", [simulation_id])
            if sim is None:
                raise ReferenceNotFoundError(f"simulation {simulation_id!r} not found")
            if int(sim["expires_at_ms"]) < created:
                raise ValidationError("simulation is expired for a new plan")

            def _f(name: str, default: Any = None) -> Any:
                value = data.get(name, default)
                return None if value is None else float(value)

            version = data.get("plan_version", 1)
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValidationError("plan_version must be an int >= 1")
            row = {
                "plan_id": plan_id,
                "symbol": data["symbol"],
                "canonical_id": data["canonical_id"],
                "mode": data["mode"],
                "status": data["status"],
                "target_hedge_ratio": _f("target_hedge_ratio", 1.0),
                "futures_notional_usd": _f("futures_notional_usd", 0.0),
                "futures_contract_qty": _f("futures_contract_qty", 0.0),
                "canonical_futures_qty": _f("canonical_futures_qty", 0.0),
                "spot_venue": data.get("spot_venue") or "BINANCE_SPOT",
                "spot_symbol": data.get("spot_symbol"),
                "spot_chain": data.get("spot_chain"),
                "spot_contract": data.get("spot_contract"),
                "target_spot_qty": _f("target_spot_qty", 0.0),
                "target_spot_notional_usd": _f("target_spot_notional_usd"),
                "leverage": _f("leverage"),
                "margin_mode": data.get("margin_mode"),
                "margin_usd": _f("margin_usd"),
                "liquidation_price": _f("liquidation_price"),
                "liquidation_price_source": data.get("liquidation_price_source"),
                "planned_hold_days": data.get("planned_hold_days"),
                "fcs_snapshot_id": data.get("fcs_snapshot_id"),
                "simulation_id": simulation_id,
                "client_request_id": client_request_id,
                "plan_safety_score": _f("plan_safety_score"),
                "plan_config_json": plan_config_json,
                "created_at_ms": created,
                "activated_at_ms": data.get("activated_at_ms"),
                "closed_at_ms": data.get("closed_at_ms"),
                "updated_at_ms": updated,
                "plan_version": int(version),
            }
            cols = ", ".join(row.keys())
            placeholders = ", ".join(["?"] * len(row))
            con.execute(f"INSERT INTO sl_hedge_plan ({cols}) VALUES ({placeholders})",
                        list(row.values()))
            # Same-transaction reference: plan -> simulation (+ extras).
            self._hedge_insert_refs_sync(
                con, "PLAN", plan_id,
                [("SIMULATION", simulation_id, "plan-simulation")] + list(references),
                created)
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return plan_id

    async def get_hedge_plan(self, plan_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_hedge_plan_sync, plan_id)

    def _get_hedge_plan_sync(self, plan_id: str) -> dict[str, Any] | None:
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_hedge_plan",))
        except ReferenceNotFoundError:
            return None
        raw = self._fetch_raw(con, "sl_hedge_plan", "plan_id = ?", [plan_id])
        if raw is None:
            return None
        out = dict(raw)
        try:
            out["plan_config_json"] = json.loads(out["plan_config_json"])
        except (TypeError, ValueError):
            pass
        return out

    async def list_hedge_plans(
        self, status: str | None = None, limit: int = 50, offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        return await self._run(
            self._list_hedge_plans_sync, status, limit, offset)

    def _list_hedge_plans_sync(
        self, status: str | None, limit: int, offset: int,
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("limit must be an int in 1..200")
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an int >= 0")
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_hedge_plan",))
        except ReferenceNotFoundError:
            return ()
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            if status not in self._HEDGE_PLAN_STATUS:
                raise ValidationError(f"status={status!r} is not a plan status")
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = con.execute(
            f"SELECT * FROM sl_hedge_plan{where} "
            "ORDER BY updated_at_ms DESC, plan_id ASC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = self._rows_to_dicts(cur)
        for row in rows:
            try:
                row["plan_config_json"] = json.loads(row["plan_config_json"])
            except (TypeError, ValueError):
                pass
        return tuple(rows)

    async def update_hedge_plan(
        self, plan_id: str, status: str, expected_version: int, updated_at_ms: int,
    ) -> int:
        return await self._run(
            self._update_hedge_plan_sync, plan_id, status,
            expected_version, updated_at_ms, priority=PRIORITY_CRITICAL,
        )

    def _update_hedge_plan_sync(
        self, plan_id: str, status: str, expected_version: int, updated_at_ms: int,
    ) -> int:
        if status not in self._HEDGE_PLAN_STATUS:
            raise ValidationError(f"status={status!r} is not a plan status")
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_plan",))
        con.execute("BEGIN TRANSACTION")
        try:
            cur = con.execute(
                "UPDATE sl_hedge_plan SET status = ?, updated_at_ms = ?, "
                "plan_version = plan_version + 1 WHERE plan_id = ? AND plan_version = ?",
                [status, _require_int(updated_at_ms, name="updated_at_ms"),
                 plan_id, int(expected_version)],
            )
            # DuckDB rowcount for UPDATE: fetch via changes()? Fall back to
            # re-reading the version when the driver reports None.
            changed = cur.rowcount if getattr(cur, "rowcount", None) not in (None, -1) else None
            if changed is None:
                row = self._fetch_raw(con, "sl_hedge_plan", "plan_id = ?", [plan_id])
                if row is None:
                    raise ReferenceNotFoundError(f"plan {plan_id!r} not found")
                if int(row["plan_version"]) != int(expected_version) + 1:
                    raise HedgeVersionConflictError("PLAN_VERSION_CONFLICT")
            elif changed != 1:
                raise HedgeVersionConflictError("PLAN_VERSION_CONFLICT")
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return int(expected_version) + 1

    # -- ledger: single indivisible worker op (B28.7.1) -------------------
    async def apply_hedge_event(
        self, plan_id: str, client_event_id: str, expected_version: int,
        event: Any, *, recorded_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Append one immutable fill event and rebuild the leg projection.

        One worker transaction only -- never split into separate await
        read-balance / write-event / write-projection / bump-version calls.
        """
        return await self._run(
            self._apply_hedge_event_sync, plan_id, client_event_id,
            expected_version, event,
            (time.time_ns() // 1_000_000) if recorded_at_ms is None else int(recorded_at_ms),
            priority=PRIORITY_CRITICAL,
        )

    def _apply_hedge_event_sync(
        self, plan_id: str, client_event_id: str, expected_version: int,
        event: Any, recorded_at_ms: int,
    ) -> dict[str, Any]:
        if not plan_id or not client_event_id:
            raise ValidationError("plan_id/client_event_id must be non-empty")
        event_json = self._hedge_validate_event(self._hedge_event_to_json(event))
        executed = event_json.get("executed_at_ms")
        if not isinstance(executed, int) or isinstance(executed, bool):
            raise ValidationError("executed_at_ms must be an int")
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_plan", "sl_hedge_fill_event",
                                         "sl_hedge_leg"))
        canonical_event = _canonical_json(event_json)
        con.execute("BEGIN TRANSACTION")
        try:
            # 1. Idempotency first: same (plan, client_event_id).
            dup = self._fetch_raw(
                con, "sl_hedge_fill_event",
                "plan_id = ? AND client_event_id = ?", [plan_id, client_event_id])
            if dup is not None:
                if dup["event_json"] == canonical_event:
                    positions = self._hedge_aggregate_sync(con, plan_id)
                    plan = self._fetch_raw(con, "sl_hedge_plan", "plan_id = ?", [plan_id])
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass
                    return {
                        "event_id": dup["event_id"],
                        "plan_id": plan_id,
                        "plan_version": int(plan["plan_version"]) if plan else int(expected_version),
                        "positions": positions,
                        "balance_source": "CONFIRMED",
                        "estimated": False,
                    }
                raise HedgeIdempotencyError(
                    "IDEMPOTENCY_PAYLOAD_MISMATCH: client_event_id reuses a "
                    "different event payload")
            # 2. Version + balance from immutable events (never DOUBLE).
            plan = self._fetch_raw(con, "sl_hedge_plan", "plan_id = ?", [plan_id])
            if plan is None:
                raise ReferenceNotFoundError(f"plan {plan_id!r} not found")
            if int(plan["plan_version"]) != int(expected_version):
                raise HedgeVersionConflictError("PLAN_VERSION_CONFLICT")
            if event_json["event_type"] != "FUNDING_RECEIPT":
                self._hedge_effective_qty_sync(con, plan_id, event_json)
            positions_before = self._hedge_aggregate_sync(con, plan_id)
            remaining_by_leg = {p["leg_type"]: p["remaining_qty"] for p in positions_before}
            etype = event_json["event_type"]
            leg = event_json["leg_type"]
            if etype in ("CLOSE_FUTURES_SHORT", "CLOSE_SPOT_LONG") and leg in remaining_by_leg:
                qty_str = self._hedge_effective_qty_sync(con, plan_id, event_json) or "0"
                remaining = Decimal(remaining_by_leg[leg] or "0")
                closing = self._hedge_parse_decimal("close_qty", str(qty_str))
                if closing > remaining:
                    raise ValidationError("cannot close more than the remaining quantity")
            # Supersede/reverse guards: same plan only, single reverse.
            for key in ("supersedes_event_id", "reverses_event_id"):
                target = event_json.get(key)
                if target is not None:
                    row = self._fetch_raw(con, "sl_hedge_fill_event",
                                          "event_id = ?", [target])
                    if row is None or row["plan_id"] != plan_id:
                        raise ValidationError(f"{key} must reference the same plan")
                    if key == "reverses_event_id":
                        cur = con.execute(
                            "SELECT count(*) FROM sl_hedge_fill_event WHERE event_json LIKE ?",
                            [f'%"reverses_event_id": "{target}"%'],
                        )
                        if cur.fetchone()[0] >= 1:
                            raise ValidationError("an event may only be reversed once")
                    cur = con.execute(
                        "SELECT closed_qty FROM sl_hedge_leg WHERE plan_id = ? AND leg_type = ?",
                        [plan_id, leg],
                    )
                    _ = cur.fetchall()  # projection read is display-only; skip
            # 3. Insert the immutable event.
            event_id = f"{plan_id}#{client_event_id}"
            if self._fetch_raw(con, "sl_hedge_fill_event", "event_id = ?",
                               [event_id]) is not None:
                event_id = f"{plan_id}#{client_event_id}#{uuid.uuid4().hex[:8]}"
            con.execute(
                "INSERT INTO sl_hedge_fill_event (event_id, plan_id, client_event_id, "
                "leg_type, event_type, executed_at_ms, recorded_at_ms, event_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [event_id, plan_id, client_event_id, leg, etype,
                 executed, int(recorded_at_ms), canonical_event],
            )
            # 4. Rebuild leg projections from events (DOUBLE display only).
            positions = self._hedge_aggregate_sync(con, plan_id)
            for pos in positions:
                if pos["leg_type"] == "FUNDING":
                    continue
                try:
                    qty_f = float(Decimal(pos["open_qty"]))
                except (InvalidOperation, ValueError):
                    qty_f = 0.0
                try:
                    closed_f = float(Decimal(pos["closed_qty"]))
                except (InvalidOperation, ValueError):
                    closed_f = 0.0
                try:
                    wavg_f = float(Decimal(pos["weighted_avg_price"])) if pos["weighted_avg_price"] else None
                except (InvalidOperation, ValueError):
                    wavg_f = None
                leg_id = f"{plan_id}:{pos['leg_type']}"
                venue = "BINANCE_SPOT" if pos["leg_type"] == "SPOT_LONG" else "FUTURES"
                side = "BUY" if pos["leg_type"] == "SPOT_LONG" else "SELL"
                state = "CLOSED" if Decimal(pos["remaining_qty"]) == 0 and Decimal(pos["open_qty"]) != 0 else "OPEN"
                existing_leg = self._fetch_raw(con, "sl_hedge_leg", "leg_id = ?", [leg_id])
                if existing_leg is None:
                    con.execute(
                        "INSERT INTO sl_hedge_leg (leg_id, plan_id, leg_type, venue, side, "
                        "qty, canonical_qty, avg_entry_price, actual_fee_usd, actual_gas_usd, "
                        "opened_at_ms, closed_qty, avg_exit_price, exit_fee_usd, exit_gas_usd, "
                        "closed_at_ms, state, updated_at_ms) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [leg_id, plan_id, pos["leg_type"], venue, side, qty_f, qty_f,
                         wavg_f, None, None, executed, closed_f, None, None, None,
                         None, state, int(recorded_at_ms)],
                    )
                else:
                    con.execute(
                        "UPDATE sl_hedge_leg SET qty = ?, canonical_qty = ?, "
                        "avg_entry_price = ?, closed_qty = ?, state = ?, "
                        "updated_at_ms = ? WHERE leg_id = ?",
                        [qty_f, qty_f, wavg_f, closed_f, state,
                         int(recorded_at_ms), leg_id],
                    )
            # 5. CAS version bump (exactly one row) + status roll-forward.
            before_status = str(plan["status"])
            after_status = before_status
            if before_status in ("DRAFT", "READY") and any(
                    Decimal(p["open_qty"]) != 0 for p in positions):
                after_status = "PARTIALLY_FILLED"
            if positions and all(Decimal(p["remaining_qty"]) == 0
                                 and Decimal(p["open_qty"]) != 0 for p in positions
                                 if p["leg_type"] in ("FUTURES_SHORT", "SPOT_LONG")):
                # Both legs fully closed (RELATIVE legs were never required
                # to be equal) -> CLOSED; dust stays an explicit record.
                after_status = "CLOSED"
            cur = con.execute(
                "UPDATE sl_hedge_plan SET plan_version = plan_version + 1, "
                "status = ?, updated_at_ms = ? WHERE plan_id = ? AND plan_version = ?",
                [after_status, int(recorded_at_ms), plan_id, int(expected_version)],
            )
            changed = cur.rowcount if getattr(cur, "rowcount", None) not in (None, -1) else 1
            if changed != 1:
                raise HedgeVersionConflictError("PLAN_VERSION_CONFLICT")
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return {
            "event_id": event_id,
            "plan_id": plan_id,
            "plan_version": int(expected_version) + 1,
            "positions": positions,
            "balance_source": "CONFIRMED",
            "estimated": False,
        }

    async def append_hedge_fill_event(
        self, plan_id: str, client_event_id: str, expected_version: int, event: Any,
    ) -> dict[str, Any]:
        """H01 storage alias for the H06 domain call (same atomic path)."""
        return await self.apply_hedge_event(plan_id, client_event_id,
                                            expected_version, event)

    async def correct_hedge_fill_event(
        self, plan_id: str, client_event_id: str, expected_version: int, event: Any,
    ) -> dict[str, Any]:
        """Explicit reversal/supersede event (same atomic path; single reverse)."""
        payload = dict(event) if isinstance(event, Mapping) else dataclasses.asdict(event)
        if payload.get("event_type") not in ("CORRECT_REVERSAL", "CORRECT_SUPERSEDE"):
            raise ValidationError("correction events must be CORRECT_REVERSAL/SUPERSEDE")
        return await self.apply_hedge_event(plan_id, client_event_id,
                                            expected_version, payload)

    async def aggregate_hedge_position(self, plan_id: str) -> tuple[dict[str, Any], ...]:
        return await self._run(self._aggregate_hedge_position_sync, plan_id)

    def _aggregate_hedge_position_sync(self, plan_id: str) -> tuple[dict[str, Any], ...]:
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_fill_event",))
        return tuple(self._hedge_aggregate_sync(con, plan_id))

    # -- funding capture snapshots --------------------------------------
    async def save_funding_capture_snapshot(self, record: Any) -> str:
        return await self._run(self._save_fcs_sync, record)

    def _save_fcs_sync(self, record: Any) -> str:
        data = _record_dict(
            record,
            {"snapshot_id", "symbol", "canonical_id", "as_of_ms", "fcs_version",
             "fcs_config_hash", "reference_notional_usd", "module_scores_json",
             "funding_metrics_json", "venue_summary_json", "risk_json",
             "readiness", "reasons_json", "created_at_ms"},
            {"fcs", "basis_json"},
            name="funding_capture_snapshot",
        )
        if data["readiness"] not in self._HEDGE_READINESS:
            raise ValidationError("readiness must be READY/NOT_READY/BLOCKED")
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_funding_capture_snapshot",))
        raw = {
            "snapshot_id": data["snapshot_id"],
            "symbol": data["symbol"],
            "canonical_id": data["canonical_id"],
            "as_of_ms": _require_int(data["as_of_ms"], name="as_of_ms"),
            "fcs_version": data["fcs_version"],
            "fcs_config_hash": data["fcs_config_hash"],
            "reference_notional_usd": float(data["reference_notional_usd"]),
            "fcs": None if data.get("fcs") is None else float(data["fcs"]),
            "module_scores_json": _json_text(data["module_scores_json"], name="module_scores_json"),
            "funding_metrics_json": _json_text(data["funding_metrics_json"], name="funding_metrics_json"),
            "venue_summary_json": _json_text(data["venue_summary_json"], name="venue_summary_json"),
            "basis_json": (None if data.get("basis_json") is None
                           else _json_text(data["basis_json"], name="basis_json")),
            "risk_json": _json_text(data["risk_json"], name="risk_json"),
            "readiness": data["readiness"],
            "reasons_json": self._hedge_json_text(data["reasons_json"], name="reasons_json"),
            "created_at_ms": _require_int(data["created_at_ms"], name="created_at_ms"),
        }
        self._insert_immutable(con, "sl_funding_capture_snapshot",
                               "snapshot_id = ?", [raw["snapshot_id"]], raw)
        return raw["snapshot_id"]

    async def list_funding_opportunities(
        self, symbol: str | None = None, limit: int = 50, offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        return await self._run(
            self._list_fcs_sync, symbol, limit, offset)

    async def list_fcs(
        self, symbol: str | None = None, limit: int = 50, offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        return await self.list_funding_opportunities(symbol, limit, offset)

    def _list_fcs_sync(self, symbol: str | None, limit: int, offset: int,
                       ) -> tuple[dict[str, Any], ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("limit must be an int in 1..200")
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an int >= 0")
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_funding_capture_snapshot",))
        except ReferenceNotFoundError:
            return ()
        clauses, params = [], []
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = con.execute(
            f"SELECT * FROM sl_funding_capture_snapshot{where} "
            "ORDER BY as_of_ms DESC, snapshot_id ASC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return tuple(self._rows_to_dicts(cur))

    # -- spot venue snapshots + mapping ----------------------------------
    async def save_spot_venue_snapshot(self, record: Any) -> str:
        return await self._run(self._save_venue_sync, record)

    async def save_venue_snapshot(self, record: Any) -> str:
        return await self.save_spot_venue_snapshot(record)

    def _save_venue_sync(self, record: Any) -> str:
        data = _record_dict(
            record,
            {"snapshot_id", "canonical_id", "venue", "as_of_ms", "fetched_at_ms",
             "reference_notional_usd", "quote_json", "status"},
            {"venue_symbol", "chain", "contract_address", "expires_at_ms",
             "reason_code"},
            name="spot_venue_snapshot",
        )
        quote_text = _json_text(data["quote_json"], name="quote_json")
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_spot_venue_snapshot",))
        raw = {
            "snapshot_id": data["snapshot_id"],
            "canonical_id": data["canonical_id"],
            "venue": data["venue"],
            "venue_symbol": data.get("venue_symbol"),
            "chain": data.get("chain"),
            "contract_address": data.get("contract_address"),
            "as_of_ms": _require_int(data["as_of_ms"], name="as_of_ms"),
            "fetched_at_ms": _require_int(data["fetched_at_ms"], name="fetched_at_ms"),
            "expires_at_ms": data.get("expires_at_ms"),
            "reference_notional_usd": float(data["reference_notional_usd"]),
            "quote_json": quote_text,
            "status": data["status"],
            "reason_code": data.get("reason_code"),
        }
        self._insert_immutable(con, "sl_spot_venue_snapshot",
                               "snapshot_id = ?", [raw["snapshot_id"]], raw)
        return raw["snapshot_id"]

    async def list_spot_venues(
        self, canonical_id: str | None = None, venue: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        return await self._run(
            self._list_venues_sync, canonical_id, venue, limit, offset)

    async def list_venues(
        self, canonical_id: str | None = None, venue: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        return await self.list_spot_venues(canonical_id, venue, limit, offset)

    def _list_venues_sync(self, canonical_id: str | None, venue: str | None,
                          limit: int, offset: int) -> tuple[dict[str, Any], ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("limit must be an int in 1..200")
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an int >= 0")
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_spot_venue_snapshot",))
        except ReferenceNotFoundError:
            return ()
        clauses, params = [], []
        if canonical_id is not None:
            clauses.append("canonical_id = ?")
            params.append(canonical_id)
        if venue is not None:
            clauses.append("venue = ?")
            params.append(venue)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = con.execute(
            f"SELECT * FROM sl_spot_venue_snapshot{where} "
            "ORDER BY as_of_ms DESC, snapshot_id ASC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return tuple(self._rows_to_dicts(cur))

    async def save_hedge_venue_mapping(self, record: Any) -> str:
        return await self._run(self._save_venue_mapping_sync, record)

    def _save_venue_mapping_sync(self, record: Any) -> str:
        data = _record_dict(
            record,
            {"mapping_id", "canonical_id", "venue", "instrument_id",
             "mapping_version", "verification_json", "verified_at_ms"},
            set(),
            name="hedge_venue_mapping",
        )
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_venue_mapping",))
        raw = {
            "mapping_id": data["mapping_id"],
            "canonical_id": data["canonical_id"],
            "venue": data["venue"],
            "instrument_id": data["instrument_id"],
            "mapping_version": data["mapping_version"],
            "verification_json": _json_text(data["verification_json"],
                                            name="verification_json"),
            "verified_at_ms": _require_int(data["verified_at_ms"], name="verified_at_ms"),
        }
        self._insert_immutable(con, "sl_hedge_venue_mapping",
                               "mapping_id = ?", [raw["mapping_id"]], raw)
        return raw["mapping_id"]

    async def read_frozen_quote(self, snapshot_id: str) -> dict[str, Any] | None:
        """HistoricalMarketProvider: archived quote by snapshot id."""
        rows = await self.list_spot_venues(limit=200, offset=0)
        for row in rows:
            if row.get("snapshot_id") == snapshot_id:
                return row
        return await self._run(self._read_frozen_quote_sync, snapshot_id)

    def _read_frozen_quote_sync(self, snapshot_id: str) -> dict[str, Any] | None:
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_spot_venue_snapshot",))
        except ReferenceNotFoundError:
            return None
        return self._fetch_raw(con, "sl_spot_venue_snapshot",
                               "snapshot_id = ?", [snapshot_id])

    async def find_frozen_quote(
        self, canonical_id: str, venue: str, canonical_qty: str,
        at_ms: int, max_skew_ms: int = 5000,
    ) -> dict[str, Any] | None:
        """HistoricalMarketProvider: unique archived quote for grading.

        Same identity/venue/net quantity, ``source_as_of`` in
        ``[at_ms-max_skew_ms, at_ms]``, ordered ``source_as_of DESC,
        snapshotId ASC``; missing returns None (never approximates).
        """
        return await self._run(
            self._find_frozen_quote_sync, canonical_id, venue, canonical_qty,
            _require_int(at_ms, name="at_ms"), int(max_skew_ms),
        )

    def _find_frozen_quote_sync(
        self, canonical_id: str, venue: str, canonical_qty: str,
        at_ms: int, max_skew_ms: int,
    ) -> dict[str, Any] | None:
        want_qty = self._hedge_parse_decimal("canonical_qty", canonical_qty)
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_spot_venue_snapshot",))
        except ReferenceNotFoundError:
            return None
        cur = con.execute(
            "SELECT * FROM sl_spot_venue_snapshot WHERE canonical_id = ? AND venue = ? "
            "AND as_of_ms >= ? AND as_of_ms <= ? ORDER BY as_of_ms DESC, snapshot_id ASC",
            [canonical_id, venue, at_ms - max_skew_ms, at_ms],
        )
        for row in self._rows_to_dicts(cur):
            try:
                quote = json.loads(row["quote_json"]) if isinstance(row["quote_json"], str) else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(quote, dict):
                continue
            row_qty = quote.get("requested_canonical_qty") or quote.get("requestedCanonicalQty")
            if row_qty is None:
                continue
            try:
                if self._hedge_parse_decimal("row_qty", str(row_qty)) != want_qty:
                    continue
            except ValidationError:
                continue
            return row
        return None

    # -- monitor ----------------------------------------------------------
    async def save_hedge_monitor_snapshot(self, record: Any) -> str:
        return await self._run(self._save_monitor_sync, record)

    async def save_monitor_snapshot(self, record: Any) -> str:
        return await self.save_hedge_monitor_snapshot(record)

    def _save_monitor_sync(self, record: Any) -> str:
        data = _record_dict(
            record,
            {"snapshot_id", "plan_id", "as_of_ms", "source_meta_json",
             "quality_json", "metrics_json", "status", "created_at_ms"},
            {"actual_hedge_ratio", "residual_short_notional_usd", "mark_price",
             "spot_price", "current_basis_pct", "basis_pnl_usd",
             "estimated_settled_funding_usd", "projected_next_funding_usd",
             "spot_pnl_usd", "futures_pnl_usd", "known_cost_usd",
             "estimated_exit_cost_usd", "net_pnl_before_exit_usd",
             "estimated_net_pnl_after_exit_usd", "liquidation_distance",
             "exit_liquidity_json", "safety_score"},
            name="hedge_monitor_snapshot",
        )
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_monitor_snapshot", "sl_hedge_plan"))

        def _opt_f(name: str) -> Any:
            value = data.get(name)
            return None if value is None else float(value)

        raw = {
            "snapshot_id": data["snapshot_id"],
            "plan_id": data["plan_id"],
            "as_of_ms": _require_int(data["as_of_ms"], name="as_of_ms"),
            "actual_hedge_ratio": _opt_f("actual_hedge_ratio"),
            "residual_short_notional_usd": _opt_f("residual_short_notional_usd"),
            "mark_price": _opt_f("mark_price"),
            "spot_price": _opt_f("spot_price"),
            "current_basis_pct": _opt_f("current_basis_pct"),
            "basis_pnl_usd": _opt_f("basis_pnl_usd"),
            "estimated_settled_funding_usd": _opt_f("estimated_settled_funding_usd"),
            "projected_next_funding_usd": _opt_f("projected_next_funding_usd"),
            "spot_pnl_usd": _opt_f("spot_pnl_usd"),
            "futures_pnl_usd": _opt_f("futures_pnl_usd"),
            "known_cost_usd": _opt_f("known_cost_usd"),
            "estimated_exit_cost_usd": _opt_f("estimated_exit_cost_usd"),
            "net_pnl_before_exit_usd": _opt_f("net_pnl_before_exit_usd"),
            "estimated_net_pnl_after_exit_usd": _opt_f("estimated_net_pnl_after_exit_usd"),
            "liquidation_distance": _opt_f("liquidation_distance"),
            "exit_liquidity_json": (None if data.get("exit_liquidity_json") is None
                                    else _json_text(data["exit_liquidity_json"],
                                                    name="exit_liquidity_json")),
            "source_meta_json": _json_text(data["source_meta_json"], name="source_meta_json"),
            "quality_json": _json_text(data["quality_json"], name="quality_json"),
            "metrics_json": _json_text(data["metrics_json"], name="metrics_json"),
            "safety_score": _opt_f("safety_score"),
            "status": data["status"],
            "created_at_ms": _require_int(data["created_at_ms"], name="created_at_ms"),
        }
        self._insert_immutable(con, "sl_hedge_monitor_snapshot",
                               "snapshot_id = ?", [raw["snapshot_id"]], raw)
        return raw["snapshot_id"]

    async def latest_hedge_monitor(self, plan_id: str) -> dict[str, Any] | None:
        return await self._run(self._latest_monitor_sync, plan_id)

    def _latest_monitor_sync(self, plan_id: str) -> dict[str, Any] | None:
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_hedge_monitor_snapshot",))
        except ReferenceNotFoundError:
            return None
        cur = con.execute(
            "SELECT * FROM sl_hedge_monitor_snapshot WHERE plan_id = ? "
            "ORDER BY as_of_ms DESC, snapshot_id ASC LIMIT 1",
            [plan_id],
        )
        rows = self._rows_to_dicts(cur)
        return rows[0] if rows else None

    # -- alerts (dedup + episode in one txn, B28.8) ------------------------
    async def upsert_hedge_alert(
        self, plan_id: str, code: str, severity: str, recommended_action: str,
        context: Mapping[str, Any] | None, now_ms: int,
        dedup_key: str | None = None, leg_or_venue: str = "",
    ) -> dict[str, Any]:
        return await self._run(
            self._upsert_alert_sync, plan_id, code, severity, recommended_action,
            dict(context or {}), _require_int(now_ms, name="now_ms"),
            dedup_key or f"{plan_id}:{code}:{leg_or_venue}",
            priority=PRIORITY_CRITICAL,
        )

    def _upsert_alert_sync(
        self, plan_id: str, code: str, severity: str, recommended_action: str,
        context: dict[str, Any], now_ms: int, dedup_key: str,
    ) -> dict[str, Any]:
        if severity not in self._HEDGE_ALERT_SEV:
            raise ValidationError("severity must be INFO/WARN/CRITICAL")
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_alert",))
        con.execute("BEGIN TRANSACTION")
        try:
            cur = con.execute(
                "SELECT * FROM sl_hedge_alert WHERE plan_id = ? AND dedup_key = ? "
                "AND state IN ('OPEN', 'ACKNOWLEDGED') ORDER BY episode DESC LIMIT 1",
                [plan_id, dedup_key],
            )
            rows = self._rows_to_dicts(cur)
            if rows:
                row = rows[0]
                con.execute(
                    "UPDATE sl_hedge_alert SET last_seen_at_ms = ?, context_json = ? "
                    "WHERE alert_id = ?",
                    [now_ms, _canonical_json(context), row["alert_id"]],
                )
                row["last_seen_at_ms"] = now_ms
                row["context_json"] = _canonical_json(context)
                try:
                    row["context_json"] = json.loads(row["context_json"])
                except (TypeError, ValueError):
                    pass
                con.execute("COMMIT")
                return row
            cur = con.execute(
                "SELECT max(episode) FROM sl_hedge_alert WHERE plan_id = ? AND dedup_key = ?",
                [plan_id, dedup_key],
            )
            prev = cur.fetchone()[0]
            episode = int(prev or 0) + 1
            alert_id = f"{plan_id}:{dedup_key}:{episode}"
            if self._fetch_raw(con, "sl_hedge_alert", "alert_id = ?", [alert_id]) is not None:
                alert_id = f"{alert_id}#{uuid.uuid4().hex[:8]}"
            con.execute(
                "INSERT INTO sl_hedge_alert (alert_id, plan_id, code, severity, state, "
                "opened_at_ms, last_seen_at_ms, acknowledged_at_ms, resolved_at_ms, "
                "dedup_key, episode, recommended_action, context_json) "
                "VALUES (?, ?, ?, ?, 'OPEN', ?, ?, NULL, NULL, ?, ?, ?, ?)",
                [alert_id, plan_id, code, severity, now_ms, now_ms,
                 dedup_key, episode, recommended_action, _canonical_json(context)],
            )
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return {
            "alert_id": alert_id, "plan_id": plan_id, "code": code,
            "severity": severity, "state": "OPEN", "opened_at_ms": now_ms,
            "last_seen_at_ms": now_ms, "acknowledged_at_ms": None,
            "resolved_at_ms": None, "dedup_key": dedup_key, "episode": episode,
            "recommended_action": recommended_action, "context_json": context,
        }

    async def ack_hedge_alert(self, alert_id: str, now_ms: int) -> dict[str, Any] | None:
        return await self._run(
            self._ack_alert_sync, alert_id,
            _require_int(now_ms, name="now_ms"), priority=PRIORITY_CRITICAL)

    def _ack_alert_sync(self, alert_id: str, now_ms: int) -> dict[str, Any] | None:
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_alert",))
        row = self._fetch_raw(con, "sl_hedge_alert", "alert_id = ?", [alert_id])
        if row is None:
            return None
        if row["state"] == "OPEN":
            con.execute(
                "UPDATE sl_hedge_alert SET state = 'ACKNOWLEDGED', acknowledged_at_ms = ?, "
                "last_seen_at_ms = ? WHERE alert_id = ?",
                [now_ms, now_ms, alert_id],
            )
            row = dict(row)
            row["state"] = "ACKNOWLEDGED"
            row["acknowledged_at_ms"] = now_ms
            row["last_seen_at_ms"] = now_ms
        return row

    async def resolve_hedge_alert(self, alert_id: str, now_ms: int) -> dict[str, Any] | None:
        return await self._run(
            self._resolve_alert_sync, alert_id,
            _require_int(now_ms, name="now_ms"), priority=PRIORITY_CRITICAL)

    def _resolve_alert_sync(self, alert_id: str, now_ms: int) -> dict[str, Any] | None:
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_alert",))
        row = self._fetch_raw(con, "sl_hedge_alert", "alert_id = ?", [alert_id])
        if row is None:
            return None
        if row["state"] in ("OPEN", "ACKNOWLEDGED"):
            con.execute(
                "UPDATE sl_hedge_alert SET state = 'RESOLVED', resolved_at_ms = ?, "
                "last_seen_at_ms = ? WHERE alert_id = ?",
                [now_ms, now_ms, alert_id],
            )
            row = dict(row)
            row["state"] = "RESOLVED"
            row["resolved_at_ms"] = now_ms
            row["last_seen_at_ms"] = now_ms
        return row

    async def list_hedge_alerts(
        self, plan_id: str | None = None, state: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        return await self._run(
            self._list_alerts_sync, plan_id, state, limit, offset)

    def _list_alerts_sync(
        self, plan_id: str | None, state: str | None, limit: int, offset: int,
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("limit must be an int in 1..200")
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an int >= 0")
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_hedge_alert",))
        except ReferenceNotFoundError:
            return ()
        clauses, params = [], []
        if plan_id is not None:
            clauses.append("plan_id = ?")
            params.append(plan_id)
        if state is not None:
            if state not in self._HEDGE_ALERT_STATE:
                raise ValidationError("state must be OPEN/ACKNOWLEDGED/RESOLVED")
            clauses.append("state = ?")
            params.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = con.execute(
            f"SELECT * FROM sl_hedge_alert{where} "
            "ORDER BY last_seen_at_ms DESC, alert_id ASC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return tuple(self._rows_to_dicts(cur))

    # -- hedge outcomes (fixed strategies, B39.2/B39.3) --------------------
    async def save_hedge_outcome(self, record: Any,
                                 references: Sequence[Any] | None = None) -> str:
        return await self._run(
            self._save_hedge_outcome_sync, record, list(references or []),
            priority=PRIORITY_SOURCE_SCORE,
        )

    async def save_hedge_outcomes(self, record: Any) -> str:
        return await self.save_hedge_outcome(record)

    def _save_hedge_outcome_sync(self, record: Any, references: list[Any]) -> str:
        data = _record_dict(
            record,
            {"outcome_id", "fcs_snapshot_id", "strategy", "horizon_days",
             "outcome_status", "evidence_version", "cost_config_hash",
             "outcome_json", "updated_at_ms"},
            {"reason_code"},
            name="hedge_outcome",
        )
        if data["strategy"] not in ("ABSOLUTE_100", "RELATIVE_75", "RELATIVE_50",
                                    "RELATIVE_25"):
            raise ValidationError("strategy must be a fixed hedge strategy")
        if data["horizon_days"] not in (7, 30, 90):
            raise ValidationError("horizon_days must be 7, 30 or 90")
        if data["outcome_status"] not in ("PENDING", "COMPLETE", "CENSORED",
                                          "UNAVAILABLE"):
            raise ValidationError("outcome_status is not a hedge outcome status")
        con = self._require_con()
        self._hedge_require_tables(con, ("sl_hedge_outcome", "sl_hedge_snapshot_reference"))
        updated = _require_int(data["updated_at_ms"], name="updated_at_ms")
        raw = {
            "outcome_id": data["outcome_id"],
            "fcs_snapshot_id": data["fcs_snapshot_id"],
            "strategy": data["strategy"],
            "horizon_days": int(data["horizon_days"]),
            "outcome_status": data["outcome_status"],
            "reason_code": data.get("reason_code"),
            "evidence_version": data["evidence_version"],
            "cost_config_hash": data["cost_config_hash"],
            "outcome_json": _json_text(data["outcome_json"], name="outcome_json"),
            "updated_at_ms": updated,
        }
        con.execute("BEGIN TRANSACTION")
        try:
            where = ("fcs_snapshot_id = ? AND strategy = ? AND horizon_days = ? "
                     "AND evidence_version = ? AND cost_config_hash = ?")
            params = [raw["fcs_snapshot_id"], raw["strategy"], raw["horizon_days"],
                      raw["evidence_version"], raw["cost_config_hash"]]
            self._insert_immutable(con, "sl_hedge_outcome", where, params, raw)
            # Same-transaction references: outcome -> FCS (+ entry sims).
            auto_refs = [("FCS", raw["fcs_snapshot_id"], "outcome-fcs")] + list(references)
            for rtype, rid, *_rest in [
                    tuple(r) if isinstance(r, (list, tuple)) else
                    (r.get("referenced_type"), r.get("referenced_id"),
                     r.get("purpose", "pin")) for r in auto_refs]:
                if rtype == "FCS" and not self._hedge_ref_exists_sync(con, rtype, rid):
                    # FCS rows may live in the same batch; missing FCS is a
                    # hard reference error (no silent JSON-only pin).
                    raise ReferenceNotFoundError(f"referenced FCS:{rid} not found")
            self._hedge_insert_refs_sync(con, "OUTCOME", raw["outcome_id"],
                                         auto_refs, updated)
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return raw["outcome_id"]

    async def list_hedge_outcomes(
        self, fcs_snapshot_id: str | None = None, limit: int = 50, offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        return await self._run(
            self._list_hedge_outcomes_sync, fcs_snapshot_id, limit, offset)

    def _list_hedge_outcomes_sync(
        self, fcs_snapshot_id: str | None, limit: int, offset: int,
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("limit must be an int in 1..200")
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an int >= 0")
        con = self._require_con()
        try:
            self._hedge_require_tables(con, ("sl_hedge_outcome",))
        except ReferenceNotFoundError:
            return ()
        clauses, params = [], []
        if fcs_snapshot_id is not None:
            clauses.append("fcs_snapshot_id = ?")
            params.append(fcs_snapshot_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = con.execute(
            f"SELECT * FROM sl_hedge_outcome{where} "
            "ORDER BY updated_at_ms DESC, outcome_id ASC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return tuple(self._rows_to_dicts(cur))

    # -- crash recovery ------------------------------------------------------------
    async def recover_running_jobs(
        self, now_ms: int, *, priority: int | None = PRIORITY_CRITICAL,
        trace: str | None = None,
    ) -> int:
        """Mark leftover RUNNING jobs API-compat FAILED/PROCESS_INTERRUPTED."""
        return await self._run(
            self._recover_running_sync, _require_int(now_ms, name="now_ms"),
            priority=priority, trace=trace,
        )

    def _recover_running_sync(self, now_ms: int) -> int:
        con = self._require_con()
        cur = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'sl_job_run'"
        )
        if cur.fetchone()[0] == 0:
            return 0
        cur = con.execute("SELECT count(*) FROM sl_job_run WHERE status = 'RUNNING'")
        count = int(cur.fetchone()[0])
        if count:
            con.execute(
                "UPDATE sl_job_run SET status = 'FAILED', finished_at_ms = ?, "
                "error_code = ? WHERE status = 'RUNNING'",
                [now_ms, PROCESS_INTERRUPTED],
            )
        return count

    # -- introspection ------------------------------------------------------------------
    async def index_names(self) -> list[str]:
        return await self._run(self._index_names_sync)

    def _index_names_sync(self) -> list[str]:
        con = self._require_con()
        cur = con.execute("SELECT index_name FROM duckdb_indexes()")
        return sorted(row[0] for row in cur.fetchall())
