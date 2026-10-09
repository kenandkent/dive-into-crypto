"""R15a harness self-check (D16/D19.1): clock / scenario / port isolation.

Covers the R15a skeleton only (real acceptance belongs to R15b):

- FakeClock advance / non-advanceable rejection.
- Scenario switching incl. plan-switchakit alias; R00 raw fixtures per
  scenario (negative-funding / unknown-schedule matrix, slow 8s marker,
  plan-switch sequence). No computed scores are injected.
- Port isolation: bindings=None is REAL_PRODUCERS/False; make_ports() fakes
  are TEST_FAKE/True; real-producer callbacks are rejected with
  TEST_BINDINGS_REQUIRED.
- Frozen factory seam: create_app(shortlab_runtime_factory=closure) builds
  a real ShortLabRuntime; factory called once, start/stop each once, no HTTP
  request triggers re-construction.
- Control origin is exactly http://127.0.0.1:46409; isolated backend
  process is reached only via stdlib urllib (never patched httpx).
- Production sources contain no /test/harness control routes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _import_harness():
    try:
        from tests.repair_harness import (  # type: ignore[import-not-found]
            CANONICAL_SCENARIOS,
            HARNESS_ORIGIN,
            HARNESS_PORT,
            HARNESS_HOST,
            HARNESS_SCENARIOS,
            PLAN_SWITCH_SEQUENCE,
            SLOW_PROVIDER_DELAY_MS,
            FakeClock,
            FakeHarnessRawProvider,
            advance_harness_clock,
            canonical_scenario,
            create_repair_test_app,
            get_raw_provider_payload,
            make_scenario_funding_context,
        )

        return {
            "CANONICAL_SCENARIOS": CANONICAL_SCENARIOS,
            "HARNESS_ORIGIN": HARNESS_ORIGIN,
            "HARNESS_PORT": HARNESS_PORT,
            "HARNESS_HOST": HARNESS_HOST,
            "HARNESS_SCENARIOS": HARNESS_SCENARIOS,
            "PLAN_SWITCH_SEQUENCE": PLAN_SWITCH_SEQUENCE,
            "SLOW_PROVIDER_DELAY_MS": SLOW_PROVIDER_DELAY_MS,
            "FakeClock": FakeClock,
            "FakeHarnessRawProvider": FakeHarnessRawProvider,
            "advance_harness_clock": advance_harness_clock,
            "canonical_scenario": canonical_scenario,
            "create_repair_test_app": create_repair_test_app,
            "get_raw_provider_payload": get_raw_provider_payload,
            "make_scenario_funding_context": make_scenario_funding_context,
        }
    except Exception:
        from repair_harness import (  # type: ignore[import-not-found]
            CANONICAL_SCENARIOS,
            HARNESS_ORIGIN,
            HARNESS_PORT,
            HARNESS_HOST,
            HARNESS_SCENARIOS,
            PLAN_SWITCH_SEQUENCE,
            SLOW_PROVIDER_DELAY_MS,
            FakeClock,
            FakeHarnessRawProvider,
            advance_harness_clock,
            canonical_scenario,
            create_repair_test_app,
            get_raw_provider_payload,
            make_scenario_funding_context,
        )

        return {
            "CANONICAL_SCENARIOS": CANONICAL_SCENARIOS,
            "HARNESS_ORIGIN": HARNESS_ORIGIN,
            "HARNESS_PORT": HARNESS_PORT,
            "HARNESS_HOST": HARNESS_HOST,
            "HARNESS_SCENARIOS": HARNESS_SCENARIOS,
            "PLAN_SWITCH_SEQUENCE": PLAN_SWITCH_SEQUENCE,
            "SLOW_PROVIDER_DELAY_MS": SLOW_PROVIDER_DELAY_MS,
            "FakeClock": FakeClock,
            "FakeHarnessRawProvider": FakeHarnessRawProvider,
            "advance_harness_clock": advance_harness_clock,
            "canonical_scenario": canonical_scenario,
            "create_repair_test_app": create_repair_test_app,
            "get_raw_provider_payload": get_raw_provider_payload,
            "make_scenario_funding_context": make_scenario_funding_context,
        }


def test_harness_origin_is_frozen() -> None:
    h = _import_harness()
    assert h["HARNESS_HOST"] == "127.0.0.1"
    assert h["HARNESS_PORT"] == 46409
    assert h["HARNESS_ORIGIN"] == "http://127.0.0.1:46409"


def test_harness_scenarios_frozen() -> None:
    h = _import_harness()
    for name in ("normal", "negative-funding", "unknown-schedule", "slow-provider", "plan-switch", "plan-switchakit"):
        assert name in h["HARNESS_SCENARIOS"], f"missing scenario {name!r}"
    assert h["canonical_scenario"]("plan-switchakit") == "plan-switch"
    assert h["canonical_scenario"]("normal") == "normal"


def test_fake_clock_advance() -> None:
    h = _import_harness()
    FakeClock = h["FakeClock"]
    clock = FakeClock(start_ms=1791417600000)
    assert clock() == 1791417600000
    assert h["advance_harness_clock"](clock, 10000) == 1791417610000
    assert clock() == 1791417610000
    clock.advance(500)
    assert clock.now() == 1791417610500
    with pytest.raises(ValueError):
        clock.advance(-1)
    with pytest.raises(TypeError):
        h["advance_harness_clock"](lambda: 1, 10)


def test_fake_provider_returns_r00_raw_fixtures() -> None:
    h = _import_harness()
    # normal -> valid positive funding; negative -> negative rates.
    normal = h["make_scenario_funding_context"]("normal")
    assert str(normal.metrics.current_rate) == "0.0005"
    negative = h["make_scenario_funding_context"]("negative-funding")
    assert str(negative.metrics.current_rate).startswith("-")
    unknown = h["make_scenario_funding_context"]("unknown-schedule")
    assert unknown.history_class == "HISTORY_CLASS_UNKNOWN"
    # Raw payload carries R00 fixtures, never computed scores.
    payload = h["get_raw_provider_payload"]("normal")
    assert payload["scenario"] == "normal"
    assert payload["funding_case"] == "valid"
    assert "decision_request" in payload and "events" in payload
    assert "market_context" in payload and "funding_context" in payload
    text = json.dumps(str(payload))
    assert "net_carry" not in text.lower() or "net_carry_usd" not in payload
    # Slow-provider marker and plan-switch sequence.
    slow = h["FakeHarnessRawProvider"]("slow-provider")
    assert slow.slow_provider_delay_ms() == h["SLOW_PROVIDER_DELAY_MS"] == 8000
    assert h["FakeHarnessRawProvider"]("normal").slow_provider_delay_ms() == 0
    assert tuple(h["FakeHarnessRawProvider"]("plan-switch").plan_sequence()) == tuple(h["PLAN_SWITCH_SEQUENCE"])
    assert tuple(h["FakeHarnessRawProvider"]("plan-switchakit").plan_sequence()) == tuple(h["PLAN_SWITCH_SEQUENCE"])
    assert h["FakeHarnessRawProvider"]("normal").plan_sequence() == ()
    with pytest.raises(ValueError):
        h["FakeHarnessRawProvider"]("nope")
    with pytest.raises(ValueError):
        h["get_raw_provider_payload"]("nope")


def test_create_repair_test_app_uses_frozen_factory_seam(tmp_path: Path) -> None:
    h = _import_harness()
    from diveintocrypto_desktop.shortlab import runtime as runtime_mod

    FakeClock = h["FakeClock"]
    clock = FakeClock()
    data_dir = tmp_path / "harness-seam"
    app = h["create_repair_test_app"](data_dir, "normal", clock)
    # Factory is called synchronously inside create_app (frozen seam);
    # lifespan owns start/stop exactly once, never re-constructs per HTTP.
    calls = app.state._repair_factory_calls
    assert len(calls) == 1, "factory must be called exactly once during create_app"
    assert app.state._repair_harness_state["bindings_mode"] == "REAL_PRODUCERS"
    assert app.state._repair_harness_state["origin"] == "http://127.0.0.1:46409"

    orig_start = runtime_mod.ShortLabRuntime.start
    orig_stop = runtime_mod.ShortLabRuntime.stop
    started: list[int] = []
    stopped: list[int] = []

    async def _count_start(self: Any) -> Any:
        started.append(1)
        return await orig_start(self)

    async def _count_stop(self: Any) -> Any:
        stopped.append(1)
        return await orig_stop(self)

    runtime_mod.ShortLabRuntime.start = _count_start  # type: ignore[method-assign]
    runtime_mod.ShortLabRuntime.stop = _count_stop  # type: ignore[method-assign]
    try:
        with TestClient(app) as client:
            assert len(calls) == 1, "lifespan must not re-construct the runtime"
            assert len(started) == 1, "Runtime.start must run exactly once"
            # HTTP traffic must not re-construct the runtime.
            r = client.get("/test/harness/state")
            assert r.status_code == 200
            body = r.json()
            assert body["origin"] == "http://127.0.0.1:46409"
            assert body["scenario"] == "normal"
            assert body["bindingsMode"] == "REAL_PRODUCERS"
            r2 = client.get("/api/short/capabilities")
            assert r2.status_code in (200, 503)
            assert len(calls) == 1, "no HTTP request may trigger factory re-construction"
            # Clock control route is harness-only and advances the same clock.
            before = int(clock())
            adv = client.post("/test/harness/advance", json={"ms": 10000})
            assert adv.status_code == 200
            assert adv.json()["nowMs"] == before + 10000
            assert int(clock()) == before + 10000
            # Scenario switch route.
            sw = client.post("/test/harness/scenario", json={"scenario": "negative-funding"})
            assert sw.status_code == 200
            assert sw.json()["scenario"] == "negative-funding"
            st = client.get("/test/harness/state")
            assert st.json()["scenario"] == "negative-funding"
            assert st.json()["fundingCase"] == "negative"
            bad = client.post("/test/harness/scenario", json={"scenario": "nope"})
            assert bad.status_code == 422
            # Alias scenario is accepted and canonicalized.
            sw2 = client.post("/test/harness/scenario", json={"scenario": "plan-switchakit"})
            assert sw2.status_code == 200
            assert sw2.json()["canonicalScenario"] == "plan-switch"
        assert len(calls) == 1
        assert len(stopped) == 1, "Runtime.stop must run exactly once"
    finally:
        runtime_mod.ShortLabRuntime.start = orig_start  # type: ignore[method-assign]
        runtime_mod.ShortLabRuntime.stop = orig_stop  # type: ignore[method-assign]


def test_test_fake_bindings_gate(tmp_path: Path) -> None:
    h = _import_harness()
    FakeClock = h["FakeClock"]
    from diveintocrypto_desktop.shortlab.repair_ports import RepairPorts

    clock = FakeClock()
    # Explicit TEST_FAKE bindings are accepted with allow True.
    from tests.repair_fixtures import make_ports  # type: ignore[import-not-found]

    fakes = make_ports()
    app = h["create_repair_test_app"](tmp_path / "fake-ok", "normal", clock, bindings=fakes)
    assert app.state._repair_harness_state["bindings_mode"] == "TEST_FAKE"
    with TestClient(app) as client:
        state = client.get("/test/harness/state").json()
        assert state["bindingsMode"] == "TEST_FAKE"

    # Real-producer callbacks (not from repair_fixtures) are rejected.
    def real_compute_schedule_coverage(*args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("must not be called")

    real_ports = RepairPorts(compute_schedule_coverage=real_compute_schedule_coverage)
    with pytest.raises(RuntimeError, match="TEST_BINDINGS_REQUIRED"):
        h["create_repair_test_app"](tmp_path / "fake-bad", "normal", FakeClock(), bindings=real_ports)

    # Unknown scenario / bad clock are rejected without constructing anything.
    with pytest.raises(ValueError, match="unknown harness scenario"):
        h["create_repair_test_app"](tmp_path / "bad", "nope", FakeClock())
    with pytest.raises(TypeError):
        h["create_repair_test_app"](tmp_path / "bad", "normal", 12345)  # type: ignore[arg-type]


def test_harness_control_routes_absent_from_production() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "diveintocrypto_desktop"
    for rel in ("api/app.py", "api/shortlab.py", "api/shortlab_repair.py", "shortlab/runtime.py", "shortlab/service.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "/test/harness" not in text, f"production file {rel} must not expose harness control"


def test_repair_server_isolation_contract() -> None:
    path = Path(__file__).resolve().parent / "repair_server.py"
    text = path.read_text(encoding="utf-8")
    # Frozen address + stub + isolation documentation markers.
    assert "127.0.0.1" in text
    assert "46409" in text
    assert "http://127.0.0.1:46409" in text
    assert "Stub" in text or "Stb" in text
    assert "conftest" in text
    assert "crypcodile" in text and "aiolimiter" in text and "httpx" in text
    assert "urllib" in text
    assert "license" in text.lower()
    assert "R15b" in text
    # The isolated server must never import the patched transports.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        assert "import httpx" not in stripped, "repair_server must not import httpx"
        assert "import websockets" not in stripped, "repair_server must not import websockets"
        assert "import crypcodile" not in stripped, "repair_server must not import crypcodile"
        assert "import aiolimiter" not in stripped, "repair_server must not import aiolimiter"
        assert "from tests.conftest" not in stripped and "import tests.conftest" not in stripped


def _urllib_get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - loopback harness
        return json.loads(resp.read().decode("utf-8"))


def _urllib_post_json(url: str, payload: dict, timeout: float = 5.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")  # noqa: S310 - loopback harness
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback harness
        return json.loads(resp.read().decode("utf-8"))


def test_isolated_server_process_port_and_urllib(tmp_path: Path) -> None:
    """Spawn repair_server.py in a fresh process; talk only via urllib."""
    backend_dir = Path(__file__).resolve().parents[1]
    server_script = backend_dir / "tests" / "repair_server.py"
    assert server_script.exists()
    data_dir = tmp_path / "isolated-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    src_dir = backend_dir / "src"
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(src_dir) + (os_sep() + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(var, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    proc = subprocess.Popen(
        [sys.executable, str(server_script), "--scenario", "normal", "--data-dir", str(data_dir)],
        cwd=str(backend_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        origin = "http://127.0.0.1:46409"
        deadline = time.monotonic() + 25.0
        state: dict | None = None
        last_error: str | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out = ""
                try:
                    out = proc.communicate(timeout=2)[0] or ""
                except Exception:
                    out = ""
                pytest.fail(f"harness server exited early code={proc.returncode}: {out[-2000:]}")
            try:
                state = _urllib_get_json(origin + "/test/harness/state", timeout=3.0)
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:200]
                time.sleep(0.3)
        assert state is not None, f"isolated server never became ready: {last_error}"
        # Port isolation: origin is exactly the frozen harness origin.
        assert state["origin"] == "http://127.0.0.1:46409"
        assert state["scenario"] == "normal"
        assert state["bindingsMode"] == "REAL_PRODUCERS"
        # Clock advance over urllib (harness control only).
        before = int(state["nowMs"])
        adv = _urllib_post_json(origin + "/test/harness/advance", {"ms": 10000})
        assert adv["nowMs"] == before + 10000
        state2 = _urllib_get_json(origin + "/test/harness/state")
        assert int(state2["nowMs"]) == before + 10000
        # Scenario switch over urllib.
        sw = _urllib_post_json(origin + "/test/harness/scenario", {"scenario": "unknown-schedule"})
        assert sw["scenario"] == "unknown-schedule"
        state3 = _urllib_get_json(origin + "/test/harness/state")
        assert state3["scenario"] == "unknown-schedule"
        assert state3["fundingCase"] == "unknown"
    finally:
        # No leftover process: terminate, then kill on stubborn exit.
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass


def os_sep() -> str:
    import os as _os

    return _os.pathsep
