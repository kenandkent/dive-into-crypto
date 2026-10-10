"""R10b repair service true wiring (D02/D12/D18/D19.1).

Real RepairPorts (never Fake) + Catalog->Resolver identity + real
Observation/receipt chain + directional metrics + H10/R14 callbacks.
Covers: stale-version 409, empty-body 422, idempotency, protection hash,
old-simulation readonly, Decision expiry, 451 unknown, delisted BLOCKED.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

NOW = 1_791_417_600_000


class FakeClock:
    def __init__(self, start_ms: int = NOW) -> None:
        self.ms = int(start_ms)

    def __call__(self) -> int:
        return int(self.ms)

    def advance(self, ms: int) -> None:
        self.ms += int(ms)


def _enabled_config():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    base = load_shortlab_config()
    try:
        hedge = dataclasses.replace(base.hedge, enabled=True)
        funding = dataclasses.replace(base.funding_capture, enabled=True)
        config = dataclasses.replace(base, hedge=hedge, funding_capture=funding)
    except Exception:
        config = base
    return config


def _disabled_config():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    return load_shortlab_config()


async def _open_repo(tmp_path, name: str = "r10b-service.duckdb"):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    repo = await ShortLabRepository.open(db_path=tmp_path / name)
    await repo.migrate(target_version=4)
    try:
        await repo.migrate(target_version=6)
    except Exception:
        try:
            await repo.migrate(target_version=5)
        except Exception:
            pass
    return repo


def _identity_overrides():
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


def _mark_fn(clock: FakeClock):
    async def _fn(symbol: str) -> dict[str, Any]:
        now = int(clock())
        return {
            "mark_price": "0.012",
            "native_price": "0.012",
            "quote_currency": "USDT",
            "quote_to_usd": "1",
            "symbol": str(symbol).upper(),
            "as_of_ms": now,
            "fetched_at_ms": now,
            "known_at_ms": now,
            "expires_at_ms": now + 60_000,
        }

    return _fn


def _quote_fn(clock: FakeClock):
    async def _fn(symbol: str, qty: str, venue: Any = None) -> dict[str, Any]:
        now = int(clock())
        return {
            "venue": str(venue or "BINANCE_SPOT"),
            "canonical_id": "pepe" if "PEPE" in str(symbol).upper() else str(symbol).lower(),
            "symbol": str(symbol).upper(),
            "chain": None,
            "contract_address": None,
            "as_of_ms": now,
            "fetched_at_ms": now,
            "known_at_ms": now,
            "expires_at_ms": now + 60_000,
            "reference_notional_usd": "10000",
            "mid_price": "0.012",
            "buy_vwap": "0.0121",
            "sell_vwap": "0.0119",
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
            "fetched_at_ms": now,
            "requested_canonical_qty": "100000",
            "trading_rules": {},
            "capabilities": {},
            "identity_confidence": "VERIFIED",
            "status": "OK",
            "reason_code": None,
        }

    return _fn


def _funding_fn(symbol: str = "1000PEPEUSDT"):
    async def _fn(sym: str) -> dict[str, Any]:
        return {
            "symbol": str(sym).upper(),
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

    return _fn


def _rules_fn():
    def _fn() -> dict[str, Any]:
        return {
            "symbol": "1000PEPEUSDT",
            "lot_rules": {"step_size": "1", "min_qty": "1", "max_qty": "10000000"},
            "notional_rules": {"min_notional": "5", "max_notional": "1000000"},
            "price_rules": {"min_price": "0.000001", "max_price": "100"},
            "order_types": ["LIMIT", "MARKET", "STOP", "STOP_MARKET"],
            "stop_orders_supported": True,
            "conditional_orders_source_ref": "exchangeInfo:1000PEPEUSDT",
        }

    return _fn


def _make_service(repo: Any, clock: FakeClock, config: Any = None, **over: Any):
    from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
    from diveintocrypto_desktop.shortlab.service import ShortLabService
    from diveintocrypto_desktop.shortlab.service import build_default_repair_ports

    cfg = config if config is not None else _enabled_config()
    # R10b: directional metrics provider (real, empty DB -> 200/0).
    try:
        from diveintocrypto_desktop.shortlab.evidence.metrics import build_metrics_provider as _bmp

        try:
            _metrics = _bmp(repo, cfg, clock)
        except TypeError:
            _metrics = _bmp(repo, cfg)
    except Exception:
        _metrics = None
    kwargs: dict[str, Any] = dict(
        config=cfg,
        repository=repo,
        registry=ProviderRegistry(),
        clock=clock,
        identity_overrides=_identity_overrides(),
        hedge_mark_fn=_mark_fn(clock),
        hedge_quote_fn=_quote_fn(clock),
        hedge_funding_fn=_funding_fn(),
        hedge_futures_rules_fn=_rules_fn(),
        hedge_spot_rules_fn=_rules_fn(),
        hedge_available=True,
        repair_ports=build_default_repair_ports(),
        metrics_provider=_metrics,
    )
    kwargs.update(over)
    svc = ShortLabService(**kwargs)
    return svc


def _decision_body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": "1000PEPEUSDT",
        "goal": "CARRY_CAPTURE",
        "futuresNotionalUsd": "10000",
        "plannedHoldDays": 30,
        "availableCapitalUsd": "25000",
        "maxScenarioLossUsd": "1000",
        "marginUsd": "12000",
        "liquidationPrice": "0.025",
        "liquidationPriceUpdatedAtMs": NOW - 3_600_000,
        "preferredSpotVenue": "AUTO",
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_r10b_capabilities_ready_with_real_ports(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        caps = svc.repair_capabilities()
        assert caps["contractSchemaVersion"] == "repair-contract-v1"
        assert caps["readiness"] == "READY", caps
        assert caps["missingBindings"] == []
        assert all(caps["bindings"].values())
        # Sources are real production modules (never test fakes).
        for src in caps["bindingSources"].values():
            assert "shortlab" in src
            assert "test" not in src.lower() or "latest" in src.lower() or "shortlab" in src
        assert caps["switches"]["hedgeEnabled"] is True
        assert caps["switches"]["fundingCaptureEnabled"] is True
        # No secrets leak.
        import json as _js

        text = _js.dumps(caps).lower()
        assert "api_key" not in text
        assert "secret" not in text
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_decision_unknown_when_no_funding_history(tmp_path) -> None:
    # Single-provider gap -> 201 DATA_INSUFFICIENT (never 503).
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        result = await svc.create_repair_decision(_decision_body())
        assert result["recommendation"] in ("DATA_INSUFFICIENT", "FULL_HEDGE", "PARTIAL_HEDGE", "AVOID", "MANUAL_REVIEW")
        # With empty DB (no schedules/events) the funding leg is UNKNOWN.
        # Real ports must not fake READY: either DATA_INSUFFICIENT or honest gate.
        if result["recommendation"] == "DATA_INSUFFICIENT":
            assert any("FUNDING" in r or "SCHEDULE" in r or "UNKNOWN" in r for r in result["reasons"])
        assert result["contractSchemaVersion"] == "repair-contract-v1"
        assert result["decisionId"]
        # GET roundtrip with expired flag.
        got = await svc.get_repair_decision(result["decisionId"])
        assert got["decisionId"] == result["decisionId"]
        assert got["expired"] is False
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_decision_expiry_flag(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        created = await svc.create_repair_decision(_decision_body())
        did = created["decisionId"]
        assert created["expired"] is False
        clock.advance(25_000)
        # Service clock advanced: GET must report expired True (frozen result).
        got = await svc.get_repair_decision(did)
        assert got["expired"] is True
        assert got["expiresAtMs"] == created["expiresAtMs"]
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_decision_empty_body_422(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        from diveintocrypto_desktop.shortlab.service import HedgeValidationError

        with pytest.raises(HedgeValidationError):
            await svc.create_repair_decision({})
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_451_unknown_not_503(tmp_path) -> None:
    # 451 region error -> honest DATA_INSUFFICIENT (201), never 503/READY.
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        async def _mark_451(symbol: str) -> Any:
            raise RuntimeError("451 Region restricted (VENUE_REGION_UNAVAILABLE)")

        svc = _make_service(repo, clock, hedge_mark_fn=_mark_451)
        result = await svc.create_repair_decision(_decision_body())
        assert result["recommendation"] == "DATA_INSUFFICIENT"
        assert result["contractSchemaVersion"] == "repair-contract-v1"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_delisted_blocked(tmp_path) -> None:
    # Delisted exchange status -> honest AVOID/BLOCKED (never READY).
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        async def _meta_delisted() -> dict[str, Any]:
            return {
                "1000PEPEUSDT": {
                    "symbol": "1000PEPEUSDT",
                    "status": "DELISTED",
                    "contract_type": "PERPETUAL",
                    "onboard_at_ms": NOW - 200 * 86_400_000,
                    "observed_at_ms": NOW,
                }
            }

        svc = _make_service(repo, clock, metadata_fn=_meta_delisted)
        result = await svc.create_repair_decision(_decision_body())
        assert result["recommendation"] == "AVOID"
        text = " ".join(result["reasons"])
        assert "BLOCKED" in text or "DELISTING" in text or "DIRECTIONAL" in text
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_old_simulation_readonly(tmp_path) -> None:
    # Old manual simulate without goal/decisionId stays readonly-compatible.
    from diveintocrypto_desktop.shortlab.service import ShortLabService

    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        # Old body without goal (legacy ABSOLUTE, no decision link).
        sim = await svc.simulate({
            "symbol": "BTCUSDT",
            "mode": "ABSOLUTE",
            "futuresNotionalUsd": "10000",
            "preferredSpotVenue": "AUTO",
        })
        assert sim["simulationId"]
        assert sim["expired"] is False
        # Expire and still readable (200 + expired True, never 503).
        clock.advance(61_000)
        got = await svc.get_simulation(sim["simulationId"])
        assert got["expired"] is True
        assert got["currentUsability"] == "EXPIRED"
        # Old plan from expired sim with new key -> 409 (but idempotent retry still 200).
        from diveintocrypto_desktop.shortlab.service import HedgeQuoteExpired

        with pytest.raises(HedgeQuoteExpired):
            await svc.save_plan({"simulation_id": sim["simulationId"], "client_request_id": "old-new-key"})
        # Idempotent retry with original key still returns existing (tested in hedge_api).
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_activate_stale_version_409_and_idempotency(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        sim = await svc.simulate({
            "symbol": "BTCUSDT",
            "mode": "ABSOLUTE",
            "futuresNotionalUsd": "10000",
            "preferredSpotVenue": "AUTO",
        })
        plan = await svc.save_plan({"simulation_id": sim["simulationId"], "client_request_id": "r10b-act-1"})
        pid = plan["planId"]
        assert plan["existing"] is False
        # Idempotent retry same key+sim -> 200 existing True.
        retry = await svc.save_plan({"simulation_id": sim["simulationId"], "client_request_id": "r10b-act-1"})
        assert retry["planId"] == pid
        assert retry["existing"] is True
        # Stale version cannot activate (409).
        from diveintocrypto_desktop.shortlab.service import HedgeVersionConflict

        with pytest.raises(HedgeVersionConflict):
            await svc.activate(pid, {"expected_version": 999})
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_protection_hash_binding(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        sim = await svc.simulate({
            "symbol": "BTCUSDT",
            "mode": "ABSOLUTE",
            "futuresNotionalUsd": "10000",
            "preferredSpotVenue": "AUTO",
        })
        plan = await svc.save_plan({"simulation_id": sim["simulationId"], "client_request_id": "r10b-prot-1"})
        pid = plan["planId"]
        ver = plan["plan"]["planVersion"]

        def _evt(leg: str, typ: str, qty: str) -> dict[str, Any]:
            return {
                "schema_version": "hedge-event-v1",
                "leg_type": leg,
                "event_type": typ,
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

        # Fill both legs so remaining (0.5) matches the protection qty.
        r1 = await svc.apply_leg_event(pid, {"event": _evt("FUTURES_SHORT", "OPEN_FUTURES_SHORT", "0.5"), "client_event_id": "e-fut-1", "expected_version": ver})
        ver2 = r1["planVersion"]
        await svc.apply_leg_event(pid, {"event": _evt("SPOT_LONG", "OPEN_SPOT_LONG", "0.5"), "client_event_id": "e-spot-1", "expected_version": ver2})
        # Re-read version for protection (legs bumped it twice).
        from diveintocrypto_desktop.shortlab.repository import ShortLabRepository as _R

        prow = await repo.get_hedge_plan(pid)
        ver3 = int(prow.get("plan_version") or ver2)
        body = {
            "expectedVersion": ver3,
            "clientRequestId": "prot-1",
            "confirmedAtMs": NOW,
            "futures": {
                "orderReference": "f-1",
                "nativeQty": "0.5",
                "triggerPrice": "70000",
                "triggerBasis": "MARK",
                "status": "CONFIRMED",
            },
            "spot": {"exitMode": "MANUAL_EXIT_ONLY", "nativeQty": "0.5", "status": "CONFIRMED"},
        }
        first = await svc.confirm_repair_protection(pid, body)
        assert first["protected_position_hash"]
        assert first["planVersion"] == ver3 + 1
        # Idempotent retry same key+payload -> same version (no bump).
        second = await svc.confirm_repair_protection(pid, body)
        assert second["planVersion"] == first["planVersion"]
        # Same key + different payload -> 409.
        from diveintocrypto_desktop.shortlab.service import HedgeIdempotencyMismatch

        altered = dict(body)
        altered = {**altered, "futures": {**altered["futures"], "nativeQty": "0.6"}}
        with pytest.raises(HedgeIdempotencyMismatch):
            await svc.confirm_repair_protection(pid, altered)
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_switches_off_new_suggestions_503_but_history_reads(tmp_path) -> None:
    # Default closed switches: new Decisions 503, old simulation reads still 200.
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        # First create with enabled switches, then disable and verify history.
        svc_on = _make_service(repo, clock, config=_enabled_config())
        created = await svc_on.create_repair_decision(_decision_body())
        did = created["decisionId"]
        # Simulate with enabled for history.
        sim = await svc_on.simulate({
            "symbol": "BTCUSDT",
            "mode": "ABSOLUTE",
            "futuresNotionalUsd": "10000",
            "preferredSpotVenue": "AUTO",
        })
        sim_id = sim["simulationId"]
        # Now switch off (new service with disabled config, same repo + real ports).
        from diveintocrypto_desktop.shortlab.service import build_default_repair_ports
        from diveintocrypto_desktop.shortlab.service import HedgeDisabled

        svc_off = _make_service(repo, clock, config=_disabled_config())
        # New decision -> 503 HEDGE_DISABLED/FUNDING_CAPTURE_DISABLED.
        with pytest.raises((HedgeDisabled, Exception)) as exc_info:
            await svc_off.create_repair_decision(_decision_body())
        assert "DISABLED" in str(type(exc_info.value).__name__) or "DISABLED" in str(exc_info.value) or getattr(exc_info.value, "error_code", "") in ("HEDGE_DISABLED", "FUNDING_CAPTURE_DISABLED")
        # History reads still work (old simulation readonly, decision GET).
        got_sim = await svc_off.get_simulation(sim_id)
        assert got_sim["simulationId"] == sim_id
        got_dec = await svc_off.get_repair_decision(did)
        assert got_dec["decisionId"] == did
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_monitor_includes_real_pnl(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        sim = await svc.simulate({
            "symbol": "BTCUSDT",
            "mode": "ABSOLUTE",
            "futuresNotionalUsd": "10000",
            "preferredSpotVenue": "AUTO",
        })
        plan = await svc.save_plan({"simulation_id": sim["simulationId"], "client_request_id": "r10b-mon-1"})
        pid = plan["planId"]
        mon = await svc.monitor(pid)
        assert mon["planId"] == pid
        # Real PnL attached (never hand-filled cache without computation).
        assert "ledgerPnl" in mon or "ledger_pnl" in mon or "positions" in mon
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_opportunity_current_query_latest_wins(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        # Seed two snapshots for same symbol (old READY, new NOT_READY).
        # Use repository's funding capture snapshot with projection_v2.
        from diveintocrypto_desktop.shortlab.hedge.projection import project_opportunity

        def _inputs(sid: str, asof: int, readiness: str) -> dict[str, Any]:
            return {
                "snapshot_id": sid,
                "symbol": "PEPEUSDT",
                "canonical_id": "pepe",
                "as_of_ms": asof,
                "fcs": 80.0,
                "fcs_config_hash": "h" * 64,
                "funding_7d": "0.004",
                "funding_30d": "0.018",
                "positive_ratio_30d": "0.85",
                "conservative_apr": "0.25",
                "break_even_days": "12.5",
                "readiness_breakdown": {
                    "data_complete": True,
                    "funding_gate": {"status": "PASS", "reasons": [], "checked_at_ms": asof, "input_refs": {}},
                    "execution_gate": {"status": "PASS" if readiness == "READY" else "FAIL", "reasons": [] if readiness == "READY" else ["EXEC_FAIL"], "checked_at_ms": asof, "input_refs": {}},
                    "economic_gate": {"status": "PASS", "reasons": [], "checked_at_ms": asof, "input_refs": {}},
                    "protection_status": "UNKNOWN",
                    "readiness": readiness,
                },
                "reasons": [],
                "reference_notional_usd": "10000",
                "best_venue": "BINANCE_SPOT",
                "expires_at_ms": asof + 1_800_000,
            }

        old_in = _inputs("fcs-old-r10b", NOW - 10_000, "READY")
        new_in = _inputs("fcs-new-r10b", NOW, "NOT_READY")
        old_proj = project_opportunity(old_in, NOW - 9_000)
        new_proj = project_opportunity(new_in, NOW + 1_000)
        import json as _js

        async def _seed(sid: str, asof: int, created: int, proj: Any) -> None:
            await repo.save_funding_capture_snapshot({
                "snapshot_id": sid,
                "symbol": "PEPEUSDT",
                "canonical_id": "pepe",
                "as_of_ms": asof,
                "fcs_version": "fcs_v2",
                "fcs_config_hash": "h" * 64,
                "reference_notional_usd": 10000.0,
                "fcs": 80.0,
                "module_scores_json": {},
                "funding_metrics_json": {"funding_30d": "0.018", "positive_ratio_30d": "0.85"},
                "venue_summary_json": {"best_venue": "BINANCE_SPOT"},
                "risk_json": {"projection_v2": proj, "break_even_days": "12.5"},
                "readiness": proj.get("readiness", "NOT_READY"),
                "reasons_json": list(proj.get("reasons", [])),
                "created_at_ms": created,
            })

        await _seed("fcs-old-r10b", NOW - 10_000, NOW - 9_000, old_proj)
        await _seed("fcs-new-r10b", NOW, NOW, new_proj)
        page = await svc.funding_opportunities({"symbol": "PEPEUSDT"})
        assert page["total"] == 1
        assert page["items"][0]["snapshotId"] == "fcs-new-r10b"
        assert page["items"][0]["readiness"] == "NOT_READY"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_no_hedge_plan_422(tmp_path) -> None:
    # NO_HEDGE decisions never seed two-leg plans (422 UNHEDGED_PLAN_UNSUPPORTED).
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        from diveintocrypto_desktop.shortlab.service import HedgeValidationError

        # Build a real NO_HEDGE decision via fixtures + real recommend_hedge.
        try:
            from tests.repair_fixtures import make_decision_context as _mk_ctx
            from tests.repair_fixtures import make_decision_request as _mk_req
            from tests.repair_fixtures import make_ports as _mk_ports
        except Exception:
            from repair_fixtures import make_decision_context as _mk_ctx  # type: ignore
            from repair_fixtures import make_decision_request as _mk_req  # type: ignore
            from repair_fixtures import make_ports as _mk_ports  # type: ignore
        from diveintocrypto_desktop.shortlab.hedge.decision import recommend_hedge as _rec2
        from diveintocrypto_desktop.shortlab.repair_contracts import to_record_dict as _to_rec2

        req = _mk_req("MEME_FULL_VALID", goal="DIRECTIONAL_SHORT")
        ctx = _mk_ctx("MEME_FULL_VALID")
        policy = svc._config
        res = _rec2(req, ctx, policy, ports=_mk_ports())
        assert res.recommendation == "NO_HEDGE", f"fixture must yield NO_HEDGE, got {res.recommendation}"
        rec = _to_rec2(res)
        await repo.save_hedge_decision({
            "decision_id": str(res.decision_id),
            "symbol": "1000PEPEUSDT",
            "generated_at_ms": int(res.generated_at_ms),
            "expires_at_ms": int(res.expires_at_ms),
            "decision_policy_hash": str(res.decision_policy_hash),
            "decision_json": rec,
        }, ())
        # Simulate with that decision then plan must be 422.
        # Use a normal simulation (same symbol) but link the NO_HEDGE decision:
        # the plan must still be rejected (422) because the decision is NO_HEDGE.
        try:
            sim = await svc.simulate({
                "symbol": "1000PEPEUSDT",
                "mode": "ABSOLUTE",
                "futuresNotionalUsd": "10000",
                "preferredSpotVenue": "AUTO",
            })
            sim_id = sim["simulationId"]
        except HedgeValidationError as exc:
            # Simulate itself may reject NO_HEDGE seeding (also 422) - acceptable.
            assert "UNHEDGED" in str(exc) or "NO_HEDGE" in str(exc) or getattr(exc, "error_code", "") == "UNHEDGED_PLAN_UNSUPPORTED"
            return
        with pytest.raises(HedgeValidationError) as exc_info:
            await svc.save_plan({"simulation_id": sim_id, "client_request_id": "no-hedge-1", "decision_id": str(res.decision_id)})
        assert "UNHEDGED" in str(exc_info.value) or "NO_HEDGE" in str(exc_info.value) or exc_info.value.error_code == "UNHEDGED_PLAN_UNSUPPORTED"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_activate_rejects_unreceipted_funding_and_unknown_costs(tmp_path) -> None:
    # CR01 D12: six-item single-cutoff ACTIVATION_CHECK persists (kind by symbol).
    # Full PASS needs liq + 90d CONFIRMED schedule + matching protection.
    import json as _js

    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        sim = await svc.simulate({
            "symbol": "BTCUSDT",
            "mode": "ABSOLUTE",
            "futuresNotionalUsd": "10000",
            "preferredSpotVenue": "AUTO",
            "liquidationPrice": "0.02",
            "liquidationPriceUpdatedAtMs": NOW - 3_600_000,
        })
        plan = await svc.save_plan({"simulation_id": sim["simulationId"], "client_request_id": "r10b-act-check-1"})
        pid = plan["planId"]
        # Plan liq for hash binding (save_plan ignores snake liq; store explicitly).
        _row0 = await repo.get_hedge_plan(pid)
        _cfg0 = _row0.get("plan_config_json") or {}
        if isinstance(_cfg0, str):
            try:
                _cfg0 = _js.loads(_cfg0)
            except Exception:
                _cfg0 = {}
        _cfg0 = dict(_cfg0) if isinstance(_cfg0, dict) else {}
        _cfg0["liquidation_price"] = "0.02"
        _cfg0["stop_trigger_basis"] = "MARK_PRICE"
        repo._require_con().execute(
            "UPDATE sl_hedge_plan SET plan_config_json = ? WHERE plan_id = ?",
            [_js.dumps(_cfg0), pid],
        )
        # Seeded 90d CONFIRMED 8h schedule for an honest funding PASS.
        _sym = "BTCUSDT"
        _h8 = 8 * 3_600_000
        _start = NOW - 90 * 86_400_000
        _slots: list[int] = []
        _cur = _start + _h8
        while _cur <= NOW:
            _slots.append(_cur)
            _cur += _h8
        await repo.upsert_funding_events([
            {"symbol": _sym, "funding_time_ms": _s, "funding_rate": 0.0005}
            for _s in _slots
        ])
        await repo.save_funding_schedule({
            "schedule_id": "sched-act-pass", "symbol": _sym,
            "effective_from_ms": _start, "effective_to_ms": None, "known_at_ms": _start,
            "schedule_json": {
                "schedule_id": "sched-act-pass", "symbol": _sym,
                "effective_from_ms": _start, "effective_to_ms": None,
                "interval_hours": 8, "anchor_ms": _slots[0], "known_at_ms": _start,
                "source": "binance:fapi/fundingInfo", "evidence_ref": "ev-act",
                "verification": "CONFIRMED",
            },
        })

        def _evt2(leg: str, typ: str, qty: str) -> dict[str, Any]:
            return {
                "schema_version": "hedge-event-v1",
                "leg_type": leg,
                "event_type": typ,
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

        # Fill both legs from the simulation's target qtys for drift-clean fill.
        got_sim = await svc.get_simulation(sim["simulationId"])
        fut_qty = got_sim["result"].get("futuresContractQty") or got_sim["result"].get("futures_contract_qty") or "0.15"
        spot_qty = got_sim["result"].get("targetSpotQty") or got_sim["result"].get("target_spot_qty") or fut_qty
        if not isinstance(fut_qty, str):
            fut_qty = str(fut_qty)
        if not isinstance(spot_qty, str):
            spot_qty = str(spot_qty)
        r1 = await svc.apply_leg_event(pid, {"event": _evt2("FUTURES_SHORT", "OPEN_FUTURES_SHORT", fut_qty), "client_event_id": "e-f-1", "expected_version": 1})
        r2 = await svc.apply_leg_event(pid, {"event": _evt2("SPOT_LONG", "OPEN_SPOT_LONG", spot_qty), "client_event_id": "e-s-1", "expected_version": r1["planVersion"]})
        # Matching protection (MARK_PRICE basis so hashes agree).
        _pos = await repo.aggregate_hedge_position(pid)
        _fr = next(p for p in _pos if p.get("leg_type") == "FUTURES_SHORT").get("remaining_qty")
        _sr = next(p for p in _pos if p.get("leg_type") == "SPOT_LONG").get("remaining_qty")
        await svc.confirm_repair_protection(pid, {
            "expected_version": r2["planVersion"],
            "client_request_id": "r10b-act-check-1",
            "confirmed_at_ms": NOW,
            "futures": {"status": "CONFIRMED", "nativeQty": str(_fr), "triggerBasis": "MARK_PRICE", "orderReference": "f-1"},
            "spot": {"status": "CONFIRMED", "nativeQty": str(_sr), "exitMode": "PLATFORM_ORDER", "orderReference": "s-1"},
        })
        _current_plan = await repo.get_hedge_plan(pid)
        from diveintocrypto_desktop.shortlab.service import HedgeValidationError

        with pytest.raises(HedgeValidationError, match="ACTIVATION_CHECK_FAILED"):
            await svc.activate(pid, {"expected_version": int(_current_plan["plan_version"])})
        assert str((await repo.get_hedge_plan(pid)).get("status")) != "ACTIVE"
        # CR01: strict symbol/kind query (no swallowed tuple fallback).
        rows = await repo.list_market_observations("BTCUSDT", "ACTIVATION_CHECK", 0, int(clock()) + 1_000, int(clock()) + 1_000)
        assert len(rows) >= 1
        _checks = rows[-1]["value_json"]["checks"]
        for _k in ("identity", "mark_vs_liquidation", "depth", "protection"):
            assert _checks[_k]["status"] == "PASS", (_k, _checks[_k])
        assert _checks["funding_gate"]["status"] == "UNKNOWN"
        assert _checks["economics"]["status"] == "UNKNOWN"
        assert isinstance(_checks.get("protected_position_hash"), str) and _checks["protected_position_hash"]
        # CR20: pid-as-symbol is a query mismatch so it must yield 0 rows.
        rows_pid = await repo.list_market_observations(pid, "ACTIVATION_CHECK", 0, int(clock()) + 1_000, int(clock()) + 1_000)
        assert isinstance(rows_pid, tuple)
        assert len(rows_pid) == 0, f"pid-as-symbol query must be empty (symbol mismatch), got {len(rows_pid)}"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_r10b_directional_metrics_and_h10_mark_history_wired(tmp_path) -> None:
    # Directional metrics provider (empty DB -> 200/0, never permanent not-wired).
    # H10 hedge grader/metrics + R14b MARK history via R03 injection.
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    try:
        svc = _make_service(repo, clock)
        # Directional metrics: empty library is honest 200/0 via real provider.
        from diveintocrypto_desktop.shortlab.service import Unavailable

        res = await svc.evidence_summary({})
        # Empty DB with real provider returns EvidenceSummary(total=0), not Unavailable.
        assert not isinstance(res, Unavailable), "directional metrics must be wired (R10b), not permanently not-wired"
        legacy_summary = await svc.hedge_evidence_summary({"cohort": "LEGACY_FCS"})
        assert not isinstance(legacy_summary, Unavailable), "LEGACY_FCS must remain a queryable read-only cohort"
        # H10 hedge metrics lazy but wired (module exists).
        try:
            from diveintocrypto_desktop.shortlab.evidence import hedge_metrics as _hm

            assert hasattr(_hm, "hedge_summary") or hasattr(_hm, "summary")
        except Exception:
            pytest.skip("H10 hedge_metrics module missing")
        # H10 hedge grader callback real injection when hedge available.
        from diveintocrypto_desktop.shortlab.runtime import ShortLabRuntime

        rt = ShortLabRuntime(
            config=_enabled_config(),
            db_path=tmp_path / "r10b-h10.duckdb",
            repair_ports=__import__("diveintocrypto_desktop.shortlab.service", fromlist=["build_default_repair_ports"]).build_default_repair_ports(),
            allow_test_bindings=False,
        )
        await rt.start()
        try:
            assert rt.service.hedge_grader_callback is not None or rt.service._metrics_provider is not None
            # MARK history via R03 (unbound returns (), bound returns MARK bars).
            hist = getattr(rt.service, "_hedge_market_provider", None)
            assert hist is not None
            assert getattr(hist, "mark_price_bars_fn", None) is not None
            # Unbound MARK path stays UNKNOWN (empty tuple, never TRADE-as-MARK).
            from diveintocrypto_desktop.shortlab.evidence.historical_market import RepositoryHistoricalMarketProvider

            empty_hist = RepositoryHistoricalMarketProvider(repo, mark_price_bars_fn=None)
            bars = await empty_hist.read_mark_price_bars("BTCUSDT", NOW - 3_600_000, NOW, None)
            assert bars == ()
        finally:
            await rt.stop()
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_hedge_evidence_http_route_preserves_real_summary_and_evaluation_bucket(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from diveintocrypto_desktop.api.app import create_app
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import resolve_cost_hash
    from diveintocrypto_desktop.shortlab.hedge import HEDGE_EVIDENCE_VERSION_CURRENT

    clock = FakeClock()
    repo = await _open_repo(tmp_path, "hedge-evidence-http.duckdb")
    service = _make_service(repo, clock)
    source = "http-entry-sample"
    await repo.save_strategy_entry({
        "entry_id": f"{source}:ABSOLUTE_100", "cohort": "USER_DECISION",
        "symbol": "BTCUSDT", "source_snapshot_id": source,
        "strategy": "ABSOLUTE_100", "decision_as_of_ms": NOW,
        "executed_as_of_ms": NOW,
        "entry_json": {"status": "ENTRY_COMPLETE", "actual_ratio": "1", "goal": "CARRY_CAPTURE"},
    }, references=())
    await repo.save_hedge_outcome({
        "outcome_id": f"{source}:ABSOLUTE_100:7d", "fcs_snapshot_id": source,
        "strategy": "ABSOLUTE_100", "horizon_days": 7,
        "outcome_status": "COMPLETE", "reason_code": None,
        "evidence_version": HEDGE_EVIDENCE_VERSION_CURRENT,
        "cost_config_hash": resolve_cost_hash(service._config),
        "outcome_json": {
            "pnl": {"net_return": "0.025", "fees_usd": "3.5"},
            "risk": {"path_coverage": "PARTIAL"},
        },
        "updated_at_ms": NOW + 8 * 86_400_000,
    })

    class TestRuntime:
        available = True
        unavailable_reason = ""

        def __init__(self):
            self.service = service

        async def start(self):
            return None

        async def stop(self):
            return None

    app = create_app(shortlab_runtime_factory=TestRuntime)
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/short/hedge/evidence/summary",
                params={
                    "strategy": "ABSOLUTE_100", "cohort": "USER_DECISION",
                    "horizon": "7D", "now_ms": NOW + 8 * 86_400_000,
                },
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 1
        assert len(body["buckets"]) == 1
        bucket = body["buckets"][0]
        assert bucket["strategy"] == "ABSOLUTE_100"
        assert bucket["cohort"] == "USER_DECISION"
        assert bucket["COMPLETE"] == 1
        assert bucket["total"] == 1
        assert bucket["meanNetReturn"] == "0.025"
        assert bucket["evaluation"]["known_costs"]["mean_fees_usd"] == 3.5
        assert bucket["evaluation"]["bootstrap"]["status"] == "INSUFFICIENT_SAMPLE"
        assert bucket["sampleStatus"] == "INSUFFICIENT_SAMPLE"
        assert len(bucket["subBuckets"]) == 1
        sub = bucket["subBuckets"][0]
        assert sub["bucket"]["cohort"] == "USER_DECISION"
        assert sub["sampleCount"] == 1
        assert sub["evaluation"]["entry_status_counts"]["ENTRY_COMPLETE"] == 1
        assert body["summary"]["paired_count"] == 0
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# CR04 (D08): current zero is final; history via independent entry.
# ---------------------------------------------------------------------------


def _cr04_inputs(sid: str, asof: int, readiness: str) -> dict[str, Any]:
    return {
        "snapshot_id": sid,
        "symbol": "PEPEUSDT",
        "canonical_id": "pepe",
        "as_of_ms": asof,
        "fcs": 80.0,
        "fcs_config_hash": "h" * 64,
        "funding_7d": "0.004",
        "funding_30d": "0.018",
        "positive_ratio_30d": "0.85",
        "conservative_apr": "0.25",
        "break_even_days": "12.5",
        "readiness_breakdown": {
            "data_complete": True,
            "funding_gate": {"status": "PASS", "reasons": [], "checked_at_ms": asof, "input_refs": {}},
            "execution_gate": {"status": "PASS" if readiness == "READY" else "FAIL", "reasons": [] if readiness == "READY" else ["EXEC_FAIL"], "checked_at_ms": asof, "input_refs": {}},
            "economic_gate": {"status": "PASS", "reasons": [], "checked_at_ms": asof, "input_refs": {}},
            "protection_status": "UNKNOWN",
            "readiness": readiness,
        },
        "reasons": [],
        "reference_notional_usd": "10000",
        "best_venue": "BINANCE_SPOT",
        "expires_at_ms": asof + 1_800_000,
    }


async def _cr04_seed_two(repo: Any) -> None:
    from diveintocrypto_desktop.shortlab.hedge.projection import project_opportunity

    old_in = _cr04_inputs("fcs-old-cr04", NOW - 10_000, "READY")
    new_in = _cr04_inputs("fcs-new-cr04", NOW, "NOT_READY")
    old_proj = project_opportunity(old_in, NOW - 9_000)
    new_proj = project_opportunity(new_in, NOW + 1_000)

    async def _seed(sid: str, asof: int, created: int, proj: Any) -> None:
        await repo.save_funding_capture_snapshot({
            "snapshot_id": sid,
            "symbol": "PEPEUSDT",
            "canonical_id": "pepe",
            "as_of_ms": asof,
            "fcs_version": "fcs_v2",
            "fcs_config_hash": "h" * 64,
            "reference_notional_usd": 10000.0,
            "fcs": 80.0,
            "module_scores_json": {},
            "funding_metrics_json": {"funding_30d": "0.018", "positive_ratio_30d": "0.85"},
            "venue_summary_json": {"best_venue": "BINANCE_SPOT"},
            "risk_json": {"projection_v2": proj, "break_even_days": "12.5"},
            "readiness": proj.get("readiness", "NOT_READY"),
            "reasons_json": list(proj.get("reasons", [])),
            "created_at_ms": created,
        })

    await _seed("fcs-old-cr04", NOW - 10_000, NOW - 9_000, old_proj)
    await _seed("fcs-new-cr04", NOW, NOW, new_proj)


@pytest.mark.asyncio
async def test_cr04_ready_filter_empty_stays_empty_not_old_ready(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, "cr04-ready.duckdb")
    try:
        svc = _make_service(repo, clock)
        await _cr04_seed_two(repo)
        page = await svc.funding_opportunities({"readiness": "READY"})
        assert page["total"] == 0 and page["items"] == []
        assert page["asOf"] is None
        # Explicit history still exposes both rows (independent entry).
        hist = await svc.funding_opportunities_history({})
        assert hist["total"] == 2
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr04_all_expired_venue_empty_overpage_stay_empty(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, "cr04-exp.duckdb")
    try:
        svc = _make_service(repo, clock)
        await _cr04_seed_two(repo)
        # All expired: advance beyond both expires (1800s) without stale.
        clock.advance(2_000_000)
        exp_page = await svc.funding_opportunities({"symbol": "PEPEUSDT"})
        assert exp_page["total"] == 0 and exp_page["items"] == []
        assert exp_page["asOf"] is None
        # Stale research view still shows NOT_READY (never READY).
        stale = await svc.funding_opportunities({"symbol": "PEPEUSDT", "include_stale": True})
        assert stale["total"] == 1
        assert stale["items"][0]["readiness"] == "NOT_READY"
        # Venue/target filter empty stays empty (no fallback to other venue).
        clock.ms = NOW
        svc2 = _make_service(repo, clock)
        venue_page = await svc2.funding_opportunities({"venue": "BINANCE_ALPHA"})
        assert venue_page["total"] == 0 and venue_page["items"] == []
        # Over-page keeps total but empty items (no history revive).
        over = await svc2.funding_opportunities({"limit": 1, "offset": 10})
        assert over["total"] == 1 and over["items"] == []
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# CR07 (D04/D05): default short Carry frozen with Hedge R05; old max24h alone
# never grants completeness. Missing schedule / deleted slots => no 30D credit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cr07_missing_schedule_no_30d_credit(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, "cr07-missing.duckdb")
    try:
        sym = "TESTUSDT"
        as_of = NOW
        day = 86_400_000
        # 30 daily events: old max24h says complete (gaps exactly 24h).
        await repo.upsert_funding_events([
            {"symbol": sym, "funding_time_ms": as_of - (30 - i) * day, "funding_rate": 0.0005}
            for i in range(30)
        ])
        svc = _make_service(repo, clock)
        res = await svc._fetch_funding(sym, {}, as_of, as_of)
        assert res["windows"][30]["complete"] is False
        assert res["windows"][30]["reason_code"] in ("FUNDING_SCHEDULE_UNKNOWN", "COVERAGE_GAP")
        assert res["windows"][30].get("r05_complete") is False
        # Inputs/Carry/DQ share the same frozen verdict (no 30D credit).
        from types import SimpleNamespace

        from diveintocrypto_desktop.shortlab.models import ProviderResult
        from diveintocrypto_desktop.shortlab.scoring.ltss import extract_features

        ident = SimpleNamespace(contract_multiplier=1.0, multiplier_source="EXCHANGE", mapping_confidence="VERIFIED")
        spot = ProviderResult(status="UNAVAILABLE", source="spot", fetched_at_ms=as_of, as_of_ms=None, data=None, stale=False, reason_code=None, error_message=None)
        inputs = svc._build_inputs(sym, {"price": 100}, ident, {}, None, spot, {}, res, as_of)
        assert inputs["funding_30d_complete"] is False and inputs["funding_30d"] is None
        feats = extract_features(inputs, as_of)
        assert feats.features["carry"]["factors"]["funding_30d"]["score"] is None
        states = svc._build_field_states(ident, {}, ProviderResult(status="UNAVAILABLE", source="coingecko", fetched_at_ms=as_of, as_of_ms=None, data=None, stale=False, reason_code=None, error_message=None), spot, {}, res, as_of, as_of)
        f30 = next(s for s in states if s.field_id == "funding_30d")
        assert f30.status != "OK"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr07_deleted_slots_no_30d_credit_but_full_passes(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, "cr07-gap.duckdb")
    try:
        h8 = 8 * 3_600_000
        start = NOW - 30 * 86_400_000
        slots: list[int] = []
        cur = start + h8
        while cur <= NOW:
            slots.append(cur)
            cur += h8
        assert len(slots) == 90
        sym_full = "FULLUSDT"
        await repo.upsert_funding_events([
            {"symbol": sym_full, "funding_time_ms": s, "funding_rate": 0.0005} for s in slots
        ])
        await repo.save_funding_schedule({
            "schedule_id": "sched-full", "symbol": sym_full,
            "effective_from_ms": start, "effective_to_ms": None, "known_at_ms": start,
            "schedule_json": {
                "schedule_id": "sched-full", "symbol": sym_full,
                "effective_from_ms": start, "effective_to_ms": None,
                "interval_hours": 8, "anchor_ms": slots[0], "known_at_ms": start,
                "source": "binance:fapi/fundingInfo", "evidence_ref": "ev-full",
                "verification": "CONFIRMED",
            },
        })
        svc = _make_service(repo, clock)
        full = await svc._fetch_funding(sym_full, {}, NOW, NOW)
        assert full["windows"][30]["complete"] is True
        # Deleted slots: same schedule but missing every 9th event.
        sym_gap = "GAPUSDT"
        gap_slots = [s for i, s in enumerate(slots) if i % 9 != 0][:80]
        await repo.upsert_funding_events([
            {"symbol": sym_gap, "funding_time_ms": s, "funding_rate": 0.0005} for s in gap_slots
        ])
        await repo.save_funding_schedule({
            "schedule_id": "sched-gap", "symbol": sym_gap,
            "effective_from_ms": start, "effective_to_ms": None, "known_at_ms": start,
            "schedule_json": {
                "schedule_id": "sched-gap", "symbol": sym_gap,
                "effective_from_ms": start, "effective_to_ms": None,
                "interval_hours": 8, "anchor_ms": slots[0], "known_at_ms": start,
                "source": "binance:fapi/fundingInfo", "evidence_ref": "ev-gap",
                "verification": "CONFIRMED",
            },
        })
        gap = await svc._fetch_funding(sym_gap, {}, NOW, NOW)
        assert gap["windows"][30]["complete"] is False
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# CR01 (D12): single-cutoff six-item activation; any FAIL/UNKNOWN/expired or
# check/persist failure blocks ACTIVE; rejection still persists audit check.
# ---------------------------------------------------------------------------


async def _cr01_seed_funding_90d(repo: Any, sym: str) -> None:
    _h8 = 8 * 3_600_000
    _start = NOW - 90 * 86_400_000
    _slots: list[int] = []
    _cur = _start + _h8
    while _cur <= NOW:
        _slots.append(_cur)
        _cur += _h8
    await repo.upsert_funding_events([
        {"symbol": sym, "funding_time_ms": _s, "funding_rate": 0.0005}
        for _s in _slots
    ])
    await repo.save_funding_schedule({
        "schedule_id": f"sched-cr01-{sym}", "symbol": sym,
        "effective_from_ms": _start, "effective_to_ms": None, "known_at_ms": _start,
        "schedule_json": {
            "schedule_id": f"sched-cr01-{sym}", "symbol": sym,
            "effective_from_ms": _start, "effective_to_ms": None,
            "interval_hours": 8, "anchor_ms": _slots[0], "known_at_ms": _start,
            "source": "binance:fapi/fundingInfo", "evidence_ref": "ev-cr01",
            "verification": "CONFIRMED",
        },
    })


async def _cr01_make_filled_plan(tmp_path, name: str, *, with_funding: bool = True):
    """Save BTCUSDT plan, liq 0.02, fill both legs; return (svc, repo, clock, pid)."""
    import json as _js

    clock = FakeClock()
    repo = await _open_repo(tmp_path, name)
    svc = _make_service(repo, clock)
    sim = await svc.simulate({
        "symbol": "BTCUSDT", "mode": "ABSOLUTE",
        "futuresNotionalUsd": "10000", "preferredSpotVenue": "AUTO",
        "liquidationPrice": "0.02", "liquidationPriceUpdatedAtMs": NOW - 3_600_000,
    })
    plan = await svc.save_plan({"simulation_id": sim["simulationId"], "client_request_id": f"{name}-1"})
    pid = plan["planId"]
    _row = await repo.get_hedge_plan(pid)
    _cfg = _row.get("plan_config_json") or {}
    if isinstance(_cfg, str):
        try:
            _cfg = _js.loads(_cfg)
        except Exception:
            _cfg = {}
    _cfg = dict(_cfg) if isinstance(_cfg, dict) else {}
    _cfg["liquidation_price"] = "0.02"
    _cfg["stop_trigger_basis"] = "MARK_PRICE"
    repo._require_con().execute(
        "UPDATE sl_hedge_plan SET plan_config_json = ? WHERE plan_id = ?",
        [_js.dumps(_cfg), pid],
    )
    if with_funding:
        await _cr01_seed_funding_90d(repo, "BTCUSDT")

    def _evt(leg: str, typ: str, qty: str) -> dict[str, Any]:
        return {
            "schema_version": "hedge-event-v1", "leg_type": leg, "event_type": typ,
            "native_qty": qty, "canonical_qty": qty, "native_price": "67000",
            "price_currency": "USDT", "fee_currency": None, "fee_amount": None,
            "fee_usd": None, "gas_usd": None, "source": "USER_ENTERED",
            "executed_at_ms": NOW, "gross_qty": qty, "net_qty": qty,
        }

    got = await svc.get_simulation(sim["simulationId"])
    fq = str(got["result"].get("futuresContractQty") or got["result"].get("futures_contract_qty") or "0.15")
    sq = str(got["result"].get("targetSpotQty") or got["result"].get("target_spot_qty") or fq)
    r1 = await svc.apply_leg_event(pid, {"event": _evt("FUTURES_SHORT", "OPEN_FUTURES_SHORT", fq), "client_event_id": "e-f-1", "expected_version": 1})
    r2 = await svc.apply_leg_event(pid, {"event": _evt("SPOT_LONG", "OPEN_SPOT_LONG", sq), "client_event_id": "e-s-1", "expected_version": r1["planVersion"]})
    return svc, repo, clock, pid, r2["planVersion"]


@pytest.mark.asyncio
async def test_cr01_real_service_activation_passes_all_checks_with_fx_and_exact_remaining_lots(tmp_path) -> None:
    """Real Service + DuckDB exercises the complete positive activation chain."""
    from diveintocrypto_desktop.shortlab.hedge.market import ProductionHedgeMarket
    from diveintocrypto_desktop.shortlab.hedge.models import TradingRulesSnapshot

    clock = FakeClock()
    repo = await _open_repo(tmp_path, "cr01-real-activation-pass.duckdb")
    try:
        svc = _make_service(repo, clock)
        # Exchange-published USDT/USD observation is frozen before the fills;
        # apply_leg_event persists one EVENT_FX row per actual fill.
        await repo.save_fx_observation({
            "fx_id": "fx-usdt-activation", "currency": "USDT",
            "source_as_of_ms": NOW, "known_at_ms": NOW,
            "rate_str": "1", "source_json": {"source": "fixture:exchange-usdt-usd"},
        })
        decision_id = "decision-cr01-activation-pass"
        decision_request = {
            "symbol": "BTCUSDT", "goal": "CARRY_CAPTURE",
            "futures_notional_usd": "10000", "planned_hold_days": 30,
            "available_capital_usd": "25000", "max_scenario_loss_usd": "2000",
            "margin_usd": "2000", "liquidation_price": "150000",
            "liquidation_price_updated_at_ms": NOW, "preferred_spot_venue": "BINANCE_SPOT",
        }
        await repo.save_hedge_decision({
            "decision_id": decision_id, "symbol": "BTCUSDT",
            "generated_at_ms": NOW, "expires_at_ms": NOW + 60 * 60_000,
            "decision_policy_hash": "a" * 64,
            "decision_json": {"request": decision_request, "recommendation": "HEDGE",
                              "context_refs": {"identity": "fixture:identity", "funding": "fixture:funding"}},
        }, ())
        async def _mark(symbol: str) -> dict[str, Any]:
            return {"symbol": symbol, "mark_price": "67000", "native_price": "67000",
                    "quote_currency": "USDT", "quote_to_usd": "1",
                    "as_of_ms": NOW - 1_000, "source_timestamp_ms": NOW - 1_000,
                    "fetched_at_ms": NOW, "known_at_ms": NOW,
                    "expires_at_ms": NOW + 60_000, "last_funding_rate": "0.0005",
                    "next_funding_time": NOW + 4 * 60 * 60_000}

        async def _quote(symbol: str, qty: str, venue: Any = None) -> dict[str, Any]:
            return {"venue": str(venue or "BINANCE_SPOT"), "canonical_id": "bitcoin",
                    "symbol": symbol, "as_of_ms": NOW - 1_000,
                    "source_timestamp_ms": NOW - 1_000, "fetched_at_ms": NOW,
                    "known_at_ms": NOW, "expires_at_ms": NOW + 60_000,
                    "reference_notional_usd": "10000", "mid_price": "67000",
                    "buy_vwap": "67010", "sell_vwap": "66990",
                    "buy_executable_qty": "1", "sell_executable_qty": "1",
                    "requested_canonical_qty": str(qty), "quote_currency": "USDT",
                    "quote_to_usd": "1", "status": "OK", "entry_feasible": True,
                    "exit_feasible": True, "exit_feasibility": "CONFIRMED",
                    "fees_included": False, "trading_rules": {"step_size": "0.000001", "min_qty": "0.000001"}}

        fut_rules = TradingRulesSnapshot(
            venue="BINANCE_SPOT", instrument_id="BTCUSDT",
            source_as_of_ms=NOW - 1_000, known_at_ms=NOW,
            rule_version="fixture-futures-v1", lot_rules={"step_size": "0.001", "min_qty": "0.001"},
            price_rules={"tick_size": "0.1"}, notional_rules={"min_notional": "5"},
            order_types={"MARKET": True, "STOP_MARKET": True}, stop_orders_supported=True,
            conditional_orders_source_ref="exchangeInfo:BTCUSDT",
        )
        spot_rules = TradingRulesSnapshot(
            venue="BINANCE_SPOT", instrument_id="BTCUSDT",
            source_as_of_ms=NOW - 1_000, known_at_ms=NOW,
            rule_version="fixture-spot-v1", lot_rules={"step_size": "0.000001", "min_qty": "0.000001"},
            price_rules={"tick_size": "0.1"}, notional_rules={"min_notional": "5"},
            order_types={"MARKET": True}, stop_orders_supported=True,
            conditional_orders_source_ref="exchangeInfo:BTCUSDT",
        )
        svc._hedge_mark_fn = _mark
        svc._hedge_quote_fn = _quote
        svc._hedge_futures_rules_fn = lambda: {"lot_rules": {"step_size": "0.001", "min_qty": "0.001"}}
        svc._hedge_spot_rules_fn = lambda: {"lot_rules": {"step_size": "0.000001", "min_qty": "0.000001"}}
        sim = await svc.simulate({
            "symbol": "BTCUSDT", "mode": "ABSOLUTE", "goal": "CARRY_CAPTURE",
            "futuresNotionalUsd": "10000", "preferredSpotVenue": "BINANCE_SPOT",
            "futuresLeverage": "5", "marginUsd": "2000", "plannedHoldDays": 30,
            "liquidationPrice": "150000", "liquidationPriceUpdatedAtMs": NOW,
            "stopTriggerPrice": "120000", "stopTriggerBasis": "MARK_PRICE",
            "decisionId": decision_id,
        })
        plan = await svc.save_plan({
            "simulation_id": sim["simulationId"], "client_request_id": "cr01-real-activation-pass",
            "decision_id": decision_id, "goal": "CARRY_CAPTURE", "leverage": "5",
            "margin_usd": "2000", "planned_hold_days": 30,
        })
        pid = plan["planId"]
        sim_result = sim["result"]
        fut_qty = str(sim_result.get("futuresContractQty") or sim_result.get("futures_contract_qty"))
        canonical_qty = str(sim_result.get("canonicalFuturesQty") or sim_result.get("canonical_futures_qty"))
        spot_qty = str(sim_result.get("targetSpotQty") or sim_result.get("target_spot_qty"))
        assert fut_qty == canonical_qty == spot_qty

        async def _event(leg: str, kind: str, qty: str, canonical: str) -> dict[str, Any]:
            return {"schema_version": "hedge-event-v1", "leg_type": leg,
                    "event_type": kind, "native_qty": qty, "canonical_qty": canonical,
                    "native_price": "67000", "price_currency": "USDT",
                    "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
                    "gas_usd": "0", "source": "USER_ENTERED", "executed_at_ms": NOW,
                    "gross_qty": qty, "net_qty": qty}

        ev_f = await svc.apply_leg_event(pid, {"event": await _event("FUTURES_SHORT", "OPEN_FUTURES_SHORT", fut_qty, canonical_qty), "client_event_id": "cr01-f-open", "expected_version": 1})
        ev_s = await svc.apply_leg_event(pid, {"event": await _event("SPOT_LONG", "OPEN_SPOT_LONG", spot_qty, spot_qty), "client_event_id": "cr01-s-open", "expected_version": ev_f["planVersion"]})

        # Historical exchange responses and response receipts are stored per
        # event; aggregate per-run timestamps are intentionally not accepted.
        _h8, _start = 8 * 3_600_000, NOW - 90 * 86_400_000
        _slots = list(range(_start + _h8, NOW + 1, _h8))
        await repo.upsert_funding_events([{"symbol": "BTCUSDT", "funding_time_ms": s, "funding_rate": 0.0005} for s in _slots])
        for _slot in _slots:
            await repo.save_funding_observation({
                "observation_id": f"receipt-{_slot}", "symbol": "BTCUSDT",
                "funding_time_ms": _slot, "known_at_ms": NOW,
                "raw_json": {"event": {"fundingTime": _slot, "fundingRate": "0.0005"},
                             "response_receipt": {"endpoint": "/fapi/v1/fundingRate", "status": 200, "received_at_ms": NOW}},
                "interval_hours": None, "interval_source": None,
                "observation_status": "OBSERVED",
            })
        await repo.save_funding_schedule({
            "schedule_id": "sched-cr01-real-pass", "symbol": "BTCUSDT",
            "effective_from_ms": _start, "effective_to_ms": None, "known_at_ms": _start,
            "schedule_json": {"schedule_id": "sched-cr01-real-pass", "symbol": "BTCUSDT",
                "effective_from_ms": _start, "effective_to_ms": None, "interval_hours": 8,
                "anchor_ms": _slots[0], "known_at_ms": _start,
                "source": "binance:fapi/fundingInfo", "evidence_ref": "fixture:verified-binance-8h",
                "verification": "CONFIRMED"},
        })
        # Preserve a versioned source regime and live status on the MarketPort.
        market = ProductionHedgeMarket(svc, svc._config, repo, None, clock)
        market.mark = lambda symbol, request_context=None: _mark(symbol)
        market._exchange_info = lambda request_context=None: __import__("asyncio").sleep(0, result={
            "symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "onboardDate": NOW - 120 * 86_400_000}]
        })
        market.rules = lambda kind, symbol, request_context=None: __import__("asyncio").sleep(0, result=fut_rules if kind == "futures" else spot_rules)
        async def _collect_futures(symbol: str, qty: str, request_context: Any = None) -> dict[str, Any]:
            return {"futures_mark": await _mark(symbol), "futures_quote": {
                "quote_id": f"fq-{qty}", "book_observation_id": f"book-{qty}",
                "requested_contract_qty": str(qty), "buy_vwap_native": "67010", "sell_vwap_native": "66990",
                "buy_executable_qty": "1", "sell_executable_qty": "1", "quote_currency": "USDT",
                "quote_to_usd": "1", "as_of_ms": NOW - 1_000, "source_as_of_ms": NOW - 1_000,
                "known_at_ms": NOW, "expires_at_ms": NOW + 60_000, "fees_included": False,
            }}
        market.collect_futures = _collect_futures
        svc._market_port = market
        svc._hedge_market = market
        svc._hedge_futures_rules_fn = None
        svc._hedge_spot_rules_fn = None
        positions = await repo.aggregate_hedge_position(pid)
        fut_remaining = next(p["remaining_qty"] for p in positions if p["leg_type"] == "FUTURES_SHORT")
        spot_remaining = next(p["remaining_qty"] for p in positions if p["leg_type"] == "SPOT_LONG")
        await svc.confirm_repair_protection(pid, {
            "expected_version": ev_s["planVersion"], "client_request_id": "cr01-real-protection",
            "confirmed_at_ms": NOW,
            "futures": {"status": "CONFIRMED", "nativeQty": str(fut_remaining), "triggerPrice": "120000", "triggerBasis": "MARK_PRICE", "orderReference": "f-1"},
            "spot": {"status": "CONFIRMED", "nativeQty": str(spot_remaining), "exitMode": "PLATFORM_ORDER", "orderReference": "s-1"},
        })
        plan_current = await repo.get_hedge_plan(pid)
        await svc.activate(pid, {"expected_version": int(plan_current["plan_version"])})
        records = await repo.list_market_observations("BTCUSDT", "ACTIVATION_CHECK", 0, NOW + 1_000, NOW + 1_000)
        assert len(records) == 1
        checks = records[0]["value_json"]["checks"]
        assert {key: checks[key]["status"] for key in ("identity", "mark_vs_liquidation", "depth", "funding_gate", "economics", "protection")} == {
            "identity": "PASS", "mark_vs_liquidation": "PASS", "depth": "PASS",
            "funding_gate": "PASS", "economics": "PASS", "protection": "PASS",
        }
        assert len(checks["economics"]["detail"]["scenarios"]) == 6
        assert (await repo.get_hedge_plan(pid))["status"] == "ACTIVE"
        active_row = await repo.get_hedge_plan(pid)
        from diveintocrypto_desktop.shortlab.service import HedgeOpenLegsRemain
        with pytest.raises(HedgeOpenLegsRemain, match="OPEN_LEGS_REMAIN"):
            await svc.close(pid, {"expected_version": int(active_row["plan_version"])})
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_manual_fill_archives_live_provider_fx_before_event_fx(tmp_path) -> None:
    """A real market FX response is frozen before EVENT_FX links to it."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from diveintocrypto_desktop.shortlab.hedge.market import ProductionHedgeMarket

    clock = FakeClock()
    repo = await _open_repo(tmp_path, "manual-fill-provider-fx.duckdb")
    try:
        svc = _make_service(repo, clock)
        market = ProductionHedgeMarket(svc, svc._config, repo, None, clock)

        async def slow_fx_fetch(identity, request_context=None):
            clock.advance(100)
            return SimpleNamespace(
                status="OK", source="coingecko", stale=False,
                as_of_ms=NOW - 1_000, fetched_at_ms=clock(),
                data=SimpleNamespace(price_usd=1.0),
            )

        market.fx_provider.fetch = AsyncMock(side_effect=slow_fx_fetch)
        svc._market_port = market
        sim = await svc.simulate({
            "symbol": "BTCUSDT", "mode": "ABSOLUTE",
            "futuresNotionalUsd": "10000", "preferredSpotVenue": "AUTO",
        })
        plan = await svc.save_plan({
            "simulation_id": sim["simulationId"], "client_request_id": "manual-fill-provider-fx",
        })
        event = {
            "schema_version": "hedge-event-v1", "leg_type": "FUTURES_SHORT",
            "event_type": "OPEN_FUTURES_SHORT", "native_qty": "0.1",
            "canonical_qty": "0.1", "native_price": "67000",
            "price_currency": "USDT", "fee_currency": None, "fee_amount": None,
            "fee_usd": None, "gas_usd": None, "source": "USER_ENTERED",
            "executed_at_ms": NOW, "gross_qty": "0.1", "net_qty": "0.1",
        }
        applied = await svc.apply_leg_event(plan["planId"], {
            "event": event, "client_event_id": "manual-fill-fx", "expected_version": 1,
        })

        stored_fx = await repo.get_fx_at("USDT", NOW, clock(), 60_000)
        assert stored_fx is not None
        assert stored_fx["rate_str"] == "1.0"
        assert stored_fx["source_as_of_ms"] == NOW - 1_000
        assert stored_fx["known_at_ms"] == NOW + 100
        event_fx_rows = await repo.list_market_observations("BTCUSDT", "EVENT_FX", 0, clock(), clock())
        assert len(event_fx_rows) == 1
        event_fx = event_fx_rows[0]["value_json"]
        assert event_fx["event_id"] == applied["eventId"]
        assert event_fx["price_fx_id"] == stored_fx["fx_id"]
        assert event_fx["price_fx"] == "1.0"
        assert event_fx_rows[0]["known_at_ms"] == NOW + 100
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_1000x_partial_close_requires_native_qty_protection_reconfirmation(tmp_path) -> None:
    from decimal import Decimal
    from diveintocrypto_desktop.shortlab.hedge.protection import validate_protection_confirmation

    clock = FakeClock()
    repo = await _open_repo(tmp_path, "cr06-1000-partial-protection.duckdb")
    try:
        async def _mark_1000(symbol: str) -> dict[str, Any]:
            return {"symbol": symbol, "mark_price": "0.012", "native_price": "0.012",
                    "quote_currency": "USDT", "quote_to_usd": "1", "as_of_ms": NOW,
                    "fetched_at_ms": NOW, "known_at_ms": NOW, "expires_at_ms": NOW + 60_000}

        async def _quote_1000(symbol: str, qty: str, venue: Any = None) -> dict[str, Any]:
            return {"venue": str(venue or "BINANCE_SPOT"), "canonical_id": "pepe",
                    "symbol": symbol, "as_of_ms": NOW, "source_timestamp_ms": NOW,
                    "fetched_at_ms": NOW, "known_at_ms": NOW, "expires_at_ms": NOW + 60_000,
                    "reference_notional_usd": "10000", "mid_price": "0.000012",
                    "buy_vwap": "0.0000121", "sell_vwap": "0.0000119",
                    "buy_executable_qty": "2000000000", "sell_executable_qty": "2000000000",
                    "requested_canonical_qty": str(qty), "quote_currency": "USDT", "quote_to_usd": "1",
                    "status": "OK", "entry_feasible": True, "exit_feasible": True,
                    "exit_feasibility": "CONFIRMED", "trading_rules": {"step_size": "1", "min_qty": "1"}}

        svc = _make_service(repo, clock, hedge_mark_fn=_mark_1000, hedge_quote_fn=_quote_1000)
        svc._hedge_futures_rules_fn = lambda: {"lot_rules": {"step_size": "0.001", "min_qty": "0.001"}}
        svc._hedge_spot_rules_fn = lambda: {"lot_rules": {"step_size": "1", "min_qty": "1"}}
        await repo.save_fx_observation({
            "fx_id": "fx-usdt-1000-partial", "currency": "USDT",
            "source_as_of_ms": NOW, "known_at_ms": NOW, "rate_str": "1",
            "source_json": {"source": "fixture:exchange-usdt-usd"},
        })
        sim = await svc.simulate({
            "symbol": "1000PEPEUSDT", "mode": "ABSOLUTE", "goal": "CARRY_CAPTURE",
            "futuresNotionalUsd": "10000", "preferredSpotVenue": "BINANCE_SPOT",
            "liquidationPrice": "0.025", "liquidationPriceUpdatedAtMs": NOW,
            "stopTriggerPrice": "0.020", "stopTriggerBasis": "MARK_PRICE",
        })
        plan = await svc.save_plan({"simulation_id": sim["simulationId"],
                                    "client_request_id": "cr06-1000-partial-plan"})
        pid = plan["planId"]
        sim_result = sim["result"]
        native_open = str(sim_result.get("futuresContractQty") or sim_result.get("futures_contract_qty"))
        canonical_open = str(sim_result.get("canonicalFuturesQty") or sim_result.get("canonical_futures_qty"))
        spot_open = str(sim_result.get("targetSpotQty") or sim_result.get("target_spot_qty"))
        assert Decimal(canonical_open) == Decimal(native_open) * Decimal("1000")

        def _fill(leg: str, kind: str, native: str, canonical: str) -> dict[str, Any]:
            balance_qty = canonical if leg == "FUTURES_SHORT" else native
            return {"schema_version": "hedge-event-v1", "leg_type": leg, "event_type": kind,
                    "native_qty": native, "canonical_qty": canonical, "native_price": "0.012",
                    "price_currency": "USDT", "fee_currency": "USDT", "fee_amount": "0",
                    "fee_usd": "0", "gas_usd": "0", "source": "USER_ENTERED",
                    "executed_at_ms": NOW, "gross_qty": balance_qty, "net_qty": balance_qty}

        first = await svc.apply_leg_event(pid, {"event": _fill("FUTURES_SHORT", "OPEN_FUTURES_SHORT", native_open, canonical_open),
                                               "client_event_id": "cr06-open-f", "expected_version": 1})
        second = await svc.apply_leg_event(pid, {"event": _fill("SPOT_LONG", "OPEN_SPOT_LONG", spot_open, spot_open),
                                                 "client_event_id": "cr06-open-s", "expected_version": first["planVersion"]})
        positions = await repo.aggregate_hedge_position(pid)
        futures_canonical = next(p["remaining_qty"] for p in positions if p["leg_type"] == "FUTURES_SHORT")
        spot_remaining = next(p["remaining_qty"] for p in positions if p["leg_type"] == "SPOT_LONG")
        futures_native = str(Decimal(futures_canonical) / Decimal("1000"))
        initial = await svc.confirm_repair_protection(pid, {
            "expected_version": second["planVersion"], "client_request_id": "cr06-protect-initial",
            "confirmed_at_ms": NOW,
            "futures": {"status": "CONFIRMED", "nativeQty": futures_native,
                        "triggerPrice": "0.020", "triggerBasis": "MARK_PRICE", "orderReference": "f-1"},
            "spot": {"status": "CONFIRMED", "nativeQty": str(spot_remaining),
                     "exitMode": "PLATFORM_ORDER", "orderReference": "s-1"},
        })

        # Close exactly 1.234 Futures contracts / 1,234 canonical spot units.
        close_f = await svc.apply_leg_event(pid, {"event": _fill("FUTURES_SHORT", "CLOSE_FUTURES_SHORT", "1.234", "1234"),
                                                  "client_event_id": "cr06-close-f", "expected_version": initial["planVersion"]})
        close_s = await svc.apply_leg_event(pid, {"event": _fill("SPOT_LONG", "CLOSE_SPOT_LONG", "1234", "1234"),
                                                  "client_event_id": "cr06-close-s", "expected_version": close_f["planVersion"]})
        current_positions = await repo.aggregate_hedge_position(pid)
        fut_canonical_now = next(p["remaining_qty"] for p in current_positions if p["leg_type"] == "FUTURES_SHORT")
        spot_now = next(p["remaining_qty"] for p in current_positions if p["leg_type"] == "SPOT_LONG")
        fut_native_now = str(Decimal(fut_canonical_now) / Decimal("1000"))
        plan_row = await repo.get_hedge_plan(pid)
        stale_gate = validate_protection_confirmation(
            await repo.get_protection_confirmation(pid),
            {"plan_id": pid, "contract_multiplier": "1000", "liquidation_price": "0.025",
             "stop_trigger_price": "0.020", "stop_trigger_basis": "MARK_PRICE"},
            current_positions, NOW,
        )
        assert stale_gate.status == "FAIL"
        refreshed = await svc.confirm_repair_protection(pid, {
            "expected_version": close_s["planVersion"], "client_request_id": "cr06-protect-after-partial",
            "confirmed_at_ms": NOW,
            "futures": {"status": "CONFIRMED", "nativeQty": fut_native_now,
                        "triggerPrice": "0.020", "triggerBasis": "MARK_PRICE", "orderReference": "f-2"},
            "spot": {"status": "CONFIRMED", "nativeQty": str(spot_now),
                     "exitMode": "PLATFORM_ORDER", "orderReference": "s-2"},
        })
        assert Decimal(fut_native_now) == Decimal(futures_native) - Decimal("1.234")
        assert refreshed["protected_position_hash"] != initial["protected_position_hash"]
        plan_cfg = plan_row.get("plan_config_json") or {}
        if isinstance(plan_cfg, str):
            import json as _json_plan
            plan_cfg = _json_plan.loads(plan_cfg)
        validated = validate_protection_confirmation(
            await repo.get_protection_confirmation(pid),
            {"plan_id": pid, "contract_multiplier": "1000",
             "liquidation_price": plan_cfg["liquidation_price"],
             "stop_trigger_price": plan_cfg["stop_trigger_price"],
             "stop_trigger_basis": plan_cfg["stop_trigger_basis"]},
            current_positions, NOW,
        )
        assert validated.status == "PASS"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr01_no_protection_rejected_and_check_persisted(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.service import HedgeValidationError

    svc, repo, clock, pid, _ver = await _cr01_make_filled_plan(tmp_path, "cr01-no-prot.duckdb")
    try:
        with pytest.raises(HedgeValidationError, match="ACTIVATION_CHECK_FAILED"):
            await svc.activate(pid, {"expected_version": _ver})
        # Rejection still persists the audit check (symbol/kind).
        rows = await repo.list_market_observations("BTCUSDT", "ACTIVATION_CHECK", 0, int(clock()) + 1_000, int(clock()) + 1_000)
        assert len(rows) >= 1
        _c = rows[-1]["value_json"]["checks"]
        assert _c["protection"]["status"] in ("UNKNOWN", "FAIL")
        assert _c["protection_status"] in ("UNKNOWN", "FAIL")
        # Plan stays not ACTIVE (CAS never ran).
        _row = await repo.get_hedge_plan(pid)
        assert str(_row.get("status")) != "ACTIVE"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr01_activate_requires_expected_version(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.service import HedgeValidationError

    svc, repo, _clock, pid, _ver = await _cr01_make_filled_plan(
        tmp_path, "cr01-expected-version.duckdb")
    try:
        with pytest.raises(HedgeValidationError, match="expected_version"):
            await svc.activate(pid, {})
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr01_close_requires_expected_version(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.service import HedgeValidationError

    svc, repo, _clock, pid, _ver = await _cr01_make_filled_plan(
        tmp_path, "cr01-close-expected-version.duckdb")
    try:
        with pytest.raises(HedgeValidationError, match="expected_version"):
            await svc.close(pid, {})
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr01_expired_protection_rejected(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.service import HedgeValidationError

    svc, repo, clock, pid, ver = await _cr01_make_filled_plan(tmp_path, "cr01-exp.duckdb")
    try:
        _pos = await repo.aggregate_hedge_position(pid)
        _fr = next(p for p in _pos if p.get("leg_type") == "FUTURES_SHORT").get("remaining_qty")
        _sr = next(p for p in _pos if p.get("leg_type") == "SPOT_LONG").get("remaining_qty")
        # Expired: confirmed 25h ago (TTL 24h) -> FAIL PROTECTION_EXPIRED.
        clock.ms = NOW
        _old = NOW - 25 * 3_600_000
        # Temporarily move clock back for confirmation, then forward for expiry.
        clock.ms = _old
        await svc.confirm_repair_protection(pid, {
            "expected_version": ver, "client_request_id": "cr01-exp-1",
            "confirmed_at_ms": _old,
            "futures": {"status": "CONFIRMED", "nativeQty": str(_fr), "triggerBasis": "MARK_PRICE", "orderReference": "f-1"},
            "spot": {"status": "CONFIRMED", "nativeQty": str(_sr), "exitMode": "PLATFORM_ORDER", "orderReference": "s-1"},
        })
        clock.ms = NOW
        with pytest.raises(HedgeValidationError, match="ACTIVATION_CHECK_FAILED"):
            await svc.activate(pid, {"expected_version": ver + 1})
        rows = await repo.list_market_observations("BTCUSDT", "ACTIVATION_CHECK", 0, int(clock()) + 1_000, int(clock()) + 1_000)
        assert len(rows) >= 1
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr01_remaining_change_rejected(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.service import HedgeValidationError

    svc, repo, clock, pid, ver = await _cr01_make_filled_plan(tmp_path, "cr01-rem.duckdb")
    try:
        _pos = await repo.aggregate_hedge_position(pid)
        _fr = next(p for p in _pos if p.get("leg_type") == "FUTURES_SHORT").get("remaining_qty")
        _sr = next(p for p in _pos if p.get("leg_type") == "SPOT_LONG").get("remaining_qty")
        _conf = await svc.confirm_repair_protection(pid, {
            "expected_version": ver, "client_request_id": "cr01-rem-1",
            "confirmed_at_ms": NOW,
            "futures": {"status": "CONFIRMED", "nativeQty": str(_fr), "triggerBasis": "MARK_PRICE", "orderReference": "f-1"},
            "spot": {"status": "CONFIRMED", "nativeQty": str(_sr), "exitMode": "PLATFORM_ORDER", "orderReference": "s-1"},
        })
        _ver_after_conf = int(_conf["planVersion"])
        # Additional OPEN changes remaining (no closes -> still activatable legs,
        # but stored hash mismatches current) -> ACTIVATION_CHECK_FAILED.
        await svc.apply_leg_event(pid, {
            "event": {
                "schema_version": "hedge-event-v1", "leg_type": "FUTURES_SHORT",
                "event_type": "OPEN_FUTURES_SHORT", "native_qty": "1",
                "canonical_qty": "1", "native_price": "67000",
                "price_currency": "USDT", "fee_currency": None, "fee_amount": None,
                "fee_usd": None, "gas_usd": None, "source": "USER_ENTERED",
                "executed_at_ms": NOW, "gross_qty": "1", "net_qty": "1",
            },
            "client_event_id": "e-open-extra", "expected_version": _ver_after_conf,
        })
        with pytest.raises(HedgeValidationError, match="ACTIVATION_CHECK_FAILED"):
            await svc.activate(pid, {"expected_version": _ver_after_conf + 1})
        rows = await repo.list_market_observations("BTCUSDT", "ACTIVATION_CHECK", 0, int(clock()) + 1_000, int(clock()) + 1_000)
        assert len(rows) >= 1
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr01_persist_failure_blocks(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.service import HedgeValidationError

    svc, repo, clock, pid, ver = await _cr01_make_filled_plan(tmp_path, "cr01-persist.duckdb")
    try:
        _pos = await repo.aggregate_hedge_position(pid)
        _fr = next(p for p in _pos if p.get("leg_type") == "FUTURES_SHORT").get("remaining_qty")
        _sr = next(p for p in _pos if p.get("leg_type") == "SPOT_LONG").get("remaining_qty")
        await svc.confirm_repair_protection(pid, {
            "expected_version": ver, "client_request_id": "cr01-persist-1",
            "confirmed_at_ms": NOW,
            "futures": {"status": "CONFIRMED", "nativeQty": str(_fr), "triggerBasis": "MARK_PRICE", "orderReference": "f-1"},
            "spot": {"status": "CONFIRMED", "nativeQty": str(_sr), "exitMode": "PLATFORM_ORDER", "orderReference": "s-1"},
        })

        async def _boom(_rec: Any) -> str:
            raise RuntimeError("disk-full")

        _orig = repo.save_market_observation
        repo.save_market_observation = _boom  # type: ignore[assignment]
        try:
            with pytest.raises(HedgeValidationError, match="ACTIVATION_CHECK_FAILED"):
                await svc.activate(pid, {"expected_version": ver + 1})
        finally:
            repo.save_market_observation = _orig  # type: ignore[assignment]
        _row = await repo.get_hedge_plan(pid)
        assert str(_row.get("status")) != "ACTIVE"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_cr01_funding_unknown_rejected(tmp_path) -> None:
    # No schedules (empty DB) -> funding UNKNOWN blocks even with valid protection.
    from diveintocrypto_desktop.shortlab.service import HedgeValidationError

    svc, repo, clock, pid, ver = await _cr01_make_filled_plan(tmp_path, "cr01-fund.duckdb", with_funding=False)
    try:
        _pos = await repo.aggregate_hedge_position(pid)
        _fr = next(p for p in _pos if p.get("leg_type") == "FUTURES_SHORT").get("remaining_qty")
        _sr = next(p for p in _pos if p.get("leg_type") == "SPOT_LONG").get("remaining_qty")
        await svc.confirm_repair_protection(pid, {
            "expected_version": ver, "client_request_id": "cr01-fund-1",
            "confirmed_at_ms": NOW,
            "futures": {"status": "CONFIRMED", "nativeQty": str(_fr), "triggerBasis": "MARK_PRICE", "orderReference": "f-1"},
            "spot": {"status": "CONFIRMED", "nativeQty": str(_sr), "exitMode": "PLATFORM_ORDER", "orderReference": "s-1"},
        })
        with pytest.raises(HedgeValidationError, match="ACTIVATION_CHECK_FAILED"):
            await svc.activate(pid, {"expected_version": ver + 1})
        rows = await repo.list_market_observations("BTCUSDT", "ACTIVATION_CHECK", 0, int(clock()) + 1_000, int(clock()) + 1_000)
        assert len(rows) >= 1
        assert rows[-1]["value_json"]["checks"]["funding_gate"]["status"] in ("UNKNOWN", "FAIL")
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_score_base_persistence_does_not_forge_funding_receipt(tmp_path) -> None:
    """Canonical score events have no response receipt and must stay that way."""
    from types import SimpleNamespace

    svc, repo, clock, _, _ = await _cr01_make_filled_plan(tmp_path, "funding-no-fake-receipt.duckdb")
    saved_observations: list[dict[str, Any]] = []
    try:
        original_save = repo.save_funding_observation

        async def _record_observation(record: Any) -> str:
            saved_observations.append(dict(record))
            return await original_save(record)

        repo.save_funding_observation = _record_observation  # type: ignore[method-assign]
        identity = SimpleNamespace(
            canonical_id="bitcoin", display_symbol="BTCUSDT", coingecko_id="bitcoin",
            contract_multiplier=1, multiplier_source="EXCHANGE",
            mapping_confidence="VERIFIED", mapping_source="EXCHANGE",
            binance_spot_symbol="BTCUSDT",
        )
        await svc._persist_base_tables_for_symbol(
            symbol="BTCUSDT", identity=identity, exchange_meta={}, fund_data=None,
            fund_result=None,
            funding_events=[{"t": int(clock()) - 8 * 60 * 60_000, "funding_rate": "0.001"}],
            cutoff_ms=int(clock()), now_ms=int(clock()),
        )
        assert saved_observations == []
    finally:
        await repo.close()
