"""R05 funding entry gate (D05.3, pure, no network/DB/clock reads).

Frozen port signature (docs/contracts/shortlab_repair_service_calls.json)::

    evaluate_funding_entry_gate(
        context: FundingContext, policy: Mapping, as_of_ms: int,
    ) -> GateResult

Consumes all eight ``funding_capture.entry_gate`` keys (D15)::

    require_current_positive, require_last_settled_positive,
    min_funding_7d, min_funding_30d,
    min_positive_ratio_30d, min_positive_ratio_90d,
    min_30d_coverage, min_90d_coverage.

Rules (D05.3):

- ``current``/``last settled`` strictly ``> 0`` when the corresponding
  ``require_*`` switch is true (``False`` skips only that condition).
- Current rate older than 120s (``hedge.freshness.funding_current.ttl``,
  default 120) can never PASS (``FUNDING_CURRENT_STALE``). The last settled
  rate is NOT judged by the 120s TTL; it must come from the latest expected
  settled slot with 60s event-match tolerance
  (``optimization.funding_schedule.event_match_tolerance_sec``).
- 7D/30D sums, positive ratios and coverages must meet their minima.
  Missing/unknown schedule, history class or required ratios return
  UNKNOWN; explicit failures return FAIL.
- ``PARTIAL_90D`` (reliable 30-89D listing) skips both 90D numeric gates and
  records ``FUNDING_90D_NA`` while still enforcing the 30D gates. Unknown
  listing (``HISTORY_CLASS_UNKNOWN``) never borrows ``first_seen`` as a
  listing date and returns UNKNOWN.
- FCS 100-point weights are scoring only; a high FCS never overrides FAIL.
  This module never reads FCS inputs.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.repair_contracts import (
    FundingContext,
    GateResult,
)

__all__ = ["evaluate_funding_entry_gate"]

_REQUIRED_GATE_KEYS = (
    "require_current_positive",
    "require_last_settled_positive",
    "min_funding_7d",
    "min_funding_30d",
    "min_positive_ratio_30d",
    "min_positive_ratio_90d",
    "min_30d_coverage",
    "min_90d_coverage",
)

_DEFAULT_CURRENT_TTL_MS = 120_000
_DEFAULT_MATCH_TOLERANCE_MS = 60_000
_FUTURE_SKEW_MS = 2_000


# ---------------------------------------------------------------------------
# Policy extraction (all eight keys consumed)
# ---------------------------------------------------------------------------

def _as_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be bool, got {value!r}")
    return value


def _as_float(name: str, value: Any, *, lo: float | None = None, hi: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    out = float(value)
    if lo is not None and out < lo - 1e-12:
        raise ValueError(f"{name} must be >= {lo}, got {value!r}")
    if hi is not None and out > hi + 1e-12:
        raise ValueError(f"{name} must be <= {hi}, got {value!r}")
    return out


def _extract_entry_gate(policy: Any) -> dict[str, Any]:
    """Return the eight entry-gate params; every key is read (no silent skip)."""
    node: Any = None
    if hasattr(policy, "funding_capture"):
        fc = policy.funding_capture  # ShortLabConfig
        entry = getattr(fc, "entry_gate", None)
        if isinstance(entry, Mapping):
            node = dict(entry)
        else:  # pragma: no cover - defensive
            raise ValueError("policy.funding_capture.entry_gate must be a mapping")
    elif hasattr(policy, "entry_gate"):
        entry2 = getattr(policy, "entry_gate")  # FundingCaptureConfig
        node = dict(entry2) if isinstance(entry2, Mapping) else None
        if node is None:  # pragma: no cover
            raise ValueError("policy.entry_gate must be a mapping")
    elif isinstance(policy, Mapping):
        work: Any = policy
        if "funding_capture" in work and isinstance(work["funding_capture"], Mapping):
            work = work["funding_capture"]
        if "entry_gate" in work and isinstance(work["entry_gate"], Mapping):
            node = dict(work["entry_gate"])
        elif all(k in work for k in _REQUIRED_GATE_KEYS):
            node = {k: work[k] for k in _REQUIRED_GATE_KEYS}
        else:
            raise ValueError(
                f"policy must carry entry_gate {{{', '.join(_REQUIRED_GATE_KEYS)}}}"
            )
    else:
        raise ValueError("policy must carry funding_capture entry_gate")
    missing = [k for k in _REQUIRED_GATE_KEYS if k not in node]
    if missing:
        raise ValueError(f"entry_gate is missing keys: {missing}")
    # Consume every key with strict typing (unknown keys are ignored here;
    # strict unknown-key rejection lives in config.py, the R00 owner).
    return {
        "require_current_positive": _as_bool(
            "entry_gate.require_current_positive", node["require_current_positive"]
        ),
        "require_last_settled_positive": _as_bool(
            "entry_gate.require_last_settled_positive",
            node["require_last_settled_positive"],
        ),
        "min_funding_7d": _as_float("entry_gate.min_funding_7d", node["min_funding_7d"]),
        "min_funding_30d": _as_float("entry_gate.min_funding_30d", node["min_funding_30d"]),
        "min_positive_ratio_30d": _as_float(
            "entry_gate.min_positive_ratio_30d", node["min_positive_ratio_30d"], lo=0, hi=1
        ),
        "min_positive_ratio_90d": _as_float(
            "entry_gate.min_positive_ratio_90d", node["min_positive_ratio_90d"], lo=0, hi=1
        ),
        "min_30d_coverage": _as_float(
            "entry_gate.min_30d_coverage", node["min_30d_coverage"], lo=0, hi=1
        ),
        "min_90d_coverage": _as_float(
            "entry_gate.min_90d_coverage", node["min_90d_coverage"], lo=0, hi=1
        ),
    }


def _extract_ttl_ms(policy: Any) -> int:
    """Current-rate TTL ms (default 120s); read when present, never guessed."""
    try:
        if hasattr(policy, "hedge"):
            hedge = getattr(policy, "hedge")
            fresh = getattr(hedge, "freshness", None)
            if isinstance(fresh, Mapping):
                node = fresh.get("funding_current", fresh.get("fundingCurrent"))
                if isinstance(node, Mapping):
                    ttl = node.get("ttl", node.get("ttl_sec", node.get("ttlSec")))
                    if ttl is not None and not isinstance(ttl, bool):
                        return int(float(ttl) * 1000)
                elif hasattr(node, "ttl"):
                    return int(float(node.ttl) * 1000)
        if isinstance(policy, Mapping):
            node2: Any = policy
            if "hedge" in node2 and isinstance(node2["hedge"], Mapping):
                node2 = node2["hedge"]
                if "freshness" in node2 and isinstance(node2["freshness"], Mapping):
                    grp = node2["freshness"].get("funding_current", node2["freshness"].get("fundingCurrent"))
                    if isinstance(grp, Mapping):
                        ttl2 = grp.get("ttl", grp.get("ttl_sec"))
                        if ttl2 is not None and not isinstance(ttl2, bool):
                            return int(float(ttl2) * 1000)
    except (TypeError, ValueError):
        pass
    return _DEFAULT_CURRENT_TTL_MS


def _extract_tolerance_ms(policy: Any) -> int:
    """Slot-match tolerance ms (default 60s); read when present."""
    try:
        if hasattr(policy, "optimization"):
            opt = getattr(policy, "optimization")
            fs = getattr(opt, "funding_schedule", None)
            if isinstance(fs, Mapping):
                tol = fs.get("event_match_tolerance_sec", fs.get("eventMatchToleranceSec"))
                if tol is not None and not isinstance(tol, bool):
                    return int(float(tol) * 1000)
        if isinstance(policy, Mapping):
            node: Any = policy
            if "optimization" in node and isinstance(node["optimization"], Mapping):
                fs2 = node["optimization"].get("funding_schedule", node["optimization"].get("fundingSchedule"))
                if isinstance(fs2, Mapping):
                    tol2 = fs2.get("event_match_tolerance_sec", fs2.get("eventMatchToleranceSec"))
                    if tol2 is not None and not isinstance(tol2, bool):
                        return int(float(tol2) * 1000)
    except (TypeError, ValueError):
        pass
    return _DEFAULT_MATCH_TOLERANCE_MS


# ---------------------------------------------------------------------------
# Observation helpers
# ---------------------------------------------------------------------------

def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def _obs_source_as_of(obs: Any) -> int | None:
    if obs is None:
        return None
    meta = getattr(obs, "meta", None)
    if meta is None and isinstance(obs, Mapping):
        meta = obs.get("meta", obs)
    if meta is None:
        return None
    if isinstance(meta, Mapping):
        v = meta.get("source_as_of_ms", meta.get("sourceAsOf"))
        try:
            return None if v is None else int(v)
        except (TypeError, ValueError):
            return None
    for attr in ("source_as_of_ms", "sourceAsOf"):
        if hasattr(meta, attr):
            try:
                v = getattr(meta, attr)
                return None if v is None else int(v)
            except (TypeError, ValueError):
                return None
    return None


def _obs_known_at(obs: Any) -> int | None:
    if obs is None:
        return None
    meta = getattr(obs, "meta", None)
    if meta is None and isinstance(obs, Mapping):
        meta = obs.get("meta", obs)
    if meta is None:
        return None
    if isinstance(meta, Mapping):
        for key in ("known_at_ms", "knownAt", "known_at", "fetched_at_ms"):
            if key in meta and meta[key] is not None:
                try:
                    return int(meta[key])
                except (TypeError, ValueError):
                    return None
        return None
    for attr in ("known_at_ms", "known_at", "fetched_at_ms"):
        if hasattr(meta, attr):
            try:
                v = getattr(meta, attr)
                return None if v is None else int(v)
            except (TypeError, ValueError):
                return None
    return None


def _obs_event_time(obs: Any) -> int | None:
    """Settled funding time carried in the observation value, if any."""
    if obs is None:
        return None
    value = getattr(obs, "value", None)
    if value is None and isinstance(obs, Mapping):
        value = obs.get("value", obs)
    if isinstance(value, Mapping):
        for key in ("funding_time_ms", "fundingTime", "t", "funding_time", "event_time_ms"):
            if key in value and value[key] is not None:
                try:
                    return int(value[key])
                except (TypeError, ValueError):
                    return None
        return None
    for attr in ("funding_time_ms", "fundingTime", "t"):
        if hasattr(value, attr):
            try:
                return int(getattr(value, attr))
            except (TypeError, ValueError):
                return None
    return None


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def evaluate_funding_entry_gate(
    context: FundingContext, policy: Mapping, as_of_ms: int
) -> GateResult:
    """Evaluate the shared funding entry gate (D05.3). Pure."""
    if not isinstance(context, FundingContext):
        raise TypeError(f"context must be FundingContext, got {type(context).__name__}")
    try:
        as_of = int(as_of_ms)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"as_of_ms must be an int: {exc}") from exc
    if as_of < 0:
        raise ValueError("as_of_ms must be >= 0")

    gate = _extract_entry_gate(policy)
    ttl_ms = _extract_ttl_ms(policy)
    tol_ms = _extract_tolerance_ms(policy)

    metrics = context.metrics
    unknown_reasons: list[str] = []
    fail_reasons: list[str] = []

    # -- history class -------------------------------------------------------
    if context.history_class == "HISTORY_CLASS_UNKNOWN":
        unknown_reasons.append("HISTORY_CLASS_UNKNOWN")

    # -- schedule verifiability (30D always; 90D skipped for PARTIAL) --------
    cov30 = context.coverage_30d
    if (
        cov30.expected_count is None
        or cov30.coverage_fraction is None
        or "FUNDING_SCHEDULE_UNKNOWN" in tuple(cov30.reasons)
    ):
        if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
            unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")
    is_partial = context.history_class == "PARTIAL_90D"
    cov90 = context.coverage_90d
    if not is_partial:
        if (
            cov90.expected_count is None
            or cov90.coverage_fraction is None
            or "FUNDING_SCHEDULE_UNKNOWN" in tuple(cov90.reasons)
        ):
            if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
                unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")
    # PARTIAL records N/A for the skipped 90D numeric gates (kept on PASS).
    na_reasons: list[str] = ["FUNDING_90D_NA"] if is_partial else []

    # -- required numeric presence (missing => UNKNOWN, never 0-fill) --------
    f7d = _parse_decimal(metrics.funding_7d)
    f30d = _parse_decimal(metrics.funding_30d)
    pr30 = _parse_decimal(metrics.positive_ratio_30d)
    cov30d = _parse_decimal(
        cov30.coverage_fraction if cov30.coverage_fraction is not None
        else metrics.coverage_30d
    )
    if f7d is None or f30d is None or pr30 is None or cov30d is None:
        if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
            unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")
    pr90: Decimal | None = None
    cov90d: Decimal | None = None
    f90d: Decimal | None = None
    if not is_partial:
        pr90 = _parse_decimal(metrics.positive_ratio_90d)
        cov90d = _parse_decimal(
            cov90.coverage_fraction if cov90.coverage_fraction is not None
            else metrics.coverage_90d
        )
        f90d = _parse_decimal(metrics.funding_90d)
        # 90D sums/ratios missing (non-PARTIAL) => UNKNOWN. Note funding_90d
        # itself has no gate threshold, but a missing 90D history still means
        # the 90D window cannot be verified.
        if pr90 is None or cov90d is None:
            if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
                unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")

    cur = _parse_decimal(metrics.current_rate)
    last = _parse_decimal(metrics.last_settled_rate)
    if cur is None or last is None:
        if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
            unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")

    # -- observation provenance (known_at <= as_of, source clock skew) -------
    cur_obs = context.current_observation
    last_obs = context.last_settled_observation
    cur_source = _obs_source_as_of(cur_obs)
    cur_known = _obs_known_at(cur_obs)
    last_source = _obs_source_as_of(last_obs)
    last_known = _obs_known_at(last_obs)
    if cur_obs is None or cur_source is None or cur_known is None:
        if "FUNDING_CURRENT_STALE" not in unknown_reasons:
            unknown_reasons.append("FUNDING_CURRENT_STALE")
    else:
        if int(cur_known) > as_of:
            if "FUNDING_CURRENT_STALE" not in unknown_reasons:
                unknown_reasons.append("FUNDING_CURRENT_STALE")
        elif int(cur_source) > as_of + _FUTURE_SKEW_MS:
            if "FUNDING_CURRENT_STALE" not in unknown_reasons:
                unknown_reasons.append("FUNDING_CURRENT_STALE")
    if last_obs is None or last_source is None or last_known is None:
        if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
            unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")
    else:
        if int(last_known) > as_of:
            if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
                unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")
        elif int(last_source) > as_of + _FUTURE_SKEW_MS:
            if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
                unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")
        else:
            # Slot consistency: value funding_time (when carried) must agree
            # with the observation source time within 60s tolerance.
            evt_time = _obs_event_time(last_obs)
            if evt_time is not None and abs(int(evt_time) - int(last_source)) > tol_ms:
                if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
                    unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")

    # -- CR09 (D05.3): require the exact latest expected settled slot --------
    # It is frozen by ProductionHedgeMarket from the CONFIRMED schedule and
    # carried through existing input_refs. Window counts cannot identify a
    # schedule anchor, and an event one interval old can still fall within an
    # age threshold immediately after a missed settlement.
    refs = context.input_refs if isinstance(context.input_refs, Mapping) else {}
    try:
        checked_at = int(refs.get("schedule_checked_at_ms"))
        expected_slot = int(refs.get("last_expected_slot_ms"))
    except (TypeError, ValueError):
        checked_at = expected_slot = None
    if (
        checked_at != as_of
        or expected_slot is None
        or expected_slot < 0
        or expected_slot > as_of
    ):
        if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
            unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")
    elif last_obs is not None and last_source is not None:
        event_time = _obs_event_time(last_obs)
        if (
            event_time is None
            or abs(int(event_time) - expected_slot) > int(tol_ms)
            or abs(int(last_source) - expected_slot) > int(tol_ms)
        ):
            if "FUNDING_SCHEDULE_UNKNOWN" not in unknown_reasons:
                unknown_reasons.append("FUNDING_SCHEDULE_UNKNOWN")

    input_refs = dict(context.input_refs) if isinstance(context.input_refs, Mapping) else {}
    if unknown_reasons:
        return GateResult(
            "UNKNOWN", tuple(sorted(set(unknown_reasons))), as_of, input_refs
        )

    # -- explicit failures (all thresholds use >=; equal passes) -------------
    if gate["require_current_positive"] and cur is not None and cur <= 0:
        fail_reasons.append("FUNDING_CURRENT_NON_POSITIVE")
    if gate["require_last_settled_positive"] and last is not None and last <= 0:
        fail_reasons.append("FUNDING_LAST_NON_POSITIVE")
    # Current freshness: strictly greater than TTL fails.
    if cur_source is not None and (as_of - int(cur_source)) > int(ttl_ms):
        fail_reasons.append("FUNDING_CURRENT_STALE")
    if f7d is not None and float(f7d) < float(gate["min_funding_7d"]) - 1e-12:
        fail_reasons.append("FUNDING_HISTORY_BELOW_MIN")
    if f30d is not None and float(f30d) < float(gate["min_funding_30d"]) - 1e-12:
        fail_reasons.append("FUNDING_HISTORY_BELOW_MIN")
    if pr30 is not None and float(pr30) < float(gate["min_positive_ratio_30d"]) - 1e-12:
        fail_reasons.append("FUNDING_POSITIVE_RATIO_LOW")
    if not is_partial:
        if pr90 is not None and float(pr90) < float(gate["min_positive_ratio_90d"]) - 1e-12:
            fail_reasons.append("FUNDING_POSITIVE_RATIO_LOW")
    if cov30d is not None and float(cov30d) < float(gate["min_30d_coverage"]) - 1e-12:
        fail_reasons.append("FUNDING_COVERAGE_LOW")
    if not is_partial:
        if cov90d is not None and float(cov90d) < float(gate["min_90d_coverage"]) - 1e-12:
            fail_reasons.append("FUNDING_COVERAGE_LOW")

    if fail_reasons:
        return GateResult("FAIL", tuple(sorted(set(fail_reasons))), as_of, input_refs)
    # PASS keeps the PARTIAL N/A marker so callers can tell 90D was skipped.
    return GateResult("PASS", tuple(sorted(set(na_reasons))), as_of, input_refs)
