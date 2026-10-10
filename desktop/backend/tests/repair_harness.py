"""R15a repair harness skeleton (D16/D19.1).

Isolated test harness: real App/Runtime/Service/Repo + scheduler via the
frozen ``create_app(shortlab_runtime_factory=...)`` seam. The HTTP provider
layer returns R00 raw fixtures (never computed scores / net carry). The test
service runs in an independent backend Python process (see
``desktop/backend/tests/repair_server.py``), isolating the root
``tests/conftest.py`` global mocks for ``crypcodile`` / ``aiolimiter`` /
``httpx``. Root E2E clients must use stdlib ``urllib`` against the isolated
process, never the patched ``httpx`` transport.

Scenarios (all records stay inside the test ``data_dir``):

- ``normal``: valid positive funding (R00 ``valid`` case).
- ``negative-funding``: negative current/last funding (gate FAIL matrix).
- ``unknown-schedule``: unknown schedule / HISTORY_CLASS_UNKNOWN.
- ``slow-provider``: valid funding but slow HTTP provider (8s simulated
  latency marker; skeleton records the delay without sleeping 8s).
- ``plan-switch``: valid funding with plan-switch sequence (plan-a -> plan-b).
- ``plan-switchakit``: alias of ``plan-switch`` (task-text literal; kept so
  the harness accepts the exact scenario token from the R15a brief).

No direct injection of computed scores / net carry: fakes only return R00
raw fixtures (identity / funding / decision request / events / market / FX).

Frozen helper ``create_repair_test_app`` follows D19.1/D19.5 exactly::

    create_repair_test_app(data_dir, scenario, clock_ms, *, bindings=None)

``bindings=None`` is the R15b real-producer combination (allow False);
non-None must be D19.6 explicit TEST_FAKE ports (allow True) or the helper
rejects with ``TEST_BINDINGS_REQUIRED``. The helper never reads env to
enable fakes and never replaces ``App.state`` with a pre-built Service.

Full functional assertions belong to R15b; this skeleton only proves
isolation, clock control, scenario switching and port gating.
"""

from __future__ import annotations

import functools
import inspect
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Harness origin (frozen: 127.0.0.1:46409, harness-only, never production).
# ---------------------------------------------------------------------------

HARNESS_HOST = "127.0.0.1"
HARNESS_PORT = 46409
HARNESS_ORIGIN = f"http://{HARNESS_HOST}:{HARNESS_PORT}"

# Canonical five scenarios + task-literal alias.
HARNESS_SCENARIOS: tuple[str, ...] = (
    "normal",
    "negative-funding",
    "unknown-schedule",
    "slow-provider",
    "plan-switch",
    "plan-switchakit",
)

#: Canonical scenario set without the alias (plan-switchakit == plan-switch).
CANONICAL_SCENARIOS: tuple[str, ...] = (
    "normal",
    "negative-funding",
    "unknown-schedule",
    "slow-provider",
    "plan-switch",
)

#: Slow-provider simulated latency marker (skeleton records, never sleeps).
SLOW_PROVIDER_DELAY_MS = 8000

#: CR20: slow-provider real transport delay (seconds). The acceptance raw
#: HTTP stub really sleeps this long for slow-provider (bounded <8s
#: MARKET_DEADLINE_MS) so E2E proves a real delay occurred (dt>=1s) while
#: staying bounded (dt<8s). Marker-only without sleep is rejected.
SLOW_PROVIDER_REAL_DELAY_S = 1.5

#: Plan-switch sequence for the plan-switch / plan-switchakit scenarios.
PLAN_SWITCH_SEQUENCE: tuple[str, ...] = ("plan-a", "plan-b")

#: Scenario -> R00 funding-context case (repair_fixtures.make_funding_context).
SCENARIO_FUNDING_CASE: dict[str, str] = {
    "normal": "valid",
    "negative-funding": "negative",
    "unknown-schedule": "unknown",
    "slow-provider": "valid",
    "plan-switch": "valid",
    "plan-switchakit": "valid",
}


def canonical_scenario(scenario: str) -> str:
    """Map the task-literal alias ``plan-switchakit`` to ``plan-switch``."""
    if scenario == "plan-switchakit":
        return "plan-switch"
    return scenario


# ---------------------------------------------------------------------------
# seed_funding helper (harness-only, never production).
# Writes CONFIRMED schedule + funding events + observations for honest
# six-item activation. Scenario-aware: negative-funding forces negative
# rate, unknown-schedule is a no-op (never broadcasts schedule).
# ---------------------------------------------------------------------------


async def _do_seed_funding(
    repo: Any,
    symbol: str,
    rate: float,
    days: int,
    interval_hours: int,
    now_ms: int,
) -> dict[str, Any]:
    """Seed CONFIRMED funding history for ``symbol`` up to ``now_ms``.

    Reuses an existing CONFIRMED schedule with the same interval when
    present (avoids overlapping CONFIRMED overlap-UNKNOWN); otherwise
    creates one with ``effective_from=start`` and ``anchor=first slot``.
    Events/observations are upserted for every slot in
    ``(start, now]`` (left-open, right-closed, matching funding_score).
    Observations carry ``known_at=slot`` (<= cutoff) so the market
    ``collect_funding`` last-settled receipt is honestly present.
    """
    sym = str(symbol).upper().strip()
    if not sym:
        raise ValueError("symbol must be non-empty str")
    try:
        rate_f = float(rate)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rate must be a number: {exc}") from exc
    import math as _math

    if not _math.isfinite(rate_f):
        raise ValueError("rate must be finite")
    try:
        days_i = int(days)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"days must be an int: {exc}") from exc
    try:
        interval_i = int(interval_hours)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"interval_hours must be an int: {exc}") from exc
    if days_i < 1 or days_i > 365:
        raise ValueError("days must be in 1..365")
    if interval_i < 1 or interval_i > 24:
        raise ValueError("interval_hours must be in 1..24")
    now = int(now_ms)
    if now < 0:
        raise ValueError("now_ms must be >= 0")
    interval_ms = int(interval_i) * 3_600_000
    day_ms = 86_400_000
    # Reuse existing CONFIRMED schedule with same interval when present.
    existing_start: int | None = None
    existing_anchor: int | None = None
    existing_id: str | None = None
    try:
        rows = await repo.list_funding_schedules(sym, int(now))  # type: ignore[attr-defined]
    except Exception:
        rows = ()
    try:
        for _r in (rows or ()):
            try:
                _sj: Any = None
                if isinstance(_r, dict):
                    _sj = _r.get("schedule_json")
                else:
                    _sj = getattr(_r, "schedule_json", None)
                    if isinstance(_sj, str):
                        import json as _js0

                        try:
                            _sj = _js0.loads(_sj)
                        except Exception:
                            _sj = None
                if isinstance(_sj, dict):
                    _ver = str(_sj.get("verification") or "")
                    _iv = _sj.get("interval_hours")
                    try:
                        _iv_i = int(_iv) if _iv is not None else None
                    except (TypeError, ValueError):
                        _iv_i = None
                    if _ver == "CONFIRMED" and _iv_i == int(interval_i):
                        # Reuse earliest effective_from for stability.
                        try:
                            if isinstance(_r, dict):
                                _eff = int(_r.get("effective_from_ms"))  # type: ignore[arg-type]
                                _anc = int(_sj.get("anchor_ms"))  # type: ignore[arg-type]
                                _sid = str(_sj.get("schedule_id") or _r.get("schedule_id"))
                            else:
                                _eff = int(getattr(_r, "effective_from_ms"))
                                _anc = int(_sj.get("anchor_ms"))
                                _sid = str(_sj.get("schedule_id") or getattr(_r, "schedule_id"))
                        except (TypeError, ValueError, AttributeError):
                            continue
                        if existing_start is None or int(_eff) < int(existing_start):
                            existing_start = int(_eff)
                            existing_anchor = int(_anc)
                            existing_id = str(_sid)
            except Exception:
                continue
    except Exception:
        pass
    if existing_start is not None and existing_anchor is not None and existing_id is not None:
        start = int(existing_start)
        anchor = int(existing_anchor)
        schedule_id = str(existing_id)
        need_new_schedule = False
    else:
        start = int(now) - int(days_i) * int(day_ms)
        # First slot after start (left-open window (start, now]).
        anchor = int(start) + int(interval_ms)
        schedule_id = f"seed-{sym}-{int(interval_i)}h-{int(start)}"
        need_new_schedule = True
    # Slots in (start, now] aligned to anchor + k*interval.
    import math as _math2

    slots: list[int] = []
    try:
        k_min = _math2.ceil((int(start) + 1 - int(anchor)) / float(interval_ms))
        k_max = _math2.floor((int(now) - int(anchor)) / float(interval_ms))
        for _k in range(int(k_min), int(k_max) + 1):
            _t = int(anchor) + int(_k) * int(interval_ms)
            if _t > int(start) and _t <= int(now):
                slots.append(int(_t))
    except Exception:
        # Fallback linear walk (same result, slower).
        _cur = int(start) + int(interval_ms)
        while _cur <= int(now):
            slots.append(int(_cur))
            _cur += int(interval_ms)
    # Events (canonical funding history; conflicts keep old, never overwrite).
    try:
        await repo.upsert_funding_events(  # type: ignore[attr-defined]
            [{"symbol": sym, "funding_time_ms": int(_s), "funding_rate": float(rate_f)} for _s in slots]
        )
    except Exception as exc:
        raise RuntimeError(f"seed events failed: {type(exc).__name__}: {exc}") from exc
    # Observations (persisted receipts for last-settled; same id+content is no-op).
    obs_ok = 0
    for _s in slots:
        _oid = f"seed-obs-{sym}-{int(_s)}"
        try:
            await repo.save_funding_observation(  # type: ignore[attr-defined]
                {
                    "observation_id": _oid,
                    "symbol": sym,
                    "funding_time_ms": int(_s),
                    "known_at_ms": int(_s),
                    "raw_json": {"rate": str(float(rate_f)), "funding_time_ms": int(_s)},
                    "interval_hours": float(interval_i),
                    "interval_source": "CONFIRMED_SCHEDULE",
                    "observation_status": "OBSERVED",
                }
            )
            obs_ok += 1
        except Exception:
            # Immutable same-content retry is fine; different-content keeps old.
            # Swallow per-slot to stay honest (events already carry history).
            try:
                # If immutable conflict with different content, keep old (honest).
                pass
            except Exception:
                pass
            continue
    # Schedule (CONFIRMED, idempotent on same id+content).
    sched_ok = 0
    if need_new_schedule:
        try:
            await repo.save_funding_schedule(  # type: ignore[attr-defined]
                {
                    "schedule_id": schedule_id,
                    "symbol": sym,
                    "effective_from_ms": int(start),
                    "effective_to_ms": None,
                    "known_at_ms": int(start),
                    "schedule_json": {
                        "schedule_id": schedule_id,
                        "symbol": sym,
                        "effective_from_ms": int(start),
                        "effective_to_ms": None,
                        "interval_hours": int(interval_i),
                        "anchor_ms": int(anchor),
                        "known_at_ms": int(start),
                        "source": "binance:fapi/fundingInfo",
                        "evidence_ref": f"seed-{sym}",
                        "verification": "CONFIRMED",
                    },
                }
            )
            sched_ok = 1
        except Exception as exc:
            # Same id same content is fine (already seeded); different content
            # with same id must not silently overwrite (honest 409-style).
            try:
                from diveintocrypto_desktop.shortlab.repository import SnapshotImmutableError as _Imm  # type: ignore

                if isinstance(exc, _Imm):
                    # Already seeded with same id (reuse path should have hit);
                    # treat as ok without overwrite.
                    sched_ok = 0
                else:
                    raise
            except RuntimeError:
                raise
            except Exception:
                # If SnapshotImmutableError import fails, re-raise original.
                if "immutable" in str(exc).lower():
                    sched_ok = 0
                else:
                    raise RuntimeError(f"seed schedule failed: {exc}") from exc
    return {
        "symbol": sym,
        "rate": str(float(rate_f)),
        "days": int(days_i),
        "interval_hours": int(interval_i),
        "slots": int(len(slots)),
        "observations": int(obs_ok),
        "schedule": int(sched_ok),
        "reused": bool(not need_new_schedule),
    }


# ---------------------------------------------------------------------------
# FakeClock (advancing test clock; injected as Runtime ``clock``).
# ---------------------------------------------------------------------------


class FakeClock:
    """Advancing test clock ``() -> int`` (UTC ms).

    Mirrors the ``FakeClock`` idiom used across backend tests: starts at
    ``start_ms`` (default R00 ``FIXTURE_NOW`` when available, else a fixed
    instant), ``advance(ms)`` moves forward, ``set(ms)`` jumps, ``now()``
    reads. Never touches real time.
    """

    def __init__(self, start_ms: int | None = None) -> None:
        if start_ms is None:
            try:  # R00 raw fixture instant; fallback keeps skeleton importable.
                from tests.repair_fixtures import FIXTURE_NOW as _NOW  # type: ignore[import-not-found]

                start_ms = int(_NOW)
            except Exception:
                try:
                    from repair_fixtures import FIXTURE_NOW as _NOW2  # type: ignore[import-not-found]

                    start_ms = int(_NOW2)
                except Exception:
                    start_ms = 1791417600000
        self.ms = int(start_ms)

    def __call__(self) -> int:
        return int(self.ms)

    def now(self) -> int:
        return int(self.ms)

    def advance(self, ms: int) -> int:
        """Advance the clock by ``ms`` milliseconds (ms >= 0)."""
        delta = int(ms)
        if delta < 0:
            raise ValueError("FakeClock.advance requires ms >= 0")
        self.ms += delta
        return int(self.ms)

    def set(self, ms: int) -> int:
        """Jump the clock to an absolute ``ms`` instant."""
        self.ms = int(ms)
        return int(self.ms)


def advance_harness_clock(clock: Any, ms: int) -> int:
    """Advance a harness clock (FakeClock with ``advance``).

    Raises ``TypeError`` when the clock is not advanceable so tests catch a
    non-harness clock instead of silently drifting real time.
    """
    adv = getattr(clock, "advance", None)
    if not callable(adv):
        raise TypeError("harness clock is not advanceable (missing advance(ms))")
    return int(adv(int(ms)))


# ---------------------------------------------------------------------------
# Fake raw provider (R00 raw fixtures only; no computed scores).
# ---------------------------------------------------------------------------


def _import_repair_fixtures() -> Any:
    """Import R00 ``repair_fixtures`` via either ``tests.`` or bare path."""
    try:
        import tests.repair_fixtures as _fx  # type: ignore[import-not-found]

        return _fx
    except Exception:
        pass
    try:
        import repair_fixtures as _fx2  # type: ignore[import-not-found]

        return _fx2
    except Exception as exc:
        raise ImportError("R00 repair_fixtures is required for harness raw fakes") from exc


class FakeHarnessRawProvider:
    """Fake HTTP-provider layer returning R00 raw fixtures per scenario.

    Only raw inputs are served (identity / funding / decision request /
    events / market / FX). Computed outputs (scores, net carry, PnL, outcome)
    are never injected here -- producers compute them or stay UNKNOWN.
    """

    def __init__(self, scenario: str = "normal") -> None:
        if scenario not in HARNESS_SCENARIOS:
            raise ValueError(f"unknown harness scenario {scenario!r}; want {list(HARNESS_SCENARIOS)}")
        self._scenario = scenario

    @property
    def scenario(self) -> str:
        return self._scenario

    def switch(self, scenario: str) -> None:
        if scenario not in HARNESS_SCENARIOS:
            raise ValueError(f"unknown harness scenario {scenario!r}")
        self._scenario = scenario

    def funding_case(self) -> str:
        return SCENARIO_FUNDING_CASE[self._scenario]

    def funding_context(self) -> Any:
        fx = _import_repair_fixtures()
        return fx.make_funding_context(self.funding_case())

    def decision_context(self) -> Any:
        fx = _import_repair_fixtures()
        # Decision context always uses the MEME_FULL_VALID baseline; the
        # funding leg varies by scenario (negative/unknown matrix).
        funding = self.funding_context()
        return fx.make_decision_context("MEME_FULL_VALID", funding_context=funding)

    def decision_request(self) -> Any:
        fx = _import_repair_fixtures()
        return fx.make_decision_request("MEME_FULL_VALID")

    def events(self) -> Any:
        fx = _import_repair_fixtures()
        return fx.make_events("partial_close")

    def event_fx(self) -> Any:
        fx = _import_repair_fixtures()
        return fx.make_event_fx("partial_close")

    def market_context(self) -> Any:
        fx = _import_repair_fixtures()
        return fx.make_market_context("partial_close")

    def slow_provider_delay_ms(self) -> int:
        """Simulated provider latency marker (0 except slow-provider)."""
        if canonical_scenario(self._scenario) == "slow-provider" or self._scenario == "slow-provider":
            return int(SLOW_PROVIDER_DELAY_MS)
        return 0

    def plan_sequence(self) -> tuple[str, ...]:
        if canonical_scenario(self._scenario) == "plan-switch":
            return PLAN_SWITCH_SEQUENCE
        return ()

    def raw_payload(self) -> dict[str, Any]:
        """Full R00 raw-fixture payload for the current scenario."""
        fx = _import_repair_fixtures()
        identity = fx.make_identity("m1000")
        funding = self.funding_context()
        return {
            "scenario": self._scenario,
            "canonical_scenario": canonical_scenario(self._scenario),
            "funding_case": self.funding_case(),
            "identity": identity,
            "funding_context": funding,
            "decision_request": self.decision_request(),
            "decision_context": self.decision_context(),
            "events": self.events(),
            "event_fx": self.event_fx(),
            "market_context": self.market_context(),
            "slow_provider_delay_ms": self.slow_provider_delay_ms(),
            "plan_sequence": list(self.plan_sequence()),
        }


# Backwards-friendly alias (task text says "Fake provider").
FakeHarnessProvider = FakeHarnessRawProvider


def get_raw_provider_payload(scenario: str) -> dict[str, Any]:
    """Return the R00 raw-fixture payload for ``scenario`` (no scores)."""
    return FakeHarnessRawProvider(scenario).raw_payload()


def make_scenario_funding_context(scenario: str) -> Any:
    """Return the R00 funding context for a harness ``scenario``."""
    if scenario not in HARNESS_SCENARIOS:
        raise ValueError(f"unknown harness scenario {scenario!r}")
    fx = _import_repair_fixtures()
    return fx.make_funding_context(SCENARIO_FUNDING_CASE[scenario])


# ---------------------------------------------------------------------------
# TEST_FAKE gate (D19.6): non-None bindings must be explicit test fakes.
# ---------------------------------------------------------------------------


def _unwrap_callback(fn: Any) -> Any:
    """Unwrap functools.partial / decorator __wrapped__ chains."""
    seen = 0
    cur = fn
    while seen < 10:
        seen += 1
        if isinstance(cur, functools.partial):
            cur = cur.func
            continue
        wrapped = getattr(cur, "__wrapped__", None)
        if wrapped is not None:
            cur = wrapped
            continue
        break
    return cur


def _is_test_fake_callback(fn: Any) -> bool:
    """Whether ``fn`` is a D19.6 explicit TEST_FAKE (repair_fixtures fake_*)."""
    if not callable(fn):
        return False
    target = _unwrap_callback(fn)
    module = str(getattr(target, "__module__", "") or "")
    name = str(getattr(target, "__name__", "") or "")
    if "repair_fixtures" not in module:
        return False
    # R00 fakes are all named fake_* (make_ports is the factory, not a port).
    if name.startswith("fake_"):
        return True
    # make_ports defaults are exactly the nine fake_* above; anything else
    # from repair_fixtures (e.g. make_identity) is not a port callback.
    return False


def _require_test_fake_bindings(bindings: Any) -> None:
    """Reject non-None real-producer bindings with TEST_BINDINGS_REQUIRED."""
    try:
        from diveintocrypto_desktop.shortlab.repair_ports import REPAIR_PORT_KEYS
    except Exception:
        REPAIR_PORT_KEYS = (
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
    offenders: list[str] = []
    for key in REPAIR_PORT_KEYS:
        try:
            cb = getattr(bindings, key, None)
        except Exception:
            cb = None
        if cb is None:
            continue  # Explicitly unbound slot is allowed (boundary test).
        if not _is_test_fake_callback(cb):
            offenders.append(str(key))
    if offenders:
        raise RuntimeError(
            "TEST_BINDINGS_REQUIRED: non-None harness bindings must be D19.6 "
            f"explicit TEST_FAKE ports (repair_fixtures fake_*); offenders={offenders}. "
            "Real-producer combinations must use bindings=None (R15b)."
        )


# ---------------------------------------------------------------------------
# Frozen helper: create_repair_test_app (D19.1/D19.5, R15a frozen).
# ---------------------------------------------------------------------------


def create_repair_test_app(
    data_dir: Path,
    scenario: str,
    clock_ms: Callable[[], int],
    *,
    bindings: Any | None = None,
) -> Any:
    """Build the isolated harness FastAPI app (frozen signature).

    Uses the ``create_app(shortlab_runtime_factory=closure)`` seam to
    construct a real ``ShortLabRuntime`` (real Service/Repo/scheduler);
    ``clock_ms`` is passed as the Runtime's existing ``clock`` parameter.
    The closure explicitly passes ``repair_ports=bindings`` and
    ``allow_test_bindings=(bindings is not None)``. ``bindings=None`` is the
    R15b real combination (False); non-None must be TEST_FAKE or the helper
    rejects with ``TEST_BINDINGS_REQUIRED``.

    The helper never replaces ``App.state`` with a pre-built Service and
    never enables fakes from the environment. Test-only harness control
    routes (``/test/harness/*``) are mounted here; they do not exist in
    production.
    """
    if scenario not in HARNESS_SCENARIOS:
        raise ValueError(f"unknown harness scenario {scenario!r}; want {list(HARNESS_SCENARIOS)}")
    if not callable(clock_ms):
        raise TypeError("clock_ms must be a callable () -> int (ms)")
    try:
        data_path = Path(data_dir).expanduser()
    except Exception as exc:
        raise TypeError(f"data_dir must be path-like: {exc}") from exc
    data_path.mkdir(parents=True, exist_ok=True)

    if bindings is not None:
        # Must look like a RepairPorts bundle; real producers are rejected.
        try:
            from diveintocrypto_desktop.shortlab.repair_ports import RepairPorts as _RP

            if not isinstance(bindings, _RP):
                raise RuntimeError(
                    "TEST_BINDINGS_REQUIRED: harness bindings must be RepairPorts "
                    f"(got {type(bindings).__name__}); real producers use bindings=None."
                )
        except RuntimeError:
            raise
        except Exception:
            # If RepairPorts is unavailable, still enforce per-key fake gating.
            pass
        _require_test_fake_bindings(bindings)

    from diveintocrypto_desktop.api.app import create_app
    from diveintocrypto_desktop.shortlab.runtime import ShortLabRuntime

    factory_calls: list[int] = []
    harness_state: dict[str, Any] = {
        "scenario": scenario,
        "canonical_scenario": canonical_scenario(scenario),
        "data_dir": str(data_path),
        "bindings_mode": "TEST_FAKE" if bindings is not None else "REAL_PRODUCERS",
        "origin": HARNESS_ORIGIN,
    }

    def shortlab_runtime_factory() -> Any:
        # Factory takes no arguments (D19.1); closure carries data_dir /
        # scenario / clock. Explicit formula (frozen):
        #   ShortLabRuntime(..., repair_ports=bindings,
        #                   allow_test_bindings=(bindings is not None))
        factory_calls.append(1)
        return ShortLabRuntime(
            data_dir=data_path,
            clock=clock_ms,
            repair_ports=bindings,
            allow_test_bindings=(bindings is not None),
        )

    # The factory seam is keyword-only with default None and no other params.
    sig = inspect.signature(shortlab_runtime_factory)
    assert len(sig.parameters) == 0, "harness factory must take no arguments"

    app = create_app(shortlab_runtime_factory=shortlab_runtime_factory)

    # -- test-only harness control routes (absent from production) -----------
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    control = APIRouter(prefix="/test/harness", tags=["repair-harness"])

    @control.get("/state")
    async def _harness_state() -> Any:
        try:
            now_ms = int(clock_ms())
        except Exception:
            now_ms = -1
        provider = FakeHarnessRawProvider(harness_state["scenario"])
        return {
            "scenario": harness_state["scenario"],
            "canonicalScenario": harness_state["canonical_scenario"],
            "nowMs": now_ms,
            "origin": HARNESS_ORIGIN,
            "bindingsMode": harness_state["bindings_mode"],
            "fundingCase": provider.funding_case(),
            "slowProviderDelayMs": provider.slow_provider_delay_ms(),
            "planSequence": list(provider.plan_sequence()),
        }

    @control.post("/advance")
    async def _harness_advance(payload: dict[str, Any]) -> Any:
        try:
            ms = int((payload or {}).get("ms", 0))
        except Exception:
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        if ms < 0:
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        try:
            new_now = advance_harness_clock(clock_ms, ms)
        except TypeError:
            return JSONResponse({"error": "HARNESS_CLOCK_NOT_ADVANCEABLE"}, status_code=400)
        return {"nowMs": int(new_now), "advancedMs": int(ms)}

    @control.post("/scenario")
    async def _harness_switch(payload: dict[str, Any]) -> Any:
        name = str((payload or {}).get("scenario", ""))
        if name not in HARNESS_SCENARIOS:
            return JSONResponse({"error": "HARNESS_SCENARIO_UNKNOWN"}, status_code=422)
        harness_state["scenario"] = name
        harness_state["canonical_scenario"] = canonical_scenario(name)
        return {"scenario": name, "canonicalScenario": harness_state["canonical_scenario"]}

    @control.post("/seed_funding")
    async def _harness_seed_funding(payload: dict[str, Any]) -> Any:
        if not isinstance(payload, dict):
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        try:
            symbol = str(payload.get("symbol", "") or "").strip()
            rate_raw = payload.get("rate", None)
            days_raw = payload.get("days", None)
            interval_raw = payload.get("interval_hours", payload.get("intervalHours", None))
            if not symbol:
                return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
            if rate_raw is None or days_raw is None or interval_raw is None:
                return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
            rate_f = float(str(rate_raw))
            days_i = int(days_raw)
            interval_i = int(interval_raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        # Scenario-aware: unknown-schedule never broadcasts schedule/events.
        try:
            _canon = str(harness_state.get("canonical_scenario") or harness_state.get("scenario") or "")
        except Exception:
            _canon = ""
        if _canon == "unknown-schedule":
            return {"symbol": symbol.upper(), "seeded": 0, "slots": 0, "reason": "UNKNOWN_SCHEDULE_NO_BROADCAST"}
        # Negative scenario broadcasts negative rate (force negative).
        if _canon == "negative-funding" and float(rate_f) > 0:
            rate_f = -abs(float(rate_f))
        try:
            now_ms = int(clock_ms())
        except Exception:
            return JSONResponse({"error": "HARNESS_CLOCK_NOT_ADVANCEABLE"}, status_code=400)
        try:
            _rt = app.state.shortlab_runtime  # type: ignore[attr-defined]
        except Exception:
            _rt = None
        _repo: Any = None
        try:
            if _rt is not None:
                _repo = getattr(_rt, "_repository", None) or getattr(_rt, "repository", None)
                if _repo is None:
                    try:
                        _svc = getattr(_rt, "service", None)
                        # service may be property raising when unavailable.
                        if _svc is not None and not callable(_svc):
                            _repo = getattr(_svc, "_repository", None)
                        elif callable(_svc):
                            try:
                                _s2 = _svc()
                                _repo = getattr(_s2, "_repository", None)
                            except Exception:
                                pass
                    except Exception:
                        pass
                # repository property may be None before start; try service repo.
                if _repo is None:
                    try:
                        _svc2 = _rt.service  # type: ignore[attr-defined]
                        _repo = getattr(_svc2, "_repository", None)
                    except Exception:
                        pass
        except Exception:
            _repo = None
        if _repo is None:
            return JSONResponse({"error": "HARNESS_STORE_UNAVAILABLE"}, status_code=503)
        try:
            out = await _do_seed_funding(_repo, symbol, float(rate_f), int(days_i), int(interval_i), int(now_ms))
        except ValueError:
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        except RuntimeError as exc:
            return JSONResponse({"error": "HARNESS_SEED_FAILED", "detail": str(exc)[:200]}, status_code=500)
        except Exception as exc:
            return JSONResponse({"error": "HARNESS_SEED_FAILED", "detail": f"{type(exc).__name__}"[:200]}, status_code=500)
        # Honest cache invalidation for FakeClock jumps (real-time QuoteCache).
        try:
            _svc3 = None
            try:
                _svc3 = _rt.service  # type: ignore[attr-defined]
            except Exception:
                try:
                    _svc3 = getattr(_rt, "_service", None)
                except Exception:
                    pass
            _mkt = getattr(_svc3, "_hedge_market", None) if _svc3 is not None else None
            if _mkt is not None:
                try:
                    getattr(_mkt, "_marks", {}).clear()  # type: ignore
                except Exception:
                    pass
                try:
                    getattr(_mkt, "_funding_cache", {}).clear()  # type: ignore
                except Exception:
                    pass
                try:
                    _mkt._exchange_cache = None  # type: ignore[attr-defined]
                except Exception:
                    pass
                for _vn in ("spot", "alpha"):
                    try:
                        _v = getattr(_mkt, _vn, None)
                        if _v is not None and hasattr(_v, "reset_cache"):
                            _v.reset_cache()  # type: ignore
                        elif _v is not None and hasattr(_v, "_quotes"):
                            try:
                                _v._quotes.clear()  # type: ignore
                            except Exception:
                                pass
                    except Exception:
                        pass
        except Exception:
            pass
        return {"symbol": out.get("symbol"), "seeded": out.get("slots"), "slots": out.get("slots"), **out}

    @control.get("/debug_activation")
    async def _harness_debug_activation(symbol: str = "") -> Any:
        sym = str(symbol or "").upper().strip() or "BTCUSDT"
        try:
            _rt2 = app.state.shortlab_runtime  # type: ignore[attr-defined]
        except Exception:
            _rt2 = None
        _repo2: Any = None
        try:
            if _rt2 is not None:
                _repo2 = getattr(_rt2, "_repository", None) or getattr(_rt2, "repository", None)
                if _repo2 is None:
                    try:
                        _svc = getattr(_rt2, "service", None)
                        if _svc is not None and not callable(_svc):
                            _repo2 = getattr(_svc, "_repository", None)
                    except Exception:
                        pass
                if _repo2 is None:
                    try:
                        _svc2 = _rt2.service  # type: ignore[attr-defined]
                        _repo2 = getattr(_svc2, "_repository", None)
                    except Exception:
                        pass
        except Exception:
            _repo2 = None
        if _repo2 is None:
            return JSONResponse({"error": "HARNESS_STORE_UNAVAILABLE"}, status_code=503)
        try:
            now2 = int(clock_ms())
        except Exception:
            now2 = 0
        try:
            rows = await _repo2.list_market_observations(sym, "ACTIVATION_CHECK", 0, int(now2) + 1000, int(now2) + 1000)  # type: ignore[attr-defined]
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if not rows:
            return {"symbol": sym, "count": 0, "checks": {}}
        last = rows[-1]
        try:
            vj = last.get("value_json") or {}
            if isinstance(vj, str):
                import json as _jsd

                vj = _jsd.loads(vj)
        except Exception:
            vj = {}
        return {"symbol": sym, "count": len(rows), "checks": (vj.get("checks") if isinstance(vj, dict) else {}), "value": vj}

    app.include_router(control)

    # Starlette matches routes in order; production mounts StaticFiles at "/"
    # (UI dist) inside create_app, which would shadow harness routes added
    # afterwards. Reorder so harness control stays reachable while the UI
    # mount remains last. Production sources are untouched.
    try:
        _harness_paths = {"/test/harness/state", "/test/harness/advance", "/test/harness/scenario", "/test/harness/seed_funding", "/test/harness/debug_activation"}
        _harness_routes = [r for r in app.routes if str(getattr(r, "path", "")) in _harness_paths]
        if _harness_routes:
            _rest = [r for r in app.routes if r not in _harness_routes]
            # Split trailing mounts (StaticFiles at "/" or Mount instances).
            _mounts: list[Any] = []
            _plain: list[Any] = []
            for r in _rest:
                _name = type(r).__name__
                if _name == "Mount" or "StaticFiles" in _name or str(getattr(r, "path", "")) == "/":
                    _mounts.append(r)
                else:
                    _plain.append(r)
            app.routes[:] = _plain + _harness_routes + _mounts  # type: ignore[attr-defined]
            try:
                app.router.routes[:] = app.routes  # type: ignore[attr-defined]
            except Exception:
                pass
    except Exception:
        pass

    # Observable harness metadata for self-checks (not a production field).
    try:
        app.state._repair_harness_state = harness_state  # type: ignore[attr-defined]
        app.state._repair_factory_calls = factory_calls  # type: ignore[attr-defined]
        app.state._repair_harness_clock = clock_ms  # type: ignore[attr-defined]
        app.state._repair_harness_scenario = scenario  # type: ignore[attr-defined]
    except Exception:
        pass
    return app


# ---------------------------------------------------------------------------
# R15b acceptance helper (extends R15a skeleton; production untouched).
# Real nine RepairPorts (bindings=None) + enabled switches + raw HTTP
# fixtures only (mark/quote/funding/rules/identity/metadata). No computed
# scores / net carry / PnL / outcome are injected; producers compute them.
# Scenario-aware funding/metadata read the live harness_state so
# POST /test/harness/scenario switches matrix without restart.
# ---------------------------------------------------------------------------

ACCEPTANCE_MODE = "REAL_PRODUCERS_WITH_RAW_FIXTURES"


def _acceptance_identity_overrides() -> dict[str, Any]:
    return {
        "1000PEPEUSDT": {
            "canonical_id": "pepe",
            "display_symbol": "1000PEPEUSDT",
            "contract_multiplier": 1000,
            "multiplier_source": "EXCHANGE",
            "mapping_confidence": "VERIFIED",
            "mapping_source": "MANUAL",
            "binance_spot_symbol": "1000PEPEUSDT",
            "coingecko_id": "pepe",
        },
        "BTCUSDT": {
            "canonical_id": "bitcoin",
            "display_symbol": "BTCUSDT",
            "contract_multiplier": 1,
            "multiplier_source": "EXCHANGE",
            "mapping_confidence": "VERIFIED",
            "mapping_source": "MANUAL",
            "binance_spot_symbol": "BTCUSDT",
            "coingecko_id": "bitcoin",
        },
    }


def _acceptance_enabled_config() -> Any:
    import dataclasses as _dc

    from diveintocrypto_desktop.shortlab.config import load_shortlab_config as _load

    base = _load()
    try:
        hedge = _dc.replace(base.hedge, enabled=True)
        funding = _dc.replace(base.funding_capture, enabled=True)
        return _dc.replace(base, hedge=hedge, funding_capture=funding)
    except Exception:
        return base


def create_repair_acceptance_app(
    data_dir: Path,
    scenario: str,
    clock_ms: Callable[[], int],
    *,
    bindings: Any | None = None,
) -> Any:
    """R15b acceptance app: real producers + raw HTTP fixtures only (CR20).

    Same frozen seam as :func:`create_repair_test_app` (factory called once,
    lifespan start/stop once, harness control routes only here), but the
    Runtime uses an explicitly enabled config (hedge + funding_capture) and
    only the original HTTP responses + FakeClock are stubbed deterministically
    (premiumIndex / exchangeInfo / bookTicker / depth / fundingRate-empty /
    universe listing + 1.5s real slow-provider sleep). Computed legs
    (mark/quote/funding/rules via ProductionHedgeMarket + nine real
    RepairPorts) are never overwritten. Identity overrides are static verified
    mappings (raw config, not scores); FX cache is a raw HTTP cache entry.
    The nine RepairPorts stay real (bindings=None => REAL_PRODUCERS/False);
    explicit non-None bindings must still be D19.6 TEST_FAKE or this helper
    raises TEST_BINDINGS_REQUIRED. No computed Score/PnL/Outcome is injected.
    """
    if scenario not in HARNESS_SCENARIOS:
        raise ValueError(f"unknown harness scenario {scenario!r}; want {list(HARNESS_SCENARIOS)}")
    if not callable(clock_ms):
        raise TypeError("clock_ms must be a callable () -> int (ms)")
    try:
        data_path = Path(data_dir).expanduser()
    except Exception as exc:
        raise TypeError(f"data_dir must be path-like: {exc}") from exc
    data_path.mkdir(parents=True, exist_ok=True)

    if bindings is not None:
        try:
            from diveintocrypto_desktop.shortlab.repair_ports import RepairPorts as _RP

            if not isinstance(bindings, _RP):
                raise RuntimeError(
                    "TEST_BINDINGS_REQUIRED: harness bindings must be RepairPorts "
                    f"(got {type(bindings).__name__}); real producers use bindings=None."
                )
        except RuntimeError:
            raise
        except Exception:
            pass
        _require_test_fake_bindings(bindings)

    from diveintocrypto_desktop.api.app import create_app
    from diveintocrypto_desktop.shortlab.runtime import ShortLabRuntime

    enabled_cfg = _acceptance_enabled_config()
    factory_calls: list[int] = []
    harness_state: dict[str, Any] = {
        "scenario": scenario,
        "canonical_scenario": canonical_scenario(scenario),
        "data_dir": str(data_path),
        "bindings_mode": "TEST_FAKE" if bindings is not None else "REAL_PRODUCERS",
        "origin": HARNESS_ORIGIN,
        "acceptance": ACCEPTANCE_MODE,
    }

    # -- CR20 raw-HTTP-only stubs (test process only) ---------------------
    # Only the original HTTP responses + FakeClock are replaced. Computed
    # legs (mark/quote/funding/rules via ProductionHedgeMarket + nine real
    # RepairPorts) keep running honestly. Scenario-aware via live
    # harness_state so POST /test/harness/scenario switches without restart.
    def _raw_now() -> int:
        try:
            return int(clock_ms())
        except Exception:
            return 1791417600000

    def _raw_scenario() -> str:
        try:
            return canonical_scenario(str(harness_state.get("scenario") or "normal"))
        except Exception:
            return "normal"

    def _raw_price_for(symbol: str) -> float:
        s = str(symbol).upper()
        if "PEPE" in s:
            return 0.012
        if s == "BTCUSDT":
            return 67000.0
        return 100.0

    def _raw_filters() -> list[dict[str, Any]]:
        return [
            {"filterType": "PRICE_FILTER", "minPrice": "0.000001", "maxPrice": "1000000", "tickSize": "0.000001"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "10000000", "stepSize": "0.001"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "10000000", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True, "avgPriceMins": 5},
        ]

    def _raw_entry(sym: str, onboard: int | None) -> dict[str, Any]:
        e: dict[str, Any] = {
            "symbol": sym,
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "baseAsset": sym.replace("USDT", ""),
            "quoteAsset": "USDT",
            "orderTypes": ["LIMIT", "MARKET", "STOP", "STOP_MARKET"],
            "filters": _raw_filters(),
        }
        if onboard is not None:
            e["onboardDate"] = int(onboard)
        return e

    # Install raw patches (CR20: last harness_state wins for the single
    # isolated server on 46409; reinstall per app so scenario-aware closures
    # stay fresh for sequential TestClient apps).
    try:
        import asyncio as _raw_aio  # noqa: F401
        from diveintocrypto_desktop.data import funding as _raw_fund_mod
        from diveintocrypto_desktop.data import http as _raw_http_mod
        from diveintocrypto_desktop.data import universe as _raw_uni_mod

        # Save true originals once (first install) so reinstalls wrap originals,
        # not already-wrapped stubs.
        if getattr(_raw_fund_mod, "_cr20_orig_saved", None) is not True:
            try:
                _raw_fund_mod._cr20_orig_premium = _raw_fund_mod.premium_index  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                _raw_fund_mod._cr20_orig_hist = getattr(_raw_fund_mod, "funding_history_range", None)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                _raw_fund_mod._cr20_orig_get_json_fund = getattr(_raw_fund_mod, "get_json", None)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                _raw_http_mod._cr20_orig_get_json = _raw_http_mod.get_json  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                _raw_uni_mod._cr20_orig_meta = _raw_uni_mod.contract_metadata_all  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                from diveintocrypto_desktop.data import orderbook as _raw_ob_mod0
                _raw_ob_mod0._cr20_orig_get_json = getattr(_raw_ob_mod0, "get_json", None)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                from diveintocrypto_desktop.data import spot as _raw_spot_mod0
                _raw_spot_mod0._cr20_orig_get_json = getattr(_raw_spot_mod0, "get_json", None)  # type: ignore[attr-defined]
            except Exception:
                pass
            try:
                from diveintocrypto_desktop.shortlab.hedge import market as _raw_mkt_mod0
                _raw_mkt_mod0._cr20_orig_fx = getattr(_raw_mkt_mod0.ProductionHedgeMarket, "_fx", None)  # type: ignore[attr-defined]
            except Exception:
                pass
            _raw_fund_mod._cr20_orig_saved = True  # type: ignore[attr-defined]
        _raw_orig_get_json = getattr(_raw_http_mod, "_cr20_orig_get_json", _raw_http_mod.get_json)
        # Reinstall per app (fresh closure over current harness_state/clock).
        if True:
            async def _cr20_raw_premium_index(symbol: str, *, request_context: Any | None = None) -> dict[str, Any]:
                scen = _raw_scenario()
                if scen == "slow-provider":
                    await _raw_aio.sleep(SLOW_PROVIDER_REAL_DELAY_S)
                s = str(symbol).upper()
                now = _raw_now()
                px = _raw_price_for(s)
                rate = -0.0005 if scen == "negative-funding" else 0.0005
                return {
                    "mark_price": float(px),
                    "index_price": float(px),
                    "last_funding_rate": float(rate),
                    "next_funding_time": int(now + 8 * 3600 * 1000),
                    "time_ms": int(now),
                    "time": int(now),
                }

            async def _cr20_raw_hist(symbol: str, start_ms: int, end_ms: int, limit: int = 1000, *, request_context: Any | None = None) -> list[Any]:
                # Fresh DB stays honestly empty offline (no network backfill).
                return []

            async def _cr20_raw_get_json(url: str, params: Any | None = None, **kw: Any) -> Any:
                ustr = str(url)
                now = _raw_now()
                scen = _raw_scenario()
                if "fundingRate" in ustr:
                    return []
                if "premiumIndex" in ustr:
                    sym = str((params or {}).get("symbol") or "BTCUSDT").upper() if isinstance(params, dict) else "BTCUSDT"
                    px = _raw_price_for(sym)
                    rate = -0.0005 if scen == "negative-funding" else 0.0005
                    if params is None:
                        # premiumIndexAll: list shape
                        return [
                            {"symbol": "1000PEPEUSDT", "markPrice": "0.012", "indexPrice": "0.012", "lastFundingRate": str(rate), "nextFundingTime": now + 8 * 3600 * 1000, "time": now},
                            {"symbol": "BTCUSDT", "markPrice": "67000", "indexPrice": "67000", "lastFundingRate": str(rate), "nextFundingTime": now + 8 * 3600 * 1000, "time": now},
                        ]
                    return {"markPrice": str(px), "indexPrice": str(px), "lastFundingRate": str(rate), "nextFundingTime": now + 8 * 3600 * 1000, "time": now, "symbol": sym}
                if "exchangeInfo" in ustr:
                    onboard: int | None = None
                    if scen != "unknown-schedule":
                        onboard = now - 200 * 86_400_000
                    syms = [_raw_entry("1000PEPEUSDT", onboard), _raw_entry("BTCUSDT", onboard), _raw_entry("PEPEUSDT", onboard)]
                    # Verified REQUEST_WEIGHT contract required by budget.configure_host_limits.
                    return {"symbols": syms, "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000}]}
                if "bookTicker" in ustr:
                    sym = str((params or {}).get("symbol") or "").upper() if isinstance(params, dict) else ""
                    px = _raw_price_for(sym)
                    if "PEPE" in sym:
                        return {"symbol": sym, "bidPrice": "0.0119", "askPrice": "0.0121", "lastPrice": "0.012"}
                    if sym == "BTCUSDT":
                        return {"symbol": sym, "bidPrice": "66990", "askPrice": "67010", "lastPrice": "67000"}
                    return {"symbol": sym, "bidPrice": str(px * 0.999), "askPrice": str(px * 1.001), "lastPrice": str(px)}
                if "/depth" in ustr:
                    sym = str((params or {}).get("symbol") or "").upper() if isinstance(params, dict) else ""
                    if "PEPE" in sym:
                        return {"lastUpdateId": 1, "bids": [["0.0119", "1000000"], ["0.0118", "1000000"]], "asks": [["0.0121", "1000000"], ["0.0122", "1000000"]], "E": now, "T": now}
                    if sym == "BTCUSDT":
                        return {"lastUpdateId": 1, "bids": [["66990", "10"], ["66980", "10"]], "asks": [["67010", "10"], ["67020", "10"]], "E": now, "T": now}
                    px = _raw_price_for(sym or "BTCUSDT")
                    return {"lastUpdateId": 1, "bids": [[str(px * 0.999), "1000"]], "asks": [[str(px * 1.001), "1000"]], "E": now, "T": now}
                return await _raw_orig_get_json(url, params, **kw)

            def _cr20_raw_meta_all() -> dict[str, Any]:
                try:
                    from diveintocrypto_desktop.data.universe import ContractMetadata as _CM
                except Exception:
                    return {}
                scen_raw = str(harness_state.get("scenario") or "normal")
                now2 = _raw_now()
                if scen_raw == "unknown-schedule":
                    return {
                        "1000PEPEUSDT": _CM(symbol="1000PEPEUSDT", onboard_at_ms=None, first_seen_ms=now2, delivery_at_ms=None, status="TRADING", contract_type="PERPETUAL", observed_at_ms=now2, contract_multiplier=None, multiplier_source=None),
                        "BTCUSDT": _CM(symbol="BTCUSDT", onboard_at_ms=None, first_seen_ms=now2, delivery_at_ms=None, status="TRADING", contract_type="PERPETUAL", observed_at_ms=now2, contract_multiplier=None, multiplier_source=None),
                    }
                ob = now2 - 200 * 86_400_000
                return {
                    "1000PEPEUSDT": _CM(symbol="1000PEPEUSDT", onboard_at_ms=ob, first_seen_ms=ob, delivery_at_ms=None, status="TRADING", contract_type="PERPETUAL", observed_at_ms=now2, contract_multiplier=1000.0, multiplier_source="EXCHANGE"),
                    "BTCUSDT": _CM(symbol="BTCUSDT", onboard_at_ms=ob, first_seen_ms=ob, delivery_at_ms=None, status="TRADING", contract_type="PERPETUAL", observed_at_ms=now2, contract_multiplier=1.0, multiplier_source="EXCHANGE"),
                }

            _raw_fund_mod.premium_index = _cr20_raw_premium_index  # type: ignore[attr-defined]
            try:
                _raw_hist_orig = getattr(_raw_fund_mod, "_cr20_orig_hist", None)
                if _raw_hist_orig is not None:
                    _raw_fund_mod.funding_history_range = _cr20_raw_hist  # type: ignore[attr-defined]
            except Exception:
                pass
            _raw_http_mod.get_json = _cr20_raw_get_json  # type: ignore[attr-defined]
            _raw_uni_mod.contract_metadata_all = _cr20_raw_meta_all  # type: ignore[attr-defined]
            # CR20: stablecoin FX is always 1 (raw CoinGecko response stub).
            # Pre-populating once is not enough: FakeClock advances 10s per
            # scenario (60s+ across the matrix) expire the 60s FX cache, so
            # patch the market FX fetch to stay fresh on current clock.
            try:
                from diveintocrypto_desktop.shortlab.hedge import market as _raw_mkt_mod

                _orig_fx = getattr(_raw_mkt_mod.ProductionHedgeMarket, "_fx", None)
                if getattr(_raw_mkt_mod.ProductionHedgeMarket, "_cr20_fx_patched", None) is not True:
                    try:
                        _raw_mkt_mod.ProductionHedgeMarket._cr20_orig_fx = _orig_fx  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    _raw_mkt_mod.ProductionHedgeMarket._cr20_fx_patched = True  # type: ignore[attr-defined]

                async def _cr20_raw_fx(self: Any, currency: str, request_context: Any = None) -> str:
                    cur = str(currency).upper()
                    if cur in ("USD", "USDT", "USDC", "FDUSD"):
                        now = _raw_now()
                        try:
                            self._fx_cache[cur] = (now, "1")  # type: ignore[attr-defined]
                            self._fx_provenance[cur] = {"source_as_of_ms": now - 1_000, "known_at_ms": now, "currency": cur}  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        return "1"
                    orig = getattr(_raw_mkt_mod.ProductionHedgeMarket, "_cr20_orig_fx", None)
                    if callable(orig):
                        return await orig(self, currency)
                    raise RuntimeError("QUOTE_FX_UNAVAILABLE")

                _raw_mkt_mod.ProductionHedgeMarket._fx = _cr20_raw_fx  # type: ignore[attr-defined]
            except Exception:
                pass
            # CR20: direct `from http import get_json` bindings bypass
            # http.get_json patch; patch each consumer module as well so raw
            # stubs win offline (budget-family UNBUDGETED would otherwise fire
            # for spot bookTicker/depth via stale direct references).
            try:
                from diveintocrypto_desktop.data import orderbook as _raw_ob_mod
                from diveintocrypto_desktop.data import spot as _raw_spot_mod
                try:
                    _raw_ob_mod.get_json = _cr20_raw_get_json  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    _raw_spot_mod.get_json = _cr20_raw_get_json  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    _raw_fund_mod.get_json = _cr20_raw_get_json  # type: ignore[attr-defined]
                except Exception:
                    pass
            except Exception:
                pass
    except Exception:
        pass

    def shortlab_runtime_factory() -> Any:
        factory_calls.append(1)
        rt = ShortLabRuntime(
            data_dir=data_path,
            clock=clock_ms,
            repair_ports=bindings,
            allow_test_bindings=(bindings is not None),
            config=enabled_cfg,
        )
        _orig_start = rt.start

        async def _acceptance_start() -> Any:
            out = await _orig_start()
            try:
                svc = rt.service
            except Exception:
                return out
            # -- raw HTTP fixture injection (test process only) --------------
            # Identity overrides (static raw, not computed scores).
            try:
                if getattr(svc, "_identity_overrides", None) in (None, {}):
                    svc._identity_overrides = _acceptance_identity_overrides()
                else:
                    merged = dict(_acceptance_identity_overrides())
                    try:
                        merged.update(dict(getattr(svc, "_identity_overrides") or {}))
                    except Exception:
                        pass
                    svc._identity_overrides = merged
            except Exception:
                pass

            def _now() -> int:
                try:
                    return int(clock_ms())
                except Exception:
                    return 1791417600000

            # CR20: computed fakes removed. Raw HTTP stubs above (+FakeClock)
            # are the only replacements; ProductionHedgeMarket computes
            # mark/quote/funding/rules honestly from those raw responses.

            # CR20: never overwrite computed legs (mark/quote/funding/rules).
            # ProductionHedgeMarket stays wired (svc._hedge_*_fn remain the
            # real market.mark/quote/funding bound by ShortLabRuntime); only
            # the original HTTP responses + FakeClock are stubbed (see raw
            # patches installed before runtime creation below). Identity
            # overrides below are static verified mappings (raw config, not
            # computed scores) and FX cache is a raw HTTP cache entry.
            try:
                mkt = getattr(svc, "_hedge_market", None)
                if mkt is not None:
                    try:
                        _now_ms = _now()
                        try:
                            mkt._fx_cache["USDT"] = (_now_ms, "1")  # type: ignore[attr-defined]
                            mkt._fx_provenance["USDT"] = {  # type: ignore[attr-defined]
                                "source_as_of_ms": _now_ms - 1_000,
                                "known_at_ms": _now_ms,
                                "currency": "USDT",
                            }
                        except Exception:
                            pass
                    except Exception:
                        pass
            except Exception:
                pass
            return out

        rt.start = _acceptance_start  # type: ignore[method-assign]
        return rt

    sig = inspect.signature(shortlab_runtime_factory)
    assert len(sig.parameters) == 0, "harness factory must take no arguments"

    app = create_app(shortlab_runtime_factory=shortlab_runtime_factory)

    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse

    control = APIRouter(prefix="/test/harness", tags=["repair-harness"])

    @control.get("/state")
    async def _harness_state() -> Any:
        try:
            now_ms = int(clock_ms())
        except Exception:
            now_ms = -1
        provider = FakeHarnessRawProvider(harness_state["scenario"])
        return {
            "scenario": harness_state["scenario"],
            "canonicalScenario": harness_state["canonical_scenario"],
            "nowMs": now_ms,
            "origin": HARNESS_ORIGIN,
            "bindingsMode": harness_state["bindings_mode"],
            "fundingCase": provider.funding_case(),
            "slowProviderDelayMs": provider.slow_provider_delay_ms(),
            "planSequence": list(provider.plan_sequence()),
            "acceptance": ACCEPTANCE_MODE,
        }

    @control.post("/advance")
    async def _harness_advance(payload: dict[str, Any]) -> Any:
        try:
            ms = int((payload or {}).get("ms", 0))
        except Exception:
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        if ms < 0:
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        try:
            new_now = advance_harness_clock(clock_ms, ms)
        except TypeError:
            return JSONResponse({"error": "HARNESS_CLOCK_NOT_ADVANCEABLE"}, status_code=400)
        return {"nowMs": int(new_now), "advancedMs": int(ms)}

    @control.post("/scenario")
    async def _harness_switch(payload: dict[str, Any]) -> Any:
        name = str((payload or {}).get("scenario", ""))
        if name not in HARNESS_SCENARIOS:
            return JSONResponse({"error": "HARNESS_SCENARIO_UNKNOWN"}, status_code=422)
        harness_state["scenario"] = name
        harness_state["canonical_scenario"] = canonical_scenario(name)
        return {"scenario": name, "canonicalScenario": harness_state["canonical_scenario"]}

    @control.post("/seed_funding")
    async def _harness_seed_funding(payload: dict[str, Any]) -> Any:
        if not isinstance(payload, dict):
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        try:
            symbol = str(payload.get("symbol", "") or "").strip()
            rate_raw = payload.get("rate", None)
            days_raw = payload.get("days", None)
            interval_raw = payload.get("interval_hours", payload.get("intervalHours", None))
            if not symbol:
                return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
            if rate_raw is None or days_raw is None or interval_raw is None:
                return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
            rate_f = float(str(rate_raw))
            days_i = int(days_raw)
            interval_i = int(interval_raw)
        except (TypeError, ValueError):
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        try:
            _canon = str(harness_state.get("canonical_scenario") or harness_state.get("scenario") or "")
        except Exception:
            _canon = ""
        if _canon == "unknown-schedule":
            return {"symbol": symbol.upper(), "seeded": 0, "slots": 0, "reason": "UNKNOWN_SCHEDULE_NO_BROADCAST"}
        if _canon == "negative-funding" and float(rate_f) > 0:
            rate_f = -abs(float(rate_f))
        try:
            now_ms = int(clock_ms())
        except Exception:
            return JSONResponse({"error": "HARNESS_CLOCK_NOT_ADVANCEABLE"}, status_code=400)
        try:
            _rt = app.state.shortlab_runtime  # type: ignore[attr-defined]
        except Exception:
            _rt = None
        _repo: Any = None
        try:
            if _rt is not None:
                _repo = getattr(_rt, "_repository", None) or getattr(_rt, "repository", None)
                if _repo is None:
                    try:
                        _svc = getattr(_rt, "service", None)
                        if _svc is not None and not callable(_svc):
                            _repo = getattr(_svc, "_repository", None)
                        elif callable(_svc):
                            try:
                                _s2 = _svc()
                                _repo = getattr(_s2, "_repository", None)
                            except Exception:
                                pass
                    except Exception:
                        pass
                if _repo is None:
                    try:
                        _svc2 = _rt.service  # type: ignore[attr-defined]
                        _repo = getattr(_svc2, "_repository", None)
                    except Exception:
                        pass
        except Exception:
            _repo = None
        if _repo is None:
            return JSONResponse({"error": "HARNESS_STORE_UNAVAILABLE"}, status_code=503)
        try:
            out = await _do_seed_funding(_repo, symbol, float(rate_f), int(days_i), int(interval_i), int(now_ms))
        except ValueError:
            return JSONResponse({"error": "HARNESS_INPUT_INVALID"}, status_code=422)
        except RuntimeError as exc:
            return JSONResponse({"error": "HARNESS_SEED_FAILED", "detail": str(exc)[:200]}, status_code=500)
        except Exception as exc:
            return JSONResponse({"error": "HARNESS_SEED_FAILED", "detail": f"{type(exc).__name__}"[:200]}, status_code=500)
        # Honest cache invalidation for FakeClock jumps (real-time QuoteCache).
        try:
            _svc3 = None
            try:
                _svc3 = _rt.service  # type: ignore[attr-defined]
            except Exception:
                try:
                    _svc3 = getattr(_rt, "_service", None)
                except Exception:
                    pass
            _mkt = getattr(_svc3, "_hedge_market", None) if _svc3 is not None else None
            if _mkt is not None:
                try:
                    getattr(_mkt, "_marks", {}).clear()  # type: ignore
                except Exception:
                    pass
                try:
                    getattr(_mkt, "_funding_cache", {}).clear()  # type: ignore
                except Exception:
                    pass
                try:
                    _mkt._exchange_cache = None  # type: ignore[attr-defined]
                except Exception:
                    pass
                for _vn in ("spot", "alpha"):
                    try:
                        _v = getattr(_mkt, _vn, None)
                        if _v is not None and hasattr(_v, "reset_cache"):
                            _v.reset_cache()  # type: ignore
                        elif _v is not None and hasattr(_v, "_quotes"):
                            try:
                                _v._quotes.clear()  # type: ignore
                            except Exception:
                                pass
                    except Exception:
                        pass
        except Exception:
            pass
        return {"symbol": out.get("symbol"), "seeded": out.get("slots"), "slots": out.get("slots"), **out}

    @control.get("/debug_activation")
    async def _harness_debug_activation(symbol: str = "") -> Any:
        sym = str(symbol or "").upper().strip() or "BTCUSDT"
        try:
            _rt2 = app.state.shortlab_runtime  # type: ignore[attr-defined]
        except Exception:
            _rt2 = None
        _repo2: Any = None
        try:
            if _rt2 is not None:
                _repo2 = getattr(_rt2, "_repository", None) or getattr(_rt2, "repository", None)
                if _repo2 is None:
                    try:
                        _svc = getattr(_rt2, "service", None)
                        if _svc is not None and not callable(_svc):
                            _repo2 = getattr(_svc, "_repository", None)
                    except Exception:
                        pass
                if _repo2 is None:
                    try:
                        _svc2 = _rt2.service  # type: ignore[attr-defined]
                        _repo2 = getattr(_svc2, "_repository", None)
                    except Exception:
                        pass
        except Exception:
            _repo2 = None
        if _repo2 is None:
            return JSONResponse({"error": "HARNESS_STORE_UNAVAILABLE"}, status_code=503)
        try:
            now2 = int(clock_ms())
        except Exception:
            now2 = 0
        try:
            rows = await _repo2.list_market_observations(sym, "ACTIVATION_CHECK", 0, int(now2) + 1000, int(now2) + 1000)  # type: ignore[attr-defined]
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if not rows:
            return {"symbol": sym, "count": 0, "checks": {}}
        last = rows[-1]
        try:
            vj = last.get("value_json") or {}
            if isinstance(vj, str):
                import json as _jsd

                vj = _jsd.loads(vj)
        except Exception:
            vj = {}
        return {"symbol": sym, "count": len(rows), "checks": (vj.get("checks") if isinstance(vj, dict) else {}), "value": vj}

    app.include_router(control)

    try:
        _harness_paths = {"/test/harness/state", "/test/harness/advance", "/test/harness/scenario", "/test/harness/seed_funding", "/test/harness/debug_activation"}
        _harness_routes = [r for r in app.routes if str(getattr(r, "path", "")) in _harness_paths]
        if _harness_routes:
            _rest = [r for r in app.routes if r not in _harness_routes]
            _mounts: list[Any] = []
            _plain: list[Any] = []
            for r in _rest:
                _name = type(r).__name__
                if _name == "Mount" or "StaticFiles" in _name or str(getattr(r, "path", "")) == "/":
                    _mounts.append(r)
                else:
                    _plain.append(r)
            app.routes[:] = _plain + _harness_routes + _mounts  # type: ignore[attr-defined]
            try:
                app.router.routes[:] = app.routes  # type: ignore[attr-defined]
            except Exception:
                pass
    except Exception:
        pass

    try:
        app.state._repair_harness_state = harness_state  # type: ignore[attr-defined]
        app.state._repair_factory_calls = factory_calls  # type: ignore[attr-defined]
        app.state._repair_harness_clock = clock_ms  # type: ignore[attr-defined]
        app.state._repair_harness_scenario = scenario  # type: ignore[attr-defined]
    except Exception:
        pass
    return app

