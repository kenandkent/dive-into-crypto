"""Base retention sweep (F09, design A9.4) + R01 pin extension (D13.2).

``async maintain(context) -> JobStatus`` is the F09 retention callback handed
to F06b via ``service.register_retention_callback`` /
``service.register_maintain``. It only calls the F01-delivered
``repository.maintain_retention(policy, now_ms, limit)`` — no new queries,
no second DB connection. H01 extends the same repository
method with 005 pin protection; R01 extends it with 006 pin protection
(market/FX/decision/entry/task + budget month GC, reference re-check in the
same worker transaction). This callback signature stays unchanged.

Semantics (base gate, schema 4; R01 adds 006 TTLs in the repository):

- base snapshots keep at least ``snapshot_min_days`` (default 180d);
  the repository always retains the latest SUCCEEDED generation, any score
  referenced by a forward outcome and ``CONFLICT`` observations regardless
  of TTL;
- R01: active plans and Evidence-referenced snapshots stay pinned; the
  repository enforces BOOK 3d, MARK/TICKER 14d, OI/RULES 180d,
  MARK_BAR_1H 365d, unreferenced Decision 30d, Entry/Task 365d, budget
  month GC 180d; ledger/plans never auto-removed;
- each call deletes at most ``batch_delete_limit`` rows clamped to 1..1000;
- the sweep runs on the repository single-writer queue
  (``PRIORITY_RETENTION`` with 30s aging).
"""

from __future__ import annotations

import time
from typing import Any

DAY_MS = 86_400_000
MAX_BATCH_LIMIT = 1000

# R01 pin/TTL reference (D13.2/D19.4, enforced inside the repository; the
# callback below keeps its F09 signature and only forwards policy/limit).
R01_BOOK_TTL_MS = 3 * DAY_MS
R01_MARK_TTL_MS = 14 * DAY_MS
R01_OI_TTL_MS = 180 * DAY_MS
R01_MARK_BAR_TTL_MS = 365 * DAY_MS
R01_DECISION_TTL_MS = 30 * DAY_MS
R01_ENTRY_TTL_MS = 365 * DAY_MS
R01_BUDGET_GC_DAYS = 180


def retention_ttl_ms(config: Any) -> int:
    """Base TTL in ms derived from ``maintenance.snapshot_min_days``."""
    days = 180
    try:
        maintenance = getattr(config, "maintenance", None)
        if maintenance is not None:
            raw = getattr(maintenance, "snapshot_min_days", None)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
                days = int(raw)
    except Exception:
        pass
    return int(days) * DAY_MS


def retention_limit(config: Any) -> int:
    """Per-call delete cap from ``maintenance.batch_delete_limit`` (1..1000)."""
    limit = MAX_BATCH_LIMIT
    try:
        maintenance = getattr(config, "maintenance", None)
        if maintenance is not None:
            raw = getattr(maintenance, "batch_delete_limit", None)
            if isinstance(raw, int) and not isinstance(raw, bool):
                limit = int(raw)
    except Exception:
        pass
    if limit < 1:
        return 1
    if limit > MAX_BATCH_LIMIT:
        return MAX_BATCH_LIMIT
    return int(limit)


def retention_policy_for_config(config: Any) -> dict[str, int]:
    """Build the ``maintain_retention`` policy mapping for the base gate."""
    ttl = retention_ttl_ms(config)
    return {
        "funding_observation_ttl_ms": int(ttl),
        "cursor_ttl_ms": int(ttl),
        "score_ttl_ms": int(ttl),
    }


async def maintain(context: Any) -> Any:
    """Run one bounded retention sweep under ``context`` (F06b wiring).

    Consumes the F06-provided :class:`JobContext` (``repository``,
    ``config``, ``clock_ms``, ``trace_id``). Never constructs its own
    context and never opens a second DB connection.
    """
    repo = getattr(context, "repository", None)
    config = getattr(context, "config", None)
    clock_ms = getattr(context, "clock_ms", None)
    trace_id = str(getattr(context, "trace_id", "") or "maintenance")
    try:
        now_ms = int(clock_ms()) if callable(clock_ms) else (time.time_ns() // 1_000_000)
    except Exception:
        now_ms = time.time_ns() // 1_000_000
    from diveintocrypto_desktop.shortlab.service import JobStatus

    if repo is None:
        return JobStatus(
            job_id=str(trace_id),
            job_type="maintenance",
            status="FAILED",
            stats={"deleted": 0, "retained": 0, "errors": 1},
            error_code="SHORTLAB_UNAVAILABLE",
        )
    policy = retention_policy_for_config(config)
    limit = retention_limit(config)
    try:
        stats = await repo.maintain_retention(policy, int(now_ms), limit)
    except Exception as exc:  # jobs record, never escape
        return JobStatus(
            job_id=str(trace_id),
            job_type="maintenance",
            status="FAILED",
            stats={"deleted": 0, "retained": 0, "errors": 1},
            error_code=type(exc).__name__,
        )
    try:
        deleted = int(getattr(stats, "deleted", 0))
        retained = int(getattr(stats, "retained", 0))
        errors = int(getattr(stats, "errors", 0))
        as_of = int(getattr(stats, "as_of_ms", now_ms))
    except Exception:
        deleted, retained, errors, as_of = 0, 0, 0, int(now_ms)
    return JobStatus(
        job_id=str(trace_id),
        job_type="maintenance",
        status="SUCCEEDED",
        stats={
            "deleted": deleted,
            "retained": retained,
            "errors": errors,
            "as_of_ms": as_of,
            "ttl_ms": int(policy["score_ttl_ms"]),
            "limit": int(limit),
        },
    )
