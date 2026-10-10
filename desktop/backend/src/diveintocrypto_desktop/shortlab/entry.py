"""Short-Lab lightweight Entry builder, budget and replay guard (Task 12).

Design contract: ``ShortLab_Detailed_Design_CN.md`` sections 6.1 (lightweight
per-symbol path, 18 upstream calls worst case), 15 / 15.1 (six Entry blocks,
fixed weights, fixed mappings) and 7.1 (``entry-v1`` versioning); task
contract: ``ShortLab_Implementation_Plan_CN.md`` Task 12.

Lightweight path per symbol (cache fully cold)::

    12 timeframe klines + 1 OI history + 4 L/S ratio series + 1 settled
    30D funding range = 18 upstream calls worst case

The builder reuses the existing data clients
(``data/binance_klines.fetch_klines``, ``data/open_interest.fetch_oi_hist``,
the four ``data/ratios`` leaf series and
``data/funding.funding_history_range``) and the pure
``scan/symbol_builder.assemble`` computation. It never calls the full
``build_symbol`` network chain and never fetches the live-only extras.

Point-in-time rule: entry snapshots describe the live observation instant.
History must be replayed from the stored ``sl_entry_snapshot`` rows via
:func:`recompute_entry_from_record`, never by rebuilding through
``build_symbol(end_ms=...)`` (which still reads the live OI / ratio /
funding tail). :func:`build_historical_entry` exists only to reject that
path with :class:`HistoricalReplayError`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping

from diveintocrypto_desktop.data import binance_klines as klines_mod
from diveintocrypto_desktop.data import funding as funding_mod
from diveintocrypto_desktop.data import open_interest as oi_mod
from diveintocrypto_desktop.data import ratios as ratios_mod
from diveintocrypto_desktop.data.http import TransientUpstreamError
from diveintocrypto_desktop.shortlab.request_budget import (
    BudgetExhausted as RequestBudgetExhausted,
    ObservedCache,
    Denied,
    RequestContext,
    UnbudgetedEndpointError,
)
from diveintocrypto_desktop.engine.loader import load_config as load_engine_config
from diveintocrypto_desktop.scan import evidence as evidence_mod
from diveintocrypto_desktop.scan import symbol_builder as symbol_builder_mod
from diveintocrypto_desktop.shortlab.config import config_hash as shortlab_config_hash
from diveintocrypto_desktop.shortlab.config import load_shortlab_config
from diveintocrypto_desktop.shortlab.features import lifecycle as lifecycle_mod
from diveintocrypto_desktop.shortlab.repository import EntrySnapshotRecord
from diveintocrypto_desktop.shortlab.scoring.ltss import round_half_up_1
from diveintocrypto_desktop.shortlab.scoring.versions import (
    ENTRY_VERSION,
    ENTRY_VERSION_CURRENT,
)

__all__ = [
    "ENTRY_VERSION",
    "ENTRY_VERSION_CURRENT",
    "ENTRY_VERSIONS_ALL",
    "ENTRY_REQUIRED_BLOCKS",
    "ENTRY_BUDGET_EXHAUSTED",
    "BudgetExhausted",
    "HistoricalReplayError",
    "EntryBudget",
    "EntryResult",
    "EntryBatchResult",
    "score_entry",
    "score_entry_detailed",
    "build_entry_snapshot",
    "save_entry_snapshot",
    "recompute_entry_from_record",
    "build_historical_entry",
    "run_entry_batch",
]

# Six Entry blocks (design 15, mirrored by config entry_required_blocks and
# the repository REQUIRED_ENTRY_META_BLOCKS). All six are required: any
# unavailable block forces ``entryScore=None`` with no reweighting.
ENTRY_REQUIRED_BLOCKS = (
    "consensus",
    "mtf",
    "micro",
    "regime",
    "failed_bounce",
    "funding",
)

ENTRY_BUDGET_EXHAUSTED = "ENTRY_BUDGET_EXHAUSTED"

# Fixed block weights (design 15.1): 30 + 20 + 20 + 10 + 10 + 10 = 100.
_CONSENSUS_MAX = 30.0
_MTF_MAX = 20.0
_MICRO_MAX = 20.0
_REGIME_MAX = 10.0
_BOUNCE_MAX = 10.0
_FUNDING_MAX = 10.0

_DAY_MS = 86_400_000
_FUNDING_LOOKBACK_MS = 30 * _DAY_MS

# Fetch shape mirrors build_symbol defaults: 300 klines per timeframe, 5m
# cadence with 48 points for the OI / L/S series.
_KLINE_LIMIT = 300
_SERIES_PERIOD = "5m"
_SERIES_LIMIT = 48

# F03: Entry no longer retries. The single retry layer lives in
# ``data/http.py`` (unique send point, honoring Retry-After); ``guarded``
# only checks the (shared) cache and calls the factory once, so one logical
# leaf is one budget spend and HTTP retries/pages are counted by the shared
# ``RequestBudget`` instead of a second Entry loop.

_CONSENSUS_TIMEFRAME_MISSING = "CONSENSUS_TIMEFRAME_MISSING"
_CONSENSUS_ASSEMBLE_FAILED = "CONSENSUS_ASSEMBLE_FAILED"
_MTF_BLOCK_MISSING = "MTF_BLOCK_MISSING"
_MICRO_BLOCK_MISSING = "MICRO_BLOCK_MISSING"
_MICROSTRUCTURE_INACTIVE = "MICROSTRUCTURE_INACTIVE"
_REGIME_BLOCK_MISSING = "REGIME_BLOCK_MISSING"

#: Readable Entry buckets (CR10/D15): new snapshots emit CURRENT/v3
#: (Snapshot ID +落库 entry_version + Evidence分桶同步); legacy v2 rows
#: stay decodable via ``recompute_entry_from_record`` and are never rewritten.
ENTRY_VERSIONS_ALL = frozenset({ENTRY_VERSION, ENTRY_VERSION_CURRENT})


def _result_entry_version(result: Any) -> str:
    """Version bucket carried by an :class:`EntryResult` (preserve on replay).

    New results carry CURRENT/v3 in ``inputs._meta.entry_version`` (and in
    the ``snapshot_id`` suffix). Replays/``to_record`` preserve a known
    bucket so old v2 rows are never silently upgraded; unknown falls back
    to CURRENT.
    """
    try:
        inputs = getattr(result, "inputs", None)
        if isinstance(inputs, Mapping):
            meta = inputs.get("_meta")
            if isinstance(meta, Mapping):
                ver = meta.get("entry_version")
                if isinstance(ver, str) and ver in ENTRY_VERSIONS_ALL:
                    return ver
        sid = str(getattr(result, "snapshot_id", "") or "")
        for cand in (ENTRY_VERSION_CURRENT, ENTRY_VERSION):
            if sid.endswith(f"-{cand}"):
                return cand
    except Exception:  # noqa: BLE001 - defensive, default to CURRENT
        pass
    return str(ENTRY_VERSION_CURRENT)


class BudgetExhausted(RuntimeError):
    """Internal control flow: an upstream call found the budget empty.

    :func:`build_entry_snapshot` converts this into an ``EntryResult`` with
    ``reason_code == ENTRY_BUDGET_EXHAUSTED`` and ``entry_score=None``; it is
    never raised to Task 13 callers.
    """


class HistoricalReplayError(RuntimeError):
    """Raised when history is rebuilt through live fetches instead of a snapshot."""


def _default_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


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


# ---------------------------------------------------------------------------
# Pure Entry scoring (design 15.1, verbatim mappings)
# ---------------------------------------------------------------------------


def consensus_component(final_signal: Any, confidence: Any) -> float | None:
    """Existing SHORT consensus block, max 30.

    Only ``SELL`` / ``STRONG_SELL`` contribute: ``30 * confidence / 100``,
    plus 3 for ``STRONG_SELL``, clamped to [0, 30]. Any other signal scores
    0 (available). ``None`` inputs are unavailable.
    """
    if final_signal is None or confidence is None:
        return None
    conf = _finite(confidence)
    if conf is None:
        return None
    if final_signal not in ("SELL", "STRONG_SELL"):
        return 0.0
    base = _CONSENSUS_MAX * conf / 100.0
    if final_signal == "STRONG_SELL":
        base += 3.0
    return min(_CONSENSUS_MAX, max(0.0, base))


def mtf_component(score: Any, direction: Any, gate: Any) -> float | None:
    """MTF bearish confluence block, max 20.

    ``20 * clamp(-score / 100)``; ``direction != -1`` forces 0; a failed
    higher-timeframe gate caps the block at 10.
    """
    raw_score = _finite(score)
    if raw_score is None or direction is None or gate is None:
        return None
    raw = _MTF_MAX * _clamp01(-raw_score / 100.0)
    if direction != -1:
        return 0.0
    if gate is False:
        raw = min(raw, 10.0)
    return raw


def micro_component(score: Any, active: Any) -> float | None:
    """Microstructure bearish block, max 20.

    ``active == 0`` means the bundle had no computable signal and is
    unavailable (never 0).
    """
    if active is None or score is None:
        return None
    try:
        active_count = int(active)
    except (TypeError, ValueError):
        return None
    if isinstance(active, bool) or active_count == 0:
        return None
    raw_score = _finite(score)
    if raw_score is None:
        return None
    return _MICRO_MAX * _clamp01(-raw_score / 100.0)


def regime_component(regime: Any, adaptive_score: Any) -> float | None:
    """Regime block, max 10: TREND-down is 10, MIXED-down is 5, else 0."""
    score = _finite(adaptive_score)
    if regime is None or score is None:
        return None
    if regime == "TREND" and score < 0:
        return _REGIME_MAX
    if regime == "MIXED" and score < 0:
        return 5.0
    if regime in ("TREND", "MIXED", "RANGE"):
        return 0.0
    return None


def failed_bounce_component(triggered: Any) -> float | None:
    """Failed-bounce block, max 10: True is 10, False is 0, None unavailable."""
    if triggered is None:
        return None
    return _BOUNCE_MAX if bool(triggered) else 0.0


def funding_component(positive_ratio_30d: Any, complete: Any) -> float | None:
    """30D positive-funding block, max 10: ``10 * clamp(ratio)``.

    An incomplete 30D window is unavailable (never a partial ratio).
    """
    if not complete:
        return None
    ratio = _finite(positive_ratio_30d)
    if ratio is None:
        return None
    return _FUNDING_MAX * _clamp01(ratio)


def _mapping_get(mapping: Any) -> dict[str, Any]:
    if isinstance(mapping, Mapping):
        return dict(mapping)
    return {}


def score_entry_detailed(
    snapshot: Mapping[str, Any] | None,
    funding: Mapping[str, Any] | None,
    bounce: bool | None,
) -> tuple[float | None, dict[str, float | None], tuple[str, ...]]:
    """Score the six Entry blocks.

    ``snapshot`` carries the assembled ``finalSignal`` / ``confidence`` /
    ``mtfConfluence`` / ``microstructure`` / ``regime`` blocks, ``funding``
    carries ``positive_ratio_30d`` plus a ``complete`` coverage flag, and
    ``bounce`` is the failed-bounce trigger (``None`` when history is
    insufficient). Returns ``(total, components, missing_blocks)``; any
    missing block forces the total to ``None``.
    """
    view = _mapping_get(snapshot)
    funding_view = _mapping_get(funding)
    components: dict[str, float | None] = {}
    missing: list[str] = []

    consensus = consensus_component(view.get("finalSignal"), view.get("confidence"))
    components["consensus"] = consensus
    if consensus is None:
        missing.append("consensus")

    mtf_block = _mapping_get(view.get("mtfConfluence"))
    if not mtf_block or "score" not in mtf_block or "direction" not in mtf_block or "gate" not in mtf_block:
        components["mtf"] = None
        missing.append("mtf")
    else:
        mtf = mtf_component(mtf_block.get("score"), mtf_block.get("direction"), mtf_block.get("gate"))
        components["mtf"] = mtf
        if mtf is None:
            missing.append("mtf")

    micro_block = _mapping_get(view.get("microstructure"))
    if not micro_block or "score" not in micro_block or "active" not in micro_block:
        components["micro"] = None
        missing.append("micro")
    else:
        micro = micro_component(micro_block.get("score"), micro_block.get("active"))
        components["micro"] = micro
        if micro is None:
            missing.append("micro")

    regime_block = _mapping_get(view.get("regime"))
    if not regime_block or "regime" not in regime_block or "adaptive_score" not in regime_block:
        components["regime"] = None
        missing.append("regime")
    else:
        regime = regime_component(regime_block.get("regime"), regime_block.get("adaptive_score"))
        components["regime"] = regime
        if regime is None:
            missing.append("regime")

    bounce_value = failed_bounce_component(bounce)
    components["failed_bounce"] = bounce_value
    if bounce_value is None:
        missing.append("failed_bounce")

    funding_value = funding_component(
        funding_view.get("positive_ratio_30d"), funding_view.get("complete")
    )
    components["funding"] = funding_value
    if funding_value is None:
        missing.append("funding")

    if missing:
        return None, components, tuple(missing)
    return round_half_up_1(sum(float(v) for v in components.values())), components, ()


def score_entry(
    snapshot: Mapping[str, Any] | None,
    funding: Mapping[str, Any] | None,
    bounce: bool | None,
) -> float | None:
    """Entry total (0-100, 1 decimal) or ``None`` when any block is missing."""
    total, _, _ = score_entry_detailed(snapshot, funding, bounce)
    return total


# ---------------------------------------------------------------------------
# Budget: upstream-call accounting, TTL cache, concurrency cap
# ---------------------------------------------------------------------------


class _RoundPermit:
    def __init__(self, bridge, permit):
        self.bridge, self.permit, self.state = bridge, permit, "reserved"

    def mark_sent(self):
        if self.state == "sent":
            return
        if self.state != "reserved":
            raise RuntimeError("released Entry permit cannot send")
        self.permit.mark_sent()
        self.bridge.reserved -= 1
        self.bridge.owner.used_calls += 1
        self.state = "sent"

    def release_unsent(self):
        if self.state == "reserved":
            self.permit.release_unsent()
            self.bridge.reserved -= 1
            self.state = "released"


class _RoundSendBudget:
    """Compose shared host limits with one Entry round's actual-send cap."""
    def __init__(self, owner, shared):
        self.owner, self.shared, self.reserved = owner, shared, 0

    def __getattr__(self, name):
        return getattr(self.shared, name)

    def try_acquire(self, host, weight, job_type, endpoint_family):
        if self.owner.used_calls + self.reserved >= self.owner.max_calls:
            return Denied(reason_code=ENTRY_BUDGET_EXHAUSTED,
                          message="Entry round final-send budget exhausted",
                          job_type=job_type, endpoint_family=endpoint_family)
        permit = self.shared.try_acquire(host, weight, job_type, endpoint_family)
        if isinstance(permit, Denied) or permit is False:
            return permit
        self.reserved += 1
        return _RoundPermit(self, permit)


class EntryBudget:
    """Per-round upstream budget for the lightweight Entry path.

    Defaults mirror the packaged Short-Lab config (``refresh``:
    ``entry_max_upstream_calls_per_run=240``, ``entry_concurrency=2``,
    ``entry_cache_ttl_sec=3600``). Each ``guarded`` miss consumes one logical
    call; cache hits consume none. All fetches still travel the shared
    data-client path (and its shared limiters); the budget only counts, it
    never bypasses.

    F03 wiring (additive): ``shared_cache`` is the long-lived
    :class:`ObservedCache` held by the runtime (F06) — counts reset per
    round but the cache persists across rounds, preserving source times.
    ``request_context`` carries the shared :class:`RequestBudget` for real
    HTTP sends (funding pages/retries count there). ``guarded`` only checks
    cache / calls the factory once — it never retries (the unique retry
    lives in ``data/http.py``).
    """

    def __init__(
        self,
        max_calls: int = 240,
        concurrency: int = 2,
        ttl_sec: int = 3600,
        *,
        clock: Callable[[], int] | None = None,
        shared_cache: ObservedCache | None = None,
        request_context: RequestContext | None = None,
        identity_snapshot_id: str | None = None,
    ) -> None:
        if isinstance(max_calls, bool) or int(max_calls) < 1:
            raise ValueError(f"max_calls must be an int >= 1, got {max_calls!r}")
        if isinstance(concurrency, bool) or int(concurrency) < 1:
            raise ValueError(f"concurrency must be an int >= 1, got {concurrency!r}")
        if isinstance(ttl_sec, bool) or int(ttl_sec) < 0:
            raise ValueError(f"ttl_sec must be an int >= 0, got {ttl_sec!r}")
        self.max_calls = int(max_calls)
        self.concurrency = int(concurrency)
        self.ttl_sec = int(ttl_sec)
        self._clock = clock or _default_clock_ms
        self.used_calls = 0
        self.cache_hits = 0
        self._cache: dict[Any, tuple[Any, int]] = {}
        self.observation_times: dict[Any, tuple[int, int]] = {}
        self._semaphores: dict[Any, asyncio.Semaphore] = {}
        self.inflight = 0
        self.peak_inflight = 0
        self.shared_cache = shared_cache
        self.request_context = (replace(request_context, budget=_RoundSendBudget(self, request_context.budget))
                                if request_context is not None and request_context.budget is not None
                                else request_context)
        if identity_snapshot_id is not None:
            self.identity_snapshot_id: str | None = str(identity_snapshot_id)
        elif request_context is not None and request_context.identity_snapshot_id is not None:
            self.identity_snapshot_id = request_context.identity_snapshot_id
        else:
            self.identity_snapshot_id = None

    def now_ms(self) -> int:
        """Current time in UTC epoch milliseconds (injectable for tests)."""
        return int(self._clock())

    @property
    def remaining(self) -> int:
        """Unspent upstream calls in this round."""
        return max(0, self.max_calls - self.used_calls)

    @property
    def exhausted(self) -> bool:
        """True once the round budget is fully spent."""
        return self.used_calls >= self.max_calls

    def try_acquire(self) -> bool:
        """Consume one upstream call; False when the budget is exhausted.

        Synchronous (no awaits between check and increment) so concurrent
        builders can never overshoot ``max_calls``.
        """
        if self.used_calls >= self.max_calls:
            return False
        self.used_calls += 1
        return True

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = self._semaphores[loop] = asyncio.Semaphore(self.concurrency)
        return semaphore

    @asynccontextmanager
    async def symbol_slot(self):
        """Cap concurrent symbol builds; tracks peak overlap for tests."""
        async with self._semaphore():
            self.inflight += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
            try:
                yield
            finally:
                self.inflight -= 1

    def cache_lookup(self, key: Any) -> tuple[bool, Any]:
        """Return ``(hit, value)``; expired rows behave as misses."""
        row = self._cache.get(key)
        if row is None:
            return False, None
        value, expires_ms = row
        if expires_ms <= self.now_ms():
            self._cache.pop(key, None)
            return False, None
        self.cache_hits += 1
        return True, value

    def cache_store(self, key: Any, value: Any) -> None:
        """Cache a fetched value for ``ttl_sec``."""
        self._cache[key] = (value, self.now_ms() + self.ttl_sec * 1000)

    def _shared_key(self, key: Any) -> Any:
        """Shared-cache key: leaf key + identity version (A7.2)."""
        try:
            return (key, self.identity_snapshot_id)
        except TypeError:
            return (repr(key), self.identity_snapshot_id)

    async def guarded(
        self,
        key: Any,
        factory: Callable[[], Awaitable[Any]],
        *,
        cutoff_ms: int | None = None,
    ) -> Any:
        """Run one cached, budgeted upstream fetch (no retry).

        Cache hits (shared :class:`ObservedCache` first, then the round-local
        TTL) return without spending budget. Each miss spends exactly one
        logical call and invokes ``factory`` once; a
        :class:`TransientUpstreamError` is *not* retried here — the unique
        retry lives in ``data/http.py`` (honoring ``Retry-After``), and HTTP
        retries/pages are counted by the shared ``RequestBudget``. Any error
        propagates to the caller, which degrades that block instead of
        failing the symbol. Already-spent calls are never refunded, including
        on cancellation.
        """
        now = self.now_ms()
        cutoff = int(cutoff_ms) if cutoff_ms is not None else now
        if self.shared_cache is not None:
            try:
                observed = self.shared_cache.get(self._shared_key(key), cutoff)
            except Exception:
                observed = None
            if observed is not None:
                self.cache_hits += 1
                self.observation_times[key] = (int(observed.meta.fetched_at_ms or 0), int(observed.meta.known_at_ms or 0))
                return observed.value
        hit, value = self.cache_lookup(key)
        if hit and self.observation_times.get(key, (0, 0))[1] <= cutoff:
            return value
        if (self.request_context is None or self.request_context.budget is None) and not self.try_acquire():
            raise BudgetExhausted(
                f"{ENTRY_BUDGET_EXHAUSTED}: spent {self.used_calls}/{self.max_calls}"
            )
        # Single attempt: no Entry-level retry (F03.1).
        value = await factory()
        completed_ms = self.now_ms()
        self.observation_times[key] = (completed_ms, completed_ms)
        self.cache_store(key, value)
        if self.shared_cache is not None:
            try:
                from diveintocrypto_desktop.shortlab import observations as _obs_mod

                observed_new = _obs_mod.make_observation(
                    value,
                    source="entry-leaf",
                    source_as_of_ms=None,
                    fetched_at_ms=completed_ms,
                    known_at_ms=completed_ms,
                )
                self.shared_cache.put(
                    self._shared_key(key),
                    observed_new,
                    completed_ms + self.ttl_sec * 1000,
                )
            except Exception:
                pass
        return value


# ---------------------------------------------------------------------------
# Entry snapshot carrier
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryResult:
    """One symbol's lightweight Entry observation (live point-in-time)."""

    symbol: str
    as_of_ms: int
    primary_tf: str
    entry_score: float | None
    components: Mapping[str, Any]
    missing_blocks: tuple[str, ...]
    reason_code: str | None
    inputs: Mapping[str, Any]
    source_meta: Mapping[str, Mapping[str, Any]]
    dive_weights_hash: str
    dive_engine_version: str
    dive_config_hash: str
    shortlab_config_hash: str
    snapshot_id: str
    fetched_at_ms: int
    created_at_ms: int

    def to_record(self) -> EntrySnapshotRecord:
        """Render the repository row Task 13 references via ``save_score``.

        The stored ``entry_version`` bucket follows the result itself (new
        v3, legacy v2 preserved) so old rows are never upgraded on replay.
        """
        return EntrySnapshotRecord(
            snapshot_id=self.snapshot_id,
            symbol=self.symbol,
            as_of_ms=self.as_of_ms,
            entry_version=_result_entry_version(self),
            dive_weights_hash=self.dive_weights_hash,
            dive_engine_version=self.dive_engine_version,
            dive_config_hash=self.dive_config_hash,
            primary_tf=self.primary_tf,
            inputs=dict(self.inputs),
            components=dict(self.components),
            source_meta={name: dict(meta) for name, meta in self.source_meta.items()},
            entry_score=self.entry_score,
            created_at_ms=self.created_at_ms,
        )


@dataclass(frozen=True)
class EntryBatchResult:
    """Outcome of one budgeted Entry round over shortlist symbols."""

    items: tuple[EntryResult, ...]
    queued_symbols: tuple[str, ...]
    stats: Mapping[str, Any] = field(default_factory=dict)


def _dive_provenance() -> tuple[str, str, str]:
    """``(weights_hash, engine_version, config_hash)`` of the live engine."""
    engine_config = load_engine_config()
    weights = engine_config.get("indicator_weights", {}) or {}
    weights_hash = evidence_mod.weights_hash(weights)
    config_hash = hashlib.sha256(
        json.dumps(engine_config, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return weights_hash, evidence_mod.ENGINE_VERSION, config_hash


def _meta_block(
    status: str,
    fetched_at_ms: int,
    as_of_ms: int,
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


async def _guarded_or_missing(
    budget: EntryBudget,
    key: Any,
    factory: Callable[[], Awaitable[Any]],
    *,
    missing_value: Any,
) -> tuple[Any, str | None]:
    """Fetch through the budget; degrade to ``missing_value`` on data errors.

    :class:`BudgetExhausted` (Entry logical budget) and the shared
    :class:`RequestBudget` exhaustion are re-raised (they abort the symbol
    and queue the remainder); an unknown-weight endpoint is also re-raised
    (fail fast, never silent). Every other failure becomes
    ``(missing_value, "FETCH_FAILED")`` so one bad block never fails its
    siblings.
    """
    try:
        return await budget.guarded(key, factory), None
    except BudgetExhausted:
        raise
    except RequestBudgetExhausted as exc:
        raise BudgetExhausted(str(exc)) from exc
    except UnbudgetedEndpointError:
        raise
    except TransientUpstreamError:
        return missing_value, "FETCH_FAILED"
    except Exception:
        return missing_value, "FETCH_FAILED"


async def _fetch_symbol_inputs(
    symbol: str,
    budget: EntryBudget,
    *,
    as_of_ms: int,
    fetched_at_ms: int,
) -> dict[str, Any]:
    """Run the 18-call lightweight fetch set for one symbol.

    Twelve timeframe klines (one per ``TF_LIST`` entry, reused — never a
    second hardcoded list), one OI history, four L/S series, one settled 30D
    funding range. Raises :class:`BudgetExhausted` when the round budget runs
    dry mid-symbol; individual block failures degrade to documented missings.
    """
    timeframes = klines_mod.TF_LIST
    klines_results = await asyncio.gather(
        *(
            _guarded_or_missing(
                budget,
                ("klines", symbol, timeframe, _KLINE_LIMIT),
                (lambda tf=timeframe: (lambda: klines_mod.fetch_klines(symbol, tf, _KLINE_LIMIT)))(),
                missing_value=[],
            )
            for timeframe in timeframes
        ),
        return_exceptions=False,
    )
    candles_by_tf: dict[str, list[dict]] = {}
    klines_errors: dict[str, str] = {}
    for timeframe, (candles, error) in zip(timeframes, klines_results):
        if error is not None:
            klines_errors[timeframe] = error
        elif candles:
            candles_by_tf[timeframe] = list(candles)
        else:
            klines_errors[timeframe] = "EMPTY_TIMEFRAME"

    window_start = as_of_ms - _FUNDING_LOOKBACK_MS
    funding_ctx = getattr(budget, "request_context", None)
    _use_funding_ctx = funding_ctx is not None and funding_ctx.budget is not None

    def _funding_factory() -> Any:
        # Preserve legacy fake signatures (fake(symbol, start, end, limit))
        # when no shared budget is wired; only pass request_context for the
        # F03 budgeted path.
        if _use_funding_ctx:
            return funding_mod.funding_history_range(
                symbol, window_start, as_of_ms, request_context=funding_ctx
            )
        return funding_mod.funding_history_range(symbol, window_start, as_of_ms)
    oi_res, glob_res, acc_res, pos_res, taker_res, funding_res = await asyncio.gather(
        _guarded_or_missing(
            budget,
            ("oi", symbol, _SERIES_PERIOD, _SERIES_LIMIT),
            lambda: oi_mod.fetch_oi_hist(symbol, _SERIES_PERIOD, limit=_SERIES_LIMIT),
            missing_value=[],
        ),
        _guarded_or_missing(
            budget,
            ("ratio", symbol, "glob", _SERIES_PERIOD, _SERIES_LIMIT),
            lambda: ratios_mod.global_account_ls(symbol, _SERIES_PERIOD, _SERIES_LIMIT),
            missing_value=[],
        ),
        _guarded_or_missing(
            budget,
            ("ratio", symbol, "acc", _SERIES_PERIOD, _SERIES_LIMIT),
            lambda: ratios_mod.top_account_ls(symbol, _SERIES_PERIOD, _SERIES_LIMIT),
            missing_value=[],
        ),
        _guarded_or_missing(
            budget,
            ("ratio", symbol, "pos", _SERIES_PERIOD, _SERIES_LIMIT),
            lambda: ratios_mod.top_position_ls(symbol, _SERIES_PERIOD, _SERIES_LIMIT),
            missing_value=[],
        ),
        _guarded_or_missing(
            budget,
            ("ratio", symbol, "taker", _SERIES_PERIOD, _SERIES_LIMIT),
            lambda: ratios_mod.taker_ls(symbol, _SERIES_PERIOD, _SERIES_LIMIT),
            missing_value=[],
        ),
        _guarded_or_missing(
            budget,
            ("funding", symbol, window_start, as_of_ms),
            _funding_factory,
            missing_value=[],
        ),
        return_exceptions=False,
    )
    oi_points, oi_error = oi_res
    glob, glob_error = glob_res
    acc, acc_error = acc_res
    pos, pos_error = pos_res
    taker, taker_error = taker_res
    funding_events, funding_error = funding_res
    return {
        "candles_by_tf": candles_by_tf,
        "klines_errors": klines_errors,
        "oi_points": list(oi_points or []),
        "oi_error": oi_error,
        "ratios": {
            "glob": [float(v) for v in (glob or [])],
            "acc": [float(v) for v in (acc or [])],
            "pos": [float(v) for v in (pos or [])],
            "taker": [float(v) for v in (taker or [])],
        },
        "ratio_errors": {
            "glob": glob_error,
            "acc": acc_error,
            "pos": pos_error,
            "taker": taker_error,
        },
        "funding_events": list(funding_events or []),
        "funding_error": funding_error,
        "window_start_ms": window_start,
    }


def _series_data(fetched: dict[str, Any]) -> dict[str, list[float]]:
    candles_by_tf = fetched["candles_by_tf"]
    five_min = candles_by_tf.get(_SERIES_PERIOD) or []
    closes_5m = [float(c["c"]) for c in five_min if isinstance(c.get("c"), (int, float))]
    oi_points = fetched["oi_points"]
    oi_series = [float(p["oi"]) for p in oi_points if isinstance(p.get("oi"), (int, float))]
    funding_events = fetched["funding_events"]
    funding_series = [
        float(e["funding_rate"])
        for e in funding_events
        if isinstance(e.get("funding_rate"), (int, float))
    ]
    ratios = fetched["ratios"]
    return {
        "oi": oi_series,
        "glob": list(ratios["glob"]),
        "acc": list(ratios["acc"]),
        "pos": list(ratios["pos"]),
        "taker": list(ratios["taker"]),
        "funding": funding_series,
        "price": closes_5m[-_SERIES_LIMIT:],
    }


def _last_close(fetched: dict[str, Any], primary_tf: str) -> float | None:
    for timeframe in (primary_tf, _SERIES_PERIOD):
        candles = fetched["candles_by_tf"].get(timeframe) or []
        if candles:
            try:
                return float(candles[-1]["c"])
            except (KeyError, TypeError, ValueError):
                continue
    for candles in fetched["candles_by_tf"].values():
        if candles:
            try:
                return float(candles[-1]["c"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _funding_block(
    fetched: dict[str, Any], as_of_ms: int
) -> tuple[dict[str, Any], str | None]:
    """30D settled-funding input plus its block reason (None when OK)."""
    events = fetched["funding_events"]
    window_start = fetched["window_start_ms"]
    if fetched["funding_error"] is not None or not events:
        return (
            {
                "positive_ratio_30d": None,
                "event_count": len(events),
                "coverage_fraction": 0.0,
                "complete": False,
            },
            funding_mod.FUNDING_HISTORY_INCOMPLETE,
        )
    positives = sum(1 for e in events if float(e.get("funding_rate", 0.0)) > 0)
    coverage = funding_mod.funding_coverage(events, window_start, as_of_ms)
    ratio = positives / len(events)
    if not coverage.complete:
        return (
            {
                "positive_ratio_30d": ratio,
                "event_count": len(events),
                "coverage_fraction": coverage.coverage_fraction,
                "complete": False,
            },
            funding_mod.FUNDING_HISTORY_INCOMPLETE,
        )
    return (
        {
            "positive_ratio_30d": ratio,
            "event_count": len(events),
            "coverage_fraction": coverage.coverage_fraction,
            "complete": True,
        },
        None,
    )


def _bounce_block(
    fetched: dict[str, Any],
) -> tuple[bool | None, dict[str, Any], str | None]:
    """Failed-bounce trigger from the 1D candles (design 8.3 rule reuse)."""
    daily = fetched["candles_by_tf"].get("1d") or []
    closes = [c.get("c") for c in daily]
    highs = [c.get("h") for c in daily]
    raw, detail, reason = lifecycle_mod.score_failed_bounce(closes, highs)
    if raw is None:
        return None, {"triggered": None, "detail": detail}, reason
    triggered = raw == 2
    return triggered, {"triggered": triggered, "detail": detail}, None if triggered else "NO_FAILED_BOUNCE"


async def build_entry_snapshot(
    symbol: str,
    primary_tf: str = "1h",
    *,
    budget: EntryBudget | None = None,
    as_of_ms: int | None = None,
    now_ms: int | None = None,
) -> EntryResult:
    """Build one symbol's lightweight Entry snapshot (live point-in-time).

    Fetches the 18-call lightweight set through ``budget`` (fresh default
    ``EntryBudget()`` when omitted), runs the pure
    ``symbol_builder.assemble`` computation off the event loop, scores the
    six blocks per 15.1, and freezes every input, contribution and
    field-level provenance. Missing blocks yield ``entry_score=None``;
    budget exhaustion yields ``ENTRY_BUDGET_EXHAUSTED`` with no further
    network. This function has no ``end_ms``: historical replay must read
    stored snapshots (see :func:`recompute_entry_from_record`).
    """
    own_budget = budget if budget is not None else EntryBudget()
    clock_ms = now_ms if now_ms is not None else own_budget.now_ms()
    observed_ms = as_of_ms if as_of_ms is not None else clock_ms
    name = str(symbol).upper()
    shortlab_hash = shortlab_config_hash(load_shortlab_config())
    dive_weights_hash, dive_engine_version, dive_config_hash_value = _dive_provenance()

    def exhausted_result(
        partial_inputs: dict[str, Any] | None = None,
        partial_meta: dict[str, dict[str, Any]] | None = None,
    ) -> EntryResult:
        inputs = partial_inputs if partial_inputs is not None else {}
        meta = partial_meta if partial_meta is not None else {}
        full_meta = {
            block: meta.get(
                block,
                _meta_block(
                    "UNAVAILABLE", clock_ms, observed_ms,
                    "entry-budget", ENTRY_BUDGET_EXHAUSTED, 0.0,
                ),
            )
            for block in ENTRY_REQUIRED_BLOCKS
        }
        full_inputs = dict(inputs)
        full_inputs.setdefault(
            "_meta",
            {
                "symbol": name,
                "primary_tf": primary_tf,
                "as_of_ms": observed_ms,
                "shortlab_config_hash": shortlab_hash,
                "entry_version": ENTRY_VERSION_CURRENT,
            },
        )
        null_components = {block: None for block in ENTRY_REQUIRED_BLOCKS}
        null_components["total"] = None
        return EntryResult(
            symbol=name,
            as_of_ms=observed_ms,
            primary_tf=primary_tf,
            entry_score=None,
            components=null_components,
            missing_blocks=ENTRY_REQUIRED_BLOCKS,
            reason_code=ENTRY_BUDGET_EXHAUSTED,
            inputs=full_inputs,
            source_meta=full_meta,
            dive_weights_hash=dive_weights_hash,
            dive_engine_version=dive_engine_version,
            dive_config_hash=dive_config_hash_value,
            shortlab_config_hash=shortlab_hash,
            snapshot_id=f"entry-{name}-{observed_ms}-{ENTRY_VERSION_CURRENT}",
            fetched_at_ms=clock_ms,
            created_at_ms=own_budget.now_ms(),
        )

    try:
        fetched = await _fetch_symbol_inputs(
            name, own_budget, as_of_ms=observed_ms, fetched_at_ms=clock_ms
        )
    except BudgetExhausted:
        return exhausted_result()

    leaf_times = [times for key, times in own_budget.observation_times.items()
                  if isinstance(key, tuple) and len(key) > 1 and key[1] == name]
    collection_completed_ms = max([clock_ms] + [t[1] for t in leaf_times])
    fetched_completed_ms = max([clock_ms] + [t[0] for t in leaf_times])

    missing_timeframes = [
        timeframe
        for timeframe in klines_mod.TF_LIST
        if timeframe not in fetched["candles_by_tf"]
    ]
    consensus_unavailable = (
        _CONSENSUS_TIMEFRAME_MISSING if missing_timeframes else None
    )

    assembled: dict[str, Any] = {}
    assemble_error: str | None = None
    if consensus_unavailable is None:
        try:
            series = _series_data(fetched)
            price = _last_close(fetched, primary_tf)
            assembled = await asyncio.to_thread(
                symbol_builder_mod.assemble,
                name,
                name.replace("USDT", ""),
                None,
                price,
                fetched["candles_by_tf"],
                series,
                {},
                primary_tf,
            )
            assembled.pop("ch", None)
        except BudgetExhausted:
            return exhausted_result()
        except Exception:
            assemble_error = _CONSENSUS_ASSEMBLE_FAILED
    if assemble_error is not None:
        consensus_unavailable = assemble_error
        assembled = {}

    funding_input, funding_reason = _funding_block(fetched, observed_ms)
    bounce_triggered, bounce_input, bounce_reason = _bounce_block(fetched)

    snapshot_view = {
        "finalSignal": assembled.get("finalSignal"),
        "confidence": assembled.get("confidence"),
        "mtfConfluence": assembled.get("mtfConfluence"),
        "microstructure": assembled.get("microstructure"),
        "regime": assembled.get("regime"),
    }
    funding_view = {
        "positive_ratio_30d": funding_input.get("positive_ratio_30d"),
        "complete": funding_input.get("complete"),
    }
    total, components, missing = score_entry_detailed(
        snapshot_view, funding_view, bounce_triggered
    )
    if consensus_unavailable is not None and "consensus" not in missing:
        missing = ("consensus",) + tuple(missing)
    if consensus_unavailable is not None:
        total = None

    def block_input(block: str, payload: Any) -> Any:
        if isinstance(payload, Mapping):
            return dict(payload)
        return {"unavailable": True}

    inputs: dict[str, Any] = {
        "consensus": block_input(
            "consensus",
            {"finalSignal": assembled.get("finalSignal"), "confidence": assembled.get("confidence")}
            if consensus_unavailable is None
            else {"reason": consensus_unavailable, "missing_timeframes": missing_timeframes},
        ),
        "mtf": block_input(
            "mtf", assembled.get("mtfConfluence") or {"reason": _MTF_BLOCK_MISSING}
        ),
        "micro": block_input(
            "micro", assembled.get("microstructure") or {"reason": _MICRO_BLOCK_MISSING}
        ),
        "regime": block_input(
            "regime", assembled.get("regime") or {"reason": _REGIME_BLOCK_MISSING}
        ),
        "failed_bounce": dict(bounce_input),
        "funding": dict(funding_input),
        "_meta": {
            "symbol": name,
            "primary_tf": primary_tf,
            "as_of_ms": observed_ms,
            "shortlab_config_hash": shortlab_hash,
            "entry_version": ENTRY_VERSION_CURRENT,
            "leaf_observations": [
                {"key": list(key), "fetched_at_ms": times[0], "known_at_ms": times[1]}
                for key, times in own_budget.observation_times.items()
                if isinstance(key, tuple) and len(key) > 1 and key[1] == name
            ],
        },
    }

    block_reasons: dict[str, str | None] = {
        "consensus": consensus_unavailable,
        "mtf": None if "mtf" not in missing else _MTF_BLOCK_MISSING,
        "micro": None if "micro" not in missing else (
            _MICROSTRUCTURE_INACTIVE
            if (assembled.get("microstructure") or {}).get("active") == 0
            else _MICRO_BLOCK_MISSING
        ),
        "regime": None if "regime" not in missing else _REGIME_BLOCK_MISSING,
        "failed_bounce": bounce_reason if "failed_bounce" in missing else bounce_reason,
        "funding": funding_reason,
    }
    block_sources = {
        "consensus": "binance-klines+assemble",
        "mtf": "dive-assemble",
        "micro": "dive-assemble",
        "regime": "dive-assemble",
        "failed_bounce": "binance-klines-daily+lifecycle-rule",
        "funding": "binance-funding-settled",
    }
    source_meta = {
        block: _meta_block(
            "OK" if block not in missing else "UNAVAILABLE",
            fetched_completed_ms,
            observed_ms,
            block_sources[block],
            block_reasons.get(block),
            funding_input.get("coverage_fraction", 1.0) if block == "funding" else 1.0,
        )
        for block in ENTRY_REQUIRED_BLOCKS
    }

    for meta in source_meta.values():
        meta["known_at_ms"] = collection_completed_ms
        meta["query_cutoff_ms"] = observed_ms

    stored_components = dict(components)
    stored_components["total"] = total
    result = EntryResult(
        symbol=name,
        as_of_ms=observed_ms,
        primary_tf=primary_tf,
        entry_score=total,
        components=stored_components,
        missing_blocks=tuple(missing),
        reason_code=None,
        inputs=inputs,
        source_meta=source_meta,
        dive_weights_hash=dive_weights_hash,
        dive_engine_version=dive_engine_version,
        dive_config_hash=dive_config_hash_value,
        shortlab_config_hash=shortlab_hash,
        snapshot_id=f"entry-{name}-{observed_ms}-{ENTRY_VERSION_CURRENT}",
        fetched_at_ms=fetched_completed_ms,
        created_at_ms=own_budget.now_ms(),
    )
    return result


def finalize_entry_snapshot(result: EntryResult, cutoff_ms: int) -> EntryResult:
    """Freeze already derived Entry at a later decision boundary.

    This retains every source/query timestamp. A UTC-day change invalidates
    the daily/funding window; the caller must recollect instead of relabelling
    yesterday's package as today. Historical replay remains unchanged.
    """
    cutoff = int(cutoff_ms)
    known = max([result.fetched_at_ms] + [int(m.get("known_at_ms") or 0)
                for m in result.source_meta.values()])
    if cutoff < max(result.as_of_ms, known):
        raise ValueError("Entry cutoff precedes frozen input availability")
    inputs = dict(result.inputs)
    inputs["_meta"] = {**inputs.get("_meta", {}), "as_of_ms": cutoff}
    changed_day = cutoff // 86400000 != result.as_of_ms // 86400000
    frozen_version = _result_entry_version(result)
    result = replace(result, as_of_ms=cutoff, inputs=inputs,
                     snapshot_id=f"entry-{result.symbol}-{cutoff}-{frozen_version}")
    if changed_day:
        inputs["consensus"] = {"reason": "ENTRY_WINDOW_ROLLOVER"}
        return replace(result, entry_score=None, reason_code="ENTRY_WINDOW_ROLLOVER",
                       missing_blocks=ENTRY_REQUIRED_BLOCKS,
                       components={**dict.fromkeys(ENTRY_REQUIRED_BLOCKS), "total": None})
    recomputed = recompute_entry_from_record(result.to_record())
    return replace(result, entry_score=recomputed["entry_score"],
                   missing_blocks=recomputed["missing_blocks"],
                   components={**recomputed["components"], "total": recomputed["entry_score"]})


async def save_entry_snapshot(repo: Any, result: EntryResult) -> str:
    """Persist an Entry snapshot via the Task 2 repository interface.

    Returns the ``entry_snapshot_id`` Task 13 passes to ``save_score``.
    Works for scored, null and budget-exhausted results alike.
    """
    return await repo.save_entry(result.to_record())


def recompute_entry_from_record(
    record: EntrySnapshotRecord,
) -> dict[str, Any]:
    """Recompute an Entry score from a stored snapshot without any network.

    Reads only the frozen ``inputs`` (six blocks); every data-client call in
    the test harness may raise and the result is unchanged. Accepts both
    legacy ``entry-v2`` and current ``entry-v3`` rows (bucket-agnostic math);
    replay never upgrades the stored version. Returns
    ``{symbol, as_of_ms, entry_score, components, missing_blocks}``.
    """
    inputs = dict(record.inputs or {})
    consensus = inputs.get("consensus") if isinstance(inputs.get("consensus"), Mapping) else {}
    mtf = inputs.get("mtf") if isinstance(inputs.get("mtf"), Mapping) else {}
    micro = inputs.get("micro") if isinstance(inputs.get("micro"), Mapping) else {}
    regime = inputs.get("regime") if isinstance(inputs.get("regime"), Mapping) else {}
    bounce_raw = inputs.get("failed_bounce") if isinstance(inputs.get("failed_bounce"), Mapping) else {}
    funding_raw = inputs.get("funding") if isinstance(inputs.get("funding"), Mapping) else {}
    snapshot_view = {
        "finalSignal": consensus.get("finalSignal"),
        "confidence": consensus.get("confidence"),
        "mtfConfluence": dict(mtf) if mtf.get("score") is not None else None,
        "microstructure": dict(micro) if micro.get("score") is not None else None,
        "regime": dict(regime) if regime.get("regime") is not None else None,
    }
    if mtf.get("reason") is not None and mtf.get("score") is None:
        snapshot_view["mtfConfluence"] = None
    bounce = bounce_raw.get("triggered")
    funding_view = {
        "positive_ratio_30d": funding_raw.get("positive_ratio_30d"),
        "complete": funding_raw.get("complete"),
    }
    total, components, missing = score_entry_detailed(snapshot_view, funding_view, bounce)
    # A stored snapshot whose consensus fetch failed stays null even if the
    # frozen payload happens to look complete: the fetch-time verdict wins.
    consensus_reason = consensus.get("reason")
    if consensus_reason in (_CONSENSUS_TIMEFRAME_MISSING, _CONSENSUS_ASSEMBLE_FAILED, "ENTRY_WINDOW_ROLLOVER"):
        if "consensus" not in missing:
            missing = ("consensus",) + tuple(missing)
        total = None
        components["consensus"] = None
    return {
        "symbol": record.symbol,
        "as_of_ms": record.as_of_ms,
        "entry_score": total,
        "components": components,
        "missing_blocks": tuple(missing),
    }


def build_historical_entry(symbol: str, end_ms: int | None = None, **kwargs: Any) -> Any:
    """Reject historical Entry rebuilds through live fetches.

    ``build_symbol(end_ms=...)`` truncates klines but still reads the live
    OI / ratio / funding tail, so any Entry derived from it mixes past
    candles with present positioning — a future-data leak. Short-Lab history
    replays stored snapshots via :func:`recompute_entry_from_record` only.
    """
    raise HistoricalReplayError(
        "Short-Lab historical replay must read the stored sl_entry_snapshot "
        f"(symbol={symbol}, end_ms={end_ms}); build_symbol(end_ms=...) still "
        "fetches live OI/ratio/funding and must never backfill Entry."
    )


async def run_entry_batch(
    symbols: list[str],
    *,
    budget: EntryBudget | None = None,
    primary_tf: str = "1h",
    now_ms: int | None = None,
    as_of_ms: int | None = None,
    as_of_by_symbol: Mapping[str, int] | None = None,
    max_symbols: int | None = 10,
) -> EntryBatchResult:
    """Run one budgeted Entry round over shortlist symbols.

    At most ``max_symbols`` symbols are attempted (runtime default 10, the
    ``entry_depth_top``); at most ``budget.concurrency`` builds overlap;
    every real upstream attempt spends ``budget``. When the budget runs dry
    the in-flight symbol records ``ENTRY_BUDGET_EXHAUSTED`` and the symbols
    never attempted are returned as ``queued_symbols`` for the next round —
    with no further network.
    """
    own_budget = budget if budget is not None else EntryBudget()
    ordered: list[str] = []
    for raw in symbols or []:
        name = str(raw).upper()
        if name not in ordered:
            ordered.append(name)
    if max_symbols is not None:
        capped = ordered[: max(0, int(max_symbols))]
        deferred = ordered[len(capped):]
    else:
        capped = ordered
        deferred = []
    started_ms = own_budget.now_ms()
    wall_start = time.time()
    used_before = own_budget.used_calls
    hits_before = own_budget.cache_hits
    results: dict[str, EntryResult] = {}
    if not capped:
        return EntryBatchResult(
            items=(),
            queued_symbols=tuple(deferred),
            stats={
                "requested": len(ordered),
                "attempted": 0,
                "scored": 0,
                "exhausted": 0,
                "queued": len(deferred),
                "calls_made": 0,
                "cache_hits": 0,
                "elapsed_ms": 0,
            },
        )

    queue: asyncio.Queue[str] = asyncio.Queue()
    for name in capped:
        queue.put_nowait(name)
    halt = asyncio.Event()

    async def worker() -> None:
        while not halt.is_set():
            try:
                next_symbol = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            async with own_budget.symbol_slot():
                if halt.is_set():
                    queue.put_nowait(next_symbol)
                    return
                result = await build_entry_snapshot(
                    next_symbol,
                    primary_tf,
                    budget=own_budget,
                    as_of_ms=(as_of_by_symbol or {}).get(next_symbol, as_of_ms),
                    now_ms=now_ms,
                )
                results[next_symbol] = result
                if result.reason_code == ENTRY_BUDGET_EXHAUSTED:
                    halt.set()

    worker_count = min(own_budget.concurrency, len(capped))
    await asyncio.gather(*[worker() for _ in range(worker_count)])
    queued = [name for name in capped if name not in results] + list(deferred)
    # Keep first-seen order for the queued tail.
    seen: set[str] = set()
    queued_ordered = [name for name in queued if not (name in seen or seen.add(name))]
    items = tuple(results[name] for name in capped if name in results)
    elapsed_ms = int((time.time() - wall_start) * 1000)
    scored = sum(
        1 for item in items
        if item.entry_score is not None and item.reason_code is None
    )
    exhausted = sum(1 for item in items if item.reason_code == ENTRY_BUDGET_EXHAUSTED)
    return EntryBatchResult(
        items=items,
        queued_symbols=tuple(queued_ordered),
        stats={
            "requested": len(ordered),
            "attempted": len(items),
            "scored": scored,
            "exhausted": exhausted,
            "queued": len(queued_ordered),
            "calls_made": own_budget.used_calls - used_before,
            "cache_hits": own_budget.cache_hits - hits_before,
            "elapsed_ms": elapsed_ms,
            "round_started_ms": started_ms,
        },
    )
