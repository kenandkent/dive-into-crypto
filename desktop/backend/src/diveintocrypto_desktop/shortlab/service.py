"""Short-Lab service orchestration (Task 13, design sections 6 / 22 / 23).

``ShortLabService`` is the use-case layer between the FastAPI router
(Task 14) and the pure layers (Tasks 1-12):

- :meth:`refresh` / :meth:`run_refresh` execute the full pipeline
  ``universe -> cheap filter -> identity -> features -> LTSS -> risk ->
  top-N Entry -> save_entry -> save_feature -> save_score`` with one
  ``job_type`` running at most once (manual and scheduled refreshes share
  the per-job lock and therefore reuse the in-flight ``job_id``).
- :meth:`candidates` / :meth:`detail` serve the latest *completed*
  generation from DuckDB; failed/half-written batches stay invisible.
- :meth:`evidence_summary` returns an explicit :class:`Unavailable` until
  Task 16 wires the metrics provider -- the router maps that to 503
  without changing when metrics land.

Risk authority (plan checklist 4): VETO / PAUSE / WARN and the dual
status come *only* from Task 11 ``evaluate_risks`` / ``derive_status``.
The Task 8 universe lifecycle helpers are consumed as raw metadata
(``exchange_status`` / ``delivery_at_ms`` / ``live_universe_present`` /
``previously_seen``); the service never converts a disappearance into a
veto itself. In particular non-terminal states such as HALT/BREAK map to
``VETO_LOW_LIQUIDITY`` inside ``evaluate_risks``, never to
``VETO_CONTRACT_DELISTING``.

Failure isolation: every provider fetch is guarded -- an exception or an
``UNAVAILABLE``/``ERROR`` result degrades that symbol's fields to
``null`` + reason and lowers DQ; it never raises through the refresh,
never touches the legacy Dive ``_scan_cache``, and never blocks sibling
symbols. A refresh whose providers all fail still completes (honest
all-null scores); only infrastructure failures (empty universe, DB
errors) fail the job.

Tier selection: the service only consults ``ProviderRegistry`` by the
string names ``unlock`` / ``social`` / ``catalyst``. Future provider
modules are never imported here; when any of them is unregistered the
effective tier stays ``LITE`` with ``FULL_PREREQUISITE_MISSING``. The
tier is resolved once per refresh, so every score row of one generation
shares the same ``analysis_tier`` -- a transient 429 never mixes LITE
rows into a FULL generation (the snapshot stays FULL, DQ drops and the
symbol degrades to NOT_READY).

FULL stage (Task 17): ``unlock`` / ``social`` / ``catalyst`` are fetched
through the registry, ``unlock_raw_15`` (forward 30/90D vesting) and
``narrative_raw_15`` (two aligned 30D attention windows) are scored by
``features/supply.py`` / ``features/narrative.py``, ``score_full`` applies
the section-14 weights, FULL DQ groups come from ``quality``, and catalyst
severity flows into the Task 11 risk engine (``PAUSE_MAJOR_CATALYST``).
Events/snapshots persist via the repository's idempotent Phase 5/6
methods (best-effort: a persistence failure degrades to in-memory data,
never fails the symbol).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from diveintocrypto_desktop.shortlab.config import ShortLabConfig, load_shortlab_config
from diveintocrypto_desktop.shortlab.entry import ENTRY_BUDGET_EXHAUSTED, EntryBudget
from diveintocrypto_desktop.shortlab.models import ProviderResult
from diveintocrypto_desktop.shortlab.quality import (
    FULL_PREREQUISITE_MISSING,
    FieldState,
    data_quality,
    full_tier_field_states,
)
from diveintocrypto_desktop.shortlab.scoring.versions import (
    ENTRY_VERSION,
    FEATURE_VERSION,
)

log = logging.getLogger(__name__)

JOB_TYPE_SCORE_REFRESH = "score_refresh"
KNOWN_JOB_TYPES = (JOB_TYPE_SCORE_REFRESH,)

#: Task 17 provider names. Referenced as plain strings only -- this module
#: must never import the future provider implementations.
FULL_PROVIDER_NAMES = ("unlock", "social", "catalyst")

EVIDENCE_UNAVAILABLE_REASON = "short_evidence_unavailable"
SHORTLAB_UNAVAILABLE_REASON = "shortlab_unavailable"

FUNDING_BATCH_WINDOW_SEC = 300
DAY_MS = 86_400_000
_LOOKBACK_MS = {7: 7 * DAY_MS, 30: 30 * DAY_MS, 90: 90 * DAY_MS}

_TRADABLE_STATUS = "TRADING"

# Repository field-level source-meta skeleton for one feature snapshot.
# Presence of every key is enforced on write (Task 2); values below are
# filled per symbol from the actual fetch outcomes.
_META_KEYS = ("status", "fetched_at_ms", "as_of_ms", "coverage_fraction", "reason_code", "source")


class ShortLabUnavailable(RuntimeError):
    """Short-Lab is down (migration failure / no repository); Dive is unaffected."""


class UnknownJobType(ValueError):
    """``refresh`` named a job_type this service does not execute."""


class JobNotFound(KeyError):
    """No ``sl_job_run`` row (nor in-flight job) for the requested id."""


class SymbolNotFound(KeyError):
    """The symbol has no score row in the referenced generation."""


@dataclass(frozen=True)
class JobRef:
    """Handle returned by :meth:`ShortLabService.refresh`."""

    job_id: str
    job_type: str
    existing: bool = False


@dataclass(frozen=True)
class JobStatus:
    """Progress/result snapshot for one refresh job."""

    job_id: str
    job_type: str
    status: str  # RUNNING | SUCCEEDED | FAILED
    stats: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    existing: bool = False


@dataclass(frozen=True)
class CandidateQuery:
    """Pagination/filter request served straight from the repository."""

    generation_id: str | None = None
    limit: int = 50
    offset: int = 0
    status: str | None = None
    sort: str | None = None
    order: str = "desc"


@dataclass(frozen=True)
class CandidateDetail:
    """One symbol's score row joined with its feature/entry snapshots."""

    symbol: str
    generation_id: str
    score: Any
    feature: Any
    entry: Any | None = None


@dataclass(frozen=True)
class EvidenceSummary:
    """Forward-evidence aggregate (populated once Task 16 wires metrics)."""

    filters: Mapping[str, Any] = field(default_factory=dict)
    horizons: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    total: int = 0
    generated_at_ms: int = 0


@dataclass(frozen=True)
class Unavailable:
    """Explicit 'not yet wired' marker (router maps it to 503)."""

    reason: str
    detail: str | None = None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _rotate(rows: list[dict[str, Any]], cursor: int) -> list[dict[str, Any]]:
    """Rotate the universe order so funding batches continue across runs."""
    if not rows:
        return []
    start = int(cursor) % len(rows)
    return rows[start:] + rows[:start]


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _meta_block(
    status: str,
    fetched_at_ms: int | None,
    as_of_ms: int | None,
    source: str,
    reason_code: str | None = None,
    coverage_fraction: float = 1.0,
) -> dict[str, Any]:
    return {
        "status": status,
        "fetched_at_ms": fetched_at_ms,
        "as_of_ms": as_of_ms,
        "coverage_fraction": coverage_fraction,
        "reason_code": reason_code,
        "source": source,
    }


def _unavailable_result(source: str, now_ms: int, reason_code: str) -> ProviderResult[None]:
    return ProviderResult(
        status="UNAVAILABLE",
        source=source,
        fetched_at_ms=now_ms,
        as_of_ms=None,
        data=None,
        stale=False,
        reason_code=reason_code,
        error_message=None,
    )


class _JobLocks:
    """Per-job locks keyed by running loop (same idiom as api/app.py)."""

    def __init__(self) -> None:
        self._by_loop: dict[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = {}

    def for_job(self, job_type: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        table = self._by_loop.setdefault(loop, {})
        lock = table.get(job_type)
        if lock is None:
            lock = table[job_type] = asyncio.Lock()
        return lock


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ShortLabService:
    """Use-case orchestration over the Task 1-12 pure layers + repository."""

    def __init__(
        self,
        *,
        config: ShortLabConfig | None = None,
        repository: Any | None = None,
        registry: Any | None = None,
        clock: Callable[[], int] | None = None,
        universe_fn: Callable[..., Any] | None = None,
        metadata_fn: Callable[[], Any] | None = None,
        funding_history_fn: Callable[..., Any] | None = None,
        funding_coverage_fn: Callable[..., Any] | None = None,
        spot_history_fn: Callable[..., Any] | None = None,
        market_inputs_fn: Callable[..., Any] | None = None,
        identity_candidates_fn: Callable[[str], Any] | None = None,
        identity_overrides: Mapping[str, Any] | None = None,
        entry_runner: Callable[..., Any] | None = None,
        metrics_provider: Callable[[Mapping[str, Any]], Awaitable[EvidenceSummary | None]] | None = None,
        unavailable_reason: str | None = None,
    ) -> None:
        from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry

        self._config = config or load_shortlab_config()
        self._repository = repository
        self._registry = registry or ProviderRegistry()
        self._clock: Callable[[], int] = clock or _now_ms
        self._unavailable_reason = unavailable_reason or SHORTLAB_UNAVAILABLE_REASON

        # Late-bind production data clients so unit tests can inject fakes
        # without importing (or hitting) any network module.
        if universe_fn is not None:
            self._universe_fn = universe_fn
        else:
            from diveintocrypto_desktop.data import universe as _universe

            self._universe_fn = _universe.list_universe
        if metadata_fn is not None:
            self._metadata_fn = metadata_fn
        else:
            from diveintocrypto_desktop.data import universe as _universe

            self._metadata_fn = _universe.contract_metadata_all
        if funding_history_fn is not None:
            self._funding_history_fn = funding_history_fn
        else:
            from diveintocrypto_desktop.data import funding as _funding

            self._funding_history_fn = _funding.funding_history_range
        if funding_coverage_fn is not None:
            self._funding_coverage_fn = funding_coverage_fn
        else:
            from diveintocrypto_desktop.data import funding as _funding

            self._funding_coverage_fn = _funding.funding_coverage
        self._spot_history_fn = spot_history_fn or _default_spot_history
        self._market_inputs_fn = market_inputs_fn or _default_market_inputs
        self._identity_candidates_fn = identity_candidates_fn or (lambda symbol: [])
        if identity_overrides is not None:
            self._identity_overrides = identity_overrides
        else:
            try:
                from diveintocrypto_desktop.shortlab.identity import overrides as _overrides

                self._identity_overrides = _overrides.load_overrides()
            except Exception:  # noqa: BLE001 - overrides are best-effort
                self._identity_overrides = {}
        if entry_runner is not None:
            self._entry_runner = entry_runner
        else:
            from diveintocrypto_desktop.shortlab import entry as _entry_mod

            self._entry_runner = _entry_mod.run_entry_batch
        self._metrics_provider = metrics_provider

        self._locks = _JobLocks()
        self._running: dict[str, dict[str, Any]] = {}
        self._seq = 0
        # Rotation cursor into the universe order: each refresh starts its
        # funding batch where the previous one stopped, so symbols deferred
        # by the 80-per-5min gate are picked up first on the next run and a
        # static universe cannot starve its own tail.
        self._funding_cursor = 0
        # CoinGecko freshness (design 23): per coingecko_id market/supply clocks.
        # One market response carries supply too, so a market fetch refreshes
        # both; an independent supply fetch only happens when no supply has
        # been seen via market inside supply_sec.
        self._fund_state: dict[str, dict[str, Any]] = {}
        # Funding 80-symbols-per-5min batch gate (design 10.1 / plan Task 13).
        self._funding_window_start_ms: int | None = None
        self._funding_window_used: int = 0

    # -- availability ------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._repository is not None

    def _require_available(self) -> Any:
        if self._repository is None:
            raise ShortLabUnavailable(self._unavailable_reason)
        return self._repository

    @property
    def config(self) -> ShortLabConfig:
        return self._config

    @property
    def registry(self) -> Any:
        return self._registry

    def _now(self) -> int:
        return int(self._clock())

    # -- tier selection ------------------------------------------------------
    def effective_tier(self, requested: str | None = None) -> tuple[str, tuple[str, ...]]:
        """Resolve the tier actually computed.

        Defaults to the configured ``analysis_tier`` (LITE). ``FULL`` is
        honoured only when every Phase 5/6 provider (``unlock`` /
        ``social`` / ``catalyst``) is registered; otherwise the service
        stays on ``LITE`` with ``FULL_PREREQUISITE_MISSING``. Only the
        registry's string names are consulted -- future provider modules
        are never imported here.
        """
        want = str(requested or self._config.analysis_tier).upper()
        if want != "FULL":
            return "LITE", ()
        missing = [name for name in FULL_PROVIDER_NAMES if not self._registry.has(name)]
        if missing:
            return "LITE", (FULL_PREREQUISITE_MISSING,)
        return "FULL", ()

    # -- refresh entry points --------------------------------------------------
    async def refresh(self, job_type: str = JOB_TYPE_SCORE_REFRESH) -> JobRef:
        """Enqueue a refresh and return its :class:`JobRef` immediately.

        A second caller for the same ``job_type`` while one is in flight
        reuses the running job's id (``existing=True``) instead of
        launching a duplicate market sweep. The pipeline itself then runs
        as a background task; use :meth:`job_status` / :meth:`await_job`
        to observe it.
        """
        if job_type not in KNOWN_JOB_TYPES:
            raise UnknownJobType(job_type)
        slot = self._running.get(job_type)
        if slot is not None and not slot["task"].done():
            return JobRef(job_id=slot["job_id"], job_type=job_type, existing=True)
        self._seq += 1
        job_id = f"{job_type}-{self._now()}-{self._seq:04d}"
        loop = asyncio.get_running_loop()
        task: asyncio.Task = loop.create_task(
            self._guarded_run(job_type, job_id), name=f"shortlab-{job_id}"
        )
        # Reserve the slot synchronously (no await above) so a concurrent
        # manual + scheduled pair cannot both start.
        self._running[job_type] = {"job_id": job_id, "task": task}
        task.add_done_callback(lambda _t, jt=job_type: self._running.pop(jt, None))
        return JobRef(job_id=job_id, job_type=job_type, existing=False)

    async def run_refresh(self, job_type: str = JOB_TYPE_SCORE_REFRESH) -> JobStatus:
        """Execute one refresh inline (scheduler ticks + deterministic tests).

        Reuses the in-flight job when one is already running for the same
        ``job_type`` -- the scheduled path therefore never doubles a manual
        refresh; both share the per-job lock afterwards.
        """
        if job_type not in KNOWN_JOB_TYPES:
            raise UnknownJobType(job_type)
        slot = self._running.get(job_type)
        if slot is not None and not slot["task"].done():
            return await self.job_status(slot["job_id"], existing=True)
        self._seq += 1
        job_id = f"{job_type}-{self._now()}-{self._seq:04d}"
        loop = asyncio.get_running_loop()
        task: asyncio.Task = loop.create_task(
            self._guarded_run(job_type, job_id), name=f"shortlab-{job_id}"
        )
        self._running[job_type] = {"job_id": job_id, "task": task}
        task.add_done_callback(lambda _t, jt=job_type: self._running.pop(jt, None))
        await task
        return await self.job_status(job_id)

    async def _guarded_run(self, job_type: str, job_id: str) -> JobStatus:
        lock = self._locks.for_job(job_type)
        async with lock:
            try:
                return await self._execute_pipeline(job_type, job_id)
            except Exception as exc:  # noqa: BLE001 - jobs record, never escape
                log.warning("shortlab job=%s id=%s crashed: %s", job_type, job_id, str(exc)[:200])
                try:
                    repo = self._repository
                    if repo is not None:
                        await repo.finish_job_run(
                            job_id, "FAILED", self._now(), error_code=f"{type(exc).__name__}",
                        )
                except Exception:  # noqa: BLE001 - best effort on a dead DB
                    pass
                return JobStatus(job_id=job_id, job_type=job_type, status="FAILED",
                                 error_code=type(exc).__name__)

    async def await_job(self, job_id: str, timeout_sec: float = 60.0) -> JobStatus:
        """Wait for a background :meth:`refresh` job, then return its status."""
        for slot in self._running.values():
            if slot["job_id"] == job_id:
                await asyncio.wait_for(asyncio.shield(slot["task"]), timeout=timeout_sec)
                break
        return await self.job_status(job_id)

    async def job_status(self, job_id: str, existing: bool = False) -> JobStatus:
        repo = self._require_available()
        for slot in self._running.values():
            if slot["job_id"] == job_id and not slot["task"].done():
                row = await repo.get_job_run(job_id)
                stats = dict(row["stats_json"]) if row and row.get("stats_json") else {}
                return JobStatus(
                    job_id=job_id,
                    job_type=row["job_type"] if row else JOB_TYPE_SCORE_REFRESH,
                    status="RUNNING",
                    stats=stats,
                    started_at_ms=row["started_at_ms"] if row else None,
                    existing=existing,
                )
        row = await repo.get_job_run(job_id)
        if row is None:
            raise JobNotFound(job_id)
        stats = row.get("stats_json")
        return JobStatus(
            job_id=job_id,
            job_type=row["job_type"],
            status=row["status"],
            stats=dict(stats) if isinstance(stats, Mapping) else {},
            error_code=row.get("error_code"),
            started_at_ms=row.get("started_at_ms"),
            finished_at_ms=row.get("finished_at_ms"),
            existing=existing,
        )

    # -- read paths ------------------------------------------------------------
    async def candidates(self, query: CandidateQuery | None = None) -> Any:
        """Serve one candidate page from the latest *completed* generation."""
        repo = self._require_available()
        q = query or CandidateQuery()
        return await repo.list_candidates(
            generation_id=q.generation_id,
            limit=q.limit,
            offset=q.offset,
            status=q.status,
            sort=q.sort,
            order=q.order,
        )

    async def detail(self, symbol: str, generation_id: str | None = None) -> CandidateDetail:
        """Join one symbol's score / feature / entry snapshots."""
        repo = self._require_available()
        name = str(symbol).upper()
        if generation_id is None:
            generation_id = await repo.latest_completed_generation()
            if generation_id is None:
                raise JobNotFound("no completed score_refresh generation")
        score = await self._find_score(repo, name, generation_id)
        if score is None:
            raise SymbolNotFound(symbol)
        feature = await repo.get_feature(score.feature_snapshot_id)
        entry = None
        if score.entry_snapshot_id is not None:
            entry = await repo.get_entry(score.entry_snapshot_id)
        return CandidateDetail(
            symbol=name, generation_id=generation_id, score=score, feature=feature, entry=entry
        )

    async def _find_score(self, repo: Any, symbol: str, generation_id: str) -> Any | None:
        offset = 0
        while True:
            page = await repo.list_candidates(generation_id=generation_id, limit=200, offset=offset)
            for item in page.items:
                if item.symbol == symbol:
                    return item
            if len(page.items) < 200:
                return None
            offset += 200

    async def evidence_summary(
        self, filters: Mapping[str, Any] | None = None
    ) -> EvidenceSummary | Unavailable:
        """Forward-evidence aggregate; :class:`Unavailable` until Task 16.

        The router maps :class:`Unavailable` to ``503
        short_evidence_unavailable``; once Task 16 supplies
        ``metrics_provider`` the same route returns a real 200 with no
        router change.
        """
        filt = dict(filters or {})
        if self._metrics_provider is None:
            return Unavailable(
                reason=EVIDENCE_UNAVAILABLE_REASON,
                detail="forward grader metrics are not wired yet (Task 16)",
            )
        summary = await self._metrics_provider(filt)
        if summary is None:
            return Unavailable(reason=EVIDENCE_UNAVAILABLE_REASON, detail="metrics returned nothing")
        if isinstance(summary, EvidenceSummary):
            return summary
        if isinstance(summary, Mapping):
            return EvidenceSummary(
                filters=filt,
                horizons=dict(summary.get("horizons", {})),
                total=int(summary.get("total", 0)),
                generated_at_ms=int(summary.get("generated_at_ms", self._now())),
            )
        return Unavailable(reason=EVIDENCE_UNAVAILABLE_REASON, detail="bad metrics shape")

    # -- pipeline ----------------------------------------------------------------
    async def _execute_pipeline(self, job_type: str, job_id: str) -> JobStatus:
        from diveintocrypto_desktop.shortlab.repository import ScoreSnapshotRecord

        repo = self._require_available()
        started_ms = self._now()
        wall_start = time.monotonic()
        await repo.create_job_run(job_id, job_type, started_ms)
        log.info("shortlab job=%s id=%s started", job_type, job_id)

        try:
            rows = await _maybe_await(self._universe_fn(self._config.universe.limit))
        except Exception as exc:
            await repo.finish_job_run(job_id, "FAILED", self._now(),
                                      error_code=f"UNIVERSE_FAILED:{type(exc).__name__}")
            return JobStatus(job_id=job_id, job_type=job_type, status="FAILED",
                             error_code="UNIVERSE_FAILED")
        live_symbols = {str(r.get("s") or r.get("symbol", "")).upper() for r in rows or []}
        live_symbols.discard("")
        universe_rows = self._cheap_filter(rows or [])
        log.info("shortlab job=%s id=%s universe=%d kept=%d",
                 job_type, job_id, len(rows or []), len(universe_rows))

        try:
            metadata_all = await _maybe_await(self._metadata_fn())
        except Exception:
            metadata_all = {}
        if not isinstance(metadata_all, Mapping):
            metadata_all = {}

        as_of_ms = self._now()
        tier, tier_warnings = self.effective_tier()

        scored: list[dict[str, Any]] = []
        succeeded = 0
        failed = 0
        deferred = 0
        funding_requested = 0
        funding_deferred = 0
        errors: list[str] = []
        rotated = _rotate(universe_rows, self._funding_cursor)
        for row in rotated:
            symbol = str(row.get("s") or row.get("symbol", "")).upper()
            if not symbol:
                continue
            try:
                outcome = await self._score_symbol(
                    symbol, row, live_symbols, metadata_all, as_of_ms, tier
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol never fails siblings
                failed += 1
                errors.append(f"{symbol}:{type(exc).__name__}"[:120])
                continue
            scored.append(outcome)
            succeeded += 1
            funding_requested += outcome.get("funding_requested", 0)
            funding_deferred += outcome.get("funding_deferred", 0)
            deferred += outcome.get("deferred", 0)
        if universe_rows:
            self._funding_cursor = (self._funding_cursor + funding_requested) % len(universe_rows)

        if not scored:
            await repo.finish_job_run(job_id, "FAILED", self._now(),
                                      error_code="EMPTY_GENERATION")
            return JobStatus(job_id=job_id, job_type=job_type, status="FAILED",
                             error_code="EMPTY_GENERATION")

        # Shortlist by LTSS (nulls last), then budgeted Entry depth.
        ranked = sorted(
            scored,
            key=lambda o: (o["breakdown"].ltss is None, -(o["breakdown"].ltss or 0.0)),
        )
        shortlist = ranked[: max(1, self._config.universe.shortlist_size)]
        entry_symbols = [o["symbol"] for o in shortlist[: max(1, self._config.universe.entry_depth_top)]]
        entry_by_symbol, entry_stats = await self._run_entry_stage(entry_symbols, as_of_ms)
        entry_queued = set(entry_stats.get("queued_symbols", ()))
        deferred += len(entry_queued)

        # Derive final statuses now that Entry is known, then persist in the
        # checklist order: save_entry -> save_feature -> save_score(batch).
        records: list[Any] = []
        entry_exhausted = {
            s for s, r in entry_by_symbol.items()
            if r is not None and r.reason_code == ENTRY_BUDGET_EXHAUSTED
        }
        for outcome in scored:
            symbol = outcome["symbol"]
            entry_result = entry_by_symbol.get(symbol)
            entry_score = entry_result.entry_score if entry_result is not None else None
            state = self._derive_symbol_status(outcome, entry_score, symbol in entry_exhausted)
            outcome["state"] = state
            if entry_result is not None:
                await repo.save_entry(entry_result.to_record())
            await repo.save_feature(outcome["feature_record"])
            records.append(
                ScoreSnapshotRecord(
                    snapshot_id=f"score-{symbol}-{as_of_ms}-{outcome['breakdown'].profile}-{job_id}",
                    generation_id=job_id,
                    feature_snapshot_id=outcome["feature_record"].snapshot_id,
                    entry_snapshot_id=entry_result.snapshot_id if entry_result is not None else None,
                    symbol=symbol,
                    as_of_ms=as_of_ms,
                    analysis_tier=tier,
                    profile=outcome["breakdown"].profile,
                    score_version=outcome["breakdown"].score_version,
                    entry_version=ENTRY_VERSION if entry_result is not None else None,
                    feature_version=FEATURE_VERSION,
                    config_hash=outcome["breakdown"].config_hash,
                    ltss=outcome["breakdown"].ltss,
                    entry_score=entry_score,
                    data_quality=outcome["dq"].data_quality,
                    candidate_status=state.candidate_status,
                    execution_status=state.execution_status,
                    status=state.status,
                    module_scores=dict(outcome["breakdown"].module_scores),
                    vetoes=tuple(state.vetoes),
                    pauses=tuple(state.pauses),
                    reasons=tuple(state.reasons),
                    warnings=tuple(state.warnings),
                )
            )

        finished_ms = self._now()
        stats = {
            "as_of_ms": as_of_ms,
            "effective_tier": tier,
            "tier_warnings": list(tier_warnings),
            "universe_rows": len(rows or []),
            "symbols_total": len(universe_rows),
            "shortlist": len(shortlist),
            "entry_depth": len(entry_symbols),
            "succeeded": succeeded,
            "failed": failed,
            "deferred": deferred,
            "funding_requested": funding_requested,
            "funding_deferred": funding_deferred,
            "entry_calls_made": entry_stats.get("calls_made", 0),
            "entry_cache_hits": entry_stats.get("cache_hits", 0),
            "entry_queued": len(entry_queued),
            "duration_ms": int((time.monotonic() - wall_start) * 1000),
            "errors": errors[:20],
        }
        await repo.save_score_batch(
            records,
            job_id=job_id,
            job_type=job_type,
            started_at_ms=started_ms,
            finished_at_ms=finished_ms,
            stats=stats,
        )
        by_status: dict[str, int] = {}
        for outcome in scored:
            by_status[outcome["state"].status] = by_status.get(outcome["state"].status, 0) + 1
        log.info(
            "shortlab job=%s id=%s SUCCEEDED dauer=%dms ok=%d fail=%d deferred=%d status=%s",
            job_type, job_id, stats["duration_ms"], succeeded, failed, deferred, by_status,
        )
        return JobStatus(
            job_id=job_id, job_type=job_type, status="SUCCEEDED", stats=stats,
            started_at_ms=started_ms, finished_at_ms=finished_ms,
        )

    # -- pipeline stages ---------------------------------------------------------
    def _cheap_filter(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Cheap eligibility + liquidity prefilter on the rolling ticker.

        Keeps rows with a finite price and quote volume, quote-volume
        ordered, capped at ``universe.limit``. The rolling ticker value is
        used *only* here -- it never enters Tradeability, DQ or snapshots
        (design 8.2/13); hard liquidity gates stay in the Task 11 risk
        layer so low-liquidity coins surface as BLOCKED, not silently
        vanished.
        """
        kept: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, Mapping):
                continue
            price = _finite(row.get("price"))
            qv = _finite(row.get("quote_volume"))
            if price is None or qv is None:
                continue
            kept.append(dict(row))
        kept.sort(key=lambda r: float(r.get("quote_volume") or 0.0), reverse=True)
        return kept[: max(1, self._config.universe.limit)]

    async def _score_symbol(
        self,
        symbol: str,
        row: Mapping[str, Any],
        live_symbols: set[str],
        metadata_all: Mapping[str, Any],
        as_of_ms: int,
        tier: str,
    ) -> dict[str, Any]:
        from diveintocrypto_desktop.shortlab.identity.resolver import resolve_identity
        from diveintocrypto_desktop.shortlab.scoring.ltss import (
            extract_features,
            score_full,
            score_lite,
        )
        from diveintocrypto_desktop.shortlab.scoring.profiles import select_profile
        from diveintocrypto_desktop.shortlab.repository import FeatureSnapshotRecord
        from diveintocrypto_desktop.shortlab.risk.veto import evaluate_risks

        now = self._now()
        meta_raw = metadata_all.get(symbol)
        exchange_meta = _contract_meta_dict(meta_raw)
        try:
            candidates = await _maybe_await(self._identity_candidates_fn(symbol))
        except Exception:
            candidates = []
        identity = resolve_identity(symbol, exchange_meta, list(candidates or []),
                                    self._identity_overrides)

        fund_result, fund_data = await self._fetch_fundamentals(identity, now)
        spot_result = await self._fetch_spot(identity, as_of_ms, now)
        market = await self._fetch_market(symbol, as_of_ms, now)
        funding = await self._fetch_funding(symbol, exchange_meta, as_of_ms, now)

        inputs = self._build_inputs(symbol, row, identity, exchange_meta, fund_data,
                                    spot_result, market, funding, as_of_ms)
        full: dict[str, Any] | None = None
        if tier == "FULL":
            full = await self._fetch_full(identity, symbol, inputs, fund_data,
                                          as_of_ms, now)
            inputs["unlock_raw_15"] = full["unlock_raw_15"]
            inputs["narrative_raw_15"] = full["narrative_raw_15"]
            inputs["unlock_status"] = full["unlock_result"].status
            inputs["social_status"] = full["social_result"].status
            inputs["catalyst_major_event"] = full["catalyst_major_event"]
            inputs["catalyst_severity"] = full["catalyst_severity"]
        features = extract_features(inputs, as_of_ms)
        profile = select_profile(identity, fund_data, self._identity_overrides)
        if tier == "FULL":
            breakdown = score_full(features, profile, self._config)
        else:
            breakdown = score_lite(features, profile, self._config)

        field_states = self._build_field_states(
            identity, exchange_meta, fund_result, spot_result, market, funding, as_of_ms, now,
            full=full,
        )
        dq = data_quality(tier, field_states, as_of_ms)
        risk_meta = self._build_risk_meta(
            symbol, row, identity, exchange_meta, live_symbols, metadata_all,
            fund_data, spot_result, market, funding, as_of_ms,
            full=full,
        )
        risk = evaluate_risks(inputs, risk_meta, dq.data_quality)

        feature_record = FeatureSnapshotRecord(
            snapshot_id=features.snapshot_id,
            symbol=symbol,
            as_of_ms=as_of_ms,
            feature_version=features.feature_version,
            features=dict(features.features),
            source_meta=self._build_feature_source_meta(
                identity, exchange_meta, fund_result, spot_result, market, funding,
                as_of_ms, now, full=full,
            ),
            data_quality=dq.data_quality,
            fundamental_snapshot_id=None,
        )
        return {
            "symbol": symbol,
            "identity": identity,
            "breakdown": breakdown,
            "dq": dq,
            "risk": risk,
            "risk_meta": risk_meta,
            "inputs": inputs,
            "feature_record": feature_record,
            "funding_requested": funding.get("requested", 0),
            "funding_deferred": funding.get("deferred", 0),
            "deferred": funding.get("deferred", 0),
            "state": None,
        }

    def _derive_symbol_status(
        self, outcome: dict[str, Any], entry_score: float | None, budget_exhausted: bool
    ):
        from diveintocrypto_desktop.shortlab.risk.veto import derive_status

        cfg = self._config.candidate
        identity = outcome["identity"]
        risk_meta = outcome["risk_meta"]
        onboard = risk_meta.get("onboard_at_ms")
        identity_view = {
            "mapping_confidence": identity.mapping_confidence,
            "contract_multiplier": identity.contract_multiplier,
            "onboard_at_ms": onboard,
        }
        return derive_status(
            outcome["breakdown"].ltss,
            entry_score,
            outcome["dq"].data_quality,
            outcome["breakdown"].tradeability_score,
            identity_view,
            outcome["risk"],
            outcome["dq"].stale,
            entry_budget_exhausted=budget_exhausted,
            ready_ltss=cfg.ready_ltss,
            ready_entry=cfg.ready_entry,
            ready_dq=cfg.ready_data_quality,
            ready_tradeability=cfg.ready_tradeability_score,
        )

    # -- funding stage (80 symbols per 5-minute batch) -----------------------------
    def _funding_allowance(self, count: int, now_ms: int) -> int:
        """How many of ``count`` symbols may fetch funding in this window."""
        window_ms = FUNDING_BATCH_WINDOW_SEC * 1000
        batch = max(1, self._config.funding.backfill_symbols_per_batch)
        if self._funding_window_start_ms is None or now_ms - self._funding_window_start_ms >= window_ms:
            self._funding_window_start_ms = now_ms
            self._funding_window_used = 0
        allowed = max(0, batch - self._funding_window_used)
        granted = min(count, allowed)
        self._funding_window_used += granted
        return granted

    async def _fetch_funding(
        self, symbol: str, exchange_meta: Mapping[str, Any], as_of_ms: int, now_ms: int
    ) -> dict[str, Any]:
        """Settled 7/30/90D funding sums + coverage (design 10.1).

        Symbols beyond the 80-per-5min batch are *deferred* (not fetched);
        their windows stay null with a batch reason and are retried on a
        later run -- the backfill spans batches and records the remainder.
        Fetch exceptions degrade to empty events (incomplete coverage),
        never to fabricated zeros.
        """
        out: dict[str, Any] = {"requested": 0, "deferred": 0, "events": [],
                               "windows": {}, "fetched_at_ms": now_ms}
        if self._funding_allowance(1, now_ms) < 1:
            out["deferred"] = 1
            out["reason"] = "FUNDING_BATCH_DEFERRED"
            return out
        out["requested"] = 1
        start_ms = as_of_ms - _LOOKBACK_MS[90]
        try:
            events = await _maybe_await(self._funding_history_fn(symbol, start_ms, as_of_ms))
        except Exception:
            events = []
        out["events"] = list(events or [])
        onboard = exchange_meta.get("onboard_at_ms")
        onboard_ms = onboard if isinstance(onboard, int) and onboard > 0 else None
        for days in (7, 30, 90):
            window_start = as_of_ms - _LOOKBACK_MS[days]
            try:
                coverage = self._funding_coverage_fn(out["events"], window_start, as_of_ms, onboard_ms)
            except Exception:
                coverage = None
            window_events = [e for e in out["events"]
                             if isinstance(e.get("t"), int) and e["t"] >= window_start]
            rates = [float(e["funding_rate"]) for e in window_events
                     if _finite(e.get("funding_rate")) is not None]
            total = sum(rates) if (coverage is not None and coverage.complete) else None
            if coverage is not None and coverage.complete and rates:
                positive = sum(1 for r in rates if r > 0) / len(rates)
            else:
                positive = None
            stability = None
            if days == 30 and coverage is not None and coverage.complete and len(rates) >= 2:
                try:
                    stability = statistics.pstdev(rates)
                except statistics.StatisticsError:
                    stability = None
            out["windows"][days] = {
                "sum": total,
                "positive_ratio": positive,
                "stability": stability,
                "complete": bool(coverage is not None and coverage.complete),
                "coverage_fraction": float(coverage.coverage_fraction) if coverage is not None else 0.0,
                "event_count": len(window_events),
                "reason_code": None if (coverage is not None and coverage.complete)
                else (coverage.reason_code if coverage is not None else "FUNDING_HISTORY_INCOMPLETE"),
            }
        return out

    # -- fundamentals stage (market 3600s / supply 21600s, single request) ---------
    async def _fetch_fundamentals(
        self, identity: Any, now_ms: int
    ) -> tuple[ProviderResult[Any], Any | None]:
        """CoinGecko market + supply through one atomic document fetch.

        The provider's ``/coins/{id}`` response already carries supply, so a
        market refresh updates the supply clock too -- no second request is
        ever issued for supply. An independent fetch happens only when no
        supply has been seen via market inside ``supply_sec``. Failures keep
        the previous snapshot (stale) instead of raising.
        """
        coingecko_id = getattr(identity, "coingecko_id", None)
        if not coingecko_id:
            return _unavailable_result("coingecko", now_ms, "IDENTITY_NOT_MAPPED"), None
        cfg = self._config.refresh
        market_ms = max(1, cfg.fundamental_sec) * 1000
        supply_ms = max(1, cfg.supply_sec) * 1000
        st = self._fund_state.get(coingecko_id)
        market_fresh = st is not None and now_ms - st["market_at"] <= market_ms
        supply_fresh = (
            st is not None and st.get("supply_at") is not None
            and now_ms - st["supply_at"] <= supply_ms
        )
        if market_fresh and supply_fresh:
            # Both clocks fresh: reuse the snapshot, no request at all.
            return st["result"], st["result"].data
        # Otherwise one atomic /coins/{id} fetch refreshes the market clock
        # and -- when the response carries supply -- the supply clock too.
        provider = self._registry.get("coingecko")
        try:
            result = await provider.fetch(identity)
        except Exception as exc:  # noqa: BLE001 - providers encapsulate, never raise
            result = ProviderResult(
                status="UNAVAILABLE",
                source=getattr(provider, "name", "coingecko"),
                fetched_at_ms=now_ms,
                as_of_ms=None,
                data=None,
                stale=False,
                reason_code="FUNDAMENTALS_FETCH_FAILED",
                error_message=f"{type(exc).__name__}"[:120],
            )
        if result.status == "OK" and result.data is not None:
            supply_at = st.get("supply_at") if st else None
            if _has_supply(result.data):
                supply_at = now_ms
            self._fund_state[coingecko_id] = {
                "result": result, "market_at": now_ms, "supply_at": supply_at,
            }
            return result, result.data
        if st is not None:
            # Keep serving the previous snapshot (provider already marks it
            # stale when it has one); timestamps stay put so DQ keeps aging.
            return st["result"], st["result"].data
        return result, result.data

    # -- spot / market stages (guarded) ----------------------------------------------
    async def _fetch_spot(
        self, identity: Any, as_of_ms: int, now_ms: int
    ) -> ProviderResult[Any]:
        try:
            result = await _maybe_await(self._spot_history_fn(identity, as_of_ms))
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            return _unavailable_result("spot", now_ms, "SPOT_FETCH_FAILED")
        if not isinstance(result, ProviderResult):
            return _unavailable_result("spot", now_ms, "SPOT_BAD_SHAPE")
        return result

    async def _fetch_market(self, symbol: str, as_of_ms: int, now_ms: int) -> dict[str, Any]:
        try:
            market = await _maybe_await(self._market_inputs_fn(symbol, as_of_ms))
        except Exception:  # noqa: BLE001 - degrade, never raise
            market = {}
        out = dict(market) if isinstance(market, Mapping) else {}
        out.setdefault("fetched_at_ms", now_ms)
        return out

    # -- FULL stage: unlock / social / catalyst (Task 17, design 5.3-5.5) -----
    async def _fetch_provider(
        self, name: str, identity: Any, as_of_ms: int, now_ms: int
    ) -> ProviderResult[Any]:
        """Guarded single-provider fetch by registry name (never raises).

        Real Task 17 providers accept ``(identity, as_of_ms)``; the shared
        ``NullProvider`` accepts ``(identity)`` -- both shapes are tried so
        unregistered names degrade to explicit ``UNAVAILABLE``.
        """
        provider = self._registry.get(name)
        try:
            try:
                result = await _maybe_await(provider.fetch(identity, as_of_ms))
            except TypeError:
                result = await _maybe_await(provider.fetch(identity))
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            result = ProviderResult(
                status="UNAVAILABLE",
                source=getattr(provider, "name", name),
                fetched_at_ms=now_ms,
                as_of_ms=None,
                data=None,
                stale=False,
                reason_code=f"{name.upper()}_FETCH_FAILED",
                error_message=f"{type(exc).__name__}"[:120],
            )
        if not isinstance(result, ProviderResult):
            return ProviderResult(
                status="UNAVAILABLE",
                source=name,
                fetched_at_ms=now_ms,
                as_of_ms=None,
                data=None,
                stale=False,
                reason_code=f"{name.upper()}_BAD_SHAPE",
                error_message=None,
            )
        return result

    async def _fetch_full(
        self,
        identity: Any,
        symbol: str,
        inputs: Mapping[str, Any],
        fund_data: Any | None,
        as_of_ms: int,
        now_ms: int,
    ) -> dict[str, Any]:
        """Fetch the three FULL providers and score unlock/narrative raws.

        Returns the provider results plus ``unlock_raw_15`` /
        ``narrative_raw_15`` (``None`` when unknown -- no reweighting
        downstream), catalyst pause signals, and persists the raw
        events/snapshots best-effort for point-in-time replay.
        """
        from diveintocrypto_desktop.shortlab.features import narrative as narrative_mod
        from diveintocrypto_desktop.shortlab.features import supply as supply_mod

        unlock_result = await self._fetch_provider("unlock", identity, as_of_ms, now_ms)
        social_result = await self._fetch_provider("social", identity, as_of_ms, now_ms)
        catalyst_result = await self._fetch_provider("catalyst", identity, as_of_ms, now_ms)

        unlock_data = unlock_result.data if unlock_result.data is not None else None
        unlock_events = _provider_events(unlock_data)
        circulating = _field(fund_data, "circulating_supply")
        if unlock_events is None and unlock_result.status == "OK":
            unlock_events = []
        unlock_raw, unlock_details, _ = supply_mod.compute_unlock_raw_15_forward(
            unlock_events, circulating, as_of_ms
        )

        social_data = social_result.data if social_result.data is not None else None
        if social_data is not None and social_result.status in ("OK", "PARTIAL"):
            narrative_raw, narrative_details, _ = narrative_mod.compute_narrative_raw_15(
                _field(social_data, "volume_prev_30d"),
                _field(social_data, "volume_30d"),
                _field(social_data, "contributors_prev_30d"),
                _field(social_data, "contributors_30d"),
                _field(social_data, "dominance_prev_30d"),
                _field(social_data, "dominance_30d"),
                inputs.get("return_30d", inputs.get("price_change_30d")),
                _spot_change_30d(inputs),
                social_volume_change_30d=_field(social_data, "volume_change_30d"),
            )
        else:
            narrative_raw, narrative_details = None, {}

        catalyst_data = (
            catalyst_result.data if catalyst_result.data is not None else None
        )
        catalyst_events = _provider_events(catalyst_data)
        major_event, top_severity = _catalyst_signals(catalyst_events or [], as_of_ms)

        full = {
            "unlock_result": unlock_result,
            "social_result": social_result,
            "catalyst_result": catalyst_result,
            "unlock_raw_15": unlock_raw,
            "unlock_details": unlock_details,
            "narrative_raw_15": narrative_raw,
            "narrative_details": narrative_details,
            "catalyst_major_event": major_event,
            "catalyst_severity": top_severity,
        }
        await self._persist_full_snapshots(
            identity, symbol, full, unlock_events,
            social_data, catalyst_events, as_of_ms, now_ms,
        )
        return full

    async def _persist_full_snapshots(
        self,
        identity: Any,
        symbol: str,
        full: Mapping[str, Any],
        unlock_events: list[Any] | None,
        social_data: Any | None,
        catalyst_events: list[Any] | None,
        as_of_ms: int,
        now_ms: int,
    ) -> None:
        """Best-effort persistence of FULL raw events (never fails the symbol)."""
        repo = self._repository
        if repo is None:
            return
        canonical = str(getattr(identity, "canonical_id", None) or symbol.lower())
        try:
            from diveintocrypto_desktop.shortlab.repository import (
                CatalystEventRecord,
                SocialSnapshotRecord,
                UnlockEventRecord,
            )

            if unlock_events:
                records = []
                for event in unlock_events:
                    records.append(
                        UnlockEventRecord(
                            event_id=str(_field(event, "event_id") or ""),
                            canonical_id=canonical,
                            known_at_ms=int(_field(event, "known_at_ms") or as_of_ms),
                            unlock_at_ms=int(_field(event, "unlock_at_ms") or 0),
                            amount_tokens=float(_field(event, "amount_tokens") or 0.0),
                            allocation_type=str(
                                _field(event, "allocation_type") or "OTHER"
                            ),
                            source=str(_field(event, "source") or "unlock"),
                            fetched_at_ms=int(
                                full["unlock_result"].fetched_at_ms or now_ms
                            ),
                        )
                    )
                await repo.save_unlock_events(
                    [r for r in records if r.event_id and r.unlock_at_ms > 0]
                )
            if social_data is not None and full["social_result"].status in ("OK", "PARTIAL"):
                metrics = {
                    name: _field(social_data, name)
                    for name in (
                        "volume_prev_30d", "volume_30d",
                        "contributors_prev_30d", "contributors_30d",
                        "dominance_prev_30d", "dominance_30d",
                        "price_change_30d", "spot_volume_change_30d",
                    )
                }
                await repo.save_social_snapshot(
                    SocialSnapshotRecord(
                        snapshot_id=f"social-{canonical}-{as_of_ms}",
                        canonical_id=canonical,
                        as_of_ms=int(_field(social_data, "as_of_ms") or as_of_ms),
                        fetched_at_ms=int(
                            full["social_result"].fetched_at_ms or now_ms
                        ),
                        source=str(full["social_result"].source or "social"),
                        metrics=metrics,
                    )
                )
            if catalyst_events:
                records = []
                for event in catalyst_events:
                    records.append(
                        CatalystEventRecord(
                            event_id=str(_field(event, "event_id") or ""),
                            canonical_id=canonical,
                            known_at_ms=int(_field(event, "known_at_ms") or as_of_ms),
                            announced_at_ms=int(_field(event, "announced_at_ms") or 0),
                            effective_at_ms=_optional_int(_field(event, "effective_at_ms")),
                            event_type=str(_field(event, "event_type") or "OTHER"),
                            severity=str(_field(event, "severity") or "INFO"),
                            confidence=float(_field(event, "confidence") or 0.0),
                            source_url=_field(event, "source_url"),
                            title=str(_field(event, "title") or ""),
                        )
                    )
                await repo.save_catalyst_events(
                    [r for r in records if r.event_id and r.announced_at_ms > 0]
                )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort here
            log.debug("shortlab FULL snapshot persist skipped for %s: %s",
                      symbol, str(exc)[:120])

    # -- input assembly ------------------------------------------------------------------
    def _build_inputs(
        self,
        symbol: str,
        row: Mapping[str, Any],
        identity: Any,
        exchange_meta: Mapping[str, Any],
        fund_data: Any | None,
        spot_result: ProviderResult[Any],
        market: Mapping[str, Any],
        funding: Mapping[str, Any],
        as_of_ms: int,
    ) -> dict[str, Any]:
        w30 = funding["windows"].get(30, {})
        w90 = funding["windows"].get(90, {})
        w7 = funding["windows"].get(7, {})
        spot_data = spot_result.data if spot_result.data is not None else None
        inputs: dict[str, Any] = {
            "symbol": symbol,
            "as_of_ms": as_of_ms,
            # Universe row (price context only; quote_volume deliberately NOT
            # copied -- the rolling ticker never enters scoring inputs).
            "current_price": _finite(row.get("price")),
            "price_change_24h": _pct(row.get("ch")),
            # Market leg (daily closes/highs, OI, spread/depth).
            "daily_closes": list(market.get("daily_closes") or []),
            "daily_highs": list(market.get("daily_highs") or []),
            "futures_qv_1d": _finite(market.get("futures_qv_1d")),
            "oi_value_usd": _finite(market.get("oi_value_usd")),
            "oi_change_7d": _finite(market.get("oi_change_7d")),
            "price_change_7d": _finite(market.get("price_change_7d")),
            "spread": _finite(market.get("spread")),
            "best_bid": _finite(market.get("best_bid")),
            "best_ask": _finite(market.get("best_ask")),
            "bid_notional_1pct": _finite(market.get("bid_notional_1pct")),
            "ask_notional_1pct": _finite(market.get("ask_notional_1pct")),
            "contract_status": exchange_meta.get("status"),
            # Funding windows (null unless the window is complete).
            "funding_7d": _finite(w7.get("sum")),
            "funding_30d": _finite(w30.get("sum")),
            "funding_90d": _finite(w90.get("sum")),
            "funding_positive_ratio_30d": _finite(w30.get("positive_ratio")),
            "funding_positive_ratio_90d": _finite(w90.get("positive_ratio")),
            "funding_rates_30d": None,
            "funding_30d_complete": bool(w30.get("complete", False)),
            "funding_30d_ratio_complete": bool(w30.get("complete", False)),
            "funding_90d_complete": bool(w90.get("complete", False)),
            # Fundamentals.
            "market_cap_usd": _field(fund_data, "market_cap_usd"),
            "fdv_usd": _field(fund_data, "fdv_usd"),
            "circulating_supply": _field(fund_data, "circulating_supply"),
            "total_supply": _field(fund_data, "total_supply"),
            "ath_price": _field(fund_data, "ath_usd", "ath_price"),
            "ath_date_ms": _field(fund_data, "ath_date_ms"),
            # Spot leg.
            "spot_volume_30d": _field(spot_data, "spot_volume_30d"),
            "spot_volume_prev_30d": _field(spot_data, "spot_volume_prev_30d"),
            "spot_quote_volume_24h": _field(spot_data, "spot_quote_volume_24h"),
            "futures_spot_volume_ratio": _field(spot_data, "futures_spot_volume_ratio_30d"),
            "spot_status": spot_result.status,
            "spot_applicable": spot_result.status != "NOT_APPLICABLE",
            # Identity / lifecycle metadata for the risk layer.
            "mapping_confidence": getattr(identity, "mapping_confidence", None),
            "contract_multiplier": getattr(identity, "contract_multiplier", None),
            "onboard_at_ms": exchange_meta.get("onboard_at_ms"),
            "delivery_at_ms": exchange_meta.get("delivery_at_ms"),
            "exchange_status": exchange_meta.get("status"),
        }
        closes = inputs["daily_closes"]
        if isinstance(closes, list) and len(closes) >= 32:
            try:
                last, prev = float(closes[-1]), float(closes[-31])
                if prev > 0:
                    inputs["return_30d"] = (last - prev) / prev
            except (TypeError, ValueError):
                pass
        if inputs["price_change_7d"] is None and isinstance(closes, list) and len(closes) >= 8:
            try:
                last, prev = float(closes[-1]), float(closes[-8])
                if prev > 0:
                    inputs["price_change_7d"] = (last - prev) / prev
            except (TypeError, ValueError):
                pass
        return inputs

    def _build_risk_meta(
        self,
        symbol: str,
        row: Mapping[str, Any],
        identity: Any,
        exchange_meta: Mapping[str, Any],
        live_symbols: set[str],
        metadata_all: Mapping[str, Any],
        fund_data: Any | None,
        spot_result: ProviderResult[Any],
        market: Mapping[str, Any],
        funding: Mapping[str, Any],
        as_of_ms: int,
        full: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Raw metadata reference for the Task 11 risk authority.

        The Task 8 universe lifecycle view is passed through as data
        (``exchange_status`` / ``delivery_at_ms`` / live presence) -- the
        service draws no veto conclusions from it; ``evaluate_risks`` alone
        decides BLOCK vs PAUSE vs WARN. On the FULL tier the catalyst pause
        signals and provider statuses ride along the same way.
        """
        w30 = funding["windows"].get(30, {})
        w7 = funding["windows"].get(7, {})
        meta: dict[str, Any] = {
            "symbol": symbol,
            "as_of_ms": as_of_ms,
            "mapping_confidence": getattr(identity, "mapping_confidence", None),
            "contract_multiplier": getattr(identity, "contract_multiplier", None),
            "onboard_at_ms": exchange_meta.get("onboard_at_ms"),
            "delivery_at_ms": exchange_meta.get("delivery_at_ms"),
            "exchange_status": exchange_meta.get("status"),
            "contract_status": exchange_meta.get("status"),
            "live_universe_present": symbol in live_symbols,
            "previously_seen": symbol in metadata_all,
            "price_change_24h": _pct(row.get("ch")),
            "price_change_7d": _finite(market.get("price_change_7d")),
            "funding_30d": _finite(w30.get("sum")),
            "funding_7d": _finite(w7.get("sum")),
            "funding_positive_ratio_30d": _finite(w30.get("positive_ratio")),
            "oi_value_usd": _finite(market.get("oi_value_usd")),
            "futures_qv_1d": _finite(market.get("futures_qv_1d")),
            "futures_quote_volume_24h": _finite(market.get("futures_qv_1d")),
        }
        if full is not None:
            meta["catalyst_major_event"] = bool(full.get("catalyst_major_event"))
            if full.get("catalyst_severity") is not None:
                meta["catalyst_severity"] = full["catalyst_severity"]
            for key in ("unlock_result", "social_result", "catalyst_result"):
                result = full.get(key)
                if result is not None:
                    meta[f"{key}"] = getattr(result, "status", None)
            meta["unlock_status"] = getattr(full.get("unlock_result"), "status", None)
            meta["social_status"] = getattr(full.get("social_result"), "status", None)
        return meta

    # -- DQ + source-meta ------------------------------------------------------------------
    def _build_field_states(
        self, identity: Any, exchange_meta: Mapping[str, Any], fund_result: ProviderResult[Any],
        spot_result: ProviderResult[Any], market: Mapping[str, Any], funding: Mapping[str, Any],
        as_of_ms: int, now_ms: int,
        full: Mapping[str, Any] | None = None,
    ) -> list[FieldState]:
        closes = market.get("daily_closes") or []
        n_daily = len(closes) if isinstance(closes, list) else 0
        states = [
            _dq_state("market_daily_price", n_daily, 70, market.get("fetched_at_ms", now_ms), "binance-klines"),
            _dq_ok("futures_qv_1d", market.get("futures_qv_1d") is not None, market.get("fetched_at_ms", now_ms), "binance-klines"),
            _dq_ok("oi_usd", market.get("oi_value_usd") is not None, market.get("fetched_at_ms", now_ms),
                   "binance-oi", market.get("oi_reason")),
            _dq_ok("contract_status", exchange_meta.get("status") is not None,
                   exchange_meta.get("observed_at_ms", now_ms), "binance-exchangeInfo"),
        ]
        for days, field_id in ((7, "funding_7d"), (30, "funding_30d"), (90, "funding_90d")):
            window = funding["windows"].get(days, {})
            if funding.get("deferred"):
                states.append(FieldState(field_id=field_id, status="UNAVAILABLE",
                                         fetched_at_ms=now_ms, reason_code="FUNDING_BATCH_DEFERRED",
                                         source="binance-funding"))
            elif window.get("complete"):
                states.append(FieldState(field_id=field_id, status="OK",
                                         fetched_at_ms=now_ms, source="binance-funding"))
            elif window.get("event_count"):
                states.append(FieldState(field_id=field_id, status="PARTIAL",
                                         coverage=window.get("coverage_fraction", 0.0),
                                         fetched_at_ms=now_ms,
                                         reason_code=window.get("reason_code"),
                                         source="binance-funding"))
            else:
                states.append(FieldState(field_id=field_id, status="UNAVAILABLE",
                                         fetched_at_ms=now_ms,
                                         reason_code=window.get("reason_code") or "FUNDING_HISTORY_INCOMPLETE",
                                         source="binance-funding"))
        fund_ok = fund_result.status == "OK" and fund_result.data is not None
        fund_fetched = fund_result.fetched_at_ms
        states.append(_dq_ok("mc", fund_ok and _field(fund_result.data, "market_cap_usd") is not None,
                             fund_fetched, "coingecko", fund_result.reason_code))
        states.append(_dq_ok("fdv", fund_ok and _field(fund_result.data, "fdv_usd") is not None,
                             fund_fetched, "coingecko", fund_result.reason_code))
        circ = _field(fund_result.data, "circulating_supply") if fund_ok else None
        total = _field(fund_result.data, "total_supply") if fund_ok else None
        if circ is not None and total is not None:
            states.append(FieldState(field_id="supply_float", status="OK", fetched_at_ms=fund_fetched, source="coingecko"))
        elif circ is not None or total is not None:
            states.append(FieldState(field_id="supply_float", status="PARTIAL", valid_count=1,
                                     fetched_at_ms=fund_fetched, source="coingecko"))
        else:
            states.append(FieldState(field_id="supply_float", status="UNAVAILABLE",
                                     fetched_at_ms=fund_fetched,
                                     reason_code=fund_result.reason_code, source="coingecko"))
        ath_n = (1 if (_field(fund_result.data, "ath_usd", "ath_price") is not None) else 0) + \
                (1 if (_field(fund_result.data, "ath_date_ms") is not None) else 0) if fund_ok else 0
        states.append(_dq_state("ath", ath_n, 2, fund_fetched, "coingecko", fund_result.reason_code))
        spot_applicable = spot_result.status != "NOT_APPLICABLE"
        spot_ok = spot_result.status in ("OK", "PARTIAL") and spot_result.data is not None
        if not spot_applicable:
            states.append(FieldState(field_id="spot_60d_qv", status="NOT_APPLICABLE",
                                     fetched_at_ms=spot_result.fetched_at_ms,
                                     reason_code=spot_result.reason_code, source="binance-spot"))
            states.append(FieldState(field_id="spot_24h_qv", status="NOT_APPLICABLE",
                                     fetched_at_ms=spot_result.fetched_at_ms,
                                     reason_code=spot_result.reason_code, source="binance-spot"))
        else:
            bars = _finite(getattr(spot_result.data, "spot_daily_bars", None)) if spot_ok else None
            have_30 = spot_ok and _field(spot_result.data, "spot_volume_30d") is not None
            have_prev = spot_ok and _field(spot_result.data, "spot_volume_prev_30d") is not None
            if have_30 and have_prev:
                states.append(FieldState(field_id="spot_60d_qv", status="OK",
                                         fetched_at_ms=spot_result.fetched_at_ms, source="binance-spot"))
            elif bars:
                states.append(FieldState(field_id="spot_60d_qv", status="PARTIAL", valid_count=bars,
                                         fetched_at_ms=spot_result.fetched_at_ms,
                                         reason_code=spot_result.reason_code or "SPOT_HISTORY_INCOMPLETE",
                                         source="binance-spot"))
            else:
                states.append(FieldState(field_id="spot_60d_qv", status="UNAVAILABLE",
                                         fetched_at_ms=spot_result.fetched_at_ms,
                                         reason_code=spot_result.reason_code, source="binance-spot"))
            states.append(_dq_ok("spot_24h_qv",
                                 spot_ok and _field(spot_result.data, "spot_quote_volume_24h") is not None,
                                 spot_result.fetched_at_ms, "binance-spot", spot_result.reason_code))
        states.append(_dq_ok("basis", False, now_ms, "binance-basis", "BASIS_NOT_WIRED"))
        states.append(_dq_ok("book_depth", False, market.get("fetched_at_ms", now_ms),
                             "binance-depth", market.get("depth_reason") or "DEPTH_NOT_WIRED"))
        confidence = getattr(identity, "mapping_confidence", None)
        identity_ok = confidence in ("VERIFIED", "HIGH", "MEDIUM")
        states.append(FieldState(field_id="canonical_mapping",
                                 status="OK" if identity_ok else "UNAVAILABLE",
                                 fetched_at_ms=now_ms,
                                 reason_code=None if identity_ok else "IDENTITY_UNVERIFIED",
                                 source="shortlab-identity"))
        states.append(FieldState(field_id="profile_basis",
                                 status="OK" if identity_ok else "UNAVAILABLE",
                                 fetched_at_ms=now_ms,
                                 reason_code=None if identity_ok else "IDENTITY_UNVERIFIED",
                                 source="shortlab-identity"))
        if full is not None:
            states.extend(
                full_tier_field_states(
                    full.get("unlock_result"),
                    full.get("social_result"),
                    full.get("catalyst_result"),
                    as_of_ms=as_of_ms,
                )
            )
        return states

    def _build_feature_source_meta(
        self, identity: Any, exchange_meta: Mapping[str, Any], fund_result: ProviderResult[Any],
        spot_result: ProviderResult[Any], market: Mapping[str, Any], funding: Mapping[str, Any],
        as_of_ms: int, now_ms: int,
        full: Mapping[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Field-level provenance for the feature snapshot (Task 2 contract)."""
        states = {s.field_id: s for s in self._build_field_states(
            identity, exchange_meta, fund_result, spot_result, market, funding, as_of_ms, now_ms)}
        windows = funding.get("windows", {})
        coverage_of = {
            "market_daily": 1.0,
            "futures_volume_24h": 1.0,
            "oi_usd": 1.0,
            "contract_status": 1.0,
            "funding_7d": float(windows.get(7, {}).get("coverage_fraction", 0.0)),
            "funding_30d": float(windows.get(30, {}).get("coverage_fraction", 0.0)),
            "funding_90d": float(windows.get(90, {}).get("coverage_fraction", 0.0)),
            "market_cap": 1.0,
            "fdv": 1.0,
            "supply_float": 1.0,
            "ath": 1.0,
            "spot_volume_60d": 1.0,
            "spot_volume_24h": 1.0,
            "basis": 0.0,
            "orderbook_depth": 0.0,
            "identity": 1.0,
            "profile": 1.0,
        }
        field_map = {
            "market_daily": "market_daily_price",
            "futures_volume_24h": "futures_qv_1d",
            "oi_usd": "oi_usd",
            "contract_status": "contract_status",
            "funding_7d": "funding_7d",
            "funding_30d": "funding_30d",
            "funding_90d": "funding_90d",
            "market_cap": "mc",
            "fdv": "fdv",
            "supply_float": "supply_float",
            "ath": "ath",
            "spot_volume_60d": "spot_60d_qv",
            "spot_volume_24h": "spot_24h_qv",
            "basis": "basis",
            "orderbook_depth": "book_depth",
            "identity": "canonical_mapping",
            "profile": "profile_basis",
        }
        meta: dict[str, dict[str, Any]] = {}
        for repo_field, dq_field in field_map.items():
            st = states[dq_field]
            meta[repo_field] = _meta_block(
                st.status, st.fetched_at_ms, as_of_ms, st.source or "shortlab",
                st.reason_code, coverage_of[repo_field],
            )
        if full is not None:
            for repo_field, result_key in (
                ("unlock", "unlock_result"),
                ("social", "social_result"),
                ("catalyst", "catalyst_result"),
            ):
                result = full.get(result_key)
                if result is None:
                    continue
                meta[repo_field] = _meta_block(
                    str(getattr(result, "status", "UNAVAILABLE")),
                    getattr(result, "fetched_at_ms", now_ms),
                    as_of_ms,
                    str(getattr(result, "source", None) or repo_field),
                    getattr(result, "reason_code", None),
                    1.0 if str(getattr(result, "status", "")) == "OK" else 0.0,
                )
        return meta

    # -- Entry stage (top-N, 240-call budget) -------------------------------------------
    async def _run_entry_stage(
        self, symbols: list[str], as_of_ms: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Budgeted Entry round over the shortlist head.

        One :class:`EntryBudget` per round: ``entry_max_upstream_calls_per_run``
        (240), ``entry_concurrency`` (2), ``entry_cache_ttl_sec`` (3600) --
        every real upstream attempt spends budget through the shared
        data-client limiters; exhaustion queues the remainder with
        ``ENTRY_BUDGET_EXHAUSTED`` and no further network.
        """
        if not symbols:
            return {}, {"calls_made": 0, "cache_hits": 0, "queued_symbols": ()}
        refresh_cfg = self._config.refresh
        budget = EntryBudget(
            max_calls=refresh_cfg.entry_max_upstream_calls_per_run,
            concurrency=refresh_cfg.entry_concurrency,
            ttl_sec=refresh_cfg.entry_cache_ttl_sec,
            clock=self._clock,
        )
        try:
            batch = await _maybe_await(self._entry_runner(
                list(symbols),
                budget=budget,
                max_symbols=max(1, self._config.universe.entry_depth_top),
                as_of_ms=as_of_ms,
                now_ms=self._now(),
            ))
        except Exception as exc:  # noqa: BLE001 - Entry failure degrades, never fails the job
            log.warning("shortlab entry batch failed, continuing without entry: %s", str(exc)[:150])
            return {}, {"calls_made": budget.used_calls, "cache_hits": budget.cache_hits,
                        "queued_symbols": tuple(symbols)}
        by_symbol = {item.symbol: item for item in (batch.items or ())}
        stats = dict(batch.stats or {})
        stats.setdefault("calls_made", budget.used_calls)
        stats.setdefault("cache_hits", budget.cache_hits)
        stats["queued_symbols"] = tuple(batch.queued_symbols or ())
        return by_symbol, stats


# ---------------------------------------------------------------------------
# Module-level fetch defaults (production wiring; tests inject fakes)
# ---------------------------------------------------------------------------


async def _default_spot_history(identity: Any, as_of_ms: int) -> ProviderResult[Any]:
    from diveintocrypto_desktop.data import spot as _spot

    return await _spot.spot_history(identity, as_of_ms)


async def _default_market_inputs(symbol: str, as_of_ms: int) -> dict[str, Any]:
    """Best-effort market leg from the existing data clients (all guarded).

    Daily closes/highs + last-closed ``qv`` from ``fetch_klines_range``,
    verified OI USD via ``fetch_oi_hist`` + ``premium_index`` +
    ``resolve_oi_value_usd`` (Task 7 contract), spread + 1% notionals from
    ``fetch_depth`` + ``book_panel``. Any failure degrades that key to
    ``None`` + reason -- the caller never sees an exception.
    """
    from diveintocrypto_desktop.data import binance_klines as _klines
    from diveintocrypto_desktop.data import funding as _funding
    from diveintocrypto_desktop.data import open_interest as _oi
    from diveintocrypto_desktop.data import orderbook as _book

    now_ms = _now_ms()
    out: dict[str, Any] = {"fetched_at_ms": now_ms, "daily_closes": [], "daily_highs": []}
    try:
        dailies = await _klines.fetch_klines_range(symbol, "1d", as_of_ms - 100 * DAY_MS, as_of_ms)
    except Exception:
        dailies = []
    closes: list[float] = []
    highs: list[float] = []
    for candle in dailies or []:
        try:
            closes.append(float(candle["c"]))
            highs.append(float(candle["h"]))
        except (KeyError, TypeError, ValueError):
            continue
    out["daily_closes"] = closes
    out["daily_highs"] = highs
    try:
        last_qv = float(dailies[-1]["qv"]) if dailies and dailies[-1].get("qv") is not None else None
    except (TypeError, ValueError):
        last_qv = None
    out["futures_qv_1d"] = last_qv

    try:
        oi_points = await _oi.fetch_oi_hist(symbol, "5m", limit=1)
        mark = await _funding.premium_index(symbol)
        latest = (oi_points or [{}])[-1]
        resolved = _oi.resolve_oi_value_usd(
            latest.get("oi"), latest.get("oi_value"),
            mark_price=mark.get("mark_price"),
            oi_time_ms=latest.get("t"), mark_time_ms=mark.get("time_ms"),
            quote_unit="USDT",
        )
        out["oi_value_usd"] = resolved.get("oi_value_usd")
        out["oi_reason"] = resolved.get("reason_code")
    except Exception:
        out["oi_value_usd"] = None
        out["oi_reason"] = "OI_FETCH_FAILED"

    try:
        depth = await _book.fetch_depth(symbol)
        bids = _book.parse_levels(depth.get("bids"))
        asks = _book.parse_levels(depth.get("asks"))
        panel = _book.book_panel(bids, asks, now_ms=now_ms)
        if isinstance(panel, Mapping) and "unavailable" not in panel:
            mid = float(panel.get("mid") or 0.0)
            best_bid = bids[0][0] if bids else None
            best_ask = asks[0][0] if asks else None
            out["best_bid"] = best_bid
            out["best_ask"] = best_ask
            out["spread"] = ((best_ask - best_bid) / mid) if (best_bid and best_ask and mid > 0) else None
            out["bid_notional_1pct"] = panel.get("bid_notional_1pct")
            out["ask_notional_1pct"] = panel.get("ask_notional_1pct")
        else:
            out["depth_reason"] = panel.get("unavailable") if isinstance(panel, Mapping) else "DEPTH_FAILED"
    except Exception:
        out["depth_reason"] = "DEPTH_FETCH_FAILED"
    return out


# ---------------------------------------------------------------------------
# Field accessors
# ---------------------------------------------------------------------------


def _field(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            try:
                return getattr(obj, name)
            except Exception:  # noqa: BLE001 - defensive accessor
                continue
    return None


def _has_supply(data: Any) -> bool:
    return _field(data, "circulating_supply") is not None or _field(data, "total_supply") is not None


def _pct(value: Any) -> float | None:
    """Universe ``ch`` is percent (e.g. 2.5 = +2.5%) -> decimal fraction."""
    number = _finite(value)
    if number is None:
        return None
    return number / 100.0


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _provider_events(data: Any) -> list[Any] | None:
    """Extract the event list from an unlock/catalyst provider payload."""
    if data is None:
        return None
    events = _field(data, "events")
    if events is None:
        return None
    if isinstance(events, (list, tuple)):
        return list(events)
    return None


def _spot_change_30d(inputs: Mapping[str, Any]) -> float | None:
    """Aligned 30D spot-volume change from already-closed inputs (or None)."""
    if not isinstance(inputs, Mapping):
        return None
    recent = _finite(inputs.get("spot_volume_30d"))
    previous = _finite(inputs.get("spot_volume_prev_30d"))
    if recent is None or previous is None or previous <= 0 or recent < 0:
        return None
    return recent / previous - 1.0


CATALYST_PAUSE_WINDOW_MS = 30 * DAY_MS


def _catalyst_signals(
    events: list[Any], as_of_ms: int
) -> tuple[bool, str | None]:
    """Pause signals for the Task 11 risk engine from catalyst events.

    Returns ``(major_in_window, top_severity)``: ``major_in_window`` is True
    when a MAJOR event was announced inside the 30D pause window at or
    before ``as_of_ms`` (drives ``PAUSE_MAJOR_CATALYST``); ``top_severity``
    is the highest recent severity (MAJOR > MATERIAL > INFO) or None when
    no event sits in the window.
    """
    as_of = int(as_of_ms)
    rank = {"INFO": 0, "MATERIAL": 1, "MAJOR": 2}
    top: str | None = None
    major = False
    for event in events or []:
        announced = _optional_int(_field(event, "announced_at_ms"))
        if announced is None or not (as_of - CATALYST_PAUSE_WINDOW_MS <= announced <= as_of):
            continue
        severity = str(_field(event, "severity") or "INFO").upper()
        if severity not in rank:
            severity = "INFO"
        if top is None or rank[severity] > rank[top]:
            top = severity
        if severity == "MAJOR":
            major = True
    return major, top


def _dq_ok(field_id: str, ok: bool, fetched_at_ms: int | None, source: str,
           reason: str | None = None) -> FieldState:
    if ok:
        return FieldState(field_id=field_id, status="OK", fetched_at_ms=fetched_at_ms, source=source)
    return FieldState(field_id=field_id, status="UNAVAILABLE", fetched_at_ms=fetched_at_ms,
                      reason_code=reason or "DATA_MISSING", source=source)


def _dq_state(field_id: str, valid: int | float | None, required: int,
              fetched_at_ms: int | None, source: str, reason: str | None = None) -> FieldState:
    try:
        n = float(valid) if valid is not None else 0.0
    except (TypeError, ValueError):
        n = 0.0
    if n >= required:
        return FieldState(field_id=field_id, status="OK", fetched_at_ms=fetched_at_ms, source=source)
    if n > 0:
        return FieldState(field_id=field_id, status="PARTIAL", valid_count=n,
                          fetched_at_ms=fetched_at_ms,
                          reason_code=reason or "PARTIAL_COVERAGE", source=source)
    return FieldState(field_id=field_id, status="UNAVAILABLE", fetched_at_ms=fetched_at_ms,
                      reason_code=reason or "DATA_MISSING", source=source)


def _contract_meta_dict(meta: Any) -> dict[str, Any]:
    if meta is None:
        return {}
    if isinstance(meta, Mapping):
        return dict(meta)
    try:
        return dict(dataclasses.asdict(meta))
    except Exception:  # noqa: BLE001 - duck-typed fallback
        out: dict[str, Any] = {}
        for name in ("symbol", "onboard_at_ms", "first_seen_ms", "delivery_at_ms",
                     "status", "contract_type", "observed_at_ms",
                     "contract_multiplier", "multiplier_source"):
            if hasattr(meta, name):
                out[name] = getattr(meta, name)
        return out
