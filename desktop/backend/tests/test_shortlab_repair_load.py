"""R11a budget section of the load test (V13).

NOTE (ownership): this file's R11a section covers ONLY the budget/HTTP
load behaviour (240/round, Funding 80/300s, weight windows, provider
RPS/month independence) with fake clocks/transports. The full
``drive_ticks`` rotation/capacity test (11 symbols, 50 plans, 500 scores,
8s deadline, fair rotation, unserved降级) belongs to R11b
(``hedge/jobs.py`` + ``hedge/market.py`` + ``scheduler.py`` fair side) and
must be added by R11b without altering this R11a section.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from diveintocrypto_desktop.data import http as http_mod
from diveintocrypto_desktop.shortlab import request_budget as rb


class _FakeResponse:
    def __init__(self, status: int, payload) -> None:
        self.status = status
        self._payload = payload
        self.headers = {}

    def raise_for_status(self) -> None:
        return None

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, n: int) -> None:
        self.calls = 0
        self._n = n

    def get(self, url, params=None):
        self.calls += 1

        class _Ctx:
            def __init__(self, payload):
                self._payload = payload

            async def __aenter__(self):
                return _FakeResponse(200, self._payload)

            async def __aexit__(self, *exc):
                return None

        return _Ctx({"i": self.calls})


async def _noop_sleep(_s: float) -> None:
    return None


class TestR11aBudgetLoad:
    """R11a-owned budget load: bounded rounds, independent windows."""

    @pytest.mark.asyncio
    async def test_single_round_240_bounded(self):
        budget = rb.RequestBudget(max_sends=240, window_ms=60_000)
        ctx = rb.make_request_context(budget, job_type="monitor")
        session = _FakeSession(300)
        with patch.object(http_mod, "get_session", return_value=session):
            for _ in range(240):
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        assert budget.sent_attempts == 240
        assert session.calls == 240
        extra = _FakeSession(1)
        with patch.object(http_mod, "get_session", return_value=extra):
            with pytest.raises(rb.BudgetExhausted):
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        assert extra.calls == 0

    def test_funding_and_weight_windows_independent(self):
        now = [1_700_000_000_000]
        budget = rb.RequestBudget(
            max_sends=1000,
            window_ms=60_000,
            funding_max=80,
            funding_window_ms=300_000,
            max_weight=100000,
            weight_window_ms=60_000,
            clock=lambda: now[0],
        )
        for _ in range(80):
            p = budget.try_acquire("fapi", 5, "backfill", "fundingInfo")
            assert not isinstance(p, rb.Denied)
            p.mark_sent()
        assert isinstance(budget.try_acquire("fapi", 5, "backfill", "fundingInfo"), rb.Denied)
        # Non-funding families still have host quota.
        p = budget.try_acquire("fapi", 10, "monitor", "klines")
        assert not isinstance(p, rb.Denied)

    @pytest.mark.asyncio
    async def test_concurrent_background_never_eats_reserves(self):
        budget = rb.RequestBudget(max_sends=20, window_ms=60_000)
        session = _FakeSession(100)
        successes: list[int] = []

        async def _bg():
            ctx = rb.make_request_context(budget, job_type="entry")
            for _ in range(20):
                try:
                    with patch.object(http_mod, "get_session", return_value=session):
                        await http_mod.get_json(
                            "https://fapi.binance.com/fapi/v1/klines",
                            {"symbol": "BTCUSDT", "limit": 10},
                            request_context=ctx,
                        )
                    successes.append(1)
                except rb.BudgetExhausted:
                    pass

        with patch.object(http_mod.asyncio, "sleep", _noop_sleep):
            await asyncio.gather(_bg(), _bg())
        # Background cap is 10 (20 - 4 monitor - 6 scanner); never 20.
        assert len(successes) <= 10
        assert budget.sent_attempts <= 10
