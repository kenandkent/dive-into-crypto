"""Historical evidence reads; frozen quotes are never replaced by live quotes."""
from __future__ import annotations

from typing import Any
import json
import time
from decimal import Decimal, InvalidOperation
from ..request_budget import scoped_request_context, get_current_request_context
from ...data.binance_klines import fetch_klines_range
from ..hedge.models import HistoricalPriceBar, HistoricalFundingEvent, HistoricalLifecycle


class RepositoryHistoricalMarketProvider:
    """Historical protocol adapter with explicit FX provenance.

    Archived FX defaults to a same-currency quote observed within 5 seconds
    before the event. An optional ``fx_fn(asset, at_ms)`` can override this
    with another archived source. Missing FX is never guessed from current FX.
    Price reads use bounded historical ranges, not latest-ticker calls.
    """
    def __init__(self, repository: Any, *, price_bars_fn=fetch_klines_range,
                 fx_fn=None, clock_ms=None):
        self.repository = repository
        self.price_bars_fn = price_bars_fn
        self.fx_fn = fx_fn
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.completed_at_ms = 0
        self.retryable_history_unavailable = False

    async def _fx(self, asset, at_ms):
        if self.fx_fn is not None:
            return await self.fx_fn(asset, at_ms)
        if self.repository is None or not hasattr(self.repository, "_run"):
            return None
        def read():
            con = self.repository._require_con()
            cur = con.execute(
                'SELECT quote_json, as_of_ms, fetched_at_ms FROM sl_spot_venue_snapshot '
                'WHERE as_of_ms >= ? AND as_of_ms <= ? AND fetched_at_ms <= ? '
                'ORDER BY as_of_ms DESC, snapshot_id ASC', [at_ms-5000, at_ms, at_ms])
            return self.repository._rows_to_dicts(cur)
        for row in await self.repository._run(read):
            try:
                quote = json.loads(row['quote_json']) if isinstance(row['quote_json'], str) else row['quote_json']
                currency = quote.get('quote_currency', quote.get('quoteCurrency'))
                capabilities = quote.get('capabilities')
                fx_meta = capabilities.get('fx', {}) if isinstance(capabilities, dict) else {}
                if not isinstance(fx_meta, dict):
                    fx_meta = {}
                source = quote.get('fx_source_as_of_ms', fx_meta.get('source_as_of_ms'))
                known = quote.get('fx_known_at_ms', fx_meta.get('known_at_ms'))
                value = quote.get('quote_to_usd', quote.get('quoteToUsd'))
                if currency != asset or source is None or known is None or value is None:
                    continue
                if not (at_ms-5000 <= int(source) <= at_ms and at_ms-5000 <= int(known) <= at_ms):
                    continue
                dec = Decimal(str(value))
                if dec.is_finite() and dec > 0:
                    return str(value)
            except (ValueError, TypeError, InvalidOperation):
                continue
        return None

    async def read_frozen_quote(self, snapshot_id):
        return await self.repository.read_frozen_quote(snapshot_id)

    async def find_frozen_quote(self, canonical_id, venue, canonical_qty,
                                at_ms, max_skew_ms=5000):
        return await self.repository.find_frozen_quote(
            canonical_id, venue, canonical_qty, at_ms, max_skew_ms)

    async def read_price_bars(self, symbol, start_ms, end_ms, request_context=None):
        context = request_context if request_context is not None else get_current_request_context()
        self.retryable_history_unavailable = False
        try:
            with scoped_request_context(context):
                rows = await self.price_bars_fn(symbol, '1h', start_ms, end_ms)
        except Exception:
            self.retryable_history_unavailable = True
            raise
        completed = self.clock_ms()
        self.completed_at_ms = max(self.completed_at_ms, completed)
        result = []
        for row in rows:
            timestamp = int(row['t'])
            # Binance parser uses nanoseconds; fixtures may use milliseconds.
            opened = timestamp // 1000000 if timestamp > 10**15 else timestamp
            closed = opened + 3600000 - 1
            if opened < start_ms or closed > end_ms:
                continue
            result.append(HistoricalPriceBar(
                symbol, opened, closed, *(str(row[k]) for k in ('o','h','l','c')),
                'USDT', await self._fx('USDT', closed),
                'binance:fapi/klines:1h', completed))
        return tuple(result)

    async def read_settled_funding(self, symbol, start_ms, end_ms, request_context=None):
        rows = await self.repository.list_funding_events(symbol, start_ms, end_ms)
        completed = self.clock_ms()
        self.completed_at_ms = max(self.completed_at_ms, completed)
        return tuple([HistoricalFundingEvent(
            symbol, row.funding_time_ms, str(row.funding_rate),
            None if row.mark_price is None else str(row.mark_price), 'USDT',
            await self._fx('USDT', row.funding_time_ms),
            'sl_funding_event', completed) for row in rows])

    async def read_lifecycle(self, symbol, cutoff_ms):
        def read():
            con = self.repository._require_con()
            cur = con.execute(
                'SELECT * FROM sl_contract_lifecycle WHERE futures_symbol = ? '
                'AND observed_at_ms <= ? ORDER BY observed_at_ms DESC LIMIT 1',
                [symbol, cutoff_ms])
            rows = self.repository._rows_to_dicts(cur)
            return rows[0] if rows else None
        row = await self.repository._run(read)
        if row is None:
            return None
        return HistoricalLifecycle(symbol, row['observed_at_ms'], row['onboard_at_ms'],
                                   row['delivery_at_ms'], row['contract_type'],
                                   row['exchange_status'],
                                   f"lifecycle:{symbol}:{row['observed_at_ms']}")
