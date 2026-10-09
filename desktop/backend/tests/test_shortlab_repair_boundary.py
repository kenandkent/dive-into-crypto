"""R10a boundary: unbound 503 + old readonly + Fake isolation (D02/D12/D18/D19.1).

- New repair routes default to 503 IMPLEMENTATION_UNAVAILABLE when
  repair_ports unbound (no Fake READY in production).
- Old archive reads (simulate/get_simulation) do not depend on new ports.
- Test Fakes live only in tests (production never imports repair_fixtures).
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _valid_decision_body() -> dict:
    # camelCase wire shape (D18.3); skeleton accepts any object but the
    # valid case must 503, not 422.
    return {
        "symbol": "1000PEPEUSDT",
        "goal": "CARRY_CAPTURE",
        "futuresNotionalUsd": "10000",
        "plannedHoldDays": 30,
        "availableCapitalUsd": "25000",
        "maxScenarioLossUsd": "1000",
        "marginUsd": "12000",
        "liquidationPrice": "0.025",
        "liquidationPriceUpdatedAtMs": 1791414000000,
        "preferredSpotVenue": "AUTO",
    }


def _make_unbound_app(tmp_path: Path):
    """Build app via the frozen factory seam with explicitly unbound ports."""
    from diveintocrypto_desktop.api.app import create_app
    from diveintocrypto_desktop.shortlab.repair_ports import RepairPorts
    from diveintocrypto_desktop.shortlab.runtime import ShortLabRuntime

    # Explicitly unbound: empty RepairPorts + allow_test_bindings=False.
    # Must still 503 (D19.6), never Fake READY.
    def factory():
        return ShortLabRuntime(
            db_path=tmp_path / "r10a-boundary.duckdb",
            repair_ports=RepairPorts(),
            allow_test_bindings=False,
        )

    app = create_app(shortlab_runtime_factory=factory)
    return app


def test_unbound_decisions_post_503(tmp_path) -> None:
    app = _make_unbound_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/short/hedge/decisions", json=_valid_decision_body())
        assert response.status_code == 503
        assert response.json()["reasonCode"] == "IMPLEMENTATION_UNAVAILABLE"


def test_unbound_decisions_get_503(tmp_path) -> None:
    app = _make_unbound_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/short/hedge/decisions/dec-missing")
        assert response.status_code == 503
        assert response.json()["reasonCode"] == "IMPLEMENTATION_UNAVAILABLE"


def test_unbound_exit_guidance_503(tmp_path) -> None:
    app = _make_unbound_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/short/hedge/plans/plan-missing/exit-guidance")
        assert response.status_code == 503
        assert response.json()["reasonCode"] == "IMPLEMENTATION_UNAVAILABLE"


def test_unbound_protection_503(tmp_path) -> None:
    app = _make_unbound_app(tmp_path)
    body = {
        "expectedVersion": 1,
        "clientRequestId": "req-1",
        "confirmedAtMs": 1791417600000,
        "futures": {
            "orderReference": "f-1",
            "nativeQty": "10",
            "triggerPrice": "0.01",
            "triggerBasis": "MARK",
            "status": "CONFIRMED",
        },
        "spot": {"exitMode": "MANUAL_EXIT_ONLY", "nativeQty": "10", "status": "CONFIRMED"},
    }
    with TestClient(app) as client:
        response = client.post("/api/short/hedge/plans/plan-missing/protection", json=body)
        assert response.status_code == 503
        assert response.json()["reasonCode"] == "IMPLEMENTATION_UNAVAILABLE"


def test_capabilities_route_frozen(tmp_path) -> None:
    """Capabilities skeleton is frozen (route + DTO alias exist, no secrets)."""
    app = _make_unbound_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/short/capabilities")
        assert response.status_code == 200
        body = response.json()
        # DTO alias frozen: camelCase contract version present.
        assert body.get("contractSchemaVersion") == "repair-contract-v1"
        assert "readiness" in body or "capabilities" in body
        text = response.text
        assert "api_key" not in text.lower()


def test_old_simulation_readonly_without_new_ports(tmp_path) -> None:
    """Old archive reads must not depend on new ports (D19.1 compat window)."""
    import dataclasses as _dc

    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    base = load_shortlab_config()
    try:
        hedge_cfg = _dc.replace(base.hedge, enabled=True)
        config = _dc.replace(base, hedge=hedge_cfg)
    except Exception:
        config = base
    from diveintocrypto_desktop.api.app import create_app
    from diveintocrypto_desktop.shortlab.repair_ports import RepairPorts
    from diveintocrypto_desktop.shortlab.runtime import ShortLabRuntime

    def factory():
        return ShortLabRuntime(
            config=config,
            db_path=tmp_path / "r10a-old-read.duckdb",
            repair_ports=RepairPorts(),
            allow_test_bindings=False,
        )

    app = create_app(shortlab_runtime_factory=factory)
    with TestClient(app) as client:
        # Missing simulation is 404 (archive read), never 503 IMPLEMENTATION_UNAVAILABLE.
        missing = client.get("/api/short/hedge/simulations/sim-missing-r10a")
        assert missing.status_code == 404
        assert missing.json().get("reasonCode") != "IMPLEMENTATION_UNAVAILABLE"
        # New decisions still 503 on the same unbound runtime.
        fresh = client.post("/api/short/hedge/decisions", json=_valid_decision_body())
        assert fresh.status_code == 503
        assert fresh.json()["reasonCode"] == "IMPLEMENTATION_UNAVAILABLE"


def test_fake_only_in_tests() -> None:
    """Production modules must never import tests/repair_fixtures (D19.6)."""
    import pathlib

    root = Path(__file__).resolve().parents[1] / "src" / "diveintocrypto_desktop"
    prod_files = [
        root / "shortlab" / "service.py",
        root / "shortlab" / "runtime.py",
        root / "api" / "app.py",
        root / "api" / "shortlab.py",
        root / "api" / "shortlab_repair.py",
    ]
    for path in prod_files:
        assert path.exists(), f"missing production file {path}"
        text = path.read_text(encoding="utf-8")
        assert "repair_fixtures" not in text, f"{path.name} must not import test Fake"
        assert "from tests" not in text and "import tests" not in text, (
            f"{path.name} must not import tests"
        )


def test_factory_and_injection_seams_frozen() -> None:
    """create_app factory + Runtime/Service explicit seams (D19.1)."""
    from diveintocrypto_desktop.api.app import create_app
    from diveintocrypto_desktop.shortlab import runtime as runtime_mod
    from diveintocrypto_desktop.shortlab import service as service_mod

    # create_app única新增工厂缝 (keyword-only, default None, no other params).
    sig = inspect.signature(create_app)
    params = list(sig.parameters.values())
    assert len(params) == 1, f"create_app must take exactly one param, got {sig}"
    param = params[0]
    assert param.name == "shortlab_runtime_factory"
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None

    # Runtime incremental keywords (defaults None/False, old params unchanged).
    runtime_sig = inspect.signature(runtime_mod.ShortLabRuntime.__init__)
    assert "repair_ports" in runtime_sig.parameters
    assert "allow_test_bindings" in runtime_sig.parameters
    assert runtime_sig.parameters["repair_ports"].default is None
    assert runtime_sig.parameters["allow_test_bindings"].default is False

    # Service explicit injection seams.
    service_sig = inspect.signature(service_mod.ShortLabService.__init__)
    for name in ("repair_ports", "repository_port", "market_port"):
        assert name in service_sig.parameters, f"service missing {name} seam"
        assert service_sig.parameters[name].default is None
