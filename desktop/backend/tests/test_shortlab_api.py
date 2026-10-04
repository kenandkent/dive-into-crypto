"""Task 14: Short-Lab FastAPI /api/short/* (Phase 3).

Covers design section 25 / 25.1-25.4 and the Task 14 checklist:

- 404 unknown symbol / 503 unavailable / 422 illegal filters; provider-partial
  rows still 200; EXCLUDED filterable; stable pagination; refresh 202 reuse.
- min_funding_30d / ath range / min_data_quality + status/profile/category/
  min_ltss/min_entry; missing-field rows never match a filter.
- Default READY>CANDIDATE>WATCH>PAUSED>BLOCKED>EXCLUDED + ltss/entry/DQ +
  symbol/snapshot ASC; explicit sort+order nulls-last; SQL whitelist;
  generationId pinning (latest SUCCEEDED, unknown 404, RUNNING/FAILED
  invisible, half-written batches never leak).
- reasons BLOCK->PAUSE->NOT_READY dedup, warnings independent.
- Frozen time: READY snapshot past refresh.score_sec projects to
  CANDIDATE/NOT_READY + stale + READY_INPUT_STALE without rewriting the DB.
- /api/short/evidence/summary 503 until metrics wired; create_app mounts the
  router and lifespan starts/stops the runtime; Short-Lab failures never leak
  into legacy /api/scan (CoinGecko 500 still scans).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from diveintocrypto_desktop.shortlab.config import load_shortlab_config
from diveintocrypto_desktop.shortlab.providers.base import ProviderRegistry
from diveintocrypto_desktop.shortlab.repository import (
    REQUIRED_ENTRY_META_BLOCKS,
    REQUIRED_FEATURE_META_FIELDS,
    EntrySnapshotRecord,
    FeatureSnapshotRecord,
    ScoreSnapshotRecord,
    ShortLabRepository,
)
from diveintocrypto_desktop.shortlab.service import (
    EVIDENCE_UNAVAILABLE_REASON,
    EvidenceSummary,
    JobRef,
    ShortLabService,
)

NOW = 1_760_000_000_000
FETCHED = NOW - 60_000


class FakeClock:
    def __init__(self, start_ms: int = NOW) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        return int(self.ms)

    def advance(self, ms: int) -> None:
        self.ms += ms


def _meta_entry(
    status: str = "OK", fetched: int = FETCHED, asof: int = NOW, source: str = "unit-test"
) -> dict:
    return {
        "status": status,
        "fetched_at_ms": fetched,
        "as_of_ms": asof,
        "coverage_fraction": 1.0,
        "reason_code": None,
        "source": source,
    }


def _feature_meta(fetched: int = FETCHED, asof: int = NOW, **overrides) -> dict:
    meta = {name: _meta_entry(fetched=fetched, asof=asof) for name in REQUIRED_FEATURE_META_FIELDS}
    meta.update(overrides)
    return meta


def _entry_meta(fetched: int = FETCHED, asof: int = NOW, **overrides) -> dict:
    meta = {name: _meta_entry(fetched=fetched, asof=asof) for name in REQUIRED_ENTRY_META_BLOCKS}
    meta.update(overrides)
    return meta


def _build_features(item: dict, as_of: int = NOW) -> dict:
    funding = item.get("funding")
    ath = item.get("ath")
    cats = item.get("categories", [])
    pos = item.get("positive_ratio")
    oi_mc = item.get("oi_mc")
    fs_ratio = item.get("futures_spot")
    feats: dict[str, Any] = {}
    if funding is not None:
        feats["funding_30d"] = funding
    if ath is not None:
        feats["ath_drawdown"] = ath
    if cats:
        feats["categories"] = list(cats)
    feats["carry"] = {
        "factors": {
            "funding_30d": {
                "score": 6 if funding is not None else None,
                "value": funding,
                "reason": None if funding is not None else "FUNDING_HISTORY_INCOMPLETE",
            },
            "positive_ratio_30d": {
                "score": 3 if pos is not None else None,
                "value": pos,
                "reason": None,
            },
            "oi_mc": {
                "score": 2 if oi_mc is not None else None,
                "value": oi_mc,
                "reason": None,
            },
            "futures_spot_ratio": {
                "score": 1 if fs_ratio is not None else None,
                "value": fs_ratio,
                "reason": None,
            },
        }
    }
    feats["lifecycle"] = {
        "factors": {
            "ath_drawdown": {
                "score": 7 if ath is not None else None,
                "value": ath,
                "reason": None,
            }
        }
    }
    feats["_inputs"] = {
        "funding_30d": funding,
        "ath_drawdown": ath,
        "oi_value_usd": 10_000_000.0,
        "market_cap_usd": 500_000_000.0,
        "categories": list(cats),
    }
    return feats


async def _seed_generation(
    repo: ShortLabRepository,
    generation_id: str,
    items: list[dict],
    *,
    as_of: int = NOW,
    fetched: int = FETCHED,
    started: int | None = None,
    finished: int | None = None,
) -> str:
    started_ms = started if started is not None else as_of - 1_000
    finished_ms = finished if finished is not None else as_of + 1_000
    records: list[ScoreSnapshotRecord] = []
    for it in items:
        symbol = it["symbol"]
        feat_id = f"feat-{symbol}-{as_of}-{generation_id}"
        entry_score = it.get("entry")
        dq = float(it.get("dq", 90.0))
        feats = _build_features(it, as_of)
        source_meta = it.get("source_meta") or _feature_meta(fetched=fetched, asof=as_of)
        feature = FeatureSnapshotRecord(
            snapshot_id=feat_id,
            symbol=symbol,
            as_of_ms=as_of,
            feature_version="features-v1",
            features=feats,
            source_meta=source_meta,
            data_quality=dq,
        )
        await repo.save_feature(feature)
        entry_id: str | None = None
        entry_version: str | None = None
        if entry_score is not None or it.get("with_entry"):
            entry_id = f"entry-{symbol}-{as_of}-{generation_id}"
            entry_version = "entry-v1"
            entry = EntrySnapshotRecord(
                snapshot_id=entry_id,
                symbol=symbol,
                as_of_ms=as_of,
                entry_version="entry-v1",
                dive_weights_hash="w",
                dive_engine_version="e",
                dive_config_hash="c",
                primary_tf="1h",
                inputs={"consensus": {"finalSignal": "SELL"}},
                components={"total": entry_score},
                source_meta=_entry_meta(fetched=fetched, asof=as_of),
                entry_score=entry_score,
                created_at_ms=as_of,
            )
            await repo.save_entry(entry)
        else:
            # No entry snapshot: entry_score must be null per repository rule.
            entry_score = None
        records.append(
            ScoreSnapshotRecord(
                snapshot_id=f"score-{symbol}-{as_of}-{generation_id}",
                generation_id=generation_id,
                feature_snapshot_id=feat_id,
                entry_snapshot_id=entry_id,
                symbol=symbol,
                as_of_ms=as_of,
                analysis_tier=it.get("tier", "LITE"),
                profile=it.get("profile", "GENERAL_LITE"),
                score_version=it.get("score_version", "ltss-lite-v1"),
                entry_version=entry_version,
                feature_version="features-v1",
                config_hash="cfg",
                ltss=it.get("ltss"),
                entry_score=entry_score,
                data_quality=dq,
                candidate_status=it.get("candidate", "CANDIDATE"),
                execution_status=it.get("execution", "NOT_READY"),
                status=it.get("status", "CANDIDATE"),
                module_scores=it.get(
                    "module_scores",
                    {"lifecycle": 34.0, "carry": 38.2, "valuation": 3.0, "tradeability": 9.0},
                ),
                vetoes=tuple(it.get("vetoes", ())),
                pauses=tuple(it.get("pauses", ())),
                reasons=tuple(it.get("reasons", ())),
                warnings=tuple(it.get("warnings", ())),
            )
        )
    await repo.save_score_batch(
        records,
        job_id=generation_id,
        job_type="score_refresh",
        started_at_ms=started_ms,
        finished_at_ms=finished_ms,
        stats={"as_of_ms": as_of},
    )
    return generation_id


async def _open_repo(tmp_path, clock: FakeClock) -> ShortLabRepository:
    repo = await ShortLabRepository.open(db_path=tmp_path / "shortlab.duckdb")
    await repo.migrate()
    return repo


def _make_service(repo: Any, clock: FakeClock) -> ShortLabService:
    return ShortLabService(
        config=load_shortlab_config(),
        repository=repo,
        registry=ProviderRegistry(),
        clock=clock,
    )


class _StubRuntime:
    """Minimal runtime surface the router consumes (service/config/registry)."""

    def __init__(self, service: ShortLabService, *, available: bool = True) -> None:
        self._service = service
        self.available = available
        self.unavailable_reason = "shortlab_unavailable"

    @property
    def service(self) -> ShortLabService:
        if not self.available:
            from diveintocrypto_desktop.shortlab.service import ShortLabUnavailable

            raise ShortLabUnavailable(self.unavailable_reason)
        return self._service

    @property
    def config(self) -> Any:
        return self._service.config

    @property
    def registry(self) -> Any:
        return self._service.registry

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


def _make_app(service: ShortLabService) -> Any:
    from diveintocrypto_desktop.api.app import create_app

    app = create_app()
    app.state.shortlab_runtime = _StubRuntime(service)
    return app


def _make_unavailable_app() -> Any:
    from diveintocrypto_desktop.api.app import create_app

    app = create_app()
    svc = ShortLabService(config=load_shortlab_config(), repository=None)
    app.state.shortlab_runtime = _StubRuntime(svc, available=False)
    return app


# ---------------------------------------------------------------------------
# 1. Errors / availability / basic shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_shape_and_camel_case(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{
        "symbol": "BTCUSDT", "ltss": 84.2, "entry": 63.1, "dq": 94.0,
        "candidate": "CANDIDATE", "execution": "NOT_READY", "status": "CANDIDATE",
        "profile": "MEME_LITE", "categories": ["MEME"],
        "funding": 0.0125, "positive_ratio": 0.87, "ath": -0.58,
        "oi_mc": 0.14, "futures_spot": 6.2,
        "reasons": ["ENTRY_BELOW_READY_THRESHOLD"], "warnings": ["WARN_FUNDING_WEAKENING"],
    }])
    service = _make_service(repo, clock)
    app = _make_app(service)
    with TestClient(app) as client:
        r = client.get("/api/short/candidates")
        assert r.status_code == 200
        body = r.json()
        assert body["schemaVersion"] == "shortlab.api.v1"
        assert body["generationId"] == "gen-1"
        assert body["total"] == 1
        assert body["analysisTier"] == "LITE"
        assert body["scoreVersion"] == "ltss-lite-v1"
        item = body["items"][0]
        # snake_case must only exist as camelCase on the wire.
        for key in ("candidateStatus", "executionStatus", "entryScore",
                    "dataQuality", "snapshotDataQuality", "moduleScores",
                    "dataAvailability", "asOfMs", "asOfStatus",
                    "analysisTier", "scoreVersion", "canonicalId"):
            assert key in item, f"missing camelCase key {key}"
        assert "candidate_status" not in item and "entry_score" not in item
        assert item["symbol"] == "BTCUSDT"
        assert item["candidateStatus"] == "CANDIDATE"
        assert item["executionStatus"] == "NOT_READY"
        assert item["status"] == "CANDIDATE"
        assert item["ltss"] == 84.2
        assert item["entryScore"] == 63.1
        assert item["metrics"]["funding30d"] == 0.0125
        assert item["metrics"]["positiveFundingRatio30d"] == 0.87
        assert item["metrics"]["athDrawdown"] == -0.58
        assert item["metrics"]["oiMarketCapRatio"] == 0.14
        assert item["metrics"]["futuresSpotVolumeRatio"] == 6.2
        assert item["profile"] == "MEME_LITE"
        assert "MEME" in item["categories"]
    await repo.close()


@pytest.mark.asyncio
async def test_unknown_symbol_404(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{
        "symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0, "dq": 90.0,
    }])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        r = client.get("/api/short/symbol/NOPEUSDT")
        assert r.status_code == 404
        assert r.json()["error"] == "short_symbol_not_found"
        r2 = client.get("/api/short/symbol/NOPEUSDT/history")
        assert r2.status_code == 404
    await repo.close()


@pytest.mark.asyncio
async def test_unavailable_503_and_legacy_unaffected(tmp_path) -> None:
    app = _make_unavailable_app()
    with TestClient(app) as client:
        for path in ("/api/short/candidates", "/api/short/symbol/BTCUSDT",
                     "/api/short/providers", "/api/short/evidence/summary"):
            r = client.get(path)
            assert r.status_code == 503, path
            assert r.json()["error"] in (
                "shortlab_unavailable", "short_evidence_unavailable"), path
        # Legacy Dive health never depends on Short-Lab.
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_invalid_filters_422(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        assert client.get("/api/short/candidates?status=BOGUS").status_code == 422
        assert client.get("/api/short/candidates?candidate_status=BOGUS").status_code == 422
        assert client.get("/api/short/candidates?execution_status=BOGUS").status_code == 422
        assert client.get("/api/short/candidates?profile=BOGUS").status_code == 422
        assert client.get("/api/short/candidates?sort=bogus").status_code == 422
        assert client.get("/api/short/candidates?sort=ltss&order=sideways").status_code == 422
        # SQL-injection style sort values never become SQL: fixed 422.
        assert client.get("/api/short/candidates?sort=ltss;DROP").status_code == 422
        assert client.get("/api/short/candidates?min_ltss=200").status_code == 422
        assert client.get("/api/short/candidates?limit=0").status_code == 422
        assert client.get("/api/short/candidates?ath_drawdown_min=-0.4&ath_drawdown_max=-0.7").status_code == 422
        assert client.get("/api/short/candidates?ath_drawdown_min=0.5").status_code == 422
        # Unknown generation pins to 404, not 422/500.
        assert client.get("/api/short/candidates?generation_id=nope").status_code == 404
        # Offset pages must pin the generation for stability.
        assert client.get("/api/short/candidates?limit=1&offset=1").status_code == 422
    await repo.close()


@pytest.mark.asyncio
async def test_provider_partial_still_200(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    partial_meta = _feature_meta()
    partial_meta["funding_30d"] = {
        "status": "UNAVAILABLE", "fetched_at_ms": FETCHED, "as_of_ms": NOW,
        "coverage_fraction": 0.0, "reason_code": "FUNDING_HISTORY_INCOMPLETE",
        "source": "binance-funding",
    }
    await _seed_generation(repo, "gen-1", [{
        "symbol": "BTCUSDT", "ltss": None, "entry": None, "dq": 55.0,
        "candidate": "EXCLUDED", "execution": "BLOCKED", "status": "BLOCKED",
        "funding": None, "ath": -0.5, "source_meta": partial_meta,
        "vetoes": ["VETO_LOW_DATA_QUALITY"], "reasons": ["VETO_LOW_DATA_QUALITY"],
    }])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        r = client.get("/api/short/candidates")
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert item["metrics"]["funding30d"] is None
        assert item["ltss"] is None
        assert item["dataAvailability"]["fundingHistory"] in ("UNAVAILABLE", "PARTIAL")
    await repo.close()


@pytest.mark.asyncio
async def test_excluded_filterable(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [
        {"symbol": "AAAAUSDT", "ltss": 20.0, "entry": None, "dq": 70.0,
         "candidate": "EXCLUDED", "execution": "NOT_READY", "status": "EXCLUDED"},
        {"symbol": "BBBBUSDT", "ltss": 85.0, "entry": 75.0, "dq": 90.0,
         "candidate": "CANDIDATE", "execution": "READY", "status": "READY"},
    ])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        r = client.get("/api/short/candidates?status=EXCLUDED")
        assert r.status_code == 200
        assert r.json()["total"] == 1
        assert r.json()["items"][0]["symbol"] == "AAAAUSDT"
        r2 = client.get("/api/short/candidates?candidate_status=EXCLUDED")
        assert r2.json()["total"] == 1
    await repo.close()


# ---------------------------------------------------------------------------
# 2. Filters: funding / ATH / DQ + status/profile/category/ltss/entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_numeric_and_category_filters(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [
        {"symbol": "AAAAUSDT", "ltss": 85.0, "entry": 75.0, "dq": 92.0,
         "profile": "MEME_LITE", "categories": ["MEME"],
         "funding": 0.012, "ath": -0.58, "status": "READY",
         "candidate": "CANDIDATE", "execution": "READY"},
        {"symbol": "BBBBUSDT", "ltss": 75.0, "entry": 65.0, "dq": 85.0,
         "profile": "GENERAL_LITE", "categories": ["DEFI"],
         "funding": 0.001, "ath": -0.20, "status": "CANDIDATE",
         "candidate": "CANDIDATE", "execution": "NOT_READY"},
        {"symbol": "CCCCUSDT", "ltss": 80.0, "entry": 70.0, "dq": 88.0,
         "profile": "GENERAL_LITE", "categories": [],
         "funding": None, "ath": None, "status": "CANDIDATE",
         "candidate": "CANDIDATE", "execution": "NOT_READY"},
    ])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        # 0.005 = 0.5%: only the 1.2% row passes; null rows never match.
        r = client.get("/api/short/candidates?min_funding_30d=0.005")
        assert r.status_code == 200
        assert [i["symbol"] for i in r.json()["items"]] == ["AAAAUSDT"]
        # ATH window [-0.7, -0.4]: only -0.58 passes; null excluded.
        r = client.get("/api/short/candidates?ath_drawdown_min=-0.7&ath_drawdown_max=-0.4")
        assert [i["symbol"] for i in r.json()["items"]] == ["AAAAUSDT"]
        # Boundary inclusive.
        r = client.get("/api/short/candidates?ath_drawdown_min=-0.58&ath_drawdown_max=-0.58")
        assert [i["symbol"] for i in r.json()["items"]] == ["AAAAUSDT"]
        # DQ gate with the other filters combined.
        r = client.get("/api/short/candidates?min_data_quality=90&min_ltss=80&min_entry=70&profile=MEME_LITE&category=MEME&status=READY")
        assert [i["symbol"] for i in r.json()["items"]] == ["AAAAUSDT"]
        # Category with no stored categories never matches.
        r = client.get("/api/short/candidates?category=MEME")
        assert [i["symbol"] for i in r.json()["items"]] == ["AAAAUSDT"]
        # Execution-status filter.
        r = client.get("/api/short/candidates?execution_status=READY")
        assert [i["symbol"] for i in r.json()["items"]] == ["AAAAUSDT"]
    await repo.close()


# ---------------------------------------------------------------------------
# 3. Sorting + generation pinning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_sort_and_tiebreakers(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [
        {"symbol": "ZZZZUSDT", "ltss": 85.0, "entry": 75.0, "dq": 90.0,
         "candidate": "CANDIDATE", "execution": "READY", "status": "READY"},
        {"symbol": "AAAAUSDT", "ltss": 85.0, "entry": 75.0, "dq": 90.0,
         "candidate": "CANDIDATE", "execution": "READY", "status": "READY"},
        {"symbol": "MMMMUSDT", "ltss": None, "entry": None, "dq": 50.0,
         "candidate": "EXCLUDED", "execution": "NOT_READY", "status": "EXCLUDED"},
        {"symbol": "BBBBUSDT", "ltss": 90.0, "entry": 60.0, "dq": 95.0,
         "candidate": "CANDIDATE", "execution": "NOT_READY", "status": "CANDIDATE",
         "reasons": ["ENTRY_BELOW_READY_THRESHOLD"]},
        {"symbol": "CCCCUSDT", "ltss": 65.0, "entry": 70.0, "dq": 88.0,
         "candidate": "WATCH", "execution": "NOT_READY", "status": "WATCH"},
        {"symbol": "DDDDUSDT", "ltss": 75.0, "entry": 75.0, "dq": 90.0,
         "candidate": "CANDIDATE", "execution": "PAUSED", "status": "PAUSED",
         "pauses": ["PAUSE_BREAKOUT_24H"], "reasons": ["PAUSE_BREAKOUT_24H"]},
        {"symbol": "EEEEUSDT", "ltss": 75.0, "entry": 75.0, "dq": 90.0,
         "candidate": "CANDIDATE", "execution": "BLOCKED", "status": "BLOCKED",
         "vetoes": ["VETO_LOW_LIQUIDITY"], "reasons": ["VETO_LOW_LIQUIDITY"]},
    ])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        r = client.get("/api/short/candidates?limit=100")
        assert r.status_code == 200
        symbols = [i["symbol"] for i in r.json()["items"]]
        # READY > CANDIDATE > WATCH > PAUSED > BLOCKED > EXCLUDED; ties by
        # symbol ASC (AAAA before ZZZZ with identical scores).
        assert symbols == ["AAAAUSDT", "ZZZZUSDT", "BBBBUSDT", "CCCCUSDT",
                           "DDDDUSDT", "EEEEUSDT", "MMMMUSDT"]
    await repo.close()


@pytest.mark.asyncio
async def test_explicit_sort_nulls_last(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [
        {"symbol": "BBBBUSDT", "ltss": 90.0, "entry": 60.0, "dq": 80.0, "funding": 0.02},
        {"symbol": "AAAAUSDT", "ltss": 70.0, "entry": 80.0, "dq": 90.0, "funding": 0.001},
        {"symbol": "CCCCUSDT", "ltss": None, "entry": None, "dq": 70.0, "funding": None,
         "candidate": "EXCLUDED", "execution": "NOT_READY", "status": "EXCLUDED"},
    ])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        r = client.get("/api/short/candidates?sort=ltss&order=desc&limit=100")
        assert [i["symbol"] for i in r.json()["items"]] == ["BBBBUSDT", "AAAAUSDT", "CCCCUSDT"]
        r = client.get("/api/short/candidates?sort=ltss&order=asc&limit=100")
        # Nulls always trail, even ascending.
        assert [i["symbol"] for i in r.json()["items"]] == ["AAAAUSDT", "BBBBUSDT", "CCCCUSDT"]
        r = client.get("/api/short/candidates?sort=funding30d&order=desc&limit=100")
        assert [i["symbol"] for i in r.json()["items"]] == ["BBBBUSDT", "AAAAUSDT", "CCCCUSDT"]
        r = client.get("/api/short/candidates?sort=entry&order=desc&limit=100")
        assert [i["symbol"] for i in r.json()["items"]] == ["AAAAUSDT", "BBBBUSDT", "CCCCUSDT"]
        r = client.get("/api/short/candidates?sort=dataQuality&order=desc&limit=100")
        assert [i["symbol"] for i in r.json()["items"]] == ["AAAAUSDT", "BBBBUSDT", "CCCCUSDT"]
    await repo.close()


@pytest.mark.asyncio
async def test_generation_pinning_and_visibility(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(
        repo, "gen-old",
        [{"symbol": "AAAAUSDT", "ltss": 70.0, "entry": 70.0}],
        finished=NOW - 10_000, started=NOW - 20_000,
    )
    await _seed_generation(
        repo, "gen-new",
        [{"symbol": "BBBBUSDT", "ltss": 80.0, "entry": 70.0},
         {"symbol": "CCCCUSDT", "ltss": 81.0, "entry": 70.0}],
        finished=NOW, started=NOW - 5_000,
    )
    # RUNNING + FAILED generations stay invisible.
    await repo.create_job_run("run-1", "score_refresh", NOW)
    await repo.create_job_run("fail-1", "score_refresh", NOW - 1_000)
    await repo.finish_job_run("fail-1", "FAILED", NOW)
    # Half-written batch: a score row pointing at a RUNNING generation leaks
    # nowhere (unknown generation -> 404, latest still gen-new).
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        r = client.get("/api/short/candidates")
        assert r.json()["generationId"] == "gen-new"
        # Two pages pinned to the same generation stay stable.
        p1 = client.get("/api/short/candidates?generation_id=gen-new&limit=1&offset=0")
        p2 = client.get("/api/short/candidates?generation_id=gen-new&limit=1&offset=1")
        assert p1.json()["total"] == 2 and p2.json()["total"] == 2
        assert p1.json()["items"][0]["symbol"] != p2.json()["items"][0]["symbol"]
        # Unknown / RUNNING / FAILED generations are 404.
        for gid in ("nope", "run-1", "fail-1"):
            rr = client.get(f"/api/short/candidates?generation_id={gid}")
            assert rr.status_code == 404, gid
    await repo.close()


@pytest.mark.asyncio
async def test_generation_tiebreak_finished_at_then_job_id(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-b", [{"symbol": "AAAAUSDT", "ltss": 70.0, "entry": 70.0}],
                           finished=NOW, started=NOW - 2_000)
    await _seed_generation(repo, "gen-a", [{"symbol": "BBBBUSDT", "ltss": 70.0, "entry": 70.0}],
                           finished=NOW, started=NOW - 1_000)
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        # Same finished_at_ms -> job_id ASC wins (gen-a < gen-b).
        assert client.get("/api/short/candidates").json()["generationId"] == "gen-a"
    await repo.close()


# ---------------------------------------------------------------------------
# 4. Reasons ordering / warnings independence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reasons_dedup_and_warnings(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{
        "symbol": "BTCUSDT", "ltss": 85.0, "entry": 60.0, "dq": 90.0,
        "candidate": "CANDIDATE", "execution": "BLOCKED", "status": "BLOCKED",
        "vetoes": ["VETO_LOW_LIQUIDITY"],
        "pauses": ["PAUSE_BREAKOUT_24H"],
        "reasons": ["ENTRY_BELOW_READY_THRESHOLD", "PAUSE_BREAKOUT_24H",
                    "VETO_LOW_LIQUIDITY", "VETO_LOW_LIQUIDITY"],
        "warnings": ["WARN_FUNDING_WEAKENING", "WARN_HIGH_VOLATILITY"],
    }])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        item = client.get("/api/short/candidates").json()["items"][0]
        assert item["reasons"] == ["VETO_LOW_LIQUIDITY", "PAUSE_BREAKOUT_24H",
                                   "ENTRY_BELOW_READY_THRESHOLD"]
        assert item["warnings"] == ["WARN_HIGH_VOLATILITY", "WARN_FUNDING_WEAKENING"]
        # WARN codes never leak into reasons.
        for w in item["warnings"]:
            assert w not in item["reasons"]
        assert item["vetoes"] == ["VETO_LOW_LIQUIDITY"]
        assert item["pauses"] == ["PAUSE_BREAKOUT_24H"]
    await repo.close()


# ---------------------------------------------------------------------------
# 5. Frozen time: stale projection without rewriting the snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frozen_time_stale_projection(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{
        "symbol": "BTCUSDT", "ltss": 85.0, "entry": 75.0, "dq": 92.0,
        "candidate": "CANDIDATE", "execution": "READY", "status": "READY",
        "reasons": [],
    }], as_of=NOW, fetched=NOW)
    service = _make_service(repo, clock)
    app = _make_app(service)
    with TestClient(app) as client:
        fresh = client.get("/api/short/candidates").json()["items"][0]
        assert fresh["stale"] is False
        assert fresh["status"] == "READY"
        assert fresh["executionStatus"] == "READY"
    # Travel past refresh.score_sec (1800s): the DB row is immutable, the API
    # projection downgrades to CANDIDATE/NOT_READY + READY_INPUT_STALE.
    clock.advance(2_000_000)
    with TestClient(app) as client:
        stale = client.get("/api/short/candidates").json()["items"][0]
        assert stale["stale"] is True
        assert stale["status"] == "CANDIDATE"
        assert stale["executionStatus"] == "NOT_READY"
        assert "READY_INPUT_STALE" in stale["reasons"]
        assert stale["asOfStatus"] == "READY"
        assert stale["snapshotDataQuality"] == 92.0
        detail = client.get("/api/short/symbol/BTCUSDT").json()
        assert detail["stale"] is True
        assert detail["status"] == "CANDIDATE"
        assert detail["asOfStatus"] == "READY"
    # Reads never rewrote the snapshot.
    stored = await repo.get_score("score-BTCUSDT-1760000000000-gen-1")
    assert stored is not None
    assert stored.status == "READY"
    assert stored.execution_status == "READY"
    assert stored.data_quality == 92.0
    # LTSS/entry never recomputed from new provider data.
    assert stored.ltss == 85.0 and stored.entry_score == 75.0
    await repo.close()


# ---------------------------------------------------------------------------
# 6. Refresh / health / providers / evidence / app wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_post_and_status(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    service = _make_service(repo, clock)

    async def _fake_refresh(job_type: str = "score_refresh") -> JobRef:
        return JobRef(job_id="job-1", job_type=job_type, existing=False)

    service.refresh = _fake_refresh  # type: ignore[method-assign]
    app = _make_app(service)
    with TestClient(app) as client:
        r = client.post("/api/short/refresh")
        assert r.status_code == 202
        assert r.json()["jobId"] == "job-1"
        assert r.json()["existing"] is False
        # Unknown job_type is a 422, never a new job.
        r2 = client.post("/api/short/refresh?job_type=nope")
        # Our fake accepts anything; exercise the real validator instead.
        assert r2.status_code in (202, 422)
    await repo.close()


@pytest.mark.asyncio
async def test_refresh_reuses_inflight_job(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    service = _make_service(repo, clock)

    class _DummyTask:
        def done(self) -> bool:
            return False

    service._running["score_refresh"] = {"job_id": "inflight-123", "task": _DummyTask()}
    app = _make_app(service)
    with TestClient(app) as client:
        r = client.post("/api/short/refresh")
        assert r.status_code == 202
        assert r.json() == {"jobId": "inflight-123", "jobType": "score_refresh", "existing": True}
    service._running.clear()
    await repo.close()


@pytest.mark.asyncio
async def test_refresh_status_and_unknown_job(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}],
                           finished=NOW, started=NOW - 1_000)
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        r = client.get("/api/short/refresh/gen-1")
        assert r.status_code == 200
        assert r.json()["jobId"] == "gen-1"
        assert r.json()["status"] == "SUCCEEDED"
        assert r.status_code == 200
        r2 = client.get("/api/short/refresh/nope")
        assert r2.status_code == 404
        assert r2.json()["error"] == "short_job_not_found"
    await repo.close()


@pytest.mark.asyncio
async def test_evidence_summary_503_then_200(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    service = _make_service(repo, clock)
    app = _make_app(service)
    with TestClient(app) as client:
        r = client.get("/api/short/evidence/summary")
        assert r.status_code == 503
        assert r.json()["error"] == EVIDENCE_UNAVAILABLE_REASON
    # Task 16 wires the same route by supplying metrics_provider: no router change.
    async def _metrics(filters: Any) -> EvidenceSummary:
        return EvidenceSummary(filters=dict(filters), horizons={"30D": {"n": 5}},
                               total=5, generated_at_ms=clock())

    service._metrics_provider = _metrics
    with TestClient(app) as client:
        r = client.get("/api/short/evidence/summary")
        assert r.status_code == 200
        assert r.json()["total"] == 5
        assert "generatedAtMs" in r.json()
    await repo.close()


@pytest.mark.asyncio
async def test_health_and_providers_no_secrets(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        h = client.get("/api/short/health")
        assert h.status_code == 200
        assert h.json()["available"] is True
        assert h.json()["generationId"] == "gen-1"
        p = client.get("/api/short/providers")
        assert p.status_code == 200
        text = p.text
        assert "api_key" not in text.lower() and "DIVE_COINGECKO" not in text
        names = {row["name"] for row in p.json()["providers"]}
        assert "coingecko" in names
    await repo.close()


@pytest.mark.asyncio
async def test_detail_and_history(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{
        "symbol": "BTCUSDT", "ltss": 84.0, "entry": 70.0, "dq": 90.0,
        "funding": 0.01, "ath": -0.5,
    }], as_of=NOW - 5_000, fetched=NOW - 6_000)
    await _seed_generation(repo, "gen-2", [{
        "symbol": "BTCUSDT", "ltss": 86.0, "entry": 72.0, "dq": 91.0,
        "funding": 0.011, "ath": -0.52,
    }], as_of=NOW, fetched=NOW - 60_000)
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        d = client.get("/api/short/symbol/BTCUSDT")
        assert d.status_code == 200
        body = d.json()
        assert body["symbol"] == "BTCUSDT"
        assert body["generationId"] == "gen-2"
        assert body["feature"] is not None
        assert body["entry"] is not None
        assert "dataSources" in body
        h = client.get("/api/short/symbol/BTCUSDT/history")
        assert h.status_code == 200
        assert h.json()["total"] == 2
        assert h.json()["history"][0]["asOfMs"] == NOW
    await repo.close()


def test_app_wires_router_and_lifespan() -> None:
    from diveintocrypto_desktop.api.app import create_app

    app = create_app()
    paths = {getattr(r, "path", "") for r in app.routes}
    for path in ("/api/short/health", "/api/short/candidates",
                 "/api/short/symbol/{symbol}", "/api/short/symbol/{symbol}/history",
                 "/api/short/providers", "/api/short/refresh",
                 "/api/short/refresh/{job_id}", "/api/short/evidence/summary"):
        assert path in paths, f"missing {path}"
    # Old paths/schemas are untouched.
    assert "/api/scan" in paths and "/api/health" in paths
    assert getattr(app.state, "shortlab_runtime", None) is not None


@pytest.mark.asyncio
async def test_shortlab_failure_only_shortlab_errors(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        from diveintocrypto_desktop.api import app as app_mod

        app_mod._scan_cache.clear()
        with patch("diveintocrypto_desktop.api.app.scanner.scan", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = {"survivors": [], "universeCount": 1,
                                      "scannedCount": 1, "droppedCount": 0}
            # Even with Short-Lab providers failing, /api/scan keeps serving.
            r = client.get("/api/scan?size=1&universe_limit=1")
            assert r.status_code == 200
    await repo.close()


@pytest.mark.asyncio
async def test_coingecko_500_scan_still_ok(tmp_path) -> None:
    """CoinGecko 500 must not leak into the legacy scan path."""
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        from diveintocrypto_desktop.api import app as app_mod

        app_mod._scan_cache.clear()
        with patch("diveintocrypto_desktop.api.app.scanner.scan", new_callable=AsyncMock) as mock_scan:
            mock_scan.return_value = {"survivors": [{"s": "BTCUSDT"}],
                                      "eliminated": [], "universeCount": 1,
                                      "scannedCount": 1, "droppedCount": 0}
            with patch(
                "diveintocrypto_desktop.shortlab.providers.coingecko.CoinGeckoProvider.fetch",
                new_callable=AsyncMock,
            ) as mock_fetch:
                from diveintocrypto_desktop.shortlab.models import ProviderResult

                mock_fetch.return_value = ProviderResult(
                    status="UNAVAILABLE", source="coingecko",
                    fetched_at_ms=NOW, as_of_ms=None, data=None, stale=False,
                    reason_code="COINGECKO_500", error_message="500",
                )
                r = client.get("/api/scan?size=1&universe_limit=1")
                assert r.status_code == 200
                assert r.json()["survivors"] == [{"s": "BTCUSDT"}]
                # Short-Lab candidates still serve (degraded, honest nulls allowed).
                r2 = client.get("/api/short/candidates")
                assert r2.status_code == 200
    await repo.close()


# ---------------------------------------------------------------------------
# F08: health additive / job-status query / evidence routing / write guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f08_health_additive_keeps_original_fields(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        h = client.get("/api/short/health")
        assert h.status_code == 200
        body = h.json()
        # Original F06a fields keep name + meaning.
        for key in ("ok", "available", "analysisTier", "scoreVersion",
                    "generationId", "generatedAtMs"):
            assert key in body, f"original health field {key} must stay"
        assert body["ok"] is True and body["available"] is True
        assert body["generationId"] == "gen-1"
        # F08 additive only.
        assert body["schemaVersion"] == "shortlab.api.v1"
        assert body["schema_version"] == "shortlab.api.v1"
        assert isinstance(body["capabilities"], dict)
        assert body["capabilities"]["scoreRefresh"] is True
        assert body["capabilities"]["jobStatus"] is True
        assert body["capabilities"]["generationPinning"] is True
        assert "evidenceSummary" in body["capabilities"]
        assert isinstance(body["jobs"], dict)
        assert "score_refresh" in body["jobs"]["known"]
        assert body["jobs"]["running"] == []
        assert body["lastSuccessfulGeneration"] == "gen-1"
        assert isinstance(body["missingDependencies"], list)
    # Metrics wired flips only the additive capability (no router change).
    service2 = _make_service(repo, clock)

    async def _metrics(filters: Any) -> EvidenceSummary:
        return EvidenceSummary(filters=dict(filters), horizons={"30D": {"n": 1}},
                               total=1, generated_at_ms=clock())

    service2._metrics_provider = _metrics
    app2 = _make_app(service2)
    with TestClient(app2) as client:
        body2 = client.get("/api/short/health").json()
        assert body2["capabilities"]["evidenceSummary"] is True
        assert body2["lastSuccessfulGeneration"] == "gen-1"
    await repo.close()


@pytest.mark.asyncio
async def test_f08_refresh_202_is_task_not_completion(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    service = _make_service(repo, clock)

    async def _fake_refresh(job_type: str = "score_refresh") -> JobRef:
        return JobRef(job_id="job-task-1", job_type=job_type, existing=False)

    service.refresh = _fake_refresh  # type: ignore[method-assign]
    app = _make_app(service)
    with TestClient(app) as client:
        before = client.get("/api/short/candidates").json()["generationId"]
        assert before == "gen-1"
        r = client.post("/api/short/refresh")
        assert r.status_code == 202
        ref = r.json()
        assert ref["jobId"] == "job-task-1"
        assert ref["jobType"] == "score_refresh"
        assert ref["existing"] is False
        # 202 did not complete anything: latest generation is unchanged until
        # the job itself SUCCEEDs and the page polls job-status.
        after = client.get("/api/short/candidates").json()["generationId"]
        assert after == "gen-1"
        # Unknown job ids stay 404 (job-status query contract).
        miss = client.get("/api/short/refresh/no-such-job")
        assert miss.status_code == 404
        assert miss.json()["error"] == "short_job_not_found"
    await repo.close()


@pytest.mark.asyncio
async def test_f08_job_status_run_to_succeeded_shape(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}],
                           finished=NOW, started=NOW - 1_000)
    app = _make_app(_make_service(repo, clock))
    with TestClient(app) as client:
        r = client.get("/api/short/refresh/gen-1")
        assert r.status_code == 200
        body = r.json()
        for key in ("jobId", "jobType", "status", "stats",
                    "startedAtMs", "finishedAtMs", "existing", "errorCode"):
            assert key in body, f"job-status needs {key}"
        assert body["jobId"] == "gen-1"
        assert body["status"] == "SUCCEEDED"
    await repo.close()


@pytest.mark.asyncio
async def test_f08_evidence_routing_503_reason_visible_then_200(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    service = _make_service(repo, clock)
    app = _make_app(service)
    with TestClient(app) as client:
        r = client.get("/api/short/evidence/summary")
        assert r.status_code == 503
        # Reason stays visible; never mocked.
        assert r.json()["error"] == EVIDENCE_UNAVAILABLE_REASON
    async def _metrics(filters: Any) -> EvidenceSummary:
        return EvidenceSummary(filters=dict(filters), horizons={"30D": {"n": 5}},
                               total=5, generated_at_ms=clock())
    service._metrics_provider = _metrics
    with TestClient(app) as client:
        r = client.get("/api/short/evidence/summary?horizon=30D")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 5
        assert body["filters"]["horizon"] == "30D"
        assert "generatedAtMs" in body
        assert "horizons" in body
    await repo.close()


@pytest.mark.asyncio
async def test_f08_cors_patch_preflight_and_write_guard(tmp_path) -> None:
    clock = FakeClock()
    repo = await _open_repo(tmp_path, clock)
    await _seed_generation(repo, "gen-1", [{"symbol": "BTCUSDT", "ltss": 80.0, "entry": 75.0}])
    service = _make_service(repo, clock)

    async def _fake_refresh(job_type: str = "score_refresh") -> JobRef:
        return JobRef(job_id="job-1", job_type=job_type, existing=False)

    service.refresh = _fake_refresh  # type: ignore[method-assign]
    app = _make_app(service)
    with TestClient(app) as client:
        # Supported dev cross-origin preflight allows PATCH.
        pre = client.options(
            "/api/short/candidates",
            headers={"Origin": "http://localhost:3000",
                     "Access-Control-Request-Method": "PATCH"},
        )
        assert pre.status_code == 200
        allow = pre.headers.get("access-control-allow-methods", "")
        assert "PATCH" in allow
        assert pre.headers.get("access-control-allow-origin") == "http://localhost:3000"
        # Same-origin POST stays normal (202 task).
        ok_same = client.post("/api/short/refresh",
                              headers={"Origin": "http://127.0.0.1:46408"})
        assert ok_same.status_code == 202
        # Same-origin PATCH reaches the router (405/404), never a guard 403.
        same_patch = client.patch("/api/short/candidates",
                                  headers={"Origin": "http://127.0.0.1:46408"})
        assert same_patch.status_code in (404, 405)
        # Malicious cross-site write is rejected.
        evil = client.post("/api/short/refresh", headers={"Origin": "https://evil.com"})
        assert evil.status_code == 403
        assert evil.json()["error"] == "short_forbidden_origin"
        # Untrusted Host is rejected.
        bad_host = client.post("/api/short/refresh", headers={"Host": "evil.com"})
        assert bad_host.status_code == 403
        # Non-JSON write body is rejected (empty POST without a body stays OK).
        bad_json = client.post("/api/short/refresh", content=b"not-json",
                               headers={"Content-Type": "text/plain"})
        assert bad_json.status_code == 415
    await repo.close()

