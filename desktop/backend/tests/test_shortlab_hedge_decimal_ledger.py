"""H01 Decimal ledger: integer accumulation, no DOUBLE read-back.

Covers plan H01.2/H01.3 ledger scenes:
- 0.1 + 0.2 == 0.3 exactly; dust never swallowed by an epsilon;
- polluted DOUBLE projections never affect balances;
- restart replays the same balances from immutable events;
- price/VWAP uses an 80-digit localcontext; zero/over-close checks never
  use rounded display values; no SQL SUM(qty) / float comparison path.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab.repository import ShortLabRepository

NOW = 1_760_000_000_000


def _sim(sim_id: str = "sim-ledger") -> dict:
    return {
        "simulation_id": sim_id,
        "symbol": "BTCUSDT",
        "generated_at_ms": NOW - 1_000,
        "expires_at_ms": NOW + 3_600_000,
        "formula_version": "hedge_v1",
        "policy_hash": "p" * 64,
        "input_json": {},
        "result_json": {},
        "source_meta_json": {},
    }


def _plan(plan_id: str = "plan-ledger", sim_id: str = "sim-ledger",
          client_id: str = "client-ledger") -> dict:
    return {
        "plan_id": plan_id,
        "symbol": "BTCUSDT",
        "canonical_id": "bitcoin",
        "mode": "ABSOLUTE",
        "status": "DRAFT",
        "simulation_id": sim_id,
        "client_request_id": client_id,
        "plan_config_json": {"target_spot_qty": "0.300000000000000001"},
        "created_at_ms": NOW,
        "updated_at_ms": NOW,
        "target_hedge_ratio": 1.0,
        "futures_notional_usd": 10000.0,
        "futures_contract_qty": 0.15,
        "canonical_futures_qty": 0.15,
        "spot_venue": "BINANCE_SPOT",
        "target_spot_qty": 0.3,
    }


def _open(leg: str, qty: str, etype: str, client: str = "e") -> dict:
    return {
        "schema_version": "hedge-event-v1",
        "leg_type": leg,
        "event_type": etype,
        "native_qty": qty,
        "canonical_qty": qty,
        "native_price": "67000.123456789012345678901234567890",
        "price_currency": "USDT",
        "fee_currency": None,
        "fee_amount": None,
        "fee_usd": None,
        "gas_usd": None,
        "source": "USER_ENTERED",
        "executed_at_ms": NOW,
        "gross_qty": qty,
        "net_qty": qty,
    }


@pytest_asyncio.fixture
async def repo(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "ledger.duckdb")
    await handle.migrate(target_version=4)
    await handle.migrate(target_version=5)
    yield handle
    await handle.close()


@pytest.mark.asyncio
async def test_point_one_plus_point_two_is_point_three(repo):
    await repo.save_hedge_simulation(_sim())
    await repo.create_hedge_plan(_plan())
    await repo.apply_hedge_event("plan-ledger", "o1", 1,
                                 _open("SPOT_LONG", "0.1", "OPEN_SPOT_LONG"))
    await repo.apply_hedge_event("plan-ledger", "o2", 2,
                                 _open("SPOT_LONG", "0.2", "OPEN_SPOT_LONG"))
    positions = await repo.aggregate_hedge_position("plan-ledger")
    spot = next(p for p in positions if p["leg_type"] == "SPOT_LONG")
    assert spot["open_qty"] == "0.3"
    assert spot["remaining_qty"] == "0.3"
    # Closing exactly 0.3 leaves a true zero (no 1e-17 residue).
    await repo.apply_hedge_event(
        "plan-ledger", "c1", 3,
        {**_open("SPOT_LONG", "0.3", "CLOSE_SPOT_LONG")})
    positions = await repo.aggregate_hedge_position("plan-ledger")
    spot = next(p for p in positions if p["leg_type"] == "SPOT_LONG")
    assert spot["remaining_qty"] == "0"


@pytest.mark.asyncio
async def test_dust_preserved_and_polluted_double_ignored(repo):
    await repo.save_hedge_simulation(_sim("sim-dust"))
    await repo.create_hedge_plan(_plan("plan-dust", "sim-dust", "client-dust"))
    dust = "0.000000000000000001"
    await repo.apply_hedge_event("plan-dust", "d1", 1, _open("SPOT_LONG", dust, "OPEN_SPOT_LONG"))
    positions = await repo.aggregate_hedge_position("plan-dust")
    spot = next(p for p in positions if p["leg_type"] == "SPOT_LONG")
    assert spot["remaining_qty"] == dust
    # Pollute the DOUBLE projection directly: balances must not move.
    repo._con.execute("UPDATE sl_hedge_leg SET qty = 999999.0, canonical_qty = 999999.0")
    repo._con.execute("UPDATE sl_hedge_plan SET target_spot_qty = 999999.0")
    positions = await repo.aggregate_hedge_position("plan-dust")
    spot = next(p for p in positions if p["leg_type"] == "SPOT_LONG")
    assert spot["remaining_qty"] == dust
    assert spot["open_qty"] == dust


@pytest.mark.asyncio
async def test_restart_replays_identical_balances(tmp_path):
    db = tmp_path / "restart-ledger.duckdb"
    handle = await ShortLabRepository.open(db)
    try:
        await handle.migrate(target_version=4)
        await handle.migrate(target_version=5)
        await handle.save_hedge_simulation(_sim())
        await handle.create_hedge_plan(_plan())
        await handle.apply_hedge_event("plan-ledger", "o1", 1,
                                       _open("SPOT_LONG", "0.1", "OPEN_SPOT_LONG"))
        await handle.apply_hedge_event("plan-ledger", "o2", 2,
                                       _open("SPOT_LONG", "0.2", "OPEN_SPOT_LONG"))
        before = await handle.aggregate_hedge_position("plan-ledger")
    finally:
        await handle.close()
    reopened = await ShortLabRepository.open(db)
    try:
        await reopened.migrate(target_version=5)
        after = await reopened.aggregate_hedge_position("plan-ledger")
        assert after == before
        spot = next(p for p in after if p["leg_type"] == "SPOT_LONG")
        assert spot["open_qty"] == "0.3"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_integer_accumulation_ignores_default_context(repo):
    # 28-digit context would round a 40-digit dust sum; integer maths keeps it.
    from decimal import Decimal
    await repo.save_hedge_simulation(_sim("sim-ctx"))
    await repo.create_hedge_plan(_plan("plan-ctx", "sim-ctx", "client-ctx"))
    tiny = "0." + "0" * 35 + "1"
    await repo.apply_hedge_event("plan-ctx", "t1", 1, _open("SPOT_LONG", tiny, "OPEN_SPOT_LONG"))
    await repo.apply_hedge_event("plan-ctx", "t2", 2, _open("SPOT_LONG", tiny, "OPEN_SPOT_LONG"))
    positions = await repo.aggregate_hedge_position("plan-ctx")
    spot = next(p for p in positions if p["leg_type"] == "SPOT_LONG")
    assert Decimal(spot["open_qty"]) == Decimal(tiny) * 2
    assert spot["open_qty"] == "0." + "0" * 34 + "2" or Decimal(spot["open_qty"]) == Decimal(tiny) * 2

@pytest.mark.asyncio
async def test_native_only_futures_replays_canonical_quantity_and_rejects_overclose(repo):
    await repo.save_hedge_simulation(_sim())
    plan = _plan()
    plan['plan_config_json']['contract_multiplier'] = '1000'
    await repo.create_hedge_plan(plan)
    event = _open('FUTURES_SHORT', '2', 'OPEN_FUTURES_SHORT')
    event['native_price'] = '0.01'
    for key in ('canonical_qty', 'net_qty', 'gross_qty'):
        event.pop(key)
    await repo.apply_hedge_event('plan-ledger', 'native-open', 1, event)
    positions = await repo.aggregate_hedge_position('plan-ledger')
    assert positions[0]['remaining_qty'] == '2000'
    assert positions[0]['weighted_avg_price'] == '0.00001'
    close = {**event, 'event_type':'CLOSE_FUTURES_SHORT', 'native_qty':'3'}
    from diveintocrypto_desktop.shortlab.repository import ValidationError
    with pytest.raises(ValidationError, match='remaining quantity'):
        await repo.apply_hedge_event('plan-ledger', 'native-overclose', 2, close)
    assert (await repo.aggregate_hedge_position('plan-ledger'))[0]['remaining_qty'] == '2000'
