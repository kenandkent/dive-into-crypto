"""R07 joint decision (D07/D18, V09).

Red->green: before R07 ``shortlab.hedge.decision.recommend_hedge`` did not
exist (ModuleNotFoundError). Pure offline only: fixed ``FIXTURE_NOW`` clock,
D15 merged config from R00, no network/DB/clock reads. Amount assertions use
Decimal exactness; Gate/status/quantity checks are exact.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from diveintocrypto_desktop.shortlab.config import (
    decision_policy_hash,
    load_shortlab_config,
)
from diveintocrypto_desktop.shortlab.hedge.decision import recommend_hedge
from diveintocrypto_desktop.shortlab.repair_contracts import (
    GateResult,
    RatioProposal,
    ScenarioResult,
)
from tests.repair_fixtures import (
    FIXTURE_NOW,
    fake_ratio_proposal,
    make_decision_context,
    make_decision_request,
    make_funding_context,
    make_ports,
)


def _default_policy():
    return load_shortlab_config()


def _assert_full(result, *, target: str = "1") -> None:
    assert result.recommendation == "FULL_HEDGE"
    assert result.selected_proposal is not None
    assert result.selected_proposal.target_ratio == target
    actual = Decimal(str(result.selected_proposal.actual_ratio))
    assert abs(actual - Decimal("1")) <= Decimal("0.02")
    assert result.validation_level == "RULE_BASED_UNVALIDATED"
    assert result.formula_version == "hedge-decision-v1"
    assert result.decision_policy_hash
    assert result.expires_at_ms > result.generated_at_ms


def test_carry_never_selects_partial():
    request = make_decision_request(goal="CARRY_CAPTURE")
    result = recommend_hedge(request, make_decision_context(), _default_policy(), ports=make_ports())
    assert result.recommendation == "FULL_HEDGE"
    assert result.recommendation != "PARTIAL_HEDGE"
    assert result.selected_proposal is not None
    assert result.selected_proposal.target_ratio == "1"


def test_decision_feasible_full():
    request = make_decision_request(goal="CARRY_CAPTURE")
    context = make_decision_context()
    policy = _default_policy()
    result = recommend_hedge(request, context, policy, ports=make_ports())
    _assert_full(result, target="1")
    # Default positive must select FULL; selected must not be None.
    assert result.selected_proposal is not None
    assert result.decision_policy_hash == decision_policy_hash(policy)
    assert result.context_refs.get("identity") == context.identity_snapshot_id
    assert result.context_refs.get("fcs") == context.fcs_snapshot_id
    # Six scenarios executed with zero future-carry credit lives in proposals.
    assert len(result.selected_proposal.scenarios) == 6
    assert {s.scenario_id for s in result.selected_proposal.scenarios} == {
        "UP_50", "UP_100", "DOWN_50", "BASIS_UP", "BASIS_DOWN", "FX_DOWN",
    }


def test_decision_feasible_partial():
    request = make_decision_request(goal="BALANCED")
    context = make_decision_context()
    policy = _default_policy()
    result = recommend_hedge(request, context, policy, ports=make_ports())
    assert result.recommendation == "PARTIAL_HEDGE"
    assert result.selected_proposal is not None
    assert result.selected_proposal.target_ratio == "0.25"
    assert result.selected_proposal.actual_ratio == "0.25"
    # Balanced requires net Carry > 0 explicitly.
    net = Decimal(str(result.selected_proposal.economics.net_carry_usd))
    assert net > 0
    assert result.decision_policy_hash == decision_policy_hash(policy)


def test_decision_feasible_no_hedge():
    request = make_decision_request(goal="DIRECTIONAL_SHORT")
    context = make_decision_context()
    policy = _default_policy()
    result = recommend_hedge(request, context, policy, ports=make_ports())
    assert result.recommendation == "NO_HEDGE"
    assert result.selected_proposal is not None
    assert result.selected_proposal.target_ratio == "0"
    assert result.selected_proposal.actual_ratio == "0"
    # h0 is contract-only read-only: no Spot leg is fabricated.
    assert result.selected_proposal.spot_net_qty == "0"
    assert result.selected_proposal.spot_venue is None
    assert "spot" not in dict(result.selected_proposal.quote_refs)
    # No SPOT_LONG leg is fabricated (R00 fake keeps guidance empty, real
    # R06a keeps a single FUTURES_SHORT read-only leg).
    assert all(
        not (isinstance(g, dict) and g.get("leg") == "SPOT_LONG")
        for g in tuple(result.selected_proposal.order_guidance or ())
    )
    assert len(tuple(result.selected_proposal.order_guidance or ())) <= 1
    assert result.decision_policy_hash == decision_policy_hash(policy)


def test_decision_no_capital_avoid():
    request = make_decision_request(goal="CARRY_CAPTURE", available_capital_usd="100")
    context = make_decision_context()
    policy = _default_policy()
    result = recommend_hedge(request, context, policy, ports=make_ports())
    assert result.recommendation == "AVOID"
    assert result.selected_proposal is None
    assert "INSUFFICIENT_CAPITAL" in tuple(result.reasons)
    assert result.decision_policy_hash == decision_policy_hash(policy)


def test_decision_unknown_data_insufficient():
    request = make_decision_request(goal="CARRY_CAPTURE")
    context = make_decision_context(funding_context=make_funding_context("unknown"))
    policy = _default_policy()
    result = recommend_hedge(request, context, policy, ports=make_ports())
    assert result.recommendation == "DATA_INSUFFICIENT"
    assert result.selected_proposal is None
    assert "FUNDING_SCHEDULE_UNKNOWN" in tuple(result.reasons)
    assert result.decision_policy_hash == decision_policy_hash(policy)


def _liquidation_proposal(request, context, target_ratio: str, policy, *, ports=None) -> RatioProposal:
    base = fake_ratio_proposal(request, context, target_ratio, policy, ports=ports)
    scenarios = []
    for s in base.scenarios:
        if s.scenario_id == "UP_100":
            scenarios.append(
                ScenarioResult(
                    scenario_id=s.scenario_id,
                    futures_move=s.futures_move,
                    spot_move=s.spot_move,
                    fx_shock=s.fx_shock,
                    status="INVALID_AFTER_LIQUIDATION",
                    net_pnl_usd=None,
                    loss_usd=None,
                    reasons=("LIQUIDATION_CROSSED",),
                )
            )
        else:
            scenarios.append(s)
    return RatioProposal(
        target_ratio=base.target_ratio,
        actual_ratio=base.actual_ratio,
        futures_contract_qty=base.futures_contract_qty,
        canonical_futures_qty=base.canonical_futures_qty,
        spot_net_qty=base.spot_net_qty,
        spot_venue=base.spot_venue,
        quote_refs=dict(base.quote_refs),
        economics=base.economics,
        scenarios=tuple(scenarios),
        execution_gate=base.execution_gate,
        risk_gate=GateResult("FAIL", ("SCENARIO_LIQUIDATION",), FIXTURE_NOW, {}),
        order_guidance=tuple(base.order_guidance),
    )


def test_decision_liquidation_manual_review():
    request = make_decision_request(goal="CARRY_CAPTURE")
    context = make_decision_context()
    policy = _default_policy()
    ports = make_ports(build_ratio_proposal=_liquidation_proposal)
    result = recommend_hedge(request, context, policy, ports=ports)
    assert result.recommendation == "MANUAL_REVIEW"
    assert result.selected_proposal is None
    assert "SCENARIO_LIQUIDATION" in tuple(result.reasons)
    assert result.decision_policy_hash == decision_policy_hash(policy)


def test_decision_ports_none_grants_no_credit():
    request = make_decision_request(goal="CARRY_CAPTURE")
    context = make_decision_context()
    policy = _default_policy()
    result = recommend_hedge(request, context, policy, ports=None)
    assert result.recommendation == "DATA_INSUFFICIENT"
    assert result.selected_proposal is None
    assert "IMPLEMENTATION_UNAVAILABLE" in tuple(result.reasons)


def test_decision_h0_needs_no_spot():
    # h0 exempts Spot: even with no venue quotes a directional NO_HEDGE stays
    # feasible and must not be masked as DATA_INSUFFICIENT.
    request = make_decision_request(goal="DIRECTIONAL_SHORT")
    context = make_decision_context(venue_quotes=())
    policy = _default_policy()
    result = recommend_hedge(request, context, policy, ports=make_ports())
    assert result.recommendation == "NO_HEDGE"
    assert result.selected_proposal is not None
    assert result.selected_proposal.target_ratio == "0"


def _collect_candidates(
    request, context, policy, ports
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """All candidates with elimination reasons (mirrors decision-cases.json)."""
    build = ports.build_ratio_proposal
    targets = {
        "CARRY_CAPTURE": ["1"],
        "DIRECTIONAL_SHORT": ["0", "0.25", "0.5", "0.75", "1"],
        "BALANCED": ["0.25", "0.5", "0.75", "1"],
    }[request.goal]
    rows: list[dict[str, Any]] = []
    for t in targets:
        try:
            prop = build(request, context, t, policy, ports=ports)
            rows.append(
                {
                    "target_ratio": t,
                    "actual_ratio": prop.actual_ratio,
                    "spot_venue": prop.spot_venue,
                    "execution": prop.execution_gate.status,
                    "risk": prop.risk_gate.status,
                    "economics": prop.economics.gate.status if prop.economics else "UNKNOWN",
                }
            )
        except Exception as exc:
            rows.append({"target_ratio": t, "error": str(exc)})
    return rows, [], []


def test_decision_cases_structure_matches_policy_hash():
    """decision-cases.json shape: input refs, all candidates, eliminations,
    selected result and policy hash (canonical recomputation, no layout SHA)."""
    policy = _default_policy()
    expected_hash = decision_policy_hash(policy)
    cases = [
        ("feasible_full", make_decision_request(goal="CARRY_CAPTURE"), make_decision_context()),
        ("feasible_partial", make_decision_request(goal="BALANCED"), make_decision_context()),
        (
            "feasible_no_hedge",
            make_decision_request(goal="DIRECTIONAL_SHORT"),
            make_decision_context(),
        ),
        (
            "no_capital_avoid",
            make_decision_request(goal="CARRY_CAPTURE", available_capital_usd="100"),
            make_decision_context(),
        ),
        (
            "unknown_insufficient",
            make_decision_request(goal="CARRY_CAPTURE"),
            make_decision_context(funding_context=make_funding_context("unknown")),
        ),
    ]
    # Liquidation case uses bound custom ports (same as manual-review test).
    liq_ports = make_ports(build_ratio_proposal=_liquidation_proposal)
    expectations = {
        "feasible_full": "FULL_HEDGE",
        "feasible_partial": "PARTIAL_HEDGE",
        "feasible_no_hedge": "NO_HEDGE",
        "no_capital_avoid": "AVOID",
        "unknown_insufficient": "DATA_INSUFFICIENT",
    }
    for name, req, ctx in cases:
        ports = make_ports()
        result = recommend_hedge(req, ctx, policy, ports=ports)
        assert result.recommendation == expectations[name], name
        assert result.decision_policy_hash == expected_hash, name
        assert result.request.symbol == req.symbol
        assert result.context_refs.get("identity") == ctx.identity_snapshot_id
        candidates, _, _ = _collect_candidates(req, ctx, policy, ports)
        assert len(candidates) == {"CARRY_CAPTURE": 1, "BALANCED": 4, "DIRECTIONAL_SHORT": 5}[req.goal], name
        # Selected target must be among candidates when present.
        if result.selected_proposal is not None:
            assert result.selected_proposal.target_ratio in [c.get("target_ratio") for c in candidates], name
    # Liquidation standalone (custom ports).
    liq_result = recommend_hedge(
        make_decision_request(goal="CARRY_CAPTURE"),
        make_decision_context(),
        policy,
        ports=liq_ports,
    )
    assert liq_result.recommendation == "MANUAL_REVIEW"
    assert liq_result.decision_policy_hash == expected_hash
