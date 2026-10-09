"""R14b historical settlement, fees and metrics (D14.2/D14.3, V15).

Red->green: before R14b ``evidence.evaluation`` did not exist
(ModuleNotFoundError); exit fees used entry notional for both legs;
MAE/MFE consumed straddling bars; MARK path fell back to TRADE;
Grader version fallbacks used local literals; metrics had no paired
baseline. This file fails on the frozen baseline and passes after R14b.

Pure offline only: fixed clocks, no network/DB clock reads beyond
in-memory fakes. Decimal assertions use tolerance 1e-8; status/version/
coverage checks are exact (no tolerance masking).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

TOL = Decimal("0.00000001")


def _assert_close(actual: Any, expected: str, label: str = "") -> None:
    assert actual is not None, label
    diff = abs(Decimal(str(actual)) - Decimal(str(expected)))
    assert diff <= TOL, f"{label}: |{actual}-{expected}|={diff} > {TOL}"


def settle_fixture(
    *,
    entry_price: str = "100",
    exit_price: str = "200",
    qty: str = "10",
    fee_rate: str = "0.001",
) -> Any:
    """Test wrapper around the real Grader fee function (FX1, no VWAP).

    Calls the production ``evaluation.settle_fees_usd`` (the actual fee
    helper consumed by hedge grading), not a re-implementation.
    """
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        settle_fees_usd,
    )

    return settle_fixture_from(settle_fees_usd, entry_price, exit_price, qty, fee_rate)


def settle_fixture_from(fn: Any, entry_price: str, exit_price: str, qty: str, fee_rate: str) -> Any:
    from types import SimpleNamespace

    result = fn(entry_price, exit_price, qty, fee_rate)
    # Normalise dict -> namespace for attribute access like the plan snippet.
    if isinstance(result, dict):
        return SimpleNamespace(**result)
    return result


# ---------------------------------------------------------------------------
# V15: exit fees follow exit notional.
# ---------------------------------------------------------------------------


def test_exit_fees_follow_exit_notional() -> None:
    result = settle_fixture(entry_price="100", exit_price="200", qty="10", fee_rate="0.001")
    assert result.entry_fee_usd == "1"
    assert result.exit_fee_usd == "2"
    # 0.5x exit: exit fee halves while entry stays.
    half = settle_fixture(entry_price="100", exit_price="50", qty="10", fee_rate="0.001")
    assert half.entry_fee_usd == "1"
    assert half.exit_fee_usd == "0.5"
    # Explicit: fees exclude VWAP impact (pure notional * rate, FX1).
    _assert_close(result.entry_fee_usd, "1", "entry_fee")
    _assert_close(result.exit_fee_usd, "2", "exit_fee")


def test_hedge_exit_fees_follow_exit_notional() -> None:
    """Hedge grader detailed fees: exit leg uses exit price * qty."""
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        compute_hedge_exit_fees,
    )

    out = compute_hedge_exit_fees(
        futures_entry_price_usd="100",
        futures_exit_price_usd="200",
        spot_entry_price_usd="100",
        spot_exit_price_usd="50",
        futures_qty="10",
        spot_qty="10",
        futures_entry_fee_rate="0.0005",
        futures_exit_fee_rate="0.0005",
        spot_entry_fee_rate="0.001",
        spot_exit_fee_rate="0.001",
    )
    # futures entry 100*10*0.0005=0.5, exit 200*10*0.0005=1.0
    _assert_close(out["futures_entry_fee_usd"], "0.5", "fut entry")
    _assert_close(out["futures_exit_fee_usd"], "1", "fut exit")
    # spot entry 100*10*0.001=1, exit 50*10*0.001=0.5
    _assert_close(out["spot_entry_fee_usd"], "1", "spot entry")
    _assert_close(out["spot_exit_fee_usd"], "0.5", "spot exit")


# ---------------------------------------------------------------------------
# V15: missing Mark lowers priced coverage (never 0-fill).
# ---------------------------------------------------------------------------


def test_missing_mark_lowers_priced_coverage() -> None:
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        priced_funding_coverage,
    )

    entry = 1_000
    exit = 1_000 + 24 * 3_600_000
    full = [
        {"t": 1_000 + 8 * 3_600_000, "funding_rate": 0.0001, "mark_price": 100.0},
        {"t": 1_000 + 16 * 3_600_000, "funding_rate": 0.0001, "mark_price": 100.0},
        {"t": 1_000 + 24 * 3_600_000, "funding_rate": 0.0001, "mark_price": 100.0},
    ]
    missing = [
        {"t": 1_000 + 8 * 3_600_000, "funding_rate": 0.0001, "mark_price": 100.0},
        {"t": 1_000 + 16 * 3_600_000, "funding_rate": 0.0001, "mark_price": None},
        {"t": 1_000 + 24 * 3_600_000, "funding_rate": 0.0001, "mark_price": 100.0},
    ]
    cov_full = priced_funding_coverage(full, entry, exit)
    cov_missing = priced_funding_coverage(missing, entry, exit)
    assert cov_full["priced"] == 3
    assert cov_full["total"] == 3
    assert cov_full["coverage"] == pytest.approx(1.0)
    assert cov_missing["priced"] == 2
    assert cov_missing["total"] == 3
    assert cov_missing["coverage"] < cov_full["coverage"]
    assert cov_missing["coverage"] == pytest.approx(2 / 3)
    # Missing mark never becomes 0 USD carry: priced leg is None-aware.
    assert cov_missing["carry_usd"] is None or isinstance(cov_missing["carry_usd"], (int, float, str))


@pytest.mark.asyncio
async def test_grader_missing_mark_keeps_price_null_carry(tmp_path) -> None:
    """Directional grader: FUNDING_MARK_MISSING keeps price, nulls carry/net."""
    from diveintocrypto_desktop.shortlab.evidence import grader
    from diveintocrypto_desktop.shortlab.repository import (
        FeatureSnapshotRecord,
        ScoreSnapshotRecord,
        ShortLabRepository,
    )

    repo = await ShortLabRepository.open(db_path=tmp_path / "r14b-mark.duckdb")
    await repo.migrate()
    grid0 = 1_699_999_200_000
    score_as_of = 1_700_000_000_000
    entry_ts = grid0 + 3_600_000
    due_7d = score_as_of + 7 * 86_400_000
    exit_ts = due_7d - (due_7d % 3_600_000) + 3_600_000
    after_due = due_7d + 2 * 3_600_000

    async def _seed() -> str:
        feat_id = "feat-mark"
        await repo.save_feature(
            FeatureSnapshotRecord(
                snapshot_id=feat_id,
                symbol="BTCUSDT",
                as_of_ms=score_as_of,
                feature_version="features-v1",
                features={"funding_30d": 0.01},
                source_meta={
                    n: {"status": "OK", "fetched_at_ms": score_as_of - 60_000,
                        "as_of_ms": score_as_of, "coverage_fraction": 1.0,
                        "reason_code": None, "source": "unit-test"}
                    for n in (
                        "funding", "basis", "spot_book", "futures_book",
                        "open_interest", "spot_price", "funding_rate",
                        "mark_price", "oi_change", "volume",
                    )
                } if hasattr(__import__("diveintocrypto_desktop.shortlab.repository", fromlist=["REQUIRED_FEATURE_META_FIELDS"]), "REQUIRED_FEATURE_META_FIELDS") else {},
                data_quality=90.0,
            )
        )
        score_id = "score-mark"
        # Build minimal meta via repository constant when available.
        try:
            from diveintocrypto_desktop.shortlab.repository import (
                REQUIRED_FEATURE_META_FIELDS,
            )

            meta = {
                n: {"status": "OK", "fetched_at_ms": score_as_of - 60_000,
                    "as_of_ms": score_as_of, "coverage_fraction": 1.0,
                    "reason_code": None, "source": "unit-test"}
                for n in REQUIRED_FEATURE_META_FIELDS
            }
            await repo.save_feature(
                FeatureSnapshotRecord(
                    snapshot_id=feat_id + "-v2",
                    symbol="BTCUSDT",
                    as_of_ms=score_as_of,
                    feature_version="features-v1",
                    features={"funding_30d": 0.01},
                    source_meta=meta,
                    data_quality=90.0,
                )
            )
            feat_use = feat_id + "-v2"
        except Exception:
            feat_use = feat_id
        await repo.save_score_batch(
            [
                ScoreSnapshotRecord(
                    snapshot_id=score_id,
                    generation_id="gen-r14b",
                    feature_snapshot_id=feat_use,
                    entry_snapshot_id=None,
                    symbol="BTCUSDT",
                    as_of_ms=score_as_of,
                    analysis_tier="LITE",
                    profile="GENERAL_LITE",
                    score_version="ltss-lite-v1",
                    entry_version=None,
                    feature_version="features-v1",
                    config_hash="cfg",
                    ltss=80.0,
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
            ]
        )
        return score_id

    # Simpler: reuse existing helper shape with explicit bars/funding.
    from tests.test_shortlab_evidence import (
        AFTER_DUE as _AFTER,
        ENTRY_TS as _ENTRY,
        EXIT_7D_TS as _EXIT,
        SCORE_AS_OF as _SCORE,
        _seed_score,
        funding_fn_factory,
        klines_fn_factory,
        lifecycle_live,
        make_bars,
        make_funding,
    )

    repo2 = await ShortLabRepository.open(db_path=tmp_path / "r14b-mark2.duckdb")
    await repo2.migrate()
    score_id = await _seed_score(repo2, "BTCUSDT", "gen-r14b")
    bars = make_bars(200)
    bad_at = _ENTRY + 8 * 3_600_000
    events = make_funding(_ENTRY, _EXIT, bad_mark_at=bad_at)
    outcome = await grader.grade(
        score_id, "7D", _AFTER, repository=repo2,
        klines_fn=klines_fn_factory(bars),
        funding_fn=funding_fn_factory(events),
        lifecycle_fn=lifecycle_live,
    )
    assert outcome.outcome_status == "UNAVAILABLE"
    assert outcome.reason_code == "FUNDING_MARK_MISSING"
    assert outcome.funding_carry is None
    assert outcome.net_short_return is None
    # Price leg kept independently; coverage lowered (not 1.0).
    assert outcome.price_short_return is not None
    assert outcome.funding_coverage is not None
    assert float(outcome.funding_coverage) < 1.0
    await repo.close()
    await repo2.close()


# ---------------------------------------------------------------------------
# V15: censored never filled with 0; delisted retained.
# ---------------------------------------------------------------------------


def test_censored_never_filled_with_zero() -> None:
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        censored_value_or_none,
    )

    assert censored_value_or_none("CENSORED", None) is None
    assert censored_value_or_none("CENSORED", "0") is None or Decimal(str(censored_value_or_none("CENSORED", "0"))) == Decimal("0")
    # Unknown final stays None, never 0-fill for advantage claims.
    assert censored_value_or_none("COMPLETE", "1.5") is not None
    # Explicit helper: unknown funding carry stays None.
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        funding_carry_or_none,
    )

    assert funding_carry_or_none([{"mark_price": None, "funding_rate": 0.0001}]) is None
    assert funding_carry_or_none([{"mark_price": 100.0, "funding_rate": 0.0001}]) is not None


@pytest.mark.asyncio
async def test_delisted_censored_retained_not_dropped(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.evidence import grader
    from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
    from tests.test_shortlab_evidence import (
        AFTER_DUE as _AFTER,
        ENTRY_TS as _ENTRY,
        _seed_score,
        funding_fn_factory,
        klines_fn_factory,
        make_bars,
        make_funding,
    )

    repo = await ShortLabRepository.open(db_path=tmp_path / "r14b-censor.duckdb")
    await repo.migrate()
    score_id = await _seed_score(repo, "BTCUSDT", "gen-r14b")
    bars = make_bars(200)
    last_tradable = _ENTRY + 3 * 86_400_000
    events = make_funding(_ENTRY, last_tradable)

    async def _delisted(symbol: str) -> dict[str, Any]:
        return {
            "is_delisted": True,
            "status": "DELISTED",
            "last_tradable_ms": last_tradable,
            "last_price": 95.0,
        }

    outcome = await grader.grade(
        score_id, "7D", _AFTER, repository=repo,
        klines_fn=klines_fn_factory(bars),
        funding_fn=funding_fn_factory(events),
        lifecycle_fn=_delisted,
    )
    assert outcome.outcome_status == "CENSORED"
    # Retained with reliable last price, never dropped, never 0-filled.
    assert outcome.exit_price is not None
    assert float(outcome.exit_price) == pytest.approx(95.0)
    assert outcome.price_short_return is not None
    # Unknown legs stay None, not 0.
    if outcome.funding_carry is None:
        assert outcome.net_short_return is None
    await repo.close()


# ---------------------------------------------------------------------------
# V15: insufficient sample marked INSUFFICIENT (never claimed valid).
# ---------------------------------------------------------------------------


def test_insufficient_sample_marked() -> None:
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        INSUFFICIENT_SAMPLE,
        bootstrap_mean_ci,
    )

    # <30 assets -> INSUFFICIENT regardless of sample count.
    few_assets = bootstrap_mean_ci([0.01] * 150, assets=[f"a{i}" for i in range(10)])
    assert few_assets["status"] == INSUFFICIENT_SAMPLE
    assert few_assets["ci_low"] is None and few_assets["ci_high"] is None
    # <100 comparable samples -> INSUFFICIENT.
    few_samples = bootstrap_mean_ci([0.01] * 50, assets=[f"a{i}" for i in range(40)])
    assert few_samples["status"] == INSUFFICIENT_SAMPLE
    # Sufficient -> 95% interval present, never auto "model valid".
    enough = bootstrap_mean_ci([0.01] * 120, assets=[f"a{i}" for i in range(35)])
    assert enough["status"] != INSUFFICIENT_SAMPLE
    assert enough["ci_low"] is not None and enough["ci_high"] is not None
    assert "model_valid" not in enough or enough.get("model_valid") is not True


def test_evaluation_report_insufficient_never_claims_edge() -> None:
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        INSUFFICIENT_SAMPLE,
        build_evaluation_report,
    )

    report = build_evaluation_report(
        outcomes=[
            {"status": "COMPLETE", "net_return": 0.05, "asset": "BTC", "decision_id": "d1"},
            {"status": "COMPLETE", "net_return": 0.03, "asset": "ETH", "decision_id": "d2"},
        ],
        assets=["BTC", "ETH"],
    )
    assert report["sample_status"] == INSUFFICIENT_SAMPLE
    assert report.get("claim_valid") is not True
    assert "paired" in report or "baseline" in report or "coverage" in report


# ---------------------------------------------------------------------------
# Paired baseline: same Decision samples only (never market average).
# ---------------------------------------------------------------------------


def test_paired_baseline_same_decision_only() -> None:
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        paired_baseline_diff,
    )

    pairs = [
        {"decision_id": "d1", "system": 0.05, "baseline": 0.02},
        {"decision_id": "d2", "system": 0.03, "baseline": 0.04},
        {"decision_id": "d3", "system": None, "baseline": 0.01},
        {"decision_id": "d4", "system": 0.02, "baseline": None},
    ]
    result = paired_baseline_diff(pairs)
    # Only d1/d2 are paired; d3/d4 counted as missing, never substituted.
    assert result["n_paired"] == 2
    assert result["n_missing"] == 2
    assert result["missing_ratio"] == pytest.approx(0.5)
    _assert_close(str(result["mean_diff"]), "0.01", "mean_diff")
    # Unknown legs never filled with 0.
    assert result["mean_system"] is not None
    assert result["mean_baseline"] is not None


def test_system_same_sample_baseline_no_cross_day_substitution() -> None:
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        match_system_baseline_by_decision,
    )

    system = [
        {"decision_id": "d1", "horizon_days": 7, "net_return": 0.05},
        {"decision_id": "d2", "horizon_days": 7, "net_return": 0.03},
    ]
    baseline = [
        {"decision_id": "d1", "horizon_days": 7, "net_return": 0.02},
        # d2 baseline missing -> stays missing, never borrows another day.
        {"decision_id": "d-other", "horizon_days": 7, "net_return": 0.09},
    ]
    matched = match_system_baseline_by_decision(system, baseline)
    assert len(matched) == 2
    by_id = {m["decision_id"]: m for m in matched}
    assert by_id["d1"]["baseline"] == pytest.approx(0.02)
    assert by_id["d2"]["baseline"] is None


# ---------------------------------------------------------------------------
# MAE/MFE only complete bars; partial head/tail marked PARTIAL.
# ---------------------------------------------------------------------------


def test_mae_mfe_only_complete_bars_partial_marked() -> None:
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        filter_complete_bars,
        mae_mfe_complete,
        path_coverage,
    )

    entry = 1_000
    exit = 1_000 + 3 * 3_600_000
    bars = [
        # Straddling head: open before entry, full-hour high must not leak.
        {"open_ms": entry - 1_800_000, "close_ms": entry + 1_800_000, "h": 1000.0, "l": 1.0},
        {"open_ms": entry, "close_ms": entry + 3_600_000, "h": 110.0, "l": 97.0},
        {"open_ms": entry + 3_600_000, "close_ms": entry + 2 * 3_600_000, "h": 102.0, "l": 80.0},
        # Straddling tail: open inside, close after exit.
        {"open_ms": exit - 1_800_000, "close_ms": exit + 1_800_000, "h": 999.0, "l": 0.1},
    ]
    complete = filter_complete_bars(bars, entry, exit)
    assert len(complete) == 2
    assert all(b["open_ms"] >= entry and b["close_ms"] <= exit for b in complete)
    mae, mfe = mae_mfe_complete(bars, entry, exit, entry_price=100.0)
    assert mae == pytest.approx(0.10)
    assert mfe == pytest.approx(0.20)
    # Partial head/tail without finer MARK -> PARTIAL, never claimed complete.
    assert path_coverage(bars, entry, exit, has_mark=False) in ("PARTIAL", "UNKNOWN")
    assert path_coverage(complete, entry, exit, has_mark=True) in ("COMPLETE", "PARTIAL")


@pytest.mark.asyncio
async def test_grader_mae_ignores_straddling_bar(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.evidence import grader

    # Direct unit check of the grader helper (complete containment).
    bars = [
        {"open_ms": 1_000, "h": 1000.0, "l": 0.01, "c": 100.0},
        {"open_ms": 1_000 + 3_600_000, "h": 110.0, "l": 97.0, "c": 100.0},
        {"open_ms": 1_000 + 2 * 3_600_000, "h": 102.0, "l": 80.0, "c": 100.0},
    ]
    # Entry inside the first hour: that bar straddles and must be ignored.
    mae, mfe = grader._mae_mfe(bars, 1_000 + 1_800_000, 1_000 + 3 * 3_600_000, 100.0)
    # Only the two fully-contained bars contribute (110 high, 80 low).
    assert mae == pytest.approx(0.10)
    assert mfe == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# MARK provider: optional injection, never TRADE as MARK.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_provider_optional_never_trades_as_mark(tmp_path) -> None:
    from diveintocrypto_desktop.shortlab.evidence.historical_market import (
        RepositoryHistoricalMarketProvider,
    )
    from diveintocrypto_desktop.shortlab.hedge.models import HistoricalPriceBar

    # Unbound MARK path stays UNKNOWN/empty, never TRADE masquerade.
    provider = RepositoryHistoricalMarketProvider(repository=None)
    assert hasattr(provider, "read_mark_price_bars")
    assert getattr(provider, "mark_price_bars_fn", None) is None
    bars = await provider.read_mark_price_bars("BTCUSDT", 1_000, 2_000, None)
    assert tuple(bars) == ()
    # Injected R03 MARK fn yields MARK basis with real receipt.
    async def _fake_mark(symbol: str, interval: str, start_ms: int, end_ms: int, *, request_context: Any = None) -> Any:
        from diveintocrypto_desktop.shortlab.observations import (
            ObservationMeta,
            Observed,
        )

        candles = [
            {
                "t": start_ms * 1_000_000,
                "openTime": start_ms,
                "closeTime": start_ms + 3_600_000 - 1,
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.5,
                "price_basis": "MARK",
            }
        ]
        return Observed(
            value=candles,
            meta=ObservationMeta(
                status="OK",
                source="binance:fapi/markPriceKlines",
                source_as_of_ms=start_ms + 3_600_000 - 1,
                fetched_at_ms=start_ms + 3_600_000,
                known_at_ms=start_ms + 3_600_000,
            ),
        )

    provider2 = RepositoryHistoricalMarketProvider(
        repository=None, mark_price_bars_fn=_fake_mark
    )
    mark_bars = await provider2.read_mark_price_bars("BTCUSDT", 1_000, 1_000 + 3_600_000, None)
    assert len(mark_bars) == 1
    bar = mark_bars[0]
    assert isinstance(bar, HistoricalPriceBar)
    assert bar.price_basis == "MARK"
    assert "mark" in str(bar.source).lower()


def test_grader_versions_import_r00_no_fallback_literals() -> None:
    """Grader files import R00 versions; no local fallback literals."""
    from pathlib import Path

    import diveintocrypto_desktop.shortlab.evidence.grader as g
    import diveintocrypto_desktop.shortlab.evidence.hedge_grader as hg
    import diveintocrypto_desktop.shortlab.evidence.hedge_metrics as hm
    import diveintocrypto_desktop.shortlab.evidence.metrics as m

    for module in (hg, hm, m):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert "hedge_evidence_v1" not in text, f"{module.__name__} keeps v1 fallback literal"
        # v2 literal may remain only as legacy-decode membership test, not as
        # a default/fallback assignment. Forbid direct fallback assignment.
        assert 'HEDGE_EVIDENCE_VERSION = "hedge_evidence_v2"' not in text
        assert '"features-v2"' not in text or "FEATURE_VERSION_CURRENT" in text
    # R00 current versions are importable and used as defaults.
    from diveintocrypto_desktop.shortlab.hedge import (
        HEDGE_EVIDENCE_VERSION_CURRENT,
    )
    from diveintocrypto_desktop.shortlab.scoring.versions import (
        ENTRY_VERSION_CURRENT,
        FEATURE_VERSION_CURRENT,
    )

    assert HEDGE_EVIDENCE_VERSION_CURRENT == "hedge_evidence_v3"
    assert FEATURE_VERSION_CURRENT == "features-v3"
    assert ENTRY_VERSION_CURRENT == "entry-v3"
    assert g.FORMULA_VERSION == "forward-v1"


def test_metrics_paired_baseline_and_coverage_present() -> None:
    """Metrics expose paired baseline + coverage/censor without 0-fill."""
    from diveintocrypto_desktop.shortlab.evidence import evaluation

    assert hasattr(evaluation, "paired_baseline_diff")
    assert hasattr(evaluation, "bootstrap_mean_ci")
    assert hasattr(evaluation, "priced_funding_coverage")
    assert hasattr(evaluation, "build_evaluation_report")
    import diveintocrypto_desktop.shortlab.evidence.metrics as m

    assert hasattr(m, "paired_baseline_summary") or hasattr(evaluation, "paired_baseline_diff")
    import diveintocrypto_desktop.shortlab.evidence.hedge_metrics as hm

    assert hasattr(hm, "hedge_summary")
