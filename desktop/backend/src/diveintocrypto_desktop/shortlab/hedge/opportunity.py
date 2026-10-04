"""H03 opportunity ranking + Hedge data quality (pure, no network).

Design: B8.4 (sorting, no normalisation) / B38+B38.1 (HedgeDQ).
Plan H03.5.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from diveintocrypto_desktop.shortlab.hedge.models import DQResult, FCSResult

__all__ = ["compute_hedge_quality", "rank_opportunities"]


# ---------------------------------------------------------------------------
# HedgeDQ (B38/B38.1)
# ---------------------------------------------------------------------------

_CANONICAL_GROUPS = ("funding", "futures", "spot_venue", "identity_units", "contract_time")

# Canonical field order per group (B38.1). Shares come from policy at runtime.
_CANONICAL_FIELDS: dict[str, tuple[str, ...]] = {
    "funding": (
        "current_rate_time",
        "next_time_interval",
        "history_7d",
        "history_30d",
        "history_90d",
        "std_rolling",
    ),
    "futures": ("mark_time", "exit_depth_vwap", "trading_rules", "symbol_status"),
    "spot_venue": ("buy_quote", "sell_quote", "rules_decimals", "cost_fee_gas"),
    "identity_units": ("asset_identity", "multiplier", "quote_usd_fx"),
    "contract_time": ("lifecycle", "source_time", "time_alignment", "heartbeat"),
}

_FUNDING_HISTORY_FIELDS = {"history_7d", "history_30d", "history_90d"}


def _extract_quality(policy: Any) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """Return (group_weights, field_shares[group][field]). Reads policy逐项."""
    node: Any = None
    if hasattr(policy, "hedge"):
        hedge = policy.hedge  # ShortLabConfig
        node = hedge.quality if isinstance(hedge, Mapping) is False else hedge.get("quality")
        if not isinstance(node, Mapping) and hasattr(hedge, "quality"):
            node = getattr(hedge, "quality")
        # HedgeConfig dataclass case
        if hasattr(hedge, "quality"):
            node = getattr(hedge, "quality")
    elif hasattr(policy, "quality"):
        node = getattr(policy, "quality")
    elif isinstance(policy, Mapping):
        if "quality" in policy and isinstance(policy["quality"], Mapping):
            node = policy["quality"]
        elif "hedge" in policy and isinstance(policy["hedge"], Mapping) and "quality" in policy["hedge"]:
            node = policy["hedge"]["quality"]
        elif "group_weights" in policy:
            node = policy
        elif "funding" in policy and "group_weights" not in policy:
            # Already field_shares-like? wrap defensively.
            node = {"group_weights": {}, "field_shares": policy}
    if not isinstance(node, Mapping):
        raise ValueError("policy must carry hedge quality group_weights/field_shares")
    gw_raw = node.get("group_weights", {})
    fs_raw = node.get("field_shares", {})
    group_weights = {str(k): float(v) for k, v in dict(gw_raw).items()}
    field_shares: dict[str, dict[str, float]] = {}
    for g, fields in dict(fs_raw).items():
        field_shares[str(g)] = {str(k): float(v) for k, v in dict(fields).items()}
    return group_weights, field_shares


def _flatten_field_states(field_states: Any) -> dict[str, Any]:
    """Accept nested {group:{field:state}} or flat {"group.field":state}."""
    if not isinstance(field_states, Mapping):
        raise ValueError("field_states must be a mapping")
    flat: dict[str, Any] = {}
    for key, value in field_states.items():
        if isinstance(value, Mapping) and "." not in str(key) and str(key) in _CANONICAL_GROUPS:
            for sub, subval in value.items():
                flat[f"{key}.{sub}"] = subval
        else:
            flat[str(key)] = value
    # Also accept bare field names by matching canonical field sets when unique.
    bare_to_qualified: dict[str, str] = {}
    for g, fields in _CANONICAL_FIELDS.items():
        for f in fields:
            bare_to_qualified.setdefault(f, f"{g}.{f}")
    for key in list(flat.keys()):
        if "." not in key and key in bare_to_qualified:
            flat[bare_to_qualified[key]] = flat.pop(key)
    return flat


def _field_credit(
    group: str,
    field: str,
    state: Any,
    *,
    now_ms: int | None = None,
) -> tuple[float, bool, str | None]:
    """Return (credit 0..1, applicable, reason).

    Funding history fields honour coverage_fraction but require
    complete=true. Only funding.history_90d may be N/A (reliable young
    age); identity/exit quotes are never N/A. Non-funding fields are
    binary 1/0 (verified+valid+fresh), never split by count.
    """
    qualified = f"{group}.{field}"
    is_history = group == "funding" and field in _FUNDING_HISTORY_FIELDS

    if state is None:
        return 0.0, True, f"MISSING_{qualified}"
    if isinstance(state, bool):
        return (1.0 if state else 0.0), True, (None if state else f"FALSE_{qualified}")
    if isinstance(state, (int, float)) and not isinstance(state, bool):
        if is_history:
            try:
                frac = max(0.0, min(1.0, float(state)))
            except (TypeError, ValueError):
                return 0.0, True, f"INVALID_{qualified}"
            return frac, True, None
        return (1.0 if float(state) != 0 else 0.0), True, None
    if isinstance(state, str):
        upper = state.strip().upper()
        if upper in ("N/A", "NA", "NOT_APPLICABLE", "INSUFFICIENT_ASSET_AGE"):
            if qualified == "funding.history_90d":
                return 0.0, False, "INSUFFICIENT_ASSET_AGE"
            return 0.0, True, f"INVALID_NA_{qualified}"
        if upper in ("TRUE", "OK", "VERIFIED", "VALID", "FRESH", "1"):
            return 1.0, True, None
        if upper in ("FALSE", "MISSING", "STALE", "EXPIRED", "0", ""):
            return 0.0, True, f"FALSE_{qualified}"
        return 0.0, True, f"INVALID_{qualified}"
    if isinstance(state, Mapping):
        d = dict(state)
        # Explicit N/A (only 90D history honours it).
        applicable = d.get("applicable", d.get("is_applicable", True))
        if applicable is False or str(d.get("status", "")).upper() in ("N/A", "NOT_APPLICABLE"):
            reason = str(d.get("reason") or d.get("missing_reason") or "NOT_APPLICABLE")
            if qualified == "funding.history_90d" and reason in (
                "INSUFFICIENT_ASSET_AGE", "NOT_APPLICABLE", "N/A", "PARTIAL_90D",
            ):
                return 0.0, False, "INSUFFICIENT_ASSET_AGE"
            return 0.0, True, f"INAPPLICABLE_{qualified}"
        # Funding history path.
        if is_history:
            complete = d.get("complete", d.get("is_complete", d.get("ok", True)))
            # Explicit False => 0 even when fraction is high.
            if complete is False:
                return 0.0, True, f"INCOMPLETE_{qualified}"
            for fk in ("coverage_fraction", "coverageFraction", "fraction", "coverage"):
                if d.get(fk) is not None:
                    try:
                        frac = max(0.0, min(1.0, float(d[fk])))
                    except (TypeError, ValueError):
                        return 0.0, True, f"INVALID_{qualified}"
                    # complete must be truthy (None treated as missing => 0).
                    if complete is True or (complete is None and "complete" not in d and "is_complete" not in d):
                        # Bare fraction without complete flag: treat as complete for
                        # convenience only when caller passed a plain fraction.
                        return frac, True, None
                    if complete:
                        return frac, True, None
                    return 0.0, True, f"INCOMPLETE_{qualified}"
            # Boolean history state.
            for bk in ("verified", "valid", "fresh", "ok", "value", "credit"):
                if bk in d:
                    v = d[bk]
                    if isinstance(v, bool):
                        return (1.0 if v else 0.0), True, (None if v else f"FALSE_{qualified}")
            # Fallback: all of verified/valid/fresh must be true when present.
            flags = [d.get(k) for k in ("verified", "valid", "fresh") if k in d]
            if flags:
                ok = all(bool(x) is True for x in flags)
                return (1.0 if ok else 0.0), True, (None if ok else f"NOT_FRESH_{qualified}")
            return 0.0, True, f"MISSING_{qualified}"
        # Non-funding binary path.
        # TTL freshness when timestamps present.
        if now_ms is not None and d.get("as_of_ms") is not None and d.get("ttl_sec") is not None:
            try:
                age_sec = (int(now_ms) - int(d["as_of_ms"])) / 1000.0
                if age_sec > float(d["ttl_sec"]):
                    return 0.0, True, f"STALE_{qualified}"
            except (TypeError, ValueError):
                return 0.0, True, f"INVALID_{qualified}"
        if d.get("expires_at_ms") is not None and now_ms is not None:
            try:
                if int(now_ms) > int(d["expires_at_ms"]):
                    return 0.0, True, f"EXPIRED_{qualified}"
            except (TypeError, ValueError):
                pass
        for bk in ("verified", "valid", "fresh", "ok"):
            if bk in d and d[bk] is False:
                return 0.0, True, f"NOT_{bk.upper()}_{qualified}"
        # Any explicit False among core flags already handled; require all present True?
        core = [d.get(k) for k in ("verified", "valid", "fresh") if k in d]
        if core and not all(bool(x) is True for x in core):
            return 0.0, True, f"NOT_VERIFIED_{qualified}"
        if "credit" in d and d["credit"] is not None:
            try:
                c = float(d["credit"])
                if 0.0 <= c <= 1.0:
                    # Non-funding fields must stay binary; clamp non-binary to 0/1?
                    # Keep binary: >=1 =>1 else 0 to avoid count-splitting.
                    return (1.0 if c >= 1.0 else (c if is_history else (1.0 if c > 0 else 0.0))), True, None
            except (TypeError, ValueError):
                pass
        if "value" in d and d["value"] is None:
            return 0.0, True, f"MISSING_{qualified}"
        # Default: present mapping without negative flags counts as 1.
        # But explicit valid=False already returned 0 above.
        if any(k in d for k in ("verified", "valid", "fresh", "ok", "value")):
            return 1.0, True, None
        return 0.0, True, f"MISSING_{qualified}"
    return 0.0, True, f"INVALID_{qualified}"


def compute_hedge_quality(
    field_states: Mapping[str, Any],
    policy: Any,
    now_ms: int,
    *,
    history_class: str | None = None,
) -> DQResult:
    """HedgeDQ 0..100 (B38.1). Pure. Reads weights逐项from policy.

    ``HedgeDQ=sum(group_weight*applicable_fraction)``. Funding history
    contributes coverage_fraction but needs complete=true. Only
    funding.history_90d may be N/A (reliable young age); identity and
    exit quotes are never N/A.
    """
    _ = history_class  # N/A is encoded in field_states (see _field_credit).
    group_weights, field_shares = _extract_quality(policy)
    flat = _flatten_field_states(field_states)

    group_scores: dict[str, Any] = {}
    field_credits: dict[str, Any] = {}
    reasons: list[str] = []
    total = 0.0
    for group in _CANONICAL_GROUPS:
        weight = float(group_weights.get(group, 0.0))
        shares = field_shares.get(group, {})
        num = 0.0
        den = 0.0
        for field in _CANONICAL_FIELDS[group]:
            share = float(shares.get(field, 0.0))
            state = flat.get(f"{group}.{field}", None)
            # Missing key => credit 0, applicable (not N/A), unless 90D N/A state.
            credit, applicable, reason = _field_credit(group, field, state, now_ms=int(now_ms))
            if applicable:
                num += share * credit
                den += share
            # N/A excluded from denominator (only funding.history_90d).
            field_credits[f"{group}.{field}"] = credit
            if reason and credit == 0.0:
                # Keep concise reasons; N/A is informational.
                if applicable:
                    reasons.append(reason)
                else:
                    reasons.append(f"N/A_{group}.{field}:{reason}")
        fraction = (num / den) if den > 0 else 0.0
        group_scores[group] = round(fraction * 100.0, 6)
        total += weight * fraction

    score = max(0.0, min(100.0, float(total)))
    readiness = "READY" if score >= 80 else "NOT_READY"
    if readiness == "NOT_READY" and not reasons:
        reasons.append("HEDGEDQ_BELOW_80")
    return DQResult(
        score=float(score),
        group_scores=dict(group_scores),
        field_credits=dict(field_credits),
        reasons=tuple(reasons),
        readiness=readiness,
    )


# ---------------------------------------------------------------------------
# rank_opportunities (B8.4, B32.1)
# ---------------------------------------------------------------------------

def _sort_value(result: FCSResult, sort: str) -> float | None:
    key = str(sort).strip()
    aliases = {
        "fcs": "fcs",
        "funding30d": "funding_30d",
        "funding_30d": "funding_30d",
        "positiveratio30d": "positive_ratio_30d",
        "positive_ratio_30d": "positive_ratio_30d",
        "breakevendays": "break_even_days",
        "break_even_days": "break_even_days",
    }
    norm = aliases.get(key, aliases.get(key.lower(), key))
    if norm == "fcs":
        v = getattr(result, "fcs", None)
        if v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f == f and abs(f) != float("inf") else None
    # funding_metrics decimal strings.
    fm = getattr(result, "funding_metrics", {}) or {}
    if not isinstance(fm, Mapping):
        fm = {}
    if norm in ("funding_30d", "positive_ratio_30d"):
        raw = fm.get(norm)
        if raw is None:
            return None
        try:
            f = float(Decimal(str(raw)))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return f if f == f else None
    if norm == "break_even_days":
        for holder in (getattr(result, "risk", {}) or {}, fm, getattr(result, "venue_summary", {}) or {}):
            if isinstance(holder, Mapping):
                for k in ("break_even_days", "breakEvenDays", "breakeven_days"):
                    if holder.get(k) is not None:
                        try:
                            f = float(Decimal(str(holder[k])))
                        except (InvalidOperation, ValueError, TypeError):
                            continue
                        if f == f and abs(f) != float("inf"):
                            return f
        return None
    # Unknown sort key => fall back to fcs.
    v2 = getattr(result, "fcs", None)
    if v2 is None:
        return None
    try:
        return float(v2)
    except (TypeError, ValueError):
        return None


def rank_opportunities(
    results: Sequence[FCSResult],
    sort: str = "fcs",
    order: str = "desc",
) -> tuple[FCSResult, ...]:
    """Sort FCSResults. Numeric ASC/DESC, NULLS LAST, symbol ASC, ID ASC.

    Never normalises by available_max_score: raw FCS decides.
    """
    direction = str(order).strip().lower()
    if direction not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    reverse_primary = direction == "desc"

    decorated: list[tuple[int, Any, str, str, FCSResult]] = []
    for r in results:
        v = _sort_value(r, sort)
        is_null = 1 if v is None else 0
        # For desc we sort non-null descending; for asc ascending. Nulls always last.
        primary = v if v is not None else 0.0
        decorated.append((is_null, primary, str(getattr(r, "symbol", "")), str(getattr(r, "snapshot_id", "")), r))

    # Stable multi-pass: symbol/id ascending first, then primary.
    decorated.sort(key=lambda d: (d[2], d[3]))
    non_null = [d for d in decorated if d[0] == 0]
    nulls = [d for d in decorated if d[0] == 1]
    non_null.sort(key=lambda d: d[1], reverse=reverse_primary)
    # Ties on primary keep symbol/id order (sort is stable).
    # Re-stabilise ties explicitly:
    ordered: list[tuple[int, Any, str, str, FCSResult]] = []
    i = 0
    while i < len(non_null):
        j = i
        while j < len(non_null) and non_null[j][1] == non_null[i][1]:
            j += 1
        tie = sorted(non_null[i:j], key=lambda d: (d[2], d[3]))
        ordered.extend(tie)
        i = j
    ordered.extend(sorted(nulls, key=lambda d: (d[2], d[3])))
    return tuple(d[4] for d in ordered)
