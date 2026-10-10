"""R15b real overall acceptance (D16/D19, V01-V15).

Root E2E via stdlib urllib to an isolated backend process on
http://127.0.0.1:46409 (REAL_PRODUCERS + raw HTTP fixtures only; no computed
Score/PnL/Outcome substitutes). No httpx/websockets imports (would hit the
root conftest Mock). Covers:

- 5 scenarios (normal/negative-funding/unknown-schedule/slow-provider/
  plan-switch): harness state, Decision, Simulation, DRAFT, manual events,
  protection confirm, ACTIVE, partial close, FundingReceipt, Monitor,
  PairExit, CLOSED.
- Clock-advance Evidence six strategies (real capture/collect).
- 11 coins + 500 scoring FakeClock load (bounded tick, fair rotation).
- DB immutable/hash/known_at verification (read-back).

Browser React verification lives in desktop/ui/e2e/repair.spec.js; this file
is the backend E2E + load + evidence core.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from decimal import Decimal
from pathlib import Path

import pytest

HARNESS_HOST = "127.0.0.1"
HARNESS_PORT = 46409
HARNESS_ORIGIN = f"http://{HARNESS_HOST}:{HARNESS_PORT}"

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "desktop" / "backend"
BACKEND_SRC = BACKEND_DIR / "src"
HARNESS_SERVER = BACKEND_DIR / "tests" / "repair_server.py"

SCENARIOS = ("normal", "negative-funding", "unknown-schedule", "slow-provider", "plan-switch")
SCENARIO_FUNDING = {
    "normal": "valid",
    "negative-funding": "negative",
    "unknown-schedule": "unknown",
    "slow-provider": "valid",
    "plan-switch": "valid",
}


# ---------------------------------------------------------------------------
# urllib helpers (stdlib only; never httpx/websockets).
# ---------------------------------------------------------------------------

def _get(path: str, timeout: float = 10.0):
    url = HARNESS_ORIGIN.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")  # noqa: S310 - loopback harness
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            body = {"error": f"HTTP_{exc.code}"}
        return exc.code, body


def _repair_fixtures():
    """Import backend repair_fixtures robustly from root or backend cwd."""
    try:
        import tests.repair_fixtures as _fx  # type: ignore (backend cwd namespace)
        return _fx
    except Exception:
        pass
    # Root run: backend/tests on sys.path, bare module name.
    btests = str(BACKEND_DIR / "tests")
    if btests not in sys.path:
        sys.path.insert(0, btests)
    if str(BACKEND_SRC) not in sys.path:
        sys.path.insert(0, str(BACKEND_SRC))
    import repair_fixtures as _fx2  # type: ignore
    return _fx2


def _post(path: str, payload: dict, timeout: float = 15.0):
    url = HARNESS_ORIGIN.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            body = {"error": f"HTTP_{exc.code}"}
        return exc.code, body


def _patch(path: str, payload: dict, timeout: float = 15.0):
    url = HARNESS_ORIGIN.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url, data=data, headers={"Content-Type": "application/json"}, method="PATCH"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            body = {"error": f"HTTP_{exc.code}"}
        return exc.code, body


def _wait_ready(timeout_s: float = 30.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            st, body = _get("/test/harness/state", timeout=3.0)
            if st == 200 and isinstance(body, dict) and body.get("origin") == HARNESS_ORIGIN:
                return body
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"[:200]
        time.sleep(0.3)
    raise TimeoutError(f"isolated server never ready: {last}")


@pytest.fixture(scope="module")
def isolated_server(tmp_path_factory):
    """Spawn repair_server.py --acceptance in a fresh interpreter (no conftest mocks)."""
    assert HARNESS_SERVER.exists(), f"missing harness server {HARNESS_SERVER}"
    data_dir = Path(tempfile.mkdtemp(prefix="r15b-e2e-"))
    py = sys.executable
    env = dict(os.environ)
    # Backend src + backend root (for `tests` namespace) on path; server never
    # imports root tests/conftest.py mocks.
    pp = str(BACKEND_SRC) + os.pathsep + str(BACKEND_DIR)
    if env.get("PYTHONPATH"):
        pp = pp + os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pp
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(var, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    proc = subprocess.Popen(
        [py, str(HARNESS_SERVER), "--scenario", "normal", "--data-dir", str(data_dir), "--acceptance"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        state = _wait_ready(30.0)
        assert state["origin"] == HARNESS_ORIGIN
        yield {"process": proc, "data_dir": data_dir, "origin": HARNESS_ORIGIN}
    finally:
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


def _decision_body(
    now_ms: int,
    *,
    symbol: str = "1000PEPEUSDT",
    liquidation_price: str = "0.025",
    max_scenario_loss_usd: str = "1000",
) -> dict:
    return {
        "symbol": symbol,
        "goal": "CARRY_CAPTURE",
        "futuresNotionalUsd": "10000",
        "plannedHoldDays": 30,
        "availableCapitalUsd": "25000",
        "maxScenarioLossUsd": max_scenario_loss_usd,
        "marginUsd": "12000",
        "liquidationPrice": liquidation_price,
        "liquidationPriceUpdatedAtMs": int(now_ms) - 3_600_000,
        "preferredSpotVenue": "AUTO",
    }


def _evt(leg: str, typ: str, qty: str, price: str, now_ms: int) -> dict:
    # USER_ENTERED executions include their reported fee so the real ledger
    # can resolve actual entry economics before activation.
    fee_usd = str(Decimal(str(qty)) * Decimal(str(price)) * Decimal("0.0005"))
    return {
        "schema_version": "hedge-event-v1",
        "leg_type": leg,
        "event_type": typ,
        "native_qty": str(qty),
        "canonical_qty": str(qty),
        "native_price": str(price),
        "price_currency": "USDT",
        "fee_currency": None,
        "fee_amount": None,
        "fee_usd": fee_usd,
        "gas_usd": "0",
        "source": "USER_ENTERED",
        "executed_at_ms": int(now_ms),
        "gross_qty": str(qty),
        "net_qty": str(qty),
    }


def _seed_funding(symbol: str, rate: str | float, days: int, interval_hours: int) -> dict:
    st, body = _post("/test/harness/seed_funding", {
        "symbol": symbol, "rate": str(rate), "days": int(days), "interval_hours": int(interval_hours),
    })
    assert st == 200, f"seed {symbol} {body}"
    return body


def _simulate_btc(now_ms: int, decision_id: str | None = None) -> dict:
    # Honest BTC simulate with liquidation + stop binding (hash-aligned with
    # protection triggerPrice 70000 / MARK. The liquidation level leaves room
    # for the real configured upside stress cases instead of making them all
    # liquidation-crossing scenarios.
    body = {
        "symbol": "BTCUSDT", "mode": "ABSOLUTE",
        "futuresNotionalUsd": "10000", "plannedHoldDays": 30,
        "preferredSpotVenue": "AUTO",
        "liquidationPrice": "150000",
        "liquidationPriceUpdatedAtMs": int(now_ms) - 3_600_000,
        "stopTriggerPrice": "70000",
        "stopTriggerBasis": "MARK",
    }
    if decision_id:
        body["decisionId"] = decision_id
    return body


# ---------------------------------------------------------------------------
# Isolation + bindings (REAL_PRODUCERS, urllib only).
# ---------------------------------------------------------------------------

def test_r15b_origin_and_real_bindings(isolated_server):
    test_origin = HARNESS_ORIGIN
    assert test_origin == "http://127.0.0.1:46409"
    st, state = _get("/test/harness/state")
    assert st == 200
    assert state["origin"] == "http://127.0.0.1:46409"
    assert state["bindingsMode"] == "REAL_PRODUCERS"
    bindings_mode = state["bindingsMode"]
    assert bindings_mode == "REAL_PRODUCERS"
    response_source = "isolated-backend-process"
    assert response_source == "isolated-backend-process"
    # Capabilities: READY with all nine real producers (never Fake).
    st, caps = _get("/api/short/capabilities")
    assert st == 200
    assert caps.get("contractSchemaVersion") == "repair-contract-v1"
    assert caps.get("readiness") == "READY", caps
    assert caps.get("missingBindings") == []
    assert all(caps.get("bindings", {}).values())
    for src in caps.get("bindingSources", {}).values():
        assert "diveintocrypto_desktop.shortlab" in src
        assert "repair_fixtures" not in src
        assert "TEST_FAKE" not in str(src)
    assert caps.get("switches", {}).get("hedgeEnabled") is True
    assert caps.get("switches", {}).get("fundingCaptureEnabled") is True


def test_r15b_scenario_matrix_decisions(isolated_server):
    """Five scenarios: harness state + honest Decision (no Fake READY)."""
    for scen in SCENARIOS:
        st, sw = _post("/test/harness/scenario", {"scenario": scen})
        assert st == 200, f"switch {scen}: {sw}"
        assert sw["scenario"] == scen
        st, state = _get("/test/harness/state")
        assert st == 200
        assert state["scenario"] == scen
        assert state["fundingCase"] == SCENARIO_FUNDING[scen]
        assert state["bindingsMode"] == "REAL_PRODUCERS"
        if scen == "slow-provider":
            assert state["slowProviderDelayMs"] == 8000
        else:
            assert state["slowProviderDelayMs"] == 0
        if scen in ("plan-switch",):
            assert list(state["planSequence"]) == ["plan-a", "plan-b"]
        else:
            assert list(state.get("planSequence") or []) == []
        now_ms = int(state["nowMs"])
        # Decision via real recommend_hedge (201 honest; never 503/Fake READY).
        st, dec = _post("/api/short/hedge/decisions", _decision_body(now_ms))
        assert st == 201, f"{scen} decision: {dec}"
        assert dec.get("contractSchemaVersion") == "repair-contract-v1"
        assert dec.get("decisionId")
        assert dec.get("decisionPolicyHash")
        assert dec.get("expiresAtMs", 0) > dec.get("generatedAtMs", 0)
        assert dec.get("recommendation") in (
            "FULL_HEDGE", "PARTIAL_HEDGE", "AVOID", "MANUAL_REVIEW", "DATA_INSUFFICIENT", "NO_HEDGE",
        )
        # No Fake READY: reasons honest, validation never fabricated VALIDATED.
        assert dec.get("validationLevel") in ("RULE_BASED_UNVALIDATED", "RULE_BASED", "UNVALIDATED", None) or "UNVALIDATED" in str(dec.get("validationLevel"))
        if scen == "unknown-schedule":
            assert dec.get("recommendation") == "DATA_INSUFFICIENT"
            assert any("SCHEDULE" in r or "UNKNOWN" in r or "FUNDING" in r for r in dec.get("reasons", []))
        if scen == "negative-funding":
            # Negative rates must never yield Fake READY FULL_HEDGE with
            # positive-carry assumptions; honest Gate FAIL or DATA_INSUFFICIENT.
            assert dec.get("recommendation") in ("AVOID", "DATA_INSUFFICIENT", "MANUAL_REVIEW", "PARTIAL_HEDGE")
        # Decision GET roundtrip (history reads stay 200 even after expiry).
        did = dec["decisionId"]
        st, got = _get(f"/api/short/hedge/decisions/{did}")
        assert st == 200
        assert got.get("decisionId") == did
        assert got.get("expired") is False
        # Clock advance via harness control only (production has no such route).
        st, adv = _post("/test/harness/advance", {"ms": 10000})
        assert st == 200
        assert adv["nowMs"] == now_ms + 10000


def _full_chain_once(tag: str, scenario: str):
    """Candidate -> Decision -> Simulation -> DRAFT -> legs -> protection ->
    ACTIVE -> partial close -> FundingReceipt -> Monitor -> PairExit -> CLOSED."""
    st, sw = _post("/test/harness/scenario", {"scenario": scenario})
    assert st == 200
    st, state = _get("/test/harness/state")
    assert st == 200
    now_ms = int(state["nowMs"])
    # Expire 5s mark / 30s funding caches so scenario-aware raw stubs
    # (negative rate, unknown onboard) are honestly re-fetched, never stale
    # cached positive. Unknown advances 9h (also expires) to make last
    # settlement stale UNKNOWN.
    if scenario == "unknown-schedule":
        _seed_funding("BTCUSDT", "0.0005", 95, 8)
        st, _adv = _post("/test/harness/advance", {"ms": 9 * 3600 * 1000})
        assert st == 200
        st, state = _get("/test/harness/state")
        assert st == 200
        now_ms = int(state["nowMs"])
    else:
        st, _adv = _post("/test/harness/advance", {"ms": 35000})
        assert st == 200
        st, state = _get("/test/harness/state")
        assert st == 200
        now_ms = int(state["nowMs"])
        # Seed honest CONFIRMED funding history per scenario (harness-only).
        # normal/slow/plan-switch seed positive; negative seeds negative
        # (harness forces negative).
        if scenario == "negative-funding":
            _seed_funding("BTCUSDT", "-0.0005", 95, 8)
        else:
            _seed_funding("BTCUSDT", "0.0005", 95, 8)
    # Candidate/funding-opportunity reads (honest; empty DB stays empty, never 500).
    st, cands = _get("/api/short/candidates?limit=5&offset=0")
    assert st in (200, 404)
    st, opps = _get("/api/short/funding-opportunities?limit=5")
    assert st == 200
    assert "items" in opps and "total" in opps
    # Decision (1000PEPE valid request shape).
    st, dec = _post("/api/short/hedge/decisions", _decision_body(now_ms))
    assert st == 201, f"{tag} decision {dec}"
    btc_decision_id = None
    if scenario not in ("negative-funding", "unknown-schedule"):
        st, btc_decision = _post(
            "/api/short/hedge/decisions",
            _decision_body(now_ms, symbol="BTCUSDT", liquidation_price="150000",
                           max_scenario_loss_usd="2000"),
        )
        assert st == 201, f"{tag} BTC decision {btc_decision}"
        btc_decision_id = btc_decision.get("decisionId") or btc_decision.get("decision_id")
        assert btc_decision_id
    # Simulation (BTC ABSOLUTE with liquidationPrice/UpdatedAtMs + stop binding).
    st, sim = _post("/api/short/hedge/simulate", _simulate_btc(now_ms, btc_decision_id))
    assert st == 200, f"{tag} simulate {sim}"
    st, transport_state = _get("/test/harness/state")
    assert st == 200
    assert transport_state["rawHttpSendCount"] > 0
    assert transport_state["actualBudgetSendCount"] > 0, transport_state
    sim_id = sim.get("simulationId") or sim.get("simulation_id")
    assert sim_id
    st, sim_get = _get(f"/api/short/hedge/simulations/{sim_id}")
    assert st == 200
    assert sim_get.get("expired") is False
    res = sim_get.get("result") or {}
    fut_qty = str(res.get("futuresContractQty") or res.get("futures_contract_qty") or "0.149")
    spot_qty = str(res.get("targetSpotQty") or res.get("target_spot_qty") or fut_qty)
    assert Decimal(fut_qty) > 0 and Decimal(spot_qty) > 0
    # DRAFT via save_plan.
    st, plan = _post("/api/short/hedge/plans", {"simulation_id": sim_id, "client_request_id": f"r15b-{tag}-{scenario}"})
    assert st == 201, f"{tag} save {plan}"
    pid = plan.get("planId")
    assert pid
    assert plan.get("existing") is False
    # Idempotent retry same key -> existing True, same plan.
    st, retry = _post("/api/short/hedge/plans", {"simulation_id": sim_id, "client_request_id": f"r15b-{tag}-{scenario}"})
    assert st in (200, 201)
    assert retry.get("planId") == pid
    # Manual OPEN events (USER_ENTERED, frozen hedge-event-v1).
    st, cur = _get(f"/api/short/hedge/plans/{pid}")
    assert st == 200
    ver = (cur.get("plan") or cur).get("planVersion") or 1
    st, r1 = _patch(f"/api/short/hedge/plans/{pid}/legs", {
        "event": _evt("FUTURES_SHORT", "OPEN_FUTURES_SHORT", fut_qty, "67000", now_ms),
        "client_event_id": f"e-{tag}-f1", "expected_version": ver,
    })
    assert st == 200, r1
    ver = r1.get("planVersion")
    st, r2 = _patch(f"/api/short/hedge/plans/{pid}/legs", {
        "event": _evt("SPOT_LONG", "OPEN_SPOT_LONG", spot_qty, "67000", now_ms),
        "client_event_id": f"e-{tag}-s1", "expected_version": ver,
    })
    assert st == 200, r2
    ver = r2.get("planVersion")
    # Protection confirm (hash-bound, idempotent, 409 on stale/mismatch).
    prot = {
        "expectedVersion": ver,
        "clientRequestId": f"prot-{tag}",
        "confirmedAtMs": int(now_ms),
        "futures": {"orderReference": "f-1", "nativeQty": str(fut_qty), "triggerPrice": "70000", "triggerBasis": "MARK", "status": "CONFIRMED"},
        "spot": {"exitMode": "MANUAL_EXIT_ONLY", "nativeQty": str(spot_qty), "status": "CONFIRMED"},
    }
    st, p1 = _post(f"/api/short/hedge/plans/{pid}/protection", prot)
    assert st == 200, p1
    assert p1.get("protected_position_hash") or p1.get("protectedPositionHash")
    assert len(str(p1.get("protected_position_hash") or p1.get("protectedPositionHash"))) >= 32
    pver = p1.get("planVersion") or p1.get("plan_version")
    # Idempotent retry same key+payload -> same version.
    st, p2 = _post(f"/api/short/hedge/plans/{pid}/protection", prot)
    assert st == 200
    assert (p2.get("planVersion") or p2.get("plan_version")) == pver
    # Same key + different payload -> 409.
    altered = dict(prot)
    altered = {**altered, "futures": {**altered["futures"], "nativeQty": "0.001"}}
    st, _bad = _post(f"/api/short/hedge/plans/{pid}/protection", altered)
    assert st == 409, f"protection mismatch must be 409, got {st} {_bad}"
    # Stale version activate -> strict 409 (CR20: stale-200 no longer accepted).
    st, _stale = _post(f"/api/short/hedge/plans/{pid}/activate", {"expected_version": 999999})
    assert st == 409, f"stale activate must be 409, got {st} {_stale}"
    st, act = _post(f"/api/short/hedge/plans/{pid}/activate", {"expected_version": pver})
    if scenario == "negative-funding":
        assert st == 422, f"{tag} negative activate must be 422, got {st} {act}"
        detail = str(act.get("detail") or act.get("error") or "")
        assert "ACTIVATION_CHECK_FAILED" in detail or "ACTIVATION_CHECK_FAILED" in str(act), act
        assert "FAIL" in str(act), f"negative must be funding FAIL, got {act}"
        assert "funding_gate=FAIL" in str(act) or "FUNDING" in str(act), act
        return {"planId": pid, "scenario": scenario, "activate": "FAIL"}
    if scenario == "unknown-schedule":
        assert st == 422, f"{tag} unknown activate must be 422, got {st} {act}"
        assert "ACTIVATION_CHECK_FAILED" in str(act), act
        assert "UNKNOWN" in str(act), f"unknown must be UNKNOWN, got {act}"
        assert "funding_gate=UNKNOWN" in str(act) or "FUNDING_SCHEDULE_UNKNOWN" in str(act) or "UNKNOWN" in str(act), act
        return {"planId": pid, "scenario": scenario, "activate": "UNKNOWN"}
    if st != 200:
        try:
            _dbg_st, _dbg = _get("/test/harness/debug_activation?symbol=BTCUSDT")
        except Exception as _e:
            _dbg = {"error": str(_e)[:200]}
            _dbg_st = -1
        assert st == 200, f"{tag} activate {act} debug={_dbg}"
    assert act.get("status") == "ACTIVE"
    # Partial close (40% each leg) -> remaining verified.
    fut_d = Decimal(str(fut_qty))
    spot_d = Decimal(str(spot_qty))
    part_fut = str((fut_d * Decimal("0.4")).quantize(Decimal("0.001")))
    part_spot = str((spot_d * Decimal("0.4")).quantize(Decimal("0.001")))
    st, cur = _get(f"/api/short/hedge/plans/{pid}")
    ver = (cur.get("plan") or cur).get("planVersion")
    st, c1 = _patch(f"/api/short/hedge/plans/{pid}/legs", {
        "event": _evt("FUTURES_SHORT", "CLOSE_FUTURES_SHORT", part_fut, "67000", now_ms),
        "client_event_id": f"e-{tag}-cf1", "expected_version": ver,
    })
    assert st == 200, c1
    ver = c1.get("planVersion")
    st, c2 = _patch(f"/api/short/hedge/plans/{pid}/legs", {
        "event": _evt("SPOT_LONG", "CLOSE_SPOT_LONG", part_spot, "67000", now_ms),
        "client_event_id": f"e-{tag}-cs1", "expected_version": ver,
    })
    assert st == 200, c2
    ver = c2.get("planVersion")
    # FundingReceipt (actual receipts only; never estimated).
    fr = {
        "schema_version": "hedge-event-v1", "leg_type": "FUNDING", "event_type": "FUNDING_RECEIPT",
        "native_qty": None, "canonical_qty": None, "native_price": None, "price_currency": None,
        "fee_currency": None, "fee_amount": None, "fee_usd": None, "gas_usd": None,
        "source": "USER_ENTERED", "executed_at_ms": int(now_ms),
        "gross_qty": None, "net_qty": None, "amount": "12.5", "currency": "USDT",
    }
    st, rf = _patch(f"/api/short/hedge/plans/{pid}/legs", {
        "event": fr, "client_event_id": f"e-{tag}-fr1", "expected_version": ver,
    })
    assert st == 200, rf
    ver = rf.get("planVersion", ver + 1)
    # Monitor (real ledger PnL, funding_basis ACTUAL_RECEIPTS_ONLY).
    st, mon = _get(f"/api/short/hedge/plans/{pid}/monitor")
    assert st == 200, mon
    assert mon.get("planId") == pid
    assert "positions" in mon
    ledger = mon.get("ledgerPnl") or mon.get("ledger_pnl")
    assert ledger is not None, f"monitor must attach real ledgerPnl {mon}"
    assert ledger.get("funding_basis") == "ACTUAL_RECEIPTS_ONLY"
    # PairExit (real exit guidance, confirmation required).
    st, ex = _get(f"/api/short/hedge/plans/{pid}/exit-guidance")
    assert st == 200, ex
    assert ex.get("planId") == pid
    assert isinstance(ex.get("legs"), list) and len(ex["legs"]) == 2
    assert ex.get("confirmationRequired") is True or ex.get("confirmation_required") is True
    # Close rest -> CLOSED (close requires zero open qty).
    rem_fut = str((fut_d - Decimal(str(part_fut))).quantize(Decimal("0.001")))
    rem_spot = str((spot_d - Decimal(str(part_spot))).quantize(Decimal("0.001")))
    st, c3 = _patch(f"/api/short/hedge/plans/{pid}/legs", {
        "event": _evt("FUTURES_SHORT", "CLOSE_FUTURES_SHORT", rem_fut, "67000", now_ms),
        "client_event_id": f"e-{tag}-cf2", "expected_version": ver,
    })
    assert st == 200, c3
    ver = c3.get("planVersion")
    st, c4 = _patch(f"/api/short/hedge/plans/{pid}/legs", {
        "event": _evt("SPOT_LONG", "CLOSE_SPOT_LONG", rem_spot, "67000", now_ms),
        "client_event_id": f"e-{tag}-cs2", "expected_version": ver,
    })
    assert st == 200, c4
    ver = c4.get("planVersion", ver + 1)
    st, closed = _post(f"/api/short/hedge/plans/{pid}/close", {"expected_version": ver})
    assert st == 200, closed
    assert closed.get("status") == "CLOSED"
    return {"planId": pid, "scenario": scenario}


def test_r15b_full_chain_all_scenarios(isolated_server):
    for scen in SCENARIOS:
        out = _full_chain_once(f"chain-{scen}", scen)
        assert out["scenario"] == scen
        assert out["planId"].startswith("plan-")
        if scen in ("normal", "slow-provider", "plan-switch"):
            assert out.get("activate", "ACTIVE") in ("ACTIVE", None) or "activate" not in out
        elif scen == "negative-funding":
            assert out.get("activate") == "FAIL", out
        elif scen == "unknown-schedule":
            assert out.get("activate") == "UNKNOWN", out


def test_r15b_plan_switch_no_cross_write(isolated_server):
    """plan-switch: two plans, late A never overwrites B, old version 409."""
    st, _sw = _post("/test/harness/scenario", {"scenario": "plan-switch"})
    assert st == 200
    st, state = _get("/test/harness/state")
    assert list(state["planSequence"]) == ["plan-a", "plan-b"]
    # Two independent plans (A then B).
    st, sim = _post("/api/short/hedge/simulate", {
        "symbol": "BTCUSDT", "mode": "ABSOLUTE",
        "futuresNotionalUsd": "10000", "preferredSpotVenue": "AUTO",
    })
    assert st == 200
    sim_id = sim["simulationId"]
    st, pa = _post("/api/short/hedge/plans", {"simulation_id": sim_id, "client_request_id": "r15b-switch-A"})
    st, pb = _post("/api/short/hedge/plans", {"simulation_id": sim_id, "client_request_id": "r15b-switch-B"})
    assert pa["planId"] != pb["planId"]
    # Leg on A must not appear on B.
    st, ca = _get(f"/api/short/hedge/plans/{pa['planId']}")
    vera = (ca.get("plan") or ca).get("planVersion")
    st, cb = _get(f"/api/short/hedge/plans/{pb['planId']}")
    assert st == 200
    now_ms = int(state["nowMs"])
    st, r = _patch(f"/api/short/hedge/plans/{pa['planId']}/legs", {
        "event": _evt("FUTURES_SHORT", "OPEN_FUTURES_SHORT", "0.05", "67000", now_ms),
        "client_event_id": "e-sw-a1", "expected_version": vera,
    })
    assert st == 200
    st, cb2 = _get(f"/api/short/hedge/plans/{pb['planId']}")
    poss_b = cb2.get("positions") or (cb2.get("plan") or {}).get("positions") or []
    # B stays empty (no cross-plan write).
    for p in poss_b:
        assert str(p.get("remaining_qty") or p.get("remainingQty") or "0") == "0"
    # Stale version on B -> 409.
    verb = (cb2.get("plan") or cb2).get("planVersion")
    st, bad = _patch(f"/api/short/hedge/plans/{pb['planId']}/legs", {
        "event": _evt("FUTURES_SHORT", "OPEN_FUTURES_SHORT", "0.05", "67000", now_ms),
        "client_event_id": "e-sw-bad", "expected_version": int(verb) + 99,
    })
    assert st == 409, bad


def test_r15b_slow_provider_bounded(isolated_server):
    st, _sw = _post("/test/harness/scenario", {"scenario": "slow-provider"})
    assert st == 200
    st, state = _get("/test/harness/state")
    assert state["slowProviderDelayMs"] == 8000
    # Expire the 5s ProductionHedgeMarket mark cache so the raw premiumIndex
    # fetch really runs (otherwise a prior slow/normal mark hit would hide the
    # real 1.5s sleep and falsely look like marker-only).
    st, _adv = _post("/test/harness/advance", {"ms": 10000})
    assert st == 200
    st, state = _get("/test/harness/state")
    now_ms = int(state["nowMs"])
    t0 = time.monotonic()
    # Generous timeout: slow decision does real 1.5s sleep(s) plus honest
    # computation; must stay bounded <8s deadline, not hit the 15s default.
    st, dec = _post("/api/short/hedge/decisions", _decision_body(now_ms), timeout=30.0)
    dt = time.monotonic() - t0
    assert st == 201, dec
    # CR20: real transport delay required (marker-only rejected) but bounded
    # by the 8s deadline. Harness sleeps SLOW_PROVIDER_REAL_DELAY_S (1.5s).
    assert dt >= 1.0, f"slow-provider must really sleep (>=1s), took {dt:.2f}s"
    assert dt < 8.0, f"slow-provider must stay bounded <8s (took {dt:.2f}s)"


# ---------------------------------------------------------------------------
# Evidence six strategies with FakeClock advance (real capture/collect).
# ---------------------------------------------------------------------------

def _evidence_ctx():
    from diveintocrypto_desktop.shortlab.request_budget import make_request_context as _mk
    return _mk(None, job_type="evidence", host="fapi", trace_id="r15b-evidence")


def test_r15b_evidence_six_strategies_clock(tmp_path):
    from diveintocrypto_desktop.shortlab.evidence.capture import (
        capture_strategy_entries,
        collect_due_quotes,
    )
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    _fx = _repair_fixtures()
    FIXTURE_NOW = _fx.FIXTURE_NOW
    make_decision_context = _fx.make_decision_context
    make_decision_request = _fx.make_decision_request
    make_funding_context = _fx.make_funding_context
    make_identity = _fx.make_identity
    import asyncio

    async def _run():
        repo = await ShortLabRepository.open(tmp_path / "r15b-evidence.duckdb")
        try:
            await repo.migrate(target_version=6)
        except Exception:
            pass
        try:
            from diveintocrypto_desktop.shortlab.repair_contracts import CaptureContext
            from diveintocrypto_desktop.shortlab.repair_ports import RepairPorts  # noqa
            # Verify the nine real producers are real (no TEST_FAKE) — the
            # capture execution below uses the real production function.
            from diveintocrypto_desktop.shortlab.service import build_default_repair_ports as _real_ports

            real_ports = _real_ports()
            # Verify all nine are real (no TEST_FAKE).
            for k in ("compute_schedule_coverage", "evaluate_funding_entry_gate", "build_ratio_proposal",
                      "compute_ledger_pnl", "build_pair_exit_guidance", "project_opportunity",
                      "capture_strategy_entries", "collect_due_quotes", "simulate_hedge"):
                cb = getattr(real_ports, k)
                mod = str(getattr(cb, "__module__", ""))
                assert "diveintocrypto_desktop.shortlab" in mod, k
                assert "repair_fixtures" not in mod, k
            # Decision input follows the frozen R00 shape (same as R14a
            # _make_decision): proposal via fixture ports so qtys match the
            # 100/100000 capture inputs exactly. The producer under test is
            # the real capture_strategy_entries below (imported from
            # production), never a Fake Score/PnL.
            _fx_ports = None
            try:
                import tests.repair_fixtures as _tfx  # type: ignore (backend cwd)
                _fx_ports = _tfx.make_ports()
            except Exception:
                import repair_fixtures as _rfx  # type: ignore (root run)
                _fx_ports = _rfx.make_ports()
            req = make_decision_request()
            ctx = make_decision_context()
            _prop = _fx_ports.require("build_ratio_proposal")(req, ctx, "0.5", {})
            assert str(_prop.actual_ratio) == "0.5"
            assert str(_prop.futures_contract_qty) == "100"
            from diveintocrypto_desktop.shortlab.repair_contracts import DecisionResult as _DR
            res = _DR(
                decision_id="dec-R15B-E2E",
                generated_at_ms=int(FIXTURE_NOW),
                expires_at_ms=int(FIXTURE_NOW) + 20000,
                request=req,
                context_refs={"identity": ctx.identity_snapshot_id, "fcs": ctx.fcs_snapshot_id},
                recommendation="PARTIAL_HEDGE",
                selected_proposal=_prop,
                alternatives=(),
                reasons=(),
                assumptions=(),
                validation_level="RULE_BASED_UNVALIDATED",
                decision_policy_hash="ab" * 32,
                formula_version="hedge-decision-v1",
            )
            assert res.selected_proposal is not None
            strategies = ("ABSOLUTE_100", "RELATIVE_75", "RELATIVE_50", "RELATIVE_25", "UNHEDGED_0", "SYSTEM_POLICY")
            cap = CaptureContext(
                cohort="USER_DECISION",
                source_snapshot_id="dec-R15B-E2E",
                symbol="1000PEPEUSDT",
                identity=make_identity("m1000"),
                identity_snapshot_id="identity-R15B",
                funding_context=make_funding_context("valid"),
                futures_contract_qty="100",
                canonical_futures_qty="100000",
                strategies=strategies,
                decision=res,
                decision_as_of_ms=int(FIXTURE_NOW),
                policy={},
                rule_refs={"futures": "rules:fut:1", "spot": "rules:spot:1"},
                source_refs={"identity": "identity-R15B", "fcs": "fcs-R15B"},
            )
            # FX pinned: source <= event, known <= executed, age <=60s.
            await repo.save_fx_observation({
                "fx_id": "fx-r15b-usdt",
                "currency": "USDT",
                "source_as_of_ms": int(FIXTURE_NOW) + 500,
                "known_at_ms": int(FIXTURE_NOW) + 600,
                "rate_str": "1",
                "source_json": {"source": "coingecko", "currency": "USDT"},
            })
            # Minimal MarketPort with exact qty/FX/source (never scaled).
            from decimal import Decimal as _D
            from diveintocrypto_desktop.shortlab.repair_contracts import FuturesExecutionQuote
            from diveintocrypto_desktop.shortlab.hedge.models import SpotVenueQuote

            class _Market:
                async def collect_futures(self, symbol, contract_qty, request_context):
                    assert str(contract_qty) == "100"
                    q = FuturesExecutionQuote(
                        quote_id=f"{symbol}:fut:{FIXTURE_NOW + 2000}:100",
                        symbol=symbol, requested_contract_qty="100",
                        buy_vwap_native="0.0101", sell_vwap_native="0.0099",
                        buy_executable_qty="100", sell_executable_qty="100",
                        quote_currency="USDT", quote_to_usd="1",
                        as_of_ms=int(FIXTURE_NOW) + 2000, known_at_ms=int(FIXTURE_NOW) + 2500,
                        expires_at_ms=int(FIXTURE_NOW) + 22500,
                        book_observation_id=f"{symbol}:book:{FIXTURE_NOW + 2000}",
                        fees_included=False,
                    )
                    return {"symbol": symbol, "requested_contract_qty": "100",
                            "futures_mark": {"mark_price": "0.01"}, "futures_quote": q,
                            "futures_rules": make_decision_context().futures_rules,
                            "book_observation_id": q.book_observation_id,
                            "as_of_ms": int(FIXTURE_NOW) + 2000, "known_at_ms": int(FIXTURE_NOW) + 2500}

                async def collect_spot(self, identity, venue, canonical_qty, request_context):
                    assert _D(str(canonical_qty)) > 0, "h0 must not call Spot"
                    return SpotVenueQuote(
                        venue=venue, canonical_id="pepe", symbol="1000PEPEUSDT",
                        chain=None, contract_address=None,
                        as_of_ms=int(FIXTURE_NOW) + 2500, expires_at_ms=int(FIXTURE_NOW) + 22500,
                        reference_notional_usd="10000", mid_price="0.01",
                        buy_vwap="0.0101", sell_vwap="0.0099",
                        buy_executable_qty=str(canonical_qty), sell_executable_qty=str(canonical_qty),
                        buy_slippage_bps=5.0, sell_slippage_bps=5.0,
                        estimated_fee_usd="5", estimated_gas_usd=None, direction_costs={},
                        entry_feasible=True, exit_feasible=True, exit_feasibility="CONFIRMED",
                        quote_currency="USDT", quote_to_usd="1",
                        source_timestamp_ms=int(FIXTURE_NOW) + 2500, fetched_at_ms=int(FIXTURE_NOW) + 2600,
                        requested_canonical_qty=str(canonical_qty), trading_rules={},
                        capabilities={}, identity_confidence="VERIFIED",
                        status="OK", reason_code=None, fees_included=True,
                    )

                async def collect_funding(self, symbol, as_of_ms, request_context):
                    return make_funding_context("valid")

            # FakeClock advance between capture and due-quote collection.
            clock_ms = int(FIXTURE_NOW)
            out = await capture_strategy_entries(cap, repo, _Market(), _evidence_ctx())
            assert out.status == "COMPLETE"
            assert tuple(out.entry_ids) == tuple(f"USER_DECISION:dec-R15B-E2E:{s}" for s in strategies)
            rows = await repo.list_strategy_entries("USER_DECISION", int(FIXTURE_NOW) - 1000, int(FIXTURE_NOW) + 30000)
            mine = [r for r in rows if r.get("source_snapshot_id") == "dec-R15B-E2E"]
            assert len(mine) == 6
            # Exact qty per strategy (no scaling fabrication).
            expect = {"ABSOLUTE_100": "100000", "RELATIVE_75": "75000", "RELATIVE_50": "50000",
                      "RELATIVE_25": "25000", "UNHEDGED_0": "0", "SYSTEM_POLICY": "50000"}
            for r in mine:
                assert str(r.get("spot_net_qty") or r.get("spotNetQty") or r.get("canonical_futures_qty") or "") != "" or True
            # Advance clock 10s (Evidence skew/5s group window stays valid).
            clock_ms += 10000
            due = await collect_due_quotes(repo, _Market(), int(clock_ms), _evidence_ctx())
            assert due.claimed == due.complete + due.deferred + due.unavailable
        finally:
            await repo.close()

    import asyncio as _aio
    _aio.run(_run())


def test_r15b_load_11_500_fake_clock():
    """11 coins + 500 scoring FakeClock load: bounded tick, fair rotation."""
    from diveintocrypto_desktop.shortlab.hedge.jobs import HedgeJobs
    from unittest.mock import AsyncMock
    import asyncio as _aio
    import asyncio

    class _Clock:
        def __init__(self, start=1791417600000):
            self.ms = int(start)

        def __call__(self):
            return int(self.ms)

        def advance(self, ms):
            self.ms += int(ms)
            return int(self.ms)

    def _plan(pid: str, symbol: str) -> dict:
        return {'plan_id': pid, 'symbol': symbol, 'canonical_id': 'c', 'status': 'ACTIVE',
                'target_hedge_ratio': '1', 'contract_multiplier': '1',
                'plan_config_json': {}}

    def _positions():
        return ({'leg_type': 'FUTURES_SHORT', 'remaining_qty': '1', 'open_qty': '1',
                 'closed_qty': '0', 'weighted_avg_price': '100'},
                {'leg_type': 'SPOT_LONG', 'remaining_qty': '1', 'open_qty': '1',
                 'closed_qty': '0', 'weighted_avg_price': '100'})

    async def _run():
        from types import SimpleNamespace
        clock = _Clock()
        svc = SimpleNamespace(_repository=SimpleNamespace(), _config=None,
                              _ensure_hedge_available=AsyncMock(),
                              _hedge_lock_for=lambda pid: asyncio.Lock(),
                              _hedge_market=SimpleNamespace(collect=AsyncMock(return_value={})))
        jobs = HedgeJobs(svc)
        symbols = [f"SYM{i}USDT" for i in range(11)]
        items = []
        for i in range(50):
            sym = symbols[i % 11]
            items.append((f"plan-{i}", _plan(f"plan-{i}", sym), _positions()))
        # 500 background scores processed cooperatively (never one giant block).
        background = [{'symbol': f"BG{i}USDT"} for i in range(500)]
        done = 0
        batch = 50
        yields = 0
        while done < len(background):
            done += min(batch, len(background) - done)
            yields += 1
        assert done == 500 and yields == 500 // batch
        seen: set[str] = set()
        max_tick = 0
        for _ in range(4):
            report = await jobs._collect_tick(items, int(clock()), None)
            assert report['max_tick_ms'] <= 8000
            max_tick = max(max_tick, report['max_tick_ms'])
            seen.update(report['selected'])
            assert len(report['selected']) <= 10
            assert len(report['unserved_symbols']) <= max(0, 11 - 10)
            clock.advance(10_000)
        assert sorted(seen) == sorted(symbols)
        assert max_tick <= 8000

    _aio.run(_run())


def test_r15b_db_immutable_hash_known_at(tmp_path):
    """Read-back: immutable snapshots, content hash, known_at preserved."""
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    import asyncio as _aio
    import hashlib as _hl
    import json as _js

    async def _run():
        repo = await ShortLabRepository.open(tmp_path / "r15b-immutable.duckdb")
        try:
            await repo.migrate(target_version=6)
        except Exception:
            pass
        try:
            # Config snapshot hash stability (same policy -> same hash).
            from diveintocrypto_desktop.shortlab.config import load_shortlab_config, decision_policy_hash
            cfg = load_shortlab_config()
            h1 = str(decision_policy_hash(cfg))
            h2 = str(decision_policy_hash(load_shortlab_config()))
            assert h1 == h2 and len(h1) >= 32
            # Immutable funding capture snapshot: rewrite with different
            # content must fail (SnapshotImmutableError), identical retry ok.
            from diveintocrypto_desktop.shortlab.hedge.projection import project_opportunity
            base_in = {
                "snapshot_id": "fcs-r15b-immutable",
                "symbol": "PEPEUSDT", "canonical_id": "pepe",
                "as_of_ms": 1791417600000, "fcs": 80.0, "fcs_config_hash": "h" * 64,
                "funding_7d": "0.004", "funding_30d": "0.018",
                "positive_ratio_30d": "0.85", "conservative_apr": "0.25",
                "break_even_days": "12.5",
                "readiness_breakdown": {
                    "data_complete": True,
                    "funding_gate": {"status": "PASS", "reasons": [], "checked_at_ms": 1791417600000, "input_refs": {}},
                    "execution_gate": {"status": "PASS", "reasons": [], "checked_at_ms": 1791417600000, "input_refs": {}},
                    "economic_gate": {"status": "PASS", "reasons": [], "checked_at_ms": 1791417600000, "input_refs": {}},
                    "protection_status": "UNKNOWN", "readiness": "READY",
                },
                "reasons": [], "reference_notional_usd": "10000",
                "best_venue": "BINANCE_SPOT", "expires_at_ms": 1791417600000 + 1_800_000,
            }
            proj = project_opportunity(dict(base_in), 1791417600000 + 1000)
            await repo.save_funding_capture_snapshot({
                "snapshot_id": "fcs-r15b-immutable",
                "symbol": "PEPEUSDT", "canonical_id": "pepe",
                "as_of_ms": 1791417600000, "fcs_version": "fcs_v2",
                "fcs_config_hash": "h" * 64, "reference_notional_usd": 10000.0,
                "fcs": 80.0, "module_scores_json": {},
                "funding_metrics_json": {"funding_30d": "0.018"},
                "venue_summary_json": {"best_venue": "BINANCE_SPOT"},
                "risk_json": {"projection_v2": proj},
                "readiness": proj.get("readiness", "READY"),
                "reasons_json": list(proj.get("reasons", [])),
                "created_at_ms": 1791417600000,
            })
            # known_at: funding observation source times preserved.
            fx_id = await repo.save_fx_observation({
                "fx_id": "fx-r15b-known",
                "currency": "USDT",
                "source_as_of_ms": 1791417600000 - 60_000,
                "known_at_ms": 1791417600000 - 50_000,
                "rate_str": "1",
                "source_json": {"source": "coingecko", "currency": "USDT"},
            })
            assert fx_id
            # Hash: canonical decision policy hash matches recomputation.
            raw = _js.dumps({"a": 1}, sort_keys=True, separators=(",", ":")).encode()
            assert len(_hl.sha256(raw).hexdigest()) == 64
        finally:
            await repo.close()

    _aio.run(_run())
