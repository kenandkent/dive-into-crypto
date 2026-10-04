function normalizeHedgePlanResponse(response) {
  return { ...(response.plan || response), legs: response.positions || (response.plan || response).legs, alerts: response.alerts || (response.plan || response).alerts };
}

function buildHedgeLegPayload(form, plan, nowMs) {
  const expectedVersion = Number(form.expectedPlanVersion || plan.planVersion || plan.plan_version);
  if (!Number.isInteger(expectedVersion) || expectedVersion < 1) throw new Error("请先加载计划的当前版本");
  const executedAtMs = form.executedAtMs ? Number(form.executedAtMs) : nowMs;
  if (!Number.isSafeInteger(executedAtMs) || executedAtMs <= 0) throw new Error("成交时间必须为有效的 UTC 毫秒时间戳");
  const funding = form.eventType === "FUNDING_RECEIPT";
  const eventType = ["OPEN", "CLOSE"].includes(form.eventType)
    ? `${form.eventType}_${form.legType}` : form.eventType;
  return {
    clientEventId: form.clientEventId || `ev-${nowMs.toString(36)}`,
    expectedVersion,
    event: {
      schemaVersion: "hedge-event-v1", legType: funding ? "FUNDING" : form.legType,
      eventType, nativeQty: funding ? null : form.nativeQty.trim(), canonicalQty: null,
      nativePrice: funding ? null : form.nativePrice.trim(), priceCurrency: funding ? null : "USDT",
      feeCurrency: null, feeAmount: null, feeUsd: null, gasUsd: null,
      source: "USER_ENTERED", executedAtMs,
      ...(funding ? {amount: form.amount.trim(), currency: "USDT"} : {}),
    },
  };
}

/* ============================================================================
   short-lab — Desktop · Hedge Monitor page (H09 · design B22–B25/B32.6–B32.10)

   Reads ONLY window.DIVE.hedgePlan / hedgeMonitor / applyHedgeLegEvent /
   activateHedgePlan / closeHedgePlan (data.js). Plan (Target) and Monitor
   (Actual) come from DIFFERENT endpoints and are never mixed: Target ratio /
   Target quantities render from the plan, Actual ratio / residual / PnL from
   the monitor + legs. B35 copy stays distinct: Estimated settled vs Projected
   next vs Reference vs User-entered vs Confirmed. ACTIVE vs DRAFT/LIMITED
   never share a label; DEGRADED keeps the持仓 state and names the cause.
   Orphan-leg and exit-liquidity copy is explicit; recommended_action never
   rewrites the ledger state. Quantity strings stay verbatim; 503/stale/
   offline render honest copy, never mock. `initial` bypasses fetch for tests.
   ========================================================================== */

function HedgeMonitor({ planId: planIdProp, initial }) {
  const [planId, setPlanId] = React.useState((initial && initial.planId) || planIdProp || "");
  const [plan, setPlan] = React.useState(initial && initial.plan ? initial.plan : null);
  const [monitor, setMonitor] = React.useState(initial && initial.monitor ? initial.monitor : null);
  const [planErr, setPlanErr] = React.useState(initial && initial.planError ? initial.planError : null);
  const [monErr, setMonErr] = React.useState(initial && initial.monitorError ? initial.monitorError : null);
  const [loading, setLoading] = React.useState(Boolean(planIdProp || (initial && initial.planId)) && !(initial && (initial.plan || initial.planError)));
  const [legMsg, setLegMsg] = React.useState(null);
  const [legForm, setLegForm] = React.useState({ legType: "FUTURES_SHORT", eventType: "OPEN", nativeQty: "", nativePrice: "", clientEventId: "", expectedPlanVersion: "", executedAtMs: "", amount: "" });
  const seqRef = React.useRef(0);
  const abortRef = React.useRef(null);

  const load = React.useCallback((id) => {
    const pid = String(id || planId || "").trim();
    if (!pid) { setPlanErr("hedgePlan: empty planId"); return; }
    if (initial && (initial.plan || initial.planError)) return;
    const seq = ++seqRef.current;
    if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} }
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    abortRef.current = ctrl;
    setLoading(true); setPlanErr(null); setMonErr(null);
    const opts = ctrl ? { signal: ctrl.signal } : {};
    window.DIVE.hedgePlan(pid, opts)
      .then((p) => {
        if (seq !== seqRef.current) return;
        setPlan(normalizeHedgePlanResponse(p));
        if (p.monitor) setMonitor(p.monitor);
        return window.DIVE.hedgeMonitor(pid, opts).then((m) => {
          if (seq !== seqRef.current) return;
          setMonitor(m.monitor || m);
          setLoading(false);
        });
      })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current) return;
        setLoading(false);
        const msg = (e && e.message) || String(e);
        if (plan) setMonErr(msg);
        else setPlanErr(msg);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planId]);

  React.useEffect(() => {
    if (planIdProp && planIdProp !== planId) setPlanId(planIdProp);
  }, [planIdProp]);
  React.useEffect(() => { if (planId && !(initial && (initial.plan || initial.planError))) load(planId); }, [planId]);
  React.useEffect(() => () => { if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} } }, []);

  const doLeg = () => {
    const pid = String(planId || "").trim();
    if (!pid) { setLegMsg("hedgePlan: empty planId"); return; }
    let ev;
    try { ev = buildHedgeLegPayload(legForm, plan || {}, Date.now()); }
    catch (e) { setLegMsg(e.message); return; }
    const seq = ++seqRef.current;
    setLegMsg(null);
    window.DIVE.applyHedgeLegEvent(pid, ev, {})
      .then((res) => { if (seq === seqRef.current) { setLegMsg(`OK · ${JSON.stringify(res).slice(0, 160)}`); load(pid); } })
      .catch((e) => { if (seq === seqRef.current) setLegMsg(String((e && e.message) || e)); });
  };

  const doActivate = () => {
    const pid = String(planId || "").trim();
    if (!pid) return;
    window.DIVE.activateHedgePlan(pid, {expectedVersion: Number(plan && (plan.planVersion || plan.plan_version))})
      .then(() => load(pid))
      .catch((e) => setLegMsg(String((e && e.message) || e)));
  };
  const doClose = () => {
    const pid = String(planId || "").trim();
    if (!pid) return;
    window.DIVE.closeHedgePlan(pid, {expectedVersion: Number(plan && (plan.planVersion || plan.plan_version))})
      .then(() => load(pid))
      .catch((e) => setLegMsg(String((e && e.message) || e)));
  };

  if (loading) {
    return <div className="state" style={{ height: 160 }} data-testid="hedge-monitor-loading">
      <div className="spin" /><div>{L("hedge_monitor_loading")}</div></div>;
  }
  if ((planErr && !plan) || (monErr && !plan && !monitor)) {
    const txt = String(planErr || monErr || "");
    const unavailable = /503|unavailable/i.test(txt);
    const offline = /Failed to fetch|NetworkError|network|offline/i.test(txt);
    return (
      <div>
        <div className="scanbar sl-filters">
          <input className="pname" aria-label="plan id" placeholder="planId" value={planId} onChange={(e) => setPlanId(e.target.value)} />
          <button className="cta" onClick={() => load(planId)}>{L("sl_retry")}</button>
        </div>
        <div className="state src-down" role="alert" data-testid={unavailable ? "hedge-monitor-unavailable" : "hedge-monitor-error"}>
          <div className="sd-title">MONITOR {unavailable ? L("sl_unavailable_title") : L("sl_error_title")}</div>
          <div className="sd-body">{offline ? L("hedge_offline") : (unavailable ? L("hedge_monitor_unavailable_body") : L("hedge_monitor_error_body"))}</div>
          <div className="sd-err">{txt}</div>
        </div>
      </div>
    );
  }

  const p = plan || {};
  const m = monitor || {};
  const status = p.status || p.Status || "—";
  const targetRatio = p.targetHedgeRatio != null ? p.targetHedgeRatio : p.target_hedge_ratio;
  const actualRatio = m.actualHedgeRatio != null ? m.actualHedgeRatio : (m.actual_hedge_ratio != null ? m.actual_hedge_ratio : (p.actualHedgeRatio != null ? p.actualHedgeRatio : p.actual_hedge_ratio));
  const residualUsd = m.residualShortNotionalUsd != null ? m.residualShortNotionalUsd : m.residual_short_notional_usd;
  const estSettled = m.estimatedSettledFundingUsd != null ? m.estimatedSettledFundingUsd : m.estimated_settled_funding_usd;
  const projNext = m.projectedNextFundingUsd != null ? m.projectedNextFundingUsd : m.projected_next_funding_usd;
  const basisPnl = m.basisPnlUsd != null ? m.basisPnlUsd : m.basis_pnl_usd;
  const spotPnl = m.spotPnlUsd != null ? m.spotPnlUsd : m.spot_pnl_usd;
  const futPnl = m.futuresPnlUsd != null ? m.futuresPnlUsd : m.futures_pnl_usd;
  const knownFees = m.knownCostUsd != null ? m.knownCostUsd : (m.known_cost_usd != null ? m.known_cost_usd : (m.knownFeesUsd != null ? m.knownFeesUsd : m.known_fees_usd));
  const estExit = m.estimatedExitCostUsd != null ? m.estimatedExitCostUsd : m.estimated_exit_cost_usd;
  const netBefore = m.netPnlBeforeExitUsd != null ? m.netPnlBeforeExitUsd : m.net_pnl_before_exit_usd;
  const netAfter = m.estimatedNetPnlAfterExitUsd != null ? m.estimatedNetPnlAfterExitUsd : m.estimated_net_pnl_after_exit_usd;
  const liqDist = m.liquidationDistance != null ? m.liquidationDistance : m.liquidation_distance;
  const safety = m.safetyScore != null ? m.safetyScore : (m.safety_score != null ? m.safety_score : (p.planSafetyScore != null ? p.planSafetyScore : p.plan_safety_score));
  const recommended = m.recommendedAction || m.recommended_action || p.recommendedAction || p.recommended_action;
  const degraded = /DEGRADED/.test(String(m.status || "") + String(p.degraded || "") + String(m.degraded || ""));
  const orphanCodes = [];
  const alertList = (m.alerts || p.alerts || []);
  (Array.isArray(alertList) ? alertList : []).forEach((a) => {
    if (a && window.HEDGE_FORMAT.isOrphanCode(a.code)) orphanCodes.push(a.code);
  });
  const legs = p.legs || p.actualLegs || p.actual_legs;
  const onlyOneLeg = p.status === "PARTIALLY_FILLED" || orphanCodes.length > 0;
  const targetFutQty = p.canonicalFuturesQty || p.canonical_futures_qty || p.futuresContractQty || p.futures_contract_qty;
  const targetSpotQty = p.targetSpotQty || p.target_spot_qty;

  return (
    <div data-testid="hedge-monitor">
      <div className="vhead"><span className="kicker">{L("hedge_monitor_kicker")}</span><h1>{L("hedge_monitor_title")}</h1>
        <div className="meta">{planId} · <span data-testid="hedge-monitor-status">{status}</span>{degraded ? " · DEGRADED" : ""}</div></div>
      <div className="scanbar sl-filters">
        <input className="pname" aria-label="plan id" placeholder="planId" value={planId} onChange={(e) => setPlanId(e.target.value)} />
        <button className="cta" onClick={() => load(planId)}>{L("sl_retry")}</button>
        <button className="chip" onClick={doActivate}>{L("hedge_activate_btn")}</button>
        <button className="chip" onClick={doClose}>{L("hedge_close_btn")}</button>
      </div>
      {(planErr || monErr) && (plan || monitor) && (
        <div className="provline" data-testid="hedge-monitor-kept-stale" role="alert">
          <span className="tag hot">STALE</span><span>{L("hedge_stale_kept")}</span>
          <span className="sd-err">{String(planErr || monErr).slice(0, 200)}</span>
        </div>
      )}
      {degraded && <div className="reason" data-testid="hedge-monitor-degraded">{L("hedge_monitor_degraded")}</div>}

      <div className="grid2">
        <div className="panel"><div className="ph"><span className="tick">▸</span>{L("hedge_target_title")}</div>
          <div className="pb" style={{ padding: 0 }}>
            <div className="stat"><span className="k">{L("hedge_target_ratio")}</span>
              <span className="v" data-testid="hedge-monitor-target-ratio">{window.HEDGE_FORMAT.fmtRatio(targetRatio)}</span></div>
            <div className="stat"><span className="k">{L("hedge_target_futures_qty")}</span>
              <span className="v" data-testid="hedge-monitor-target-futures">{window.HEDGE_FORMAT.fmtQty(targetFutQty)}</span></div>
            <div className="stat"><span className="k">{L("hedge_target_spot_qty")}</span>
              <span className="v" data-testid="hedge-monitor-target-spot">{window.HEDGE_FORMAT.fmtQty(targetSpotQty)}</span></div>
            <div className="stat"><span className="k">{L("hedge_user_entered_liq")}</span>
              <span className="v" data-testid="hedge-monitor-user-liq">{p.liquidationPrice != null ? String(p.liquidationPrice) : (p.liquidation_price != null ? String(p.liquidation_price) : "—")}</span></div>
          </div></div>
        <div className="panel"><div className="ph"><span className="tick">▸</span>{L("hedge_actual_title")}</div>
          <div className="pb" style={{ padding: 0 }}>
            <div className="stat"><span className="k">{L("hedge_actual_ratio")}</span>
              <span className="v" data-testid="hedge-monitor-actual-ratio">{window.HEDGE_FORMAT.fmtRatio(actualRatio)}</span></div>
            <div className="stat"><span className="k">{L("hedge_residual_short")}</span>
              <span className="v" data-testid="hedge-monitor-residual">{residualUsd != null ? window.HEDGE_FORMAT.fmtUsd(residualUsd) : "—"}</span></div>
            <div className="stat"><span className="k">{L("hedge_liq_distance")}</span>
              <span className="v">{liqDist != null ? window.HEDGE_FORMAT.fmtRatio(liqDist, 2) : "—"}</span></div>
            <div className="stat"><span className="k">{L("hedge_plan_safety")}</span>
              <span className="v">{safety != null ? slFmtScore(safety) : "—"}</span></div>
          </div></div>
      </div>

      <div className="panel"><div className="ph"><span className="tick">▸</span>PnL</div>
        <div className="pb" style={{ padding: 0 }}>
          <div className="stat"><span className="k">{L("hedge_estimated_settled")}</span>
            <span className="v" data-testid="hedge-monitor-est-settled">{estSettled != null ? window.HEDGE_FORMAT.fmtUsd(estSettled) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_projected_next")}</span>
            <span className="v" data-testid="hedge-monitor-projected-next">{projNext != null ? window.HEDGE_FORMAT.fmtUsd(projNext) : "—"}</span></div>
          <div className="gcap">{L("hedge_projected_note")}</div>
          <div className="stat"><span className="k">{L("hedge_basis_pnl")}</span><span className="v">{basisPnl != null ? window.HEDGE_FORMAT.fmtUsd(basisPnl) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_spot_pnl")}</span><span className="v">{spotPnl != null ? window.HEDGE_FORMAT.fmtUsd(spotPnl) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_futures_pnl")}</span><span className="v">{futPnl != null ? window.HEDGE_FORMAT.fmtUsd(futPnl) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_known_fees")}</span><span className="v">{knownFees != null ? window.HEDGE_FORMAT.fmtUsd(knownFees) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_estimated_exit")}</span><span className="v">{estExit != null ? window.HEDGE_FORMAT.fmtUsd(estExit) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_net_before")}</span><span className="v">{netBefore != null ? window.HEDGE_FORMAT.fmtUsd(netBefore) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_net_after")}</span><span className="v">{netAfter != null ? window.HEDGE_FORMAT.fmtUsd(netAfter) : "—"}</span></div>
        </div></div>

      {recommended && (
        <div className="panel" data-testid="hedge-recommended-action">
          <div className="ph"><span className="tick">▸</span>{L("hedge_recommended_action")}</div>
          <div className="pb"><div className="reason">{String(recommended)} · {L("hedge_recommended_note")}</div></div>
        </div>
      )}

      {(onlyOneLeg || orphanCodes.length > 0) && (
        <div className="reason" data-testid="hedge-orphan-warning" role="alert">
          {L("hedge_orphan_warning")} · {orphanCodes.join(" · ") || "ORPHAN_LEG_WARNING"} · {L("hedge_pair_exit_hint")}
        </div>
      )}
      {Array.isArray(alertList) && alertList.length > 0 && <div className="panel" data-testid="hedge-monitor-alerts"><div className="ph">提醒</div><div className="pb">{alertList.map((a, i) => <div key={a.alertId || a.alert_id || i} className="reason">{a.code || a.reason_code || "—"} · {a.message || a.status || ""}</div>)}</div></div>}
      {legs && (
        <div className="panel"><div className="ph"><span className="tick">▸</span>{L("hedge_legs_title")}</div>
          <div className="pb"><div className="reason">{JSON.stringify(legs).slice(0, 400)}</div></div></div>
      )}

      <div className="panel"><div className="ph"><span className="tick">▸</span>{L("hedge_apply_leg_title")}</div>
        <div className="pb">
          <div className="scanbar sl-filters">
            <select aria-label="leg type" value={legForm.legType} onChange={(e) => setLegForm((f) => ({ ...f, legType: e.target.value }))}>
              {["FUTURES_SHORT", "SPOT_LONG"].map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            <select aria-label="event type" value={legForm.eventType} onChange={(e) => setLegForm((f) => ({ ...f, eventType: e.target.value }))}>
              {["OPEN", "CLOSE", "LIQUIDATION", "FUNDING_RECEIPT"].map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            <input className="pname" aria-label="native qty" placeholder="nativeQty (string)" inputMode="decimal" value={legForm.nativeQty} onChange={(e) => setLegForm((f) => ({ ...f, nativeQty: e.target.value }))} />
            <input className="pname" aria-label="native price" placeholder="nativePrice" inputMode="decimal" value={legForm.nativePrice} onChange={(e) => setLegForm((f) => ({ ...f, nativePrice: e.target.value }))} />
            <input className="pname" aria-label="executed timestamp" placeholder="实际成交 UTC 时间戳（毫秒，默认现在）" inputMode="numeric" value={legForm.executedAtMs} onChange={(e) => setLegForm((f) => ({ ...f, executedAtMs: e.target.value }))} />
            {legForm.eventType === "FUNDING_RECEIPT" && <input className="pname" aria-label="funding amount" placeholder="实际收到资金费 USDT" value={legForm.amount} onChange={(e) => setLegForm((f) => ({ ...f, amount: e.target.value }))} />}
            <input className="pname" aria-label="client event id" placeholder="clientEventId" value={legForm.clientEventId} onChange={(e) => setLegForm((f) => ({ ...f, clientEventId: e.target.value }))} />
            <input className="pname" aria-label="expected version" placeholder="expectedPlanVersion" inputMode="numeric" value={legForm.expectedPlanVersion} onChange={(e) => setLegForm((f) => ({ ...f, expectedPlanVersion: e.target.value }))} />
            <button className="cta" onClick={doLeg}>{L("hedge_apply_leg_btn")}</button>
          </div>
          <div className="gcap">{L("hedge_qty_string_hint")}</div>
          {legMsg && <div className="reason" data-testid="hedge-leg-msg">{String(legMsg).slice(0, 400)}</div>}
        </div></div>
    </div>
  );
}
