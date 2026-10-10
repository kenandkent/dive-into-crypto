"""Production score-refresh producer to frozen Entry/QuoteTask integration."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.test_shortlab_default_wiring import (
    DAY_MS,
    NOW,
    FakeClock,
    make_service,
    make_universe,
    open_repo,
)


@pytest.mark.asyncio
async def test_score_refresh_automatically_persists_cohort_entries_and_quote_tasks(tmp_path) -> None:
    """The ordinary score refresh exercises the real capture owner and DuckDB rows."""
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.repair_contracts import FuturesExecutionQuote
    from diveintocrypto_desktop.shortlab.hedge.models import (
        SpotVenueQuote,
        TradingRulesSnapshot,
    )
    from diveintocrypto_desktop.shortlab.service import JOB_TYPE_SCORE_REFRESH

    clock = FakeClock()
    repo = await open_repo(tmp_path)
    try:
        await repo.migrate(target_version=6)
        rows = make_universe(1)
        service = make_service(repo, clock, rows)
        symbol = rows[0]["s"]

        async def resolved_identity(requested_symbol):
            assert requested_symbol == symbol
            return {
                "canonical_id": "fixture-asset", "display_symbol": symbol,
                "binance_futures_symbol": symbol, "binance_spot_symbol": symbol,
                "contract_multiplier": "1", "multiplier_source": "EXCHANGE",
                "mapping_confidence": "VERIFIED", "mapping_source": "MANUAL",
            }

        service._hedge_identity_fn = resolved_identity
        service._identity_catalog = SimpleNamespace(
            snapshot_id_for=lambda requested_symbol, identity, cutoff: f"identity-{requested_symbol}-{cutoff}",
            mapping_version="fixture-identity-v1",
        )
        from diveintocrypto_desktop.shortlab.service import build_default_repair_ports
        service._repair_ports = build_default_repair_ports()
        await repo.save_fx_observation({
            "fx_id": "fx-usdt-cohort-capture", "currency": "USDT",
            "source_as_of_ms": NOW + 1_000, "known_at_ms": NOW + 1_000,
            "rate_str": "1", "source_json": {"source": "fixture:exchange-usdt-usd"},
        })
        base = load_shortlab_config()
        service._config = replace(
            base,
            hedge=replace(base.hedge, enabled=True),
            funding_capture=replace(base.funding_capture, enabled=True),
        )
        service._hedge_available = True

        class ReplayMarket:
            """Local exchange-shaped responses; the production capture path stays real."""

            async def mark(self, symbol, request_context=None):
                return {
                    "symbol": symbol, "canonical_price_usd": "4", "native_price": "4",
                    "mark_price": "4", "quote_to_usd": "1", "as_of_ms": NOW,
                    "source_as_of_ms": NOW, "known_at_ms": NOW,
                    "fetched_at_ms": NOW, "expires_at_ms": NOW + 60_000,
                }

            async def rules(self, kind, symbol, request_context=None):
                return TradingRulesSnapshot(
                    venue="BINANCE_SPOT", instrument_id=symbol,
                    source_as_of_ms=NOW, known_at_ms=NOW,
                    rule_version=f"fixture-{kind}-v1",
                    lot_rules={"step_size": "0.000001", "min_qty": "0.000001"},
                    price_rules={"tick_size": "0.000001"},
                    notional_rules={"min_notional": "1"},
                    order_types={"MARKET": True}, stop_orders_supported=True,
                    conditional_orders_source_ref=f"fixture:{kind}:{symbol}",
                )

            async def collect_futures(self, symbol, contract_qty, request_context=None):
                return {
                    "futures_quote": FuturesExecutionQuote(
                        quote_id=f"futures:{symbol}:{contract_qty}", symbol=symbol,
                        requested_contract_qty=str(contract_qty), buy_vwap_native="4.01",
                        sell_vwap_native="3.99", buy_executable_qty=str(contract_qty),
                        sell_executable_qty=str(contract_qty), quote_currency="USDT",
                            quote_to_usd="1", as_of_ms=NOW + 1_000, known_at_ms=NOW + 1_000,
                            expires_at_ms=NOW + 61_000,
                        book_observation_id=f"book:{symbol}:{contract_qty}",
                        fees_included=True,
                    )
                }

            async def collect_spot(self, identity, venue, canonical_qty, request_context=None):
                return SpotVenueQuote(
                    venue="BINANCE_SPOT", canonical_id=identity.canonical_id,
                    symbol=identity.binance_spot_symbol or identity.display_symbol,
                        chain=None, contract_address=None, as_of_ms=NOW + 1_000,
                        expires_at_ms=NOW + 61_000, reference_notional_usd="10000",
                    mid_price="4", buy_vwap="4.01", sell_vwap="3.99",
                    buy_executable_qty=str(canonical_qty),
                    sell_executable_qty=str(canonical_qty), buy_slippage_bps=1,
                    sell_slippage_bps=1, estimated_fee_usd="0", estimated_gas_usd="0",
                    entry_feasible=True, exit_feasible=True,
                    exit_feasibility="CONFIRMED", quote_currency="USDT",
                        quote_to_usd="1", source_timestamp_ms=NOW + 1_000, fetched_at_ms=NOW + 1_000,
                    requested_canonical_qty=str(canonical_qty), fees_included=True,
                    identity_confidence="VERIFIED", status="OK",
                )

        service._market_port = ReplayMarket()
        capture_results = []
        original_capture = service._capture_market_entry_cohort

        async def record_capture_result(**kwargs):
            result = await original_capture(**kwargs)
            capture_results.append(result)
            return result

        service._capture_market_entry_cohort = record_capture_result
        status = await service.run_refresh(JOB_TYPE_SCORE_REFRESH)
        assert status.status == "SUCCEEDED", status
        assert status.stats["cohort_capture_attempted"] >= 1
        assert status.stats["cohort_capture_failed"] == 0
        assert capture_results and len(capture_results[0].stats["entry_ids"]) == 5, capture_results
        assert capture_results[0].stats["status"] == "COMPLETE", capture_results

        utc_start = NOW - (NOW % DAY_MS)
        entries = await repo.list_strategy_entries(
            "RESEARCH_CANDIDATE", utc_start, utc_start + DAY_MS
        )
        assert len(entries) >= 5
        assert {entry["strategy"] for entry in entries} >= {
            "UNHEDGED_0", "ABSOLUTE_100", "RELATIVE_75", "RELATIVE_50", "RELATIVE_25"
        }
        assert all(entry["entry_json"]["status"] == "ENTRY_COMPLETE" for entry in entries)
        for entry in entries:
            entry_json = entry["entry_json"]
            assert await repo.get_identity_snapshot(entry_json["identity_snapshot_id"]) is not None
            for rule_id in entry_json["rule_refs"].values():
                assert await repo.get_contract_rules_snapshot(rule_id) is not None
        source_ids = {str(entry["source_snapshot_id"]) for entry in entries}
        page = await service.candidates()
        assert source_ids <= {str(candidate.snapshot_id) for candidate in page.items}
        for entry in entries:
            tasks = await repo.list_strategy_quote_tasks(
                str(entry["entry_id"]), start_ms=0, end_ms=NOW + 365 * DAY_MS,
                limit=10, offset=0,
            )
            assert len(tasks) == 3
            assert all(task["entry_id"] == entry["entry_id"] for task in tasks)
    finally:
        await repo.close()
