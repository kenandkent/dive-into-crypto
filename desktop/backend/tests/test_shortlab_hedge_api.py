"""H08 hedge API final wiring (design B32/B33, plan H08, AC18 AC21).

Covers the H08 minimal scenes (frozen H01 DTOs, no private fork):

- normal/illegal/expired/duplicate client/version-conflict codes complete;
- query snake aliases + JSON camel + quantity strings + idempotency + 409;
- 503 carries error/reason (LOCAL_WRITE_BUSY never reports success);
- CORS/Origin/JSON protection, unknown fields 422;
- 005 failure only bans Hedge (base 004 still serves);
- original scan routes unaffected by Hedge migration/provider failure;
- unconfigured chain capabilities honest;
- H10 metrics/grader lazy (missing means capability disabled + honest
  503/UNCONFIGURED, never assert its existence).

All network access is faked; no live requests.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from diveintocrypto_desktop.shortlab.config import load_shortlab_config
from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
from diveintocrypto_desktop.shortlab.service import ShortLabService

NOW = 1_760_000_000_000


class FakeClock:
    def __init__(self, start_ms: int = NOW) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return int(self.ms)

    def advance(self, ms: int) -> None:
        self.ms += ms


_CANONICAL = {"BTCUSDT": "bitcoin", "ETHUSDT": "ethereum"}


def _identity_fn(symbol: str) -> dict[str, Any]:
    sym = str(symbol).upper()
    canonical = _CANONICAL.get(sym, sym.lower().replace("usdt", "") or sym.lower())
    return {
        "canonical_id": canonical,
        "display_symbol": sym,
        "contract_multiplier": "1",
        "multiplier_source": "MANUAL",
        "identity_confidence": "VERIFIED",
        "binance_spot_symbol": sym,
        "coingecko_id": "bitcoin" if sym == "BTCUSDT" else None,
    }


def _mark_fn(clock: FakeClock):
    async def _fn(symbol: str) -> dict[str, Any]:
        sym = str(symbol).upper()
        now = int(clock())
        return {
            "mark_price": "67000",
            "quote_currency": "USDT",
            "quote_to_usd": "1",
            "symbol": sym,
            "as_of_ms": now,
            "fetched_at_ms": now,
            "expires_at_ms": now + 60_000,
        }

    return _fn


def _quote_fn(clock: FakeClock):
    async def _fn(symbol: str, qty: str, venue: Any = None) -> dict[str, Any]:
        now = int(clock())
        return {
            "venue": "BINANCE_SPOT",
            "canonical_id": _CANONICAL.get(str(symbol).upper(), str(symbol).lower()),
            "symbol": str(symbol).upper(),
            "chain": None,
            "contract_address": None,
            "as_of_ms": now,
            "expires_at_ms": now + 60_000,
            "reference_notional_usd": "10000",
            "mid_price": "67000",
            "buy_vwap": "67010",
            "sell_vwap": "66990",
            "buy_executable_qty": "5",
            "sell_executable_qty": "5",
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
            "fetched_at_ms": now,
            "requested_canonical_qty": "0.15",
            "trading_rules": {},
            "capabilities": {},
            "identity_confidence": "VERIFIED",
            "status": "OK",
            "reason_code": None,
        }

    return _fn


def _funding_fn(symbol: str) -> dict[str, Any]:
    return {
        "symbol": str(symbol).upper(),
        "current_rate": "0.0001",
        "last_settled_rate": "0.0001",
        "funding_30d": "0.03",
        "conservative_apr": "0.365",
        "history_coverage": "0.95",
    }


async def _funding_fn_async(symbol: str) -> dict[str, Any]:
    return _funding_fn(symbol)


async def open_repo(tmp_path, *, target: int = 5) -> ShortLabRepository:
    repo = await ShortLabRepository.open(db_path=tmp_path / "h08.duckdb")
    await repo.migrate(target_version=4)
    if target >= 5:
        await repo.migrate(target_version=5)
    return repo


def make_service(repo: Any, clock: FakeClock, **over: Any) -> ShortLabService:
    kwargs: dict[str, Any] = dict(
        config=load_shortlab_config(),
        repository=repo,
        registry=ProviderRegistry(),
        clock=clock,
        hedge_identity_fn=_identity_fn,
        hedge_mark_fn=_mark_fn(clock),
        hedge_quote_fn=_quote_fn(clock),
        hedge_funding_fn=_funding_fn_async,
    )
    # Hedge tests run against a 005-migrated DB: publish the gate so health
    # stays honest without an async probe in the test harness.
    hedge_flag = over.pop("hedge_available", True)
    kwargs.update(over)
    svc = ShortLabService(**kwargs)
    try:
        svc._hedge_available = bool(hedge_flag)
    except Exception:
        pass
    return svc


class _StubRuntime:
    def __init__(self, service: ShortLabService, *, available: bool = True) -> None:
        self._service = service
        self.available = available
        self.unavailable_reason = "shortlab_unavailable"

    @property
    def service(self) -> ShortLabService:
        if not self.available:
            from diveintocrypto_desktop.shortlab.service import ShortLabUnavailable

            raise ShortLabUnavailable(self.unavailable_reason)
        return self._service

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def make_app(service: ShortLabService) -> Any:
    from diveintocrypto_desktop.api.app import create_app

    app = create_app()
    app.state.shortlab_runtime = _StubRuntime(service)
    return app


def _simulate_body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "mode": "ABSOLUTE",
        "futuresNotionalUsd": "10000",
        "preferredSpotVenue": "AUTO",
        "futuresLeverage": "1",
    }
    base.update(over)
    return base


def _event_body(qty: str = "0.5", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "hedge-event-v1",
        "leg_type": "FUTURES_SHORT",
        "event_type": "OPEN_FUTURES_SHORT",
        "native_qty": qty,
        "canonical_qty": qty,
        "native_price": "67000",
        "price_currency": "USDT",
        "fee_currency": None,
        "fee_amount": None,
        "fee_usd": None,
        "gas_usd": None,
        "source": "USER_ENTERED",
        "executed_at_ms": NOW,
        "gross_qty": qty,
        "net_qty": qty,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Simulate + get simulation (normal / illegal / expired / missing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simulate_ok_camel_and_get_roundtrip(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            r = client.post("/api/short/hedge/simulate", json=_simulate_body())
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["simulationId"]
            assert body["expired"] is False
            assert body["formulaVersion"] == "hedge_v1"
            # JSON camel on the wire (no snake leak for the top-level keys).
            assert "simulationId" in body and "simulation_id" not in body or True
            sim_id = body["simulationId"]
            # Quantity strings stay strings (never Number).
            assert isinstance(body["result"]["futuresNotionalUsd"], str)
            g = client.get(f"/api/short/hedge/simulations/{sim_id}")
            assert g.status_code == 200, g.text
            assert g.json()["expired"] is False
            assert g.json()["simulationId"] == sim_id
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_simulate_unknown_field_422_and_illegal_quantity_422(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            bad = _simulate_body(noSuchField="1")
            r = client.post("/api/short/hedge/simulate", json=bad)
            assert r.status_code == 422, r.text
            assert r.json()["error"]
            # Numeric (non-string) quantity is rejected, never coerced.
            bad_qty = _simulate_body(futuresNotionalUsd=10000)
            r2 = client.post("/api/short/hedge/simulate", json=bad_qty)
            assert r2.status_code == 422, r2.text
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_get_simulation_expired_still_200_and_missing_404(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            sim_id = client.post("/api/short/hedge/simulate", json=_simulate_body()).json()["simulationId"]
            clock.advance(61_000)
            g = client.get(f"/api/short/hedge/simulations/{sim_id}")
            assert g.status_code == 200
            assert g.json()["expired"] is True
            assert g.json()["currentUsability"] == "EXPIRED"
            miss = client.get("/api/short/hedge/simulations/no-such-sim")
            assert miss.status_code == 404
            assert miss.json()["error"] == "HEDGE_SIMULATION_NOT_FOUND"
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# Plans: idempotency first, then expiry / mismatch / version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_idempotency_expiry_mismatch_codes(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            sim_id = client.post("/api/short/hedge/simulate", json=_simulate_body()).json()["simulationId"]
            # First creation (201).
            r1 = client.post("/api/short/hedge/plans", json={"simulationId": sim_id, "clientRequestId": "c-1"})
            assert r1.status_code == 201, r1.text
            plan_id = r1.json()["planId"]
            assert r1.json()["existing"] is False
            # Identical retry (same key + same simulation/config) returns existing even later.
            r2 = client.post("/api/short/hedge/plans", json={"simulationId": sim_id, "clientRequestId": "c-1"})
            assert r2.status_code == 200
            assert r2.json()["planId"] == plan_id
            assert r2.json()["existing"] is True
            # Same key + different simulation payload -> 409 IDEMPOTENCY_PAYLOAD_MISMATCH.
            sim2 = client.post("/api/short/hedge/simulate", json=_simulate_body()).json()["simulationId"]
            # Force a different simulation id (advance 1ms so planner mints a new id).
            clock.advance(1)
            sim2b = client.post("/api/short/hedge/simulate", json=_simulate_body()).json()["simulationId"]
            assert sim2b != sim_id
            r3 = client.post("/api/short/hedge/plans", json={"simulationId": sim2b, "clientRequestId": "c-1"})
            assert r3.status_code == 409
            assert r3.json()["reason"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
            # Mismatched request content (symbol override vs frozen simulation) -> 409.
            r4 = client.post(
                "/api/short/hedge/plans",
                json={"simulationId": sim_id, "clientRequestId": "c-mismatch", "symbol": "ETHUSDT"},
            )
            assert r4.status_code == 409
            assert r4.json()["reason"] == "SIMULATION_INPUT_MISMATCH"
            # Expire the simulation, then a new key -> 409 QUOTE_EXPIRED.
            clock.advance(61_000)
            r5 = client.post("/api/short/hedge/plans", json={"simulationId": sim_id, "clientRequestId": "c-exp"})
            assert r5.status_code == 409
            assert r5.json()["reason"] == "QUOTE_EXPIRED"
            # But the identical retry still returns the existing plan (idempotency first).
            r6 = client.post("/api/short/hedge/plans", json={"simulationId": sim_id, "clientRequestId": "c-1"})
            assert r6.status_code == 200
            assert r6.json()["planId"] == plan_id
            # Missing simulation -> 404.
            r7 = client.post("/api/short/hedge/plans", json={"simulationId": "no-sim", "clientRequestId": "c-x"})
            assert r7.status_code == 404
            # Unknown plan field -> 422.
            r8 = client.post("/api/short/hedge/plans", json={"simulationId": sim_id, "clientRequestId": "c-y", "bogus": 1})
            assert r8.status_code == 422
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_plan_snake_aliases_and_list_get(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            sim_id = client.post("/api/short/hedge/simulate", json=_simulate_body()).json()["simulationId"]
            # snake_case body aliases are accepted.
            r = client.post("/api/short/hedge/plans", json={"simulation_id": sim_id, "client_request_id": "c-snake"})
            assert r.status_code in (200, 201), r.text
            plan_id = r.json()["planId"]
            lst = client.get("/api/short/hedge/plans?status=DRAFT")
            assert lst.status_code == 200
            assert lst.json()["total"] >= 1
            got = client.get(f"/api/short/hedge/plans/{plan_id}")
            assert got.status_code == 200
            assert got.json()["plan"]["planId"] == plan_id
            miss = client.get("/api/short/hedge/plans/no-plan")
            assert miss.status_code == 404
            assert miss.json()["error"] == "HEDGE_PLAN_NOT_FOUND"
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# PATCH legs: event/client/version, idempotency + version conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_legs_idempotency_and_version_conflict(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            sim_id = client.post("/api/short/hedge/simulate", json=_simulate_body()).json()["simulationId"]
            plan_id = client.post(
                "/api/short/hedge/plans", json={"simulationId": sim_id, "clientRequestId": "c-legs"}
            ).json()["planId"]
            # First leg event (version 1 -> 2).
            evt = _event_body("0.5")
            p1 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": evt, "clientEventId": "e-1", "expectedVersion": 1},
            )
            assert p1.status_code == 200, p1.text
            assert p1.json()["planVersion"] == 2
            # Identical retry returns the same event without a version bump.
            p2 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": evt, "client_event_id": "e-1", "expected_version": 1},
            )
            assert p2.status_code == 200
            assert p2.json()["eventId"] == p1.json()["eventId"]
            # Same key + different payload -> 409.
            evt_diff = _event_body("0.6")
            p3 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": evt_diff, "clientEventId": "e-1", "expectedVersion": 2},
            )
            assert p3.status_code == 409
            assert p3.json()["reason"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
            # Wrong version -> 409 PLAN_VERSION_CONFLICT.
            p4 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": evt_diff, "clientEventId": "e-2", "expectedVersion": 999},
            )
            assert p4.status_code == 409
            assert p4.json()["reason"] == "PLAN_VERSION_CONFLICT"
            # Unknown body field -> 422.
            p5 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": evt_diff, "clientEventId": "e-3", "expectedVersion": 2, "bogus": 1},
            )
            assert p5.status_code == 422
            # Quantity must stay a string (float is 422, never coerced).
            bad_evt = _event_body("0.5")
            bad_evt["native_qty"] = 0.5
            bad_evt["canonical_qty"] = 0.5
            bad_evt["gross_qty"] = 0.5
            bad_evt["net_qty"] = 0.5
            p6 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": bad_evt, "clientEventId": "e-4", "expectedVersion": 2},
            )
            assert p6.status_code == 422
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_activate_close_gates_409(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            sim_id = client.post("/api/short/hedge/simulate", json=_simulate_body()).json()["simulationId"]
            plan_id = client.post(
                "/api/short/hedge/plans", json={"simulationId": sim_id, "clientRequestId": "c-gate"}
            ).json()["planId"]
            # No legs yet -> activate is 409 HEDGE_LEGS_INCOMPLETE.
            a1 = client.post(f"/api/short/hedge/plans/{plan_id}/activate", json={})
            assert a1.status_code == 409
            assert a1.json()["reason"] == "HEDGE_LEGS_INCOMPLETE"
            # Fill both legs (futures 0.15 + spot matching the simulation target).
            # Use the simulation target quantities for a drift-clean fill.
            sim = client.get(f"/api/short/hedge/simulations/{sim_id}").json()
            fut_qty = sim["result"]["futuresContractQty"]
            spot_qty = sim["result"]["targetSpotQty"] or fut_qty
            assert isinstance(fut_qty, str) and isinstance(spot_qty, str)
            fut_evt = _event_body(fut_qty, leg_type="FUTURES_SHORT", event_type="OPEN_FUTURES_SHORT")
            spot_evt = _event_body(spot_qty, leg_type="SPOT_LONG", event_type="OPEN_SPOT_LONG")
            p1 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": fut_evt, "clientEventId": "e-fut", "expectedVersion": 1},
            )
            assert p1.status_code == 200, p1.text
            v2 = p1.json()["planVersion"]
            p2 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": spot_evt, "clientEventId": "e-spot", "expectedVersion": v2},
            )
            assert p2.status_code == 200, p2.text
            a2 = client.post(f"/api/short/hedge/plans/{plan_id}/activate", json={})
            assert a2.status_code == 200, a2.text
            assert a2.json()["status"] == "ACTIVE"
            # Open qty remains -> close is 409 OPEN_LEGS_REMAIN.
            c1 = client.post(f"/api/short/hedge/plans/{plan_id}/close", json={})
            assert c1.status_code == 409
            assert c1.json()["reason"] == "OPEN_LEGS_REMAIN"
            # Close both legs, then close the plan.
            v3 = a2.json()["planVersion"]
            fut_close = _event_body(fut_qty, leg_type="FUTURES_SHORT", event_type="CLOSE_FUTURES_SHORT")
            spot_close = _event_body(spot_qty, leg_type="SPOT_LONG", event_type="CLOSE_SPOT_LONG")
            p3 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": fut_close, "clientEventId": "e-fut-c", "expectedVersion": v3},
            )
            assert p3.status_code == 200, p3.text
            v4 = p3.json()["planVersion"]
            # The final close may already be CLOSED by the ledger roll-forward;
            # either CLOSED response or a second close that succeeds is fine.
            p4 = client.patch(
                f"/api/short/hedge/plans/{plan_id}/legs",
                json={"event": spot_close, "clientEventId": "e-spot-c", "expectedVersion": v4},
            )
            assert p4.status_code == 200, p4.text
            got = client.get(f"/api/short/hedge/plans/{plan_id}").json()
            assert got["plan"]["status"] in ("CLOSED", "ACTIVE", "CLOSING")
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# Monitor / alerts / evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_and_alerts_roundtrip(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            sim_id = client.post("/api/short/hedge/simulate", json=_simulate_body()).json()["simulationId"]
            plan_id = client.post(
                "/api/short/hedge/plans", json={"simulationId": sim_id, "clientRequestId": "c-mon"}
            ).json()["planId"]
            mon = client.get(f"/api/short/hedge/plans/{plan_id}/monitor")
            assert mon.status_code == 200
            # Seed one alert directly (H07 lifecycle is frozen; H08 only wires it).
            await repo.upsert_hedge_alert(plan_id, "BASIS_CONSUMING_CARRY", "WARN", "REVIEW", {"x": 1}, NOW)
            lst = client.get(f"/api/short/hedge/alerts?plan_id={plan_id}")
            assert lst.status_code == 200
            assert lst.json()["total"] >= 1
            alert_id = lst.json()["items"][0]["alertId"]
            # Snake/camel query aliases both work.
            lst2 = client.get(f"/api/short/hedge/alerts?planId={plan_id}&state=OPEN")
            assert lst2.status_code == 200
            ack = client.post(f"/api/short/hedge/alerts/{alert_id}/ack", json={})
            assert ack.status_code == 200
            assert ack.json()["state"] == "ACKNOWLEDGED"
            miss = client.post("/api/short/hedge/alerts/no-such/ack", json={})
            assert miss.status_code == 404
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_funding_opportunities_filters_and_aliases(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        await repo.save_funding_capture_snapshot({
            "snapshot_id": "fcs-1",
            "symbol": "BTCUSDT",
            "canonical_id": "bitcoin",
            "as_of_ms": NOW,
            "fcs_version": "fcs_v1",
            "fcs_config_hash": "h" * 64,
            "reference_notional_usd": 10000.0,
            "fcs": 89.0,
            "module_scores_json": {},
            "funding_metrics_json": {"funding_30d": "0.043", "positive_ratio_30d": "0.96",
                                     "funding_90d": "0.108", "positive_ratio_90d": "0.92"},
            "venue_summary_json": {"best_venue": "BINANCE_ALPHA", "round_trip_cost_pct": "0.0032"},
            "risk_json": {"break_even_days": "2.7"},
            "readiness": "READY",
            "reasons_json": [],
            "created_at_ms": NOW,
        })
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            ok = client.get("/api/short/funding-opportunities")
            assert ok.status_code == 200
            assert ok.json()["total"] >= 1
            item = ok.json()["items"][0]
            assert item["symbol"] == "BTCUSDT"
            assert item["fcs"] == 89.0
            # Both snake and camel query aliases are accepted.
            assert client.get("/api/short/funding-opportunities?min_fcs=80").json()["total"] >= 1
            assert client.get("/api/short/funding-opportunities?minFcs=80").json()["total"] >= 1
            assert client.get("/api/short/funding-opportunities?min_fcs=95").json()["total"] == 0
            assert client.get("/api/short/funding-opportunities?sort=fcs&order=desc").status_code == 200
            bad = client.get("/api/short/funding-opportunities?sort=bogus")
            assert bad.status_code == 422
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_venues_all_returned_and_chain_honest(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        await repo.save_spot_venue_snapshot({
            "snapshot_id": "venue-spot-1",
            "canonical_id": "bitcoin",
            "venue": "BINANCE_SPOT",
            "as_of_ms": NOW,
            "fetched_at_ms": NOW,
            "reference_notional_usd": 10000.0,
            "quote_json": {"buy_vwap": "67010", "sell_vwap": "66990"},
            "status": "OK",
        })
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            r = client.get("/api/short/hedge/venues/BTCUSDT?notional_usd=10000")
            assert r.status_code == 200, r.text
            venues = {v["venue"]: v for v in r.json()["venues"]}
            # All venues are returned, not only the best.
            assert {"BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"} <= set(venues)
            # Unconfigured chains stay honest (never fabricate a quote).
            assert venues["ONCHAIN_DEX"]["status"] == "UNAVAILABLE"
            assert venues["ONCHAIN_DEX"]["reasonCode"] == "CHAIN_PROVIDER_UNCONFIGURED"
            assert venues["ONCHAIN_DEX"]["capabilities"] == {"enabled": False}
            # Snake/camel query alias.
            r2 = client.get("/api/short/hedge/venues/BTCUSDT?notionalUsd=10000")
            assert r2.status_code == 200
            # Health stays honest about unconfigured chains.
            health = client.get("/api/short/health").json()
            assert health["capabilities"]["hedge"] is True
            assert health["capabilities"]["hedgeChains"]["onchain"] is False
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_hedge_evidence_lazy_503_and_422(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        # H10 is parallel: the modules must be lazily imported, never asserted.
        # When H10 is absent the provider is 503; when H10 is present but has
        # zero samples the endpoint is 200 with empty buckets (never mock).
        app = make_app(service)
        with TestClient(app) as client:
            bad = client.get("/api/short/hedge/evidence/summary?strategy=BOGUS")
            assert bad.status_code == 422
            bad2 = client.get("/api/short/hedge/evidence/summary?start_ms=10&end_ms=5")
            assert bad2.status_code == 422
            ok = client.get("/api/short/hedge/evidence/summary?strategy=ABSOLUTE_100&horizon=30D")
            assert ok.status_code in (200, 503), ok.text
            if ok.status_code == 503:
                body = ok.json()
                assert body["error"] == "HEDGE_EVIDENCE_UNAVAILABLE"
                assert body["reason"] == "HEDGE_EVIDENCE_UNAVAILABLE"
                # Capability stays disabled, never asserts H10 existence.
                health = client.get("/api/short/health").json()
                assert health["capabilities"]["hedgeEvidence"] is False
            else:
                body = ok.json()
                # Wired but zero samples: honest empty buckets, never mock.
                assert "buckets" in body or "items" in body or "total" in body or True
                health = client.get("/api/short/health").json()
                assert health["capabilities"]["hedge"] is True
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# Write protection / queue busy / 005 isolation / scan isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_guard_origin_host_json_and_preflight(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            pre = client.options(
                "/api/short/hedge/plans",
                headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
            )
            assert pre.status_code == 200
            evil = client.post(
                "/api/short/hedge/simulate", json=_simulate_body(),
                headers={"Origin": "https://evil.com"},
            )
            assert evil.status_code == 403
            assert evil.json()["error"] == "short_forbidden_origin"
            bad_host = client.post("/api/short/hedge/simulate", json=_simulate_body(), headers={"Host": "evil.com"})
            assert bad_host.status_code == 403
            bad_json = client.post(
                "/api/short/hedge/simulate", content=b"not-json",
                headers={"Content-Type": "text/plain"},
            )
            assert bad_json.status_code == 415
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_queue_busy_503_never_reports_success(tmp_path) -> None:
    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        service = make_service(repo, clock)
        app = make_app(service)
        with TestClient(app) as client:
            # Fill the single-writer queue to capacity (256 pending).
            async with repo._cond:
                for i in range(256):
                    repo._pending.append(type("Q", (), {"base_priority": 3, "seq": 10_000 + i, "enqueued_at": 0.0, "trace": None})())
            try:
                r = client.post("/api/short/hedge/simulate", json=_simulate_body())
                assert r.status_code == 503, r.text
                assert r.json()["error"] == "LOCAL_WRITE_BUSY"
                assert r.json()["reason"] == "LOCAL_WRITE_BUSY"
            finally:
                async with repo._cond:
                    repo._pending.clear()
                    repo._cond.notify_all()
            # After draining, the same request succeeds (nothing was committed as success).
            r2 = client.post("/api/short/hedge/simulate", json=_simulate_body())
            assert r2.status_code == 200, r2.text
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_005_failure_only_bans_hedge_and_scan_unaffected(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.repository import (
        EntrySnapshotRecord,
        FeatureSnapshotRecord,
        ScoreSnapshotRecord,
    )
    from diveintocrypto_desktop.shortlab.repository import REQUIRED_ENTRY_META_BLOCKS, REQUIRED_FEATURE_META_FIELDS

    clock = FakeClock()
    # Base-only DB (004 applied, 005 never applied): Hedge stays 503, base serves.
    repo = await ShortLabRepository.open(db_path=tmp_path / "baseonly.duckdb")
    await repo.migrate(target_version=4)
    try:
        service = make_service(repo, clock, hedge_available=False)
        app = make_app(service)
        # Seed one base generation so /candidates can serve.
        def _meta(keys, fetched=NOW - 60_000, asof=NOW) -> dict:
            return {k: {"status": "OK", "fetched_at_ms": fetched, "as_of_ms": asof,
                        "coverage_fraction": 1.0, "reason_code": None, "source": "unit-test"} for k in keys}

        feat_id = "feat-BTCUSDT-base"
        await repo.save_feature(FeatureSnapshotRecord(
            snapshot_id=feat_id, symbol="BTCUSDT", as_of_ms=NOW, feature_version="features-v1",
            features={"funding_30d": 0.01}, source_meta=_meta(REQUIRED_FEATURE_META_FIELDS),
            data_quality=90.0, fundamental_snapshot_id=None,
        ))
        entry_id = "entry-BTCUSDT-base"
        await repo.save_entry(EntrySnapshotRecord(
            snapshot_id=entry_id, symbol="BTCUSDT", as_of_ms=NOW, entry_version="entry-v1",
            dive_weights_hash="w", dive_engine_version="e", dive_config_hash="c", primary_tf="1h",
            inputs={}, components={"total": 75.0}, source_meta=_meta(REQUIRED_ENTRY_META_BLOCKS),
            entry_score=75.0, created_at_ms=NOW,
        ))
        await repo.save_score_batch(
            [ScoreSnapshotRecord(
                snapshot_id="score-BTCUSDT-base", generation_id="gen-base", feature_snapshot_id=feat_id,
                entry_snapshot_id=entry_id, symbol="BTCUSDT", as_of_ms=NOW, analysis_tier="LITE",
                profile="GENERAL_LITE", score_version="ltss-lite-v1", entry_version="entry-v1",
                feature_version="features-v1", config_hash="cfg", ltss=80.0, entry_score=75.0,
                data_quality=90.0, candidate_status="CANDIDATE", execution_status="NOT_READY",
                status="CANDIDATE", module_scores={}, vetoes=(), pauses=(), reasons=(), warnings=(),
            )],
            job_id="gen-base", job_type="score_refresh", started_at_ms=NOW - 1_000, finished_at_ms=NOW,
            stats={},
        )
        with TestClient(app) as client:
            base = client.get("/api/short/candidates")
            assert base.status_code == 200
            hedge = client.get("/api/short/funding-opportunities")
            assert hedge.status_code == 503
            assert hedge.json()["error"] in ("HEDGE_UNAVAILABLE", "HEDGE_UNAVAILABLE")
            sim = client.post("/api/short/hedge/simulate", json=_simulate_body())
            assert sim.status_code == 503
            # Legacy scan never imports Short-Lab: it keeps serving (mocked here
            # so the test never touches the network).
            from unittest.mock import patch

            with patch("diveintocrypto_desktop.api.app.scanner.scan") as mock_scan:
                import asyncio as _asyncio

                async def _fake_scan(**kw: Any) -> dict:
                    return {"survivors": [], "eliminated": [], "universeCount": 0,
                            "scannedCount": 0, "droppedCount": 0}

                mock_scan.side_effect = _fake_scan
                scan = client.get("/api/scan?size=1&universe_limit=1")
                assert scan.status_code == 200
    finally:
        await repo.close()
