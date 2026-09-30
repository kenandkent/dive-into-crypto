"""Short-Lab in-process scheduler (Task 13, design sections 22-23).

V1 uses plain asyncio periodic tasks -- no Celery / Redis / APScheduler
server (design 22.2). One ``asyncio`` task per registered job; a per-job
async lock guarantees a single running instance per ``job_type`` so a
manual refresh and its scheduled twin reuse/serialize instead of scanning
the market twice (design 22.3).

Jitter (design 23): every *scheduled* execution independently draws
``jitter_fn(jitter_max_sec)`` before running; the production default is
``random.uniform(0, max_sec)``. Tests inject a fixed function. Manual
triggers (:meth:`ShortLabScheduler.trigger_now`) skip jitter but take the
same per-job lock, so they share mutual exclusion with the periodic path.
The shared upstream rate limiters live in the data clients themselves, so
both paths travel them; the scheduler never bypasses them.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

#: Async periodic body. Returns optional stats for observability; raised
#: errors are recorded on the job and never kill the periodic loop.
TickFn = Callable[[], Awaitable[Any]]
#: ``jitter_fn(max_sec) -> float``; production default is uniform(0, max).
JitterFn = Callable[[float], float]
#: Injectable sleep so tests never really wait out a 300s jitter boundary.
SleepFn = Callable[[float], Awaitable[None]]
#: UTC epoch milliseconds.
ClockMs = Callable[[], int]


def default_jitter_fn(max_sec: float) -> float:
    """Production jitter: ``uniform(0, max_sec)`` (design 23)."""
    try:
        cap = float(max_sec)
    except (TypeError, ValueError):
        return 0.0
    if not cap > 0.0:
        return 0.0
    return random.uniform(0.0, cap)


class UnknownJobError(KeyError):
    """A manual trigger named a job_type the scheduler never registered."""


@dataclass
class PeriodicJob:
    """One registered periodic job (mutable run counters, fixed schedule)."""

    job_type: str
    interval_sec: float
    func: TickFn
    jitter_max_sec: float = 0.0
    runs: int = 0
    last_run_ms: int | None = None
    last_error: str | None = None


class _LoopLocks:
    """Per-job locks keyed by running loop.

    Mirrors the ``_PerKeyLocks`` idiom in ``api/app.py``: successive
    requests (or test cases) may run on different event loops, and a bare
    ``asyncio.Lock`` must not be shared across them.
    """

    def __init__(self) -> None:
        self._by_loop: dict[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = {}

    def for_job(self, job_type: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        table = self._by_loop.setdefault(loop, {})
        lock = table.get(job_type)
        if lock is None:
            lock = table[job_type] = asyncio.Lock()
        return lock


class ShortLabScheduler:
    """Process-local periodic task runner for Short-Lab refresh jobs."""

    def __init__(
        self,
        *,
        jitter_fn: JitterFn | None = None,
        sleep: SleepFn | None = None,
        clock: ClockMs | None = None,
        default_jitter_max_sec: float = 300.0,
    ) -> None:
        self._jitter_fn: JitterFn = jitter_fn or default_jitter_fn
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._clock: ClockMs = clock or (lambda: time.time_ns() // 1_000_000)
        self._default_jitter_max = float(default_jitter_max_sec)
        self._jobs: dict[str, PeriodicJob] = {}
        self._tasks: list[asyncio.Task] = []
        self._locks = _LoopLocks()
        self._shutdown = asyncio.Event()
        self._started = False

    # -- registration ----------------------------------------------------
    def register(
        self,
        job_type: str,
        interval_sec: float,
        func: TickFn,
        *,
        jitter_max_sec: float | None = None,
    ) -> PeriodicJob:
        """Register one periodic job. Duplicate ``job_type`` is an error."""
        if job_type in self._jobs:
            raise ValueError(f"scheduler job {job_type!r} is already registered")
        if not callable(func):
            raise ValueError(f"scheduler job {job_type!r} func must be callable")
        try:
            interval = float(interval_sec)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"interval_sec for {job_type!r} must be a number") from exc
        if not interval >= 0:
            raise ValueError(f"interval_sec for {job_type!r} must be >= 0")
        job = PeriodicJob(
            job_type=job_type,
            interval_sec=interval,
            func=func,
            jitter_max_sec=(
                self._default_jitter_max if jitter_max_sec is None else float(jitter_max_sec)
            ),
        )
        self._jobs[job_type] = job
        return job

    def job_types(self) -> tuple[str, ...]:
        return tuple(self._jobs)

    def lock_for(self, job_type: str) -> asyncio.Lock:
        """The single-instance lock for ``job_type`` (shared with manual triggers)."""
        if job_type not in self._jobs:
            raise UnknownJobError(job_type)
        return self._locks.for_job(job_type)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Spawn one background task per job (non-blocking; first run inside the task)."""
        if self._started:
            return
        asyncio.get_running_loop()  # fail fast outside async context
        self._shutdown.clear()
        for job in self._jobs.values():
            self._tasks.append(asyncio.create_task(self._run_loop(job), name=f"shortlab-{job.job_type}"))
        self._started = True

    async def stop(self) -> None:
        """Signal shutdown, cancel every periodic task and await them.

        Awaiting (not abandoning) the tasks plus closing the repository in
        ``ShortLabRuntime.stop`` is the design 22.1 shutdown contract.
        """
        self._shutdown.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._started = False

    @property
    def running(self) -> bool:
        return self._started and any(not t.done() for t in self._tasks)

    def pending_tasks(self) -> tuple[asyncio.Task, ...]:
        return tuple(t for t in self._tasks if not t.done())

    # -- manual trigger ------------------------------------------------------
    async def trigger_now(self, job_type: str) -> Any:
        """Run one job immediately: no jitter, same per-job lock.

        Raises :class:`UnknownJobError` for unregistered jobs; the job's own
        errors propagate to the caller (manual refresh is user-visible).
        """
        job = self._jobs.get(job_type)
        if job is None:
            raise UnknownJobError(job_type)
        async with self._locks.for_job(job_type):
            return await self._run_once(job)

    # -- internals -------------------------------------------------------------
    async def _run_once(self, job: PeriodicJob) -> Any:
        out = await job.func()
        job.runs += 1
        job.last_run_ms = int(self._clock())
        job.last_error = None
        return out

    async def _run_loop(self, job: PeriodicJob) -> None:
        first = True
        while not self._shutdown.is_set():
            try:
                if not first:
                    await self._sleep(job.interval_sec)
                first = False
                if self._shutdown.is_set():
                    break
                # Independent jitter draw before EVERY planned execution.
                delay = self._jitter_fn(job.jitter_max_sec)
                try:
                    delay_s = float(delay)
                except (TypeError, ValueError):
                    delay_s = 0.0
                if delay_s > 0:
                    await self._sleep(delay_s)
                if self._shutdown.is_set():
                    break
                async with self._locks.for_job(job.job_type):
                    if self._shutdown.is_set():
                        break
                    await self._run_once(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - periodic ticks must survive
                job.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning(
                    "shortlab scheduler job=%s failed (%s); next run in %.0fs",
                    job.job_type,
                    job.last_error,
                    job.interval_sec,
                )

    def job_stats(self, job_type: str) -> dict[str, Any]:
        job = self._jobs.get(job_type)
        if job is None:
            raise UnknownJobError(job_type)
        return {
            "job_type": job.job_type,
            "interval_sec": job.interval_sec,
            "jitter_max_sec": job.jitter_max_sec,
            "runs": job.runs,
            "last_run_ms": job.last_run_ms,
            "last_error": job.last_error,
        }
