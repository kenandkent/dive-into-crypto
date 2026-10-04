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
    "QUALITY_POLICY_VERSION",
    "QualityPolicy",
    "default_quality_policy",
    "quality_policy_from_config",
    "FieldState",
    "FieldCredit",
    "DQBreakdown",
    "data_quality",
    "full_tier_field_states",
    "round_half_up_1",
    "group_weights",
    "is_ready_required",
]

CATALYST_REQUIRED = "CATALYST_REQUIRED"
FULL_PREREQUISITE_MISSING = "FULL_PREREQUISITE_MISSING"

#: Version stamp for the frozen DQ policy bundle (design A5.3). New
#: production inputs are scored under this version; `policy=None` stays the
#: v1 legacy replay path (module constants, future-timestamp quirk kept so
#: stored history replays bit-identically).
QUALITY_POLICY_VERSION = "quality-policy-v2"

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


# ---------------------------------------------------------------------------
# Frozen DQ policy (F05, design A5.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityPolicy:
    """One frozen read of every DQ input (fail loud on garbage).

    Carries copies of the group weights, intra-group shares, PARTIAL
    denominators, freshness windows (group windows plus the
    ``supply_float``-style field overrides, resolved so every field maps
    through ``field_freshness``), the READY key-field sets and the
    ``policy_hash`` of the frozen config bundle when built from a config
    (``None`` for hand-built test policies). ``reject_future_timestamps``
    is always True on v2: a ``fetched_at`` after ``as_of`` is invalid
    (freshness 0), never clamped to FRESH.
    """

    group_weights_lite: Mapping[str, float]
    group_weights_full: Mapping[str, float]
    group_field_shares: Mapping[str, Mapping[str, float]]
    field_required_counts: Mapping[str, int]
    freshness_sec: Mapping[str, tuple[int, int]]
    field_freshness: Mapping[str, str]
    ready_required_lite: frozenset[str]
    ready_required_full: frozenset[str]
    version: str = QUALITY_POLICY_VERSION
    policy_hash: str | None = None
    reject_future_timestamps: bool = True

    def __post_init__(self) -> None:
        for name in ("group_weights_lite", "group_weights_full"):
            weights = getattr(self, name)
            total = sum(float(v) for v in weights.values())
            if total != 100:
                raise ValueError(f"{name} weights must sum to 100, got {total}")
        for group, shares in self.group_field_shares.items():
            total = sum(float(v) for v in shares.values())
            if abs(total - 1.0) > 1e-9:
                raise ValueError(
                    f"group {group!r} shares must sum to 1.0, got {total}"
                )
            for field_id in shares:
                window = self.field_freshness.get(field_id)
                if window is None:
                    raise ValueError(
                        f"field {field_id!r} has no freshness window mapping"
                    )
                if window not in self.freshness_sec:
                    raise ValueError(
                        f"field {field_id!r} maps to unknown window {window!r}"
                    )
        for window, (ttl, grace) in self.freshness_sec.items():
            if not grace > ttl > 0:
                raise ValueError(
                    f"freshness window {window!r} requires grace > ttl > 0, "
                    f"got ttl={ttl} grace={grace}"
                )
        for field_id, required in self.field_required_counts.items():
            if required is None or int(required) <= 0:
                raise ValueError(
                    f"required count for {field_id!r} must be positive"
                )


def default_quality_policy(*, policy_hash: str | None = None) -> QualityPolicy:
    """v2 policy from the frozen module constants (code-owned math tables).

    Freshness windows mirror ``shortlab/default.yaml``
    ``quality_freshness_sec`` plus the ``supply_float`` override -- the same
    values :func:`quality_policy_from_config` reads from a config, so the
    default config reproduces this policy exactly.
    """
    return QualityPolicy(
        group_weights_lite=dict(LITE_GROUP_WEIGHTS),
        group_weights_full=dict(FULL_GROUP_WEIGHTS),
        group_field_shares={
            group: dict(shares) for group, shares in GROUP_FIELD_SHARES.items()
        },
        field_required_counts=dict(FIELD_REQUIRED_COUNTS),
        freshness_sec={key: (ttl, grace) for key, (ttl, grace) in FRESHNESS_SEC.items()},
        field_freshness=dict(FIELD_FRESHNESS),
        ready_required_lite=frozenset(READY_REQUIRED_FIELDS_LITE),
        ready_required_full=frozenset(READY_REQUIRED_FIELDS_FULL),
        policy_hash=policy_hash,
    )


def quality_policy_from_config(
    config: Any, *, policy_hash: str | None = None
) -> QualityPolicy:
    """One frozen DQ-policy read from a validated config (F05/F06 handoff).

    TTL/grace windows come from ``config.quality_freshness_sec`` plus
    ``config.quality_field_overrides_sec``; shares, denominators and key
    sets stay the frozen code-owned tables. ``config`` is duck-typed (no
    import of ``shortlab.config``) so tests can pass light doubles.
    """
    freshness = getattr(config, "quality_freshness_sec")
    overrides = getattr(config, "quality_field_overrides_sec")
    windows: dict[str, tuple[int, int]] = {}
    for group, window in freshness.items():
        windows[str(group)] = (int(window.ttl), int(window.grace))
    for name, window in overrides.items():
        windows[str(name)] = (int(window.ttl), int(window.grace))
    return QualityPolicy(
        group_weights_lite=dict(LITE_GROUP_WEIGHTS),
        group_weights_full=dict(FULL_GROUP_WEIGHTS),
        group_field_shares={
            group: dict(shares) for group, shares in GROUP_FIELD_SHARES.items()
        },
        field_required_counts=dict(FIELD_REQUIRED_COUNTS),
        freshness_sec=windows,
        field_freshness=dict(FIELD_FRESHNESS),
        ready_required_lite=frozenset(READY_REQUIRED_FIELDS_LITE),
        ready_required_full=frozenset(READY_REQUIRED_FIELDS_FULL),
        policy_hash=policy_hash,
    )


def _result_status_of(result: Any) -> str:
    status = getattr(result, "status", None)
    text = str(status).upper() if status is not None else "UNAVAILABLE"
    if text not in ("OK", "PARTIAL", "NOT_APPLICABLE", "UNAVAILABLE", "ERROR"):
        return "UNAVAILABLE"
    return text


def full_tier_field_states(
    unlock_result: Any = None,
    social_result: Any = None,
    catalyst_result: Any = None,
    *,
    as_of_ms: int = 0,
) -> list[FieldState]:
    """Map Task 17 provider outcomes to FULL DQ field states (pure).

    One :class:`FieldState` per fixed section-18 field of the ``unlock`` /
    ``social`` / ``catalyst`` groups (3 + 4 + 2). ``OK`` fetches mark every
    field of the group ``OK`` (an ``OK`` fetch *is* the coverage -- a known
    empty unlock schedule or a quiet catalyst feed scores normally); any
    other status marks the group's fields with that same status and reason
    so DQ drops with no reweighting. ``None`` results (provider never
    queried, e.g. LITE path) yield no states at all.
    """
    states: list[FieldState] = []
    groups: tuple[tuple[Any, tuple[str, ...], str], ...] = (
        (unlock_result, ("unlock_30d", "unlock_90d", "unlock_allocation"), "unlock"),
        (
            social_result,
            (
                "social_volume",
                "social_contributors",
                "social_dominance",
                "social_window",
            ),
            "social",
        ),
        (
            catalyst_result,
            ("catalyst_coverage", "catalyst_dedup"),
            "catalyst",
        ),
    )
    for result, field_ids, source in groups:
        if result is None:
            continue
        status = _result_status_of(result)
        fetched = getattr(result, "fetched_at_ms", None)
        try:
            fetched_ms: int | None = None if fetched is None else int(fetched)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            fetched_ms = None
        reason = getattr(result, "reason_code", None)
        origin = getattr(result, "source", None) or source
        for field_id in field_ids:
            if status == "OK":
                states.append(
                    FieldState(
                        field_id=field_id,
                        status="OK",
                        fetched_at_ms=fetched_ms,
                        source=str(origin),
                    )
                )
            else:
                states.append(
                    FieldState(
                        field_id=field_id,
                        status=status,
                        fetched_at_ms=fetched_ms,
                        reason_code=str(reason) if reason is not None else "DATA_MISSING",
                        source=str(origin),
                    )
                )
    _ = as_of_ms  # freshness is evaluated inside data_quality against as_of_ms
    return states


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


def _coverage_for(
    state: FieldState, required_counts: Mapping[str, int] | None = None
) -> float:
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
                table = (
                    required_counts
                    if required_counts is not None
                    else FIELD_REQUIRED_COUNTS
                )
                required = float(table.get(state.field_id, 1))
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


def _freshness_for(
    field_id: str,
    fetched_at_ms: int | None,
    as_of_ms: int,
    *,
    field_windows: Mapping[str, str] | None = None,
    freshness_sec: Mapping[str, tuple[int, int]] | None = None,
    reject_future: bool = False,
) -> tuple[float, str]:
    windows = field_windows if field_windows is not None else FIELD_FRESHNESS
    table = freshness_sec if freshness_sec is not None else FRESHNESS_SEC
    window = windows.get(field_id)
    if window is None:
        raise ValueError(f"unknown field_id {field_id!r}")
    ttl, grace = table[window]
    if fetched_at_ms is None:
        return 0.0, "UNAVAILABLE"
    try:
        age_sec = (int(as_of_ms) - int(fetched_at_ms)) / 1000.0
    except (TypeError, ValueError):
        return 0.0, "UNAVAILABLE"
    if age_sec < 0:
        # v1 legacy replay keeps the historical clamp (fresh); the frozen
        # v2 policy treats future timestamps as invalid, never FRESH.
        if reject_future:
            return 0.0, "UNAVAILABLE"
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
    policy: QualityPolicy | None = None,
) -> DQBreakdown:
    """Compute Data Quality for ``tier`` (design section 18, exact formula).

    ``field_states`` is an iterable of :class:`FieldState` (or plain mappings
    with the same keys). Missing fields for an expected group count as
    ``UNAVAILABLE`` (credit 0, denominator kept). Groups absent from ``tier``
    are ignored. ``FULL`` with no catalyst fields at all raises ``ValueError``
    so callers cannot silently claim FULL without Catalyst coverage.

    ``policy`` is the frozen v2 bundle from :func:`quality_policy_from_config`
    (service, API and Entry read the same object once per decision); ``None``
    replays the v1 legacy tables bit-identically, including the historical
    future-timestamp clamp. The ``fraction * freshness`` formula, the
    1/0.5/0 freshness steps and the single final ROUND_HALF_UP are identical
    on both paths -- only the tables and the future-timestamp rule differ.
    """
    tier_key = str(tier).upper()
    if policy is None:
        weights = group_weights(tier_key)
        shares_table: Mapping[str, Mapping[str, float]] = GROUP_FIELD_SHARES
        required_counts: Mapping[str, int] = FIELD_REQUIRED_COUNTS
        field_windows: Mapping[str, str] = FIELD_FRESHNESS
        windows: Mapping[str, tuple[int, int]] = FRESHNESS_SEC
        key_set = (
            READY_REQUIRED_FIELDS_FULL if tier_key == "FULL" else READY_REQUIRED_FIELDS_LITE
        )
        reject_future = False
    else:
        if not isinstance(policy, QualityPolicy):
            raise ValueError(
                f"policy must be a QualityPolicy or None, got {type(policy).__name__}"
            )
        if tier_key == "LITE":
            weights = dict(policy.group_weights_lite)
        elif tier_key == "FULL":
            weights = dict(policy.group_weights_full)
        else:
            raise ValueError(f"unknown tier {tier!r}; expected 'LITE' or 'FULL'")
        shares_table = policy.group_field_shares
        required_counts = policy.field_required_counts
        field_windows = policy.field_freshness
        windows = policy.freshness_sec
        key_set = (
            policy.ready_required_full
            if tier_key == "FULL"
            else policy.ready_required_lite
        )
        reject_future = bool(policy.reject_future_timestamps)
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
            fid in by_id for fid in shares_table["catalyst"]
        )
        if not has_catalyst:
            raise ValueError(
                f"{FULL_PREREQUISITE_MISSING}: {CATALYST_REQUIRED} "
                "(FULL needs catalyst event coverage; use LITE until Catalyst "
                "is wired)"
            )

    group_credits: dict[str, float] = {}
    group_status: dict[str, str] = {}
    field_details: dict[str, FieldCredit] = {}
    stale_fields: list[str] = []

    for group, weight in weights.items():
        shares = shares_table[group]
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
                coverage = _coverage_for(state, required_counts)
                if status in ("UNAVAILABLE", "ERROR"):
                    freshness, label = 0.0, "UNAVAILABLE"
                else:
                    freshness, label = _freshness_for(
                        field_id,
                        state.fetched_at_ms,
                        as_of,
                        field_windows=field_windows,
                        freshness_sec=windows,
                        reject_future=reject_future,
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
