"""Task 16: 7D/30D/90D forward grader + evidence metrics (Phase 4).

Covers design section 21 / 21.1-21.4 and the Task 16 checklist:

- entry/exit are the first *complete* 1h bar opens after the score time /
  after maturity; ``price = 1 - exit/entry``; ``carry = SUM(rate*mark/entry)``;
  ``net = price + carry - fee - slippage``; MAE/MFE only over bars with
  ``entry_ts < close_ts <= exit_ts`` (entry bar in, post-exit bar out);
  cost + formula hashes are persisted.
- A funding event without ``mark_price`` yields ``FUNDING_MARK_MISSING`` with
  funding/net null and price kept; live contracts report ``UNAVAILABLE``,
  delisted contracts stay ``CENSORED``.
- Default cost is two-sided fee 0.001 + two-sided slippage 0.002 = 0.003; a
  changed cost only mints another outcome row.
- Not-due ``PENDING``, missing-bar ``UNAVAILABLE``, delisted ``CENSORED``
  with a last tradable price, no reliable exit price never ``COMPLETE``;
  the four state counts always sum to the sample size.
- ``build_symbol(end_ms)`` is never used for backfill: only the archived
  score/feature snapshot is read.
- The Task 14 router (untouched) flips ``/api/short/evidence/summary`` from
  ``503 short_evidence_unavailable`` to a real ``200`` once the metrics
  provider is supplied through the service interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from diveintocrypto_desktop.shortlab.config import (
    cost_config_hash,
    load_shortlab_config,
)
from diveintocrypto_desktop.shortlab.evidence import grader, metrics
from diveintocrypto_desktop.shortlab.evidence.grader import (
    CONTRACT_DELISTED,
    FORMULA_VERSION,
    FUNDING_MARK_MISSING,
    HORIZON_MS,
    NO_EXIT_BAR,
    NO_EXIT_PRICE,
    PENDING_NOT_DUE,
)
from diveintocrypto_desktop.shortlab.evidence.metrics import build_metrics_provider
from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
from diveintocrypto_desktop.shortlab.repository import (
    REQUIRED_FEATURE_META_FIELDS,
    FeatureSnapshotRecord,
    ReferenceNotFoundError,
    ScoreSnapshotRecord,
    ShortLabRepository,
)
from diveintocrypto_desktop.shortlab.service import (
    EVIDENCE_UNAVAILABLE_REASON,
    EvidenceSummary,
    ShortLabService,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000

GRID0 = 1_699_999_200_000  # aligned 1h boundary
SCORE_AS_OF = 1_700_000_000_000  # 800s after GRID0
ENTRY_TS = GRID0 + HOUR_MS  # first 1h open strictly after SCORE_AS_OF
DUE_7D = SCORE_AS_OF + HORIZON_MS["7D"]
EXIT_7D_TS = DUE_7D - (DUE_7D % HOUR_MS) + HOUR_MS  # first 1h open strictly after due
AFTER_DUE = DUE_7D + 2 * HOUR_MS


class FakeClock:
    def __init__(self, start_ms: int = AFTER_DUE) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return int(self.ms)


def _meta(asof: int = SCORE_AS_OF) -> dict[str, Any]:
    return {
        name: {
            "status": "OK",
            "fetched_at_ms": asof - 60_000,
            "as_of_ms": asof,
            "coverage_fraction": 1.0,
            "reason_code": None,
            "source": "unit-test",
        }
        for name in REQUIRED_FEATURE_META_FIELDS
    }


async def _open_repo(tmp_path: Path) -> ShortLabRepository:
    repo = await ShortLabRepository.open(db_path=tmp_path / "shortlab.duckdb")
    await repo.migrate()
    return repo


async def _seed_score(
    repo: ShortLabRepository,
    symbol: str,
    generation_id: str,
    *,
    as_of: int = SCORE_AS_OF,
    ltss: float = 80.0,
) -> str:
    feat_id = f"feat-{symbol}-{as_of}"
    await repo.save_feature(
        FeatureSnapshotRecord(
            snapshot_id=feat_id,
            symbol=symbol,
            as_of_ms=as_of,
            feature_version="features-v1",
            features={"funding_30d": 0.01},
            source_meta=_meta(as_of),
            data_quality=90.0,
        )
    )
    score_id = f"score-{symbol}-{as_of}-{generation_id}"
    await repo.save_score_batch(
        [
            ScoreSnapshotRecord(
                snapshot_id=score_id,
                generation_id=generation_id,
                feature_snapshot_id=feat_id,
                entry_snapshot_id=None,
                symbol=symbol,
                as_of_ms=as_of,
                analysis_tier="LITE",
                profile="GENERAL_LITE",
                score_version="ltss-lite-v1",
                entry_version=None,
                feature_version="features-v1",
                config_hash="cfg",
                ltss=ltss,
                entry_score=None,
                data_quality=90.0,
                candidate_status="CANDIDATE",
                execution_status="NOT_READY",
                status="CANDIDATE",
                module_scores={"carry": 30.0},
                vetoes=(),
                pauses=(),
                reasons=(),
                warnings=(),
            )
        ],
        job_id=generation_id,
        job_type="score_refresh",
        started_at_ms=as_of - 1_000,
        finished_at_ms=as_of + 1_000,
        stats={"as_of_ms": as_of},
    )
    return score_id


# ---------------------------------------------------------------------------
# Fake market builders (deterministic, offline)
# ---------------------------------------------------------------------------


def make_bars(
    count: int,
    *,
    entry_open: float = 100.0,
    exit_open: float = 90.0,
    exit_high: float = 1_000.0,
    exit_low: float = 0.5,
) -> list[dict[str, Any]]:
    """Hourly bars from GRID0; exit bar sits at EXIT_7D_TS (excluded from MAE)."""
    bars: list[dict[str, Any]] = []
    for k in range(count):
        open_ms = GRID0 + k * HOUR_MS
        if open_ms == ENTRY_TS:
            bars.append({"t": open_ms, "o": entry_open, "h": 102.0, "l": 98.0, "c": 100.0})
        elif open_ms == EXIT_7D_TS:
            bars.append({"t": open_ms, "o": exit_open, "h": exit_high, "l": exit_low, "c": exit_open})
        elif k == 50:
            bars.append({"t": open_ms, "o": 100.0, "h": 110.0, "l": 97.0, "c": 100.0})
        elif k == 80:
            bars.append({"t": open_ms, "o": 100.0, "h": 101.0, "l": 80.0, "c": 100.0})
        elif k == 0:
            # Before the score time: must never enter MAE/MFE.
            bars.append({"t": open_ms, "o": 100.0, "h": 101.0, "l": 1.0, "c": 100.0})
        else:
            bars.append({"t": open_ms, "o": 100.0, "h": 102.0, "l": 98.0, "c": 100.0})
    return bars


def make_funding(
    entry_ts: int,
    exit_ts: int,
    *,
    rate: float = 0.0001,
    mark: float = 100.0,
    step_ms: int = 8 * HOUR_MS,
    bad_mark_at: int | None = None,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    moment = entry_ts + step_ms
    while moment <= exit_ts:
        events.append(
            {
                "t": moment,
                "funding_rate": rate,
                "mark_price": None if moment == bad_mark_at else mark,
            }
        )
        moment += step_ms
    return events


def klines_fn_factory(bars: list[dict[str, Any]]):
    async def _fn(symbol: str, interval: str, start_ms: int, end_ms: int):
        assert interval == "1h"
        return [b for b in bars if int(start_ms) <= int(b["t"]) <= int(end_ms)]

    return _fn


def funding_fn_factory(events: list[dict[str, Any]]):
    async def _fn(symbol: str, start_ms: int, end_ms: int):
        return [e for e in events if int(start_ms) <= int(e["t"]) <= int(end_ms)]

    return _fn


async def lifecycle_live(symbol: str) -> None:
    return None


def lifecycle_delisted_factory(last_tradable_ms: int, last_price: float):
    async def _fn(symbol: str) -> dict[str, Any]:
        return {
            "is_delisted": True,
            "status": "DELISTED",
            "last_tradable_ms": last_tradable_ms,
            "last_price": last_price,
        }

    return _fn


# ---------------------------------------------------------------------------
# 1. Entry/exit anchoring, return formula, MAE/MFE window, hashes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_exit_returns_mae_mfe_and_hashes(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    score_id = await _seed_score(repo, "BTCUSDT", "gen-1")
    bars = make_bars(200)
    events = make_funding(ENTRY_TS, EXIT_7D_TS)
    assert len(events) == 21  # 7D at 8h settlements

    outcome = await grader.grade(
        score_id,
        "7D",
        AFTER_DUE,
        repository=repo,
        klines_fn=klines_fn_factory(bars),
        funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_live,
    )

    assert outcome.outcome_status == "COMPLETE"
    assert outcome.reason_code is None
    # First complete 1h opens strictly after the anchor instants.
    assert outcome.entry_ts_ms == ENTRY_TS
    assert outcome.exit_ts_ms == EXIT_7D_TS
    assert outcome.horizon_due_ms == DUE_7D
    assert outcome.entry_price == pytest.approx(100.0)
    assert outcome.exit_price == pytest.approx(90.0)

    price = 1.0 - 90.0 / 100.0
    carry = sum(e["funding_rate"] * e["mark_price"] / 100.0 for e in events)
    assert outcome.price_short_return == pytest.approx(price)
    assert outcome.funding_carry == pytest.approx(carry)
    # Default research cost: two-sided fee 0.001 + two-sided slippage 0.002.
    assert outcome.fee_assumption == pytest.approx(0.001)
    assert outcome.slippage_assumption == pytest.approx(0.002)
    assert outcome.fee_assumption + outcome.slippage_assumption == pytest.approx(0.003)
    assert outcome.net_short_return == pytest.approx(price + carry - 0.003)
    # MAE from the k=50 bar high (110); MFE from the k=80 bar low (80).
    # The exit bar (high 1000 / low 0.5) and the pre-entry bar (low 1.0)
    # are outside entry_ts < close_ts <= exit_ts and must not leak in.
    assert outcome.mae == pytest.approx(0.10)
    assert outcome.mfe == pytest.approx(0.20)
    # Formula + cost hashes are persisted for audit.
    assert outcome.formula_version == FORMULA_VERSION == "forward-v1"
    assert outcome.cost_config_hash == cost_config_hash(load_shortlab_config())
    assert outcome.funding_event_count == 21
    assert outcome.funding_coverage == pytest.approx(1.0)

    stored = await repo.get_outcome(score_id, "7D", FORMULA_VERSION, outcome.cost_config_hash)
    assert stored is not None and stored.net_short_return == pytest.approx(price + carry - 0.003)
    await repo.close()


# ---------------------------------------------------------------------------
# 2. Funding mark missing: price kept, funding/net null
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_funding_mark_missing_live_is_unavailable(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    score_id = await _seed_score(repo, "BTCUSDT", "gen-1")
    bars = make_bars(200)
    bad_at = ENTRY_TS + 8 * HOUR_MS
    events = make_funding(ENTRY_TS, EXIT_7D_TS, bad_mark_at=bad_at)

    outcome = await grader.grade(
        score_id,
        "7D",
        AFTER_DUE,
        repository=repo,
        klines_fn=klines_fn_factory(bars),
        funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_live,
    )

    assert outcome.outcome_status == "UNAVAILABLE"
    assert outcome.reason_code == FUNDING_MARK_MISSING
    assert outcome.funding_carry is None
    assert outcome.net_short_return is None
    # Price return is independent of the funding leg.
    assert outcome.price_short_return == pytest.approx(0.10)
    assert outcome.entry_price == pytest.approx(100.0)
    assert outcome.exit_price == pytest.approx(90.0)
    await repo.close()


@pytest.mark.asyncio
async def test_funding_mark_missing_delisted_stays_censored(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    score_id = await _seed_score(repo, "BTCUSDT", "gen-1")
    bars = make_bars(200)
    last_tradable = ENTRY_TS + 3 * DAY_MS
    events = make_funding(
        ENTRY_TS, last_tradable, bad_mark_at=ENTRY_TS + 8 * HOUR_MS
    )

    outcome = await grader.grade(
        score_id,
        "7D",
        AFTER_DUE,
        repository=repo,
        klines_fn=klines_fn_factory(bars),
        funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_delisted_factory(last_tradable, 95.0),
    )

    assert outcome.outcome_status == "CENSORED"
    assert outcome.reason_code == FUNDING_MARK_MISSING
    assert outcome.funding_carry is None
    assert outcome.net_short_return is None
    assert outcome.price_short_return == pytest.approx(1.0 - 95.0 / 100.0)
    await repo.close()


# ---------------------------------------------------------------------------
# 3. Cost versioning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_change_mints_new_outcome_version(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    score_id = await _seed_score(repo, "BTCUSDT", "gen-1")
    bars = make_bars(200)
    events = make_funding(ENTRY_TS, EXIT_7D_TS)
    klines_fn = klines_fn_factory(bars)
    funding_fn = funding_fn_factory(events)

    default_outcome = await grader.grade(
        score_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn, funding_fn=funding_fn, lifecycle_fn=lifecycle_live,
    )
    custom_outcome = await grader.grade(
        score_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn, funding_fn=funding_fn, lifecycle_fn=lifecycle_live,
        fee_assumption=0.01, slippage_assumption=0.02,
        cost_config_hash="test-cost-v2",
    )

    assert custom_outcome.cost_config_hash == "test-cost-v2"
    assert custom_outcome.cost_config_hash != default_outcome.cost_config_hash
    assert custom_outcome.net_short_return == pytest.approx(
        default_outcome.net_short_return - (0.03 - 0.003)
    )
    rows = await repo.list_outcomes_for_score(score_id)
    assert len(rows) == 2
    assert {r.cost_config_hash for r in rows} == {
        default_outcome.cost_config_hash,
        "test-cost-v2",
    }
    # The original row is untouched.
    again = await repo.get_outcome(
        score_id, "7D", FORMULA_VERSION, default_outcome.cost_config_hash
    )
    assert again is not None and again.net_short_return == pytest.approx(
        default_outcome.net_short_return
    )
    await repo.close()


# ---------------------------------------------------------------------------
# 4. PENDING / UNAVAILABLE / CENSORED / never-COMPLETE-without-exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_before_due(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    score_id = await _seed_score(repo, "BTCUSDT", "gen-1")

    async def _boom(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("no market fetch before maturity")

    outcome = await grader.grade(
        score_id, "7D", DUE_7D - HOUR_MS, repository=repo,
        klines_fn=_boom, funding_fn=_boom, lifecycle_fn=lifecycle_live,
    )
    assert outcome.outcome_status == "PENDING"
    assert outcome.reason_code == PENDING_NOT_DUE
    assert outcome.horizon_due_ms == DUE_7D
    assert outcome.entry_price is None and outcome.exit_price is None
    await repo.close()


@pytest.mark.asyncio
async def test_missing_exit_bar_is_unavailable(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    score_id = await _seed_score(repo, "BTCUSDT", "gen-1")
    bars = [b for b in make_bars(200) if int(b["t"]) <= DUE_7D - 10 * HOUR_MS]
    events = make_funding(ENTRY_TS, EXIT_7D_TS)

    outcome = await grader.grade(
        score_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn_factory(bars), funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_live,
    )
    assert outcome.outcome_status == "UNAVAILABLE"
    assert outcome.reason_code == NO_EXIT_BAR
    assert outcome.exit_price is None
    assert outcome.net_short_return is None
    await repo.close()


@pytest.mark.asyncio
async def test_delisted_is_censored_with_last_tradable_price(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    score_id = await _seed_score(repo, "BTCUSDT", "gen-1")
    bars = make_bars(200)
    last_tradable = ENTRY_TS + 3 * DAY_MS
    events = make_funding(ENTRY_TS, last_tradable)

    outcome = await grader.grade(
        score_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn_factory(bars), funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_delisted_factory(last_tradable, 95.0),
    )

    assert outcome.outcome_status == "CENSORED"
    assert outcome.reason_code == CONTRACT_DELISTED
    assert outcome.exit_ts_ms == last_tradable
    assert outcome.exit_price == pytest.approx(95.0)
    assert outcome.price_short_return == pytest.approx(0.05)
    carry = sum(e["funding_rate"] * e["mark_price"] / 100.0 for e in events)
    assert outcome.funding_carry == pytest.approx(carry)
    assert outcome.net_short_return == pytest.approx(0.05 + carry - 0.003)
    await repo.close()


@pytest.mark.asyncio
async def test_unreliable_exit_price_is_never_complete(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    score_id = await _seed_score(repo, "BTCUSDT", "gen-1")
    bars = make_bars(200, exit_open=0.0)
    events = make_funding(ENTRY_TS, EXIT_7D_TS)

    outcome = await grader.grade(
        score_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn_factory(bars), funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_live,
    )
    assert outcome.outcome_status == "UNAVAILABLE"
    assert outcome.reason_code == NO_EXIT_PRICE
    assert outcome.outcome_status != "COMPLETE"
    assert outcome.net_short_return is None
    await repo.close()


@pytest.mark.asyncio
async def test_bad_horizon_and_missing_score_raise(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    await _seed_score(repo, "BTCUSDT", "gen-1")
    with pytest.raises(ValueError):
        await grader.grade("score-nope", "12H", AFTER_DUE, repository=repo)
    with pytest.raises(ReferenceNotFoundError):
        await grader.grade("score-nope", "7D", AFTER_DUE, repository=repo)
    await repo.close()


# ---------------------------------------------------------------------------
# 5. No build_symbol backfill: archived snapshots are the only history source
# ---------------------------------------------------------------------------


def test_grader_never_backfills_with_build_symbol() -> None:
    import re

    for module in (grader, metrics):
        path = Path(module.__file__)
        text = path.read_text(encoding="utf-8")
        # Forbid actual use (calls / imports); prose may name the helper to
        # document the prohibition.
        assert "build_symbol(" not in text, f"{path} must not call build_symbol"
        assert re.search(r"^\s*(from|import)\s+\S*symbol_builder", text, re.M) is None, (
            f"{path} must not import symbol_builder"
        )
        assert re.search(r"^\s*(from|import)\s+\S*scan\b", text, re.M) is None, (
            f"{path} must not import legacy scan modules"
        )


@pytest.mark.asyncio
async def test_grading_leaves_archived_snapshots_untouched(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    score_id = await _seed_score(repo, "BTCUSDT", "gen-1")
    before_score = await repo.get_score(score_id)
    before_feature = await repo.get_feature(before_score.feature_snapshot_id)
    bars = make_bars(200)
    events = make_funding(ENTRY_TS, EXIT_7D_TS)

    await grader.grade(
        score_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn_factory(bars), funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_live,
    )

    assert await repo.get_score(score_id) == before_score
    assert await repo.get_feature(before_score.feature_snapshot_id) == before_feature
    await repo.close()


# ---------------------------------------------------------------------------
# 6. Metrics: four-state counts always sum to the sample size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_four_states_sum_to_sample(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    config = load_shortlab_config()
    bars = make_bars(200)

    ok_id = await _seed_score(repo, "S_COMPLETE", "gen-4")
    await _seed_score(repo, "S_PENDING", "gen-4")
    bad_id = await _seed_score(repo, "S_UNAVAIL", "gen-4")
    out_id = await _seed_score(repo, "S_CENSOR", "gen-4")

    await grader.grade(
        ok_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn_factory(bars),
        funding_fn=funding_fn_factory(make_funding(ENTRY_TS, EXIT_7D_TS)),
        lifecycle_fn=lifecycle_live,
    )
    await grader.grade(
        bad_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn_factory([]),
        funding_fn=funding_fn_factory([]),
        lifecycle_fn=lifecycle_live,
    )
    last_tradable = ENTRY_TS + 3 * DAY_MS
    await grader.grade(
        out_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn_factory(bars),
        funding_fn=funding_fn_factory(make_funding(ENTRY_TS, last_tradable)),
        lifecycle_fn=lifecycle_delisted_factory(last_tradable, 95.0),
    )
    # S_PENDING is never graded: it counts as PENDING (NOT_GRADED).

    report = await metrics.summary(repo, {"horizon": "7D"}, config=config)
    assert isinstance(report, EvidenceSummary)
    bucket = report.horizons["7D"]
    assert bucket["COMPLETE"] == 1
    assert bucket["PENDING"] == 1
    assert bucket["CENSORED"] == 1
    assert bucket["UNAVAILABLE"] == 1
    assert (
        bucket["PENDING"] + bucket["COMPLETE"] + bucket["CENSORED"] + bucket["UNAVAILABLE"]
        == bucket["total"] == 4 == report.total
    )
    assert bucket["meanNetReturn"] is not None

    full = await metrics.summary(repo, {}, config=config)
    assert set(full.horizons) == {"7D", "30D", "90D"}
    for name, per_horizon in full.horizons.items():
        assert (
            per_horizon["PENDING"] + per_horizon["COMPLETE"]
            + per_horizon["CENSORED"] + per_horizon["UNAVAILABLE"]
            == per_horizon["total"] == 4
        )
    assert full.total == 12
    await repo.close()


# ---------------------------------------------------------------------------
# 7. Task 14 router (unchanged): 503 -> real 200 with four-state counts
# ---------------------------------------------------------------------------


class _StubRuntime:
    def __init__(self, service: ShortLabService, *, available: bool = True) -> None:
        self._service = service
        self.available = available
        self.unavailable_reason = "shortlab_unavailable"

    @property
    def service(self) -> ShortLabService:
        return self._service

    @property
    def config(self) -> Any:
        return self._service.config

    @property
    def registry(self) -> Any:
        return self._service.registry


def _make_app(service: ShortLabService) -> FastAPI:
    from diveintocrypto_desktop.api.shortlab import router as short_router

    app = FastAPI()
    app.include_router(short_router)
    app.state.shortlab_runtime = _StubRuntime(service)
    return app


@pytest.mark.asyncio
async def test_router_evidence_503_then_real_200(tmp_path: Path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path)
    config = load_shortlab_config()
    service = ShortLabService(
        config=config, repository=repo, registry=ProviderRegistry(), clock=clock,
    )
    app = _make_app(service)

    with TestClient(app) as client:
        response = client.get("/api/short/evidence/summary")
        assert response.status_code == 503
        assert response.json()["error"] == EVIDENCE_UNAVAILABLE_REASON

    # Wire the real Task 16 metrics through the service interface (no router
    # change): the same route now returns genuine aggregates.
    ok_id = await _seed_score(repo, "R_COMPLETE", "gen-r")
    bad_id = await _seed_score(repo, "R_UNAVAIL", "gen-r")
    bars = make_bars(200)
    await grader.grade(
        ok_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn_factory(bars),
        funding_fn=funding_fn_factory(make_funding(ENTRY_TS, EXIT_7D_TS)),
        lifecycle_fn=lifecycle_live,
    )
    await grader.grade(
        bad_id, "7D", AFTER_DUE, repository=repo,
        klines_fn=klines_fn_factory([]),
        funding_fn=funding_fn_factory([]),
        lifecycle_fn=lifecycle_live,
    )
    service._metrics_provider = build_metrics_provider(repo, config=config, clock=clock)  # noqa: SLF001

    with TestClient(app) as client:
        response = client.get("/api/short/evidence/summary?horizon=7D")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        bucket = body["horizons"]["7D"]
        assert bucket["COMPLETE"] == 1
        assert bucket["UNAVAILABLE"] == 1
        assert (
            bucket["PENDING"] + bucket["COMPLETE"] + bucket["CENSORED"]
            + bucket["UNAVAILABLE"] == bucket["total"] == 2
        )
        assert "generatedAtMs" in body
    await repo.close()
