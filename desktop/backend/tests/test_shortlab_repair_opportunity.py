"""R09 real opportunity projection (D08/D03.3, V07/V10).

Pure offline only: fixed clock, no network/DB clock reads. Covers
project_opportunity typed projection (7D/bestVenue/BE/age-class/
fcsConfigHash/real expires, default hold 30, indicative never best,
dynamic expiry NOT_READY, LEGACY) plus repository latest-wins/total/
null-last/stable-sort behaviour (ROW_NUMBER, no 200-row pre-truncation).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from diveintocrypto_desktop.shortlab.hedge.opportunity import (
    build_projection_v2,
    build_risk_json,
)
from diveintocrypto_desktop.shortlab.hedge.projection import project_opportunity
from diveintocrypto_desktop.shortlab.repair_contracts import OpportunityQuery
from diveintocrypto_desktop.shortlab.repository import (
    ShortLabRepository,
    ValidationError,
)

NOW = 1791417600000
DAY_MS = 86_400_000
HASH = "ab" * 32


@pytest_asyncio.fixture
async def repo(tmp_path):
    handle = await ShortLabRepository.open(tmp_path / "r09.duckdb")
    await handle.migrate(target_version=6)
    yield handle
    await handle.close()


def _ready_breakdown(readiness: str = "READY") -> dict[str, Any]:
    gate = {"status": "PASS", "reasons": [], "checked_at_ms": NOW, "input_refs": {}}
    if readiness != "READY":
        gate = {"status": "FAIL", "reasons": ["FUNDING_CURRENT_NON_POSITIVE"], "checked_at_ms": NOW, "input_refs": {}}
    return {
        "data_complete": readiness == "READY",
        "funding_gate": dict(gate),
        "execution_gate": {"status": "PASS", "reasons": [], "checked_at_ms": NOW, "input_refs": {}},
        "economic_gate": {"status": "PASS", "reasons": [], "checked_at_ms": NOW, "input_refs": {}},
        "protection_status": "UNKNOWN",
        "readiness": readiness,
    }


def _snapshot_inputs(
    *,
    snapshot_id: str,
    symbol: str,
    as_of: int,
    fcs: float | None = 80.0,
    funding_7d: str | None = "0.004",
    funding_30d: str | None = "0.018",
    positive_ratio: str | None = "0.85",
    history_class: str | None = None,
    listing_age: int | None = 120,
    best_explicit: str | None = None,
    break_even: str | None = "12.5",
    conservative_apr: str | None = "0.25",
    expires_at: int | None = None,
    readiness: str = "READY",
    venue_quotes: Any = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "symbol": symbol,
        "canonical_id": symbol.lower(),
        "as_of_ms": as_of,
        "fcs": fcs,
        "fcs_config_hash": HASH,
        "funding_7d": funding_7d,
        "funding_30d": funding_30d,
        "positive_ratio_30d": positive_ratio,
        "conservative_apr": conservative_apr,
        "break_even_days": break_even,
        "readiness_breakdown": _ready_breakdown(readiness),
        "reasons": [],
        "reference_notional_usd": "10000",
    }
    if history_class is not None:
        snap["history_class"] = history_class
    if listing_age is not None:
        snap["listing_age_days"] = listing_age
    else:
        # Explicit None must survive (unknown age) — keep key with None.
        snap["listing_age_days"] = None
    if best_explicit is not None:
        snap["best_venue"] = best_explicit
    if venue_quotes is not None:
        snap["venue_quotes"] = venue_quotes
    if venue_quotes is None and best_explicit is None:
        # Default executable venue so plain READY seeds stay executable;
        # indicative/empty-venue cases pass explicit quotes or best=None.
        snap["best_venue"] = "BINANCE_SPOT"
    if expires_at is not None:
        snap["expires_at_ms"] = expires_at
    else:
        snap["expires_at_ms"] = as_of + 1_800_000
    if extra:
        snap.update(extra)
    # Drop None break_even key? No — keep explicit None to test nullBE.
    return snap


def _venue(
    venue: str,
    *,
    cost: float = 0.002,
    expires: int = NOW + 1_800_000,
    kind: str | None = None,
    status: str = "OK",
    ref: str = "10000",
) -> dict[str, Any]:
    q: dict[str, Any] = {
        "venue": venue,
        "status": status,
        "reference_notional_usd": ref,
        "buy_executable_qty": "200000",
        "sell_executable_qty": "200000",
        "requested_canonical_qty": "100000",
        "roundtrip_cost_pct": cost,
        "exit_feasibility": "CONFIRMED",
        "expires_at_ms": expires,
    }
    if kind is not None:
        q["quote_kind"] = kind
    return q


async def seed_fcs(
    repo: ShortLabRepository,
    *,
    snapshot_id: str,
    symbol: str,
    as_of: int,
    created: int,
    projection_inputs: dict[str, Any] | None = None,
    legacy: bool = False,
) -> dict[str, Any]:
    """Build a real v2 projection and persist it as risk_json.projection_v2."""
    if legacy:
        risk_json: dict[str, Any] = {}
        proj: dict[str, Any] | None = None
        readiness = "NOT_READY"
        reasons: list[str] = []
        fcs: float | None = None
    else:
        assert projection_inputs is not None
        # Fresh projection at row time (repository re-applies stale on read).
        proj = build_projection_v2(projection_inputs, projection_inputs.get("as_of_ms", as_of))
        risk_json = build_risk_json(projection_inputs, projection_inputs.get("as_of_ms", as_of))
        # build_risk_json already embeds projection_v2; keep identity out.
        readiness = str(proj.get("readiness", "NOT_READY"))
        reasons = list(proj.get("reasons", []))
        fcs = proj.get("fcs")
    record = {
        "snapshot_id": snapshot_id,
        "symbol": symbol,
        "canonical_id": symbol.lower(),
        "as_of_ms": as_of,
        "fcs_version": "fcs_v2",
        "fcs_config_hash": HASH,
        "reference_notional_usd": 10000.0,
        "fcs": fcs,
        "module_scores_json": {},
        "funding_metrics_json": {
            "funding_7d": (proj or {}).get("funding_7d"),
            "funding_30d": (proj or {}).get("funding_30d"),
        },
        "venue_summary_json": {"best_venue": (proj or {}).get("best_venue")},
        "risk_json": risk_json,
        "readiness": readiness,
        "reasons_json": reasons,
        "created_at_ms": created,
    }
    await repo.save_funding_capture_snapshot(record)
    return proj or {}


# ---------------------------------------------------------------------------
# V10: latest NOT_READY hides old READY.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latest_not_ready_hides_old_ready(repo):
    old_inputs = _snapshot_inputs(
        snapshot_id="fcs-old",
        symbol="PEPEUSDT",
        as_of=NOW - 10_000,
        readiness="READY",
    )
    await seed_fcs(repo, snapshot_id="fcs-old", symbol="PEPEUSDT",
                   as_of=NOW - 10_000, created=NOW - 9_000,
                   projection_inputs=old_inputs)
    new_inputs = _snapshot_inputs(
        snapshot_id="fcs-new",
        symbol="PEPEUSDT",
        as_of=NOW,
        readiness="NOT_READY",
        extra={"reasons": ["FUNDING_CURRENT_NON_POSITIVE"]},
    )
    await seed_fcs(repo, snapshot_id="fcs-new", symbol="PEPEUSDT",
                   as_of=NOW, created=NOW,
                   projection_inputs=new_inputs)
    page = await repo.list_current_funding_opportunities(
        OpportunityQuery(readiness="READY"), as_of_ms=NOW + 1_000
    )
    assert all(item["symbol"] != "PEPEUSDT" for item in page.items)
    assert page.total == 0 and page.as_of_ms is None
    # Unfiltered latest is the newer NOT_READY row (never both, never old).
    page_all = await repo.list_current_funding_opportunities(
        OpportunityQuery(), as_of_ms=NOW + 1_000
    )
    pepe = [i for i in page_all.items if i["symbol"] == "PEPEUSDT"]
    assert len(pepe) == 1 and pepe[0]["snapshot_id"] == "fcs-new"
    assert pepe[0]["readiness"] == "NOT_READY"


@pytest.mark.asyncio
async def test_200_history_same_symbol_latest_wins(repo):
    base = NOW - 500_000
    for k in range(200):
        sid = f"hist-{k:03d}"
        as_of = base + k * 1_000
        inputs = _snapshot_inputs(
            snapshot_id=sid, symbol="HISTUSDT", as_of=as_of,
            fcs=float(10 + (k % 80)), readiness="READY" if k % 2 == 0 else "NOT_READY",
        )
        await seed_fcs(repo, snapshot_id=sid, symbol="HISTUSDT",
                       as_of=as_of, created=as_of, projection_inputs=inputs)
    page = await repo.list_current_funding_opportunities(
        OpportunityQuery(symbol="HISTUSDT"), as_of_ms=NOW + 1_000
    )
    assert page.total == 1
    assert len(page.items) == 1
    assert page.items[0]["snapshot_id"] == "hist-199"
    # No 200-row pre-truncation: distinct symbols are all visible.
    for k in range(3):
        inputs = _snapshot_inputs(
            snapshot_id=f"extra-{k}", symbol=f"C{k}USDT", as_of=NOW,
            fcs=float(10 + k),
        )
        await seed_fcs(repo, snapshot_id=f"extra-{k}", symbol=f"C{k}USDT",
                       as_of=NOW, created=NOW + k, projection_inputs=inputs)
    full = await repo.list_current_funding_opportunities(
        OpportunityQuery(limit=200), as_of_ms=NOW + 1_000
    )
    # HIST + 3 extras = 4 current rows (latest per symbol only).
    assert full.total == 4


@pytest.mark.asyncio
async def test_45d_partial_history_class(repo):
    inputs = _snapshot_inputs(
        snapshot_id="young-1", symbol="YOUNGUSDT", as_of=NOW,
        history_class=None, listing_age=45,
    )
    proj = project_opportunity(inputs, NOW + 1_000)
    assert proj["history_class"] == "PARTIAL_90D"
    assert proj["listing_age_days"] == 45
    # Persisted row preserves the young class.
    await seed_fcs(repo, snapshot_id="young-1", symbol="YOUNGUSDT",
                   as_of=NOW, created=NOW, projection_inputs=inputs)
    page = await repo.list_current_funding_opportunities(
        OpportunityQuery(symbol="YOUNGUSDT"), as_of_ms=NOW + 1_000
    )
    assert page.items[0]["history_class"] == "PARTIAL_90D"


@pytest.mark.asyncio
async def test_null_be_sorted_last(repo):
    with_be = _snapshot_inputs(
        snapshot_id="be-yes", symbol="BE1USDT", as_of=NOW, break_even="10",
    )
    await seed_fcs(repo, snapshot_id="be-yes", symbol="BE1USDT",
                   as_of=NOW, created=NOW, projection_inputs=with_be)
    null_be = _snapshot_inputs(
        snapshot_id="be-null", symbol="BE2USDT", as_of=NOW, break_even=None,
    )
    # Explicit null BE stays null (never 0-filled).
    direct = project_opportunity(null_be, NOW + 1_000)
    assert direct["break_even_days"] is None
    await seed_fcs(repo, snapshot_id="be-null", symbol="BE2USDT",
                   as_of=NOW, created=NOW + 1, projection_inputs=null_be)
    for order in ("asc", "desc"):
        page = await repo.list_current_funding_opportunities(
            OpportunityQuery(sort="breakEvenDays", order=order), as_of_ms=NOW + 1_000
        )
        be_items = [i for i in page.items if i["symbol"] in ("BE1USDT", "BE2USDT")]
        assert len(be_items) == 2
        # Nulls always last regardless of direction.
        assert be_items[-1]["break_even_days"] is None
        assert be_items[0]["break_even_days"] == "10"


@pytest.mark.asyncio
async def test_venue_filter(repo):
    spot_inputs = _snapshot_inputs(
        snapshot_id="v-spot", symbol="SPOTUSDT", as_of=NOW,
        venue_quotes=[_venue("BINANCE_SPOT", cost=0.002)],
        best_explicit=None,
    )
    spot_proj = project_opportunity(spot_inputs, NOW + 1_000)
    assert spot_proj["best_venue"] == "BINANCE_SPOT"
    await seed_fcs(repo, snapshot_id="v-spot", symbol="SPOTUSDT",
                   as_of=NOW, created=NOW, projection_inputs=spot_inputs)
    alpha_inputs = _snapshot_inputs(
        snapshot_id="v-alpha", symbol="ALPHAUSDT", as_of=NOW,
        venue_quotes=[_venue("BINANCE_ALPHA", cost=0.003)],
    )
    alpha_proj = project_opportunity(alpha_inputs, NOW + 1_000)
    assert alpha_proj["best_venue"] == "BINANCE_ALPHA"
    await seed_fcs(repo, snapshot_id="v-alpha", symbol="ALPHAUSDT",
                   as_of=NOW, created=NOW + 1, projection_inputs=alpha_inputs)
    page = await repo.list_current_funding_opportunities(
        OpportunityQuery(venue="BINANCE_SPOT"), as_of_ms=NOW + 1_000
    )
    assert {i["symbol"] for i in page.items} == {"SPOTUSDT"}
    assert page.total == 1


@pytest.mark.asyncio
async def test_indicative_never_best_venue(repo):
    only_indicative = _snapshot_inputs(
        snapshot_id="ind-1", symbol="INDUSDT", as_of=NOW,
        venue_quotes=[_venue("ONCHAIN_DEX", cost=0.0001, kind="INDICATIVE")],
    )
    proj = project_opportunity(only_indicative, NOW + 1_000)
    assert proj["best_venue"] is None
    assert proj["readiness"] == "NOT_READY"
    assert "VENUE_UNAVAILABLE" in proj["reasons"]
    # Mixed book: cheap indicative must lose to the executable venue.
    mixed = _snapshot_inputs(
        snapshot_id="mix-1", symbol="MIXUSDT", as_of=NOW,
        venue_quotes=[
            _venue("ONCHAIN_DEX", cost=0.0001, kind="INDICATIVE"),
            _venue("BINANCE_SPOT", cost=0.005),
        ],
    )
    mixed_proj = project_opportunity(mixed, NOW + 1_000)
    assert mixed_proj["best_venue"] == "BINANCE_SPOT"
    await seed_fcs(repo, snapshot_id="ind-1", symbol="INDUSDT",
                   as_of=NOW, created=NOW, projection_inputs=only_indicative)
    page = await repo.list_current_funding_opportunities(
        OpportunityQuery(symbol="INDUSDT"), as_of_ms=NOW + 1_000
    )
    assert page.items[0]["best_venue"] is None


@pytest.mark.asyncio
async def test_expiry_dynamic_not_ready_and_include_stale(repo):
    exp = NOW + 500
    inputs = _snapshot_inputs(
        snapshot_id="exp-1", symbol="EXPUSDT", as_of=NOW,
        expires_at=exp, readiness="READY",
    )
    await seed_fcs(repo, snapshot_id="exp-1", symbol="EXPUSDT",
                   as_of=NOW, created=NOW, projection_inputs=inputs)
    # Fresh read before expiry keeps READY.
    fresh = await repo.list_current_funding_opportunities(
        OpportunityQuery(symbol="EXPUSDT"), as_of_ms=NOW + 100
    )
    assert len(fresh.items) == 1 and fresh.items[0]["readiness"] == "READY"
    assert fresh.items[0]["stale"] is False
    # Boundary is inclusive: query == expires is already stale.
    at = await repo.list_current_funding_opportunities(
        OpportunityQuery(symbol="EXPUSDT", include_stale=True), as_of_ms=exp
    )
    assert at.items[0]["stale"] is True and at.items[0]["readiness"] == "NOT_READY"
    # Expired rows are hidden by default and surface NOT_READY for research.
    hidden = await repo.list_current_funding_opportunities(
        OpportunityQuery(symbol="EXPUSDT"), as_of_ms=NOW + 1_000
    )
    assert hidden.total == 0 and len(hidden.items) == 0
    shown = await repo.list_current_funding_opportunities(
        OpportunityQuery(symbol="EXPUSDT", include_stale=True), as_of_ms=NOW + 1_000
    )
    assert len(shown.items) == 1
    assert shown.items[0]["stale"] is True
    assert shown.items[0]["readiness"] == "NOT_READY"
    assert "STALE" in shown.items[0]["reasons"]


@pytest.mark.asyncio
async def test_legacy_marked_not_ready(repo):
    await seed_fcs(repo, snapshot_id="leg-1", symbol="LEGUSDT",
                   as_of=NOW - 5_000, created=NOW - 4_000, legacy=True)
    page = await repo.list_current_funding_opportunities(
        OpportunityQuery(symbol="LEGUSDT", include_stale=True), as_of_ms=NOW + 1_000
    )
    assert len(page.items) == 1
    item = page.items[0]
    assert item["readiness"] == "NOT_READY"
    assert "LEGACY" in item["reasons"]
    assert item["fcs"] is None and item["best_venue"] is None
    # Legacy rows never appear as READY.
    ready = await repo.list_current_funding_opportunities(
        OpportunityQuery(readiness="READY"), as_of_ms=NOW + 1_000
    )
    assert all(i["symbol"] != "LEGUSDT" for i in ready.items)
    # Direct projection of an empty snapshot is also LEGACY.
    direct = project_opportunity(
        {"snapshot_id": "leg-x", "symbol": "LEGUSDT", "as_of_ms": NOW}, NOW + 1_000
    )
    assert direct["readiness"] == "NOT_READY"
    assert "LEGACY" in direct["reasons"]


@pytest.mark.asyncio
async def test_total_consistent_and_stable_pagination(repo):
    # Distinct FCS values give a deterministic numeric order; pages must
    # agree on total and never overlap/drift across repeats.
    specs = (("AAAUSDT", 71.0), ("BBBUSDT", 73.0), ("CCCUSDT", 72.0))
    for sym, fcs in specs:
        inputs = _snapshot_inputs(
            snapshot_id=f"tie-{sym}", symbol=sym, as_of=NOW, fcs=fcs,
        )
        await seed_fcs(repo, snapshot_id=f"tie-{sym}", symbol=sym,
                       as_of=NOW, created=NOW, projection_inputs=inputs)
    first = await repo.list_current_funding_opportunities(
        OpportunityQuery(sort="fcs", order="desc", limit=2, offset=0), as_of_ms=NOW + 1_000
    )
    second = await repo.list_current_funding_opportunities(
        OpportunityQuery(sort="fcs", order="desc", limit=2, offset=2), as_of_ms=NOW + 1_000
    )
    assert first.total == 3 == second.total
    assert len(first.items) == 2 and len(second.items) == 1
    # Numeric desc: BBB(73), CCC(72) then AAA(71).
    assert [i["symbol"] for i in first.items] == ["BBBUSDT", "CCCUSDT"]
    assert [i["symbol"] for i in second.items] == ["AAAUSDT"]
    # No overlap and union covers all.
    assert {i["symbol"] for i in (*first.items, *second.items)} == {"AAAUSDT", "BBBUSDT", "CCCUSDT"}
    repeat = await repo.list_current_funding_opportunities(
        OpportunityQuery(sort="fcs", order="desc", limit=2, offset=0), as_of_ms=NOW + 1_000
    )
    assert [i["snapshot_id"] for i in repeat.items] == [i["snapshot_id"] for i in first.items]
    # page.as_of_ms is the earliest source cutoff, empty pages carry null.
    assert first.as_of_ms == NOW
    empty = await repo.list_current_funding_opportunities(
        OpportunityQuery(symbol="MISSING"), as_of_ms=NOW + 1_000
    )
    assert empty.total == 0 and empty.as_of_ms is None


def test_rank_ties_break_by_symbol_id_stable():
    # Equal-score tie-breaks live in rank_opportunities (pure, null-last).
    from diveintocrypto_desktop.shortlab.hedge.models import FCSResult
    from diveintocrypto_desktop.shortlab.hedge.opportunity import rank_opportunities

    def _fcs(sym: str, snap: str) -> FCSResult:
        return FCSResult(
            snapshot_id=snap, symbol=sym, canonical_id=sym.lower(), as_of_ms=NOW,
            fcs_version="fcs_v2", fcs_config_hash=HASH,
            reference_notional_usd="10000", fcs=70.0,
            module_scores={}, funding_metrics={}, venue_summary={},
            basis=None, risk={}, readiness="READY", reasons=(), created_at_ms=NOW,
        )

    out = rank_opportunities(
        [_fcs("BBBUSDT", "id-b"), _fcs("AAAUSDT", "id-a"), _fcs("CCCUSDT", "id-c")],
        sort="fcs", order="desc",
    )
    assert [r.symbol for r in out] == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    again = rank_opportunities(
        [_fcs("CCCUSDT", "id-c"), _fcs("BBBUSDT", "id-b"), _fcs("AAAUSDT", "id-a")],
        sort="fcs", order="desc",
    )
    assert [r.snapshot_id for r in again] == [r.snapshot_id for r in out]


def test_projection_writes_all_fields_and_real_expiry():
    early = NOW + 60_000
    late = NOW + 120_000
    inputs = _snapshot_inputs(
        snapshot_id="full-1", symbol="FULLUSDT", as_of=NOW,
        venue_quotes=[
            _venue("BINANCE_SPOT", cost=0.005, expires=late),
            _venue("BINANCE_ALPHA", cost=0.002, expires=early),
        ],
    )
    # Cheapest executable wins regardless of input order.
    proj = project_opportunity(inputs, NOW + 1_000)
    assert proj["funding_7d"] == "0.004"
    assert proj["funding_30d"] == "0.018"
    assert proj["positive_ratio_30d"] == "0.85"
    assert proj["fcs_config_hash"] == HASH
    assert proj["conservative_apr"] == "0.25"
    assert proj["best_venue"] == "BINANCE_ALPHA"
    assert proj["break_even_days"] == "12.5"
    assert proj["history_class"] == "FULL_90D"
    # Real expiry is the earliest Funding/Mark/Quote expiry (never an
    # invented +1800s refresh): ALPHA 60s beats SPOT 120s and the row hint.
    assert proj["expires_at_ms"] == early
    assert proj["stale"] is False
    assert proj["reasons"] == sorted(proj["reasons"])


def test_projection_real_expiry_is_earliest_quote():
    q1 = _venue("BINANCE_SPOT", cost=0.002, expires=NOW + 100_000)
    q2 = _venue("BINANCE_ALPHA", cost=0.003, expires=NOW + 50_000)
    inputs = _snapshot_inputs(
        snapshot_id="exp-q", symbol="QUSDT", as_of=NOW,
        venue_quotes=[q1, q2],
    )
    inputs.pop("expires_at_ms", None)
    proj = project_opportunity(inputs, NOW + 1_000)
    assert proj["expires_at_ms"] == NOW + 50_000


def test_projection_default_hold30_no_fictitious_term():
    from tests.repair_fixtures import make_funding_context

    ctx = make_funding_context(conservative_apr="0.02")
    snap: dict[str, Any] = {
        "snapshot_id": "be-hold",
        "symbol": "HOLDUSDT",
        "canonical_id": "hold",
        "as_of_ms": NOW,
        "fcs": 80.0,
        "fcs_config_hash": HASH,
        "funding_7d": "0.01",
        "funding_30d": "0.02",
        "positive_ratio_30d": "0.9",
        "history_class": "FULL_90D",
        "listing_age_days": 120,
        "conservative_apr": "0.02",
        "funding_context": ctx,
        "actual_futures_notional_usd": "10000",
        "entry_fee_usd": "15",
        "exit_fee_usd": "15",
        "slippage_usd": "0",
        "gas_usd": "0",
        "fees_included": True,
        "spot_cash_usd": "0",
        "margin_usd": "12000",
        "reserve_fraction": "0.05",
        "cost_policy": {"min_net_carry_usd": "0"},
        "venue_quotes": [_venue("BINANCE_SPOT")],
        "expires_at_ms": NOW + 1_800_000,
        "readiness_breakdown": _ready_breakdown("READY"),
        "reasons": [],
    }
    proj = project_opportunity(snap, NOW + 1_000)
    # N=10000 APR=0.02 roundtrip=30 hold=30 -> BE=54.75 (V07 carriers).
    assert proj["break_even_days"] is not None
    assert abs(Decimal(str(proj["break_even_days"])) - Decimal("54.75")) <= Decimal("0.00000001")
    # An explicit hold is honoured; an absent hold never invents 7/90.
    snap7 = dict(snap, snapshot_id="be-hold-7", hold_days=7)
    proj7 = project_opportunity(snap7, NOW + 1_000)
    assert proj7["break_even_days"] == proj["break_even_days"]


def test_build_risk_json_envelope():
    inputs = _snapshot_inputs(snapshot_id="env-1", symbol="ENVUSDT", as_of=NOW)
    risk = build_risk_json(inputs, NOW + 1_000, extra={"identity": {"canonical_id": "env"}})
    assert set(risk) == {"identity", "projection_v2"}
    assert risk["projection_v2"]["snapshot_id"] == "env-1"
    with pytest.raises(ValueError):
        build_projection_v2({"symbol": "MISSING_ID"}, NOW)
