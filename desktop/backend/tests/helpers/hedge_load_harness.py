"""H07 load harness: virtual clock, controllable worker and fixtures.

H07 owns the deterministic single-process harness (fake timers only); H08
adds the real DuckDB worker integration in
``test_shortlab_hedge_worker_integration.py`` without mocking the
production monitor functions. The default load is 10 active symbols
(including multi-plan symbols), 500 frozen scoring inputs, a 10s
in-memory tick and a 60s (or risk-change) persist sampler -- all driven by
the test clock, never by real 10s x 500 waits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping

__all__ = [
    "InjectedClock",
    "FakeWorker",
    "HarnessStats",
    "make_plan",
    "make_positions",
    "make_market_cache",
    "make_settled_events",
    "make_monitor_inputs",
    "MONITOR_TICK_SEC",
    "MONITOR_PERSIST_SEC",
    "PERSIST_LAG_SEC",
]

MONITOR_TICK_SEC = 10
MONITOR_PERSIST_SEC = 60
PERSIST_LAG_SEC = 5


class InjectedClock:
    """Deterministic test clock (H07 harness).

    ``advance(seconds)`` moves the virtual ``now_ms`` forward and returns
    it; no real sleeping ever happens. H08 drives the real scheduler with
    the same tick constants.
    """

    def __init__(self, start_ms: int = 1_760_000_000_000) -> None:
        self._now_ms = int(start_ms)

    @property
    def now_ms(self) -> int:
        return self._now_ms

    def advance(self, seconds: float) -> int:
        """Advance the virtual clock by ``seconds`` (may be fractional)."""
        self._now_ms = int(self._now_ms + float(seconds) * 1000)
        return self._now_ms

    def advance_ms(self, ms: int) -> int:
        self._now_ms = int(self._now_ms + int(ms))
        return self._now_ms


@dataclass
class HarnessStats:
    ticks: int = 0
    persists: int = 0
    jitter_samples: list[float] = field(default_factory=list)
    max_queue_depth: int = 0
    degraded_ticks: int = 0
    dropped: int = 0


class FakeWorker:
    """Single-worker queue double with block/release (H07 harness).

    - ``submit(fn, *, kind="persist", plan_id="")`` enqueues while blocked
      and runs inline while open (unless ``inline=False``).
    - ``enqueue_persist(plan_id, snapshot)`` coalesces to the latest entry
      per plan: repeated 10s ticks while blocked keep only one pending
      write per plan (B30 queue-merges-latest).
    - ``block()`` simulates a stuck DuckDB worker; ``release()`` drains in
      enqueue order and returns the executed ops. Queue lag beyond
      ``PERSIST_LAG_SEC`` reports ``is_degraded`` without dropping real
      positions or critical alerts.
    - Background scoring batches call :meth:`yield_background` to prove
      500 items are processed in cooperative chunks, never one giant sync
      block inside an async handler.
    """

    def __init__(self, clock: InjectedClock,
                 persist_lag_sec: int = PERSIST_LAG_SEC) -> None:
        self._clock = clock
        self._lag_limit_ms = int(persist_lag_sec) * 1000
        self._blocked = False
        self._queue: list[dict[str, Any]] = []
        self.executed: list[dict[str, Any]] = []
        self.max_depth = 0
        self.background_yields = 0
        self.background_items = 0

    # -- block / release ----------------------------------------------------
    def block(self) -> None:
        self._blocked = True

    def release(self) -> list[dict[str, Any]]:
        """Unblock and drain the queue in order; returns executed ops."""
        self._blocked = False
        # Coalescing already happened at enqueue time (latest per plan for
        # persist ops); drain FIFO for determinism.
        drained = list(self._queue)
        self._queue.clear()
        for op in drained:
            self.executed.append(op)
            if op.get("fn") is not None:
                try:
                    op["fn"]()
                except Exception:
                    pass
        return drained

    @property
    def blocked(self) -> bool:
        return self._blocked

    @property
    def depth(self) -> int:
        return len(self._queue)

    def queue_lag_ms(self) -> int:
        if not self._queue:
            return 0
        oldest = min(op["enqueued_at_ms"] for op in self._queue)
        return self._clock.now_ms - oldest

    def is_degraded(self) -> bool:
        return self.queue_lag_ms() > self._lag_limit_ms

    # -- submit --------------------------------------------------------------
    def submit(
        self,
        fn: Callable[[], Any] | None,
        *,
        kind: str = "persist",
        plan_id: str = "",
        inline: bool = True,
    ) -> str:
        op = {"fn": fn, "kind": kind, "plan_id": plan_id,
              "enqueued_at_ms": self._clock.now_ms}
        if self._blocked or not inline:
            if kind == "persist" and plan_id:
                # Merge-latest: replace any pending persist for this plan.
                for existing in self._queue:
                    if existing["kind"] == "persist" and existing["plan_id"] == plan_id:
                        # Keep the original enqueue time so lag measures the
                        # true head-of-line delay; only the payload is latest.
                        first_seen = existing["enqueued_at_ms"]
                        existing.update(op)
                        existing["enqueued_at_ms"] = first_seen
                        self.max_depth = max(self.max_depth, len(self._queue))
                        return "coalesced"
            self._queue.append(op)
            self.max_depth = max(self.max_depth, len(self._queue))
            return "queued"
        self.executed.append(op)
        if fn is not None:
            fn()
        return "executed"

    def enqueue_persist(self, plan_id: str, snapshot: Any) -> str:
        """Queue a latest-only monitor persist for ``plan_id``."""
        return self.submit(lambda: None, kind="persist", plan_id=plan_id,
                           inline=not self._blocked)

    def pending_persists(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for op in self._queue:
            if op["kind"] == "persist":
                out[op["plan_id"]] = op
        return out

    # -- background batching ---------------------------------------------------
    def yield_background(self, items: int, batch: int = 50) -> int:
        """Process ``items`` background scores in ``batch`` chunks."""
        done = 0
        while done < items:
            done += min(batch, items - done)
            self.background_yields += 1
        self.background_items += items
        return done


# ---------------------------------------------------------------------------
# Fixture builders (frozen H01 shapes, plain mappings for speed).
# ---------------------------------------------------------------------------


def make_plan(plan_id: str = "plan-0", symbol: str = "BTCUSDT", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "plan_id": plan_id,
        "symbol": symbol,
        "canonical_id": "bitcoin",
        "mode": "ABSOLUTE",
        "status": "ACTIVE",
        "target_hedge_ratio": "1",
        "liquidation_price": "90000",
        "liquidation_price_source": "USER_EXCHANGE",
        "liquidation_price_updated_at_ms": 1_760_000_000_000,
        "futures_notional_usd": "10000",
    }
    base.update(over)
    return base


def make_positions(
    fut_qty: str = "1.0",
    spot_qty: str = "1.0",
    *,
    plan_id: str = "plan-0",
    fut_entry: str = "67000",
    spot_entry: str = "67000",
    fut_closed: str = "0",
    spot_closed: str = "0",
) -> tuple[dict[str, Any], ...]:
    return (
        {"plan_id": plan_id, "leg_type": "FUTURES_SHORT",
         "open_qty": fut_qty, "closed_qty": fut_closed,
         "remaining_qty": fut_qty, "gross_qty": fut_qty, "net_qty": fut_qty,
         "weighted_avg_price": fut_entry, "event_ids": ("e-fut",)},
        {"plan_id": plan_id, "leg_type": "SPOT_LONG",
         "open_qty": spot_qty, "closed_qty": spot_closed,
         "remaining_qty": spot_qty, "gross_qty": spot_qty, "net_qty": spot_qty,
         "weighted_avg_price": spot_entry, "event_ids": ("e-spot",)},
    )


def make_market_cache(
    *,
    now_ms: int = 1_760_000_000_000,
    mark: str = "67000",
    spot: str = "67000",
    skew_sec: int = 0,
    quote_to_usd: str = "1",
    sell_executable_qty: str = "10",
    sell_slippage_bps: float = 5.0,
    known_cost_usd: str | None = "5",
    exit_cost_usd: str | None = "3",
    projected_next: str | None = "0.5",
    persist_lag_ms: int = 0,
) -> dict[str, Any]:
    fut_asof = now_ms - skew_sec * 1000 if skew_sec else now_ms
    return {
        "futures_mark": {
            "price": mark, "quote_currency": "USDT",
            "quote_to_usd": quote_to_usd,
            "as_of_ms": fut_asof, "fetched_at_ms": fut_asof,
            "expires_at_ms": now_ms + 60_000,
        },
        "spot_quote": {
            "sell_vwap": spot, "mid_price": spot,
            "quote_currency": "USDT", "quote_to_usd": quote_to_usd,
            "as_of_ms": now_ms, "fetched_at_ms": now_ms,
            "expires_at_ms": now_ms + 60_000,
            "sell_executable_qty": sell_executable_qty,
            "sell_slippage_bps": sell_slippage_bps,
        },
        "known_cost_usd": known_cost_usd,
        "estimated_exit_cost_usd": exit_cost_usd,
        "projected_next_funding_usd": projected_next,
        "persist_lag_ms": persist_lag_ms,
    }


def make_settled_events(
    *,
    count: int = 3,
    start_ms: int = 1_760_000_000_000 - 3 * 8 * 3_600_000,
    interval_ms: int = 8 * 3_600_000,
    rate: str = "0.0001",
    mark: str = "67000",
    short_qty: str = "1.0",
    fx: str = "1",
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"funding_time_ms": start_ms + i * interval_ms,
         "rate": rate, "mark_price": mark,
         "quote_asset": "USDT", "fx_to_usd": fx,
         "short_qty": short_qty}
        for i in range(count)
    )


def make_monitor_inputs(
    *,
    n_plans: int = 12,
    n_symbols: int = 10,
    now_ms: int = 1_760_000_000_000,
) -> tuple[list[dict[str, Any]], list[tuple[Any, ...]],
             list[dict[str, Any]], list[tuple[dict[str, Any], ...]]]:
    """Build the default mixed load: 10 symbols, multi-plan symbols.

    Returns ``(plans, positions_list, caches, settled_list)`` with
    ``n_plans`` plans spread over ``n_symbols`` symbols (plan 0/1 share
    symbol 0 to prove per-asset sharing still ticks per plan).
    """
    symbols = [f"SYM{i}USDT" for i in range(n_symbols)]
    plans: list[dict[str, Any]] = []
    positions_list: list[tuple[Any, ...]] = []
    caches: list[dict[str, Any]] = []
    settled_list: list[tuple[dict[str, Any], ...]] = []
    for i in range(n_plans):
        symbol = symbols[i % n_symbols]
        plan_id = f"plan-{i}"
        plans.append(make_plan(plan_id, symbol))
        positions_list.append(make_positions(plan_id=plan_id))
        caches.append(make_market_cache(now_ms=now_ms))
        settled_list.append(make_settled_events())
    return (plans, positions_list, caches, settled_list)


def make_background_scores(n: int = 500) -> list[dict[str, Any]]:
    """Return ``n`` frozen scoring inputs (cooperative background load)."""
    return [{"symbol": f"BG{i}USDT", "fcs": 50.0 + (i % 50),
             "as_of_ms": 1_760_000_000_000} for i in range(n)]
