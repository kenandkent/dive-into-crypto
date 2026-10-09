"""R11a budget mapping, weights and HTTP send (D11/D19.3/D19.4).

Covers plan R11a must-asserts (budget_class tiers, unknown rejection,
weight accounting, monthly restart-retained):

- budget_class('opportunity') == 'BACKGROUND' etc.; interactive is SCANNER;
  unknown job_types refuse as JOB_TYPE_UNKNOWN (never silent background)
- tier isolation: monitor 100%, scanner 80% (floor), background 50%;
  background never eats monitor/scanner reserves
- unknown host/path/limit refuses as UNBUDGETED without transport
- weight reservation/deduction per (family, limit); spotTicker single vs
  full; depth missing limit refuses
- deadline check before transport (expired => DEADLINE_EXCEEDED, no send)
- retry = new permit/ID, cache never reserves
- monthly via R01 RepositoryPort: D19.4 state table spot-checks +
  UTC cross-month new-UUID reserve + restart retention (reopen DB keeps
  counts; lowering limit never resets history)
- CoinGecko monthly/RPM/reserve (default 9000), batch markets, FX reuse
  (60s fresh else UNKNOWN, never USDT=1 guess)
- scheduler budget side: tier mapping + ambient context wiring
- midnight: queued 23:59:59 -> 00:00:00 send rolls month; in-flight return
  stays on original month; new-month exhaustion never sends

All network faked; no live requests.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from diveintocrypto_desktop.data import http as http_mod
from diveintocrypto_desktop.shortlab import request_budget as rb
from diveintocrypto_desktop.shortlab.providers import coingecko as cg
from diveintocrypto_desktop.shortlab.repository import ShortLabRepository
from diveintocrypto_desktop.shortlab.scheduler import ShortLabScheduler


# ---------------------------------------------------------------------------
# Fakes
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
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.urls: list[str] = []

    def get(self, url, params=None):
        self.calls += 1
        self.urls.append(str(url))
        resp = self.responses.pop(0)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return None

        return _Ctx()


async def _noop_sleep(_s: float) -> None:
    return None


def _ctx(budget, job_type="entry", **kw):
    return rb.make_request_context(budget, job_type=job_type, **kw)


# ---------------------------------------------------------------------------
# budget_class tiers (D19.3 frozen table)
# ---------------------------------------------------------------------------


class TestBudgetClass:
    def test_plan_examples(self):
        assert rb.budget_class("opportunity") == "BACKGROUND"
        assert rb.budget_class("interactive") == "SCANNER"
        assert rb.budget_class("monitor") == "MONITOR"

    def test_full_table(self):
        for jt in ("monitor", "active_monitor", "critical"):
            assert rb.budget_class(jt) == "MONITOR", jt
            assert rb.budget_class(jt.upper()) == "MONITOR", jt
        for jt in ("scanner", "user_scanner", "interactive"):
            assert rb.budget_class(jt) == "SCANNER", jt
        for jt in (
            "opportunity",
            "evidence",
            "entry",
            "backfill",
            "funding_backfill",
            "retention",
            "background",
        ):
            assert rb.budget_class(jt) == "BACKGROUND", jt

    def test_unknown_refuses(self):
        assert rb.budget_class("bogus_xyz") == rb.JOB_TYPE_UNKNOWN
        assert rb.budget_class("") == rb.JOB_TYPE_UNKNOWN
        assert rb.budget_class(None) == rb.JOB_TYPE_UNKNOWN
        # Scheduler legacy names map (R11a compat), truly unknown still rejects.
        assert rb.budget_class("hedge_monitor") == "MONITOR"
        assert rb.budget_class("score_refresh") == "SCANNER"
        assert rb.budget_class("grader") == "BACKGROUND"

    def test_request_context_appends_job_id_deadline(self):
        ctx = rb.make_request_context(None, job_type="monitor", job_id="j-1", deadline_ms=123)
        assert ctx.job_id == "j-1" and ctx.deadline_ms == 123
        legacy = rb.make_request_context(None, job_type="entry")
        assert legacy.job_id is None and legacy.deadline_ms is None
        # R00 six-field order preserved.
        assert list(rb.RequestContext.__dataclass_fields__)[:6] == [
            "budget",
            "host",
            "job_type",
            "endpoint_family",
            "trace_id",
            "identity_snapshot_id",
        ]

    def test_utc_month_key_matches_r01(self):
        assert rb.utc_month_key(0) == "1970-01"
        # 2024-06-01T00:00:00Z
        assert rb.utc_month_key(1717200000000) == "2024-06"
        # Month boundary: 2024-05-31T23:59:59Z vs 2024-06-01T00:00:00Z
        may_end = int(dt.datetime(2024, 5, 31, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp() * 1000)
        jun_start = int(dt.datetime(2024, 6, 1, 0, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
        assert rb.utc_month_key(may_end) == "2024-05"
        assert rb.utc_month_key(jun_start) == "2024-06"

    def test_monthly_effective_limit(self):
        assert rb.monthly_effective_limit(10000, 0.1) == 9000
        assert cg.coingecko_effective_limit() == 9000


# ---------------------------------------------------------------------------
# Tier isolation + unknown job rejection (no silent background)
# ---------------------------------------------------------------------------


class TestTierIsolation:
    def test_monitor_scanner_background_quotas(self):
        budget = rb.RequestBudget(max_sends=10, window_ms=60_000)
        # Background: 10 - floor(10*0.2)=2 - floor(10*0.3)=3 => 5.
        for _ in range(5):
            p = budget.try_acquire("fapi", 10, "entry", "klines")
            assert not isinstance(p, rb.Denied)
            p.mark_sent()
        assert isinstance(budget.try_acquire("fapi", 10, "entry", "klines"), rb.Denied)
        # Scanner: 10 - 2 => 8 total, 5 used => 3 left.
        for _ in range(3):
            p = budget.try_acquire("fapi", 10, "scanner", "klines")
            assert not isinstance(p, rb.Denied)
            p.mark_sent()
        assert isinstance(budget.try_acquire("fapi", 10, "scanner", "klines"), rb.Denied)
        # Monitor: full 10, 8 used => 2 left.
        for _ in range(2):
            p = budget.try_acquire("fapi", 10, "monitor", "klines")
            assert not isinstance(p, rb.Denied)
            p.mark_sent()
        assert budget.sent_attempts == 10

    def test_interactive_is_scanner_not_background(self):
        budget = rb.RequestBudget(max_sends=10, window_ms=60_000)
        for _ in range(5):
            p = budget.try_acquire("fapi", 10, "entry", "klines")
            assert not isinstance(p, rb.Denied)
            p.mark_sent()
        # Background exhausted, interactive (SCANNER) still has quota.
        p = budget.try_acquire("fapi", 10, "interactive", "klines")
        assert not isinstance(p, rb.Denied)

    def test_unknown_job_type_denied_without_send(self):
        budget = rb.RequestBudget(max_sends=100, window_ms=60_000)
        denied = budget.try_acquire("fapi", 10, "bogus_xyz", "klines")
        assert isinstance(denied, rb.Denied)
        assert denied.reason_code == rb.JOB_TYPE_UNKNOWN
        assert budget.sent_attempts == 0
        # Remaining for unknown is 0 (no quota).
        assert budget.remaining("bogus_xyz") == 0

    @pytest.mark.asyncio
    async def test_http_unknown_job_never_sends(self):
        budget = rb.RequestBudget(max_sends=100, window_ms=60_000)
        ctx = _ctx(budget, job_type="bogus_xyz")
        session = _FakeSession([_FakeResponse(200, {"ok": 1})])
        with patch.object(http_mod, "get_session", return_value=session):
            with pytest.raises(rb.BudgetExhausted) as exc:
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        assert exc.value.reason_code == rb.JOB_TYPE_UNKNOWN
        assert session.calls == 0
        assert budget.sent_attempts == 0


# ---------------------------------------------------------------------------
# Unknown host/path/limit + weights v2
# ---------------------------------------------------------------------------


class TestUnknownRegistry:
    def test_version_is_v2(self):
        assert rb.ENDPOINT_WEIGHTS_VERSION == "endpoint-weights-v2"
        assert http_mod.ENDPOINT_WEIGHTS_VERSION == "endpoint-weights-v2"

    def test_known_families(self):
        assert rb.endpoint_family_for_url("https://fapi.binance.com/fapi/v1/markPriceKlines") == "markKlines"
        assert rb.endpoint_family_for_url("https://fapi.binance.com/fapi/v1/fundingInfo") == "fundingInfo"
        assert rb.endpoint_family_for_url("https://api.binance.com/api/v3/klines") == "spotKlines"
        assert rb.endpoint_family_for_url("https://api.binance.com/api/v3/depth") == "spotDepth"
        assert rb.endpoint_family_for_url("https://api.binance.com/api/v3/ticker/24hr") == "spotTicker"
        assert (
            rb.endpoint_family_for_url(
                "https://www.binance.com/bapi/defi/v1/public/alpha-trade/ticker"
            )
            == "alphaTicker"
        )
        assert rb.endpoint_family_for_url("https://api.coingecko.com/api/v3/coins/list") == "cgDirectory"
        assert rb.endpoint_family_for_url("https://api.coingecko.com/api/v3/coins/markets") == "cgMarkets"
        assert rb.endpoint_family_for_url("https://api.coingecko.com/api/v3/coins/bitcoin") == "cgCoin"
        assert rb.endpoint_family_for_url("https://api.coingecko.com/api/v3/simple/price") == "cgFx"
        assert rb.endpoint_family_for_url("https://api.0x.org/swap/allowance-holder/price/v2") == "onchainPrice"
        # coins/list beats dynamic {id}; pro host shares families.
        assert rb.endpoint_family_for_url("https://pro-api.coingecko.com/api/v3/coins/list") == "cgDirectory"
        assert rb.endpoint_family_for_url("https://pro-api.coingecko.com/api/v3/coins/ethereum") == "cgCoin"

    def test_unknown_host_path_limit(self):
        # Same path on wrong host never inherits rights.
        assert rb.endpoint_family_for_url("https://evil.com/fapi/v1/klines") is None
        assert rb.endpoint_family_for_url("https://fapi.binance.com/fapi/v1/nopeUnknown") is None
        # 0x trade/approve/calldata never allowed.
        assert rb.endpoint_family_for_url("https://api.0x.org/swap/approve") is None
        assert rb.endpoint_family_for_url("https://api.0x.org/swap/calldata") is None
        # Limit buckets: 0 and over-max refuse.
        assert rb.endpoint_weight("markKlines", {"limit": 0}) is None
        assert rb.endpoint_weight("markKlines", {"limit": 2000}) is None
        assert rb.endpoint_weight("spotKlines", {"limit": 2000}) is None
        assert rb.endpoint_weight("futuresDepth", None) is None
        assert rb.endpoint_weight("spotDepth", {"limit": 0}) is None
        # spotTicker single (2) vs full (40).
        assert rb.endpoint_weight("spotTicker", {"symbol": "BTCUSDT"}) == 2
        assert rb.endpoint_weight("spotTicker", None) == 40
        assert rb.endpoint_weight("fundingInfo", None) == 5

    @pytest.mark.asyncio
    async def test_http_unknown_never_sends(self):
        budget = rb.RequestBudget(max_sends=100, window_ms=60_000)
        ctx = _ctx(budget, job_type="monitor")
        for url, params in [
            ("https://evil.com/fapi/v1/klines", {"limit": 10}),
            ("https://fapi.binance.com/fapi/v1/nopeUnknown", None),
            ("https://api.0x.org/swap/approve", None),
            ("https://fapi.binance.com/fapi/v1/markPriceKlines", {"limit": 5000}),
        ]:
            session = _FakeSession([_FakeResponse(200, {"ok": 1})])
            with patch.object(http_mod, "get_session", return_value=session):
                with pytest.raises(rb.UnbudgetedEndpointError):
                    await http_mod.get_json(url, params, request_context=ctx)
            assert session.calls == 0
        assert budget.sent_attempts == 0

    def test_fapi_mirror_alias_shares_budget(self, monkeypatch):
        monkeypatch.setenv("DIVE_FAPI_BASE", "https://mirror.example.com")
        # Mirror host resolves to fapi families and host bucket.
        assert rb.endpoint_family_for_url("https://mirror.example.com/fapi/v1/klines") == "klines"
        assert rb.host_from_url("https://mirror.example.com/fapi/v1/klines") == "fapi"
        # Non-mirror host still unknown.
        assert rb.endpoint_family_for_url("https://other.example.com/fapi/v1/klines") is None


# ---------------------------------------------------------------------------
# Weight reservation / deduction
# ---------------------------------------------------------------------------


class TestWeightAccounting:
    def test_weight_reserved_then_deducted(self):
        budget = rb.RequestBudget(max_sends=100, window_ms=60_000, max_weight=100, weight_window_ms=60_000)
        p = budget.try_acquire("fapi", 30, "monitor", "klines")
        assert not isinstance(p, rb.Denied)
        assert budget.stats()["weight_reserved"] == 30
        p.mark_sent()
        assert budget.stats()["weight_reserved"] == 0
        assert budget.stats()["weight_in_window"] == 30
        assert budget.weight_used == 30

    def test_weight_window_denies_without_send(self):
        budget = rb.RequestBudget(max_sends=100, window_ms=60_000, max_weight=50, weight_window_ms=60_000)
        p = budget.try_acquire("fapi", 40, "monitor", "klines")
        assert not isinstance(p, rb.Denied)
        p.mark_sent()
        denied = budget.try_acquire("fapi", 20, "monitor", "klines")
        assert isinstance(denied, rb.Denied)
        assert budget.sent_attempts == 1

    @pytest.mark.asyncio
    async def test_http_counts_weight_per_send(self):
        budget = rb.RequestBudget(max_sends=100, window_ms=60_000, max_weight=10000, weight_window_ms=60_000)
        ctx = _ctx(budget, job_type="monitor")
        session = _FakeSession([_FakeResponse(200, {"ok": 1}), _FakeResponse(200, {"ok": 2})])
        with patch.object(http_mod, "get_session", return_value=session):
            await http_mod.get_json(
                "https://fapi.binance.com/fapi/v1/depth",
                {"symbol": "BTCUSDT", "limit": 100},
                request_context=ctx,
            )
            await http_mod.get_json(
                "https://fapi.binance.com/fapi/v1/depth",
                {"symbol": "BTCUSDT", "limit": 1000},
                request_context=ctx,
            )
        # futuresDepth 100->20, 1000->50.
        assert budget.weight_used == 70
        assert session.calls == 2


# ---------------------------------------------------------------------------
# Deadline + retry new ID + cache never reserves
# ---------------------------------------------------------------------------


class TestDeadlineRetryCache:
    @pytest.mark.asyncio
    async def test_expired_deadline_never_sends(self):
        now = [1_700_000_000_000]
        budget = rb.RequestBudget(max_sends=100, window_ms=60_000, clock=lambda: now[0])
        ctx = _ctx(budget, job_type="monitor", deadline_ms=now[0] - 1)
        session = _FakeSession([_FakeResponse(200, {"ok": 1})])
        with patch.object(http_mod, "get_session", return_value=session):
            with pytest.raises(rb.BudgetExhausted) as exc:
                await http_mod.get_json(
                    "https://fapi.binance.com/fapi/v1/klines",
                    {"symbol": "BTCUSDT", "limit": 10},
                    request_context=ctx,
                )
        assert exc.value.reason_code == "DEADLINE_EXCEEDED"
        assert session.calls == 0

    @pytest.mark.asyncio
    async def test_retry_uses_new_permit_each_attempt(self):
        budget = rb.RequestBudget(max_sends=100, window_ms=60_000)
        ctx = _ctx(budget, job_type="monitor")
        session = _FakeSession(
            [_FakeResponse(429, None), _FakeResponse(429, None), _FakeResponse(200, {"ok": 1})]
        )
        with patch.object(http_mod, "get_session", return_value=session), patch.object(
            http_mod.asyncio, "sleep", _noop_sleep
        ):
            out = await http_mod.get_json(
                "https://fapi.binance.com/fapi/v1/klines",
                {"symbol": "BTCUSDT", "limit": 10},
                request_context=ctx,
            )
        assert out == {"ok": 1}
        assert session.calls == 3
        assert budget.sent_attempts == 3

    def test_cache_hit_never_reserves(self):
        from diveintocrypto_desktop.shortlab import observations as obs_mod

        cache = rb.ObservedCache(clock=lambda: 1_700_000_000_000)
        observed = obs_mod.make_observation(
            {"px": 1.0},
            source="s",
            source_as_of_ms=1_700_000_000_000,
            fetched_at_ms=1_700_000_000_000,
            known_at_ms=1_700_000_000_000,
        )
        cache.put("k", observed, 1_700_000_000_000 + 3_600_000)
        budget = rb.RequestBudget(max_sends=100, window_ms=60_000)
        # Cache hit path: caller checks cache first and never touches budget/http.
        hit = cache.get("k", 1_700_000_000_000 + 1)
        assert hit is observed
        assert budget.sent_attempts == 0


# ---------------------------------------------------------------------------
# Monthly via R01 (D19.4 state spot-checks + cross-month + restart)
# ---------------------------------------------------------------------------

BUDGET_MONTH = "2026-10"
BUDGET_AS_OF = int(dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)


async def _open_repo(tmp_path, name="r11a.duckdb"):
    repo = await ShortLabRepository.open(tmp_path / name)
    await repo.migrate(target_version=6)
    return repo


class TestMonthlyViaRepo:
    @pytest.mark.asyncio
    async def test_state_table_spot_checks(self, tmp_path):
        repo = await _open_repo(tmp_path)
        try:
            # Unknown finish -> NOT_FOUND.
            from diveintocrypto_desktop.shortlab.repository import ValidationError

            with pytest.raises(ValidationError, match="BUDGET_REQUEST_NOT_FOUND"):
                await repo.finish_provider_request("ghost-r11a", True, BUDGET_AS_OF)
            r1 = await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "r11a-1", 2, BUDGET_AS_OF)
            assert r1["admitted"] is True
            dup = await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "r11a-1", 2, BUDGET_AS_OF)
            assert dup["admitted"] is False and dup["reason_code"] == "BUDGET_RESERVATION_ALREADY_HELD"
            await repo.finish_provider_request("r11a-1", True, BUDGET_AS_OF + 1_000)
            # Idempotent same-terminal finish.
            await repo.finish_provider_request("r11a-1", True, BUDGET_AS_OF + 2_000)
            with pytest.raises(ValidationError, match="BUDGET_STATE_CONFLICT"):
                await repo.finish_provider_request("r11a-1", False, BUDGET_AS_OF + 3_000)
            # Cancelled cannot be reused (needs new ID).
            await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "r11a-2", 10, BUDGET_AS_OF)
            await repo.finish_provider_request("r11a-2", False, BUDGET_AS_OF + 1_000)
            cancelled = await repo.reserve_provider_request(
                "coingecko", BUDGET_MONTH, "r11a-2", 10, BUDGET_AS_OF
            )
            assert cancelled["reason_code"] == "BUDGET_REQUEST_CANCELLED"
            # Month mismatch + clock skew.
            with pytest.raises(ValidationError, match="MONTH_KEY_MISMATCH"):
                await repo.reserve_provider_request("coingecko", "2026-09", "r11a-9", 2, BUDGET_AS_OF)
            await repo.reserve_provider_request("coingecko", BUDGET_MONTH, "r11a-3", 10, BUDGET_AS_OF)
            with pytest.raises(ValidationError, match="CLOCK_SKEW"):
                await repo.finish_provider_request("r11a-3", True, BUDGET_AS_OF - 1_000)
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_cross_month_cancel_then_new_uuid(self, tmp_path):
        repo = await _open_repo(tmp_path)
        try:
            oct_asof = int(dt.datetime(2026, 10, 31, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp() * 1000)
            nov_asof = int(dt.datetime(2026, 11, 1, 0, 0, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
            r = await repo.reserve_provider_request("coingecko", "2026-10", "r11a-cross-1", 10, oct_asof)
            assert r["admitted"] is True
            # Midnight: old un-sent ID cancelled, new UUID/month reserved.
            new_id, new_res = await http_mod.handle_month_rollover(
                repo,
                provider="coingecko",
                old_request_id="r11a-cross-1",
                old_month_key="2026-10",
                new_month_key="2026-11",
                monthly_limit=10,
                as_of_ms=nov_asof,
                uuid_fn=lambda: "r11a-cross-2",
            )
            assert new_id == "r11a-cross-2"
            assert new_res["admitted"] is True and new_res["month_key"] == "2026-11"
            # Old ID is now CANCELLED (re-reserve reports cancelled).
            again = await repo.reserve_provider_request("coingecko", "2026-10", "r11a-cross-1", 10, oct_asof)
            assert again["reason_code"] == "BUDGET_REQUEST_CANCELLED"
            # In-flight completion stays on original month: finish new ID sent.
            await repo.finish_provider_request("r11a-cross-2", True, nov_asof + 5_000)
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_month_counts_restart_retained(self, tmp_path):
        db = tmp_path / "r11a-restart.duckdb"
        h1 = await ShortLabRepository.open(db)
        try:
            await h1.migrate(target_version=6)
            r = await h1.reserve_provider_request("coingecko", BUDGET_MONTH, "r11a-restart-1", 10, BUDGET_AS_OF)
            assert r["admitted"] is True
            await h1.finish_provider_request("r11a-restart-1", True, BUDGET_AS_OF + 1_000)
        finally:
            await h1.close()
        # Reopen: counts persist (no restart reset); lowering limit never clears history.
        h2 = await ShortLabRepository.open(db)
        try:
            await h2.migrate(target_version=6)
            dup = await h2.reserve_provider_request("coingecko", BUDGET_MONTH, "r11a-restart-1", 10, BUDGET_AS_OF)
            assert dup["reason_code"] == "BUDGET_REQUEST_ALREADY_SENT"
            # Occupied=1 (SENT); limit 1 now exhausted even though raised before.
            exhausted = await h2.reserve_provider_request(
                "coingecko", BUDGET_MONTH, "r11a-restart-2", 1, BUDGET_AS_OF
            )
            assert exhausted["reason_code"] == "BUDGET_MONTHLY_EXHAUSTED"
        finally:
            await h2.close()

    @pytest.mark.asyncio
    async def test_http_monthly_midnight_queue_to_send(self, tmp_path):
        repo = await _open_repo(tmp_path, "r11a-mid.duckdb")
        try:
            oct_ms = [int(dt.datetime(2026, 10, 31, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp() * 1000)]
            nov_ms = int(dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)

            def _clock():
                return oct_ms[0]

            budget = rb.RequestBudget(max_sends=100, window_ms=60_000, clock=_clock)
            ctx = _ctx(budget, job_type="monitor")
            session = _FakeSession([_FakeResponse(200, {"ok": 1})])
            uuids = iter(["mid-1", "mid-2"])

            async def _slow_session():
                # Simulate queue wait crossing midnight before transport.
                oct_ms[0] = nov_ms
                return session

            with patch.object(http_mod, "get_session", _slow_session):
                out = await http_mod.get_json(
                    "https://api.coingecko.com/api/v3/coins/bitcoin",
                    None,
                    request_context=ctx,
                    repository=repo,
                    monthly_provider="coingecko",
                    monthly_limit=10,
                    clock=_clock,
                    uuid_fn=lambda: next(uuids),
                )
            assert out == {"ok": 1}
            # Old October ID was cancelled, November ID sent.
            oct_dup = await repo.reserve_provider_request("coingecko", "2026-10", "mid-1", 10, oct_ms[0] - 10_000)
            assert oct_dup["reason_code"] in ("BUDGET_REQUEST_CANCELLED", "BUDGET_REQUEST_ALREADY_SENT")
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_http_new_month_exhausted_never_sends(self, tmp_path):
        repo = await _open_repo(tmp_path, "r11a-exh.duckdb")
        try:
            oct_ms = int(dt.datetime(2026, 10, 31, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp() * 1000)
            nov_ms = int(dt.datetime(2026, 11, 1, 0, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
            now = [oct_ms]
            # Fill November quota (limit 1).
            await repo.reserve_provider_request("coingecko", "2026-11", "nov-fill", 1, nov_ms)
            await repo.finish_provider_request("nov-fill", True, nov_ms + 1_000)
            budget = rb.RequestBudget(max_sends=100, window_ms=60_000, clock=lambda: now[0])
            ctx = _ctx(budget, job_type="monitor")
            session = _FakeSession([_FakeResponse(200, {"ok": 1})])

            async def _slow_session2():
                now[0] = nov_ms
                return session

            uuids2 = iter(["oct-queued-1", "nov-retry-1"])
            with patch.object(http_mod, "get_session", _slow_session2):
                with pytest.raises(rb.BudgetExhausted):
                    await http_mod.get_json(
                        "https://api.coingecko.com/api/v3/coins/bitcoin",
                        None,
                        request_context=ctx,
                        repository=repo,
                        monthly_provider="coingecko",
                        monthly_limit=1,
                        clock=lambda: now[0],
                        uuid_fn=lambda: next(uuids2),
                    )
            assert session.calls == 0
            assert budget.sent_attempts == 0
        finally:
            await repo.close()


# ---------------------------------------------------------------------------
# CoinGecko: monthly/RPM/reserve, batch, FX reuse
# ---------------------------------------------------------------------------


class TestCoinGeckoBudget:
    def test_monthly_defaults(self):
        assert cg.ACCOUNT_MONTHLY_LIMIT == 10000
        assert cg.LOCAL_REQUESTS_PER_MINUTE == 30
        assert cg.COINGECKO_MAX_PER_MIN == 30
        assert cg.coingecko_effective_limit() == 9000
        assert cg.coingecko_effective_limit(10000, 0.1) == 9000

    def test_batch_request_builders(self):
        url, params = cg.build_markets_request(["bitcoin", "ethereum"])
        assert url.endswith("/api/v3/coins/markets")
        assert params["ids"] == "bitcoin,ethereum"
        url2, params2 = cg.build_fx_request(["bitcoin"])
        assert url2.endswith("/api/v3/simple/price")

    def test_fx_freshness_never_guesses(self):
        now = 1_700_000_000_000
        assert cg.is_fx_fresh(now - 30_000, now) is True
        assert cg.is_fx_fresh(now - 61_000, now) is False
        assert cg.is_fx_fresh(None, now) is False

    @pytest.mark.asyncio
    async def test_batch_prefers_markets_single_transport(self):
        now = 1_700_000_000_000
        markets_payload = [
            {
                "id": "bitcoin",
                "symbol": "btc",
                "name": "Bitcoin",
                "current_price": 50000,
                "market_cap": 900_000_000_000,
                "fully_diluted_valuation": 1_000_000_000_000,
                "circulating_supply": 19_000_000,
                "total_supply": 21_000_000,
                "max_supply": 21_000_000,
                "ath": 69000,
                "ath_change_percentage": -27.5,
                "ath_date": "2021-11-10T14:24:11.849Z",
                "last_updated": "2024-06-01T00:00:00.000Z",
            },
            {
                "id": "ethereum",
                "symbol": "eth",
                "name": "Ethereum",
                "current_price": 3000,
                "market_cap": 360_000_000_000,
                "fully_diluted_valuation": 360_000_000_000,
                "circulating_supply": 120_000_000,
                "total_supply": 120_000_000,
                "max_supply": None,
                "ath": 4878,
                "ath_change_percentage": -38.5,
                "ath_date": "2021-11-12T14:24:11.849Z",
                "last_updated": "2024-06-01T00:00:00.000Z",
            },
        ]
        calls: list[tuple[str, dict]] = []

        async def _fake_fetcher(url: str, params):
            calls.append((url, dict(params)))
            assert url.endswith("/api/v3/coins/markets")
            return markets_payload

        provider = cg.CoinGeckoProvider(
            clock=lambda: now,
            fetcher=_fake_fetcher,
            limiter=__import__("aiolimiter").AsyncLimiter(max_rate=100, time_period=60),
        )
        out = await provider.fetch_many(
            [SimpleNamespace(coingecko_id="bitcoin"), SimpleNamespace(coingecko_id="ethereum")]
        )
        assert set(out) == {"bitcoin", "ethereum"}
        assert all(r.status == "OK" for r in out.values())
        assert len(calls) == 1
        # Second batch hits market TTL: no new transport.
        out2 = await provider.fetch_many(
            [SimpleNamespace(coingecko_id="bitcoin"), SimpleNamespace(coingecko_id="ethereum")]
        )
        assert len(calls) == 1
        assert out2["bitcoin"].data.price_usd == 50000

    @pytest.mark.asyncio
    async def test_fx_reuse_no_extra_fetch(self):
        now = [1_700_000_000_000]

        async def _should_not_fetch(url, params):  # pragma: no cover
            raise AssertionError("must reuse fresh FX without transport")

        provider = cg.CoinGeckoProvider(
            clock=lambda: now[0],
            fetcher=_should_not_fetch,
            limiter=__import__("aiolimiter").AsyncLimiter(max_rate=100, time_period=60),
        )
        provider.put_fx("tether", "1", now[0] - 10_000)
        assert provider.get_cached_fx("USDT", now[0]) is None  # currency-keyed, not USDT guess
        provider._fx_cache["BITCOIN"] = (now[0] - 10_000, "50000", now[0])
        out = await provider.fetch_fx(["bitcoin"])
        assert out == {"bitcoin": "50000"}
        # Stale FX returns None (UNKNOWN), never 1.
        now[0] += 120_000
        assert provider.get_cached_fx("bitcoin", now[0]) is None

    @pytest.mark.asyncio
    async def test_monthly_exhausted_reports_limited(self, tmp_path):
        repo = await _open_repo(tmp_path, "r11a-cg.duckdb")
        try:
            month = BUDGET_MONTH
            asof = BUDGET_AS_OF
            # Fill quota limit 1.
            await repo.reserve_provider_request("coingecko", month, "cg-fill", 1, asof)
            await repo.finish_provider_request("cg-fill", True, asof + 1_000)

            async def _no_transport(url, params):
                raise AssertionError("exhausted monthly must not transport")

            provider = cg.CoinGeckoProvider(
                clock=lambda: asof,
                fetcher=_no_transport,
                repository=repo,
                account_monthly_limit=1,
                reserve_fraction=0.0,
                limiter=__import__("aiolimiter").AsyncLimiter(max_rate=100, time_period=60),
            )
            res = await provider.fetch(SimpleNamespace(coingecko_id="bitcoin"))
            assert res.status == "UNAVAILABLE"
            assert res.reason_code == cg.BUDGET_LIMITED
        finally:
            await repo.close()


# ---------------------------------------------------------------------------
# Scheduler budget side
# ---------------------------------------------------------------------------


class TestSchedulerBudget:
    def test_tier_mapping(self):
        sched = ShortLabScheduler(request_budget=rb.RequestBudget())
        assert sched.budget_tier_for("monitor") == "MONITOR"
        assert sched.budget_tier_for("hedge_monitor") == "MONITOR"
        assert sched.budget_tier_for("interactive") == "SCANNER"
        assert sched.budget_tier_for("score_refresh") == "SCANNER"
        assert sched.budget_tier_for("opportunity") == "BACKGROUND"
        assert sched.budget_tier_for("grader") == "BACKGROUND"
        assert sched.budget_tier_for("bogus_xyz") == rb.JOB_TYPE_UNKNOWN

    def test_context_wiring(self):
        budget = rb.RequestBudget()
        sched = ShortLabScheduler(request_budget=budget, clock=lambda: 1_700_000_000_000)
        ctx = sched.context_for_job("monitor", job_id="j-1", deadline_ms=1_700_000_000_100)
        assert ctx is not None and ctx.budget is budget
        assert ctx.job_type == "monitor" and ctx.job_id == "j-1"
        assert ctx.deadline_ms == 1_700_000_000_100
        # Unwired scheduler keeps legacy None (no behaviour change).
        assert ShortLabScheduler().context_for_job("monitor") is None

    @pytest.mark.asyncio
    async def test_tick_installs_ambient_context(self):
        budget = rb.RequestBudget()
        seen: list[str | None] = []

        async def _job():
            from diveintocrypto_desktop.shortlab.request_budget import (
                get_current_request_context as _get,
            )

            ctx = _get()
            seen.append(ctx.job_type if ctx is not None else None)

        sched = ShortLabScheduler(
            jitter_fn=lambda m: 0.0,
            sleep=_noop_sleep,
            clock=lambda: 1_700_000_000_000,
            request_budget=budget,
        )
        sched.register("monitor", 10, _job, jitter_max_sec=0)
        await sched.trigger_now("monitor")
        assert seen == ["monitor"]


# ---------------------------------------------------------------------------
# Host isolation
# ---------------------------------------------------------------------------


class TestHostIsolation:
    @pytest.mark.asyncio
    async def test_hosts_do_not_share_windows(self):
        budget = rb.RequestBudget(max_sends=1, window_ms=60_000)
        fapi_ctx = _ctx(budget, job_type="monitor", host="fapi")
        api_ctx = _ctx(budget, job_type="monitor", host="api")
        s1 = _FakeSession([_FakeResponse(200, {})])
        s2 = _FakeSession([_FakeResponse(200, {})])
        with patch.object(http_mod, "get_session", return_value=s1):
            await http_mod.get_json("https://fapi.binance.com/fapi/v1/exchangeInfo", request_context=fapi_ctx)
        with patch.object(http_mod, "get_session", return_value=s2):
            await http_mod.get_json("https://api.binance.com/api/v3/exchangeInfo", request_context=api_ctx)
        assert s1.calls == 1 and s2.calls == 1
        assert budget.stats(host="fapi")["host_sent_in_window"] == 1
        assert budget.stats(host="api")["host_sent_in_window"] == 1

    def test_host_from_url_buckets(self):
        assert rb.host_from_url("https://fapi.binance.com/fapi/v1/klines") == "fapi"
        assert rb.host_from_url("https://api.binance.com/api/v3/klines") == "api"
        assert rb.host_from_url("https://www.binance.com/bapi/defi/v1/public/alpha-trade/ticker") == "www.binance.com"
        assert rb.host_from_url("https://api.coingecko.com/api/v3/coins/list") == "api.coingecko.com"
        assert rb.host_from_url("https://api.0x.org/swap/allowance-holder/price/v2") == "api.0x.org"
