"""F07 grader background job: ``async run_due(context) -> JobStatus`` (design A8).

The runtime (F06b) registers this callback via
``service.register_grader_callback`` / ``register_run_due``; the job never
starts a second service and never imports ``scan/symbol_builder`` -- grading
reads only archived score snapshots plus post-score market data.

Behaviour:

- scans the default 180d ``SUCCEEDED`` history (``list_scores_for_evidence``)
  for scores whose horizon is due but which have no stored outcome row, and
  grades at most ``grader_batch_size`` of them per pass, oldest-due first;
- existing ``PENDING`` rows from ``list_due_scores`` are reported as queue
  depth (they are write-once in F01, so run_due never overwrites them --
  see ``grader.grade`` idempotency);
- not-due scores are skipped without minting ``PENDING`` rows, so a later
  terminal grade never hits the write-once guard;
- missing price/funding/mark yields ``UNAVAILABLE`` (never 0), reliable
  termination yields ``CENSORED``; only complete legs yield ``COMPLETE``.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from diveintocrypto_desktop.shortlab.evidence.grader import (
    FORMULA_VERSION,
    HORIZON_MS,
    default_cost_hash,
    horizon_due_ms,
)

_HORIZON_ORDER = ("7D", "30D", "90D")
_DAY_MS = 86_400_000
_DEFAULT_BATCH = 100
_JOB_TYPE = "grader"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _history_days(config: Any | None) -> int:
    try:
        days = int(getattr(getattr(config, "evidence", None), "default_history_days", 0))
    except (TypeError, ValueError):
        days = 0
    return days if days and days > 0 else 180


def _batch_size(config: Any | None) -> int:
    try:
        size = int(getattr(getattr(config, "evidence", None), "grader_batch_size", 0))
    except (TypeError, ValueError):
        size = 0
    return size if size and size > 0 else _DEFAULT_BATCH


def _cost_hash(config: Any | None) -> str:
    if config is not None:
        try:
            from diveintocrypto_desktop.shortlab.config import (
                cost_config_hash as _cost_hash,
            )

            return str(_cost_hash(config))
        except Exception:  # noqa: BLE001 - fall back to the default hash
            pass
    return default_cost_hash()


async def run_due(
    context: Any,
    *,
    klines_fn: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    funding_fn: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    lifecycle_fn: Callable[[str], Awaitable[Any] | Any] | None = None,
    config: Any | None = None,
    batch_size: int | None = None,
) -> Any:
    """Grade due-but-ungraded scores once per pass.

    ``context`` is the F06 :class:`JobContext` (repository, config,
    clock_ms, request_budget, trace_id, data_dir). The optional fetchers
    default to the grader's production market-data paths; tests inject
    fakes. Returns the existing :class:`JobStatus` shape.
    """
    from diveintocrypto_desktop.shortlab.service import JobStatus

    repository = getattr(context, "repository", None)
    cfg = config if config is not None else getattr(context, "config", None)
    clock = getattr(context, "clock_ms", None) or _now_ms
    trace = str(getattr(context, "trace_id", "") or f"{_JOB_TYPE}-{int(clock())}")
    job_id = trace
    now_ms = int(clock())
    limit = int(batch_size) if batch_size else _batch_size(cfg)
    digest = _cost_hash(cfg)

    if repository is None:
        return JobStatus(
            job_id=job_id, job_type=_JOB_TYPE, status="FAILED",
            stats={"graded": 0, "error": "NO_REPOSITORY"},
            error_code="NO_REPOSITORY",
        )

    started_ms = now_ms
    try:
        await repository.create_job_run(job_id, _JOB_TYPE, started_ms)
        created = True
    except Exception:  # noqa: BLE001 - retry of the same trace reuses the row
        created = False

    graded = 0
    complete = 0
    censored = 0
    unavailable = 0
    skipped_not_due = 0
    skipped_have_row = 0
    errors = 0

    # Queue depth per the F01 due contract (ordered due/symbol/score).
    try:
        due_rows = await repository.list_due_scores(now_ms, limit)
        queue_depth = len(due_rows)
    except Exception:  # noqa: BLE001 - queue read never fails the pass
        queue_depth = 0

    # Discovery: SUCCEEDED scores in the default history window lacking a
    # stored outcome for a now-due horizon. Oldest-due first.
    days = _history_days(cfg)
    window_from = now_ms - days * _DAY_MS
    candidates: list[tuple[int, str, str, Any, str]] = []
    offset = 0
    try:
        while True:
            page = await repository.list_scores_for_evidence(
                {"as_of_from_ms": window_from, "as_of_to_ms": now_ms}, 200, offset
            )
            items = list(page.items)
            for score in items:
                try:
                    as_of = int(score.as_of_ms)
                except (TypeError, ValueError):
                    continue
                for horizon in _HORIZON_ORDER:
                    try:
                        due_ms = horizon_due_ms(as_of, horizon)
                    except ValueError:
                        continue
                    if now_ms < due_ms:
                        continue
                    candidates.append(
                        (due_ms, str(score.symbol), str(score.snapshot_id), score, horizon)
                    )
            if len(items) < 200:
                break
            offset += 200
            if len(candidates) >= limit * len(_HORIZON_ORDER):
                break
    except Exception:  # noqa: BLE001 - discovery failure degrades to queue stats
        candidates = []

    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    from diveintocrypto_desktop.shortlab.evidence import grader as _grader

    for _due_ms, _symbol, _snapshot_id, score, horizon in candidates:
        if graded >= limit:
            break
        try:
            have = await repository.get_outcome(
                score.snapshot_id, horizon, FORMULA_VERSION, digest
            )
        except Exception:  # noqa: BLE001 - treat unreadable as missing
            have = None
        if have is not None:
            skipped_have_row += 1
            continue
        try:
            outcome = await _grader.grade(
                score.snapshot_id,
                horizon,
                now_ms,
                repository=repository,
                klines_fn=klines_fn,
                funding_fn=funding_fn,
                lifecycle_fn=lifecycle_fn,
                config=cfg,
            )
        except Exception:  # noqa: BLE001 - one bad score never fails the pass
            errors += 1
            continue
        graded += 1
        status = str(getattr(outcome, "outcome_status", ""))
        if status == "COMPLETE":
            complete += 1
        elif status == "CENSORED":
            censored += 1
        elif status == "UNAVAILABLE":
            unavailable += 1

    # Not-due count is informational only (no rows minted).
    _ = skipped_not_due

    finished_ms = int(clock())
    stats = {
        "graded": graded,
        "complete": complete,
        "censored": censored,
        "unavailable": unavailable,
        "queueDepth": queue_depth,
        "skippedHaveRow": skipped_have_row,
        "errors": errors,
        "windowDays": days,
    }
    try:
        if created:
            await repository.finish_job_run(
                job_id, "SUCCEEDED", finished_ms, stats=stats
            )
        else:
            try:
                await repository.finish_job_run(
                    job_id, "SUCCEEDED", finished_ms, stats=stats
                )
            except Exception:  # noqa: BLE001 - best effort on a reused trace
                pass
    except Exception:  # noqa: BLE001 - job bookkeeping never fails grading
        pass
    return JobStatus(
        job_id=job_id,
        job_type=_JOB_TYPE,
        status="SUCCEEDED",
        stats=stats,
        started_at_ms=started_ms,
        finished_at_ms=finished_ms,
    )


__all__ = ["run_due"]
