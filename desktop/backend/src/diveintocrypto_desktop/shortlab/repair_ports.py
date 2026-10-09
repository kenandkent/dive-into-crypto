"""R00 repair freeze: collaborator / repository / market ports (D19.1/D13.1).

- :class:`RepairPorts`: nine optional callbacks + :meth:`require`.
- :class:`RepositoryPort`: full D13.1 async protocol (20 methods incl. 2 month-budget).
- :class:`MarketPort`: three async collectors.

Production binds real implementations via Runtime (R10b); tests use
``desktop/backend/tests/repair_fixtures.make_ports`` fakes (never imported
by production).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

__all__ = [
    "REPAIR_PORT_KEYS",
    "RepairDependencyUnavailable",
    "RepairPorts",
    "RepositoryPort",
    "MarketPort",
]

#: Frozen nine callback keys (D19.1). Order is canonical for docs.
REPAIR_PORT_KEYS: tuple[str, ...] = (
    "compute_schedule_coverage",
    "evaluate_funding_entry_gate",
    "build_ratio_proposal",
    "compute_ledger_pnl",
    "build_pair_exit_guidance",
    "project_opportunity",
    "capture_strategy_entries",
    "collect_due_quotes",
    "simulate_hedge",
)


class RepairDependencyUnavailable(RuntimeError):
    """A required repair collaborator is not bound (HTTP 503 upstream)."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"repair collaborator {name!r} is not bound: "
            "IMPLEMENTATION_UNAVAILABLE"
        )
        self.port_name = name
        self.reason_code = "IMPLEMENTATION_UNAVAILABLE"


@dataclass(frozen=True)
class RepairPorts:
    """Immutable nine-callback bundle (all optional, default None).

    Frozen: only Runtime binds real implementations; test fakes never enter
    production. :meth:`require` raises :class:`RepairDependencyUnavailable`
    for unbound names.
    """

    compute_schedule_coverage: Callable[..., Any] | None = None
    evaluate_funding_entry_gate: Callable[..., Any] | None = None
    build_ratio_proposal: Callable[..., Any] | None = None
    compute_ledger_pnl: Callable[..., Any] | None = None
    build_pair_exit_guidance: Callable[..., Any] | None = None
    project_opportunity: Callable[..., Any] | None = None
    capture_strategy_entries: Callable[..., Any] | None = None
    collect_due_quotes: Callable[..., Any] | None = None
    simulate_hedge: Callable[..., Any] | None = None

    def require(self, name: str) -> Callable[..., Any]:
        if name not in REPAIR_PORT_KEYS:
            raise ValueError(f"unknown repair port {name!r}; keys={list(REPAIR_PORT_KEYS)}")
        callback = getattr(self, name)
        if callback is None:
            raise RepairDependencyUnavailable(name)
        return callback


@runtime_checkable
class RepositoryPort(Protocol):
    """D13.1 full async repository protocol (20 methods, incl. 2 budget).

    Records are D03 JSON mappings; saves return str IDs. Failures raise
    ValidationError / LocalWriteBusyError by the real repository (R01).
    """

    async def save_market_observation(self, record: Mapping[str, Any]) -> str: ...
    async def get_market_observation(self, id: str) -> Mapping[str, Any] | None: ...
    async def list_market_observations(
        self, symbol: str, kind: str, start_ms: int, end_ms: int, known_by_ms: int
    ) -> tuple[Mapping[str, Any], ...]: ...
    async def list_funding_observations(
        self, symbol: str, start_ms: int, end_ms: int, known_by_ms: int
    ) -> tuple[Mapping[str, Any], ...]: ...
    async def save_funding_schedule(self, record: Mapping[str, Any]) -> str: ...
    async def list_funding_schedules(
        self, symbol: str, known_by_ms: int
    ) -> tuple[Mapping[str, Any], ...]: ...
    async def save_fx_observation(self, record: Mapping[str, Any]) -> str: ...
    async def get_fx_at(
        self, currency: str, event_ms: int, known_by_ms: int, max_age_ms: int = 60000
    ) -> Mapping[str, Any] | None: ...
    async def save_hedge_decision(
        self, record: Mapping[str, Any], references: tuple[Mapping[str, Any], ...]
    ) -> str: ...
    async def get_hedge_decision(self, id: str) -> Mapping[str, Any] | None: ...
    async def save_protection_confirmation(
        self, plan_id: str, expected_version: int, record: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...
    async def get_protection_confirmation(
        self, plan_id: str
    ) -> Mapping[str, Any] | None: ...
    async def reserve_provider_request(
        self, provider: str, month_key: str, request_id: str, monthly_limit: int, as_of_ms: int
    ) -> Mapping[str, Any]: ...
    async def finish_provider_request(
        self, request_id: str, sent: bool, as_of_ms: int
    ) -> None: ...
    async def list_current_funding_opportunities(
        self, query: Any, as_of_ms: int
    ) -> Any: ...
    async def save_strategy_entry(
        self, record: Mapping[str, Any], references: tuple[Mapping[str, Any], ...]
    ) -> str: ...
    async def list_strategy_entries(
        self, cohort: str, start_ms: int, end_ms: int
    ) -> tuple[Mapping[str, Any], ...]: ...
    async def save_strategy_quote_task(self, record: Mapping[str, Any]) -> str: ...
    async def claim_due_quote_tasks(
        self, as_of_ms: int, limit: int = 20
    ) -> tuple[Mapping[str, Any], ...]: ...
    async def finish_quote_task(
        self, task_id: str, status: str, result: Mapping[str, Any]
    ) -> None: ...


@runtime_checkable
class MarketPort(Protocol):
    """Frozen market collectors (D19.1): futures/spot/funding."""

    async def collect_futures(
        self, symbol: str, contract_qty: str, request_context: Any
    ) -> Mapping[str, Any]: ...
    async def collect_spot(
        self, identity: Any, venue: str, canonical_qty: str, request_context: Any
    ) -> Any: ...
    async def collect_funding(
        self, symbol: str, as_of_ms: int, request_context: Any
    ) -> Any: ...
