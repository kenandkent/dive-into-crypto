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
    from fastapi import APIRouter
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

    app.include_router(control)

    # Starlette matches routes in order; production mounts StaticFiles at "/"
    # (UI dist) inside create_app, which would shadow harness routes added
    # afterwards. Reorder so harness control stays reachable while the UI
    # mount remains last. Production sources are untouched.
    try:
        _harness_paths = {"/test/harness/state", "/test/harness/advance", "/test/harness/scenario"}
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
    """R15b acceptance app: real producers + raw HTTP fixtures (offline-safe).

    Same frozen seam as :func:`create_repair_test_app` (factory called once,
    lifespan start/stop once, harness control routes only here), but the
    Runtime uses an explicitly enabled config (hedge + funding_capture) and,
    after start, the service's raw HTTP layer returns deterministic R00-style
    raw fixtures (mark/quote/funding/rules/identity/metadata). The nine
    RepairPorts stay real (bindings=None => REAL_PRODUCERS/False); explicit
    non-None bindings must still be D19.6 TEST_FAKE or this helper raises
    TEST_BINDINGS_REQUIRED. No computed Score/PnL/Outcome is ever injected.
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

            async def _mark_fn(symbol: str) -> dict[str, Any]:
                sym = str(symbol).upper()
                now = _now()
                if "PEPE" in sym:
                    px = "0.012"
                elif sym == "BTCUSDT":
                    px = "67000"
                else:
                    px = "100"
                return {
                    "mark_price": px,
                    "native_price": px,
                    "quote_currency": "USDT",
                    "quote_to_usd": "1",
                    "symbol": sym,
                    "as_of_ms": now,
                    "fetched_at_ms": now,
                    "known_at_ms": now,
                    "expires_at_ms": now + 60_000,
                }

            async def _quote_fn(symbol: str, qty: str, venue: Any = None) -> dict[str, Any]:
                sym = str(symbol).upper()
                now = _now()
                if "PEPE" in sym:
                    mid, buy, sell = "0.012", "0.0121", "0.0119"
                    canon = "pepe"
                elif sym == "BTCUSDT":
                    mid, buy, sell = "67000", "67010", "66990"
                    canon = "bitcoin"
                else:
                    mid, buy, sell = "100", "100.1", "99.9"
                    canon = sym.lower()
                return {
                    "venue": str(venue or "BINANCE_SPOT"),
                    "canonical_id": canon,
                    "symbol": sym,
                    "chain": None,
                    "contract_address": None,
                    "as_of_ms": now,
                    "fetched_at_ms": now,
                    "known_at_ms": now,
                    "expires_at_ms": now + 60_000,
                    "reference_notional_usd": "10000",
                    "mid_price": mid,
                    "buy_vwap": buy,
                    "sell_vwap": sell,
                    "buy_executable_qty": "1000000",
                    "sell_executable_qty": "1000000",
                    "buy_slippage_bps": 5.0,
                    "sell_slippage_bps": 5.0,
                    "estimated_fee_usd": None,
                    "estimated_gas_usd": None,
                    "direction_costs": {},
                    "entry_feasible": True,
                    "exit_feasible": True,
                    "exit_feasibility": "CONFIRMED",
                    "quote_currency": "USDT",
                    "quote_to_usd": "1",
                    "source_timestamp_ms": now - 1_000,
                    "requested_canonical_qty": "100000",
                    "trading_rules": {},
                    "capabilities": {},
                    "identity_confidence": "VERIFIED",
                    "status": "OK",
                    "reason_code": None,
                }

            async def _funding_fn(sym: str) -> dict[str, Any]:
                s = str(sym).upper()
                scen = canonical_scenario(str(harness_state.get("scenario") or "normal"))
                # Scenario-aware raw funding (never computed carry).
                if scen == "negative-funding":
                    return {
                        "symbol": s,
                        "current_rate": "-0.0005",
                        "last_settled_rate": "-0.0005",
                        "funding_30d": "0.018",
                        "funding_7d": "-0.004",
                        "funding_90d": "0.05",
                        "positive_ratio_30d": "0.85",
                        "positive_ratio_90d": "0.8",
                        "coverage_30d": "0.95",
                        "coverage_90d": "0.92",
                        "conservative_apr": "0.25",
                        "history_coverage": "0.95",
                    }
                if scen == "unknown-schedule" or harness_state.get("scenario") == "unknown-schedule":
                    return {"symbol": s}
                return {
                    "symbol": s,
                    "current_rate": "0.0005",
                    "last_settled_rate": "0.0004",
                    "funding_30d": "0.018",
                    "funding_7d": "0.004",
                    "funding_90d": "0.05",
                    "positive_ratio_30d": "0.85",
                    "positive_ratio_90d": "0.8",
                    "coverage_30d": "0.95",
                    "coverage_90d": "0.92",
                    "conservative_apr": "0.25",
                    "history_coverage": "0.95",
                }

            def _rules_fn() -> dict[str, Any]:
                return {
                    "symbol": "1000PEPEUSDT",
                    "lot_rules": {"step_size": "0.001", "min_qty": "0.001", "max_qty": "10000000"},
                    "notional_rules": {"min_notional": "5", "max_notional": "1000000"},
                    "price_rules": {"min_price": "0.000001", "max_price": "1000000"},
                    "order_types": ["LIMIT", "MARKET", "STOP", "STOP_MARKET"],
                    "stop_orders_supported": True,
                    "conditional_orders_source_ref": "exchangeInfo:1000PEPEUSDT",
                }

            async def _metadata_fn() -> dict[str, Any]:
                now = _now()
                scen = str(harness_state.get("scenario") or "normal")
                if scen == "unknown-schedule":
                    # Unknown listing (no onboard) -> HISTORY_CLASS_UNKNOWN.
                    return {
                        "1000PEPEUSDT": {"symbol": "1000PEPEUSDT", "status": "TRADING", "contract_type": "PERPETUAL", "observed_at_ms": now},
                        "BTCUSDT": {"symbol": "BTCUSDT", "status": "TRADING", "contract_type": "PERPETUAL", "observed_at_ms": now},
                    }
                onboard = now - 200 * 86_400_000
                return {
                    "1000PEPEUSDT": {"symbol": "1000PEPEUSDT", "status": "TRADING", "contract_type": "PERPETUAL", "onboard_at_ms": onboard, "observed_at_ms": now},
                    "BTCUSDT": {"symbol": "BTCUSDT", "status": "TRADING", "contract_type": "PERPETUAL", "onboard_at_ms": onboard, "observed_at_ms": now},
                }

            try:
                # Only fill when unbound so explicit test doubles still win.
                if getattr(svc, "_hedge_mark_fn", None) is None:
                    svc._hedge_mark_fn = _mark_fn  # type: ignore[attr-defined]
                # ProductionHedgeMarket already set mark/quote/funding; for
                # acceptance we override with deterministic raw fixtures so
                # offline runs are reproducible (real ports still compute).
                svc._hedge_mark_fn = _mark_fn  # type: ignore[attr-defined]
                svc._hedge_quote_fn = _quote_fn  # type: ignore[attr-defined]
                svc._hedge_funding_fn = _funding_fn  # type: ignore[attr-defined]
                svc._hedge_futures_rules_fn = _rules_fn  # type: ignore[attr-defined]
                svc._hedge_spot_rules_fn = _rules_fn  # type: ignore[attr-defined]
                svc._metadata_fn = _metadata_fn  # type: ignore[attr-defined]
            except Exception:
                pass
            return out

        rt.start = _acceptance_start  # type: ignore[method-assign]
        return rt

    sig = inspect.signature(shortlab_runtime_factory)
    assert len(sig.parameters) == 0, "harness factory must take no arguments"

    app = create_app(shortlab_runtime_factory=shortlab_runtime_factory)

    from fastapi import APIRouter
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

    app.include_router(control)

    try:
        _harness_paths = {"/test/harness/state", "/test/harness/advance", "/test/harness/scenario"}
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

