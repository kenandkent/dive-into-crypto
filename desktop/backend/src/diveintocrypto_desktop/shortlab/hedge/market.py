"""Production public market wiring; no account access or order submission.

R11b (D11/D18/D19): implements MarketPort three collectors
(collect_futures/collect_spot/collect_funding) with true BOOK two-sided
depth (SELL entry / BUY exit via compute_contract_vwap), Mark never
posing as execution price, budget refusal as BudgetExhausted (DEFERRED
upstream), and immutable RequestContext pass-through for R14 capture.
Legacy mark/quote/funding/rules/collect entry points are preserved.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass, replace
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.request_budget import (
    BudgetExhausted,
    make_request_context,
    Denied,
)
from diveintocrypto_desktop.shortlab.hedge.funding_score import compute_funding_metrics
from diveintocrypto_desktop.shortlab.hedge.venues.binance_spot import BinanceSpotVenue
from diveintocrypto_desktop.shortlab.hedge.venues.binance_alpha import BinanceAlphaVenue
from diveintocrypto_desktop.shortlab.hedge.venues.ethereum_0x import Ethereum0xPriceVenue
from diveintocrypto_desktop.shortlab.providers.coingecko import CoinGeckoProvider

#: R11b tick bounds (D11): deep quotes cap, concurrency, total deadline.
MARKET_DEEP_LIMIT = 10
MARKET_CONCURRENCY = 4
MARKET_DEADLINE_MS = 8000
QUOTE_TTL_MS = 20_000


def _is_budget_denial(exc: BaseException) -> bool:
    """Whether ``exc`` is a budget refusal (DEFERRED, never fabricated)."""
    name = type(exc).__name__
    if "BudgetExhausted" in name or "Unbudgeted" in name:
        return True
    code = str(getattr(exc, "reason_code", "") or "")
    if "BUDGET" in code or "UNBUDGETED" in code or "REQUEST_BUDGET_EXHAUSTED" in code:
        return True
    text = str(exc)[:300]
    if "BUDGET_EXHAUSTED" in text or "UNBUDGETED_ENDPOINT" in text or "JOB_TYPE_UNKNOWN" in text:
        return True
    return False


def _normalize_contract_qty(contract_qty: Any) -> str:
    from diveintocrypto_desktop.shortlab.repair_contracts import normalize_decimal_str

    text = normalize_decimal_str(contract_qty) if isinstance(contract_qty, Decimal) else normalize_decimal_str(str(contract_qty))
    if Decimal(text) <= 0:
        raise ValueError(f"contract_qty must be > 0, got {contract_qty!r}")
    return text


def _mapping(value):
    return asdict(value) if is_dataclass(value) else dict(value)


def _funding_time_of(ev: Any) -> int | None:
    """CR02 (D03/D05): compat settled time for FundingEventRecord + mappings.

    Accepts ``FundingEventRecord`` dataclass (``funding_time_ms``), legacy
    dicts (``funding_time_ms``/``fundingTime``/``t``/``funding_time``) and
    ``Observed`` envelopes (``value``/``meta``). Returns ``None`` when the
    time is missing or unparseable (caller skips, never ``e.get`` crash).
    """
    if isinstance(ev, Mapping):
        for key in ("funding_time_ms", "fundingTime", "t", "funding_time",
                    "event_time_ms", "time_ms"):
            if key in ev and ev[key] is not None:
                try:
                    return int(ev[key])  # type: ignore[index]
                except (TypeError, ValueError):
                    return None
        try:
            nested = ev.get("value")
        except Exception:
            nested = None
        if isinstance(nested, Mapping):
            return _funding_time_of(nested)
        return None
    for attr in ("funding_time_ms", "fundingTime", "t"):
        try:
            if hasattr(ev, attr):
                val = getattr(ev, attr)
                return None if val is None else int(val)
        except (TypeError, ValueError):
            return None
    try:
        nested_obj = getattr(ev, "value", None)
        if nested_obj is not None and nested_obj is not ev and isinstance(nested_obj, Mapping):
            return _funding_time_of(nested_obj)
    except Exception:
        pass
    return None


def _funding_receipt_known_at(row: Any) -> int | None:
    """CR02 (D03): compat ``known_at`` for persisted funding observation rows.

    Handles ``list_funding_observations`` mappings (``known_at_ms`` top
    level, possibly ``meta_json``) and ``FundingObservationRecord``
    dataclasses. ``None`` when no receipt is carried.
    """
    if isinstance(row, Mapping):
        for key in ("known_at_ms", "knownAt", "known_at"):
            try:
                if key in row and row[key] is not None:
                    return int(row[key])  # type: ignore[index]
            except (TypeError, ValueError):
                return None
        try:
            meta = row.get("meta_json", row.get("meta"))
        except Exception:
            meta = None
        if isinstance(meta, Mapping):
            for key in ("known_at_ms", "knownAt", "known_at", "fetched_at_ms"):
                try:
                    if key in meta and meta[key] is not None:
                        return int(meta[key])
                except (TypeError, ValueError):
                    return None
        return None
    for attr in ("known_at_ms", "known_at", "fetched_at_ms"):
        try:
            if hasattr(row, attr):
                val = getattr(row, attr)
                return None if val is None else int(val)
        except (TypeError, ValueError):
            return None
    try:
        meta_obj = getattr(row, "meta", None)
    except Exception:
        meta_obj = None
    if meta_obj is not None:
        if isinstance(meta_obj, Mapping):
            for key in ("known_at_ms", "known_at", "fetched_at_ms"):
                try:
                    if key in meta_obj and meta_obj[key] is not None:
                        return int(meta_obj[key])
                except (TypeError, ValueError):
                    return None
        else:
            for attr in ("known_at_ms", "known_at", "fetched_at_ms"):
                try:
                    if hasattr(meta_obj, attr):
                        val = getattr(meta_obj, attr)
                        return None if val is None else int(val)
                except (TypeError, ValueError):
                    return None
    return None


class ProductionHedgeMarket:
    def __init__(self, service, config, repo, budget, clock, env=None):
        self.service, self.config, self.repo = service, config, repo
        self.budget, self.clock = budget, clock
        self.spot = BinanceSpotVenue(config, now_ms_fn=clock)
        self.alpha = BinanceAlphaVenue(config, now_ms_fn=clock)
        self.chain = Ethereum0xPriceVenue(config, env=env, now_ms_fn=clock, price_fn=self._chain_transport)
        self.fx_provider = CoinGeckoProvider(clock=clock, market_ttl_sec=60)
        self.fx_provider._fetcher = self._fx_transport
        self._fx_cache = {}
        self._fx_provenance = {}
        self._marks = {}
        self._rules_cache = {}
        self._funding_cache = {}
        self._funding_backfill = {}
        self._exchange_cache = None
        self._exchange_lock = asyncio.Lock()
        self._lock = asyncio.Lock()

    def _unavailable(self, reason):
        from diveintocrypto_desktop.shortlab.service import HedgeUnavailable
        return HedgeUnavailable(reason)

    def _ctx(self, host='fapi'):
        return make_request_context(self.budget, job_type='monitor', host=host)

    def _child_ctx(self, request_context: Any | None, host: str, family: str | None):
        """Derive an immutable child RequestContext (R11b: real task names).

        Preserves the caller's budget/job_type/trace/identity (never
        re-labels everything ``monitor``); only host/family are scoped for
        this send. ``None`` input keeps the legacy unbounded/monitor path
        for pre-repair callers and unit tests.
        """
        if request_context is None:
            return make_request_context(self.budget, job_type='monitor', host=host,
                                        endpoint_family=family)
        budget = getattr(request_context, 'budget', self.budget)
        # When the caller carries no budget the legacy path stays unbounded.
        if budget is None and self.budget is None:
            return make_request_context(None, job_type=getattr(request_context, 'job_type', 'monitor') or 'monitor',
                                        host=host, endpoint_family=family,
                                        trace_id=getattr(request_context, 'trace_id', None),
                                        identity_snapshot_id=getattr(request_context, 'identity_snapshot_id', None))
        job_type = getattr(request_context, 'job_type', 'monitor') or 'monitor'
        trace_id = getattr(request_context, 'trace_id', None)
        identity_snapshot_id = getattr(request_context, 'identity_snapshot_id', None)
        extra: dict[str, Any] = {}
        for key in ('job_id', 'deadline_ms'):
            try:
                if hasattr(request_context, key):
                    extra[key] = getattr(request_context, key)
            except Exception:
                pass
        try:
            return make_request_context(budget if budget is not None else self.budget,
                                        job_type=job_type, host=host,
                                        endpoint_family=family, trace_id=trace_id,
                                        identity_snapshot_id=identity_snapshot_id, **extra)
        except TypeError:
            # R00 RequestContext has no job_id/deadline_ms yet; drop extras.
            return make_request_context(budget if budget is not None else self.budget,
                                        job_type=job_type, host=host,
                                        endpoint_family=family, trace_id=trace_id,
                                        identity_snapshot_id=identity_snapshot_id)

    def _permit(self, host, family, request_context: Any | None = None):
        if self.budget is None:
            return None
        # CR14 (D11/D19.3): record the real caller job_type, never force
        # everything to ``monitor``. Non-Binance transports have a public
        # request unit (one send), not a fabricated Binance exchange weight.
        # Every retry acquires separately. Legacy callers without a context
        # keep the monitor default for backward compatibility.
        jt = 'monitor'
        try:
            if request_context is not None:
                cand = getattr(request_context, 'job_type', None)
                if isinstance(cand, str) and cand.strip():
                    jt = cand
        except Exception:
            jt = 'monitor'
        fam = family
        try:
            if (fam is None or fam in ('coingecko', '0x-price')) and request_context is not None:
                cand_fam = getattr(request_context, 'endpoint_family', None)
                if isinstance(cand_fam, str) and cand_fam.strip():
                    fam = cand_fam
        except Exception:
            pass
        # Normalize legacy labels to D19.3 real families for honest accounting.
        if fam == 'coingecko':
            fam = 'cgFx'
        elif fam == '0x-price':
            fam = 'onchainPrice'
        permit = self.budget.try_acquire(host, 1, jt, fam if fam is not None else family)
        if isinstance(permit, Denied):
            raise self._unavailable(permit.reason_code)
        return permit

    async def _fx_transport(self, url, params):
        # CR14 (D11): single billing lives in CoinGeckoProvider
        # (_transport_with_budget with the real caller RequestContext and
        # real family from URL). This fetcher is the raw transport only and
        # must not acquire a second hardcoded-monitor permit (no double
        # charge, no monitor mislabel for background FX).
        return await self.fx_provider._default_fetcher(url, params)

    async def _chain_transport(self, url, params, headers, request_context):
        import aiohttp
        from diveintocrypto_desktop.data.http import get_session
        # CR14 (D11/D19.3): 0x venue has no own budget layer, so this is the
        # single billing point. Record the real caller job_type with the real
        # D19.3 family (never hardcoded monitor / legacy label).
        permit = self._permit('api.0x.org', 'onchainPrice', request_context)
        marked = False
        try:
            session = await get_session()
            if permit is not None:
                permit.mark_sent()
                marked = True
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                return await response.json()
        except Exception:
            # Pre-send failure before mark: release the reservation so the
            # window is not leaked; sent attempts are never refunded.
            if permit is not None and not marked:
                try:
                    permit.release_unsent()
                except Exception:
                    pass
            raise

    async def _identity(self, symbol):
        value = await self.service._hedge_identity_for(symbol)
        return SimpleNamespace(**value) if isinstance(value, Mapping) else value

    async def _fx(self, currency, request_context: Any | None = None):
        if currency == 'USD':
            return '1'
        coin = {'USDT': 'tether', 'USDC': 'usd-coin', 'FDUSD': 'first-digital-usd'}.get(currency)
        if coin is None:
            raise self._unavailable('QUOTE_FX_UNAVAILABLE')
        async with self._lock:
            cached = self._fx_cache.get(currency)
            if cached and self.clock() - cached[0] < 60_000:
                return cached[1]
            # CR14 (D11): forward the caller context so FX bills the real
            # job_type/family via the provider (never forced monitor).
            try:
                result = await self.fx_provider.fetch(SimpleNamespace(coingecko_id=coin),
                                                      request_context=request_context)
            except TypeError:
                # Pre-repair provider without request_context kwarg.
                result = await self.fx_provider.fetch(SimpleNamespace(coingecko_id=coin))
            price = getattr(result.data, 'price_usd', None)
            source_ms = getattr(result, 'as_of_ms', None)
            if not isinstance(source_ms, int) or not 0 <= self.clock() - source_ms <= 60_000:
                raise self._unavailable('QUOTE_FX_STALE_OR_TIME_UNKNOWN')
            if result.status != 'OK' or result.stale or price is None or Decimal(str(price)) <= 0:
                raise self._unavailable('QUOTE_FX_UNAVAILABLE')
            self._fx_cache[currency] = (source_ms, str(price))
            self._fx_provenance[currency] = {'source_as_of_ms': source_ms, 'known_at_ms': self.clock(), 'currency': currency}
            return str(price)

    async def mark(self, symbol, request_context: Any | None = None):
        cached = self._marks.get(symbol)
        if cached and self.clock() - cached['fetched_at_ms'] < 5_000:
            return dict(cached)
        from diveintocrypto_desktop.data.funding import premium_index
        identity = await self._identity(symbol)
        ctx = self._child_ctx(request_context, 'fapi', 'premiumIndex') if request_context is not None else self._ctx()
        try:
            raw = await premium_index(symbol, request_context=ctx)
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            raise
        ts = raw.get('time_ms')
        if not isinstance(ts, int) or ts <= 0:
            raise self._unavailable('MARK_SOURCE_TIME_UNKNOWN')
        if self.clock() - ts > 60_000 or ts > self.clock() + 5_000:
            raise self._unavailable('MARK_STALE')
        try:
            multiplier = Decimal(str(getattr(identity, 'contract_multiplier', None)))
            native = Decimal(str(raw['mark_price']))
        except (InvalidOperation, ValueError, KeyError):
            raise self._unavailable('MARK_OR_MULTIPLIER_INVALID') from None
        if not multiplier.is_finite() or not native.is_finite() or multiplier <= 0 or native <= 0:
            raise self._unavailable('MARK_OR_MULTIPLIER_INVALID')
        # CR14: FX leg carries the same caller context (real job_type).
        fx = await self._fx('USDT', request_context)
        mark = {**raw, 'native_price': str(native), 'mark_price': str(native),
                'price': str(native / multiplier),
                'canonical_price_usd': str(native / multiplier * Decimal(fx)),
                'quote_currency': 'USDT', 'quote_to_usd': fx,
                'as_of_ms': ts, 'source_timestamp_ms': ts,
                'fetched_at_ms': self.clock(), 'expires_at_ms': ts + 60_000}
        self._marks[symbol] = mark
        return dict(mark)

    async def quote(self, symbol, canonical_qty, venue=None, request_context: Any | None = None):
        """Legacy spot quote (CR14: accepts caller context, else monitor default).

        ``request_context`` is additive and trailing for backward
        compatibility. When provided, the real job_type/family is preserved
        via child contexts (never forced monitor); ``None`` keeps the legacy
        unbounded/monitor path for pre-repair callers and unit tests.
        """
        identity = await self._identity(symbol)
        venue = venue or 'BINANCE_SPOT'
        if venue == 'ONCHAIN_DEX':
            # CR14: forward caller context (real job_type) to both legs.
            buy_ctx = self._child_ctx(request_context, '0x', 'onchainPrice') if request_context is not None else self._ctx('0x')
            sell_ctx = self._child_ctx(request_context, '0x', 'onchainPrice') if request_context is not None else self._ctx('0x')
            buy = await self.chain.quote_buy(identity, canonical_qty, request_context=buy_ctx)
            sell = await self.chain.quote_sell(identity, canonical_qty, request_context=sell_ctx)
            if buy.data is None or sell.data is None:
                raise self._unavailable(buy.reason_code or sell.reason_code or 'CHAIN_QUOTE_UNAVAILABLE')
            data = _mapping(buy.data)
            data['capabilities'] = {**dict(data.get('capabilities') or {}), 'fx': dict(self._fx_provenance.get(buy.data.quote_currency, {}))}
            data.update(sell_vwap=sell.data.sell_vwap, sell_executable_qty=sell.data.sell_executable_qty,
                        mid_price=None, exit_feasible=False, exit_feasibility='UNKNOWN',
                        entry_feasible=False, quote_to_usd=await self._fx(buy.data.quote_currency, request_context))
            data['capabilities']['fx'] = dict(self._fx_provenance.get(buy.data.quote_currency, {}))
            if data.get('buy_vwap') is not None:
                data['reference_notional_usd'] = str(Decimal(data['buy_vwap']) * Decimal(canonical_qty) * Decimal(data['quote_to_usd']))
            return data
        adapter = {'BINANCE_SPOT': self.spot, 'BINANCE_ALPHA': self.alpha}.get(venue)
        if adapter is None:
            raise self._unavailable('VENUE_UNSUPPORTED')
        host = 'spot' if venue == 'BINANCE_SPOT' else 'alpha'
        adapter_ctx = self._child_ctx(request_context, host, None) if request_context is not None else self._ctx(host)
        result = await adapter.quote(identity, canonical_qty, request_context=adapter_ctx)
        if result.status not in ('OK', 'PARTIAL') or result.data is None or result.stale:
            raise self._unavailable(result.reason_code or 'VENUE_QUOTE_UNAVAILABLE')
        data = result.data
        if data.requested_canonical_qty != canonical_qty:
            raise self._unavailable('QUOTE_QUANTITY_MISMATCH')
        # CR14: FX leg carries the same caller context (real job_type).
        fx = await self._fx(data.quote_currency, request_context)
        # Venue execution prices are quote currency values; planner applies FX once.
        scale = Decimal(fx)
        direction_costs = dict(data.direction_costs)
        for key in ('buy_fee_usd', 'sell_fee_usd'):
            if direction_costs.get(key) is not None:
                direction_costs[key] = str(Decimal(str(direction_costs[key])) * scale)
        return replace(data, quote_to_usd=fx,
                       reference_notional_usd=str(Decimal(data.reference_notional_usd) * scale),
                       estimated_fee_usd=(str(Decimal(data.estimated_fee_usd) * scale)
                                          if data.estimated_fee_usd is not None else None),
                       direction_costs=direction_costs,
                       capabilities={**dict(data.capabilities), 'fx': dict(self._fx_provenance.get(data.quote_currency, {}))})

    async def _exchange_info(self, request_context: Any | None = None):
        from diveintocrypto_desktop.data import http
        async with self._exchange_lock:
            if self._exchange_cache and self.clock() - self._exchange_cache[0] < 30 * 60_000:
                return self._exchange_cache[1]
            ctx = self._child_ctx(request_context, 'fapi', 'exchangeInfo') if request_context is not None else self._ctx()
            try:
                raw = await http.get_json(f'{http.FAPI_V1}/exchangeInfo', request_context=ctx)
            except Exception as exc:
                if _is_budget_denial(exc):
                    raise BudgetExhausted(str(exc)[:300]) from exc
                raise
            if self.budget is not None:
                self.budget.configure_host_limits('fapi', raw.get('rateLimits', []))
            self._exchange_cache = (self.clock(), raw)
            return raw

    async def funding(self, symbol, request_context: Any | None = None):
        """Legacy funding (CR14: accepts caller context, else monitor default).

        ``request_context`` is additive and trailing. When provided, Mark and
        exchangeInfo inherit the real job_type via child contexts; ``None``
        keeps the legacy path for pre-repair callers and unit tests.
        """
        now = self.clock()
        cached = self._funding_cache.get(symbol)
        if cached and now - cached[0] < 30_000:
            return cached[1]
        mark = await self.mark(symbol, request_context=request_context)
        try:
            raw = await self._exchange_info(request_context=request_context)
            entry = next((e for e in raw.get('symbols', []) if e.get('symbol') == symbol), {})
        except Exception:
            entry = {}
        onboard = entry.get('onboardDate')
        listing = ({'onboard_at_ms': onboard, 'reliable': True}
                   if isinstance(onboard, int) and 0 < onboard <= now else None)
        events = await self.repo.list_funding_events(symbol, now - 90 * 86_400_000, now)
        metrics = compute_funding_metrics(events, [], now, listing, symbol=symbol,
                                           current_rate=mark.get('last_funding_rate'))
        last_attempt = self._funding_backfill.get(symbol)
        if (metrics.coverage_90d is None or Decimal(metrics.coverage_90d) < 1) and (last_attempt is None or now - last_attempt >= 300_000):
            self._funding_backfill[symbol] = now
            fetch = getattr(self.service, '_fetch_funding', None)
            if fetch is not None:
                fetched = await fetch(symbol, {'onboard_at_ms': onboard if listing else None}, now, now)
                # _fetch_funding repairs and persists gaps; it returns the full
                # retained archive, not just the newly fetched tail.
                archive = fetched.get('events', [])
                if archive:
                    metrics = compute_funding_metrics(archive, [], now, listing, symbol=symbol,
                                                       current_rate=mark.get('last_funding_rate'))
        self._funding_cache[symbol] = (now, metrics)
        return metrics

    async def rules(self, kind, symbol, request_context: Any | None = None):
        from diveintocrypto_desktop.data import spot
        from diveintocrypto_desktop.data.trading_rules import parse_trading_rules
        identity = await self._identity(symbol)
        key = (kind, symbol)
        cached = self._rules_cache.get(key)
        if cached and self.clock() - cached[0] < 30 * 60_000:
            return cached[1]
        if kind == 'futures':
            try:
                raw = await self._exchange_info(request_context=request_context)
            except Exception as exc:
                if _is_budget_denial(exc):
                    raise BudgetExhausted(str(exc)[:300]) from exc
                raise
            entry = next((e for e in raw.get('symbols', []) if e.get('symbol') == symbol), None)
            instrument, venue = symbol, 'BINANCE_SPOT'
        else:
            instrument = getattr(identity, 'binance_spot_symbol', None)
            if not instrument:
                raise self._unavailable('TRADING_RULES_UNVERIFIED')
            spot_ctx = self._child_ctx(request_context, 'spot', None) if request_context is not None else self._ctx('spot')
            try:
                entry = await spot.fetch_spot_exchange_entry(instrument, request_context=spot_ctx)
            except Exception as exc:
                if _is_budget_denial(exc):
                    raise BudgetExhausted(str(exc)[:300]) from exc
                raise
            venue = 'BINANCE_SPOT'
        if entry is None:
            raise self._unavailable('TRADING_RULES_UNVERIFIED')
        rules = parse_trading_rules(entry, venue, instrument, {'known_at_ms': self.clock()})
        self._rules_cache[key] = (self.clock(), rules)
        return rules

    async def collect(self, symbol, qty, venue=None, request_context: Any | None = None):
        """Legacy bundle (CR14: accepts caller context, else monitor default).

        ``request_context`` is additive and trailing. When provided, all three
        legs inherit the real job_type/family (never forced monitor); ``None``
        keeps the legacy path for pre-repair callers and unit tests.
        """
        # CR14: forward the immutable caller context to every leg so mixed
        # loads record the real family/job_type (background never misbilled
        # as monitor). Each leg derives its own child host/family internally.
        values = await asyncio.gather(
            self.mark(symbol, request_context=request_context),
            self.quote(symbol, qty, venue, request_context=request_context),
            self.funding(symbol, request_context=request_context),
            return_exceptions=True)
        result = {'fetched_at_ms': self.clock(), 'collection_errors': {}}
        for key, value in zip(('futures_mark', 'spot_quote', 'funding_metrics'), values):
            if isinstance(value, Exception):
                result['collection_errors'][key] = str(value)
            else:
                result[key] = _mapping(value) if key == 'spot_quote' else value
        return result

    # -- R11b MarketPort (D19.1, frozen 3 collectors) ----------------------
    async def _fetch_book_observed(self, symbol: str, request_context: Any | None):
        """True BOOK Observed with verbatim bids/asks (never merged total)."""
        from diveintocrypto_desktop.data import orderbook as _book

        book_ctx = self._child_ctx(request_context, 'fapi', 'futuresDepth') if request_context is not None else self._ctx()
        try:
            observed = await _book.fetch_book_observed(symbol, request_context=book_ctx)
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            raise
        return observed

    async def collect_futures(
        self, symbol: str, contract_qty: str, request_context: Any
    ) -> Mapping[str, Any]:
        """MarketPort futures collector (D19.1): Mark + ExecutionQuote + Rules.

        - True BOOK two-sided depth via compute_contract_vwap (SELL=bids
          entry, BUY=asks exit); Mark never poses as execution price.
        - Budget refusal raises BudgetExhausted (DEFERRED upstream); other
          data gaps return UNKNOWN parts with reasons, never fabricated 0.
        - Immutable RequestContext pass-through (real job_type preserved).
        """
        from diveintocrypto_desktop.shortlab.hedge.units import compute_contract_vwap
        from diveintocrypto_desktop.shortlab.repair_contracts import FuturesExecutionQuote

        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(f"symbol must be non-empty str, got {symbol!r}")
        qty_str = _normalize_contract_qty(contract_qty)
        now = int(self.clock())

        # Mark (source time preserved; stale/unknown raises honestly).
        try:
            mark = await self.mark(symbol, request_context=request_context)
        except BudgetExhausted:
            raise
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            raise

        # BOOK (verbatim sides; UNBUDGETED/unknown stays UNAVAILABLE).
        book_observed: Any = None
        book_reason: str | None = None
        try:
            book_observed = await self._fetch_book_observed(symbol, request_context)
        except BudgetExhausted:
            raise
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            book_reason = str(getattr(exc, 'reason_code', None) or type(exc).__name__)

        # Rules (futures trading rules; missing stays honest error).
        try:
            rules = await self.rules('futures', symbol, request_context=request_context)
        except BudgetExhausted:
            raise
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            raise

        # VWAP per side (never merged): SELL=bids (short entry), BUY=asks (exit).
        sell_vwap: str | None = None
        buy_vwap: str | None = None
        sell_exec = "0"
        buy_exec = "0"
        book_id = f"{symbol}:book:{mark.get('as_of_ms', now)}"
        if book_observed is not None:
            try:
                value = getattr(book_observed, 'value', book_observed)
                if isinstance(value, Mapping) and 'symbol' in value:
                    pass
                sell_leg = compute_contract_vwap(book_observed, qty_str, 'SELL')
                buy_leg = compute_contract_vwap(book_observed, qty_str, 'BUY')
                sell_vwap = sell_leg.get('vwap_native')
                buy_vwap = buy_leg.get('vwap_native')
                sell_exec = str(sell_leg.get('executable_contract_qty', '0'))
                buy_exec = str(buy_leg.get('executable_contract_qty', '0'))
                meta = getattr(book_observed, 'meta', None)
                source_as_of = getattr(meta, 'source_as_of_ms', None) if meta is not None else None
                book_id = f"{symbol}:book:{source_as_of if isinstance(source_as_of, int) else mark.get('as_of_ms', now)}"
            except Exception:
                # Invalid book shape stays UNKNOWN (no fabricated VWAP).
                sell_vwap, buy_vwap = None, None
                sell_exec, buy_exec = "0", "0"
                if book_reason is None:
                    book_reason = 'BOOK_INVALID'
        elif book_reason is None:
            book_reason = 'BOOK_UNAVAILABLE'

        # FX for quote currency (USDT; USD==1, never guessed).
        # CR14: same caller context so the FX leg bills the real job_type.
        try:
            fx = await self._fx(str(mark.get('quote_currency', 'USDT')), request_context)
        except BudgetExhausted:
            raise
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            # FX unknown stays null (never USDT==1 guess for non-USD).
            fx = None
            if mark.get('quote_currency', 'USDT') != 'USD':
                book_reason = (book_reason + ',QUOTE_FX_UNAVAILABLE') if book_reason else 'QUOTE_FX_UNAVAILABLE'

        as_of = int(mark.get('as_of_ms', now))
        quote = FuturesExecutionQuote(
            quote_id=f"{symbol}:fut:{as_of}:{qty_str}",
            symbol=symbol,
            requested_contract_qty=qty_str,
            buy_vwap_native=buy_vwap,
            sell_vwap_native=sell_vwap,
            buy_executable_qty=buy_exec,
            sell_executable_qty=sell_exec,
            quote_currency=str(mark.get('quote_currency', 'USDT')),
            quote_to_usd=fx,
            as_of_ms=as_of,
            known_at_ms=now,
            expires_at_ms=now + QUOTE_TTL_MS,
            book_observation_id=book_id,
            fees_included=False,
        )
        out: dict[str, Any] = {
            'symbol': symbol,
            'requested_contract_qty': qty_str,
            'futures_mark': dict(mark),
            'futures_quote': quote,
            'futures_rules': rules,
            'book_observation_id': book_id,
            'as_of_ms': as_of,
            'known_at_ms': now,
        }
        if book_observed is not None:
            try:
                out['book'] = {'value': dict(getattr(book_observed, 'value', book_observed)),
                               'meta': asdict(getattr(book_observed, 'meta')) if hasattr(book_observed, 'meta') else {}}
            except Exception:
                out['book'] = {'value': {}, 'meta': {}}
        if book_reason is not None and (sell_vwap is None or buy_vwap is None):
            out['reasons'] = (book_reason,)
            out['coverage'] = '0'
        return out

    async def collect_spot(
        self, identity: Any, venue: str, canonical_qty: str, request_context: Any
    ) -> Any:
        """MarketPort spot collector (D19.1): same-qty dual SpotVenueQuote."""
        from diveintocrypto_desktop.shortlab.repair_contracts import normalize_decimal_str as _norm

        if not isinstance(venue, str) or not venue:
            raise ValueError(f"venue must be non-empty str, got {venue!r}")
        qty_str = _norm(str(canonical_qty))
        if Decimal(qty_str) <= 0:
            raise ValueError(f"canonical_qty must be > 0, got {canonical_qty!r}")
        host = {'BINANCE_SPOT': 'spot', 'BINANCE_ALPHA': 'alpha', 'ONCHAIN_DEX': '0x'}.get(venue, 'spot')
        child = self._child_ctx(request_context, host, None) if request_context is not None else self._ctx(host)
        try:
            if venue == 'ONCHAIN_DEX':
                buy = await self.chain.quote_buy(identity, qty_str, request_context=child)
                sell = await self.chain.quote_sell(identity, qty_str, request_context=child)
                if buy.data is None or sell.data is None:
                    reason = (getattr(buy, 'reason_code', None) or getattr(sell, 'reason_code', None) or 'CHAIN_QUOTE_UNAVAILABLE')
                    raise self._unavailable(reason)
                data = _mapping(buy.data)
                data['capabilities'] = {**dict(data.get('capabilities') or {}), 'fx': dict(self._fx_provenance.get(buy.data.quote_currency, {}))}
                data.update(sell_vwap=sell.data.sell_vwap, sell_executable_qty=sell.data.sell_executable_qty,
                            mid_price=None, exit_feasible=False, exit_feasibility='UNKNOWN',
                            entry_feasible=False, quote_to_usd=await self._fx(buy.data.quote_currency, request_context))
                data['capabilities']['fx'] = dict(self._fx_provenance.get(buy.data.quote_currency, {}))
                return data
            adapter = {'BINANCE_SPOT': self.spot, 'BINANCE_ALPHA': self.alpha}.get(venue)
            if adapter is None:
                raise self._unavailable('VENUE_UNSUPPORTED')
            result = await adapter.quote(identity, qty_str, request_context=child)
            if result.status not in ('OK', 'PARTIAL') or result.data is None or result.stale:
                # Budget denial must surface as BudgetExhausted for DEFERRED;
                # other venue gaps stay honest UNAVAILABLE (never fabricated).
                probe = getattr(result, 'reason_code', None) or ''
                if 'BUDGET' in str(probe) or 'UNBUDGETED' in str(probe):
                    raise BudgetExhausted(str(probe)[:300])
                raise self._unavailable(result.reason_code or 'VENUE_QUOTE_UNAVAILABLE')
            data_obj = result.data
            if getattr(data_obj, 'requested_canonical_qty', qty_str) != qty_str:
                raise self._unavailable('QUOTE_QUANTITY_MISMATCH')
            # CR14: FX leg carries the same caller context (real job_type).
            fx = await self._fx(data_obj.quote_currency, request_context)
            scale = Decimal(fx)
            direction_costs = dict(data_obj.direction_costs)
            for key in ('buy_fee_usd', 'sell_fee_usd'):
                if direction_costs.get(key) is not None:
                    direction_costs[key] = str(Decimal(str(direction_costs[key])) * scale)
            return replace(data_obj, quote_to_usd=fx,
                           reference_notional_usd=str(Decimal(data_obj.reference_notional_usd) * scale),
                           estimated_fee_usd=(str(Decimal(data_obj.estimated_fee_usd) * scale)
                                              if data_obj.estimated_fee_usd is not None else None),
                           direction_costs=direction_costs,
                           capabilities={**dict(data_obj.capabilities), 'fx': dict(self._fx_provenance.get(data_obj.quote_currency, {}))})
        except BudgetExhausted:
            raise
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            raise

    async def collect_funding(self, symbol: str, as_of_ms: int, request_context: Any) -> Any:
        """MarketPort funding collector (D19.1): frozen FundingContext."""
        from diveintocrypto_desktop.shortlab.hedge.funding_score import build_funding_context
        from diveintocrypto_desktop.shortlab.hedge.funding_score import resolve_history_class

        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(f"symbol must be non-empty str, got {symbol!r}")
        as_of = int(as_of_ms)
        # Current predicted rate comes from Mark (source time preserved).
        try:
            mark = await self.mark(symbol, request_context=request_context)
        except BudgetExhausted:
            raise
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            raise
        current_rate = mark.get('last_funding_rate')
        # Settled history + listing (honest gaps, never zero-filled).
        try:
            events = await self.repo.list_funding_events(symbol, as_of - 90 * 86_400_000, as_of)
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            raise
        try:
            raw = await self._exchange_info(request_context=request_context)
            entry = next((e for e in raw.get('symbols', []) if e.get('symbol') == symbol), {})
        except BudgetExhausted:
            raise
        except Exception as exc:
            if _is_budget_denial(exc):
                raise BudgetExhausted(str(exc)[:300]) from exc
            entry = {}
        onboard = entry.get('onboardDate')
        listing = ({'onboard_at_ms': onboard, 'reliable': True}
                   if isinstance(onboard, int) and 0 < onboard <= as_of else None)
        metrics = compute_funding_metrics(events, [], as_of, listing, symbol=symbol,
                                          current_rate=current_rate)
        # Schedule coverages (UNKNOWN when no verifiable schedule; never 1-fill).
        try:
            from diveintocrypto_desktop.shortlab import funding_schedule as _sched

            try:
                schedules = await self.repo.list_funding_schedules(symbol, as_of)
            except Exception:
                schedules = ()
            cov7 = _sched.compute_schedule_coverage(events, schedules, as_of - 7 * 86_400_000, as_of, as_of)
            cov30 = _sched.compute_schedule_coverage(events, schedules, as_of - 30 * 86_400_000, as_of, as_of)
            cov90 = _sched.compute_schedule_coverage(events, schedules, as_of - 90 * 86_400_000, as_of, as_of)
        except Exception:
            from diveintocrypto_desktop.shortlab.repair_contracts import FundingCoverage as _FC

            def _unknown_cov(start: int, end: int) -> Any:
                return _FC(window_start_ms=start, window_end_ms=end, expected_count=None,
                           received_count=0, coverage_fraction=None,
                           schedule_coverage_fraction="0",
                           missing_slots=(), reasons=("FUNDING_SCHEDULE_UNKNOWN",))
            cov7 = _unknown_cov(as_of - 7 * 86_400_000, as_of)
            cov30 = _unknown_cov(as_of - 30 * 86_400_000, as_of)
            cov90 = _unknown_cov(as_of - 90 * 86_400_000, as_of)
        history_class, _age = resolve_history_class(listing if listing is not None else {'reliable': False}, as_of)
        try:
            listing_days: int | None = None
            if listing is not None and isinstance(listing.get('onboard_at_ms'), int):
                listing_days = max(0, (as_of - int(listing['onboard_at_ms'])) // 86_400_000)
        except Exception:
            listing_days = None
        from diveintocrypto_desktop.shortlab import observations as _obs

        cur_obs = _obs.make_observation(
            {'rate': str(current_rate) if current_rate is not None else None,
             'funding_time_ms': mark.get('next_funding_time', mark.get('next_funding_time_ms'))},
            source="binance:fapi/premiumIndex", source_as_of_ms=mark.get('as_of_ms'),
            fetched_at_ms=mark.get('fetched_at_ms', as_of), known_at_ms=mark.get('fetched_at_ms', as_of),
            status="OK" if current_rate is not None else "UNAVAILABLE",
            reason_code=None if current_rate is not None else "FUNDING_CURRENT_UNKNOWN",
        )
        last_rate = getattr(metrics, 'last_settled_rate', None)
        last_obs: Any = None
        if last_rate is not None and events:
            # CR02 (D03/D05): compat latest settled + persisted receipt.
            # The canonical ``sl_funding_event`` row carries no receipt; the
            # original ``sl_funding_observation`` receipt (``known_at``) for
            # that ``funding_time`` known no later than ``as_of`` is restored
            # verbatim. A missing receipt stays ``None`` (UNKNOWN downstream);
            # an old event is never restamped with current ``as_of``.
            latest_t: int | None = None
            try:
                for _ev in events:
                    _t = _funding_time_of(_ev)
                    if _t is None or _t > as_of:
                        continue
                    if latest_t is None or _t > latest_t:
                        latest_t = _t
            except Exception:
                latest_t = None
            if latest_t is not None:
                receipt_known: int | None = None
                receipt_fetched: int | None = None
                try:
                    _list_obs = getattr(self.repo, "list_funding_observations", None)
                    if callable(_list_obs):
                        try:
                            _rows = await _list_obs(symbol, latest_t, latest_t, as_of)
                        except Exception as _exc:
                            if isinstance(_exc, BudgetExhausted) or _is_budget_denial(_exc):
                                raise
                            _rows = ()
                        if _rows:
                            _best_known: int | None = None
                            _best_fetched: int | None = None
                            for _row in _rows:
                                _k = _funding_receipt_known_at(_row)
                                if _k is None or _k > as_of:
                                    continue
                                if _best_known is None or _k > _best_known:
                                    _best_known = _k
                                    _best_fetched = _k
                                    try:
                                        if isinstance(_row, Mapping):
                                            _f = _row.get("fetched_at_ms", _row.get("fetchedAt"))
                                            if _f is None:
                                                _mj = _row.get("meta_json", _row.get("meta"))
                                                if isinstance(_mj, Mapping):
                                                    _f = _mj.get("fetched_at_ms", _mj.get("known_at_ms"))
                                            if _f is not None:
                                                _best_fetched = int(_f)
                                        else:
                                            _f2 = getattr(_row, "fetched_at_ms", None)
                                            if _f2 is not None:
                                                _best_fetched = int(_f2)
                                    except (TypeError, ValueError):
                                        _best_fetched = _k
                            receipt_known = _best_known
                            receipt_fetched = _best_fetched
                except BudgetExhausted:
                    raise
                except Exception:
                    receipt_known = None
                if receipt_known is not None:
                    try:
                        last_obs = _obs.make_observation(
                            {'rate': str(last_rate), 'funding_time_ms': latest_t},
                            source="binance:fapi/fundingRate", source_as_of_ms=latest_t,
                            fetched_at_ms=int(receipt_fetched if receipt_fetched is not None else receipt_known),
                            known_at_ms=int(receipt_known), status="OK")
                    except Exception:
                        last_obs = None
                else:
                    last_obs = None
        return build_funding_context(
            metrics, cov7, cov30, cov90, history_class=history_class,
            listing_age_days=listing_days,
            conservative_apr=getattr(metrics, 'conservative_apr', None),
            conservative_method="CONSERVATIVE_P25",
            current_observation=cur_obs, last_settled_observation=last_obs,
            schedule_refs=(), input_refs={'funding': f"funding:{symbol}:{as_of}"})
