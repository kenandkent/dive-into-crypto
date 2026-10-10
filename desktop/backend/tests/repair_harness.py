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
from typing import Any, Callable, Mapping

# ---------------------------------------------------------------------------
# Harness origin (frozen: 127.0.0.1:46409, harness-only, never production).
# ---------------------------------------------------------------------------

HARNESS_HOST = "127.0.0.1"
HARNESS_PORT = 46409
HARNESS_ORIGIN = f"http://{HARNESS_HOST}:{HARNESS_PORT}"


def _install_public_network_guard(state: dict[str, Any]) -> Callable[[], None]:
    """Deny public requests before DNS/socket send; keep loopback harness API.

    The production providers still receive ordinary connection failures and
    therefore report unavailable inputs. The harness substitutes no computed
    data and records every blocked host so acceptance can assert isolation.
    """
    import ipaddress as _ipaddress
    import socket as _socket
    import errno as _errno
    import aiohttp as _aiohttp
    from yarl import URL as _URL

    def _loopback_host(host: Any) -> bool:
        if not isinstance(host, str) or not host:
            return False
        value = host.rstrip(".").lower()
        if value == "localhost" or value.endswith(".localhost"):
            return True
        try:
            return _ipaddress.ip_address(value).is_loopback
        except ValueError:
            return False

    def _record(host: Any, method: str = "GET") -> None:
        try:
            attempts = state.setdefault("blocked_public_requests", [])
            if isinstance(attempts, list):
                attempts.append({"host": str(host or ""), "method": str(method).upper()})
        except Exception:
            pass

    session_cls = _aiohttp.ClientSession
    old_request = getattr(session_cls, "_repair_harness_original_request", None)
    if old_request is None:
        old_request = session_cls._request
        session_cls._repair_harness_original_request = old_request
    old_getaddrinfo = getattr(_socket, "_repair_harness_original_getaddrinfo", None)
    if old_getaddrinfo is None:
        old_getaddrinfo = _socket.getaddrinfo
        _socket._repair_harness_original_getaddrinfo = old_getaddrinfo
    old_connect = getattr(_socket.socket, "_repair_harness_original_connect", None)
    if old_connect is None:
        old_connect = _socket.socket.connect
        _socket.socket._repair_harness_original_connect = old_connect
    old_connect_ex = getattr(_socket.socket, "_repair_harness_original_connect_ex", None)
    if old_connect_ex is None:
        old_connect_ex = _socket.socket.connect_ex
        _socket.socket._repair_harness_original_connect_ex = old_connect_ex

    def _guard_socket_address(address: Any) -> bool:
        # Unix-domain sockets and loopback are local harness traffic. Blocking
        # connect itself also covers clients using literal public IPs, which
        # do not pass through getaddrinfo.
        if not isinstance(address, tuple) or not address:
            return False
        host = address[0]
        if _loopback_host(host):
            return False
        _record(host, "SOCKET")
        return True

    async def _guard_request(self: Any, method: str, str_or_url: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            url = str_or_url if isinstance(str_or_url, _URL) else _URL(str(str_or_url))
            if not url.scheme and getattr(self, "_base_url", None) is not None:
                url = self._base_url.join(url)
            host = url.host
        except Exception:
            host = None
        if _loopback_host(host):
            return await old_request(self, method, str_or_url, *args, **kwargs)
        _record(host, method)
        raise _aiohttp.ClientConnectionError("HARNESS_PUBLIC_NETWORK_DISABLED")

    def _guard_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if _loopback_host(host):
            return old_getaddrinfo(host, *args, **kwargs)
        _record(host, "SOCKET")
        raise _socket.gaierror("HARNESS_PUBLIC_NETWORK_DISABLED")

    def _guard_connect(sock: Any, address: Any) -> Any:
        if _guard_socket_address(address):
            raise OSError("HARNESS_PUBLIC_NETWORK_DISABLED")
        return old_connect(sock, address)

    def _guard_connect_ex(sock: Any, address: Any) -> int:
        if _guard_socket_address(address):
            return int(_errno.EACCES)
        return int(old_connect_ex(sock, address))

    session_cls._request = _guard_request  # type: ignore[method-assign]
    _socket.getaddrinfo = _guard_getaddrinfo  # type: ignore[assignment]
    _socket.socket.connect = _guard_connect  # type: ignore[method-assign]
    _socket.socket.connect_ex = _guard_connect_ex  # type: ignore[method-assign]

    def _restore() -> None:
        if getattr(session_cls, "_request", None) is _guard_request:
            session_cls._request = old_request  # type: ignore[method-assign]
        if getattr(_socket, "getaddrinfo", None) is _guard_getaddrinfo:
            _socket.getaddrinfo = old_getaddrinfo  # type: ignore[assignment]
        if getattr(_socket.socket, "connect", None) is _guard_connect:
            _socket.socket.connect = old_connect  # type: ignore[method-assign]
        if getattr(_socket.socket, "connect_ex", None) is _guard_connect_ex:
            _socket.socket.connect_ex = old_connect_ex  # type: ignore[method-assign]

    return _restore


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
    mappings (raw config, not scores); stablecoin FX is fetched by the real
    CoinGecko provider through the raw-response transport below.
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
        "blocked_public_requests": [],
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

    # Define an app-scoped fixture transport. It is installed only when this
    # app's lifespan starts and restored at shutdown; constructing an app has
    # no effect on shared HTTP globals.
    try:
        import asyncio as _raw_aio
        from diveintocrypto_desktop.data import http as _raw_http_mod
        if True:
            def _cr20_raw_get_json(url: str, params: Any | None = None) -> Any:
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
                if "/ticker/24hr" in ustr:
                    return [
                        {"symbol": "1000PEPEUSDT", "lastPrice": "0.012", "priceChangePercent": "1.5", "quoteVolume": "100000000", "closeTime": now},
                        {"symbol": "BTCUSDT", "lastPrice": "67000", "priceChangePercent": "0.26", "quoteVolume": "1000000000", "closeTime": now},
                    ]
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
                if "/api/v3/coins/" in ustr:
                    coin = ustr.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
                    coin_symbols = {"tether": "usdt", "usd-coin": "usdc", "first-digital-usd": "fdusd"}
                    if coin in coin_symbols:
                        import datetime as _dt
                        as_of = _dt.datetime.fromtimestamp(now / 1000, tz=_dt.timezone.utc).isoformat()
                        return {
                            "id": coin, "symbol": coin_symbols[coin], "name": coin,
                            "market_data": {
                                "current_price": {"usd": "1"},
                                "market_cap": {"usd": "1"},
                                "fully_diluted_valuation": {"usd": "1"},
                                "circulating_supply": 1, "total_supply": 1,
                                "max_supply": None, "ath": {"usd": "1"},
                                "ath_change_percentage": {"usd": 0},
                                "ath_date": {"usd": as_of},
                                "last_updated": as_of,
                            },
                            "categories": [],
                        }
                # Acceptance stays raw-fixture-only. Unknown endpoints retain
                # the provider's honest unavailable path instead of falling
                # through to a public service.
                raise RuntimeError(f"HARNESS_RAW_HTTP_FIXTURE_UNAVAILABLE:{ustr[:160]}")

            class _FixtureResponse:
                status = 200
                headers: dict[str, str] = {}

                def __init__(self, payload: Any, url: str) -> None:
                    self._payload = payload
                    self._url = str(url)

                async def __aenter__(self) -> Any:
                    harness_state["raw_http_sends"] = int(harness_state.get("raw_http_sends", 0)) + 1
                    if _raw_scenario() == "slow-provider" and "premiumIndex" in self._url:
                        await _raw_aio.sleep(SLOW_PROVIDER_REAL_DELAY_S)
                    return self

                async def __aexit__(self, *exc: Any) -> bool:
                    return False

                def raise_for_status(self) -> None:
                    return None

                async def json(self) -> Any:
                    return self._payload

            class _FixtureSession:
                closed = False

                def get(self, url: Any, params: Any = None, **_kwargs: Any) -> Any:
                    try:
                        payload = _cr20_raw_get_json(str(url), params)
                    except RuntimeError:
                        try:
                            from urllib.parse import urlsplit as _urlsplit
                            host = _urlsplit(str(url)).hostname or ""
                            attempts = harness_state.setdefault("blocked_public_requests", [])
                            if isinstance(attempts, list):
                                attempts.append({"host": host, "method": "GET"})
                        except Exception:
                            pass
                        raise
                    return _FixtureResponse(payload, str(url))

                async def close(self) -> None:
                    self.closed = True

            async def _cr20_fixture_get_session() -> Any:
                return _FixtureSession()

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
        _orig_stop = rt.stop
        _network_restore: Callable[[], None] | None = None
        _raw_restore: Callable[[], None] | None = None

        async def _acceptance_start() -> Any:
            nonlocal _network_restore, _raw_restore
            _network_restore = _install_public_network_guard(harness_state)
            _prior_get_session = _raw_http_mod.get_session
            _raw_http_mod.get_session = _cr20_fixture_get_session  # type: ignore[attr-defined]

            def _restore_transport() -> None:
                if _raw_http_mod.get_session is _cr20_fixture_get_session:
                    _raw_http_mod.get_session = _prior_get_session  # type: ignore[attr-defined]

            _raw_restore = _restore_transport
            try:
                out = await _orig_start()
            except Exception:
                if _network_restore is not None:
                    _network_restore()
                    _network_restore = None
                if _raw_restore is not None:
                    _raw_restore()
                    _raw_restore = None
                raise
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

            # Raw HTTP response fixtures run below data.http.get_json, so its
            # endpoint budget, retry and accounting logic remains production.

            return out

        async def _acceptance_stop() -> Any:
            nonlocal _network_restore, _raw_restore
            try:
                return await _orig_stop()
            finally:
                if _network_restore is not None:
                    _network_restore()
                    _network_restore = None
                if _raw_restore is not None:
                    _raw_restore()
                    _raw_restore = None

        rt.start = _acceptance_start  # type: ignore[method-assign]
        rt.stop = _acceptance_stop  # type: ignore[method-assign]
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
        runtime = getattr(app.state, "shortlab_runtime", None)
        budget = getattr(runtime, "_request_budget", None)
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
            "blockedPublicRequestCount": len(harness_state.get("blocked_public_requests", ())),
            "blockedPublicHosts": sorted({
                str(item.get("host") or "")
                for item in harness_state.get("blocked_public_requests", ())
                if isinstance(item, Mapping)
            }),
            "rawHttpSendCount": int(harness_state.get("raw_http_sends", 0)),
            "actualBudgetSendCount": int(getattr(budget, "sent_attempts", 0) or 0),
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

