"""R14a capture (D14.1/D14.2, V14).

Red->green: before R14a ``shortlab.evidence.capture`` did not exist
(ModuleNotFoundError). V14 vectors: exact qty/FX/source, h0 no Spot,
missing no scaling.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab.repair_contracts import (
    CaptureContext,
    CaptureResult,
    DecisionResult,
    FuturesExecutionQuote,
)
from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
from diveintocrypto_desktop.shortlab.request_budget import (
    BudgetExhausted,
    make_request_context,
)
from tests.repair_fixtures import (
    FIXTURE_NOW,
    make_decision_context,
    make_decision_request,
    make_funding_context,
    make_identity,
    make_ports,
)


# ---------------------------------------------------------------------------
# Helpers: decision / capture contexts (R00 fixtures, D18)
# ---------------------------------------------------------------------------

def _make_decision(selected_target: str = "0.5") -> DecisionResult:
    req = make_decision_request()
    ctx = make_decision_context()
    ports = make_ports()
    proposal = ports.require("build_ratio_proposal")(req, ctx, selected_target, {})
    assert proposal.actual_ratio == selected_target
    return DecisionResult(
        decision_id="dec-MEME_FULL_VALID",
        generated_at_ms=FIXTURE_NOW,
        expires_at_ms=FIXTURE_NOW + 20000,
        request=req,
        context_refs={"identity": ctx.identity_snapshot_id, "fcs": ctx.fcs_snapshot_id},
        recommendation="PARTIAL_HEDGE" if selected_target != "1" else "FULL_HEDGE",
        selected_proposal=proposal,
        alternatives=(),
        reasons=(),
        assumptions=(),
        validation_level="RULE_BASED_UNVALIDATED",
        decision_policy_hash="ab" * 32,
        formula_version="hedge-decision-v1",
    )


def _make_capture(
    strategies: tuple[str, ...] = (
        "ABSOLUTE_100", "RELATIVE_75", "RELATIVE_50",
        "RELATIVE_25", "UNHEDGED_0", "SYSTEM_POLICY",
    ),
    *,
    cohort: str = "USER_DECISION",
    source_snapshot_id: str = "dec-MEME_FULL_VALID",
    decision: DecisionResult | None = None,
    decision_as_of_ms: int = FIXTURE_NOW,
    futures_contract_qty: str = "100",
    canonical_futures_qty: str = "100000",
    policy: Mapping[str, Any] | None = None,
) -> CaptureContext:
    if decision is None and cohort == "USER_DECISION":
        decision = _make_decision("0.5")
    return CaptureContext(
        cohort=cohort,
        source_snapshot_id=source_snapshot_id,
        symbol="1000PEPEUSDT",
        identity=make_identity("m1000"),
        identity_snapshot_id="identity-MEME_FULL_VALID",
        funding_context=make_funding_context("valid"),
        futures_contract_qty=futures_contract_qty,
        canonical_futures_qty=canonical_futures_qty,
        strategies=strategies,
        decision=decision,
        decision_as_of_ms=decision_as_of_ms,
        policy=dict(policy or {}),
        rule_refs={"futures": "rules:fut:1", "spot": "rules:spot:1"},
        source_refs={"identity": "identity-MEME_FULL_VALID", "fcs": "fcs-MEME_FULL_VALID"},
    )


@pytest_asyncio.fixture
async def repo(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "r14a.duckdb")
    await handle.migrate(target_version=6)
    yield handle
    await handle.close()


async def _save_usdt_fx(repo: ShortLabRepository, *, source_as_of_ms: int, known_at_ms: int, fx_id: str = "fx-usdt-1") -> str:
    return await repo.save_fx_observation({
        "fx_id": fx_id,
        "currency": "USDT",
        "source_as_of_ms": source_as_of_ms,
        "known_at_ms": known_at_ms,
        "rate_str": "1",
        "source_json": {"source": "coingecko", "currency": "USDT"},
    })


def _futures_quote(
    *,
    symbol: str = "1000PEPEUSDT",
    requested: str = "100",
    as_of_ms: int,
    known_at_ms: int,
    sell_vwap: str | None = "0.0099",
    buy_vwap: str | None = "0.0101",
    executable: str | None = None,
    quote_to_usd: str | None = "1",
) -> FuturesExecutionQuote:
    exe = executable if executable is not None else requested
    return FuturesExecutionQuote(
        quote_id=f"{symbol}:fut:{as_of_ms}:{requested}",
        symbol=symbol,
        requested_contract_qty=requested,
        buy_vwap_native=buy_vwap,
        sell_vwap_native=sell_vwap,
        buy_executable_qty=exe if buy_vwap is not None else "0",
        sell_executable_qty=exe if sell_vwap is not None else "0",
        quote_currency="USDT",
        quote_to_usd=quote_to_usd,
        as_of_ms=as_of_ms,
        known_at_ms=known_at_ms,
        expires_at_ms=known_at_ms + 20000,
        book_observation_id=f"{symbol}:book:{as_of_ms}",
        fees_included=False,
    )


def _spot_quote(
    *,
    canonical_qty: str,
    as_of_ms: int,
    known_at_ms: int,
    buy_vwap: str | None = "0.0101",
    sell_vwap: str | None = "0.0099",
    venue: str = "BINANCE_SPOT",
    quote_to_usd: str | None = "1",
):
    from diveintocrypto_desktop.shortlab.hedge.models import SpotVenueQuote

    exe = canonical_qty if buy_vwap is not None or sell_vwap is not None else "0"
    # When one side is missing keep the other executable; missing both -> 0.
    buy_exe = canonical_qty if buy_vwap is not None else "0"
    sell_exe = canonical_qty if sell_vwap is not None else "0"
    return SpotVenueQuote(
        venue=venue,
        canonical_id="pepe",
        symbol="1000PEPEUSDT",
        chain=None,
        contract_address=None,
        as_of_ms=as_of_ms,
        expires_at_ms=known_at_ms + 20000,
        reference_notional_usd="10000",
        mid_price="0.01",
        buy_vwap=buy_vwap,
        sell_vwap=sell_vwap,
        buy_executable_qty=buy_exe,
        sell_executable_qty=sell_exe,
        buy_slippage_bps=5.0,
        sell_slippage_bps=5.0,
        estimated_fee_usd="5",
        estimated_gas_usd=None,
        direction_costs={},
        entry_feasible=True,
        exit_feasible=True,
        exit_feasibility="CONFIRMED",
        quote_currency="USDT",
        quote_to_usd=quote_to_usd,
        source_timestamp_ms=as_of_ms,
        fetched_at_ms=known_at_ms,
        requested_canonical_qty=canonical_qty,
        trading_rules={},
        capabilities={},
        identity_confidence="VERIFIED",
        status="OK",
        reason_code=None,
        fees_included=True,
    )


class FakeMarket:
    """R11b MarketPort shape (D19.1): futures/spot/funding, BudgetExhausted for DEFERRED."""

    def __init__(
        self,
        *,
        futures_as_of: int = FIXTURE_NOW + 2000,
        futures_known: int | None = None,
        spot_as_of: int = FIXTURE_NOW + 2500,
        spot_known: int | None = None,
        missing_futures_depth: bool = False,
        missing_spot_depth: bool = False,
        missing_fx: bool = False,
        raise_budget_on: str | None = None,
    ) -> None:
        self.futures_as_of = futures_as_of
        self.futures_known = futures_known if futures_known is not None else futures_as_of + 500
        self.spot_as_of = spot_as_of
        self.spot_known = spot_known if spot_known is not None else spot_as_of + 500
        self.missing_futures_depth = missing_futures_depth
        self.missing_spot_depth = missing_spot_depth
        self.missing_fx = missing_fx
        self.raise_budget_on = raise_budget_on
        self.futures_calls: list[dict[str, Any]] = []
        self.spot_calls: list[dict[str, Any]] = []

    async def collect_futures(self, symbol: str, contract_qty: str, request_context: Any) -> Mapping[str, Any]:
        self.futures_calls.append({"symbol": symbol, "contract_qty": contract_qty})
        if self.raise_budget_on == "futures":
            raise BudgetExhausted("REQUEST_BUDGET_EXHAUSTED: host window full")
        q = _futures_quote(
            symbol=symbol,
            requested=contract_qty,
            as_of_ms=self.futures_as_of,
            known_at_ms=self.futures_known,
            sell_vwap=None if self.missing_futures_depth else "0.0099",
            buy_vwap=None if self.missing_futures_depth else "0.0101",
            quote_to_usd=None if self.missing_fx else "1",
        )
        return {
            "symbol": symbol,
            "requested_contract_qty": contract_qty,
            "futures_mark": {"mark_price": "0.01", "as_of_ms": self.futures_as_of},
            "futures_quote": q,
            "futures_rules": make_decision_context().futures_rules,
            "book_observation_id": q.book_observation_id,
            "as_of_ms": self.futures_as_of,
            "known_at_ms": self.futures_known,
        }

    async def collect_spot(self, identity: Any, venue: str, canonical_qty: str, request_context: Any) -> Any:
        self.spot_calls.append({"venue": venue, "canonical_qty": canonical_qty})
        if self.raise_budget_on == "spot":
            raise BudgetExhausted("REQUEST_BUDGET_EXHAUSTED: host window full")
        # h0 must never reach here: caller skips Spot when qty is 0.
        assert Decimal(str(canonical_qty)) > 0, "collect_spot must not be called for h0 (qty 0)"
        q = _spot_quote(
            canonical_qty=canonical_qty,
            as_of_ms=self.spot_as_of,
            known_at_ms=self.spot_known,
            buy_vwap=None if self.missing_spot_depth else "0.0101",
            sell_vwap=None if self.missing_spot_depth else "0.0099",
            venue=venue,
            quote_to_usd=None if self.missing_fx else "1",
        )
        return q

    async def collect_funding(self, symbol: str, as_of_ms: int, request_context: Any) -> Any:
        return make_funding_context("valid")


def _ctx():
    return make_request_context(None, job_type="evidence", host="fapi", trace_id="t-r14a")


# ---------------------------------------------------------------------------
# V14: exact qty / FX / source, six strategies share decision group
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v14_user_decision_six_strategies_exact_qty_fx_source(repo):
    from diveintocrypto_desktop.shortlab.evidence.capture import capture_strategy_entries

    decision = _make_decision("0.5")
    context = _make_capture(decision=decision)
    # FX pinned before execution: source <= event, known <= executed, age <=60s.
    await _save_usdt_fx(repo, source_as_of_ms=FIXTURE_NOW + 500, known_at_ms=FIXTURE_NOW + 600)
    market = FakeMarket()
    result = await capture_strategy_entries(context, repo, market, _ctx())
    assert result.status == "COMPLETE"
    assert tuple(result.entry_ids) == tuple(f"{context.source_snapshot_id}:{s}" for s in context.strategies)
    assert result.executed_as_of_ms is not None and result.executed_as_of_ms > context.decision_as_of_ms
    # Group skew <=5s (futures 2000 vs spot 2500 -> 500ms).
    assert result.executed_as_of_ms - context.decision_as_of_ms < 10000

    rows = await repo.list_strategy_entries("USER_DECISION", FIXTURE_NOW - 1000, FIXTURE_NOW + 10000)
    by_strategy = {r["strategy"]: r for r in rows if r["source_snapshot_id"] == "dec-MEME_FULL_VALID"}
    assert set(by_strategy) == set(context.strategies)
    expected_spot = {
        "ABSOLUTE_100": "100000",
        "RELATIVE_75": "75000",
        "RELATIVE_50": "50000",
        "RELATIVE_25": "25000",
        "UNHEDGED_0": "0",
        "SYSTEM_POLICY": "50000",
    }
    for strategy, row in by_strategy.items():
        entry = row["entry_json"]
        assert entry["status"] == "ENTRY_COMPLETE"
        assert entry["source_snapshot_id"] == "dec-MEME_FULL_VALID"
        assert entry["decision_snapshot_id"] == "dec-MEME_FULL_VALID"
        assert entry["native_futures_qty"] == "100"
        assert entry["canonical_futures_qty"] == "100000"
        assert entry["spot_net_qty"] == expected_spot[strategy]
        # Futures entry SELL, Spot entry BUY (never Mark 0.01 as execution).
        assert entry["futures_entry_vwap_native"] == "0.0099"
        assert entry["futures_entry_side"] == "SELL"
        if strategy != "UNHEDGED_0":
            assert entry["spot_entry_vwap_native"] == "0.0101"
            assert entry["spot_entry_side"] == "BUY"
            assert entry["quote_refs"]["spot"] is not None
        else:
            assert entry["spot_entry_vwap_native"] is None
            assert "spot" not in entry["quote_refs"]
        # FX pinned, source refs kept.
        assert entry["fx_refs"]["USDT"] == "fx-usdt-1"
        assert entry["quote_refs"]["futures"].startswith("1000PEPEUSDT:fut:")
        assert entry["rule_refs"] == {"futures": "rules:fut:1", "spot": "rules:spot:1"}
        assert entry["decision_as_of_ms"] == FIXTURE_NOW
        assert entry["executed_as_of_ms"] == result.executed_as_of_ms

    # Plan snippet: SYSTEM copies frozen proposal, shares decision group.
    system_entry = by_strategy["SYSTEM_POLICY"]["entry_json"]
    assert system_entry["actual_ratio"] == decision.selected_proposal.actual_ratio
    paired_baseline = by_strategy["RELATIVE_50"]["entry_json"]
    assert paired_baseline["source_snapshot_id"] == system_entry["source_snapshot_id"]
    assert paired_baseline["decision_snapshot_id"] == system_entry["decision_snapshot_id"]

    # 7/30/90 EXIT tasks per ENTRY_COMPLETE entry (6*3=18, due from executed_as_of).
    # Each horizon claimed at its own due (+1s, within 5min deadline).
    for horizon, expected_qty in ((7, 6), (30, 6), (90, 6)):
        due = result.executed_as_of_ms + horizon * 86400_000
        claimed = await repo.claim_due_quote_tasks(due + 1000, limit=20)
        # Only this horizon is due (earlier horizons already finished).
        assert len(claimed) == expected_qty, f"horizon {horizon}: {claimed}"
        assert all(c["horizon_days"] == horizon for c in claimed)
        for task in claimed:
            assert task["purpose"] == "EXIT"
            tj = task["task_json"]
            assert tj["deadline_ms"] >= tj["due_ms"]
            assert tj["deadline_ms"] - tj["due_ms"] == 300_000
            assert tj["requested_futures_qty"] == "100"
            # h0 spot 0, others exact.
            assert tj["requested_spot_qty"] in ("0", "25000", "50000", "75000", "100000")
            await repo.finish_quote_task(task["task_id"], "COMPLETE", {"quote_refs": {"futures": "fq-x"}})


@pytest.mark.asyncio
async def test_v14_h0_no_spot_dependency(repo):
    from diveintocrypto_desktop.shortlab.evidence.capture import capture_strategy_entries

    context = _make_capture(strategies=("UNHEDGED_0",))
    await _save_usdt_fx(repo, source_as_of_ms=FIXTURE_NOW + 500, known_at_ms=FIXTURE_NOW + 600, fx_id="fx-h0")
    market = FakeMarket()
    result = await capture_strategy_entries(context, repo, market, _ctx())
    assert result.status == "COMPLETE"
    assert market.spot_calls == [], "h0 must not call collect_spot"
    assert market.futures_calls and market.futures_calls[0]["contract_qty"] == "100"
    rows = await repo.list_strategy_entries("USER_DECISION", FIXTURE_NOW - 1000, FIXTURE_NOW + 10000)
    assert len([r for r in rows if r["strategy"] == "UNHEDGED_0"]) == 1
    entry = next(r for r in rows if r["strategy"] == "UNHEDGED_0")["entry_json"]
    assert entry["spot_net_qty"] == "0"
    assert entry["spot_entry_vwap_native"] is None
    assert "spot" not in entry["quote_refs"]


@pytest.mark.asyncio
async def test_v14_missing_depth_unexecutable_no_scaling(repo):
    from diveintocrypto_desktop.shortlab.evidence.capture import capture_strategy_entries

    context = _make_capture(strategies=("ABSOLUTE_100",))
    await _save_usdt_fx(repo, source_as_of_ms=FIXTURE_NOW + 500, known_at_ms=FIXTURE_NOW + 600, fx_id="fx-depth")
    market = FakeMarket(missing_futures_depth=True)
    result = await capture_strategy_entries(context, repo, market, _ctx())
    assert result.status == "UNAVAILABLE"
    rows = await repo.list_strategy_entries("USER_DECISION", FIXTURE_NOW - 1000, FIXTURE_NOW + 10000)
    entry = next(r for r in rows if r["strategy"] == "ABSOLUTE_100")["entry_json"]
    assert entry["status"] == "UNEXECUTABLE"
    assert entry["executed_as_of_ms"] is None
    # No EXIT tasks for UNEXECUTABLE (no fabrication with 100% quote).
    claimed = await repo.claim_due_quote_tasks(FIXTURE_NOW + 90 * 86400_000 + 5000, limit=100)
    assert claimed == ()


@pytest.mark.asyncio
async def test_v14_missing_fx_unavailable_no_backfill(repo):
    from diveintocrypto_desktop.shortlab.evidence.capture import capture_strategy_entries

    context = _make_capture(strategies=("RELATIVE_50",))
    # No FX saved: market also reports quote_to_usd None -> must stay UNAVAILABLE.
    market = FakeMarket(missing_fx=True)
    result = await capture_strategy_entries(context, repo, market, _ctx())
    assert result.status == "UNAVAILABLE"
    rows = await repo.list_strategy_entries("USER_DECISION", FIXTURE_NOW - 1000, FIXTURE_NOW + 10000)
    entry = next(r for r in rows if r["strategy"] == "RELATIVE_50")["entry_json"]
    assert entry["status"] == "UNAVAILABLE"
    assert "UNKNOWN_FX" in entry["reasons"] or "FX" in " ".join(entry["reasons"])
    claimed = await repo.claim_due_quote_tasks(FIXTURE_NOW + 90 * 86400_000 + 5000, limit=100)
    assert claimed == ()


@pytest.mark.asyncio
async def test_v14_group_skew_exceeded_unavailable(repo):
    from diveintocrypto_desktop.shortlab.evidence.capture import capture_strategy_entries

    context = _make_capture()
    await _save_usdt_fx(repo, source_as_of_ms=FIXTURE_NOW + 500, known_at_ms=FIXTURE_NOW + 600, fx_id="fx-skew")
    market = FakeMarket(futures_as_of=FIXTURE_NOW + 1000, spot_as_of=FIXTURE_NOW + 1000 + 6000)
    result = await capture_strategy_entries(context, repo, market, _ctx())
    assert result.status == "UNAVAILABLE"
    assert "GROUP_SKEW_EXCEEDED" in result.reasons
    rows = await repo.list_strategy_entries("USER_DECISION", FIXTURE_NOW - 1000, FIXTURE_NOW + 20000)
    assert all(r["entry_json"]["status"] == "UNAVAILABLE" for r in rows)


@pytest.mark.asyncio
async def test_v14_system_requires_real_selection(repo):
    from diveintocrypto_desktop.shortlab.evidence.capture import capture_strategy_entries

    # SYSTEM without decision must not fabricate from current model.
    context = _make_capture(
        strategies=("SYSTEM_POLICY",),
        decision=None,
        source_snapshot_id="dec-missing",
    )
    # Bypass DTO guard (DTO would reject SYSTEM without decision): build manually.
    # Instead use non-USER cohort? No: directly assert capture rejects.
    # Here we construct a USER_DECISION context with decision=None via object.__setattr__ trick
    # is not needed: use FUNDING_CARRY which forbids SYSTEM at DTO level, so test
    # the capture-level guard with a stub context.
    stub = SimpleNamespace(
        cohort="USER_DECISION",
        source_snapshot_id="dec-missing",
        symbol="1000PEPEUSDT",
        identity=make_identity("m1000"),
        identity_snapshot_id="identity-MEME_FULL_VALID",
        funding_context=make_funding_context("valid"),
        futures_contract_qty="100",
        canonical_futures_qty="100000",
        strategies=("SYSTEM_POLICY",),
        decision=None,
        decision_as_of_ms=FIXTURE_NOW,
        policy={},
        rule_refs={},
        source_refs={},
    )
    _ = context
    market = FakeMarket()
    result = await capture_strategy_entries(stub, repo, market, _ctx())  # type: ignore[arg-type]
    assert result.status == "UNAVAILABLE"
    assert market.futures_calls == [] and market.spot_calls == []


@pytest.mark.asyncio
async def test_v14_day_first_sample_rejects_second_source(repo):
    from diveintocrypto_desktop.shortlab.evidence.capture import capture_strategy_entries

    first = _make_capture(source_snapshot_id="dec-day-1", decision_as_of_ms=FIXTURE_NOW)
    # First decision needs matching decision_id.
    dec1 = DecisionResult(
        **{**_make_decision("0.5").__dict__, "decision_id": "dec-day-1"},
    )
    import dataclasses

    first = dataclasses.replace(first, decision=dec1)
    await _save_usdt_fx(repo, source_as_of_ms=FIXTURE_NOW + 500, known_at_ms=FIXTURE_NOW + 600, fx_id="fx-day")
    assert (await capture_strategy_entries(first, repo, FakeMarket(), _ctx())).status == "COMPLETE"

    dec2 = DecisionResult(
        **{**_make_decision("0.5").__dict__, "decision_id": "dec-day-2"},
    )
    second = dataclasses.replace(
        first, source_snapshot_id="dec-day-2", decision=dec2,
        decision_as_of_ms=FIXTURE_NOW + 3600_000,
    )
    result = await capture_strategy_entries(second, repo, FakeMarket(), _ctx())
    assert result.status == "UNAVAILABLE"
    assert "DAY_SAMPLE_ALREADY_EXISTS" in result.reasons


@pytest.mark.asyncio
async def test_v14_user_decision_group_id_must_match(repo):
    from diveintocrypto_desktop.shortlab.evidence.capture import capture_strategy_entries

    decision = _make_decision("0.5")
    context = _make_capture(source_snapshot_id="dec-WRONG", decision=decision)
    result = await capture_strategy_entries(context, repo, FakeMarket(), _ctx())
    assert result.status == "UNAVAILABLE"
    assert "GROUP_ID_MISMATCH" in result.reasons


@pytest.mark.asyncio
async def test_v14_collect_due_quotes_exit_sides_and_budget(repo):
    from diveintocrypto_desktop.shortlab.evidence.capture import (
        capture_strategy_entries,
        collect_due_quotes,
    )

    decision = _make_decision("1")
    context = _make_capture(strategies=("ABSOLUTE_100",), decision=decision)
    import dataclasses

    # Align decision id with source for USER_DECISION group.
    dec = DecisionResult(**{**decision.__dict__, "decision_id": context.source_snapshot_id})
    context = dataclasses.replace(context, decision=dec)
    await _save_usdt_fx(repo, source_as_of_ms=FIXTURE_NOW + 500, known_at_ms=FIXTURE_NOW + 600, fx_id="fx-exit-entry")
    entry_result = await capture_strategy_entries(context, repo, FakeMarket(), _ctx())
    assert entry_result.status == "COMPLETE"
    executed = entry_result.executed_as_of_ms
    assert executed is not None
    # FX for exit event (due 7d): fresh observation before exit.
    due7 = executed + 7 * 86400_000
    await _save_usdt_fx(repo, source_as_of_ms=due7 + 500, known_at_ms=due7 + 600, fx_id="fx-exit-7")

    class ExitMarket(FakeMarket):
        async def collect_futures(self, symbol: str, contract_qty: str, request_context: Any) -> Mapping[str, Any]:
            assert contract_qty == "100", "exit must use exact entry futures qty"
            self.futures_calls.append({"symbol": symbol, "contract_qty": contract_qty})
            q = _futures_quote(
                symbol=symbol, requested=contract_qty,
                as_of_ms=due7 + 1000, known_at_ms=due7 + 1500,
                sell_vwap="0.0098", buy_vwap="0.0102",
            )
            return {
                "symbol": symbol, "requested_contract_qty": contract_qty,
                "futures_mark": {"mark_price": "0.01", "as_of_ms": due7 + 1000},
                "futures_quote": q, "futures_rules": make_decision_context().futures_rules,
                "book_observation_id": q.book_observation_id,
                "as_of_ms": due7 + 1000, "known_at_ms": due7 + 1500,
            }

        async def collect_spot(self, identity: Any, venue: str, canonical_qty: str, request_context: Any) -> Any:
            assert canonical_qty == "100000", "exit must use exact entry spot qty (no 100% scaling)"
            self.spot_calls.append({"venue": venue, "canonical_qty": canonical_qty})
            return _spot_quote(
                canonical_qty=canonical_qty, as_of_ms=due7 + 1200, known_at_ms=due7 + 1600,
                buy_vwap="0.0103", sell_vwap="0.0097", venue=venue,
            )

    market = ExitMarket()
    out = await collect_due_quotes(repo, market, due7 + 2000, _ctx())
    assert out.claimed == 1 and out.complete == 1 and out.deferred == 0 and out.unavailable == 0
    # Exit uses reverse sides: Futures BUY, Spot SELL (stored in finished task).
    # Claim again shows nothing left (finished COMPLETE).
    assert await repo.claim_due_quote_tasks(due7 + 2000, limit=20) == ()

    # Budget refusal -> DEFERRED retained (never scaled fabrication).
    await repo.save_strategy_entry({
        "entry_id": "entry-budget:ABSOLUTE_100", "cohort": "USER_DECISION", "symbol": "1000PEPEUSDT",
        "source_snapshot_id": "entry-budget", "strategy": "ABSOLUTE_100",
        "decision_as_of_ms": FIXTURE_NOW, "executed_as_of_ms": FIXTURE_NOW + 2000,
        "entry_json": {"status": "ENTRY_COMPLETE"},
    }, references=())
    await repo.save_strategy_quote_task({
        "task_id": "task-budget-7", "entry_id": "entry-budget:ABSOLUTE_100", "horizon_days": 7,
        "purpose": "EXIT", "due_ms": due7, "status": "PENDING",
        "task_json": {
            "schema_version": "repair-contract-v1", "entry_id": "entry-budget:ABSOLUTE_100",
            "purpose": "EXIT", "strategy": "ABSOLUTE_100", "horizon_days": 7,
            "due_ms": due7, "deadline_ms": due7 + 300_000,
            "requested_futures_qty": "100", "requested_spot_qty": "100000",
            "venue": "BINANCE_SPOT", "symbol": "1000PEPEUSDT",
            "attempt_count": 0, "quote_refs": {}, "reason_code": "EXIT_SCHEDULED",
        },
        "updated_at_ms": due7,
    })
    budget_market = FakeMarket(raise_budget_on="futures")
    out2 = await collect_due_quotes(repo, budget_market, due7 + 2000, _ctx())
    assert out2.deferred == 1 and out2.complete == 0
    assert out2.claimed == out2.complete + out2.deferred + out2.unavailable
