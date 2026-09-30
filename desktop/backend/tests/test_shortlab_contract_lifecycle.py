"""Task 8: contract lifecycle retention (Short-Lab Phase 1).

Covers ``ShortLab_Implementation_Plan_CN.md`` Task 8 and
``ShortLab_Detailed_Design_CN.md`` section 10.2:

- exchangeInfo ``onboardDate``/``deliveryDate``/``status``/``contractType``
  are retained in ``contract_metadata_all()``; a missing/invalid onboardDate
  leaves ``onboard_at_ms=None`` while ``first_seen_ms`` records only the
  first local observation (never proof of listing age).
- ``first_seen_ms`` is sticky: later refreshes keep the earliest value even
  when the contract status changes; stopped-TRADING contracts stay in
  metadata plus per-symbol history snapshots.
- Listing-age gate: no valid onboardDate -> NOT_READY + LISTING_AGE_UNKNOWN
  (``first_seen_ms`` never clears it); recent onboardDate -> PAUSED +
  PAUSE_NEW_TOKEN; old onboardDate -> OK.
- Termination gate: explicit non-TRADING status or a near delivery ->
  BLOCKED + VETO_CONTRACT_DELISTING; vanished-from-live with no confirmed
  terminal evidence -> PAUSED + PAUSE_CONTRACT_STATUS_UNVERIFIED (never
  misreported as delisted); far-future delivery placeholders never veto;
  listing-flow statuses (PENDING_TRADING) never veto; missing status never
  defaults to TRADING.
- Separation: metadata keeps every observed contract while ``list_universe()``
  still returns TRADING perps only (legacy behavior preserved).
- Missing explicit exchange multiplier -> (None, None); never prefix-guessed.

All network access is faked; no live requests.
"""

from __future__ import annotations

import pytest

from diveintocrypto_desktop.data import universe as uni

DAY_MS = 86_400_000
NOW_MS = 1_720_000_000_000  # fixed "now" for deterministic age/delivery math
PLACEHOLDER_DELIVERY_MS = 4_133_404_800_000  # Binance perp far-future sentinel


@pytest.fixture(autouse=True)
def _clean_state():
    uni.reset_universe_cache()
    yield
    uni.reset_universe_cache()


def _entry(
    symbol: str,
    *,
    status: str | None = "TRADING",
    ctype: str | None = "PERPETUAL",
    quote: str = "USDT",
    base: str | None = None,
    onboard: int | None = None,
    delivery: int | None = None,
    extra: dict | None = None,
) -> dict:
    row: dict = {
        "symbol": symbol,
        "status": status,
        "contractType": ctype,
        "quoteAsset": quote,
        "baseAsset": base if base is not None else symbol.removesuffix(quote),
    }
    if onboard is not None:
        row["onboardDate"] = onboard
    if delivery is not None:
        row["deliveryDate"] = delivery
    if extra:
        row.update(extra)
    return row


# ── retention ─────────────────────────────────────────────────────────────

def test_retains_onboard_delivery_status_contract_type():
    onboard = NOW_MS - 200 * DAY_MS
    delivery = NOW_MS + 30 * DAY_MS
    uni.update_contract_metadata(
        [_entry("BTCUSDT", onboard=onboard, delivery=delivery)], NOW_MS
    )
    meta = uni.contract_metadata_all()["BTCUSDT"]
    assert meta.symbol == "BTCUSDT"
    assert meta.onboard_at_ms == onboard
    assert meta.delivery_at_ms == delivery
    assert meta.status == "TRADING"
    assert meta.contract_type == "PERPETUAL"
    assert meta.first_seen_ms == NOW_MS
    assert meta.observed_at_ms == NOW_MS
    # No explicit exchange multiplier -> null pair (Task 5 provenance rule).
    assert meta.contract_multiplier is None
    assert meta.multiplier_source is None


def test_missing_onboard_leaves_first_seen_only():
    uni.update_contract_metadata([_entry("NEWCOINUSDT", base="NEWCOIN")], NOW_MS)
    meta = uni.contract_metadata_all()["NEWCOINUSDT"]
    assert meta.onboard_at_ms is None
    assert meta.first_seen_ms == NOW_MS  # first observation only, not an age

    action, reasons = uni.listing_age_status(meta, NOW_MS)
    assert action == "NOT_READY"
    assert reasons == (uni.LISTING_AGE_UNKNOWN,)


def test_first_seen_sticky_across_status_change():
    t1 = NOW_MS - 10 * DAY_MS
    t2 = NOW_MS
    uni.update_contract_metadata([_entry("OLDUSDT", base="OLD")], t1)
    assert uni.contract_metadata_all()["OLDUSDT"].first_seen_ms == t1

    # Status flips TRADING -> SETTLING: earliest first_seen must survive and
    # observed_at_ms must advance to the latest snapshot.
    uni.update_contract_metadata(
        [_entry("OLDUSDT", base="OLD", status="SETTLING")], t2
    )
    meta = uni.contract_metadata_all()["OLDUSDT"]
    assert meta.first_seen_ms == t1
    assert meta.observed_at_ms == t2
    assert meta.status == "SETTLING"


def test_first_seen_keeps_earliest_on_out_of_order_refresh():
    uni.update_contract_metadata([_entry("ODDUSDT", base="ODD")], NOW_MS)
    uni.update_contract_metadata(
        [_entry("ODDUSDT", base="ODD")], NOW_MS - 5 * DAY_MS
    )
    assert (
        uni.contract_metadata_all()["ODDUSDT"].first_seen_ms
        == NOW_MS - 5 * DAY_MS
    )


def test_stopped_trading_remains_queryable_with_history():
    uni.update_contract_metadata([_entry("DEADUSDT", base="DEAD")], NOW_MS - DAY_MS)
    uni.update_contract_metadata(
        [_entry("DEADUSDT", base="DEAD", status="CLOSE")], NOW_MS
    )
    store = uni.contract_metadata_all()
    assert "DEADUSDT" in store  # still retained after leaving live trading
    assert store["DEADUSDT"].status == "CLOSE"

    hist = uni.contract_history("DEADUSDT")
    assert [h.status for h in hist] == ["TRADING", "CLOSE"]

    # An identical refresh adds no duplicate snapshot.
    uni.update_contract_metadata(
        [_entry("DEADUSDT", base="DEAD", status="CLOSE")], NOW_MS + 1000
    )
    assert len(uni.contract_history("DEADUSDT")) == 2
    # ... but the latest observation time still advances.
    assert uni.contract_metadata_all()["DEADUSDT"].observed_at_ms == NOW_MS + 1000


def test_invalid_timestamps_normalized_to_none():
    uni.update_contract_metadata(
        [
            {"symbol": "AUSDT", "onboardDate": 0, "deliveryDate": -5,
             "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "BUSDT", "onboardDate": "garbage", "deliveryDate": None,
             "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "CUSDT", "onboardDate": True, "status": "TRADING",
             "contractType": "PERPETUAL"},
            "not-a-mapping",
            {"no_symbol": True},
        ],
        NOW_MS,
    )
    store = uni.contract_metadata_all()
    assert set(store) == {"AUSDT", "BUSDT", "CUSDT"}
    assert store["AUSDT"].onboard_at_ms is None
    assert store["AUSDT"].delivery_at_ms is None
    assert store["BUSDT"].onboard_at_ms is None
    assert store["CUSDT"].onboard_at_ms is None


# ── listing-age gate ──────────────────────────────────────────────────────

def test_missing_onboard_is_not_ready_even_with_ancient_first_seen():
    ancient = NOW_MS - 5 * 365 * DAY_MS
    uni.update_contract_metadata([_entry("FOSSILUSDT", base="FOSSIL")], ancient)
    meta = uni.contract_metadata_all()["FOSSILUSDT"]
    assert meta.first_seen_ms == ancient
    # A years-old first_seen must NOT clear the unknown-age gate.
    action, reasons = uni.listing_age_status(meta, NOW_MS)
    assert action == "NOT_READY"
    assert reasons == (uni.LISTING_AGE_UNKNOWN,)


def test_missing_metadata_is_not_ready():
    action, reasons = uni.listing_age_status(None, NOW_MS)
    assert action == "NOT_READY"
    assert reasons == (uni.LISTING_AGE_UNKNOWN,)


def test_recent_onboard_pauses_new_token_old_onboard_ok():
    uni.update_contract_metadata(
        [_entry("YOUNGUSDT", base="YOUNG", onboard=NOW_MS - 10 * DAY_MS)], NOW_MS
    )
    action, reasons = uni.listing_age_status(
        uni.contract_metadata_all()["YOUNGUSDT"], NOW_MS, new_token_days=45
    )
    assert action == "PAUSED"
    assert reasons == (uni.PAUSE_NEW_TOKEN,)

    uni.update_contract_metadata(
        [_entry("MATUREUSDT", base="MATURE", onboard=NOW_MS - 100 * DAY_MS)],
        NOW_MS,
    )
    action, reasons = uni.listing_age_status(
        uni.contract_metadata_all()["MATUREUSDT"], NOW_MS, new_token_days=45
    )
    assert (action, reasons) == ("OK", ())

    # Age exactly at the threshold is no longer "new".
    uni.update_contract_metadata(
        [_entry("EDGEUSDT", base="EDGE", onboard=NOW_MS - 45 * DAY_MS)], NOW_MS
    )
    action, _ = uni.listing_age_status(
        uni.contract_metadata_all()["EDGEUSDT"], NOW_MS, new_token_days=45
    )
    assert action == "OK"


# ── termination gate ──────────────────────────────────────────────────────

def test_disappearance_without_evidence_is_unverified_not_delisted():
    uni.update_contract_metadata(
        [_entry("VANISHUSDT", base="VANISH", delivery=PLACEHOLDER_DELIVERY_MS)],
        NOW_MS,
    )
    store = uni.contract_metadata_all()
    action, reasons = uni.contract_termination_status(
        "VANISHUSDT", live_symbols={"BTCUSDT"}, now_ms=NOW_MS, metadata_all=store
    )
    assert action == "PAUSED"
    assert reasons == (uni.PAUSE_CONTRACT_STATUS_UNVERIFIED,)
    assert uni.CONTRACT_STATUS_UNVERIFIED == uni.PAUSE_CONTRACT_STATUS_UNVERIFIED
    assert uni.VETO_CONTRACT_DELISTING not in reasons


@pytest.mark.parametrize("status", ["SETTLING", "CLOSE"])
def test_terminal_status_vetoes_delisting(status: str):
    uni.update_contract_metadata(
        [_entry("GONEUSDT", base="GONE", status=status)], NOW_MS
    )
    store = uni.contract_metadata_all()
    action, reasons = uni.contract_termination_status(
        "GONEUSDT", live_symbols=set(), now_ms=NOW_MS, metadata_all=store
    )
    assert action == "BLOCKED"
    assert reasons == (uni.VETO_CONTRACT_DELISTING,)


def test_pending_trading_never_vetoes():
    uni.update_contract_metadata(
        [_entry("FRESHUSDT", base="FRESH", status="PENDING_TRADING")], NOW_MS
    )
    store = uni.contract_metadata_all()
    action, reasons = uni.contract_termination_status(
        "FRESHUSDT", live_symbols=set(), now_ms=NOW_MS, metadata_all=store
    )
    assert action == "PAUSED"
    assert reasons == (uni.PAUSE_CONTRACT_STATUS_UNVERIFIED,)


def test_near_delivery_vetoes_far_future_placeholder_ignored():
    uni.update_contract_metadata(
        [
            _entry("QUSDT", base="Q", ctype="CURRENT_QUARTER",
                   delivery=NOW_MS + 2 * DAY_MS),
            _entry("PUSDT", base="P", delivery=PLACEHOLDER_DELIVERY_MS),
            _entry("WUSDT", base="W", delivery=NOW_MS - 400 * DAY_MS),
        ],
        NOW_MS,
    )
    store = uni.contract_metadata_all()

    action, reasons = uni.contract_termination_status(
        "QUSDT", live_symbols={"QUSDT"}, now_ms=NOW_MS, metadata_all=store
    )
    assert action == "BLOCKED"  # still live, but delivery is imminent
    assert reasons == (uni.VETO_CONTRACT_DELISTING,)

    action, reasons = uni.contract_termination_status(
        "PUSDT", live_symbols={"PUSDT"}, now_ms=NOW_MS, metadata_all=store
    )
    assert (action, reasons) == ("OK", ())  # placeholder never triggers

    action, reasons = uni.contract_termination_status(
        "WUSDT", live_symbols=set(), now_ms=NOW_MS, metadata_all=store
    )
    assert action == "PAUSED"  # stale far-past delivery is not "recent"
    assert reasons == (uni.PAUSE_CONTRACT_STATUS_UNVERIFIED,)


def test_missing_status_never_defaults_to_trading():
    uni.update_contract_metadata(
        [{"symbol": "NOSTATUSUSDT", "contractType": "PERPETUAL"}], NOW_MS
    )
    store = uni.contract_metadata_all()
    assert store["NOSTATUSUSDT"].status is None

    action, reasons = uni.contract_termination_status(
        "NOSTATUSUSDT", live_symbols=set(), now_ms=NOW_MS, metadata_all=store
    )
    assert action == "PAUSED"
    assert reasons == (uni.PAUSE_CONTRACT_STATUS_UNVERIFIED,)


def test_live_membership_without_veto_evidence_is_ok():
    uni.update_contract_metadata([_entry("LIVEUSDT", base="LIVE")], NOW_MS)
    store = uni.contract_metadata_all()
    action, reasons = uni.contract_termination_status(
        "LIVEUSDT", live_symbols={"LIVEUSDT"}, now_ms=NOW_MS, metadata_all=store
    )
    assert (action, reasons) == ("OK", ())


def test_never_seen_symbol_makes_no_claim():
    action, reasons = uni.contract_termination_status(
        "GHOSTUSDT", live_symbols=set(), now_ms=NOW_MS, metadata_all={}
    )
    assert action == "UNKNOWN"
    assert reasons == ()
    assert uni.VETO_CONTRACT_DELISTING not in reasons


# ── multiplier provenance ─────────────────────────────────────────────────

def test_multiplier_null_without_explicit_source():
    uni.update_contract_metadata(
        [
            _entry("1000PEPEUSDT", base="1000PEPE"),  # prefix alone proves nothing
            _entry(
                "XUSDT",
                base="X",
                extra={"contract_multiplier": 1000},  # no source -> ignored
            ),
            _entry(
                "YUSDT",
                base="Y",
                extra={
                    "contract_multiplier": 1000,
                    "multiplier_source": "EXCHANGE",
                },
            ),
        ],
        NOW_MS,
    )
    store = uni.contract_metadata_all()
    assert (store["1000PEPEUSDT"].contract_multiplier,
            store["1000PEPEUSDT"].multiplier_source) == (None, None)
    assert (store["XUSDT"].contract_multiplier,
            store["XUSDT"].multiplier_source) == (None, None)
    assert (store["YUSDT"].contract_multiplier,
            store["YUSDT"].multiplier_source) == (1000.0, "EXCHANGE")


# ── separation: full metadata vs scannable universe ───────────────────────

def _ticker(symbol: str, quote_volume: float) -> dict:
    return {
        "symbol": symbol,
        "lastPrice": "1.0",
        "priceChangePercent": "0.5",
        "quoteVolume": str(quote_volume),
    }


@pytest.mark.asyncio
async def test_list_universe_still_trading_perps_only(monkeypatch):
    exchange_info = {
        "symbols": [
            _entry("BTCUSDT", base="BTC", onboard=NOW_MS - 300 * DAY_MS),
            _entry("ETHUSDT", base="ETH", onboard=NOW_MS - 300 * DAY_MS),
            _entry("XRPUSDT", base="XRP", status="SETTLING"),  # halted perp
            _entry(  # quarterly delivery contract, still TRADING
                "BTCUSDT_250627", base="BTC", ctype="CURRENT_QUARTER",
                status="TRADING",
            ),
            _entry("DOGEUSDC", base="DOGE", quote="USDC"),  # wrong quote
            _entry("USDCUSDT", base="USDC"),  # skipped stablecoin base
        ]
    }
    tickers = [
        _ticker("BTCUSDT", 900.0),
        _ticker("ETHUSDT", 500.0),
        _ticker("XRPUSDT", 9999.0),  # highest volume, but not tradable
        _ticker("BTCUSDT_250627", 800.0),
        _ticker("DOGEUSDC", 700.0),
        _ticker("USDCUSDT", 600.0),
    ]

    async def _fake_get_json(url: str, params=None):
        if url.endswith("/exchangeInfo"):
            return exchange_info
        if url.endswith("/ticker/24hr"):
            return tickers
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(uni, "get_json", _fake_get_json)

    rows = await uni.list_universe()
    assert [r["s"] for r in rows] == ["BTCUSDT", "ETHUSDT"]  # sorted desc
    assert rows[0]["quote_volume"] >= rows[1]["quote_volume"]

    # Full metadata retains everything the scannable universe filters out.
    store = uni.contract_metadata_all()
    assert set(store) == {
        "BTCUSDT", "ETHUSDT", "XRPUSDT",
        "BTCUSDT_250627", "DOGEUSDC", "USDCUSDT",
    }
    assert store["XRPUSDT"].status == "SETTLING"
    assert store["BTCUSDT_250627"].contract_type == "CURRENT_QUARTER"

    perps = await uni.perp_symbols()
    assert set(perps) == {"BTCUSDT", "ETHUSDT"}
