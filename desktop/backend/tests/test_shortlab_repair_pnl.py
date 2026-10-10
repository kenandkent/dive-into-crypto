"""R08a ledger PnL pure function (D09, V11).

Red->green: before R08a ``hedge.pnl`` did not exist (ModuleNotFoundError).
Pure offline only: fixed clocks, no network/DB/clock reads. Amounts are
exact Decimal strings (no tolerance masking); V11 partial_close must give
futures/spot realised ``40`` each and remaining ``6`` each (via unrealised
``60``/``60`` at marks 90/110).
"""

from __future__ import annotations

from decimal import Decimal, getcontext, localcontext
from typing import Any

import pytest

from diveintocrypto_desktop.shortlab.hedge.pnl import compute_ledger_pnl
from tests.repair_fixtures import (
    FIXTURE_NOW,
    make_event_fx,
    make_events,
    make_identity,
    make_market_context,
)


def _ctx(**over: Any) -> dict[str, Any]:
    return dict(make_market_context(**over))


def _fx(**over: Any) -> dict[str, Any]:
    return dict(make_event_fx(**over))


# ---------------------------------------------------------------------------
# V11: partial_close realised 40/40, remaining 6/6.
# ---------------------------------------------------------------------------


def test_partial_close_realised_V11() -> None:
    events = make_events()
    identity = make_identity()
    event_fx = make_event_fx()
    market = make_market_context()
    pnl = compute_ledger_pnl(events, identity, event_fx, market)
    assert pnl.realized_futures_usd == "40"
    assert pnl.realized_spot_usd == "40"
    assert pnl.funding_basis == "ACTUAL_RECEIPTS_ONLY"
    assert pnl.coverage["total_events"] == 4
    assert pnl.coverage["status"] == "COMPLETE"
    assert pnl.unknown_components == ()
    # Default marks (fut 100 / spot 110): futures flat, spot 60.
    assert pnl.unrealized_futures_usd == "0"
    assert pnl.unrealized_spot_usd == "60"


def test_partial_close_remaining_six_each_V11() -> None:
    # Remaining 6 each implies unrealised 60/60 at fut mark 90 / spot 110.
    market = make_market_context(futures_mark_native="90")
    pnl = compute_ledger_pnl(make_events(), make_identity(), make_event_fx(), market)
    assert pnl.realized_futures_usd == "40"
    assert pnl.realized_spot_usd == "40"
    assert pnl.unrealized_futures_usd == "60"
    assert pnl.unrealized_spot_usd == "60"
    # Known nets: 40+40+0-0=80, exit 0.
    assert pnl.known_cost_usd == "0"
    assert pnl.actual_funding_usd == "0"
    assert pnl.known_net_subtotal_usd == "80"
    assert pnl.net_before_exit_usd == "80"
    assert pnl.net_after_exit_usd == "80"


# ---------------------------------------------------------------------------
# Fees: USDC/BNB independent, zero explicit vs unknown.
# ---------------------------------------------------------------------------


def _event_with_fee(
    base: dict[str, Any],
    *,
    fee_currency: Any,
    fee_amount: Any,
    fee_usd: Any,
    gas_usd: Any = "0",
    event_id: str = "e-x",
) -> dict[str, Any]:
    evt = dict(base)
    evt["event_id"] = event_id
    evt["fee_currency"] = fee_currency
    evt["fee_amount"] = fee_amount
    evt["fee_usd"] = fee_usd
    evt["gas_usd"] = gas_usd
    return evt


def test_usdc_and_bnb_fees_are_independent_costs() -> None:
    events = list(make_events())
    # Futures open pays USDC 2 (explicit amount+fx), spot open pays BNB 3.
    events[0] = _event_with_fee(
        events[0], fee_currency="USDC", fee_amount="2", fee_usd="2",
        event_id="e-open-fut",
    )
    events[2] = _event_with_fee(
        events[2], fee_currency="BNB", fee_amount="0.01", fee_usd="3",
        event_id="e-open-spot",
    )
    fx = dict(make_event_fx())
    fx["e-open-fut"] = {"price_fx_id": "fx-a", "price_fx": "1", "fee_fx_id": "fx-af", "fee_fx": "1"}
    fx["e-open-spot"] = {"price_fx_id": "fx-b", "price_fx": "1", "fee_fx_id": "fx-bf", "fee_fx": "300"}
    pnl = compute_ledger_pnl(events, make_identity(), fx, make_market_context())
    assert pnl.realized_futures_usd == "40"
    assert pnl.realized_spot_usd == "40"
    # 2 + 3, gas zeros elsewhere.
    assert pnl.known_cost_usd == "5"
    assert pnl.known_net_subtotal_usd == "75"
    assert pnl.net_before_exit_usd == "75"


def test_base_fee_counted_once_via_net_qty() -> None:
    # Spot buy: gross 10 @100, net 9 (1 base fee). Fee must not double-count.
    now = FIXTURE_NOW
    events = (
        {"event_id": "b1", "leg_type": "SPOT_LONG", "event_type": "OPEN_SPOT_LONG",
         "gross_qty": "10", "net_qty": "9", "canonical_qty": "9",
         "native_qty": "9", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "PEPE", "fee_amount": "1", "fee_usd": "100",
         "gas_usd": "0", "source": "USER_ENTERED", "executed_at_ms": now - 1000},
        {"event_id": "s1", "leg_type": "SPOT_LONG", "event_type": "CLOSE_SPOT_LONG",
         "gross_qty": "9", "net_qty": "9", "canonical_qty": "9",
         "native_qty": "9", "native_price": "110", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0",
         "gas_usd": "0", "source": "USER_ENTERED", "executed_at_ms": now},
    )
    fx = {
        "b1": {"price_fx_id": "x", "price_fx": "1", "fee_fx_id": "y", "fee_fx": "100"},
        "s1": {"price_fx_id": "x", "price_fx": "1", "fee_fx_id": "y", "fee_fx": "1"},
    }
    pnl = compute_ledger_pnl(events, make_identity(), fx, make_market_context(
        futures_mark_native="100", futures_quote_fx="1",
        spot_sell_vwap_native="110", spot_quote_fx="1",
        estimated_exit_fee_usd="0",
    ))
    # Cost 10*100=1000 over 9 => avg 111.11; proceeds 990; realised 990-1000=-10.
    assert pnl.realized_spot_usd is not None
    assert abs(Decimal(str(pnl.realized_spot_usd)) - Decimal("-10")) <= Decimal("0.00000001")
    # Base fee not added again: known cost only gas zeros.
    assert pnl.known_cost_usd == "0"


def test_zero_fee_explicit_vs_unknown_fee() -> None:
    # Explicit zeros stay known.
    pnl_known = compute_ledger_pnl(
        make_events(), make_identity(), make_event_fx(), make_market_context()
    )
    assert pnl_known.known_cost_usd == "0"
    assert pnl_known.net_before_exit_usd is not None
    # Unknown fee (None) => null + UNKNOWN_FEE, nets null, subtotal visible.
    events = list(make_events())
    broken = dict(events[0])
    broken["fee_amount"] = None
    broken["fee_usd"] = None
    broken["gas_usd"] = None
    events[0] = broken
    pnl = compute_ledger_pnl(events, make_identity(), make_event_fx(), make_market_context())
    assert "UNKNOWN_FEE" in pnl.unknown_components
    assert pnl.net_before_exit_usd is None
    assert pnl.net_after_exit_usd is None
    # Known subtotal stays visible (partial) but is not the complete net.
    assert pnl.known_net_subtotal_usd is not None


def test_fee_amount_usd_mismatch_is_unknown() -> None:
    events = list(make_events())
    broken = dict(events[0])
    broken["fee_amount"] = "1"
    broken["fee_usd"] = "999"
    events[0] = broken
    pnl = compute_ledger_pnl(events, make_identity(), make_event_fx(), make_market_context())
    assert "UNKNOWN_FEE" in pnl.unknown_components
    assert pnl.net_before_exit_usd is None


# ---------------------------------------------------------------------------
# Corrections resolved before moving-weighted cost.
# ---------------------------------------------------------------------------


def test_correction_reversal_does_not_create_realised() -> None:
    now = FIXTURE_NOW
    events = (
        {"event_id": "o1", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": "10", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 3000},
        {"event_id": "o2", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": "5", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 2000},
        {"event_id": "r1", "leg_type": "FUTURES_SHORT", "event_type": "CORRECT_REVERSAL",
         "native_qty": "5", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 1000,
         "reverses_event_id": "o2"},
        {"event_id": "c1", "leg_type": "FUTURES_SHORT", "event_type": "CLOSE_FUTURES_SHORT",
         "native_qty": "4", "native_price": "90", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now},
        {"event_id": "so", "leg_type": "SPOT_LONG", "event_type": "OPEN_SPOT_LONG",
         "native_qty": "10", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 3000},
        {"event_id": "sc", "leg_type": "SPOT_LONG", "event_type": "CLOSE_SPOT_LONG",
         "native_qty": "4", "native_price": "110", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now},
    )
    fx = {e["event_id"]: {"price_fx_id": "x", "price_fx": "1", "fee_fx_id": "y", "fee_fx": "1"} for e in events}
    pnl = compute_ledger_pnl(events, make_identity(), fx, make_market_context(futures_mark_native="90"))
    # Typo 5 reversed: realised still 40 (not inflated by the correction).
    assert pnl.realized_futures_usd == "40"
    assert pnl.realized_spot_usd == "40"
    assert pnl.unrealized_futures_usd == "60"


def test_double_reverse_rejected() -> None:
    now = FIXTURE_NOW
    events = (
        {"event_id": "o1", "leg_type": "SPOT_LONG", "event_type": "OPEN_SPOT_LONG",
         "native_qty": "1", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now},
        {"event_id": "r1", "leg_type": "SPOT_LONG", "event_type": "CORRECT_REVERSAL",
         "native_qty": "1", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now + 1, "reverses_event_id": "o1"},
        {"event_id": "r2", "leg_type": "SPOT_LONG", "event_type": "CORRECT_REVERSAL",
         "native_qty": "1", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now + 2, "reverses_event_id": "o1"},
    )
    fx = {e["event_id"]: {"price_fx_id": "x", "price_fx": "1", "fee_fx_id": "y", "fee_fx": "1"} for e in events}
    with pytest.raises(ValueError, match="only be reversed once"):
        compute_ledger_pnl(events, make_identity(), fx, make_market_context())


# ---------------------------------------------------------------------------
# Futures settlement FX at execution; spot USD frozen.
# ---------------------------------------------------------------------------


def test_futures_uses_execution_fx_not_current() -> None:
    now = FIXTURE_NOW
    events = (
        {"event_id": "o1", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": "10", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 2000},
        {"event_id": "c1", "leg_type": "FUTURES_SHORT", "event_type": "CLOSE_FUTURES_SHORT",
         "native_qty": "4", "native_price": "90", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 1000},
    )
    fx = {
        "o1": {"price_fx_id": "a", "price_fx": "1", "fee_fx_id": "b", "fee_fx": "1"},
        "c1": {"price_fx_id": "a", "price_fx": "2", "fee_fx_id": "b", "fee_fx": "1"},
    }
    market = make_market_context(futures_mark_native="90", futures_quote_fx="1")
    pnl = compute_ledger_pnl(events, make_identity(), fx, market)
    # realised_quote 40 at exit FX 2 => 80; unrealised 6*10*1=60.
    assert pnl.realized_futures_usd == "80"
    assert pnl.unrealized_futures_usd == "60"


def test_spot_usd_cost_frozen_at_entry_fx() -> None:
    now = FIXTURE_NOW
    events = (
        {"event_id": "b1", "leg_type": "SPOT_LONG", "event_type": "OPEN_SPOT_LONG",
         "native_qty": "10", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 3000},
        {"event_id": "b2", "leg_type": "SPOT_LONG", "event_type": "OPEN_SPOT_LONG",
         "native_qty": "10", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 2000},
        {"event_id": "s1", "leg_type": "SPOT_LONG", "event_type": "CLOSE_SPOT_LONG",
         "native_qty": "10", "native_price": "110", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "fee_usd": "0", "gas_usd": "0",
         "source": "USER_ENTERED", "executed_at_ms": now - 1000},
    )
    fx = {
        # Entry FX differs: frozen costs 1000*1 + 1000*2 = 3000 for 20.
        "b1": {"price_fx_id": "a", "price_fx": "1", "fee_fx_id": "b", "fee_fx": "1"},
        "b2": {"price_fx_id": "a", "price_fx": "2", "fee_fx_id": "b", "fee_fx": "1"},
        "s1": {"price_fx_id": "a", "price_fx": "1", "fee_fx_id": "b", "fee_fx": "1"},
    }
    pnl = compute_ledger_pnl(events, make_identity(), fx, make_market_context())
    # Proceeds 1100, allocated 1500 (frozen avg 150) => -400. Rewriting history
    # with exit FX would give +100 and must not happen.
    assert pnl.realized_spot_usd == "-400"


# ---------------------------------------------------------------------------
# FundingReceipt independent; ESTIMATED/ACTUAL truth table.
# ---------------------------------------------------------------------------


def test_funding_receipt_independent_and_actual_basis() -> None:
    now = FIXTURE_NOW
    events = tuple(make_events()) + (
        {"event_id": "f1", "leg_type": "FUNDING", "event_type": "FUNDING_RECEIPT",
         "amount": "5", "currency": "USDT", "public_funding_event_id": "pub-1",
         "source": "USER_ENTERED", "executed_at_ms": now},
    )
    fx = dict(make_event_fx())
    fx["f1"] = {"price_fx_id": "x", "price_fx": "1", "funding_fx_id": "y", "funding_fx": "1"}
    pnl = compute_ledger_pnl(events, make_identity(), fx, make_market_context())
    assert pnl.actual_funding_usd == "5"
    assert pnl.estimated_unconfirmed_funding_usd is None
    assert pnl.funding_basis == "ACTUAL_RECEIPTS_ONLY"


def test_unconfirmed_estimate_forces_estimated_even_zero() -> None:
    now = FIXTURE_NOW
    events = tuple(make_events()) + (
        {"event_id": "e1", "leg_type": "FUNDING", "event_type": "FUNDING_ESTIMATE",
         "amount": "0", "currency": "USDT", "public_funding_event_id": "pub-x",
         "source": "USER_ENTERED", "executed_at_ms": now},
    )
    fx = dict(make_event_fx())
    fx["e1"] = {"price_fx_id": "x", "price_fx": "1", "funding_fx_id": "y", "funding_fx": "1"}
    pnl = compute_ledger_pnl(events, make_identity(), fx, make_market_context())
    assert pnl.funding_basis == "ESTIMATED"
    assert pnl.estimated_unconfirmed_funding_usd == "0"
    # Actual leg stays independent (known zero, not merged with estimate).
    assert pnl.actual_funding_usd == "0"


def test_covered_estimate_not_double_counted() -> None:
    now = FIXTURE_NOW
    events = tuple(make_events()) + (
        {"event_id": "e1", "leg_type": "FUNDING", "event_type": "FUNDING_ESTIMATE",
         "amount": "7", "currency": "USDT", "public_funding_event_id": "pub-1",
         "source": "USER_ENTERED", "executed_at_ms": now - 1000},
        {"event_id": "f1", "leg_type": "FUNDING", "event_type": "FUNDING_RECEIPT",
         "amount": "5", "currency": "USDT", "public_funding_event_id": "pub-1",
         "source": "USER_ENTERED", "executed_at_ms": now},
    )
    fx = dict(make_event_fx())
    fx["e1"] = {"price_fx_id": "x", "price_fx": "1", "funding_fx_id": "y", "funding_fx": "1"}
    fx["f1"] = {"price_fx_id": "x", "price_fx": "1", "funding_fx_id": "y", "funding_fx": "1"}
    pnl = compute_ledger_pnl(events, make_identity(), fx, make_market_context())
    assert pnl.actual_funding_usd == "5"
    # Covered estimate excluded; no remaining unconfirmed => ACTUAL.
    assert pnl.funding_basis == "ACTUAL_RECEIPTS_ONLY"
    assert pnl.estimated_unconfirmed_funding_usd is None


def test_unknown_fx_gives_null_and_partial() -> None:
    events = list(make_events())
    fx = dict(make_event_fx())
    # Drop exit FX for the futures close.
    fx["e-close-fut"] = {"price_fx_id": "x", "price_fx": None, "fee_fx_id": "y", "fee_fx": "1"}
    pnl = compute_ledger_pnl(events, make_identity(), fx, make_market_context())
    assert pnl.realized_futures_usd is None
    assert "UNKNOWN_FX" in pnl.unknown_components
    assert pnl.coverage["status"] == "PARTIAL"
    assert pnl.net_before_exit_usd is None
    assert pnl.net_after_exit_usd is None
    # Spot leg stays known; known subtotal remains visible (partial).
    assert pnl.realized_spot_usd == "40"
    assert pnl.known_net_subtotal_usd is not None


def test_unknown_exit_cost_nulls_net_after_only() -> None:
    market = dict(make_market_context())
    market["estimated_exit_fee_usd"] = None
    pnl = compute_ledger_pnl(make_events(), make_identity(), make_event_fx(), market)
    assert pnl.realized_futures_usd == "40"
    assert pnl.net_before_exit_usd == "80"
    assert pnl.estimated_exit_cost_usd is None
    assert pnl.net_after_exit_usd is None
    assert "UNKNOWN_EXIT_COST" in pnl.unknown_components


def test_remaining_entry_basis_matches_moving_average_after_partial_close_and_fx() -> None:
    from diveintocrypto_desktop.shortlab.hedge.pnl import remaining_open_entry_basis

    events = [
        {"event_id": "f1", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": "1", "native_price": "100", "price_currency": "USDT"},
        {"event_id": "f2", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": "3", "native_price": "200", "price_currency": "USDT"},
        {"event_id": "fc", "leg_type": "FUTURES_SHORT", "event_type": "CLOSE_FUTURES_SHORT",
         "native_qty": "1.5", "native_price": "190", "price_currency": "USDT"},
        {"event_id": "s1", "leg_type": "SPOT_LONG", "event_type": "OPEN_SPOT_LONG",
         "native_qty": "1", "native_price": "10", "price_currency": "USDT"},
        {"event_id": "s2", "leg_type": "SPOT_LONG", "event_type": "OPEN_SPOT_LONG",
         "native_qty": "3", "native_price": "20", "price_currency": "USDT"},
        {"event_id": "sc", "leg_type": "SPOT_LONG", "event_type": "CLOSE_SPOT_LONG",
         "native_qty": "1", "native_price": "15", "price_currency": "USDT"},
    ]
    fx = {
        "f1": {"price_fx": "1.1"}, "f2": {"price_fx": "1.2"},
        "fc": {"price_fx": "1.3"},
        "s1": {"price_fx": "2"}, "s2": {"price_fx": "3"},
        "sc": {"price_fx": "1.5"},
    }
    # Futures remain in native quote units and retain the moving average 175;
    # spot basis is historical USD cost per unit and remains 50 after close.
    assert remaining_open_entry_basis(events, fx, "FUTURES_SHORT", "2.5", contract_multiplier="1") == "175"
    assert remaining_open_entry_basis(events, fx, "SPOT_LONG", "3") == "50"
    pnl = compute_ledger_pnl(
        events,
        {"canonical_id": "test", "contract_multiplier": "1", "multiplier_source": "EXCHANGE"},
        fx,
        {"now_ms": FIXTURE_NOW, "futures_mark_native": "175", "futures_quote_fx": "1",
         "spot_sell_vwap_native": "15", "spot_quote_fx": "1"},
    )
    # Moving average 175 gives a $29.25 Futures realized loss on the 1.5
    # close at 190*1.3; 50 USD spot basis gives the matching realized result.
    assert pnl.realized_futures_usd == "-29.25"
    assert pnl.realized_spot_usd == "-27.5"

    corrected = events[:2] + [
        {"event_id": "fc", "leg_type": "FUTURES_SHORT", "event_type": "CLOSE_FUTURES_SHORT",
         "native_qty": "1", "native_price": "150", "price_currency": "USDT", "executed_at_ms": 3},
        {"event_id": "fr", "leg_type": "FUTURES_SHORT", "event_type": "CORRECT_REVERSAL",
         "reverses_event_id": "fc", "native_qty": "1", "native_price": "150",
         "price_currency": "USDT", "executed_at_ms": 4},
    ]
    assert remaining_open_entry_basis(corrected, fx, "FUTURES_SHORT", "4", contract_multiplier="1") == "175"


def test_remaining_entry_basis_reopen_after_partial_close_uses_remaining_inventory_weight() -> None:
    from diveintocrypto_desktop.shortlab.hedge.pnl import remaining_open_entry_basis

    events = [
        {"event_id": "open-100", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": "2", "native_price": "100", "price_currency": "USDT", "executed_at_ms": 1},
        {"event_id": "close-1", "leg_type": "FUTURES_SHORT", "event_type": "CLOSE_FUTURES_SHORT",
         "native_qty": "1", "native_price": "90", "price_currency": "USDT", "executed_at_ms": 2},
        {"event_id": "open-200", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": "1", "native_price": "200", "price_currency": "USDT", "executed_at_ms": 3},
    ]
    fx = {event["event_id"]: {"price_fx": "1"} for event in events}
    # Remaining inventory is 1 @ 100 plus 1 @ 200. Closed history cannot stay
    # in the average denominator (which would incorrectly yield 133.33).
    assert remaining_open_entry_basis(events, fx, "FUTURES_SHORT", "2", contract_multiplier="1") == "150"
    ledger = compute_ledger_pnl(
        events,
        {"canonical_id": "test", "contract_multiplier": "1", "multiplier_source": "EXCHANGE"},
        fx,
        {"now_ms": FIXTURE_NOW, "futures_mark_native": "150", "futures_quote_fx": "1"},
    )
    assert ledger.unrealized_futures_usd == "0"


def test_remaining_entry_basis_restores_close_and_rejects_zero_cost_full_restore() -> None:
    from diveintocrypto_desktop.shortlab.hedge.pnl import remaining_open_entry_basis

    opens_and_full_close = [
        {"event_id": "open", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": "1", "native_price": "100", "price_currency": "USDT", "executed_at_ms": 1},
        {"event_id": "close", "leg_type": "FUTURES_SHORT", "event_type": "CLOSE_FUTURES_SHORT",
         "native_qty": "1", "native_price": "90", "price_currency": "USDT", "executed_at_ms": 2},
        {"event_id": "restore", "leg_type": "FUTURES_SHORT", "event_type": "CORRECT_REVERSAL",
         "reverses_event_id": "close", "native_qty": "1", "native_price": "90", "price_currency": "USDT", "executed_at_ms": 3},
    ]
    fx = {event["event_id"]: {"price_fx": "1"} for event in opens_and_full_close}
    # compute_ledger_pnl carries full-close reversal inventory with zero cost;
    # this helper must not misrepresent that unknown basis as a known zero.
    with pytest.raises(ValueError, match="restore entry basis for empty position"):
        remaining_open_entry_basis(opens_and_full_close, fx, "FUTURES_SHORT", "1", contract_multiplier="1")


def test_remaining_entry_basis_uses_80_digit_context_and_restores_global_precision() -> None:
    from diveintocrypto_desktop.shortlab.hedge.pnl import remaining_open_entry_basis

    q1 = Decimal("1.000000000000000000000000000000000000001")
    q2 = Decimal("1")
    events = [
        {"event_id": "precision-open-1", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": str(q1), "native_price": "100", "price_currency": "USDT", "executed_at_ms": 1},
        {"event_id": "precision-open-2", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "native_qty": str(q2), "native_price": "200", "price_currency": "USDT", "executed_at_ms": 2},
    ]
    with localcontext() as ctx:
        ctx.prec = 80
        expected_qty = q1 + q2
        expected_value = q1 * Decimal("100") + q2 * Decimal("200")
        expected = expected_value / expected_qty

    old_precision = getcontext().prec
    try:
        getcontext().prec = 10
        result = remaining_open_entry_basis(
            events, {}, "FUTURES_SHORT", str(expected_qty), contract_multiplier="1",
        )
        assert Decimal(result) == expected
        assert getcontext().prec == 10
    finally:
        getcontext().prec = old_precision
