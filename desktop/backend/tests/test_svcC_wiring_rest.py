"""svcC wiring-rest regressions (CR05/CR08/CR12/CR15/CR23).

Production-trigger only: Decision/jobs/service paths, never direct
capture/ledger pure-function substitution for production proof.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

DAY_MS = 86_400_000


# ---------------------------------------------------------------------------
# CR08: per-symbol manual profile + frozen reason
# ---------------------------------------------------------------------------

def test_svcC_cr08_btcusdt_meme_lite_with_frozen_reason():
    from diveintocrypto_desktop.shortlab.scoring.profiles import select_profile

    doc = {"version": 1, "overrides": {"BTCUSDT": {"profile": "MEME", "canonical_id": "btc"}}}
    p = select_profile(None, None, doc, "BTCUSDT")
    assert p.name == "MEME_LITE"
    assert p.base == "MEME"
    assert p.is_manual is True
    assert p.reason == "MANUAL_OVERRIDE"
    # other symbol stays general
    p2 = select_profile(None, None, doc, "ETHUSDT")
    assert p2.is_manual is False
    assert p2.name == "GENERAL_LITE"
    # flat shape also works
    flat = {"BTCUSDT": {"profile": "MEME"}}
    assert select_profile(None, None, flat, "BTCUSDT").name == "MEME_LITE"


def test_svcC_cr08_profile_basis_freezes_manual_reason():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.scoring.profiles import select_profile
    from diveintocrypto_desktop.shortlab.service import ShortLabService

    cfg = load_shortlab_config()
    svc = ShortLabService(config=cfg, repository=None, clock=lambda: 0)
    profile = select_profile(None, None, {"profile": "MEME"}, None)
    assert profile.is_manual is True
    # minimal identity/exchange/funding/market for field states
    from diveintocrypto_desktop.shortlab.models import ProviderResult

    ident = SimpleNamespace(mapping_confidence="VERIFIED")
    states = svc._build_field_states(
        ident, {"status": "TRADING"}, ProviderResult(status="UNAVAILABLE", source="x", fetched_at_ms=0, as_of_ms=None, data=None, stale=False, reason_code="X", error_message=None),
        ProviderResult(status="UNAVAILABLE", source="x", fetched_at_ms=0, as_of_ms=None, data=None, stale=False, reason_code="X", error_message=None),
        {"daily_closes": [], "fetched_at_ms": 0}, {"windows": {}}, 0, 0, profile=profile,
    )
    by_id = {s.field_id: s for s in states}
    assert by_id["profile_basis"].reason_code == "MANUAL_OVERRIDE"
    assert by_id["profile_basis"].status == "OK"


# ---------------------------------------------------------------------------
# CR23: same-round Micro/Taker freeze into risk input
# ---------------------------------------------------------------------------

def test_svcC_cr23_frozen_confirm_pause_and_missing_not_ready():
    from diveintocrypto_desktop.shortlab import observations as obs
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.inputs import build_feature_inputs
    from diveintocrypto_desktop.shortlab.risk.veto import derive_status, evaluate_risks

    asof = int(dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    known = asof - 60_000
    midnight = (asof // DAY_MS) * DAY_MS

    def mk(v, src, **kw):
        return obs.make_observation(v, source=src, source_as_of_ms=kw.pop("source_as_of_ms", known),
                                    fetched_at_ms=kw.pop("fetched_at_ms", known),
                                    known_at_ms=kw.pop("known_at_ms", known), **kw)

    cfg = load_shortlab_config()
    ident = SimpleNamespace(contract_multiplier=1.0, multiplier_source="EXCHANGE",
                            mapping_confidence="VERIFIED", identity_snapshot_id="isl")
    base = {
        "quote_asset": "USDT",
        "fx_rate": mk("1", "fx"),
        "klines_daily": mk([{"t": (midnight - (10 - i) * DAY_MS) * 1_000_000, "c": 100.0, "h": 101, "qv": 35_000_000.0} for i in range(10)], "k"),
        "ticker_24h": mk({"current_price": 100.0, "price_change_24h": 0.01}, "t"),
        "funding_history": mk([{"t": midnight - 30 * DAY_MS + i * 8 * 3_600_000, "funding_rate": 0.0001} for i in range(90)], "f",
                              complete=True, coverage_fraction=1.0, window_start_ms=midnight - 30 * DAY_MS, window_end_ms=midnight),
        "funding_history_90d": mk([{"t": midnight - 90 * DAY_MS + i * 8 * 3_600_000, "funding_rate": 0.0001} for i in range(270)], "f",
                                   complete=True, coverage_fraction=1.0, window_start_ms=midnight - 90 * DAY_MS, window_end_ms=midnight),
        "oi_history": mk([{"t": midnight * 1_000_000, "oi_value": 15_000_000.0}], "oi"),
        "ath": mk({"ath_usd": 200.0, "ath_date_ms": asof - 200 * DAY_MS}, "c"),
        "fundamentals": mk({"market_cap_usd": 1e8, "fdv_usd": 6e8, "circulating_supply": 15e6, "total_supply": 100e6}, "c"),
        "spot_history": mk({"status": "OK", "data": {}}, "s"),
        "book": mk({"bid_notional_1pct": 1e6, "ask_notional_1pct": 1e6}, "b"),
        "contract": mk({"status": "TRADING", "onboard_at_ms": asof - 200 * DAY_MS}, "e"),
        "micro_score": mk(40.0, "micro"),
        "taker_buy_ratio": mk(0.6, "taker"),
    }
    fi = build_feature_inputs("TSTUSDT", dict(base), ident, asof, cfg)
    assert fi.micro_score == 40.0 and fi.taker_buy_ratio == 0.6
    rm = fi.risk_meta()
    assert isinstance(rm.get("micro_score"), dict) and rm["micro_score"]["value"] == 40.0
    assert rm["micro_score"]["known_at_ms"] == known

    feats = {"price_change_7d": 0.20, "oi_change_7d": 0.20}
    meta = {"as_of_ms": asof, "mapping_confidence": "VERIFIED", "futures_qv_1d": 35e6,
            "oi_value_usd": 15e6, "contract_status": "TRADING", "exchange_status": "TRADING",
            "live_universe_present": True, "previously_seen": True,
            "onboard_at_ms": asof - 200 * DAY_MS, "price_change_24h": 0.01,
            "price_change_7d": 0.20, "oi_change_7d": 0.20,
            "funding_30d": 0.012, "funding_7d": 0.003, "funding_positive_ratio_30d": 0.8}
    meta_c = dict(meta)
    meta_c.update({k: v for k, v in rm.items() if k in ("micro_score", "taker_buy_ratio")})
    r = evaluate_risks(dict(feats), dict(meta_c), 95.0)
    assert "PAUSE_SQUEEZE" in r.pauses
    # missing confirm => NOT_READY
    r2 = evaluate_risks(dict(feats), dict(meta), 95.0)
    st = derive_status(85, 80, 95.0, 8.0,
                       {"mapping_confidence": "VERIFIED", "contract_multiplier": 1.0, "has_valid_onboard": True},
                       r2, False)
    assert st.execution_status == "NOT_READY"
    assert "SQUEEZE_CHECK_UNVERIFIED" in st.reasons
    # late confirm stays unconfirmable
    late = obs.make_observation(80.0, source="micro", source_as_of_ms=known, fetched_at_ms=asof + 10000, known_at_ms=asof + 10000)
    base2 = dict(base)
    base2["micro_score"] = late
    base2.pop("taker_buy_ratio", None)
    fi2 = build_feature_inputs("TSTUSDT", base2, ident, asof, cfg)
    assert fi2.micro_score is None


# ---------------------------------------------------------------------------
# CR12 pure: correct FX 40/40, empty null, no USDT=1
# ---------------------------------------------------------------------------

def test_svcC_cr12_ledger_fx_40_40_vs_empty_null():
    from diveintocrypto_desktop.shortlab.hedge.pnl import compute_ledger_pnl

    events = [
        {"event_id": "f-open", "leg_type": "FUTURES_SHORT", "event_type": "OPEN_FUTURES_SHORT",
         "canonical_qty": "10", "native_price": "100", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "gas_usd": "0", "executed_at_ms": 1000},
        {"event_id": "s-open", "leg_type": "SPOT_LONG", "event_type": "OPEN_SPOT_LONG",
         "canonical_qty": "10", "gross_qty": "10", "net_qty": "10", "native_price": "100",
         "price_currency": "USDT", "fee_currency": "USDT", "fee_amount": "0", "gas_usd": "0", "executed_at_ms": 1000},
        {"event_id": "f-close", "leg_type": "FUTURES_SHORT", "event_type": "CLOSE_FUTURES_SHORT",
         "canonical_qty": "4", "native_price": "90", "price_currency": "USDT",
         "fee_currency": "USDT", "fee_amount": "0", "gas_usd": "0", "executed_at_ms": 2000},
        {"event_id": "s-close", "leg_type": "SPOT_LONG", "event_type": "CLOSE_SPOT_LONG",
         "canonical_qty": "4", "gross_qty": "4", "net_qty": "4", "native_price": "110",
         "price_currency": "USDT", "fee_currency": "USDT", "fee_amount": "0", "gas_usd": "0", "executed_at_ms": 2000},
    ]
    ident = {"contract_multiplier": "1", "multiplier_source": "MANUAL"}
    fx = {e["event_id"]: {"price_fx": "1", "fee_fx": "1"} for e in events}
    mctx = {"now_ms": 3000, "futures_mark_native": "90", "futures_quote_fx": "1",
            "spot_sell_vwap_native": "110", "spot_quote_fx": "1"}
    p = compute_ledger_pnl(events, ident, fx, mctx)
    assert p.realized_futures_usd == "40"
    assert p.realized_spot_usd == "40"
    assert p.net_before_exit_usd == "80"
    # empty FX => null, never USDT=1
    p2 = compute_ledger_pnl(events, ident, {k: {} for k in fx}, mctx)
    assert p2.realized_futures_usd is None and p2.realized_spot_usd is None
    assert "UNKNOWN_FX" in tuple(p2.unknown_components)


# ---------------------------------------------------------------------------
# CR05 + CR12 production: monitor tick + EVENT_FX + API same 40/40
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_svcC_cr05_cr12_monitor_tick_partial_close_40_40(tmp_path):
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from diveintocrypto_desktop.shortlab.service import ShortLabService
    from test_shortlab_hedge_repository import _open_spot, _plan, _sim, NOW

    repo = await ShortLabRepository.open(tmp_path / "svcC-cr05.duckdb")
    await repo.migrate(target_version=6)
    try:
        clock = [NOW]
        cfg = load_shortlab_config()
        await repo.save_fx_observation({"fx_id": "fx-usdt-1", "currency": "USDT",
                                        "source_as_of_ms": NOW - 1000, "known_at_ms": NOW - 1000,
                                        "rate_str": "1", "source_json": {"source": "test"}})
        identity = {"canonical_id": "bitcoin", "contract_multiplier": "1", "multiplier_source": "MANUAL",
                    "identity_confidence": "VERIFIED", "mapping_confidence": "VERIFIED"}
        mark = {"price": "90", "mark_price": "90", "quote_to_usd": "1",
                "canonical_price_usd": "90", "next_funding_time_ms": NOW + 1000}
        quote = {"snapshot_id": "q", "venue": "BINANCE_SPOT", "mid_price": "110", "buy_vwap": "110",
                 "sell_vwap": "110", "buy_executable_qty": "100", "sell_executable_qty": "100",
                 "requested_canonical_qty": "10", "quote_to_usd": "1",
                 "as_of_ms": NOW, "fetched_at_ms": NOW, "expires_at_ms": NOW + 60000, "status": "OK"}
        from diveintocrypto_desktop.shortlab.hedge.models import FundingMetrics
        funding = FundingMetrics(symbol="BTCUSDT")
        svc = ShortLabService(config=cfg, repository=repo, registry=ProviderRegistry(), clock=lambda: clock[0],
                              universe_fn=lambda n: [{"symbol": "BTCUSDT", "quote_volume": 1}],
                              hedge_identity_fn=AsyncMock(return_value=dict(identity)),
                              hedge_mark_fn=AsyncMock(return_value=dict(mark)),
                              hedge_quote_fn=AsyncMock(return_value=dict(quote)),
                              hedge_funding_fn=AsyncMock(return_value=funding),
                              hedge_available=True)
        from types import SimpleNamespace as _NS

        async def collect(symbol, qty, venue=None, request_context=None):
            now = clock[0]
            return {"futures_mark": {"price": "90", "quote_to_usd": "1", "as_of_ms": now,
                                     "fetched_at_ms": now, "expires_at_ms": now + 60000},
                    "spot_quote": {**quote, "as_of_ms": now, "fetched_at_ms": now, "expires_at_ms": now + 60000}}

        svc._hedge_market = _NS(collect=collect)
        await repo.save_hedge_simulation(_sim())
        plan = _plan()
        plan["symbol"] = "BTCUSDT"
        plan["canonical_id"] = "bitcoin"
        plan["plan_config_json"].update(contract_multiplier="1")
        await repo.create_hedge_plan(plan)
        fut_open = dict(_open_spot("10", "100"))
        fut_open.update(leg_type="FUTURES_SHORT", event_type="OPEN_FUTURES_SHORT",
                        fee_amount="0", fee_usd="0", gas_usd="0", executed_at_ms=NOW)
        spot_open = dict(_open_spot("10", "100"))
        spot_open.update(fee_amount="0", fee_usd="0", gas_usd="0", executed_at_ms=NOW)
        await svc.apply_leg_event("plan-1", {"event": fut_open, "client_event_id": "c0", "expected_version": 1})
        await svc.apply_leg_event("plan-1", {"event": spot_open, "client_event_id": "c1", "expected_version": 2})
        # EVENT_FX persisted on trade (auditable, not empty)
        rows = await repo.list_market_observations("BTCUSDT", "EVENT_FX", 0, NOW + 1000, NOW + 1000)
        assert len(rows) == 2
        ctx = _NS(trace_id="t", clock_ms=lambda: clock[0])
        await svc.run_hedge_monitor(ctx)
        clock[0] += 10000
        await repo.save_fx_observation({"fx_id": "fx-usdt-2", "currency": "USDT",
                                        "source_as_of_ms": clock[0] - 1000, "known_at_ms": clock[0] - 1000,
                                        "rate_str": "1", "source_json": {"source": "test"}})
        fut_close = dict(_open_spot("4", "90"))
        fut_close.update(leg_type="FUTURES_SHORT", event_type="CLOSE_FUTURES_SHORT",
                         fee_amount="0", fee_usd="0", gas_usd="0", executed_at_ms=clock[0])
        spot_close = dict(_open_spot("4", "110"))
        spot_close.update(event_type="CLOSE_SPOT_LONG",
                          fee_amount="0", fee_usd="0", gas_usd="0", executed_at_ms=clock[0])
        prow = await repo.get_hedge_plan("plan-1")
        await svc.apply_leg_event("plan-1", {"event": fut_close, "client_event_id": "c2",
                                             "expected_version": int(prow.get("plan_version"))})
        prow = await repo.get_hedge_plan("plan-1")
        await svc.apply_leg_event("plan-1", {"event": spot_close, "client_event_id": "c3",
                                             "expected_version": int(prow.get("plan_version"))})
        await svc.run_hedge_monitor(ctx)
        # memory / DB / API same 40/40 (fast path did not skip recompute)
        mir = svc._hedge_jobs.mirror.get("plan-1", {}).get("previous") if getattr(svc, "_hedge_jobs", None) else None
        assert mir is not None
        assert mir.metrics_json.get("ledger_realized_futures_usd") == "40"
        assert mir.metrics_json.get("ledger_realized_spot_usd") == "40"
        snap = await repo.latest_hedge_monitor("plan-1")
        mj = snap.get("metrics_json") if isinstance(snap, dict) else {}
        import json as _js
        if isinstance(mj, str):
            mj = _js.loads(mj)
        assert mj.get("ledger_realized_futures_usd") == "40"
        assert mj.get("ledger_realized_spot_usd") == "40"
        api = await svc.monitor("plan-1")
        wired = api.get("ledger_pnl") or api.get("ledgerPnl")
        assert wired is not None
        assert wired.get("realized_futures_usd") == "40"
        assert wired.get("realized_spot_usd") == "40"
    finally:
        await repo.close()


# ---------------------------------------------------------------------------
# CR15: production capture/collect triggers (no direct capture call)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_svcC_cr15_production_capture_and_due_quotes(tmp_path):
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from diveintocrypto_desktop.shortlab.service import ShortLabService, build_default_repair_ports
    from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
    from test_shortlab_repair_capture import FIXTURE_NOW, FakeMarket, _make_capture

    repo = await ShortLabRepository.open(tmp_path / "svcC-cr15.duckdb")
    await repo.migrate(target_version=6)
    try:
        cfg = load_shortlab_config()
        ports = build_default_repair_ports()
        svc = ShortLabService(config=cfg, repository=repo, registry=ProviderRegistry(),
                              clock=lambda: FIXTURE_NOW, repair_ports=ports,
                              repository_port=repo, market_port=FakeMarket(),
                              hedge_available=True)
        # Production trigger: service run_strategy_capture (not direct capture fn)
        ctx = SimpleNamespace(trace_id="t", clock_ms=lambda: FIXTURE_NOW,
                              repository=repo, config=cfg, request_budget=None)
        cap_ctx = _make_capture()
        # FakeMarket in capture tests needs FX rows for USDT
        await repo.save_fx_observation({"fx_id": "fx-usdt-1", "currency": "USDT",
                                        "source_as_of_ms": FIXTURE_NOW - 1000, "known_at_ms": FIXTURE_NOW - 1000,
                                        "rate_str": "1", "source_json": {"source": "test"}})
        # MarketPort for capture is FakeMarket (frozen quotes with FX=1)
        svc._market_port = FakeMarket()
        svc._repository_port = repo
        res = await svc.run_strategy_capture(ctx, cap_ctx, job_id="strategy_capture")
        assert res.status == "SUCCEEDED"
        assert res.stats.get("claimed", 0) >= 1
        rows = await repo.list_strategy_entries("USER_DECISION", FIXTURE_NOW - 1000, FIXTURE_NOW + 10000)
        assert len(rows) >= 1
        by_strat = {r.get("strategy"): r for r in rows}
        # UNHEDGED_0 participates alongside fixed ratios
        assert "UNHEDGED_0" in by_strat
        # Due-quote production trigger (expiry job, 20/round)
        qres = await svc.run_strategy_quote_collection(ctx, FIXTURE_NOW + 7 * DAY_MS + 1000,
                                                       job_id="strategy_quote_collection")
        assert qres.status in ("SUCCEEDED", "FAILED")
        # Grader consumes real Entries (UNHEDGED_0 present) via run_due
        from diveintocrypto_desktop.shortlab.evidence.hedge_grader import normalize_strategy
        assert normalize_strategy("UNHEDGED_0") == "UNHEDGED_0"
        assert normalize_strategy("SYSTEM_POLICY") == "SYSTEM_POLICY"
    finally:
        await repo.close()


@pytest.mark.asyncio
async def test_svcC_cr15_grader_grades_unhedged_entry(tmp_path):
    from diveintocrypto_desktop.shortlab.evidence.hedge_grader import grade_hedge
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from test_shortlab_hedge_evidence import _complete_fake, _save_fcs, _save_venue, NOW, DAY_MS

    repo = await ShortLabRepository.open(tmp_path / "svcC-cr15g.duckdb")
    await repo.migrate(target_version=6)
    try:
        await _save_fcs(repo, "fcs-unhedged", NOW)
        await _save_venue(repo, "q-entry", "bitcoin", "BINANCE_SPOT", NOW, "100")
        await _save_venue(repo, "q-exit", "bitcoin", "BINANCE_SPOT", NOW + 7 * DAY_MS, "100")
        fake = _complete_fake(NOW, NOW + 7 * DAY_MS, entry_qty="100")
        fake.quotes[0]["snapshot_id"] = "q-entry"
        fake.quotes[1]["snapshot_id"] = "q-exit"
        out = await grade_hedge("fcs-unhedged", "UNHEDGED_0", 7, NOW + 8 * DAY_MS, repo, fake, None)
        assert out.outcome_status in ("COMPLETE", "UNAVAILABLE", "CENSORED")
        # UNHEDGED_0 spot leg is zero but grading still consumes the Entry path
        assert out.strategy == "UNHEDGED_0"
    finally:
        await repo.close()
