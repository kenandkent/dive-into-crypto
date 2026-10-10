"""R00 shared fixtures: identities, funding/decision contexts, events, market, FX + ports.

Frozen helpers (D19.5/D19.6): make_identity, make_funding_context,
make_decision_context, make_decision_request, make_events,
make_market_context, make_event_fx, plus make_ports test fakes.
Default decision case is MEME_FULL_VALID (1000PEPE, liquidation >2x Mark,
legal two-sided depth, sufficient capital, positive net Carry). UP_100 is a
stress scenario within that case, not a case ID.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from diveintocrypto_desktop.shortlab.hedge.models import (
    FundingMetrics,
    SpotVenueQuote,
    TradingRulesSnapshot,
)
from diveintocrypto_desktop.shortlab.models import AssetIdentity
from diveintocrypto_desktop.shortlab.observations import ObservationMeta, Observed
from diveintocrypto_desktop.shortlab.repair_contracts import (
    CaptureContext,
    CaptureResult,
    DecisionContext,
    DecisionRequest,
    DecisionResult,
    EconomicsResult,
    FundingContext,
    FundingCoverage,
    FundingScheduleSegment,
    FuturesExecutionQuote,
    GateResult,
    LedgerPnl,
    PairExitGuidance,
    QuoteCollectionResult,
    RatioProposal,
    ScenarioResult,
)
from diveintocrypto_desktop.shortlab.repair_ports import REPAIR_PORT_KEYS, RepairPorts

FIXTURE_NOW = 1791417600000
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "repair" / "ports"


class FixtureCaseNotFound(KeyError):
    """Unknown fixture case / mismatched asset/qty/window (no generic PASS)."""


def _load_json(name: str) -> Any:
    path = FIXTURE_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _observed(value: Any, *, source: str, source_as_of_ms: int | None, known_at_ms: int) -> Observed[Any]:
    return Observed(
        value=value,
        meta=ObservationMeta(
            status="OK",
            source=source,
            source_as_of_ms=source_as_of_ms,
            fetched_at_ms=known_at_ms,
            known_at_ms=known_at_ms,
        ),
    )


# ---------------------------------------------------------------------------
# make_identity
# ---------------------------------------------------------------------------

_IDENTITY_FIELDS = {
    "canonical_id", "display_symbol", "name", "binance_futures_symbol",
    "binance_spot_symbol", "contract_multiplier", "multiplier_source",
    "coingecko_id", "unlock_provider_id", "social_provider_id", "chain",
    "contract_address", "categories", "mapping_confidence", "mapping_source",
}


def make_identity(case: str = "default", **kwargs: Any) -> AssetIdentity:
    """BTC/m1 default; m1000 for 1000PEPE; ambiguous for unresolved."""
    if case == "default":
        base: dict[str, Any] = dict(
            canonical_id="bitcoin",
            display_symbol="BTCUSDT",
            name="Bitcoin",
            binance_futures_symbol="BTCUSDT",
            binance_spot_symbol="BTCUSDT",
            contract_multiplier=1,
            multiplier_source="EXCHANGE",
            coingecko_id="bitcoin",
            chain=None,
            contract_address=None,
            categories=("MEME",),
            mapping_confidence="VERIFIED",
            mapping_source="MANUAL",
        )
    elif case in ("m1000", "MEME_FULL_VALID", "1000PEPE"):
        base = dict(
            canonical_id="pepe",
            display_symbol="1000PEPEUSDT",
            name="Pepe",
            binance_futures_symbol="1000PEPEUSDT",
            binance_spot_symbol="1000PEPEUSDT",
            contract_multiplier=1000,
            multiplier_source="EXCHANGE",
            coingecko_id="pepe",
            chain=None,
            contract_address=None,
            categories=("MEME",),
            mapping_confidence="VERIFIED",
            mapping_source="MANUAL",
        )
    elif case == "ambiguous":
        base = dict(
            canonical_id="ambiguous",
            display_symbol="PEPEUSDT",
            name=None,
            binance_futures_symbol="PEPEUSDT",
            binance_spot_symbol=None,
            contract_multiplier=None,
            multiplier_source=None,
            categories=(),
            mapping_confidence="UNRESOLVED",
            mapping_source="OTHER",
        )
    else:
        raise FixtureCaseNotFound(f"unknown identity case {case!r}")
    for key in kwargs:
        if key not in _IDENTITY_FIELDS:
            raise ValueError(f"make_identity rejects unknown key {key!r}")
    base.update(kwargs)
    return AssetIdentity(**base)


# ---------------------------------------------------------------------------
# make_funding_context
# ---------------------------------------------------------------------------

_FUNDING_METRICS_FIELDS = {
    "symbol", "current_rate", "last_settled_rate", "funding_7d", "funding_30d",
    "funding_90d", "positive_ratio_30d", "positive_ratio_90d", "coverage_30d",
    "coverage_90d", "conservative_apr", "history_coverage",
}
_FUNDING_CONTEXT_FIELDS = {
    "history_class", "listing_age_days", "conservative_apr",
    "conservative_method", "schedule_refs", "input_refs",
}


def _coverage(window_start: int, window_end: int, *, expected: int | None, received: int,
              coverage: str | None, schedule_cov: str, reasons: tuple[str, ...] = ()) -> FundingCoverage:
    return FundingCoverage(
        window_start_ms=window_start,
        window_end_ms=window_end,
        expected_count=expected,
        received_count=received,
        coverage_fraction=coverage,
        schedule_coverage_fraction=schedule_cov,
        missing_slots=(),
        reasons=reasons,
    )


def make_funding_context(case: str = "valid", **kwargs: Any) -> FundingContext:
    """Valid (positive) default; negative/zero/unknown variants for gate matrix."""
    for key in kwargs:
        if key not in _FUNDING_METRICS_FIELDS | _FUNDING_CONTEXT_FIELDS | {
            "coverage_7d", "coverage_30d", "coverage_90d", "metrics",
            "current_observation", "last_settled_observation",
        }:
            raise ValueError(f"make_funding_context rejects unknown key {key!r}")
    now = FIXTURE_NOW
    start_90 = now - 90 * 86400_000
    if case == "valid":
        metrics = FundingMetrics(
            symbol="1000PEPEUSDT",
            current_rate="0.0005",
            last_settled_rate="0.0004",
            funding_7d="0.004",
            funding_30d="0.018",
            funding_90d="0.05",
            positive_ratio_30d="0.85",
            positive_ratio_90d="0.8",
            coverage_30d="0.95",
            coverage_90d="0.92",
            conservative_apr="0.25",
            history_coverage="0.95",
        )
        cov7 = _coverage(now - 7 * 86400_000, now, expected=21, received=21, coverage="1", schedule_cov="1")
        cov30 = _coverage(now - 30 * 86400_000, now, expected=90, received=88, coverage="0.97777778", schedule_cov="1")
        cov90 = _coverage(start_90, now, expected=270, received=270, coverage="1", schedule_cov="1")
        history_class = "FULL_90D"
        listing_age = 120
        conservative_apr = "0.25"
        method = "CONSERVATIVE_P25"
        current_obs = _observed({"rate": "0.0005", "funding_time_ms": now - 60_000},
                                source="binance:fapi/fundingRate", source_as_of_ms=now - 60_000, known_at_ms=now - 50_000)
        last_obs = _observed({"rate": "0.0004", "funding_time_ms": now - 8 * 3600_000},
                             source="binance:fapi/fundingRate", source_as_of_ms=now - 8 * 3600_000, known_at_ms=now - 8 * 3600_000 + 5000)
    elif case == "negative":
        metrics = FundingMetrics(
            symbol="1000PEPEUSDT", current_rate="-0.0005", last_settled_rate="-0.0005",
            funding_7d="-0.004", funding_30d="0.018", funding_90d="0.05",
            positive_ratio_30d="0.85", positive_ratio_90d="0.8",
            coverage_30d="0.95", coverage_90d="0.92",
            conservative_apr="0.25", history_coverage="0.95",
        )
        cov7 = _coverage(now - 7 * 86400_000, now, expected=21, received=21, coverage="1", schedule_cov="1")
        cov30 = _coverage(now - 30 * 86400_000, now, expected=90, received=88, coverage="0.97777778", schedule_cov="1")
        cov90 = _coverage(start_90, now, expected=270, received=270, coverage="1", schedule_cov="1")
        history_class = "FULL_90D"
        listing_age = 120
        conservative_apr = "0.25"
        method = "CONSERVATIVE_P25"
        current_obs = _observed({"rate": "-0.0005"}, source="binance:fapi/fundingRate",
                                source_as_of_ms=now - 60_000, known_at_ms=now - 50_000)
        last_obs = _observed({"rate": "-0.0005"}, source="binance:fapi/fundingRate",
                             source_as_of_ms=now - 8 * 3600_000, known_at_ms=now - 8 * 3600_000 + 5000)
    elif case == "zero":
        metrics = FundingMetrics(
            symbol="1000PEPEUSDT", current_rate="0", last_settled_rate="0.0004",
            funding_7d="0.004", funding_30d="0.018", funding_90d="0.05",
            positive_ratio_30d="0.85", positive_ratio_90d="0.8",
            coverage_30d="0.95", coverage_90d="0.92",
            conservative_apr="0.25", history_coverage="0.95",
        )
        cov7 = _coverage(now - 7 * 86400_000, now, expected=21, received=21, coverage="1", schedule_cov="1")
        cov30 = _coverage(now - 30 * 86400_000, now, expected=90, received=88, coverage="0.97777778", schedule_cov="1")
        cov90 = _coverage(start_90, now, expected=270, received=270, coverage="1", schedule_cov="1")
        history_class = "FULL_90D"
        listing_age = 120
        conservative_apr = "0.25"
        method = "CONSERVATIVE_P25"
        current_obs = _observed({"rate": "0"}, source="binance:fapi/fundingRate",
                                source_as_of_ms=now - 60_000, known_at_ms=now - 50_000)
        last_obs = _observed({"rate": "0.0004"}, source="binance:fapi/fundingRate",
                             source_as_of_ms=now - 8 * 3600_000, known_at_ms=now - 8 * 3600_000 + 5000)
    elif case == "unknown":
        metrics = FundingMetrics(symbol="1000PEPEUSDT")
        cov7 = _coverage(now - 7 * 86400_000, now, expected=None, received=0, coverage=None, schedule_cov="0",
                         reasons=("FUNDING_SCHEDULE_UNKNOWN",))
        cov30 = _coverage(now - 30 * 86400_000, now, expected=None, received=0, coverage=None, schedule_cov="0",
                          reasons=("FUNDING_SCHEDULE_UNKNOWN",))
        cov90 = _coverage(start_90, now, expected=None, received=0, coverage=None, schedule_cov="0",
                          reasons=("FUNDING_SCHEDULE_UNKNOWN",))
        history_class = "HISTORY_CLASS_UNKNOWN"
        listing_age = None
        conservative_apr = None
        method = "UNKNOWN"
        current_obs = _observed(None, source="unknown", source_as_of_ms=None, known_at_ms=now - 50_000)
        last_obs = None
    else:
        raise FixtureCaseNotFound(f"unknown funding case {case!r}")

    # kwargs overrides: FundingMetrics fields patch metrics; context fields patch context.
    metric_overrides = {k: v for k, v in kwargs.items() if k in _FUNDING_METRICS_FIELDS}
    if metric_overrides:
        import dataclasses

        patched = dict(dataclasses.asdict(metrics))
        patched.update(metric_overrides)
        metrics = FundingMetrics(**patched)
    context_kwargs: dict[str, Any] = {}
    for key in ("history_class", "listing_age_days", "conservative_apr", "conservative_method"):
        if key in kwargs:
            context_kwargs[key] = kwargs[key]
    # Direct coverage/observation overrides (DTO fields).
    for key in ("coverage_7d", "coverage_30d", "coverage_90d", "current_observation", "last_settled_observation"):
        if key in kwargs:
            context_kwargs[key] = kwargs[key]
    return FundingContext(
        metrics=metrics,
        coverage_7d=context_kwargs.get("coverage_7d", cov7),
        coverage_30d=context_kwargs.get("coverage_30d", cov30),
        coverage_90d=context_kwargs.get("coverage_90d", cov90),
        history_class=context_kwargs.get("history_class", history_class),
        listing_age_days=context_kwargs.get("listing_age_days", listing_age),
        conservative_apr=context_kwargs.get("conservative_apr", conservative_apr),
        conservative_method=context_kwargs.get("conservative_method", method),
        current_observation=context_kwargs.get("current_observation", current_obs),
        last_settled_observation=context_kwargs.get("last_settled_observation", last_obs),
        schedule_refs=tuple(kwargs.get("schedule_refs", ("sched-1",))),
        input_refs=dict(kwargs.get("input_refs", {
            "funding": "fcs-MEME_FULL_VALID",
            **({
                "schedule_checked_at_ms": str(now),
                "last_expected_slot_ms": str(now - 8 * 3600_000),
            } if last_obs is not None else {}),
        })),
    )


# ---------------------------------------------------------------------------
# make_decision_request / context (MEME_FULL_VALID default)
# ---------------------------------------------------------------------------

_DECISION_REQUEST_FIELDS = {
    "symbol", "goal", "futures_notional_usd", "planned_hold_days",
    "available_capital_usd", "max_scenario_loss_usd", "margin_usd",
    "liquidation_price", "liquidation_price_updated_at_ms", "preferred_spot_venue",
}


def make_decision_request(case: str = "MEME_FULL_VALID", **kwargs: Any) -> DecisionRequest:
    if case != "MEME_FULL_VALID":
        raise FixtureCaseNotFound(f"unknown decision request case {case!r}")
    for key in kwargs:
        if key not in _DECISION_REQUEST_FIELDS:
            raise ValueError(f"make_decision_request rejects unknown key {key!r}")
    base: dict[str, Any] = dict(
        symbol="1000PEPEUSDT",
        goal="CARRY_CAPTURE",
        futures_notional_usd="10000",
        planned_hold_days=30,
        available_capital_usd="25000",
        max_scenario_loss_usd="1000",
        margin_usd="12000",
        liquidation_price="0.025",
        liquidation_price_updated_at_ms=FIXTURE_NOW - 3600_000,
        preferred_spot_venue="AUTO",
    )
    base.update(kwargs)
    return DecisionRequest(**base)


def _default_futures_quote() -> FuturesExecutionQuote:
    return FuturesExecutionQuote(
        quote_id="fq-MEME_FULL_VALID",
        symbol="1000PEPEUSDT",
        requested_contract_qty="100",
        buy_vwap_native="0.0101",
        sell_vwap_native="0.0099",
        buy_executable_qty="1000",
        sell_executable_qty="1000",
        quote_currency="USDT",
        quote_to_usd="1",
        as_of_ms=FIXTURE_NOW - 5000,
        known_at_ms=FIXTURE_NOW - 4000,
        expires_at_ms=FIXTURE_NOW + 15000,
        book_observation_id="book-1",
        fees_included=True,
    )


def _default_rules() -> TradingRulesSnapshot:
    return TradingRulesSnapshot(
        venue="BINANCE_SPOT",
        instrument_id="1000PEPEUSDT",
        source_as_of_ms=FIXTURE_NOW - 60000,
        known_at_ms=FIXTURE_NOW - 50000,
        rule_version="rules-v1",
        raw_filters={},
        order_types={"LIMIT": True, "MARKET": True, "STOP": True, "STOP_MARKET": True},
        price_rules={"tick_size": "0.000001"},
        lot_rules={"step_size": "1", "min_qty": "1", "max_qty": "1000000"},
        notional_rules={"min_notional": "5", "max_notional": "1000000"},
        stop_orders_supported=True,
        conditional_orders_source_ref="rules:1000PEPEUSDT:1",
    )


def _default_venue_quotes() -> tuple[SpotVenueQuote, ...]:
    q = SpotVenueQuote(
        venue="BINANCE_SPOT",
        canonical_id="pepe",
        symbol="1000PEPEUSDT",
        chain=None,
        contract_address=None,
        as_of_ms=FIXTURE_NOW - 5000,
        expires_at_ms=FIXTURE_NOW + 15000,
        reference_notional_usd="10000",
        mid_price="0.01",
        buy_vwap="0.0101",
        sell_vwap="0.0099",
        buy_executable_qty="200000",
        sell_executable_qty="200000",
        buy_slippage_bps=5.0,
        sell_slippage_bps=5.0,
        estimated_fee_usd="5",
        estimated_gas_usd=None,
        direction_costs={},
        entry_feasible=True,
        exit_feasible=True,
        exit_feasibility="CONFIRMED",
        quote_currency="USDT",
        quote_to_usd="1",
        source_timestamp_ms=FIXTURE_NOW - 5000,
        fetched_at_ms=FIXTURE_NOW - 4000,
        requested_canonical_qty="100000",
        trading_rules={},
        capabilities={},
        identity_confidence="VERIFIED",
        status="OK",
        reason_code=None,
        fees_included=True,
    )
    return (q,)


def make_decision_context(case: str = "MEME_FULL_VALID", **kwargs: Any) -> DecisionContext:
    if case != "MEME_FULL_VALID":
        raise FixtureCaseNotFound(f"unknown decision context case {case!r}")
    allowed = {
        "identity", "funding_context", "futures_mark", "futures_quote",
        "futures_rules", "venue_quotes", "directional", "directional_score_id",
        "identity_snapshot_id", "fcs_snapshot_id", "source_refs", "as_of_ms",
    }
    for key in kwargs:
        if key not in allowed:
            raise ValueError(f"make_decision_context rejects unknown key {key!r}")
    identity = kwargs.get("identity", make_identity("m1000"))
    funding_context = kwargs.get("funding_context", make_funding_context("valid"))
    # Mark 0.01, liquidation 0.025 (>2x) keeps UP_100 (0.02) below liquidation.
    futures_mark = kwargs.get(
        "futures_mark",
        _observed({"price": "0.01"}, source="binance:fapi/mark",
                  source_as_of_ms=FIXTURE_NOW - 3000, known_at_ms=FIXTURE_NOW - 2000),
    )
    futures_quote = kwargs.get("futures_quote", _default_futures_quote())
    futures_rules = kwargs.get("futures_rules", _default_rules())
    venue_quotes = kwargs.get("venue_quotes", _default_venue_quotes())
    directional = kwargs.get(
        "directional",
        {
            "profile": "MEME",
            "ltss": 85.0,
            "entry": 75.0,
            "data_quality": 85.0,
            "tradeability": 8.0,
            "candidate_status": "CANDIDATE",
            "execution_status": "READY",
            "vetoes": [],
            "pauses": [],
            "score_as_of_ms": FIXTURE_NOW - 60000,
            "config_hash": "0" * 64,
        },
    )
    return DecisionContext(
        identity_snapshot_id=kwargs.get("identity_snapshot_id", "identity-MEME_FULL_VALID"),
        directional_score_id=kwargs.get("directional_score_id", None),
        fcs_snapshot_id=kwargs.get("fcs_snapshot_id", "fcs-MEME_FULL_VALID"),
        funding_context=funding_context,
        identity=identity,
        futures_mark=futures_mark,
        futures_quote=futures_quote,
        futures_rules=futures_rules,
        venue_quotes=venue_quotes,
        directional=directional,
        source_refs=dict(kwargs.get("source_refs", {"identity": "identity-MEME_FULL_VALID", "fcs": "fcs-MEME_FULL_VALID"})),
        as_of_ms=kwargs.get("as_of_ms", FIXTURE_NOW),
    )


# ---------------------------------------------------------------------------
# make_events / market / fx (partial_close default, FX1, explicit zero fees)
# ---------------------------------------------------------------------------


def make_events(case: str = "partial_close", **kwargs: Any) -> tuple[Mapping[str, Any], ...]:
    if case != "partial_close":
        raise FixtureCaseNotFound(f"unknown events case {case!r}")
    if kwargs:
        raise ValueError(f"make_events rejects unknown keys {sorted(kwargs)}")
    now = FIXTURE_NOW
    return (
        {"event_id": "e-open-fut", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": "10", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 86400_000},
        {"event_id": "e-close-fut", "leg_type": "FUTURES_SHORT", "event_type": "CLOSE_FUTURES_SHORT",
         "native_qty": "4", "native_price": "90", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 43200_000},
        {"event_id": "e-open-spot", "leg_type": "SPOT_LONG", "event_type": "OPEN_SPOT_LONG",
         "native_qty": "10", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 86400_000},
        {"event_id": "e-close-spot", "leg_type": "SPOT_LONG", "event_type": "CLOSE_SPOT_LONG",
         "native_qty": "4", "native_price": "110", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 43200_000},
    )


def make_event_fx(case: str = "partial_close", **kwargs: Any) -> Mapping[str, Mapping[str, Any]]:
    if case != "partial_close":
        raise FixtureCaseNotFound(f"unknown event fx case {case!r}")
    if kwargs:
        raise ValueError(f"make_event_fx rejects unknown keys {sorted(kwargs)}")
    out: dict[str, Any] = {}
    for event in make_events():
        eid = event["event_id"]
        out[eid] = {"price_fx_id": f"fx-{eid}", "price_fx": "1",
                    "fee_fx_id": f"fx-{eid}-fee", "fee_fx": "1",
                    "funding_fx_id": None, "funding_fx": None}
    return out


def make_market_context(case: str = "partial_close", **kwargs: Any) -> Mapping[str, Any]:
    if case != "partial_close":
        raise FixtureCaseNotFound(f"unknown market case {case!r}")
    allowed = {
        "now_ms", "futures_mark_native", "futures_quote_fx", "spot_sell_vwap_native",
        "spot_quote_fx", "estimated_exit_fee_usd", "exit_quote_refs",
        "futures_buy_vwap_native", "futures_exit_coverage", "spot_exit_coverage",
        "expires_at_ms",
    }
    for key in kwargs:
        if key not in allowed:
            raise ValueError(f"make_market_context rejects unknown key {key!r}")
    base: dict[str, Any] = dict(
        now_ms=FIXTURE_NOW,
        futures_mark_native="100",
        futures_quote_fx="1",
        spot_sell_vwap_native="110",
        spot_quote_fx="1",
        estimated_exit_fee_usd="0",
        exit_quote_refs={"futures": "fq-1", "spot": "sq-1"},
        futures_buy_vwap_native="100",
        futures_exit_coverage="1",
        spot_exit_coverage="1",
        expires_at_ms=FIXTURE_NOW + 20000,
    )
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Fake ports (TEST_FAKE, JSON-backed)
# ---------------------------------------------------------------------------


def _gate_for_context(context: FundingContext) -> GateResult:
    data = _load_json("gate_cases.json")
    by_id = {c["case_id"]: c for c in data["cases"]}
    metrics = context.metrics
    cur = getattr(metrics, "current_rate", None)
    last = getattr(metrics, "last_settled_rate", None)
    cov90 = getattr(context, "coverage_90d", None)
    if cov90 is not None and getattr(cov90, "expected_count", 1) is None:
        row = by_id["UNKNOWN_SCHEDULE"]
    elif cur is None or last is None:
        row = by_id["UNKNOWN_SCHEDULE"]
    else:
        try:
            from decimal import Decimal

            if Decimal(str(cur)) <= 0 or Decimal(str(last)) <= 0:
                if Decimal(str(cur)) == 0:
                    row = by_id["ZERO_FAIL"]
                else:
                    row = by_id["NEGATIVE_FAIL"]
            else:
                row = by_id["POSITIVE_PASS"]
        except Exception:
            row = by_id["UNKNOWN_SCHEDULE"]
    return GateResult(row["status"], tuple(row["reasons"]), int(row["checked_at_ms"]), dict(row["input_refs"]))


def fake_schedule_coverage(events: Sequence[Mapping], schedules: Sequence[Any],
                           start_ms: int, end_ms: int, known_by_ms: int) -> FundingCoverage:
    data = _load_json("coverage_cases.json")
    by_id = {c["case_id"]: c for c in data["cases"]}
    if not schedules:
        row = by_id["UNKNOWN_SCHEDULE"]
    else:
        # Default valid window; head/tail gaps would select MISSING_HEAD_TAIL in
        # producer tests with explicit event gaps (kept simple here).
        row = by_id["MEME_FULL_VALID_90D"]
        # Honour the requested window for determinism.
        row = dict(row, window_start_ms=int(start_ms), window_end_ms=int(end_ms))
    return FundingCoverage(
        window_start_ms=int(row["window_start_ms"]),
        window_end_ms=int(row["window_end_ms"]),
        expected_count=row["expected_count"],
        received_count=int(row["received_count"]),
        coverage_fraction=row["coverage_fraction"],
        schedule_coverage_fraction=str(row["schedule_coverage_fraction"]),
        missing_slots=tuple(row["missing_slots"]),
        reasons=tuple(row["reasons"]),
    )


def fake_funding_gate(context: FundingContext, policy: Mapping, as_of_ms: int) -> GateResult:
    # Policy is accepted but not re-implemented here; routing is by context rates.
    _ = policy, as_of_ms
    if not isinstance(context, FundingContext):
        raise FixtureCaseNotFound("fake_funding_gate requires FundingContext")
    return _gate_for_context(context)


def fake_ratio_proposal(request: DecisionRequest, context: DecisionContext,
                        target_ratio: str, policy: Mapping, *, ports: Any = None) -> RatioProposal:
    _ = policy, ports
    data = _load_json("ratio_cases.json")
    if getattr(request, "symbol", None) != data["symbol"] or getattr(context, "funding_context", None) is None:
        raise FixtureCaseNotFound("ratio fixture asset mismatch")
    rows = {r["target_ratio"]: r for r in data["ratios"]}
    if str(target_ratio) not in rows:
        raise FixtureCaseNotFound(f"unknown target_ratio {target_ratio!r}")
    row = rows[str(target_ratio)]
    scenarios = (
        ScenarioResult("UP_50", "0.50", "0.50", "0", "VALID", "100", "0", ()),
        ScenarioResult("UP_100", "1", "1", "0", row["up100_status"], "50", "0", ()),
        ScenarioResult("DOWN_50", "-0.50", "-0.50", "0", "VALID", "200", "0", ()),
        ScenarioResult("BASIS_UP", "0.20", "0.10", "0", "VALID", "80", "0", ()),
        ScenarioResult("BASIS_DOWN", "-0.10", "-0.20", "0", "VALID", "90", "0", ()),
        ScenarioResult("FX_DOWN", "0", "0", "-0.03", "VALID", "10", "0", ()),
    )
    economics = EconomicsResult(
        hold_days=30,
        actual_futures_notional_usd=str(data["actual_notional_usd"]),
        conservative_carry_usd=str(row["net_carry_usd"]) if row["net_carry_usd"] is not None else None,
        roundtrip_cost_usd="30",
        net_carry_usd=str(row["net_carry_usd"]) if row["net_carry_usd"] is not None else None,
        break_even_days="12.5",
        capital_required_usd="12000",
        cost_basis="NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V2",
        gate=GateResult("PASS", (), FIXTURE_NOW, {}),
        unknown_components=(),
    )
    return RatioProposal(
        target_ratio=str(row["target_ratio"]),
        actual_ratio=str(row["actual_ratio"]),
        futures_contract_qty=str(row["futures_contract_qty"]),
        canonical_futures_qty=str(row["canonical_futures_qty"]),
        spot_net_qty=str(row["spot_net_qty"]),
        spot_venue=row["spot_venue"],
        quote_refs={"futures": "fq-MEME_FULL_VALID", "spot": "sq-MEME_FULL_VALID"},
        economics=economics,
        scenarios=scenarios,
        execution_gate=GateResult("PASS", (), FIXTURE_NOW, {}),
        risk_gate=GateResult("PASS", (), FIXTURE_NOW, {}),
        order_guidance=(),
    )


def fake_ledger_pnl(events: Sequence[Mapping], identity: AssetIdentity,
                    event_fx: Mapping[str, Mapping], market_context: Mapping) -> LedgerPnl:
    if not events:
        raise FixtureCaseNotFound("empty events have no ledger fixture")
    data = _load_json("ledger_cases.json")
    row = data["cases"][0]
    return LedgerPnl(
        realized_futures_usd=row["realized_futures_usd"],
        realized_spot_usd=row["realized_spot_usd"],
        unrealized_futures_usd=row["unrealized_futures_usd"],
        unrealized_spot_usd=row["unrealized_spot_usd"],
        actual_funding_usd=row["actual_funding_usd"],
        estimated_unconfirmed_funding_usd=row["estimated_unconfirmed_funding_usd"],
        known_cost_usd=row["known_cost_usd"],
        estimated_exit_cost_usd=row["estimated_exit_cost_usd"],
        known_net_subtotal_usd=row["known_net_subtotal_usd"],
        net_before_exit_usd=row["net_before_exit_usd"],
        net_after_exit_usd=row["net_after_exit_usd"],
        unknown_components=tuple(row["unknown_components"]),
        coverage=dict(row["coverage"]),
        funding_basis=row["funding_basis"],
        as_of_ms=FIXTURE_NOW,
    )


def fake_pair_exit(plan: Mapping, positions: Sequence[Mapping],
                   market: Mapping, rules: Mapping, now_ms: int) -> PairExitGuidance:
    data = _load_json("exit_cases.json")
    row = data["cases"][0]
    return PairExitGuidance(
        plan_id=str(row["plan_id"]),
        plan_version=int(row["plan_version"]),
        generated_at_ms=int(now_ms),
        expires_at_ms=int(now_ms) + 20000,
        legs=tuple(row["legs"]),
        unexecutable_dust={},
        reasons=(),
        confirmation_required=bool(row["confirmation_required"]),
    )


def fake_projection(snapshot: Mapping, as_of_ms: int) -> Mapping[str, Any]:
    data = _load_json("opportunity_cases.json")
    row = data["cases"][0]
    if isinstance(snapshot, Mapping):
        sid = snapshot.get("snapshot_id", snapshot.get("snapshotId"))
        if sid is not None and sid != row["snapshot_id"]:
            raise FixtureCaseNotFound(f"unknown snapshot {sid!r}")
    return dict(row)


async def fake_capture_entries(context: CaptureContext, repository: Any,
                               market: Any, request_context: Any) -> CaptureResult:
    data = _load_json("capture_cases.json")
    row = data["cases"][0]
    strategies = list(getattr(context, "strategies", ()))
    if [s for s in strategies] != row["strategies"] and strategies:
        # Strict: SYSTEM group must match exactly; other subsets are PARTIAL only
        # when explicitly requested with matching entry_ids order.
        if set(strategies) - set(row["strategies"]):
            raise FixtureCaseNotFound(f"capture strategies mismatch {strategies!r}")
    entry_ids = tuple(f"entry-{s}" for s in strategies) if strategies else tuple(row["entry_ids"])
    status = "COMPLETE" if len(entry_ids) == len(row["entry_ids"]) else ("PARTIAL" if entry_ids else "UNAVAILABLE")
    return CaptureResult(entry_ids, status, FIXTURE_NOW, ())


async def fake_due_quotes(repository: Any, market: Any, as_of_ms: int, request_context: Any) -> QuoteCollectionResult:
    data = _load_json("due_quote_cases.json")
    row = data["cases"][0]
    return QuoteCollectionResult(
        int(row["claimed"]), int(row["complete"]), int(row["deferred"]),
        int(row["unavailable"]), tuple(row["task_ids"]), int(as_of_ms), (),
    )


def fake_simulation(request: Any, **kwargs: Any) -> Any:
    from diveintocrypto_desktop.shortlab.hedge.models import HedgeSimulationResult

    data = _load_json("simulation_cases.json")
    symbol = getattr(request, "symbol", None) if not isinstance(request, Mapping) else request.get("symbol")
    match = next((c for c in data["cases"] if c["symbol"] == symbol), None)
    if match is None:
        raise FixtureCaseNotFound(f"no simulation fixture for {symbol!r}")
    # Unknown capability row selected when caller passes unknown protection hint.
    if isinstance(request, Mapping) and request.get("stop_policy") == "UNKNOWN_CAP":
        match = next(c for c in data["cases"] if c["case_id"] == "UNKNOWN_CAPABILITY")
    return HedgeSimulationResult(
        simulation_id="sim-MEME_FULL_VALID",
        generated_at_ms=FIXTURE_NOW,
        expires_at_ms=FIXTURE_NOW + 20000,
        symbol=symbol,
        canonical_id="pepe",
        mode=getattr(request, "mode", "ABSOLUTE") if not isinstance(request, Mapping) else request.get("mode", "ABSOLUTE"),
        futures_symbol=symbol,
        futures_price="0.01",
        canonical_futures_price_usd="0.01",
        futures_quote_currency="USDT",
        quote_to_usd="1",
        futures_notional_usd="10000",
        futures_contract_qty="100",
        canonical_futures_qty="100000",
        target_hedge_ratio="1",
        spot_venue="BINANCE_SPOT",
        readiness=str(match["readiness"]),
        risk_validation=str(match["risk_validation"]),
    )


_DEFAULT_PORT_CALLBACKS: dict[str, Any] = {
    "compute_schedule_coverage": fake_schedule_coverage,
    "evaluate_funding_entry_gate": fake_funding_gate,
    "build_ratio_proposal": fake_ratio_proposal,
    "compute_ledger_pnl": fake_ledger_pnl,
    "build_pair_exit_guidance": fake_pair_exit,
    "project_opportunity": fake_projection,
    "capture_strategy_entries": fake_capture_entries,
    "collect_due_quotes": fake_due_quotes,
    "simulate_hedge": fake_simulation,
}


def make_ports(**overrides: Any) -> RepairPorts:
    """Build a fresh frozen RepairPorts with TEST_FAKE defaults (D19.6)."""
    for key, value in overrides.items():
        if key not in REPAIR_PORT_KEYS:
            raise ValueError(f"unknown port override {key!r}; keys={list(REPAIR_PORT_KEYS)}")
        if value is not None and not callable(value):
            raise ValueError(f"port override {key!r} must be callable or None")
    callbacks = dict(_DEFAULT_PORT_CALLBACKS)
    callbacks.update(overrides)
    return RepairPorts(**callbacks)
