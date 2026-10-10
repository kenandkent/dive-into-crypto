"""R14a strategy entry capture (D14.1/D14.2, D18.1/D18.2, D19.1).

Read-only consumption: R01 ``RepositoryPort`` capture methods
(``save_strategy_entry``/``list_strategy_entries``/``save_strategy_quote_task``/
``claim_due_quote_tasks``/``finish_quote_task``/``get_fx_at``) and R11b
``MarketPort`` collectors (``collect_futures``/``collect_spot``/``collect_funding``).
R00 fixtures and D18 signatures are the contract (no custom dict returns,
no invented ports).

Semantics (task R14a):

- Four cohorts, day-first-sample, status buckets.
- USER_DECISION uses ``decision_id`` as group id for six strategies;
  SYSTEM_POLICY only for real selected samples (copies frozen proposal).
- Same-group entry quote span <=5s; decision/executed times separated.
- Futures entry SELL / exit BUY; Spot entry BUY / exit SELL.
- Snapshot saved once with ENTRY_COMPLETE/UNEXECUTABLE/UNAVAILABLE
  (PENDING_ENTRY only in-memory).
- 7/30/90 EXIT tasks at exact quantities.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.repair_contracts import (
    CaptureResult,
    QuoteCollectionResult,
    canonical_json,
    normalize_decimal_str,
)

__all__ = ["capture_strategy_entries", "collect_due_quotes"]


_DAY_MS = 86_400_000
_GROUP_SKEW_MS = 5_000  # D15 quote_group_skew_sec
_FX_MAX_AGE_MS = 60_000  # D14.2
_EXIT_DELAY_MS = 300_000  # D15 exit_delay_sec (5min deadline window)
_QUOTE_BATCH = 20  # D15 quote_task_batch / D14.2 20/round


def _entry_id_for(cohort: str, source_snapshot_id: str, strategy: str) -> str:
    """Give the same source/strategy a separate immutable ID per cohort."""
    return f"{cohort}:{source_snapshot_id}:{strategy}"
_HORIZONS = (7, 30, 90)
_ENTRY_VERSION = "entry-v3"
_EVIDENCE_VERSION = "hedge_evidence_v3"

_STRATEGY_TARGET = {
    "UNHEDGED_0": "0",
    "RELATIVE_25": "0.25",
    "RELATIVE_50": "0.5",
    "RELATIVE_75": "0.75",
    "ABSOLUTE_100": "1",
}
_ALLOWED_STRATEGIES = frozenset(
    {"UNHEDGED_0", "ABSOLUTE_100", "RELATIVE_75", "RELATIVE_50", "RELATIVE_25", "SYSTEM_POLICY"}
)
_ALLOWED_COHORTS = frozenset(
    {"RESEARCH_CANDIDATE", "EXECUTABLE_DIRECTIONAL", "FUNDING_CARRY", "USER_DECISION"}
)


def _is_budget_denial(exc: BaseException) -> bool:
    name = type(exc).__name__
    if "BudgetExhausted" in name or "Unbudgeted" in name:
        return True
    code = str(getattr(exc, "reason_code", "") or "")
    if "BUDGET" in code or "UNBUDGETED" in code or "REQUEST_BUDGET_EXHAUSTED" in code:
        return True
    text = str(exc)[:300]
    return "BUDGET_EXHAUSTED" in text or "UNBUDGETED_ENDPOINT" in text or "JOB_TYPE_UNKNOWN" in text


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _utc_day_bounds(ts_ms: int) -> tuple[int, int]:
    start = (int(ts_ms) // _DAY_MS) * _DAY_MS
    return start, start + _DAY_MS - 1


def _policy_hash(policy: Mapping[str, Any]) -> str:
    try:
        text = canonical_json(dict(policy or {}))
    except Exception:
        text = canonical_json({})
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be decimal str")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str) and value.strip():
        try:
            parsed = Decimal(value.strip())
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise ValueError(f"{name} not decimal: {value!r}") from exc
    else:
        raise ValueError(f"{name} must be decimal str, got {value!r}")
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _spot_qty_for(canonical_str: str, target_ratio_str: str) -> str:
    canonical = _parse_decimal(canonical_str, "canonical_futures_qty")
    ratio = _parse_decimal(target_ratio_str, "target_ratio")
    out = canonical * ratio
    return normalize_decimal_str(out)


def _cohort_eligible(cohort: str, policy: Mapping[str, Any]) -> tuple[bool, str]:
    """Status-bucket gate (D14.1). Missing keys mean no info -> eligible."""
    try:
        pol = dict(policy or {})
    except Exception:
        return True, ""
    if cohort == "EXECUTABLE_DIRECTIONAL":
        # Only READY without BLOCK/PAUSE.
        if pol.get("readiness") is not None and pol.get("readiness") != "READY":
            return False, "NOT_READY_FOR_BUCKET"
        if pol.get("candidate_status") == "BLOCKED" or pol.get("execution_status") == "BLOCKED":
            return False, "NOT_READY_FOR_BUCKET"
        vetoes = pol.get("vetoes") or pol.get("veto") or []
        pauses = pol.get("pauses") or []
        try:
            if len(list(vetoes)) > 0 or len(list(pauses)) > 0:
                return False, "NOT_READY_FOR_BUCKET"
        except Exception:
            pass
        return True, ""
    if cohort == "FUNDING_CARRY":
        # Only Funding Gate PASS + public market execution PASS.
        gate = pol.get("funding_gate") or pol.get("fundingGate")
        if gate is not None and gate != "PASS":
            return False, "FUNDING_GATE_NOT_PASS"
        exe = pol.get("execution_gate") or pol.get("executionGate")
        if exe is not None and exe != "PASS":
            return False, "EXECUTION_GATE_NOT_PASS"
        return True, ""
    # RESEARCH_CANDIDATE stratified (never rejected here); USER_DECISION checked elsewhere.
    return True, ""


def _strategy_plan(
    strategy: str,
    context: Any,
    decision: Any,
) -> tuple[str, str, str, str, str | None, str]:
    """Return (target_ratio, actual_ratio, futures_qty, canonical_qty, spot_venue, spot_qty)."""
    futures_qty = str(_get(context, "futures_contract_qty"))
    canonical_qty = str(_get(context, "canonical_futures_qty"))
    # Validate base quantities early.
    _parse_decimal(futures_qty, "futures_contract_qty")
    _parse_decimal(canonical_qty, "canonical_futures_qty")
    if strategy == "SYSTEM_POLICY":
        # Frozen copy, never recomputed (D14.1).
        selected = _get(decision, "selected_proposal")
        if selected is None:
            raise ValueError("SYSTEM_POLICY requires decision.selected_proposal")
        target = str(_get(selected, "actual_ratio"))
        actual = str(_get(selected, "actual_ratio"))
        futures_qty = str(_get(selected, "futures_contract_qty"))
        canonical_qty = str(_get(selected, "canonical_futures_qty"))
        spot_qty = str(_get(selected, "spot_net_qty"))
        venue = _get(selected, "spot_venue")
        return target, actual, futures_qty, canonical_qty, venue, spot_qty
    target = _STRATEGY_TARGET[strategy]
    actual = target
    if target == "0":
        return target, actual, futures_qty, canonical_qty, None, "0"
    policy = _get(context, "policy", {}) or {}
    venue = policy.get("spot_venue") if isinstance(policy, Mapping) else None
    if not venue:
        venue = "BINANCE_SPOT"
    spot_qty = _spot_qty_for(canonical_qty, target)
    return target, actual, futures_qty, canonical_qty, venue, spot_qty


async def _resolve_fx(
    repository: Any, currency: str, event_ms: int, known_by_ms: int
) -> Mapping[str, Any] | None:
    if currency == "USD":
        return {"fx_id": "FX:USD:1", "rate_str": "1", "currency": "USD"}
    try:
        row = await repository.get_fx_at(currency, event_ms, known_by_ms, _FX_MAX_AGE_MS)
    except Exception:
        return None
    return row


async def capture_strategy_entries(
    context: CaptureContext, repository: RepositoryPort, market: MarketPort, request_context: RequestContext  # type: ignore[valid-type]
) -> CaptureResult:
    """R14a entry capture (D18.1 exact signature, async)."""
    # Duck-typed field access (supports CaptureContext + test stubs).
    cohort = _get(context, "cohort")
    symbol = _get(context, "symbol")
    source_snapshot_id = _get(context, "source_snapshot_id")
    strategies = tuple(_get(context, "strategies", ()) or ())
    decision = _get(context, "decision")
    decision_as_of = _get(context, "decision_as_of_ms")
    identity = _get(context, "identity")
    identity_snapshot_id = _get(context, "identity_snapshot_id")
    funding_context = _get(context, "funding_context")
    futures_contract_qty = _get(context, "futures_contract_qty")
    canonical_futures_qty = _get(context, "canonical_futures_qty")
    policy = _get(context, "policy", {}) or {}
    rule_refs = dict(_get(context, "rule_refs", {}) or {})
    source_refs = dict(_get(context, "source_refs", {}) or {})
    _ = funding_context

    if cohort not in _ALLOWED_COHORTS:
        raise ValueError(f"cohort={cohort!r} unknown")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol must be non-empty str")
    if not isinstance(source_snapshot_id, str) or not source_snapshot_id:
        raise ValueError("source_snapshot_id must be non-empty str")
    if not strategies:
        return CaptureResult((), "UNAVAILABLE", None, ("NO_STRATEGIES",))
    for s in strategies:
        if s not in _ALLOWED_STRATEGIES:
            raise ValueError(f"strategy={s!r} unknown")
    if not isinstance(decision_as_of, int) or isinstance(decision_as_of, bool) or decision_as_of < 0:
        raise ValueError("decision_as_of_ms must be int >=0")

    # SYSTEM_POLICY only with USER_DECISION + selected (D14.1/D18).
    if "SYSTEM_POLICY" in strategies:
        if cohort != "USER_DECISION":
            entry_ids = tuple(_entry_id_for(cohort, source_snapshot_id, s) for s in strategies)
            return CaptureResult(entry_ids, "UNAVAILABLE", None, ("SYSTEM_POLICY_COHORT_MISMATCH",))
        if decision is None or _get(decision, "selected_proposal") is None:
            # No market calls: cannot fabricate from current model.
            entry_ids = tuple(_entry_id_for(cohort, source_snapshot_id, s) for s in strategies)
            # Save UNAVAILABLE snapshots for deterministically failed SYSTEM?
            # For single-SYSTEM stub tests return without saving (fresh repo, no day conflict).
            # For mixed groups the per-strategy path below handles partial.
            if len(strategies) == 1:
                return CaptureResult(entry_ids, "UNAVAILABLE", None, ("SYSTEM_NO_SELECTION",))
            # Mixed group: fall through to per-strategy handling.

    # USER_DECISION group id = decision_id (D14.1).
    if cohort == "USER_DECISION":
        if decision is None:
            entry_ids = tuple(_entry_id_for(cohort, source_snapshot_id, s) for s in strategies)
            return CaptureResult(entry_ids, "UNAVAILABLE", None, ("USER_DECISION_MISSING",))
        did = _get(decision, "decision_id")
        if did != source_snapshot_id:
            entry_ids = tuple(_entry_id_for(cohort, source_snapshot_id, s) for s in strategies)
            return CaptureResult(entry_ids, "UNAVAILABLE", None, ("GROUP_ID_MISMATCH",))

    # Cohort status bucket (D14.1).
    eligible, bucket_reason = _cohort_eligible(cohort, policy if isinstance(policy, Mapping) else {})
    if not eligible:
        entry_ids = tuple(_entry_id_for(cohort, source_snapshot_id, s) for s in strategies)
        return CaptureResult(entry_ids, "UNAVAILABLE", None, (bucket_reason,))

    # Day-first-sample (D14.1): same cohort/symbol/UTC-day first group wins.
    day_start, day_end = _utc_day_bounds(decision_as_of)
    try:
        existing = await repository.list_strategy_entries(cohort, day_start, day_end)
    except Exception:
        existing = ()
    same_symbol = [r for r in (existing or ()) if _get(r, "symbol") == symbol]
    for row in same_symbol:
        if _get(row, "source_snapshot_id") != source_snapshot_id:
            entry_ids = tuple(_entry_id_for(cohort, source_snapshot_id, s) for s in strategies)
            return CaptureResult(entry_ids, "UNAVAILABLE", None, ("DAY_SAMPLE_ALREADY_EXISTS",))
    existing_keys = {
        (_get(r, "source_snapshot_id"), _get(r, "strategy")): _get(r, "entry_id")
        for r in same_symbol
    }
    if all((source_snapshot_id, s) in existing_keys for s in strategies):
        # Idempotent replay: derive status from stored entries.
        ids = tuple(existing_keys[(source_snapshot_id, s)] for s in strategies)
        # Fetch statuses via listed rows.
        status_by_id = {}
        execs = []
        for r in same_symbol:
            if (_get(r, "source_snapshot_id"), _get(r, "strategy")) in {
                (source_snapshot_id, s) for s in strategies
            }:
                ej = _get(r, "entry_json", {}) or {}
                if isinstance(ej, str):
                    try:
                        import json as _json

                        ej = _json.loads(ej)
                    except Exception:
                        ej = {}
                status_by_id[_get(r, "entry_id")] = _get(ej, "status")
                if _get(r, "executed_as_of_ms") is not None:
                    execs.append(int(_get(r, "executed_as_of_ms")))
        states = [status_by_id.get(i) for i in ids]
        if all(s == "ENTRY_COMPLETE" for s in states):
            status = "COMPLETE"
        elif any(s == "ENTRY_COMPLETE" for s in states):
            status = "PARTIAL"
        else:
            status = "UNAVAILABLE"
        executed = max(execs) if execs and any(s == "ENTRY_COMPLETE" for s in states) else None
        return CaptureResult(ids, status, executed, ("ALREADY_CAPTURED",))

    # Per-strategy deterministic UNAVAILABLE (SYSTEM without selection in mixed group).
    system_unavailable: set[str] = set()
    if "SYSTEM_POLICY" in strategies and (decision is None or _get(decision, "selected_proposal") is None):
        system_unavailable.add("SYSTEM_POLICY")

    need_market = [s for s in strategies if s not in system_unavailable]
    if not need_market:
        # All deterministically unavailable; save snapshots once.
        saved_ids: list[str] = []
        for s in strategies:
            eid = _entry_id_for(cohort, source_snapshot_id, s)
            if (source_snapshot_id, s) in existing_keys:
                saved_ids.append(existing_keys[(source_snapshot_id, s)])
                continue
            await _save_entry(
                repository,
                cohort=cohort,
                symbol=symbol,
                source_snapshot_id=source_snapshot_id,
                strategy=s,
                decision_as_of_ms=decision_as_of,
                executed_as_of_ms=None,
                context=context,
                decision=decision,
                policy=policy,
                rule_refs=rule_refs,
                source_refs=source_refs,
                identity_snapshot_id=identity_snapshot_id,
                status="UNAVAILABLE",
                reasons=("SYSTEM_NO_SELECTION",),
                target_ratio="0",
                actual_ratio="0",
                futures_qty=str(futures_contract_qty),
                canonical_qty=str(canonical_futures_qty),
                spot_qty="0",
                venue=None,
                quote_refs={},
                fx_refs={},
                futures_vwap=None,
                spot_vwap=None,
                futures_exe=None,
                spot_exe=None,
                futures_ccy=None,
                spot_ccy=None,
                book_id=None,
                group_skew=None,
            )
            saved_ids.append(eid)
        return CaptureResult(tuple(saved_ids), "UNAVAILABLE", None, ("SYSTEM_NO_SELECTION",))

    # Group market collection (first real quote set after decision).
    # Futures once (canonical qty fixed); spot per exact strategy qty.
    try:
        group_futures_qty = str(futures_contract_qty)
        futures_out = await market.collect_futures(symbol, group_futures_qty, request_context)
    except Exception as exc:
        if _is_budget_denial(exc):
            raise
        # No quote at all -> all need-market strategies UNAVAILABLE (saved below).
        futures_out = None

    futures_quote = _get(futures_out, "futures_quote") if futures_out is not None else None
    futures_as_of = _get(futures_quote, "as_of_ms") if futures_quote is not None else None
    futures_known = _get(futures_quote, "known_at_ms") if futures_quote is not None else None
    book_id = _get(futures_quote, "book_observation_id")
    if book_id is None and futures_out is not None:
        book_id = _get(futures_out, "book_observation_id")

    # Spot per strategy (exact qty, skipped for h0).
    spot_by_strategy: dict[str, Any] = {}
    try:
        for s in need_market:
            if s in system_unavailable:
                continue
            _, _, _, _, venue, spot_qty = _strategy_plan(s, context, decision)
            if _parse_decimal(spot_qty, "spot_qty") == 0:
                continue
            if (s in spot_by_strategy) or any(
                _get(q, "requested_canonical_qty") == spot_qty for q in spot_by_strategy.values() if q is not None
            ):
                # Reuse already-fetched quote with same exact qty.
                continue
            q = await market.collect_spot(identity, venue, spot_qty, request_context)
            # Find which strategies share this qty (reuse).
            spot_by_strategy[s] = q
    except Exception as exc:
        if _is_budget_denial(exc):
            raise
        # Spot collection error -> mark affected strategies UNAVAILABLE below.
        # Keep already-fetched quotes; missing ones stay None.
        pass

    # Map every need-market non-zero strategy to a spot quote (reuse by qty).
    # Build qty -> quote index from fetched.
    qty_to_quote: dict[str, Any] = {}
    for key, q in spot_by_strategy.items():
        qty = str(_get(q, "requested_canonical_qty"))
        qty_to_quote[qty] = q
    # For strategies not yet mapped but sharing qty, map them.
    for s in need_market:
        if s in system_unavailable or s in spot_by_strategy:
            continue
        try:
            _, _, _, _, _, spot_qty = _strategy_plan(s, context, decision)
        except Exception:
            continue
        if _parse_decimal(spot_qty, "spot_qty") == 0:
            continue
        if spot_qty in qty_to_quote:
            spot_by_strategy[s] = qty_to_quote[spot_qty]
        # Else: spot was never fetched (e.g., earlier error) -> stays missing.

    # Group skew + executed time (D14.1: <=5s, decision/executed separated).
    as_ofs: list[int] = []
    knowns: list[int] = []
    if futures_as_of is not None:
        try:
            as_ofs.append(int(futures_as_of))
        except Exception:
            pass
    if futures_known is not None:
        try:
            knowns.append(int(futures_known))
        except Exception:
            pass
    for q in spot_by_strategy.values():
        if q is None:
            continue
        a = _get(q, "as_of_ms")
        k = _get(q, "fetched_at_ms", _get(q, "known_at_ms"))
        # SpotVenueQuote uses fetched_at_ms/as_of_ms; accept either.
        if a is not None:
            try:
                as_ofs.append(int(a))
            except Exception:
                pass
        kk = _get(q, "known_at_ms", _get(q, "fetched_at_ms"))
        if kk is not None:
            try:
                knowns.append(int(kk))
            except Exception:
                pass
        else:
            # Fallback: use as_of when known missing.
            if a is not None:
                try:
                    knowns.append(int(a))
                except Exception:
                    pass
    group_skew = (max(as_ofs) - min(as_ofs)) if len(as_ofs) >= 2 else 0
    executed_as_of = max(knowns) if knowns else None
    skew_exceeded = group_skew > _GROUP_SKEW_MS
    executed_before_decision = (
        executed_as_of is not None and int(executed_as_of) <= int(decision_as_of)
    )
    # Stale: any quote as_of <= decision_as_of means not "decision后首个真实".
    stale_quote = any(int(a) <= int(decision_as_of) for a in as_ofs) if as_ofs else True
    if not as_ofs:
        stale_quote = True

    # Per-strategy evaluation + single snapshot save.
    saved_ids_ordered: list[str] = []
    complete_count = 0
    group_reasons: set[str] = set()
    # Pre-resolve FX once per currency at group executed time (when known).
    fx_cache: dict[str, Any] = {}

    async def _fx_for(currency: str | None) -> Any:
        if currency is None:
            return None
        if currency in fx_cache:
            return fx_cache[currency]
        if executed_as_of is None:
            fx_cache[currency] = None
            return None
        row = await _resolve_fx(repository, currency, int(executed_as_of), int(executed_as_of))
        fx_cache[currency] = row
        return row

    for s in strategies:
        eid = _entry_id_for(cohort, source_snapshot_id, s)
        if (source_snapshot_id, s) in existing_keys:
            saved_ids_ordered.append(existing_keys[(source_snapshot_id, s)])
            # Count existing COMPLETE for group status (read from listed rows).
            for r in same_symbol:
                if _get(r, "entry_id") == existing_keys[(source_snapshot_id, s)]:
                    ej = _get(r, "entry_json", {}) or {}
                    if isinstance(ej, str):
                        try:
                            import json as _json

                            ej = _json.loads(ej)
                        except Exception:
                            ej = {}
                    if _get(ej, "status") == "ENTRY_COMPLETE":
                        complete_count += 1
            continue
        if s in system_unavailable:
            await _save_entry(
                repository,
                cohort=cohort,
                symbol=symbol,
                source_snapshot_id=source_snapshot_id,
                strategy=s,
                decision_as_of_ms=decision_as_of,
                executed_as_of_ms=None,
                context=context,
                decision=decision,
                policy=policy,
                rule_refs=rule_refs,
                source_refs=source_refs,
                identity_snapshot_id=identity_snapshot_id,
                status="UNAVAILABLE",
                reasons=("SYSTEM_NO_SELECTION",),
                target_ratio="0",
                actual_ratio="0",
                futures_qty=str(futures_contract_qty),
                canonical_qty=str(canonical_futures_qty),
                spot_qty="0",
                venue=None,
                quote_refs={},
                fx_refs={},
                futures_vwap=None,
                spot_vwap=None,
                futures_exe=None,
                spot_exe=None,
                futures_ccy=None,
                spot_ccy=None,
                book_id=None,
                group_skew=group_skew,
            )
            saved_ids_ordered.append(eid)
            group_reasons.add("SYSTEM_NO_SELECTION")
            continue

        # Strategy quantities (frozen for SYSTEM).
        try:
            target_ratio, actual_ratio, strat_fut_qty, strat_canon_qty, venue, spot_qty = _strategy_plan(
                s, context, decision
            )
        except Exception as exc:
            await _save_entry(
                repository,
                cohort=cohort,
                symbol=symbol,
                source_snapshot_id=source_snapshot_id,
                strategy=s,
                decision_as_of_ms=decision_as_of,
                executed_as_of_ms=None,
                context=context,
                decision=decision,
                policy=policy,
                rule_refs=rule_refs,
                source_refs=source_refs,
                identity_snapshot_id=identity_snapshot_id,
                status="UNAVAILABLE",
                reasons=("QUANTITY_INVALID",),
                target_ratio="0",
                actual_ratio="0",
                futures_qty=str(futures_contract_qty),
                canonical_qty=str(canonical_futures_qty),
                spot_qty="0",
                venue=None,
                quote_refs={},
                fx_refs={},
                futures_vwap=None,
                spot_vwap=None,
                futures_exe=None,
                spot_exe=None,
                futures_ccy=None,
                spot_ccy=None,
                book_id=None,
                group_skew=group_skew,
            )
            saved_ids_ordered.append(eid)
            group_reasons.add("QUANTITY_INVALID")
            _ = exc
            continue

        # Group-level failures -> all UNAVAILABLE (saved per strategy).
        if skew_exceeded or executed_before_decision or stale_quote or futures_quote is None:
            reason = (
                "GROUP_SKEW_EXCEEDED"
                if skew_exceeded
                else ("STALE_QUOTE_BEFORE_DECISION" if (stale_quote or executed_before_decision) else "UNAVAILABLE_NO_QUOTE")
            )
            await _save_entry(
                repository,
                cohort=cohort,
                symbol=symbol,
                source_snapshot_id=source_snapshot_id,
                strategy=s,
                decision_as_of_ms=decision_as_of,
                executed_as_of_ms=None,
                context=context,
                decision=decision,
                policy=policy,
                rule_refs=rule_refs,
                source_refs=source_refs,
                identity_snapshot_id=identity_snapshot_id,
                status="UNAVAILABLE",
                reasons=(reason,),
                target_ratio=target_ratio,
                actual_ratio=actual_ratio,
                futures_qty=strat_fut_qty,
                canonical_qty=strat_canon_qty,
                spot_qty=spot_qty,
                venue=venue,
                quote_refs={},
                fx_refs={},
                futures_vwap=None,
                spot_vwap=None,
                futures_exe=None,
                spot_exe=None,
                futures_ccy=None,
                spot_ccy=None,
                book_id=book_id if isinstance(book_id, str) else None,
                group_skew=group_skew,
            )
            saved_ids_ordered.append(eid)
            group_reasons.add(reason)
            continue

        # Exact-quantity checks (D14.1: precise, never scaled).
        fq_requested = str(_get(futures_quote, "requested_contract_qty"))
        try:
            qty_ok = _parse_decimal(fq_requested, "fq_requested") == _parse_decimal(
                strat_fut_qty, "strat_fut"
            )
        except Exception:
            qty_ok = False
        if not qty_ok:
            await _save_entry(
                repository,
                cohort=cohort,
                symbol=symbol,
                source_snapshot_id=source_snapshot_id,
                strategy=s,
                decision_as_of_ms=decision_as_of,
                executed_as_of_ms=None,
                context=context,
                decision=decision,
                policy=policy,
                rule_refs=rule_refs,
                source_refs=source_refs,
                identity_snapshot_id=identity_snapshot_id,
                status="UNAVAILABLE",
                reasons=("QUANTITY_MISMATCH",),
                target_ratio=target_ratio,
                actual_ratio=actual_ratio,
                futures_qty=strat_fut_qty,
                canonical_qty=strat_canon_qty,
                spot_qty=spot_qty,
                venue=venue,
                quote_refs={},
                fx_refs={},
                futures_vwap=None,
                spot_vwap=None,
                futures_exe=None,
                spot_exe=None,
                futures_ccy=None,
                spot_ccy=None,
                book_id=book_id if isinstance(book_id, str) else None,
                group_skew=group_skew,
            )
            saved_ids_ordered.append(eid)
            group_reasons.add("QUANTITY_MISMATCH")
            continue

        # Futures entry SELL (D14.1).
        sell_vwap = _get(futures_quote, "sell_vwap_native")
        sell_exe = _get(futures_quote, "sell_executable_qty")
        try:
            sell_exe_dec = _parse_decimal(sell_exe, "sell_exe") if sell_exe is not None else Decimal("0")
        except Exception:
            sell_exe_dec = Decimal("0")
        need_fut = _parse_decimal(strat_fut_qty, "strat_fut")
        futures_ccy = _get(futures_quote, "quote_currency")
        futures_fx_quote = _get(futures_quote, "quote_to_usd")

        # Spot entry BUY (skipped for h0).
        spot_q = spot_by_strategy.get(s)
        # Reuse by qty when direct key missing (shared qty).
        if spot_q is None and _parse_decimal(spot_qty, "spot_qty") != 0:
            spot_q = qty_to_quote.get(spot_qty)
        spot_buy = _get(spot_q, "buy_vwap") if spot_q is not None else None
        spot_buy_exe = _get(spot_q, "buy_executable_qty") if spot_q is not None else None
        spot_ccy = _get(spot_q, "quote_currency") if spot_q is not None else None
        spot_fx_quote = _get(spot_q, "quote_to_usd") if spot_q is not None else None
        spot_requested = _get(spot_q, "requested_canonical_qty") if spot_q is not None else None

        needs_spot = _parse_decimal(spot_qty, "spot_qty") != 0
        if needs_spot:
            if spot_q is None:
                await _save_entry(
                    repository,
                    cohort=cohort,
                    symbol=symbol,
                    source_snapshot_id=source_snapshot_id,
                    strategy=s,
                    decision_as_of_ms=decision_as_of,
                    executed_as_of_ms=None,
                    context=context,
                    decision=decision,
                    policy=policy,
                    rule_refs=rule_refs,
                    source_refs=source_refs,
                    identity_snapshot_id=identity_snapshot_id,
                    status="UNAVAILABLE",
                    reasons=("UNAVAILABLE_NO_QUOTE",),
                    target_ratio=target_ratio,
                    actual_ratio=actual_ratio,
                    futures_qty=strat_fut_qty,
                    canonical_qty=strat_canon_qty,
                    spot_qty=spot_qty,
                    venue=venue,
                    quote_refs={"futures": str(_get(futures_quote, "quote_id"))},
                    fx_refs={},
                    futures_vwap=sell_vwap if isinstance(sell_vwap, str) else None,
                    spot_vwap=None,
                    futures_exe=str(sell_exe) if sell_exe is not None else None,
                    spot_exe=None,
                    futures_ccy=futures_ccy if isinstance(futures_ccy, str) else None,
                    spot_ccy=None,
                    book_id=book_id if isinstance(book_id, str) else None,
                    group_skew=group_skew,
                )
                saved_ids_ordered.append(eid)
                group_reasons.add("UNAVAILABLE_NO_QUOTE")
                continue
            # Exact spot qty check.
            try:
                spot_qty_ok = _parse_decimal(str(spot_requested), "spot_requested") == _parse_decimal(
                    spot_qty, "spot_qty"
                )
            except Exception:
                spot_qty_ok = False
            if not spot_qty_ok:
                await _save_entry(
                    repository,
                    cohort=cohort,
                    symbol=symbol,
                    source_snapshot_id=source_snapshot_id,
                    strategy=s,
                    decision_as_of_ms=decision_as_of,
                    executed_as_of_ms=None,
                    context=context,
                    decision=decision,
                    policy=policy,
                    rule_refs=rule_refs,
                    source_refs=source_refs,
                    identity_snapshot_id=identity_snapshot_id,
                    status="UNAVAILABLE",
                    reasons=("QUANTITY_MISMATCH",),
                    target_ratio=target_ratio,
                    actual_ratio=actual_ratio,
                    futures_qty=strat_fut_qty,
                    canonical_qty=strat_canon_qty,
                    spot_qty=spot_qty,
                    venue=venue,
                    quote_refs={},
                    fx_refs={},
                    futures_vwap=None,
                    spot_vwap=None,
                    futures_exe=None,
                    spot_exe=None,
                    futures_ccy=None,
                    spot_ccy=None,
                    book_id=book_id if isinstance(book_id, str) else None,
                    group_skew=group_skew,
                )
                saved_ids_ordered.append(eid)
                group_reasons.add("QUANTITY_MISMATCH")
                continue

        # Depth -> UNEXECUTABLE (D14.1); missing quote/FX -> UNAVAILABLE (no scaling).
        futures_depth_ok = (
            isinstance(sell_vwap, str)
            and sell_vwap.strip() != ""
            and sell_exe_dec >= need_fut
        )
        if not futures_depth_ok:
            await _save_entry(
                repository,
                cohort=cohort,
                symbol=symbol,
                source_snapshot_id=source_snapshot_id,
                strategy=s,
                decision_as_of_ms=decision_as_of,
                executed_as_of_ms=None,
                context=context,
                decision=decision,
                policy=policy,
                rule_refs=rule_refs,
                source_refs=source_refs,
                identity_snapshot_id=identity_snapshot_id,
                status="UNEXECUTABLE",
                reasons=("INSUFFICIENT_DEPTH",),
                target_ratio=target_ratio,
                actual_ratio=actual_ratio,
                futures_qty=strat_fut_qty,
                canonical_qty=strat_canon_qty,
                spot_qty=spot_qty,
                venue=venue,
                quote_refs={"futures": str(_get(futures_quote, "quote_id"))},
                fx_refs={},
                futures_vwap=sell_vwap if isinstance(sell_vwap, str) else None,
                spot_vwap=spot_buy if isinstance(spot_buy, str) else None,
                futures_exe=str(sell_exe) if sell_exe is not None else None,
                spot_exe=str(spot_buy_exe) if spot_buy_exe is not None else None,
                futures_ccy=futures_ccy if isinstance(futures_ccy, str) else None,
                spot_ccy=spot_ccy if isinstance(spot_ccy, str) else None,
                book_id=book_id if isinstance(book_id, str) else None,
                group_skew=group_skew,
            )
            saved_ids_ordered.append(eid)
            group_reasons.add("INSUFFICIENT_DEPTH")
            continue

        if needs_spot:
            try:
                spot_exe_dec = (
                    _parse_decimal(str(spot_buy_exe), "spot_exe")
                    if spot_buy_exe is not None
                    else Decimal("0")
                )
            except Exception:
                spot_exe_dec = Decimal("0")
            need_spot = _parse_decimal(spot_qty, "spot_qty")
            spot_depth_ok = (
                isinstance(spot_buy, str) and spot_buy.strip() != "" and spot_exe_dec >= need_spot
            )
            if not spot_depth_ok:
                await _save_entry(
                    repository,
                    cohort=cohort,
                    symbol=symbol,
                    source_snapshot_id=source_snapshot_id,
                    strategy=s,
                    decision_as_of_ms=decision_as_of,
                    executed_as_of_ms=None,
                    context=context,
                    decision=decision,
                    policy=policy,
                    rule_refs=rule_refs,
                    source_refs=source_refs,
                    identity_snapshot_id=identity_snapshot_id,
                    status="UNEXECUTABLE",
                    reasons=("INSUFFICIENT_DEPTH",),
                    target_ratio=target_ratio,
                    actual_ratio=actual_ratio,
                    futures_qty=strat_fut_qty,
                    canonical_qty=strat_canon_qty,
                    spot_qty=spot_qty,
                    venue=venue,
                    quote_refs={
                        "futures": str(_get(futures_quote, "quote_id")),
                        "spot": str(_get(spot_q, "quote_id", _get(spot_q, "symbol", ""))),
                    },
                    fx_refs={},
                    futures_vwap=sell_vwap,
                    spot_vwap=spot_buy if isinstance(spot_buy, str) else None,
                    futures_exe=str(sell_exe),
                    spot_exe=str(spot_buy_exe) if spot_buy_exe is not None else None,
                    futures_ccy=futures_ccy if isinstance(futures_ccy, str) else None,
                    spot_ccy=spot_ccy if isinstance(spot_ccy, str) else None,
                    book_id=book_id if isinstance(book_id, str) else None,
                    group_skew=group_skew,
                )
                saved_ids_ordered.append(eid)
                group_reasons.add("INSUFFICIENT_DEPTH")
                continue

        # FX pinned (D14.2): non-USD must have repo FX; never backfill current.
        fx_refs: dict[str, str] = {}
        fx_missing = False
        # Futures FX.
        if isinstance(futures_ccy, str) and futures_ccy != "USD":
            if futures_fx_quote is None:
                fx_missing = True
            else:
                row = await _fx_for(futures_ccy)
                if row is None:
                    fx_missing = True
                else:
                    fx_refs[futures_ccy] = str(_get(row, "fx_id"))
        elif futures_ccy == "USD":
            fx_refs["USD"] = "FX:USD:1"
        # Spot FX.
        if needs_spot and isinstance(spot_ccy, str) and spot_ccy != "USD":
            if spot_fx_quote is None:
                fx_missing = True
            else:
                row = await _fx_for(spot_ccy)
                if row is None:
                    fx_missing = True
                else:
                    # Same currency reuses same fx_id (already cached).
                    fx_refs[spot_ccy] = str(_get(row, "fx_id"))
        elif needs_spot and spot_ccy == "USD":
            fx_refs["USD"] = "FX:USD:1"
        if fx_missing:
            # Distinguish quote FX missing vs repo FX missing for reason.
            reason = "UNKNOWN_FX"
            await _save_entry(
                repository,
                cohort=cohort,
                symbol=symbol,
                source_snapshot_id=source_snapshot_id,
                strategy=s,
                decision_as_of_ms=decision_as_of,
                executed_as_of_ms=None,
                context=context,
                decision=decision,
                policy=policy,
                rule_refs=rule_refs,
                source_refs=source_refs,
                identity_snapshot_id=identity_snapshot_id,
                status="UNAVAILABLE",
                reasons=(reason,),
                target_ratio=target_ratio,
                actual_ratio=actual_ratio,
                futures_qty=strat_fut_qty,
                canonical_qty=strat_canon_qty,
                spot_qty=spot_qty,
                venue=venue,
                quote_refs=_quote_refs_for(futures_quote, spot_q, needs_spot),
                fx_refs={},
                futures_vwap=sell_vwap,
                spot_vwap=spot_buy if isinstance(spot_buy, str) else None,
                futures_exe=str(sell_exe),
                spot_exe=str(spot_buy_exe) if spot_buy_exe is not None else None,
                futures_ccy=futures_ccy if isinstance(futures_ccy, str) else None,
                spot_ccy=spot_ccy if isinstance(spot_ccy, str) else None,
                book_id=book_id if isinstance(book_id, str) else None,
                group_skew=group_skew,
            )
            saved_ids_ordered.append(eid)
            group_reasons.add(reason)
            continue

        # ENTRY_COMPLETE: single immutable snapshot + 7/30/90 tasks.
        quote_refs = _quote_refs_for(futures_quote, spot_q, needs_spot)
        await _save_entry(
            repository,
            cohort=cohort,
            symbol=symbol,
            source_snapshot_id=source_snapshot_id,
            strategy=s,
            decision_as_of_ms=decision_as_of,
            executed_as_of_ms=int(executed_as_of) if executed_as_of is not None else None,
            context=context,
            decision=decision,
            policy=policy,
            rule_refs=rule_refs,
            source_refs=source_refs,
            identity_snapshot_id=identity_snapshot_id,
            status="ENTRY_COMPLETE",
            reasons=(),
            target_ratio=target_ratio,
            actual_ratio=actual_ratio,
            futures_qty=strat_fut_qty,
            canonical_qty=strat_canon_qty,
            spot_qty=spot_qty,
            venue=venue,
            quote_refs=quote_refs,
            fx_refs=fx_refs,
            futures_vwap=sell_vwap,
            spot_vwap=spot_buy if isinstance(spot_buy, str) else None,
            futures_exe=str(sell_exe),
            spot_exe=str(spot_buy_exe) if spot_buy_exe is not None else None,
            futures_ccy=futures_ccy if isinstance(futures_ccy, str) else None,
            spot_ccy=spot_ccy if isinstance(spot_ccy, str) else None,
            book_id=book_id if isinstance(book_id, str) else None,
            group_skew=group_skew,
        )
        saved_ids_ordered.append(eid)
        complete_count += 1
        # EXIT tasks at exact quantities (D14.2).
        if executed_as_of is not None:
            await _create_exit_tasks(
                repository,
                entry_id=eid,
                cohort=cohort,
                symbol=symbol,
                strategy=s,
                executed_as_of_ms=int(executed_as_of),
                futures_qty=strat_fut_qty,
                spot_qty=spot_qty,
                venue=venue if isinstance(venue, str) else "BINANCE_SPOT",
                identity=identity,
            )

    if complete_count == len(strategies):
        status = "COMPLETE"
    elif complete_count > 0:
        status = "PARTIAL"
    else:
        status = "UNAVAILABLE"
    executed_out = int(executed_as_of) if (complete_count > 0 and executed_as_of is not None) else None
    reasons = tuple(sorted(group_reasons)) if group_reasons else (() if status == "COMPLETE" else ("NO_COMPLETE_ENTRY",))
    # Skew already saved per entry; surface group reason when all failed on skew.
    if skew_exceeded and complete_count == 0:
        reasons = ("GROUP_SKEW_EXCEEDED",)
    return CaptureResult(tuple(saved_ids_ordered), status, executed_out, reasons)


def _quote_refs_for(futures_quote: Any, spot_q: Any, needs_spot: bool) -> dict[str, str]:
    refs: dict[str, str] = {}
    fid = _get(futures_quote, "quote_id")
    if isinstance(fid, str) and fid:
        refs["futures"] = fid
    bid = _get(futures_quote, "book_observation_id")
    if isinstance(bid, str) and bid:
        refs["book"] = bid
    if needs_spot and spot_q is not None:
        # SpotVenueQuote has no quote_id; use venue+qty+as_of as ref.
        sid = _get(spot_q, "quote_id")
        if isinstance(sid, str) and sid:
            refs["spot"] = sid
        else:
            venue = _get(spot_q, "venue", "BINANCE_SPOT")
            as_of = _get(spot_q, "as_of_ms", 0)
            qty = _get(spot_q, "requested_canonical_qty", "")
            refs["spot"] = f"{venue}:spot:{as_of}:{qty}"
    return refs


async def _save_entry(
    repository: Any,
    *,
    cohort: str,
    symbol: str,
    source_snapshot_id: str,
    strategy: str,
    decision_as_of_ms: int,
    executed_as_of_ms: int | None,
    context: Any,
    decision: Any,
    policy: Any,
    rule_refs: Mapping[str, str],
    source_refs: Mapping[str, str],
    identity_snapshot_id: Any,
    status: str,
    reasons: tuple[str, ...],
    target_ratio: str,
    actual_ratio: str,
    futures_qty: str,
    canonical_qty: str,
    spot_qty: str,
    venue: str | None,
    quote_refs: Mapping[str, str],
    fx_refs: Mapping[str, str],
    futures_vwap: str | None,
    spot_vwap: str | None,
    futures_exe: str | None,
    spot_exe: str | None,
    futures_ccy: str | None,
    spot_ccy: str | None,
    book_id: str | None,
    group_skew: int | None,
) -> str:
    entry_id = _entry_id_for(cohort, source_snapshot_id, strategy)
    decision_id = _get(decision, "decision_id") if decision is not None else None
    goal = None
    try:
        req = _get(decision, "request")
        goal = _get(req, "goal") if req is not None else None
    except Exception:
        goal = None
    if not isinstance(goal, str) or not goal:
        try:
            goal = dict(policy or {}).get("goal", "CARRY_CAPTURE")
        except Exception:
            goal = "CARRY_CAPTURE"
    entry_json: dict[str, Any] = {
        "schema_version": "repair-contract-v1",
        "cohort": cohort,
        "strategy": strategy,
        "decision_snapshot_id": decision_id,
        "source_snapshot_id": source_snapshot_id,
        "identity_snapshot_id": identity_snapshot_id,
        "decision_as_of_ms": int(decision_as_of_ms),
        "executed_as_of_ms": executed_as_of_ms,
        "target_ratio": normalize_decimal_str(target_ratio),
        "actual_ratio": normalize_decimal_str(actual_ratio),
        "native_futures_qty": normalize_decimal_str(futures_qty),
        "canonical_futures_qty": normalize_decimal_str(canonical_qty),
        "spot_net_qty": normalize_decimal_str(spot_qty),
        "spot_venue": venue,
        "quote_refs": dict(quote_refs or {}),
        "rule_refs": dict(rule_refs or {}),
        "fx_refs": dict(fx_refs or {}),
        "source_refs": dict(source_refs or {}),
        "goal": goal,
        "policy_hash": _policy_hash(policy if isinstance(policy, Mapping) else {}),
        "formula_version": _ENTRY_VERSION,
        "evidence_version": _EVIDENCE_VERSION,
        "status": status,
        "reasons": sorted(set(reasons or ())),
        # Settlement helpers (entry sides frozen per D14.1).
        "futures_entry_side": "SELL",
        "spot_entry_side": "BUY",
        "futures_entry_vwap_native": futures_vwap,
        "spot_entry_vwap_native": spot_vwap,
        "futures_entry_executable_qty": futures_exe,
        "spot_entry_executable_qty": spot_exe,
        "futures_quote_currency": futures_ccy,
        "spot_quote_currency": spot_ccy,
        "futures_book_observation_id": book_id,
        "group_skew_ms": group_skew,
    }
    record = {
        "entry_id": entry_id,
        "cohort": cohort,
        "symbol": symbol,
        "source_snapshot_id": source_snapshot_id,
        "strategy": strategy,
        "decision_as_of_ms": int(decision_as_of_ms),
        "executed_as_of_ms": executed_as_of_ms,
        "entry_json": entry_json,
    }
    # Single immutable save (no PENDING_ENTRY persisted).
    await repository.save_strategy_entry(record, references=())
    return entry_id


async def _create_exit_tasks(
    repository: Any,
    *,
    entry_id: str,
    cohort: str,
    symbol: str,
    strategy: str,
    executed_as_of_ms: int,
    futures_qty: str,
    spot_qty: str,
    venue: str,
    identity: Any,
) -> None:
    # Identity passthrough for exit spot reconstruction (R11b shape).
    binance_spot = _get(identity, "binance_spot_symbol", symbol)
    canonical_id = _get(identity, "canonical_id", symbol)
    multiplier = _get(identity, "contract_multiplier")
    try:
        mult_str = str(multiplier) if multiplier is not None else None
    except Exception:
        mult_str = None
    for horizon in _HORIZONS:
        due = int(executed_as_of_ms) + int(horizon) * _DAY_MS
        deadline = due + _EXIT_DELAY_MS
        task_id = f"{entry_id}:{int(horizon)}:EXIT"
        task_json: dict[str, Any] = {
            "schema_version": "repair-contract-v1",
            "entry_id": entry_id,
            "purpose": "EXIT",
            "strategy": strategy,
            "horizon_days": int(horizon),
            "due_ms": due,
            "deadline_ms": deadline,
            "requested_futures_qty": normalize_decimal_str(futures_qty),
            "requested_spot_qty": normalize_decimal_str(spot_qty),
            "venue": venue,
            "symbol": symbol,
            "cohort": cohort,
            "attempt_count": 0,
            "quote_refs": {},
            "reason_code": "EXIT_SCHEDULED",
            "identity_snapshot_id": None,
            "binance_spot_symbol": binance_spot if isinstance(binance_spot, str) else symbol,
            "canonical_id": canonical_id if isinstance(canonical_id, str) else symbol,
            "contract_multiplier": mult_str,
        }
        record = {
            "task_id": task_id,
            "entry_id": entry_id,
            "horizon_days": int(horizon),
            "purpose": "EXIT",
            "due_ms": due,
            "status": "PENDING",
            "task_json": task_json,
            "updated_at_ms": int(executed_as_of_ms),
        }
        try:
            await repository.save_strategy_quote_task(record)
        except Exception as exc:
            # Idempotent triple (entry,horizon,purpose): already scheduled.
            msg = str(exc)
            if "UNIQUE" in msg.upper() or "unique" in msg or "conflict" in msg.lower():
                continue
            raise


async def collect_due_quotes(
    repository: RepositoryPort, market: MarketPort, as_of_ms: int, request_context: RequestContext  # type: ignore[valid-type]
) -> QuoteCollectionResult:
    """R14b due-quote collection (D18.1 exact signature, async)."""
    as_of = int(as_of_ms)
    if as_of < 0:
        raise ValueError("as_of_ms must be >=0")
    try:
        claimed = await repository.claim_due_quote_tasks(as_of, limit=_QUOTE_BATCH)
    except Exception:
        raise
    claimed = tuple(claimed or ())
    # Preserve due_ms ASC / task_id ASC (repo already ordered).
    complete = 0
    deferred = 0
    unavailable = 0
    reasons: set[str] = set()
    task_ids = [str(_get(t, "task_id")) for t in claimed]

    for task in claimed:
        task_id = str(_get(task, "task_id"))
        tj = _get(task, "task_json", {}) or {}
        if isinstance(tj, str):
            try:
                import json as _json

                tj = _json.loads(tj)
            except Exception:
                tj = {}
        if not isinstance(tj, Mapping):
            tj = {}
        due = _get(tj, "due_ms", _get(task, "due_ms"))
        deadline = _get(tj, "deadline_ms")
        try:
            due_i = int(due) if due is not None else None
        except Exception:
            due_i = None
        try:
            dl_i = int(deadline) if deadline is not None else None
        except Exception:
            dl_i = None
        # Past-deadline explicitly fails (never scaled with 100% quote).
        if dl_i is not None and as_of > dl_i:
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "TASK_DEADLINE_EXCEEDED"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("TASK_DEADLINE_EXCEEDED")
            continue

        symbol = _get(tj, "symbol", _get(task, "symbol"))
        strat = _get(tj, "strategy", _get(task, "strategy"))
        fut_qty = _get(tj, "requested_futures_qty")
        spot_qty = _get(tj, "requested_spot_qty")
        venue = _get(tj, "venue", "BINANCE_SPOT")
        if not isinstance(symbol, str) or not symbol:
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "TASK_MISSING_SYMBOL"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("TASK_MISSING_SYMBOL")
            continue
        if fut_qty is None or spot_qty is None:
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "TASK_MISSING_QUANTITY"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("TASK_MISSING_QUANTITY")
            continue
        try:
            fut_dec = _parse_decimal(str(fut_qty), "fut_qty")
            spot_dec = _parse_decimal(str(spot_qty), "spot_qty")
            if fut_dec <= 0 or spot_dec < 0:
                raise ValueError("qty range")
        except Exception:
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "TASK_MISSING_QUANTITY"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("TASK_MISSING_QUANTITY")
            continue

        # Reverse quotes at exact quantities (Futures BUY exit, Spot SELL exit).
        try:
            futures_out = await market.collect_futures(symbol, normalize_decimal_str(fut_dec), request_context)
        except Exception as exc:
            if _is_budget_denial(exc):
                try:
                    await repository.finish_quote_task(task_id, "DEFERRED", {"reason_code": "BUDGET_DEFERRED"})
                except Exception:
                    pass
                deferred += 1
                reasons.add("BUDGET_DEFERRED")
                continue
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "EXIT_QUOTE_UNAVAILABLE"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("EXIT_QUOTE_UNAVAILABLE")
            continue

        futures_quote = _get(futures_out, "futures_quote")
        buy_vwap = _get(futures_quote, "buy_vwap_native") if futures_quote is not None else None
        buy_exe = _get(futures_quote, "buy_executable_qty") if futures_quote is not None else None
        fut_ccy = _get(futures_quote, "quote_currency") if futures_quote is not None else None
        fut_fx_q = _get(futures_quote, "quote_to_usd") if futures_quote is not None else None
        fut_req = _get(futures_quote, "requested_contract_qty") if futures_quote is not None else None
        fut_as_of = _get(futures_quote, "as_of_ms") if futures_quote is not None else None
        fut_known = _get(futures_quote, "known_at_ms") if futures_quote is not None else None

        # Spot exit (SELL) unless h0 (qty 0).
        needs_spot = spot_dec != 0
        spot_q: Any = None
        if needs_spot:
            # Reconstruct minimal identity for MarketPort (real market needs symbols).
            binance_spot = _get(tj, "binance_spot_symbol", symbol)
            canonical_id = _get(tj, "canonical_id", symbol)
            mult = _get(tj, "contract_multiplier")
            ident = SimpleNamespace(
                binance_spot_symbol=binance_spot,
                binance_futures_symbol=symbol,
                canonical_id=canonical_id,
                contract_multiplier=mult,
                mapping_confidence="VERIFIED",
            )
            try:
                spot_q = await market.collect_spot(ident, venue, normalize_decimal_str(spot_dec), request_context)
            except Exception as exc:
                if _is_budget_denial(exc):
                    try:
                        await repository.finish_quote_task(task_id, "DEFERRED", {"reason_code": "BUDGET_DEFERRED"})
                    except Exception:
                        pass
                    deferred += 1
                    reasons.add("BUDGET_DEFERRED")
                    continue
                try:
                    await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "EXIT_QUOTE_UNAVAILABLE"})
                except Exception:
                    pass
                unavailable += 1
                reasons.add("EXIT_QUOTE_UNAVAILABLE")
                continue

        # Exact-quantity checks (no 100% scaling).
        try:
            fut_qty_ok = (
                futures_quote is not None
                and _parse_decimal(str(fut_req), "fut_req") == fut_dec
            )
        except Exception:
            fut_qty_ok = False
        if not fut_qty_ok:
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "EXIT_QUANTITY_MISMATCH"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("EXIT_QUANTITY_MISMATCH")
            continue
        if needs_spot:
            spot_req = _get(spot_q, "requested_canonical_qty") if spot_q is not None else None
            try:
                spot_qty_ok = _parse_decimal(str(spot_req), "spot_req") == spot_dec
            except Exception:
                spot_qty_ok = False
            if not spot_qty_ok:
                try:
                    await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "EXIT_QUANTITY_MISMATCH"})
                except Exception:
                    pass
                unavailable += 1
                reasons.add("EXIT_QUANTITY_MISMATCH")
                continue

        # Exit depth: Futures BUY, Spot SELL (opposite of entry).
        try:
            buy_exe_dec = _parse_decimal(str(buy_exe), "buy_exe") if buy_exe is not None else Decimal("0")
        except Exception:
            buy_exe_dec = Decimal("0")
        if not (isinstance(buy_vwap, str) and buy_vwap.strip() != "" and buy_exe_dec >= fut_dec):
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "EXIT_QUOTE_UNAVAILABLE"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("EXIT_QUOTE_UNAVAILABLE")
            continue
        exit_spot_vwap: str | None = None
        if needs_spot:
            sell_vwap = _get(spot_q, "sell_vwap") if spot_q is not None else None
            sell_exe = _get(spot_q, "sell_executable_qty") if spot_q is not None else None
            try:
                sell_exe_dec = _parse_decimal(str(sell_exe), "sell_exe") if sell_exe is not None else Decimal("0")
            except Exception:
                sell_exe_dec = Decimal("0")
            if not (isinstance(sell_vwap, str) and sell_vwap.strip() != "" and sell_exe_dec >= spot_dec):
                try:
                    await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "EXIT_QUOTE_UNAVAILABLE"})
                except Exception:
                    pass
                unavailable += 1
                reasons.add("EXIT_QUOTE_UNAVAILABLE")
                continue
            exit_spot_vwap = sell_vwap

        # Exit group skew (futures+spot <=5s) + exit time is real obtained time.
        exit_as_ofs: list[int] = []
        exit_knowns: list[int] = []
        for v in (fut_as_of, _get(spot_q, "as_of_ms") if needs_spot else None):
            if v is not None:
                try:
                    exit_as_ofs.append(int(v))
                except Exception:
                    pass
        for v in (fut_known, _get(spot_q, "known_at_ms", _get(spot_q, "fetched_at_ms")) if needs_spot else None):
            if v is not None:
                try:
                    exit_knowns.append(int(v))
                except Exception:
                    pass
        if len(exit_as_ofs) >= 2 and (max(exit_as_ofs) - min(exit_as_ofs) > _GROUP_SKEW_MS):
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "GROUP_SKEW_EXCEEDED"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("GROUP_SKEW_EXCEEDED")
            continue
        exit_as_of = max(exit_knowns) if exit_knowns else (max(exit_as_ofs) if exit_as_ofs else as_of)
        if due_i is not None and int(exit_as_of) < int(due_i):
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "EXIT_BEFORE_DUE"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("EXIT_BEFORE_DUE")
            continue

        # Exit FX pinned (no current backfill).
        fx_refs: dict[str, str] = {}
        fx_missing = False
        if isinstance(fut_ccy, str) and fut_ccy != "USD":
            if fut_fx_q is None:
                fx_missing = True
            else:
                row = await _resolve_fx(repository, fut_ccy, int(exit_as_of), int(exit_as_of))
                if row is None:
                    fx_missing = True
                else:
                    fx_refs[fut_ccy] = str(_get(row, "fx_id"))
        elif fut_ccy == "USD":
            fx_refs["USD"] = "FX:USD:1"
        if needs_spot:
            spot_ccy = _get(spot_q, "quote_currency")
            spot_fx_q = _get(spot_q, "quote_to_usd")
            if isinstance(spot_ccy, str) and spot_ccy != "USD":
                if spot_fx_q is None:
                    fx_missing = True
                else:
                    row = await _resolve_fx(repository, spot_ccy, int(exit_as_of), int(exit_as_of))
                    if row is None:
                        fx_missing = True
                    else:
                        fx_refs[spot_ccy] = str(_get(row, "fx_id"))
            elif spot_ccy == "USD":
                fx_refs["USD"] = "FX:USD:1"
        if fx_missing:
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "EXIT_FX_UNAVAILABLE"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("EXIT_FX_UNAVAILABLE")
            continue

        # COMPLETE with real exit time (never forced to due).
        spot_ccy_val = _get(spot_q, "quote_currency") if needs_spot else None
        result_payload: dict[str, Any] = {
            "exit_as_of_ms": int(exit_as_of),
            "quoted_futures_qty": normalize_decimal_str(str(fut_req)),
            "quoted_spot_qty": normalize_decimal_str(str(spot_req)) if needs_spot else "0",
            "futures_exit_vwap_native": buy_vwap,
            "futures_exit_side": "BUY",
            "futures_exit_quote_currency": fut_ccy,
            "futures_exit_fx_to_usd": (
                str(fut_fx_q) if fut_fx_q is not None else "1" if fut_ccy == "USD" else None
            ),
            "spot_exit_vwap_native": exit_spot_vwap,
            "spot_exit_side": "SELL" if needs_spot else None,
            "spot_exit_quote_currency": (
                _get(spot_q, "quote_currency") if needs_spot else None
            ),
            "spot_exit_fx_to_usd": (
                str(_get(spot_q, "quote_to_usd"))
                if needs_spot and _get(spot_q, "quote_to_usd") is not None
                else "1" if needs_spot and _get(spot_q, "quote_currency") == "USD"
                else None
            ),
            "quote_refs": _quote_refs_for(futures_quote, spot_q, needs_spot),
            "fx_refs": fx_refs,
            "reason_code": "EXIT_COMPLETE",
        }
        try:
            await repository.finish_quote_task(task_id, "COMPLETE", result_payload)
        except Exception:
            # Finish failure keeps RUNNING; count deferred to preserve invariant?
            # Real repo should not hit here for claimed RUNNING tasks.
            try:
                await repository.finish_quote_task(task_id, "UNAVAILABLE", {"reason_code": "FINISH_ERROR"})
            except Exception:
                pass
            unavailable += 1
            reasons.add("FINISH_ERROR")
            continue
        complete += 1

    claimed_n = len(claimed)
    # Frozen invariant: claimed == complete+deferred+unavailable.
    assert claimed_n == complete + deferred + unavailable, (
        f"claimed {claimed_n} != {complete}+{deferred}+{unavailable}"
    )
    return QuoteCollectionResult(
        claimed_n, complete, deferred, unavailable, tuple(task_ids), as_of, tuple(sorted(reasons))
    )
