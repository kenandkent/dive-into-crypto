"""Production Hedge jobs. Memory monitoring never invents prices or balances.

R11b (D11/D18/D19): symbol-merged Mark, deep-quote cap 10 / 4 concurrency /
8s deadline, fair rotation (last_served asc, symbol), risk-change immediate
+ 60s persist, 5s degraded without clearing positions, budget DEFERRED, and
R14 capture/quote-task interfaces via RepairPorts + MarketPort. Legacy
_collect/monitor paths are preserved for pre-repair callers.
"""
from __future__ import annotations
import asyncio
import dataclasses
import json
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from .monitor import compute_monitor, should_persist
from .alerts import evaluate_alerts
from .funding_score import (score_fcs, compute_funding_std_30d, compute_longest_negative_streak, compute_rolling_7d_aprs, compute_p25, resolve_history_class)

#: R11b tick bounds (D11): per-tick deep quotes, concurrency, deadline.
R11B_DEEP_LIMIT = 10
R11B_CONCURRENCY = 4
R11B_DEADLINE_MS = 8000
R11B_DEPTH_TTL_MS = 30_000
R11B_FUNDING_TTL_MS = 60_000


def _is_budget_denial(exc: BaseException) -> bool:
    name = type(exc).__name__
    if "BudgetExhausted" in name or "Unbudgeted" in name:
        return True
    code = str(getattr(exc, "reason_code", "") or "")
    if "BUDGET" in code or "UNBUDGETED" in code or "REQUEST_BUDGET_EXHAUSTED" in code or "JOB_TYPE_UNKNOWN" in code:
        return True
    text = str(exc)[:300]
    return ("BUDGET_EXHAUSTED" in text or "UNBUDGETED_ENDPOINT" in text
            or "JOB_TYPE_UNKNOWN" in text)


def _is_rate_limited(exc: BaseException) -> bool:
    code = str(getattr(exc, "reason_code", "") or "")
    if "RATE_LIMITED" in code or "429" in code:
        return True
    status = getattr(exc, "status", None)
    try:
        if int(status) == 429:
            return True
    except (TypeError, ValueError):
        pass
    return "429" in str(exc)[:200] or "RATE_LIMITED" in str(exc)[:200]


def mapping(value):
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return dict(value) if isinstance(value, Mapping) else {}


class HedgeJobs:
    def __init__(self, service):
        self.service = service
        service._hedge_jobs = self
        self.mirror = {}
        self.hydrated = False
        self.market_cache = {}
        self.collection_times = {}
        self.clear_ticks = {}
        self.persist_lag = {}
        self.hydrate_lock = asyncio.Lock()
        # R11b fair rotation + accounting (per-symbol last served, 429/budget).
        self._last_served: dict[str, int] = {}
        self._rate_limited_count = 0
        self._budget_deferred_count = 0
        self._last_tick_ms = 0
        self._last_unserved: tuple[str, ...] = ()

    def invalidate(self, plan_id=None):
        self.hydrated = False

    async def _db(self, awaitable):
        return await asyncio.wait_for(awaitable, timeout=5)

    async def hydrate(self):
        async with self.hydrate_lock:
            repo = self.service._repository
            plans=[]
            statuses = ['PARTIALLY_FILLED','ACTIVE','CLOSING']
            # Future-proof: include FUNDED_PENDING_ACTIVATION once the
            # repository supports it (R01 006 follow-up). The mock repos in
            # pre-R11b unit tests lack _HEDGE_PLAN_STATUS, so the extra
            #status is skipped there to preserve their 3-query expectation.
            try:
                supported = getattr(repo, '_HEDGE_PLAN_STATUS', None)
                if supported is not None and 'FUNDED_PENDING_ACTIVATION' in set(supported):
                    statuses = ['PARTIALLY_FILLED','FUNDED_PENDING_ACTIVATION','ACTIVE','CLOSING']
            except Exception:
                pass
            for status in statuses:
                offset=0
                while True:
                    page=await self._db(repo.list_hedge_plans(status=status,limit=200,offset=offset))
                    plans.extend(p for p in page if p.get('status') == status)
                    if len(page)<200: break
                    offset+=200
            fresh={}
            for row in plans:
                row=mapping(row); pid=row['plan_id']
                positions=await self._db(repo.aggregate_hedge_position(pid))
                previous=await self._db(repo.latest_hedge_monitor(pid))
                alerts=await self._db(repo.list_hedge_alerts(plan_id=pid,limit=200))
                old=self.mirror.get(pid,{})
                fills,quotes=await self._read_archived(pid,row.get('canonical_id'))
                frozen=mapping(row.get('plan_config_json'))
                multiplier=frozen.get('contract_multiplier',mapping(frozen.get('identity')).get('contract_multiplier'))
                if multiplier is None and hasattr(self.service,'_hedge_identity_for'):
                    try: multiplier=mapping(await self.service._hedge_identity_for(row['symbol'])).get('contract_multiplier')
                    except Exception: multiplier=None
                row['contract_multiplier']=multiplier
                fresh[pid]={'plan':row,'positions':positions,'previous':previous,'last_persist':mapping(previous).get('as_of_ms'),'alerts':{a['dedup_key']:dict(a) for a in alerts if a['state'] in ('OPEN','ACKNOWLEDGED')},'events':old.get('events',()),'fills':fills,'quotes':quotes}
            self.mirror=fresh
            self.hydrated=True

    async def _read_archived(self, pid, canonical_id):
        repo=self.service._repository
        if not hasattr(repo,'_run'):
            return (),()
        def read():
            con=repo._require_con()
            rows=con.execute("SELECT event_id,event_json FROM sl_hedge_fill_event WHERE plan_id=? ORDER BY executed_at_ms,event_id",[pid]).fetchall()
            fills=[dict(json.loads(raw),event_id=eid) for eid,raw in rows]
            quotes=con.execute("SELECT quote_json FROM sl_spot_venue_snapshot WHERE canonical_id=? ORDER BY as_of_ms",[canonical_id]).fetchall()
            return fills,[json.loads(raw) for (raw,) in quotes]
        return await self._db(repo._run(read))

    def _settled_inputs(self,state,events):
        from .ledger import aggregate_events
        out=[]
        for original in events:
            event=mapping(original)
            when=int(event.get('funding_time_ms') or 0)
            event['rate']=str(event.get('rate',event.get('funding_rate'))) if event.get('rate',event.get('funding_rate')) is not None else None
            if event.get('mark_price') is not None: event['mark_price']=str(event['mark_price'])
            # Public events contain no historical FX or contemporaneous position.
            # Only user-entered fills and archived contemporaneous quotes supply it.
            fills=[f for f in state['fills'] if int(f.get('executed_at_ms') or 0)<=when]
            if fills and state['plan'].get('contract_multiplier') is not None:
                try:
                    positions=aggregate_events(fills,identity={'contract_multiplier':str(state['plan']['contract_multiplier'])})
                    short=next((p for p in positions if p.leg_type=='FUTURES_SHORT'),None)
                    if short is not None:
                        mult=Decimal(str(state['plan']['contract_multiplier']))
                        event['short_qty']=str(Decimal(short.remaining_qty)/mult)
                except Exception:
                    event['short_qty']=None
            fx=[]
            for q in state['quotes']:
                provenance=mapping(mapping(q.get('capabilities')).get('fx'))
                source=provenance.get('source_as_of_ms')
                known=provenance.get('known_at_ms')
                if q.get('quote_to_usd') is not None and isinstance(source,int) and isinstance(known,int) and abs(source-when)<=5000 and known<=when:
                    fx.append(q)
            if fx: event['fx_to_usd']=str(fx[-1]['quote_to_usd'])
            out.append(event)
        return tuple(out)

    async def _collect(self, plan, positions, now_ms=None, request_context: Any | None = None):
        symbol=plan['symbol']
        qty=sum((Decimal(str(p.get('remaining_qty') or '0')) for p in positions if p.get('leg_type')=='SPOT_LONG'),Decimal(0))
        venue=plan.get('spot_venue') or mapping(plan.get('plan_config_json')).get('spot_venue') or 'BINANCE_SPOT'
        key=(symbol,str(qty),venue)
        if now_ms is not None and self.collection_times.get(key)==now_ms:
            return dict(self.market_cache.get(key,{}))
        market=getattr(self.service,'_hedge_market',None)
        if market is None:
            return self.market_cache.get(key,{})
        try:
            # R11b: forward immutable RequestContext when the market supports
            # it; legacy market.collect(symbol,qty,venue) keeps working with
            # pre-repair fakes (TypeError fallback).
            if request_context is not None:
                try:
                    import inspect as _inspect
                    try:
                        _sig = _inspect.signature(market.collect)
                        if 'request_context' in _sig.parameters:
                            cache=await market.collect(symbol,str(qty),venue,request_context=request_context)
                        else:
                            cache=await market.collect(symbol,str(qty),venue)
                    except (TypeError, ValueError):
                        cache=await market.collect(symbol,str(qty),venue)
                except Exception:
                    raise
            else:
                cache=await market.collect(symbol,str(qty),venue)
            self.market_cache[key]=mapping(cache)
            self.collection_times[key]=now_ms
        except Exception as exc:
            # Budget denial and 429 are counted (DEFERRED/rate-limited
            # honesty); old quotes naturally expire, positions preserved.
            if _is_budget_denial(exc):
                self._budget_deferred_count += 1
            if _is_rate_limited(exc):
                self._rate_limited_count += 1
            # Keep original source timestamps; old quotes naturally expire.
            pass
        return dict(self.market_cache.get(key,{}))

    def _request_context_for(self, context: Any, *, job_type: str, job_id: str | None = None) -> Any | None:
        """Build an immutable RequestContext for market sends (R11b).

        Preserves the JobContext budget/trace; job_type is the D19档位
        (monitor/opportunity/background) while job_id carries the real task
        name (hedge_monitor/...). ``None`` budget stays unbounded (tests).
        """
        try:
            from ..request_budget import make_request_context
        except Exception:
            return None
        budget = getattr(context, 'request_budget', None)
        trace_id = getattr(context, 'trace_id', None)
        now_ms: int | None = None
        try:
            now_ms = int(context.clock_ms())
        except Exception:
            try:
                now_ms = int(time.time() * 1000)
            except Exception:
                now_ms = None
        deadline_ms = (int(now_ms) + R11B_DEADLINE_MS) if isinstance(now_ms, int) else None
        try:
            return make_request_context(budget, job_type=job_type, host='fapi',
                                        trace_id=str(trace_id) if trace_id is not None else None)
        except Exception:
            return None

    def select_deep_symbols(self, symbols: Sequence[str], now_ms: int, limit: int = R11B_DEEP_LIMIT) -> list[str]:
        """Fair rotation: last_served asc then symbol (D11, 11 coins serve tail)."""
        ordered = sorted(set(symbols), key=lambda s: (int(self._last_served.get(s, 0)), str(s)))
        return list(ordered[:max(0, int(limit))])

    def mark_symbols_served(self, symbols: Sequence[str], now_ms: int) -> None:
        for symbol in symbols:
            self._last_served[str(symbol)] = int(now_ms)

    async def _collect_tick(
        self,
        items: Sequence[tuple[str, Any, Any]],
        now_ms: int,
        request_context: Any | None = None,
        *,
        limit: int = R11B_DEEP_LIMIT,
        concurrency: int = R11B_CONCURRENCY,
        deadline_ms: int = R11B_DEADLINE_MS,
    ) -> dict[str, Any]:
        """One bounded tick over (plan_id, plan, positions) items (D11).

        Symbol-merged Mark (one logical fetch per symbol), deep quotes for at
        most ``limit`` symbols via fair rotation, ``concurrency``-wide with a
        total ``deadline_ms``. Unserved symbols stay capacity/time degraded
        (positions preserved, no realtime exit feasible). Returns a report
        with max_tick_ms/unserved_symbols for load tests (Fake timer).
        """
        started_real = time.monotonic()
        symbols = sorted({str(plan.get('symbol')) for _, plan, _ in items if isinstance(plan, Mapping) and plan.get('symbol')})
        selected = self.select_deep_symbols(symbols, now_ms, limit)
        selected_set = set(selected)
        unserved = sorted(set(symbols) - selected_set)
        self._last_unserved = tuple(unserved)
        sem = asyncio.Semaphore(max(1, int(concurrency)))

        async def _one(pid: str, plan: Any, positions: Any) -> tuple[str, dict[str, Any]]:
            symbol = str(plan.get('symbol')) if isinstance(plan, Mapping) else ''
            if symbol not in selected_set:
                return pid, {'_unserved': True, 'reason': 'MONITOR_CAPACITY_LIMITED'}
            async with sem:
                try:
                    cache = await asyncio.wait_for(
                        self._collect(plan, positions, now_ms, request_context),
                        timeout=max(0.001, float(deadline_ms) / 1000.0),
                    )
                    return pid, dict(cache)
                except asyncio.TimeoutError:
                    return pid, {'_timeout': True, 'reason': 'TICK_DEADLINE_EXCEEDED'}
                except Exception as exc:
                    if _is_budget_denial(exc):
                        return pid, {'_deferred': True, 'reason': 'BUDGET_DEFERRED'}
                    return pid, {'_error': type(exc).__name__, 'reason': str(exc)[:160]}

        try:
            pairs = await asyncio.wait_for(
                asyncio.gather(*(_one(pid, plan, pos) for pid, plan, pos in items)),
                timeout=max(0.001, float(deadline_ms) / 1000.0),
            )
        except asyncio.TimeoutError:
            # Total deadline exceeded: everything not yet served is degraded,
            # never cleared or fabricated.
            pairs = [(pid, {'_timeout': True, 'reason': 'TICK_DEADLINE_EXCEEDED'}) for pid, _, _ in items]
        caches = {pid: cache for pid, cache in pairs}
        # Rotation advances only for symbols actually attempted this tick.
        self.mark_symbols_served(selected, now_ms)
        elapsed_ms = int((time.monotonic() - started_real) * 1000)
        self._last_tick_ms = elapsed_ms
        return {'caches': caches, 'selected': selected, 'unserved_symbols': unserved,
                'max_tick_ms': elapsed_ms, 'served': len(selected), 'total_symbols': len(symbols)}

    async def _alerts(self, pid, previous, current, now):
        state=self.mirror[pid]; repo=self.service._repository
        for change in evaluate_alerts(previous,current,self.service._config):
            key=change['dedup_key']; existing=state['alerts'].get(key)
            if not change['fresh']:
                self.clear_ticks[key]=0
                continue
            if change['active']:
                self.clear_ticks[key]=0
                if existing and now-int(existing.get('last_seen_at_ms') or 0)<60000:
                    continue
                record=await self._db(repo.upsert_hedge_alert(pid,change['code'],change['severity'],change['recommended_action'],change['context'],now,dedup_key=key))
                state['alerts'][key]=record
            elif existing:
                self.clear_ticks[key]=self.clear_ticks.get(key,0)+1
                if self.clear_ticks[key]>=2:
                    await self._db(repo.resolve_hedge_alert(existing['alert_id'],now))
                    state['alerts'].pop(key,None)

    async def monitor(self, context, job_id=None):
        from ..service import JobStatus
        now=int(context.clock_ms()); stats={'computed':0,'persisted':0,'degraded':0}
        try:
            await self.service._ensure_hedge_available()
            if not self.hydrated: await self.hydrate()
        except Exception as exc:
            # A failed refresh cannot leave the last safe snapshot exposed as
            # current, especially after a newly committed fill invalidated it.
            unknown = ('actual_hedge_ratio', 'residual_short_notional_usd', 'mark_price',
                'spot_price', 'current_basis_pct', 'basis_pnl_usd', 'estimated_settled_funding_usd',
                'projected_next_funding_usd', 'spot_pnl_usd', 'futures_pnl_usd', 'known_cost_usd',
                'estimated_exit_cost_usd', 'net_pnl_before_exit_usd', 'estimated_net_pnl_after_exit_usd',
                'liquidation_distance', 'safety_score', 'exit_liquidity_json')
            for state in self.mirror.values():
                previous = state.get('previous')
                if previous is None:
                    continue
                metrics = dict(previous.metrics_json)
                metrics['degraded_reasons'] = list(metrics.get('degraded_reasons', [])) + ['POSITION_MIRROR_UNAVAILABLE']
                metrics['persist_lag_ms'] = 5001
                quality = dict(previous.quality_json)
                quality['position_mirror'] = {'status':'UNAVAILABLE', 'reason':type(exc).__name__}
                state['previous'] = dataclasses.replace(previous, as_of_ms=now,
                    status='MONITOR_DEGRADED', metrics_json=metrics, quality_json=quality,
                    **{field: None for field in unknown})
                stats['degraded'] += 1
            return JobStatus(str(job_id or context.trace_id),'hedge_monitor','FAILED',stats=stats,error_code=type(exc).__name__,started_at_ms=now,finished_at_ms=now)
        # R11b: immutable task context (monitor档位, real job_id) + bounded tick.
        request_context = self._request_context_for(context, job_type='monitor', job_id=str(job_id or 'hedge_monitor'))
        entries = [(pid, state['plan'], state['positions']) for pid, state in tuple(self.mirror.items())]
        tick_report: dict[str, Any] = {'caches': {}, 'unserved_symbols': [], 'max_tick_ms': 0}
        if entries:
            try:
                tick_report = await self._collect_tick(entries, now, request_context)
            except Exception:
                tick_report = {'caches': {}, 'unserved_symbols': [], 'max_tick_ms': 0}
        caches = tick_report.get('caches', {}) if isinstance(tick_report, Mapping) else {}
        # Extra honesty counters (old keys preserved for pre-repair tests).
        stats['unserved_symbols'] = list(tick_report.get('unserved_symbols', [])) if isinstance(tick_report, Mapping) else []
        stats['max_tick_ms'] = int(tick_report.get('max_tick_ms', 0)) if isinstance(tick_report, Mapping) else 0
        stats['rate_limited'] = int(self._rate_limited_count)
        stats['budget_deferred'] = int(self._budget_deferred_count)
        for pid,state in tuple(self.mirror.items()):
            async with self.service._hedge_lock_for(pid):
                raw_cache = caches.get(pid, None)
                if raw_cache is None:
                    cache=await self._collect(state['plan'],state['positions'],now, request_context)
                elif isinstance(raw_cache, Mapping) and (raw_cache.get('_unserved') or raw_cache.get('_timeout') or raw_cache.get('_deferred')):
                    # Capacity/time/budget degraded: keep old quotes (naturally
                    # expire), positions never cleared, no realtime exit.
                    cache=dict(self.market_cache.get((state['plan'].get('symbol'), str(sum((Decimal(str(p.get('remaining_qty') or '0')) for p in state['positions'] if p.get('leg_type')=='SPOT_LONG'),Decimal(0))), state['plan'].get('spot_venue') or mapping(state['plan'].get('plan_config_json')).get('spot_venue') or 'BINANCE_SPOT'), {}))
                    cache['_tick_degraded_reason'] = str(raw_cache.get('reason', 'MONITOR_CAPACITY_LIMITED'))
                else:
                    cache=dict(raw_cache)
                cache['persist_lag_ms']=self.persist_lag.get(pid,0)
                monitor_plan=dict(state['plan'])
                config=mapping(monitor_plan.get('plan_config_json'))
                simulation_input=mapping(config.get('simulation_input'))
                for field in ('liquidation_price','liquidation_price_source','liquidation_price_updated_at_ms'):
                    if simulation_input.get(field) is not None: monitor_plan[field]=simulation_input[field]
                for field in ('target_hedge_ratio','futures_notional_usd','futures_contract_qty','canonical_futures_qty','target_spot_qty','liquidation_price'):
                    value=config.get(field,monitor_plan.get(field))
                    if value is not None: monitor_plan[field]=str(value)
                original_liquidation=monitor_plan.get('liquidation_price')
                if original_liquidation is not None:
                    try:
                        multiplier=Decimal(str(monitor_plan.get('contract_multiplier')))
                        if multiplier<=0: raise ValueError('unknown multiplier')
                        monitor_plan['liquidation_price']=str(Decimal(str(original_liquidation))/multiplier)
                    except Exception:
                        monitor_plan['liquidation_price']=None
                snapshot=compute_monitor(monitor_plan,state['positions'],cache,state['events'],self.service._config,now)
                funding=mapping(cache.get('funding_metrics'))
                extra=dict(snapshot.metrics_json)
                extra['user_liquidation_native_price']=original_liquidation
                extra['contract_multiplier']=state['plan'].get('contract_multiplier')
                extra['funding_current_rate']=funding.get('current_rate')
                extra['funding_recent_rates']=[mapping(e).get('rate') for e in state['events'][-3:] if mapping(e).get('rate') is not None]
                # R11b: capacity/time/budget degraded keeps positions, hides
                # realtime exit feasibility (never fabricates fresh quotes).
                tick_reason = cache.get('_tick_degraded_reason') if isinstance(cache, Mapping) else None
                if tick_reason:
                    degraded = list(extra.get('degraded_reasons', []))
                    if tick_reason not in degraded:
                        degraded.append(str(tick_reason))
                    extra['degraded_reasons'] = degraded
                    # Unserved symbols must not display realtime exit feasible.
                    exit_liq = dict(extra.get('exit_liquidity', {}) or {})
                    exit_liq['covers_remaining'] = False
                    extra['exit_liquidity'] = exit_liq
                snapshot=dataclasses.replace(snapshot,metrics_json=extra,created_at_ms=now)
                if tick_reason:
                    snapshot=dataclasses.replace(snapshot,status='MONITOR_DEGRADED')
                previous=state['previous']; stats['computed']+=1
                try:
                    await self._alerts(pid,previous,snapshot,now)
                    if should_persist(previous,snapshot,state['last_persist'],self.service._config,now_ms=now):
                        await self._db(self.service._repository.save_monitor_snapshot(dataclasses.asdict(snapshot)))
                        state['last_persist']=now; stats['persisted']+=1
                    self.persist_lag[pid]=0
                except Exception:
                    self.persist_lag[pid]=5001
                    metrics=dict(snapshot.metrics_json); metrics['persist_lag_ms']=5001
                    metrics['degraded_reasons']=list(metrics.get('degraded_reasons',[]))+['PERSISTENCE_LAG']
                    snapshot=dataclasses.replace(snapshot,status='MONITOR_DEGRADED',metrics_json=metrics)
                state['previous']=snapshot
                stats['degraded']+=snapshot.status=='MONITOR_DEGRADED'
        return JobStatus(str(job_id or context.trace_id),'hedge_monitor','SUCCEEDED',stats=stats,started_at_ms=now,finished_at_ms=int(context.clock_ms()))

    async def venue_refresh(self,context,job_id=None):
        from ..service import JobStatus
        now=int(context.clock_ms()); refreshed=0; failed=0
        await self.service._ensure_hedge_available()
        await self.hydrate()
        for state in self.mirror.values():
            cache=await self._collect(state['plan'],state['positions'],now)
            quote=mapping(cache.get('spot_quote'))
            if not quote:
                failed+=1; continue
            try:
                await self._save_quote(state['plan'],quote,now)
                refreshed+=1
            except Exception: failed+=1
        return JobStatus(str(job_id or context.trace_id),'hedge_venue_refresh','FAILED' if failed and not refreshed else 'SUCCEEDED',error_code='VENUE_REFRESH_UNAVAILABLE' if failed and not refreshed else None,stats={'refreshed':refreshed,'failed':failed},started_at_ms=now,finished_at_ms=int(context.clock_ms()))

    async def _save_quote(self,plan,quote,now):
        await self._db(self.service._repository.save_spot_venue_snapshot({'snapshot_id':quote.get('snapshot_id') or f"{plan['symbol']}:{quote.get('venue','BINANCE_SPOT')}:{now}",'canonical_id':plan['canonical_id'],'venue':quote.get('venue','BINANCE_SPOT'),'venue_symbol':quote.get('venue_symbol'),'as_of_ms':quote['as_of_ms'],'fetched_at_ms':quote['fetched_at_ms'],'expires_at_ms':quote.get('expires_at_ms'),'reference_notional_usd':quote.get('reference_notional_usd') or plan.get('futures_notional_usd') or '10000','quote_json':quote,'status':quote.get('status','UNAVAILABLE'),'reason_code':quote.get('reason_code')}))

    async def opportunity(self,context,job_id=None):
        from ..service import JobStatus, _maybe_await
        now=int(context.clock_ms()); computed=0; failed=0
        await self.service._ensure_hedge_available()
        rows=await _maybe_await(self.service._universe_fn(self.service._config.universe.limit))
        rows=sorted(rows,key=lambda r:float(r.get('quote_volume') or 0),reverse=True)[:self.service._config.universe.shortlist_size]
        for row in rows:
            symbol=row.get('symbol') or row.get('s')
            if not symbol: continue
            try:
                identity=mapping(await self.service._hedge_identity_for(symbol))
                funding=await self.service._hedge_funding_for(symbol)
                mark=mapping(await self.service._hedge_mark_for(symbol))
                price=Decimal(str(mark['price']))
                price_usd=Decimal(str(mark['canonical_price_usd']))
                reference=str(self.service._config.funding_capture.reference_notional_usd)
                qty=Decimal(reference)/price_usd
                quote=mapping(await self.service._hedge_quote_for(symbol,str(qty),'BINANCE_SPOT'))
                basis=None
                spot=quote.get('mid_price')
                if spot is not None and Decimal(str(spot))>0:
                    basis=float((price-Decimal(str(spot)))/Decimal(str(spot)))
                events=await self._db(self.service._repository.list_funding_events(symbol,now-90*86400000,now))
                history_class,_=resolve_history_class(identity,now)
                stats={'funding_std_30d':compute_funding_std_30d(events,None,now), 'longest_negative_streak_30d':compute_longest_negative_streak(events,now),'p25_rolling_7d_apr_30d':compute_p25(compute_rolling_7d_aprs(events,now,lookback_days=30)), 'p25_rolling_7d_apr_90d':compute_p25(compute_rolling_7d_aprs(events,now,lookback_days=90)),'history_class':history_class}
                venue_summary=dict(quote)
                venue_summary['requested_canonical_qty']=str(qty)
                if quote.get('buy_vwap') is not None and quote.get('sell_vwap') is not None and spot is not None and Decimal(str(spot))>0:
                    costs=self.service._config.hedge.costs
                    fees=sum((Decimal(str(costs[k])) for k in ('futures_entry_fee_rate','futures_exit_fee_rate','spot_entry_fee_rate','spot_exit_fee_rate')),Decimal(0))
                    venue_summary['roundtrip_cost_pct']=str((Decimal(str(quote['buy_vwap']))-Decimal(str(quote['sell_vwap'])))/Decimal(str(spot))+fees)
                market=getattr(self.service,'_hedge_market',None)
                if market is not None and hasattr(market,'_exchange_info'):
                    info=await market._exchange_info()
                    contract=next((c for c in info.get('symbols',[]) if c.get('symbol')==symbol),{})
                    status=contract.get('status'); delivery=contract.get('deliveryDate')
                    identity.update(futures_status=status, no_delisting=(status=='TRADING' and isinstance(delivery,int) and delivery>now) if status is not None and delivery is not None else None)
                identity['multiplier_verified']=identity.get('contract_multiplier') is not None and identity.get('identity_confidence') in ('HIGH','VERIFIED')
                identity['spot_quote_fresh']=quote.get('status')=='OK' and quote.get('expires_at_ms') is not None and int(quote['expires_at_ms'])>=now
                identity['next_funding_time_known']=mark.get('next_funding_time') is not None or mark.get('next_funding_time_ms') is not None
                # CR11 (D08/R09): real FCS hash for the frozen projection (never "").
                try:
                    from ..config import fcs_config_hash as _fcs_hash_fn
                    _fcs_hash = _fcs_hash_fn(self.service._config)
                    if not isinstance(_fcs_hash, str) or not _fcs_hash.strip():
                        _fcs_hash = ""
                except Exception:
                    _fcs_hash = ""
                result=score_fcs(funding,venue_summary,basis,identity,self.service._config,funding_stats=stats,reference_notional_usd=reference,symbol=symbol,canonical_id=identity['canonical_id'],as_of_ms=now,snapshot_id=f'{symbol}:fcs:{now}',fcs_config_hash=_fcs_hash,created_at_ms=now)
                data=dataclasses.asdict(result)
                for key in ('module_scores','funding_metrics','venue_summary','basis','risk','reasons'):
                    data[key+'_json']=data.pop(key)
                # CR11 (D08/R09): opportunity refresh calls the single frozen
                # projection implementation and persists the complete frozen
                # projection + Gate/hash/term/expiry (never LEGACY/stale by
                # omission). No GET-time fabrication of historic projections.
                from .opportunity import build_projection_v2 as _build_proj
                from .opportunity import build_risk_json as _build_risk
                _fm = mapping(data.get('funding_metrics_json'))
                # Listing age (term) from identity onboard when available.
                _age_days = None
                try:
                    for _k in ('listing_age_days', 'listingAgeDays', 'age_days', 'ageDays'):
                        _v = identity.get(_k)
                        if _v is not None and not isinstance(_v, bool):
                            try:
                                _iv = int(float(str(_v).strip())) if isinstance(_v, str) else int(float(_v))
                            except (TypeError, ValueError):
                                continue
                            if _iv >= 0:
                                _age_days = _iv
                                break
                    if _age_days is None:
                        for _k in ('onboard_at_ms', 'onboardMs', 'onboard_ms'):
                            _v = identity.get(_k)
                            if isinstance(_v, bool):
                                continue
                            try:
                                _ob = int(_v)
                            except (TypeError, ValueError):
                                continue
                            if _ob > 0 and _ob <= now:
                                _age_days = max(0, (int(now) - _ob) // 86400000)
                                break
                except Exception:
                    _age_days = None
                def _int_or_none(_v):
                    try:
                        if _v is None or isinstance(_v, bool):
                            return None
                        _iv = int(_v)
                    except (TypeError, ValueError):
                        return None
                    return _iv if _iv >= 0 else None
                _quote_exp = _int_or_none(quote.get('expires_at_ms'))
                _mark_exp = _int_or_none(mark.get('expires_at_ms', mark.get('mark_expires_at_ms')))
                _fund_exp = None
                try:
                    if isinstance(funding, Mapping):
                        _fund_exp = _int_or_none(funding.get('expires_at_ms', funding.get('funding_expires_at_ms')))
                    else:
                        _fund_exp = _int_or_none(getattr(funding, 'expires_at_ms', None))
                except Exception:
                    _fund_exp = None
                _venue_quote = dict(quote)
                _venue_quote.setdefault('venue', 'BINANCE_SPOT')
                _venue_quote['requested_canonical_qty'] = str(qty)
                if 'reference_notional_usd' not in _venue_quote and 'referenceNotionalUsd' not in _venue_quote:
                    _venue_quote['reference_notional_usd'] = reference
                if venue_summary.get('roundtrip_cost_pct') is not None:
                    _venue_quote.setdefault('roundtrip_cost_pct', venue_summary.get('roundtrip_cost_pct'))
                _expiries = [_v for _v in (_quote_exp, _mark_exp, _fund_exp) if _v is not None]
                _snapshot = {
                    'snapshot_id': str(result.snapshot_id),
                    'symbol': str(symbol),
                    'canonical_id': str(identity.get('canonical_id') or symbol.lower()),
                    'as_of_ms': int(now),
                    'fcs': result.fcs,
                    'fcs_config_hash': _fcs_hash,
                    'funding_metrics': dict(_fm),
                    'funding_7d': _fm.get('funding_7d'),
                    'funding_30d': _fm.get('funding_30d'),
                    'positive_ratio_30d': _fm.get('positive_ratio_30d'),
                    'conservative_apr': _fm.get('conservative_apr'),
                    'history_class': history_class,
                    'listing_age_days': _age_days,
                    'venue_quotes': [_venue_quote],
                    'venue_summary': dict(venue_summary),
                    'reference_notional_usd': reference,
                    'quote_expires_at_ms': _quote_exp,
                    'mark_expires_at_ms': _mark_exp,
                    'funding_expires_at_ms': _fund_exp,
                    'expires_at_candidates': list(_expiries),
                }
                _proj = _build_proj(_snapshot, int(now))
                _risk = _build_risk(_snapshot, int(now), extra={'identity': dict(identity)})
                data['fcs'] = _proj.get('fcs')
                data['readiness'] = str(_proj.get('readiness', 'NOT_READY'))
                data['reasons_json'] = list(_proj.get('reasons', []))
                data['fcs_config_hash'] = str(_proj.get('fcs_config_hash') or _fcs_hash or "")
                data['risk_json'] = _risk
                await self._db(self.service._repository.save_funding_capture_snapshot(data))
                await self._save_quote({'symbol':symbol,'canonical_id':identity['canonical_id']},quote,now)
                computed+=1
            except Exception: failed+=1
        return JobStatus(str(job_id or context.trace_id),'funding_capture_refresh','FAILED' if failed and not computed else 'SUCCEEDED',error_code='FCS_REFRESH_UNAVAILABLE' if failed and not computed else None,stats={'computed':computed,'failed':failed},started_at_ms=now,finished_at_ms=int(context.clock_ms()))

    async def settlement(self,context,job_id=None):
        from ..service import JobStatus
        now=int(context.clock_ms()); checked=0
        await self.service._ensure_hedge_available()
        await self.hydrate()
        for state in self.mirror.values():
            plan=state['plan']; symbol=plan['symbol']
            cache=await self._collect(plan,state['positions'],now)
            events=cache.get('settled_events') or cache.get('funding_events')
            if events is None:
                events=await self._db(self.service._repository.list_funding_events(symbol,int(plan.get('created_at_ms') or now),now))
            # Raw public events cannot establish event-time holdings/FX. Preserve
            # missing inputs so carry stays unknown rather than inventing 1 USD.
            state['events']=self._settled_inputs(state,events)
            checked+=1
        result=await self.monitor(context,job_id=job_id)
        return dataclasses.replace(result,job_type='hedge_settlement_check',stats={**result.stats,'checked':checked})

    # -- R11b R14 interfaces (independent of user positions) ---------------
    def _resolve_repository_port(self) -> Any:
        repo = getattr(self.service, '_repository_port', None)
        if repo is not None:
            return repo
        return getattr(self.service, '_repository', None)

    def _resolve_market_port(self) -> Any:
        market = getattr(self.service, '_market_port', None)
        if market is not None:
            return market
        return getattr(self.service, '_hedge_market', None)

    def _require_capture_callback(self, name: str) -> Any:
        require = getattr(self.service, '_require_repair_port', None)
        if callable(require):
            return require(name)
        ports = getattr(self.service, '_repair_ports', None)
        if ports is None:
            from ..repair_ports import RepairDependencyUnavailable
            raise RepairDependencyUnavailable(name)
        return ports.require(name)

    async def capture_entries(self, context: Any, capture_context: Any, job_id: str | None = None) -> Any:
        """R14 entry capture task (D14/D18.1): independent of user holdings.

        Calls the bound ``capture_strategy_entries`` via RepairPorts with the
        MarketPort and an immutable evidence RequestContext. Budget refusal
        stays DEFERRED upstream (never scaled fabrication); group skew and
        missing depth stay UNAVAILABLE/UNEXECUTABLE via R14a.
        """
        from ..service import JobStatus
        now = int(context.clock_ms())
        started = now
        try:
            await self.service._ensure_hedge_available()
        except Exception as exc:
            return JobStatus(str(job_id or getattr(context, 'trace_id', 'capture')), 'strategy_capture',
                             'FAILED', stats={'claimed': 0, 'deferred': 1}, error_code=type(exc).__name__,
                             started_at_ms=started, finished_at_ms=int(context.clock_ms()))
        request_context = self._request_context_for(context, job_type='evidence', job_id=str(job_id or 'strategy_capture'))
        # Evidence budget档位: BACKGROUND (D19.3 opportunity/evidence/entry).
        if request_context is not None:
            try:
                import dataclasses as _dc
                request_context = _dc.replace(request_context, job_type='evidence')
            except Exception:
                pass
        try:
            callback = self._require_capture_callback('capture_strategy_entries')
        except Exception as exc:
            return JobStatus(str(job_id or getattr(context, 'trace_id', 'capture')), 'strategy_capture',
                             'FAILED', stats={'claimed': 0, 'deferred': 0},
                             error_code=getattr(exc, 'reason_code', type(exc).__name__),
                             started_at_ms=started, finished_at_ms=int(context.clock_ms()))
        repository = self._resolve_repository_port()
        market = self._resolve_market_port()
        try:
            result = await asyncio.wait_for(callback(capture_context, repository, market, request_context),
                                            timeout=max(0.001, float(R11B_DEADLINE_MS) / 1000.0))
        except asyncio.TimeoutError:
            return JobStatus(str(job_id or getattr(context, 'trace_id', 'capture')), 'strategy_capture',
                             'FAILED', stats={'claimed': 0, 'deferred': 0},
                             error_code='TICK_DEADLINE_EXCEEDED',
                             started_at_ms=started, finished_at_ms=int(context.clock_ms()))
        except Exception as exc:
            if _is_budget_denial(exc):
                self._budget_deferred_count += 1
                return JobStatus(str(job_id or getattr(context, 'trace_id', 'capture')), 'strategy_capture',
                                 'SUCCEEDED', stats={'claimed': 0, 'deferred': 1, 'status': 'DEFERRED'},
                                 started_at_ms=started, finished_at_ms=int(context.clock_ms()))
            return JobStatus(str(job_id or getattr(context, 'trace_id', 'capture')), 'strategy_capture',
                             'FAILED', stats={'claimed': 0},
                             error_code=type(exc).__name__,
                             started_at_ms=started, finished_at_ms=int(context.clock_ms()))
        try:
            entry_ids = tuple(getattr(result, 'entry_ids', ()) or ())
            status = str(getattr(result, 'status', 'UNAVAILABLE'))
        except Exception:
            entry_ids, status = (), 'UNAVAILABLE'
        return JobStatus(str(job_id or getattr(context, 'trace_id', 'capture')), 'strategy_capture',
                         'SUCCEEDED', stats={'claimed': len(entry_ids), 'status': status, 'entry_ids': list(entry_ids)},
                         started_at_ms=started, finished_at_ms=int(context.clock_ms()))

    async def collect_due_quotes(self, context: Any, as_of_ms: int, job_id: str | None = None) -> Any:
        """R14 due-quote task (D14.2/D18.1): 20/round, DEFERRED retained.

        Claims via RepositoryPort and collects via MarketPort; budget refusal
        counts DEFERRED (retained, retried before deadline); past-deadline
        tasks explicitly fail (never scaled with 100% quote).
        """
        from ..service import JobStatus
        now = int(context.clock_ms())
        started = now
        try:
            await self.service._ensure_hedge_available()
        except Exception as exc:
            return JobStatus(str(job_id or getattr(context, 'trace_id', 'due_quotes')), 'strategy_quote_collection',
                             'FAILED', stats={'claimed': 0, 'deferred': 0},
                             error_code=type(exc).__name__,
                             started_at_ms=started, finished_at_ms=int(context.clock_ms()))
        request_context = self._request_context_for(context, job_type='evidence', job_id=str(job_id or 'strategy_quote_collection'))
        if request_context is not None:
            try:
                import dataclasses as _dc
                request_context = _dc.replace(request_context, job_type='evidence')
            except Exception:
                pass
        try:
            callback = self._require_capture_callback('collect_due_quotes')
        except Exception as exc:
            return JobStatus(str(job_id or getattr(context, 'trace_id', 'due_quotes')), 'strategy_quote_collection',
                             'FAILED', stats={'claimed': 0, 'complete': 0, 'deferred': 0, 'unavailable': 0},
                             error_code=getattr(exc, 'reason_code', type(exc).__name__),
                             started_at_ms=started, finished_at_ms=int(context.clock_ms()))
        repository = self._resolve_repository_port()
        market = self._resolve_market_port()
        try:
            result = await asyncio.wait_for(callback(repository, market, int(as_of_ms), request_context),
                                            timeout=max(0.001, float(R11B_DEADLINE_MS) / 1000.0))
        except asyncio.TimeoutError:
            return JobStatus(str(job_id or getattr(context, 'trace_id', 'due_quotes')), 'strategy_quote_collection',
                             'FAILED', stats={'claimed': 0, 'complete': 0, 'deferred': 0, 'unavailable': 0},
                             error_code='TICK_DEADLINE_EXCEEDED',
                             started_at_ms=started, finished_at_ms=int(context.clock_ms()))
        except Exception as exc:
            if _is_budget_denial(exc):
                self._budget_deferred_count += 1
                return JobStatus(str(job_id or getattr(context, 'trace_id', 'due_quotes')), 'strategy_quote_collection',
                                 'SUCCEEDED', stats={'claimed': 0, 'complete': 0, 'deferred': 1, 'unavailable': 0},
                                 started_at_ms=started, finished_at_ms=int(context.clock_ms()))
            return JobStatus(str(job_id or getattr(context, 'trace_id', 'due_quotes')), 'strategy_quote_collection',
                             'FAILED', stats={'claimed': 0},
                             error_code=type(exc).__name__,
                             started_at_ms=started, finished_at_ms=int(context.clock_ms()))
        try:
            claimed = int(getattr(result, 'claimed', 0))
            complete = int(getattr(result, 'complete', 0))
            deferred = int(getattr(result, 'deferred', 0))
            unavailable = int(getattr(result, 'unavailable', 0))
            task_ids = list(getattr(result, 'task_ids', ()) or ())
        except Exception:
            claimed, complete, deferred, unavailable, task_ids = 0, 0, 0, 0, []
        # claimed == complete+deferred+unavailable (R00 frozen invariant).
        if claimed != complete + deferred + unavailable:
            return JobStatus(str(job_id or getattr(context, 'trace_id', 'due_quotes')), 'strategy_quote_collection',
                             'FAILED', stats={'claimed': claimed, 'complete': complete, 'deferred': deferred, 'unavailable': unavailable},
                             error_code='QUOTE_RESULT_INVARIANT',
                             started_at_ms=started, finished_at_ms=int(context.clock_ms()))
        self._budget_deferred_count += int(deferred)
        return JobStatus(str(job_id or getattr(context, 'trace_id', 'due_quotes')), 'strategy_quote_collection',
                         'SUCCEEDED', stats={'claimed': claimed, 'complete': complete, 'deferred': deferred,
                                             'unavailable': unavailable, 'task_ids': task_ids},
                         started_at_ms=started, finished_at_ms=int(context.clock_ms()))
