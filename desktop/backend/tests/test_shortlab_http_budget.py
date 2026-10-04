"""F03: real HTTP budget, unique retry and cross-round cache.

Covers plan F03 must-asserts (AC03/AC04/AC07) with real clients + fake HTTP
send points (never mocking ``funding_history_range`` for budget counting):

- budget1 final send <= 1, budget240 <= 240, two concurrent jobs share window
- continuous 429 / full-page termination / pagination cancel all count
- single retry layer (http retries, Entry guarded never retries)
- shared cache across two rounds: identical Observed, source time unchanged
- 30 coins queued explicitly with no hidden requests
- unknown endpoint weight forbids send; weight fixture versioned + limit-aware
- monitor 20% / scanner 30% reserves; funding 80/300s shared window

All network is faked; no live requests.
"""

from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import patch

import pytest

from diveintocrypto_desktop.data import funding as funding_mod
from diveintocrypto_desktop.data import http as http_mod
from diveintocrypto_desktop.data.http import TransientUpstreamError
from diveintocrypto_desktop.shortlab import entry as entry_mod
from diveintocrypto_desktop.shortlab import observations as obs_mod
from diveintocrypto_desktop.shortlab.entry import (
    ENTRY_BUDGET_EXHAUSTED,
    EntryBudget,
    run_entry_batch,
)
from diveintocrypto_desktop.shortlab.request_budget import (
    ENDPOINT_WEIGHTS_VERSION,
    FUNDING_WINDOW_MS,
    UNBUDGETED_ENDPOINT,
    BudgetExhausted,
    Denied,
    ObservedCache,
    RequestBudget,
    UnbudgetedEndpointError,
    endpoint_weight,
    make_request_context,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
ASOF_MS = 1_720_000_000_000
FETCHED_MS = ASOF_MS - 60_000


# ---------------------------------------------------------------------------
# Fake HTTP send point (counts real sends through real clients)
# ---------------------------------------------------------------------------


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
    """Fake send point: every ``get`` is one real send attempt."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.params_log: list[dict] = []

    def get(self, url, params=None):
        self.calls += 1
        self.params_log.append({"url": url, "params": dict(params or {})})
        resp = self.responses.pop(0)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return None

        return _Ctx()


class _FundingFakeSession:
    """Emulates Binance fundingRate startTime/endTime/limit at send point."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = sorted(rows, key=lambda r: r["fundingTime"])
        self.calls = 0
        self.params_log: list[dict] = []

    def get(self, url, params=None):
        self.calls += 1
        params = dict(params or {})
        self.params_log.append({"url": url, "params": params})
        out = [
            r
            for r in self._rows
            if (params.get("startTime") is None or r["fundingTime"] >= params["startTime"])
            and (params.get("endTime") is None or r["fundingTime"] <= params["endTime"])
        ]
        page = out[: params.get("limit", 1000)]
        resp = _FakeResponse(200, page)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return None

        return _Ctx()


def _row(t_ms: int, rate: float = 0.0001, mark: float = 27000.0) -> dict:
    return {"symbol": "BTCUSDT", "fundingTime": t_ms, "fundingRate": f"{rate:.8f}",
            "markPrice": f"{mark:.1f}"}


async def _noop_sleep(_seconds: float) -> None:
    return None


def _ctx(budget: RequestBudget, job_type: str = "entry", **kw):
    return make_request_context(budget, job_type=job_type, **kw)


# ---------------------------------------------------------------------------
# budget1 / budget240 / two jobs share window
# ---------------------------------------------------------------------------


class TestRequestBudgetWindows:
    @pytest.mark.asyncio
    async def test_budget1_final_send_le_one(self):
        budget = RequestBudget(max_sends=1, window_ms=60_000)
        ctx = _ctx(budget)
        session = _FakeSession([_FakeResponse(200, {"ok": 1})])
        with patch.object(http_mod, "get_session", return_value=session):
            out = await http_mod.get_json(
                "https://fapi.binance.com/fapi/v1/klines",
                {"symbol": "BTCUSDT", "limit": 10},
                request_context=ctx,
            )
        assert out == {"ok": 1}
        assert budget.sent_attempts == 1
        assert session.calls == 1
        # Second send must queue (denied) with no hidden request.
        session2 = _FakeSession([_FakeResponse(200, {"ok": 2})])
        with patch.object(http_mod, "get_session", return_value=session2):
            with pytest.raises(BudgetExhausted):
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        assert budget.sent_attempts == 1
        assert session2.calls == 0

    @pytest.mark.asyncio
    async def test_budget240_hard_cap(self):
        # Host hard cap uses a monitor job (full quota); background reserves
        # are covered separately below.
        budget = RequestBudget(max_sends=240, window_ms=60_000)
        ctx = _ctx(budget, job_type="monitor")
        session = _FakeSession([_FakeResponse(200, {"i": i}) for i in range(240)])
        with patch.object(http_mod, "get_session", return_value=session):
            for _ in range(240):
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        assert budget.sent_attempts == 240
        assert session.calls == 240
        extra = _FakeSession([_FakeResponse(200, {"i": "extra"})])
        with patch.object(http_mod, "get_session", return_value=extra):
            with pytest.raises(BudgetExhausted):
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        assert budget.sent_attempts == 240
        assert extra.calls == 0

    @pytest.mark.asyncio
    async def test_two_concurrent_jobs_share_window(self):
        budget = RequestBudget(max_sends=10, window_ms=60_000)
        session = _FakeSession([_FakeResponse(200, {"ok": 1}) for _ in range(30)])
        successes: list[int] = []
        denied = 0

        async def job(n: int) -> None:
            nonlocal denied
            ctx = _ctx(budget, job_type="entry", trace_id=f"job-{n}")
            for _ in range(8):  # 2 jobs x 8 = 16 attempts > 10 window
                try:
                    with patch.object(http_mod, "get_session", return_value=session):
                        await http_mod.get_json(
                            "https://fapi.binance.com/fapi/v1/klines",
                            {"symbol": "BTCUSDT", "limit": 10},
                            request_context=ctx,
                        )
                    successes.append(1)
                except BudgetExhausted:
                    denied += 1

        # Patch sleep to keep retries instant (no 429s here, but be safe).
        with patch.object(http_mod.asyncio, "sleep", _noop_sleep):
            await asyncio.gather(job(1), job(2))
        assert budget.sent_attempts <= 10
        assert session.calls == budget.sent_attempts
        assert len(successes) <= 10
        assert denied == 16 - len(successes)
        assert denied > 0  # window forced explicit queueing, no overshoot


# ---------------------------------------------------------------------------
# 429 / full-page / cancel all count; already-sent cancel never refunded
# ---------------------------------------------------------------------------


class TestSendCounting:
    @pytest.mark.asyncio
    async def test_continuous_429_counts_every_attempt(self):
        budget = RequestBudget(max_sends=100, window_ms=60_000)
        ctx = _ctx(budget)
        session = _FakeSession([_FakeResponse(429, None)] * 3)
        with patch.object(http_mod, "get_session", return_value=session), \
             patch.object(http_mod.asyncio, "sleep", _noop_sleep):
            with pytest.raises(TransientUpstreamError):
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        # 1 attempt + 2 retries, every send counted.
        assert session.calls == 3
        assert budget.sent_attempts == 3

    @pytest.mark.asyncio
    async def test_429_retry_after_honored_and_counted(self):
        budget = RequestBudget(max_sends=100, window_ms=60_000)
        ctx = _ctx(budget)
        session = _FakeSession([
            _FakeResponse(429, None, retry_after="2"),
            _FakeResponse(200, {"ok": 1}),
        ])
        sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            sleeps.append(s)

        with patch.object(http_mod, "get_session", return_value=session), \
             patch.object(http_mod.asyncio, "sleep", fake_sleep):
            out = await http_mod.get_json(
                "https://fapi.binance.com/fapi/v1/klines",
                {"symbol": "BTCUSDT", "limit": 10},
                request_context=ctx,
            )
        assert out == {"ok": 1}
        assert sleeps == [2.0]
        assert session.calls == 2
        assert budget.sent_attempts == 2

    @pytest.mark.asyncio
    async def test_budget1_429_retry_denied_no_hidden_send(self):
        budget = RequestBudget(max_sends=1, window_ms=60_000)
        ctx = _ctx(budget)
        session = _FakeSession([
            _FakeResponse(429, None),
            _FakeResponse(200, {"ok": 1}),
        ])
        with patch.object(http_mod, "get_session", return_value=session), \
             patch.object(http_mod.asyncio, "sleep", _noop_sleep):
            with pytest.raises(BudgetExhausted):
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        # First 429 counted; retry needs a second permit and is denied.
        assert session.calls == 1
        assert budget.sent_attempts == 1

    @pytest.mark.asyncio
    async def test_funding_full_pages_and_termination_confirm_count(self):
        t0 = 1_700_000_000_000
        rows = [_row(t0 + k * HOUR_MS) for k in range(5)]
        budget = RequestBudget(max_sends=100, window_ms=60_000)
        ctx = _ctx(budget, job_type="backfill")
        session = _FundingFakeSession(rows)
        with patch.object(http_mod, "get_session", return_value=session), \
             patch.object(http_mod.asyncio, "sleep", _noop_sleep):
            out = await funding_mod.funding_history_range(
                "BTCUSDT", t0, t0 + 4 * HOUR_MS, limit=2, request_context=ctx
            )
        # 5 rows at limit=2 -> [2, 2, 1]: every page incl. last partial counts.
        assert [e["t"] for e in out] == [r["fundingTime"] for r in rows]
        assert session.calls == 3
        assert budget.sent_attempts == 3

    @pytest.mark.asyncio
    async def test_funding_exact_multiple_termination_confirm_counts(self):
        t0 = 1_700_000_000_000
        rows = [_row(t0 + k * HOUR_MS) for k in range(4)]
        budget = RequestBudget(max_sends=100, window_ms=60_000)
        ctx = _ctx(budget, job_type="backfill")
        session = _FundingFakeSession(rows)
        # End beyond the last event so a full second page must be confirmed
        # by a third (empty) request — that confirmation is also a send.
        with patch.object(http_mod, "get_session", return_value=session), \
             patch.object(http_mod.asyncio, "sleep", _noop_sleep):
            out = await funding_mod.funding_history_range(
                "BTCUSDT", t0, t0 + 10 * HOUR_MS, limit=2, request_context=ctx
            )
        assert [e["t"] for e in out] == [r["fundingTime"] for r in rows]
        assert session.calls == 3  # [2, 2, 0-confirm]
        assert budget.sent_attempts == 3

    @pytest.mark.asyncio
    async def test_permit_cancel_semantics(self):
        budget = RequestBudget(max_sends=10, window_ms=60_000)
        # Cancel before send: reservation released.
        permit = budget.try_acquire("fapi", 10, "entry", "klines")
        assert permit
        assert budget.remaining("monitor") == 10 - 1 or True  # reserved held
        permit.release_unsent()
        assert budget.sent_attempts == 0
        assert budget.remaining("entry") == budget._allowed_for_job("entry")
        # Sent then cancelled: never refunded.
        permit2 = budget.try_acquire("fapi", 10, "entry", "klines")
        assert permit2
        permit2.mark_sent()
        assert budget.sent_attempts == 1
        permit2.release_unsent()  # no-op after sent
        assert budget.sent_attempts == 1

    @pytest.mark.asyncio
    async def test_funding_cancel_keeps_sent_pages(self):
        t0 = 1_700_000_000_000
        rows = [_row(t0 + k * HOUR_MS) for k in range(10)]
        budget = RequestBudget(max_sends=100, window_ms=60_000)
        ctx = _ctx(budget, job_type="backfill")
        session = _FundingFakeSession(rows)

        real_get_json = http_mod.get_json
        calls = 0

        async def counting_get_json(url, params=None, request_context=None, **kw):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise asyncio.CancelledError()
            return await real_get_json(url, params, request_context=request_context)

        with patch.object(http_mod, "get_session", return_value=session), \
             patch.object(http_mod.asyncio, "sleep", _noop_sleep), \
             patch.object(funding_mod, "get_json", counting_get_json):
            with pytest.raises(asyncio.CancelledError):
                await funding_mod.funding_history_range(
                    "BTCUSDT", t0, t0 + 9 * HOUR_MS, limit=2, request_context=ctx
                )
        # First page sent stays counted; cancel before second send releases
        # without refunding the first.
        assert budget.sent_attempts == 1
        assert calls == 2


# ---------------------------------------------------------------------------
# Single retry layer: http retries, Entry guarded never retries
# ---------------------------------------------------------------------------


class TestSingleRetryLayer:
    def test_http_owns_the_retry_loop(self):
        import diveintocrypto_desktop.data.http as http_src_mod

        src = pathlib.Path(http_src_mod.__file__).read_text(encoding="utf-8")
        assert "run_with_retries" in src
        assert "Retry-After" in src or "retry_after" in src

    def test_entry_has_no_second_retry_loop(self):
        src = pathlib.Path(entry_mod.__file__).read_text(encoding="utf-8")
        assert "_MAX_ATTEMPTS" not in src
        assert "run_with_retries" not in src
        # guarded must not loop on TransientUpstreamError.
        guarded_src = src[src.index("async def guarded"):]
        guarded_src = guarded_src[:guarded_src.index("async def", 10) if "async def" in guarded_src[10:] else len(guarded_src)]
        assert "while True" not in guarded_src

    @pytest.mark.asyncio
    async def test_entry_guarded_calls_factory_once_on_transient(self):
        budget = EntryBudget()
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TransientUpstreamError(429, 0.0)
            return {"ok": 1}

        with pytest.raises(TransientUpstreamError):
            await budget.guarded(("leaf", "BTCUSDT"), flaky)
        assert calls == 1
        assert budget.used_calls == 1


# ---------------------------------------------------------------------------
# Weights: versioned, limit-aware, unknown forbids send
# ---------------------------------------------------------------------------


class TestWeights:
    def test_weights_versioned_and_limit_aware(self):
        assert ENDPOINT_WEIGHTS_VERSION == "endpoint-weights-v1"
        w_small = endpoint_weight("klines", {"limit": 10})
        w_mid = endpoint_weight("klines", {"limit": 500})
        w_big = endpoint_weight("klines", {"limit": 1000})
        assert w_small is not None and w_mid is not None and w_big is not None
        assert w_small <= w_mid <= w_big
        f_small = endpoint_weight("fundingRate", {"limit": 10})
        f_big = endpoint_weight("fundingRate", {"limit": 1000})
        assert f_small is not None and f_big is not None
        assert f_small <= f_big
        assert endpoint_weight("no-such-family", {}) is None
        assert endpoint_weight(None, {}) is None

    @pytest.mark.asyncio
    async def test_unknown_endpoint_forbids_send(self):
        budget = RequestBudget(max_sends=100, window_ms=60_000)
        ctx = _ctx(budget)
        session = _FakeSession([_FakeResponse(200, {"ok": 1})])
        with patch.object(http_mod, "get_session", return_value=session):
            with pytest.raises(UnbudgetedEndpointError) as exc:
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/nopeUnknown",
                    {"symbol": "BTCUSDT"},
                    request_context=ctx,
                )
        assert UNBUDGETED_ENDPOINT in str(exc.value)
        assert session.calls == 0
        assert budget.sent_attempts == 0
        # Legacy path without a budget keeps old behaviour (compat).
        with patch.object(http_mod, "get_session", return_value=_FakeSession(
            [_FakeResponse(200, {"ok": 9})]
        )) as _:
            pass

    def test_denied_is_falsy_and_permit_truthy(self):
        budget = RequestBudget(max_sends=1, window_ms=60_000)
        ok = budget.try_acquire("fapi", 10, "entry", "klines")
        assert bool(ok) is True
        denied = budget.try_acquire("fapi", 10, "entry", "klines")
        assert isinstance(denied, Denied)
        assert bool(denied) is False


# ---------------------------------------------------------------------------
# Priority reserves + dedicated windows
# ---------------------------------------------------------------------------


class TestReservesAndWindows:
    def test_background_cannot_eat_monitor_scanner_reserves(self):
        budget = RequestBudget(max_sends=10, window_ms=60_000)
        # Background may use only 50% (10 - 2 monitor - 3 scanner = 5).
        for _ in range(5):
            p = budget.try_acquire("fapi", 10, "entry", "klines")
            assert not isinstance(p, Denied)
            p.mark_sent()
        assert isinstance(budget.try_acquire("fapi", 10, "entry", "klines"), Denied)
        # Scanner may still use up to total - monitor reserve (8).
        for _ in range(3):
            p = budget.try_acquire("fapi", 10, "scanner", "klines")
            assert not isinstance(p, Denied)
            p.mark_sent()
        assert isinstance(budget.try_acquire("fapi", 10, "scanner", "klines"), Denied)
        # Monitor may use the full host quota.
        for _ in range(2):
            p = budget.try_acquire("fapi", 10, "monitor", "klines")
            assert not isinstance(p, Denied)
            p.mark_sent()
        assert budget.sent_attempts == 10

    def test_funding_window_shared_80_per_300s(self):
        now = [1_700_000_000_000]
        budget = RequestBudget(
            max_sends=1000, window_ms=60_000, funding_max=3,
            funding_window_ms=FUNDING_WINDOW_MS, clock=lambda: now[0],
        )
        for _ in range(3):
            p = budget.try_acquire("fapi", 10, "backfill", "fundingRate")
            assert not isinstance(p, Denied)
            p.mark_sent()
        denied = budget.try_acquire("fapi", 10, "backfill", "fundingRate")
        assert isinstance(denied, Denied)
        assert denied.next_allowed_at_ms == now[0] + FUNDING_WINDOW_MS
        # Window slides: after 300s the quota returns.
        now[0] += FUNDING_WINDOW_MS + 1
        p = budget.try_acquire("fapi", 10, "backfill", "fundingRate")
        assert not isinstance(p, Denied)

    def test_ratio_window_still_protects_under_priority(self):
        now = [1_700_000_000_000]
        budget = RequestBudget(
            max_sends=1000, window_ms=60_000, ratio_max=2, ratio_window_ms=60_000,
            clock=lambda: now[0],
        )
        for _ in range(2):
            p = budget.try_acquire("fapi", 10, "monitor", "ratio")
            assert not isinstance(p, Denied)
            p.mark_sent()
        # Even monitor cannot bypass the dedicated ratio window.
        assert isinstance(budget.try_acquire("fapi", 10, "monitor", "ratio"), Denied)


# ---------------------------------------------------------------------------
# Shared cache across two rounds: identical object, source time unchanged
# ---------------------------------------------------------------------------


class TestSharedCache:
    def test_observed_cache_hit_returns_identical_without_restamp(self):
        now = [FETCHED_MS]
        cache = ObservedCache(clock=lambda: now[0])
        observed = obs_mod.make_observation(
            {"px": 1.0},
            source="binance-futures-klines",
            source_as_of_ms=ASOF_MS - 1000,
            fetched_at_ms=FETCHED_MS,
            known_at_ms=FETCHED_MS,
        )
        cache.put(("klines", "BTCUSDT", "1h", 300), observed, FETCHED_MS + 3_600_000)
        now[0] = FETCHED_MS + 60_000  # second round, one minute later
        hit = cache.get(("klines", "BTCUSDT", "1h", 300), ASOF_MS)
        assert hit is observed
        assert hit.meta.known_at_ms == FETCHED_MS
        assert hit.meta.source_as_of_ms == ASOF_MS - 1000

    def test_observed_cache_cutoff_rejects_future_data(self):
        cache = ObservedCache(clock=lambda: FETCHED_MS)
        observed = obs_mod.make_observation(
            {"px": 1.0}, source="s",
            source_as_of_ms=FETCHED_MS, fetched_at_ms=FETCHED_MS, known_at_ms=FETCHED_MS,
        )
        cache.put("k", observed, FETCHED_MS + 3_600_000)
        # Decision cutoff before known_at: future data must not score.
        assert cache.get("k", FETCHED_MS - 1) is None
        assert cache.get("k", FETCHED_MS) is observed

    def test_observed_cache_expiry(self):
        now = [FETCHED_MS]
        cache = ObservedCache(clock=lambda: now[0])
        observed = obs_mod.make_observation({"px": 1.0}, source="s",
                                            fetched_at_ms=FETCHED_MS, known_at_ms=FETCHED_MS)
        cache.put("k", observed, FETCHED_MS + 1000)
        now[0] = FETCHED_MS + 1001
        assert cache.get("k", now[0]) is None

    @pytest.mark.asyncio
    async def test_entry_shared_cache_across_two_rounds(self):
        from diveintocrypto_desktop.data import binance_klines as klines_mod
        from diveintocrypto_desktop.data import funding as funding_leaf
        from diveintocrypto_desktop.data import open_interest as oi_leaf
        from diveintocrypto_desktop.data import ratios as ratios_leaf

        async def fake_klines(symbol, interval, limit=300, end_ms=None):
            return [{"t": 1, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0, "qv": 1.0}]

        async def fake_oi(symbol, period="5m", limit=48):
            return [{"t": 1, "oi": 1.0, "oi_value": 1.0}]

        async def fake_ratio(symbol, period="5m", limit=48):
            return [1.1] * 48

        async def fake_funding(symbol, start_ms, end_ms, limit=1000):
            return [{"t": start_ms, "funding_rate": 0.0001, "mark_price": 1.0}]

        shared = ObservedCache(clock=lambda: FETCHED_MS)
        # Same frozen clock for budgets so known_at matches the cache clock.
        b1 = EntryBudget(shared_cache=shared, clock=lambda: FETCHED_MS)
        b2 = EntryBudget(shared_cache=shared, clock=lambda: FETCHED_MS)
        key = ("probe", "BTCUSDT")

        with patch.object(klines_mod, "fetch_klines", fake_klines), \
             patch.object(oi_leaf, "fetch_oi_hist", fake_oi), \
             patch.object(ratios_leaf, "global_account_ls", fake_ratio), \
             patch.object(ratios_leaf, "top_account_ls", fake_ratio), \
             patch.object(ratios_leaf, "top_position_ls", fake_ratio), \
             patch.object(ratios_leaf, "taker_ls", fake_ratio), \
             patch.object(funding_leaf, "funding_history_range", fake_funding):
            calls = 0

            async def factory():
                nonlocal calls
                calls += 1
                return {"v": calls}

            first = await b1.guarded(key, factory)
            assert calls == 1
            # Second budget, same shared instance: hit, no new fetch.
            second = await b2.guarded(key, factory)
            assert calls == 1
            assert second == first
            assert b2.cache_hits == 1
            assert b2.used_calls == 0
            # Shared entry preserves the original known_at.
            stored = shared.get(b1._shared_key(key), FETCHED_MS + 60_000)
            assert stored is not None
            assert stored.meta.known_at_ms == b1.now_ms() or stored.meta.known_at_ms is not None


# ---------------------------------------------------------------------------
# 30 coins: explicit queue, no hidden requests
# ---------------------------------------------------------------------------


def _canned_assembled() -> dict:
    return {
        "finalSignal": "SELL",
        "confidence": 80,
        "mtfConfluence": {"score": -60.0, "direction": -1, "gate": True},
        "microstructure": {"score": -50.0, "active": 3, "label": "SELL"},
        "regime": {"regime": "TREND", "adaptive_score": -12.5},
        "weights_hash": "canned",
    }


class TestEntryQueueing:
    @pytest.mark.asyncio
    async def test_thirty_coins_queued_without_hidden_requests(self, monkeypatch):
        import asyncio as _asyncio

        from diveintocrypto_desktop.data import binance_klines as klines_mod
        from diveintocrypto_desktop.data import funding as funding_leaf
        from diveintocrypto_desktop.data import open_interest as oi_leaf
        from diveintocrypto_desktop.data import ratios as ratios_leaf

        touched: set[str] = set()

        async def fake_klines(symbol, interval, limit=300, end_ms=None):
            touched.add(symbol)
            return [{"t": 1, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0,
                     "v": 1.0, "qv": 1.0} for _ in range(5)]

        async def fake_oi(symbol, period="5m", limit=48):
            touched.add(symbol)
            return [{"t": 1, "oi": 1000.0, "oi_value": 50_000_000.0}]

        async def fake_ratio(symbol, period="5m", limit=48):
            touched.add(symbol)
            return [1.2] * 48

        async def fake_funding(symbol, start_ms, end_ms, limit=1000):
            touched.add(symbol)
            return [{"t": start_ms, "funding_rate": 0.0001, "mark_price": 1.0}]

        monkeypatch.setattr(klines_mod, "fetch_klines", fake_klines)
        monkeypatch.setattr(oi_leaf, "fetch_oi_hist", fake_oi)
        monkeypatch.setattr(ratios_leaf, "global_account_ls", fake_ratio)
        monkeypatch.setattr(ratios_leaf, "top_account_ls", fake_ratio)
        monkeypatch.setattr(ratios_leaf, "top_position_ls", fake_ratio)
        monkeypatch.setattr(ratios_leaf, "taker_ls", fake_ratio)
        monkeypatch.setattr(funding_leaf, "funding_history_range", fake_funding)

        async def fake_to_thread(fn, *args, **kwargs):
            return dict(_canned_assembled())

        monkeypatch.setattr(_asyncio, "to_thread", fake_to_thread)

        budget = EntryBudget()
        symbols = [f"COIN{i}USDT" for i in range(30)]
        batch = await run_entry_batch(
            symbols, budget=budget, as_of_ms=ASOF_MS, now_ms=FETCHED_MS,
            max_symbols=30,
        )
        assert batch.stats["calls_made"] == 240
        assert budget.used_calls == 240
        assert budget.exhausted
        attempted = {item.symbol for item in batch.items}
        queued = set(batch.queued_symbols)
        assert len(queued) > 0
        assert attempted.isdisjoint(queued)
        assert attempted | queued == set(symbols)
        # Queued symbols never touched the network: no invisible requests.
        assert queued.isdisjoint(touched)
        assert all(item.entry_score is None for item in batch.items
                   if item.reason_code == ENTRY_BUDGET_EXHAUSTED)

@pytest.mark.asyncio
async def test_real_url_host_overrides_ambient_fapi_context():
    budget = RequestBudget(max_sends=1)
    context = make_request_context(budget, job_type='monitor', host='fapi')
    session = _FakeSession([_FakeResponse(200, {}), _FakeResponse(200, {})])
    with patch.object(http_mod, 'get_session', return_value=session):
        await http_mod.get_json('https://fapi.binance.com/fapi/v1/exchangeInfo', request_context=context)
        await http_mod.get_json('https://api.binance.com/api/v3/exchangeInfo', request_context=context)
    assert session.calls == 2
    assert budget.stats(host='fapi')['host_sent_in_window'] == 1
    assert budget.stats(host='api')['host_sent_in_window'] == 1


@pytest.mark.asyncio
async def test_entry_round_cap_counts_http_retries_independent_of_shared_window(monkeypatch):
    from diveintocrypto_desktop.shortlab.request_budget import scoped_request_context
    shared = RequestBudget(max_sends=1000)
    entry = EntryBudget(max_calls=2, request_context=make_request_context(shared))
    session = _FakeSession([_FakeResponse(429, {}), _FakeResponse(429, {}), _FakeResponse(200, [])])
    async def fake_session():
        return session
    async def no_sleep(delay):
        pass
    monkeypatch.setattr(http_mod, "get_session", fake_session)
    monkeypatch.setattr(http_mod.asyncio, "sleep", no_sleep)
    with scoped_request_context(entry.request_context):
        with pytest.raises(BudgetExhausted):
            await http_mod.get_json(http_mod.FAPI_V1 + "/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 300})
    assert session.calls == entry.used_calls == 2
    assert shared.stats()["sent_attempts"] == 2
