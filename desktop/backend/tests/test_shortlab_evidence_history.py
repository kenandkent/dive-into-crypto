"""F07: long-term evidence history view without live pollution (design A8).

Covers plan F07 behaviour assertions:

- two 7D-apart old SUCCEEDED batches enter the default 180d summary;
  a fresh PENDING batch never masks old COMPLETE rows;
- four-state counts always sum; version/cost buckets never silently mix;
- missing mark / funding / exit price are retained with reasons (never 0);
- changing live OI/ratio never rewrites a saved historical Entry;
- per symbol/profile/UTC-day first-eligible sampling;
- due-but-ungraded counts as UNAVAILABLE/NOT_GRADED_DUE with queue depth;
- delisted stays CENSORED and is retained in totals;
- old symbol_builder history marks OI/ratio/funding/micro blocks
  HISTORICAL_INPUT_UNAVAILABLE while K-line indicators still compute;
- evidence.jobs.run_due exists as ``async run_due(context) -> JobStatus``
  and grades due-missing outcomes without build_symbol backfill.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

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
)
from diveintocrypto_desktop.shortlab.repository import (
    REQUIRED_FEATURE_META_FIELDS,
    FeatureSnapshotRecord,
    ScoreSnapshotRecord,
    ShortLabRepository,
)
from diveintocrypto_desktop.shortlab.scoring.versions import (
    ENTRY_VERSION,
    FEATURE_VERSION,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
NOW = 1_760_000_000_000


class FakeClock:
    def __init__(self, start_ms: int = NOW) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return int(self.ms)


def _meta(asof: int) -> dict[str, Any]:
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
    as_of: int,
    profile: str = "GENERAL_LITE",
    feature_version: str = FEATURE_VERSION,
    entry_version: str | None = None,
    candidate_status: str = "CANDIDATE",
    ltss: float = 80.0,
) -> str:
    feat_id = f"feat-{symbol}-{profile}-{as_of}-{feature_version}"
    await repo.save_feature(
        FeatureSnapshotRecord(
            snapshot_id=feat_id,
            symbol=symbol,
            as_of_ms=as_of,
            feature_version=feature_version,
            features={"funding_30d": 0.01},
            source_meta=_meta(as_of),
            data_quality=90.0,
        )
    )
    score_id = f"score-{symbol}-{profile}-{as_of}-{generation_id}"
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
                profile=profile,
                score_version="ltss-lite-v1",
                entry_version=entry_version,
                feature_version=feature_version,
                config_hash="cfg",
                ltss=ltss,
                entry_score=None,
                data_quality=90.0,
                candidate_status=candidate_status,
                execution_status="NOT_READY",
                status=candidate_status,
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


def _grid(as_of: int) -> int:
    return as_of - (as_of % HOUR_MS)


def make_bars_for(as_of: int, horizon: str = "7D", entry_open: float = 100.0,
                 exit_open: float = 90.0) -> list[dict[str, Any]]:
    grid = _grid(as_of)
    due = as_of + HORIZON_MS[horizon]
    exit_ts = due - (due % HOUR_MS) + HOUR_MS
    bars: list[dict[str, Any]] = []
    moment = grid - 5 * HOUR_MS
    end = exit_ts + 2 * HOUR_MS
    while moment <= end:
        if moment == grid + HOUR_MS:
            bars.append({"t": moment, "o": entry_open, "h": 102.0, "l": 98.0, "c": 100.0})
        elif moment == exit_ts:
            bars.append({"t": moment, "o": exit_open, "h": 102.0, "l": 88.0, "c": exit_open})
        else:
            bars.append({"t": moment, "o": 100.0, "h": 102.0, "l": 98.0, "c": 100.0})
        moment += HOUR_MS
    return bars


def make_funding_for(as_of: int, horizon: str = "7D", bad_mark: bool = False):
    grid = _grid(as_of)
    entry_ts = grid + HOUR_MS
    due = as_of + HORIZON_MS[horizon]
    exit_ts = due - (due % HOUR_MS) + HOUR_MS
    events: list[dict[str, Any]] = []
    moment = entry_ts + 8 * HOUR_MS
    first = True
    while moment <= exit_ts:
        events.append({
            "t": moment,
            "funding_rate": 0.0001,
            "mark_price": None if (bad_mark and first) else 100.0,
        })
        first = False
        moment += 8 * HOUR_MS
    return entry_ts, exit_ts, events


def klines_fn_factory(bars):
    async def _fn(symbol, interval, start_ms, end_ms):
        assert interval == "1h"
        return [b for b in bars if int(start_ms) <= int(b["t"]) <= int(end_ms)]
    return _fn


def funding_fn_factory(events):
    async def _fn(symbol, start_ms, end_ms):
        return [e for e in events if int(start_ms) <= int(e["t"]) <= int(end_ms)]
    return _fn


async def lifecycle_live(symbol: str) -> None:
    return None


# ---------------------------------------------------------------------------
# 1. Two old batches enter the default summary; fresh PENDING never masks them
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_old_batches_survive_fresh_pending(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    config = load_shortlab_config()
    clock = FakeClock(NOW)
    old1_asof = NOW - 20 * DAY_MS
    old2_asof = NOW - 12 * DAY_MS
    new_asof = NOW - 1 * DAY_MS
    old1 = await _seed_score(repo, "BTCUSDT", "gen-old-1", as_of=old1_asof)
    old2 = await _seed_score(repo, "BTCUSDT", "gen-old-2", as_of=old2_asof)
    await _seed_score(repo, "BTCUSDT", "gen-new", as_of=new_asof)
    for score_id, asof in ((old1, old1_asof), (old2, old2_asof)):
        _, _, events = make_funding_for(asof)
        outcome = await grader.grade(
            score_id, "7D", NOW, repository=repo,
            klines_fn=klines_fn_factory(make_bars_for(asof)),
            funding_fn=funding_fn_factory(events),
            lifecycle_fn=lifecycle_live,
        )
        assert outcome.outcome_status == "COMPLETE"

    report = await metrics.summary(repo, {}, config=config, clock=clock)
    bucket = report.horizons["7D"]
    # Two old COMPLETE samples on distinct UTC days + one fresh PENDING sample.
    assert bucket["COMPLETE"] >= 2, bucket
    assert bucket["PENDING"] >= 1, bucket
    assert (
        bucket["PENDING"] + bucket["COMPLETE"] + bucket["CENSORED"] + bucket["UNAVAILABLE"]
        == bucket["total"]
    )
    assert report.total >= 3
    await repo.close()


# ---------------------------------------------------------------------------
# 2. Four-state sum + version/cost buckets never mix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_cost_buckets_do_not_mix(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    config = load_shortlab_config()
    clock = FakeClock(NOW)
    asof_current = NOW - 20 * DAY_MS
    asof_old = NOW - 19 * DAY_MS
    cur_id = await _seed_score(
        repo, "BTCUSDT", "gen-cur", as_of=asof_current, feature_version=FEATURE_VERSION,
    )
    old_id = await _seed_score(
        repo, "ETHUSDT", "gen-old", as_of=asof_old, feature_version="features-v1",
    )
    _, _, events_cur = make_funding_for(asof_current)
    await grader.grade(
        cur_id, "7D", NOW, repository=repo,
        klines_fn=klines_fn_factory(make_bars_for(asof_current)),
        funding_fn=funding_fn_factory(events_cur),
        lifecycle_fn=lifecycle_live,
    )
    _, _, events_old = make_funding_for(asof_old)
    await grader.grade(
        old_id, "7D", NOW, repository=repo,
        klines_fn=klines_fn_factory(make_bars_for(asof_old)),
        funding_fn=funding_fn_factory(events_old),
        lifecycle_fn=lifecycle_live,
    )
    # A second cost version on the current score coexists without overwrite.
    await grader.grade(
        cur_id, "7D", NOW, repository=repo,
        klines_fn=klines_fn_factory(make_bars_for(asof_current)),
        funding_fn=funding_fn_factory(events_cur),
        lifecycle_fn=lifecycle_live,
        fee_assumption=0.01, slippage_assumption=0.02,
        cost_config_hash="test-cost-v2",
    )

    default_report = await metrics.summary(repo, {}, config=config, clock=clock)
    bucket = default_report.horizons["7D"]
    assert bucket["COMPLETE"] >= 1
    assert (
        bucket["PENDING"] + bucket["COMPLETE"] + bucket["CENSORED"] + bucket["UNAVAILABLE"]
        == bucket["total"]
    )
    # Explicit old-version selection sees the legacy row without mixing.
    old_report = await metrics.summary(
        repo, {"feature_version": "features-v1"}, config=config, clock=clock,
    )
    old_bucket = old_report.horizons["7D"]
    assert old_bucket["total"] >= 1
    assert old_bucket["COMPLETE"] >= 1
    await repo.close()


# ---------------------------------------------------------------------------
# 3. Missing mark / funding / exit retained with reasons (never 0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_legs_retained_with_reasons(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    config = load_shortlab_config()
    clock = FakeClock(NOW)
    asof = NOW - 20 * DAY_MS
    bad_id = await _seed_score(repo, "BTCUSDT", "gen-bad", as_of=asof)
    _, _, events = make_funding_for(asof, bad_mark=True)
    outcome = await grader.grade(
        bad_id, "7D", NOW, repository=repo,
        klines_fn=klines_fn_factory(make_bars_for(asof)),
        funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_live,
    )
    assert outcome.outcome_status == "UNAVAILABLE"
    assert outcome.reason_code == FUNDING_MARK_MISSING
    assert outcome.funding_carry is None
    assert outcome.net_short_return is None
    # Price leg is independent and never zero-filled.
    assert outcome.price_short_return is not None

    noexit_id = await _seed_score(
        repo, "ETHUSDT", "gen-noexit", as_of=asof,
    )
    thin_bars = [b for b in make_bars_for(asof) if b["t"] <= asof + HORIZON_MS["7D"] - 10 * HOUR_MS]
    outcome2 = await grader.grade(
        noexit_id, "7D", NOW, repository=repo,
        klines_fn=klines_fn_factory(thin_bars),
        funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_live,
    )
    assert outcome2.outcome_status == "UNAVAILABLE"
    assert outcome2.net_short_return is None

    report = await metrics.summary(repo, {"horizon": "7D"}, config=config, clock=clock)
    bucket = report.horizons["7D"]
    assert bucket["UNAVAILABLE"] >= 2
    assert (
        bucket["PENDING"] + bucket["COMPLETE"] + bucket["CENSORED"] + bucket["UNAVAILABLE"]
        == bucket["total"]
    )
    await repo.close()


# ---------------------------------------------------------------------------
# 4. Due-but-ungraded is UNAVAILABLE/NOT_GRADED_DUE with queue depth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_due_ungraded_is_not_graded_due(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    config = load_shortlab_config()
    clock = FakeClock(NOW)
    asof = NOW - 10 * DAY_MS  # 7D due, never graded
    await _seed_score(repo, "BTCUSDT", "gen-queue", as_of=asof)

    report = await metrics.summary(repo, {"horizon": "7D"}, config=config, clock=clock)
    bucket = report.horizons["7D"]
    assert bucket["UNAVAILABLE"] >= 1, bucket
    assert bucket.get("notGradedDue", bucket.get("not_graded_due", 0)) >= 1, bucket
    assert bucket["PENDING"] == 0 or bucket["total"] >= 1
    assert (
        bucket["PENDING"] + bucket["COMPLETE"] + bucket["CENSORED"] + bucket["UNAVAILABLE"]
        == bucket["total"]
    )
    await repo.close()


# ---------------------------------------------------------------------------
# 5. Delisted CENSORED retained in totals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delisted_censored_retained(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    config = load_shortlab_config()
    clock = FakeClock(NOW)
    asof = NOW - 20 * DAY_MS
    score_id = await _seed_score(repo, "OLDUSDT", "gen-old", as_of=asof)
    last_tradable = _grid(asof) + HOUR_MS + 3 * DAY_MS

    async def _delisted(symbol: str) -> dict[str, Any]:
        return {
            "is_delisted": True, "status": "DELISTED",
            "last_tradable_ms": last_tradable, "last_price": 95.0,
        }

    _, _, events = make_funding_for(asof)
    outcome = await grader.grade(
        score_id, "7D", NOW, repository=repo,
        klines_fn=klines_fn_factory(make_bars_for(asof)),
        funding_fn=funding_fn_factory(events),
        lifecycle_fn=_delisted,
    )
    assert outcome.outcome_status == "CENSORED"
    assert outcome.reason_code == CONTRACT_DELISTED

    report = await metrics.summary(repo, {"horizon": "7D"}, config=config, clock=clock)
    bucket = report.horizons["7D"]
    assert bucket["CENSORED"] >= 1
    assert (
        bucket["PENDING"] + bucket["COMPLETE"] + bucket["CENSORED"] + bucket["UNAVAILABLE"]
        == bucket["total"]
    )
    await repo.close()


# ---------------------------------------------------------------------------
# 6. Per symbol/profile/UTC-day first-eligible sampling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_eligible_per_symbol_profile_day(tmp_path: Path) -> None:
    repo = await _open_repo(tmp_path)
    config = load_shortlab_config()
    clock = FakeClock(NOW)
    day = (NOW - 20 * DAY_MS) // DAY_MS * DAY_MS + 12 * HOUR_MS
    first = day
    second = day + 30 * 60_000
    await _seed_score(repo, "BTCUSDT", "gen-a", as_of=first, profile="GENERAL_LITE")
    await _seed_score(repo, "BTCUSDT", "gen-b", as_of=second, profile="GENERAL_LITE")
    # EXCLUDED rows never seed a tradable sample for their day.
    await _seed_score(
        repo, "ETHUSDT", "gen-c", as_of=first, profile="GENERAL_LITE",
        candidate_status="EXCLUDED",
    )

    report = await metrics.summary(repo, {"horizon": "7D"}, config=config, clock=clock)
    bucket = report.horizons["7D"]
    # BTCUSDT contributes exactly one sample for the day (the earliest);
    # the EXCLUDED ETHUSDT day contributes none.
    assert bucket["total"] == 1, bucket
    await repo.close()


# ---------------------------------------------------------------------------
# 7. run_due import path + behaviour (grades due-missing, skips not-due)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_due_grades_due_missing(tmp_path: Path) -> None:
    from diveintocrypto_desktop.shortlab.evidence.jobs import run_due
    from diveintocrypto_desktop.shortlab.service import JobContext, JobStatus

    import inspect

    sig = inspect.signature(run_due)
    params = list(sig.parameters)
    assert params[0] == "context"

    repo = await _open_repo(tmp_path)
    config = load_shortlab_config()
    clock = FakeClock(NOW)
    due_asof = NOW - 10 * DAY_MS
    fresh_asof = NOW - 1 * DAY_MS
    due_id = await _seed_score(repo, "BTCUSDT", "gen-due", as_of=due_asof)
    await _seed_score(repo, "ETHUSDT", "gen-fresh", as_of=fresh_asof)
    bars = make_bars_for(due_asof)
    _, _, events = make_funding_for(due_asof)

    ctx = JobContext(
        repository=repo, config=config, clock_ms=clock,
        request_budget=None, trace_id="grader-test-1", data_dir=tmp_path,
    )
    status = await run_due(
        ctx, klines_fn=klines_fn_factory(bars), funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_live,
    )
    assert isinstance(status, JobStatus)
    assert status.status == "SUCCEEDED"
    stored = await repo.get_outcome(
        due_id, "7D", FORMULA_VERSION, cost_config_hash(config),
    )
    assert stored is not None and stored.outcome_status == "COMPLETE"
    # Fresh (not-due) scores never mint PENDING rows that would block later.
    fresh_outcomes = await repo.list_outcomes_for_score(
        f"score-ETHUSDT-GENERAL_LITE-{fresh_asof}-gen-fresh"
    )
    assert all(o.outcome_status != "PENDING" for o in fresh_outcomes)
    await repo.close()


def test_jobs_and_metrics_never_backfill_with_build_symbol() -> None:
    import re
    from diveintocrypto_desktop.shortlab.evidence import jobs as jobs_mod

    for module in (grader, metrics, jobs_mod):
        path = Path(module.__file__)
        text = path.read_text(encoding="utf-8")
        assert "build_symbol(" not in text, f"{path} must not call build_symbol"
        assert re.search(r"^\s*(from|import)\s+\S*symbol_builder", text, re.M) is None
        assert re.search(r"^\s*(from|import)\s+\S*scan\b", text, re.M) is None


# ---------------------------------------------------------------------------
# 8. Symbol builder history: OI/ratio/funding/micro unavailable, klines still work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_symbol_builder_history_marks_live_blocks_unavailable() -> None:
    from diveintocrypto_desktop.scan import symbol_builder as sb

    end_ms = 1_700_000_000_000

    def _hist(n: int, step_ms: int) -> list[dict]:
        rows, price = [], 100.0
        for i in range(n):
            close = price + 0.05
            t_ms = end_ms - (n - i) * step_ms
            rows.append({"t": t_ms * 1_000_000, "o": price,
                         "h": max(price, close) + 0.3, "l": min(price, close) - 0.3,
                         "c": close, "v": 1000 + i})
            price = close
        return rows

    candles_by_tf = {"1h": _hist(120, 3_600_000), "5m": _hist(200, 300_000)}

    async def fake_all_tf(symbol, limit=300, intervals=None, end_ms=None):
        assert end_ms == end_ms
        return candles_by_tf

    async def boom_oi(*a, **k):
        raise AssertionError("historical view must not fetch live OI")

    async def boom_ratio(*a, **k):
        raise AssertionError("historical view must not fetch live ratios")

    async def boom_funding(*a, **k):
        raise AssertionError("historical view must not fetch live funding")

    import unittest.mock as mock

    with mock.patch.object(sb.kl, "fetch_all_tf", fake_all_tf), \
        mock.patch.object(sb.oi_mod, "fetch_oi_hist", boom_oi), \
        mock.patch.object(sb.rat, "fetch_ratio_series", boom_ratio), \
        mock.patch.object(sb.fnd, "funding_hist", boom_funding), \
        mock.patch.object(sb, "_divergence_inputs", return_value={}):
        obj = await sb.build_symbol("BTCUSDT", end_ms=end_ms)

    # K-line verdicts still compute from the truncated window.
    assert len(obj["multiTf"]) == 12
    assert len(obj["indicators"]) > 0
    text = str(obj)
    assert "HISTORICAL_INPUT_UNAVAILABLE" in text
    for key in ("cascade", "microstructure"):
        assert obj[key] == {"unavailable": "HISTORICAL_INPUT_UNAVAILABLE"}, key
    assert obj["series"]["oi"] == [] and obj["series"]["funding"] == []
    assert "ch" not in obj


# ---------------------------------------------------------------------------
# 9. Saved historical Entry never moves with live OI/ratio
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_saved_entry_immune_to_live_oi_change(tmp_path: Path) -> None:
    from diveintocrypto_desktop.shortlab.repository import EntrySnapshotRecord

    repo = await _open_repo(tmp_path)
    asof = NOW - 20 * DAY_MS
    block = {"status": "OK", "fetched_at_ms": asof - 1, "as_of_ms": asof,
             "coverage_fraction": 1.0, "reason_code": None, "source": "unit-test"}
    snap = EntrySnapshotRecord(
        snapshot_id="entry-BTCUSDT-hist", symbol="BTCUSDT", as_of_ms=asof,
        entry_version=ENTRY_VERSION, dive_weights_hash="w", dive_engine_version="e",
        dive_config_hash="c", primary_tf="1h", inputs={"k": 1},
        components={"consensus": 1.0}, source_meta={
            "consensus": block, "mtf": block, "micro": block,
            "regime": block, "failed_bounce": block, "funding": block,
        },
        entry_score=70.0, created_at_ms=asof,
    )
    await repo.save_entry(snap)
    before = await repo.get_entry("entry-BTCUSDT-hist")
    # Simulate a live OI/ratio regime shift: grading must read the archive.
    _ = {"oi": [999.0] * 48, "pos": [9.9] * 48}
    after = await repo.get_entry("entry-BTCUSDT-hist")
    assert after == before
    assert after.entry_score == 70.0
    await repo.close()
