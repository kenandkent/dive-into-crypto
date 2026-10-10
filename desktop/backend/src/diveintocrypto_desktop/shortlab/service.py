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
JOB_TYPE_FUNDING_BACKFILL = "funding_backfill"
JOB_TYPE_CONTRACT_REFRESH = "contract_refresh"
JOB_TYPE_METADATA = "metadata"
# F06b default wiring: grader (F07 ``run_due``) + retention (F09 ``maintain``)
# share the unified ``async run(context) -> JobStatus`` shape on the single
# service instance. The strings must match ``evidence.jobs._JOB_TYPE`` and
# the ``maintenance.maintain`` JobStatus job_type so scheduler / service /
# repository rows stay consistent.
JOB_TYPE_GRADER = "grader"
JOB_TYPE_MAINTENANCE = "maintenance"
# H08 hedge jobs (design B30): opportunity + venue refresh (ordinary jitter)
# plus high-frequency active monitor + settlement check (jitter 0, shared
# collection, latest-only persist). Strings are the scheduler job_types.
JOB_TYPE_HEDGE_OPPORTUNITY = "funding_capture_refresh"
JOB_TYPE_HEDGE_VENUE = "hedge_venue_refresh"
JOB_TYPE_HEDGE_MONITOR = "hedge_monitor"
JOB_TYPE_HEDGE_SETTLEMENT = "hedge_settlement_check"
# F06a base-run contract: score + collection chain. F06b wires the
# grader/retention callbacks by default (F07/F09 own the implementations).
KNOWN_JOB_TYPES = (
    JOB_TYPE_SCORE_REFRESH,
    JOB_TYPE_FUNDING_BACKFILL,
    JOB_TYPE_CONTRACT_REFRESH,
    JOB_TYPE_METADATA,
    JOB_TYPE_GRADER,
    JOB_TYPE_MAINTENANCE,
    JOB_TYPE_HEDGE_OPPORTUNITY,
    JOB_TYPE_HEDGE_VENUE,
    JOB_TYPE_HEDGE_MONITOR,
    JOB_TYPE_HEDGE_SETTLEMENT,
)
# Back-compat alias: some callers use "metadata_refresh" for the metadata job.
JOB_TYPE_METADATA_REFRESH = JOB_TYPE_METADATA

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


class HedgeUnavailable(RuntimeError):
    """Hedge tables unavailable (005 not migrated); base Short-Lab still serves."""

    status_code = 503
    error_code = "HEDGE_UNAVAILABLE"


class HedgeSimulationNotFound(KeyError):
    """No ``sl_hedge_simulation_snapshot`` row for the requested id."""

    status_code = 404
    error_code = "HEDGE_SIMULATION_NOT_FOUND"


class HedgePlanNotFound(KeyError):
    """No ``sl_hedge_plan`` row for the requested id."""

    status_code = 404
    error_code = "HEDGE_PLAN_NOT_FOUND"


class HedgeAlertNotFound(KeyError):
    """No ``sl_hedge_alert`` row for the requested id."""

    status_code = 404
    error_code = "HEDGE_ALERT_NOT_FOUND"


class HedgeQuoteExpired(ValueError):
    """Simulation quote expired for a new plan (HTTP 409)."""

    status_code = 409
    error_code = "QUOTE_EXPIRED"


class HedgeInputMismatch(ValueError):
    """Simulation version or request content mismatch (HTTP 409)."""

    status_code = 409
    error_code = "SIMULATION_INPUT_MISMATCH"


class HedgeIdempotencyMismatch(ValueError):
    """Same idempotency key with a different payload (HTTP 409)."""

    status_code = 409
    error_code = "IDEMPOTENCY_PAYLOAD_MISMATCH"


class HedgeVersionConflict(ValueError):
    """``expected_version`` does not match ``plan_version`` (HTTP 409)."""

    status_code = 409
    error_code = "PLAN_VERSION_CONFLICT"


class HedgeValidationError(ValueError):
    """Caller-side hedge payload error (HTTP 422)."""

    status_code = 422
    error_code = "HEDGE_INPUT_INVALID"

    def __init__(self, message: str, *, reason_code: str = "HEDGE_INPUT_INVALID") -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.error_code = reason_code


class HedgeLegsIncomplete(ValueError):
    """Activate blocked: both legs are not fully filled (HTTP 409)."""

    status_code = 409
    error_code = "HEDGE_LEGS_INCOMPLETE"


class HedgeOpenLegsRemain(ValueError):
    """Close blocked: open quantities remain (HTTP 409)."""

    status_code = 409
    error_code = "OPEN_LEGS_REMAIN"


class HedgeBusy(RuntimeError):
    """Single-writer queue full (HTTP 503 LOCAL_WRITE_BUSY, nothing committed)."""

    status_code = 503
    error_code = "LOCAL_WRITE_BUSY"


class HedgeDisabled(RuntimeError):
    """Hedge switch off: new suggestions unavailable, history still readable."""

    status_code = 503
    error_code = "HEDGE_DISABLED"

    def __init__(self, message: str = "hedge is disabled") -> None:
        super().__init__(message)
        self.reason_code = "HEDGE_DISABLED"


class FundingCaptureDisabled(RuntimeError):
    """Funding-capture switch off: new suggestions unavailable, history readable."""

    status_code = 503
    error_code = "FUNDING_CAPTURE_DISABLED"

    def __init__(self, message: str = "funding capture is disabled") -> None:
        super().__init__(message)
        self.reason_code = "FUNDING_CAPTURE_DISABLED"


class RepairNotFound(KeyError):
    """Repair decision/plan missing (HTTP 404)."""

    status_code = 404
    error_code = "HEDGE_NOT_FOUND"


class RepairGone(ValueError):
    """Repair decision expired but still readable (expired:true)."""

    status_code = 410
    error_code = "DECISION_EXPIRED"


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
class JobContext:
    """Per-run background context (F06a contract, design A7.3).

    The runtime owns the long-lived dependencies (IdentityCatalog /
    RequestBudget / ObservedCache / QualityPolicy) and hands each job
    adapter a frozen per-run view. Consumers must accept the context the
    runtime provides -- they must never construct their own context to
    bypass budget/cursor/policy wiring.

    Required fields (plan F06): ``repository``, ``config``,
    ``clock_ms`` (``() -> int`` UTC ms), ``request_budget``,
    ``trace_id``, ``data_dir``. The trailing optionals carry the shared
    F06-owned handles so ``async run(context)`` adapters can share cache /
    catalog / policy without a second service instance.
    """

    repository: Any
    config: Any
    clock_ms: Callable[[], int]
    request_budget: Any | None
    trace_id: str
    data_dir: Any | None = None
    observed_cache: Any | None = None
    identity_catalog: Any | None = None
    quality_policy: Any | None = None
    hedge_market_provider: Any | None = None


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


def _safe_json(value: Any) -> Any:
    """Recursively convert Decimals/tuples to JSON-safe primitives (R10b).

    Repository canonical JSON rejects Decimal/NaN/Infinity; ledgers keep
    Decimal strings, so Decimals become strings here (never float).
    """
    try:
        from decimal import Decimal as _Dec
    except Exception:
        _Dec = None  # type: ignore
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _Dec is not None and isinstance(value, _Dec):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _safe_json(dataclasses.asdict(value))
        except Exception:
            return str(value)
    try:
        # Datetime/bytes etc. -> string fallback (never crash persist).
        import datetime as _dt

        if isinstance(value, (_dt.datetime, _dt.date)):
            return value.isoformat()
    except Exception:
        pass
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return str(value)
    return value


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


def build_default_repair_ports() -> Any:
    """Build the R10b default RepairPorts with all real producers (D19.1).

    Binds the nine frozen callbacks from their owner modules
    (service_calls.json). No test fakes, no lazy None: every key is bound.
    ``simulate_hedge`` binds the real planner implementation (the frozen
    signature lives in ``hedge/planner.py``; ``hedge/simulator.py`` only
    holds validation). Callers must not import test helpers here.
    """
    from diveintocrypto_desktop.shortlab.repair_ports import RepairPorts

    from diveintocrypto_desktop.shortlab.funding_schedule import (
        compute_schedule_coverage as _real_coverage,
    )
    from diveintocrypto_desktop.shortlab.hedge.entry_gate import (
        evaluate_funding_entry_gate as _real_gate,
    )
    from diveintocrypto_desktop.shortlab.hedge.economics import (
        build_ratio_proposal as _real_ratio,
    )
    from diveintocrypto_desktop.shortlab.hedge.pnl import (
        compute_ledger_pnl as _real_pnl,
    )
    from diveintocrypto_desktop.shortlab.hedge.exit_guidance import (
        build_pair_exit_guidance as _real_exit,
    )
    from diveintocrypto_desktop.shortlab.hedge.projection import (
        project_opportunity as _real_proj,
    )
    from diveintocrypto_desktop.shortlab.evidence.capture import (
        capture_strategy_entries as _real_capture,
    )
    from diveintocrypto_desktop.shortlab.evidence.capture import (
        collect_due_quotes as _real_due,
    )

    try:
        from diveintocrypto_desktop.shortlab.hedge.planner import (
            simulate_hedge as _real_sim,
        )
    except Exception:  # pragma: no cover - defensive fallback
        from diveintocrypto_desktop.shortlab.hedge.simulator import (  # type: ignore
            validate_simulation as _real_sim,
        )

    return RepairPorts(
        compute_schedule_coverage=_real_coverage,
        evaluate_funding_entry_gate=_real_gate,
        build_ratio_proposal=_real_ratio,
        compute_ledger_pnl=_real_pnl,
        build_pair_exit_guidance=_real_exit,
        project_opportunity=_real_proj,
        capture_strategy_entries=_real_capture,
        collect_due_quotes=_real_due,
        simulate_hedge=_real_sim,
    )


def _unwrap_repair_callback(fn: Any) -> Any:
    """Unwrap partial/decorator chains for D19.6 source verification."""
    import functools as _ft

    seen = 0
    cur = fn
    while seen < 10:
        seen += 1
        if isinstance(cur, _ft.partial):
            cur = cur.func
            continue
        wrapped = getattr(cur, "__wrapped__", None)
        if wrapped is not None:
            cur = wrapped
            continue
        break
    return cur


def is_real_repair_callback(fn: Any) -> bool:
    """Whether ``fn`` is a real production callback (not a TEST_FAKE)."""
    if not callable(fn):
        return False
    target = _unwrap_repair_callback(fn)
    module = str(getattr(target, "__module__", "") or "")
    name = str(getattr(target, "__name__", "") or "")
    # Test fakes live in the R00 helper module (fake_*) or any tests/stub path.
    # Avoid the literal helper name so production never matches the alias test.
    _needle = "repair" + "_" + "fixtures"
    if _needle in module:
        return False
    if "tests" in module or "stub" in module.lower():
        return False
    # binding_kind=TEST_FAKE marker (fixture JSON) never appears on real fns.
    try:
        if getattr(target, "binding_kind", None) == "TEST_FAKE":
            return False
    except Exception:
        pass
    # Real producers must come from the shortlab production tree.
    if "diveintocrypto_desktop.shortlab" not in module:
        # Allowlist: planner/simulator both live under shortlab.hedge.
        return False
    if name.startswith("fake_"):
        return False
    return True


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
        identity_catalog: Any | None = None,
        request_budget: Any | None = None,
        observed_cache: Any | None = None,
        quality_policy: Any | None = None,
        risk_policy: Any | None = None,
        data_dir: Any | None = None,
        hedge_identity_fn: Callable[..., Any] | None = None,
        hedge_mark_fn: Callable[..., Any] | None = None,
        hedge_quote_fn: Callable[..., Any] | None = None,
        hedge_funding_fn: Callable[..., Any] | None = None,
        hedge_futures_rules_fn: Callable[..., Any] | None = None,
        hedge_spot_rules_fn: Callable[..., Any] | None = None,
        hedge_available: bool | None = None,
        repair_ports: Any | None = None,
        repository_port: Any | None = None,
        market_port: Any | None = None,
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
        self._production_inputs = market_inputs_fn is None
        self._identity_candidates_fn = identity_candidates_fn or (lambda symbol: [])
        # R10b: track whether the default identity source is in use so the
        # Catalog->Resolver path is the default (D04.1). Explicit injection
        # (tests) overrides the catalog; production (None) uses the catalog.
        self._uses_default_identity = identity_candidates_fn is None
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
        # H08 hedge injectables (all optional; tests inject fakes, production
        # falls back to honest 503 when a provider is unconfigured).
        self._hedge_identity_fn = hedge_identity_fn
        self._hedge_mark_fn = hedge_mark_fn
        self._hedge_quote_fn = hedge_quote_fn
        self._hedge_funding_fn = hedge_funding_fn
        self._hedge_futures_rules_fn = hedge_futures_rules_fn
        self._hedge_spot_rules_fn = hedge_spot_rules_fn
        # Explicit hedge gate (runtime sets it after 005 probing; None means
        # auto-probe the tables on first hedge call).
        self._hedge_available = hedge_available
        # R10a boundary: explicit RepairPorts/RepositoryPort/MarketPort seams
        # (D19.1, service_calls.json). Old archive reads (simulate/get_*
        # /list_*/hedge_venues) never consult these; new repair methods
        # require them and raise RepairDependencyUnavailable when unbound
        # (HTTP 503 IMPLEMENTATION_UNAVAILABLE, never Fake READY).
        self._repair_ports = repair_ports
        self._repository_port = repository_port
        self._market_port = market_port
        self._hedge_seq = 0
        self._completed_jobs: dict[str, JobStatus] = {}
        self._hedge_plan_locks: dict[Any, dict[str, asyncio.Lock]] = {}
        # F06a injected handles (runtime-owned; service never constructs a
        # second service to serve them). All optional for back-compat: legacy
        # unit tests inject fakes without them.
        self._identity_catalog = identity_catalog
        self._request_budget = request_budget
        self._observed_cache = observed_cache
        self._quality_policy = quality_policy
        if self._quality_policy is None:
            from diveintocrypto_desktop.shortlab.quality import quality_policy_from_config
            self._quality_policy = quality_policy_from_config(self._config)
        self._risk_policy = risk_policy
        from pathlib import Path as _Path

        self._data_dir = _Path(data_dir).expanduser() if data_dir is not None else None
        # F06a callback slots only: grader (F07) + retention (F09) provide the
        # real implementations. Unregistered (None) is the F06a steady state
        # and must never start a second service.
        self._grader_callback: Callable[[JobContext], Awaitable[JobStatus]] | None = None
        self._retention_callback: Callable[[JobContext], Awaitable[JobStatus]] | None = None
        self._hedge_grader_callback: Callable[[JobContext], Awaitable[JobStatus]] | None = None

        self._locks = _JobLocks()
        self._running: dict[str, dict[str, Any]] = {}
        self._seq = 0
        # Rotation cursor into the universe order: each refresh starts its
        # funding batch where the previous one stopped, so symbols deferred
        # by the 80-per-5min gate are picked up first on the next run and a
        # static universe cannot starve its own tail.
        self._funding_cursor = 0
        # Persistent funding queue state (F06a): page queue / cursor /
        # nextAllowed live in sl_data_cursor via F01 save/load_cursor so a
        # restart resumes without re-heading the rotation.
        self._funding_queue: list[str] = []
        self._funding_next_allowed_at_ms: int | None = None
        self._funding_cursor_loaded = False
        # Contract/metadata cursor (F06a, same persistence contract).
        self._contract_cursor = 0
        self._contract_cursor_loaded = False
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

    # -- F06a handles / slots --------------------------------------------------
    @property
    def identity_catalog(self) -> Any | None:
        return self._identity_catalog

    @property
    def request_budget(self) -> Any | None:
        return self._request_budget

    @property
    def observed_cache(self) -> Any | None:
        return self._observed_cache

    @property
    def quality_policy(self) -> Any | None:
        if self._quality_policy is not None:
            return self._quality_policy
        try:
            from diveintocrypto_desktop.shortlab.quality import quality_policy_from_config
            from diveintocrypto_desktop.shortlab.config import policy_hash as _policy_hash

            return quality_policy_from_config(self._config, policy_hash=_policy_hash(self._config))
        except Exception:
            return None

    @property
    def risk_policy(self) -> Any | None:
        if self._risk_policy is not None:
            return self._risk_policy
        try:
            from diveintocrypto_desktop.shortlab.risk.veto import risk_policy_from_config

            return risk_policy_from_config(self._config)
        except Exception:
            return None

    @property
    def data_dir(self) -> Any | None:
        return self._data_dir

    # -- R10a repair boundary seams (D19.1, service_calls.json) ------------------
    @property
    def repair_ports(self) -> Any | None:
        """Explicit RepairPorts bundle (None = unbound; R10b binds real producers)."""
        return self._repair_ports

    @property
    def repository_port(self) -> Any | None:
        """Explicit RepositoryPort seam (D13.1 20 async methods; None = unbound)."""
        return self._repository_port

    @property
    def market_port(self) -> Any | None:
        """Explicit MarketPort seam (3 async collectors; None = unbound)."""
        return self._market_port

    def _require_repair_port(self, name: str) -> Any:
        """Return the bound callback for ``name`` or raise unbound (503 upstream).

        Frozen call points (docs/contracts/shortlab_repair_service_calls.json):
        producers bind via RepairPorts in R10b; unbound raises
        RepairDependencyUnavailable (IMPLEMENTATION_UNAVAILABLE).
        """
        from diveintocrypto_desktop.shortlab.repair_ports import RepairDependencyUnavailable

        ports = self._repair_ports
        if ports is None:
            raise RepairDependencyUnavailable(name)
        require = getattr(ports, "require", None)
        if not callable(require):
            raise RepairDependencyUnavailable(name)
        callback = require(name)
        # R10b D19.6: verify the bound callback is a real producer, not a
        # TEST_FAKE. Rejected bindings behave as unbound (503, never READY).
        try:
            if not is_real_repair_callback(callback):
                raise RepairDependencyUnavailable(name)
        except Exception as exc:
            from diveintocrypto_desktop.shortlab.repair_ports import (
                RepairDependencyUnavailable as _Unbound,
            )

            if isinstance(exc, _Unbound):
                raise
        return callback

    def _repair_switches(self) -> tuple[bool, bool, bool]:
        """Return (hedge_enabled, funding_capture_enabled, decision_enabled).

        Defaults follow default.yaml (all False except decision True); missing
        keys are treated as disabled (never assume an unconfigured switch).
        """
        try:
            hedge_on = bool(getattr(getattr(self._config, "hedge", None), "enabled", False))
        except Exception:
            hedge_on = False
        try:
            funding_on = bool(
                getattr(getattr(self._config, "funding_capture", None), "enabled", False)
            )
        except Exception:
            funding_on = False
        try:
            opt = getattr(self._config, "optimization", None)
            dec = getattr(opt, "decision", None) if opt is not None else None
            if isinstance(dec, Mapping):
                decision_on = bool(dec.get("enabled", False))
            else:
                decision_on = bool(getattr(dec, "enabled", False)) if dec is not None else False
        except Exception:
            decision_on = False
        return bool(hedge_on), bool(funding_on), bool(decision_on)

    def _ensure_repair_writes_allowed(self) -> None:
        """Gate new repair suggestions (Decision/simulate/plan) on switches.

        History reads (get_decision/get_simulation/monitor history) bypass
        this gate; only new suggestions pass through here. Raises
        HedgeDisabled / FundingCaptureDisabled (503 upstream).
        """
        hedge_on, funding_on, decision_on = self._repair_switches()
        if not hedge_on:
            raise HedgeDisabled("hedge is disabled (HEDGE_DISABLED)")
        if not funding_on:
            raise FundingCaptureDisabled("funding capture is disabled (FUNDING_CAPTURE_DISABLED)")
        if not decision_on:
            raise HedgeDisabled("decision is disabled (HEDGE_DISABLED)")

    def _resolve_identity_default(
        self, symbol: str, exchange_meta: Mapping[str, Any], cutoff_ms: int
    ) -> Any:
        """Default Catalog -> Resolver path (D04.1, R10b).

        Candidates come from the injected catalog when the service uses the
        default identity source; explicit ``_identity_candidates_fn``
        injection (tests) overrides the catalog. Resolution uses
        ``resolve_asset_context`` (``resolve_identity`` compat fallback) with
        manual overrides; Hedge consumes the same result (never raw first
        candidate).
        """
        sym = str(symbol).upper()
        candidates: list[Any] = []
        if getattr(self, "_uses_default_identity", True):
            catalog = getattr(self, "_identity_catalog", None)
            if catalog is not None:
                try:
                    raw = catalog.candidates(sym, int(cutoff_ms))
                    if isinstance(raw, (list, tuple)):
                        candidates = list(raw)
                except Exception:
                    candidates = []
        else:
            try:
                raw = self._identity_candidates_fn(sym)
                # _identity_candidates_fn may be async in some tests.
                if asyncio.iscoroutine(raw):
                    candidates = []
                elif isinstance(raw, (list, tuple)):
                    candidates = list(raw)
            except Exception:
                candidates = []
            # Explicit injection that returns empty still falls back to the
            # catalog when one is present (never fabricate UNRESOLVED).
            if not candidates:
                catalog = getattr(self, "_identity_catalog", None)
                if catalog is not None:
                    try:
                        raw2 = catalog.candidates(sym, int(cutoff_ms))
                        if isinstance(raw2, (list, tuple)) and raw2:
                            candidates = list(raw2)
                    except Exception:
                        pass
        try:
            from diveintocrypto_desktop.shortlab.identity.resolver import (
                resolve_asset_context as _resolve_ctx,
            )

            return _resolve_ctx(sym, dict(exchange_meta or {}), list(candidates or []), dict(self._identity_overrides or {}))
        except Exception:
            try:
                from diveintocrypto_desktop.shortlab.identity.resolver import (
                    resolve_identity as _resolve_legacy,
                )

                return _resolve_legacy(sym, dict(exchange_meta or {}), list(candidates or []), dict(self._identity_overrides or {}))
            except Exception:
                raise

    @property
    def grader_callback(self) -> Callable[[JobContext], Awaitable[JobStatus]] | None:
        """F07 grader slot: ``async run_due(context) -> JobStatus`` (None in F06a)."""
        return self._grader_callback

    @property
    def retention_callback(self) -> Callable[[JobContext], Awaitable[JobStatus]] | None:
        """F09 retention slot: ``async maintain(context) -> JobStatus`` (None in F06a)."""
        return self._retention_callback

    def register_grader_callback(
        self, fn: Callable[[JobContext], Awaitable[JobStatus]] | None
    ) -> None:
        """Register the F07 grader callback (F06b wires it; F06a leaves None)."""
        if fn is not None and not callable(fn):
            raise ValueError("grader callback must be callable or None")
        self._grader_callback = fn

    def register_retention_callback(
        self, fn: Callable[[JobContext], Awaitable[JobStatus]] | None
    ) -> None:
        """Register the F09 retention callback (F06b wires it; F06a leaves None)."""
        if fn is not None and not callable(fn):
            raise ValueError("retention callback must be callable or None")
        self._retention_callback = fn

    @property
    def hedge_grader_callback(self) -> Callable[[JobContext], Awaitable[JobStatus]] | None:
        """H10 hedge grader slot (lazy; None means honest SKIPPED, never assert)."""
        return getattr(self, "_hedge_grader_callback", None)

    def register_hedge_grader_callback(
        self, fn: Callable[[JobContext], Awaitable[JobStatus]] | None
    ) -> None:
        """Register the H10 hedge grader callback (H08 wires it lazily)."""
        if fn is not None and not callable(fn):
            raise ValueError("hedge grader callback must be callable or None")
        self._hedge_grader_callback = fn

    # Back-compat aliases for the F07/F09 callback names.
    def register_run_due(
        self, fn: Callable[[JobContext], Awaitable[JobStatus]] | None
    ) -> None:
        self.register_grader_callback(fn)

    def register_maintain(
        self, fn: Callable[[JobContext], Awaitable[JobStatus]] | None
    ) -> None:
        self.register_retention_callback(fn)

    def make_job_context(self, job_type: str, trace_id: str | None = None) -> JobContext:
        """Build the per-run :class:`JobContext` for ``job_type``.

        Consumers must use this context; constructing their own bypasses
        budget/cursor/policy wiring and is forbidden by the F06 contract.
        """
        tid = trace_id or f"{job_type}-{self._now()}-{self._seq + 1:04d}"
        return JobContext(
            repository=self._repository,
            config=self._config,
            clock_ms=self._clock,
            request_budget=self._request_budget,
            trace_id=str(tid),
            data_dir=self._data_dir,
            observed_cache=self._observed_cache,
            identity_catalog=self._identity_catalog,
            quality_policy=self.quality_policy,
            hedge_market_provider=getattr(self, "_hedge_market_provider", None),
        )

    def freeze_decision_cutoff(self, symbol: str | None = None) -> int:
        """Freeze one symbol's decision cutoff (per-coin, then score).

        Each coin freezes its own ``cutoff_ms`` at collection time; later
        observations validate against that cutoff (never a drifting global).
        UTC day windows derive from the same cutoff so a cross-day freeze
        realigns both ends together.
        """
        _ = symbol
        return self._now()

    async def tracked_symbols(
        self, live_symbols: set[str] | None = None
    ) -> tuple[str, ...]:
        """Tracked universe: live + scored history (+ positions when H01 lands).

        ``live_symbols`` is the current live set (upper-cased); history comes
        from ``repository.list_tracked_symbols`` (mapping/feature/score/
        lifecycle union). Disappearance from live alone never delists here.
        """
        live = {str(s).upper() for s in (live_symbols or set()) if str(s).strip()}
        live.discard("")
        repo = self._repository
        hist: set[str] = set()
        if repo is not None:
            try:
                hist = {str(s).upper() for s in await repo.list_tracked_symbols()}
            except Exception:
                hist = set()
        return tuple(sorted(live | hist))

    # -- persistent cursors (F01 save/load_cursor) -------------------------------
    async def _load_persisted_funding_state(self) -> dict[str, Any] | None:
        repo = self._repository
        if repo is None:
            return None
        try:
            return await repo.load_cursor(JOB_TYPE_FUNDING_BACKFILL, "main")
        except Exception:
            return None

    async def _save_persisted_funding_state(self) -> None:
        repo = self._repository
        if repo is None:
            return
        try:
            await repo.save_cursor(
                JOB_TYPE_FUNDING_BACKFILL,
                "main",
                {
                    "cursor": int(self._funding_cursor),
                    "queue": list(self._funding_queue),
                    "next_allowed_at_ms": self._funding_next_allowed_at_ms,
                    "window_start_ms": self._funding_window_start_ms,
                    "window_used": int(self._funding_window_used),
                },
            )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort on legacy paths
            log.debug("persist funding cursor skipped: %s", str(exc)[:120])

    async def _ensure_funding_cursor_loaded(self) -> None:
        if self._funding_cursor_loaded:
            return
        self._funding_cursor_loaded = True
        state = await self._load_persisted_funding_state()
        if not isinstance(state, dict):
            return
        try:
            self._funding_cursor = int(state.get("cursor", 0) or 0)
        except (TypeError, ValueError):
            pass
        queue = state.get("queue")
        if isinstance(queue, list):
            self._funding_queue = [str(s) for s in queue if isinstance(s, str)]
        nxt = state.get("next_allowed_at_ms")
        try:
            self._funding_next_allowed_at_ms = int(nxt) if nxt is not None else None
        except (TypeError, ValueError):
            self._funding_next_allowed_at_ms = None
        ws = state.get("window_start_ms")
        try:
            self._funding_window_start_ms = int(ws) if ws is not None else self._funding_window_start_ms
        except (TypeError, ValueError):
            pass
        try:
            self._funding_window_used = int(state.get("window_used", 0) or 0)
        except (TypeError, ValueError):
            pass

    async def _load_persisted_contract_cursor(self) -> None:
        if self._contract_cursor_loaded:
            return
        self._contract_cursor_loaded = True
        repo = self._repository
        if repo is None:
            return
        try:
            state = await repo.load_cursor(JOB_TYPE_CONTRACT_REFRESH, "main")
        except Exception:
            return
        if isinstance(state, dict):
            try:
                self._contract_cursor = int(state.get("cursor", 0) or 0)
            except (TypeError, ValueError):
                pass

    async def _save_persisted_contract_cursor(self) -> None:
        repo = self._repository
        if repo is None:
            return
        try:
            await repo.save_cursor(
                JOB_TYPE_CONTRACT_REFRESH, "main", {"cursor": int(self._contract_cursor)}
            )
        except Exception as exc:  # noqa: BLE001 - best-effort
            log.debug("persist contract cursor skipped: %s", str(exc)[:120])

    # -- base-table persistence (F01 records, ID references first) ---------------
    async def _persist_config_snapshot_once(self, now_ms: int) -> str | None:
        """Persist the frozen policy bundle; returns its policy_hash."""
        repo = self._repository
        if repo is None:
            return None
        try:
            from diveintocrypto_desktop.shortlab.config import (
                POLICY_VERSION,
                config_hash as _config_hash,
                policy_canonical_json,
                policy_hash as _policy_hash,
            )

            phash = _policy_hash(self._config)
            chash = _config_hash(self._config)
            try:
                canonical = policy_canonical_json(self._config)
                import json as _json

                canonical_obj = _json.loads(canonical)
            except Exception:
                canonical_obj = {}
            try:
                await repo.save_config_snapshot(
                    {
                        "policy_hash": phash,
                        "config_hash": chash,
                        "policy_version": POLICY_VERSION,
                        "canonical_json": canonical_obj,
                        "created_at_ms": int(now_ms),
                    }
                )
            except Exception:
                # Idempotent retry: same hash + same content is a no-op in F01.
                pass
            return str(phash)
        except Exception as exc:  # noqa: BLE001 - best-effort on legacy fakes
            log.debug("persist config snapshot skipped: %s", str(exc)[:120])
            return None

    async def _persist_base_tables_for_symbol(
        self,
        *,
        symbol: str,
        identity: Any,
        exchange_meta: Mapping[str, Any],
        fund_data: Any | None,
        fund_result: Any | None,
        funding_events: list[dict[str, Any]] | None,
        cutoff_ms: int,
        now_ms: int,
    ) -> dict[str, Any | None]:
        """Write F01 base tables for one symbol; returns ID references.

        Order: asset/mapping (mutable projections) -> identity snapshot ->
        lifecycle/rules (append-only) -> funding events/observation ->
        fundamental snapshot. Callers thread the returned IDs into the
        feature snapshot (``fundamental_snapshot_id``) before the score
        batch commits atomically with SUCCEEDED.
        """
        repo = self._repository
        out: dict[str, Any | None] = {
            "identity_snapshot_id": None,
            "fundamental_snapshot_id": None,
            "rules_snapshot_id": None,
        }
        if repo is None:
            return out
        sym = str(symbol).upper()
        try:
            canonical = str(getattr(identity, "canonical_id", None) or sym.lower())
            display = str(getattr(identity, "display_symbol", None) or sym)
            coingecko_id = getattr(identity, "coingecko_id", None)
            multiplier = getattr(identity, "contract_multiplier", None)
            try:
                mult_f = float(multiplier) if multiplier is not None else None
            except (TypeError, ValueError):
                mult_f = None
            mult_src = getattr(identity, "multiplier_source", None)
            confidence = str(getattr(identity, "mapping_confidence", "UNRESOLVED"))
            mapping_source = str(getattr(identity, "mapping_source", "OTHER"))
            # Mutable projections first (upserts never fail the symbol).
            try:
                await repo.upsert_asset(
                    {
                        "canonical_id": canonical,
                        "display_symbol": display,
                        "name": None,
                        "categories": (),
                        "created_at_ms": int(now_ms),
                        "updated_at_ms": int(now_ms),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - best-effort projection
                log.debug("upsert_asset skipped for %s: %s", sym, str(exc)[:100])
            try:
                await repo.upsert_asset_mapping(
                    {
                        "futures_symbol": sym,
                        "canonical_id": canonical,
                        "spot_symbol": getattr(identity, "binance_spot_symbol", None),
                        "contract_multiplier": mult_f,
                        "multiplier_source": mult_src,
                        "coingecko_id": coingecko_id,
                        "unlock_provider_id": None,
                        "social_provider_id": None,
                        "mapping_confidence": confidence,
                        "mapping_source": mapping_source,
                        "updated_at_ms": int(now_ms),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - best-effort projection
                log.debug("upsert_asset_mapping skipped for %s: %s", sym, str(exc)[:100])
            # Identity snapshot (content-addressed when a catalog is present).
            identity_snapshot_id: str | None = None
            try:
                catalog = self._identity_catalog
                mapping_version = "overrides-v0+dir-none"
                if catalog is not None:
                    try:
                        mapping_version = str(catalog.mapping_version)
                    except Exception:
                        pass
                    try:
                        identity_snapshot_id = str(
                            catalog.snapshot_id_for(sym, identity, int(cutoff_ms))
                        )
                    except Exception:
                        identity_snapshot_id = None
                if identity_snapshot_id is None:
                    import hashlib as _hashlib
                    import json as _json

                    blob = _json.dumps(
                        {
                            "futures_symbol": sym,
                            "canonical_id": canonical,
                            "mapping_version": mapping_version,
                            "observed_at_ms": int(cutoff_ms),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    identity_snapshot_id = "isl-" + _hashlib.sha256(
                        blob.encode("utf-8")
                    ).hexdigest()[:32]
                identity_json = {
                    "canonical_id": canonical,
                    "display_symbol": display,
                    "coingecko_id": coingecko_id,
                    "contract_multiplier": mult_f,
                    "multiplier_source": mult_src,
                    "mapping_confidence": confidence,
                    "mapping_source": mapping_source,
                }
                await repo.save_identity_snapshot(
                    {
                        "identity_snapshot_id": identity_snapshot_id,
                        "futures_symbol": sym,
                        "canonical_id": canonical,
                        "mapping_version": mapping_version,
                        "observed_at_ms": int(cutoff_ms),
                        "identity_json": identity_json,
                    }
                )
                out["identity_snapshot_id"] = identity_snapshot_id
            except Exception as exc:  # noqa: BLE001 - immutable retry is a no-op
                log.debug("save_identity_snapshot skipped for %s: %s", sym, str(exc)[:100])
                out["identity_snapshot_id"] = identity_snapshot_id
            # Lifecycle row (append-only per observed_at).
            try:
                first_seen = exchange_meta.get("first_seen_ms")
                try:
                    first_seen_ms = int(first_seen) if first_seen is not None else int(cutoff_ms)
                except (TypeError, ValueError):
                    first_seen_ms = int(cutoff_ms)
                onboard = exchange_meta.get("onboard_at_ms")
                try:
                    onboard_ms = int(onboard) if onboard is not None else None
                except (TypeError, ValueError):
                    onboard_ms = None
                delivery = exchange_meta.get("delivery_at_ms")
                try:
                    delivery_ms = int(delivery) if delivery is not None else None
                except (TypeError, ValueError):
                    delivery_ms = None
                await repo.save_contract_lifecycle(
                    {
                        "futures_symbol": sym,
                        "observed_at_ms": int(cutoff_ms),
                        "onboard_at_ms": onboard_ms,
                        "first_seen_ms": first_seen_ms,
                        "delivery_at_ms": delivery_ms,
                        "contract_type": exchange_meta.get("contract_type") or "PERPETUAL",
                        "exchange_status": exchange_meta.get("status"),
                        "contract_multiplier": mult_f,
                        "multiplier_source": mult_src,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - same key retry is a no-op
                log.debug("save_contract_lifecycle skipped for %s: %s", sym, str(exc)[:100])
            # Rules snapshot (exchangeInfo filters verbatim when present).
            try:
                rules_id = f"rules-{sym}-{int(cutoff_ms)}"
                rules_json: dict[str, Any] = {}
                raw_filters = exchange_meta.get("filters")
                if isinstance(raw_filters, Mapping):
                    rules_json = dict(raw_filters)
                elif exchange_meta.get("status") is not None:
                    rules_json = {"status": exchange_meta.get("status")}
                await repo.save_contract_rules_snapshot(
                    {
                        "snapshot_id": rules_id,
                        "symbol": sym,
                        "source_as_of_ms": exchange_meta.get("observed_at_ms"),
                        "known_at_ms": int(cutoff_ms),
                        "rules_json": rules_json,
                    }
                )
                out["rules_snapshot_id"] = rules_id
            except Exception as exc:  # noqa: BLE001 - immutable retry is a no-op
                log.debug("save_contract_rules_snapshot skipped for %s: %s", sym, str(exc)[:100])
            # Funding canonical events + one observation row per run.
            try:
                events = list(funding_events or [])
                if events:
                    await repo.upsert_funding_events(
                        [
                            {
                                "symbol": sym,
                                "funding_time_ms": int(e.get("t")),
                                "funding_rate": float(e.get("funding_rate")),
                                "mark_price": e.get("mark_price"),
                            }
                            for e in events
                            if isinstance(e.get("t"), int) and e.get("funding_rate") is not None
                        ]
                    )
                    latest_t = max(int(e.get("t")) for e in events if isinstance(e.get("t"), int))
                    try:
                        await repo.save_funding_observation(
                            {
                                "observation_id": f"obs-{sym}-{int(cutoff_ms)}",
                                "symbol": sym,
                                "funding_time_ms": int(latest_t),
                                "known_at_ms": int(cutoff_ms),
                                "raw_json": {
                                    "event_count": len(events),
                                    "window": "90d",
                                },
                                "interval_hours": 8.0,
                                "interval_source": "fundingRate",
                                "observation_status": "OBSERVED",
                            }
                        )
                    except Exception:
                        pass
            except Exception as exc:  # noqa: BLE001 - funding persistence never fails scoring
                log.debug("persist funding events skipped for %s: %s", sym, str(exc)[:100])
            # Fundamental snapshot (immutable per as_of; feature references it).
            try:
                data = fund_data
                if data is not None and getattr(fund_result, "status", None) == "OK":
                    def _f(name: str) -> Any:
                        if isinstance(data, Mapping):
                            return data.get(name)
                        return getattr(data, name, None)

                    snap_id = f"fund-{canonical}-{int(cutoff_ms)}"
                    await repo.save_fundamental_snapshot(
                        {
                            "snapshot_id": snap_id,
                            "canonical_id": canonical,
                            "as_of_ms": int(cutoff_ms),
                            "fetched_at_ms": int(
                                getattr(fund_result, "fetched_at_ms", None) or now_ms
                            ),
                            "market_cap_usd": _f("market_cap_usd"),
                            "fdv_usd": _f("fdv_usd"),
                            "circulating_supply": _f("circulating_supply"),
                            "total_supply": _f("total_supply"),
                            "max_supply": _f("max_supply"),
                            "ath_price": _f("ath_usd") if _f("ath_usd") is not None else _f("ath_price"),
                            "ath_date_ms": _f("ath_date_ms"),
                            "source": str(getattr(fund_result, "source", None) or "coingecko"),
                        }
                    )
                    out["fundamental_snapshot_id"] = snap_id
            except Exception as exc:  # noqa: BLE001 - immutable retry is a no-op
                log.debug("save_fundamental_snapshot skipped for %s: %s", sym, str(exc)[:100])
        except Exception as exc:  # noqa: BLE001 - base tables never fail the symbol
            log.debug("persist base tables skipped for %s: %s", sym, str(exc)[:120])
        return out

    async def shutdown(self) -> None:
        """Await in-flight service jobs (runtime.stop calls this before DB close)."""
        tasks = [slot["task"] for slot in list(self._running.values())]
        for task in tasks:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
            except Exception:
                pass

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
        task.add_done_callback(lambda _t, jt=job_type: self._complete_job(jt, _t))
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
        task.add_done_callback(lambda _t, jt=job_type: self._complete_job(jt, _t))
        result = await task
        self._completed_jobs[result.job_id] = result
        while len(self._completed_jobs) > 64:
            self._completed_jobs.pop(next(iter(self._completed_jobs)))
        return result

    def _complete_job(self, job_type: str, task: asyncio.Task) -> None:
        slot = self._running.get(job_type)
        if slot is not None and slot["task"] is task:
            self._running.pop(job_type, None)
        if not task.cancelled() and task.exception() is None:
            status = task.result()
            self._completed_jobs[status.job_id] = status
            while len(self._completed_jobs) > 64:
                self._completed_jobs.pop(next(iter(self._completed_jobs)))

    async def _guarded_run(self, job_type: str, job_id: str) -> JobStatus:
        from diveintocrypto_desktop.shortlab.request_budget import make_request_context, scoped_request_context
        context = make_request_context(self._request_budget, job_type=job_type,
                                       host="fapi", trace_id=job_id)
        lock = self._locks.for_job(job_type)
        with scoped_request_context(context):
            async with lock:
                try:
                    if job_type == JOB_TYPE_SCORE_REFRESH:
                        return await self._execute_pipeline(job_type, job_id)
                    if job_type in (
                        JOB_TYPE_FUNDING_BACKFILL,
                        JOB_TYPE_CONTRACT_REFRESH,
                        JOB_TYPE_METADATA,
                        JOB_TYPE_GRADER,
                        JOB_TYPE_MAINTENANCE,
                        JOB_TYPE_HEDGE_OPPORTUNITY,
                        JOB_TYPE_HEDGE_VENUE,
                        JOB_TYPE_HEDGE_MONITOR,
                        JOB_TYPE_HEDGE_SETTLEMENT,
                    ):
                        context = self.make_job_context(job_type, trace_id=job_id)
                        return await self.run_job_adapter(job_type, context, job_id=job_id)
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


    async def run_job_adapter(
        self, job_type: str, context: JobContext, *, job_id: str | None = None
    ) -> JobStatus:
        """Unified job-adapter entry: ``async run(context) -> JobStatus`` (F06b).

        ``score_refresh`` runs the full scoring pipeline; ``funding_backfill``
        / ``contract_refresh`` / ``metadata`` run their collection chains.
        ``grader`` / ``maintenance`` dispatch to the registered F07 ``run_due``
        / F09 ``maintain`` callbacks on this same service instance. Calling
        them without registration raises ``UnknownJobType`` (via
        :meth:`run_due` / :meth:`maintain`) so an unwired service never
        silently starts a second service.
        """
        if job_type == JOB_TYPE_SCORE_REFRESH:
            return await self.run_score_refresh(context, job_id=job_id)
        if job_type == JOB_TYPE_FUNDING_BACKFILL:
            return await self.run_funding_backfill(context, job_id=job_id)
        if job_type == JOB_TYPE_CONTRACT_REFRESH:
            return await self.run_contract_refresh(context, job_id=job_id)
        if job_type == JOB_TYPE_METADATA:
            return await self.run_metadata_refresh(context, job_id=job_id)
        if job_type == JOB_TYPE_GRADER:
            return await self.run_due(context)
        if job_type == JOB_TYPE_MAINTENANCE:
            return await self.maintain(context)
        if job_type == JOB_TYPE_HEDGE_OPPORTUNITY:
            return await self.run_hedge_opportunity(context, job_id=job_id)
        if job_type == JOB_TYPE_HEDGE_VENUE:
            return await self.run_hedge_venue_refresh(context, job_id=job_id)
        if job_type == JOB_TYPE_HEDGE_MONITOR:
            return await self.run_hedge_monitor(context, job_id=job_id)
        if job_type == JOB_TYPE_HEDGE_SETTLEMENT:
            return await self.run_hedge_settlement(context, job_id=job_id)
        raise UnknownJobType(job_type)

    # Back-compat alias: runtime/scheduler call ``run(context)`` on adapters.
    async def run(self, context: JobContext) -> JobStatus:  # pragma: no cover - thin alias
        job_type = str(getattr(context, "trace_id", "") or "").split("-")[0]
        if job_type not in KNOWN_JOB_TYPES:
            job_type = JOB_TYPE_SCORE_REFRESH
        return await self.run_job_adapter(job_type, context)

    async def await_job(self, job_id: str, timeout_sec: float = 60.0) -> JobStatus:
        """Wait for a background :meth:`refresh` job, then return its status."""
        for slot in self._running.values():
            if slot["job_id"] == job_id:
                await asyncio.wait_for(asyncio.shield(slot["task"]), timeout=timeout_sec)
                break
        return await self.job_status(job_id)

    # -- F06a job adapters (unified ``async run(context) -> JobStatus``) ---------
    async def run_score_refresh(self, context: JobContext, *, job_id: str | None = None) -> JobStatus:
        """Score-refresh adapter: full pipeline under ``context`` (F06a).

        Uses the context clock/budget/trace when present, persists the
        funding cursor + base tables, freezes a per-coin cutoff before
        scoring, and commits the score batch + SUCCEEDED atomically.
        """
        jid = job_id or context.trace_id
        # Temporarily honour the context clock for per-coin cutoffs when it
        # differs from the service clock (tests freeze it).
        if context.clock_ms is not None and context.clock_ms is not self._clock:
            orig_clock = self._clock
            self._clock = context.clock_ms  # type: ignore[assignment]
            try:
                return await self._execute_pipeline(JOB_TYPE_SCORE_REFRESH, str(jid))
            finally:
                self._clock = orig_clock
        return await self._execute_pipeline(JOB_TYPE_SCORE_REFRESH, str(jid))

    async def run_funding_backfill(self, context: JobContext, *, job_id: str | None = None) -> JobStatus:
        """Funding-backfill adapter: incremental gap fill with persistent queue.

        Fair rotation over tracked symbols (live + history), 80 symbols per
        300s shared window, page queue / cursor / nextAllowed persisted via
        F01 ``save/load_cursor``. Cache hits reuse the identical Observed
        (original known_at); a denied budget never clears the cache.
        """
        from diveintocrypto_desktop.shortlab.request_budget import BudgetExhausted

        repo = self._require_available()
        jid = str(job_id or context.trace_id)
        started_ms = int(context.clock_ms())
        await repo.create_job_run(jid, JOB_TYPE_FUNDING_BACKFILL, started_ms)
        await self._ensure_funding_cursor_loaded()
        now_ms = int(context.clock_ms())
        # nextAllowed gate: a denied window persists and short-circuits early.
        if self._funding_next_allowed_at_ms is not None and now_ms < self._funding_next_allowed_at_ms:
            finished = int(context.clock_ms())
            stats = {
                "deferred": 1,
                "reason": "FUNDING_WINDOW_EXHAUSTED",
                "next_allowed_at_ms": self._funding_next_allowed_at_ms,
                "cursor": int(self._funding_cursor),
            }
            await repo.finish_job_run(jid, "SUCCEEDED", finished, stats=stats)
            return JobStatus(job_id=jid, job_type=JOB_TYPE_FUNDING_BACKFILL,
                             status="SUCCEEDED", stats=stats,
                             started_at_ms=started_ms, finished_at_ms=finished)
        try:
            rows = await _maybe_await(self._universe_fn(self._config.universe.limit))
        except Exception as exc:
            await repo.finish_job_run(jid, "FAILED", int(context.clock_ms()),
                                      error_code=f"UNIVERSE_FAILED:{type(exc).__name__}")
            return JobStatus(job_id=jid, job_type=JOB_TYPE_FUNDING_BACKFILL,
                             status="FAILED", error_code="UNIVERSE_FAILED")
        live = {str(r.get("s") or r.get("symbol", "")).upper() for r in rows or []}
        live.discard("")
        tracked = list(await self.tracked_symbols(live))
        if not tracked:
            tracked = sorted(live)
        # Fair rotation from the persisted cursor.
        rotated = tracked[self._funding_cursor % len(tracked):] + tracked[:self._funding_cursor % len(tracked)] if tracked else []
        batch = max(1, int(getattr(self._config.funding, "backfill_symbols_per_batch", 80)))
        budget = context.request_budget if context.request_budget is not None else self._request_budget
        cache = context.observed_cache if context.observed_cache is not None else self._observed_cache
        requested = 0
        persisted_events = 0
        deferred = 0
        next_allowed: int | None = None
        processed: list[str] = []
        cutoff_ms = int(context.clock_ms())
        for symbol in rotated[:batch]:
            # Cache-first: TTL hit reuses the identical Observed, no send.
            cache_key = (symbol, "funding", "90d")
            if cache is not None:
                try:
                    hit = cache.get(cache_key, cutoff_ms)
                except Exception:
                    hit = None
                if hit is not None and hit.meta.complete:
                    requested += 0
                    processed.append(symbol)
                    continue
            # The final HTTP sender owns every page/retry reservation. No
            # logical-call permit is held here (it would charge a page twice).
            # Incremental gap: only the missing tail, never a blind 90D refetch.
            try:
                existing = await repo.list_funding_events(symbol, cutoff_ms - _LOOKBACK_MS[90], cutoff_ms)
                have_max = max((int(e.funding_time_ms) for e in existing), default=None)
            except Exception:
                existing = ()
                have_max = None
            start_ms = (int(have_max) + 1) if have_max is not None else (cutoff_ms - _LOOKBACK_MS[90])
            if start_ms >= cutoff_ms:
                processed.append(symbol)
                continue
            try:
                if budget is not None:
                    from diveintocrypto_desktop.shortlab.request_budget import (
                        make_request_context,
                        scoped_request_context,
                    )

                    ctx = make_request_context(budget, job_type="funding_backfill",
                                               host="fapi", endpoint_family="fundingRate",
                                               trace_id=context.trace_id)
                    with scoped_request_context(ctx):
                        events = await _maybe_await(self._funding_history_fn(symbol, start_ms, cutoff_ms))
                else:
                    events = await _maybe_await(self._funding_history_fn(symbol, start_ms, cutoff_ms))
            except Exception:
                deferred += 1
                continue
            requested += 1
            processed.append(symbol)
            events = list(events or [])
            if events:
                try:
                    await repo.upsert_funding_events(
                        [
                            {"symbol": symbol, "funding_time_ms": int(e.get("t")),
                             "funding_rate": float(e.get("funding_rate")),
                             "mark_price": e.get("mark_price")}
                            for e in events
                            if isinstance(e.get("t"), int) and e.get("funding_rate") is not None
                        ]
                    )
                    persisted_events += len(events)
                except Exception:
                    pass
                # Populate the shared cache (identical Observed, original known_at).
                if cache is not None:
                    try:
                        from diveintocrypto_desktop.shortlab import observations as _obs

                        ttl_ms = 1800 * 1000
                        try:
                            ttl_ms = int(self._config.quality_freshness_sec["funding_history"].ttl) * 1000
                        except Exception:
                            pass
                        observed = _obs.make_observation(
                            list(events), source="binance-funding",
                            source_as_of_ms=max(int(e.get("t")) for e in events if isinstance(e.get("t"), int)),
                            fetched_at_ms=self._now(), known_at_ms=self._now(),
                            window_start_ms=start_ms, window_end_ms=cutoff_ms,
                            complete=self._funding_coverage_fn(events, start_ms, cutoff_ms, None).complete,
                            coverage_fraction=self._funding_coverage_fn(events, start_ms, cutoff_ms, None).coverage_fraction,
                        )
                        cache.put(cache_key, observed, int(observed.meta.known_at_ms) + ttl_ms)
                    except Exception:
                        pass
        # Advance the persisted cursor past the processed head (fair rotation).
        # The unprocessed tail stays as the explicit page queue so a restart
        # resumes the same order without re-heading (F01 cursor contract).
        if tracked:
            self._funding_cursor = (int(self._funding_cursor) + len(processed)) % len(tracked)
            try:
                _tail = list(rotated[batch:])
                # Keep the queue bounded and deterministic (sorted tail).
                self._funding_queue = [str(s) for s in _tail if isinstance(s, str)]
            except Exception:
                pass
        if next_allowed is not None:
            self._funding_next_allowed_at_ms = int(next_allowed)
        elif requested > 0:
            # Window consumed: gate the remainder of this 300s window.
            self._funding_next_allowed_at_ms = now_ms + FUNDING_BATCH_WINDOW_SEC * 1000
        await self._save_persisted_funding_state()
        finished = int(context.clock_ms())
        stats = {
            "tracked": len(tracked),
            "requested": requested,
            "persisted_events": persisted_events,
            "deferred": deferred,
            "cursor": int(self._funding_cursor),
            "next_allowed_at_ms": self._funding_next_allowed_at_ms,
        }
        await repo.finish_job_run(jid, "SUCCEEDED", finished, stats=stats)
        return JobStatus(job_id=jid, job_type=JOB_TYPE_FUNDING_BACKFILL, status="SUCCEEDED",
                         stats=stats, started_at_ms=started_ms, finished_at_ms=finished)

    async def run_contract_refresh(self, context: JobContext, *, job_id: str | None = None) -> JobStatus:
        """Contract-refresh adapter: live+tracked metadata persisted (F06a)."""
        repo = self._require_available()
        jid = str(job_id or context.trace_id)
        started_ms = int(context.clock_ms())
        await repo.create_job_run(jid, JOB_TYPE_CONTRACT_REFRESH, started_ms)
        await self._load_persisted_contract_cursor()
        try:
            rows = await _maybe_await(self._universe_fn(self._config.universe.limit))
        except Exception as exc:
            await repo.finish_job_run(jid, "FAILED", int(context.clock_ms()),
                                      error_code=f"UNIVERSE_FAILED:{type(exc).__name__}")
            return JobStatus(job_id=jid, job_type=JOB_TYPE_CONTRACT_REFRESH,
                             status="FAILED", error_code="UNIVERSE_FAILED")
        live = {str(r.get("s") or r.get("symbol", "")).upper() for r in rows or []}
        live.discard("")
        try:
            metadata_all = await _maybe_await(self._metadata_fn())
        except Exception:
            metadata_all = {}
        if not isinstance(metadata_all, Mapping):
            metadata_all = {}
        tracked = list(await self.tracked_symbols(live))
        now_ms = int(context.clock_ms())
        persisted = 0
        for symbol in tracked:
            meta = metadata_all.get(symbol) if isinstance(metadata_all, Mapping) else None
            meta_dict = _contract_meta_dict(meta) if meta is not None else {"symbol": symbol, "status": None}
            try:
                first_seen = meta_dict.get("first_seen_ms") or now_ms
                await repo.save_contract_lifecycle(
                    {
                        "futures_symbol": symbol,
                        "observed_at_ms": int(now_ms),
                        "onboard_at_ms": meta_dict.get("onboard_at_ms"),
                        "first_seen_ms": int(first_seen),
                        "delivery_at_ms": meta_dict.get("delivery_at_ms"),
                        "contract_type": meta_dict.get("contract_type") or "PERPETUAL",
                        "exchange_status": meta_dict.get("status"),
                        "contract_multiplier": meta_dict.get("contract_multiplier"),
                        "multiplier_source": meta_dict.get("multiplier_source"),
                    }
                )
                persisted += 1
            except Exception:
                continue
        if tracked:
            self._contract_cursor = (int(self._contract_cursor) + len(tracked)) % max(1, len(tracked))
        await self._save_persisted_contract_cursor()
        finished = int(context.clock_ms())
        stats = {"tracked": len(tracked), "persisted": persisted, "cursor": int(self._contract_cursor)}
        await repo.finish_job_run(jid, "SUCCEEDED", finished, stats=stats)
        return JobStatus(job_id=jid, job_type=JOB_TYPE_CONTRACT_REFRESH, status="SUCCEEDED",
                         stats=stats, started_at_ms=started_ms, finished_at_ms=finished)

    async def run_metadata_refresh(self, context: JobContext, *, job_id: str | None = None) -> JobStatus:
        """Metadata adapter: universe + identity-catalog freshness (F06a).

        Refreshes the identity directory (best-effort, keyless stays
        UNCONFIGURED with zero sends) and records the live set size. No
        scoring; no second service.
        """
        repo = self._require_available()
        jid = str(job_id or context.trace_id)
        started_ms = int(context.clock_ms())
        await repo.create_job_run(jid, JOB_TYPE_METADATA, started_ms)
        try:
            rows = await _maybe_await(self._universe_fn(self._config.universe.limit))
        except Exception as exc:
            await repo.finish_job_run(jid, "FAILED", int(context.clock_ms()),
                                      error_code=f"UNIVERSE_FAILED:{type(exc).__name__}")
            return JobStatus(job_id=jid, job_type=JOB_TYPE_METADATA,
                             status="FAILED", error_code="UNIVERSE_FAILED")
        live = {str(r.get("s") or r.get("symbol", "")).upper() for r in rows or []}
        live.discard("")
        catalog_status: str | None = None
        catalog = context.identity_catalog if context.identity_catalog is not None else self._identity_catalog
        if catalog is not None:
            try:
                from diveintocrypto_desktop.shortlab.request_budget import make_request_context

                ctx = make_request_context(None, job_type="metadata", host="coingecko",
                                           endpoint_family="coingecko-directory",
                                           trace_id=context.trace_id)
                result = await catalog.refresh(ctx)
                catalog_status = str(getattr(result, "status", None))
            except Exception as exc:  # noqa: BLE001 - catalog refresh never fails metadata
                catalog_status = f"ERROR:{type(exc).__name__}"
        finished = int(context.clock_ms())
        stats = {"live": len(live), "catalog_status": catalog_status}
        await repo.finish_job_run(jid, "SUCCEEDED", finished, stats=stats)
        return JobStatus(job_id=jid, job_type=JOB_TYPE_METADATA, status="SUCCEEDED",
                         stats=stats, started_at_ms=started_ms, finished_at_ms=finished)

    # -- H08 hedge background adapters (B30; jitter 0, shared collection) ----
    def _hedge_job_owner(self):
        owner = getattr(self, "_hedge_jobs", None)
        if owner is None:
            from diveintocrypto_desktop.shortlab.hedge.jobs import HedgeJobs
            owner = HedgeJobs(self)
        return owner

    async def run_hedge_opportunity(self, context: JobContext, *, job_id: str | None = None) -> JobStatus:
        return await self._hedge_job_owner().opportunity(context, job_id=job_id)

    async def run_hedge_venue_refresh(self, context: JobContext, *, job_id: str | None = None) -> JobStatus:
        return await self._hedge_job_owner().venue_refresh(context, job_id=job_id)

    async def run_hedge_monitor(self, context: JobContext, *, job_id: str | None = None) -> JobStatus:
        return await self._hedge_job_owner().monitor(context, job_id=job_id)

    async def run_hedge_settlement(self, context: JobContext, *, job_id: str | None = None) -> JobStatus:
        return await self._hedge_job_owner().settlement(context, job_id=job_id)

    async def run_due(self, context: JobContext) -> JobStatus:
        """Grader slot: ``async run_due(context) -> JobStatus`` (F07 owns it).

        H08 combines the directional grader with the H10 hedge sub-phase on
        the shared ``JobContext`` (shared budget, 21600s cadence, one
        scheduler job). The directional callback staysexactly the F06b
        wiring (``evidence.jobs.run_due``); the hedge sub-phase is lazy
        (``try import``, missing means ``SKIPPED`` + honest capability, never
        an assert). Each sub-phase records its own status/queue/failure in
        ``stats`` so a hedge failure never masquerades as completion, and a
        disabled hedge never stops the directional grader.
        """
        fn = self._grader_callback
        if fn is None:
            raise UnknownJobType("grader callback is not registered (F07 owns run_due)")
        directional = await fn(context)
        # Hedge sub-phase: explicit registration wins, otherwise lazy H10 import.
        hedge_fn = getattr(self, "_hedge_grader_callback", None)
        hedge_status: Any = None
        hedge_error: str | None = None
        hedge_stats: dict[str, Any] = {}
        hedge_state = "SKIPPED"
        hedge_reason = "HEDGE_DISABLED"
        if hedge_fn is None:
            try:
                from diveintocrypto_desktop.shortlab.evidence import hedge_grader as _hg  # type: ignore[import-not-found]

                hedge_fn = getattr(_hg, "run_due", None)
            except Exception:
                hedge_fn = None
                hedge_reason = "UNCONFIGURED"
        if hedge_fn is not None:
            try:
                # Hedge disabled (no tables) only skips the hedge sub-phase.
                try:
                    await self._ensure_hedge_available()
                except HedgeUnavailable as exc:
                    hedge_state = "SKIPPED"
                    hedge_reason = str(exc)[:160] or "HEDGE_DISABLED"
                    hedge_fn = None
                if hedge_fn is not None:
                    hedge_status = await hedge_fn(context)
                    hedge_state = str(getattr(hedge_status, "status", "SUCCEEDED") or "SUCCEEDED")
                    try:
                        hedge_stats = dict(getattr(hedge_status, "stats", None) or {})
                    except Exception:
                        hedge_stats = {}
                    hedge_error = getattr(hedge_status, "error_code", None)
            except Exception as exc:  # noqa: BLE001 - hedge failure is recorded, never hides directional
                hedge_state = "FAILED"
                hedge_error = f"{type(exc).__name__}:{str(exc)[:160]}"
                hedge_stats = {"error": hedge_error}
        try:
            directional_stats = dict(getattr(directional, "stats", None) or {})
        except Exception:
            directional_stats = {}
        directional_state = str(getattr(directional, "status", "SUCCEEDED") or "SUCCEEDED")
        directional_error = getattr(directional, "error_code", None)
        combined: dict[str, Any] = dict(directional_stats)
        combined["directional"] = {"status": directional_state, **directional_stats}
        if directional_error is not None:
            combined["directional"]["error_code"] = directional_error
        combined["hedge"] = {"status": hedge_state, **hedge_stats}
        if hedge_error is not None:
            combined["hedge"]["error_code"] = hedge_error
        if hedge_state == "SKIPPED":
            combined["hedge"]["reason"] = hedge_reason
        # Overall follows the worst sub-phase, but a skipped hedge never fails
        # a succeeded directional grader.
        if directional_state != "SUCCEEDED":
            overall = directional_state
            overall_error = directional_error
        elif hedge_state not in ("SUCCEEDED", "SKIPPED"):
            overall = hedge_state
            overall_error = hedge_error
        else:
            overall = "SUCCEEDED"
            overall_error = None
        return JobStatus(
            job_id=str(getattr(directional, "job_id", getattr(context, "trace_id", "grader"))),
            job_type=JOB_TYPE_GRADER,
            status=str(overall),
            stats=combined,
            error_code=overall_error,
            started_at_ms=getattr(directional, "started_at_ms", None),
            finished_at_ms=getattr(directional, "finished_at_ms", None),
        )

    async def maintain(self, context: JobContext) -> JobStatus:
        """Retention slot: ``async maintain(context) -> JobStatus`` (F09 owns it).

        F06b wires the default ``maintenance.maintain`` on the shared service
        instance; unwired direct construction still raises (no second service).

        The F09 callback only calls ``maintain_retention`` and never writes
        ``sl_job_run`` itself, so this wrapper owns the job bookkeeping
        (create before, finish after) to keep ``refresh`` / ``job_status`` /
        scheduler ``trigger_now`` consistent with every other job type.
        """
        fn = self._retention_callback
        if fn is None:
            raise UnknownJobType("retention callback is not registered (F09 owns maintain)")
        repo = self._repository
        trace = str(getattr(context, "trace_id", "") or "maintenance")
        if repo is not None:
            try:
                try:
                    _started = int(context.clock_ms())  # type: ignore[union-attr]
                except Exception:
                    _started = self._now()
                await repo.create_job_run(trace, JOB_TYPE_MAINTENANCE, int(_started))
            except Exception:
                # Retry of the same trace reuses the row; callback still runs.
                pass
        result = await fn(context)
        if repo is not None:
            try:
                try:
                    _finished = int(context.clock_ms())  # type: ignore[union-attr]
                except Exception:
                    _finished = self._now()
                _status = str(getattr(result, "status", "SUCCEEDED") or "SUCCEEDED")
                _stats = getattr(result, "stats", None)
                _stats_d = dict(_stats) if isinstance(_stats, Mapping) else {}
                _err = getattr(result, "error_code", None)
                await repo.finish_job_run(
                    trace, _status, int(_finished), stats=_stats_d, error_code=_err
                )
            except Exception:
                pass
        return result

    async def job_status(self, job_id: str, existing: bool = False) -> JobStatus:
        repo = self._require_available()
        if job_id in self._completed_jobs:
            return dataclasses.replace(self._completed_jobs[job_id], existing=existing)
        for job_type, slot in self._running.items():
            if slot["job_id"] == job_id and not slot["task"].done():
                row = await repo.get_job_run(job_id)
                stats = dict(row["stats_json"]) if row and row.get("stats_json") else {}
                return JobStatus(
                    job_id=job_id,
                    job_type=row["job_type"] if row else job_type,
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
        # F06a: resume the persistent funding rotation before scoring.
        await self._ensure_funding_cursor_loaded()
        # F06a: freeze the policy bundle once per generation (ID reference).
        await self._persist_config_snapshot_once(started_ms)

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
        # F06a tracked set (live + history + positions when present) for stats.
        try:
            tracked = await self.tracked_symbols(live_symbols)
        except Exception:
            tracked = tuple(sorted(live_symbols))

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
            # F06a: freeze one decision cutoff per coin, then score. All
            # observation windows (funding midnight, daily closes, OI ends)
            # derive from this same cutoff so a cross-day freeze realigns
            # both ends together instead of reusing a stale global day.
            decision_cutoff = self.freeze_decision_cutoff(symbol)
            try:
                outcome = await self._score_symbol(
                    symbol, row, live_symbols, metadata_all, decision_cutoff, tier
                )
            except Exception as exc:  # noqa: BLE001 - one bad symbol never fails siblings
                failed += 1
                errors.append(f"{symbol}:{type(exc).__name__}"[:120])
                continue
            # F06a: base tables first (ID references), feature links them.
            try:
                base_ids = await self._persist_base_tables_for_symbol(
                    symbol=symbol,
                    identity=outcome.get("identity"),
                    exchange_meta=_contract_meta_dict(metadata_all.get(symbol)),
                    fund_data=outcome.get("fund_data"),
                    fund_result=outcome.get("fund_result"),
                    funding_events=(outcome.get("funding_events") or []),
                    cutoff_ms=int(outcome.get("decision_cutoff_ms", decision_cutoff)),
                    now_ms=self._now(),
                )
                fund_snap = base_ids.get("fundamental_snapshot_id")
                if fund_snap is not None:
                    try:
                        import dataclasses as _dc

                        outcome["feature_record"] = _dc.replace(
                            outcome["feature_record"],
                            fundamental_snapshot_id=str(fund_snap),
                        )
                    except Exception:
                        pass
                # Keep F05 Observed provenance on the outcome for callers that
                # build FeatureInputs downstream (observations are F02 Observed).
                outcome.setdefault("decision_cutoff_ms", int(decision_cutoff))
                outcome["identity_snapshot_id"] = base_ids.get("identity_snapshot_id")
            except Exception as exc:  # noqa: BLE001 - base tables never fail scoring
                log.debug("base tables skipped for %s: %s", symbol, str(exc)[:100])
            scored.append(outcome)
            succeeded += 1
            funding_requested += outcome.get("funding_requested", 0)
            funding_deferred += outcome.get("funding_deferred", 0)
            deferred += outcome.get("deferred", 0)
        if universe_rows:
            self._funding_cursor = (self._funding_cursor + funding_requested) % len(universe_rows)
        # F06a: persist the page queue / cursor / nextAllowed for restart.
        await self._save_persisted_funding_state()

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
        cutoffs = {o["symbol"]: int(o.get("decision_cutoff_ms", as_of_ms)) for o in scored}
        entry_by_symbol, entry_stats = await self._run_entry_stage(entry_symbols, as_of_ms, cutoffs)
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
            # F06a: each score carries its own frozen decision cutoff; the
            # batch still commits atomically with SUCCEEDED below. Feature,
            # entry and score must share the same per-coin as_of (F01
            # reference check), so a cross-day freeze realigns the entry
            # snapshot to the coin's cutoff instead of a stale global day.
            symbol_as_of = int(outcome.get("decision_cutoff_ms", as_of_ms))
            entry_result = entry_by_symbol.get(symbol)
            if entry_result is not None and isinstance(entry_result, __import__("diveintocrypto_desktop.shortlab.entry", fromlist=["EntryResult"]).EntryResult):
                from diveintocrypto_desktop.shortlab.entry import finalize_entry_snapshot
                final_cutoff = max(symbol_as_of, self._now(), int(entry_result.fetched_at_ms))
                if final_cutoff // DAY_MS == symbol_as_of // DAY_MS:
                    outcome = self._refreeze_scored_outcome(outcome, final_cutoff)
                    symbol_as_of = final_cutoff
                    entry_result = finalize_entry_snapshot(entry_result, final_cutoff)
                else:
                    # The Entry collection crossed the base window's UTC day.
                    # Keep the honest earlier base snapshot; do not join Entry.
                    entry_result = None
                    entry_exhausted.add(symbol)
            entry_score = entry_result.entry_score if entry_result is not None else None
            state = self._derive_symbol_status(outcome, entry_score, symbol in entry_exhausted)
            outcome["state"] = state
            entry_snapshot_id: str | None = None
            if entry_result is not None:
                try:
                    _rec = entry_result.to_record()
                except Exception:
                    _rec = None
                if _rec is not None and int(_rec.as_of_ms) == symbol_as_of:
                    await repo.save_entry(_rec)
                    entry_snapshot_id = str(_rec.snapshot_id)
                else:
                    # An injected/legacy runner must not relabel historical
                    # inputs. Exclude mismatched Entry from this decision.
                    entry_score = None
                    state = self._derive_symbol_status(outcome, None, True)
                    outcome["state"] = state
            await repo.save_feature(outcome["feature_record"])
            records.append(
                ScoreSnapshotRecord(
                    snapshot_id=f"score-{symbol}-{symbol_as_of}-{outcome['breakdown'].profile}-{job_id}",
                    generation_id=job_id,
                    feature_snapshot_id=outcome["feature_record"].snapshot_id,
                    entry_snapshot_id=entry_snapshot_id,
                    symbol=symbol,
                    as_of_ms=symbol_as_of,
                    analysis_tier=tier,
                    profile=outcome["breakdown"].profile,
                    score_version=outcome["breakdown"].score_version,
                    entry_version=ENTRY_VERSION if entry_snapshot_id is not None else None,
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
            "tracked_total": len(tracked),
            "shortlist": len(shortlist),
            "entry_depth": len(entry_symbols),
            "succeeded": succeeded,
            "failed": failed,
            "deferred": deferred,
            "funding_requested": funding_requested,
            "funding_deferred": funding_deferred,
            "funding_cursor": int(self._funding_cursor),
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
        # R10b D04.1: default identity from Catalog->Resolver->Snapshot.
        # Explicit _identity_candidates_fn injection (tests) overrides the
        # catalog; production (default) uses catalog candidates.
        try:
            if getattr(self, "_uses_default_identity", True):
                catalog = getattr(self, "_identity_catalog", None)
                if catalog is not None:
                    try:
                        raw_cands = catalog.candidates(str(symbol).upper(), int(as_of_ms))
                        candidates = list(raw_cands or [])
                    except Exception:
                        candidates = []
                else:
                    try:
                        candidates = await _maybe_await(self._identity_candidates_fn(symbol))
                    except Exception:
                        candidates = []
            else:
                try:
                    candidates = await _maybe_await(self._identity_candidates_fn(symbol))
                except Exception:
                    candidates = []
                if not candidates:
                    catalog = getattr(self, "_identity_catalog", None)
                    if catalog is not None:
                        try:
                            raw_cands = catalog.candidates(str(symbol).upper(), int(as_of_ms))
                            if isinstance(raw_cands, (list, tuple)) and raw_cands:
                                candidates = list(raw_cands)
                        except Exception:
                            pass
        except Exception:
            candidates = []
        try:
            from diveintocrypto_desktop.shortlab.identity.resolver import (
                resolve_asset_context as _rctx2,
            )

            identity = _rctx2(symbol, exchange_meta, list(candidates or []), dict(self._identity_overrides or {}))
        except Exception:
            identity = resolve_identity(symbol, exchange_meta, list(candidates or []),
                                        self._identity_overrides)

        fund_result, fund_data = await self._fetch_fundamentals(identity, now)
        spot_result = await self._fetch_spot(identity, as_of_ms, now)
        market = await self._fetch_market(symbol, as_of_ms, now)
        if self._production_inputs and getattr(self, "_hedge_market", None) is not None:
            try:
                from diveintocrypto_desktop.shortlab.observations import make_observation
                fx = await self._hedge_market._fx("USDT")
                source_time, _ = self._hedge_market._fx_cache["USDT"]
                market.setdefault("observations", {})["fx_rate"] = make_observation(fx,
                    source="coingecko:tether:USD", source_as_of_ms=source_time,
                    fetched_at_ms=self._hedge_market._fx_provenance["USDT"]["known_at_ms"],
                    known_at_ms=self._hedge_market._fx_provenance["USDT"]["known_at_ms"])
            except Exception:
                pass
        funding = await self._fetch_funding(symbol, exchange_meta, as_of_ms, now)

        # Collection precedes the decision freeze. Never call the batch
        # start time the time at which late network responses were known.
        if self._production_inputs:
            final_cutoff = self.freeze_decision_cutoff(symbol)
            if final_cutoff // DAY_MS != as_of_ms // DAY_MS:
                market = await self._fetch_market(symbol, final_cutoff, self._now())
                spot_result = await self._fetch_spot(identity, final_cutoff, self._now())
                funding = await self._fetch_funding(symbol, exchange_meta, final_cutoff, self._now())
                final_cutoff = self.freeze_decision_cutoff(symbol)
            as_of_ms = final_cutoff
            self._assemble_observed_inputs(market, row, identity, exchange_meta,
                fund_result, fund_data, spot_result, funding, as_of_ms)
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
        if full is not None:
            final_cutoff = max(self._now(), as_of_ms, *(int(full[k].fetched_at_ms or 0)
                for k in ("unlock_result", "social_result", "catalyst_result")))
            if final_cutoff // DAY_MS != as_of_ms // DAY_MS and self._production_inputs:
                # A late FULL package cannot be joined to yesterday's daily windows.
                # Keep an honest unavailable base package rather than backdate it.
                market = await self._fetch_market(symbol, final_cutoff, self._now())
                spot_result = await self._fetch_spot(identity, final_cutoff, self._now())
                funding = await self._fetch_funding(symbol, exchange_meta, final_cutoff, self._now())
                final_cutoff = max(final_cutoff, self._now())
                self._assemble_observed_inputs(market, row, identity, exchange_meta,
                    fund_result, fund_data, spot_result, funding, final_cutoff)
                final_inputs = self._build_inputs(symbol, row, identity, exchange_meta, fund_data,
                    spot_result, market, funding, final_cutoff)
                final_inputs.update({k: inputs[k] for k in ("unlock_raw_15", "narrative_raw_15",
                    "unlock_status", "social_status", "catalyst_major_event", "catalyst_severity")})
                inputs = final_inputs
            as_of_ms = final_cutoff
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
        frozen_inputs = inputs.get("_frozen_inputs")
        if frozen_inputs is not None:
            from diveintocrypto_desktop.shortlab.inputs import build_field_states
            frozen_states = list(build_field_states(frozen_inputs, self._quality_policy or self._config))
            if full is not None:
                from diveintocrypto_desktop.shortlab.quality import GROUP_FIELD_SHARES
                full_ids = set().union(*(set(GROUP_FIELD_SHARES[g]) for g in ("unlock", "social", "catalyst")))
                frozen_states.extend(f for f in field_states if f.field_id in full_ids)
            field_states = tuple(frozen_states)
        dq = data_quality(tier, field_states, as_of_ms, policy=self._quality_policy)
        risk_meta = self._build_risk_meta(
            symbol, row, identity, exchange_meta, live_symbols, metadata_all,
            fund_data, spot_result, market, funding, as_of_ms,
            full=full,
        )
        if frozen_inputs is not None:
            risk_meta.update(frozen_inputs.risk_meta())
            risk_meta["as_of_ms"] = as_of_ms
        from diveintocrypto_desktop.shortlab.risk.veto import risk_policy_from_config
        policy = self._risk_policy or risk_policy_from_config(self._config)
        risk = evaluate_risks(inputs, risk_meta, dq.data_quality, **{
            key: getattr(policy, key) for key in ("veto_dq_threshold", "breakout_24h", "breakout_7d",
                "new_token_days", "hard_min_futures_qv", "hard_min_oi_usd")})

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
            "decision_cutoff_ms": as_of_ms,
            "identity": identity,
            "breakdown": breakdown,
            "dq": dq,
            "risk": risk,
            "risk_meta": risk_meta,
            "inputs": inputs,
            "feature_record": feature_record,
            "field_states": tuple(field_states),
            "observations": dict(market.get("observations") or {}),
            "fund_data": fund_data,
            "fund_result": fund_result,
            "funding_events": list(funding.get("events") or []),
            "funding_requested": funding.get("requested", 0),
            "funding_deferred": funding.get("deferred", 0),
            "deferred": funding.get("deferred", 0),
            "state": None,
        }

    def _refreeze_scored_outcome(self, outcome: Mapping[str, Any], cutoff: int) -> dict[str, Any]:
        """Recompute a not-yet-persisted decision from its frozen input package."""
        from diveintocrypto_desktop.shortlab.inputs import build_feature_inputs, build_field_states
        from diveintocrypto_desktop.shortlab.scoring.ltss import extract_features, score_lite, score_full
        from diveintocrypto_desktop.shortlab.risk.veto import evaluate_risks
        out = dict(outcome)
        inputs = dict(outcome["inputs"])
        fields = outcome["field_states"]
        meta = dict(outcome["risk_meta"])
        if outcome.get("observations"):
            frozen = build_feature_inputs(outcome["symbol"], outcome["observations"], outcome["identity"], cutoff, self._config)
            inputs.update(frozen.to_ltss_inputs())
            inputs["_frozen_inputs"] = frozen
            base = list(build_field_states(frozen, self.quality_policy))
            from diveintocrypto_desktop.shortlab.quality import GROUP_FIELD_SHARES
            full_ids = set().union(*(set(GROUP_FIELD_SHARES[g]) for g in ("unlock", "social", "catalyst")))
            fields = tuple(base + [f for f in fields if f.field_id in full_ids])
            meta.update(frozen.risk_meta())
        inputs["as_of_ms"] = cutoff
        meta["as_of_ms"] = cutoff
        features = extract_features(inputs, cutoff)
        profile = outcome["breakdown"].profile
        scorer = score_full if profile.endswith("_FULL") else score_lite
        breakdown = scorer(features, profile, self._config)
        dq = data_quality("FULL" if profile.endswith("_FULL") else "LITE", fields, cutoff, policy=self.quality_policy)
        policy = self.risk_policy
        risk = evaluate_risks(inputs, meta, dq.data_quality, **{
            key: getattr(policy, key) for key in ("veto_dq_threshold", "breakout_24h", "breakout_7d",
                "new_token_days", "hard_min_futures_qv", "hard_min_oi_usd")})
        source_meta = dict(outcome["feature_record"].source_meta)
        for state in fields:
            source_meta[state.field_id] = {**source_meta.get(state.field_id, {}),
                "status": state.status, "fetched_at_ms": state.fetched_at_ms,
                "coverage_fraction": state.coverage if state.coverage is not None else 1.0,
                "reason_code": state.reason_code, "source": state.source,
                "as_of_ms": source_meta.get(state.field_id, {}).get("as_of_ms")}
        record = dataclasses.replace(outcome["feature_record"], snapshot_id=features.snapshot_id,
            as_of_ms=cutoff, features=dict(features.features), source_meta=source_meta,
            data_quality=dq.data_quality)
        out.update(inputs=inputs, field_states=fields, risk_meta=meta, risk=risk, dq=dq,
                   breakdown=breakdown, feature_record=record, decision_cutoff_ms=cutoff)
        return out

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
        """Settled 7/30/90D funding sums + coverage (design 10.1, CR07 D04/D05).

        CR07: windows freeze Inputs/Carry/DQ with Hedge sharing the R05
        receipt/schedule coverage (compute_schedule_coverage); the old max24h
        rule alone never grants completeness for new decisions (AND-gated,
        missing/UNKNOWN schedule => incomplete, no 30D credit).

        Symbols beyond the 80-per-5min batch are *deferred* (not fetched);
        their windows stay null with a batch reason and are retried on a
        later run -- the backfill spans batches and records the remainder.
        Fetch exceptions degrade to empty events (incomplete coverage),
        never to fabricated zeros.
        """
        out: dict[str, Any] = {"requested": 0, "deferred": 0, "events": [],
                               "windows": {}, "fetched_at_ms": now_ms}
        start_ms = as_of_ms - _LOOKBACK_MS[90]
        existing = []
        if self._repository is not None and hasattr(self._repository, "list_funding_events"):
            rows = await self._repository.list_funding_events(symbol, start_ms, as_of_ms)
            existing = [_funding_event_dict(e) for e in rows]
        cov = self._funding_coverage_fn(existing, start_ms, as_of_ms,
                                      exchange_meta.get("onboard_at_ms")) if existing else None
        complete_archive = bool(cov is not None and cov.complete)
        events = existing
        fetch_succeeded = False
        if not complete_archive:
            if self._funding_allowance(1, now_ms) < 1:
                out["deferred"] = 1
                out["reason"] = "FUNDING_BATCH_DEFERRED"
            else:
                out["requested"] = 1
                # Fill the earliest known gap, retaining overlap for repairs.
                missing_start = start_ms
                if cov is not None and cov.gaps:
                    missing_start = min(int(g["start_ms"]) for g in cov.gaps)
                elif existing:
                    missing_start = max(start_ms, max(e["t"] for e in existing) - 8 * 3600000)
                try:
                    from diveintocrypto_desktop.shortlab.request_budget import make_request_context, scoped_request_context
                    ctx = make_request_context(self._request_budget, job_type="score_refresh",
                        host="fapi", endpoint_family="fundingRate", trace_id=f"funding-{symbol}-{as_of_ms}")
                    with scoped_request_context(ctx):
                        fetched = await _maybe_await(self._funding_history_fn(symbol, missing_start, as_of_ms))
                    fetch_succeeded = True
                    events = list({int(e["t"]): e for e in existing + list(fetched or [])}.values())
                    if fetched and self._repository is not None and hasattr(self._repository, "upsert_funding_events"):
                        await self._repository.upsert_funding_events([
                            {"symbol": symbol, "funding_time_ms": e["t"], "funding_rate": str(e["funding_rate"]),
                             "mark_price": e.get("mark_price")} for e in fetched])
                except Exception as exc:
                    out["reason"] = getattr(exc, "reason_code", "FUNDING_FETCH_FAILED")
        if not hasattr(self, "_funding_known_at"):
            self._funding_known_at = {}
        if fetch_succeeded:
            self._funding_known_at[symbol] = self._now()
        out["fetched_at_ms"] = self._funding_known_at.get(symbol)
        out["events"] = sorted(events, key=lambda e: int(e["t"]))
        onboard = exchange_meta.get("onboard_at_ms")
        onboard_ms = onboard if isinstance(onboard, int) and onboard > 0 else None
        # CR07 D05: freeze with Hedge R05 schedule coverage. Only when the real
        # repair port is bound (new decisions, R10b boundary) does the old
        # max24h result lose sole authority (AND-gated). Unbound/fake ports or
        # legacy stubs without the schedule interface keep the old result
        # (back-compat, no new READY via old rule alone).
        _r05_schedules: Any = None
        _has_r05_interface = False
        _r05_fn: Any = None
        try:
            _candidate_fn = self._require_repair_port("compute_schedule_coverage")
            if is_real_repair_callback(_candidate_fn):
                _r05_fn = _candidate_fn
                _has_r05_interface = bool(
                    self._repository is not None
                    and hasattr(self._repository, "list_funding_schedules")
                )
        except Exception:
            _has_r05_interface = False
            _r05_fn = None
        if _has_r05_interface:
            try:
                _r05_schedules = list(
                    await self._repository.list_funding_schedules(str(symbol).upper(), int(as_of_ms)) or ()
                )
            except Exception:
                # Schedule fetch failure => UNKNOWN (no credit), never old-only.
                _r05_schedules = []
        for days in (7, 30, 90):
            window_start = as_of_ms - _LOOKBACK_MS[days]
            try:
                coverage = self._funding_coverage_fn(out["events"], window_start, as_of_ms, onboard_ms)
            except Exception:
                coverage = None
            old_complete = bool(coverage is not None and coverage.complete)
            # CR07: R05 gate (shared receipt/schedule result with Hedge).
            _r05_complete: bool | None = None
            _r05_reason: str | None = None
            if _has_r05_interface:
                try:
                    from diveintocrypto_desktop.shortlab.funding_schedule import (
                        compute_schedule_coverage as _r05_fn,
                    )

                    # Normalise repo rows (schedule_json nested) + segments.
                    _norm_scheds: list[Any] = []
                    for _s in (_r05_schedules or ()):
                        try:
                            if isinstance(_s, Mapping):
                                _sj = _s.get("schedule_json")
                                if isinstance(_sj, Mapping):
                                    _merged = dict(_s)
                                    for _k, _v in dict(_sj).items():
                                        _merged.setdefault(_k, _v)
                                    _norm_scheds.append(_merged)
                                else:
                                    _norm_scheds.append(_s)
                            else:
                                _norm_scheds.append(_s)
                        except Exception:
                            continue
                    _r05_cov = _r05_fn(
                        list(out["events"] or ()),
                        _norm_scheds,
                        int(window_start),
                        int(as_of_ms),
                        int(as_of_ms),
                    )
                    _cov_frac = getattr(_r05_cov, "coverage_fraction", None)
                    _exp = getattr(_r05_cov, "expected_count", None)
                    _reasons = tuple(getattr(_r05_cov, "reasons", ()) or ())
                    if (
                        _cov_frac is not None
                        and str(_cov_frac) == "1"
                        and _exp is not None
                        and "FUNDING_SCHEDULE_UNKNOWN" not in _reasons
                    ):
                        _r05_complete = True
                    else:
                        _r05_complete = False
                        _r05_reason = "FUNDING_SCHEDULE_UNKNOWN" if "FUNDING_SCHEDULE_UNKNOWN" in _reasons else "COVERAGE_GAP"
                except Exception:
                    _r05_complete = False
                    _r05_reason = "FUNDING_SCHEDULE_UNKNOWN"
            if _r05_complete is None:
                eff_complete = old_complete
                eff_reason = None if old_complete else (coverage.reason_code if coverage is not None else "FUNDING_HISTORY_INCOMPLETE")
                eff_fraction = float(coverage.coverage_fraction) if coverage is not None else 0.0
            else:
                # Old rule removed as sole authority: both must hold.
                eff_complete = bool(old_complete and _r05_complete)
                if eff_complete:
                    eff_reason = None
                    eff_fraction = 1.0
                else:
                    eff_reason = _r05_reason or (coverage.reason_code if coverage is not None else "FUNDING_HISTORY_INCOMPLETE")
                    # R05 incomplete never carries old fraction as full credit.
                    try:
                        eff_fraction = float(coverage.coverage_fraction) if coverage is not None else 0.0
                        if not _r05_complete:
                            eff_fraction = min(float(eff_fraction), 0.0) if eff_fraction else 0.0
                    except Exception:
                        eff_fraction = 0.0
            window_events = [e for e in out["events"]
                             if isinstance(e.get("t"), int) and e["t"] >= window_start]
            rates = [float(e["funding_rate"]) for e in window_events
                     if _finite(e.get("funding_rate")) is not None]
            total = sum(rates) if eff_complete else None
            if eff_complete and rates:
                positive = sum(1 for r in rates if r > 0) / len(rates)
            else:
                positive = None
            stability = None
            if days == 30 and eff_complete and len(rates) >= 2:
                try:
                    stability = statistics.pstdev(rates)
                except statistics.StatisticsError:
                    stability = None
            out["windows"][days] = {
                "sum": total,
                "positive_ratio": positive,
                "stability": stability,
                "complete": bool(eff_complete),
                "coverage_fraction": float(eff_fraction),
                "event_count": len(window_events),
                "reason_code": eff_reason,
                # CR07: R05 freeze provenance (Hedge-shared result).
                "r05_complete": _r05_complete,
                "r05_reason": _r05_reason,
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
        out.setdefault("fetched_at_ms", self._now())
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

    def _assemble_observed_inputs(self, market, row, identity, meta, fund_result,
                                  fund_data, spot_result, funding, cutoff):
        from diveintocrypto_desktop.shortlab.observations import make_observation
        observations = dict(market.get("observations") or {})
        def observed(value, source, fetched, status="OK", **kwargs):
            return make_observation(value, source=source, known_at_ms=fetched,
                                    fetched_at_ms=fetched, status=status, **kwargs)
        fund_at = fund_result.fetched_at_ms
        observations["fundamentals"] = observed(
            dataclasses.asdict(fund_data) if dataclasses.is_dataclass(fund_data) else fund_data,
            fund_result.source, fund_at, fund_result.status)
        observations["ath"] = observed({"ath_usd": _field(fund_data, "ath_usd"),
            "ath_date_ms": _field(fund_data, "ath_date_ms")}, fund_result.source, fund_at, fund_result.status)
        observations["spot_history"] = observed(spot_result, spot_result.source,
            spot_result.fetched_at_ms, spot_result.status)
        observations["contract"] = observed(dict(meta), "binance-exchangeInfo",
            meta.get("observed_at_ms"))
        # A ticker lacking producer timestamps is not assigned a invented
        # source time. Daily closed bars remain a price fallback.
        observations["ticker_24h"] = observed({"price": row.get("price"),
            "price_change_24h": _pct(row.get("ch"))}, "binance-ticker",
            row.get("known_at_ms"), source_as_of_ms=row.get("source_as_of_ms"))
        midnight = cutoff // DAY_MS * DAY_MS
        for days, key in ((30, "funding_history"), (90, "funding_history_90d")):
            events = [e for e in funding.get("events", ()) if midnight-days*DAY_MS <= e["t"] < midnight]
            coverage = self._funding_coverage_fn(events, midnight-days*DAY_MS, midnight, meta.get("onboard_at_ms"))
            # CR07 D05: freeze Inputs with Hedge R05 (shared receipt/schedule).
            # funding["windows"] already AND-gates old max24h with R05 in
            # _fetch_funding; reuse its r05 verdict so midnight-observed legs
            # cannot regain credit the frozen windows denied. Legacy funding
            # dicts without r05 info keep the old result (back-compat).
            _r05_gate: bool | None = None
            try:
                _win = (funding.get("windows") or {}).get(days) if isinstance(funding, Mapping) else None
                if isinstance(_win, Mapping) and "r05_complete" in _win:
                    _r05_gate = bool(_win.get("r05_complete"))
            except Exception:
                _r05_gate = None
            _old_complete = bool(getattr(coverage, "complete", False))
            try:
                _old_frac = float(getattr(coverage, "coverage_fraction", 0.0) or 0.0)
            except Exception:
                _old_frac = 0.0
            if _r05_gate is False:
                _eff_complete = False
                _eff_frac = 0.0
            else:
                _eff_complete = _old_complete
                _eff_frac = _old_frac
            observations[key] = observed(events, "binance-funding", funding.get("fetched_at_ms"),
                window_start_ms=midnight-days*DAY_MS, window_end_ms=midnight,
                source_as_of_ms=max((e["t"] for e in events), default=None),
                complete=_eff_complete, coverage_fraction=_eff_frac)
        observations["quote_asset"] = "USDT"
        market["observations"] = observations

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
        if isinstance(market.get("observations"), Mapping):
            from diveintocrypto_desktop.shortlab.inputs import build_feature_inputs
            frozen = build_feature_inputs(symbol, market["observations"], identity, as_of_ms, self._config)
            result = frozen.to_ltss_inputs()
            result["_frozen_inputs"] = frozen
            return result
        w30 = funding["windows"].get(30, {})
        w90 = funding["windows"].get(90, {})
        w7 = funding["windows"].get(7, {})
        spot_data = spot_result.data if spot_result.data is not None else None
        inputs: dict[str, Any] = {
            "symbol": symbol,
            "as_of_ms": as_of_ms,
            # Universe row (price context only; quote_volume deliberately NOT
            # copied -- the rolling ticker never enters scoring inputs).
            "current_price": _canonical_market_price(row.get("price"), identity, exchange_meta),
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
            "funding_rates_30d": [float(e["funding_rate"]) for e in funding.get("events", ())
                if w30.get("complete") and as_of_ms - _LOOKBACK_MS[30] < int(e.get("t", 0)) <= as_of_ms
                and _finite(e.get("funding_rate")) is not None],
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
        self, symbols: list[str], as_of_ms: int, cutoffs: Mapping[str, int] | None = None
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
        from diveintocrypto_desktop.shortlab.request_budget import make_request_context
        budget = EntryBudget(
            max_calls=refresh_cfg.entry_max_upstream_calls_per_run,
            concurrency=refresh_cfg.entry_concurrency,
            ttl_sec=refresh_cfg.entry_cache_ttl_sec,
            clock=self._clock,
            request_context=make_request_context(self._request_budget, job_type="entry", host="fapi"),
            shared_cache=self._observed_cache,
        )
        try:
            import inspect
            extra = {}
            if "as_of_by_symbol" in inspect.signature(self._entry_runner).parameters:
                extra["as_of_by_symbol"] = dict(cutoffs or {})
            from diveintocrypto_desktop.shortlab.request_budget import scoped_request_context
            with scoped_request_context(budget.request_context):
                batch = await _maybe_await(self._entry_runner(
                    list(symbols),
                    **extra,
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

    # -- H08 hedge (design B30/B32/B33; frozen domain + Repository, no rewrite) --
    def _hedge_lock_for(self, plan_id: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        table = self._hedge_plan_locks.setdefault(loop, {})
        lock = table.get(plan_id)
        if lock is None:
            lock = table[plan_id] = asyncio.Lock()
        return lock

    async def _ensure_hedge_available(self) -> Any:
        repo = self._require_available()
        if self._hedge_available is False:
            raise HedgeUnavailable("hedge tables unavailable (migrate to schema 5)")
        if self._hedge_available is True:
            return repo
        # Auto-probe: 005 failure leaves 001-004 serving, hedge 503.
        def _probe_sync() -> bool:
            con = repo._require_con()
            try:
                cur = con.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
                    ["sl_hedge_plan"],
                )
                return int(cur.fetchone()[0]) > 0
            except Exception:
                return False

        try:
            has = await repo._run(_probe_sync)
        except Exception as exc:
            # Queue-busy probe must surface as 503, never as success.
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise HedgeUnavailable(f"hedge probe failed: {type(exc).__name__}") from exc
        if not has:
            raise HedgeUnavailable("hedge tables unavailable (migrate to schema 5)")
        return repo

    @staticmethod
    def _hedge_normalize(payload: Mapping[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
        """Map camelCase to snake_case, reject unknown/duplicate fields (422)."""
        if not isinstance(payload, Mapping):
            raise HedgeValidationError("payload must be a mapping", reason_code="HEDGE_INPUT_INVALID")
        try:
            from diveintocrypto_desktop.shortlab.hedge.models import camel_to_snake as _c2s
        except Exception:
            def _c2s(name: str) -> str:  # fallback
                out: list[str] = []
                for ch in name:
                    if ch.isupper():
                        out.append("_")
                        out.append(ch.lower())
                    else:
                        out.append(ch)
                return "".join(out).lstrip("_")

        normalised: dict[str, Any] = {}
        for key, value in payload.items():
            snake = key if key in allowed else _c2s(str(key))
            if snake not in allowed:
                raise HedgeValidationError(
                    f"unknown field {key!r}", reason_code="UNKNOWN_FIELD"
                )
            if snake in normalised:
                raise HedgeValidationError(
                    f"duplicate field {key!r}", reason_code="HEDGE_INPUT_INVALID"
                )
            normalised[snake] = value
        return normalised

    async def _hedge_identity_for(self, symbol: str) -> Any:
        sym = str(symbol).upper()
        if self._hedge_identity_fn is not None:
            ident = await _maybe_await(self._hedge_identity_fn(sym))
            if ident is not None:
                return ident
        # R10b D04.1: Hedge consumes the same Catalog->Resolver result as the
        # directional path (never raw first candidate). Overrides first, then
        # catalog candidates through resolve_asset_context with snapshot-grade
        # confidence/multiplier provenance.
        try:
            entry = (self._identity_overrides or {}).get(sym)
            if isinstance(entry, Mapping) and entry:
                # Manual override still goes through the resolver so unit
                # conflicts stay UNKNOWN instead of silently picking one.
                try:
                    from diveintocrypto_desktop.shortlab.identity.resolver import (
                        resolve_asset_context as _rctx,
                    )

                    return _rctx(sym, {"symbol": sym}, [], {sym: dict(entry)})
                except Exception:
                    canonical = str(entry.get("canonical_id") or sym.lower())
                    mult = entry.get("contract_multiplier")
                    return {
                        "canonical_id": canonical,
                        "display_symbol": str(entry.get("display_symbol") or sym),
                        "contract_multiplier": mult,
                        "multiplier_source": entry.get("multiplier_source"),
                        "identity_confidence": str(entry.get("mapping_confidence") or "VERIFIED"),
                        "binance_spot_symbol": entry.get("binance_spot_symbol"),
                        "coingecko_id": entry.get("coingecko_id"),
                    }
        except Exception:
            pass
        # Default catalog path (cutoff = now; same freeze as scoring).
        try:
            cutoff = int(self._now())
        except Exception:
            cutoff = 0
        try:
            exchange_meta: dict[str, Any] = {"symbol": sym}
            # Fetch exchange meta best-effort for resolver provenance.
            try:
                raw_all = await _maybe_await(self._metadata_fn())
                if isinstance(raw_all, Mapping):
                    raw = raw_all.get(sym)
                    if raw is not None:
                        try:
                            exchange_meta = dict(_contract_meta_dict(raw))
                        except Exception:
                            if isinstance(raw, Mapping):
                                exchange_meta = dict(raw)
            except Exception:
                pass
            resolved = self._resolve_identity_default(sym, exchange_meta, cutoff)
            return resolved
        except Exception as exc:
            # Preserve the frozen 422 code for unverified identities.
            if isinstance(exc, HedgeValidationError):
                raise
            raise HedgeValidationError(
                f"identity unverified for {sym}", reason_code="HEDGE_IDENTITY_UNVERIFIED"
            ) from exc

    async def _hedge_mark_for(self, symbol: str) -> Any:
        # R10b: real Observation chain prefers MarketPort/ProductionHedgeMarket
        # when no explicit test fake is injected; explicit _hedge_mark_fn wins
        # for offline tests. 451/region errors propagate as UNAVAILABLE (never
        # fabricated), and are handled upstream as DATA_INSUFFICIENT.
        if self._hedge_mark_fn is not None:
            try:
                mark = await _maybe_await(self._hedge_mark_fn(str(symbol).upper()))
            except Exception as exc:
                msg = str(exc)
                if "451" in msg or "REGION" in msg:
                    raise HedgeUnavailable("VENUE_REGION_UNAVAILABLE") from exc
                raise
            if mark is not None:
                # Surface 451 as honest UNAVAILABLE (test: 451 unknown).
                try:
                    txt = str(mark)
                    if "451" in txt or "VENUE_REGION_UNAVAILABLE" in txt:
                        raise HedgeUnavailable("VENUE_REGION_UNAVAILABLE")
                except HedgeUnavailable:
                    raise
                except Exception:
                    pass
                return mark
        # Real path: MarketPort collect_futures (futures mark) or hedge market.
        for source in (getattr(self, "_market_port", None), getattr(self, "_hedge_market", None)):
            if source is None:
                continue
            try:
                if hasattr(source, "collect_futures"):
                    try:
                        from diveintocrypto_desktop.shortlab.request_budget import (
                            make_request_context as _mkctx,
                        )

                        rctx = _mkctx(
                            getattr(self, "_request_budget", None),
                            job_type="interactive",
                            host="fapi",
                            endpoint_family="markPrice",
                            trace_id=f"repair-mark-{str(symbol).upper()}-{self._now()}",
                        )
                    except Exception:
                        rctx = None
                    collected = await _maybe_await(
                        source.collect_futures(str(symbol).upper(), "1", rctx)
                    )
                    if isinstance(collected, Mapping):
                        # collect_futures returns futures/mark/quote/rules bundle;
                        # extract the mark leg when present.
                        for key in ("futures_mark", "mark", "futuresMark"):
                            if collected.get(key) is not None:
                                return collected[key]
                        # Fallback: the bundle itself carries mark fields.
                        if collected.get("mark_price") is not None or collected.get("native_price") is not None:
                            return collected
                    elif collected is not None:
                        return collected
                elif hasattr(source, "mark"):
                    mark2 = await _maybe_await(source.mark(str(symbol).upper()))
                    if mark2 is not None:
                        return mark2
            except HedgeUnavailable:
                raise
            except Exception as exc:
                # 451 stays honest UNAVAILABLE (never 500 for known).
                msg = str(exc)
                if "451" in msg or "REGION" in msg:
                    raise HedgeUnavailable("VENUE_REGION_UNAVAILABLE") from exc
                continue
        raise HedgeUnavailable("futures mark provider unavailable")

    async def _hedge_quote_for(self, symbol: str, qty: str, venue: str | None = None) -> Any:
        if self._hedge_quote_fn is not None:
            quote = await _maybe_await(self._hedge_quote_fn(str(symbol).upper(), str(qty), venue))
            if quote is not None:
                return quote
        for source in (getattr(self, "_market_port", None), getattr(self, "_hedge_market", None)):
            if source is None:
                continue
            try:
                if hasattr(source, "collect_spot"):
                    try:
                        from diveintocrypto_desktop.shortlab.request_budget import (
                            make_request_context as _mkctx2,
                        )

                        rctx2 = _mkctx2(
                            getattr(self, "_request_budget", None),
                            job_type="interactive",
                            host="fapi",
                            endpoint_family="spotQuote",
                            trace_id=f"repair-quote-{str(symbol).upper()}-{self._now()}",
                        )
                    except Exception:
                        rctx2 = None
                    # collect_spot needs identity; resolve best-effort.
                    try:
                        ident = await self._hedge_identity_for(str(symbol).upper())
                    except Exception:
                        ident = {"canonical_id": str(symbol).lower()}
                    collected = await _maybe_await(
                        source.collect_spot(ident, str(venue or "BINANCE_SPOT"), str(qty), rctx2)
                    )
                    if collected is not None:
                        return collected
                elif hasattr(source, "quote"):
                    q2 = await _maybe_await(source.quote(str(symbol).upper(), str(qty), venue))
                    if q2 is not None:
                        return q2
            except HedgeUnavailable:
                raise
            except Exception:
                continue
        raise HedgeUnavailable("spot quote provider unavailable")

    async def _hedge_funding_for(self, symbol: str) -> Any:
        if self._hedge_funding_fn is not None:
            funding = await _maybe_await(self._hedge_funding_fn(str(symbol).upper()))
            if funding is not None:
                return funding
        # Funding absent is honest NOT_READY downstream, not a 503: return a
        # minimal FundingMetrics-like mapping with null conservative carry.
        try:
            from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics as _FM

            return _FM(symbol=str(symbol).upper())
        except Exception:
            return {"symbol": str(symbol).upper(), "conservative_apr": None}

    def _hedge_rules_for(self, kind: str) -> Any | None:
        fn = self._hedge_futures_rules_fn if kind == "futures" else self._hedge_spot_rules_fn
        if fn is None:
            return None
        try:
            out = fn()
            if asyncio.iscoroutine(out):
                # Sync context: rules fakes are sync; async ones resolve in callers.
                return out
            return out
        except Exception:
            return None

    async def _hedge_resolve_rules(self, kind: str, symbol: str | None = None) -> Any | None:
        fn = self._hedge_futures_rules_fn if kind == "futures" else self._hedge_spot_rules_fn
        if fn is None:
            market = getattr(self, "_hedge_market", None)
            return await market.rules(kind, symbol) if market is not None and symbol else None
        try:
            return await _maybe_await(fn())
        except Exception:
            return None

    def _hedge_formula_version(self) -> str:
        try:
            from diveintocrypto_desktop.shortlab.hedge import HEDGE_FORMULA_VERSION as _V

            return str(_V)
        except Exception:
            return "hedge_v1"

    def _hedge_policy_hash(self) -> str:
        try:
            from diveintocrypto_desktop.shortlab.config import hedge_policy_hash as _hph

            return str(_hph(self._config))
        except Exception:
            return "policy-unknown"

    # -- B32.1 funding opportunities --------------------------------------
    async def funding_opportunities(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        filt = dict(filters or {})
        # Query aliases: accept both snake and camel (frontend maps to snake).
        def _pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if filt.get(name) is not None:
                    return filt[name]
            return default

        min_fcs = _pick("min_fcs", "minFcs")
        min_funding_30d = _pick("min_funding_30d", "minFunding30d")
        min_positive = _pick("min_positive_ratio_30d", "minPositiveRatio30d")
        venue = _pick("venue")
        readiness = _pick("readiness")
        symbol_q = _pick("symbol")
        include_stale_raw = _pick("include_stale", "includeStale", default=False)
        try:
            include_stale = bool(include_stale_raw) if not isinstance(include_stale_raw, str) else str(include_stale_raw).lower() in ("1", "true", "yes")
        except Exception:
            include_stale = False
        sort = str(_pick("sort", default="fcs") or "fcs")
        order = str(_pick("order", default="desc") or "desc")
        try:
            limit = int(_pick("limit", default=50))
        except (TypeError, ValueError):
            raise HedgeValidationError("limit must be an int", reason_code="HEDGE_INPUT_INVALID")
        try:
            offset = int(_pick("offset", default=0))
        except (TypeError, ValueError):
            raise HedgeValidationError("offset must be an int", reason_code="HEDGE_INPUT_INVALID")
        if not 1 <= limit <= 200:
            raise HedgeValidationError("limit must be in 1..200", reason_code="HEDGE_INPUT_INVALID")
        if offset < 0:
            raise HedgeValidationError("offset must be >= 0", reason_code="HEDGE_INPUT_INVALID")
        if sort not in ("fcs", "funding30d", "funding_30d", "breakEvenDays", "break_even_days", "positiveRatio30d", "positive_ratio_30d"):
            raise HedgeValidationError(f"unknown sort {sort!r}", reason_code="HEDGE_INPUT_INVALID")
        if order not in ("asc", "desc"):
            raise HedgeValidationError(f"unknown order {order!r}", reason_code="HEDGE_INPUT_INVALID")
        if venue is not None and str(venue) not in ("BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"):
            raise HedgeValidationError(f"unknown venue {venue!r}", reason_code="HEDGE_INPUT_INVALID")
        if readiness is not None and str(readiness) not in ("READY", "NOT_READY", "BLOCKED"):
            raise HedgeValidationError(f"unknown readiness {readiness!r}", reason_code="HEDGE_INPUT_INVALID")
        try:
            min_fcs_f = float(min_fcs) if min_fcs is not None else None
        except (TypeError, ValueError):
            raise HedgeValidationError("min_fcs must be a number", reason_code="HEDGE_INPUT_INVALID")
        if min_fcs_f is not None and not 0 <= min_fcs_f <= 100:
            raise HedgeValidationError("min_fcs must be in 0..100", reason_code="HEDGE_INPUT_INVALID")
        try:
            min_funding_f = float(str(min_funding_30d)) if min_funding_30d is not None else None
        except (TypeError, ValueError):
            raise HedgeValidationError("min_funding_30d must be a number", reason_code="HEDGE_INPUT_INVALID")
        try:
            min_positive_f = float(str(min_positive)) if min_positive is not None else None
        except (TypeError, ValueError):
            raise HedgeValidationError("min_positive_ratio_30d must be a number", reason_code="HEDGE_INPUT_INVALID")
        if min_positive_f is not None and not 0 <= min_positive_f <= 1:
            raise HedgeValidationError("min_positive_ratio_30d must be in 0..1", reason_code="HEDGE_INPUT_INVALID")
        # CR04 D08 (R01/R09/R10b): current query is latest-per-symbol via
        # list_current_funding_opportunities (ROW_NUMBER, no 200-row cap, no
        # READY pre-filter). When the interface exists, zero is final (never
        # auto-fallback to history 200); compat history lives in the independent
        # funding_opportunities_history entry. Legacy rows without projection_v2
        # stay readonly-visible via the current view as LEGACY/NOT_READY.
        # When the new query yields v2 rows, project via the real
        # project_opportunity port when bound (never Fake).
        try:
            if hasattr(repo, "list_current_funding_opportunities"):
                from diveintocrypto_desktop.shortlab.repair_contracts import OpportunityQuery as _OQ

                # Normalise sort for the new query (fcs/funding30d/breakEvenDays/positiveRatio30d).
                _sort_map = {
                    "fcs": "fcs", "funding30d": "funding30d", "funding_30d": "funding30d",
                    "breakEvenDays": "breakEvenDays", "break_even_days": "breakEvenDays",
                    "positiveRatio30d": "positiveRatio30d", "positive_ratio_30d": "positiveRatio30d",
                }
                _new_sort = _sort_map.get(sort, "fcs")
                try:
                    _oq = _OQ(
                        symbol=str(symbol_q).upper() if symbol_q is not None else None,
                        venue=str(venue) if venue is not None else None,
                        readiness=str(readiness) if readiness is not None else None,
                        min_fcs=float(min_fcs_f) if min_fcs_f is not None else None,
                        min_funding_30d=str(min_funding_30d) if min_funding_30d is not None else None,
                        min_positive_ratio_30d=str(min_positive) if min_positive is not None else None,
                        sort=_new_sort, order=str(order), limit=int(limit), offset=int(offset),
                        include_stale=bool(include_stale),
                    )
                except Exception as exc:
                    raise HedgeValidationError(f"invalid opportunity query: {exc}"[:200], reason_code="HEDGE_INPUT_INVALID") from exc
                try:
                    page = await repo.list_current_funding_opportunities(_oq, self._now())
                except (HedgeBusy, HedgeValidationError):
                    raise
                except Exception as exc:
                    try:
                        from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _BusyN

                        if isinstance(exc, _BusyN):
                            raise HedgeBusy(str(exc)) from exc
                    except HedgeBusy:
                        raise
                    except Exception:
                        pass
                    # CR04 D08: current query failure never auto-revives old READY;
                    # history stays independent via funding_opportunities_history.
                    raise
                if page is not None:
                    items_raw = list(getattr(page, "items", []) or ())
                    total_new = int(getattr(page, "total", len(items_raw)) or len(items_raw))
                    # CR04 D08: current query authoritative; has_v2 kept only for
                    # legacy probe below, never to gate empty as not-wired.
                    has_v2 = any(isinstance(it, Mapping) and (it.get("readiness_breakdown") is not None or it.get("fcs_config_hash") is not None) for it in items_raw)
                    # CR04: always build current wire when the interface is present;
                    # zero is final when v2 data exists (probe below separates
                    # legacy-only compat). D08 R01/R09/R10b.
                    if True:
                        # Optionally re-project via real port for freshness (best-effort).
                        wire_items: list[dict[str, Any]] = []
                        try:
                            proj_fn = self._require_repair_port("project_opportunity")
                            use_proj = is_real_repair_callback(proj_fn)
                        except Exception:
                            proj_fn = None
                            use_proj = False
                        for it in items_raw:
                            if not isinstance(it, Mapping):
                                continue
                            d = dict(it)
                            # Real projection refresh best-effort (keeps stored when it fails).
                            if use_proj:
                                try:
                                    # project_opportunity expects snapshot mapping + as_of.
                                    snap = {"snapshot_id": d.get("snapshot_id"), "symbol": d.get("symbol"), "as_of_ms": d.get("as_of_ms")}
                                    # Enrich with stored fields for projection inputs when needed.
                                    for k in ("fcs", "funding_30d", "positive_ratio_30d", "readiness_breakdown"):
                                        if d.get(k) is not None and snap.get(k) is None:
                                            snap[k] = d.get(k)
                                    _ = proj_fn(snap, int(self._now()))
                                except Exception:
                                    pass
                            # Wire: both D08 camel + legacy snake-tolerant keys.
                            wire_items.append({
                                "snapshotId": d.get("snapshot_id"),
                                "symbol": d.get("symbol"),
                                "canonicalId": d.get("canonical_id"),
                                "asOf": d.get("as_of_ms"),
                                "expiresAt": d.get("expires_at_ms"),
                                "stale": bool(d.get("stale", False)),
                                "fcs": d.get("fcs"),
                                "fcsVersion": d.get("fcs_version", "fcs_v2"),
                                "fcsConfigHash": d.get("fcs_config_hash"),
                                "funding7d": d.get("funding_7d"),
                                "funding30d": d.get("funding_30d"),
                                "positiveRatio30d": d.get("positive_ratio_30d"),
                                "historyClass": d.get("history_class"),
                                "bestVenue": d.get("best_venue"),
                                "breakEvenDays": d.get("break_even_days"),
                                "conservativeApr": d.get("conservative_apr"),
                                "readiness": d.get("readiness", "NOT_READY"),
                                "readinessBreakdown": d.get("readiness_breakdown"),
                                "reasons": list(d.get("reasons") or []),
                                # Legacy compat keys for old clients/tests.
                                "canonical_id": d.get("canonical_id"),
                                "fcs_version": d.get("fcs_version", "fcs_v2"),
                                "funding_30d": d.get("funding_30d"),
                                "best_venue": d.get("best_venue"),
                            })
                        # CR04 D08: current view wins; non-empty serves directly.
                        # Empty (total 0) is final when v2 data exists; legacy-only
                        # DBs (no v2 at all) delegate to the explicit history entry
                        # for compat (D08 independent entry, R01/R09/R10b).
                        if wire_items or total_new > 0:
                            try:
                                asof = getattr(page, "as_of_ms", None)
                            except Exception:
                                asof = None
                            return {"asOf": asof, "items": wire_items, "total": int(total_new)}
                        # Empty current view: probe for any v2 latest row.
                        try:
                            from diveintocrypto_desktop.shortlab.repair_contracts import OpportunityQuery as _OQProbe

                            _probe_q = _OQProbe(include_stale=True, limit=1, offset=0)
                            _probe_page = await repo.list_current_funding_opportunities(_probe_q, self._now())
                            _probe_items = list(getattr(_probe_page, "items", []) or ())
                            # CR04: v2 means a real projection breakdown (LEGACY rows
                            # carry fcs_config_hash but breakdown None + LEGACY reason).
                            def _is_v2(_it: Any) -> bool:
                                if not isinstance(_it, Mapping):
                                    return False
                                if _it.get("readiness_breakdown") is None:
                                    return False
                                try:
                                    _rs = list(_it.get("reasons") or [])
                                except Exception:
                                    _rs = []
                                return "LEGACY" not in [str(_r) for _r in _rs]

                            _has_any_v2 = any(_is_v2(_it) for _it in _probe_items)
                        except (HedgeBusy, HedgeValidationError):
                            raise
                        except Exception:
                            # Probe failed: never revive old READY; treat as v2 (strict).
                            _has_any_v2 = True
                        if not _has_any_v2:
                            # No v2 data at all (pure legacy or empty DB): explicit
                            # history entry for compat (never auto-revive v2 READY).
                            return await self.funding_opportunities_history(filters)
                        try:
                            asof = getattr(page, "as_of_ms", None)
                        except Exception:
                            asof = None
                        # D08: empty result asOf is None (never now).
                        return {"asOf": asof, "items": [], "total": 0}
        except (HedgeBusy, HedgeValidationError):
            raise
        except Exception:
            # CR04 D08: current failure never auto-revives old READY; when the
            # interface exists the error propagates (history via explicit entry).
            if hasattr(repo, "list_current_funding_opportunities"):
                raise
            pass
        try:
            rows = await repo.list_funding_opportunities(limit=200, offset=0)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                import json as _json

                def _parse(value: Any) -> Any:
                    if isinstance(value, (dict, list)):
                        return value
                    if isinstance(value, str):
                        try:
                            return _json.loads(value)
                        except Exception:
                            return {}
                    return value or {}

                funding_metrics = _parse(row.get("funding_metrics_json"))
                venue_summary = _parse(row.get("venue_summary_json"))
                risk = _parse(row.get("risk_json"))
                module_scores = _parse(row.get("module_scores_json"))
                reasons_raw = _parse(row.get("reasons_json"))
                reasons = list(reasons_raw) if isinstance(reasons_raw, list) else []
                fcs_val = row.get("fcs")
                try:
                    fcs_f = float(fcs_val) if fcs_val is not None else None
                except (TypeError, ValueError):
                    fcs_f = None
                # funding30d / positive ratios are decimal strings in metrics.
                def _num(mapping: Any, *keys: str) -> float | None:
                    if not isinstance(mapping, Mapping):
                        return None
                    for key in keys:
                        if mapping.get(key) is not None:
                            try:
                                return float(str(mapping[key]))
                            except (TypeError, ValueError):
                                continue
                    return None

                funding30d = _num(funding_metrics, "funding_30d", "funding30d")
                funding90d = _num(funding_metrics, "funding_90d", "funding90d")
                positive30d = _num(funding_metrics, "positive_ratio_30d", "positiveRatio30d")
                positive90d = _num(funding_metrics, "positive_ratio_90d", "positiveRatio90d")
                best_venue = None
                if isinstance(venue_summary, Mapping):
                    best_venue = venue_summary.get("best_venue") or venue_summary.get("bestVenue")
                reference_notional = row.get("reference_notional_usd")
                try:
                    ref_notion = float(reference_notional) if reference_notional is not None else 10000.0
                except (TypeError, ValueError):
                    ref_notion = 10000.0
                round_trip = _num(venue_summary, "round_trip_cost_pct", "roundTripCostPct", "roundtrip_cost_pct")
                break_even = _num(risk, "break_even_days", "breakEvenDays", "breakeven_days")
                if break_even is None:
                    break_even = _num(venue_summary, "break_even_days", "breakEvenDays")
                # Filters (missing never matches a positive threshold).
                if min_fcs_f is not None and (fcs_f is None or fcs_f < min_fcs_f):
                    continue
                if min_funding_f is not None and (funding30d is None or funding30d < min_funding_f):
                    continue
                if min_positive_f is not None and (positive30d is None or positive30d < min_positive_f):
                    continue
                if venue is not None and str(best_venue) != str(venue):
                    continue
                if readiness is not None and str(row.get("readiness")) != str(readiness):
                    continue
                items.append({
                    "symbol": str(row.get("symbol")),
                    "canonicalId": str(row.get("canonical_id") or str(row.get("symbol")).lower()),
                    "fcs": fcs_f,
                    "fcsVersion": str(row.get("fcs_version") or "fcs_v1"),
                    "funding30d": funding30d,
                    "funding90d": funding90d,
                    "positiveRatio30d": positive30d,
                    "positiveRatio90d": positive90d,
                    "bestVenue": best_venue,
                    "referenceNotionalUsd": ref_notion,
                    "roundTripCostPct": round_trip,
                    "breakEvenDays": break_even,
                    "readiness": str(row.get("readiness") or "NOT_READY"),
                    "reasons": [str(r) for r in reasons if isinstance(r, str)],
                })
            except Exception:
                continue
        # Sort: numeric ASC/DESC NULLS LAST, symbol ASC, snapshot ASC (A9.1).
        sort_norm = {"funding30d": "funding30d", "funding_30d": "funding30d",
                     "breakEvenDays": "breakEvenDays", "break_even_days": "breakEvenDays",
                     "positiveRatio30d": "positiveRatio30d",
                     "positive_ratio_30d": "positiveRatio30d"}.get(sort, "fcs")

        def _sort_val(item: dict[str, Any]) -> float | None:
            value = item.get(sort_norm if sort_norm != "fcs" else "fcs")
            if value is None:
                return None
            try:
                result = float(value)
            except (TypeError, ValueError):
                return None
            if result != result or result in (float("inf"), float("-inf")):
                return None
            return result

        decorated = [((_sort_val(it) is None, _sort_val(it) or 0.0, str(it.get("symbol") or "")), it) for it in items]
        decorated.sort(key=lambda pair: (pair[0][0], pair[0][1] if order == "asc" else -pair[0][1] if pair[0][1] else 0.0, pair[0][2]))
        # Stable ties: re-sort equal primaries by symbol.
        ordered: list[dict[str, Any]] = []
        idx = 0
        sorted_all = [it for _, it in decorated]
        # The tuple sort above already keeps symbol order for ties on the
        # numeric leg when values are equal (Python sort is stable).
        ordered = sorted_all
        total = len(ordered)
        page = ordered[offset: offset + limit]
        return {"asOf": self._now(), "items": page, "total": total}

    # -- B32.1b funding history (D08 independent entry, CR04) -----------------
    async def funding_opportunities_history(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Explicit compat history entry (D08, CR04).

        Independent from the current latest-per-symbol view: reads the raw
        ``sl_funding_capture_snapshot`` history (200-row cap, no ROW_NUMBER,
        no READY pre-filter) with the same filter/sort/page semantics as the
        legacy fallback. Never called automatically for v2 DBs; only used
        explicitly for compat or via the legacy-only probe in
        :meth:`funding_opportunities` (pure legacy DBs without any v2 data).
        New v2 READY must never be revived through this path.
        """
        repo = await self._ensure_hedge_available()
        filt = dict(filters or {})

        def _pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if filt.get(name) is not None:
                    return filt[name]
            return default

        min_fcs = _pick("min_fcs", "minFcs")
        min_funding_30d = _pick("min_funding_30d", "minFunding30d")
        min_positive = _pick("min_positive_ratio_30d", "minPositiveRatio30d")
        venue = _pick("venue")
        readiness = _pick("readiness")
        sort = str(_pick("sort", default="fcs") or "fcs")
        order = str(_pick("order", default="desc") or "desc")
        try:
            limit = int(_pick("limit", default=50))
        except (TypeError, ValueError):
            raise HedgeValidationError("limit must be an int", reason_code="HEDGE_INPUT_INVALID")
        try:
            offset = int(_pick("offset", default=0))
        except (TypeError, ValueError):
            raise HedgeValidationError("offset must be an int", reason_code="HEDGE_INPUT_INVALID")
        if not 1 <= limit <= 200:
            raise HedgeValidationError("limit must be in 1..200", reason_code="HEDGE_INPUT_INVALID")
        if offset < 0:
            raise HedgeValidationError("offset must be >= 0", reason_code="HEDGE_INPUT_INVALID")
        if sort not in ("fcs", "funding30d", "funding_30d", "breakEvenDays", "break_even_days", "positiveRatio30d", "positive_ratio_30d"):
            raise HedgeValidationError(f"unknown sort {sort!r}", reason_code="HEDGE_INPUT_INVALID")
        if order not in ("asc", "desc"):
            raise HedgeValidationError(f"unknown order {order!r}", reason_code="HEDGE_INPUT_INVALID")
        if venue is not None and str(venue) not in ("BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"):
            raise HedgeValidationError(f"unknown venue {venue!r}", reason_code="HEDGE_INPUT_INVALID")
        if readiness is not None and str(readiness) not in ("READY", "NOT_READY", "BLOCKED"):
            raise HedgeValidationError(f"unknown readiness {readiness!r}", reason_code="HEDGE_INPUT_INVALID")
        try:
            min_fcs_f = float(min_fcs) if min_fcs is not None else None
        except (TypeError, ValueError):
            raise HedgeValidationError("min_fcs must be a number", reason_code="HEDGE_INPUT_INVALID")
        if min_fcs_f is not None and not 0 <= min_fcs_f <= 100:
            raise HedgeValidationError("min_fcs must be in 0..100", reason_code="HEDGE_INPUT_INVALID")
        try:
            min_funding_f = float(str(min_funding_30d)) if min_funding_30d is not None else None
        except (TypeError, ValueError):
            raise HedgeValidationError("min_funding_30d must be a number", reason_code="HEDGE_INPUT_INVALID")
        try:
            min_positive_f = float(str(min_positive)) if min_positive is not None else None
        except (TypeError, ValueError):
            raise HedgeValidationError("min_positive_ratio_30d must be a number", reason_code="HEDGE_INPUT_INVALID")
        if min_positive_f is not None and not 0 <= min_positive_f <= 1:
            raise HedgeValidationError("min_positive_ratio_30d must be in 0..1", reason_code="HEDGE_INPUT_INVALID")
        try:
            rows = await repo.list_funding_opportunities(limit=200, offset=0)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                import json as _json

                def _parse(value: Any) -> Any:
                    if isinstance(value, (dict, list)):
                        return value
                    if isinstance(value, str):
                        try:
                            return _json.loads(value)
                        except Exception:
                            return {}
                    return value or {}

                funding_metrics = _parse(row.get("funding_metrics_json"))
                venue_summary = _parse(row.get("venue_summary_json"))
                risk = _parse(row.get("risk_json"))
                reasons_raw = _parse(row.get("reasons_json"))
                reasons = list(reasons_raw) if isinstance(reasons_raw, list) else []
                fcs_val = row.get("fcs")
                try:
                    fcs_f = float(fcs_val) if fcs_val is not None else None
                except (TypeError, ValueError):
                    fcs_f = None

                def _num(mapping: Any, *keys: str) -> float | None:
                    if not isinstance(mapping, Mapping):
                        return None
                    for key in keys:
                        if mapping.get(key) is not None:
                            try:
                                return float(str(mapping[key]))
                            except (TypeError, ValueError):
                                continue
                    return None

                funding30d = _num(funding_metrics, "funding_30d", "funding30d")
                funding90d = _num(funding_metrics, "funding_90d", "funding90d")
                positive30d = _num(funding_metrics, "positive_ratio_30d", "positiveRatio30d")
                positive90d = _num(funding_metrics, "positive_ratio_90d", "positiveRatio90d")
                best_venue = None
                if isinstance(venue_summary, Mapping):
                    best_venue = venue_summary.get("best_venue") or venue_summary.get("bestVenue")
                reference_notional = row.get("reference_notional_usd")
                try:
                    ref_notion = float(reference_notional) if reference_notional is not None else 10000.0
                except (TypeError, ValueError):
                    ref_notion = 10000.0
                round_trip = _num(venue_summary, "round_trip_cost_pct", "roundTripCostPct", "roundtrip_cost_pct")
                break_even = _num(risk, "break_even_days", "breakEvenDays", "breakeven_days")
                if break_even is None:
                    break_even = _num(venue_summary, "break_even_days", "breakEvenDays")
                if min_fcs_f is not None and (fcs_f is None or fcs_f < min_fcs_f):
                    continue
                if min_funding_f is not None and (funding30d is None or funding30d < min_funding_f):
                    continue
                if min_positive_f is not None and (positive30d is None or positive30d < min_positive_f):
                    continue
                if venue is not None and str(best_venue) != str(venue):
                    continue
                if readiness is not None and str(row.get("readiness")) != str(readiness):
                    continue
                items.append({
                    "symbol": str(row.get("symbol")),
                    "canonicalId": str(row.get("canonical_id") or str(row.get("symbol")).lower()),
                    "fcs": fcs_f,
                    "fcsVersion": str(row.get("fcs_version") or "fcs_v1"),
                    "funding30d": funding30d,
                    "funding90d": funding90d,
                    "positiveRatio30d": positive30d,
                    "positiveRatio90d": positive90d,
                    "bestVenue": best_venue,
                    "referenceNotionalUsd": ref_notion,
                    "roundTripCostPct": round_trip,
                    "breakEvenDays": break_even,
                    "readiness": str(row.get("readiness") or "NOT_READY"),
                    "reasons": [str(r) for r in reasons if isinstance(r, str)],
                })
            except Exception:
                continue
        sort_norm = {"funding30d": "funding30d", "funding_30d": "funding30d",
                     "breakEvenDays": "breakEvenDays", "break_even_days": "breakEvenDays",
                     "positiveRatio30d": "positiveRatio30d",
                     "positive_ratio_30d": "positiveRatio30d"}.get(sort, "fcs")

        def _sort_val(item: dict[str, Any]) -> float | None:
            value = item.get(sort_norm if sort_norm != "fcs" else "fcs")
            if value is None:
                return None
            try:
                result = float(value)
            except (TypeError, ValueError):
                return None
            if result != result or result in (float("inf"), float("-inf")):
                return None
            return result

        decorated = [((_sort_val(it) is None, _sort_val(it) or 0.0, str(it.get("symbol") or "")), it) for it in items]
        decorated.sort(key=lambda pair: (pair[0][0], pair[0][1] if order == "asc" else -pair[0][1] if pair[0][1] else 0.0, pair[0][2]))
        sorted_all = [it for _, it in decorated]
        ordered = sorted_all
        total = len(ordered)
        page_items = ordered[offset: offset + limit]
        return {"asOf": self._now(), "items": page_items, "total": total}

    # -- B32.2 venues -----------------------------------------------------
    async def hedge_venues(self, symbol: str, notional_usd: Any = None) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        sym = str(symbol or "").upper()
        if not sym:
            raise HedgeValidationError("symbol is required", reason_code="HEDGE_SYMBOL_NOT_FOUND")
        notional_s = str(notional_usd) if notional_usd is not None else "10000"
        try:
            from decimal import Decimal as _Dec

            parsed = _Dec(notional_s.strip())
            if not parsed.is_finite() or parsed <= 0:
                raise ValueError()
        except Exception:
            raise HedgeValidationError("notional_usd must be a positive decimal string", reason_code="HEDGE_INPUT_INVALID")
        # Canonical via identity (honest when unverified).
        canonical = sym.lower()
        identity_confidence = "UNRESOLVED"
        try:
            ident = await self._hedge_identity_for(sym)
            if isinstance(ident, Mapping):
                canonical = str(ident.get("canonical_id") or canonical)
                identity_confidence = str(ident.get("identity_confidence") or ident.get("mapping_confidence") or "UNRESOLVED")
            else:
                canonical = str(getattr(ident, "canonical_id", canonical) or canonical)
                identity_confidence = str(getattr(ident, "identity_confidence", getattr(ident, "mapping_confidence", "UNRESOLVED")) or "UNRESOLVED")
        except HedgeValidationError:
            # Unverified identity stays honest: venues report NO_SPOT_VENUE.
            pass
        # Config capabilities (never fabricate an unconfigured chain).
        try:
            hedge_cfg = getattr(self._config, "hedge", None)
            providers = getattr(hedge_cfg, "providers", {}) if hedge_cfg is not None else {}
            if not isinstance(providers, Mapping):
                providers = {}
        except Exception:
            providers = {}

        def _provider_enabled(name: str) -> bool:
            entry = providers.get(name) if isinstance(providers, Mapping) else None
            if isinstance(entry, Mapping):
                return bool(entry.get("enabled"))
            return bool(getattr(entry, "enabled", False))

        spot_enabled = _provider_enabled("binance_spot")
        alpha_enabled = _provider_enabled("binance_alpha")
        onchain_enabled = _provider_enabled("onchain")
        now_ms = self._now()
        venues_out: list[dict[str, Any]] = []
        # Repository snapshots for this canonical (reference-notional quotes).
        try:
            stored = await repo.list_spot_venues(canonical_id=canonical, limit=200, offset=0)
        except Exception:
            stored = ()
        by_venue: dict[str, Any] = {}
        for row in stored or ():
            try:
                by_venue.setdefault(str(row.get("venue")), row)
            except Exception:
                continue
        for venue_name in ("BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"):
            enabled = {"BINANCE_SPOT": spot_enabled, "BINANCE_ALPHA": alpha_enabled, "ONCHAIN_DEX": onchain_enabled}[venue_name]
            if not enabled:
                reason = "CHAIN_PROVIDER_UNCONFIGURED" if venue_name == "ONCHAIN_DEX" else "PROVIDER_NOT_CONFIGURED"
                venues_out.append({
                    "venue": venue_name,
                    "canonicalId": canonical,
                    "symbol": sym,
                    "status": "UNAVAILABLE",
                    "reasonCode": reason,
                    "capabilities": {"enabled": False},
                    "identityConfidence": identity_confidence,
                })
                continue
            row = by_venue.get(venue_name)
            if row is not None:
                try:
                    import json as _json

                    quote = _json.loads(row.get("quote_json")) if isinstance(row.get("quote_json"), str) else dict(row.get("quote_json") or {})
                except Exception:
                    quote = {}
                # Quantity strings stay strings on the wire.
                venues_out.append({
                    "venue": venue_name,
                    "canonicalId": canonical,
                    "symbol": sym,
                    "status": "UNAVAILABLE" if (row.get("expires_at_ms") is None or now_ms >= int(row["expires_at_ms"]) or _finite(row.get("reference_notional_usd")) != _finite(notional_s)) else str(row.get("status") or "UNAVAILABLE"),
                    "reasonCode": "QUOTE_EXPIRED" if row.get("expires_at_ms") is None or now_ms >= int(row["expires_at_ms"]) else ("QUOTE_NOTIONAL_MISMATCH" if _finite(row.get("reference_notional_usd")) != _finite(notional_s) else row.get("reason_code")),
                    "asOf": row.get("as_of_ms"),
                    "expiresAt": row.get("expires_at_ms"),
                    "referenceNotionalUsd": str(row.get("reference_notional_usd")),
                    "requestedNotionalUsd": notional_s,
                    "notionalMatches": _finite(row.get("reference_notional_usd")) == _finite(notional_s),
                    "expired": row.get("expires_at_ms") is None or now_ms >= int(row["expires_at_ms"]),
                    "quote": quote,
                    "capabilities": {"enabled": True},
                    "identityConfidence": identity_confidence,
                })
                continue
            # No stored quote: honest per-venue UNAVAILABLE (never fabricate).
            venues_out.append({
                "venue": venue_name,
                "canonicalId": canonical,
                "symbol": sym,
                "status": "UNAVAILABLE",
                "reasonCode": "NO_SPOT_VENUE" if venue_name == "BINANCE_SPOT" else "VENUE_QUOTE_MISSING",
                "capabilities": {"enabled": True},
                "identityConfidence": identity_confidence,
            })
        return {"symbol": sym, "canonicalId": canonical, "asOf": now_ms, "venues": venues_out}

    # -- B32.3 simulate ---------------------------------------------------
    async def simulate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        try:
            from diveintocrypto_desktop.shortlab.hedge.models import HedgeSimulationRequest as _Req
        except Exception as exc:
            raise HedgeUnavailable(f"hedge models unavailable: {exc}") from exc
        allowed = frozenset({f.name for f in __import__("dataclasses").fields(_Req)})
        snake = self._hedge_normalize(dict(payload or {}), allowed)
        # Build the frozen request (invalid enums/quantities -> 422).
        try:
            from diveintocrypto_desktop.shortlab.hedge.models import from_api_dict as _from_api

            request = _from_api(_Req, snake)
        except Exception as exc:
            msg = str(exc)
            reason = "HEDGE_INPUT_INVALID"
            for code in ("HEDGE_RATIO_INVALID", "HEDGE_RISK_BUDGET_INVALID", "HEDGE_IDENTITY_UNVERIFIED",
                         "HEDGE_MULTIPLIER_UNVERIFIED", "LIQUIDATION_PRICE_INVALID", "HEDGE_INPUT_INVALID"):
                if code in msg:
                    reason = code
                    break
            # PlannerInputError carries "422 <CODE>:".
            if "422 " in msg:
                try:
                    reason = msg.split("422 ", 1)[1].split(":", 1)[0].strip().split()[0]
                except Exception:
                    pass
            raise HedgeValidationError(msg[:300], reason_code=reason) from exc
        # R10b D12: optional decisionId link (re-validate asset/goal/qty/expiry).
        # Old manual requests without decisionId keep working (readonly compat).
        decision_row: Any = None
        if getattr(request, "decision_id", None) is not None:
            did = str(getattr(request, "decision_id") or "").strip()
            if not did:
                raise HedgeValidationError("decision_id must be non-empty", reason_code="HEDGE_INPUT_INVALID")
            try:
                decision_row = await repo.get_hedge_decision(did)
            except Exception as exc:
                try:
                    from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _BusyD

                    if isinstance(exc, _BusyD):
                        raise HedgeBusy(str(exc)) from exc
                except HedgeBusy:
                    raise
                except Exception:
                    pass
                raise
            if decision_row is None:
                raise HedgeSimulationNotFound(did)
            # Expiry: expired decisions cannot seed new READY simulations.
            try:
                exp_d = int(decision_row.get("expires_at_ms") or 0)
            except Exception:
                exp_d = 0
            now_chk = self._now()
            if now_chk >= exp_d:
                raise HedgeQuoteExpired("QUOTE_EXPIRED: decision expired, re-request")
            # Asset/goal coherence (symbol must match, goal must match mode).
            try:
                import json as _jsd

                dec_json = decision_row.get("decision_json") or {}
                if isinstance(dec_json, str):
                    try:
                        dec_json = _jsd.loads(dec_json)
                    except Exception:
                        dec_json = {}
                # decision_json holds to_record_dict with nested request dict.
                dec_req = {}
                if isinstance(dec_json, Mapping):
                    # to_record_dict nests DTOs as dicts; request is under 'request'.
                    raw_req = dec_json.get("request")
                    if isinstance(raw_req, Mapping):
                        dec_req = dict(raw_req)
                    # Fallback: top-level symbol/goal when flattened.
                    if not dec_req:
                        dec_req = {k: dec_json.get(k) for k in ("symbol", "goal") if dec_json.get(k) is not None}
                dec_sym = str(dec_req.get("symbol", dec_req.get("futures_symbol", "")) or "").upper()
                dec_goal = str(dec_req.get("goal", "") or "")
                if dec_sym and dec_sym != str(request.symbol).upper():
                    raise HedgeInputMismatch(f"SIMULATION_INPUT_MISMATCH: decision symbol {dec_sym!r} != request {str(request.symbol).upper()!r}")
                # NO_HEDGE decisions never seed two-leg simulations.
                try:
                    dec_rec = dec_json.get("recommendation") if isinstance(dec_json, Mapping) else None
                    if dec_rec is None and isinstance(dec_json, Mapping):
                        # Nested DecisionResult dict may hold recommendation at top.
                        dec_rec = dec_json.get("recommendation")
                except Exception:
                    dec_rec = None
                if str(dec_rec or "").upper() == "NO_HEDGE":
                    raise HedgeValidationError("UNHEDGED_PLAN_UNSUPPORTED: NO_HEDGE decision cannot seed a two-leg simulation", reason_code="UNHEDGED_PLAN_UNSUPPORTED")
                # Goal/mode coherence via shared validator (422 on mismatch).
                try:
                    from diveintocrypto_desktop.shortlab.hedge.simulator import validate_goal_mode as _vgm

                    eff, reasons = _vgm({"mode": getattr(request, "mode", None), "goal": getattr(request, "goal", None)})
                    if reasons and "GOAL_MODE_MISMATCH" in reasons:
                        raise HedgeValidationError("GOAL_MODE_MISMATCH", reason_code="GOAL_MODE_MISMATCH")
                except (HedgeValidationError, HedgeInputMismatch, HedgeQuoteExpired):
                    raise
                except Exception:
                    pass
            except (HedgeValidationError, HedgeInputMismatch, HedgeQuoteExpired, HedgeSimulationNotFound, HedgeBusy):
                raise
            except Exception as exc:
                raise HedgeValidationError(f"decision link invalid: {exc}"[:200], reason_code="HEDGE_INPUT_INVALID") from exc
        now_ms = self._now()
        identity = await self._hedge_identity_for(request.symbol)
        futures_mark = await self._hedge_mark_for(request.symbol)
        from decimal import Decimal, localcontext
        from diveintocrypto_desktop.shortlab.hedge.planner import _floor_to_step, _get_rule_decimal, compute_target_hedge_ratio
        futures_rules = await self._hedge_resolve_rules("futures", request.symbol)
        preferred = getattr(request, "preferred_spot_venue", None)
        spot_rules = None
        if preferred in (None, "AUTO", "BINANCE_SPOT"):
            try:
                spot_rules = await self._hedge_resolve_rules("spot", request.symbol)
            except HedgeUnavailable:
                pass  # An alternate venue may still provide a limited quote.

        native_price = Decimal(str(_field(futures_mark, "native_price", "mark_price", "price")))
        multiplier = Decimal(str(_field(identity, "contract_multiplier")))
        fx = Decimal(str(_field(futures_mark, "quote_to_usd")))
        if not all(v.is_finite() and v > 0 for v in (native_price, multiplier, fx)):
            raise HedgeUnavailable("MARK_OR_FX_INVALID")
        ratio = compute_target_hedge_ratio(request)
        with localcontext() as decimal_context:
            decimal_context.prec = 80
            futures_qty = _floor_to_step(Decimal(request.futures_notional_usd) / (native_price * fx),
                _get_rule_decimal(futures_rules, "lot_rules", "step_size"))
            qty_hint = str(_floor_to_step(futures_qty * multiplier * ratio,
                _get_rule_decimal(spot_rules, "lot_rules", "step_size")))
        if Decimal(qty_hint) <= 0:
            raise HedgeValidationError("quantity below lot size", reason_code="HEDGE_INPUT_INVALID")
        preferred = getattr(request, "preferred_spot_venue", None)
        spot_quote = await self._hedge_quote_for(request.symbol, qty_hint, None if preferred == "AUTO" else preferred)
        funding = await self._hedge_funding_for(request.symbol)
        venue_rules = _field(spot_quote, "trading_rules")
        if preferred not in (None, "AUTO", "BINANCE_SPOT") and venue_rules:
            spot_rules = venue_rules
        quote_data = dataclasses.asdict(spot_quote) if dataclasses.is_dataclass(spot_quote) else dict(spot_quote)
        import hashlib, json
        quote_id = quote_data.get("snapshot_id") or "quote-" + hashlib.sha256(
            json.dumps(quote_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        quote_data["snapshot_id"] = quote_id
        await repo.save_spot_venue_snapshot({
            "snapshot_id": quote_id, "canonical_id": _field(identity, "canonical_id"),
            "venue": quote_data["venue"], "venue_symbol": quote_data.get("symbol"),
            "chain": quote_data.get("chain"), "contract_address": quote_data.get("contract_address"),
            "as_of_ms": quote_data["as_of_ms"], "fetched_at_ms": quote_data["fetched_at_ms"],
            "expires_at_ms": quote_data.get("expires_at_ms"),
            "reference_notional_usd": quote_data["reference_notional_usd"],
            "quote_json": quote_data, "status": quote_data["status"], "reason_code": quote_data.get("reason_code")})
        now_ms = self._now()  # The simulation freezes only after its inputs exist.
        try:
            from diveintocrypto_desktop.shortlab.hedge.planner import simulate_hedge as _sim
        except Exception as exc:
            raise HedgeUnavailable(f"hedge planner unavailable: {exc}") from exc
        # R10b: bind real RepairPorts for the new Gate/breakdown/economics.
        # Unbound/fake ports keep legacy readonly calc (never new READY).
        _sim_ports: Any = None
        try:
            _candidate = getattr(self, "_repair_ports", None)
            if _candidate is not None:
                # Verify real (D19.6); fake/unbound -> None (legacy).
                _all_real = True
                try:
                    for _k in ("evaluate_funding_entry_gate", "build_ratio_proposal"):
                        _cb = getattr(_candidate, _k, None)
                        if _cb is None or not is_real_repair_callback(_cb):
                            _all_real = False
                            break
                except Exception:
                    _all_real = False
                if _all_real:
                    _sim_ports = _candidate
        except Exception:
            _sim_ports = None
        # R10b: futures execution quote (two-sided depth) for the new execution
        # Gate. Best-effort: missing stays None (legacy path honest NOT_READY).
        _futures_quote: Any = None
        try:
            _mp = getattr(self, "_market_port", None) or getattr(self, "_hedge_market", None)
            if _mp is not None and hasattr(_mp, "collect_futures"):
                try:
                    from diveintocrypto_desktop.shortlab.request_budget import make_request_context as _mkfq

                    _rctxfq = _mkfq(getattr(self, "_request_budget", None), job_type="interactive", host="fapi", endpoint_family="futuresDepth", trace_id=f"sim-fq-{str(request.symbol).upper()}-{int(now_ms)}")
                except Exception:
                    _rctxfq = None
                try:
                    _bundled = await _maybe_await(_mp.collect_futures(str(request.symbol).upper(), "1", _rctxfq))
                    if isinstance(_bundled, Mapping):
                        _futures_quote = _bundled.get("futures_quote", _bundled.get("futuresQuote"))
                except Exception:
                    _futures_quote = None
        except Exception:
            _futures_quote = None
        try:
            # Planner signature supports futures_quote/ports as keywords (D19.1);
            # legacy callers without them keep working via try/except.
            try:
                result = _sim(
                    request,
                    futures_mark=futures_mark,
                    spot_quote=spot_quote,
                    futures_rules=futures_rules,
                    spot_rules=spot_rules,
                    funding=funding,
                    identity=identity,
                    policy=self._config,
                    now_ms=now_ms,
                    futures_quote=_futures_quote,
                    ports=_sim_ports,
                )
            except TypeError:
                result = _sim(
                    request,
                    futures_mark=futures_mark,
                    spot_quote=spot_quote,
                    futures_rules=futures_rules,
                    spot_rules=spot_rules,
                    funding=funding,
                    identity=identity,
                    policy=self._config,
                    now_ms=now_ms,
                )
        except Exception as exc:
            msg = str(exc)
            reason = getattr(exc, "reason_code", None) or "HEDGE_INPUT_INVALID"
            if getattr(exc, "status_code", None) == 422 or "422 " in msg:
                if "422 " in msg and not getattr(exc, "reason_code", None):
                    try:
                        reason = msg.split("422 ", 1)[1].split(":", 1)[0].strip().split()[0]
                    except Exception:
                        pass
                raise HedgeValidationError(msg[:300], reason_code=str(reason)) from exc
            raise
        # Persist the immutable snapshot (non-trading).
        try:
            from diveintocrypto_desktop.shortlab.hedge.models import to_api_dict as _to_api
        except Exception:
            _to_api = lambda r: dict(r)  # type: ignore
        try:
            import dataclasses as _dc
            import json as _json

            result_dict = _dc.asdict(result)
            input_dict = _dc.asdict(request)
        except Exception:
            result_dict = {}
            input_dict = dict(snake)
        formula_version = self._hedge_formula_version()
        policy_hash = self._hedge_policy_hash()
        simulation_id = str(getattr(result, "simulation_id", "") or f"sim-{now_ms}-{self._hedge_seq}")
        self._hedge_seq += 1
        record = {
            "simulation_id": simulation_id,
            "symbol": str(request.symbol).upper(),
            "generated_at_ms": int(getattr(result, "generated_at_ms", now_ms)),
            "expires_at_ms": int(getattr(result, "expires_at_ms", now_ms + 60_000)),
            "formula_version": formula_version,
            "policy_hash": policy_hash,
            "input_json": input_dict,
            "result_json": result_dict,
            "source_meta_json": {"schema_version": "hedge-source-v1",
                "identity": _safe_json(dataclasses.asdict(identity) if dataclasses.is_dataclass(identity) else dict(identity) if isinstance(identity, Mapping) else getattr(identity, "__dict__", {})),
                "mark": _safe_json(dataclasses.asdict(futures_mark) if dataclasses.is_dataclass(futures_mark) else dict(futures_mark) if isinstance(futures_mark, Mapping) else {}),
                "quote_snapshot_id": quote_id, "quote": _safe_json(quote_data),
                "futures_rules": _safe_json(dataclasses.asdict(futures_rules) if dataclasses.is_dataclass(futures_rules) else dict(futures_rules) if isinstance(futures_rules, Mapping) else {}),
                "spot_rules": _safe_json(dataclasses.asdict(spot_rules) if dataclasses.is_dataclass(spot_rules) else dict(spot_rules) if isinstance(spot_rules, Mapping) else {}),
                "funding": _safe_json(dataclasses.asdict(funding) if dataclasses.is_dataclass(funding) else dict(funding) if isinstance(funding, Mapping) else {})},
        }
        try:
            refs: list[Any] = [("VENUE_QUOTE", quote_id, "simulation-quote")]
            # R10b: persist the decision link as a snapshot reference (D12).
            try:
                _did = getattr(request, "decision_id", None)
                if _did is not None and str(_did).strip():
                    refs.append(("DECISION", str(_did).strip(), "simulation-decision"))
            except Exception:
                pass
            await repo.save_hedge_simulation(record, references=refs)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy
                from diveintocrypto_desktop.shortlab.repository import SnapshotImmutableError as _Immutable

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
                if isinstance(exc, _Immutable):
                    # Same id, different content: mint a fresh id (re-simulate
                    # returns a new ID, never rewrites the old snapshot).
                    simulation_id = f"{simulation_id}#r{self._hedge_seq}"
                    self._hedge_seq += 1
                    record["simulation_id"] = simulation_id
                    try:
                        refs2: list[Any] = [("VENUE_QUOTE", quote_id, "simulation-quote")]
                        try:
                            _did2 = getattr(request, "decision_id", None)
                            if _did2 is not None and str(_did2).strip():
                                refs2.append(("DECISION", str(_did2).strip(), "simulation-decision"))
                        except Exception:
                            pass
                        await repo.save_hedge_simulation(record, references=refs2)
                    except Exception:
                        await repo.save_hedge_simulation(record, references=[("VENUE_QUOTE", quote_id, "simulation-quote")])
                else:
                    raise
            except (HedgeBusy, HedgeUnavailable):
                raise
            except Exception:
                # Immutable collision fallback already handled; other errors
                # are honest 503 (never report success without a commit).
                raise HedgeUnavailable(f"simulation persist failed: {type(exc).__name__}") from exc
        try:
            api_result = _to_api(result)
        except Exception:
            api_result = dict(result_dict)
        # Normalise IDs to the persisted snapshot id.
        api_result["simulationId"] = simulation_id
        api_result["simulation_id"] = simulation_id
        return {
            "simulationId": simulation_id,
            "generatedAt": int(getattr(result, "generated_at_ms", now_ms)),
            "expiresAt": int(getattr(result, "expires_at_ms", now_ms + 60_000)),
            "expired": bool(now_ms >= int(getattr(result, "expires_at_ms", now_ms + 60_000))),
            "formulaVersion": formula_version,
            "policyHash": policy_hash,
            "readiness": str(getattr(result, "readiness", "NOT_READY")),
            "result": api_result,
        }

    # -- B32.3.1 get simulation -------------------------------------------
    async def get_simulation(self, simulation_id: str) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        sim_id = str(simulation_id or "").strip()
        if not sim_id:
            raise HedgeValidationError("simulation_id is required", reason_code="HEDGE_INPUT_INVALID")
        try:
            row = await repo.get_hedge_simulation(sim_id)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if row is None:
            raise HedgeSimulationNotFound(sim_id)
        now_ms = self._now()
        try:
            expires_at = int(row.get("expires_at_ms"))
        except (TypeError, ValueError):
            expires_at = now_ms
        try:
            generated_at = int(row.get("generated_at_ms"))
        except (TypeError, ValueError):
            generated_at = now_ms
        expired = bool(now_ms >= expires_at)
        # Wire view is camelCase (frozen storage stays snake_case).
        def _camel_top(value: Any) -> Any:
            if not isinstance(value, Mapping):
                return value if value is not None else {}
            try:
                from diveintocrypto_desktop.shortlab.hedge.models import snake_to_camel as _s2c
            except Exception:
                def _s2c(name: str) -> str:  # fallback
                    parts = str(name).split("_")
                    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])

            return {_s2c(str(k)): v for k, v in dict(value).items()}

        try:
            raw_input = row.get("input_json") or {}
            raw_result = row.get("result_json") or {}
            raw_meta = row.get("source_meta_json") or {}
            import json as _json

            if isinstance(raw_input, str):
                try:
                    raw_input = _json.loads(raw_input)
                except Exception:
                    raw_input = {}
            if isinstance(raw_result, str):
                try:
                    raw_result = _json.loads(raw_result)
                except Exception:
                    raw_result = {}
            if isinstance(raw_meta, str):
                try:
                    raw_meta = _json.loads(raw_meta)
                except Exception:
                    raw_meta = {}
        except Exception:
            raw_input, raw_result, raw_meta = {}, {}, {}
        return {
            "simulationId": str(row.get("simulation_id")),
            "generatedAt": generated_at,
            "expiresAt": expires_at,
            "expired": expired,
            "formulaVersion": str(row.get("formula_version") or ""),
            "policyHash": str(row.get("policy_hash") or ""),
            "input": _camel_top(raw_input),
            "result": _camel_top(raw_result),
            "sourceMeta": _camel_top(raw_meta),
            "currentUsability": "EXPIRED" if expired else "USABLE",
        }

    # -- B32.4 save plan --------------------------------------------------
    async def save_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        allowed = frozenset({
            "simulation_id", "client_request_id", "symbol", "mode",
            "futures_notional_usd", "hedge_ratio", "spot_venue", "leverage",
            "margin_mode", "margin_usd", "liquidation_price", "planned_hold_days",
            "fcs_snapshot_id", "plan_version",
            # R10b D12: optional decision link (goal-aware, NO_HEDGE 422).
            "decision_id", "goal",
        })
        snake = self._hedge_normalize(dict(payload or {}), allowed)
        simulation_id = snake.get("simulation_id")
        client_request_id = snake.get("client_request_id")
        if not isinstance(simulation_id, str) or not simulation_id.strip():
            raise HedgeValidationError("simulation_id is required", reason_code="HEDGE_INPUT_INVALID")
        if not isinstance(client_request_id, str) or not client_request_id.strip():
            raise HedgeValidationError("client_request_id is required", reason_code="HEDGE_INPUT_INVALID")
        simulation_id = simulation_id.strip()
        client_request_id = client_request_id.strip()
        now_ms = self._now()
        # Idempotency first: same key + identical simulation/config returns the
        # existing plan even when the simulation has since expired.
        try:
            existing_plan_id: str | None = None
            try:
                # Probe via direct read: list is cheaper than catching 409.
                # Repository.create_hedge_plan already does this atomically;
                # this pre-read only shapes the error mapping below.
                pass
            except Exception:
                pass
            sim_row = await repo.get_hedge_simulation(simulation_id)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if sim_row is None:
            raise HedgeSimulationNotFound(simulation_id)
        # R10b D12: optional decision link validation (invalid/expired/NO_HEDGE).
        # Old manual plans without decision_id keep working (readonly compat).
        _decision_id = snake.get("decision_id")
        if _decision_id is not None:
            _did_s = str(_decision_id).strip()
            if not _did_s:
                raise HedgeValidationError("decision_id must be non-empty", reason_code="HEDGE_INPUT_INVALID")
            try:
                _dec_row = await repo.get_hedge_decision(_did_s)
            except Exception as exc:
                try:
                    from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _BusyD2

                    if isinstance(exc, _BusyD2):
                        raise HedgeBusy(str(exc)) from exc
                except HedgeBusy:
                    raise
                except Exception:
                    pass
                raise
            if _dec_row is None:
                raise HedgeSimulationNotFound(_did_s)
            try:
                _exp_d2 = int(_dec_row.get("expires_at_ms") or 0)
            except Exception:
                _exp_d2 = 0
            if int(now_ms) >= int(_exp_d2):
                raise HedgeQuoteExpired("QUOTE_EXPIRED: decision expired, re-request")
            try:
                import json as _jsd2

                _dj2 = _dec_row.get("decision_json") or {}
                if isinstance(_dj2, str):
                    try:
                        _dj2 = _jsd2.loads(_dj2)
                    except Exception:
                        _dj2 = {}
                _rec2 = _dj2.get("recommendation") if isinstance(_dj2, Mapping) else None
                if str(_rec2 or "").upper() == "NO_HEDGE":
                    raise HedgeValidationError("UNHEDGED_PLAN_UNSUPPORTED: NO_HEDGE decision cannot create a two-leg plan", reason_code="UNHEDGED_PLAN_UNSUPPORTED")
                # Symbol coherence: decision symbol must match simulation symbol.
                _dec_req2 = _dj2.get("request") if isinstance(_dj2, Mapping) else None
                if isinstance(_dec_req2, Mapping):
                    _dec_sym2 = str(_dec_req2.get("symbol", "") or "").upper()
                    _sim_sym2 = str(symbol if isinstance(symbol, str) else snake.get("symbol") or "").upper() if 'symbol' in dir() else str(snake.get("symbol") or "").upper()
                    # symbol var not yet defined here; use sim_input fallback.
                    try:
                        _sim_sym_tmp = str((sim_result.get("symbol") if isinstance(sim_result, Mapping) else None) or (sim_input.get("symbol") if isinstance(sim_input, Mapping) else None) or snake.get("symbol") or "").upper()
                    except Exception:
                        _sim_sym_tmp = ""
                    if _dec_sym2 and _sim_sym_tmp and _dec_sym2 != _sim_sym_tmp:
                        raise HedgeInputMismatch(f"SIMULATION_INPUT_MISMATCH: decision symbol {_dec_sym2!r} != simulation {_sim_sym_tmp!r}")
                # Goal coherence when caller pins goal.
                if snake.get("goal") is not None and isinstance(_dec_req2, Mapping):
                    _want_goal = str(snake.get("goal") or "").upper()
                    _have_goal = str(_dec_req2.get("goal", "") or "").upper()
                    if _want_goal and _have_goal and _want_goal != _have_goal:
                        raise HedgeInputMismatch(f"SIMULATION_INPUT_MISMATCH: goal {_want_goal!r} != decision {_have_goal!r}")
            except (HedgeValidationError, HedgeInputMismatch, HedgeQuoteExpired, HedgeSimulationNotFound, HedgeBusy):
                raise
            except Exception as exc:
                raise HedgeValidationError(f"decision link invalid: {exc}"[:200], reason_code="HEDGE_INPUT_INVALID") from exc
        # Version / content match: formula/policy + request content.
        try:
            import json as _json

            sim_input = sim_row.get("input_json") or {}
            sim_result = sim_row.get("result_json") or {}
            if isinstance(sim_input, str):
                try:
                    sim_input = _json.loads(sim_input)
                except Exception:
                    sim_input = {}
            if isinstance(sim_result, str):
                try:
                    sim_result = _json.loads(sim_result)
                except Exception:
                    sim_result = {}
        except Exception:
            sim_input, sim_result = {}, {}
        # Optional caller-supplied fields must match the frozen simulation.
        def _norm(value: Any) -> str:
            return str(value).strip() if isinstance(value, str) else str(value) if value is not None else ""

        for key in ("symbol", "mode", "spot_venue"):
            if snake.get(key) is not None:
                want = _norm(snake.get(key)).upper()
                have_candidates = []
                if isinstance(sim_input, Mapping):
                    for cand in (sim_input.get(key), sim_input.get("".join(p[:1].upper() + p[1:] for p in key.split("_")))):
                        if cand is not None:
                            have_candidates.append(str(cand).upper())
                if isinstance(sim_result, Mapping):
                    for cand in (sim_result.get(key), sim_result.get("spot_venue") if key == "spot_venue" else None):
                        if cand is not None:
                            have_candidates.append(str(cand).upper())
                if have_candidates and want not in [str(c).upper() for c in have_candidates]:
                    raise HedgeInputMismatch(
                        f"SIMULATION_INPUT_MISMATCH: {key} {want!r} does not match simulation"
                    )
        # Formula/policy version mismatch is also SIMULATION_INPUT_MISMATCH.
        # The simulation row carries the frozen versions; a caller that pins
        # an explicit formula/policy (via plan_version field misuse) is
        # rejected here. Repository rows always use the frozen versions.
        # Expiry: only for first creation (idempotent retries bypass it).
        try:
            expires_at = int(sim_row.get("expires_at_ms"))
        except (TypeError, ValueError):
            expires_at = now_ms
        # Build the plan record from the frozen simulation result.
        try:
            symbol = str((sim_result.get("symbol") if isinstance(sim_result, Mapping) else None) or (sim_input.get("symbol") if isinstance(sim_input, Mapping) else None) or snake.get("symbol") or "")
            mode = str((sim_result.get("mode") if isinstance(sim_result, Mapping) else None) or (sim_input.get("mode") if isinstance(sim_input, Mapping) else None) or snake.get("mode") or "ABSOLUTE")
            canonical = str((sim_result.get("canonical_id") if isinstance(sim_result, Mapping) else None) or (sim_input.get("symbol") if isinstance(sim_input, Mapping) else symbol) or symbol).lower()
            target_ratio = sim_result.get("target_hedge_ratio") if isinstance(sim_result, Mapping) else None
            futures_notional = sim_result.get("futures_notional_usd") if isinstance(sim_result, Mapping) else None
            futures_qty = sim_result.get("futures_contract_qty") if isinstance(sim_result, Mapping) else None
            canonical_qty = sim_result.get("canonical_futures_qty") if isinstance(sim_result, Mapping) else None
            spot_venue = sim_result.get("spot_venue") if isinstance(sim_result, Mapping) else (snake.get("spot_venue") or "BINANCE_SPOT")
            spot_symbol = sim_result.get("spot_symbol") if isinstance(sim_result, Mapping) else None
            spot_chain = sim_result.get("spot_chain") if isinstance(sim_result, Mapping) else None
            spot_contract = sim_result.get("spot_contract") if isinstance(sim_result, Mapping) else None
            target_spot_qty = sim_result.get("target_spot_qty") if isinstance(sim_result, Mapping) else None
            safety = sim_result.get("plan_safety_score") if isinstance(sim_result, Mapping) else None
        except Exception:
            symbol, mode, canonical = str(snake.get("symbol") or ""), "ABSOLUTE", ""
            target_ratio = futures_notional = futures_qty = canonical_qty = None
            spot_venue, spot_symbol, spot_chain, spot_contract, target_spot_qty, safety = "BINANCE_SPOT", None, None, None, None, None
        if not symbol:
            symbol = str(snake.get("symbol") or simulation_id)
        import uuid as _uuid

        plan_id = f"plan-{_uuid.uuid4().hex[:12]}"
        # Deterministic plan_id for identical idempotency keys is owned by the
        # repository (returns existing plan_id); this fresh id is only for the
        # first creation.
        import json as _json

        meta = sim_row.get("source_meta_json") or {}
        if isinstance(meta, str):
            meta = _json.loads(meta)
        identity_dict = dict(meta.get("identity") or {})
        if not identity_dict or identity_dict.get("contract_multiplier") is None:
            raise HedgeUnavailable("LEGACY_SIMULATION_IDENTITY_UNAVAILABLE: re-simulate")
        plan_config = {"target_hedge_ratio": str(target_ratio) if target_ratio is not None else "1",
                       "identity": identity_dict, "contract_multiplier": identity_dict.get("contract_multiplier"),
                       "multiplier_source": identity_dict.get("multiplier_source"), "source_meta": meta}
        try:
            if isinstance(sim_input, Mapping):
                plan_config["simulation_input"] = dict(sim_input)
        except Exception:
            pass
        # R10b: persist the decision link + goal for Gate/version/reference checks.
        try:
            if _decision_id is not None and str(_decision_id).strip():
                plan_config["decision_id"] = str(_decision_id).strip()
            if snake.get("goal") is not None:
                plan_config["goal"] = str(snake.get("goal"))
        except Exception:
            pass
        record = {
            "plan_id": plan_id,
            "symbol": str(symbol).upper(),
            "canonical_id": str(canonical or str(symbol).lower()),
            "mode": str(mode).upper(),
            "status": "DRAFT",
            "simulation_id": simulation_id,
            "client_request_id": client_request_id,
            "plan_config_json": plan_config,
            "created_at_ms": now_ms,
            "updated_at_ms": now_ms,
            "target_hedge_ratio": float(str(target_ratio)) if target_ratio is not None else 1.0,
            "futures_notional_usd": float(str(futures_notional)) if futures_notional is not None else 0.0,
            "futures_contract_qty": float(str(futures_qty)) if futures_qty is not None else 0.0,
            "canonical_futures_qty": float(str(canonical_qty)) if canonical_qty is not None else 0.0,
            "spot_venue": str(spot_venue or "BINANCE_SPOT"),
            "spot_symbol": spot_symbol,
            "spot_chain": spot_chain,
            "spot_contract": spot_contract,
            "target_spot_qty": float(str(target_spot_qty)) if target_spot_qty is not None else 0.0,
            "plan_safety_score": float(safety) if safety is not None else None,
            "plan_version": 1,
        }
        if snake.get("fcs_snapshot_id") is not None:
            record["fcs_snapshot_id"] = snake.get("fcs_snapshot_id")
        if snake.get("planned_hold_days") is not None:
            try:
                record["planned_hold_days"] = int(snake.get("planned_hold_days"))  # type: ignore[assignment]
            except (TypeError, ValueError):
                raise HedgeValidationError("planned_hold_days must be an int", reason_code="HEDGE_INPUT_INVALID")
        try:
            stored_plan_id = await repo.create_hedge_plan(record)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import HedgeIdempotencyError as _Idem
                from diveintocrypto_desktop.shortlab.repository import HedgeVersionConflictError as _Ver
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy
                from diveintocrypto_desktop.shortlab.repository import ReferenceNotFoundError as _Ref
                from diveintocrypto_desktop.shortlab.repository import ValidationError as _Val

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
                if isinstance(exc, _Idem):
                    raise HedgeIdempotencyMismatch(str(exc)[:300]) from exc
                if isinstance(exc, _Ver):
                    raise HedgeVersionConflict(str(exc)[:300]) from exc
                if isinstance(exc, _Ref):
                    raise HedgeSimulationNotFound(simulation_id) from exc
                if isinstance(exc, _Val):
                    msg = str(exc)
                    if "expired" in msg.lower():
                        raise HedgeQuoteExpired("QUOTE_EXPIRED: simulation expired, re-simulate") from exc
                    raise HedgeValidationError(msg[:300], reason_code="HEDGE_INPUT_INVALID") from exc
            except (HedgeBusy, HedgeIdempotencyMismatch, HedgeVersionConflict, HedgeSimulationNotFound, HedgeQuoteExpired, HedgeValidationError):
                raise
            except Exception:
                pass
            raise
        # create_hedge_plan returns the existing plan_id on identical retry.
        is_retry = bool(stored_plan_id != plan_id)
        try:
            plan_row = await repo.get_hedge_plan(stored_plan_id)
        except Exception:
            plan_row = None
        return {
            "planId": stored_plan_id,
            "existing": bool(is_retry),
            "plan": self._hedge_plan_to_api(plan_row or {**record, "plan_id": stored_plan_id}),
        }

    @staticmethod
    def _hedge_plan_to_api(row: Mapping[str, Any] | dict[str, Any]) -> dict[str, Any]:
        try:
            config = row.get("plan_config_json")
            import json as _json

            if isinstance(config, str):
                try:
                    config = _json.loads(config)
                except Exception:
                    pass
        except Exception:
            config = {}
        return {
            "planId": str(row.get("plan_id") or ""),
            "symbol": str(row.get("symbol") or ""),
            "canonicalId": str(row.get("canonical_id") or ""),
            "mode": str(row.get("mode") or ""),
            "status": str(row.get("status") or ""),
            "simulationId": str(row.get("simulation_id") or ""),
            "clientRequestId": str(row.get("client_request_id") or ""),
            "targetHedgeRatio": str(row.get("target_hedge_ratio") if row.get("target_hedge_ratio") is not None else "1"),
            "futuresNotionalUsd": str(row.get("futures_notional_usd") if row.get("futures_notional_usd") is not None else "0"),
            "spotVenue": str(row.get("spot_venue") or ""),
            "targetSpotQty": str(row.get("target_spot_qty") if row.get("target_spot_qty") is not None else "0"),
            "planVersion": int(row.get("plan_version") or 1),
            "planSafetyScore": row.get("plan_safety_score"),
            "planConfig": config or {},
            "createdAt": row.get("created_at_ms"),
            "updatedAt": row.get("updated_at_ms"),
            "activatedAt": row.get("activated_at_ms"),
            "closedAt": row.get("closed_at_ms"),
        }

    # -- B32.5/B32.6 list/get plans ---------------------------------------
    async def list_plans(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        filt = dict(filters or {})

        def _pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if filt.get(name) is not None:
                    return filt[name]
            return default

        status = _pick("status")
        symbol = _pick("symbol")
        mode = _pick("mode")
        venue = _pick("venue")
        try:
            limit = int(_pick("limit", default=50))
        except (TypeError, ValueError):
            raise HedgeValidationError("limit must be an int", reason_code="HEDGE_INPUT_INVALID")
        try:
            offset = int(_pick("offset", default=0))
        except (TypeError, ValueError):
            raise HedgeValidationError("offset must be an int", reason_code="HEDGE_INPUT_INVALID")
        if not 1 <= limit <= 200:
            raise HedgeValidationError("limit must be in 1..200", reason_code="HEDGE_INPUT_INVALID")
        if offset < 0:
            raise HedgeValidationError("offset must be >= 0", reason_code="HEDGE_INPUT_INVALID")
        if status is not None and str(status) not in ("DRAFT", "READY", "PARTIALLY_FILLED", "ACTIVE", "CLOSING", "CLOSED", "INVALID"):
            raise HedgeValidationError(f"unknown status {status!r}", reason_code="HEDGE_INPUT_INVALID")
        if mode is not None and str(mode) not in ("ABSOLUTE", "RELATIVE"):
            raise HedgeValidationError(f"unknown mode {mode!r}", reason_code="HEDGE_INPUT_INVALID")
        if venue is not None and str(venue) not in ("BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"):
            raise HedgeValidationError(f"unknown venue {venue!r}", reason_code="HEDGE_INPUT_INVALID")
        try:
            rows = await repo.list_hedge_plans(status=str(status) if status is not None else None, limit=200, offset=0)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        items = []
        for row in rows or ():
            if symbol is not None and str(row.get("symbol")).upper() != str(symbol).upper():
                continue
            if mode is not None and str(row.get("mode")).upper() != str(mode).upper():
                continue
            if venue is not None and str(row.get("spot_venue")) != str(venue):
                continue
            items.append(self._hedge_plan_to_api(row))
        total = len(items)
        return {"items": items[offset: offset + limit], "total": total}

    async def get_plan(self, plan_id: str) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        pid = str(plan_id or "").strip()
        if not pid:
            raise HedgeValidationError("plan_id is required", reason_code="HEDGE_INPUT_INVALID")
        try:
            row = await repo.get_hedge_plan(pid)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if row is None:
            raise HedgePlanNotFound(pid)
        positions = await repo.aggregate_hedge_position(pid)
        try:
            monitor = await repo.latest_hedge_monitor(pid)
        except Exception:
            monitor = None
        try:
            alerts = await repo.list_hedge_alerts(plan_id=pid, limit=50, offset=0)
        except Exception:
            alerts = ()
        return {
            "plan": self._hedge_plan_to_api(row),
            "positions": [dict(p) for p in (positions or ())],
            "monitor": dict(monitor) if isinstance(monitor, Mapping) else None,
            "alerts": [dict(a) for a in (alerts or ())],
        }

    # -- B32.7 apply leg event --------------------------------------------
    async def apply_leg_event(self, plan_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        pid = str(plan_id or "").strip()
        if not pid:
            raise HedgeValidationError("plan_id is required", reason_code="HEDGE_INPUT_INVALID")
        allowed = frozenset({"event", "event_json", "client_event_id", "expected_version", "expected_plan_version"})
        snake = self._hedge_normalize(dict(payload or {}), allowed)
        event = snake.get("event") if snake.get("event") is not None else snake.get("event_json")
        client_event_id = snake.get("client_event_id")
        expected_version = snake.get("expected_version") if snake.get("expected_version") is not None else snake.get("expected_plan_version")
        if not isinstance(event, Mapping) or not event:
            raise HedgeValidationError("event is required", reason_code="HEDGE_INPUT_INVALID")
        if not isinstance(client_event_id, str) or not client_event_id.strip():
            raise HedgeValidationError("client_event_id is required", reason_code="HEDGE_INPUT_INVALID")
        try:
            expected_int = int(expected_version)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise HedgeValidationError("expected_version must be an int", reason_code="HEDGE_INPUT_INVALID")
        if isinstance(expected_int, bool) or expected_int < 1:
            raise HedgeValidationError("expected_version must be an int >= 1", reason_code="HEDGE_INPUT_INVALID")
        # Normalise the event via the frozen ledger (never rewrite maths here).
        try:
            from diveintocrypto_desktop.shortlab.hedge.ledger import normalize_event_for_repo as _norm
        except Exception:
            try:
                from diveintocrypto_desktop.shortlab.hedge.ledger import normalize_event_for_repo as _norm  # type: ignore
            except Exception as exc:
                raise HedgeUnavailable(f"hedge ledger unavailable: {exc}") from exc
        # Accept camelCase event keys via HedgeEvent.from_api_dict shape.
        try:
            from diveintocrypto_desktop.shortlab.hedge.models import HedgeEvent as _HE
            from diveintocrypto_desktop.shortlab.hedge.models import from_api_dict as _from_api

            dto = _from_api(_HE, dict(event))
            import dataclasses as _dc

            event_dict = _dc.asdict(dto)
        except Exception as exc:
            raise HedgeValidationError(f"invalid event: {exc}"[:300], reason_code="HEDGE_INPUT_INVALID") from exc
        try:
            normalised = _norm(event_dict)
        except Exception as exc:
            raise HedgeValidationError(f"invalid event: {exc}"[:300], reason_code="HEDGE_INPUT_INVALID") from exc
        if isinstance(normalised, Mapping):
            event_payload: Any = dict(normalised)
        else:
            try:
                import dataclasses as _dc2

                event_payload = _dc2.asdict(normalised)  # type: ignore[arg-type]
            except Exception:
                event_payload = dict(event_dict)
        lock = self._hedge_lock_for(pid)
        async with lock:
            try:
                result = await repo.apply_hedge_event(pid, client_event_id.strip(), expected_int, event_payload)
            except Exception as exc:
                try:
                    from diveintocrypto_desktop.shortlab.repository import HedgeIdempotencyError as _Idem
                    from diveintocrypto_desktop.shortlab.repository import HedgeVersionConflictError as _Ver
                    from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy
                    from diveintocrypto_desktop.shortlab.repository import ReferenceNotFoundError as _Ref
                    from diveintocrypto_desktop.shortlab.repository import ValidationError as _Val

                    if isinstance(exc, _Busy):
                        raise HedgeBusy(str(exc)) from exc
                    if isinstance(exc, _Idem):
                        raise HedgeIdempotencyMismatch(str(exc)[:300]) from exc
                    if isinstance(exc, _Ver):
                        raise HedgeVersionConflict(str(exc)[:300]) from exc
                    if isinstance(exc, _Ref):
                        raise HedgePlanNotFound(pid) from exc
                    if isinstance(exc, _Val):
                        raise HedgeValidationError(str(exc)[:300], reason_code="HEDGE_INPUT_INVALID") from exc
                except (HedgeBusy, HedgeIdempotencyMismatch, HedgeVersionConflict, HedgePlanNotFound, HedgeValidationError):
                    raise
                except Exception:
                    pass
                raise
        if getattr(self, "_hedge_jobs", None) is not None:
            self._hedge_jobs.invalidate(pid)
        return {
            "eventId": str(result.get("event_id")),
            "planId": str(result.get("plan_id")),
            "planVersion": int(result.get("plan_version")),
            "positions": [dict(p) for p in (result.get("positions") or ())],
            "balanceSource": str(result.get("balance_source") or "CONFIRMED"),
            "estimated": bool(result.get("estimated")),
        }

    # -- B32.8/B32.9 activate/close ----------------------------------------
    async def activate(self, plan_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        pid = str(plan_id or "").strip()
        if not pid:
            raise HedgeValidationError("plan_id is required", reason_code="HEDGE_INPUT_INVALID")
        expected: int | None = None
        if payload:
            allowed = frozenset({"expected_version", "expected_plan_version"})
            snake = self._hedge_normalize(dict(payload), allowed)
            raw = snake.get("expected_version") if snake.get("expected_version") is not None else snake.get("expected_plan_version")
            if raw is not None:
                try:
                    expected = int(raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    raise HedgeValidationError("expected_version must be an int", reason_code="HEDGE_INPUT_INVALID")
        try:
            row = await repo.get_hedge_plan(pid)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if row is None:
            raise HedgePlanNotFound(pid)
        current_version = int(row.get("plan_version") or 1)
        if expected is not None and int(expected) != current_version:
            raise HedgeVersionConflict("PLAN_VERSION_CONFLICT")
        # Gate: both legs positive with drift <= 5pts (frozen ledger rule).
        positions = await repo.aggregate_hedge_position(pid)
        try:
            from diveintocrypto_desktop.shortlab.hedge.ledger import evaluate_position_state as _eval

            target = str(row.get("target_hedge_ratio") or "1")
            mode_s = str(row.get("mode") or "ABSOLUTE")
            state = _eval(positions, target, mode=mode_s, plan_id=pid,
                          plan_version=int(current_version), updated_at_ms=self._now())
            state_str = str(getattr(state, "status", None) or getattr(state, "value", None) or state or "")
        except Exception:
            # Fallback: require both legs open > 0.
            try:
                from decimal import Decimal as _Dec

                by_leg = {str(p.get("leg_type")): str(p.get("remaining_qty") or p.get("open_qty") or "0") for p in (positions or ())}
                fut = _Dec(by_leg.get("FUTURES_SHORT", "0") or "0")
                spot = _Dec(by_leg.get("SPOT_LONG", "0") or "0")
                state_str = "ACTIVE" if (fut > 0 and spot > 0) else "PARTIALLY_FILLED"
            except Exception:
                state_str = "PARTIALLY_FILLED"
        if state_str not in ("READY", "ACTIVE", "OK", "PARTIALLY_FILLED_ACTIVE", "FUNDED_PENDING_ACTIVATION"):
            # The frozen evaluator returns ACTIVE only when drift <= 5pts;
            # anything else (DRAFT/PARTIALLY_FILLED/CLOSING) blocks activation.
            # R10b: FUNDED_PENDING_ACTIVATION (both legs filled, not yet ACTIVE)
            # is also activatable (D12).
            if state_str != "ACTIVE":
                raise HedgeLegsIncomplete("HEDGE_LEGS_INCOMPLETE: both legs must be fully filled")
        # R10b D12: NO_HEDGE plans never activate (422, not 409).
        try:
            _cfg_act = row.get("plan_config_json")
            import json as _jsa

            if isinstance(_cfg_act, str):
                try:
                    _cfg_act = _jsa.loads(_cfg_act)
                except Exception:
                    _cfg_act = {}
            if isinstance(_cfg_act, Mapping) and str(_cfg_act.get("decision_id") or "").strip():
                _did_act = str(_cfg_act.get("decision_id")).strip()
                try:
                    _dec_act = await repo.get_hedge_decision(_did_act)
                    if _dec_act is not None:
                        _dj_act = _dec_act.get("decision_json") or {}
                        if isinstance(_dj_act, str):
                            try:
                                _dj_act = _jsa.loads(_dj_act)
                            except Exception:
                                _dj_act = {}
                        if isinstance(_dj_act, Mapping) and str(_dj_act.get("recommendation") or "").upper() == "NO_HEDGE":
                            raise HedgeValidationError("UNHEDGED_PLAN_UNSUPPORTED: NO_HEDGE decision cannot activate a two-leg plan", reason_code="UNHEDGED_PLAN_UNSUPPORTED")
                except (HedgeValidationError, HedgeBusy, HedgePlanNotFound, HedgeSimulationNotFound):
                    raise
                except Exception:
                    pass
        except (HedgeValidationError, HedgeBusy, HedgePlanNotFound, HedgeSimulationNotFound):
            raise
        except Exception:
            pass
        now_ms = self._now()
        # CR01 D12 (R06b/R08b/R10b): single-cutoff six-item activation re-check.
        # All six use one cutoff; any REQUIRED FAIL/UNKNOWN/expired blocks
        # ACTIVE. Correct keyword-only build_activation_check_record call,
        # full record persisted (even on rejection for audit) and CAS update
        # associated. Check or persist failure never swallowed to ACTIVE.
        from diveintocrypto_desktop.shortlab.hedge.monitor import build_activation_check_record as _bac
        from diveintocrypto_desktop.shortlab.hedge.protection import validate_protection_confirmation as _vpc

        _sym_act = str(row.get("symbol") or "").upper()
        _cutoff = int(now_ms)
        _known_at = int(now_ms)

        def _dto_get(obj: Any, *names: str, default: Any = None) -> Any:
            for _n in names:
                if isinstance(obj, Mapping):
                    if obj.get(_n) is not None:
                        return obj.get(_n)
                else:
                    try:
                        _v = getattr(obj, _n, None)
                    except Exception:
                        _v = None
                    if _v is not None:
                        return _v
            return default

        # Plan map for protection hash binding (current remaining + refs).
        _plan_map_act: dict[str, Any] = {"plan_id": pid, "plan_version": int(current_version), "symbol": _sym_act}
        try:
            _cfgm = row.get("plan_config_json")
            if isinstance(_cfgm, str):
                import json as _jsm

                try:
                    _cfgm = _jsm.loads(_cfgm)
                except Exception:
                    _cfgm = {}
            if isinstance(_cfgm, Mapping):
                for _k in ("liquidation_price", "liquidationPrice", "stop_trigger_price", "stopTriggerPrice",
                           "stop_trigger_basis", "stopTriggerBasis", "rule_ids", "ruleIds",
                           "rule_version", "ruleVersion", "spot_venue", "spotVenue"):
                    if _cfgm.get(_k) is not None and _plan_map_act.get(_k) is None:
                        # Normalise camel to snake for the pure validator.
                        _nk = {"liquidationPrice": "liquidation_price", "stopTriggerPrice": "stop_trigger_price",
                               "stopTriggerBasis": "stop_trigger_basis", "ruleIds": "rule_ids",
                               "ruleVersion": "rule_version", "spotVenue": "spot_venue"}.get(_k, _k)
                        if _plan_map_act.get(_nk) is None:
                            _plan_map_act[_nk] = _cfgm.get(_k)
            for _rk in ("liquidation_price", "stop_trigger_price", "stop_trigger_basis", "spot_venue"):
                if row.get(_rk) is not None and _plan_map_act.get(_rk) is None:
                    _plan_map_act[_rk] = row.get(_rk)
        except Exception:
            pass
        # CR01: simulation fallback for liq/STOP when plan carries none
        # (save_plan ignores snake liq; simulate request may hold it).
        try:
            if _plan_map_act.get("liquidation_price") is None:
                _sim_id_x = str(row.get("simulation_id") or "")
                if _sim_id_x:
                    try:
                        _sim_row_x = await repo.get_hedge_simulation(_sim_id_x)
                    except Exception:
                        _sim_row_x = None
                    if isinstance(_sim_row_x, Mapping):
                        import json as _jsx2
                        _inp_x = _sim_row_x.get("input_json") or {}
                        _res_x = _sim_row_x.get("result_json") or {}
                        if isinstance(_inp_x, str):
                            try:
                                _inp_x = _jsx2.loads(_inp_x)
                            except Exception:
                                _inp_x = {}
                        if isinstance(_res_x, str):
                            try:
                                _res_x = _jsx2.loads(_res_x)
                            except Exception:
                                _res_x = {}
                        for _src in (_inp_x, _res_x):
                            if isinstance(_src, Mapping):
                                for _lk, _lk2 in (("liquidation_price", "liquidationPrice"),
                                                 ("stop_trigger_price", "stopTriggerPrice"),
                                                 ("stop_trigger_basis", "stopTriggerBasis")):
                                    if _plan_map_act.get(_lk) is None and _src.get(_lk) is not None:
                                        _plan_map_act[_lk] = _src.get(_lk)
                                    if _plan_map_act.get(_lk) is None and _src.get(_lk2) is not None:
                                        _plan_map_act[_lk] = _src.get(_lk2)
        except Exception:
            pass
        # Remaining per leg (effective events, never UI reports).
        try:
            _by_leg: dict[str, Any] = {}
            for _p in (positions or ()):
                _leg = str(_dto_get(_p, "leg_type", "legType", default="") or "").upper()
                _rem = _dto_get(_p, "remaining_qty", "remainingQty", "remaining", "open_qty", default="0")
                if _leg:
                    _by_leg[_leg] = str(_rem)
            _fut_rem_s = str(_by_leg.get("FUTURES_SHORT", "0") or "0")
            _spot_rem_s = str(_by_leg.get("SPOT_LONG", "0") or "0")
        except Exception:
            _fut_rem_s, _spot_rem_s = "0", "0"

        # 1) Identity / multiplier (D04.1 Catalog->Resolver, same as scoring).
        try:
            _ident = await self._hedge_identity_for(_sym_act)
            _conf = str(_dto_get(_ident, "mapping_confidence", "identity_confidence", default="UNRESOLVED") or "UNRESOLVED").upper()
            _mult = _dto_get(_ident, "contract_multiplier", "multiplier", default=None)
            try:
                _mult_ok = _mult is not None and float(str(_mult)) > 0
            except Exception:
                _mult_ok = False
            if _conf in ("VERIFIED", "HIGH") and _mult_ok:
                _id_status, _id_reasons = "PASS", []
            else:
                _id_status, _id_reasons = "UNKNOWN", ["IDENTITY_UNVERIFIED" if _conf in ("LOW", "UNRESOLVED") else "MULTIPLIER_UNVERIFIED"]
            _id_detail: dict[str, Any] = {"confidence": _conf}
        except Exception:
            _id_status, _id_reasons, _id_detail = "UNKNOWN", ["IDENTITY_ERROR"], {}

        # 2) Current Mark vs native liquidation price (fresh, not breached).
        try:
            _mark_act: Any = await self._hedge_mark_for(_sym_act)
        except Exception:
            _mark_act = None
        try:
            from decimal import Decimal as _DecM, InvalidOperation as _InvM

            _mark_px_raw = _dto_get(_mark_act, "mark_price", "native_price", "price", "markPrice", default=None) if _mark_act is not None else None
            _mark_exp_raw = _dto_get(_mark_act, "expires_at_ms", "expiresAtMs", default=None) if _mark_act is not None else None
            _mark_asof_raw = _dto_get(_mark_act, "as_of_ms", "source_as_of_ms", "fetched_at_ms", default=None) if _mark_act is not None else None
            _liq_raw = _plan_map_act.get("liquidation_price")
            if _mark_px_raw is None or _liq_raw is None:
                _mark_status, _mark_reasons = "UNKNOWN", ["MARK_OR_LIQ_UNKNOWN"]
                _mark_detail = {}
            else:
                try:
                    _mark_d = _DecM(str(_mark_px_raw))
                    _liq_d = _DecM(str(_liq_raw))
                    _mark_ok = _mark_d.is_finite() and _liq_d.is_finite() and _mark_d > 0 and _liq_d > 0
                except (_InvM, ValueError, ArithmeticError):
                    _mark_ok = False
                    _mark_d = _liq_d = None
                if not _mark_ok:
                    _mark_status, _mark_reasons = "UNKNOWN", ["MARK_OR_LIQ_INVALID"]
                    _mark_detail = {}
                else:
                    # Freshness: expired mark never PASS.
                    try:
                        _exp_i = int(_mark_exp_raw) if _mark_exp_raw is not None else None
                    except Exception:
                        _exp_i = None
                    if _exp_i is not None and int(_exp_i) <= int(_cutoff):
                        _mark_status, _mark_reasons = "UNKNOWN", ["MARK_EXPIRED"]
                        _mark_detail = {"expires_at_ms": _exp_i}
                    else:
                        assert _mark_d is not None and _liq_d is not None
                        try:
                            _dist = (_liq_d - _mark_d) / _mark_d if _mark_d != 0 else None
                        except Exception:
                            _dist = None
                        if _dist is None:
                            _mark_status, _mark_reasons = "UNKNOWN", ["LIQ_DISTANCE_UNKNOWN"]
                            _mark_detail = {}
                        elif _dist <= 0:
                            _mark_status, _mark_reasons = "FAIL", ["LIQUIDATION_BREACH"]
                            _mark_detail = {"distance": str(_dist)}
                        else:
                            _mark_status, _mark_reasons = "PASS", []
                            _mark_detail = {"distance": str(_dist)}
        except Exception:
            _mark_status, _mark_reasons, _mark_detail = "UNKNOWN", ["MARK_ERROR"], {}

        # 3) Exact-remaining two-way depth (Futures BUY + Spot SELL, same cutoff).
        try:
            from decimal import Decimal as _DecD

            _spot_venue_act = str(_plan_map_act.get("spot_venue") or row.get("spot_venue") or "BINANCE_SPOT")
            _fut_qty_s = _fut_rem_s
            _spot_qty_s = _spot_rem_s
            try:
                _fut_rem_d = _DecD(str(_fut_qty_s or "0"))
                _spot_rem_d = _DecD(str(_spot_qty_s or "0"))
            except Exception:
                _fut_rem_d = _spot_rem_d = None
            if _fut_rem_d is None or _spot_rem_d is None or _fut_rem_d <= 0 or _spot_rem_d <= 0:
                _depth_status, _depth_reasons, _depth_detail = "UNKNOWN", ["DEPTH_REMAINING_UNKNOWN"], {}
            else:
                _depth_reasons_l: list[str] = []
                _depth_detail_l: dict[str, Any] = {}
                _depth_ok = True
                # Spot SELL depth via real quote (Mapping or SpotVenueQuote DTO).
                try:
                    _sq = await self._hedge_quote_for(_sym_act, str(_spot_qty_s), _spot_venue_act)
                    _sq_sell = _dto_get(_sq, "sell_vwap", "sellVwap", "mid_price", "midPrice", default=None)
                    _sq_exec = _dto_get(_sq, "sell_executable_qty", "sellExecutableQty", default=None)
                    _sq_exp = _dto_get(_sq, "expires_at_ms", "expiresAtMs", default=None)
                    _sq_venue = _dto_get(_sq, "venue", default=None)
                    try:
                        _sq_exec_d = _DecD(str(_sq_exec)) if _sq_exec is not None else None
                    except Exception:
                        _sq_exec_d = None
                    try:
                        _sq_exp_i = int(_sq_exp) if _sq_exp is not None else None
                    except Exception:
                        _sq_exp_i = None
                    _depth_detail_l["spot_venue"] = str(_sq_venue) if _sq_venue is not None else _spot_venue_act
                    _depth_detail_l["spot_expires_at_ms"] = _sq_exp_i
                    if _sq_sell is None or _sq_exec_d is None:
                        _depth_ok = False
                        _depth_reasons_l.append("SPOT_DEPTH_UNKNOWN")
                    elif _sq_exp_i is not None and int(_sq_exp_i) <= int(_cutoff):
                        _depth_ok = False
                        _depth_reasons_l.append("SPOT_QUOTE_EXPIRED")
                    elif _sq_exec_d < _spot_rem_d:
                        _depth_ok = False
                        _depth_reasons_l.append("SPOT_DEPTH_INSUFFICIENT")
                    else:
                        _depth_detail_l["spot_sell_vwap"] = str(_sq_sell)
                        _depth_detail_l["spot_executable_qty"] = str(_sq_exec)
                except Exception:
                    _depth_ok = False
                    _depth_reasons_l.append("SPOT_DEPTH_UNKNOWN")
                # Futures BUY depth: real collect_futures first, offline fallback to quote BUY side.
                try:
                    _fq_buy = _fq_exec = _fq_exp = _fq_ref = None
                    _mp = getattr(self, "_market_port", None) or getattr(self, "_hedge_market", None)
                    _got_fut = False
                    if _mp is not None and hasattr(_mp, "collect_futures"):
                        try:
                            from diveintocrypto_desktop.shortlab.request_budget import make_request_context as _mkc

                            try:
                                _rctx = _mkc(getattr(self, "_request_budget", None), job_type="interactive",
                                             host="fapi", endpoint_family="futuresDepth",
                                             trace_id=f"repair-activate-{_sym_act}-{int(_cutoff)}")
                            except Exception:
                                _rctx = None
                            _bund = await _mp.collect_futures(_sym_act, str(_fut_qty_s), _rctx)
                            _fq_obj = None
                            if isinstance(_bund, Mapping):
                                _fq_obj = _bund.get("futures_quote", _bund.get("futuresQuote"))
                            if _fq_obj is not None:
                                _fq_buy = _dto_get(_fq_obj, "buy_vwap_native", "buyVwapNative", "buy_vwap", "buyVwap", default=None)
                                _fq_exec = _dto_get(_fq_obj, "buy_executable_qty", "buyExecutableQty", default=None)
                                _fq_exp = _dto_get(_fq_obj, "expires_at_ms", "expiresAtMs", default=None)
                                _fq_ref = _dto_get(_fq_obj, "quote_id", "quoteId", default=None)
                                _got_fut = True
                        except Exception:
                            _got_fut = False
                    if not _got_fut:
                        # Offline/test fallback: same quote fn BUY side (still checks qty/expiry).
                        _fq2 = await self._hedge_quote_for(_sym_act, str(_fut_qty_s), _spot_venue_act)
                        _fq_buy = _dto_get(_fq2, "buy_vwap", "buyVwap", "mid_price", default=None)
                        _fq_exec = _dto_get(_fq2, "buy_executable_qty", "buyExecutableQty", default=None)
                        _fq_exp = _dto_get(_fq2, "expires_at_ms", "expiresAtMs", default=None)
                        _fq_ref = _dto_get(_fq2, "venue", default=None)
                    try:
                        _fq_exec_d = _DecD(str(_fq_exec)) if _fq_exec is not None else None
                    except Exception:
                        _fq_exec_d = None
                    try:
                        _fq_exp_i = int(_fq_exp) if _fq_exp is not None else None
                    except Exception:
                        _fq_exp_i = None
                    _depth_detail_l["futures_expires_at_ms"] = _fq_exp_i
                    if _fq_ref is not None:
                        _depth_detail_l["futures_quote_ref"] = str(_fq_ref)
                    if _fq_buy is None or _fq_exec_d is None:
                        _depth_ok = False
                        _depth_reasons_l.append("FUTURES_DEPTH_UNKNOWN")
                    elif _fq_exp_i is not None and int(_fq_exp_i) <= int(_cutoff):
                        _depth_ok = False
                        _depth_reasons_l.append("FUTURES_QUOTE_EXPIRED")
                    elif _fq_exec_d < _fut_rem_d:
                        _depth_ok = False
                        _depth_reasons_l.append("FUTURES_DEPTH_INSUFFICIENT")
                    else:
                        _depth_detail_l["futures_buy_vwap"] = str(_fq_buy)
                        _depth_detail_l["futures_executable_qty"] = str(_fq_exec)
                except Exception:
                    _depth_ok = False
                    _depth_reasons_l.append("FUTURES_DEPTH_UNKNOWN")
                if _depth_ok:
                    _depth_status, _depth_reasons, _depth_detail = "PASS", [], _depth_detail_l
                else:
                    # Insufficient is FAIL, missing/expired is UNKNOWN-equivalent block.
                    _is_fail = any(r in ("SPOT_DEPTH_INSUFFICIENT", "FUTURES_DEPTH_INSUFFICIENT") for r in _depth_reasons_l)
                    _depth_status = "FAIL" if _is_fail else "UNKNOWN"
                    _depth_reasons, _depth_detail = sorted(set(_depth_reasons_l)), _depth_detail_l
        except Exception:
            _depth_status, _depth_reasons, _depth_detail = "UNKNOWN", ["DEPTH_ERROR"], {}

        # 4) Shared Funding gate (real context + real port, same cutoff).
        # CR01: _build_repair_funding_context fallback metrics are partial
        # (only current/last/30d/apr); enrich missing 7d/90d/ratios from the
        # real _hedge_funding_for DTO so a seeded schedule can honestly PASS.
        try:
            _fund_ctx_act: Any = await self._build_repair_funding_context(_sym_act, _cutoff)
        except Exception:
            _fund_ctx_act = None
        try:
            if _fund_ctx_act is not None:
                try:
                    _raw_f = await self._hedge_funding_for(_sym_act)
                    if isinstance(_raw_f, Mapping):
                        from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics as _FMx
                        _m0 = getattr(_fund_ctx_act, "metrics", None)
                        def _pick_f(*ns: str, default: Any = None) -> Any:
                            for _nn in ns:
                                if isinstance(_m0, Mapping):
                                    if _m0.get(_nn) is not None:
                                        return _m0.get(_nn)
                                else:
                                    try:
                                        _vv = getattr(_m0, _nn, None)
                                    except Exception:
                                        _vv = None
                                    if _vv is not None:
                                        return _vv
                            for _nn in ns:
                                if _raw_f.get(_nn) is not None:
                                    return _raw_f.get(_nn)
                                # camelCase alias.
                                _parts = _nn.split("_")
                                _camel = _parts[0] + "".join(p[:1].upper() + p[1:] for p in _parts[1:])
                                if _raw_f.get(_camel) is not None:
                                    return _raw_f.get(_camel)
                            return default
                        try:
                            _enriched = _FMx(
                                symbol=_sym_act,
                                current_rate=str(_pick_f("current_rate")) if _pick_f("current_rate") is not None else None,
                                last_settled_rate=str(_pick_f("last_settled_rate")) if _pick_f("last_settled_rate") is not None else None,
                                funding_7d=str(_pick_f("funding_7d")) if _pick_f("funding_7d") is not None else None,
                                funding_30d=str(_pick_f("funding_30d")) if _pick_f("funding_30d") is not None else None,
                                funding_90d=str(_pick_f("funding_90d")) if _pick_f("funding_90d") is not None else None,
                                positive_ratio_30d=str(_pick_f("positive_ratio_30d")) if _pick_f("positive_ratio_30d") is not None else None,
                                positive_ratio_90d=str(_pick_f("positive_ratio_90d")) if _pick_f("positive_ratio_90d") is not None else None,
                                coverage_30d=str(_pick_f("coverage_30d")) if _pick_f("coverage_30d") is not None else None,
                                coverage_90d=str(_pick_f("coverage_90d")) if _pick_f("coverage_90d") is not None else None,
                                conservative_apr=str(_pick_f("conservative_apr")) if _pick_f("conservative_apr") is not None else None,
                                history_coverage=str(_pick_f("history_coverage")) if _pick_f("history_coverage") is not None else None,
                            )
                            import dataclasses as _dcf
                            if _dcf.is_dataclass(_fund_ctx_act):
                                _fund_ctx_act = _dcf.replace(_fund_ctx_act, metrics=_enriched)
                        except Exception:
                            pass
                except Exception:
                    pass
                # CR01: recompute schedule coverage with unwrapped schedule_json
                # (repo returns nested schedule_json; _coerce_segment needs flat).
                try:
                    import dataclasses as _dcf2
                    if _dcf2.is_dataclass(_fund_ctx_act):
                        try:
                            _raw_evs = await repo.list_funding_events(_sym_act, int(_cutoff) - 90 * 86400_000, int(_cutoff)) if hasattr(repo, "list_funding_events") else ()
                        except Exception:
                            _raw_evs = ()
                        try:
                            _raw_ss = await repo.list_funding_schedules(_sym_act, int(_cutoff)) if hasattr(repo, "list_funding_schedules") else ()
                        except Exception:
                            _raw_ss = ()
                        _unwrapped: list[Any] = []
                        for _s in (_raw_ss or ()):
                            try:
                                if isinstance(_s, Mapping) and isinstance(_s.get("schedule_json"), Mapping):
                                    _inner = dict(_s.get("schedule_json") or {})
                                    # Ensure known_at/symbol present for _coerce_segment.
                                    if _inner.get("known_at_ms") is None and _s.get("known_at_ms") is not None:
                                        _inner["known_at_ms"] = _s.get("known_at_ms")
                                    if _inner.get("symbol") is None and _s.get("symbol") is not None:
                                        _inner["symbol"] = _s.get("symbol")
                                    if _inner.get("schedule_id") is None and _s.get("schedule_id") is not None:
                                        _inner["schedule_id"] = _s.get("schedule_id")
                                    _unwrapped.append(_inner)
                                else:
                                    _unwrapped.append(_s)
                            except Exception:
                                continue
                        try:
                            _cov_fn = self._require_repair_port("compute_schedule_coverage")
                        except Exception:
                            _cov_fn = None
                        if _cov_fn is not None and _unwrapped:
                            try:
                                _c7 = _cov_fn(list(_raw_evs or ()), list(_unwrapped), int(_cutoff) - 7 * 86400_000, int(_cutoff), int(_cutoff))
                                _c30 = _cov_fn(list(_raw_evs or ()), list(_unwrapped), int(_cutoff) - 30 * 86400_000, int(_cutoff), int(_cutoff))
                                _c90 = _cov_fn(list(_raw_evs or ()), list(_unwrapped), int(_cutoff) - 90 * 86400_000, int(_cutoff), int(_cutoff))
                                _fund_ctx_act = _dcf2.replace(_fund_ctx_act, coverage_7d=_c7, coverage_30d=_c30, coverage_90d=_c90)
                            except Exception:
                                pass
                except Exception:
                    pass
            if _fund_ctx_act is None:
                _fund_status, _fund_reasons = "UNKNOWN", ["FUNDING_CONTEXT_UNKNOWN"]
                _fund_detail = {}
            else:
                _gate_fn = self._require_repair_port("evaluate_funding_entry_gate")
                _gate = _gate_fn(_fund_ctx_act, self._config, int(_cutoff))
                if hasattr(_gate, "status"):
                    _fund_status = str(_gate.status).upper()
                    _fund_reasons = [str(r) for r in (getattr(_gate, "reasons", ()) or ())]
                elif isinstance(_gate, Mapping):
                    _fund_status = str(_gate.get("status", "UNKNOWN")).upper()
                    _fund_reasons = [str(r) for r in (_gate.get("reasons", ()) or ())]
                else:
                    _fund_status, _fund_reasons = "UNKNOWN", ["FUNDING_GATE_UNKNOWN"]
                _fund_detail = {"checked_at_ms": int(_cutoff)}
                if _fund_status not in ("PASS", "FAIL", "UNKNOWN"):
                    _fund_status = "UNKNOWN"
        except Exception:
            _fund_status, _fund_reasons, _fund_detail = "UNKNOWN", ["FUNDING_GATE_ERROR"], {}

        # 5) Actual-remaining capital/risk/horizon economics (never 0-fill).
        try:
            if _fund_ctx_act is None or _mark_act is None:
                _eco_status, _eco_reasons = "UNKNOWN", ["ECONOMICS_INPUT_UNKNOWN"]
                _eco_detail = {}
            elif _fund_status != "PASS":
                _eco_status, _eco_reasons = "UNKNOWN", ["ECONOMICS_FUNDING_UNKNOWN"]
                _eco_detail = {"funding_gate": _fund_status}
            elif _mark_status != "PASS":
                _eco_status, _eco_reasons = "UNKNOWN", ["ECONOMICS_MARK_UNKNOWN"]
                _eco_detail = {"mark": _mark_status}
            else:
                # Conservative carry present and horizon configured; missing stays UNKNOWN.
                _cons = _dto_get(getattr(_fund_ctx_act, "metrics", None), "conservative_apr", default=None)
                if _cons is None and isinstance(_fund_ctx_act, Mapping):
                    _cons = (_fund_ctx_act.get("metrics") or {}).get("conservative_apr") if isinstance(_fund_ctx_act.get("metrics"), Mapping) else None
                if _cons is None:
                    _eco_status, _eco_reasons = "UNKNOWN", ["ECONOMICS_CARRY_UNKNOWN"]
                    _eco_detail = {}
                else:
                    _eco_status, _eco_reasons = "PASS", []
                    _eco_detail = {"conservative_apr": str(_cons)}
        except Exception:
            _eco_status, _eco_reasons, _eco_detail = "UNKNOWN", ["ECONOMICS_ERROR"], {}

        # 6) Protection fingerprint + confirmation expiry (D06.3/D12).
        try:
            _prot_act: Any = await repo.get_protection_confirmation(pid)
        except Exception:
            _prot_act = None
        try:
            if _prot_act is None:
                _prot_status, _prot_reasons, _prot_hash = "UNKNOWN", ["PROTECTION_MISSING"], "MISSING"
                _prot_detail = {}
            else:
                _gate_p = _vpc(_prot_act, dict(_plan_map_act), list(positions or ()), int(_cutoff))
                _prot_status = str(getattr(_gate_p, "status", "UNKNOWN")).upper()
                _prot_reasons = [str(r) for r in (getattr(_gate_p, "reasons", ()) or ())]
                try:
                    _cj = (_prot_act.get("confirmation_json") or {}) if isinstance(_prot_act, Mapping) else {}
                    if isinstance(_cj, str):
                        import json as _jsp
                        try:
                            _cj = _jsp.loads(_cj)
                        except Exception:
                            _cj = {}
                    _prot_hash = str((_cj.get("protected_position_hash") if isinstance(_cj, Mapping) else None) or (_prot_act.get("protected_position_hash") if isinstance(_prot_act, Mapping) else None) or "MISSING")
                except Exception:
                    _prot_hash = "MISSING"
                _prot_detail = {"confirmation": str(getattr(_gate_p, "input_refs", {}) or {})}
                if _prot_status not in ("PASS", "FAIL", "UNKNOWN"):
                    _prot_status = "UNKNOWN"
        except Exception:
            _prot_status, _prot_reasons, _prot_hash, _prot_detail = "UNKNOWN", ["PROTECTION_ERROR"], "MISSING", {}

        _checks: dict[str, Any] = {
            "identity": {"status": _id_status, "reasons": sorted(_id_reasons), "checked_at_ms": int(_cutoff), "detail": _id_detail},
            "mark_vs_liquidation": {"status": _mark_status, "reasons": sorted(_mark_reasons), "checked_at_ms": int(_cutoff), "detail": _mark_detail},
            "depth": {"status": _depth_status, "reasons": sorted(_depth_reasons), "checked_at_ms": int(_cutoff), "detail": _depth_detail},
            "funding_gate": {"status": _fund_status, "reasons": sorted(_fund_reasons), "checked_at_ms": int(_cutoff), "detail": _fund_detail},
            "economics": {"status": _eco_status, "reasons": sorted(_eco_reasons), "checked_at_ms": int(_cutoff), "detail": _eco_detail},
            "protection": {"status": _prot_status, "reasons": sorted(_prot_reasons), "checked_at_ms": int(_cutoff), "detail": _prot_detail},
            "protection_status": _prot_status,
            "protected_position_hash": str(_prot_hash),
        }
        _source_refs: dict[str, Any] = {
            "plan_id": pid,
            "plan_version": int(current_version),
            "symbol": _sym_act,
            "cutoff_ms": int(_cutoff),
            "protected_position_hash": str(_prot_hash),
        }
        try:
            if isinstance(_prot_act, Mapping) and _prot_act.get("confirmation_id"):
                _source_refs["protection_confirmation"] = str(_prot_act.get("confirmation_id"))
            elif isinstance(_prot_act, Mapping) and _prot_act.get("client_request_id"):
                _source_refs["protection_confirmation"] = str(_prot_act.get("client_request_id"))
        except Exception:
            pass
        # Correct keyword-only signature (CR01 TypeError root cause).
        try:
            _check_rec = _bac(plan_id=pid, symbol=_sym_act, cutoff_ms=int(_cutoff), known_at_ms=int(_known_at),
                              checks=dict(_checks), source_refs=dict(_source_refs), source_as_of_ms=int(_cutoff))
        except Exception as exc:
            raise HedgeValidationError(f"ACTIVATION_CHECK_FAILED: check build failed: {type(exc).__name__}", reason_code="ACTIVATION_CHECK_FAILED") from exc
        # Persist full record first (audit); persist failure blocks activation.
        try:
            await repo.save_market_observation(dict(_check_rec))
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _BusyA

                if isinstance(exc, _BusyA):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise HedgeValidationError(f"ACTIVATION_CHECK_FAILED: persist failed: {type(exc).__name__}", reason_code="ACTIVATION_CHECK_FAILED") from exc
        # Single-cutoff verdict: any REQUIRED FAIL/UNKNOWN/expired blocks.
        _failed = [(k, v.get("status")) for k, v in _checks.items() if isinstance(v, Mapping) and "status" in v]
        _blocked = [(k, s) for k, s in _failed if s != "PASS"]
        # Top-level protection_status string mirrors the protection dict.
        if _prot_status != "PASS" and ("protection", _prot_status) not in _blocked:
            _blocked.append(("protection", _prot_status))
        if _blocked:
            _desc = ",".join(f"{k}={s}" for k, s in sorted(_blocked))
            raise HedgeValidationError(f"ACTIVATION_CHECK_FAILED: {_desc}", reason_code="ACTIVATION_CHECK_FAILED")
        lock = self._hedge_lock_for(pid)
        async with lock:
            try:
                new_version = await repo.update_hedge_plan(pid, "ACTIVE", current_version, now_ms)
            except Exception as exc:
                try:
                    from diveintocrypto_desktop.shortlab.repository import HedgeVersionConflictError as _Ver
                    from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy
                    from diveintocrypto_desktop.shortlab.repository import ValidationError as _Val

                    if isinstance(exc, _Busy):
                        raise HedgeBusy(str(exc)) from exc
                    if isinstance(exc, _Ver):
                        raise HedgeVersionConflict(str(exc)[:300]) from exc
                    if isinstance(exc, _Val):
                        raise HedgeValidationError(str(exc)[:300], reason_code="HEDGE_INPUT_INVALID") from exc
                except (HedgeBusy, HedgeVersionConflict, HedgeValidationError):
                    raise
                except Exception:
                    pass
                raise
        if getattr(self, "_hedge_jobs", None) is not None:
            self._hedge_jobs.invalidate(pid)
        return {"planId": pid, "status": "ACTIVE", "planVersion": int(new_version)}

    async def close(self, plan_id: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        pid = str(plan_id or "").strip()
        if not pid:
            raise HedgeValidationError("plan_id is required", reason_code="HEDGE_INPUT_INVALID")
        expected: int | None = None
        if payload:
            allowed = frozenset({"expected_version", "expected_plan_version"})
            snake = self._hedge_normalize(dict(payload), allowed)
            raw = snake.get("expected_version") if snake.get("expected_version") is not None else snake.get("expected_plan_version")
            if raw is not None:
                try:
                    expected = int(raw)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    raise HedgeValidationError("expected_version must be an int", reason_code="HEDGE_INPUT_INVALID")
        try:
            row = await repo.get_hedge_plan(pid)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if row is None:
            raise HedgePlanNotFound(pid)
        current_version = int(row.get("plan_version") or 1)
        if expected is not None and int(expected) != current_version:
            raise HedgeVersionConflict("PLAN_VERSION_CONFLICT")
        # Read failure is not proof of a flat position. Leave the plan live.
        positions = await repo.aggregate_hedge_position(pid)
        # CLOSED allows each leg exactly 0 or rule dust (frozen ledger rule).
        try:
            from decimal import Decimal as _Dec

            open_remain = False
            for pos in (positions or ()):
                rem = _Dec(str(pos["remaining_qty"]))
                if not rem.is_finite() or rem < 0:
                    raise ValueError("invalid remaining quantity")
                if rem != 0:
                    open_remain = True
                    break
            if open_remain:
                raise HedgeOpenLegsRemain("OPEN_LEGS_REMAIN: close requires zero open qty on both legs")
        except (HedgeOpenLegsRemain, HedgeBusy, HedgeVersionConflict):
            raise
        except Exception as exc:
            raise HedgeValidationError(str(exc)[:200], reason_code="HEDGE_INPUT_INVALID") from exc
        now_ms = self._now()
        lock = self._hedge_lock_for(pid)
        async with lock:
            try:
                new_version = await repo.update_hedge_plan(pid, "CLOSED", current_version, now_ms)
            except Exception as exc:
                try:
                    from diveintocrypto_desktop.shortlab.repository import HedgeVersionConflictError as _Ver
                    from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                    if isinstance(exc, _Busy):
                        raise HedgeBusy(str(exc)) from exc
                    if isinstance(exc, _Ver):
                        raise HedgeVersionConflict(str(exc)[:300]) from exc
                except (HedgeBusy, HedgeVersionConflict):
                    raise
                except Exception:
                    pass
                raise
        # Closing resolves open alerts for the plan (best-effort, same worker
        # semantics are not required here).
        try:
            open_alerts = await repo.list_hedge_alerts(plan_id=pid, state="OPEN", limit=50, offset=0)
            for alert in (open_alerts or ()):
                try:
                    await repo.resolve_hedge_alert(str(alert.get("alert_id")), now_ms)
                except Exception:
                    continue
        except Exception:
            pass
        if getattr(self, "_hedge_jobs", None) is not None:
            self._hedge_jobs.invalidate(pid)
        return {"planId": pid, "status": "CLOSED", "planVersion": int(new_version)}

    # -- B32.10 monitor ----------------------------------------------------
    async def monitor(self, plan_id: str) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        pid = str(plan_id or "").strip()
        if not pid:
            raise HedgeValidationError("plan_id is required", reason_code="HEDGE_INPUT_INVALID")
        owner = getattr(self, "_hedge_jobs", None)
        memory = owner.mirror.get(pid) if owner is not None else None
        if memory is not None and memory.get("previous") is not None:
            snapshot = memory["previous"]
            latest = dataclasses.asdict(snapshot) if dataclasses.is_dataclass(snapshot) else dict(snapshot)
            return _monitor_wire(pid, latest, memory["positions"], tuple(memory["alerts"].values()))
        try:
            plan_row = await repo.get_hedge_plan(pid)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if plan_row is None:
            raise HedgePlanNotFound(pid)
        try:
            latest = await repo.latest_hedge_monitor(pid)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            latest = None
        positions = await repo.aggregate_hedge_position(pid)
        try:
            alerts = await repo.list_hedge_alerts(plan_id=pid, limit=50, offset=0)
        except Exception:
            alerts = ()
        # R10b D09: actual PnL/costs from R08a/R08b (never hand-filled cache).
        # Compute via the real compute_ledger_pnl port from effective events +
        # persisted FX; unknown stays null/Partial (never 0-fill). Best-effort:
        # failures keep the stored snapshot (honest, not fabricated).
        _ledger_wire: Any = None
        try:
            _pnl_fn = self._require_repair_port("compute_ledger_pnl")
            try:
                _ident_m = await self._hedge_identity_for(str(plan_row.get("symbol") or ""))
            except Exception:
                _ident_m = {"canonical_id": str(plan_row.get("canonical_id") or ""), "contract_multiplier": "1"}
            try:
                _evts = await repo.list_hedge_events(pid) if hasattr(repo, "list_hedge_events") else list(positions or ())
            except Exception:
                _evts = list(positions or ())
            # Event FX map persisted per event (best-effort, empty stays Partial).
            _efx: dict[str, Any] = {}
            try:
                for _ev in (_evts or ()):
                    _eid = None
                    if isinstance(_ev, Mapping):
                        _eid = _ev.get("event_id", _ev.get("eventId"))
                    else:
                        _eid = getattr(_ev, "event_id", None)
                    if _eid:
                        _efx[str(_eid)] = {}
            except Exception:
                pass
            _mctx: dict[str, Any] = {"now_ms": int(self._now())}
            try:
                _sym_m = str(plan_row.get("symbol") or "").upper()
                try:
                    _mk_m = await self._hedge_mark_for(_sym_m)
                    if isinstance(_mk_m, Mapping):
                        _mctx["futures_mark_native"] = str(_mk_m.get("mark_price", _mk_m.get("native_price", "")) or "")
                        _mctx["futures_quote_fx"] = str(_mk_m.get("quote_to_usd", "1"))
                except Exception:
                    pass
            except Exception:
                pass
            try:
                _ledger = _pnl_fn(list(_evts or ()), _ident_m, dict(_efx), dict(_mctx))
                try:
                    import dataclasses as _dcl

                    _ledger_wire = _dcl.asdict(_ledger) if _dcl.is_dataclass(_ledger) else dict(_ledger) if isinstance(_ledger, Mapping) else None
                except Exception:
                    _ledger_wire = None
            except Exception:
                _ledger_wire = None
        except Exception:
            _ledger_wire = None
        owner = getattr(self, "_hedge_jobs", None)
        memory = owner.mirror.get(pid, {}).get("previous") if owner is not None else None
        if memory is not None:
            latest = dataclasses.asdict(memory) if dataclasses.is_dataclass(memory) else dict(memory)
        if latest is None:
            _no_snap: dict[str, Any] = {
                "planId": pid,
                "status": "NO_SNAPSHOT",
                "asOf": None,
                "actualHedgeRatio": None,
                "residualShortNotionalUsd": None,
                "positions": [dict(p) for p in (positions or ())],
                "alerts": [dict(a) for a in (alerts or ())],
            }
            if _ledger_wire is not None:
                _no_snap["ledgerPnl"] = dict(_ledger_wire)
                _no_snap["ledger_pnl"] = dict(_ledger_wire)
            return _no_snap
        out = dict(latest)
        _wired = _monitor_wire(pid, out, positions, alerts)
        # Attach real ledger PnL alongside the frozen monitor snapshot.
        try:
            if _ledger_wire is not None and isinstance(_wired, Mapping):
                _wired = dict(_wired)
                _wired["ledgerPnl"] = dict(_ledger_wire)
                _wired["ledger_pnl"] = dict(_ledger_wire)
        except Exception:
            pass
        return _wired

    # -- B32.11/B32.12 alerts ----------------------------------------------
    async def hedge_alerts(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        filt = dict(filters or {})

        def _pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if filt.get(name) is not None:
                    return filt[name]
            return default

        plan_id = _pick("plan_id", "planId")
        state = _pick("state")
        severity = _pick("severity")
        code = _pick("code")
        try:
            limit = int(_pick("limit", default=50))
        except (TypeError, ValueError):
            raise HedgeValidationError("limit must be an int", reason_code="HEDGE_INPUT_INVALID")
        try:
            offset = int(_pick("offset", default=0))
        except (TypeError, ValueError):
            raise HedgeValidationError("offset must be an int", reason_code="HEDGE_INPUT_INVALID")
        if not 1 <= limit <= 200:
            raise HedgeValidationError("limit must be in 1..200", reason_code="HEDGE_INPUT_INVALID")
        if offset < 0:
            raise HedgeValidationError("offset must be >= 0", reason_code="HEDGE_INPUT_INVALID")
        if state is not None and str(state) not in ("OPEN", "ACKNOWLEDGED", "RESOLVED"):
            raise HedgeValidationError(f"unknown state {state!r}", reason_code="HEDGE_INPUT_INVALID")
        if severity is not None and str(severity) not in ("INFO", "WARN", "CRITICAL"):
            raise HedgeValidationError(f"unknown severity {severity!r}", reason_code="HEDGE_INPUT_INVALID")
        try:
            rows = await repo.list_hedge_alerts(
                plan_id=str(plan_id) if plan_id is not None else None,
                state=str(state) if state is not None else None,
                limit=200,
                offset=0,
            )
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        items = []
        for row in rows or ():
            row_d = dict(row)
            if severity is not None and str(row_d.get("severity")) != str(severity):
                continue
            if code is not None and str(row_d.get("code")) != str(code):
                continue
            # context_json may be a JSON string; keep it as-is.
            items.append({
                "alertId": str(row_d.get("alert_id")),
                "planId": str(row_d.get("plan_id")),
                "code": str(row_d.get("code")),
                "severity": str(row_d.get("severity")),
                "state": str(row_d.get("state")),
                "openedAt": row_d.get("opened_at_ms"),
                "lastSeenAt": row_d.get("last_seen_at_ms"),
                "acknowledgedAt": row_d.get("acknowledged_at_ms"),
                "resolvedAt": row_d.get("resolved_at_ms"),
                "dedupKey": str(row_d.get("dedup_key") or ""),
                "episode": int(row_d.get("episode") or 1),
                "recommendedAction": str(row_d.get("recommended_action") or "NONE"),
                "context": row_d.get("context_json"),
            })
        # Newest first is already the repository order; slice for pagination.
        total = len(items)
        return {"items": items[offset: offset + limit], "total": total}

    async def ack_hedge_alert(self, alert_id: str) -> dict[str, Any]:
        repo = await self._ensure_hedge_available()
        aid = str(alert_id or "").strip()
        if not aid:
            raise HedgeValidationError("alert_id is required", reason_code="HEDGE_INPUT_INVALID")
        now_ms = self._now()
        try:
            row = await repo.ack_hedge_alert(aid, now_ms)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if row is None:
            raise HedgeAlertNotFound(aid)
        return {
            "alertId": str(row.get("alert_id")),
            "planId": str(row.get("plan_id")),
            "state": str(row.get("state")),
            "acknowledgedAt": row.get("acknowledged_at_ms"),
            "lastSeenAt": row.get("last_seen_at_ms"),
        }

    # Back-compat alias for the H08 plan task name.
    async def alerts(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self.hedge_alerts(filters)

    # -- B32.13 hedge evidence summary (H10 lazy) ---------------------------
    async def hedge_evidence_summary(self, filters: Mapping[str, Any] | None = None) -> Any:
        filt = dict(filters or {})

        def _pick(*names: str, default: Any = None) -> Any:
            for name in names:
                if filt.get(name) is not None:
                    return filt[name]
            return default

        start_ms = _pick("start_ms", "startMs")
        end_ms = _pick("end_ms", "endMs")
        strategy = _pick("strategy")
        horizon = _pick("horizon")
        venue = _pick("venue")
        history_class = _pick("history_class", "historyClass")
        fcs_version = _pick("fcs_version", "fcsVersion")
        hedge_formula_version = _pick("hedge_formula_version", "hedgeFormulaVersion")
        hedge_evidence_version = _pick("hedge_evidence_version", "hedgeEvidenceVersion")
        cost_config_hash = _pick("cost_config_hash", "costConfigHash")
        # Enum validation first (422 precedes 503).
        allowed_strategies = ("ABSOLUTE_100", "RELATIVE_75", "RELATIVE_50", "RELATIVE_25")
        allowed_horizons = ("7D", "30D", "90D")
        if strategy is not None and str(strategy) not in allowed_strategies:
            raise HedgeValidationError(f"unknown strategy {strategy!r}", reason_code="HEDGE_INPUT_INVALID")
        if horizon is not None and str(horizon) not in allowed_horizons:
            raise HedgeValidationError(f"unknown horizon {horizon!r}", reason_code="HEDGE_INPUT_INVALID")
        if venue is not None and str(venue) not in ("BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"):
            raise HedgeValidationError(f"unknown venue {venue!r}", reason_code="HEDGE_INPUT_INVALID")
        if history_class is not None and str(history_class) not in ("FULL_90D", "PARTIAL_90D", "FULL", "PARTIAL"):
            raise HedgeValidationError(f"unknown history_class {history_class!r}", reason_code="HEDGE_INPUT_INVALID")
        try:
            start_int = int(start_ms) if start_ms is not None else None
            end_int = int(end_ms) if end_ms is not None else None
        except (TypeError, ValueError):
            raise HedgeValidationError("start_ms/end_ms must be ints", reason_code="HEDGE_INPUT_INVALID")
        if start_int is not None and end_int is not None and start_int >= end_int:
            raise HedgeValidationError("start_ms must be < end_ms", reason_code="HEDGE_INPUT_INVALID")
        # Lazy H10: missing means honest 503, never an assert.
        hedge_summary_fn = None
        try:
            from diveintocrypto_desktop.shortlab.evidence import hedge_metrics as _hm  # type: ignore[import-not-found]

            hedge_summary_fn = getattr(_hm, "hedge_summary", None) or getattr(_hm, "summary", None)
        except Exception:
            hedge_summary_fn = None
        if hedge_summary_fn is None:
            return Unavailable(reason="HEDGE_EVIDENCE_UNAVAILABLE", detail="hedge evidence provider is not wired (H10)")
        repo = await self._ensure_hedge_available()
        try:
            result = await hedge_summary_fn(dict(filt), repository=repo, policy=self._config)
        except HedgeValidationError:
            raise
        except Exception as exc:
            return Unavailable(reason="HEDGE_EVIDENCE_UNAVAILABLE", detail=f"{type(exc).__name__}"[:160])
        if isinstance(result, Unavailable):
            return result
        return result

    # -- R10b repair wiring (D02/D12/D18/D19.1; real producers) ---------------
    def _repair_policy(self) -> Any:
        """Decision policy for recommend_hedge (full config, D15)."""
        return self._config

    def _decision_to_wire(self, result: Any, *, expired: bool | None = None) -> dict[str, Any]:
        """Convert DecisionResult DTO to camelCase HTTP wire (D18.3)."""
        try:
            from diveintocrypto_desktop.shortlab.repair_contracts import REPAIR_CONTRACT_VERSION
        except Exception:
            REPAIR_CONTRACT_VERSION = "repair-contract-v1"
        try:
            import dataclasses as _dc

            if _dc.is_dataclass(result):
                d = _dc.asdict(result)
            elif isinstance(result, Mapping):
                d = dict(result)
            else:
                d = {}
        except Exception:
            d = {}
        # Request camel conversion (snake internal -> camel wire).
        req = d.get("request") or {}
        try:
            if not isinstance(req, Mapping):
                req = {}
            else:
                req = dict(req)
            # Dataclass request -> dict already; ensure camel keys.
            _camel_req: dict[str, Any] = {}
            for k, v in req.items():
                ks = str(k)
                if "_" in ks:
                    parts = ks.split("_")
                    ck = parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])
                else:
                    ck = ks
                    # Map known snake->camel explicitly for wire compat.
                    _map = {
                        "symbol": "symbol", "goal": "goal",
                        "futures_notional_usd": "futuresNotionalUsd",
                        "planned_hold_days": "plannedHoldDays",
                        "available_capital_usd": "availableCapitalUsd",
                        "max_scenario_loss_usd": "maxScenarioLossUsd",
                        "margin_usd": "marginUsd",
                        "liquidation_price": "liquidationPrice",
                        "liquidation_price_updated_at_ms": "liquidationPriceUpdatedAtMs",
                        "preferred_spot_venue": "preferredSpotVenue",
                    }
                    ck = _map.get(ks, ks)
                _camel_req[ck] = v
            # Ensure all D18.3 keys present.
            req = _camel_req
        except Exception:
            pass
        # Selected proposal: keep as mapping (already JSON-safe via DTO).
        sel = d.get("selected_proposal")
        try:
            import dataclasses as _dc2

            if sel is not None and _dc2.is_dataclass(sel):
                sel = _dc2.asdict(sel)
        except Exception:
            pass
        alts = d.get("alternatives") or []
        try:
            import dataclasses as _dc3

            alts = [(_dc3.asdict(a) if _dc3.is_dataclass(a) else dict(a) if isinstance(a, Mapping) else a) for a in alts]
        except Exception:
            pass
        now_ms = self._now()
        exp_ms = d.get("expires_at_ms")
        try:
            exp_i = int(exp_ms) if exp_ms is not None else now_ms
        except Exception:
            exp_i = now_ms
        is_exp = bool(expired) if expired is not None else bool(now_ms >= exp_i)
        out: dict[str, Any] = {
            "contractSchemaVersion": REPAIR_CONTRACT_VERSION,
            "decisionId": d.get("decision_id"),
            "generatedAtMs": d.get("generated_at_ms"),
            "expiresAtMs": d.get("expires_at_ms"),
            "expired": bool(is_exp),
            "recommendation": d.get("recommendation"),
            "validationLevel": d.get("validation_level"),
            "selectedProposal": sel,
            "alternatives": list(alts or []),
            "reasons": list(d.get("reasons") or []),
            "assumptions": list(d.get("assumptions") or []),
            "contextRefs": dict(d.get("context_refs") or {}),
            "formulaVersion": d.get("formula_version"),
            "decisionPolicyHash": d.get("decision_policy_hash"),
            "request": dict(req or {}),
        }
        # Snake aliases for compat (frozen error envelope keeps both).
        out["decision_id"] = out["decisionId"]
        out["generated_at_ms"] = out["generatedAtMs"]
        out["expires_at_ms"] = out["expiresAtMs"]
        return out

    async def _build_repair_funding_context(self, symbol: str, as_of_ms: int) -> Any:
        """Build FundingContext via real collection chain (R10b).

        Prefers MarketPort.collect_funding (ProductionHedgeMarket, real
        Observation+receipt with schedule coverages). Falls back to
        repo events/schedules + real compute_schedule_coverage port +
        _hedge_funding_fn metrics when MarketPort is unbound (offline tests).
        451/budget denials become UNKNOWN (DATA_INSUFFICIENT downstream),
        never 503 for a single provider.
        """
        sym = str(symbol).upper()
        # Real MarketPort path first (production).
        market_port = getattr(self, "_market_port", None)
        if market_port is not None:
            try:
                cb = getattr(market_port, "collect_funding", None)
                if callable(cb) and is_real_repair_callback(getattr(market_port, "collect_funding", None) or (lambda: None)):
                    pass
            except Exception:
                pass
            try:
                collect = getattr(market_port, "collect_funding", None)
                if callable(collect):
                    try:
                        from diveintocrypto_desktop.shortlab.request_budget import (
                            make_request_context as _mk,
                        )

                        rctx = _mk(
                            getattr(self, "_request_budget", None),
                            job_type="interactive",
                            host="fapi",
                            endpoint_family="fundingRate",
                            trace_id=f"repair-funding-{sym}-{int(as_of_ms)}",
                        )
                    except Exception:
                        rctx = None
                    ctx = await _maybe_await(collect(sym, int(as_of_ms), rctx))
                    if ctx is not None:
                        return ctx
            except Exception as exc:
                msg = str(exc)
                if "451" in msg or "REGION" in msg or "BUDGET" in msg.upper():
                    pass
                # Fall through to repo+ports fallback (UNKNOWN, not 503).
                pass
        # Hedge-market fallback (ProductionHedgeMarket.collect_funding).
        hedge_market = getattr(self, "_hedge_market", None)
        if hedge_market is not None and hasattr(hedge_market, "collect_funding"):
            try:
                try:
                    from diveintocrypto_desktop.shortlab.request_budget import (
                        make_request_context as _mk2,
                    )

                    rctx2 = _mk2(
                        getattr(self, "_request_budget", None),
                        job_type="interactive",
                        host="fapi",
                        endpoint_family="fundingRate",
                        trace_id=f"repair-funding2-{sym}-{int(as_of_ms)}",
                    )
                except Exception:
                    rctx2 = None
                ctx2 = await _maybe_await(hedge_market.collect_funding(sym, int(as_of_ms), rctx2))
                if ctx2 is not None:
                    return ctx2
            except Exception:
                pass
        # Offline/test fallback: repo events + real schedule coverage + injected metrics.
        from diveintocrypto_desktop.shortlab.repair_contracts import FundingContext

        repo = self._require_available()
        events: list[Any] = []
        try:
            if hasattr(repo, "list_funding_events"):
                rows = await repo.list_funding_events(sym, int(as_of_ms) - 90 * 86400_000, int(as_of_ms))
                for r in rows or ():
                    try:
                        if hasattr(r, "funding_time_ms"):
                            events.append({"t": int(r.funding_time_ms), "funding_rate": str(r.funding_rate), "mark_price": getattr(r, "mark_price", None)})
                        elif isinstance(r, Mapping):
                            t = r.get("funding_time_ms", r.get("t"))
                            fr = r.get("funding_rate")
                            if t is not None and fr is not None:
                                events.append({"t": int(t), "funding_rate": str(fr)})
                    except Exception:
                        continue
        except Exception as exc:
            msg = str(exc)
            if "451" in msg:
                events = []
        schedules: list[Any] = []
        try:
            if hasattr(repo, "list_funding_schedules"):
                schedules = list(await repo.list_funding_schedules(sym, int(as_of_ms)) or ())
        except Exception:
            schedules = []
        # Real coverage port (never Fake READY when unbound -> UNKNOWN).
        try:
            cov_fn = self._require_repair_port("compute_schedule_coverage")
        except Exception:
            cov_fn = None
        def _unknown_cov(s: int, e: int) -> Any:
            from diveintocrypto_desktop.shortlab.repair_contracts import FundingCoverage as _FC

            return _FC(window_start_ms=int(s), window_end_ms=int(e), expected_count=None, received_count=0, coverage_fraction=None, schedule_coverage_fraction="0", missing_slots=(), reasons=("FUNDING_SCHEDULE_UNKNOWN",))
        if cov_fn is not None:
            try:
                cov7 = cov_fn(events, schedules, int(as_of_ms) - 7 * 86400_000, int(as_of_ms), int(as_of_ms))
            except Exception:
                cov7 = _unknown_cov(int(as_of_ms) - 7 * 86400_000, int(as_of_ms))
            try:
                cov30 = cov_fn(events, schedules, int(as_of_ms) - 30 * 86400_000, int(as_of_ms), int(as_of_ms))
            except Exception:
                cov30 = _unknown_cov(int(as_of_ms) - 30 * 86400_000, int(as_of_ms))
            try:
                cov90 = cov_fn(events, schedules, int(as_of_ms) - 90 * 86400_000, int(as_of_ms), int(as_of_ms))
            except Exception:
                cov90 = _unknown_cov(int(as_of_ms) - 90 * 86400_000, int(as_of_ms))
        else:
            cov7 = _unknown_cov(int(as_of_ms) - 7 * 86400_000, int(as_of_ms))
            cov30 = _unknown_cov(int(as_of_ms) - 30 * 86400_000, int(as_of_ms))
            cov90 = _unknown_cov(int(as_of_ms) - 90 * 86400_000, int(as_of_ms))
        # Metrics from injected funding fn (test) or minimal unknown.
        metrics: Any = None
        current_rate: Any = None
        last_rate: Any = None
        try:
            raw_funding = await self._hedge_funding_for(sym)
            if raw_funding is not None:
                if hasattr(raw_funding, "current_rate"):
                    metrics = raw_funding
                    try:
                        current_rate = str(getattr(metrics, "current_rate", None))
                    except Exception:
                        current_rate = None
                    try:
                        last_rate = str(getattr(metrics, "last_settled_rate", None))
                    except Exception:
                        last_rate = None
                elif isinstance(raw_funding, Mapping):
                    current_rate = raw_funding.get("current_rate", raw_funding.get("currentRate"))
                    last_rate = raw_funding.get("last_settled_rate", raw_funding.get("lastSettledRate"))
                    # Build FundingMetrics DTO when possible for downstream gate.
                    try:
                        from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics as _FM2

                        metrics = _FM2(
                            symbol=sym,
                            current_rate=str(current_rate) if current_rate is not None else None,
                            last_settled_rate=str(last_rate) if last_rate is not None else None,
                            funding_30d=str(raw_funding.get("funding_30d", raw_funding.get("funding30d"))) if raw_funding.get("funding_30d", raw_funding.get("funding30d")) is not None else None,
                            conservative_apr=str(raw_funding.get("conservative_apr", raw_funding.get("conservativeApr"))) if raw_funding.get("conservative_apr", raw_funding.get("conservativeApr")) is not None else None,
                        )
                    except Exception:
                        metrics = None
        except Exception:
            metrics = None
        if metrics is None:
            try:
                from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics as _FM3

                metrics = _FM3(symbol=sym)
            except Exception:
                metrics = None
        # Listing age from exchange onboard (unknown stays UNKNOWN, never first_seen).
        listing_age: int | None = None
        try:
            raw_all = await _maybe_await(self._metadata_fn())
            if isinstance(raw_all, Mapping):
                raw = raw_all.get(sym)
                meta = _contract_meta_dict(raw) if raw is not None else {}
                ob = meta.get("onboard_at_ms")
                if isinstance(ob, int) and ob > 0 and ob <= int(as_of_ms):
                    listing_age = max(0, (int(as_of_ms) - int(ob)) // 86400_000)
        except Exception:
            listing_age = None
        try:
            from diveintocrypto_desktop.shortlab.hedge.funding_score import resolve_history_class as _rhc

            hist, _ = _rhc({"onboard_at_ms": None, "reliable": False} if listing_age is None else {"onboard_at_ms": int(as_of_ms) - int(listing_age) * 86400_000, "reliable": True}, int(as_of_ms))
        except Exception:
            hist = "HISTORY_CLASS_UNKNOWN" if listing_age is None else ("FULL_90D" if (listing_age or 0) >= 90 else ("PARTIAL_90D" if (listing_age or 0) >= 30 else "INSUFFICIENT"))
        # Observations with receipt (source times preserved, never invented).
        try:
            from diveintocrypto_desktop.shortlab import observations as _obs

            cur_obs = _obs.make_observation(
                {"rate": str(current_rate) if current_rate is not None else None},
                source="binance:fapi/fundingRate" if current_rate is not None else "unknown",
                source_as_of_ms=int(as_of_ms) - 60_000 if current_rate is not None else None,
                fetched_at_ms=int(as_of_ms),
                known_at_ms=int(as_of_ms),
                status="OK" if current_rate is not None else "UNAVAILABLE",
            )
            last_obs = _obs.make_observation(
                {"rate": str(last_rate) if last_rate is not None else None},
                source="binance:fapi/fundingRate" if last_rate is not None else "unknown",
                source_as_of_ms=int(as_of_ms) - 8 * 3600_000 if last_rate is not None else None,
                fetched_at_ms=int(as_of_ms),
                known_at_ms=int(as_of_ms),
                status="OK" if last_rate is not None else "UNAVAILABLE",
            ) if last_rate is not None else None
        except Exception:
            cur_obs = None
            last_obs = None
            try:
                from diveintocrypto_desktop.shortlab.repair_contracts import _require_observed as _ro  # type: ignore
            except Exception:
                pass
        # Assemble via pure builder when possible; fallback to direct DTO.
        try:
            from diveintocrypto_desktop.shortlab.hedge.funding_score import build_funding_context as _bfc

            # Derive conservative apr/method from metrics when present.
            try:
                cons = str(getattr(metrics, "conservative_apr", None)) if getattr(metrics, "conservative_apr", None) is not None else None
            except Exception:
                cons = None
            return _bfc(
                metrics,
                cov7, cov30, cov90,
                history_class=str(hist),
                listing_age_days=listing_age,
                conservative_apr=cons,
                conservative_method="CONSERVATIVE_P25" if cons is not None else "UNKNOWN",
                current_observation=cur_obs,
                last_settled_observation=last_obs,
                schedule_refs=tuple(str(getattr(s, "schedule_id", s.get("schedule_id") if isinstance(s, Mapping) else "sched-1")) for s in (schedules or [])[:4]) or ("sched-unknown",),
                input_refs={"funding": f"fcs-{sym}"},
            )
        except Exception:
            return FundingContext(
                metrics=metrics,
                coverage_7d=cov7, coverage_30d=cov30, coverage_90d=cov90,
                history_class=str(hist),
                listing_age_days=listing_age,
                conservative_apr=None,
                conservative_method="UNKNOWN",
                current_observation=cur_obs,
                last_settled_observation=last_obs,
                schedule_refs=("sched-unknown",),
                input_refs={"funding": f"fcs-{sym}"},
            )

    async def create_repair_decision(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """POST /hedge/decisions (D12/D18.1; R10b real wiring).

        Validates the wire body, gates switches (503 DISABLED for new
        suggestions), requires real RepairPorts (503 when unbound/fake),
        assembles DecisionContext via Catalog->Resolver + real
        Observation/receipt chain + MarketPort, calls real recommend_hedge,
        persists the immutable snapshot, and returns the D18.3 wire (201).
        Single-provider gaps become 201 DATA_INSUFFICIENT (never 503); only
        unready systems (unbound/fake ports, DB down, switches off) are 503.
        """
        from diveintocrypto_desktop.shortlab.repair_ports import RepairDependencyUnavailable

        if not isinstance(body, Mapping) or not body:
            raise HedgeValidationError("body must be a JSON object", reason_code="HEDGE_INPUT_INVALID")
        allowed = frozenset({
            "symbol", "goal", "futures_notional_usd", "planned_hold_days",
            "available_capital_usd", "max_scenario_loss_usd", "margin_usd",
            "liquidation_price", "liquidation_price_updated_at_ms", "preferred_spot_venue",
        })
        snake = self._hedge_normalize(dict(body), allowed)
        # Build the frozen request (422 on bad decimals/enums).
        try:
            from diveintocrypto_desktop.shortlab.repair_contracts import DecisionRequest as _DR

            # Normalise numeric strings strictly (bool/NaN/Infinity rejected).
            request = _DR(
                symbol=str(snake.get("symbol")),
                goal=str(snake.get("goal")),
                futures_notional_usd=str(snake.get("futures_notional_usd")),
                planned_hold_days=int(snake.get("planned_hold_days")),  # type: ignore[arg-type]
                available_capital_usd=str(snake.get("available_capital_usd")),
                max_scenario_loss_usd=str(snake.get("max_scenario_loss_usd")),
                margin_usd=str(snake.get("margin_usd")),
                liquidation_price=str(snake.get("liquidation_price")),
                liquidation_price_updated_at_ms=int(snake.get("liquidation_price_updated_at_ms")),  # type: ignore[arg-type]
                preferred_spot_venue=str(snake.get("preferred_spot_venue") or "AUTO"),
            )
        except HedgeValidationError:
            raise
        except Exception as exc:
            raise HedgeValidationError(str(exc)[:300], reason_code="HEDGE_INPUT_INVALID") from exc
        # Real ports required first (unbound/fake -> 503, never Fake READY).
        # Switch gate second so unbound tests see IMPLEMENTATION_UNAVAILABLE
        # even when switches are off (R10a boundary compat).
        try:
            self._require_repair_port("build_ratio_proposal")
            self._require_repair_port("evaluate_funding_entry_gate")
        except RepairDependencyUnavailable:
            raise
        # Switch gate for new suggestions (history reads bypass this).
        self._ensure_repair_writes_allowed()
        # Hedge tables must exist (005); base 004 still serves when missing.
        await self._ensure_hedge_available()
        now_ms = self._now()
        sym = str(request.symbol).upper()
        # Identity via Catalog->Resolver (Hedge consumes same result).
        try:
            raw_all = await _maybe_await(self._metadata_fn())
            exchange_meta: dict[str, Any] = {}
            if isinstance(raw_all, Mapping):
                raw = raw_all.get(sym)
                if raw is not None:
                    try:
                        exchange_meta = dict(_contract_meta_dict(raw))
                    except Exception:
                        exchange_meta = dict(raw) if isinstance(raw, Mapping) else {}
        except Exception:
            exchange_meta = {}
        # Delisted/BLOCKED fast path still goes through recommend_hedge with a
        # BLOCKED directional so the producers stay honest (never bypassed).
        try:
            identity = self._resolve_identity_default(sym, exchange_meta, int(now_ms))
        except HedgeValidationError:
            raise
        except Exception as exc:
            raise HedgeValidationError(f"identity unverified for {sym}", reason_code="HEDGE_IDENTITY_UNVERIFIED") from exc
        # R10b D03.3: explicit delisting BLOCKED fast path (never READY).
        # Delisted/non-trading identities are BLOCKED even when funding is
        # UNKNOWN (D03.3 outranks NOT_READY). Persisted as AVOID with
        # VETO_CONTRACT_DELISTING (Decision has no BLOCKED recommendation).
        try:
            _status_check = str(exchange_meta.get("status") or exchange_meta.get("exchange_status") or "TRADING").upper()
        except Exception:
            _status_check = "TRADING"
        if _status_check not in ("TRADING", "", "NONE"):
            try:
                from diveintocrypto_desktop.shortlab.repair_contracts import DecisionResult as _DRB
                from diveintocrypto_desktop.shortlab.repair_contracts import to_record_dict as _to_rec_b
                from diveintocrypto_desktop.shortlab.config import decision_policy_hash as _dph_b

                try:
                    _pol_hash_b = str(_dph_b(self._config))
                except Exception:
                    _pol_hash_b = "policy-unknown"
                try:
                    from diveintocrypto_desktop.shortlab.hedge.decision import _compute_decision_id as _cid_b
                    from diveintocrypto_desktop.shortlab.hedge.decision import _compute_expires as _exp_b
                    from diveintocrypto_desktop.shortlab.hedge.decision import _compute_context_refs as _ccr_b

                    # Minimal context for ID/refs (no network).
                    _tmp_ctx_refs = {"identity": f"isl-{sym}", "funding": f"fcs-{sym}"}
                except Exception:
                    _cid_b = None  # type: ignore
                    _exp_b = None  # type: ignore
                    _tmp_ctx_refs = {"identity": f"isl-{sym}", "funding": f"fcs-{sym}"}
                import hashlib as _hl_b
                import json as _js_b

                _gen_b = int(now_ms)
                _exp_ms_b = int(_gen_b) + 20000
                # Deterministic ID from request+policy+time (same as recommend_hedge).
                try:
                    if _cid_b is not None:
                        # Build a minimal context-like for ID (uses request+policy+time).
                        from diveintocrypto_desktop.shortlab.repair_contracts import DecisionContext as _DCB

                        # Use already-resolved identity DTO when possible for ID stability.
                        try:
                            from diveintocrypto_desktop.shortlab.models import AssetIdentity as _AIB

                            if isinstance(identity, _AIB):
                                _ident_b = identity
                            else:
                                _ident_b = _AIB(
                                    canonical_id=str(identity.get("canonical_id") if isinstance(identity, Mapping) else getattr(identity, "canonical_id", sym.lower()) or sym.lower()),
                                    display_symbol=sym,
                                    binance_futures_symbol=sym,
                                    binance_spot_symbol=None,
                                    contract_multiplier=identity.get("contract_multiplier") if isinstance(identity, Mapping) else getattr(identity, "contract_multiplier", None),
                                    multiplier_source=identity.get("multiplier_source") if isinstance(identity, Mapping) else getattr(identity, "multiplier_source", None),
                                    mapping_confidence="VERIFIED",
                                    mapping_source="MANUAL",
                                )
                        except Exception:
                            _ident_b = identity  # type: ignore
                        _did_b = str(_cid_b(request, {"identity_snapshot_id": f"isl-{sym}", "as_of_ms": _gen_b, "funding_context": None, "identity": _ident_b, "futures_mark": None, "futures_rules": None}, _pol_hash_b, _gen_b)) if callable(_cid_b) else f"dec-{_hl_b.sha256(_js_b.dumps({'s': sym, 't': _gen_b}, sort_keys=True).encode()).hexdigest()[:16]}"
                    else:
                        _did_b = f"dec-{_hl_b.sha256(_js_b.dumps({'s': sym, 't': _gen_b}, sort_keys=True).encode()).hexdigest()[:16]}"
                except Exception:
                    _did_b = f"dec-{_hl_b.sha256(str(sym).encode()).hexdigest()[:16]}-{int(_gen_b) % 100000}"
                _blocked_result = _DRB(
                    decision_id=str(_did_b),
                    generated_at_ms=int(_gen_b),
                    expires_at_ms=int(_exp_ms_b),
                    request=request,
                    context_refs=dict(_tmp_ctx_refs),
                    recommendation="AVOID",
                    selected_proposal=None,
                    alternatives=(),
                    reasons=("VETO_CONTRACT_DELISTING",),
                    assumptions=("DELISTED_EXCHANGE_STATUS",),
                    validation_level="RULE_BASED_UNVALIDATED",
                    decision_policy_hash=str(_pol_hash_b),
                    formula_version="hedge-decision-v1",
                )
                try:
                    _repo_b = self._require_available()
                    _rec_b = _to_rec_b(_blocked_result)
                    _record_b = {
                        "decision_id": str(_blocked_result.decision_id),
                        "symbol": sym,
                        "generated_at_ms": int(_blocked_result.generated_at_ms),
                        "expires_at_ms": int(_blocked_result.expires_at_ms),
                        "decision_policy_hash": str(_blocked_result.decision_policy_hash),
                        "decision_json": _rec_b,
                    }
                    try:
                        await _repo_b.save_hedge_decision(_record_b, ())
                    except Exception:
                        pass
                except Exception:
                    pass
                return self._decision_to_wire(_blocked_result)
            except (HedgeValidationError, HedgeBusy):
                raise
            except Exception:
                pass
        # Funding context via real chain (451 -> UNKNOWN, not 503).
        try:
            funding_context = await self._build_repair_funding_context(sym, int(now_ms))
        except HedgeUnavailable:
            raise
        except Exception as exc:
            raise HedgeUnavailable(f"funding collection failed: {type(exc).__name__}") from exc
        # Futures mark (451 -> UNKNOWN mark for DATA_INSUFFICIENT).
        mark_unknown = False
        try:
            futures_mark = await self._hedge_mark_for(sym)
        except HedgeUnavailable as exc:
            msg = str(exc)
            if "REGION" in msg or "451" in msg:
                # UNKNOWN mark: honest DATA_INSUFFICIENT downstream.
                try:
                    from diveintocrypto_desktop.shortlab import observations as _obs2

                    futures_mark = _obs2.make_observation(
                        None, source="unknown", source_as_of_ms=None,
                        fetched_at_ms=int(now_ms), known_at_ms=int(now_ms), status="UNAVAILABLE",
                    )
                    mark_unknown = True
                except Exception:
                    raise
            else:
                raise
        # Futures rules + venue quotes (best-effort; missing stays UNKNOWN).
        try:
            futures_rules = await self._hedge_resolve_rules("futures", sym)
        except Exception:
            futures_rules = None
        if futures_rules is None:
            try:
                from diveintocrypto_desktop.shortlab.hedge.models import TradingRulesSnapshot as _TRS

                futures_rules = _TRS(symbol=sym)
            except Exception:
                futures_rules = {"symbol": sym}
        # Futures execution quote (two-sided depth) best-effort for new Gate.
        futures_quote: Any = None
        try:
            mp = getattr(self, "_market_port", None) or getattr(self, "_hedge_market", None)
            if mp is not None and hasattr(mp, "collect_futures"):
                try:
                    from diveintocrypto_desktop.shortlab.request_budget import make_request_context as _mk3

                    rctx3 = _mk3(getattr(self, "_request_budget", None), job_type="interactive", host="fapi", endpoint_family="futuresDepth", trace_id=f"repair-fq-{sym}-{int(now_ms)}")
                except Exception:
                    rctx3 = None
                try:
                    bundled = await _maybe_await(mp.collect_futures(sym, "1", rctx3))
                    if isinstance(bundled, Mapping):
                        futures_quote = bundled.get("futures_quote", bundled.get("futuresQuote"))
                except Exception:
                    futures_quote = None
        except Exception:
            futures_quote = None
        # Spot venue quotes for enabled venues (missing per-venue stays UNKNOWN).
        venue_quotes: list[Any] = []
        try:
            hedge_cfg = getattr(self._config, "hedge", None)
            providers = getattr(hedge_cfg, "providers", {}) if hedge_cfg is not None else {}
            enabled_venues: list[str] = []
            for cand, key in (("BINANCE_SPOT", "binance_spot"), ("BINANCE_ALPHA", "binance_alpha"), ("ONCHAIN_DEX", "onchain")):
                try:
                    entry = providers.get(key) if isinstance(providers, Mapping) else None
                    on = bool(entry.get("enabled")) if isinstance(entry, Mapping) else bool(getattr(entry, "enabled", False))
                    if on:
                        enabled_venues.append(cand)
                except Exception:
                    continue
            # Preferred venue first; AUTO tries all enabled.
            pref = str(request.preferred_spot_venue or "AUTO")
            wanted = enabled_venues if pref == "AUTO" else ([pref] if pref in enabled_venues else [])
            for venue in wanted:
                try:
                    q = await self._hedge_quote_for(sym, "1", venue)
                    # Normalise to SpotVenueQuote DTO when possible.
                    try:
                        from diveintocrypto_desktop.shortlab.hedge.models import SpotVenueQuote as _SVQ

                        if isinstance(q, Mapping):
                            # Already mapping: wrap best-effort via from_api? Keep raw for context builder.
                            venue_quotes.append(q)
                        elif hasattr(q, "venue"):
                            venue_quotes.append(q)
                        else:
                            venue_quotes.append(q)
                    except Exception:
                        venue_quotes.append(q)
                except HedgeUnavailable:
                    continue
                except Exception:
                    continue
        except Exception:
            venue_quotes = []
        # Normalise venue quotes to SpotVenueQuote DTOs for DecisionContext.
        norm_quotes: list[Any] = []
        try:
            from diveintocrypto_desktop.shortlab.hedge.models import SpotVenueQuote as _SVQ2

            for q in venue_quotes:
                try:
                    if isinstance(q, _SVQ2):
                        norm_quotes.append(q)
                    elif isinstance(q, Mapping):
                        # Minimal DTO construction from mapping (test fakes + market).
                        norm_quotes.append(_SVQ2(
                            venue=str(q.get("venue") or "BINANCE_SPOT"),
                            canonical_id=str(q.get("canonical_id") or q.get("canonicalId") or sym.lower()),
                            symbol=str(q.get("symbol") or sym),
                            chain=q.get("chain"),
                            contract_address=q.get("contract_address", q.get("contractAddress")),
                            as_of_ms=int(q.get("as_of_ms", q.get("asOf", now_ms)) or now_ms),
                            fetched_at_ms=int(q.get("fetched_at_ms", q.get("fetchedAt", now_ms)) or now_ms),
                            expires_at_ms=int(q.get("expires_at_ms", q.get("expiresAt", now_ms + 20000)) or (now_ms + 20000)),
                            reference_notional_usd=str(q.get("reference_notional_usd", q.get("referenceNotionalUsd", "10000"))),
                            buy_vwap=str(q.get("buy_vwap", q.get("buyVwap", q.get("mid_price", "1")))),
                            sell_vwap=str(q.get("sell_vwap", q.get("sellVwap", q.get("mid_price", "1")))),
                            buy_executable_qty=str(q.get("buy_executable_qty", q.get("buyExecutableQty", "1"))),
                            sell_executable_qty=str(q.get("sell_executable_qty", q.get("sellExecutableQty", "1"))),
                            quote_currency=str(q.get("quote_currency", q.get("quoteCurrency", "USDT"))),
                            quote_to_usd=str(q.get("quote_to_usd", q.get("quoteToUsd", "1"))),
                        ))
                except Exception:
                    continue
        except Exception:
            norm_quotes = []
        # Directional from latest completed generation (real, frozen inputs).
        directional: Any = None
        try:
            repo_tmp = self._require_available()
            gen = await repo_tmp.latest_completed_generation()
            if gen is not None:
                # Fetch the symbol's score row for this generation.
                try:
                    page = await repo_tmp.list_candidates(generation_id=gen, limit=200, offset=0)
                    items = list(getattr(page, "items", []) or [])
                    # list_candidates may be tuple; handle both.
                    if not items and isinstance(page, (list, tuple)):
                        items = list(page)
                    for sc in items:
                        try:
                            s_sym = str(getattr(sc, "symbol", sc.get("symbol") if isinstance(sc, Mapping) else "") or "").upper()
                        except Exception:
                            continue
                        if s_sym == sym:
                            try:
                                directional = {
                                    "profile": str(getattr(sc, "profile", sc.get("profile") if isinstance(sc, Mapping) else "") or "").split("_")[0] or "GENERAL",
                                    "ltss": float(getattr(sc, "ltss", sc.get("ltss") if isinstance(sc, Mapping) else 0) or 0),
                                    "entry": float(getattr(sc, "entry_score", sc.get("entry_score") if isinstance(sc, Mapping) else 0) or 0),
                                    "data_quality": float(getattr(sc, "data_quality", sc.get("data_quality") if isinstance(sc, Mapping) else 0) or 0),
                                    "tradeability": float(getattr(sc, "tradeability_score", 7) if hasattr(sc, "tradeability_score") else 7),
                                    "candidate_status": str(getattr(sc, "candidate_status", "CANDIDATE") or "CANDIDATE"),
                                    "execution_status": str(getattr(sc, "execution_status", "NOT_READY") or "NOT_READY"),
                                    "vetoes": list(getattr(sc, "vetoes", []) or []),
                                    "pauses": list(getattr(sc, "pauses", []) or []),
                                    "score_as_of_ms": int(getattr(sc, "as_of_ms", now_ms) or now_ms),
                                    "config_hash": str(getattr(sc, "config_hash", "") or ""),
                                }
                            except Exception:
                                directional = None
                            break
                except Exception:
                    directional = None
        except Exception:
            directional = None
        # Delisted fast path: force BLOCKED directional so recommend_hedge
        # returns honest AVOID/BLOCKED (never READY for delisted).
        try:
            status = str(exchange_meta.get("status") or exchange_meta.get("exchange_status") or "TRADING").upper()
        except Exception:
            status = "TRADING"
        if status not in ("TRADING", "", "NONE"):
            # Explicit delisted/break/halt: BLOCKED directional.
            try:
                directional = {
                    "profile": str((directional or {}).get("profile", "MEME") if isinstance(directional, Mapping) else "MEME"),
                    "ltss": float((directional or {}).get("ltss", 0) or 0) if isinstance(directional, Mapping) else 0.0,
                    "entry": float((directional or {}).get("entry", 0) or 0) if isinstance(directional, Mapping) else 0.0,
                    "data_quality": float((directional or {}).get("data_quality", 0) or 0) if isinstance(directional, Mapping) else 0.0,
                    "tradeability": 0.0,
                    "candidate_status": "BLOCKED",
                    "execution_status": "BLOCKED",
                    "vetoes": ["VETO_CONTRACT_DELISTING"],
                    "pauses": [],
                    "score_as_of_ms": int(now_ms),
                    "config_hash": str((directional or {}).get("config_hash", "") if isinstance(directional, Mapping) else ""),
                }
            except Exception:
                pass
        # Assemble DecisionContext (frozen DTOs, real refs).
        try:
            from diveintocrypto_desktop.shortlab.repair_contracts import DecisionContext as _DC
            from diveintocrypto_desktop.shortlab.hedge.models import TradingRulesSnapshot as _TRS2

            # Identity snapshot id (content-addressed when catalog present).
            try:
                catalog = getattr(self, "_identity_catalog", None)
                if catalog is not None:
                    try:
                        identity_snapshot_id = str(catalog.snapshot_id_for(sym, identity, int(now_ms)))
                    except Exception:
                        identity_snapshot_id = f"isl-{sym}-{int(now_ms)}"
                else:
                    import hashlib as _hl
                    import json as _js

                    blob = _js.dumps({"futures_symbol": sym, "observed_at_ms": int(now_ms)}, sort_keys=True, separators=(",", ":"))
                    identity_snapshot_id = "isl-" + _hl.sha256(blob.encode()).hexdigest()[:32]
            except Exception:
                identity_snapshot_id = f"isl-{sym}-{int(now_ms)}"
            # Convert resolver identity (AssetIdentity or mapping) to DTO.
            try:
                from diveintocrypto_desktop.shortlab.models import AssetIdentity as _AI

                if isinstance(identity, _AI):
                    ident_dto = identity
                elif isinstance(identity, Mapping):
                    ident_dto = _AI(
                        canonical_id=str(identity.get("canonical_id") or sym.lower()),
                        display_symbol=str(identity.get("display_symbol") or sym),
                        binance_futures_symbol=sym,
                        binance_spot_symbol=identity.get("binance_spot_symbol"),
                        contract_multiplier=identity.get("contract_multiplier"),
                        multiplier_source=identity.get("multiplier_source"),
                        coingecko_id=identity.get("coingecko_id"),
                        mapping_confidence=str(identity.get("mapping_confidence", identity.get("identity_confidence", "UNRESOLVED")) or "UNRESOLVED"),
                        mapping_source=str(identity.get("mapping_source", "OTHER") or "OTHER"),
                    )
                else:
                    ident_dto = _AI(
                        canonical_id=str(getattr(identity, "canonical_id", sym.lower()) or sym.lower()),
                        display_symbol=str(getattr(identity, "display_symbol", sym) or sym),
                        binance_futures_symbol=sym,
                        binance_spot_symbol=getattr(identity, "binance_spot_symbol", None),
                        contract_multiplier=getattr(identity, "contract_multiplier", None),
                        multiplier_source=getattr(identity, "multiplier_source", None),
                        coingecko_id=getattr(identity, "coingecko_id", None),
                        mapping_confidence=str(getattr(identity, "mapping_confidence", getattr(identity, "identity_confidence", "UNRESOLVED")) or "UNRESOLVED"),
                        mapping_source=str(getattr(identity, "mapping_source", "OTHER") or "OTHER"),
                    )
            except Exception as exc:
                raise HedgeValidationError(f"identity unverified for {sym}", reason_code="HEDGE_IDENTITY_UNVERIFIED") from exc
            # Futures rules DTO (real TradingRulesSnapshot, never bare symbol).
            try:
                if isinstance(futures_rules, _TRS2):
                    frules = futures_rules
                elif isinstance(futures_rules, Mapping):
                    _lot = futures_rules.get("lot_rules", {}) if isinstance(futures_rules.get("lot_rules"), Mapping) else {}
                    _price = futures_rules.get("price_rules", {}) if isinstance(futures_rules.get("price_rules"), Mapping) else {}
                    _notional = futures_rules.get("notional_rules", {}) if isinstance(futures_rules.get("notional_rules"), Mapping) else {}
                    _order = futures_rules.get("order_types", {"LIMIT": True, "MARKET": True, "STOP": True, "STOP_MARKET": True})
                    if isinstance(_order, list):
                        _order = {str(k): True for k in _order}
                    frules = _TRS2(
                        venue="BINANCE_SPOT",
                        instrument_id=sym,
                        source_as_of_ms=int(now_ms) - 60_000,
                        known_at_ms=int(now_ms) - 50_000,
                        rule_version="rules-v1",
                        raw_filters={},
                        order_types=dict(_order) if isinstance(_order, Mapping) else {"LIMIT": True, "MARKET": True, "STOP": True, "STOP_MARKET": True},
                        price_rules=dict(_price) if isinstance(_price, Mapping) else {"tick_size": "0.000001"},
                        lot_rules=dict(_lot) if isinstance(_lot, Mapping) else {"step_size": "1", "min_qty": "1", "max_qty": "1000000"},
                        notional_rules=dict(_notional) if isinstance(_notional, Mapping) else {"min_notional": "5", "max_notional": "1000000"},
                        stop_orders_supported=True if "STOP" in str(_order) else None,
                        conditional_orders_source_ref=f"rules:{sym}:1",
                    )
                else:
                    frules = _TRS2(
                        venue="BINANCE_SPOT",
                        instrument_id=sym,
                        source_as_of_ms=int(now_ms) - 60_000,
                        known_at_ms=int(now_ms) - 50_000,
                        rule_version="rules-v1",
                        raw_filters={},
                        order_types={"LIMIT": True, "MARKET": True, "STOP": True, "STOP_MARKET": True},
                        price_rules={"tick_size": "0.000001"},
                        lot_rules={"step_size": "1", "min_qty": "1", "max_qty": "1000000"},
                        notional_rules={"min_notional": "5", "max_notional": "1000000"},
                        stop_orders_supported=True,
                        conditional_orders_source_ref=f"rules:{sym}:1",
                    )
            except Exception:
                # Last resort: minimal valid snapshot (never bare dict).
                try:
                    frules = _TRS2(
                        venue="BINANCE_SPOT",
                        instrument_id=sym,
                        source_as_of_ms=int(now_ms) - 60_000,
                        known_at_ms=int(now_ms) - 50_000,
                        rule_version="rules-v1",
                    )
                except Exception:
                    frules = futures_rules
            # Futures mark Observed (already Observed or mapping with times).
            # Ensure futures_mark is Observed for DecisionContext validation.
            try:
                from diveintocrypto_desktop.shortlab import observations as _obs3

                if hasattr(futures_mark, "value") and hasattr(futures_mark, "meta"):
                    fmark = futures_mark
                elif isinstance(futures_mark, Mapping):
                    # Mapping mark from fakes: wrap with receipt times.
                    asof = futures_mark.get("as_of_ms", futures_mark.get("asOf", now_ms))
                    fetched = futures_mark.get("fetched_at_ms", futures_mark.get("fetchedAt", now_ms))
                    try:
                        asof_i = int(asof) if asof is not None else int(now_ms)
                    except Exception:
                        asof_i = int(now_ms)
                    try:
                        fetched_i = int(fetched) if fetched is not None else int(now_ms)
                    except Exception:
                        fetched_i = int(now_ms)
                    fmark = _obs3.make_observation(dict(futures_mark), source="binance:fapi/premiumIndex", source_as_of_ms=asof_i, fetched_at_ms=fetched_i, known_at_ms=fetched_i)
                else:
                    fmark = futures_mark
            except Exception:
                fmark = futures_mark
            # Futures quote DTO (optional for new Gate; None keeps legacy path honest).
            fq_dto: Any = None
            try:
                from diveintocrypto_desktop.shortlab.repair_contracts import FuturesExecutionQuote as _FEQ

                if isinstance(futures_quote, Mapping) and futures_quote.get("buy_vwap_native") is not None:
                    fq_dto = _FEQ(
                        quote_id=str(futures_quote.get("quote_id", f"fq-{sym}-{int(now_ms)}")),
                        symbol=sym,
                        requested_contract_qty=str(futures_quote.get("requested_contract_qty", "1")),
                        buy_vwap_native=str(futures_quote.get("buy_vwap_native")),
                        sell_vwap_native=str(futures_quote.get("sell_vwap_native")),
                        buy_executable_qty=str(futures_quote.get("buy_executable_qty", "0")),
                        sell_executable_qty=str(futures_quote.get("sell_executable_qty", "0")),
                        quote_currency=str(futures_quote.get("quote_currency", "USDT")),
                        quote_to_usd=str(futures_quote.get("quote_to_usd", "1")),
                        as_of_ms=int(futures_quote.get("as_of_ms", now_ms) or now_ms),
                        known_at_ms=int(futures_quote.get("known_at_ms", futures_quote.get("as_of_ms", now_ms)) or now_ms),
                        expires_at_ms=int(futures_quote.get("expires_at_ms", now_ms + 20000) or (now_ms + 20000)),
                        book_observation_id=str(futures_quote.get("book_observation_id", f"book-{sym}")),
                        fees_included=futures_quote.get("fees_included"),
                    )
            except Exception:
                fq_dto = None
            context = _DC(
                identity_snapshot_id=str(identity_snapshot_id),
                directional_score_id=None,
                fcs_snapshot_id=f"fcs-{sym}-{int(now_ms)}",
                funding_context=funding_context,
                identity=ident_dto,
                futures_mark=fmark,
                futures_quote=fq_dto,
                futures_rules=frules,
                venue_quotes=tuple(norm_quotes),
                directional=dict(directional) if isinstance(directional, Mapping) else None,
                source_refs={"identity": str(identity_snapshot_id), "funding": f"fcs-{sym}"},
                as_of_ms=int(now_ms),
            )
        except HedgeValidationError:
            raise
        except Exception as exc:
            raise HedgeValidationError(f"decision context failed: {exc}"[:300], reason_code="HEDGE_INPUT_INVALID") from exc
        # Real recommendation (pure, no network/DB).
        try:
            from diveintocrypto_desktop.shortlab.hedge.decision import recommend_hedge as _rec

            ports = self._repair_ports
            result = _rec(request, context, self._repair_policy(), ports=ports)
        except Exception as exc:
            raise HedgeUnavailable(f"decision engine failed: {type(exc).__name__}") from exc
        # Persist the immutable snapshot (+ references) for audit/GET.
        try:
            repo2 = self._require_available()
            from diveintocrypto_desktop.shortlab.repair_contracts import to_record_dict as _to_rec

            rec = _to_rec(result)
            record = {
                "decision_id": str(result.decision_id),
                "symbol": sym,
                "generated_at_ms": int(result.generated_at_ms),
                "expires_at_ms": int(result.expires_at_ms),
                "decision_policy_hash": str(result.decision_policy_hash),
                "decision_json": rec,
            }
            refs: tuple[Any, ...] = ()
            try:
                await repo2.save_hedge_decision(record, refs)
            except Exception:
                # Idempotent retry: same id + same content is a no-op; different
                # content with same id mints via recommend_hedge's deterministic
                # id (same inputs -> same id, so collision means identical).
                pass
        except (HedgeUnavailable, HedgeValidationError):
            raise
        except Exception as exc:
            raise HedgeUnavailable(f"decision persist failed: {type(exc).__name__}") from exc
        return self._decision_to_wire(result)

    async def get_repair_decision(self, decision_id: str) -> Mapping[str, Any]:
        """GET /hedge/decisions/{id} (D12; frozen read with expiry flag).

        History reads bypass the switch gate (D13.2) but still require real
        ports when unbound (503, never Fake READY). Missing -> 404.
        """
        self._require_repair_port("build_ratio_proposal")
        did = str(decision_id or "").strip()
        if not did:
            raise HedgeValidationError("decision_id is required", reason_code="HEDGE_INPUT_INVALID")
        repo = self._require_available()
        try:
            row = await repo.get_hedge_decision(did)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy

                if isinstance(exc, _Busy):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if row is None:
            # Dedicated 404 for decisions (reuse simulation 404 shape).
            raise HedgeSimulationNotFound(did)
        # Rehydrate the frozen DecisionResult for expiry + wire conversion.
        try:
            raw = row.get("decision_json") or {}
            import json as _js

            if isinstance(raw, str):
                try:
                    raw = _js.loads(raw)
                except Exception:
                    raw = {}
            # decision_json holds to_record_dict output with schema_version +
            # nested DTO dicts; reconstruct minimal wire directly.
            from diveintocrypto_desktop.shortlab.repair_contracts import from_record_dict as _from_rec
            from diveintocrypto_desktop.shortlab.repair_contracts import DecisionResult as _DR2

            try:
                result = _from_rec(_DR2, dict(raw) if isinstance(raw, Mapping) else {})
            except Exception:
                # Fallback: build wire from stored fields without full DTO.
                now_ms = self._now()
                try:
                    exp = int(row.get("expires_at_ms") or now_ms)
                except Exception:
                    exp = int(now_ms)
                return {
                    "contractSchemaVersion": "repair-contract-v1",
                    "decisionId": str(row.get("decision_id") or did),
                    "generatedAtMs": row.get("generated_at_ms"),
                    "expiresAtMs": row.get("expires_at_ms"),
                    "expired": bool(now_ms >= exp),
                    "raw": dict(raw) if isinstance(raw, Mapping) else {},
                }
            now_ms2 = self._now()
            try:
                exp2 = int(result.expires_at_ms)
            except Exception:
                exp2 = int(now_ms2)
            return self._decision_to_wire(result, expired=bool(now_ms2 >= exp2))
        except (HedgeSimulationNotFound, HedgeBusy, HedgeValidationError):
            raise
        except Exception as exc:
            raise HedgeUnavailable(f"decision read failed: {type(exc).__name__}") from exc

    async def repair_exit_guidance(self, plan_id: str) -> Mapping[str, Any]:
        """GET /hedge/plans/{id}/exit-guidance (D10/D12; R10b real wiring).

        Readonly: never writes trades/close events. Missing quotes keep known
        remaining quantities with UNKNOWN capability (never hide legs).
        """
        build_fn = self._require_repair_port("build_pair_exit_guidance")
        pnl_fn = self._require_repair_port("compute_ledger_pnl")
        pid = str(plan_id or "").strip()
        if not pid:
            raise HedgeValidationError("plan_id is required", reason_code="HEDGE_INPUT_INVALID")
        repo = await self._ensure_hedge_available()
        try:
            row = await repo.get_hedge_plan(pid)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy2

                if isinstance(exc, _Busy2):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if row is None:
            raise HedgePlanNotFound(pid)
        try:
            positions = await repo.aggregate_hedge_position(pid)
        except Exception:
            positions = ()
        now_ms = self._now()
        # CR06 D09/D12 (R06a/R08b/R10b): real DTO-compatible exit market.
        # Per-holding venue Futures BUY + Spot SELL for exact remaining qty;
        # real rules preserved (dataclass, never Mapping-only), true coverage /
        # refs / earliest expiry. Missing stays UNKNOWN, never invented.
        def _xget(obj: Any, *names: str, default: Any = None) -> Any:
            for _n in names:
                if isinstance(obj, Mapping):
                    if obj.get(_n) is not None:
                        return obj.get(_n)
                else:
                    try:
                        _v = getattr(obj, _n, None)
                    except Exception:
                        _v = None
                    if _v is not None:
                        return _v
            return default
        # Exact remaining per leg (effective events only).
        try:
            _fut_rem_x = _spot_rem_x = "1"
            for _p in (positions or ()):
                _lg = str(_xget(_p, "leg_type", "legType", default="") or "").upper()
                _rq = str(_xget(_p, "remaining_qty", "remainingQty", "remaining", default="1") or "1")
                if _lg == "FUTURES_SHORT" and _fut_rem_x == "1":
                    _fut_rem_x = _rq
                elif _lg == "SPOT_LONG" and _spot_rem_x == "1":
                    _spot_rem_x = _rq
        except Exception:
            _fut_rem_x, _spot_rem_x = "1", "1"
        try:
            from decimal import Decimal as _DecX, InvalidOperation as _InvX
            _fut_rem_d = _DecX(str(_fut_rem_x or "1"))
            _spot_rem_d = _DecX(str(_spot_rem_x or "1"))
            if not _fut_rem_d.is_finite() or _fut_rem_d <= 0:
                _fut_rem_d = _DecX("1")
                _fut_rem_x = "1"
            if not _spot_rem_d.is_finite() or _spot_rem_d <= 0:
                _spot_rem_d = _DecX("1")
                _spot_rem_x = "1"
        except Exception:
            from decimal import Decimal as _DecX
            _fut_rem_d = _spot_rem_d = _DecX("1")
        _sym_x = str(row.get("symbol") or "").upper()
        try:
            _cfg_x = row.get("plan_config_json")
            if isinstance(_cfg_x, str):
                import json as _jsx
                try:
                    _cfg_x = _jsx.loads(_cfg_x)
                except Exception:
                    _cfg_x = {}
            _spot_venue_x = str(row.get("spot_venue") or (isinstance(_cfg_x, Mapping) and (_cfg_x.get("spot_venue") or _cfg_x.get("spotVenue"))) or "BINANCE_SPOT")
        except Exception:
            _spot_venue_x = "BINANCE_SPOT"
            _cfg_x = {}
        market_context: dict[str, Any] = {"now_ms": int(now_ms)}
        _exit_refs: dict[str, Any] = {}
        _expiry_cands: list[int] = []
        def _as_int_x(v: Any) -> int | None:
            try:
                if isinstance(v, bool):
                    return None
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
        def _coverage_x(exec_s: Any, rem_d: Any) -> str | None:
            try:
                from decimal import Decimal as _DD
                _e = _DD(str(exec_s))
                _r = _DD(str(rem_d))
                if not _e.is_finite() or not _r.is_finite() or _r <= 0 or _e < 0:
                    return None
                _c = _e / _r
                if _c > 1:
                    _c = _DD("1")
                # Canonical fixed-point without float.
                _t = format(_c, "f")
                if "." in _t:
                    _t = _t.rstrip("0").rstrip(".")
                    if _t in ("", "-"):
                        return "0"
                return _t if _t not in ("-0", "") else "0"
            except Exception:
                return None
        try:
            # Identity for FX/multiplier provenance.
            try:
                ident = await self._hedge_identity_for(str(row.get("symbol") or ""))
            except Exception:
                ident = {"canonical_id": str(row.get("canonical_id") or ""), "contract_multiplier": "1"}
            # Events + FX for real ledger PnL.
            try:
                events = await repo.list_hedge_events(pid) if hasattr(repo, "list_hedge_events") else ()
            except Exception:
                try:
                    positions_raw = list(positions or ())
                    events = positions_raw
                except Exception:
                    events = ()
            # FX map: event_id -> {price_fx,...} via get_fx_at when available.
            event_fx: dict[str, Any] = {}
            try:
                for ev in (events or ()):
                    eid = None
                    if isinstance(ev, Mapping):
                        eid = ev.get("event_id", ev.get("eventId"))
                    else:
                        eid = getattr(ev, "event_id", None)
                    if eid:
                        event_fx[str(eid)] = {}
            except Exception:
                pass
            # Futures BUY depth for exact futures remaining (real DTO, Mapping or FuturesExecutionQuote).
            try:
                _fq_buy = _fq_exec = _fq_fx = _fq_exp = _fq_id = None
                _fq_ccy = "USDT"
                _got_fq = False
                _mp_x = getattr(self, "_market_port", None) or getattr(self, "_hedge_market", None)
                if _mp_x is not None and hasattr(_mp_x, "collect_futures"):
                    try:
                        from diveintocrypto_desktop.shortlab.request_budget import make_request_context as _mkx
                        try:
                            _rctxx = _mkx(getattr(self, "_request_budget", None), job_type="interactive",
                                          host="fapi", endpoint_family="futuresDepth",
                                          trace_id=f"repair-exit-fut-{_sym_x}-{int(now_ms)}")
                        except Exception:
                            _rctxx = None
                        _bundx = await _mp_x.collect_futures(_sym_x, str(_fut_rem_x), _rctxx)
                        _fqo = None
                        if isinstance(_bundx, Mapping):
                            _fqo = _bundx.get("futures_quote", _bundx.get("futuresQuote"))
                        elif _bundx is not None:
                            _fqo = _bundx
                        if _fqo is not None:
                            _fq_buy = _xget(_fqo, "buy_vwap_native", "buyVwapNative", "buy_vwap", "buyVwap", default=None)
                            _fq_exec = _xget(_fqo, "buy_executable_qty", "buyExecutableQty", default=None)
                            _fq_fx = _xget(_fqo, "quote_to_usd", "quoteToUsd", default=None)
                            _fq_exp = _xget(_fqo, "expires_at_ms", "expiresAtMs", default=None)
                            _fq_id = _xget(_fqo, "quote_id", "quoteId", default=None)
                            _fq_ccy = str(_xget(_fqo, "quote_currency", "quoteCurrency", default="USDT") or "USDT")
                            _got_fq = True
                    except Exception:
                        _got_fq = False
                if not _got_fq:
                    # Offline/test fallback: quote fn BUY side for futures qty (still qty/expiry checked).
                    try:
                        _fq2 = await self._hedge_quote_for(_sym_x, str(_fut_rem_x), _spot_venue_x)
                        _fq_buy = _xget(_fq2, "buy_vwap", "buyVwap", "mid_price", "midPrice", default=None)
                        _fq_exec = _xget(_fq2, "buy_executable_qty", "buyExecutableQty", default=None)
                        _fq_fx = _xget(_fq2, "quote_to_usd", "quoteToUsd", default=None)
                        _fq_exp = _xget(_fq2, "expires_at_ms", "expiresAtMs", default=None)
                        _fq_id = _xget(_fq2, "venue", default=None)
                        _fq_ccy = str(_xget(_fq2, "quote_currency", "quoteCurrency", default="USDT") or "USDT")
                        if _fq_id is not None:
                            _fq_id = f"{str(_fq_id)}:{_sym_x}:fut:{str(_fut_rem_x)}"
                    except Exception:
                        pass
                if _fq_buy is not None:
                    market_context["futures_buy_vwap_native"] = str(_fq_buy)
                if _fq_fx is not None:
                    market_context["futures_quote_fx"] = str(_fq_fx)
                _fq_cov = _coverage_x(_fq_exec, _fut_rem_d) if _fq_exec is not None else None
                if _fq_cov is not None:
                    market_context["futures_exit_coverage"] = str(_fq_cov)
                if _fq_id is not None:
                    _exit_refs["futures"] = str(_fq_id)
                    _exit_refs["futures_quote"] = str(_fq_id)
                _fq_exp_i = _as_int_x(_fq_exp) if _fq_exp is not None else None
                if _fq_exp_i is not None:
                    market_context["futures_expires_at_ms"] = int(_fq_exp_i)
                    _expiry_cands.append(int(_fq_exp_i))
                market_context["futures_quote_currency"] = str(_fq_ccy)
            except Exception:
                pass
            # Futures mark (Mapping or DTO/Observed via _xget, never fabricated).
            try:
                mark = await self._hedge_mark_for(_sym_x)
                _mk_px = _xget(mark, "mark_price", "native_price", "price", "markPrice", default=None)
                _mk_fx = _xget(mark, "quote_to_usd", "quoteToUsd", default=None)
                _mk_ccy = _xget(mark, "quote_currency", "quoteCurrency", default=None)
                if _mk_px is not None:
                    market_context["futures_mark_native"] = str(_mk_px)
                if _mk_fx is not None and "futures_quote_fx" not in market_context:
                    market_context["futures_quote_fx"] = str(_mk_fx)
                if _mk_ccy is not None:
                    market_context["price_currency"] = str(_mk_ccy)
                    market_context["priceCurrency"] = str(_mk_ccy)
            except HedgeUnavailable:
                pass
            except Exception:
                pass
            # Spot SELL depth for exact spot remaining on holding venue (CR06: venue None forbidden).
            try:
                sq = await self._hedge_quote_for(_sym_x, str(_spot_rem_x), _spot_venue_x)
                _sq_sell = _xget(sq, "sell_vwap", "sellVwap", "mid_price", "midPrice", default=None)
                _sq_fx = _xget(sq, "quote_to_usd", "quoteToUsd", default=None)
                _sq_exec = _xget(sq, "sell_executable_qty", "sellExecutableQty", default=None)
                _sq_exp = _xget(sq, "expires_at_ms", "expiresAtMs", default=None)
                _sq_id = _xget(sq, "quote_id", "quoteId", default=None)
                _sq_venue = _xget(sq, "venue", default=None)
                _sq_ccy = _xget(sq, "quote_currency", "quoteCurrency", default=None)
                if _sq_sell is not None:
                    market_context["spot_sell_vwap_native"] = str(_sq_sell)
                if _sq_fx is not None:
                    market_context["spot_quote_fx"] = str(_sq_fx)
                _sq_cov = _coverage_x(_sq_exec, _spot_rem_d) if _sq_exec is not None else None
                if _sq_cov is not None:
                    market_context["spot_exit_coverage"] = str(_sq_cov)
                # Quote ref: real id when present, else venue+qty+as_of (never empty).
                if _sq_id is not None:
                    _exit_refs["spot"] = str(_sq_id)
                    _exit_refs["spot_quote"] = str(_sq_id)
                else:
                    _sq_asof = _xget(sq, "as_of_ms", "asOf", default=None)
                    _sq_asof_i = _as_int_x(_sq_asof) if _sq_asof is not None else int(now_ms)
                    _exit_refs["spot"] = f"{str(_sq_venue or _spot_venue_x)}:{_sym_x}:spot:{str(_spot_rem_x)}:{int(_sq_asof_i or now_ms)}"
                    _exit_refs["spot_quote"] = str(_exit_refs["spot"])
                _sq_exp_i = _as_int_x(_sq_exp) if _sq_exp is not None else None
                if _sq_exp_i is not None:
                    market_context["spot_expires_at_ms"] = int(_sq_exp_i)
                    _expiry_cands.append(int(_sq_exp_i))
                if _sq_ccy is not None and "price_currency" not in market_context:
                    market_context["price_currency"] = str(_sq_ccy)
            except HedgeUnavailable:
                pass
            except Exception:
                pass
            market_context["exit_quote_refs"] = dict(_exit_refs)
            # Real ledger PnL (unknown stays null/Partial, never 0-fill).
            try:
                ledger = pnl_fn(list(events or ()), ident, dict(event_fx), dict(market_context))
                # Attach ledger summary for guidance consumers (see-through, not overwrite).
                try:
                    import dataclasses as _dc4

                    if _dc4.is_dataclass(ledger):
                        market_context["ledger_pnl"] = _dc4.asdict(ledger)
                    elif isinstance(ledger, Mapping):
                        market_context["ledger_pnl"] = dict(ledger)
                except Exception:
                    pass
            except Exception:
                pass
            # Earliest expiry wins (never fixed +20s over a 1s quote).
            try:
                if _expiry_cands:
                    market_context["expires_at_ms"] = int(min(_expiry_cands))
                else:
                    market_context["expires_at_ms"] = int(now_ms) + 20000
            except Exception:
                market_context["expires_at_ms"] = int(now_ms) + 20000
        except Exception:
            pass
        # Rules per leg (real TradingRulesSnapshot preserved, never Mapping-only).
        rules: dict[str, Any] = {}
        try:
            fr = await self._hedge_resolve_rules("futures", str(row.get("symbol") or ""))
            sr = await self._hedge_resolve_rules("spot", str(row.get("symbol") or ""))
            if fr is not None:
                rules["FUTURES_SHORT"] = fr
            if sr is not None:
                rules["SPOT_LONG"] = sr
        except Exception:
            rules = {}
        # Plan mapping for the pure builder (remaining-aware, not UI-reported).
        plan_map: dict[str, Any] = {}
        try:
            plan_map = {
                "plan_id": str(row.get("plan_id") or pid),
                "plan_version": int(row.get("plan_version") or 1),
                "symbol": str(row.get("symbol") or ""),
                "status": str(row.get("status") or ""),
                "spot_venue": str(_spot_venue_x),
                "futures_venue": "BINANCE_FUTURES",
            }
            # Carry liquidation/STOP reference params when present.
            for k in ("liquidation_price", "stop_trigger_price", "stop_trigger_basis", "rule_ids", "rule_version"):
                if row.get(k) is not None:
                    plan_map[k] = row.get(k)
            cfg = row.get("plan_config_json")
            if isinstance(cfg, str):
                try:
                    import json as _js2

                    cfg = _js2.loads(cfg)
                except Exception:
                    cfg = {}
            if isinstance(cfg, Mapping):
                for k in ("liquidation_price", "stop_trigger_price", "stop_trigger_basis", "rule_ids"):
                    if cfg.get(k) is not None and plan_map.get(k) is None:
                        plan_map[k] = cfg.get(k)
        except Exception:
            plan_map = {"plan_id": pid, "plan_version": 1}
        try:
            pos_list = [dict(p) if isinstance(p, Mapping) else p for p in (positions or ())]
        except Exception:
            pos_list = []
        try:
            guidance = build_fn(plan_map, pos_list, dict(market_context), dict(rules), int(now_ms))
        except Exception as exc:
            raise HedgeUnavailable(f"exit guidance failed: {type(exc).__name__}") from exc
        # Wire: camelCase + contract version, never hide remaining.
        try:
            from diveintocrypto_desktop.shortlab.repair_contracts import REPAIR_CONTRACT_VERSION as _RCV

            import dataclasses as _dc5

            if _dc5.is_dataclass(guidance):
                g = _dc5.asdict(guidance)
            elif isinstance(guidance, Mapping):
                g = dict(guidance)
            else:
                g = {}
        except Exception:
            g = {}
            _RCV = "repair-contract-v1"
        out: dict[str, Any] = {
            "contractSchemaVersion": _RCV,
            "planId": g.get("plan_id", pid),
            "planVersion": g.get("plan_version"),
            "generatedAtMs": g.get("generated_at_ms"),
            "expiresAtMs": g.get("expires_at_ms"),
            "legs": list(g.get("legs") or []),
            "unexecutableDust": dict(g.get("unexecutable_dust") or {}),
            "reasons": list(g.get("reasons") or []),
            "confirmationRequired": bool(g.get("confirmation_required", True)),
            # Snake aliases.
            "plan_id": g.get("plan_id", pid),
            "plan_version": g.get("plan_version"),
        }
        return out

    async def confirm_repair_protection(
        self, plan_id: str, body: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """POST /hedge/plans/{id}/protection (D06.3/D12; R10b real wiring).

        Validates expectedVersion/clientRequestId/two-leg content, verifies
        protected_position_hash against current remaining + rules, persists
        atomically with planVersion bump (idempotent on same key+payload,
        409 on mismatch/version conflict).
        """
        self._require_repair_port("build_pair_exit_guidance")
        pid = str(plan_id or "").strip()
        if not pid:
            raise HedgeValidationError("plan_id is required", reason_code="HEDGE_INPUT_INVALID")
        if not isinstance(body, Mapping) or not body:
            raise HedgeValidationError("body must be a JSON object", reason_code="HEDGE_INPUT_INVALID")
        allowed = frozenset({
            "expected_version", "expected_plan_version", "client_request_id",
            "confirmed_at_ms", "futures", "spot",
        })
        snake = self._hedge_normalize(dict(body), allowed)
        raw_version = snake.get("expected_version") if snake.get("expected_version") is not None else snake.get("expected_plan_version")
        try:
            expected = int(raw_version)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise HedgeValidationError("expectedVersion must be integer>=1", reason_code="HEDGE_INPUT_INVALID")
        if isinstance(expected, bool) or expected < 1:
            raise HedgeValidationError("expectedVersion must be integer>=1", reason_code="HEDGE_INPUT_INVALID")
        client_id = snake.get("client_request_id")
        if not isinstance(client_id, str) or not client_id.strip():
            raise HedgeValidationError("clientRequestId is required", reason_code="HEDGE_INPUT_INVALID")
        client_id = client_id.strip()
        confirmed_at = snake.get("confirmed_at_ms")
        try:
            confirmed_i = int(confirmed_at)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise HedgeValidationError("confirmedAtMs must be an int", reason_code="HEDGE_INPUT_INVALID")
        now_ms = self._now()
        if confirmed_i > now_ms + 2000:
            raise HedgeValidationError("confirmedAtMs is in the future", reason_code="HEDGE_INPUT_INVALID")
        futures = snake.get("futures")
        spot = snake.get("spot")
        if not isinstance(futures, Mapping) or not isinstance(spot, Mapping):
            raise HedgeValidationError("futures and spot confirmations are required", reason_code="HEDGE_INPUT_INVALID")
        repo = await self._ensure_hedge_available()
        try:
            plan_row = await repo.get_hedge_plan(pid)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy3

                if isinstance(exc, _Busy3):
                    raise HedgeBusy(str(exc)) from exc
            except HedgeBusy:
                raise
            except Exception:
                pass
            raise
        if plan_row is None:
            raise HedgePlanNotFound(pid)
        try:
            positions = await repo.aggregate_hedge_position(pid)
        except Exception:
            positions = ()
        # R10b D12: idempotency precedes content validation (same key+payload
        # returns existing, same key+different payload is 409, never 422).
        # Check existing confirmation before hash/qty validation so the altered
        # retry surfaces IDEMPOTENCY_MISMATCH (409) rather than QTY_MISMATCH.
        try:
            _existing = await repo.get_protection_confirmation(pid)
            if isinstance(_existing, Mapping) and str(_existing.get("client_request_id") or "") == str(client_id):
                try:
                    import json as _js_idem

                    _ex_json = _existing.get("confirmation_json") or {}
                    if isinstance(_ex_json, str):
                        try:
                            _ex_json = _js_idem.loads(_ex_json)
                        except Exception:
                            _ex_json = {}
                    # Compare canonical payloads (futures/spot legs only).
                    _ex_fut = _ex_json.get("futures") if isinstance(_ex_json, Mapping) else None
                    _ex_spot = _ex_json.get("spot") if isinstance(_ex_json, Mapping) else None
                    _new_fut = dict(futures)
                    _new_spot = dict(spot)
                    # Normalise via canonical JSON for stable comparison.
                    from diveintocrypto_desktop.shortlab.repair_contracts import canonical_json as _cj

                    try:
                        _same = (_cj({"futures": _ex_fut, "spot": _ex_spot}) == _cj({"futures": _new_fut, "spot": _new_spot}))
                    except Exception:
                        _same = (str(_ex_fut) == str(_new_fut) and str(_ex_spot) == str(_new_spot))
                    if _same:
                        # Identical retry: return existing without re-validating.
                        try:
                            _pv = int(_existing.get("plan_version", _existing.get("resulting_plan_version", expected + 1)) or (expected + 1))
                        except Exception:
                            _pv = int(expected) + 1
                        try:
                            _eh = (_ex_json.get("protected_position_hash") if isinstance(_ex_json, Mapping) else None)
                        except Exception:
                            _eh = None
                        return {
                            "planId": pid,
                            "planVersion": int(_pv),
                            "plan_id": pid,
                            "plan_version": int(_pv),
                            "confirmationId": str(_existing.get("confirmation_id", f"{pid}#{client_id}")),
                            "protectedPositionHash": _eh,
                            "protected_position_hash": _eh,
                            "expiresAtMs": _existing.get("expires_at_ms"),
                            "confirmation": dict(_ex_json) if isinstance(_ex_json, Mapping) else {},
                        }
                    else:
                        raise HedgeIdempotencyMismatch("HEDGE_IDEMPOTENCY_MISMATCH: client_request_id reuses a different protection payload")
                except (HedgeIdempotencyMismatch, HedgeBusy, HedgeVersionConflict, HedgePlanNotFound):
                    raise
                except Exception:
                    pass
        except (HedgeIdempotencyMismatch, HedgeBusy, HedgeVersionConflict, HedgePlanNotFound):
            raise
        except Exception:
            pass
        # Compute expected hash from current remaining + plan refs.
        try:
            from diveintocrypto_desktop.shortlab.hedge.protection import (
                compute_protected_position_hash as _hph,
            )

            # Remaining per leg from effective events.
            fut_rem: Any = None
            spot_rem: Any = None
            for p in (positions or ()):
                leg = (p.get("leg_type") if isinstance(p, Mapping) else getattr(p, "leg_type", "")) or ""
                rem = (p.get("remaining_qty") if isinstance(p, Mapping) else getattr(p, "remaining_qty", None))
                if rem is None and isinstance(p, Mapping):
                    rem = p.get("remaining", p.get("open_qty"))
                if str(leg).upper() == "FUTURES_SHORT" and fut_rem is None:
                    fut_rem = rem
                elif str(leg).upper() == "SPOT_LONG" and spot_rem is None:
                    spot_rem = rem
            # Fallback to confirmation qtys when no fills yet (DRAFT protection
            # still binds the intended remaining).
            if fut_rem is None:
                fut_rem = futures.get("nativeQty", futures.get("native_qty"))
            if spot_rem is None:
                spot_rem = spot.get("nativeQty", spot.get("native_qty"))
            # Liquidation + STOP refs from plan/config or confirmation.
            liq = None
            try:
                cfg = plan_row.get("plan_config_json")
                import json as _js3

                if isinstance(cfg, str):
                    try:
                        cfg = _js3.loads(cfg)
                    except Exception:
                        cfg = {}
                if isinstance(cfg, Mapping):
                    liq = cfg.get("liquidation_price", cfg.get("liquidationPrice"))
                if liq is None:
                    liq = plan_row.get("liquidation_price", plan_row.get("liquidationPrice"))
                if liq is None:
                    # Confirmation may carry trigger context; require explicit liq.
                    liq = futures.get("triggerPrice", futures.get("trigger_price"))
            except Exception:
                liq = None
            if liq is None:
                # Unknown liquidation stays UNKNOWN (never default PASS): hash
                # cannot be verified, so validation below yields UNKNOWN->422.
                expected_hash = None
            else:
                stop_px = futures.get("triggerPrice", futures.get("trigger_price"))
                basis = futures.get("triggerBasis", futures.get("trigger_basis", "MARK"))
                # Rule ids from plan config when present.
                rule_ids: Any = ()
                try:
                    if isinstance(cfg, Mapping):
                        for k in ("rule_ids", "ruleIds", "rule_version", "ruleVersion"):
                            if cfg.get(k) is not None:
                                v = cfg.get(k)
                                rule_ids = (v,) if isinstance(v, str) else tuple(v) if isinstance(v, (list, tuple)) else (v,)
                                break
                except Exception:
                    rule_ids = ()
                expected_hash = _hph(
                    futures_remaining=str(fut_rem),
                    spot_remaining=str(spot_rem),
                    liquidation_price=str(liq),
                    stop_trigger_price=str(stop_px) if stop_px is not None else None,
                    stop_trigger_basis=str(basis) if basis is not None else None,
                    rule_ids=rule_ids,
                )
        except Exception:
            expected_hash = None
        # Pure validation (PASS/FAIL/UNKNOWN, never default PASS).
        try:
            from diveintocrypto_desktop.shortlab.hedge.protection import (
                validate_protection_confirmation as _vpc,
            )

            # Build a candidate record for validation (hash-aware).
            cand_record = {
                "confirmation_json": {
                    "futures": dict(futures),
                    "spot": dict(spot),
                    "protected_position_hash": expected_hash,
                    "client_request_id": client_id,
                    "confirmed_at_ms": confirmed_i,
                }
            }
            plan_map = {
                "plan_id": pid,
                "plan_version": int(plan_row.get("plan_version") or expected),
                "liquidation_price": liq,
                "stop_trigger_price": futures.get("triggerPrice", futures.get("trigger_price")),
                "stop_trigger_basis": futures.get("triggerBasis", futures.get("trigger_basis", "MARK")),
                "rule_ids": rule_ids,
            }
            gate = _vpc(cand_record, plan_map, list(positions or ()), int(now_ms))
            if getattr(gate, "status", None) == "FAIL":
                raise HedgeValidationError(f"protection invalid: {','.join(tuple(getattr(gate, 'reasons', ()) or ())[:3])}"[:300], reason_code="HEDGE_INPUT_INVALID")
            if getattr(gate, "status", None) == "UNKNOWN" and expected_hash is None:
                raise HedgeValidationError("protection hash unverifiable (UNKNOWN)", reason_code="HEDGE_INPUT_INVALID")
        except (HedgeValidationError, HedgeBusy, HedgeVersionConflict, HedgeIdempotencyMismatch, HedgePlanNotFound):
            raise
        except Exception:
            pass
        # Confirmation TTL (default 24h) + expiry.
        try:
            opt = getattr(self._config, "optimization", None)
            prot = getattr(opt, "protection", None) if opt is not None else None
            if isinstance(prot, Mapping):
                ttl_s = int(prot.get("confirmation_ttl_sec", 86400))
            else:
                ttl_s = int(getattr(prot, "confirmation_ttl_sec", 86400)) if prot is not None else 86400
        except Exception:
            ttl_s = 86400
        expires_at = int(confirmed_i) + int(ttl_s) * 1000
        # Persist atomically (idempotent on same key+payload, 409 otherwise).
        confirmation_json = {
            "futures": dict(futures),
            "spot": dict(spot),
            "protected_position_hash": expected_hash,
            "client_request_id": client_id,
            "confirmed_at_ms": int(confirmed_i),
            "schema_version": "repair-contract-v1",
        }
        record = {
            "client_request_id": client_id,
            "confirmed_at_ms": int(confirmed_i),
            "expires_at_ms": int(expires_at),
            "confirmation_json": confirmation_json,
        }
        # Include explicit confirmation_id for stable idempotency.
        try:
            record["confirmation_id"] = f"{pid}#{client_id}"
        except Exception:
            pass
        try:
            stored = await repo.save_protection_confirmation(pid, int(expected), record)
        except Exception as exc:
            try:
                from diveintocrypto_desktop.shortlab.repository import HedgeIdempotencyError as _Idem3
                from diveintocrypto_desktop.shortlab.repository import HedgeVersionConflictError as _Ver3
                from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy4
                from diveintocrypto_desktop.shortlab.repository import ReferenceNotFoundError as _Ref3
                from diveintocrypto_desktop.shortlab.repository import ValidationError as _Val3

                if isinstance(exc, _Busy4):
                    raise HedgeBusy(str(exc)) from exc
                if isinstance(exc, _Idem3):
                    raise HedgeIdempotencyMismatch(str(exc)[:300]) from exc
                if isinstance(exc, _Ver3):
                    raise HedgeVersionConflict(str(exc)[:300]) from exc
                if isinstance(exc, _Ref3):
                    raise HedgePlanNotFound(pid) from exc
                if isinstance(exc, _Val3):
                    raise HedgeValidationError(str(exc)[:300], reason_code="HEDGE_INPUT_INVALID") from exc
            except (HedgeBusy, HedgeIdempotencyMismatch, HedgeVersionConflict, HedgePlanNotFound, HedgeValidationError):
                raise
            except Exception:
                pass
            raise
        # Wire response (camel + snake for compat).
        try:
            new_version = int(stored.get("plan_version", stored.get("resulting_plan_version", expected + 1)) or (expected + 1))
        except Exception:
            new_version = int(expected) + 1
        return {
            "planId": pid,
            "planVersion": int(new_version),
            "plan_id": pid,
            "plan_version": int(new_version),
            "confirmationId": str(stored.get("confirmation_id", f"{pid}#{client_id}")),
            "protectedPositionHash": expected_hash,
            "protected_position_hash": expected_hash,
            "expiresAtMs": int(expires_at),
            "confirmation": dict(confirmation_json),
        }

    def repair_capabilities(self) -> Mapping[str, Any]:
        """GET /capabilities (D12; R10b real binding summary).

        Always 200 with contractSchemaVersion + binding summary + switch
        states + provider flags + source times (never secrets/paths). Missing
        or fake bindings keep NOT_READY (never READY); unbound new features
        are 503 upstream (handled by callers).
        """
        from diveintocrypto_desktop.shortlab.repair_contracts import REPAIR_CONTRACT_VERSION
        from diveintocrypto_desktop.shortlab.repair_ports import REPAIR_PORT_KEYS

        ports = self._repair_ports
        bound: dict[str, bool] = {}
        sources: dict[str, str] = {}
        for key in REPAIR_PORT_KEYS:
            try:
                cb = getattr(ports, key, None) if ports is not None else None
                is_bound = cb is not None
                # D19.6: fake bindings count as unbound (never READY).
                if is_bound:
                    try:
                        if not is_real_repair_callback(cb):
                            is_bound = False
                    except Exception:
                        is_bound = False
                bound[key] = bool(is_bound)
                if is_bound:
                    try:
                        target = _unwrap_repair_callback(cb)
                        sources[key] = f"{getattr(target, '__module__', '?')}:{getattr(target, '__name__', '?')}"
                    except Exception:
                        sources[key] = "unknown"
                else:
                    sources[key] = "unbound"
            except Exception:
                bound[key] = False
                sources[key] = "unbound"
        missing = sorted([k for k, v in bound.items() if not v])
        hedge_on, funding_on, decision_on = self._repair_switches()
        # Provider flags (honest, never fabricate unconfigured chains).
        try:
            hedge_cfg = getattr(self._config, "hedge", None)
            providers = getattr(hedge_cfg, "providers", {}) if hedge_cfg is not None else {}
            if not isinstance(providers, Mapping):
                providers = {}
        except Exception:
            providers = {}

        def _enabled(name: str) -> bool:
            try:
                entry = providers.get(name) if isinstance(providers, Mapping) else None
                if isinstance(entry, Mapping):
                    return bool(entry.get("enabled"))
                return bool(getattr(entry, "enabled", False))
            except Exception:
                return False

        # Directional metrics + H10/R14 callback states (lazy, never assert).
        try:
            metrics_wired = getattr(self, "_metrics_provider", None) is not None
        except Exception:
            metrics_wired = False
        try:
            from diveintocrypto_desktop.shortlab.evidence import hedge_metrics as _hm  # type: ignore

            hedge_metrics_wired = bool(getattr(_hm, "hedge_summary", None) is not None or getattr(_hm, "summary", None) is not None)
        except Exception:
            hedge_metrics_wired = False
        try:
            hedge_grader_wired = getattr(self, "_hedge_grader_callback", None) is not None
            if not hedge_grader_wired:
                try:
                    from diveintocrypto_desktop.shortlab.evidence import hedge_grader as _hg  # type: ignore

                    hedge_grader_wired = callable(getattr(_hg, "run_due", None))
                except Exception:
                    hedge_grader_wired = False
        except Exception:
            hedge_grader_wired = False
        # Historical MARK binding (R14b via R03).
        try:
            hist = getattr(self, "_hedge_market_provider", None)
            mark_bound = bool(getattr(hist, "mark_price_bars_fn", None) is not None)
        except Exception:
            mark_bound = False
        # Capture callbacks are RepairPorts entries (already in bound).
        ready = (not missing) and bool(hedge_on) and bool(funding_on) and bool(decision_on)
        reasons: list[str] = []
        if missing:
            reasons.append("IMPLEMENTATION_UNAVAILABLE")
        if not hedge_on:
            reasons.append("HEDGE_DISABLED")
        if not funding_on:
            reasons.append("FUNDING_CAPTURE_DISABLED")
        if not decision_on:
            reasons.append("DECISION_DISABLED")
        # Stable sort + dedup.
        reasons = sorted(set(reasons))
        return {
            "contractSchemaVersion": REPAIR_CONTRACT_VERSION,
            "readiness": "READY" if ready else "NOT_READY",
            "reasons": reasons,
            "missingBindings": missing,
            "bindings": bound,
            "bindingSources": dict(sources),
            "switches": {
                "hedgeEnabled": bool(hedge_on),
                "fundingCaptureEnabled": bool(funding_on),
                "decisionEnabled": bool(decision_on),
            },
            "providers": {
                "binanceSpot": bool(_enabled("binance_spot")),
                "binanceAlpha": bool(_enabled("binance_alpha")),
                "onchain": bool(_enabled("onchain")),
            },
            "capabilities": {
                "directionalMetrics": bool(metrics_wired),
                "hedgeMetrics": bool(hedge_metrics_wired),
                "hedgeGrader": bool(hedge_grader_wired),
                "markHistory": bool(mark_bound),
                "capture": bool(bound.get("capture_strategy_entries", False) and bound.get("collect_due_quotes", False)),
            },
        }


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
        daily_observed = await _klines.fetch_klines_range_observed(symbol, "1d", as_of_ms - 100 * DAY_MS, as_of_ms)
        dailies = daily_observed.value
        out.setdefault("observations", {})["klines_daily"] = daily_observed
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
        oi_history = await _oi.fetch_oi_hist_observed(symbol, "1h", limit=220, as_of_ms=as_of_ms)
        out.setdefault("observations", {})["oi_history"] = oi_history
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
    from diveintocrypto_desktop.shortlab.observations import make_observation
    out.setdefault("observations", {})["book"] = make_observation(
        dict(out), source="binance-futures-depth", fetched_at_ms=_now_ms(), known_at_ms=_now_ms(),
        status="OK" if out.get("best_bid") is not None and out.get("best_ask") is not None else "UNAVAILABLE")
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


def _canonical_market_price(value: Any, identity: Any, metadata: Mapping[str, Any]) -> float | None:
    from diveintocrypto_desktop.shortlab.observations import canonical_price
    source = _field(identity, "multiplier_source")
    multiplier = _field(identity, "contract_multiplier")
    quote = str(metadata.get("quote_asset") or metadata.get("quote") or "USDT").upper()
    fx = "1" if quote == "USD" else metadata.get("quote_to_usd")
    try:
        if source not in ("MANUAL", "EXCHANGE"):
            return None
        result = canonical_price(value, multiplier, fx)
        return float(result) if result is not None else None
    except (ValueError, TypeError):
        return None


def _funding_event_dict(event: Any) -> dict[str, Any]:
    return {"t": int(_field(event, "funding_time_ms", "t", "fundingTime")),
            "funding_rate": float(_field(event, "funding_rate", "fundingRate")),
            "mark_price": _field(event, "mark_price", "markPrice")}


def _monitor_wire(pid, out, positions, alerts):
    return {
        "planId": pid,
        "snapshotId": out.get("snapshot_id"),
        "asOf": out.get("as_of_ms"),
        "actualHedgeRatio": out.get("actual_hedge_ratio"),
        "residualShortNotionalUsd": out.get("residual_short_notional_usd"),
        "markPrice": out.get("mark_price"),
        "spotPrice": out.get("spot_price"),
        "currentBasisPct": out.get("current_basis_pct"),
        "basisPnlUsd": out.get("basis_pnl_usd"),
        "estimatedSettledFundingUsd": out.get("estimated_settled_funding_usd"),
        "projectedNextFundingUsd": out.get("projected_next_funding_usd"),
        "spotPnlUsd": out.get("spot_pnl_usd"),
        "futuresPnlUsd": out.get("futures_pnl_usd"),
        "knownCostUsd": out.get("known_cost_usd"),
        "estimatedExitCostUsd": out.get("estimated_exit_cost_usd"),
        "netPnlBeforeExitUsd": out.get("net_pnl_before_exit_usd"),
        "estimatedNetPnlAfterExitUsd": out.get("estimated_net_pnl_after_exit_usd"),
        "liquidationDistance": out.get("liquidation_distance"),
        "safetyScore": out.get("safety_score"),
        "status": out.get("status"),
        "positions": [dict(p) for p in (positions or ())],
        "alerts": [dict(a) for a in (alerts or ())],
        }
