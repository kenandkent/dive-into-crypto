"""Short-Lab FastAPI router (Task 14, design section 25).

Single owner: Task 14. Phase 3 registers every ``/api/short/*`` route,
including ``/api/short/evidence/summary`` (503 until Task 16 wires metrics).
Task 16 only supplies ``metrics_provider`` on the service -- this file is
never touched by Task 16/17.

Conventions (frozen contracts):

- Python stays ``snake_case``; only this module converts to ``camelCase``.
- ``ProviderResult`` / ``CandidateState`` come from Task 1 ``models.py``.
- Reads never overwrite snapshots: staleness is a read-time projection
  (``asOfStatus`` / ``snapshotDataQuality`` stay immutable, ``status`` /
  ``dataQuality`` / ``stale`` reflect ``now``).
- SQL never interpolates user input: repository whitelists sort columns;
  this layer fetches the pinned generation then filters / sorts in Python.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from diveintocrypto_desktop.shortlab.service import (
    EVIDENCE_UNAVAILABLE_REASON,
    JOB_TYPE_SCORE_REFRESH,
    SHORTLAB_UNAVAILABLE_REASON,
    JobNotFound,
    ShortLabUnavailable,
    SymbolNotFound,
    UnknownJobType,
    Unavailable,
)
from diveintocrypto_desktop.shortlab.repository import GenerationNotFoundError

SCHEMA_VERSION = "shortlab.api.v1"

router = APIRouter(prefix="/api/short", tags=["shortlab"])

_STATUS_RANK = {
    "READY": 0,
    "CANDIDATE": 1,
    "WATCH": 2,
    "PAUSED": 3,
    "BLOCKED": 4,
    "EXCLUDED": 5,
}

_ALLOWED_STATUS = frozenset({"READY", "CANDIDATE", "WATCH", "EXCLUDED", "PAUSED", "BLOCKED"})
_ALLOWED_CANDIDATE_STATUS = frozenset({"EXCLUDED", "WATCH", "CANDIDATE"})
_ALLOWED_EXECUTION_STATUS = frozenset({"NOT_READY", "READY", "PAUSED", "BLOCKED"})
_ALLOWED_PROFILES = frozenset({
    "MEME_LITE", "GENERAL_LITE", "LOW_FLOAT_VC_LITE",
    "MEME_FULL", "GENERAL_FULL", "LOW_FLOAT_VC_FULL",
})
_ALLOWED_SORTS = frozenset({"ltss", "entry", "funding30d", "dataQuality"})

# BLOCK -> PAUSE -> NOT_READY stable order (design 25.3 + Task 11 orders).
try:  # frozen orders from the risk authority; fallback keeps router importable.
    from diveintocrypto_desktop.shortlab.risk.veto import (
        BLOCK_ORDER as _BLOCK_ORDER,
    )
    from diveintocrypto_desktop.shortlab.risk.veto import (
        NOT_READY_ORDER as _NOT_READY_ORDER,
    )
    from diveintocrypto_desktop.shortlab.risk.veto import (
        PAUSE_ORDER as _PAUSE_ORDER,
    )

    _REASON_ORDER = tuple(_BLOCK_ORDER) + tuple(_PAUSE_ORDER) + tuple(_NOT_READY_ORDER)
except Exception:  # pragma: no cover - defensive fallback
    _REASON_ORDER = (
        "VETO_DATA_IDENTITY", "VETO_LOW_DATA_QUALITY", "VETO_LOW_LIQUIDITY",
        "VETO_CONTRACT_DELISTING",
        "PAUSE_BREAKOUT_24H", "PAUSE_BREAKOUT_7D", "PAUSE_SQUEEZE",
        "PAUSE_NEGATIVE_CARRY", "PAUSE_NEW_TOKEN",
        "PAUSE_CONTRACT_STATUS_UNVERIFIED", "PAUSE_MAJOR_CATALYST",
        "LTSS_BELOW_READY", "ENTRY_NOT_AVAILABLE", "ENTRY_BUDGET_EXHAUSTED",
        "ENTRY_BELOW_READY_THRESHOLD", "DATA_QUALITY_BELOW_READY",
        "TRADEABILITY_BELOW_READY", "IDENTITY_REVIEW_REQUIRED",
        "MULTIPLIER_UNVERIFIED", "LISTING_AGE_UNKNOWN", "READY_INPUT_STALE",
    )

_READY_INPUT_STALE = "READY_INPUT_STALE"

# repo_field -> dq_field (mirrors ShortLabService._build_feature_source_meta).
_REPO_TO_DQ = {
    "market_daily": "market_daily_price",
    "futures_volume_24h": "futures_qv_1d",
    "oi_usd": "oi_usd",
    "contract_status": "contract_status",
    "funding_7d": "funding_7d",
    "funding_30d": "funding_30d",
    "funding_90d": "funding_90d",
    "market_cap": "mc",
    "fdv": "fdv",
    "supply_float": "supply_float",
    "ath": "ath",
    "spot_volume_60d": "spot_60d_qv",
    "spot_volume_24h": "spot_24h_qv",
    "basis": "basis",
    "orderbook_depth": "book_depth",
    "identity": "canonical_mapping",
    "profile": "profile_basis",
}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _service_now(service: Any) -> int:
    for attr in ("_now", "_clock"):
        fn = getattr(service, attr, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                continue
    clock = getattr(service, "_clock", None)
    if callable(clock):
        try:
            return int(clock())
        except Exception:
            pass
    return _now_ms()


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


def _get_runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "shortlab_runtime", None)
    return runtime


def _require_service(request: Request) -> Any:
    runtime = _get_runtime(request)
    if runtime is None:
        raise ShortLabUnavailable(SHORTLAB_UNAVAILABLE_REASON)
    if not bool(getattr(runtime, "available", False)):
        reason = str(getattr(runtime, "unavailable_reason", SHORTLAB_UNAVAILABLE_REASON))
        raise ShortLabUnavailable(reason or SHORTLAB_UNAVAILABLE_REASON)
    try:
        service = runtime.service
    except ShortLabUnavailable:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise ShortLabUnavailable(f"{SHORTLAB_UNAVAILABLE_REASON}:{type(exc).__name__}") from exc
    # Service without a repository is unavailable too (DB never opened).
    if getattr(service, "_repository", None) is None and not bool(
        getattr(service, "available", True)
    ):
        raise ShortLabUnavailable(SHORTLAB_UNAVAILABLE_REASON)
    if getattr(service, "_repository", None) is None:
        # A service constructed with repository=None reports unavailable.
        try:
            if not service.available:  # type: ignore[attr-defined]
                raise ShortLabUnavailable(SHORTLAB_UNAVAILABLE_REASON)
        except ShortLabUnavailable:
            raise
        except Exception:
            pass
    return service


def _unavailable_response(reason: str, detail: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": "shortlab_unavailable", "reason": reason}
    if detail:
        body["detail"] = detail[:200]
    return JSONResponse(body, status_code=503)


def _hedge_capabilities_for(service: Any) -> dict[str, Any]:
    """Additive H08 hedge capabilities (never break the F08 health shape).

    ``hedge`` is True only when the Hedge tables are usable (005 migrated);
    ``hedgeEvidence`` is True only when the H10 provider is wired (lazy
    import, missing means honest False, never assert). Chain venues report
    their configured ``enabled`` flags honestly (unconfigured stays False).
    """
    try:
        hedge_on = False
        flag = getattr(service, "_hedge_available", None)
        if flag is True:
            hedge_on = True
        elif flag is False:
            hedge_on = False
        else:
            # Auto-probe without a runtime flag: base tests without hedge
            # tables report False; hedge-enabled DBs report True. Best-effort
            # only (probe failures mean disabled, never a 500 here).
            hedge_on = False
    except Exception:
        hedge_on = False
    try:
        hedge_evidence_on = False
        try:
            from diveintocrypto_desktop.shortlab.evidence import hedge_metrics as _hm  # type: ignore[import-not-found]

            hedge_evidence_on = bool(
                getattr(_hm, "hedge_summary", None) is not None
                or getattr(_hm, "summary", None) is not None
            ) and bool(hedge_on)
        except Exception:
            hedge_evidence_on = False
    except Exception:
        hedge_evidence_on = False
    try:
        cfg = getattr(service, "config", None)
        if callable(cfg):
            cfg = None
        else:
            try:
                cfg = service.config  # type: ignore[attr-defined]
            except Exception:
                cfg = getattr(service, "_config", None)
        hedge_cfg = getattr(cfg, "hedge", None) if cfg is not None else None
        providers = getattr(hedge_cfg, "providers", {}) if hedge_cfg is not None else {}
        if not isinstance(providers, Mapping):
            providers = {}
    except Exception:
        providers = {}  # type: ignore[assignment]

    def _enabled(name: str) -> bool:
        try:
            entry = providers.get(name) if isinstance(providers, Mapping) else None
            if isinstance(entry, Mapping):
                return bool(entry.get("enabled"))
            return bool(getattr(entry, "enabled", False))
        except Exception:
            return False

    return {
        "hedge": bool(hedge_on),
        "hedgeEvidence": bool(hedge_evidence_on),
        "hedgeChains": {
            "binanceSpot": bool(_enabled("binance_spot")),
            "binanceAlpha": bool(_enabled("binance_alpha")),
            "onchain": bool(_enabled("onchain")),
        },
    }


def _hedge_error(status: int, error: str, reason: str, detail: str | None = None) -> JSONResponse:
    # R10a boundary: freeze error envelope with reasonCode alias (D12/D18).
    # Wire keeps both snake + camel for compat; R10b reuses this envelope.
    body: dict[str, Any] = {
        "error": error,
        "reason": reason,
        "reasonCode": reason,
        "reason_code": reason,
        "code": error,
    }
    if detail:
        body["detail"] = str(detail)[:300]
    return JSONResponse(body, status_code=status)


def _repair_unavailable_response(
    detail: str | None = None,
) -> JSONResponse:
    """R10a unbound repair bundle (503 IMPLEMENTATION_UNAVAILABLE, D19.1)."""
    return _hedge_error(503, "IMPLEMENTATION_UNAVAILABLE", "IMPLEMENTATION_UNAVAILABLE", detail)


def _map_hedge_exception(exc: Exception) -> JSONResponse:
    """Map frozen Hedge/service errors to B33 HTTP codes (never 500 for known)."""
    # R10a boundary: unbound RepairPorts -> 503 IMPLEMENTATION_UNAVAILABLE.
    # Old archive reads (simulate/get_simulation/list/plans) never raise this;
    # only new repair methods do (service_calls.json call points).
    try:
        from diveintocrypto_desktop.shortlab.repair_ports import (
            RepairDependencyUnavailable as _RepairUnbound,
        )

        if isinstance(exc, _RepairUnbound):
            reason = str(getattr(exc, "reason_code", None) or "IMPLEMENTATION_UNAVAILABLE")
            return _hedge_error(503, reason, reason, str(exc)[:300])
    except Exception:
        pass
    status = int(getattr(exc, "status_code", 500) or 500)
    code = str(getattr(exc, "error_code", None) or getattr(exc, "reason_code", None) or type(exc).__name__)
    reason = str(getattr(exc, "reason_code", None) or code)
    detail = str(exc)[:300]
    # Repository-level aliases (H01 single-worker contracts).
    try:
        from diveintocrypto_desktop.shortlab.repository import HedgeIdempotencyError as _Idem
        from diveintocrypto_desktop.shortlab.repository import HedgeVersionConflictError as _Ver
        from diveintocrypto_desktop.shortlab.repository import LocalWriteBusyError as _Busy
        from diveintocrypto_desktop.shortlab.repository import ReferenceNotFoundError as _Ref
        from diveintocrypto_desktop.shortlab.repository import ValidationError as _Val

        if isinstance(exc, _Busy):
            return _hedge_error(503, "LOCAL_WRITE_BUSY", "LOCAL_WRITE_BUSY", detail)
        if isinstance(exc, _Idem):
            return _hedge_error(409, "IDEMPOTENCY_PAYLOAD_MISMATCH", "IDEMPOTENCY_PAYLOAD_MISMATCH", detail)
        if isinstance(exc, _Ver):
            return _hedge_error(409, "PLAN_VERSION_CONFLICT", "PLAN_VERSION_CONFLICT", detail)
        if isinstance(exc, _Ref):
            msg = str(exc).lower()
            if "simulation" in msg:
                return _hedge_error(404, "HEDGE_SIMULATION_NOT_FOUND", "HEDGE_SIMULATION_NOT_FOUND", detail)
            if "plan" in msg:
                return _hedge_error(404, "HEDGE_PLAN_NOT_FOUND", "HEDGE_PLAN_NOT_FOUND", detail)
            return _hedge_error(404, code or "HEDGE_NOT_FOUND", code or "HEDGE_NOT_FOUND", detail)
        if isinstance(exc, _Val):
            msg_l = str(exc).lower()
            if "expired" in msg_l:
                return _hedge_error(409, "QUOTE_EXPIRED", "QUOTE_EXPIRED", detail)
            return _hedge_error(422, code or "HEDGE_INPUT_INVALID", code or "HEDGE_INPUT_INVALID", detail)
    except Exception:
        pass
    # Service-level Hedge* errors already carry status/error/reason.
    try:
        from diveintocrypto_desktop.shortlab.service import HedgeAlertNotFound as _Alert404
        from diveintocrypto_desktop.shortlab.service import HedgeBusy as _Busy2
        from diveintocrypto_desktop.shortlab.service import HedgeDisabled as _Disabled
        from diveintocrypto_desktop.shortlab.service import FundingCaptureDisabled as _FundingOff
        from diveintocrypto_desktop.shortlab.service import HedgeInputMismatch as _Mismatch
        from diveintocrypto_desktop.shortlab.service import HedgeLegsIncomplete as _Legs
        from diveintocrypto_desktop.shortlab.service import HedgeOpenLegsRemain as _Open
        from diveintocrypto_desktop.shortlab.service import HedgePlanNotFound as _Plan404
        from diveintocrypto_desktop.shortlab.service import HedgeQuoteExpired as _Expired
        from diveintocrypto_desktop.shortlab.service import HedgeSimulationNotFound as _Sim404
        from diveintocrypto_desktop.shortlab.service import HedgeUnavailable as _Unavail
        from diveintocrypto_desktop.shortlab.service import HedgeValidationError as _Valid
        from diveintocrypto_desktop.shortlab.service import HedgeVersionConflict as _Conflict
        from diveintocrypto_desktop.shortlab.service import HedgeIdempotencyMismatch as _Idem2

        if isinstance(exc, _Busy2):
            return _hedge_error(503, "LOCAL_WRITE_BUSY", "LOCAL_WRITE_BUSY", detail)
        # R10b D12/D13.2: switch-off new suggestions (history reads bypass).
        if isinstance(exc, _Disabled):
            return _hedge_error(503, "HEDGE_DISABLED", "HEDGE_DISABLED", detail)
        if isinstance(exc, _FundingOff):
            return _hedge_error(503, "FUNDING_CAPTURE_DISABLED", "FUNDING_CAPTURE_DISABLED", detail)
        if isinstance(exc, _Unavail):
            # Preserve VENUE_REGION_UNAVAILABLE (451) as honest 503 with that code.
            if "REGION" in code or "451" in detail:
                return _hedge_error(503, code or "VENUE_REGION_UNAVAILABLE", reason or code, detail)
            return _hedge_error(503, "HEDGE_UNAVAILABLE", "HEDGE_UNAVAILABLE", detail)
        if isinstance(exc, (_Sim404, _Plan404, _Alert404)):
            return _hedge_error(404, code, code, detail)
        if isinstance(exc, (_Expired, _Mismatch, _Idem2, _Conflict, _Legs, _Open)):
            return _hedge_error(409, code, code, detail)
        if isinstance(exc, _Valid):
            return _hedge_error(422, code, reason or code, detail)
    except Exception:
        pass
    if status == 422:
        return _hedge_error(422, code or "HEDGE_INPUT_INVALID", reason or code, detail)
    if status == 404:
        return _hedge_error(404, code or "HEDGE_NOT_FOUND", code or "HEDGE_NOT_FOUND", detail)
    if status == 409:
        return _hedge_error(409, code or "HEDGE_CONFLICT", code or "HEDGE_CONFLICT", detail)
    if status == 503:
        return _hedge_error(503, code or "HEDGE_UNAVAILABLE", code or "HEDGE_UNAVAILABLE", detail)
    return _hedge_error(500, "hedge_internal", "hedge_internal", detail)


def _sort_codes(codes: list[str], order: tuple[str, ...]) -> list[str]:
    seen: dict[str, None] = {}
    for code in codes:
        if isinstance(code, str) and code and code not in seen:
            seen[code] = None
    rank = {code: idx for idx, code in enumerate(order)}
    return sorted(seen, key=lambda c: (rank.get(c, len(order)), c))


def _sort_reasons(codes: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for code in codes:
        if isinstance(code, str) and code and code not in seen:
            seen[code] = None
    rank = {code: idx for idx, code in enumerate(_REASON_ORDER)}
    return tuple(sorted(seen, key=lambda c: (rank.get(c, len(_REASON_ORDER)), c)))


# ---------------------------------------------------------------------------
# Feature helpers (raw values only; missing stays None, never 0)
# ---------------------------------------------------------------------------


def _feature_map(feature: Any) -> dict[str, Any]:
    if feature is None:
        return {}
    feats = getattr(feature, "features", None)
    if isinstance(feats, Mapping):
        return dict(feats)
    if isinstance(feature, Mapping):
        inner = feature.get("features")
        if isinstance(inner, Mapping):
            return dict(inner)
        return dict(feature)
    return {}


def _factor_value(feats: Mapping[str, Any], *path: str) -> Any:
    cur: Any = feats
    for key in path:
        if not isinstance(cur, Mapping) or key not in cur:
            return None
        cur = cur[key]
    if isinstance(cur, Mapping) and "value" in cur:
        return cur["value"]
    return cur


def _extract_funding_30d(feats: Mapping[str, Any]) -> float | None:
    inputs = feats.get("_inputs")
    if isinstance(inputs, Mapping) and _finite(inputs.get("funding_30d")) is not None:
        return _finite(inputs.get("funding_30d"))
    value = _factor_value(feats, "carry", "factors", "funding_30d", "value")
    if _finite(value) is not None:
        return _finite(value)
    # Flat test fixtures store the raw directly.
    if _finite(feats.get("funding_30d")) is not None:
        return _finite(feats.get("funding_30d"))
    if _finite(feats.get("funding30d")) is not None:
        return _finite(feats.get("funding30d"))
    return None


def _extract_positive_ratio_30d(feats: Mapping[str, Any]) -> float | None:
    value = _factor_value(feats, "carry", "factors", "positive_ratio_30d", "value")
    if _finite(value) is not None:
        return _finite(value)
    inputs = feats.get("_inputs")
    if isinstance(inputs, Mapping):
        for key in ("funding_positive_ratio_30d", "positive_funding_ratio_30d",
                    "positive_ratio_30d"):
            if _finite(inputs.get(key)) is not None:
                return _finite(inputs.get(key))
    if _finite(feats.get("positiveFundingRatio30d")) is not None:
        return _finite(feats.get("positiveFundingRatio30d"))
    if _finite(feats.get("positive_funding_ratio_30d")) is not None:
        return _finite(feats.get("positive_funding_ratio_30d"))
    return None


def _extract_ath_drawdown(feats: Mapping[str, Any]) -> float | None:
    inputs = feats.get("_inputs")
    if isinstance(inputs, Mapping) and _finite(inputs.get("ath_drawdown")) is not None:
        return _finite(inputs.get("ath_drawdown"))
    value = _factor_value(feats, "lifecycle", "factors", "ath_drawdown", "value")
    if _finite(value) is not None:
        return _finite(value)
    if _finite(feats.get("ath_drawdown")) is not None:
        return _finite(feats.get("ath_drawdown"))
    if _finite(feats.get("athDrawdown")) is not None:
        return _finite(feats.get("athDrawdown"))
    return None


def _extract_oi_mc_ratio(feats: Mapping[str, Any]) -> float | None:
    value = _factor_value(feats, "carry", "factors", "oi_mc", "value")
    if _finite(value) is not None:
        return _finite(value)
    inputs = feats.get("_inputs") if isinstance(feats.get("_inputs"), Mapping) else {}
    oi = _finite(inputs.get("oi_value_usd")) if isinstance(inputs, Mapping) else None
    mc = _finite(inputs.get("market_cap_usd")) if isinstance(inputs, Mapping) else None
    if oi is None:
        oi = _finite(feats.get("oi_value_usd"))
    if mc is None:
        mc = _finite(feats.get("market_cap_usd"))
    if oi is not None and mc is not None and mc > 0 and oi >= 0:
        return oi / mc
    if _finite(feats.get("oiMarketCapRatio")) is not None:
        return _finite(feats.get("oiMarketCapRatio"))
    return None


def _extract_futures_spot_ratio(feats: Mapping[str, Any]) -> float | None:
    value = _factor_value(feats, "carry", "factors", "futures_spot_ratio", "value")
    if _finite(value) is not None:
        return _finite(value)
    inputs = feats.get("_inputs")
    if isinstance(inputs, Mapping) and _finite(inputs.get("futures_spot_volume_ratio")) is not None:
        return _finite(inputs.get("futures_spot_volume_ratio"))
    if _finite(feats.get("futures_spot_volume_ratio")) is not None:
        return _finite(feats.get("futures_spot_volume_ratio"))
    if _finite(feats.get("futuresSpotVolumeRatio")) is not None:
        return _finite(feats.get("futuresSpotVolumeRatio"))
    return None


def _extract_categories(feats: Mapping[str, Any], score: Any = None) -> list[str]:
    for key in ("categories", "category_list"):
        raw = feats.get(key)
        if isinstance(raw, (list, tuple)) and raw:
            return [str(c) for c in raw if isinstance(c, str) and c]
    inputs = feats.get("_inputs")
    if isinstance(inputs, Mapping):
        for key in ("categories", "category_list"):
            raw = inputs.get(key)
            if isinstance(raw, (list, tuple)) and raw:
                return [str(c) for c in raw if isinstance(c, str) and c]
    # Fallback: profile base implies a category (MEME_LITE -> MEME).
    profile = str(getattr(score, "profile", "") or feats.get("profile") or "")
    if profile.startswith("MEME"):
        return ["MEME"]
    return []


def _extract_canonical_id(symbol: str, feats: Mapping[str, Any]) -> str:
    inputs = feats.get("_inputs")
    if isinstance(inputs, Mapping):
        for key in ("canonical_id", "canonicalId"):
            raw = inputs.get(key)
            if isinstance(raw, str) and raw:
                return raw
    raw = feats.get("canonical_id") or feats.get("canonicalId")
    if isinstance(raw, str) and raw:
        return raw
    return str(symbol).lower()


def _source_meta_map(feature: Any) -> dict[str, Any]:
    if feature is None:
        return {}
    meta = getattr(feature, "source_meta", None)
    if isinstance(meta, Mapping):
        return dict(meta)
    if isinstance(feature, Mapping):
        inner = feature.get("source_meta") or feature.get("sourceMeta")
        if isinstance(inner, Mapping):
            return dict(inner)
    return {}


def _data_availability(source_meta: Mapping[str, Any]) -> dict[str, str]:
    def _status(*names: str) -> str:
        for name in names:
            entry = source_meta.get(name)
            if isinstance(entry, Mapping) and entry.get("status"):
                return str(entry["status"])
        return "UNAVAILABLE"

    funding = [_status(n) for n in ("funding_7d", "funding_30d", "funding_90d")]
    fundamentals = [_status(n) for n in ("market_cap", "fdv", "supply_float", "ath")]
    market = [_status(n) for n in ("market_daily", "futures_volume_24h", "oi_usd", "contract_status")]
    spot = [_status(n) for n in ("spot_volume_60d", "spot_volume_24h")]

    def _worst(statuses: list[str]) -> str:
        if all(s == "OK" for s in statuses):
            return "OK"
        if any(s == "NOT_APPLICABLE" for s in statuses) and all(
            s in ("OK", "NOT_APPLICABLE") for s in statuses
        ):
            # Mixed OK + N/A stays PARTIAL unless every leg is N/A.
            if all(s == "NOT_APPLICABLE" for s in statuses):
                return "NOT_APPLICABLE"
            return "PARTIAL"
        if any(s in ("UNAVAILABLE", "ERROR") for s in statuses):
            # A fully failed leg degrades the group but stays honest.
            if any(s == "OK" for s in statuses):
                return "PARTIAL"
            return "UNAVAILABLE"
        return "PARTIAL"

    return {
        "fundingHistory": _worst(funding),
        "spot": _worst(spot),
        "fundamentals": _worst(fundamentals),
        "marketFutures": _worst(market),
        "identityProfile": _status("identity", "profile"),
        "orderbookDepth": _status("orderbook_depth"),
        "basis": _status("basis"),
    }


def _freshness_windows(service: Any) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
    # Prefer live config TTLs; fall back to quality.py constants.
    try:
        from diveintocrypto_desktop.shortlab.quality import (
            FIELD_FRESHNESS as _FF,
        )
        from diveintocrypto_desktop.shortlab.quality import (
            FRESHNESS_SEC as _FS,
        )

        freshness = dict(_FS)
        field_to_window = dict(_FF)
    except Exception:  # pragma: no cover - defensive
        freshness = {
            "market_futures": (900, 3600),
            "funding_history": (1800, 7200),
            "fundamentals": (3600, 21600),
            "supply_float": (21600, 43200),
            "spot_liquidity": (3600, 10800),
            "identity_profile": (86400, 259200),
        }
        field_to_window = {}
    try:
        config = getattr(service, "_config", None) or getattr(
            getattr(service, "config", None), "_config", None
        )
        if config is None and hasattr(service, "config"):
            maybe = service.config  # type: ignore[attr-defined]
            if not callable(maybe):
                config = maybe
        if config is not None:
            fresh = getattr(config, "quality_freshness_sec", None)
            overrides = getattr(config, "quality_field_overrides_sec", None)
            if isinstance(fresh, Mapping):
                for group, window in fresh.items():
                    ttl = getattr(window, "ttl", None)
                    grace = getattr(window, "grace", None)
                    if isinstance(window, Mapping):
                        ttl, grace = window.get("ttl"), window.get("grace")
                    if isinstance(ttl, (int, float)) and isinstance(grace, (int, float)):
                        freshness[str(group)] = (int(ttl), int(grace))
            if isinstance(overrides, Mapping):
                for field_name, window in overrides.items():
                    ttl = getattr(window, "ttl", None)
                    grace = getattr(window, "grace", None)
                    if isinstance(window, Mapping):
                        ttl, grace = window.get("ttl"), window.get("grace")
                    if isinstance(ttl, (int, float)) and isinstance(grace, (int, float)):
                        freshness[f"override:{field_name}"] = (int(ttl), int(grace))
    except Exception:
        pass
    return freshness, field_to_window


def _dq_window_for(
    dq_field: str,
    freshness: Mapping[str, tuple[int, int]],
    field_to_window: Mapping[str, str],
    service: Any,
) -> tuple[int, int]:
    # supply_float has a per-field override (6h/12h).
    try:
        config = getattr(service, "_config", None)
        if config is None and hasattr(service, "config"):
            maybe = service.config  # type: ignore[attr-defined]
            if not callable(maybe):
                config = maybe
        if dq_field == "supply_float" and config is not None:
            overrides = getattr(config, "quality_field_overrides_sec", None)
            if isinstance(overrides, Mapping) and "supply_float" in overrides:
                window = overrides["supply_float"]
                ttl = getattr(window, "ttl", None)
                grace = getattr(window, "grace", None)
                if isinstance(window, Mapping):
                    ttl, grace = window.get("ttl"), window.get("grace")
                if ttl is not None and grace is not None:
                    return int(ttl), int(grace)
    except Exception:
        pass
    window_name = field_to_window.get(dq_field)
    if window_name and window_name in freshness:
        return freshness[window_name]
    return freshness.get("market_futures", (900, 3600))


def _project_row(
    score: Any, feature: Any, service: Any, now_ms: int
) -> dict[str, Any]:
    """Read-time projection: immutable snapshot + current stale/status/DQ.

    Never writes. Only downgrades on elapsed time (score TTL or key-field
    TTL); LTSS / entry are never recomputed from new provider data.
    """
    stored_status = str(getattr(score, "status", "CANDIDATE"))
    stored_candidate = str(getattr(score, "candidate_status", "CANDIDATE"))
    stored_execution = str(getattr(score, "execution_status", "NOT_READY"))
    stored_reasons = list(getattr(score, "reasons", ()) or ())
    stored_warnings = list(getattr(score, "warnings", ()) or ())
    stored_vetoes = list(getattr(score, "vetoes", ()) or ())
    stored_pauses = list(getattr(score, "pauses", ()) or ())
    stored_dq = _finite(getattr(score, "data_quality", None))
    as_of_ms = int(getattr(score, "as_of_ms", now_ms) or now_ms)
    tier = str(getattr(score, "analysis_tier", "LITE") or "LITE")

    try:
        score_sec = int(service.config.refresh.score_sec)  # type: ignore[attr-defined]
    except Exception:
        try:
            score_sec = int(getattr(getattr(service, "_config", None), "refresh").score_sec)
        except Exception:
            score_sec = 1800

    stale = False
    if now_ms - as_of_ms > max(0, score_sec) * 1000:
        stale = True

    # Recompute current DQ from the frozen per-field provenance so the
    # projected dataQuality ages exactly like Task 11 math.
    projected_dq = stored_dq
    dq_stale = False
    try:
        from diveintocrypto_desktop.shortlab.quality import FieldState
        from diveintocrypto_desktop.shortlab.quality import data_quality as _dq_fn

        source_meta = _source_meta_map(feature)
        freshness, field_to_window = _freshness_windows(service)
        states: list[Any] = []
        for repo_field, dq_field in _REPO_TO_DQ.items():
            entry = source_meta.get(repo_field)
            if not isinstance(entry, Mapping):
                states.append(
                    FieldState(field_id=dq_field, status="UNAVAILABLE",
                               fetched_at_ms=as_of_ms, source="shortlab-missing")
                )
                continue
            status = str(entry.get("status") or "UNAVAILABLE")
            fetched = entry.get("fetched_at_ms")
            try:
                fetched_ms = int(fetched) if fetched is not None else None
            except (TypeError, ValueError):
                fetched_ms = None
            coverage = entry.get("coverage_fraction")
            try:
                cov = float(coverage) if coverage is not None else None
            except (TypeError, ValueError):
                cov = None
            states.append(
                FieldState(
                    field_id=dq_field,
                    status=status,  # type: ignore[arg-type]
                    coverage=cov,
                    fetched_at_ms=fetched_ms,
                    reason_code=entry.get("reason_code"),
                    source=str(entry.get("source") or "shortlab"),
                )
            )
        breakdown = _dq_fn(tier, states, now_ms)
        projected_dq = float(breakdown.data_quality)
        dq_stale = bool(breakdown.stale)
        if dq_stale:
            stale = True
    except Exception:
        # Fallback: key-field TTL walk when DQ recomputation is unavailable.
        try:
            from diveintocrypto_desktop.shortlab.quality import (
                READY_REQUIRED_FIELDS_LITE as _REQ_LITE,
            )
            from diveintocrypto_desktop.shortlab.quality import (
                READY_REQUIRED_FIELDS_FULL as _REQ_FULL,
            )

            required = _REQ_FULL if tier == "FULL" else _REQ_LITE
            freshness, field_to_window = _freshness_windows(service)
            dq_to_repo = {dq: repo for repo, dq in _REPO_TO_DQ.items()}
            source_meta = _source_meta_map(feature)
            for dq_field in required:
                repo_field = dq_to_repo.get(dq_field)
                if repo_field is None:
                    continue
                entry = source_meta.get(repo_field)
                if not isinstance(entry, Mapping):
                    continue
                if str(entry.get("status")) not in ("OK", "PARTIAL"):
                    continue
                try:
                    fetched_ms = int(entry.get("fetched_at_ms"))
                except (TypeError, ValueError):
                    stale = True
                    break
                ttl, _grace = _dq_window_for(dq_field, freshness, field_to_window, service)
                if now_ms - fetched_ms > ttl * 1000:
                    stale = True
                    break
        except Exception:
            pass

    execution = stored_execution
    status = stored_status
    reasons = list(stored_reasons)
    if stale:
        if stored_candidate == "CANDIDATE" and _READY_INPUT_STALE not in reasons:
            reasons.append(_READY_INPUT_STALE)
        if execution == "READY":
            execution = "NOT_READY"
            # Display follows section 17: CANDIDATE leg + NOT_READY -> CANDIDATE.
            if stored_candidate == "CANDIDATE":
                status = "CANDIDATE"
            elif stored_candidate == "WATCH":
                status = "WATCH"
            elif stored_candidate == "EXCLUDED":
                status = "EXCLUDED"
            else:
                status = "CANDIDATE"
    reasons_t = _sort_reasons(reasons)
    try:
        from diveintocrypto_desktop.shortlab.risk.veto import (
            BLOCK_ORDER as _BO,
        )
        from diveintocrypto_desktop.shortlab.risk.veto import (
            PAUSE_ORDER as _PO,
        )
        from diveintocrypto_desktop.shortlab.risk.veto import (
            WARN_ORDER as _WO,
        )

        vetoes_out = _sort_codes(list(stored_vetoes), tuple(_BO))
        pauses_out = _sort_codes(list(stored_pauses), tuple(_PO))
        warnings_out = _sort_codes(list(stored_warnings), tuple(_WO))
    except Exception:
        vetoes_out = sorted(set(stored_vetoes))
        pauses_out = sorted(set(stored_pauses))
        warnings_out = sorted(set(stored_warnings))

    return {
        "status": status,
        "asOfStatus": stored_status,
        "candidateStatus": stored_candidate,
        "executionStatus": execution,
        "stale": bool(stale),
        "reasons": list(reasons_t),
        "vetoes": list(vetoes_out),
        "pauses": list(pauses_out),
        "warnings": list(warnings_out),
        "dataQuality": projected_dq,
        "snapshotDataQuality": stored_dq,
    }


def _serialize_item(score: Any, feature: Any, service: Any, now_ms: int) -> dict[str, Any]:
    feats = _feature_map(feature)
    projection = _project_row(score, feature, service, now_ms)
    source_meta = _source_meta_map(feature)
    module_scores = getattr(score, "module_scores", None)
    if isinstance(module_scores, Mapping):
        module_scores_out = {str(k): _finite(v) for k, v in dict(module_scores).items()}
    else:
        module_scores_out = {}
    metrics = {
        "funding30d": _extract_funding_30d(feats),
        "positiveFundingRatio30d": _extract_positive_ratio_30d(feats),
        "athDrawdown": _extract_ath_drawdown(feats),
        "oiMarketCapRatio": _extract_oi_mc_ratio(feats),
        "futuresSpotVolumeRatio": _extract_futures_spot_ratio(feats),
    }
    symbol = str(getattr(score, "symbol", ""))
    return {
        "symbol": symbol,
        "canonicalId": _extract_canonical_id(symbol, feats),
        "profile": str(getattr(score, "profile", "")),
        "categories": _extract_categories(feats, score),
        "status": projection["status"],
        "asOfStatus": projection["asOfStatus"],
        "candidateStatus": projection["candidateStatus"],
        "executionStatus": projection["executionStatus"],
        "ltss": _finite(getattr(score, "ltss", None)),
        "entryScore": _finite(getattr(score, "entry_score", None)),
        "dataQuality": projection["dataQuality"],
        "snapshotDataQuality": projection["snapshotDataQuality"],
        "moduleScores": module_scores_out,
        "metrics": metrics,
        "vetoes": projection["vetoes"],
        "warnings": projection["warnings"],
        "pauses": projection["pauses"],
        "reasons": projection["reasons"],
        "dataAvailability": _data_availability(source_meta),
        "asOfMs": int(getattr(score, "as_of_ms", now_ms) or now_ms),
        "stale": projection["stale"],
        "analysisTier": str(getattr(score, "analysis_tier", "LITE") or "LITE"),
        "scoreVersion": str(getattr(score, "score_version", "") or ""),
        "featureVersion": str(getattr(score, "feature_version", "") or ""),
        "entryVersion": getattr(score, "entry_version", None),
        "configHash": str(getattr(score, "config_hash", "") or ""),
        "snapshotId": str(getattr(score, "snapshot_id", "") or ""),
    }


async def _fetch_all_scores(repo: Any, generation_id: str) -> list[Any]:
    out: list[Any] = []
    offset = 0
    while True:
        page = await repo.list_candidates(
            generation_id=generation_id, limit=200, offset=offset
        )
        out.extend(list(page.items))
        if len(page.items) < 200:
            break
        offset += 200
    return out


async def _resolve_generation(
    repo: Any, generation_id: str | None
) -> tuple[str, dict[str, Any] | None]:
    if generation_id is None:
        resolved = await repo.latest_completed_generation()
        if resolved is None:
            raise GenerationNotFoundError("no completed score_refresh generation")
        row = await repo.get_job_run(resolved)
        return resolved, row
    row = await repo.get_job_run(generation_id)
    if row is None or row.get("status") != "SUCCEEDED":
        raise GenerationNotFoundError(
            f"generation {generation_id!r} is not a completed score_refresh job"
        )
    return generation_id, row


def _apply_filters(
    items: list[dict[str, Any]],
    *,
    status: str | None,
    candidate_status: str | None,
    execution_status: str | None,
    category: str | None,
    profile: str | None,
    min_ltss: float | None,
    min_entry: float | None,
    min_funding_30d: float | None,
    ath_min: float | None,
    ath_max: float | None,
    min_data_quality: float | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if status is not None and item.get("status") != status:
            continue
        if candidate_status is not None and item.get("candidateStatus") != candidate_status:
            continue
        if execution_status is not None and item.get("executionStatus") != execution_status:
            continue
        if profile is not None and item.get("profile") != profile:
            continue
        if category is not None:
            cats = [str(c).upper() for c in (item.get("categories") or [])]
            if str(category).upper() not in cats:
                continue
        if min_ltss is not None:
            value = _finite(item.get("ltss"))
            if value is None or value < min_ltss:
                continue
        if min_entry is not None:
            value = _finite(item.get("entryScore"))
            if value is None or value < min_entry:
                continue
        if min_data_quality is not None:
            value = _finite(item.get("dataQuality"))
            if value is None or value < min_data_quality:
                continue
        metrics = item.get("metrics") or {}
        if min_funding_30d is not None:
            value = _finite(metrics.get("funding30d"))
            if value is None or value < min_funding_30d:
                continue
        if ath_min is not None or ath_max is not None:
            value = _finite(metrics.get("athDrawdown"))
            if value is None:
                continue
            if ath_min is not None and value < ath_min:
                continue
            if ath_max is not None and value > ath_max:
                continue
        out.append(item)
    return out


def _sort_key_for(sort: str | None, item: dict[str, Any]) -> float | None:
    if sort is None or sort == "ltss":
        return _finite(item.get("ltss"))
    if sort == "entry":
        return _finite(item.get("entryScore"))
    if sort == "funding30d":
        metrics = item.get("metrics") or {}
        return _finite(metrics.get("funding30d"))
    if sort == "dataQuality":
        return _finite(item.get("dataQuality"))
    return None


def _apply_sort(
    items: list[dict[str, Any]], sort: str | None, order: str
) -> list[dict[str, Any]]:
    reverse = order != "asc"
    if sort is None:
        def _default_key(item: dict[str, Any]) -> tuple:
            rank = _STATUS_RANK.get(str(item.get("status")), 99)
            ltss = _finite(item.get("ltss"))
            entry = _finite(item.get("entryScore"))
            dq = _finite(item.get("dataQuality"))
            # Nulls last for every numeric leg, then symbol/snapshot ASC.
            return (
                rank,
                1 if ltss is None else 0,
                -(ltss or 0.0) if reverse or True else (ltss or 0.0),
                1 if entry is None else 0,
                -(entry or 0.0),
                1 if dq is None else 0,
                -(dq or 0.0),
                str(item.get("symbol") or ""),
                str(item.get("snapshotId") or ""),
            )

        # Default is always DESC NULLS LAST for the numeric legs.
        return sorted(items, key=_default_key)

    def _explicit_key(item: dict[str, Any]) -> tuple:
        value = _sort_key_for(sort, item)
        missing = 1 if value is None else 0
        numeric = value if value is not None else 0.0
        if reverse:
            numeric = -numeric
        return (missing, numeric, str(item.get("symbol") or ""), str(item.get("snapshotId") or ""))

    return sorted(items, key=_explicit_key)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/health")
async def short_health(request: Request) -> Any:
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return JSONResponse(
            {"error": "shortlab_unavailable", "detail": str(exc)[:200]}, status_code=503
        )
    repo = getattr(service, "_repository", None)
    now_ms = _service_now(service)
    try:
        generation_id = await repo.latest_completed_generation() if repo is not None else None
    except Exception:
        generation_id = None
    try:
        tier = str(service.config.analysis_tier)  # type: ignore[attr-defined]
    except Exception:
        tier = "LITE"
    # F08 additive (A9.1): original fields above stay byte-identical in name
    # and meaning; the keys below are additive only. UI reads the latest
    # generation via lastSuccessfulGeneration and never assumes provider
    # health from available=true.
    try:
        from diveintocrypto_desktop.shortlab.service import (
            FULL_PROVIDER_NAMES as _FULL_NAMES,
        )
        from diveintocrypto_desktop.shortlab.service import (
            KNOWN_JOB_TYPES as _KNOWN_JOBS,
        )
    except Exception:  # pragma: no cover - defensive fallback
        _FULL_NAMES = ("unlock", "social", "catalyst")
        _KNOWN_JOBS = ("score_refresh", "funding_backfill", "contract_refresh", "metadata")
    try:
        _metrics_ready = getattr(service, "_metrics_provider", None) is not None
    except Exception:
        _metrics_ready = False
    try:
        _registry = getattr(service, "registry", None)
        _full_ready = bool(
            _registry is not None
            and all(bool(_registry.has(name)) for name in _FULL_NAMES)
        )
    except Exception:
        _full_ready = False
    try:
        _missing: list[str] = []
        _registry2 = getattr(service, "registry", None)
        for _name in _FULL_NAMES:
            try:
                if _registry2 is None or not bool(_registry2.has(_name)):
                    _missing.append(str(_name))
            except Exception:
                _missing.append(str(_name))
    except Exception:
        _missing = []
    try:
        _running_ids: list[str] = []
        for _slot in dict(getattr(service, "_running", {}) or {}).values():
            try:
                if not bool(_slot["task"].done()):
                    _running_ids.append(str(_slot["job_id"]))
            except Exception:
                continue
    except Exception:
        _running_ids = []
    return {
        "ok": True,
        "available": True,
        "analysisTier": tier,
        "scoreVersion": "ltss-lite-v1",
        "generationId": generation_id,
        "generatedAtMs": now_ms,
        "schemaVersion": SCHEMA_VERSION,
        "schema_version": SCHEMA_VERSION,
        "capabilities": {
            "scoreRefresh": True,
            "jobStatus": True,
            "generationPinning": True,
            "evidenceSummary": bool(_metrics_ready),
            "fullTier": bool(_full_ready),
            **_hedge_capabilities_for(service),
        },
        "jobs": {"known": list(_KNOWN_JOBS), "running": _running_ids},
        "lastSuccessfulGeneration": generation_id,
        "missingDependencies": _missing,
    }


@router.get("/candidates")
async def short_candidates(
    request: Request,
    status: str | None = Query(None),
    candidate_status: str | None = Query(None),
    execution_status: str | None = Query(None),
    category: str | None = Query(None),
    profile: str | None = Query(None),
    min_ltss: float | None = Query(None, ge=0, le=100),
    min_entry: float | None = Query(None, ge=0, le=100),
    min_funding_30d: float | None = Query(None),
    ath_drawdown_min: float | None = Query(None, ge=-1, le=0),
    ath_drawdown_max: float | None = Query(None, ge=-1, le=0),
    min_data_quality: float | None = Query(None, ge=0, le=100),
    sort: str | None = Query(None),
    order: str | None = Query("desc"),
    generation_id: str | None = Query(None),
    generationId: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Any:
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))

    # Manual enum validation -> 422 (FastAPI Query alone would accept anything).
    if status is not None and status not in _ALLOWED_STATUS:
        return JSONResponse(
            {"error": "short_invalid_filter", "detail": f"unknown status {status!r}"},
            status_code=422,
        )
    if candidate_status is not None and candidate_status not in _ALLOWED_CANDIDATE_STATUS:
        return JSONResponse(
            {"error": "short_invalid_filter",
             "detail": f"unknown candidate_status {candidate_status!r}"},
            status_code=422,
        )
    if execution_status is not None and execution_status not in _ALLOWED_EXECUTION_STATUS:
        return JSONResponse(
            {"error": "short_invalid_filter",
             "detail": f"unknown execution_status {execution_status!r}"},
            status_code=422,
        )
    if profile is not None and profile not in _ALLOWED_PROFILES:
        return JSONResponse(
            {"error": "short_invalid_filter", "detail": f"unknown profile {profile!r}"},
            status_code=422,
        )
    if sort is not None and sort not in _ALLOWED_SORTS:
        return JSONResponse(
            {"error": "short_invalid_filter", "detail": f"unknown sort {sort!r}"},
            status_code=422,
        )
    if order not in ("asc", "desc"):
        return JSONResponse(
            {"error": "short_invalid_filter", "detail": f"unknown order {order!r}"},
            status_code=422,
        )
    if (
        ath_drawdown_min is not None
        and ath_drawdown_max is not None
        and ath_drawdown_min > ath_drawdown_max
    ):
        return JSONResponse(
            {"error": "short_invalid_filter",
             "detail": "ath_drawdown_min must be <= ath_drawdown_max"},
            status_code=422,
        )
    if min_funding_30d is not None and _finite(min_funding_30d) is None:
        return JSONResponse(
            {"error": "short_invalid_filter", "detail": "bad min_funding_30d"},
            status_code=422,
        )

    pinned = generationId if generationId is not None else generation_id
    if offset > 0 and pinned is None:
        return JSONResponse(
            {"error": "short_invalid_filter",
             "detail": "generation_id is required when offset > 0 (pin pagination)"},
            status_code=422,
        )
    repo = service._repository
    try:
        resolved, job_row = await _resolve_generation(repo, pinned)
    except GenerationNotFoundError as exc:
        return JSONResponse(
            {"error": "short_generation_not_found", "detail": str(exc)[:200]},
            status_code=404,
        )
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))

    now_ms = _service_now(service)
    try:
        scores = await _fetch_all_scores(repo, resolved)
    except GenerationNotFoundError as exc:
        return JSONResponse(
            {"error": "short_generation_not_found", "detail": str(exc)[:200]},
            status_code=404,
        )
    except Exception as exc:  # Short-Lab failures stay Short-Lab errors.
        return JSONResponse(
            {"error": "shortlab_unavailable", "detail": f"{type(exc).__name__}"[:120]},
            status_code=503,
        )

    items: list[dict[str, Any]] = []
    for score in scores:
        try:
            feature = await repo.get_feature(score.feature_snapshot_id)
        except Exception:
            feature = None
        try:
            items.append(_serialize_item(score, feature, service, now_ms))
        except Exception:
            # Provider-partial rows still return 200 with null metrics.
            try:
                symbol = str(getattr(score, "symbol", ""))
                items.append({
                    "symbol": symbol,
                    "canonicalId": symbol.lower(),
                    "profile": str(getattr(score, "profile", "")),
                    "categories": [],
                    "status": str(getattr(score, "status", "CANDIDATE")),
                    "asOfStatus": str(getattr(score, "status", "CANDIDATE")),
                    "candidateStatus": str(getattr(score, "candidate_status", "CANDIDATE")),
                    "executionStatus": str(getattr(score, "execution_status", "NOT_READY")),
                    "ltss": _finite(getattr(score, "ltss", None)),
                    "entryScore": _finite(getattr(score, "entry_score", None)),
                    "dataQuality": _finite(getattr(score, "data_quality", None)),
                    "snapshotDataQuality": _finite(getattr(score, "data_quality", None)),
                    "moduleScores": dict(getattr(score, "module_scores", {}) or {}),
                    "metrics": {
                        "funding30d": None, "positiveFundingRatio30d": None,
                        "athDrawdown": None, "oiMarketCapRatio": None,
                        "futuresSpotVolumeRatio": None,
                    },
                    "vetoes": list(getattr(score, "vetoes", ()) or ()),
                    "warnings": list(getattr(score, "warnings", ()) or ()),
                    "pauses": list(getattr(score, "pauses", ()) or ()),
                    "reasons": list(getattr(score, "reasons", ()) or ()),
                    "dataAvailability": {},
                    "asOfMs": int(getattr(score, "as_of_ms", now_ms) or now_ms),
                    "stale": False,
                    "analysisTier": str(getattr(score, "analysis_tier", "LITE")),
                    "scoreVersion": str(getattr(score, "score_version", "")),
                    "featureVersion": str(getattr(score, "feature_version", "")),
                    "entryVersion": getattr(score, "entry_version", None),
                    "configHash": str(getattr(score, "config_hash", "")),
                    "snapshotId": str(getattr(score, "snapshot_id", "")),
                })
            except Exception:
                continue

    filtered = _apply_filters(
        items,
        status=status,
        candidate_status=candidate_status,
        execution_status=execution_status,
        category=category,
        profile=profile,
        min_ltss=_finite(min_ltss) if min_ltss is not None else None,
        min_entry=_finite(min_entry) if min_entry is not None else None,
        min_funding_30d=_finite(min_funding_30d) if min_funding_30d is not None else None,
        ath_min=_finite(ath_drawdown_min) if ath_drawdown_min is not None else None,
        ath_max=_finite(ath_drawdown_max) if ath_drawdown_max is not None else None,
        min_data_quality=_finite(min_data_quality) if min_data_quality is not None else None,
    )
    ordered = _apply_sort(filtered, sort, order)
    total = len(ordered)
    page_items = ordered[offset: offset + limit]

    generated_at_ms = now_ms
    try:
        if isinstance(job_row, Mapping) and job_row.get("finished_at_ms") is not None:
            generated_at_ms = int(job_row["finished_at_ms"])
    except (TypeError, ValueError):
        pass
    analysis_tier = "LITE"
    score_version = "ltss-lite-v1"
    if ordered:
        analysis_tier = str(ordered[0].get("analysisTier") or "LITE")
        score_version = str(ordered[0].get("scoreVersion") or score_version)
    else:
        try:
            analysis_tier = str(service.config.analysis_tier)  # type: ignore[attr-defined]
        except Exception:
            pass
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generationId": resolved,
        "generatedAtMs": generated_at_ms,
        "analysisTier": analysis_tier,
        "scoreVersion": score_version,
        "items": page_items,
        "total": total,
    }


@router.get("/symbol/{symbol}")
async def short_symbol_detail(
    symbol: str, request: Request, generation_id: str | None = Query(None)
) -> Any:
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    name = str(symbol).upper()
    try:
        detail = await service.detail(name, generation_id=generation_id)
    except SymbolNotFound:
        return JSONResponse({"error": "short_symbol_not_found", "symbol": name}, status_code=404)
    except GenerationNotFoundError as exc:
        return JSONResponse(
            {"error": "short_generation_not_found", "detail": str(exc)[:200]},
            status_code=404,
        )
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    now_ms = _service_now(service)
    score = detail.score
    feature = detail.feature
    entry = detail.entry
    item = _serialize_item(score, feature, service, now_ms)
    source_meta = _source_meta_map(feature)
    data_sources: dict[str, Any] = {}
    for field_name, entry_meta in source_meta.items():
        if not isinstance(entry_meta, Mapping):
            continue
        data_sources[field_name] = {
            "status": entry_meta.get("status"),
            "fetchedAtMs": entry_meta.get("fetched_at_ms"),
            "asOfMs": entry_meta.get("as_of_ms"),
            "coverageFraction": entry_meta.get("coverage_fraction"),
            "reasonCode": entry_meta.get("reason_code"),
            "source": entry_meta.get("source"),
        }
    feature_out: Any = None
    if feature is not None:
        feature_out = {
            "snapshotId": getattr(feature, "snapshot_id", None),
            "asOfMs": getattr(feature, "as_of_ms", None),
            "featureVersion": getattr(feature, "feature_version", None),
            "features": _feature_map(feature),
            "sourceMeta": dict(source_meta),
            "dataQuality": getattr(feature, "data_quality", None),
        }
    entry_out: Any = None
    if entry is not None:
        components = getattr(entry, "components", None)
        inputs = getattr(entry, "inputs", None)
        entry_out = {
            "snapshotId": getattr(entry, "snapshot_id", None),
            "asOfMs": getattr(entry, "as_of_ms", None),
            "entryVersion": getattr(entry, "entry_version", None),
            "entryScore": _finite(getattr(entry, "entry_score", None)),
            "components": dict(components) if isinstance(components, Mapping) else components,
            "inputs": dict(inputs) if isinstance(inputs, Mapping) else inputs,
        }
    body = dict(item)
    body.update({
        "schemaVersion": SCHEMA_VERSION,
        "generationId": detail.generation_id,
        "dataSources": data_sources,
        "feature": feature_out,
        "entry": entry_out,
    })
    return body


@router.get("/symbol/{symbol}/history")
async def short_symbol_history(
    symbol: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Any:
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    name = str(symbol).upper()
    repo = service._repository

    def _query_sync(repo_inner: Any, sym: str, lim: int, off: int) -> tuple[int, list[dict]]:
        con = repo_inner._require_con()
        total = con.execute(
            "SELECT count(*) FROM sl_score_snapshot WHERE symbol = ?", [sym]
        ).fetchone()[0]
        cur = con.execute(
            "SELECT * FROM sl_score_snapshot WHERE symbol = ? "
            "ORDER BY as_of_ms DESC, generation_id DESC, snapshot_id ASC "
            "LIMIT ? OFFSET ?",
            [sym, lim, off],
        )
        names = [col[0] for col in cur.description]
        rows = [dict(zip(names, row)) for row in cur.fetchall()]
        return int(total), rows

    try:
        total, rows = await repo._run(_query_sync, repo, name, limit, offset)
    except Exception as exc:
        return JSONResponse(
            {"error": "shortlab_unavailable", "detail": f"{type(exc).__name__}"[:120]},
            status_code=503,
        )
    if total == 0:
        return JSONResponse({"error": "short_symbol_not_found", "symbol": name}, status_code=404)
    history: list[dict[str, Any]] = []
    for row in rows:
        history.append({
            "snapshotId": row.get("snapshot_id"),
            "generationId": row.get("generation_id"),
            "asOfMs": row.get("as_of_ms"),
            "ltss": _finite(row.get("ltss")),
            "entryScore": _finite(row.get("entry_score")),
            "dataQuality": _finite(row.get("data_quality")),
            "status": row.get("status"),
            "candidateStatus": row.get("candidate_status"),
            "executionStatus": row.get("execution_status"),
            "profile": row.get("profile"),
            "analysisTier": row.get("analysis_tier"),
            "scoreVersion": row.get("score_version"),
        })
    return {"symbol": name, "history": history, "total": total}


@router.get("/providers")
async def short_providers(request: Request) -> Any:
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    now_ms = _service_now(service)
    try:
        config = service.config  # type: ignore[attr-defined]
    except Exception:
        config = getattr(service, "_config", None)
    registry = getattr(service, "registry", None)
    providers_cfg: Mapping[str, Any] = {}
    try:
        providers_cfg = getattr(config, "providers", {}) or {}
    except Exception:
        providers_cfg = {}
    names: list[str] = []
    for name in list(providers_cfg.keys()):
        if name not in names:
            names.append(str(name))
    try:
        for name in list(registry.names()):  # type: ignore[union-attr]
            if name not in names:
                names.append(str(name))
    except Exception:
        pass
    if not names:
        names = ["coingecko"]
    out: list[dict[str, Any]] = []
    for name in sorted(names):
        entry = providers_cfg.get(name) if isinstance(providers_cfg, Mapping) else None
        enabled = bool(getattr(entry, "enabled", False)) if entry is not None else False
        if isinstance(entry, Mapping):
            enabled = bool(entry.get("enabled", False))
        registered = False
        try:
            registered = bool(registry.has(name))  # type: ignore[union-attr]
        except Exception:
            registered = False
        if registered:
            status_str = "OK"
            reason = None
        elif enabled:
            status_str = "UNAVAILABLE"
            reason = "PROVIDER_NOT_REGISTERED"
        else:
            status_str = "UNAVAILABLE"
            reason = "PROVIDER_NOT_CONFIGURED"
        # Never expose secrets: only the enabled flag, never api_key_env values.
        out.append({
            "name": name,
            "enabled": enabled,
            "registered": registered,
            "status": status_str,
            "reasonCode": reason,
        })
    return {"providers": out, "generatedAtMs": now_ms}


@router.post("/refresh", status_code=202)
async def short_refresh(
    request: Request, job_type: str | None = Query(None), jobType: str | None = Query(None)
) -> Any:
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    body_job: str | None = None
    try:
        payload = await request.json()
        if isinstance(payload, Mapping):
            raw = payload.get("jobType", payload.get("job_type"))
            if isinstance(raw, str) and raw:
                body_job = raw
    except Exception:
        body_job = None
    effective = body_job or jobType or job_type or JOB_TYPE_SCORE_REFRESH
    try:
        ref = await service.refresh(effective)
    except UnknownJobType:
        return JSONResponse(
            {"error": "short_unknown_job_type", "jobType": effective}, status_code=422
        )
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    return JSONResponse(
        {"jobId": ref.job_id, "jobType": ref.job_type, "existing": bool(ref.existing)},
        status_code=202,
    )


@router.get("/refresh/{job_id}")
async def short_refresh_status(job_id: str, request: Request) -> Any:
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        status_obj = await service.job_status(job_id)
    except (JobNotFound, KeyError):
        return JSONResponse({"error": "short_job_not_found", "jobId": job_id}, status_code=404)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    return {
        "jobId": status_obj.job_id,
        "jobType": status_obj.job_type,
        "status": status_obj.status,
        "stats": dict(status_obj.stats or {}),
        "startedAtMs": status_obj.started_at_ms,
        "finishedAtMs": status_obj.finished_at_ms,
        "existing": bool(status_obj.existing),
        "errorCode": status_obj.error_code,
    }


@router.get("/evidence/summary")
async def short_evidence_summary(request: Request) -> Any:
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    filters = dict(request.query_params)
    try:
        result = await service.evidence_summary(filters)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    if isinstance(result, Unavailable) or (
        hasattr(result, "reason") and result.__class__.__name__ == "Unavailable"
    ):
        reason = str(getattr(result, "reason", EVIDENCE_UNAVAILABLE_REASON))
        detail = getattr(result, "detail", None)
        body: dict[str, Any] = {"error": reason}
        if detail:
            body["detail"] = str(detail)[:300]
        return JSONResponse(body, status_code=503)
    total = int(getattr(result, "total", 0) or 0)
    horizons = getattr(result, "horizons", {}) or {}
    generated_at = getattr(result, "generated_at_ms", None)
    if generated_at is None:
        generated_at = _service_now(service)
    try:
        horizons_out = {str(k): dict(v) if isinstance(v, Mapping) else v
                        for k, v in dict(horizons).items()}
    except Exception:
        horizons_out = {}
    return {
        "filters": dict(getattr(result, "filters", filters) or {}),
        "horizons": horizons_out,
        "total": total,
        "generatedAtMs": int(generated_at),
    }


# ---------------------------------------------------------------------------
# H08 hedge routes (design B32, 14 categories; frozen DTOs, camelCase wire)
# ---------------------------------------------------------------------------


@router.get("/funding-opportunities")
async def hedge_funding_opportunities(request: Request) -> Any:
    """B32.1 funding opportunities (query snake aliases, camelCase JSON)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    filters = dict(request.query_params)
    try:
        result = await service.funding_opportunities(filters)
    except Exception as exc:
        try:
            from diveintocrypto_desktop.shortlab.service import HedgeUnavailable as _HU

            if isinstance(exc, _HU):
                return _hedge_error(503, "HEDGE_UNAVAILABLE", "HEDGE_UNAVAILABLE", str(exc)[:200])
        except Exception:
            pass
        return _map_hedge_exception(exc)
    return result


@router.get("/hedge/venues/{symbol}")
async def hedge_venues(symbol: str, request: Request) -> Any:
    """B32.2 venue quotes for one symbol (all venues, never only best)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    query = dict(request.query_params)
    # notional_usd alias (camel + snake).
    notional = query.get("notional_usd", query.get("notionalUsd"))
    try:
        result = await service.hedge_venues(symbol, notional)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.post("/hedge/simulate")
async def hedge_simulate(request: Request) -> Any:
    """B32.3 simulate (compute only, persist immutable snapshot, no plan)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        payload = await request.json()
    except Exception:
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be JSON")
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be a JSON object")
    try:
        result = await service.simulate(payload)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return JSONResponse(result, status_code=200)


@router.get("/hedge/simulations/{simulation_id}")
async def hedge_get_simulation(simulation_id: str, request: Request) -> Any:
    """B32.3.1 read-only simulation (expired still 200+expired:true, missing 404)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        result = await service.get_simulation(simulation_id)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.post("/hedge/plans")
async def hedge_create_plan(request: Request) -> Any:
    """B32.4 save plan (idempotency first, then expiry/version/content)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        payload = await request.json()
    except Exception:
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be JSON")
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be a JSON object")
    try:
        result = await service.save_plan(payload)
    except Exception as exc:
        return _map_hedge_exception(exc)
    # Idempotent retry returns 200 with existing:true; first creation is 201.
    status = 200 if bool(result.get("existing")) else 201
    return JSONResponse(result, status_code=status)


@router.get("/hedge/plans")
async def hedge_list_plans(request: Request) -> Any:
    """B32.5 list plans (status/symbol/mode/venue/limit/offset)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    filters = dict(request.query_params)
    try:
        result = await service.list_plans(filters)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.get("/hedge/plans/{plan_id}")
async def hedge_get_plan(plan_id: str, request: Request) -> Any:
    """B32.6 full plan + actual legs + latest monitor + alerts."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        result = await service.get_plan(plan_id)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.patch("/hedge/plans/{plan_id}/legs")
async def hedge_apply_leg_event(plan_id: str, request: Request) -> Any:
    """B32.7 manual fill event (event/client/version, idempotent, CAS)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        payload = await request.json()
    except Exception:
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be JSON")
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be a JSON object")
    try:
        result = await service.apply_leg_event(plan_id, payload)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.post("/hedge/plans/{plan_id}/activate")
async def hedge_activate_plan(plan_id: str, request: Request) -> Any:
    """B32.8 activate (local state only, 409 when legs incomplete)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        payload = await request.json()
    except Exception:
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be JSON")
    if not isinstance(payload, Mapping):
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be a JSON object")
    try:
        result = await service.activate(plan_id, payload)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.post("/hedge/plans/{plan_id}/close")
async def hedge_close_plan(plan_id: str, request: Request) -> Any:
    """B32.9 close (only after both legs exited, 409 when open qty remains)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        payload = await request.json()
    except Exception:
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be JSON")
    if not isinstance(payload, Mapping):
        return _hedge_error(422, "HEDGE_INPUT_INVALID", "HEDGE_INPUT_INVALID", "body must be a JSON object")
    try:
        result = await service.close(plan_id, payload)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.get("/hedge/plans/{plan_id}/monitor")
async def hedge_monitor(plan_id: str, request: Request) -> Any:
    """B32.10 monitor snapshot (ratio/exposure/PnL/funding/basis/liq/exit/safety/alerts)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        result = await service.monitor(plan_id)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.get("/hedge/alerts")
async def hedge_alerts(request: Request) -> Any:
    """B32.11 alerts (plan_id/state/severity/code)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    filters = dict(request.query_params)
    try:
        # Service exposes both hedge_alerts and the H08 alias alerts.
        fn = getattr(service, "hedge_alerts", None) or getattr(service, "alerts", None)
        result = await fn(filters)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.post("/hedge/alerts/{alert_id}/ack")
async def hedge_ack_alert(alert_id: str, request: Request) -> Any:
    """B32.12 ack a local alert (never resolves as fixed)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    try:
        result = await service.ack_hedge_alert(alert_id)
    except Exception as exc:
        return _map_hedge_exception(exc)
    return result


@router.get("/hedge/evidence/summary")
async def hedge_evidence_summary(request: Request) -> Any:
    """B32.13 independent hedge evidence (directional evidence stays separate)."""
    try:
        service = _require_service(request)
    except ShortLabUnavailable as exc:
        return _unavailable_response(str(exc))
    filters = dict(request.query_params)
    try:
        result = await service.hedge_evidence_summary(filters)
    except Exception as exc:
        return _map_hedge_exception(exc)
    if isinstance(result, Unavailable) or (
        hasattr(result, "reason") and result.__class__.__name__ == "Unavailable"
    ):
        reason = str(getattr(result, "reason", "HEDGE_EVIDENCE_UNAVAILABLE"))
        detail = getattr(result, "detail", None)
        body: dict[str, Any] = {"error": reason, "reason": reason, "reason_code": reason}
        if detail:
            body["detail"] = str(detail)[:300]
        return JSONResponse(body, status_code=503)
    # H10 real shape passes through; normalise the camelCase wire view.
    if isinstance(result, Mapping):
        out = dict(result)
        # Ensure camelCase aliases for the B32.13 contract.
        if "generated_at_ms" in out and "generatedAt" not in out:
            out["generatedAt"] = out.pop("generated_at_ms")
        return out
    try:
        import dataclasses as _dataclasses

        if _dataclasses.is_dataclass(result):
            summary = _dataclasses.asdict(result)
            strategy = str(summary.get("strategy") or "")
            horizon_days = int(summary.get("horizon_days") or 0)
            report = summary.get("evaluation_report") if isinstance(summary.get("evaluation_report"), Mapping) else {}
            dimensions = report.get("bucket") if isinstance(report.get("bucket"), Mapping) else {}
            history_values = dimensions.get("history_class")
            if isinstance(history_values, (list, tuple)):
                history_class = ", ".join(str(v) for v in history_values) if history_values else "UNKNOWN"
            else:
                history_class = str(history_values or "UNKNOWN")
            bucket = {
                "strategy": strategy,
                "horizon": f"{horizon_days}D",
                "horizonDays": horizon_days,
                "cohort": summary.get("cohort") or "MIXED_COHORTS",
                "historyClass": history_class,
                "total": int(summary.get("sample_count") or 0),
                "sampleCount": int(summary.get("sample_count") or 0),
                "PENDING": int(summary.get("pending_count") or 0),
                "COMPLETE": int(summary.get("complete_count") or 0),
                "CENSORED": int(summary.get("censored_count") or 0),
                "UNAVAILABLE": int(summary.get("unavailable_count") or 0),
                "meanNetReturn": summary.get("avg_net_return"),
                "medianNetReturn": summary.get("median_net_return"),
                "pairedCount": int(summary.get("paired_count") or 0),
                "pairedMissingCount": int(summary.get("paired_missing_count") or 0),
                "pairedMeanStrategyReturn": summary.get("paired_mean_strategy_return"),
                "pairedMeanUnhedgedReturn": summary.get("paired_mean_unhedged_return"),
                "pairedMeanDiff": summary.get("paired_mean_diff"),
                "evaluation": report,
                "sampleStatus": report.get("sample_status"),
                "subBuckets": [
                    {
                        "bucket": dict(item.get("bucket") or {}),
                        "sampleCount": int(item.get("sample_count") or 0),
                        "completeCount": int(item.get("complete_count") or 0),
                        "censoredCount": int(item.get("censored_count") or 0),
                        "unavailableCount": int(item.get("unavailable_count") or 0),
                        "pendingCount": int(item.get("pending_count") or 0),
                        "evaluation": dict(item.get("evaluation") or {}),
                    }
                    for item in (report.get("sub_buckets") or [])
                    if isinstance(item, Mapping)
                ],
            }
            return {
                "generatedAt": int(_service_now(service)),
                "filters": dict(filters),
                "total": bucket["total"],
                "buckets": [bucket],
                "summary": summary,
            }
        return {
            "generatedAt": int(getattr(result, "generated_at_ms", _service_now(service))),
            "filters": dict(getattr(result, "filters", filters) or {}),
            "buckets": list(getattr(result, "buckets", []) or []),
        }
    except Exception as exc:
        return _hedge_error(503, "HEDGE_EVIDENCE_UNAVAILABLE", "HEDGE_EVIDENCE_UNAVAILABLE", str(exc)[:160])

