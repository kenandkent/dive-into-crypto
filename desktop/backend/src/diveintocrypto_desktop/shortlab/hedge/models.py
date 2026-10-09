"""Frozen Hedge DTOs (H01, design B9/B12/B15/B16/B20/B25/B26/B28/B39).

Every H02-H10 output type is frozen here; consumers must import from this
module instead of redefining them.

Conventions (plan H01 + B28.0/B28.7):

- internal field names are ``snake_case``; :func:`to_api_dict` returns
  ``camelCase`` aliases for the HTTP layer (``from_api_dict`` accepts
  either spelling on the way in);
- every quantity/price/notional/fee/remaining/dust field is a native
  decimal string (``Decimal(value)`` must parse, no NaN/Infinity);
  floats never carry ledger balances -- ``sl_hedge_plan``/``sl_hedge_leg``
  DOUBLE columns are display projections only;
- all records are ``@dataclass(frozen=True)``; mutating raises
  ``dataclasses.FrozenInstanceError`` (the pre-H01 ``Record`` freeze rule);
- ``event_json`` uses ``schema_version="hedge-event-v1"``,
  ``source_meta`` uses ``schema_version="hedge-source-v1"``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol, runtime_checkable

from diveintocrypto_desktop.shortlab.hedge import (
    COST_FORMULA_VERSION,
    COST_FORMULA_VERSION_V2,
    FCS_VERSION,
    FCS_VERSION_V2,
    FCS_VERSIONS_LEGACY,
    HEDGE_EVIDENCE_VERSION,
    HEDGE_EVIDENCE_VERSION_V3,
    HEDGE_EVIDENCE_VERSIONS_ALL,
    HEDGE_FORMULA_VERSION,
    HEDGE_FORMULA_VERSION_V2,
    HEDGE_FORMULA_VERSIONS_ALL,
    PLAN_SAFETY_RULES_VERSION,
    PLAN_SAFETY_RULES_VERSION_V2,
    VENUE_SELECTION_VERSION,
    VENUE_SELECTION_VERSION_V2,
)

__all__ = [
    "FCS_VERSION",
    "HEDGE_FORMULA_VERSION",
    "HEDGE_EVIDENCE_VERSION",
    "COST_FORMULA_VERSION",
    "PLAN_SAFETY_RULES_VERSION",
    "VENUE_SELECTION_VERSION",
    "FCS_VERSION_V2",
    "HEDGE_FORMULA_VERSION_V2",
    "HEDGE_EVIDENCE_VERSION_V3",
    "COST_FORMULA_VERSION_V2",
    "PLAN_SAFETY_RULES_VERSION_V2",
    "VENUE_SELECTION_VERSION_V2",
    "FCS_VERSIONS_LEGACY",
    "HEDGE_FORMULA_VERSIONS_ALL",
    "HEDGE_EVIDENCE_VERSIONS_ALL",
    "ALLOWED_GOALS",
    "ALLOWED_PRICE_BASIS",
    "HEDGE_EVENT_SCHEMA_VERSION",
    "HEDGE_SOURCE_SCHEMA_VERSION",
    "ALLOWED_VENUES",
    "ALLOWED_EXIT_FEASIBILITY",
    "ALLOWED_IDENTITY_CONFIDENCE",
    "ALLOWED_MODES",
    "ALLOWED_PLAN_STATUS",
    "ALLOWED_EVENT_TYPES",
    "ALLOWED_LEG_TYPES",
    "ALLOWED_ALERT_SEVERITY",
    "ALLOWED_ALERT_STATE",
    "ALLOWED_READINESS",
    "ALLOWED_RISK_VALIDATION",
    "ALLOWED_OUTCOME_STATUS",
    "ALLOWED_STRATEGIES",
    "ALLOWED_HORIZON_DAYS",
    "ALLOWED_SNAPSHOT_REF_TYPES",
    "SpotVenueQuote",
    "TradingRulesSnapshot",
    "OnchainQuote",
    "HedgeSimulationRequest",
    "HedgeSimulationResult",
    "HedgeEvent",
    "HedgePosition",
    "HedgeAlert",
    "HedgeMonitor",
    "FundingMetrics",
    "FCSResult",
    "DQResult",
    "OrderGuidance",
    "Readiness",
    "LedgerResult",
    "PlanState",
    "HedgeOutcome",
    "HedgeEvidenceSummary",
    "HistoricalPriceBar",
    "HistoricalFundingEvent",
    "HistoricalLifecycle",
    "HistoricalMarketProvider",
    "to_api_dict",
    "from_api_dict",
    "snake_to_camel",
    "camel_to_snake",
    "require_decimal_str",
    "require_optional_decimal_str",
]

HEDGE_EVENT_SCHEMA_VERSION = "hedge-event-v1"
HEDGE_SOURCE_SCHEMA_VERSION = "hedge-source-v1"

ALLOWED_VENUES = frozenset({"BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"})
ALLOWED_EXIT_FEASIBILITY = frozenset({"CONFIRMED", "PARTIAL", "UNKNOWN", "NO"})
ALLOWED_IDENTITY_CONFIDENCE = frozenset({"VERIFIED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"})
ALLOWED_MODES = frozenset({"ABSOLUTE", "RELATIVE"})
ALLOWED_GOALS = frozenset({"CARRY_CAPTURE", "DIRECTIONAL_SHORT", "BALANCED"})
ALLOWED_PRICE_BASIS = frozenset({"TRADE", "MARK"})
ALLOWED_PLAN_STATUS = frozenset(
    {
        "DRAFT",
        "READY",
        "PARTIALLY_FILLED",
        "FUNDED_PENDING_ACTIVATION",
        "ACTIVE",
        "CLOSING",
        "CLOSED",
        "INVALID",
    }
)
ALLOWED_EVENT_TYPES = frozenset(
    {
        "OPEN_FUTURES_SHORT",
        "OPEN_SPOT_LONG",
        "CLOSE_FUTURES_SHORT",
        "CLOSE_SPOT_LONG",
        "CORRECT_REVERSAL",
        "CORRECT_SUPERSEDE",
        "FUNDING_RECEIPT",
        "LIQUIDATION",
    }
)
ALLOWED_LEG_TYPES = frozenset({"FUTURES_SHORT", "SPOT_LONG", "FUNDING"})
ALLOWED_ALERT_SEVERITY = frozenset({"INFO", "WARN", "CRITICAL"})
ALLOWED_ALERT_STATE = frozenset({"OPEN", "ACKNOWLEDGED", "RESOLVED"})
ALLOWED_READINESS = frozenset({"READY", "NOT_READY", "BLOCKED"})
ALLOWED_RISK_VALIDATION = frozenset({"VERIFIED", "LIMITED", "UNKNOWN"})
ALLOWED_OUTCOME_STATUS = frozenset({"PENDING", "COMPLETE", "CENSORED", "UNAVAILABLE"})
# R00 (D14.1): extended strategy enum, old four stay valid for legacy decode.
ALLOWED_STRATEGIES = frozenset(
    {
        "UNHEDGED_0",
        "ABSOLUTE_100",
        "RELATIVE_75",
        "RELATIVE_50",
        "RELATIVE_25",
        "SYSTEM_POLICY",
    }
)
ALLOWED_STRATEGIES_LEGACY = frozenset(
    {"ABSOLUTE_100", "RELATIVE_75", "RELATIVE_50", "RELATIVE_25"}
)
ALLOWED_HORIZON_DAYS = frozenset({7, 30, 90})
ALLOWED_SNAPSHOT_REF_TYPES = frozenset(
    {
        "FCS",
        "SIMULATION",
        "PLAN",
        "OUTCOME",
        "VENUE_QUOTE",
        "IDENTITY",
        "CONFIG",
        "VENUE_MAPPING",
        "CONTRACT_RULES",
        "FUNDING_OBSERVATION",
        "DECISION",
        "STRATEGY_ENTRY",
        "MARKET_OBSERVATION",
        "FX",
        "PROTECTION",
    }
)


def require_decimal_str(name: str, value: Any) -> str:
    """Validate a native decimal string (B28.0 ledger boundary)."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty decimal string, got {value!r}")
    text = value.strip()
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError(f"{name} is not a decimal string: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")
    return text


def require_optional_decimal_str(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return require_decimal_str(name, value)


def _coerce_str_tuple(name: str, value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    items = tuple(value) if isinstance(value, (list, tuple)) else (value,)
    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"{name} must hold str, got {item!r}")
    return items


def _coerce_dict(name: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping, got {value!r}")
    return dict(value)


def snake_to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def camel_to_snake(name: str) -> str:
    out: list[str] = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out).lstrip("_")


def to_api_dict(record: Any) -> dict[str, Any]:
    """Return the ``camelCase`` API view of a frozen hedge DTO."""
    if dataclasses.is_dataclass(record) and not isinstance(record, type):
        data = dataclasses.asdict(record)
    elif isinstance(record, Mapping):
        data = dict(record)
    else:
        raise ValueError("to_api_dict requires a hedge DTO or mapping")
    return {snake_to_camel(k): v for k, v in data.items()}


def from_api_dict(cls: Any, payload: Mapping[str, Any]) -> Any:
    """Build a hedge DTO from ``snake_case`` or ``camelCase`` keys."""
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    allowed = {f.name for f in dataclasses.fields(cls)}
    normalised: dict[str, Any] = {}
    for key, value in payload.items():
        snake = camel_to_snake(key) if key not in allowed else key
        if snake not in allowed:
            raise ValueError(f"{cls.__name__} has unknown field {key!r}")
        if snake in normalised:
            raise ValueError(f"{cls.__name__} got duplicate field {key!r}")
        normalised[snake] = value
    return cls(**normalised)


def _check_enum(name: str, value: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"{name}={value!r} must be one of {sorted(allowed)}")
    return value


# ---------------------------------------------------------------------------
# B9.2 SpotVenueQuote
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpotVenueQuote:
    """Venue quote for one canonical token (B9.2, venue instrument ID in
    ``symbol``; mid/VWAP are per-canonical USD reference prices).

    Quantity fields (``buy_executable_qty``/``sell_executable_qty``/
    ``requested_canonical_qty``) and every price/notional/fee field are
    native decimal strings; slippage stays a float bps number.
    """

    venue: str
    canonical_id: str
    symbol: str | None
    chain: str | None
    contract_address: str | None
    as_of_ms: int
    expires_at_ms: int | None
    reference_notional_usd: str
    mid_price: str | None
    buy_vwap: str | None
    sell_vwap: str | None
    buy_executable_qty: str | None
    sell_executable_qty: str | None
    buy_slippage_bps: float | None
    sell_slippage_bps: float | None
    estimated_fee_usd: str | None
    estimated_gas_usd: str | None
    direction_costs: Mapping[str, Any] = field(default_factory=dict)
    entry_feasible: bool = False
    exit_feasible: bool = False
    exit_feasibility: str = "UNKNOWN"
    quote_currency: str = "USDT"
    quote_to_usd: str | None = None
    source_timestamp_ms: int | None = None
    fetched_at_ms: int = 0
    requested_canonical_qty: str = "0"
    trading_rules: Mapping[str, Any] = field(default_factory=dict)
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    identity_confidence: str = "UNRESOLVED"
    status: str = "UNAVAILABLE"
    reason_code: str | None = None
    # R00 (D06.2): explicit fee-inclusion flag; old JSON缺字段解析为None(LEGACY).
    fees_included: bool | None = None

    def __post_init__(self) -> None:
        _check_enum("venue", self.venue, ALLOWED_VENUES)
        _check_enum("exit_feasibility", self.exit_feasibility, ALLOWED_EXIT_FEASIBILITY)
        _check_enum("identity_confidence", self.identity_confidence, ALLOWED_IDENTITY_CONFIDENCE)
        if self.fees_included is not None and not isinstance(self.fees_included, bool):
            raise ValueError("fees_included must be bool or None")
        object.__setattr__(self, "reference_notional_usd",
                            require_decimal_str("reference_notional_usd", self.reference_notional_usd))
        object.__setattr__(self, "mid_price",
                            require_optional_decimal_str("mid_price", self.mid_price))
        object.__setattr__(self, "buy_vwap",
                            require_optional_decimal_str("buy_vwap", self.buy_vwap))
        object.__setattr__(self, "sell_vwap",
                            require_optional_decimal_str("sell_vwap", self.sell_vwap))
        object.__setattr__(self, "buy_executable_qty",
                            require_optional_decimal_str("buy_executable_qty", self.buy_executable_qty))
        object.__setattr__(self, "sell_executable_qty",
                            require_optional_decimal_str("sell_executable_qty", self.sell_executable_qty))
        object.__setattr__(self, "estimated_fee_usd",
                            require_optional_decimal_str("estimated_fee_usd", self.estimated_fee_usd))
        object.__setattr__(self, "estimated_gas_usd",
                            require_optional_decimal_str("estimated_gas_usd", self.estimated_gas_usd))
        object.__setattr__(self, "quote_to_usd",
                            require_optional_decimal_str("quote_to_usd", self.quote_to_usd))
        object.__setattr__(self, "requested_canonical_qty",
                            require_decimal_str("requested_canonical_qty", self.requested_canonical_qty))
        if not isinstance(self.as_of_ms, int) or isinstance(self.as_of_ms, bool):
            raise ValueError("as_of_ms must be an int")
        if self.expires_at_ms is not None and (
            not isinstance(self.expires_at_ms, int) or isinstance(self.expires_at_ms, bool)
        ):
            raise ValueError("expires_at_ms must be an int or None")
        object.__setattr__(self, "direction_costs", _coerce_dict("direction_costs", self.direction_costs))
        object.__setattr__(self, "trading_rules", _coerce_dict("trading_rules", self.trading_rules))
        object.__setattr__(self, "capabilities", _coerce_dict("capabilities", self.capabilities))


# ---------------------------------------------------------------------------
# B16.1 TradingRulesSnapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradingRulesSnapshot:
    """Unified trading-rules snapshot (B16.1).

    ``tick_size``/``step_size``/``min_qty``/``max_qty``/``min_notional``/
    ``max_notional`` and market-order flags are decimal strings; a ``0``
    keeps its venue-native "disabled" meaning and is never treated as a
    legal step.
    """

    venue: str
    instrument_id: str
    source_as_of_ms: int | None
    known_at_ms: int
    rule_version: str
    raw_filters: Mapping[str, Any] = field(default_factory=dict)
    order_types: Mapping[str, Any] = field(default_factory=dict)
    price_rules: Mapping[str, Any] = field(default_factory=dict)
    lot_rules: Mapping[str, Any] = field(default_factory=dict)
    notional_rules: Mapping[str, Any] = field(default_factory=dict)
    # R00 (D15): explicit stop-order capability; old JSON缺字段为None(LEGACY).
    stop_orders_supported: bool | None = None
    conditional_orders_source_ref: str | None = None

    def __post_init__(self) -> None:
        _check_enum("venue", self.venue, ALLOWED_VENUES)
        if self.stop_orders_supported is not None and not isinstance(
            self.stop_orders_supported, bool
        ):
            raise ValueError("stop_orders_supported must be bool or None")
        if self.conditional_orders_source_ref is not None and (
            not isinstance(self.conditional_orders_source_ref, str)
            or not self.conditional_orders_source_ref
        ):
            raise ValueError("conditional_orders_source_ref must be non-empty str or None")
        if not self.instrument_id or not isinstance(self.instrument_id, str):
            raise ValueError("instrument_id must be a non-empty str")
        if not self.rule_version or not isinstance(self.rule_version, str):
            raise ValueError("rule_version must be a non-empty str")
        # Decimal-string rule values are validated when present.
        for group_name in ("price_rules", "lot_rules", "notional_rules"):
            group = getattr(self, group_name)
            if not isinstance(group, Mapping):
                raise ValueError(f"{group_name} must be a mapping")
            for key in ("tick_size", "step_size", "min_qty", "max_qty",
                        "min_price", "max_price", "min_notional", "max_notional"):
                if key in group and group[key] is not None:
                    require_decimal_str(f"{group_name}.{key}", group[key])
            object.__setattr__(self, group_name, dict(group))
        object.__setattr__(self, "raw_filters", _coerce_dict("raw_filters", self.raw_filters))
        object.__setattr__(self, "order_types", _coerce_dict("order_types", self.order_types))


# ---------------------------------------------------------------------------
# B12 OnchainQuote
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OnchainQuote:
    """Read-only on-chain indicative quote (B12.2).

    Carries the B9 public fields plus ``provider_id``/``api_version``/
    ``chain_id``/``block_number``/``atomic_amounts``/``decimals``/
    ``quote_kind="INDICATIVE"`` and gas/tax/route flags. All raw token
    amounts are integer atomic-unit strings -- never ``10**decimals`` via
    float. ``simulation_verified`` is always ``False`` in V1.
    """

    venue: str
    canonical_id: str
    chain: str | None
    contract_address: str | None
    as_of_ms: int
    expires_at_ms: int | None
    requested_canonical_qty: str
    provider_id: str
    api_version: str
    chain_id: int
    block_number: int | None
    atomic_amounts: Mapping[str, Any] = field(default_factory=dict)
    decimals: int = 18
    quote_kind: str = "INDICATIVE"
    buy_vwap: str | None = None
    sell_vwap: str | None = None
    buy_executable_qty: str | None = None
    sell_executable_qty: str | None = None
    gas_units: str | None = None
    gas_price: str | None = None
    native_gas_fx: str | None = None
    estimated_gas_usd: str | None = None
    estimated_fee_usd: str | None = None
    token_tax_status: str = "UNKNOWN"
    route_complete: bool = False
    simulation_verified: bool = False
    quote_currency: str = "USDC"
    quote_to_usd: str | None = None
    fetched_at_ms: int = 0
    status: str = "UNAVAILABLE"
    reason_code: str | None = None
    # R00 (D06.2): explicit fee-inclusion flag; old JSON缺字段为None(LEGACY).
    fees_included: bool | None = None

    def __post_init__(self) -> None:
        if self.fees_included is not None and not isinstance(self.fees_included, bool):
            raise ValueError("fees_included must be bool or None")
        if self.venue != "ONCHAIN_DEX":
            raise ValueError("OnchainQuote.venue must be ONCHAIN_DEX")
        if self.quote_kind != "INDICATIVE":
            raise ValueError('OnchainQuote.quote_kind must be "INDICATIVE"')
        if self.simulation_verified is not False:
            raise ValueError("OnchainQuote.simulation_verified must be False in V1")
        object.__setattr__(self, "requested_canonical_qty",
                            require_decimal_str("requested_canonical_qty", self.requested_canonical_qty))
        object.__setattr__(self, "buy_vwap",
                            require_optional_decimal_str("buy_vwap", self.buy_vwap))
        object.__setattr__(self, "sell_vwap",
                            require_optional_decimal_str("sell_vwap", self.sell_vwap))
        object.__setattr__(self, "buy_executable_qty",
                            require_optional_decimal_str("buy_executable_qty", self.buy_executable_qty))
        object.__setattr__(self, "sell_executable_qty",
                            require_optional_decimal_str("sell_executable_qty", self.sell_executable_qty))
        object.__setattr__(self, "estimated_fee_usd",
                            require_optional_decimal_str("estimated_fee_usd", self.estimated_fee_usd))
        object.__setattr__(self, "estimated_gas_usd",
                            require_optional_decimal_str("estimated_gas_usd", self.estimated_gas_usd))
        object.__setattr__(self, "native_gas_fx",
                            require_optional_decimal_str("native_gas_fx", self.native_gas_fx))
        object.__setattr__(self, "quote_to_usd",
                            require_optional_decimal_str("quote_to_usd", self.quote_to_usd))
        # Atomic amounts must be integer strings (no float 10**decimals).
        amounts = dict(self.atomic_amounts) if isinstance(self.atomic_amounts, Mapping) else None
        if amounts is None:
            raise ValueError("atomic_amounts must be a mapping")
        for key, value in amounts.items():
            if not isinstance(value, str) or not value.strip().lstrip("-").isdigit():
                raise ValueError(f"atomic_amounts[{key!r}] must be an integer string, got {value!r}")
        object.__setattr__(self, "atomic_amounts", amounts)
        if isinstance(self.decimals, bool) or not isinstance(self.decimals, int) or self.decimals < 0:
            raise ValueError("decimals must be a non-negative int")
        if self.gas_units is not None:
            if not isinstance(self.gas_units, str) or not self.gas_units.strip().isdigit():
                raise ValueError("gas_units must be an integer string or None")
        if self.gas_price is not None:
            require_decimal_str("gas_price", self.gas_price)


# ---------------------------------------------------------------------------
# B15 HedgeSimulationRequest / Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HedgeSimulationRequest:
    """Planner input (B15.1). Notional/margin/price/qty are decimal strings;
    ``planned_hold_days`` is 1..365 or None. ABSOLUTE must not carry
    relative ratio/budget fields; RELATIVE requires exactly one input style.
    """

    symbol: str
    mode: str
    futures_notional_usd: str
    hedge_ratio: str | None = None
    stress_up_pct: str | None = None
    max_directional_loss_usd: str | None = None
    preferred_spot_venue: str = "AUTO"
    futures_leverage: str = "1"
    margin_mode: str | None = None
    margin_usd: str | None = None
    liquidation_price: str | None = None
    liquidation_price_source: str | None = None
    liquidation_price_updated_at_ms: int | None = None
    stop_policy: str | None = None
    stop_trigger_price: str | None = None
    stop_trigger_basis: str | None = None
    maximum_pair_loss_usd: str | None = None
    planned_hold_days: int | None = None
    fee_overrides: Mapping[str, Any] | None = None
    # R00 (D15/D03.3): optional goal + decision link; old JSON缺字段为None(LEGACY).
    goal: str | None = None
    decision_id: str | None = None

    def __post_init__(self) -> None:
        _check_enum("mode", self.mode, ALLOWED_MODES)
        if self.goal is not None:
            _check_enum("goal", self.goal, ALLOWED_GOALS)
            if self.mode == "ABSOLUTE" and self.goal != "CARRY_CAPTURE":
                raise ValueError("ABSOLUTE mode only supports goal=CARRY_CAPTURE")
        if self.decision_id is not None and (
            not isinstance(self.decision_id, str) or not self.decision_id
        ):
            raise ValueError("decision_id must be a non-empty str or None")
        object.__setattr__(self, "futures_notional_usd",
                            require_decimal_str("futures_notional_usd", self.futures_notional_usd))
        if Decimal(self.futures_notional_usd) <= 0:
            raise ValueError("futures_notional_usd must be > 0")
        object.__setattr__(self, "futures_leverage",
                            require_decimal_str("futures_leverage", self.futures_leverage))
        if Decimal(self.futures_leverage) < 1:
            raise ValueError("futures_leverage must be >= 1")
        for name in ("hedge_ratio", "stress_up_pct", "max_directional_loss_usd",
                     "margin_usd", "liquidation_price", "stop_trigger_price",
                     "maximum_pair_loss_usd"):
            value = getattr(self, name)
            object.__setattr__(self, name, require_optional_decimal_str(name, value))
        if self.margin_usd is not None and Decimal(self.margin_usd) <= 0:
            raise ValueError("margin_usd must be > 0")
        if self.planned_hold_days is not None:
            if (isinstance(self.planned_hold_days, bool)
                    or not isinstance(self.planned_hold_days, int)
                    or not 1 <= self.planned_hold_days <= 365):
                raise ValueError("planned_hold_days must be an int in 1..365 or None")
        if self.mode == "ABSOLUTE" and (
            self.hedge_ratio is not None or self.stress_up_pct is not None
            or self.max_directional_loss_usd is not None
        ):
            raise ValueError("ABSOLUTE must not carry relative ratio/budget fields")
        if self.mode == "RELATIVE" and self.hedge_ratio is None and (
            self.stress_up_pct is None or self.max_directional_loss_usd is None
        ):
            raise ValueError("RELATIVE requires hedge_ratio or stress_up_pct+max_directional_loss_usd")
        if self.fee_overrides is not None:
            object.__setattr__(self, "fee_overrides", _coerce_dict("fee_overrides", self.fee_overrides))


@dataclass(frozen=True)
class HedgeSimulationResult:
    """Planner output (B15.2). Quantity/price/notional fields are decimal
    strings; nested metric blocks stay as plain dicts (typed
    ``FundingMetrics``/``FCSResult``/``DQResult`` siblings describe them).
    """

    simulation_id: str
    generated_at_ms: int
    expires_at_ms: int
    symbol: str
    canonical_id: str
    mode: str
    futures_symbol: str
    futures_price: str
    canonical_futures_price_usd: str
    futures_quote_currency: str
    quote_to_usd: str | None
    futures_notional_usd: str
    futures_contract_qty: str
    canonical_futures_qty: str
    target_hedge_ratio: str
    spot_venue: str
    spot_symbol: str | None = None
    spot_chain: str | None = None
    spot_contract: str | None = None
    spot_price: str | None = None
    target_spot_qty: str | None = None
    spot_notional_usd: str | None = None
    residual_short_ratio: str | None = None
    residual_short_qty: str | None = None
    residual_short_notional_usd: str | None = None
    fcs: float | None = None
    plan_safety_score: float | None = None
    funding_metrics: Mapping[str, Any] = field(default_factory=dict)
    basis_metrics: Mapping[str, Any] = field(default_factory=dict)
    cost_metrics: Mapping[str, Any] = field(default_factory=dict)
    break_even: Mapping[str, Any] = field(default_factory=dict)
    stress_scenarios: tuple[Mapping[str, Any], ...] = ()
    order_guidance: tuple[Mapping[str, Any], ...] = ()
    monitoring_capability: str = "UNKNOWN"
    risk_validation: str = "UNKNOWN"
    liquidation_check_status: str = "UNKNOWN"
    risks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    readiness: str = "NOT_READY"
    # R00 (D15): nested readiness + economics; old JSON缺字段为None/{} (LEGACY).
    readiness_breakdown: Mapping[str, Any] | None = None
    economics: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _check_enum("mode", self.mode, ALLOWED_MODES)
        _check_enum("readiness", self.readiness, ALLOWED_READINESS)
        _check_enum("risk_validation", self.risk_validation, ALLOWED_RISK_VALIDATION)
        if self.readiness_breakdown is not None:
            object.__setattr__(
                self,
                "readiness_breakdown",
                _coerce_dict("readiness_breakdown", self.readiness_breakdown),
            )
        if self.economics is not None:
            object.__setattr__(self, "economics", _coerce_dict("economics", self.economics))
        for name in ("futures_price", "canonical_futures_price_usd", "futures_notional_usd",
                     "futures_contract_qty", "canonical_futures_qty", "target_hedge_ratio"):
            require_decimal_str(name, getattr(self, name))
        for name in ("spot_price", "target_spot_qty", "spot_notional_usd",
                     "residual_short_ratio", "residual_short_qty",
                     "residual_short_notional_usd", "quote_to_usd"):
            require_optional_decimal_str(name, getattr(self, name))
        if self.expires_at_ms <= self.generated_at_ms:
            raise ValueError("expires_at_ms must be > generated_at_ms")
        object.__setattr__(self, "funding_metrics", _coerce_dict("funding_metrics", self.funding_metrics))
        object.__setattr__(self, "basis_metrics", _coerce_dict("basis_metrics", self.basis_metrics))
        object.__setattr__(self, "cost_metrics", _coerce_dict("cost_metrics", self.cost_metrics))
        object.__setattr__(self, "break_even", _coerce_dict("break_even", self.break_even))
        object.__setattr__(self, "risks", _coerce_str_tuple("risks", self.risks))
        object.__setattr__(self, "warnings", _coerce_str_tuple("warnings", self.warnings))


# ---------------------------------------------------------------------------
# B28.7 HedgeEvent / Position / LedgerResult / PlanState
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HedgeEvent:
    """Immutable fill/correction/funding event (B28.7, schema hedge-event-v1).

    Price/quantity/fee amounts are decimal strings. Base-token fees and
    transfer taxes reduce the *net* spot quantity; quote-currency fees stay
    a cost line. The same fee is never double-counted. ``FUNDING_RECEIPT``
    carries ``amount``/``currency``/``public_funding_event_id`` with all
    quantity fields null.
    """

    schema_version: str
    leg_type: str
    event_type: str
    native_qty: str | None
    canonical_qty: str | None
    native_price: str | None
    price_currency: str | None
    fee_currency: str | None
    fee_amount: str | None
    fee_usd: str | None
    gas_usd: str | None
    source: str
    executed_at_ms: int
    supersedes_event_id: str | None = None
    reverses_event_id: str | None = None
    gross_qty: str | None = None
    net_qty: str | None = None
    amount: str | None = None
    currency: str | None = None
    public_funding_event_id: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != HEDGE_EVENT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {HEDGE_EVENT_SCHEMA_VERSION!r}")
        _check_enum("leg_type", self.leg_type, ALLOWED_LEG_TYPES)
        _check_enum("event_type", self.event_type, ALLOWED_EVENT_TYPES)
        if self.source != "USER_ENTERED":
            raise ValueError('source must be "USER_ENTERED"')
        if self.event_type == "FUNDING_RECEIPT":
            if self.amount is None or self.currency is None:
                raise ValueError("FUNDING_RECEIPT requires amount/currency")
            require_decimal_str("amount", self.amount)
            for name in ("native_qty", "canonical_qty", "native_price",
                         "gross_qty", "net_qty"):
                if getattr(self, name) is not None:
                    raise ValueError(f"FUNDING_RECEIPT must leave {name} null")
        else:
            for name in ("native_qty", "canonical_qty", "native_price",
                         "fee_amount", "fee_usd", "gas_usd", "gross_qty", "net_qty"):
                require_optional_decimal_str(name, getattr(self, name))
            if self.amount is not None or self.public_funding_event_id is not None:
                raise ValueError("non-funding events must not carry amount/public_funding_event_id")
            if self.native_qty is None and self.canonical_qty is None:
                raise ValueError("non-funding events require a quantity")
            for name in ("native_qty", "canonical_qty", "gross_qty", "net_qty"):
                value = getattr(self, name)
                if value is not None and Decimal(value) < 0:
                    raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True)
class HedgePosition:
    """Aggregated per-leg position (B28.0). All balances are decimal strings
    accumulated with integer coefficient/exponent alignment -- never float,
    never ``SQL SUM(qty)``, never a DOUBLE projection read-back.
    """

    plan_id: str
    leg_type: str
    open_qty: str
    closed_qty: str
    remaining_qty: str
    gross_qty: str
    net_qty: str
    weighted_avg_price: str | None = None
    event_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_enum("leg_type", self.leg_type, ALLOWED_LEG_TYPES)
        for name in ("open_qty", "closed_qty", "remaining_qty", "gross_qty", "net_qty"):
            require_decimal_str(name, getattr(self, name))
        require_optional_decimal_str("weighted_avg_price", self.weighted_avg_price)
        object.__setattr__(self, "event_ids", _coerce_str_tuple("event_ids", self.event_ids))
        if Decimal(self.remaining_qty) < 0:
            raise ValueError("remaining_qty must be non-negative")
        if Decimal(self.closed_qty) < 0 or Decimal(self.open_qty) < 0:
            raise ValueError("open/closed qty must be non-negative")


@dataclass(frozen=True)
class LedgerResult:
    """Return of ``apply_hedge_event`` (B28.7.1): ``event_id``,
    the new ``plan_version``, per-leg ``positions`` and the
    estimated/confirmed provenance of the balances.
    """

    event_id: str
    plan_id: str
    plan_version: int
    positions: tuple[HedgePosition, ...] = ()
    balance_source: str = "CONFIRMED"
    estimated: bool = False

    def __post_init__(self) -> None:
        if not self.event_id or not isinstance(self.event_id, str):
            raise ValueError("event_id must be a non-empty str")
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int):
            raise ValueError("plan_version must be an int")
        if self.balance_source not in ("CONFIRMED", "ESTIMATED"):
            raise ValueError('balance_source must be "CONFIRMED" or "ESTIMATED"')
        positions = tuple(self.positions)
        for item in positions:
            if not isinstance(item, HedgePosition):
                raise ValueError("positions must hold HedgePosition")
        object.__setattr__(self, "positions", positions)


@dataclass(frozen=True)
class PlanState:
    """Plan lifecycle state (B43.3). ``status`` is the持仓 state machine;
    ``readiness``/``risk_validation`` stay independent fields elsewhere.
    """

    plan_id: str
    status: str
    plan_version: int
    mode: str
    updated_at_ms: int

    def __post_init__(self) -> None:
        _check_enum("status", self.status, ALLOWED_PLAN_STATUS)
        _check_enum("mode", self.mode, ALLOWED_MODES)
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int):
            raise ValueError("plan_version must be an int")


# ---------------------------------------------------------------------------
# B26/B28.5/B28.6 Alert + Monitor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HedgeAlert:
    """Alert lifecycle row (B26/B28.6/B28.8). ``dedup_key`` is
    ``(plan_id, alert_code, leg_type_or_venue)``; the same condition in
    OPEN/ACKNOWLEDGED only bumps ``last_seen_at_ms``/context, while a
    re-trigger after RESOLVED opens a new ``episode`` in the same txn.
    """

    alert_id: str
    plan_id: str
    code: str
    severity: str
    state: str
    opened_at_ms: int
    last_seen_at_ms: int
    acknowledged_at_ms: int | None = None
    resolved_at_ms: int | None = None
    dedup_key: str = ""
    episode: int = 1
    recommended_action: str = "NONE"
    context_json: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_enum("severity", self.severity, ALLOWED_ALERT_SEVERITY)
        _check_enum("state", self.state, ALLOWED_ALERT_STATE)
        if not self.alert_id or not self.plan_id or not self.code:
            raise ValueError("alert_id/plan_id/code must be non-empty")
        if isinstance(self.episode, bool) or not isinstance(self.episode, int) or self.episode < 1:
            raise ValueError("episode must be an int >= 1")
        if self.recommended_action not in ("NONE", "REVIEW", "PAIR_EXIT", "URGENT_PAIR_EXIT"):
            raise ValueError(f"unknown recommended_action {self.recommended_action!r}")
        object.__setattr__(self, "context_json", _coerce_dict("context_json", self.context_json))


@dataclass(frozen=True)
class HedgeMonitor:
    """Active-plan monitor snapshot (B25/B28.5). All PnL/cost/carry legs are
    decimal strings with explicit settlement/FX provenance in
    ``source_meta_json``/``metrics_json``; unknown critical values stay
    null instead of ``0``.
    """

    snapshot_id: str
    plan_id: str
    as_of_ms: int
    actual_hedge_ratio: str | None = None
    residual_short_notional_usd: str | None = None
    mark_price: str | None = None
    spot_price: str | None = None
    current_basis_pct: str | None = None
    basis_pnl_usd: str | None = None
    estimated_settled_funding_usd: str | None = None
    projected_next_funding_usd: str | None = None
    spot_pnl_usd: str | None = None
    futures_pnl_usd: str | None = None
    known_cost_usd: str | None = None
    estimated_exit_cost_usd: str | None = None
    net_pnl_before_exit_usd: str | None = None
    estimated_net_pnl_after_exit_usd: str | None = None
    liquidation_distance: str | None = None
    exit_liquidity_json: Mapping[str, Any] | None = None
    source_meta_json: Mapping[str, Any] = field(default_factory=dict)
    quality_json: Mapping[str, Any] = field(default_factory=dict)
    metrics_json: Mapping[str, Any] = field(default_factory=dict)
    safety_score: float | None = None
    status: str = "OK"
    created_at_ms: int = 0

    def __post_init__(self) -> None:
        for name in ("actual_hedge_ratio", "residual_short_notional_usd", "mark_price",
                     "spot_price", "current_basis_pct", "basis_pnl_usd",
                     "estimated_settled_funding_usd", "projected_next_funding_usd",
                     "spot_pnl_usd", "futures_pnl_usd", "known_cost_usd",
                     "estimated_exit_cost_usd", "net_pnl_before_exit_usd",
                     "estimated_net_pnl_after_exit_usd", "liquidation_distance"):
            require_optional_decimal_str(name, getattr(self, name))
        object.__setattr__(self, "source_meta_json", _coerce_dict("source_meta_json", self.source_meta_json))
        object.__setattr__(self, "quality_json", _coerce_dict("quality_json", self.quality_json))
        object.__setattr__(self, "metrics_json", _coerce_dict("metrics_json", self.metrics_json))
        if self.exit_liquidity_json is not None:
            object.__setattr__(
                self, "exit_liquidity_json", _coerce_dict("exit_liquidity_json", self.exit_liquidity_json))


# ---------------------------------------------------------------------------
# Funding / FCS / DQ / Guidance / Readiness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FundingMetrics:
    """Per-symbol funding carry inputs (B14/B25). Rates and notionals are
    decimal strings; coverage fractions stay decimal strings as well so
    ``0.9`` never becomes binary-float ``0.8999999``.
    """

    symbol: str
    current_rate: str | None = None
    last_settled_rate: str | None = None
    funding_7d: str | None = None
    funding_30d: str | None = None
    funding_90d: str | None = None
    positive_ratio_30d: str | None = None
    positive_ratio_90d: str | None = None
    coverage_30d: str | None = None
    coverage_90d: str | None = None
    conservative_apr: str | None = None
    history_coverage: str | None = None

    def __post_init__(self) -> None:
        for name in ("current_rate", "last_settled_rate", "funding_7d", "funding_30d",
                     "funding_90d", "positive_ratio_30d", "positive_ratio_90d",
                     "coverage_30d", "coverage_90d", "conservative_apr", "history_coverage"):
            require_optional_decimal_str(name, getattr(self, name))


@dataclass(frozen=True)
class FCSResult:
    """Funding-capture score snapshot (B28.1). ``fcs`` is a 0..100 float for
    display/sort; the underlying yield/persistence maths stay in the
    frozen ``funding_metrics`` decimal strings.
    """

    snapshot_id: str
    symbol: str
    canonical_id: str
    as_of_ms: int
    fcs_version: str
    fcs_config_hash: str
    reference_notional_usd: str
    fcs: float | None = None
    module_scores: Mapping[str, Any] = field(default_factory=dict)
    funding_metrics: Mapping[str, Any] = field(default_factory=dict)
    venue_summary: Mapping[str, Any] = field(default_factory=dict)
    basis: Mapping[str, Any] | None = None
    risk: Mapping[str, Any] = field(default_factory=dict)
    readiness: str = "NOT_READY"
    reasons: tuple[str, ...] = ()
    created_at_ms: int = 0

    def __post_init__(self) -> None:
        _check_enum("readiness", self.readiness, ALLOWED_READINESS)
        if self.fcs_version not in FCS_VERSIONS_LEGACY:
            raise ValueError(
                f"fcs_version must be one of {sorted(FCS_VERSIONS_LEGACY)}, got {self.fcs_version!r}"
            )
        require_decimal_str("reference_notional_usd", self.reference_notional_usd)
        object.__setattr__(self, "module_scores", _coerce_dict("module_scores", self.module_scores))
        object.__setattr__(self, "funding_metrics", _coerce_dict("funding_metrics", self.funding_metrics))
        object.__setattr__(self, "venue_summary", _coerce_dict("venue_summary", self.venue_summary))
        object.__setattr__(self, "risk", _coerce_dict("risk", self.risk))
        object.__setattr__(self, "reasons", _coerce_str_tuple("reasons", self.reasons))


@dataclass(frozen=True)
class DQResult:
    """Hedge data-quality result (B38). ``score`` is 0..100; only verified,
    valid and fresh fields contribute ``1`` (funding history contributes
    ``coverage_fraction``). Identity and exit quotes are never N/A.
    """

    score: float
    group_scores: Mapping[str, Any] = field(default_factory=dict)
    field_credits: Mapping[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    readiness: str = "NOT_READY"

    def __post_init__(self) -> None:
        if not isinstance(self.score, (int, float)) or isinstance(self.score, bool):
            raise ValueError("score must be a number")
        if not 0 <= float(self.score) <= 100:
            raise ValueError("score must be in 0..100")
        _check_enum("readiness", self.readiness, ALLOWED_READINESS)
        object.__setattr__(self, "group_scores", _coerce_dict("group_scores", self.group_scores))
        object.__setattr__(self, "field_credits", _coerce_dict("field_credits", self.field_credits))
        object.__setattr__(self, "reasons", _coerce_str_tuple("reasons", self.reasons))


@dataclass(frozen=True)
class OrderGuidance:
    """Manual execution guidance (B20.2). Only venue operation parameters --
    never a trade request or on-chain payload. Quantity/limit/trigger
    prices are decimal strings.
    """

    leg: str
    venue: str
    instrument: str
    side: str
    order_type: str
    qty: str
    limit_price: str | None = None
    trigger_price: str | None = None
    trigger_basis: str | None = None
    reduce_only_or_close_position: bool = False
    valid_until_ms: int | None = None
    max_slippage_bps: float | None = None
    capability: str = "UNKNOWN"

    def __post_init__(self) -> None:
        require_decimal_str("qty", self.qty)
        if Decimal(self.qty) <= 0:
            raise ValueError("qty must be > 0")
        require_optional_decimal_str("limit_price", self.limit_price)
        require_optional_decimal_str("trigger_price", self.trigger_price)
        if not self.leg or not self.venue or not self.instrument:
            raise ValueError("leg/venue/instrument must be non-empty")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")


@dataclass(frozen=True)
class Readiness:
    """Planning/readiness gate (B15/B37). ``READY`` means local data plus
    user-verified contracts are satisfied -- never an account guarantee.
    """

    value: str
    reasons: tuple[str, ...] = ()
    risk_validation: str = "UNKNOWN"

    def __post_init__(self) -> None:
        _check_enum("value", self.value, ALLOWED_READINESS)
        _check_enum("risk_validation", self.risk_validation, ALLOWED_RISK_VALIDATION)
        object.__setattr__(self, "reasons", _coerce_str_tuple("reasons", self.reasons))


# ---------------------------------------------------------------------------
# B39 Outcome + Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HedgeOutcome:
    """Fixed-strategy hedge outcome (B28 outcome table + B39.3). Net return
    denominator is frozen ``capital_at_risk``, not a single margin leg.
    """

    outcome_id: str
    fcs_snapshot_id: str
    strategy: str
    horizon_days: int
    outcome_status: str
    reason_code: str | None
    evidence_version: str
    cost_config_hash: str
    outcome_json: Mapping[str, Any] = field(default_factory=dict)
    updated_at_ms: int = 0

    def __post_init__(self) -> None:
        _check_enum("strategy", self.strategy, ALLOWED_STRATEGIES)
        if self.horizon_days not in ALLOWED_HORIZON_DAYS:
            raise ValueError("horizon_days must be 7, 30 or 90")
        _check_enum("outcome_status", self.outcome_status, ALLOWED_OUTCOME_STATUS)
        if self.evidence_version not in HEDGE_EVIDENCE_VERSIONS_ALL:
            raise ValueError(
                f"evidence_version must be one of {sorted(HEDGE_EVIDENCE_VERSIONS_ALL)}, "
                f"got {self.evidence_version!r}"
            )
        object.__setattr__(self, "outcome_json", _coerce_dict("outcome_json", self.outcome_json))


@dataclass(frozen=True)
class HedgeEvidenceSummary:
    """Per-strategy evidence roll-up (B39.2/B39.3). Never backfills entry
    cost from a future spot quote; missing Alpha/on-chain exit quotes stay
    ``UNAVAILABLE``.
    """

    strategy: str
    horizon_days: int
    sample_count: int
    complete_count: int
    censored_count: int
    avg_net_return: str | None = None
    median_net_return: str | None = None
    evidence_version: str = HEDGE_EVIDENCE_VERSION

    def __post_init__(self) -> None:
        _check_enum("strategy", self.strategy, ALLOWED_STRATEGIES)
        if self.evidence_version not in HEDGE_EVIDENCE_VERSIONS_ALL:
            raise ValueError(
                f"evidence_version must be one of {sorted(HEDGE_EVIDENCE_VERSIONS_ALL)}, "
                f"got {self.evidence_version!r}"
            )
        if self.horizon_days not in ALLOWED_HORIZON_DAYS:
            raise ValueError("horizon_days must be 7, 30 or 90")
        for name in ("sample_count", "complete_count", "censored_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        require_optional_decimal_str("avg_net_return", self.avg_net_return)
        require_optional_decimal_str("median_net_return", self.median_net_return)


# ---------------------------------------------------------------------------
# B39.1.1 Historical market protocol + frozen bars/events/lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoricalPriceBar:
    """Frozen price bar (B39.1.1). OHLC are native decimal strings with the
    contemporaneous FX and source/known_at provenance.

    R00 (D18.1): ``price_basis`` defaults to TRADE for legacy bars; MARK bars
    carry explicit MARK provenance and only MARK bars feed liquidation paths.
    """

    symbol: str
    open_ms: int
    close_ms: int
    native_open: str
    native_high: str
    native_low: str
    native_close: str
    quote_asset: str
    fx_to_usd: str | None = None
    source: str = ""
    known_at_ms: int = 0
    price_basis: str = "TRADE"

    def __post_init__(self) -> None:
        for name in ("native_open", "native_high", "native_low", "native_close"):
            require_decimal_str(name, getattr(self, name))
        require_optional_decimal_str("fx_to_usd", self.fx_to_usd)
        _check_enum("price_basis", self.price_basis, ALLOWED_PRICE_BASIS)
        if self.close_ms <= self.open_ms:
            raise ValueError("close_ms must be > open_ms")


@dataclass(frozen=True)
class HistoricalFundingEvent:
    """Settled funding event (B39.1.1). Missing mark/FX stays null, never 0."""

    symbol: str
    funding_time_ms: int
    rate: str
    mark_price: str | None = None
    quote_asset: str = "USDT"
    fx_to_usd: str | None = None
    source: str = ""
    known_at_ms: int = 0

    def __post_init__(self) -> None:
        require_decimal_str("rate", self.rate)
        require_optional_decimal_str("mark_price", self.mark_price)
        require_optional_decimal_str("fx_to_usd", self.fx_to_usd)


@dataclass(frozen=True)
class HistoricalLifecycle:
    """Lifecycle at a cutoff (B39.1.1, A6 fields plus source ID)."""

    futures_symbol: str
    cutoff_ms: int
    onboard_at_ms: int | None = None
    delivery_at_ms: int | None = None
    contract_type: str | None = None
    exchange_status: str | None = None
    source_id: str = ""

    def __post_init__(self) -> None:
        if not self.futures_symbol:
            raise ValueError("futures_symbol must be non-empty")


@runtime_checkable
class HistoricalMarketProvider(Protocol):
    """Point-in-time history protocol (B39.1.1, frozen by H01).

    H10 implements/consumes this protocol; H10 must not copy
    ``SpotVenueQuote`` or invent a same-named DTO. ``find_frozen_quote``
    only answers from archived quotes with identical identity/venue/net
    quantity whose ``source_as_of`` lies in ``[at_ms-max_skew_ms, at_ms]``
    and whose ``known_at`` does not post-date grading; it picks
    ``source_as_of DESC, snapshotId ASC`` with ``max_skew_ms`` default
    5000 and returns ``None`` when missing. Current providers or
    different-quantity quotes must never approximate the past.
    """

    async def read_frozen_quote(self, snapshot_id: str) -> SpotVenueQuote | None: ...
    async def find_frozen_quote(
        self,
        canonical_id: str,
        venue: str,
        canonical_qty: str,
        at_ms: int,
        max_skew_ms: int = 5000,
    ) -> SpotVenueQuote | None: ...
    async def read_price_bars(
        self, symbol: str, start_ms: int, end_ms: int, request_context: Any
    ) -> tuple[HistoricalPriceBar, ...]: ...
    async def read_mark_price_bars(
        self, symbol: str, start_ms: int, end_ms: int, request_context: Any
    ) -> tuple[HistoricalPriceBar, ...]: ...
    async def read_settled_funding(
        self, symbol: str, start_ms: int, end_ms: int, request_context: Any
    ) -> tuple[HistoricalFundingEvent, ...]: ...
    async def read_lifecycle(self, symbol: str, cutoff_ms: int) -> HistoricalLifecycle | None: ...
