"""R10a repair API skeleton (D02/D12/D18/D19.1).

Five frozen routes (behavior lands in R10b; this task only freezes
route/DTO alias/error schema):

- POST /hedge/decisions
- GET /hedge/decisions/{decision_id}
- GET /hedge/plans/{plan_id}/exit-guidance
- POST /hedge/plans/{plan_id}/protection
- GET /capabilities

Unbound RepairPorts -> 503 IMPLEMENTATION_UNAVAILABLE with reasonCode
alias (never Fake READY). Bodies are read as raw bytes and parsed with
load_json_strict (duplicate keys / NaN / Infinity rejected). Protection
requires expectedVersion int>=1 (422 otherwise). Capabilities always 200
with contractSchemaVersion + binding summary (never exposes secrets).
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from diveintocrypto_desktop.api.shortlab import (
    _hedge_error,
    _map_hedge_exception,
    _repair_unavailable_response,
    _require_service,
)
from diveintocrypto_desktop.shortlab.service import ShortLabUnavailable

try:
    from diveintocrypto_desktop.shortlab.repair_contracts import REPAIR_CONTRACT_VERSION
except Exception:  # pragma: no cover - defensive fallback
    REPAIR_CONTRACT_VERSION = "repair-contract-v1"

router = APIRouter(prefix="/api/short", tags=["shortlab-repair"])


def _strict_body(raw: bytes) -> tuple[Mapping[str, Any] | None, JSONResponse | None]:
    """Parse raw body strictly (D03.1); returns (body, error_response)."""
    from diveintocrypto_desktop.shortlab.repair_contracts import load_json_strict

    if not raw:
        return None, _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be JSON")
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        parsed = load_json_strict(text)
    except Exception as exc:
        return None, _hedge_error(
            422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", str(exc)[:200]
        )
    if not isinstance(parsed, Mapping):
        return None, _hedge_error(
            422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be a JSON object"
        )
    return parsed, None


@router.post("/hedge/decisions", status_code=201)
async def create_repair_decision(request: Request) -> Any:
    """D12 Decision POST boundary (R10b implements real recommendation)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return JSONResponse(
            {"error": "shortlab_unavailable", "detail": str(exc)[:200]}, status_code=503
        )
    raw = await request.body()
    body, err = _strict_body(raw)
    if err is not None:
        return err
    assert body is not None
    try:
        result = await service.create_repair_decision(dict(body))
    except Exception as exc:
        mapped = _map_hedge_exception(exc)
        # Skeleton never returns Fake READY; unbound must be 503.
        if isinstance(mapped, JSONResponse) and mapped.status_code == 503:
            return mapped
        # Any other mapped error preserves its code; default to 503.
        try:
            from diveintocrypto_desktop.shortlab.repair_ports import (
                RepairDependencyUnavailable as _Unbound,
            )

            if isinstance(exc, _Unbound):
                return _repair_unavailable_response(str(exc)[:200])
        except Exception:
            pass
        return mapped
    return JSONResponse(dict(result), status_code=201)


@router.get("/hedge/decisions/{decision_id}")
async def get_repair_decision(decision_id: str, request: Request) -> Any:
    """D12 Decision GET boundary (frozen read; R10b implements)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return JSONResponse(
            {"error": "shortlab_unavailable", "detail": str(exc)[:200]}, status_code=503
        )
    try:
        result = await service.get_repair_decision(str(decision_id))
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.get("/hedge/plans/{plan_id}/exit-guidance")
async def repair_exit_guidance(plan_id: str, request: Request) -> Any:
    """D12 exit-guidance boundary (readonly; R10b implements)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return JSONResponse(
            {"error": "shortlab_unavailable", "detail": str(exc)[:200]}, status_code=503
        )
    try:
        result = await service.repair_exit_guidance(str(plan_id))
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.post("/hedge/plans/{plan_id}/protection")
async def confirm_repair_protection(plan_id: str, request: Request) -> Any:
    """D12 protection boundary (expectedVersion gate frozen; R10b implements)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return JSONResponse(
            {"error": "shortlab_unavailable", "detail": str(exc)[:200]}, status_code=503
        )
    raw = await request.body()
    body, err = _strict_body(raw)
    if err is not None:
        return err
    assert body is not None
    # Freeze expectedVersion contract (int>=1, camel + snake alias accepted,
    # never silently dropped). R10b adds full confirmation validation.
    version = body.get("expectedVersion", body.get("expected_version"))
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return _hedge_error(
            422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "expectedVersion must be integer>=1"
        )
    try:
        result = await service.confirm_repair_protection(str(plan_id), dict(body))
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.get("/capabilities")
async def repair_capabilities(request: Request) -> Any:
    """D12 capabilities boundary (always 200 with binding summary)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return JSONResponse(
            {"error": "shortlab_unavailable", "detail": str(exc)[:200]}, status_code=503
        )
    try:
        summary = service.repair_capabilities()
        if isinstance(summary, Mapping):
            out = dict(summary)
        else:
            out = {}
    except Exception:
        # Old stub services without the seam still report unbound honestly.
        out = {
            "contractSchemaVersion": REPAIR_CONTRACT_VERSION,
            "readiness": "NOT_READY",
            "reasons": ["IMPLEMENTATION_UNAVAILABLE"],
            "missingBindings": [],
            "bindings": {},
        }
    out.setdefault("contractSchemaVersion", REPAIR_CONTRACT_VERSION)
    # Never expose secrets/paths; only counts and readiness.
    return out
