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
    JOB_TYPE_CONTRACT_REFRESH,
    JOB_TYPE_FUNDING_BACKFILL,
    JOB_TYPE_GRADER,
    JOB_TYPE_HEDGE_MONITOR,
    JOB_TYPE_HEDGE_OPPORTUNITY,
    JOB_TYPE_HEDGE_SETTLEMENT,
    JOB_TYPE_HEDGE_VENUE,
    JOB_TYPE_MAINTENANCE,
    JOB_TYPE_METADATA,
    JOB_TYPE_SCORE_REFRESH,
    SHORTLAB_UNAVAILABLE_REASON,
    JobContext,
    ShortLabService,
    ShortLabUnavailable,
)

__all__ = [
    "JobContext",
    "ShortLabRuntime",
    "build_default_registry",
    "build_default_request_budget",
    "build_default_observed_cache",
    "build_default_identity_catalog",
    "build_default_quality_policy",
]

log = logging.getLogger(__name__)


def _provider_api_key(
    provider_cfg: Any, env: Mapping[str, str] | None
) -> str | None:
    """Resolve the configured key env var (name only in config); ``None``
    when the provider needs no key or the variable is unset/empty."""
    api_key_env = getattr(provider_cfg, "api_key_env", None)
    if not api_key_env:
        return None
    if env is not None and str(api_key_env) in env:
        value = env[str(api_key_env)]
    else:
        import os as _os

        value = _os.environ.get(str(api_key_env))
    if isinstance(value, str) and value.strip():
        return value
    return None


def build_default_registry(
    config: ShortLabConfig,
    *,
    clock: Callable[[], int] | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Default provider registry: CoinGecko when enabled, plus the Task 17
    Phase 5/6 providers (``unlock`` / ``social`` / ``catalyst``) when each
    is enabled *and* its key (if any) resolves.

    Future provider modules are imported lazily and only on the enabled
    branch, so a LITE-only runtime never imports them. Anything
    unregistered resolves to the shared ``NullProvider`` (explicit
    ``UNAVAILABLE``), which keeps the effective tier at ``LITE`` with
    ``FULL_PREREQUISITE_MISSING``.
    """
    from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry

    registry = ProviderRegistry(clock=clock)
    providers = getattr(config, "providers", {}) or {}

    def _enabled(name: str) -> bool:
        cfg = providers.get(name) if isinstance(providers, Mapping) else None
        return bool(getattr(cfg, "enabled", False))

    if _enabled("coingecko"):
        from diveintocrypto_desktop.shortlab.providers.coingecko import CoinGeckoProvider

        registry.register(
            "coingecko",
            CoinGeckoProvider(market_ttl_sec=config.refresh.fundamental_sec, clock=clock),
        )
    if _enabled("unlock"):
        from diveintocrypto_desktop.shortlab.providers.unlock import UnlockProvider

        key = _provider_api_key(providers.get("unlock"), env)
        if key is not None:
            registry.register("unlock", UnlockProvider(api_key=key, clock=clock))
    if _enabled("social"):
        from diveintocrypto_desktop.shortlab.providers.social import SocialProvider

        key = _provider_api_key(providers.get("social"), env)
        if key is not None:
            registry.register("social", SocialProvider(api_key=key, clock=clock))
    if _enabled("catalyst"):
        from diveintocrypto_desktop.shortlab.providers.catalyst import CatalystProvider

        key = _provider_api_key(providers.get("catalyst"), env)
        # Catalyst needs no key (api_key_env null): enabled alone registers.
        registry.register("catalyst", CatalystProvider(api_key=key, clock=clock))
    return registry


def build_default_request_budget(
    config: ShortLabConfig,
    *,
    clock: Callable[[], int] | None = None,
) -> Any:
    """Default shared send budget: 240/60s host + 80/300s funding (F03)."""
    from diveintocrypto_desktop.shortlab.request_budget import RequestBudget

    return RequestBudget(max_sends=240, window_ms=60_000, clock=clock)


def build_default_observed_cache(
    *,
    clock: Callable[[], int] | None = None,
) -> Any:
    """Default shared Observed cache (F06 holds the instance, F03 contract)."""
    from diveintocrypto_desktop.shortlab.request_budget import ObservedCache

    return ObservedCache(clock=clock)


def build_default_identity_catalog(
    config: ShortLabConfig | None = None,
    *,
    clock: Callable[[], int] | None = None,
) -> Any:
    """Default identity directory: verified set first, network on refresh.

    Keyless stays UNCONFIGURED with zero sends (F04 contract); the local
    verified set keeps serving either way.
    """
    from diveintocrypto_desktop.shortlab.identity.catalog import IdentityCatalog

    return IdentityCatalog(clock=clock)


def build_default_quality_policy(config: ShortLabConfig) -> Any:
    """One frozen DQ-policy read from the validated config (F05/F06 handoff)."""
    from diveintocrypto_desktop.shortlab.config import policy_hash as _policy_hash
    from diveintocrypto_desktop.shortlab.quality import quality_policy_from_config

    return quality_policy_from_config(config, policy_hash=_policy_hash(config))


class ShortLabRuntime:
    """Lifecycle owner for the Short-Lab service + scheduler (F06b contract).

    Injects IdentityCatalog / RequestBudget / ObservedCache / Repository /
    QualityPolicy / clock and wires them by default. Hands every background
    domain a frozen :class:`JobContext` (repository, config, clock_ms,
    request_budget, trace_id, data_dir); job adapters share the unified
    ``async run(context) -> JobStatus`` shape and the per-job mutual-exclusion
    slot. F06b wires the grader (F07 ``run_due``) and retention (F09
    ``maintain``) callbacks on the single shared service instance and
    registers their scheduler jobs -- never a second service.
    """

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
        identity_catalog: Any | None = None,
        request_budget: Any | None = None,
        observed_cache: Any | None = None,
        quality_policy: Any | None = None,
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
        self._identity_catalog = identity_catalog
        self._request_budget = request_budget
        self._observed_cache = observed_cache
        self._quality_policy = quality_policy
        self._available = False
        self._unavailable_reason = SHORTLAB_UNAVAILABLE_REASON
        self._started = False
        # Pre-start custom callbacks (register_* before start() when no
        # service exists yet). Applied in start() before F06b defaults so an
        # explicit override is never clobbered by the default wiring.
        self._pending_grader_callback: Any | None = None
        self._pending_retention_callback: Any | None = None

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
        return self._quality_policy

    @property
    def hedge_available(self) -> bool:
        """Whether Hedge tables are usable (005 migrated); base stays available otherwise."""
        try:
            svc = self._service
            if svc is None:
                return False
            flag = getattr(svc, "_hedge_available", None)
            if flag is True:
                return True
            if flag is False:
                return False
            # Auto-probe defaults to disabled until start() sets it.
            return False
        except Exception:
            return False

    @property
    def hedge_capabilities(self) -> dict[str, Any]:
        """Honest chain capabilities (never fabricate an unconfigured chain)."""
        try:
            cfg = self._config
            hedge = getattr(cfg, "hedge", None) if cfg is not None else None
            providers = getattr(hedge, "providers", {}) if hedge is not None else {}
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

        return {
            "hedge": bool(self.hedge_available),
            "binanceSpot": bool(_enabled("binance_spot")),
            "binanceAlpha": bool(_enabled("binance_alpha")),
            "onchain": bool(_enabled("onchain")),
        }

    @property
    def data_dir(self) -> Path | None:
        return self._data_dir

    def _clock_ms(self) -> int:
        if self._clock is not None:
            try:
                return int(self._clock())
            except Exception:
                pass
        import time as _time

        return _time.time_ns() // 1_000_000

    def make_context(self, job_type: str, trace_id: str | None = None) -> JobContext:
        """Build the per-run :class:`JobContext` for ``job_type`` (F06 contract)."""
        import time as _time

        tid = trace_id or f"{job_type}-{self._clock_ms()}"
        clock_fn = self._clock if self._clock is not None else (lambda: _time.time_ns() // 1_000_000)
        return JobContext(
            repository=self._repository,
            config=self._config,
            clock_ms=clock_fn,
            request_budget=self._request_budget,
            trace_id=str(tid),
            data_dir=self._data_dir,
            observed_cache=self._observed_cache,
            identity_catalog=self._identity_catalog,
            quality_policy=self._quality_policy,
        )

    def register_grader_callback(self, fn: Any | None) -> None:
        """F07 slot: ``async run_due(context) -> JobStatus`` (F06b wires default)."""
        if fn is not None and not callable(fn):
            raise ValueError("grader callback must be callable or None")
        if self._service is not None:
            self._service.register_grader_callback(fn)
        else:
            # No service yet (pre-start): remember for start() wiring.
            self._pending_grader_callback = fn

    def register_retention_callback(self, fn: Any | None) -> None:
        """F09 slot: ``async maintain(context) -> JobStatus`` (F06b wires default)."""
        if fn is not None and not callable(fn):
            raise ValueError("retention callback must be callable or None")
        if self._service is not None:
            self._service.register_retention_callback(fn)
        else:
            self._pending_retention_callback = fn

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
            from diveintocrypto_desktop.shortlab.repository import (
                ShortLabRepository,
                schema_target_for_config,
            )

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
            # H08: base 004 first, hedge 005 separately. A 005 failure only
            # disables Hedge; 001-004 keep serving (B28/B29, A9.4).
            hedge_available = False
            try:
                base_version = await self._repository.migrate(target_version=4)
            except Exception as exc:
                return self._mark_unavailable(f"migration: {type(exc).__name__}: {str(exc)[:160]}")
            try:
                want = int(schema_target_for_config(config))
            except Exception:
                want = 4
            if want >= 5:
                try:
                    version = await self._repository.migrate(target_version=want)
                    hedge_available = True
                    log.info("shortlab database ready (schema version %s, hedge on)", version)
                except Exception as exc:  # noqa: BLE001 - 005 failure only bans hedge
                    log.warning(
                        "shortlab hedge migration to 5 failed, hedge disabled (base 4 serves): %s",
                        str(exc)[:200],
                    )
                    hedge_available = False
                    version = base_version
            else:
                version = base_version
                log.info("shortlab database ready (schema version %s, hedge off)", version)
        except Exception as exc:  # noqa: BLE001 - design 19.3: unavailable, never fatal
            return self._mark_unavailable(f"migration: {type(exc).__name__}: {str(exc)[:160]}")

        if self._registry is None:
            self._registry = build_default_registry(config, clock=self._clock, env=self._env)
        # F06a default wiring: shared budget / cache / catalog / policy.
        if self._request_budget is None:
            try:
                self._request_budget = build_default_request_budget(config, clock=self._clock)
            except Exception:
                self._request_budget = None
        if self._observed_cache is None:
            try:
                self._observed_cache = build_default_observed_cache(clock=self._clock)
            except Exception:
                self._observed_cache = None
        if self._identity_catalog is None:
            try:
                self._identity_catalog = build_default_identity_catalog(config, clock=self._clock)
            except Exception:
                self._identity_catalog = None
        if self._quality_policy is None:
            try:
                self._quality_policy = build_default_quality_policy(config)
            except Exception:
                self._quality_policy = None
        # Crash recovery: leftover RUNNING rows become API-compat FAILED /
        # PROCESS_INTERRUPTED before new jobs start (F01.3 / A7.3).
        try:
            recovered = await self._repository.recover_running_jobs(self._clock_ms())
            if recovered:
                log.info("shortlab recovered %d interrupted job(s)", int(recovered))
        except Exception as exc:  # noqa: BLE001 - recovery never blocks startup
            log.debug("recover running jobs skipped: %s", str(exc)[:120])
        if self._service is None:
            self._service = ShortLabService(
                config=config,
                repository=self._repository,
                registry=self._registry,
                clock=self._clock,
                unavailable_reason=self._unavailable_reason,
                identity_catalog=self._identity_catalog,
                request_budget=self._request_budget,
                observed_cache=self._observed_cache,
                quality_policy=self._quality_policy,
                data_dir=self._data_dir,
            )
        else:
            self._service._repository = self._repository
            # Inject F06a handles into an externally provided service when it
            # does not already carry them (tests inject fakes + handles).
            for _name, _value in (
                ("_identity_catalog", self._identity_catalog),
                ("_request_budget", self._request_budget),
                ("_observed_cache", self._observed_cache),
                ("_quality_policy", self._quality_policy),
            ):
                try:
                    if getattr(self._service, _name, None) is None and _value is not None:
                        setattr(self._service, _name, _value)
                except Exception:
                    pass
            try:
                if getattr(self._service, "_data_dir", None) is None and self._data_dir is not None:
                    self._service._data_dir = self._data_dir
            except Exception:
                pass
        from diveintocrypto_desktop.shortlab.hedge.market import ProductionHedgeMarket
        from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider
        from diveintocrypto_desktop.shortlab.hedge.jobs import HedgeJobs
        service = self._service
        market = getattr(service, "_hedge_market", None)
        if market is None:
            market = ProductionHedgeMarket(service, config, self._repository,
                self._request_budget, self._clock_ms, env=self._env)
            service._hedge_market = market
        for name, callback in (("_hedge_mark_fn", market.mark), ("_hedge_quote_fn", market.quote),
                               ("_hedge_funding_fn", market.funding)):
            if getattr(service, name, None) is None:
                setattr(service, name, callback)
        service._hedge_market_provider = RepositoryHistoricalMarketProvider(self._repository)
        if getattr(service, "_hedge_jobs", None) is None:
            HedgeJobs(service)
        if self._scheduler is None:
            self._scheduler = ShortLabScheduler(
                jitter_fn=self._jitter_fn,
                default_jitter_max_sec=config.refresh.jitter_sec,
                clock=self._clock,
            )
        scheduler = self._scheduler
        service = self._service
        # H08: publish the hedge gate on the shared service (005 failure only
        # bans Hedge; base 004 keeps serving). An explicitly injected flag
        # wins so tests can force either branch.
        try:
            if getattr(service, "_hedge_available", None) is None:
                service._hedge_available = bool(hedge_available)  # type: ignore[attr-defined]
        except Exception:
            pass
        # F06b default callback wiring on the single shared service instance.
        # Pre-start overrides win; otherwise import the F07/F09 owners. Never
        # constructs a second service -- callbacks run as
        # ``async run(context) -> JobStatus`` under the runtime JobContext.
        if self._pending_grader_callback is not None:
            try:
                if service.grader_callback is None:
                    service.register_grader_callback(self._pending_grader_callback)
            except Exception:
                pass
        if self._pending_retention_callback is not None:
            try:
                if service.retention_callback is None:
                    service.register_retention_callback(self._pending_retention_callback)
            except Exception:
                pass
        if service.grader_callback is None:
            try:
                from diveintocrypto_desktop.shortlab.evidence.jobs import (
                    run_due as _default_grader,
                )

                service.register_grader_callback(_default_grader)
            except Exception as exc:  # noqa: BLE001 - wiring is observable via callback
                log.warning("shortlab default grader wiring skipped: %s", str(exc)[:150])
        if service.retention_callback is None:
            try:
                from diveintocrypto_desktop.shortlab.maintenance import (
                    maintain as _default_maintain,
                )

                service.register_retention_callback(_default_maintain)
            except Exception as exc:  # noqa: BLE001 - wiring is observable via callback
                log.warning("shortlab default retention wiring skipped: %s", str(exc)[:150])
        # H08/H10: hedge grader is lazy (try import, missing means capability
        # disabled + honest 503/UNCONFIGURED, never assert its existence).
        # The combined forward grader (service.run_due) keeps the directional
        # phase alive when hedge is disabled.
        if getattr(service, "hedge_grader_callback", None) is None:
            try:
                from diveintocrypto_desktop.shortlab.evidence import hedge_grader as _hg  # type: ignore[import-not-found]

                _hedge_run = getattr(_hg, "run_due", None)
                if callable(_hedge_run) and bool(getattr(service, "_hedge_available", False)):
                    service.register_hedge_grader_callback(_hedge_run)
            except Exception:
                pass
        # F06b jobs: base-run (A7.3 defaults) + grader/retention callbacks.
        # All share ``service.run_refresh`` (per-job mutual exclusion) with
        # jitter from refresh.jitter_sec.
        _ingestion = getattr(config, "ingestion", None)
        _contract_sec = 1800.0
        _funding_sec = 300.0
        _grader_sec = 21600.0
        _retention_sec = 86400.0
        try:
            if _ingestion is not None:
                _contract_sec = float(getattr(_ingestion, "contract_refresh_sec", 1800.0))
                _funding_sec = float(getattr(_ingestion, "funding_backfill_sec", 300.0))
        except (TypeError, ValueError):
            pass
        try:
            _grader_sec = float(getattr(config.refresh, "grader_sec", 21600.0))
        except (TypeError, ValueError):
            pass
        try:
            _maintenance = getattr(config, "maintenance", None)
            if _maintenance is not None:
                _retention_sec = float(getattr(_maintenance, "retention_sec", 86400.0))
        except (TypeError, ValueError):
            pass
        _defaults: tuple[tuple[str, float], ...] = (
            (JOB_TYPE_SCORE_REFRESH, float(config.refresh.score_sec)),
            (JOB_TYPE_FUNDING_BACKFILL, float(_funding_sec)),
            (JOB_TYPE_CONTRACT_REFRESH, float(_contract_sec)),
            (JOB_TYPE_METADATA, float(_contract_sec)),
            (JOB_TYPE_GRADER, float(_grader_sec)),
            (JOB_TYPE_MAINTENANCE, float(_retention_sec)),
        )
        for _job_type, _interval in _defaults:
            if _job_type not in scheduler.job_types():
                _jt = _job_type  # late-binding guard for the closure
                scheduler.register(
                    _jt,
                    _interval,
                    lambda jt=_jt: service.run_refresh(jt),
                )
        # H08 hedge jobs (B30): opportunity + venue on the ordinary jitter
        # budget; active monitor + settlement check explicitly jitter 0 (never
        # inherit the 300s ordinary jitter). Same-asset collection is shared
        # per tick (service.run_hedge_monitor dedups by symbol); different
        # quantities keep distinct VWAP keys. Disabled hedge registers
        # nothing (base jobs keep serving).
        if bool(getattr(service, "_hedge_available", False)):
            try:
                hedge_cfg = getattr(config, "hedge", None)
                refresh = getattr(hedge_cfg, "refresh", {}) if hedge_cfg is not None else {}
                if not isinstance(refresh, Mapping):
                    try:
                        refresh = dict(refresh)
                    except Exception:
                        refresh = {}

                def _hedge_sec(name: str, default: float) -> float:
                    try:
                        value = refresh.get(name, default) if isinstance(refresh, Mapping) else default
                        return float(value)
                    except (TypeError, ValueError):
                        return float(default)

                _opp_sec = _hedge_sec("opportunity_sec", 1800.0)
                _venue_sec = _hedge_sec("contract_status_sec", 600.0)
                _mark_sec = _hedge_sec("active_mark_sec", 10.0)
                _settle_sec = _hedge_sec("funding_settlement_check_sec", 30.0)
            except Exception:
                _opp_sec, _venue_sec, _mark_sec, _settle_sec = 1800.0, 600.0, 10.0, 30.0
            _hedge_jobs: tuple[tuple[str, float, float | None], ...] = (
                (JOB_TYPE_HEDGE_OPPORTUNITY, float(_opp_sec), None),
                (JOB_TYPE_HEDGE_VENUE, float(_venue_sec), None),
                (JOB_TYPE_HEDGE_MONITOR, float(_mark_sec), 0.0),
                (JOB_TYPE_HEDGE_SETTLEMENT, float(_settle_sec), 0.0),
            )
            for _job_type, _interval, _jitter in _hedge_jobs:
                if _job_type not in scheduler.job_types():
                    _jt = _job_type
                    _ji = _interval
                    _jj = _jitter
                    if _jj is None:
                        scheduler.register(_jt, _ji, lambda jt=_jt: service.run_refresh(jt))
                    else:
                        scheduler.register(
                            _jt, _ji, lambda jt=_jt: service.run_refresh(jt),
                            jitter_max_sec=float(_jj),
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
        """A7.3 shutdown: stop scheduler, await service jobs, then close DB."""
        scheduler = self._scheduler
        if scheduler is not None:
            try:
                await scheduler.stop()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.warning("shortlab scheduler stop failed: %s", str(exc)[:150])
        service = self._service
        if service is not None:
            try:
                await service.shutdown()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.warning("shortlab service shutdown failed: %s", str(exc)[:150])
        repository, keep = self._repository, self._repository
        if keep is not None and self._owns_repository:
            try:
                # Drop orphaned queue entries left by cancelled scheduler ticks.
                # Repository._run removes its entry only on the acquire path;
                # a task cancelled while waiting on the cond leaves the entry
                # behind with no owner. After scheduler.stop + service.shutdown
                # no legitimate pending should remain (all service tasks are
                # done), so clearing unblocks close() instead of hanging.
                # F06b exposes this via fast DB-only grader/maintenance ticks
                # under fake sleeps; production (real sleeps) never hits it.
                try:
                    _pending = getattr(keep, "_pending", None)
                    _cond = getattr(keep, "_cond", None)
                    if _pending is not None and _cond is not None and len(_pending):
                        _running = getattr(service, "_running", {}) if service is not None else {}
                        _alive = False
                        try:
                            for _slot in list(_running.values()):
                                _task = _slot.get("task") if isinstance(_slot, dict) else None
                                if _task is not None and not _task.done():
                                    _alive = True
                                    break
                        except Exception:
                            _alive = True
                        if not _alive:
                            async with _cond:
                                if len(_pending):
                                    _pending.clear()
                                    _cond.notify_all()
                except Exception:
                    pass
                await keep.close()
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                log.warning("shortlab repository close failed: %s", str(exc)[:150])
        self._available = False
        self._started = False
