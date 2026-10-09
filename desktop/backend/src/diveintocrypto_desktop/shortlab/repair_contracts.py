"""R00 repair freeze: DTOs, strict JSON, Decimal canonicalisation (D03/D18).

Frozen contract for all downstream repair tasks. New DTOs are defined here;
pre-existing domain types are *re-exported* (never redefined as synonyms):

- :class:`AssetIdentity` from :mod:`shortlab.models`
- :class:`FundingMetrics`, :class:`TradingRulesSnapshot`,
  :class:`SpotVenueQuote`, :class:`OnchainQuote` from :mod:`hedge.models`
- :class:`Observed`, :class:`ObservationMeta` from :mod:`observations`
- :class:`RequestContext` from :mod:`request_budget`

Conventions (D03):

- internal snake_case, HTTP camelCase (see :func:`to_api_dict` in hedge.models);
- time UTC ms ints; amounts Decimal fixed-point strings (no exponent,
  no trailing zeros, ``-0`` -> ``0``); missing is null + reason, never 0;
- JSON objects forbid duplicate keys; snake/camel synonyms must not co-exist
  (enforced in :func:`load_json_strict` + DTO constructors);
- frozen dataclasses; nested mappings are defensively copied so external
  mutation cannot alter snapshots.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

# -- re-exports (no synonymous redefinition) ---------------------------------
from diveintocrypto_desktop.shortlab.hedge.models import (  # noqa: F401
    FundingMetrics,
    OnchainQuote,
    SpotVenueQuote,
    TradingRulesSnapshot,
    require_decimal_str,
    require_optional_decimal_str,
)
from diveintocrypto_desktop.shortlab.models import AssetIdentity  # noqa: F401
from diveintocrypto_desktop.shortlab.observations import (  # noqa: F401
    ObservationMeta,
    Observed,
)
from diveintocrypto_desktop.shortlab.request_budget import RequestContext  # noqa: F401

__all__ = [
    "REPAIR_CONTRACT_VERSION",
    "REPAIR_CONTRACT_VERSIONS",
    "GATE_STATUSES",
    "READINESS_VALUES",
    "PROTECTION_STATUSES",
    "HISTORY_CLASSES",
    "COHORTS",
    "STRATEGIES",
    "GOALS",
    "RECOMMENDATIONS",
    "VALIDATION_LEVELS",
    "SCENARIO_STATUSES",
    "FUNDING_BASES",
    "VERIFICATIONS",
    "PRICE_BASES",
    "AssetIdentity",
    "FundingMetrics",
    "TradingRulesSnapshot",
    "SpotVenueQuote",
    "OnchainQuote",
    "Observed",
    "ObservationMeta",
    "RequestContext",
    "require_decimal_str",
    "require_optional_decimal_str",
    "normalize_decimal_str",
    "decimal_to_str",
    "load_json_strict",
    "canonical_json",
    "to_record_dict",
    "from_record_dict",
    "GateResult",
    "ReadinessBreakdown",
    "FundingScheduleSegment",
    "FundingCoverage",
    "FundingContext",
    "FuturesExecutionQuote",
    "EconomicsResult",
    "RatioProposal",
    "ScenarioResult",
    "DecisionRequest",
    "DecisionContext",
    "DecisionResult",
    "OpportunityQuery",
    "OpportunityPage",
    "LedgerPnl",
    "PairExitGuidance",
    "CaptureContext",
    "CaptureResult",
    "QuoteCollectionResult",
]

#: New JSON schema version for all repair persistence (D15).
REPAIR_CONTRACT_VERSION = "repair-contract-v1"
REPAIR_CONTRACT_VERSIONS = frozenset({REPAIR_CONTRACT_VERSION})

GATE_STATUSES = frozenset({"PASS", "FAIL", "UNKNOWN"})
READINESS_VALUES = frozenset({"READY", "NOT_READY", "BLOCKED"})
PROTECTION_STATUSES = frozenset(
    {"CONFIRMED", "PENDING", "UNSUPPORTED", "UNKNOWN", "EXPIRED", "MANUAL_EXIT_ONLY"}
)
HISTORY_CLASSES = frozenset(
    {"FULL_90D", "PARTIAL_90D", "INSUFFICIENT", "HISTORY_CLASS_UNKNOWN"}
)
COHORTS = frozenset(
    {"RESEARCH_CANDIDATE", "EXECUTABLE_DIRECTIONAL", "FUNDING_CARRY", "USER_DECISION"}
)
STRATEGIES = frozenset(
    {
        "UNHEDGED_0",
        "ABSOLUTE_100",
        "RELATIVE_75",
        "RELATIVE_50",
        "RELATIVE_25",
        "SYSTEM_POLICY",
    }
)
GOALS = frozenset({"CARRY_CAPTURE", "DIRECTIONAL_SHORT", "BALANCED"})
RECOMMENDATIONS = frozenset(
    {
        "DATA_INSUFFICIENT",
        "MANUAL_REVIEW",
        "AVOID",
        "NO_HEDGE",
        "PARTIAL_HEDGE",
        "FULL_HEDGE",
    }
)
VALIDATION_LEVELS = frozenset({"RULE_BASED_UNVALIDATED"})
SCENARIO_STATUSES = frozenset({"VALID", "INVALID_AFTER_LIQUIDATION", "UNKNOWN"})
FUNDING_BASES = frozenset({"ACTUAL_RECEIPTS_ONLY", "ESTIMATED"})
VERIFICATIONS = frozenset({"CONFIRMED", "INFERRED", "UNKNOWN"})
PRICE_BASES = frozenset({"TRADE", "MARK"})


# ---------------------------------------------------------------------------
# Decimal canonicalisation (D03.1)
# ---------------------------------------------------------------------------


def normalize_decimal_str(value: str | Decimal) -> str:
    """Canonical fixed-point decimal string (no exponent, no trailing zeros).

    ``-0``/``-0.00`` normalises to ``0``. Raises ``ValueError`` for
    non-finite or non-decimal input.
    """
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = Decimal(value.strip())
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise ValueError(f"not a decimal string: {value!r}") from exc
    else:
        raise ValueError(f"not a decimal string: {value!r}")
    if not parsed.is_finite():
        raise ValueError(f"decimal must be finite, got {value!r}")
    if parsed == 0:
        return "0"
    # Fixed-point, strip trailing zeros, no exponent.
    text = format(parsed, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if text in ("-0", "-0.0"):
            return "0"
    return text


def decimal_to_str(value: str | Decimal) -> str:
    """Alias for :func:`normalize_decimal_str` (ledger boundary)."""
    return normalize_decimal_str(value)


# ---------------------------------------------------------------------------
# Strict JSON (D03.1): duplicate keys rejected, NaN/Infinity rejected
# ---------------------------------------------------------------------------


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def load_json_strict(text: str | bytes) -> Mapping[str, Any] | Sequence[Any]:
    """Parse JSON strictly: duplicate keys, NaN/Infinity rejected.

    Returns a mapping or sequence; top-level scalars are rejected to keep
    DTO envelopes explicit. Snake/camel synonym co-existence is enforced
    by DTO constructors (see ``from_api_dict``-style checks), not here.
    """
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"invalid UTF-8 JSON: {exc}") from exc
    if not isinstance(text, str):
        raise ValueError("load_json_strict requires str or bytes")
    def _reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON constant rejected: {value!r}")

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    except ValueError:
        raise
    if isinstance(parsed, bool) or not isinstance(parsed, (dict, list)):
        raise ValueError("strict JSON top level must be object or array")
    return parsed


def canonical_json(value: Any) -> str:
    """Canonical JSON (sorted keys, compact, no trailing newline)."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def to_record_dict(dto: Any) -> dict[str, Any]:
    """Serialize a frozen DTO for persistence, injecting schema version.

    The top level gains ``schema_version=repair-contract-v1``; nested DTOs
    do not repeat it (D18.2).
    """
    if dataclasses.is_dataclass(dto) and not isinstance(dto, type):
        data = dataclasses.asdict(dto)
    elif isinstance(dto, Mapping):
        data = dict(dto)
    else:
        raise ValueError("to_record_dict requires a frozen DTO or mapping")
    # Convert tuples to lists for JSON, keep Decimal strings as-is.
    def _freeze(value: Any) -> Any:
        if isinstance(value, tuple):
            return [_freeze(v) for v in value]
        if isinstance(value, Mapping):
            return {k: _freeze(v) for k, v in value.items()}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return _freeze(dataclasses.asdict(value))
        return value

    record = _freeze(data)
    if not isinstance(record, dict):
        raise ValueError("to_record_dict requires a mapping-shaped DTO")
    record["schema_version"] = REPAIR_CONTRACT_VERSION
    return record


def from_record_dict(cls: Any, payload: Mapping[str, Any]) -> Any:
    """Validate top-level version, then construct ``cls`` (D07.3)."""
    if not isinstance(payload, Mapping):
        raise ValueError("record must be a mapping")
    version = payload.get("schema_version")
    if version != REPAIR_CONTRACT_VERSION:
        raise ValueError(
            f"{cls.__name__} requires schema_version={REPAIR_CONTRACT_VERSION!r}, "
            f"got {version!r}"
        )
    kwargs = {k: v for k, v in payload.items() if k != "schema_version"}
    # Allow both snake_case construction; camelCase must be converted by caller.
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ValueError(f"cannot build {cls.__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _check_enum(name: str, value: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{name}={value!r} must be one of {sorted(allowed)}")
    return value


def _check_int(name: str, value: Any, *, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if lo is not None and value < lo:
        raise ValueError(f"{name} must be >= {lo}, got {value}")
    if hi is not None and value > hi:
        raise ValueError(f"{name} must be <= {hi}, got {value}")
    return value


def _coerce_str_tuple(name: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    items = tuple(value) if isinstance(value, (list, tuple)) else (value,)
    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"{name} must hold str, got {item!r}")
    # Sorted + deduped for reasons (D03.3); order preserved elsewhere via explicit lists.
    return items


def _coerce_sorted_reasons(value: Any) -> tuple[str, ...]:
    items = _coerce_str_tuple("reasons", value)
    return tuple(sorted(set(items)))


def _coerce_dict(name: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping, got {value!r}")
    return dict(value)


def _require_observed(name: str, value: Any) -> Any:
    # Accept Observed instances (frozen) or explicit mappings for test legs.
    if isinstance(value, Observed):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError(f"{name} must be Observed or mapping, got {type(value).__name__}")


# ---------------------------------------------------------------------------
# D18 DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    """Unified tri-state gate (D03.3): FAIL/UNKNOWN never count as PASS."""

    status: str
    reasons: tuple[str, ...] = ()
    checked_at_ms: int = 0
    input_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_enum("status", self.status, GATE_STATUSES)
        object.__setattr__(self, "reasons", _coerce_sorted_reasons(self.reasons))
        _check_int("checked_at_ms", self.checked_at_ms, lo=0)
        refs = _coerce_dict("input_refs", self.input_refs)
        for k, v in refs.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("input_refs must map str->str")
        object.__setattr__(self, "input_refs", refs)


@dataclass(frozen=True)
class ReadinessBreakdown:
    data_complete: bool
    funding_gate: GateResult
    execution_gate: GateResult
    economic_gate: GateResult
    protection_status: str
    readiness: str

    def __post_init__(self) -> None:
        if not isinstance(self.data_complete, bool):
            raise ValueError("data_complete must be bool")
        for name in ("funding_gate", "execution_gate", "economic_gate"):
            if not isinstance(getattr(self, name), GateResult):
                raise ValueError(f"{name} must be GateResult")
        _check_enum("protection_status", self.protection_status, PROTECTION_STATUSES)
        _check_enum("readiness", self.readiness, READINESS_VALUES)


@dataclass(frozen=True)
class FundingScheduleSegment:
    schedule_id: str
    symbol: str
    effective_from_ms: int
    effective_to_ms: int | None
    interval_hours: int
    anchor_ms: int
    known_at_ms: int
    source: str
    evidence_ref: str
    verification: str

    def __post_init__(self) -> None:
        for name in ("schedule_id", "symbol", "source", "evidence_ref"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty str")
        _check_int("effective_from_ms", self.effective_from_ms, lo=0)
        if self.effective_to_ms is not None:
            _check_int("effective_to_ms", self.effective_to_ms, lo=0)
            if self.effective_to_ms <= self.effective_from_ms:
                raise ValueError("effective_to_ms must be > effective_from_ms")
        _check_int("interval_hours", self.interval_hours, lo=1)
        _check_int("anchor_ms", self.anchor_ms, lo=0)
        _check_int("known_at_ms", self.known_at_ms, lo=0)
        _check_enum("verification", self.verification, VERIFICATIONS)


@dataclass(frozen=True)
class FundingCoverage:
    window_start_ms: int
    window_end_ms: int
    expected_count: int | None
    received_count: int
    coverage_fraction: str | None
    schedule_coverage_fraction: str
    missing_slots: tuple[int, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_int("window_start_ms", self.window_start_ms, lo=0)
        _check_int("window_end_ms", self.window_end_ms, lo=0)
        if self.window_end_ms <= self.window_start_ms:
            raise ValueError("window_end_ms must be > window_start_ms")
        if self.expected_count is not None:
            _check_int("expected_count", self.expected_count, lo=0)
        _check_int("received_count", self.received_count, lo=0)
        require_optional_decimal_str("coverage_fraction", self.coverage_fraction)
        require_decimal_str(
            "schedule_coverage_fraction", self.schedule_coverage_fraction
        )
        slots = tuple(self.missing_slots) if isinstance(self.missing_slots, (list, tuple)) else ()
        for s in slots:
            _check_int("missing_slots[]", s, lo=0)
        object.__setattr__(self, "missing_slots", slots)
        object.__setattr__(self, "reasons", _coerce_sorted_reasons(self.reasons))


@dataclass(frozen=True)
class FundingContext:
    metrics: FundingMetrics
    coverage_7d: FundingCoverage
    coverage_30d: FundingCoverage
    coverage_90d: FundingCoverage
    history_class: str
    listing_age_days: int | None
    conservative_apr: str | None
    conservative_method: str
    current_observation: Any
    last_settled_observation: Any | None
    schedule_refs: tuple[str, ...] = ()
    input_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.metrics, FundingMetrics):
            raise ValueError("metrics must be FundingMetrics")
        for name in ("coverage_7d", "coverage_30d", "coverage_90d"):
            if not isinstance(getattr(self, name), FundingCoverage):
                raise ValueError(f"{name} must be FundingCoverage")
        _check_enum("history_class", self.history_class, HISTORY_CLASSES)
        if self.listing_age_days is not None:
            _check_int("listing_age_days", self.listing_age_days, lo=0)
        require_optional_decimal_str("conservative_apr", self.conservative_apr)
        if not isinstance(self.conservative_method, str) or not self.conservative_method:
            raise ValueError("conservative_method must be non-empty str")
        object.__setattr__(
            self, "current_observation", _require_observed("current_observation", self.current_observation)
        )
        if self.last_settled_observation is not None:
            object.__setattr__(
                self,
                "last_settled_observation",
                _require_observed("last_settled_observation", self.last_settled_observation),
            )
        object.__setattr__(self, "schedule_refs", _coerce_str_tuple("schedule_refs", self.schedule_refs))
        object.__setattr__(self, "input_refs", _coerce_dict("input_refs", self.input_refs))


@dataclass(frozen=True)
class FuturesExecutionQuote:
    quote_id: str
    symbol: str
    requested_contract_qty: str
    buy_vwap_native: str | None
    sell_vwap_native: str | None
    buy_executable_qty: str
    sell_executable_qty: str
    quote_currency: str
    quote_to_usd: str | None
    as_of_ms: int
    known_at_ms: int
    expires_at_ms: int
    book_observation_id: str
    fees_included: bool | None = None

    def __post_init__(self) -> None:
        for name in ("quote_id", "symbol", "quote_currency", "book_observation_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty str")
        require_decimal_str("requested_contract_qty", self.requested_contract_qty)
        require_optional_decimal_str("buy_vwap_native", self.buy_vwap_native)
        require_optional_decimal_str("sell_vwap_native", self.sell_vwap_native)
        require_decimal_str("buy_executable_qty", self.buy_executable_qty)
        require_decimal_str("sell_executable_qty", self.sell_executable_qty)
        require_optional_decimal_str("quote_to_usd", self.quote_to_usd)
        for name in ("as_of_ms", "known_at_ms", "expires_at_ms"):
            _check_int(name, getattr(self, name), lo=0)
        if self.fees_included is not None and not isinstance(self.fees_included, bool):
            raise ValueError("fees_included must be bool or None")


@dataclass(frozen=True)
class EconomicsResult:
    hold_days: int | None
    actual_futures_notional_usd: str
    conservative_carry_usd: str | None
    roundtrip_cost_usd: str | None
    net_carry_usd: str | None
    break_even_days: str | None
    capital_required_usd: str | None
    cost_basis: str
    gate: GateResult
    unknown_components: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.hold_days is not None:
            _check_int("hold_days", self.hold_days, lo=1, hi=365)
        require_decimal_str("actual_futures_notional_usd", self.actual_futures_notional_usd)
        for name in (
            "conservative_carry_usd",
            "roundtrip_cost_usd",
            "net_carry_usd",
            "break_even_days",
            "capital_required_usd",
        ):
            require_optional_decimal_str(name, getattr(self, name))
        if not isinstance(self.cost_basis, str) or not self.cost_basis:
            raise ValueError("cost_basis must be non-empty str")
        if not isinstance(self.gate, GateResult):
            raise ValueError("gate must be GateResult")
        object.__setattr__(
            self, "unknown_components", _coerce_str_tuple("unknown_components", self.unknown_components)
        )


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    futures_move: str
    spot_move: str
    fx_shock: str
    status: str
    net_pnl_usd: str | None = None
    loss_usd: str | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id:
            raise ValueError("scenario_id must be non-empty str")
        for name in ("futures_move", "spot_move", "fx_shock"):
            require_decimal_str(name, getattr(self, name))
        _check_enum("status", self.status, SCENARIO_STATUSES)
        require_optional_decimal_str("net_pnl_usd", self.net_pnl_usd)
        require_optional_decimal_str("loss_usd", self.loss_usd)
        object.__setattr__(self, "reasons", _coerce_sorted_reasons(self.reasons))


@dataclass(frozen=True)
class RatioProposal:
    target_ratio: str
    actual_ratio: str
    futures_contract_qty: str
    canonical_futures_qty: str
    spot_net_qty: str
    spot_venue: str | None
    quote_refs: Mapping[str, str] = field(default_factory=dict)
    economics: EconomicsResult | None = None
    scenarios: tuple[ScenarioResult, ...] = ()
    execution_gate: GateResult | None = None
    risk_gate: GateResult | None = None
    order_guidance: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "target_ratio",
            "actual_ratio",
            "futures_contract_qty",
            "canonical_futures_qty",
            "spot_net_qty",
        ):
            require_decimal_str(name, getattr(self, name))
        if self.spot_venue is not None and (
            not isinstance(self.spot_venue, str) or not self.spot_venue
        ):
            raise ValueError("spot_venue must be non-empty str or None")
        object.__setattr__(self, "quote_refs", _coerce_dict("quote_refs", self.quote_refs))
        if self.economics is not None and not isinstance(self.economics, EconomicsResult):
            raise ValueError("economics must be EconomicsResult or None")
        scenarios = tuple(self.scenarios) if isinstance(self.scenarios, (list, tuple)) else ()
        for item in scenarios:
            if not isinstance(item, ScenarioResult):
                raise ValueError("scenarios must hold ScenarioResult")
        object.__setattr__(self, "scenarios", scenarios)
        for name in ("execution_gate", "risk_gate"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, GateResult):
                raise ValueError(f"{name} must be GateResult or None")
        guidance = tuple(self.order_guidance) if isinstance(self.order_guidance, (list, tuple)) else ()
        object.__setattr__(self, "order_guidance", tuple(dict(g) if isinstance(g, Mapping) else g for g in guidance))


@dataclass(frozen=True)
class DecisionRequest:
    symbol: str
    goal: str
    futures_notional_usd: str
    planned_hold_days: int
    available_capital_usd: str
    max_scenario_loss_usd: str
    margin_usd: str
    liquidation_price: str
    liquidation_price_updated_at_ms: int
    preferred_spot_venue: str = "AUTO"

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("symbol must be non-empty str")
        _check_enum("goal", self.goal, GOALS)
        require_decimal_str("futures_notional_usd", self.futures_notional_usd)
        if Decimal(self.futures_notional_usd) <= 0:
            raise ValueError("futures_notional_usd must be > 0")
        _check_int("planned_hold_days", self.planned_hold_days, lo=1, hi=365)
        for name in ("available_capital_usd", "max_scenario_loss_usd", "margin_usd", "liquidation_price"):
            require_decimal_str(name, getattr(self, name))
            if Decimal(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be > 0")
        _check_int("liquidation_price_updated_at_ms", self.liquidation_price_updated_at_ms, lo=0)
        if not isinstance(self.preferred_spot_venue, str) or not self.preferred_spot_venue:
            raise ValueError("preferred_spot_venue must be non-empty str")


@dataclass(frozen=True)
class DecisionContext:
    identity_snapshot_id: str
    directional_score_id: str | None
    fcs_snapshot_id: str
    funding_context: FundingContext
    identity: AssetIdentity
    futures_mark: Any
    futures_quote: FuturesExecutionQuote | None
    futures_rules: TradingRulesSnapshot
    venue_quotes: tuple[SpotVenueQuote, ...] = ()
    directional: Mapping[str, Any] | None = None
    source_refs: Mapping[str, str] = field(default_factory=dict)
    as_of_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.identity_snapshot_id, str) or not self.identity_snapshot_id:
            raise ValueError("identity_snapshot_id must be non-empty str")
        if self.directional_score_id is not None and (
            not isinstance(self.directional_score_id, str) or not self.directional_score_id
        ):
            raise ValueError("directional_score_id must be non-empty str or None")
        if not isinstance(self.fcs_snapshot_id, str) or not self.fcs_snapshot_id:
            raise ValueError("fcs_snapshot_id must be non-empty str")
        if not isinstance(self.funding_context, FundingContext):
            raise ValueError("funding_context must be FundingContext")
        if not isinstance(self.identity, AssetIdentity):
            raise ValueError("identity must be AssetIdentity")
        object.__setattr__(
            self, "futures_mark", _require_observed("futures_mark", self.futures_mark)
        )
        if self.futures_quote is not None and not isinstance(self.futures_quote, FuturesExecutionQuote):
            raise ValueError("futures_quote must be FuturesExecutionQuote or None")
        if not isinstance(self.futures_rules, TradingRulesSnapshot):
            raise ValueError("futures_rules must be TradingRulesSnapshot")
        quotes = tuple(self.venue_quotes) if isinstance(self.venue_quotes, (list, tuple)) else ()
        for q in quotes:
            if not isinstance(q, SpotVenueQuote):
                raise ValueError("venue_quotes must hold SpotVenueQuote")
        object.__setattr__(self, "venue_quotes", quotes)
        if self.directional is not None:
            d = _coerce_dict("directional", self.directional)
            if d is not None:
                required = {
                    "profile", "ltss", "entry", "data_quality", "tradeability",
                    "candidate_status", "execution_status", "vetoes", "pauses",
                    "score_as_of_ms", "config_hash",
                }
                missing = required - set(d)
                if missing:
                    raise ValueError(f"directional is missing keys: {sorted(missing)}")
            object.__setattr__(self, "directional", d)
        object.__setattr__(self, "source_refs", _coerce_dict("source_refs", self.source_refs))
        _check_int("as_of_ms", self.as_of_ms, lo=0)


@dataclass(frozen=True)
class DecisionResult:
    decision_id: str
    generated_at_ms: int
    expires_at_ms: int
    request: DecisionRequest
    context_refs: Mapping[str, str]
    recommendation: str
    selected_proposal: RatioProposal | None
    alternatives: tuple[RatioProposal, ...] = ()
    reasons: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    validation_level: str = "RULE_BASED_UNVALIDATED"
    decision_policy_hash: str = ""
    formula_version: str = "hedge-decision-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, str) or not self.decision_id:
            raise ValueError("decision_id must be non-empty str")
        _check_int("generated_at_ms", self.generated_at_ms, lo=0)
        _check_int("expires_at_ms", self.expires_at_ms, lo=0)
        if self.expires_at_ms <= self.generated_at_ms:
            raise ValueError("expires_at_ms must be > generated_at_ms")
        if not isinstance(self.request, DecisionRequest):
            raise ValueError("request must be DecisionRequest")
        object.__setattr__(self, "context_refs", _coerce_dict("context_refs", self.context_refs))
        _check_enum("recommendation", self.recommendation, RECOMMENDATIONS)
        if self.selected_proposal is not None and not isinstance(self.selected_proposal, RatioProposal):
            raise ValueError("selected_proposal must be RatioProposal or None")
        alts = tuple(self.alternatives) if isinstance(self.alternatives, (list, tuple)) else ()
        for item in alts:
            if not isinstance(item, RatioProposal):
                raise ValueError("alternatives must hold RatioProposal")
        object.__setattr__(self, "alternatives", alts)
        object.__setattr__(self, "reasons", _coerce_sorted_reasons(self.reasons))
        object.__setattr__(
            self, "assumptions", _coerce_str_tuple("assumptions", self.assumptions)
        )
        _check_enum("validation_level", self.validation_level, VALIDATION_LEVELS)
        if not isinstance(self.decision_policy_hash, str):
            raise ValueError("decision_policy_hash must be str")
        if not isinstance(self.formula_version, str) or not self.formula_version:
            raise ValueError("formula_version must be non-empty str")


@dataclass(frozen=True)
class OpportunityQuery:
    symbol: str | None = None
    venue: str | None = None
    readiness: str | None = None
    min_fcs: float | None = None
    min_funding_30d: str | None = None
    min_positive_ratio_30d: str | None = None
    sort: str | None = None
    order: str = "desc"
    limit: int = 50
    offset: int = 0
    include_stale: bool = False

    def __post_init__(self) -> None:
        for name in ("symbol", "venue", "readiness", "sort"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be non-empty str or None")
        if self.readiness is not None:
            _check_enum("readiness", self.readiness, READINESS_VALUES)
        if self.min_fcs is not None:
            if isinstance(self.min_fcs, bool) or not isinstance(self.min_fcs, (int, float)):
                raise ValueError("min_fcs must be a number or None")
        require_optional_decimal_str("min_funding_30d", self.min_funding_30d)
        require_optional_decimal_str("min_positive_ratio_30d", self.min_positive_ratio_30d)
        if self.sort is not None and self.sort not in {
            "fcs", "funding30d", "breakEvenDays", "positiveRatio30d"
        }:
            raise ValueError(f"sort={self.sort!r} unknown")
        if self.order not in ("asc", "desc"):
            raise ValueError("order must be asc or desc")
        _check_int("limit", self.limit, lo=1, hi=200)
        _check_int("offset", self.offset, lo=0)
        if not isinstance(self.include_stale, bool):
            raise ValueError("include_stale must be bool")


@dataclass(frozen=True)
class OpportunityPage:
    items: tuple[Mapping[str, Any], ...] = ()
    total: int = 0
    as_of_ms: int | None = None

    def __post_init__(self) -> None:
        items = tuple(self.items) if isinstance(self.items, (list, tuple)) else ()
        object.__setattr__(self, "items", tuple(dict(i) if isinstance(i, Mapping) else i for i in items))
        _check_int("total", self.total, lo=0)
        if self.as_of_ms is not None:
            _check_int("as_of_ms", self.as_of_ms, lo=0)


@dataclass(frozen=True)
class LedgerPnl:
    realized_futures_usd: str | None
    realized_spot_usd: str | None
    unrealized_futures_usd: str | None
    unrealized_spot_usd: str | None
    actual_funding_usd: str | None
    estimated_unconfirmed_funding_usd: str | None
    known_cost_usd: str | None
    estimated_exit_cost_usd: str | None
    known_net_subtotal_usd: str | None
    net_before_exit_usd: str | None
    net_after_exit_usd: str | None
    unknown_components: tuple[str, ...] = ()
    coverage: Mapping[str, Any] = field(default_factory=dict)
    funding_basis: str = "ESTIMATED"
    as_of_ms: int = 0

    def __post_init__(self) -> None:
        for name in (
            "realized_futures_usd", "realized_spot_usd", "unrealized_futures_usd",
            "unrealized_spot_usd", "actual_funding_usd",
            "estimated_unconfirmed_funding_usd", "known_cost_usd",
            "estimated_exit_cost_usd", "known_net_subtotal_usd",
            "net_before_exit_usd", "net_after_exit_usd",
        ):
            require_optional_decimal_str(name, getattr(self, name))
        object.__setattr__(
            self, "unknown_components", _coerce_str_tuple("unknown_components", self.unknown_components)
        )
        cov = _coerce_dict("coverage", self.coverage)
        object.__setattr__(self, "coverage", cov)
        _check_enum("funding_basis", self.funding_basis, FUNDING_BASES)
        _check_int("as_of_ms", self.as_of_ms, lo=0)


@dataclass(frozen=True)
class PairExitGuidance:
    plan_id: str
    plan_version: int
    generated_at_ms: int
    expires_at_ms: int
    legs: tuple[Mapping[str, Any], ...] = ()
    unexecutable_dust: Mapping[str, str] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    confirmation_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id:
            raise ValueError("plan_id must be non-empty str")
        _check_int("plan_version", self.plan_version, lo=1)
        _check_int("generated_at_ms", self.generated_at_ms, lo=0)
        _check_int("expires_at_ms", self.expires_at_ms, lo=0)
        legs = tuple(self.legs) if isinstance(self.legs, (list, tuple)) else ()
        object.__setattr__(self, "legs", tuple(dict(l) if isinstance(l, Mapping) else l for l in legs))
        dust = _coerce_dict("unexecutable_dust", self.unexecutable_dust)
        for k, v in dust.items():
            require_decimal_str(f"unexecutable_dust[{k}]", v)
        object.__setattr__(self, "unexecutable_dust", dust)
        object.__setattr__(self, "reasons", _coerce_sorted_reasons(self.reasons))
        if not isinstance(self.confirmation_required, bool):
            raise ValueError("confirmation_required must be bool")


@dataclass(frozen=True)
class CaptureContext:
    cohort: str
    source_snapshot_id: str
    symbol: str
    identity: AssetIdentity
    identity_snapshot_id: str
    funding_context: FundingContext
    futures_contract_qty: str
    canonical_futures_qty: str
    strategies: tuple[str, ...] = ()
    decision: DecisionResult | None = None
    decision_as_of_ms: int = 0
    policy: Mapping[str, Any] = field(default_factory=dict)
    rule_refs: Mapping[str, str] = field(default_factory=dict)
    source_refs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_enum("cohort", self.cohort, COHORTS)
        for name in ("source_snapshot_id", "symbol", "identity_snapshot_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty str")
        if not isinstance(self.identity, AssetIdentity):
            raise ValueError("identity must be AssetIdentity")
        if not isinstance(self.funding_context, FundingContext):
            raise ValueError("funding_context must be FundingContext")
        require_decimal_str("futures_contract_qty", self.futures_contract_qty)
        require_decimal_str("canonical_futures_qty", self.canonical_futures_qty)
        strategies = _coerce_str_tuple("strategies", self.strategies)
        for s in strategies:
            _check_enum(f"strategies[]={s}", s, STRATEGIES)
        object.__setattr__(self, "strategies", strategies)
        if self.decision is not None and not isinstance(self.decision, DecisionResult):
            raise ValueError("decision must be DecisionResult or None")
        # SYSTEM_POLICY only with USER_DECISION + selected proposal (D19.6).
        if "SYSTEM_POLICY" in strategies:
            if self.cohort != "USER_DECISION":
                raise ValueError("SYSTEM_POLICY only allows USER_DECISION cohort")
            if self.decision is None or self.decision.selected_proposal is None:
                raise ValueError("SYSTEM_POLICY requires decision.selected_proposal")
        _check_int("decision_as_of_ms", self.decision_as_of_ms, lo=0)
        object.__setattr__(self, "policy", _coerce_dict("policy", self.policy))
        object.__setattr__(self, "rule_refs", _coerce_dict("rule_refs", self.rule_refs))
        object.__setattr__(self, "source_refs", _coerce_dict("source_refs", self.source_refs))


@dataclass(frozen=True)
class CaptureResult:
    entry_ids: tuple[str, ...]
    status: str
    executed_as_of_ms: int | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_ids", _coerce_str_tuple("entry_ids", self.entry_ids))
        if self.status not in ("COMPLETE", "PARTIAL", "UNAVAILABLE"):
            raise ValueError(f"status={self.status!r} unknown")
        if self.executed_as_of_ms is not None:
            _check_int("executed_as_of_ms", self.executed_as_of_ms, lo=0)
        object.__setattr__(self, "reasons", _coerce_sorted_reasons(self.reasons))


@dataclass(frozen=True)
class QuoteCollectionResult:
    claimed: int
    complete: int
    deferred: int
    unavailable: int
    task_ids: tuple[str, ...] = ()
    as_of_ms: int = 0
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("claimed", "complete", "deferred", "unavailable"):
            _check_int(name, getattr(self, name), lo=0)
        if self.claimed != self.complete + self.deferred + self.unavailable:
            raise ValueError("claimed must equal complete+deferred+unavailable")
        object.__setattr__(self, "task_ids", _coerce_str_tuple("task_ids", self.task_ids))
        _check_int("as_of_ms", self.as_of_ms, lo=0)
        object.__setattr__(self, "reasons", _coerce_sorted_reasons(self.reasons))
