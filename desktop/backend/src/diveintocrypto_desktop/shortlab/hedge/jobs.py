"""Production Hedge jobs. Memory monitoring never invents prices or balances."""
from __future__ import annotations
import asyncio
import dataclasses
import json
from collections.abc import Mapping
from decimal import Decimal

from .monitor import compute_monitor, should_persist
from .alerts import evaluate_alerts
from .funding_score import (score_fcs, compute_funding_std_30d, compute_longest_negative_streak, compute_rolling_7d_aprs, compute_p25, resolve_history_class)


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

    def invalidate(self, plan_id=None):
        self.hydrated = False

    async def _db(self, awaitable):
        return await asyncio.wait_for(awaitable, timeout=5)

    async def hydrate(self):
        async with self.hydrate_lock:
            repo = self.service._repository
            plans=[]
            for status in ('PARTIALLY_FILLED','ACTIVE','CLOSING'):
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

    async def _collect(self, plan, positions, now_ms=None):
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
            cache=await market.collect(symbol,str(qty),venue)
            self.market_cache[key]=mapping(cache)
            self.collection_times[key]=now_ms
        except Exception:
            # Keep original source timestamps; old quotes naturally expire.
            pass
        return dict(self.market_cache.get(key,{}))

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
        for pid,state in tuple(self.mirror.items()):
            async with self.service._hedge_lock_for(pid):
                cache=await self._collect(state['plan'],state['positions'],now)
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
                snapshot=dataclasses.replace(snapshot,metrics_json=extra,created_at_ms=now)
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
                result=score_fcs(funding,venue_summary,basis,identity,self.service._config,funding_stats=stats,reference_notional_usd=reference,symbol=symbol,canonical_id=identity['canonical_id'],as_of_ms=now,snapshot_id=f'{symbol}:fcs:{now}',created_at_ms=now)
                data=dataclasses.asdict(result)
                for key in ('module_scores','funding_metrics','venue_summary','basis','risk','reasons'):
                    data[key+'_json']=data.pop(key)
                data['risk_json']={**mapping(data.get('risk_json')),'identity':dict(identity)}
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
