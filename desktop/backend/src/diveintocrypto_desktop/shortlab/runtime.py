"""Short-Lab runtime lifecycle (Task 13, design section 22.1).

``ShortLabRuntime`` assembles config / repository / provider registry /
service / scheduler and mounts them on the existing FastAPI lifespan
(Task 14 calls :meth:`start` in startup and :meth:`stop` in shutdown --
the same process, no new service or port).

Availability contract (design 19.3): a DB open/migrate failure only marks
Short-Lab unavailable (``available == False``; every service entry raises
:class:`ShortLabUnavailable`). It never raises out of :meth:`start` and
never prevents the host Dive app from serving.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Mapping
from pathlib import Path

from diveintocrypto_desktop.shortlab.config import ShortLabConfig, load_shortlab_config
from diveintocrypto_desktop.shortlab.scheduler import ShortLabScheduler, default_jitter_fn
from diveintocrypto_desktop.shortlab.service import (
    JOB_TYPE_SCORE_REFRESH,
    SHORTLAB_UNAVAILABLE_REASON,
    ShortLabService,
    ShortLabUnavailable,
)

log = logging.getLogger(__name__)


def build_default_registry(config: ShortLabConfig, *, clock: Callable[[], int] | None = None) -> Any:
    """Default provider registry: CoinGecko when enabled, nothing else.

    Phase 5/6 providers (``unlock`` / ``social`` / ``catalyst``) stay
    unregistered until Task 17 wires them on this same runtime -- and this
    module never imports those future modules. Unregistered names resolve
    to the shared ``NullProvider`` (explicit ``UNAVAILABLE``), which keeps
    the effective tier at ``LITE``.
    """
    from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry

    registry = ProviderRegistry(clock=clock)
    providers = getattr(config, "providers", {}) or {}
    coingecko_cfg = providers.get("coingecko") if isinstance(providers, Mapping) else None
    enabled = bool(getattr(coingecko_cfg, "enabled", False)) if coingecko_cfg is not None else False
    if enabled:
        from diveintocrypto_desktop.shortlab.providers.coingecko import CoinGeckoProvider

        registry.register(
            "coingecko",
            CoinGeckoProvider(market_ttl_sec=config.refresh.fundamental_sec, clock=clock),
        )
    return registry


class ShortLabRuntime:
    """Lifecycle owner for the Short-Lab service + scheduler."""

    def __init__(
        self,
        *,
        config: ShortLabConfig | None = None,
        db_path: Path | str | None = None,
        data_dir: Path | str | None = None,
        frozen: bool = False,
        env: Mapping[str, str] | None = None,
        repository: Any | None = None,
        registry: Any | None = None,
        service: ShortLabService | None = None,
        scheduler: ShortLabScheduler | None = None,
        jitter_fn: Callable[[float], float] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._config = config
        self._db_path = Path(db_path).expanduser() if db_path is not None else None
        self._data_dir = Path(data_dir).expanduser() if data_dir is not None else None
        self._frozen = frozen
        self._env = env
        self._repository = repository
        self._owns_repository = repository is None
        self._registry = registry
        self._service = service
        self._scheduler = scheduler
        self._jitter_fn = jitter_fn or default_jitter_fn
        self._clock = clock
        self._available = False
        self._unavailable_reason = SHORTLAB_UNAVAILABLE_REASON
        self._started = False

    # -- introspection ---------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._available

    @property
    def unavailable_reason(self) -> str:
        return self._unavailable_reason

    @property
    def config(self) -> ShortLabConfig | None:
        return self._config

    @property
    def repository(self) -> Any | None:
        return self._repository

    @property
    def registry(self) -> Any | None:
        return self._registry

    @property
    def service(self) -> ShortLabService:
        if self._service is None:
            raise ShortLabUnavailable(self._unavailable_reason)
        return self._service

    @property
    def scheduler(self) -> ShortLabScheduler | None:
        return self._scheduler

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        """Open/migrate the DB, assemble the service, start periodic tasks.

        Migration failure only flips ``available`` to False and returns;
        the caller (FastAPI lifespan) keeps serving the legacy Dive API.
        """
        if self._started:
            return
        try:
            config = self._config or load_shortlab_config()
        except Exception as exc:  # noqa: BLE001 - bad config => unavailable, not fatal
            return self._mark_unavailable(f"config: {type(exc).__name__}: {exc}")
        self._config = config

        try:
            from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

            if self._repository is None:
                if self._db_path is not None:
                    self._repository = await ShortLabRepository.open(self._db_path)
                elif self._data_dir is not None:
                    self._repository = await ShortLabRepository.open(data_dir=self._data_dir)
                else:
                    self._repository = await ShortLabRepository.open(
                        frozen=self._frozen, env=self._env
                    )
                self._owns_repository = True
            version = await self._repository.migrate()
            log.info("shortlab database ready (schema version %s)", version)
        except Exception as exc:  # noqa: BLE001 - design 19.3: unavailable, never fatal
            return self._mark_unavailable(f"migration: {type(exc).__name__}: {str(exc)[:160]}")

        if self._registry is None:
            self._registry = build_default_registry(config, clock=self._clock)
        if self._service is None:
            self._service = ShortLabService(
                config=config,
                repository=self._repository,
                registry=self._registry,
                clock=self._clock,
                unavailable_reason=self._unavailable_reason,
            )
        else:
            self._service._repository = self._repository
        if self._scheduler is None:
            self._scheduler = ShortLabScheduler(
                jitter_fn=self._jitter_fn,
                default_jitter_max_sec=config.refresh.jitter_sec,
                clock=self._clock,
            )
        scheduler = self._scheduler
        service = self._service
        if JOB_TYPE_SCORE_REFRESH not in scheduler.job_types():
            scheduler.register(
                JOB_TYPE_SCORE_REFRESH,
                config.refresh.score_sec,
                lambda: service.run_refresh(JOB_TYPE_SCORE_REFRESH),
            )
        scheduler.start()
        self._available = True
        self._started = True
        log.info(
            "shortlab runtime started (tier default LITE, score every %ss, jitter ≤ %ss)",
            config.refresh.score_sec,
            config.refresh.jitter_sec,
        )

    def _mark_unavailable(self, reason: str) -> None:
        self._available = False
        self._unavailable_reason = reason[:300]
        log.warning("shortlab unavailable (%s); legacy Dive service unaffected", reason[:200])

    async def stop(self) -> None:
        """Cancel/await background tasks, then flush + close the DB worker."""
        scheduler, self._scheduler = self._scheduler, self._scheduler
        if scheduler is not None:
            try:
                await scheduler.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.warning("shortlab scheduler stop failed: %s", str(exc)[:150])
        repository, keep = self._repository, self._repository
        if keep is not None and self._owns_repository:
            try:
                await keep.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.warning("shortlab repository close failed: %s", str(exc)[:150])
        self._available = False
        self._started = False
