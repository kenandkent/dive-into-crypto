"""Task 3: funding history pagination + coverage (Short-Lab Phase 1).

Covers ``ShortLab_Implementation_Plan_CN.md`` Task 3 and
``ShortLab_Detailed_Design_CN.md`` section 10.1:

- ``funding_history_range()`` pages ``/fapi/v1/fundingRate`` with
  ``startTime/endTime/limit``, resumes a full page from
  ``last fundingTime + 1``, dedupes repeated times, stops on empty pages,
  and goes through the shared ``data/http.py`` retry path (429 honors
  ``Retry-After``).
- ``funding_coverage()`` implements the 10.1 completeness rule
  (edges <= 24h, adjacent gaps <= 24h, >= 3 events) and the incomplete-window
  ``coverage_fraction`` formula including head/tail deductions.
- ``premium_index()`` carries an additive ``time_ms`` from the response
  ``time`` field (``None`` when absent; never the local clock).
- ``funding_hist()`` keeps its legacy shape for existing callers.

All network access is faked; no live requests.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import pytest

from diveintocrypto_desktop.data import funding
from diveintocrypto_desktop.data import http as http_mod

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
GAP_MS = 24 * HOUR_MS  # 10.1 gap tolerance

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "shortlab"


def _row(t_ms: int, rate: float = 0.0001, mark: float = 27000.0) -> dict:
    return {
        "symbol": "BTCUSDT",
        "fundingTime": t_ms,
        "fundingRate": f"{rate:.8f}",
        "markPrice": f"{mark:.1f}",
    }


class _PagedGetJson:
    """Emulates Binance server-side startTime/endTime/limit filtering."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = sorted(rows, key=lambda r: r["fundingTime"])
        self.calls: list[dict] = []

    async def __call__(self, url: str, params: dict | None = None) -> list[dict]:
        params = dict(params or {})
        self.calls.append({"url": url, "params": params})
        out = [
            r
            for r in self._rows
            if (params.get("startTime") is None or r["fundingTime"] >= params["startTime"])
            and (params.get("endTime") is None or r["fundingTime"] <= params["endTime"])
        ]
        return out[: params.get("limit", 1000)]


class _FakeResponse:
    def __init__(self, status: int, payload, retry_after: str | None = None) -> None:
        self.status = status
        self._payload = payload
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}

    def raise_for_status(self) -> None:
        if self.status >= 400:
            import aiohttp

            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status, message="err"
            )

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        resp = self.responses.pop(0)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return None

        return _Ctx()


# ── pagination ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_range_sends_start_end_limit():
    t0 = 1_700_000_000_000
    fake = _PagedGetJson([_row(t0), _row(t0 + 8 * HOUR_MS)])
    with patch.object(funding, "get_json", fake):
        out = await funding.funding_history_range("BTCUSDT", t0, t0 + 8 * HOUR_MS)
    assert fake.calls[0]["url"].endswith("/fundingRate")
    assert "premiumIndex" not in fake.calls[0]["url"]  # settled history only, never predicted
    assert fake.calls[0]["params"] == {
        "symbol": "BTCUSDT",
        "startTime": t0,
        "endTime": t0 + 8 * HOUR_MS,
        "limit": 1000,
    }
    assert [e["t"] for e in out] == [t0, t0 + 8 * HOUR_MS]
    assert out[0] == {"t": t0, "funding_rate": 0.0001, "mark_price": 27000.0}


@pytest.mark.asyncio
async def test_history_range_uses_task0_fixture_shape():
    rows = json.loads((FIXTURES / "funding_page.json").read_text())
    fake = _PagedGetJson(rows)
    with patch.object(funding, "get_json", fake):
        out = await funding.funding_history_range(
            "BTCUSDT", rows[0]["fundingTime"], rows[-1]["fundingTime"]
        )
    assert [e["t"] for e in out] == sorted(r["fundingTime"] for r in rows)
    assert out[0] == {
        "t": 1_699_999_999_000,
        "funding_rate": 0.0001,
        "mark_price": 27000.0,
    }


@pytest.mark.asyncio
async def test_history_range_full_page_resumes_from_last_plus_one():
    t0 = 1_700_000_000_000
    rows = [_row(t0 + k * HOUR_MS, rate=0.0001 + k * 1e-6) for k in range(5)]
    fake = _PagedGetJson(rows)
    with patch.object(funding, "get_json", fake):
        out = await funding.funding_history_range("BTCUSDT", t0, t0 + 4 * HOUR_MS, limit=2)
    # 5 rows at limit=2 -> pages [0,1] [2,3] [4]; each full page resumes at last+1.
    assert len(fake.calls) == 3
    assert fake.calls[1]["params"]["startTime"] == rows[1]["fundingTime"] + 1
    assert fake.calls[2]["params"]["startTime"] == rows[3]["fundingTime"] + 1
    assert [e["t"] for e in out] == [r["fundingTime"] for r in rows]


@pytest.mark.asyncio
async def test_history_range_dedupes_repeated_times():
    t0 = 1_700_000_000_000
    r0, r1, r2 = _row(t0), _row(t0 + HOUR_MS), _row(t0 + 2 * HOUR_MS)
    pages = [[r0, r1], [r1, r2], []]  # server re-includes the boundary row, then empty

    async def overlapping(url, params=None):
        return pages.pop(0)

    with patch.object(funding, "get_json", overlapping):
        out = await funding.funding_history_range("BTCUSDT", t0, t0 + 2 * HOUR_MS, limit=2)
    assert [e["t"] for e in out] == [t0, t0 + HOUR_MS, t0 + 2 * HOUR_MS]


@pytest.mark.asyncio
async def test_history_range_empty_page_ends():
    fake = _PagedGetJson([])
    with patch.object(funding, "get_json", fake):
        out = await funding.funding_history_range("BTCUSDT", 1, 2)
    assert out == []
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_history_range_429_honors_retry_after_through_shared_http():
    t0 = 1_700_000_000_000
    rows = [_row(t0), _row(t0 + 8 * HOUR_MS)]
    session = _FakeSession(
        [_FakeResponse(429, None, retry_after="2"), _FakeResponse(200, rows)]
    )
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with (
        patch.object(http_mod, "get_session", return_value=session),
        patch.object(http_mod.asyncio, "sleep", fake_sleep),
    ):
        out = await funding.funding_history_range("BTCUSDT", t0, t0 + 8 * HOUR_MS)
    assert sleeps == [2.0]
    assert [e["t"] for e in out] == [t0, t0 + 8 * HOUR_MS]


@pytest.mark.asyncio
async def test_history_range_never_touches_predicted_premium_index():
    t0 = 1_700_000_000_000
    fake = _PagedGetJson([])
    with patch.object(funding, "get_json", fake):
        assert await funding.funding_history_range("BTCUSDT", t0, t0 + HOUR_MS) == []
    assert fake.calls, "must issue at least the first page request"
    assert all("premiumIndex" not in c["url"] for c in fake.calls)


# ── legacy behaviour ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funding_hist_legacy_shape_preserved():
    rows = json.loads((FIXTURES / "funding_page.json").read_text())
    seen: list[dict] = []

    async def fake(url, params=None):
        seen.append({"url": url, "params": dict(params or {})})
        return rows

    with patch.object(funding, "get_json", fake):
        out = await funding.funding_hist("BTCUSDT", limit=48)
    assert out == [
        {"t": 1_699_999_999_000, "funding_rate": 0.0001},
        {"t": 1_700_000_000_000, "funding_rate": -0.00005},
    ]
    assert seen[0]["params"] == {"symbol": "BTCUSDT", "limit": 48}


@pytest.mark.asyncio
async def test_premium_index_additive_time_ms():
    payload = {
        "markPrice": "27000.0",
        "indexPrice": "26990.0",
        "lastFundingRate": "0.0001",
        "nextFundingTime": 1_700_002_800_000,
        "time": 1_699_999_999_000,
    }
    with patch.object(funding, "get_json", return_value=payload):
        out = await funding.premium_index("BTCUSDT")
    assert out["mark_price"] == 27000.0
    assert out["index_price"] == 26990.0
    assert out["last_funding_rate"] == 0.0001
    assert out["next_funding_time"] == 1_700_002_800_000
    assert out["time_ms"] == 1_699_999_999_000  # exchange timestamp, not local clock


@pytest.mark.asyncio
async def test_premium_index_time_ms_null_when_absent():
    payload = {
        "markPrice": "27000.0",
        "indexPrice": "26990.0",
        "lastFundingRate": "0.0001",
        "nextFundingTime": 0,
    }
    with patch.object(funding, "get_json", return_value=payload):
        out = await funding.premium_index("BTCUSDT")
    assert out["time_ms"] is None


# ── budgets ────────────────────────────────────────────────────────────────

def test_shortlab_funding_budget_constants():
    assert funding.SHORTLAB_FUNDING_BUDGET_PER_5MIN == 80
    assert funding.SHORTLAB_FUNDING_BATCH_SYMBOLS == 80


# ── coverage (design 10.1) ─────────────────────────────────────────────────

def _events(times: list[int], rate: float = 0.0001) -> list[dict]:
    return [{"t": t, "funding_rate": rate, "mark_price": 27000.0} for t in times]


def _mixed_interval_times(w0: int, w1: int) -> list[int]:
    """1h/4h/8h settlement mix ending exactly on the window end."""
    times, cursor, step_idx = [w0], w0, 0
    for step_h in [8, 4, 1] * 100:
        nxt = cursor + step_h * HOUR_MS
        if nxt >= w1:
            break
        times.append(nxt)
        cursor = nxt
        step_idx += 1
    if times[-1] != w1:
        times.append(w1)
    return times


def test_coverage_complete_with_mixed_1h_4h_8h_intervals():
    w0, w1 = 1_700_000_000_000, 1_700_000_000_000 + 3 * DAY_MS
    times = _mixed_interval_times(w0, w1)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert {8 * HOUR_MS, 4 * HOUR_MS, HOUR_MS} <= set(gaps)  # 8h/4h/1h mix present
    assert max(gaps) <= GAP_MS and len(times) >= 3
    cov = funding.funding_coverage(_events(times), w0, w1, None)
    assert cov.coverage == 1
    assert cov.complete is True
    assert cov.coverage_fraction == 1.0
    assert cov.gaps == ()
    assert cov.reason_code is None


def test_coverage_gap_over_24h_nulls_metric_and_reports_fraction():
    w0, w1 = 1_700_000_000_000, 1_700_000_000_000 + 3 * DAY_MS
    times = [t for t in _mixed_interval_times(w0, w1) if not (w0 + DAY_MS < t < w0 + DAY_MS + 30 * HOUR_MS)]
    # The surviving neighbours of the removed segment must form a >24h gap.
    raw_gaps = [(a, b, b - a) for a, b in zip(times, times[1:])]
    big = [g for g in raw_gaps if g[2] > GAP_MS]
    assert len(big) == 1
    a, b, gap_ms = big[0]
    excess = gap_ms - GAP_MS
    expected_fraction = ((times[-1] - times[0]) - excess) / (w1 - w0)

    cov = funding.funding_coverage(_events(times), w0, w1, None)
    assert cov.coverage == 0
    assert cov.complete is False
    assert cov.coverage_fraction == pytest.approx(expected_fraction)
    assert cov.reason_code == "FUNDING_HISTORY_INCOMPLETE"
    assert len(cov.gaps) == 1
    assert cov.gaps[0]["start_ms"] == a and cov.gaps[0]["end_ms"] == b
    # Incomplete window: the 30D/90D metric must be treated as null, never partial.
    window_metric = sum(e["funding_rate"] for e in _events(times)) if cov.complete else None
    assert window_metric is None


def test_coverage_late_first_event_deducts_head_gap():
    as_of = 1_700_000_000_000
    w0 = as_of - 90 * DAY_MS
    first = w0 + 10 * DAY_MS  # first settlement arrives 10 days late
    times = []
    t = first
    while t <= as_of:
        times.append(t)
        t += 8 * HOUR_MS
    cov = funding.funding_coverage(_events(times), w0, as_of, None)
    assert cov.coverage == 0  # late head edge alone breaks completeness
    expected_fraction = (times[-1] - times[0]) / (as_of - w0)  # no adjacent excess
    assert cov.coverage_fraction == pytest.approx(expected_fraction)
    assert any(g["start_ms"] == w0 for g in cov.gaps)  # head shortfall reported


def test_coverage_first_seen_must_not_shorten_90d_window():
    as_of = 1_700_000_000_000
    w0 = as_of - 90 * DAY_MS
    # Dense 8h settlements only in the last 30 days; listing date unknown.
    times = []
    t = as_of - 30 * DAY_MS
    while t <= as_of:
        times.append(t)
        t += 8 * HOUR_MS
    cov = funding.funding_coverage(_events(times), w0, as_of, None)
    assert cov.window_start_ms == w0  # no first_seen shortcut
    assert cov.coverage == 0
    assert cov.coverage_fraction < 1.0


def test_coverage_valid_onboard_shortens_window_for_new_coin():
    as_of = 1_700_000_000_000
    onboard = as_of - 10 * DAY_MS
    times = []
    t = onboard
    while t <= as_of:
        times.append(t)
        t += 8 * HOUR_MS
    cov = funding.funding_coverage(_events(times), as_of - 90 * DAY_MS, as_of, onboard)
    assert cov.window_start_ms == onboard
    assert cov.coverage == 1
    assert cov.coverage_fraction == 1.0


def test_coverage_no_events_single_event_and_empty_window():
    w0, w1 = 1_700_000_000_000, 1_700_000_000_000 + 3 * DAY_MS
    empty = funding.funding_coverage([], w0, w1, None)
    assert (empty.coverage, empty.coverage_fraction) == (0, 0.0)
    assert empty.reason_code == "FUNDING_HISTORY_INCOMPLETE"
    assert len(empty.gaps) == 1

    single = funding.funding_coverage(_events([w0]), w0, w1, None)
    assert (single.coverage, single.coverage_fraction) == (0, 0.0)

    zero = funding.funding_coverage(_events([w0]), w0, w0, None)
    assert (zero.coverage, zero.coverage_fraction) == (0, 0.0)
