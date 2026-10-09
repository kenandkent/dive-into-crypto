"""R09 real opportunity projection (D08/D03.3, pure, no network/DB/clock reads).

Frozen port signature (docs/contracts/shortlab_repair_service_calls.json)::

    project_opportunity(snapshot: Mapping, as_of_ms: int) -> Mapping

Returns the ``risk_json.projection_v2`` snake_case mapping persisted by
``save_funding_capture_snapshot`` and read by
``list_current_funding_opportunities`` (R01). The repository (R01) owns
ROW_NUMBER latest selection, null-last ordering and stable
``symbol ASC, snapshot_id ASC`` tie-breaks; this module owns the per-row
typed projection so the repository never imports it (no R01 -> R09 edge).

Snapshot input (flexible, all optional except ``snapshot_id``/``symbol``):

- identity: ``snapshot_id``, ``symbol``, ``canonical_id``,
  ``as_of_ms`` (row time; fallback to ``as_of_ms`` arg only for LEGACY).
- FCS: ``fcs`` (0..100 float, raw, never normalised by available_max),
  ``fcs_config_hash``.
- funding (any of): flat ``funding_7d``/``funding_30d``/
  ``positive_ratio_30d`` (decimal strings), ``funding_metrics`` mapping,
  or ``funding_context`` (``FundingContext`` with ``metrics``).
- age/class: ``history_class`` and/or ``listing_age_days`` (int); missing
  class is derived from reliable age (>=90 FULL_90D, 30-89 PARTIAL_90D,
  <30 INSUFFICIENT, unknown HISTORY_CLASS_UNKNOWN; never borrows
  ``first_seen``).
- venues: ``venue_quotes`` sequence of quote mappings for ``best_venue``
  selection. Only executable quotes compete (same reference notional,
  known cost, sufficient two-sided executable depth, non-indicative).
  A quote with ``quote_kind == "INDICATIVE"`` (notably every
  ``ONCHAIN_DEX`` indicative quote) can never become ``best_venue``.
- economics: explicit ``break_even_days`` passes through when valid;
  otherwise ``break_even_days`` is derived via R06a
  ``evaluate_economics`` with ``hold_days=30`` (``reference_hold_days``).
  No fictitious term is invented: without an explicit hold the default
  is 30, and without APR/costs the field stays null (never 0).
- expiries: real ``expires_at_ms`` is the earliest Funding/Mark/Quote
  expiry found in the snapshot (``expires_at_ms``,
  ``funding_expires_at_ms``, ``mark_expires_at_ms``,
  ``quote_expires_at_ms``, ``futures_expires_at_ms``,
  ``spot_expires_at_ms``, each ``venue_quotes[].expires_at_ms``,
  ``futures_quote.expires_at_ms``, ``expires_at_candidates``). With no
  real expiry the projection expires immediately at ``snap_as_of_ms``
  (stale, never +1800s invented; the 1800s background refresh is not an
  execution guarantee).
- readiness: ``readiness_breakdown`` mapping when precomputed, else
  derived from ``funding_context`` (+ ``policy``/``execution_gate``/
  ``economic_gate``/``protection_status``). Expiry is applied
  dynamically: ``query_as_of_ms >= expires_at_ms`` (boundary inclusive,
  no grace) forces ``stale=True`` and ``readiness=NOT_READY`` with
  ``STALE``/``STALE_EXPIRED`` reasons. Research ``include_stale=true``
  rows stay ``NOT_READY`` and never masquerade as current READY.
- legacy: snapshots without any v2-capable inputs (no funding sums,
  no ``funding_context``, no ``fcs``) project to an explicit
  ``LEGACY``/``LEGACY_NO_V2`` ``NOT_READY`` shape (visible as history,
  never current READY).

All amounts stay Decimal strings; ``fcs`` stays float; times stay ints;
``reasons`` are sorted deduped machine codes.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

__all__ = ["project_opportunity", "DEFAULT_HOLD_DAYS"]

#: Opportunity reference hold (D06.2/D15 ``reference_hold_days``).
DEFAULT_HOLD_DAYS = 30

_HISTORY_CLASSES = frozenset(
    {"FULL_90D", "PARTIAL_90D", "INSUFFICIENT", "HISTORY_CLASS_UNKNOWN"}
)
_READINESS = frozenset({"READY", "NOT_READY", "BLOCKED"})
_EXECUTABLE_VENUES = frozenset({"BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"})


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _as_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _dec_str(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(value)
    if not value.is_finite():
        raise ValueError("non-finite decimal")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if text in ("", "-"):
            return "0"
        if text.startswith("."):
            text = "0" + text
        elif text.startswith("-."):
            text = "-0." + text[2:]
    if text == "-0":
        return "0"
    return text


def _opt_dec(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        text = str(value).strip() if isinstance(value, str) else str(value)
        if not text:
            return None
        parsed = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite():
        return None
    try:
        return _dec_str(parsed)
    except ValueError:
        return None


def _opt_fcs(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        f = float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if f != f or abs(f) == float("inf"):
        return None
    if not 0.0 <= f <= 100.0:
        # Out-of-range FCS is data corruption: treat as null, never clamp.
        return None
    return float(f)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _sorted_reasons(items: Sequence[str] | None) -> list[str]:
    if not items:
        return []
    return sorted({str(r) for r in items if isinstance(r, str) and str(r)})


# ---------------------------------------------------------------------------
# Input extraction (flat keys, camel aliases, nested funding_metrics/context).
# ---------------------------------------------------------------------------


def _metrics_source(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Merge funding-metric inputs from flat keys, ``funding_metrics`` and
    ``funding_context.metrics`` (flat keys win, then funding_metrics, then
    context metrics)."""
    merged: dict[str, Any] = {}
    # FundingContext metrics (lowest priority).
    ctx = snapshot.get("funding_context")
    if ctx is not None:
        metrics = _field(ctx, "metrics", None)
        if metrics is not None:
            for key in (
                "funding_7d",
                "funding_30d",
                "funding_90d",
                "positive_ratio_30d",
                "positive_ratio_90d",
                "conservative_apr",
                "current_rate",
                "last_settled_rate",
            ):
                val = _field(metrics, key, None)
                if val is not None and key not in merged:
                    merged[key] = val
    # funding_metrics mapping.
    fm = snapshot.get("funding_metrics")
    if fm is None:
        fm = snapshot.get("fundingMetrics")
    if isinstance(fm, Mapping):
        for key in (
            "funding_7d",
            "funding_30d",
            "funding_7D",
            "funding_30D",
            "funding7d",
            "funding30d",
            "positive_ratio_30d",
            "positiveRatio30d",
            "positive_ratio_30D",
            "conservative_apr",
            "conservativeApr",
        ):
            if fm.get(key) is not None:
                norm = key.lower()
                if "7d" in norm and "funding_7d" not in merged:
                    merged["funding_7d"] = fm[key]
                elif "30d" in norm and "ratio" not in norm and "funding_30d" not in merged:
                    merged["funding_30d"] = fm[key]
                elif "ratio" in norm and "positive_ratio_30d" not in merged:
                    merged["positive_ratio_30d"] = fm[key]
                elif "conservative" in norm and "conservative_apr" not in merged:
                    merged["conservative_apr"] = fm[key]
    # Flat keys (highest priority, both snake and camel).
    aliases: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("funding_7d", ("funding_7d", "funding7d", "funding_7D")),
        ("funding_30d", ("funding_30d", "funding30d", "funding_30D")),
        ("positive_ratio_30d", ("positive_ratio_30d", "positiveRatio30d", "positive_ratio_30D")),
        ("conservative_apr", ("conservative_apr", "conservativeApr")),
    )
    for canonical, keys in aliases:
        for key in keys:
            if snapshot.get(key) is not None:
                merged[canonical] = snapshot[key]
                break
    return merged


def _history_inputs(
    snapshot: Mapping[str, Any], snap_as_of: int
) -> tuple[str, int | None]:
    """Return (history_class, listing_age_days)."""
    _ = snap_as_of
    raw_class = snapshot.get("history_class", snapshot.get("historyClass"))
    listing: Any = snapshot.get("listing_age_days", snapshot.get("listingAgeDays"))
    if listing is None:
        ctx = snapshot.get("funding_context")
        if ctx is not None:
            listing = _field(ctx, "listing_age_days", None)
    if listing is None:
        # Also accept raw age shorthands (days float or onboard ms).
        for key in ("listing_age", "age_days", "ageDays"):
            if snapshot.get(key) is not None:
                listing = snapshot.get(key)
                break
    age_days: int | None = None
    if listing is not None and not isinstance(listing, bool):
        try:
            if isinstance(listing, Mapping):
                # {"age_days": n, "reliable": bool} or {"onboard_ms": t}.
                rel = listing.get("reliable", listing.get("is_reliable", True))
                if rel is False:
                    age_days = None
                elif listing.get("age_days") is not None:
                    age_days = int(float(str(listing.get("age_days"))))
                elif listing.get("listing_age_days") is not None:
                    age_days = int(float(str(listing.get("listing_age_days"))))
                else:
                    age_days = None
            elif isinstance(listing, float) and listing == listing:
                age_days = int(listing)
            else:
                age_days = int(float(str(listing).strip())) if str(listing).strip() else None
        except (TypeError, ValueError):
            age_days = None
        if age_days is not None and age_days < 0:
            age_days = None
    # Explicit reliable age <0 or huge ms timestamps (>1e12) are onboard times.
    if isinstance(listing, (int, float)) and not isinstance(listing, bool):
        try:
            raw_f = float(listing)
            if raw_f > 1e12:
                # Onboard ms: derive age days from snapshot time when possible.
                snap_i = _as_int(snapshot.get("as_of_ms", snapshot.get("asOf")), "as_of")
                if snap_i is not None:
                    derived = int((snap_i - int(raw_f)) // 86_400_000)
                    age_days = derived if derived >= 0 else None
        except (TypeError, ValueError):
            pass
    if isinstance(raw_class, str) and raw_class in _HISTORY_CLASSES:
        return raw_class, age_days
    # Derive from reliable age (D05.2): >=90 FULL, 30-89 PARTIAL, <30
    # INSUFFICIENT, unknown HISTORY_CLASS_UNKNOWN. Never borrow first_seen.
    ctx_class: Any = None
    ctx = snapshot.get("funding_context")
    if ctx is not None:
        ctx_class = _field(ctx, "history_class", None)
        if isinstance(ctx_class, str) and ctx_class in _HISTORY_CLASSES:
            return ctx_class, age_days if age_days is not None else _field(ctx, "listing_age_days", None)
    if age_days is None:
        # Unknown listing point: HISTORY_CLASS_UNKNOWN (not first_seen).
        return "HISTORY_CLASS_UNKNOWN", None
    if age_days >= 90:
        return "FULL_90D", age_days
    if age_days >= 30:
        return "PARTIAL_90D", age_days
    return "INSUFFICIENT", age_days


# ---------------------------------------------------------------------------
# Venue selection (executable-only, indicative never wins).
# ---------------------------------------------------------------------------


def _is_indicative(quote: Mapping[str, Any]) -> bool:
    kind = quote.get("quote_kind", quote.get("quoteKind"))
    if isinstance(kind, str) and kind.strip().upper() == "INDICATIVE":
        return True
    venue = str(quote.get("venue", "")).upper()
    if venue == "ONCHAIN_DEX":
        # On-chain only produces INDICATIVE quotes (B12.2); without an
        # explicit FIRM kind it stays indicative.
        if not isinstance(kind, str) or kind.strip().upper() != "FIRM":
            return True
    return False


def _venue_cost(quote: Mapping[str, Any]) -> Decimal | None:
    for key in (
        "roundtrip_cost_pct",
        "roundtripCostPct",
        "roundtrip_cost",
        "cost_pct",
        "roundtrip_cost_usd",
        "roundtripCostUsd",
        "estimated_fee_usd",
        "estimatedFeeUsd",
    ):
        if quote.get(key) is not None:
            try:
                raw = quote[key]
                if isinstance(raw, bool):
                    continue
                parsed = Decimal(str(raw).strip() if isinstance(raw, str) else str(raw))
            except (InvalidOperation, ValueError, TypeError, ArithmeticError):
                continue
            if parsed.is_finite() and parsed >= 0:
                return parsed
    return None


def _venue_expiry(quote: Mapping[str, Any]) -> int | None:
    return _as_int(quote.get("expires_at_ms", quote.get("expiresAt")), "expires")


def _is_executable_venue(
    quote: Any, reference_notional: str | None
) -> bool:
    if not isinstance(quote, Mapping):
        return False
    venue = quote.get("venue")
    if not isinstance(venue, str) or not venue.strip():
        return False
    if _is_indicative(quote):
        return False
    status = quote.get("status")
    if status is not None:
        if not isinstance(status, str) or status.strip().upper() != "OK":
            return False
    else:
        feas = quote.get("exit_feasibility", quote.get("exitFeasibility"))
        if feas is not None:
            if str(feas).strip().upper() not in ("CONFIRMED", "PARTIAL"):
                return False
        entry_ok = quote.get("entry_feasible", quote.get("entryFeasible"))
        exit_ok = quote.get("exit_feasible", quote.get("exitFeasible"))
        if entry_ok is not None or exit_ok is not None:
            if entry_ok is not True or exit_ok is not True:
                # When feasibility flags are present both legs must be true.
                if not (entry_ok is True and exit_ok is True):
                    return False
    # Same reference notional group only.
    if reference_notional is not None:
        qref = quote.get("reference_notional_usd", quote.get("referenceNotionalUsd"))
        if qref is not None:
            need = _opt_dec(reference_notional)
            have = _opt_dec(qref)
            if need is not None and have is not None and need != have:
                return False
    # Cost must be known (unknown cost never enters executable selection).
    if _venue_cost(quote) is None:
        return False
    # Two-sided executable depth must be known and sufficient.
    buy = _opt_dec(quote.get("buy_executable_qty", quote.get("buyExecutableQty")))
    sell = _opt_dec(quote.get("sell_executable_qty", quote.get("sellExecutableQty")))
    if buy is None or sell is None:
        return False
    try:
        buy_d = Decimal(str(buy))
        sell_d = Decimal(str(sell))
    except (InvalidOperation, ValueError):
        return False
    if buy_d <= 0 or sell_d <= 0:
        return False
    requested = quote.get("requested_canonical_qty", quote.get("requestedCanonicalQty"))
    if requested is not None:
        req = _opt_dec(requested)
        if req is not None:
            try:
                req_d = Decimal(str(req))
            except (InvalidOperation, ValueError):
                return False
            if req_d > 0 and (buy_d < req_d or sell_d < req_d):
                return False
    return True


def _select_best_venue(
    snapshot: Mapping[str, Any], reference_notional: str | None
) -> str | None:
    quotes = snapshot.get("venue_quotes", snapshot.get("venueQuotes"))
    if quotes is None:
        quotes = snapshot.get("venues")
    if quotes is None:
        # Single-venue summary shape (jobs.opportunity legacy): fall back to
        # an explicit best_venue only when the summary itself is executable.
        summary = snapshot.get("venue_summary", snapshot.get("venueSummary"))
        explicit = snapshot.get("best_venue", snapshot.get("bestVenue"))
        if isinstance(summary, Mapping) and isinstance(explicit, str) and explicit.strip():
            if _is_executable_venue({**dict(summary), "venue": explicit}, reference_notional):
                return explicit.strip()
            return None
        if isinstance(explicit, str) and explicit.strip():
            # Explicit best without quote detail: trust only non-indicative
            # named venues (indicative-only snapshots must yield None).
            kind = snapshot.get("quote_kind", snapshot.get("quoteKind"))
            if isinstance(kind, str) and kind.strip().upper() == "INDICATIVE":
                return None
            if explicit.strip().upper() == "ONCHAIN_DEX":
                return None
            return explicit.strip()
        return None
    if isinstance(quotes, Mapping):
        quotes = [quotes]
    if not isinstance(quotes, (list, tuple)):
        return None
    candidates: list[tuple[Decimal, int, str, Mapping[str, Any]]] = []
    for quote in quotes:
        if not isinstance(quote, Mapping):
            continue
        if not _is_executable_venue(quote, reference_notional):
            continue
        cost = _venue_cost(quote)
        assert cost is not None
        expiry = _venue_expiry(quote)
        venue = str(quote.get("venue")).strip()
        # Sort: cost asc, expiry desc, venue asc (Decimal compare, no float).
        candidates.append((cost, -(expiry or 0), venue, quote))
    if not candidates:
        return None
    # Stable: venue asc first, then cost/expiry (keeps ties deterministic).
    candidates.sort(key=lambda c: c[2])
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


# ---------------------------------------------------------------------------
# Expiry (real Funding/Mark/Quote earliest, never invented).
# ---------------------------------------------------------------------------


def _collect_expires(
    snapshot: Mapping[str, Any], snap_as_of: int
) -> int:
    candidates: list[int] = []
    for key in (
        "expires_at_ms",
        "expiresAt",
        "funding_expires_at_ms",
        "fundingExpiresAt",
        "mark_expires_at_ms",
        "markExpiresAt",
        "quote_expires_at_ms",
        "quoteExpiresAt",
        "futures_expires_at_ms",
        "spot_expires_at_ms",
    ):
        val = _as_int(snapshot.get(key), key)
        if val is not None:
            candidates.append(val)
    extra = snapshot.get("expires_at_candidates", snapshot.get("expiresAtCandidates"))
    if isinstance(extra, (list, tuple)):
        for val in extra:
            parsed = _as_int(val, "candidate")
            if parsed is not None:
                candidates.append(parsed)
    quotes = snapshot.get("venue_quotes", snapshot.get("venueQuotes"))
    if isinstance(quotes, Mapping):
        quotes = [quotes]
    if isinstance(quotes, (list, tuple)):
        for quote in quotes:
            if isinstance(quote, Mapping):
                exp = _venue_expiry(quote)
                if exp is not None:
                    candidates.append(exp)
    for key in ("futures_quote", "futuresQuote", "spot_quote", "spotQuote"):
        nested = snapshot.get(key)
        if isinstance(nested, Mapping):
            exp = _venue_expiry(nested)
            if exp is not None:
                candidates.append(exp)
            # FuturesExecutionQuote shape uses expires_at_ms as well.
            raw = nested.get("expires_at_ms")
            parsed = _as_int(raw, key)
            if parsed is not None and parsed not in candidates:
                candidates.append(parsed)
    if not candidates:
        # No real expiry known: expire immediately (stale on read, never
        # invent a hold or a +1800s refresh as execution credit).
        return int(snap_as_of)
    return min(candidates)


# ---------------------------------------------------------------------------
# Break-even (explicit pass-through else R06a hold-30 economics).
# ---------------------------------------------------------------------------


def _explicit_break_even(snapshot: Mapping[str, Any]) -> str | None:
    for key in ("break_even_days", "breakEvenDays", "breakeven_days"):
        if snapshot.get(key) is not None:
            return _opt_dec(snapshot.get(key))
    # Nested economics passthrough.
    econ = snapshot.get("economics")
    if isinstance(econ, Mapping) and econ.get("break_even_days") is not None:
        return _opt_dec(econ.get("break_even_days"))
    return None


def _derive_break_even(
    snapshot: Mapping[str, Any],
    conservative_apr: str | None,
    reference_notional: str | None,
) -> str | None:
    explicit = _explicit_break_even(snapshot)
    if explicit is not None:
        return explicit
    # Need APR + actual notional + complete costs for R06a economics.
    if conservative_apr is None:
        return None
    actual = snapshot.get("actual_futures_notional_usd", snapshot.get("actualFuturesNotionalUsd"))
    if actual is None:
        actual = reference_notional
    if actual is None:
        return None
    # Cost legs: flat keys win, then economics/costs mappings.
    def _pick(*keys: str) -> Any:
        for key in keys:
            if snapshot.get(key) is not None:
                return snapshot.get(key)
        for nested_key in ("costs", "economics_inputs", "economicsInputs", "proposal"):
            nested = snapshot.get(nested_key)
            if isinstance(nested, Mapping):
                for key in keys:
                    if nested.get(key) is not None:
                        return nested[key]
        return None

    entry = _pick("entry_fee_usd", "entryFeeUsd")
    exit_fee = _pick("exit_fee_usd", "exitFeeUsd")
    slippage = _pick("slippage_usd", "slippageUsd")
    gas = _pick("gas_usd", "gasUsd")
    fees_included = snapshot.get("fees_included", snapshot.get("feesIncluded"))
    if fees_included is None:
        for nested_key in ("costs", "economics_inputs", "economicsInputs", "proposal"):
            nested = snapshot.get(nested_key)
            if isinstance(nested, Mapping) and nested.get("fees_included") is not None:
                fees_included = nested.get("fees_included")
                break
    if not isinstance(fees_included, bool):
        return None
    if entry is None or exit_fee is None or slippage is None or gas is None:
        return None
    # Hold: explicit valid hold wins, else default 30 (never invent 7/90).
    hold: int = DEFAULT_HOLD_DAYS
    for key in ("hold_days", "holdDays", "planned_hold_days", "reference_hold_days"):
        raw = snapshot.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and 1 <= raw <= 365:
            hold = int(raw)
            break
    # FundingContext for APR: prefer the real DTO, else synthesise a minimal
    # one from the snapshot metrics for the economics call.
    funding_context = snapshot.get("funding_context", snapshot.get("fundingContext"))
    cost_policy: Any = snapshot.get("cost_policy", snapshot.get("costPolicy", {"min_net_carry_usd": "0"}))
    if funding_context is None:
        return None
    # Lazy R06a import (R06a is merged in the R09 baseline; a missing module
    # must surface as null BE, never as a fabricated 0).
    try:
        from diveintocrypto_desktop.shortlab.hedge.economics import (  # noqa: WPS433
            evaluate_economics,
        )
    except Exception:
        return None
    proposal = {
        "actual_futures_notional_usd": str(actual),
        "spot_cash_usd": str(_pick("spot_cash_usd", "spotCashUsd") or "0"),
        "margin_usd": str(_pick("margin_usd", "marginUsd") or str(actual)),
        "entry_fee_usd": str(entry),
        "exit_fee_usd": str(exit_fee),
        "slippage_usd": str(slippage),
        "gas_usd": str(gas),
        "fees_included": bool(fees_included),
        "reserve_fraction": str(_pick("reserve_fraction", "reserveFraction") or "0.05"),
    }
    try:
        result = evaluate_economics(proposal, funding_context, hold, cost_policy)
    except Exception:
        return None
    return _opt_dec(getattr(result, "break_even_days", None))


# ---------------------------------------------------------------------------
# Readiness (precomputed breakdown else FundingContext gates, stale-forced).
# ---------------------------------------------------------------------------


def _gate_to_dict(gate: Any, fallback_ms: int) -> dict[str, Any]:
    if gate is None:
        return {
            "status": "UNKNOWN",
            "reasons": ["GATE_NOT_EVALUATED"],
            "checked_at_ms": int(fallback_ms),
            "input_refs": {},
        }
    if isinstance(gate, Mapping):
        status = gate.get("status", "UNKNOWN")
        if status not in ("PASS", "FAIL", "UNKNOWN"):
            status = "UNKNOWN"
        reasons = _sorted_reasons(gate.get("reasons", ()))
        try:
            checked = int(gate.get("checked_at_ms", fallback_ms))
        except (TypeError, ValueError):
            checked = int(fallback_ms)
        refs = gate.get("input_refs", gate.get("inputRefs", {}))
        refs_dict = dict(refs) if isinstance(refs, Mapping) else {}
        return {
            "status": status,
            "reasons": reasons,
            "checked_at_ms": checked,
            "input_refs": {str(k): str(v) for k, v in refs_dict.items()},
        }
    # GateResult DTO.
    try:
        return {
            "status": str(getattr(gate, "status", "UNKNOWN")),
            "reasons": _sorted_reasons(tuple(getattr(gate, "reasons", ())) or ()),
            "checked_at_ms": int(getattr(gate, "checked_at_ms", fallback_ms)),
            "input_refs": dict(getattr(gate, "input_refs", {}) or {}),
        }
    except (TypeError, ValueError):
        return {
            "status": "UNKNOWN",
            "reasons": ["GATE_NOT_EVALUATED"],
            "checked_at_ms": int(fallback_ms),
            "input_refs": {},
        }


def _derive_breakdown(
    snapshot: Mapping[str, Any], snap_as_of: int
) -> tuple[dict[str, Any], list[str]]:
    """Return (readiness_breakdown_dict, gate_reasons)."""
    prebuilt = snapshot.get("readiness_breakdown", snapshot.get("readinessBreakdown"))
    if isinstance(prebuilt, Mapping) and prebuilt.get("readiness") in ("READY", "NOT_READY", "BLOCKED"):
        funding_gate = _gate_to_dict(prebuilt.get("funding_gate", prebuilt.get("fundingGate")), snap_as_of)
        execution_gate = _gate_to_dict(
            prebuilt.get("execution_gate", prebuilt.get("executionGate")), snap_as_of
        )
        economic_gate = _gate_to_dict(
            prebuilt.get("economic_gate", prebuilt.get("economicGate")), snap_as_of
        )
        protection = prebuilt.get("protection_status", prebuilt.get("protectionStatus", "UNKNOWN"))
        if protection not in ("CONFIRMED", "PENDING", "UNSUPPORTED", "UNKNOWN", "EXPIRED", "MANUAL_EXIT_ONLY"):
            protection = "UNKNOWN"
        data_complete = prebuilt.get("data_complete", prebuilt.get("dataComplete"))
        if not isinstance(data_complete, bool):
            data_complete = (
                funding_gate["status"] != "UNKNOWN"
                and execution_gate["status"] != "UNKNOWN"
                and economic_gate["status"] != "UNKNOWN"
            )
        readiness = str(prebuilt.get("readiness"))
        gate_reasons: list[str] = []
        for gate in (funding_gate, execution_gate, economic_gate):
            gate_reasons.extend(gate["reasons"])
        breakdown = {
            "data_complete": bool(data_complete),
            "funding_gate": funding_gate,
            "execution_gate": execution_gate,
            "economic_gate": economic_gate,
            "protection_status": protection,
            "readiness": readiness,
        }
        return breakdown, gate_reasons
    # Derive from FundingContext when available.
    funding_context = snapshot.get("funding_context", snapshot.get("fundingContext"))
    policy = snapshot.get("policy", snapshot.get("config"))
    if funding_context is not None and hasattr(funding_context, "metrics"):
        try:
            from diveintocrypto_desktop.shortlab.hedge.entry_gate import (  # noqa: WPS433
                evaluate_funding_entry_gate,
            )
        except Exception:
            evaluate_funding_entry_gate = None  # type: ignore[assignment]
        funding_gate_dict: dict[str, Any]
        if evaluate_funding_entry_gate is not None and policy is not None:
            try:
                gate = evaluate_funding_entry_gate(funding_context, policy, snap_as_of)
                funding_gate_dict = _gate_to_dict(gate, snap_as_of)
            except Exception:
                funding_gate_dict = _gate_to_dict(None, snap_as_of)
        elif evaluate_funding_entry_gate is not None:
            # Without a policy the gate cannot PASS; record UNKNOWN rather
            # than guessing defaults.
            funding_gate_dict = {
                "status": "UNKNOWN",
                "reasons": ["POLICY_UNKNOWN"],
                "checked_at_ms": int(snap_as_of),
                "input_refs": {},
            }
        else:
            funding_gate_dict = _gate_to_dict(None, snap_as_of)
        execution_gate_dict = _gate_to_dict(
            snapshot.get("execution_gate", snapshot.get("executionGate")), snap_as_of
        )
        economic_gate_dict = _gate_to_dict(
            snapshot.get("economic_gate", snapshot.get("economicGate")), snap_as_of
        )
        # Economic gate from R06a when costs are present and no explicit gate.
        if (
            snapshot.get("economic_gate") is None
            and snapshot.get("economicGate") is None
            and funding_context is not None
        ):
            econ_inputs_present = any(
                snapshot.get(k) is not None
                for k in (
                    "entry_fee_usd",
                    "exit_fee_usd",
                    "actual_futures_notional_usd",
                    "costs",
                    "economics_inputs",
                )
            )
            if econ_inputs_present:
                try:
                    from diveintocrypto_desktop.shortlab.hedge.economics import (  # noqa: WPS433
                        evaluate_economics,
                    )

                    hold = DEFAULT_HOLD_DAYS
                    for key in ("hold_days", "planned_hold_days", "reference_hold_days"):
                        raw = snapshot.get(key)
                        if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 365:
                            hold = int(raw)
                            break
                    actual = snapshot.get("actual_futures_notional_usd") or snapshot.get(
                        "reference_notional_usd"
                    )
                    if actual is not None and getattr(funding_context, "conservative_apr", None) is not None:
                        proposal = {
                            "actual_futures_notional_usd": str(actual),
                            "spot_cash_usd": "0",
                            "margin_usd": str(actual),
                            "entry_fee_usd": str(snapshot.get("entry_fee_usd", "0")),
                            "exit_fee_usd": str(snapshot.get("exit_fee_usd", "0")),
                            "slippage_usd": "0",
                            "gas_usd": "0",
                            "fees_included": True,
                            "reserve_fraction": "0.05",
                        }
                        econ = evaluate_economics(
                            proposal,
                            funding_context,
                            hold,
                            policy if isinstance(policy, Mapping) else {"min_net_carry_usd": "0"},
                        )
                        economic_gate_dict = _gate_to_dict(getattr(econ, "gate", None), snap_as_of)
                except Exception:
                    pass
        protection_status = snapshot.get("protection_status", snapshot.get("protectionStatus", "UNKNOWN"))
        if protection_status not in (
            "CONFIRMED",
            "PENDING",
            "UNSUPPORTED",
            "UNKNOWN",
            "EXPIRED",
            "MANUAL_EXIT_ONLY",
        ):
            protection_status = "UNKNOWN"
        data_complete = (
            funding_gate_dict["status"] != "UNKNOWN"
            and execution_gate_dict["status"] != "UNKNOWN"
            and economic_gate_dict["status"] != "UNKNOWN"
        )
        readiness = (
            "READY"
            if (
                funding_gate_dict["status"] == "PASS"
                and execution_gate_dict["status"] == "PASS"
                and economic_gate_dict["status"] == "PASS"
            )
            else "NOT_READY"
        )
        breakdown2 = {
            "data_complete": bool(data_complete),
            "funding_gate": funding_gate_dict,
            "execution_gate": execution_gate_dict,
            "economic_gate": economic_gate_dict,
            "protection_status": protection_status,
            "readiness": readiness,
        }
        reasons2: list[str] = []
        for gate in (funding_gate_dict, execution_gate_dict, economic_gate_dict):
            reasons2.extend(gate["reasons"])
        return breakdown2, reasons2
    # No context and no prebuilt breakdown: UNKNOWN (caller decides LEGACY).
    unknown = {
        "data_complete": False,
        "funding_gate": _gate_to_dict(None, snap_as_of),
        "execution_gate": _gate_to_dict(None, snap_as_of),
        "economic_gate": _gate_to_dict(None, snap_as_of),
        "protection_status": "UNKNOWN",
        "readiness": "NOT_READY",
    }
    return unknown, ["GATE_NOT_EVALUATED"]


# ---------------------------------------------------------------------------
# Main entry.
# ---------------------------------------------------------------------------


def project_opportunity(snapshot: Mapping[str, Any], as_of_ms: int) -> Mapping[str, Any]:
    """Build the typed ``projection_v2`` mapping for one capture snapshot.

    Pure: no network, no DB, no clock reads. ``as_of_ms`` is the query
    time used only for the dynamic stale check (``>= expires`` forces
    ``NOT_READY``); the frozen row time stays ``snapshot.as_of_ms``.
    """
    if not isinstance(snapshot, Mapping):
        raise TypeError(f"snapshot must be a mapping, got {type(snapshot).__name__}")
    try:
        query_ms = int(as_of_ms)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"as_of_ms must be an int: {exc}") from exc
    if query_ms < 0:
        raise ValueError("as_of_ms must be >= 0")

    # Already-projected read path: risk_json.projection_v2 or top-level copy.
    base_proj: Mapping[str, Any] | None = None
    risk = snapshot.get("risk_json", snapshot.get("riskJson"))
    if isinstance(risk, Mapping) and isinstance(risk.get("projection_v2"), Mapping):
        base_proj = risk["projection_v2"]
    elif isinstance(snapshot.get("projection_v2"), Mapping):
        base_proj = snapshot["projection_v2"]  # type: ignore[assignment]
    if base_proj is not None:
        # Re-validate + apply the dynamic stale check at the new query time.
        merged = dict(snapshot)
        for key, value in dict(base_proj).items():
            merged.setdefault(key, value)
        # Preserve the frozen projection times; query time stays separate.
        return _finish_from_parts(merged, query_ms, _from_existing=True)

    return _finish_from_parts(dict(snapshot), query_ms, _from_existing=False)


def _finish_from_parts(
    snapshot: dict[str, Any], query_ms: int, *, _from_existing: bool
) -> dict[str, Any]:
    snapshot_id = snapshot.get("snapshot_id", snapshot.get("snapshotId"))
    symbol = snapshot.get("symbol")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise ValueError("snapshot.snapshot_id must be a non-empty str")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("snapshot.symbol must be a non-empty str")
    canonical = snapshot.get("canonical_id", snapshot.get("canonicalId"))
    if not isinstance(canonical, str) or not canonical:
        canonical = symbol.lower()
    snap_as_of = _as_int(snapshot.get("as_of_ms", snapshot.get("asOf")), "as_of_ms")
    if snap_as_of is None:
        snap_as_of = int(query_ms)

    metrics = _metrics_source(snapshot)
    fcs = _opt_fcs(snapshot.get("fcs"))
    fcs_hash = snapshot.get("fcs_config_hash", snapshot.get("fcsConfigHash"))
    if not isinstance(fcs_hash, str) or not fcs_hash.strip():
        fcs_hash = None
    else:
        fcs_hash = fcs_hash.strip()
    funding_7d = _opt_dec(metrics.get("funding_7d"))
    funding_30d = _opt_dec(metrics.get("funding_30d"))
    positive_30d = _opt_dec(metrics.get("positive_ratio_30d"))
    conservative_apr = _opt_dec(metrics.get("conservative_apr"))
    if conservative_apr is None:
        # Explicit top-level fallback (flat snapshot without metrics map).
        conservative_apr = _opt_dec(snapshot.get("conservative_apr", snapshot.get("conservativeApr")))

    history_class, listing_age = _history_inputs(snapshot, snap_as_of)

    reference_notional = snapshot.get("reference_notional_usd", snapshot.get("referenceNotionalUsd"))
    if reference_notional is None:
        reference_notional = "10000"
    ref_str = _opt_dec(reference_notional) or "10000"

    best_venue = _select_best_venue(snapshot, ref_str)
    break_even = _derive_break_even(snapshot, conservative_apr, ref_str)
    expires_at = _collect_expires(snapshot, snap_as_of)

    # LEGACY: no v2-capable inputs at all (old JSON without projection data).
    has_v2_inputs = (
        fcs is not None
        or funding_7d is not None
        or funding_30d is not None
        or positive_30d is not None
        or snapshot.get("funding_context") is not None
        or snapshot.get("fundingContext") is not None
        or snapshot.get("funding_metrics") is not None
        or snapshot.get("fundingMetrics") is not None
        or _from_existing
    )
    is_legacy = not has_v2_inputs

    breakdown, gate_reasons = _derive_breakdown(snapshot, snap_as_of)

    # Dynamic expiry (boundary inclusive, no grace): stale forces NOT_READY.
    stale = bool(query_ms >= int(expires_at))
    readiness = str(breakdown.get("readiness", "NOT_READY"))
    if readiness not in ("READY", "NOT_READY", "BLOCKED"):
        readiness = "NOT_READY"
    # BLOCKED is reserved for delisted/identity states; an expired row is
    # NOT_READY (never keep a stale READY, never invent BLOCKED here).
    if stale and readiness == "BLOCKED":
        readiness = "NOT_READY"

    reasons: list[str] = []
    explicit = snapshot.get("reasons", snapshot.get("reason"))
    if isinstance(explicit, (list, tuple)):
        reasons.extend([str(r) for r in explicit if isinstance(r, str)])
    elif isinstance(explicit, str) and explicit:
        reasons.append(explicit)
    reasons.extend(gate_reasons)
    # Breakdown-level extra reasons (e.g. FUNDING_90D_NA) are already in the
    # gate reasons above; keep the top-level list as the union.
    if stale:
        readiness = "NOT_READY"
        reasons.extend(["STALE", "STALE_EXPIRED"])
    if is_legacy:
        readiness = "NOT_READY"
        reasons.extend(["LEGACY", "LEGACY_NO_V2"])
        # Legacy rows carry no executable credit: null the derived fields.
        if not _from_existing:
            best_venue = None
            # Keep through funding sums when present (history view), but a
            # fully empty legacy row stays null.
            pass
    # A stale executable breakdown can never stay READY for the current view
    # (research include_stale=true rows stay NOT_READY by construction here;
    # the repository re-applies the same rule on read for defence in depth).
    if stale:
        breakdown = dict(breakdown)
        breakdown["readiness"] = "NOT_READY"

    # No executable venue can never stay READY: an indicative-only (or
    # otherwise non-executable) book must not present as executable.
    if best_venue is None and readiness == "READY" and not is_legacy and not stale:
        readiness = "NOT_READY"
        reasons.extend(["VENUE_UNAVAILABLE"])

    # BLOCKED passthrough only from an explicit prebuilt breakdown that is
    # not stale and not legacy.
    if not stale and not is_legacy and breakdown.get("readiness") == "BLOCKED":
        readiness = "BLOCKED"
    elif readiness == "BLOCKED" and (stale or is_legacy):
        readiness = "NOT_READY"

    # Ensure the breakdown readiness matches the top-level shorthand.
    breakdown = dict(breakdown)
    breakdown["readiness"] = readiness

    projection: dict[str, Any] = {
        "snapshot_id": str(snapshot_id),
        "symbol": str(symbol),
        "canonical_id": str(canonical),
        "as_of_ms": int(snap_as_of),
        "expires_at_ms": int(expires_at),
        "stale": bool(stale),
        "fcs": fcs,
        "fcs_config_hash": fcs_hash,
        "funding_7d": funding_7d,
        "funding_30d": funding_30d,
        "positive_ratio_30d": positive_30d,
        "history_class": history_class,
        "listing_age_days": listing_age,
        "best_venue": best_venue,
        "break_even_days": break_even,
        "conservative_apr": conservative_apr,
        "readiness_breakdown": breakdown,
        "readiness": readiness,
        "reasons": _sorted_reasons(reasons),
    }
    return projection
