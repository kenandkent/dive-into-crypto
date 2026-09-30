"""Task 11: Data Quality (design section 18 / 18.1).

Pure computation (no HTTP / SQL). Implements, verbatim:

- LITE / FULL group weights (LITE 35/20/20/15/10, FULL 25/15/15/10/10/10/10/5,
  each summing to 100) and the fixed intra-group field shares from section 18.
- ``DataQuality = sum(group_weight * group_credit)`` with a single final
  ROUND_HALF_UP to 1 decimal. ``group_credit`` normalises over the *applicable*
  denominator only: ``NOT_APPLICABLE`` removes that field's share from the
  denominator (a fully N/A group contributes 0); ``UNAVAILABLE``/``ERROR``
  stay in the denominator with credit 0. Scoring weights are never rebalanced.
- ``field_credit = coverage * freshness`` where freshness is 1 (age <= ttl),
  0.5 (ttl < age <= grace, stale) or 0 (age > grace, expired). TTL/grace
  defaults mirror ``shortlab/default.yaml`` ``quality_freshness_sec`` plus the
  ``supply_float`` 6h/12h override; funding fields use the section 10.1 time
  coverage supplied by the caller.
- PARTIAL coverage (non-funding) is ``valid_count / required_count`` with
  fixed denominators: market daily price 70, spot 60D quote volume 60,
  supply/float 2, ATH 2, two-sided book 2, every other single-value field 1.
  Funding PARTIAL coverage must be supplied as the section 10.1 time-coverage
  fraction.
- FULL without Catalyst coverage is not enabled: requesting ``tier="FULL"``
  with no catalyst fields present raises ``ValueError`` (``CATALYST_REQUIRED``
  / ``FULL_PREREQUISITE_MISSING``). A present-but-``UNAVAILABLE`` catalyst
  still computes (DQ drops, READY blocked downstream) so transient 429s keep
  the FULL snapshot instead of silently downgrading to LITE.
- Top-level ``stale`` is true iff at least one READY-required *applicable*
  field with status OK/PARTIAL is stale-or-expired (freshness < 1).
  Non-key stale fields only appear in ``field_details`` and lower DQ.

Public surface: :func:`data_quality`, :class:`FieldState`,
:class:`DQBreakdown`, weight/share constants and ``READY_REQUIRED_FIELDS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

__all__ = [
    "LITE_GROUP_WEIGHTS",
    "FULL_GROUP_WEIGHTS",
    "GROUP_FIELD_SHARES",
    "FIELD_TO_GROUP",
    "FIELD_FRESHNESS",
    "FRESHNESS_SEC",
    "FIELD_REQUIRED_COUNTS",
    "READY_REQUIRED_FIELDS_LITE",
    "READY_REQUIRED_FIELDS_FULL",
    "CATALYST_REQUIRED",
    "FULL_PREREQUISITE_MISSING",
    "FieldState",
    "FieldCredit",
    "DQBreakdown",
    "data_quality",
    "round_half_up_1",
    "group_weights",
    "is_ready_required",
]

CATALYST_REQUIRED = "CATALYST_REQUIRED"
FULL_PREREQUISITE_MISSING = "FULL_PREREQUISITE_MISSING"

# ---------------------------------------------------------------------------
# Group weights (design section 18 tables). Each sums to 100.
# ---------------------------------------------------------------------------

LITE_GROUP_WEIGHTS: dict[str, float] = {
    "market_futures": 35,
    "funding_history": 20,
    "fundamentals": 20,
    "spot_liquidity": 15,
    "identity_profile": 10,
}

FULL_GROUP_WEIGHTS: dict[str, float] = {
    "market_futures": 25,
    "funding_history": 15,
    "fundamentals": 15,
    "spot_liquidity": 10,
    "identity_profile": 10,
    "unlock": 10,
    "social": 10,
    "catalyst": 5,
}

# ---------------------------------------------------------------------------
# Intra-group field shares (fractions summing to 1.0 per group, design 18).
# ---------------------------------------------------------------------------

GROUP_FIELD_SHARES: dict[str, dict[str, float]] = {
    "market_futures": {
        "market_daily_price": 0.35,
        "futures_qv_1d": 0.20,
        "oi_usd": 0.25,
        "contract_status": 0.20,
    },
    "funding_history": {
        "funding_7d": 0.20,
        "funding_30d": 0.40,
        "funding_90d": 0.40,
    },
    "fundamentals": {
        "mc": 0.30,
        "fdv": 0.20,
        "supply_float": 0.25,
        "ath": 0.25,
    },
    "spot_liquidity": {
        "spot_60d_qv": 0.40,
        "spot_24h_qv": 0.20,
        "basis": 0.20,
        "book_depth": 0.20,
    },
    "identity_profile": {
        "canonical_mapping": 0.60,
        "profile_basis": 0.40,
    },
    "unlock": {
        "unlock_30d": 0.40,
        "unlock_90d": 0.40,
        "unlock_allocation": 0.20,
    },
    "social": {
        "social_volume": 0.35,
        "social_contributors": 0.25,
        "social_dominance": 0.25,
        "social_window": 0.15,
    },
    "catalyst": {
        "catalyst_coverage": 0.60,
        "catalyst_dedup": 0.40,
    },
}

FIELD_TO_GROUP: dict[str, str] = {
    field_id: group
    for group, fields in GROUP_FIELD_SHARES.items()
    for field_id in fields
}

# Which freshness window (key of FRESHNESS_SEC) governs each field.
FIELD_FRESHNESS: dict[str, str] = {
    "market_daily_price": "market_futures",
    "futures_qv_1d": "market_futures",
    "oi_usd": "market_futures",
    "contract_status": "market_futures",
    "funding_7d": "funding_history",
    "funding_30d": "funding_history",
    "funding_90d": "funding_history",
    "mc": "fundamentals",
    "fdv": "fundamentals",
    "supply_float": "supply_float",
    "ath": "fundamentals",
    "spot_60d_qv": "spot_liquidity",
    "spot_24h_qv": "spot_liquidity",
    "basis": "spot_liquidity",
    "book_depth": "spot_liquidity",
    "canonical_mapping": "identity_profile",
    "profile_basis": "identity_profile",
    "unlock_30d": "unlock",
    "unlock_90d": "unlock",
    "unlock_allocation": "unlock",
    "social_volume": "social",
    "social_contributors": "social",
    "social_dominance": "social",
    "social_window": "social",
    "catalyst_coverage": "catalyst",
    "catalyst_dedup": "catalyst",
}

# (ttl_sec, grace_sec) mirroring default.yaml quality_freshness_sec plus the
# supply_float override. grace > ttl > 0 is asserted at import.
FRESHNESS_SEC: dict[str, tuple[int, int]] = {
    "market_futures": (900, 3600),
    "funding_history": (1800, 7200),
    "fundamentals": (3600, 21600),
    "supply_float": (21600, 43200),
    "spot_liquidity": (3600, 10800),
    "identity_profile": (86400, 259200),
    "unlock": (43200, 129600),
    "social": (10800, 43200),
    "catalyst": (1800, 7200),
}

for _k, (_ttl, _grace) in FRESHNESS_SEC.items():
    assert _grace > _ttl > 0, f"freshness window {_k} must satisfy grace > ttl > 0"

# Fixed PARTIAL denominators (design 18, paragraph 3). Funding fields are
# deliberately absent: their coverage is the section 10.1 time-coverage
# fraction supplied by the caller.
FIELD_REQUIRED_COUNTS: dict[str, int] = {
    "market_daily_price": 70,
    "spot_60d_qv": 60,
    "supply_float": 2,
    "ath": 2,
    "book_depth": 2,
}

_FUNDING_FIELDS = frozenset({"funding_7d", "funding_30d", "funding_90d"})

# READY-required (key) fields. A stale-or-expired key field raises top-level
# ``stale`` and downstream ``READY_INPUT_STALE``; auxiliary fields (basis /
# book depth / profile basis / social window / catalyst dedup) only appear in
# ``field_details`` and lower DQ. Rationale: key fields feed critical LTSS
# inputs, Tradeability hard gates, funding windows or canonical identity;
# the auxiliaries are diagnostic depth/classification details.
READY_REQUIRED_FIELDS_LITE: frozenset[str] = frozenset(
    {
        "market_daily_price",
        "futures_qv_1d",
        "oi_usd",
        "contract_status",
        "funding_7d",
        "funding_30d",
        "funding_90d",
        "mc",
        "fdv",
        "supply_float",
        "ath",
        "spot_60d_qv",
        "spot_24h_qv",
        "canonical_mapping",
    }
)

READY_REQUIRED_FIELDS_FULL: frozenset[str] = frozenset(
    set(READY_REQUIRED_FIELDS_LITE)
    | {
        "unlock_30d",
        "unlock_90d",
        "unlock_allocation",
        "social_volume",
        "social_contributors",
        "social_dominance",
        "catalyst_coverage",
    }
)


def group_weights(tier: str) -> dict[str, float]:
    """Return the fixed outer group weights for ``tier`` (sums to 100)."""
    key = str(tier).upper()
    if key == "LITE":
        return dict(LITE_GROUP_WEIGHTS)
    if key == "FULL":
        return dict(FULL_GROUP_WEIGHTS)
    raise ValueError(f"unknown tier {tier!r}; expected 'LITE' or 'FULL'")


def is_ready_required(field_id: str, tier: str) -> bool:
    """True when ``field_id`` is a READY-required key field for ``tier``."""
    key = str(tier).upper()
    if key == "FULL":
        return field_id in READY_REQUIRED_FIELDS_FULL
    return field_id in READY_REQUIRED_FIELDS_LITE


def round_half_up_1(value: float) -> float:
    """Round to 1 decimal with ROUND_HALF_UP (design: final step only)."""
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Input / output DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldState:
    """One DQ input field.

    ``status`` follows the frozen ``ProviderResult`` vocabulary (OK / PARTIAL /
    NOT_APPLICABLE / UNAVAILABLE / ERROR). ``coverage`` is the verified
    coverage fraction for PARTIAL funding fields (section 10.1 time coverage);
    for non-funding PARTIAL fields prefer ``valid_count`` (with the fixed
    ``FIELD_REQUIRED_COUNTS`` denominator, default 1) so ``60/70`` stays
    ``0.857142...``. ``fetched_at_ms`` drives freshness against ``as_of_ms``.
    """

    field_id: str
    status: str = "OK"
    coverage: float | None = None
    valid_count: float | None = None
    required_count: float | None = None
    fetched_at_ms: int | None = None
    reason_code: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class FieldCredit:
    field_id: str
    group: str
    share: float
    status: str
    coverage: float
    freshness: float
    freshness_label: str  # FRESH | STALE | EXPIRED | NOT_APPLICABLE | UNAVAILABLE
    credit: float
    fetched_at_ms: int | None
    reason_code: str | None
    is_key: bool


@dataclass(frozen=True)
class DQBreakdown:
    """Deterministic DQ result (never API JSON)."""

    tier: str
    data_quality: float
    stale: bool
    ready_input_stale: bool
    ready_stale_fields: tuple[str, ...]
    group_credits: dict[str, float]
    group_status: dict[str, str]
    field_details: dict[str, FieldCredit]

    @property
    def dq(self) -> float:
        return self.data_quality


def _coerce_field(raw: Any) -> FieldState:
    if isinstance(raw, FieldState):
        return raw
    if isinstance(raw, Mapping):
        return FieldState(
            field_id=str(raw.get("field_id", raw.get("field", ""))),
            status=str(raw.get("status", "OK")),
            coverage=(
                None
                if raw.get("coverage") is None
                and raw.get("coverage_fraction") is None
                else float(
                    raw.get("coverage", raw.get("coverage_fraction"))  # type: ignore[arg-type]
                )
            ),
            valid_count=(
                None if raw.get("valid_count") is None else float(raw.get("valid_count"))  # type: ignore[arg-type]
            ),
            required_count=(
                None
                if raw.get("required_count") is None
                else float(raw.get("required_count"))  # type: ignore[arg-type]
            ),
            fetched_at_ms=raw.get("fetched_at_ms"),
            reason_code=raw.get("reason_code"),
            source=raw.get("source"),
        )
    raise ValueError(f"unsupported field_state {raw!r}")


def _clamp01(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def _coverage_for(state: FieldState) -> float:
    status = str(state.status).upper()
    if status == "OK":
        return 1.0
    if status in ("UNAVAILABLE", "ERROR"):
        return 0.0
    if status == "NOT_APPLICABLE":
        return 0.0  # excluded from denominator; credit unused
    if status == "PARTIAL":
        if state.coverage is not None:
            try:
                return _clamp01(float(state.coverage))
            except (TypeError, ValueError):
                return 0.0
        if state.valid_count is not None:
            required = state.required_count
            if required is None:
                required = float(
                    FIELD_REQUIRED_COUNTS.get(state.field_id, 1)
                )
            try:
                required_f = float(required)
            except (TypeError, ValueError):
                return 0.0
            if required_f <= 0:
                return 0.0
            return _clamp01(float(state.valid_count) / required_f)
        # Funding PARTIAL without an explicit time-coverage fraction, or any
        # other PARTIAL without counts, carries no verified coverage.
        return 0.0
    raise ValueError(
        f"unknown field status {state.status!r} for field {state.field_id!r}"
    )


def _freshness_for(field_id: str, fetched_at_ms: int | None, as_of_ms: int) -> tuple[float, str]:
    window = FIELD_FRESHNESS.get(field_id)
    if window is None:
        raise ValueError(f"unknown field_id {field_id!r}")
    ttl, grace = FRESHNESS_SEC[window]
    if fetched_at_ms is None:
        return 0.0, "UNAVAILABLE"
    try:
        age_sec = (int(as_of_ms) - int(fetched_at_ms)) / 1000.0
    except (TypeError, ValueError):
        return 0.0, "UNAVAILABLE"
    if age_sec < 0:
        age_sec = 0.0
    if age_sec <= ttl:
        return 1.0, "FRESH"
    if age_sec <= grace:
        return 0.5, "STALE"
    return 0.0, "EXPIRED"


def data_quality(
    tier: str,
    field_states: Any,
    as_of_ms: int,
) -> DQBreakdown:
    """Compute Data Quality for ``tier`` (design section 18, exact formula).

    ``field_states`` is an iterable of :class:`FieldState` (or plain mappings
    with the same keys). Missing fields for an expected group count as
    ``UNAVAILABLE`` (credit 0, denominator kept). Groups absent from ``tier``
    are ignored. ``FULL`` with no catalyst fields at all raises ``ValueError``
    so callers cannot silently claim FULL without Catalyst coverage.
    """
    tier_key = str(tier).upper()
    weights = group_weights(tier_key)
    as_of = int(as_of_ms)

    states: list[FieldState] = [_coerce_field(item) for item in (field_states or [])]
    by_id: dict[str, FieldState] = {}
    for item in states:
        if not item.field_id:
            raise ValueError("field_state requires a non-empty field_id")
        if item.field_id not in FIELD_TO_GROUP:
            raise ValueError(f"unknown field_id {item.field_id!r}")
        # Last occurrence wins; inputs are expected to be unique per field.
        by_id[item.field_id] = item

    if tier_key == "FULL":
        has_catalyst = any(
            fid in by_id for fid in GROUP_FIELD_SHARES["catalyst"]
        )
        if not has_catalyst:
            raise ValueError(
                f"{FULL_PREREQUISITE_MISSING}: {CATALYST_REQUIRED} "
                "(FULL needs catalyst event coverage; use LITE until Catalyst "
                "is wired)"
            )

    key_set = (
        READY_REQUIRED_FIELDS_FULL if tier_key == "FULL" else READY_REQUIRED_FIELDS_LITE
    )

    group_credits: dict[str, float] = {}
    group_status: dict[str, str] = {}
    field_details: dict[str, FieldCredit] = {}
    stale_fields: list[str] = []

    for group, weight in weights.items():
        shares = GROUP_FIELD_SHARES[group]
        num = 0.0
        den = 0.0
        applicable = 0
        any_unavailable = False
        any_partial_or_stale = False
        for field_id, share in shares.items():
            state = by_id.get(field_id)
            status = str(state.status).upper() if state is not None else "UNAVAILABLE"
            if status == "NOT_APPLICABLE":
                field_details[field_id] = FieldCredit(
                    field_id=field_id,
                    group=group,
                    share=share,
                    status=status,
                    coverage=0.0,
                    freshness=0.0,
                    freshness_label="NOT_APPLICABLE",
                    credit=0.0,
                    fetched_at_ms=state.fetched_at_ms if state else None,
                    reason_code=state.reason_code if state else None,
                    is_key=field_id in key_set,
                )
                continue
            if state is None:
                coverage = 0.0
                freshness, label = 0.0, "UNAVAILABLE"
                fetched: int | None = None
                reason: str | None = "FIELD_MISSING"
            else:
                coverage = _coverage_for(state)
                if status in ("UNAVAILABLE", "ERROR"):
                    freshness, label = 0.0, "UNAVAILABLE"
                else:
                    freshness, label = _freshness_for(
                        field_id, state.fetched_at_ms, as_of
                    )
                fetched = state.fetched_at_ms
                reason = state.reason_code
            credit = coverage * freshness
            field_details[field_id] = FieldCredit(
                field_id=field_id,
                group=group,
                share=share,
                status=status,
                coverage=coverage,
                freshness=freshness,
                freshness_label=label,
                credit=credit,
                fetched_at_ms=fetched,
                reason_code=reason,
                is_key=field_id in key_set,
            )
            den += share
            num += share * credit
            applicable += 1
            if status in ("UNAVAILABLE", "ERROR") or label in ("EXPIRED", "UNAVAILABLE"):
                any_unavailable = True
            if status == "PARTIAL" or label == "STALE":
                any_partial_or_stale = True
            if (
                field_id in key_set
                and status in ("OK", "PARTIAL")
                and freshness < 1.0
            ):
                stale_fields.append(field_id)

        if applicable == 0:
            group_credits[group] = 0.0
            group_status[group] = "NOT_APPLICABLE"
        else:
            group_credits[group] = num / den if den > 0 else 0.0
            if any_unavailable and group_credits[group] <= 0:
                group_status[group] = "UNAVAILABLE"
            elif any_unavailable or any_partial_or_stale or group_credits[group] < 1.0:
                group_status[group] = "PARTIAL"
            else:
                group_status[group] = "OK"

    total = sum(weights[group] * group_credits[group] for group in weights)
    dq = round_half_up_1(total)
    # Clamp half-up artefacts (e.g. 100.0000001) into [0, 100].
    dq = max(0.0, min(100.0, dq))
    ready_stale = tuple(sorted(set(stale_fields)))
    return DQBreakdown(
        tier=tier_key,
        data_quality=dq,
        stale=bool(ready_stale),
        ready_input_stale=bool(ready_stale),
        ready_stale_fields=ready_stale,
        group_credits=dict(group_credits),
        group_status=dict(group_status),
        field_details=dict(field_details),
    )
