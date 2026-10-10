"""H10 independent hedge Evidence domain (design B39, plan H10, AC19 AC20).

Covers the H10 minimal scene table (no private schema fork; all DTOs from
``hedge.models``):

- complete / not-due / missing price-or-quote / delisted four states;
- capital denominator / two-leg cost / funding consistency, directional
  ``sl_forward_outcome`` untouched;
- same snapshot different cost/version mints a new row (no overwrite);
- cross-30d referenced quote/simulation still queryable (pin retention).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab.hedge.models import (
    HedgeEvidenceSummary,
    HedgeOutcome,
    HistoricalFundingEvent,
    HistoricalLifecycle,
    HistoricalPriceBar,
)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
NOW = 1_760_000_000_000

GOLDEN_FCS = "72eca2e7214ac6dfdf908541d1dd185c6b6ce882e30de3297b495c8f1d61c136"
GOLDEN_COST = "9915b1468e5d0e4fc02ee71b4883c5445351928b0e7509f0dc4e6727fcca37dc"


# ---------------------------------------------------------------------------
# Fake HistoricalMarketProvider (same protocol, no live state).
# ---------------------------------------------------------------------------


class FakeMarketProvider:
    """In-memory HistoricalMarketProvider for H10 domain tests."""

    def __init__(self) -> None:
        self.bars: dict[str, list[Any]] = {}
        self.fundings: dict[str, list[Any]] = {}
        self.lifecycles: dict[str, Any] = {}
        self.quotes: list[dict[str, Any]] = []
        # CR16: true MARK history (price_basis=MARK) + call proof that the
        # grader really executed the MARK reader (never TRADE-as-MARK).
        self.mark_bars: dict[str, list[Any]] = {}
        self.read_mark_calls: list[tuple[Any, ...]] = []

    def add_bar(self, bar: Any) -> None:
        symbol = bar.symbol if hasattr(bar, "symbol") else bar["symbol"]
        self.bars.setdefault(str(symbol), []).append(bar)

    def add_funding(self, event: Any) -> None:
        symbol = event.symbol if hasattr(event, "symbol") else event["symbol"]
        self.fundings.setdefault(str(symbol), []).append(event)

    def set_lifecycle(self, symbol: str, lifecycle: Any | None) -> None:
        self.lifecycles[str(symbol)] = lifecycle

    def add_quote(self, quote: dict[str, Any]) -> None:
        self.quotes.append(dict(quote))

    def add_mark_bar(self, bar: Any) -> None:
        symbol = bar.symbol if hasattr(bar, "symbol") else bar["symbol"]
        self.mark_bars.setdefault(str(symbol), []).append(bar)

    async def read_frozen_quote(self, snapshot_id: str) -> Any | None:
        for quote in self.quotes:
            if quote.get("snapshot_id") == snapshot_id:
                return dict(quote)
        return None

    async def find_frozen_quote(
        self,
        canonical_id: str,
        venue: str,
        canonical_qty: str,
        at_ms: int,
        max_skew_ms: int = 5000,
    ) -> Any | None:
        want = Decimal(str(canonical_qty))
        best: dict[str, Any] | None = None
        for quote in self.quotes:
            if str(quote.get("canonical_id")) != str(canonical_id):
                continue
            if str(quote.get("venue")) != str(venue):
                continue
            try:
                got = Decimal(str(quote.get("requested_canonical_qty")))
            except Exception:
                continue
            if got != want:
                continue
            as_of = int(quote.get("as_of_ms"))
            if not (at_ms - int(max_skew_ms) <= as_of <= at_ms):
                continue
            if best is None or (as_of, str(quote.get("snapshot_id"))) > (
                int(best["as_of_ms"]),
                str(best.get("snapshot_id")),
            ):
                best = quote
        return dict(best) if best is not None else None

    async def read_price_bars(
        self, symbol: str, start_ms: int, end_ms: int, request_context: Any
    ) -> tuple[Any, ...]:
        out = []
        for bar in self.bars.get(str(symbol), []):
            open_ms = bar.open_ms if hasattr(bar, "open_ms") else bar["open_ms"]
            if int(start_ms) <= int(open_ms) <= int(end_ms):
                out.append(bar)
        return tuple(sorted(
            out,
            key=lambda b: int(b.open_ms if hasattr(b, "open_ms") else b["open_ms"]),
        ))

    async def read_settled_funding(
        self, symbol: str, start_ms: int, end_ms: int, request_context: Any
    ) -> tuple[Any, ...]:
        out = []
        for event in self.fundings.get(str(symbol), []):
            t = event.funding_time_ms if hasattr(event, "funding_time_ms") else event["funding_time_ms"]
            if int(start_ms) < int(t) <= int(end_ms):
                out.append(event)
        return tuple(sorted(
            out,
            key=lambda e: int(e.funding_time_ms if hasattr(e, "funding_time_ms") else e["funding_time_ms"]),
        ))

    async def read_lifecycle(self, symbol: str, cutoff_ms: int) -> Any | None:
        return self.lifecycles.get(str(symbol))

    async def read_mark_price_bars(
        self, symbol: str, start_ms: int, end_ms: int, request_context: Any = None
    ) -> tuple[Any, ...]:
        # Call proof for CR16: every grading that reaches settlement must
        # invoke this reader; tests assert len(read_mark_calls) >= 1.
        self.read_mark_calls.append((str(symbol), int(start_ms), int(end_ms)))
        out = []
        for bar in self.mark_bars.get(str(symbol), []):
            open_ms = bar.open_ms if hasattr(bar, "open_ms") else bar["open_ms"]
            close_ms = (
                bar.close_ms if hasattr(bar, "close_ms") else bar.get("close_ms")
            )
            if close_ms is None:
                close_ms = int(open_ms) + HOUR_MS
            if int(start_ms) <= int(open_ms) and int(close_ms) <= int(end_ms):
                out.append(bar)
        return tuple(sorted(
            out,
            key=lambda b: int(b.open_ms if hasattr(b, "open_ms") else b["open_ms"]),
        ))


def _bar(symbol: str, open_ms: int, price: str, fx: str | None = "1") -> HistoricalPriceBar:
    return HistoricalPriceBar(
        symbol=symbol,
        open_ms=int(open_ms),
        close_ms=int(open_ms) + HOUR_MS,
        native_open=str(price),
        native_high=str(price),
        native_low=str(price),
        native_close=str(price),
        quote_asset="USDT",
        fx_to_usd=fx,
        source="test-bars",
        known_at_ms=int(open_ms) + HOUR_MS,
    )


def _mark_bar(
    symbol: str,
    open_ms: int,
    price: str,
    fx: str | None = "1",
    basis: str = "MARK",
    known_at_ms: int | None = None,
) -> HistoricalPriceBar:
    """True MARK bar helper (CR16: price_basis=MARK, MARK source)."""
    return HistoricalPriceBar(
        symbol=symbol,
        open_ms=int(open_ms),
        close_ms=int(open_ms) + HOUR_MS,
        native_open=str(price),
        native_high=str(price),
        native_low=str(price),
        native_close=str(price),
        quote_asset="USDT",
        fx_to_usd=fx,
        source="binance:fapi/markPriceKlines:1h",
        known_at_ms=int(known_at_ms) if known_at_ms is not None else int(open_ms) + HOUR_MS,
        price_basis=basis,
    )


def _funding(symbol: str, t_ms: int, rate: str = "0.0001",
             mark: str | None = "100", fx: str | None = "1") -> HistoricalFundingEvent:
    return HistoricalFundingEvent(
        symbol=symbol,
        funding_time_ms=int(t_ms),
        rate=str(rate),
        mark_price=mark,
        quote_asset="USDT",
        fx_to_usd=fx,
        source="test-funding",
        known_at_ms=int(t_ms) + 1_000,
    )


def _quote(
    snapshot_id: str,
    canonical_id: str,
    venue: str,
    as_of_ms: int,
    qty: str,
    price: str | None,
    fx: str | None = "1",
    fetched_offset_ms: int = 1_000,
) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot_id,
        "canonical_id": canonical_id,
        "venue": venue,
        "as_of_ms": int(as_of_ms),
        "fetched_at_ms": int(as_of_ms) + fetched_offset_ms,
        "buy_vwap": price,
        "sell_vwap": price,
        "mid_price": price,
        "quote_to_usd": fx,
        "requested_canonical_qty": str(qty),
        "estimated_gas_usd": "0.5",
        "estimated_fee_usd": None,
        "quote_currency": "USDT",
        "status": "OK",
    }


@pytest_asyncio.fixture
async def repo(tmp_path):
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

    handle = await ShortLabRepository.open(tmp_path / "h10.duckdb")
    await handle.migrate(target_version=4)
    await handle.migrate(target_version=5)
    yield handle
    await handle.close()


def _fcs_row(snapshot_id: str, symbol: str = "BTCUSDT",
             canonical: str = "bitcoin", as_of: int = NOW,
             venue: str = "BINANCE_SPOT") -> dict:
    return {
        "snapshot_id": snapshot_id,
        "symbol": symbol,
        "canonical_id": canonical,
        "as_of_ms": int(as_of),
        "fcs_version": "fcs_v1",
        "fcs_config_hash": GOLDEN_FCS,
        "reference_notional_usd": 10000.0,
        "fcs": 82.5,
        "module_scores_json": {},
        "funding_metrics_json": {"funding_30d": "0.012", "history_class": "FULL_90D"},
        "venue_summary_json": {"venue": venue, "reference_notional_usd": "10000"},
        "risk_json": {"history_class": "FULL_90D", "identity": {"contract_multiplier":"1", "multiplier_source":"MANUAL"}},
        "readiness": "READY",
        "reasons_json": [],
        "created_at_ms": int(as_of),
    }


async def _save_fcs(repo, snapshot_id: str, as_of: int = NOW,
                    venue: str = "BINANCE_SPOT") -> None:
    await repo.save_funding_capture_snapshot(_fcs_row(snapshot_id, as_of=as_of, venue=venue))


async def _save_venue(repo, snapshot_id: str, canonical: str,
                      venue: str, as_of: int, qty: str) -> None:
    await repo.save_spot_venue_snapshot({
        "snapshot_id": snapshot_id,
        "canonical_id": canonical,
        "venue": venue,
        "as_of_ms": int(as_of),
        "fetched_at_ms": int(as_of) + 1_000,
        "reference_notional_usd": 10000.0,
        "quote_json": {
            "requested_canonical_qty": str(qty),
            "buy_vwap": "100",
            "sell_vwap": "100",
            "mid_price": "100",
            "quote_to_usd": "1",
        },
        "status": "OK",
    })


def _complete_fake(
    snapshot_as_of: int,
    due_ms: int,
    entry_qty: str = "100",
    entry_price: str = "100",
    exit_price: str = "110",
    venue: str = "BINANCE_SPOT",
) -> FakeMarketProvider:
    """Bars 100->110 plus complete 8h funding and matching entry/exit quotes."""
    fake = FakeMarketProvider()
    # Daily bars across the window (gap 24h <= 25h keeps drawdown complete).
    t = int(snapshot_as_of)
    # Entry bar at snapshot, exit bar at due; linear interpolation between.
    steps = max(1, (int(due_ms) - int(snapshot_as_of)) // DAY_MS)
    for i in range(steps + 1):
        open_ms = int(snapshot_as_of) + i * DAY_MS
        if open_ms > int(due_ms):
            break
        frac = (open_ms - int(snapshot_as_of)) / max(1, int(due_ms) - int(snapshot_as_of))
        price = str(Decimal(entry_price) + (Decimal(exit_price) - Decimal(entry_price)) * Decimal(str(frac)))
        fake.add_bar(_bar("BTCUSDT", open_ms, price))
    # Complete 8h funding.
    step = 8 * HOUR_MS
    # Use then-mark interpolated similarly (100->110).
    cursor = int(snapshot_as_of) + step
    while cursor <= int(due_ms):
        frac = (cursor - int(snapshot_as_of)) / max(1, int(due_ms) - int(snapshot_as_of))
        mark = str(Decimal("100") + (Decimal("110") - Decimal("100")) * Decimal(str(frac)))
        fake.add_funding(_funding("BTCUSDT", cursor, "0.0001", mark, "1"))
        cursor += step
    fake.add_quote(_quote("q-entry", "bitcoin", venue, int(snapshot_as_of), entry_qty, entry_price))
    fake.add_quote(_quote("q-exit", "bitcoin", venue, int(due_ms), entry_qty, exit_price))
    return fake


def _directional_count(repo) -> int:
    cur = repo._con.execute("SELECT count(*) FROM sl_forward_outcome")
    return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Four states: COMPLETE / PENDING / UNAVAILABLE / CENSORED.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_four_states_complete_pending_missing_quote_delisted(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    # COMPLETE: 7D horizon, entry 100 -> exit 110.
    await _save_fcs(repo, "fcs-complete", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    # Exit quote row for pinning (repo-level; Fake carries the same id).
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    # Align Fake snapshot ids with repo rows for pins.
    fake.quotes[0]["snapshot_id"] = "q-entry"
    fake.quotes[1]["snapshot_id"] = "q-exit"
    before_dir = _directional_count(repo)
    outcome = await grade_hedge(
        "fcs-complete", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake, None,
    )
    assert isinstance(outcome, HedgeOutcome)
    assert outcome.outcome_status == "COMPLETE"
    assert outcome.strategy == "ABSOLUTE_100"
    assert outcome.horizon_days == 7
    # Frozen entry quantity/price/capital present.
    entry = dict(outcome.outcome_json["entry"])
    assert entry["futures_qty"] == "100"
    assert entry["spot_qty"] == "100"
    assert entry["futures_entry_price_usd"] == "100"
    assert entry["spot_entry_price_usd"] == "100"
    assert Decimal(entry["capital_at_risk_usd"]) > Decimal("10000")
    # Directional table untouched.
    assert _directional_count(repo) == before_dir

    # PENDING: same snapshot graded before maturity is virtual (no DB row).
    pending = await grade_hedge(
        "fcs-complete", "ABSOLUTE_100", 30, NOW + 7 * DAY_MS,
        repo, fake, None,
    )
    assert pending.outcome_status == "PENDING"
    assert pending.reason_code == "PENDING_NOT_DUE"
    rows = await repo.list_hedge_outcomes("fcs-complete")
    assert all(
        not (r["strategy"] == "ABSOLUTE_100" and int(r["horizon_days"]) == 30)
        for r in rows
    )

    # UNAVAILABLE: missing exit quote.
    await _save_fcs(repo, "fcs-noquote", NOW)
    fake_missing = FakeMarketProvider()
    for bar in fake.bars["BTCUSDT"]:
        fake_missing.add_bar(bar)
    for event in fake.fundings["BTCUSDT"]:
        fake_missing.add_funding(event)
    fake_missing.add_quote(_quote("q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100", "100"))
    missing = await grade_hedge(
        "fcs-noquote", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake_missing, None,
    )
    assert missing.outcome_status == "UNAVAILABLE"
    assert missing.reason_code in ("NO_EXIT_QUOTE", "NO_MARKET_PROVIDER")

    # CENSORED: delisted lifecycle retains the sample.
    await _save_fcs(repo, "fcs-delisted", NOW)
    fake_delisted = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    await _save_venue(repo, "q-entry-d", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit-d", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake_delisted.quotes[0]["snapshot_id"] = "q-entry-d"
    fake_delisted.quotes[1]["snapshot_id"] = "q-exit-d"
    fake_delisted.set_lifecycle("BTCUSDT", HistoricalLifecycle(
        futures_symbol="BTCUSDT", cutoff_ms=NOW + 7 * DAY_MS,
        onboard_at_ms=NOW - 90 * DAY_MS,
        delivery_at_ms=NOW + 3 * DAY_MS,
        contract_type="PERPETUAL", exchange_status="DELISTED",
        source_id="test-lifecycle",
    ))
    censored = await grade_hedge(
        "fcs-delisted", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake_delisted, None,
    )
    assert censored.outcome_status == "CENSORED"
    assert censored.reason_code == "CONTRACT_DELISTED"


# ---------------------------------------------------------------------------
# Capital denominator / two-leg cost / funding consistency.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capital_denominator_two_leg_cost_funding(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-capital", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake.quotes[0]["snapshot_id"] = "q-entry"
    fake.quotes[1]["snapshot_id"] = "q-exit"
    before_dir = _directional_count(repo)
    outcome = await grade_hedge(
        "fcs-capital", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake, None,
    )
    assert outcome.outcome_status == "COMPLETE"
    entry = outcome.outcome_json["entry"]
    pnl = outcome.outcome_json["pnl"]
    # Capital is invested (spot cash + margin + reserve), not margin alone.
    spot_cash = Decimal(entry["spot_cash_usd"])
    margin = Decimal(entry["futures_margin_usd"])
    capital = Decimal(entry["capital_at_risk_usd"])
    assert spot_cash == Decimal("10000")
    assert margin == Decimal("10000")
    assert capital == spot_cash + margin + Decimal(pnl["fees_usd"]) + Decimal(pnl["gas_usd"])
    assert capital > margin
    # CR18: two-leg fees follow actual entry/exit notionals (FX1).
    # Entry 100->exit 110, qty 100: futures 5 + 5.5, spot 10 + 11 = 31.5.
    # The legacy entry-notional total (10 + 20 = 30) understated the exit leg.
    assert Decimal(pnl["fees_usd"]) == Decimal("31.5")
    assert Decimal(pnl["fees_usd_exit_based"]) == Decimal("31.5")
    assert Decimal(pnl["futures_entry_fee_usd"]) == Decimal("5")
    assert Decimal(pnl["futures_exit_fee_usd"]) == Decimal("5.5")
    assert Decimal(pnl["spot_entry_fee_usd"]) == Decimal("10")
    assert Decimal(pnl["spot_exit_fee_usd"]) == Decimal("11")
    assert Decimal(pnl["entry_fee_usd"]) == Decimal("15")
    assert Decimal(pnl["exit_fee_usd"]) == Decimal("16.5")
    assert Decimal(entry["cost_reserve_usd"]) == Decimal(pnl["fees_usd"]) + Decimal(pnl["gas_usd"])
    # Net consumes the true exit-based fees (CR18), not the legacy total.
    carry = Decimal(pnl["funding_carry_usd"])
    basis = Decimal(pnl["basis_pnl_usd"])
    gas = Decimal(pnl["gas_usd"])
    assert Decimal(pnl["net_pnl_usd"]) == basis + carry - Decimal("31.5") - gas
    # Funding carry: sum(rate * mark * futures_qty) over the window.
    assert Decimal(pnl["funding_carry_usd"]) > 0
    # Net return uses capital, and the futures-notional return is also shown.
    net = Decimal(pnl["net_pnl_usd"])
    # 80-digit domain maths vs 28-digit test context: compare with tolerance.
    assert abs(Decimal(pnl["net_return"]) - net / capital) < Decimal("1e-12")
    assert abs(Decimal(pnl["futures_notional_return"]) - net / Decimal("10000")) < Decimal("1e-12")
    # Basis legs sum (short + long) without double counting.
    assert Decimal(pnl["basis_pnl_usd"]) == (
        Decimal(pnl["futures_pnl_usd"]) + Decimal(pnl["spot_pnl_usd"])
    )
    assert _directional_count(repo) == before_dir


# ---------------------------------------------------------------------------
# Same snapshot different cost/version -> new row (no overwrite).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_snapshot_different_cost_new_row(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-cost", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake.quotes[0]["snapshot_id"] = "q-entry"
    fake.quotes[1]["snapshot_id"] = "q-exit"
    first = await grade_hedge(
        "fcs-cost", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake, {"cost_config_hash": "a" * 64},
    )
    assert first.outcome_status == "COMPLETE"
    assert first.cost_config_hash == "a" * 64
    second = await grade_hedge(
        "fcs-cost", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake, {"cost_config_hash": "b" * 64},
    )
    assert second.outcome_status == "COMPLETE"
    assert second.cost_config_hash == "b" * 64
    assert second.outcome_id != first.outcome_id
    rows = await repo.list_hedge_outcomes("fcs-cost")
    hashes = {r["cost_config_hash"] for r in rows
              if r["strategy"] == "ABSOLUTE_100" and int(r["horizon_days"]) == 7}
    assert hashes == {"a" * 64, "b" * 64}
    # Re-grading the first cost is idempotent (no third row).
    replay = await grade_hedge(
        "fcs-cost", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake, {"cost_config_hash": "a" * 64},
    )
    assert replay.outcome_id == first.outcome_id
    rows2 = await repo.list_hedge_outcomes("fcs-cost")
    assert len(rows2) == len(rows)


# ---------------------------------------------------------------------------
# Future quote never backfilled; missing mark/FX -> UNAVAILABLE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_future_quote_never_backfilled(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-future", NOW)
    fake = FakeMarketProvider()
    fake.add_bar(_bar("BTCUSDT", NOW, "100"))
    fake.add_bar(_bar("BTCUSDT", NOW + 7 * DAY_MS, "110"))
    fake.add_funding(_funding("BTCUSDT", NOW + DAY_MS, "0.0001", "105", "1"))
    fake.add_funding(_funding("BTCUSDT", NOW + 2 * DAY_MS, "0.0001", "106", "1"))
    # Only a future quote exists (as_of after the due instant).
    fake.add_quote(_quote("q-future", "bitcoin", "BINANCE_SPOT",
                          NOW + 7 * DAY_MS + HOUR_MS, "100", "110"))
    outcome = await grade_hedge(
        "fcs-future", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake, None,
    )
    assert outcome.outcome_status == "UNAVAILABLE"
    assert outcome.reason_code in ("NO_ENTRY_QUOTE", "NO_EXIT_QUOTE",
                                   "FUTURE_QUOTE_NOT_USED")


@pytest.mark.asyncio
async def test_missing_mark_or_fx_unavailable(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    # Missing funding mark voids the carry (price legs kept, no 0-fill).
    await _save_fcs(repo, "fcs-mark", NOW)
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake.fundings["BTCUSDT"][0] = _funding(
        "BTCUSDT", NOW + 8 * HOUR_MS, "0.0001", None, "1")
    outcome = await grade_hedge(
        "fcs-mark", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake, None,
    )
    assert outcome.outcome_status == "UNAVAILABLE"
    assert outcome.reason_code == "FUNDING_MARK_MISSING"
    assert outcome.outcome_json["funding_event_count"] is not None

    # Missing quote FX is never defaulted to 1.
    await _save_fcs(repo, "fcs-fx", NOW)
    fake_fx = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake_fx.quotes[0] = _quote("q-entry", "bitcoin", "BINANCE_SPOT",
                                NOW, "100", "100", fx=None)
    outcome_fx = await grade_hedge(
        "fcs-fx", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake_fx, None,
    )
    assert outcome_fx.outcome_status == "UNAVAILABLE"
    assert outcome_fx.reason_code == "QUOTE_FX_MISSING"


# ---------------------------------------------------------------------------
# Four strategies, three horizons, frozen drawdown rules.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_four_strategies_freeze_ratios(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-strat", NOW)
    # Spot qty per strategy: futures 10000/100=100, spot = 100*ratio.
    qtys = {"ABSOLUTE_100": "100", "RELATIVE_75": "75",
            "RELATIVE_50": "50", "RELATIVE_25": "25"}
    # One shared bar/funding timeline; per-strategy quotes.
    base = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    for strategy, qty in qtys.items():
        await _save_venue(repo, f"q-entry-{strategy}", "bitcoin",
                          "BINANCE_SPOT", NOW, qty)
        await _save_venue(repo, f"q-exit-{strategy}", "bitcoin",
                          "BINANCE_SPOT", NOW + 7 * DAY_MS, qty)
    for strategy, qty in qtys.items():
        fake = FakeMarketProvider()
        for bar in base.bars["BTCUSDT"]:
            fake.add_bar(bar)
        for event in base.fundings["BTCUSDT"]:
            fake.add_funding(event)
        fake.add_quote(_quote(f"q-entry-{strategy}", "bitcoin",
                              "BINANCE_SPOT", NOW, qty, "100"))
        fake.add_quote(_quote(f"q-exit-{strategy}", "bitcoin",
                              "BINANCE_SPOT", NOW + 7 * DAY_MS, qty, "110"))
        outcome = await grade_hedge(
            "fcs-strat", strategy, 7, NOW + 8 * DAY_MS,
            repo, fake, None,
        )
        assert outcome.outcome_status == "COMPLETE", strategy
        assert outcome.outcome_json["entry"]["spot_qty"] == qty


@pytest.mark.asyncio
async def test_drawdown_only_complete_timeline(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    # Gapped bars: entry+exit exist but the interior gap voids drawdown.
    await _save_fcs(repo, "fcs-gap", NOW)
    fake = FakeMarketProvider()
    fake.add_bar(_bar("BTCUSDT", NOW, "100"))
    fake.add_bar(_bar("BTCUSDT", NOW + 7 * DAY_MS, "110"))
    cursor = NOW + 8 * HOUR_MS
    while cursor <= NOW + 7 * DAY_MS:
        fake.add_funding(_funding("BTCUSDT", cursor, "0.0001", "105", "1"))
        cursor += 8 * HOUR_MS
    fake.add_quote(_quote("q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100", "100"))
    fake.add_quote(_quote("q-exit", "bitcoin", "BINANCE_SPOT",
                          NOW + 7 * DAY_MS, "100", "110"))
    outcome = await grade_hedge(
        "fcs-gap", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake, None,
    )
    assert outcome.outcome_status == "COMPLETE"
    assert outcome.outcome_json["risk"]["max_portfolio_drawdown_usd"] is None

    # Complete futures timeline still lacks synchronized spot prices.
    await _save_fcs(repo, "fcs-full", NOW)
    fake_full = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    outcome_full = await grade_hedge(
        "fcs-full", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS,
        repo, fake_full, None,
    )
    assert outcome_full.outcome_status == "COMPLETE"
    assert outcome_full.outcome_json["risk"]["max_portfolio_drawdown_usd"] is None


# ---------------------------------------------------------------------------
# Cross-30d pins: referenced quote/simulation survive retention.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_30d_pinned_quote_simulation_survive_retention(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    old_as_of = NOW - 31 * DAY_MS
    old_due = old_as_of + 7 * DAY_MS
    # Pinned simulation (old but referenced by a plan) + pinned quotes.
    await repo.save_hedge_simulation({
        "simulation_id": "sim-pin",
        "symbol": "BTCUSDT",
        "generated_at_ms": old_as_of,
        "expires_at_ms": NOW + 365 * DAY_MS,
        "formula_version": "hedge_v1",
        "policy_hash": "f" * 64,
        "input_json": {"symbol": "BTCUSDT", "mode": "ABSOLUTE"},
        "result_json": {"target_spot_qty": "100"},
        "source_meta_json": {"schema_version": "hedge-source-v1"},
    })
    await _save_fcs(repo, "fcs-pin", old_as_of)
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "quote-pin-entry", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT", "as_of_ms": old_as_of,
        "fetched_at_ms": old_as_of + 1_000,
        "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "100", "buy_vwap": "100",
                       "sell_vwap": "100", "mid_price": "100",
                       "quote_to_usd": "1"},
        "status": "OK",
    })
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "quote-pin-exit", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT", "as_of_ms": old_due,
        "fetched_at_ms": old_due + 1_000,
        "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "100", "buy_vwap": "110",
                       "sell_vwap": "110", "mid_price": "110",
                       "quote_to_usd": "1"},
        "status": "OK",
    })
    await repo.save_snapshot_references(
        "SIMULATION", "sim-pin",
        [("VENUE_QUOTE", "quote-pin-entry", "sim-quote")], NOW)
    await repo.create_hedge_plan({
        "plan_id": "plan-pin", "symbol": "BTCUSDT", "canonical_id": "bitcoin",
        "mode": "ABSOLUTE", "status": "DRAFT", "simulation_id": "sim-pin",
        "client_request_id": "client-pin",
        "plan_config_json": {"target_hedge_ratio": "1"},
        "created_at_ms": NOW, "updated_at_ms": NOW,
        "target_hedge_ratio": 1.0, "futures_notional_usd": 10000.0,
        "futures_contract_qty": 100.0, "canonical_futures_qty": 100.0,
        "spot_venue": "BINANCE_SPOT", "target_spot_qty": 100.0,
    }, references=[("VENUE_QUOTE", "quote-pin-entry", "plan-quote")])
    # Isolated old simulation + ordinary old quote (both sweepable).
    await repo.save_hedge_simulation({
        "simulation_id": "sim-old", "symbol": "BTCUSDT",
        "generated_at_ms": old_as_of,
        "expires_at_ms": old_as_of + 1_000,
        "formula_version": "hedge_v1",
        "policy_hash": "e" * 64,
        "input_json": {}, "result_json": {}, "source_meta_json": {},
    })
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "quote-old", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT", "as_of_ms": NOW - 2 * DAY_MS,
        "fetched_at_ms": NOW - 2 * DAY_MS + 1_000,
        "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "0.1"},
        "status": "OK",
    })
    # Grade the pinned FCS (entry 100 -> exit 110) so the outcome pins both.
    fake = FakeMarketProvider()
    t = old_as_of
    while t <= old_due:
        price = "100" if t < old_due else "110"
        fake.add_bar(_bar("BTCUSDT", t, price))
        t += DAY_MS
    cursor = old_as_of + 8 * HOUR_MS
    while cursor <= old_due:
        fake.add_funding(_funding("BTCUSDT", cursor, "0.0001", "105", "1"))
        cursor += 8 * HOUR_MS
    fake.add_quote(_quote("quote-pin-entry", "bitcoin", "BINANCE_SPOT",
                          old_as_of, "100", "100"))
    fake.add_quote(_quote("quote-pin-exit", "bitcoin", "BINANCE_SPOT",
                          old_due, "100", "110"))
    outcome = await grade_hedge(
        "fcs-pin", "ABSOLUTE_100", 7, NOW, repo, fake, None,
    )
    assert outcome.outcome_status == "COMPLETE"
    refs = await repo.list_snapshot_references("OUTCOME", outcome.outcome_id)
    pinned_quote_ids = {r["referenced_id"] for r in refs
                        if r["referenced_type"] == "VENUE_QUOTE"}
    assert {"quote-pin-entry", "quote-pin-exit"} <= pinned_quote_ids

    stats = await repo.maintain_retention({}, NOW, 1000)
    assert stats.errors == 0
    assert await repo.get_hedge_simulation("sim-pin") is not None
    assert await repo.get_hedge_simulation("sim-old") is None
    venues = {r["snapshot_id"] for r in await repo.list_spot_venues()}
    assert "quote-pin-entry" in venues
    assert "quote-pin-exit" in venues
    assert "quote-old" not in venues
    # The pinned outcome itself is never swept.
    assert len(await repo.list_hedge_outcomes("fcs-pin")) >= 1


# ---------------------------------------------------------------------------
# Simulated vs live-manual ledger stay separate; hedge_summary aggregates.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_separate_and_hedge_summary(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge
    from diveintocrypto_desktop.shortlab.evidence.hedge_metrics import hedge_summary
    from diveintocrypto_desktop.shortlab.hedge.ledger import Ledger

    await _save_fcs(repo, "fcs-sum-1", NOW)
    await _save_fcs(repo, "fcs-sum-2", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake.quotes[0]["snapshot_id"] = "q-entry"
    fake.quotes[1]["snapshot_id"] = "q-exit"
    first = await grade_hedge(
        "fcs-sum-1", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS, repo, fake, None)
    second = await grade_hedge(
        "fcs-sum-2", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS, repo, fake, None)
    assert first.outcome_status == "COMPLETE"
    assert second.outcome_status == "COMPLETE"

    # Live manual ledger fills (separate calibre, never merged).
    await repo.save_hedge_simulation({
        "simulation_id": "sim-ledger", "symbol": "BTCUSDT",
        "generated_at_ms": NOW - 1_000, "expires_at_ms": NOW + 3_600_000,
        "formula_version": "hedge_v1", "policy_hash": "f" * 64,
        "input_json": {}, "result_json": {}, "source_meta_json": {},
    })
    await repo.create_hedge_plan({
        "plan_id": "plan-ledger", "symbol": "BTCUSDT",
        "canonical_id": "bitcoin", "mode": "ABSOLUTE", "status": "DRAFT",
        "simulation_id": "sim-ledger", "client_request_id": "client-ledger",
        "plan_config_json": {"target_hedge_ratio": "1"},
        "created_at_ms": NOW, "updated_at_ms": NOW,
        "target_hedge_ratio": 1.0, "futures_notional_usd": 10000.0,
        "futures_contract_qty": 0.15, "canonical_futures_qty": 0.15,
        "spot_venue": "BINANCE_SPOT", "target_spot_qty": 0.3,
    })
    ledger = Ledger(repo)
    await ledger.apply_event("plan-ledger", "live-1", 1, {
        "schema_version": "hedge-event-v1", "leg_type": "SPOT_LONG",
        "event_type": "OPEN_SPOT_LONG", "native_qty": "1.0",
        "canonical_qty": "1.0", "native_price": "100",
        "price_currency": "USDT", "source": "USER_ENTERED",
        "executed_at_ms": NOW, "gross_qty": "1.0", "net_qty": "1.0",
    })
    positions = await ledger.read_positions("plan-ledger")
    assert positions[1].remaining_qty == "1"

    summary = await hedge_summary(
        {"strategy": "ABSOLUTE_100", "horizon": "7D"}, repo, None)
    assert isinstance(summary, HedgeEvidenceSummary)
    assert summary.strategy == "ABSOLUTE_100"
    assert summary.horizon_days == 7
    assert summary.sample_count == 2
    assert summary.complete_count == 2
    assert summary.censored_count == 0
    assert summary.avg_net_return is not None
    assert summary.median_net_return is not None
    # Ledger fills never leak into the fixed-strategy bucket.
    assert summary.sample_count != 3


@pytest.mark.asyncio
async def test_run_due_grades_due_and_leaves_pending(repo):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import run_due
    from diveintocrypto_desktop.shortlab.evidence.hedge_metrics import hedge_summary

    await _save_fcs(repo, "fcs-due", NOW - 10 * DAY_MS)
    await _save_fcs(repo, "fcs-fresh", NOW)
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "q-due-entry", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT", "as_of_ms": NOW - 10 * DAY_MS,
        "fetched_at_ms": NOW - 10 * DAY_MS + 1_000,
        "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "100", "buy_vwap": "100",
                       "sell_vwap": "100", "mid_price": "100",
                       "quote_to_usd": "1"},
        "status": "OK",
    })
    await repo.save_spot_venue_snapshot({
        "snapshot_id": "q-due-exit", "canonical_id": "bitcoin",
        "venue": "BINANCE_SPOT",
        "as_of_ms": NOW - 10 * DAY_MS + 7 * DAY_MS,
        "fetched_at_ms": NOW - 10 * DAY_MS + 7 * DAY_MS + 1_000,
        "reference_notional_usd": 10000.0,
        "quote_json": {"requested_canonical_qty": "100", "buy_vwap": "110",
                       "sell_vwap": "110", "mid_price": "110",
                       "quote_to_usd": "1"},
        "status": "OK",
    })
    fake = FakeMarketProvider()
    base_as_of = NOW - 10 * DAY_MS
    base_due = base_as_of + 7 * DAY_MS
    t = base_as_of
    while t <= base_due:
        price = "100" if t < base_due else "110"
        fake.add_bar(_bar("BTCUSDT", t, price))
        t += DAY_MS
    cursor = base_as_of + 8 * HOUR_MS
    while cursor <= base_due:
        fake.add_funding(_funding("BTCUSDT", cursor, "0.0001", "105", "1"))
        cursor += 8 * HOUR_MS
    fake.add_quote(_quote("q-due-entry", "bitcoin", "BINANCE_SPOT",
                          base_as_of, "100", "100"))
    fake.add_quote(_quote("q-due-exit", "bitcoin", "BINANCE_SPOT",
                          base_due, "100", "110"))

    class _Context:
        def __init__(self, repository, clock_ms) -> None:
            self.repository = repository
            self.config = None
            self.clock_ms = clock_ms
            self.request_budget = None
            self.trace_id = "hedge-due-test"
            self.data_dir = None
            self.market_provider = fake

    context = _Context(repo, lambda: NOW + DAY_MS)
    status = await run_due(context)
    assert status.status == "SUCCEEDED"
    assert status.job_type == "hedge_grader"
    assert int(status.stats.get("graded", 0)) >= 1
    # The due FCS now has at least one outcome; the fresh FCS stays pending.
    assert len(await repo.list_hedge_outcomes("fcs-due")) >= 1
    summary = await hedge_summary(
        {"strategy": "ABSOLUTE_100", "horizon": "7D",
         "start_ms": NOW - 11 * DAY_MS, "end_ms": NOW + DAY_MS,
         "now_ms": NOW + DAY_MS},
        repo, None,
    )
    assert summary.sample_count == 2


# ---------------------------------------------------------------------------
# CR16 (D14.2): MARK path needs a real async MARK read + price_basis check.
# TRADE-complete never masquerades as a complete liquidation path.
# ---------------------------------------------------------------------------


def _mark_daily(fake: FakeMarketProvider, start_ms: int, end_ms: int) -> None:
    """Mirror the TRADE daily timeline with true MARK bars (price 100->110)."""
    steps = max(1, (int(end_ms) - int(start_ms)) // DAY_MS)
    for i in range(steps + 1):
        open_ms = int(start_ms) + i * DAY_MS
        if open_ms > int(end_ms):
            break
        frac = (open_ms - int(start_ms)) / max(1, int(end_ms) - int(start_ms))
        price = str(Decimal("100") + (Decimal("110") - Decimal("100")) * Decimal(str(frac)))
        fake.add_mark_bar(_mark_bar("BTCUSDT", open_ms, price))


@pytest.mark.asyncio
async def test_mark_empty_never_complete_reader_proved(repo):
    """TRADE complete + MARK empty => PARTIAL (reader executed, no masquerade)."""
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-mark-empty", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake.quotes[0]["snapshot_id"] = "q-entry"
    fake.quotes[1]["snapshot_id"] = "q-exit"
    # No MARK bars added: reader returns () after a real async call.
    outcome = await grade_hedge(
        "fcs-mark-empty", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS, repo, fake, None,
    )
    assert outcome.outcome_status == "COMPLETE"
    # Proof the MARK reader really executed (not a binding existence check).
    assert len(fake.read_mark_calls) >= 1
    assert fake.read_mark_calls[0][0] == "BTCUSDT"
    assert int(fake.read_mark_calls[0][1]) == NOW
    assert int(fake.read_mark_calls[0][2]) == NOW + 7 * DAY_MS
    cov = outcome.outcome_json["risk"]["path_coverage"]
    assert cov in ("PARTIAL", "UNKNOWN")
    assert cov != "COMPLETE"


@pytest.mark.asyncio
async def test_mark_trade_basis_never_counts(repo):
    """MARK reader returning TRADE-basis bars => PARTIAL, never COMPLETE."""
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-mark-trade", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake.quotes[0]["snapshot_id"] = "q-entry"
    fake.quotes[1]["snapshot_id"] = "q-exit"
    # Wrong basis: TRADE bars injected into the MARK store must be discarded.
    t = NOW
    while t <= NOW + 7 * DAY_MS:
        fake.add_mark_bar(_bar("BTCUSDT", t, "105"))
        t += DAY_MS
    outcome = await grade_hedge(
        "fcs-mark-trade", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS, repo, fake, None,
    )
    assert outcome.outcome_status == "COMPLETE"
    assert len(fake.read_mark_calls) >= 1
    assert outcome.outcome_json["risk"]["path_coverage"] != "COMPLETE"
    assert outcome.outcome_json["risk"]["path_coverage"] in ("PARTIAL", "UNKNOWN")


@pytest.mark.asyncio
async def test_mark_window_mismatch_partial(repo):
    """MARK bars outside / covering only half the window => PARTIAL."""
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-mark-window", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake.quotes[0]["snapshot_id"] = "q-entry"
    fake.quotes[1]["snapshot_id"] = "q-exit"
    # Interior 72h gap (NOW+1d -> NOW+4d) voids COMPLETE per D14.2 gap rule.
    for open_ms in (NOW, NOW + DAY_MS, NOW + 4 * DAY_MS, NOW + 5 * DAY_MS):
        fake.add_mark_bar(_mark_bar("BTCUSDT", open_ms, "105"))
    outcome = await grade_hedge(
        "fcs-mark-window", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS, repo, fake, None,
    )
    assert outcome.outcome_status == "COMPLETE"
    assert len(fake.read_mark_calls) >= 1
    assert outcome.outcome_json["risk"]["path_coverage"] == "PARTIAL"


@pytest.mark.asyncio
async def test_mark_complete_when_mark_covers(repo):
    """True MARK bars covering the window => COMPLETE (binding + real read)."""
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-mark-full", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake.quotes[0]["snapshot_id"] = "q-entry"
    fake.quotes[1]["snapshot_id"] = "q-exit"
    _mark_daily(fake, NOW, NOW + 7 * DAY_MS)
    outcome = await grade_hedge(
        "fcs-mark-full", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS, repo, fake, None,
    )
    assert outcome.outcome_status == "COMPLETE"
    assert len(fake.read_mark_calls) >= 1
    assert outcome.outcome_json["risk"]["path_coverage"] == "COMPLETE"


@pytest.mark.asyncio
async def test_mark_future_known_excluded(repo):
    """MARK bars known after grading are not then-known => PARTIAL."""
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-mark-future", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
    fake.quotes[0]["snapshot_id"] = "q-entry"
    fake.quotes[1]["snapshot_id"] = "q-exit"
    grading_ms = NOW + 8 * DAY_MS
    t = NOW
    while t <= NOW + 7 * DAY_MS:
        fake.add_mark_bar(_mark_bar("BTCUSDT", t, "105", known_at_ms=grading_ms + HOUR_MS))
        t += DAY_MS
    outcome = await grade_hedge(
        "fcs-mark-future", "ABSOLUTE_100", 7, grading_ms, repo, fake, None,
    )
    assert outcome.outcome_status == "COMPLETE"
    assert len(fake.read_mark_calls) >= 1
    assert outcome.outcome_json["risk"]["path_coverage"] in ("PARTIAL", "UNKNOWN")
    assert outcome.outcome_json["risk"]["path_coverage"] != "COMPLETE"


@pytest.mark.asyncio
async def test_mark_unbound_and_missing_reader_unknown():
    """No reader / explicitly unbound MARK fn => UNKNOWN (never TRADE)."""
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import (
        _mark_path_coverage,
    )

    class _NoMark:
        pass

    assert await _mark_path_coverage(_NoMark(), "BTCUSDT", 1000, 2000, 3000) == "UNKNOWN"
    assert await _mark_path_coverage(None, "BTCUSDT", 1000, 2000, 3000) == "UNKNOWN"

    class _Unbound:
        mark_price_bars_fn = None

        async def read_mark_price_bars(self, symbol, start_ms, end_ms, ctx=None):
            raise AssertionError("unbound MARK must not be read")

    assert await _mark_path_coverage(_Unbound(), "BTCUSDT", 1000, 2000, 3000) == "UNKNOWN"


# ---------------------------------------------------------------------------
# CR18 (D14.2/V15): net consumes true per-leg entry/exit-amount fees.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_net_consumes_exit_based_fees(repo):
    """Exit 2x entry: exit fee follows exit notional; legacy total is wrong."""
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge

    await _save_fcs(repo, "fcs-fees-exit", NOW)
    await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
    await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
    fake = FakeMarketProvider()
    # TRADE bars 100 -> 200 (exit 2x) with complete funding/quotes.
    t = NOW
    steps = 7
    for i in range(steps + 1):
        open_ms = NOW + i * DAY_MS
        frac = (open_ms - NOW) / max(1, 7 * DAY_MS)
        price = str(Decimal("100") + (Decimal("200") - Decimal("100")) * Decimal(str(frac)))
        fake.add_bar(_bar("BTCUSDT", open_ms, price))
    cursor = NOW + 8 * HOUR_MS
    while cursor <= NOW + 7 * DAY_MS:
        fake.add_funding(_funding("BTCUSDT", cursor, "0.0001", "150", "1"))
        cursor += 8 * HOUR_MS
    fake.add_quote(_quote("q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100", "100"))
    fake.add_quote(_quote("q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100", "200"))
    _mark_daily(fake, NOW, NOW + 7 * DAY_MS)
    outcome = await grade_hedge(
        "fcs-fees-exit", "ABSOLUTE_100", 7, NOW + 8 * DAY_MS, repo, fake, None,
    )
    assert outcome.outcome_status == "COMPLETE"
    pnl = outcome.outcome_json["pnl"]
    entry = outcome.outcome_json["entry"]
    # Futures 10000 entry / 20000 exit; spot 10000 / 20000.
    # True fees: 5 + 10 + 10 + 20 = 45; legacy entry-notional total would be 30.
    assert Decimal(pnl["futures_entry_fee_usd"]) == Decimal("5")
    assert Decimal(pnl["futures_exit_fee_usd"]) == Decimal("10")
    assert Decimal(pnl["spot_entry_fee_usd"]) == Decimal("10")
    assert Decimal(pnl["spot_exit_fee_usd"]) == Decimal("20")
    assert Decimal(pnl["fees_usd"]) == Decimal("45")
    assert Decimal(pnl["fees_usd_exit_based"]) == Decimal("45")
    assert Decimal(pnl["fees_usd"]) != Decimal("30")
    # Net consumes the true exit-based total (off by 15 vs legacy here).
    carry = Decimal(pnl["funding_carry_usd"])
    basis = Decimal(pnl["basis_pnl_usd"])
    gas = Decimal(pnl["gas_usd"])
    assert Decimal(pnl["net_pnl_usd"]) == basis + carry - Decimal("45") - gas
    # Capital denominator stays frozen (spot cash + margin + reserve).
    capital = Decimal(entry["capital_at_risk_usd"])
    assert capital == Decimal("10000") + Decimal("10000") + Decimal("45") + gas
    assert abs(Decimal(pnl["net_return"]) - Decimal(pnl["net_pnl_usd"]) / capital) < Decimal("1e-12")
