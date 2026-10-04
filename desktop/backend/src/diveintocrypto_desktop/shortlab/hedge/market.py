"""Production public market wiring; no account access or order submission."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass, replace
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Mapping

from diveintocrypto_desktop.shortlab.request_budget import make_request_context, Denied
from diveintocrypto_desktop.shortlab.hedge.funding_score import compute_funding_metrics
from diveintocrypto_desktop.shortlab.hedge.venues.binance_spot import BinanceSpotVenue
from diveintocrypto_desktop.shortlab.hedge.venues.binance_alpha import BinanceAlphaVenue
from diveintocrypto_desktop.shortlab.hedge.venues.ethereum_0x import Ethereum0xPriceVenue
from diveintocrypto_desktop.shortlab.providers.coingecko import CoinGeckoProvider


def _mapping(value):
    return asdict(value) if is_dataclass(value) else dict(value)


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

    def _permit(self, host, family):
        if self.budget is None:
            return None
        # Non-Binance transports have a public request unit (one send), not a
        # fabricated Binance exchange weight. Every retry acquires separately.
        permit = self.budget.try_acquire(host, 1, 'monitor', family)
        if isinstance(permit, Denied):
            raise self._unavailable(permit.reason_code)
        return permit

    async def _fx_transport(self, url, params):
        permit = self._permit('api.coingecko.com', 'coingecko')
        try:
            if permit is not None:
                permit.mark_sent()
            return await self.fx_provider._default_fetcher(url, params)
        finally:
            if permit is not None:
                permit.release()

    async def _chain_transport(self, url, params, headers, request_context):
        import aiohttp
        from diveintocrypto_desktop.data.http import get_session
        permit = self._permit('api.0x.org', '0x-price')
        try:
            session = await get_session()
            if permit is not None:
                permit.mark_sent()
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                return await response.json()
        finally:
            if permit is not None:
                permit.release()

    async def _identity(self, symbol):
        value = await self.service._hedge_identity_for(symbol)
        return SimpleNamespace(**value) if isinstance(value, Mapping) else value

    async def _fx(self, currency):
        if currency == 'USD':
            return '1'
        coin = {'USDT': 'tether', 'USDC': 'usd-coin', 'FDUSD': 'first-digital-usd'}.get(currency)
        if coin is None:
            raise self._unavailable('QUOTE_FX_UNAVAILABLE')
        async with self._lock:
            cached = self._fx_cache.get(currency)
            if cached and self.clock() - cached[0] < 60_000:
                return cached[1]
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

    async def mark(self, symbol):
        cached = self._marks.get(symbol)
        if cached and self.clock() - cached['fetched_at_ms'] < 5_000:
            return dict(cached)
        from diveintocrypto_desktop.data.funding import premium_index
        identity = await self._identity(symbol)
        raw = await premium_index(symbol, request_context=self._ctx())
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
        fx = await self._fx('USDT')
        mark = {**raw, 'native_price': str(native), 'mark_price': str(native),
                'price': str(native / multiplier),
                'canonical_price_usd': str(native / multiplier * Decimal(fx)),
                'quote_currency': 'USDT', 'quote_to_usd': fx,
                'as_of_ms': ts, 'source_timestamp_ms': ts,
                'fetched_at_ms': self.clock(), 'expires_at_ms': ts + 60_000}
        self._marks[symbol] = mark
        return dict(mark)

    async def quote(self, symbol, canonical_qty, venue=None):
        identity = await self._identity(symbol)
        venue = venue or 'BINANCE_SPOT'
        if venue == 'ONCHAIN_DEX':
            buy = await self.chain.quote_buy(identity, canonical_qty, request_context=self._ctx('0x'))
            sell = await self.chain.quote_sell(identity, canonical_qty, request_context=self._ctx('0x'))
            if buy.data is None or sell.data is None:
                raise self._unavailable(buy.reason_code or sell.reason_code or 'CHAIN_QUOTE_UNAVAILABLE')
            data = _mapping(buy.data)
            data['capabilities'] = {**dict(data.get('capabilities') or {}), 'fx': dict(self._fx_provenance.get(buy.data.quote_currency, {}))}
            data.update(sell_vwap=sell.data.sell_vwap, sell_executable_qty=sell.data.sell_executable_qty,
                        mid_price=None, exit_feasible=False, exit_feasibility='UNKNOWN',
                        entry_feasible=False, quote_to_usd=await self._fx(buy.data.quote_currency))
            data['capabilities']['fx'] = dict(self._fx_provenance.get(buy.data.quote_currency, {}))
            if data.get('buy_vwap') is not None:
                data['reference_notional_usd'] = str(Decimal(data['buy_vwap']) * Decimal(canonical_qty) * Decimal(data['quote_to_usd']))
            return data
        adapter = {'BINANCE_SPOT': self.spot, 'BINANCE_ALPHA': self.alpha}.get(venue)
        if adapter is None:
            raise self._unavailable('VENUE_UNSUPPORTED')
        result = await adapter.quote(identity, canonical_qty, request_context=self._ctx('spot' if venue == 'BINANCE_SPOT' else 'alpha'))
        if result.status not in ('OK', 'PARTIAL') or result.data is None or result.stale:
            raise self._unavailable(result.reason_code or 'VENUE_QUOTE_UNAVAILABLE')
        data = result.data
        if data.requested_canonical_qty != canonical_qty:
            raise self._unavailable('QUOTE_QUANTITY_MISMATCH')
        fx = await self._fx(data.quote_currency)
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

    async def _exchange_info(self):
        from diveintocrypto_desktop.data import http
        async with self._exchange_lock:
            if self._exchange_cache and self.clock() - self._exchange_cache[0] < 30 * 60_000:
                return self._exchange_cache[1]
            raw = await http.get_json(f'{http.FAPI_V1}/exchangeInfo', request_context=self._ctx())
            if self.budget is not None:
                self.budget.configure_host_limits('fapi', raw.get('rateLimits', []))
            self._exchange_cache = (self.clock(), raw)
            return raw

    async def funding(self, symbol):
        now = self.clock()
        cached = self._funding_cache.get(symbol)
        if cached and now - cached[0] < 30_000:
            return cached[1]
        mark = await self.mark(symbol)
        try:
            raw = await self._exchange_info()
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

    async def rules(self, kind, symbol):
        from diveintocrypto_desktop.data import spot
        from diveintocrypto_desktop.data.trading_rules import parse_trading_rules
        identity = await self._identity(symbol)
        key = (kind, symbol)
        cached = self._rules_cache.get(key)
        if cached and self.clock() - cached[0] < 30 * 60_000:
            return cached[1]
        if kind == 'futures':
            raw = await self._exchange_info()
            entry = next((e for e in raw.get('symbols', []) if e.get('symbol') == symbol), None)
            instrument, venue = symbol, 'BINANCE_SPOT'
        else:
            instrument = getattr(identity, 'binance_spot_symbol', None)
            if not instrument:
                raise self._unavailable('TRADING_RULES_UNVERIFIED')
            entry = await spot.fetch_spot_exchange_entry(instrument, request_context=self._ctx('spot'))
            venue = 'BINANCE_SPOT'
        if entry is None:
            raise self._unavailable('TRADING_RULES_UNVERIFIED')
        rules = parse_trading_rules(entry, venue, instrument, {'known_at_ms': self.clock()})
        self._rules_cache[key] = (self.clock(), rules)
        return rules

    async def collect(self, symbol, qty, venue=None):
        values = await asyncio.gather(self.mark(symbol), self.quote(symbol, qty, venue),
                                      self.funding(symbol), return_exceptions=True)
        result = {'fetched_at_ms': self.clock(), 'collection_errors': {}}
        for key, value in zip(('futures_mark', 'spot_quote', 'funding_metrics'), values):
            if isinstance(value, Exception):
                result['collection_errors'][key] = str(value)
            else:
                result[key] = _mapping(value) if key == 'spot_quote' else value
        return result
