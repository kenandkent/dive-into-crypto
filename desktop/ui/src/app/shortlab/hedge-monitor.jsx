function normalizeHedgePlanResponse(response) {
  return { ...(response.plan || response), legs: response.positions || (response.plan || response).legs, alerts: response.alerts || (response.plan || response).alerts };
}

function _firstNonEmptyStr(list) {
  for (const v of (Array.isArray(list) ? list : [])) {
    if (typeof v === "string" && v.trim() !== "") return v.trim();
  }
  return null;
}

function _monitorLegList(plan) {
  if (!plan || typeof plan !== "object") return [];
  for (const k of ["legs", "positions", "actualLegs", "actual_legs", "frozenLegs", "frozen_legs"]) {
    if (Array.isArray(plan[k])) return plan[k];
  }
  return [];
}

/* Fee/price currency comes from the frozen legs only — unknown stays null,
 * never hardcoded "USDT" (USDC legs must not be rewritten). Mirrors
 * monitor-state.mjs resolveFeeCurrency so the bundle works without an
 * explicit import (build.mjs concatenates globals, not ESM). */
function _resolveMonitorFeeCurrency(plan) {
  const legs = _monitorLegList(plan);
  for (const leg of legs) {
    if (!leg || typeof leg !== "object") continue;
    const cur = _firstNonEmptyStr([
      leg.feeCurrency, leg.fee_currency, leg.priceCurrency, leg.price_currency,
      leg.currency, leg.quoteAsset, leg.quote_asset,
      leg.settlementCurrency, leg.settlement_currency,
    ]);
    if (cur) return cur;
  }
  if (plan && typeof plan === "object") {
    const top = _firstNonEmptyStr([
      plan.feeCurrency, plan.fee_currency, plan.priceCurrency, plan.price_currency,
      plan.settlementCurrency, plan.settlement_currency,
      plan.quoteAsset, plan.quote_asset,
    ]);
    if (top) return top;
  }
  return null;
}

function buildHedgeLegPayload(form, plan, nowMs) {
  const expectedVersion = Number(form.expectedPlanVersion || plan.planVersion || plan.plan_version);
  if (!Number.isInteger(expectedVersion) || expectedVersion < 1) throw new Error("请先加载计划的当前版本");
  const executedAtMs = form.executedAtMs ? Number(form.executedAtMs) : nowMs;
  if (!Number.isSafeInteger(executedAtMs) || executedAtMs <= 0) throw new Error("成交时间必须为有效的 UTC 毫秒时间戳");
  const funding = form.eventType === "FUNDING_RECEIPT";
  const eventType = ["OPEN", "CLOSE"].includes(form.eventType)
    ? `${form.eventType}_${form.legType}` : form.eventType;
  const frozenCcy = _resolveMonitorFeeCurrency(plan || {});
  const feeCcyRaw = typeof form.feeCurrency === "string" ? form.feeCurrency.trim() : "";
  const feeAmtRaw = typeof form.feeAmount === "string" ? form.feeAmount.trim() : (form.feeAmount != null && form.feeAmount !== "" ? String(form.feeAmount).trim() : "");
  const gasRaw = typeof form.gasUsd === "string" ? form.gasUsd.trim() : (form.gasUsd != null && form.gasUsd !== "" ? String(form.gasUsd).trim() : "");
  return {
    clientEventId: form.clientEventId || `ev-${nowMs.toString(36)}`,
    expectedVersion,
    event: {
      schemaVersion: "hedge-event-v1", legType: funding ? "FUNDING" : form.legType,
      eventType, nativeQty: funding ? null : form.nativeQty.trim(), canonicalQty: null,
      nativePrice: funding ? null : form.nativePrice.trim(), priceCurrency: funding ? null : frozenCcy,
      feeCurrency: feeCcyRaw !== "" ? feeCcyRaw : null,
      feeAmount: feeAmtRaw !== "" ? feeAmtRaw : null,
      feeUsd: null,
      gasUsd: gasRaw !== "" ? gasRaw : null,
      source: "USER_ENTERED", executedAtMs,
      ...(funding ? { amount: form.amount.trim(), currency: frozenCcy } : {}),
    },
  };
}

function _monitorAsOf(mon) {
  if (!mon || typeof mon !== "object") return null;
  for (const k of ["asOf", "as_of_ms", "asOfMs"]) {
    const n = Number(mon[k]);
    if (Number.isSafeInteger(n) && n > 0) return n;
  }
  const meta = mon.sourceMeta || mon.source_meta || mon.source_meta_json;
  if (meta && typeof meta === "object") {
    for (const k of ["known_at_ms", "knownAtMs"]) {
      const n = Number(meta[k]);
      if (Number.isSafeInteger(n) && n > 0) return n;
    }
  }
  return null;
}

function _pickField(obj, keys) {
  if (!obj || typeof obj !== "object") return undefined;
  for (const k of keys) {
    if (obj[k] !== undefined && obj[k] !== null) return obj[k];
  }
  return undefined;
}

/* CR19 write guard (D12.1/D10): every write button shares one permission
 * computed from the loaded object + error + stale + in-flight + version +
 * expiry state. Load failure, STALE, expired/beyond-grace, switching/loading
 * or a write already in flight disables ALL writes; only a fresh loaded plan
 * with a service-returned integer planVersion>=1 enables them. Pure so
 * node --test can pin the matrix without a DOM. */
function resolveHedgeMonitorWrite(s) {
  const o = (s && typeof s === "object") ? s : {};
  const deny = (reason) => ({ disabled: true, reason });
  const pid = (typeof o.loadedPlanId === "string" ? o.loadedPlanId : String(o.loadedPlanId || "")).trim();
  if (!pid) return deny("load a plan first");
  if (!o.plan) return deny("load a plan first");
  if (o.loading) return deny("switching plan — writes paused");
  if (o.writing) return deny("write in flight — please wait");
  if (o.planErr || o.monErr) return deny("data error — writes paused until refresh");
  if (o.stale) return deny("stale data — writes paused until refresh");
  if (o.expired) return deny("expired — writes paused until refresh");
  if (!o.versionOk) return deny("missing plan version — reload first");
  return { disabled: false, reason: "" };
}

try {
  if (typeof window !== "undefined" && !window.HEDGE_MONITOR_WRITE_GUARD) {
    window.HEDGE_MONITOR_WRITE_GUARD = { resolveWrite: resolveHedgeMonitorWrite };
  }
} catch (e) {}

/* ============================================================================
   short-lab — Desktop · Hedge Monitor page (R12 · D12.1/D18.3; CR19 write guard)

   R12 bindings:
   - planInput (text field) vs loadedPlanId (actually loaded) are separate.
     Switching input never rewrites old data as the new plan: a new load
     clears risk display immediately and bumps generation; late A responses
     after B are ignored; B failure never leaves A displayed as B. All
     writes use loadedPlanId + the service-returned planVersion.
   - generation + AbortController guard every fetch; a single in-flight
     request per plan; unmount aborts; data errors never write (old values
     kept and flagged STALE).
   - foreground 10s poll, background (document.hidden) paused, visible
     recovery refetches immediately. sourceAge/STALE from Mark/Quote TTL
     (20s); beyond grace risk is UNKNOWN (served by backend status).
   - CR19: activate/close/apply-leg share resolveHedgeMonitorWrite — load
     failure, STALE, expired/beyond-grace, switching/loading or a write in
     flight disables every write (not just a missing loadedPlanId), and an
     invalid planVersion keeps them disabled too. Every request (load, poll,
     writes) carries the current AbortSignal; switching plans or unmounting
     aborts the previous controller so old requests never write.
   - activate/close send {expectedVersion} via (planId, body, opts) and never
     drop it. Manual leg currency/fees/execution time come from the frozen
     legs (no hardcoded USDT; unknown stays null, zero is explicit input).
   - shows actual vs estimated funding, Partial net when coverage is
     partial, manual protection status and bilateral exit guidance.

   Reads ONLY window.DIVE.* (data.js) + window.HEDGE_FORMAT. `initial`
   bypasses fetch for tests. No new i18n keys (R13a owns i18n).
   ========================================================================== */

function HedgeMonitor({ planId: planIdProp, initial }) {
  const initialBypass = Boolean(initial && (initial.plan || initial.planError || initial.monitor || initial.monitorError));
  const [planInput, setPlanInput] = React.useState((initial && initial.planId) || planIdProp || "");
  const [loadedPlanId, setLoadedPlanId] = React.useState((initial && initial.planId) || planIdProp || "");
  const [plan, setPlan] = React.useState(initial && initial.plan ? initial.plan : null);
  const [monitor, setMonitor] = React.useState(initial && initial.monitor ? initial.monitor : null);
  const [exitGuide, setExitGuide] = React.useState(initial && initial.exitGuidance ? initial.exitGuidance : null);
  const [planErr, setPlanErr] = React.useState(initial && initial.planError ? initial.planError : null);
  const [monErr, setMonErr] = React.useState(initial && initial.monitorError ? initial.monitorError : null);
  const [exitErr, setExitErr] = React.useState(initial && initial.exitGuidanceError ? initial.exitGuidanceError : null);
  const [loading, setLoading] = React.useState(Boolean(planIdProp || (initial && initial.planId)) && !(initial && (initial.plan || initial.planError)));
  const [nowMs, setNowMs] = React.useState(() => Date.now());
  const [legMsg, setLegMsg] = React.useState(null);
  const [writing, setWriting] = React.useState(false);
  const [legForm, setLegForm] = React.useState({ legType: "FUTURES_SHORT", eventType: "OPEN", nativeQty: "", nativePrice: "", clientEventId: "", expectedPlanVersion: "", executedAtMs: "", amount: "", feeCurrency: "", feeAmount: "", gasUsd: "" });
  const seqRef = React.useRef(0);
  const abortRef = React.useRef(null);
  const inFlightRef = React.useRef(false);
  const mountedRef = React.useRef(true);
  const loadedRef = React.useRef((initial && initial.planId) || planIdProp || "");

  React.useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false; if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} } }; }, []);

  const load = React.useCallback((id) => {
    const pid = String(id != null ? id : planInput || "").trim();
    if (!pid) { setPlanErr("hedgePlan: empty planId"); return; }
    if (initialBypass) return;
    const seq = seqRef.current + 1;
    seqRef.current = seq;
    if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} }
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    abortRef.current = ctrl;
    inFlightRef.current = true;
    // Switch immediately clears risk display so A is never shown as B.
    setPlanInput(pid);
    setLoadedPlanId(null);
    loadedRef.current = null;
    setPlan(null); setMonitor(null); setExitGuide(null);
    setPlanErr(null); setMonErr(null); setExitErr(null);
    setLoading(true);
    const opts = ctrl ? { signal: ctrl.signal } : {};
    window.DIVE.hedgePlan(pid, opts)
      .then((p) => {
        if (seq !== seqRef.current) return null;
        if (!mountedRef.current) return null;
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return null;
        const normalized = normalizeHedgePlanResponse(p);
        setPlan(normalized);
        setLoadedPlanId(pid);
        loadedRef.current = pid;
        if (p && p.monitor) setMonitor(p.monitor);
        // Monitor + exit-guidance refresh under the same generation.
        const mP = window.DIVE.hedgeMonitor(pid, opts).then((m) => {
          if (seq !== seqRef.current || !mountedRef.current) return;
          if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
          setMonitor(m.monitor || m);
          setMonErr(null);
        }).catch((e) => {
          if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
          if (seq !== seqRef.current || !mountedRef.current) return;
          // Partial load: plan stays, monitor error recorded, no write.
          setMonErr((e && e.message) || String(e));
        });
        const gP = (typeof window.DIVE.hedgeExitGuidance === "function")
          ? window.DIVE.hedgeExitGuidance(pid, opts).then((g) => {
            if (seq !== seqRef.current || !mountedRef.current) return;
            if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
            setExitGuide(g.guidance || g.exitGuidance || g);
            setExitErr(null);
          }).catch((e) => {
            if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
            if (seq !== seqRef.current || !mountedRef.current) return;
            setExitErr((e && e.message) || String(e));
          })
          : Promise.resolve();
        return Promise.all([mP, gP]).then(() => {
          if (seq !== seqRef.current || !mountedRef.current) return;
          setLoading(false);
          inFlightRef.current = false;
          setNowMs(Date.now());
        });
      })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) { inFlightRef.current = false; return; }
        if (seq !== seqRef.current || !mountedRef.current) return;
        // B failure never leaves A displayed as B: everything stays cleared.
        setLoading(false);
        inFlightRef.current = false;
        setPlan(null); setMonitor(null); setExitGuide(null);
        setLoadedPlanId(null);
        loadedRef.current = null;
        setPlanErr((e && e.message) || String(e));
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planInput, initialBypass]);

  const pollOnce = React.useCallback(() => {
    const pid = loadedRef.current;
    if (!pid || initialBypass) return;
    if (typeof document !== "undefined" && document.hidden) return;
    if (inFlightRef.current) return;
    const seq = seqRef.current;
    const ctrl = abortRef.current;
    // Reuse the load generation; late full-load responses still win over poll.
    const opts = ctrl && ctrl.signal ? { signal: ctrl.signal } : {};
    // Poll must not clear: single in-flight flag only for the duration.
    // CR19: the poll carries the current AbortSignal (never {}) so a plan
    // switch or unmount cancels it like every other request.
    inFlightRef.current = true;
    window.DIVE.hedgeMonitor(pid, opts).then((m) => {
      if (ctrl && ctrl.signal && ctrl.signal.aborted) { inFlightRef.current = false; return; }
      if (seq !== seqRef.current || !mountedRef.current) { inFlightRef.current = false; return; }
      setMonitor(m.monitor || m);
      setMonErr(null);
      setNowMs(Date.now());
      inFlightRef.current = false;
    }).catch((e) => {
      if (ctrl && ctrl.signal && ctrl.signal.aborted) { inFlightRef.current = false; return; }
      if (seq !== seqRef.current || !mountedRef.current) { inFlightRef.current = false; return; }
      const msg = String((e && e.message) || e);
      if (/abort/i.test(msg)) { inFlightRef.current = false; return; }
      // Data error: keep old values, flag STALE via monErr.
      setMonErr(msg);
      inFlightRef.current = false;
    });
    if (typeof window.DIVE.hedgeExitGuidance === "function") {
      window.DIVE.hedgeExitGuidance(pid, opts).then((g) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current || !mountedRef.current) return;
        setExitGuide(g.guidance || g.exitGuidance || g);
      }).catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialBypass]);

  React.useEffect(() => {
    if (planIdProp && planIdProp !== planInput) setPlanInput(planIdProp);
  }, [planIdProp]);
  React.useEffect(() => {
    const start = (initial && initial.planId) || planIdProp;
    if (start && !initialBypass) load(start);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  React.useEffect(() => () => { if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} } }, []);

  /* Foreground 10s poll, background paused, visible recovery refetches. */
  React.useEffect(() => {
    if (initialBypass) return undefined;
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      pollOnce();
      setNowMs(Date.now());
    }, 10000);
    const onVis = () => {
      if (typeof document !== "undefined" && !document.hidden) {
        setNowMs(Date.now());
        pollOnce();
      }
    };
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("visibilitychange", onVis);
    }
    return () => {
      clearInterval(timer);
      if (typeof document !== "undefined" && document.removeEventListener) {
        document.removeEventListener("visibilitychange", onVis);
      }
    };
  }, [pollOnce, initialBypass]);

  /* CR19: every write re-checks the unified guard (writeDisabled is computed
   * below from loaded object / error / stale / in-flight / version / expiry
   * but, as a render-local const, is already initialized by click time),
   * carries the current AbortSignal so a plan switch or unmount cancels it,
   * and ignores late/aborted responses by generation. */
  const currentWriteOpts = () => {
    const c = abortRef.current;
    return (c && c.signal) ? { signal: c.signal } : {};
  };

  const doLeg = () => {
    if (writeDisabled) { setLegMsg(writeReason || "请先加载计划的当前版本"); return; }
    const pid = String(loadedPlanId || "").trim();
    if (!pid) { setLegMsg("请先加载计划的当前版本"); return; }
    let ev;
    try { ev = buildHedgeLegPayload(legForm, plan || {}, Date.now()); }
    catch (e) { setLegMsg(e.message); return; }
    const seq = ++seqRef.current;
    const ctrl = abortRef.current;
    const wOpts = currentWriteOpts();
    setLegMsg(null);
    setWriting(true);
    window.DIVE.applyHedgeLegEvent(pid, ev, wOpts)
      .then((res) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current || !mountedRef.current) return;
        setLegMsg(`OK · ${JSON.stringify(res).slice(0, 160)}`);
        load(pid);
      })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        const msg = String((e && e.message) || e);
        if (/abort/i.test(msg)) return;
        if (seq !== seqRef.current || !mountedRef.current) return;
        setLegMsg(msg);
      })
      .finally(() => { if (mountedRef.current) setWriting(false); });
  };

  const doActivate = () => {
    if (writeDisabled) { setLegMsg(writeReason || "请先加载计划的当前版本"); return; }
    const pid = String(loadedPlanId || "").trim();
    if (!pid) { setLegMsg("请先加载计划的当前版本"); return; }
    const ver = Number(plan && (plan.planVersion != null ? plan.planVersion : plan.plan_version));
    if (!Number.isInteger(ver) || ver < 1) { setLegMsg("请先加载计划的当前版本"); return; }
    const seq = ++seqRef.current;
    const ctrl = abortRef.current;
    const wOpts = currentWriteOpts();
    setWriting(true);
    window.DIVE.activateHedgePlan(pid, { expectedVersion: ver }, wOpts)
      .then(() => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current || !mountedRef.current) return;
        load(pid);
      })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        const msg = String((e && e.message) || e);
        if (/abort/i.test(msg)) return;
        if (seq !== seqRef.current || !mountedRef.current) return;
        setLegMsg(msg);
      })
      .finally(() => { if (mountedRef.current) setWriting(false); });
  };
  const doClose = () => {
    if (writeDisabled) { setLegMsg(writeReason || "请先加载计划的当前版本"); return; }
    const pid = String(loadedPlanId || "").trim();
    if (!pid) { setLegMsg("请先加载计划的当前版本"); return; }
    const ver = Number(plan && (plan.planVersion != null ? plan.planVersion : plan.plan_version));
    if (!Number.isInteger(ver) || ver < 1) { setLegMsg("请先加载计划的当前版本"); return; }
    const seq = ++seqRef.current;
    const ctrl = abortRef.current;
    const wOpts = currentWriteOpts();
    setWriting(true);
    window.DIVE.closeHedgePlan(pid, { expectedVersion: ver }, wOpts)
      .then(() => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current || !mountedRef.current) return;
        load(pid);
      })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        const msg = String((e && e.message) || e);
        if (/abort/i.test(msg)) return;
        if (seq !== seqRef.current || !mountedRef.current) return;
        setLegMsg(msg);
      })
      .finally(() => { if (mountedRef.current) setWriting(false); });
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
          <input className="pname" aria-label="plan id" placeholder="planId" value={planInput} onChange={(e) => setPlanInput(e.target.value)} />
          <button className="cta" onClick={() => load(planInput)}>{L("sl_retry")}</button>
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
  const mRaw = monitor || {};
  const m = (mRaw.monitor && typeof mRaw.monitor === "object") ? mRaw.monitor : mRaw;
  const status = p.status || p.Status || "—";
  const targetRatio = p.targetHedgeRatio != null ? p.targetHedgeRatio : p.target_hedge_ratio;
  const actualRatio = m.actualHedgeRatio != null ? m.actualHedgeRatio : (m.actual_hedge_ratio != null ? m.actual_hedge_ratio : (p.actualHedgeRatio != null ? p.actualHedgeRatio : p.actual_hedge_ratio));
  const residualUsd = m.residualShortNotionalUsd != null ? m.residualShortNotionalUsd : m.residual_short_notional_usd;
  const metrics = m.metrics || m.metrics_json || {};
  const estSettled = m.estimatedSettledFundingUsd != null ? m.estimatedSettledFundingUsd : m.estimated_settled_funding_usd;
  const projNext = m.projectedNextFundingUsd != null ? m.projectedNextFundingUsd : m.projected_next_funding_usd;
  const knownSubtotal = metrics.funding_known_subtotal_usd != null ? metrics.funding_known_subtotal_usd : (metrics.fundingKnownSubtotalUsd != null ? metrics.fundingKnownSubtotalUsd : (m.fundingKnownSubtotalUsd != null ? m.fundingKnownSubtotalUsd : m.funding_known_subtotal_usd));
  let actualReceipts = m.actualFundingReceiptsUsd != null ? m.actualFundingReceiptsUsd : m.actual_funding_receipts_usd;
  if (actualReceipts === undefined || actualReceipts === null) {
    actualReceipts = metrics.actual_funding_receipts_usd != null ? metrics.actual_funding_receipts_usd : metrics.actualFundingReceiptsUsd;
  }
  if (actualReceipts && typeof actualReceipts === "object" && !Array.isArray(actualReceipts)) {
    const tot = actualReceipts.totalUsd != null ? actualReceipts.totalUsd : (actualReceipts.total_usd != null ? actualReceipts.total_usd : (actualReceipts.total != null ? actualReceipts.total : null));
    actualReceipts = tot;
  }
  const fundingCoverage = metrics.funding_coverage != null ? metrics.funding_coverage : (metrics.fundingCoverage != null ? metrics.fundingCoverage : null);
  const fundingFlags = metrics.funding_flags || metrics.fundingFlags || [];
  const fundingPartial = (Array.isArray(fundingFlags) && fundingFlags.includes("FUNDING_COVERAGE_PARTIAL")) || (estSettled == null && knownSubtotal != null);
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
  const asOf = _monitorAsOf(m);
  const sourceAge = (asOf != null) ? Math.max(0, Number(nowMs) - Number(asOf)) : null;
  const staleByAge = sourceAge != null && sourceAge > 20000;
  const showStale = staleByAge || Boolean((planErr || monErr) && (plan || monitor));
  const protectionStatus = _pickField(p, ["protectionStatus", "protection_status"]) || _pickField(m, ["protectionStatus", "protection_status"]) || "UNKNOWN";
  const guideRaw = exitGuide && typeof exitGuide === "object" ? (exitGuide.guidance || exitGuide.exitGuidance || exitGuide) : null;
  const guideRules = guideRaw && typeof guideRaw === "object" ? (guideRaw.rules || guideRaw.legs || guideRaw) : null;
  const guideFut = guideRules && typeof guideRules === "object" && !Array.isArray(guideRules) ? (guideRules.FUTURES_SHORT != null ? guideRules.FUTURES_SHORT : (guideRules.futures_short != null ? guideRules.futures_short : null)) : null;
  const guideSpot = guideRules && typeof guideRules === "object" && !Array.isArray(guideRules) ? (guideRules.SPOT_LONG != null ? guideRules.SPOT_LONG : (guideRules.spot_long != null ? guideRules.spot_long : null)) : null;
  const inputDiffers = String(planInput || "").trim() !== String(loadedPlanId || "").trim();
  /* CR19 unified write permission: loaded object + error + stale + in-flight
   * + version + expiry. Load failure, STALE, expired/beyond-grace, switching
   * (loading) or a write in flight disables every write — never just the
   * presence of loadedPlanId. */
  const planVerRaw = plan ? (plan.planVersion != null ? plan.planVersion : plan.plan_version) : null;
  const planVerNum = Number(planVerRaw);
  const versionOk = Number.isInteger(planVerNum) && planVerNum >= 1;
  const beyondGrace = sourceAge != null && sourceAge > 60000;
  let guideExpired = false;
  let planExpired = false;
  try {
    const fmt = (typeof window !== "undefined" && window.HEDGE_FORMAT) ? window.HEDGE_FORMAT : null;
    if (fmt && typeof fmt.isExpired === "function") {
      if (guideRaw) guideExpired = fmt.isExpired(guideRaw, nowMs) === true;
      if (plan) planExpired = fmt.isExpired(plan, nowMs) === true;
    }
  } catch (e) { guideExpired = false; planExpired = false; }
  const expiredView = beyondGrace || guideExpired || planExpired;
  const writeEval = resolveHedgeMonitorWrite({
    loadedPlanId, plan, planErr, monErr,
    stale: showStale, expired: expiredView,
    loading, writing, versionOk,
  });
  const writeDisabled = writeEval.disabled;
  const writeReason = writeEval.reason || "load a plan first";
  const writeTitle = writeDisabled ? writeReason : "";

  return (
    <div data-testid="hedge-monitor">
      <div className="vhead"><span className="kicker">{L("hedge_monitor_kicker")}</span><h1>{L("hedge_monitor_title")}</h1>
        <div className="meta">{loadedPlanId || planInput} · <span data-testid="hedge-monitor-status">{status}</span>{degraded ? " · DEGRADED" : ""}</div></div>
      <div className="scanbar sl-filters">
        <input className="pname" aria-label="plan id" placeholder="planId" value={planInput} onChange={(e) => setPlanInput(e.target.value)} />
        <button className="cta" onClick={() => load(planInput)}>{L("sl_retry")}</button>
        <button className="chip" disabled={writeDisabled} title={writeTitle} onClick={doActivate}>{L("hedge_activate_btn")}</button>
        <button className="chip" disabled={writeDisabled} title={writeTitle} onClick={doClose}>{L("hedge_close_btn")}</button>
      </div>
      {inputDiffers && loadedPlanId && (
        <div className="provline" data-testid="hedge-monitor-input-differs">
          <span className="tag">INPUT</span><span>input differs from loaded plan — writes stay on {loadedPlanId}</span>
        </div>
      )}
      {showStale && (
        <div className="provline" data-testid={staleByAge ? "hedge-monitor-stale" : "hedge-monitor-kept-stale"} role="alert">
          <span className="tag hot">STALE</span><span>{L("hedge_stale_kept")}</span>
          {sourceAge != null && <span data-testid="hedge-monitor-source-age">sourceAge {Math.round(sourceAge / 1000)}s</span>}
          {(planErr || monErr) && <span className="sd-err">{String(planErr || monErr).slice(0, 200)}</span>}
        </div>
      )}
      {expiredView && !showStale && (
        <div className="provline" data-testid="hedge-monitor-expired" role="alert">
          <span className="tag hot">EXPIRED</span><span>expired — writes paused until fresh data loads</span>
        </div>
      )}
      {!showStale && sourceAge != null && (
        <div className="provline" data-testid="hedge-monitor-source-age-wrap">
          <span data-testid="hedge-monitor-source-age">sourceAge {Math.round(sourceAge / 1000)}s</span>
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
          <div className="stat"><span className="k">Actual funding</span>
            <span className="v" data-testid="hedge-monitor-actual-funding">{actualReceipts != null ? window.HEDGE_FORMAT.fmtUsd(actualReceipts) : "—"}</span></div>
          {knownSubtotal != null && (
            <div className="stat"><span className="k">Known funding subtotal{fundingPartial ? " · PARTIAL" : ""}</span>
              <span className="v" data-testid="hedge-monitor-known-subtotal">{window.HEDGE_FORMAT.fmtUsd(knownSubtotal)}{fundingCoverage ? ` · ${fundingCoverage}` : ""}</span></div>
          )}
          <div className="stat"><span className="k">{L("hedge_projected_next")}</span>
            <span className="v" data-testid="hedge-monitor-projected-next">{projNext != null ? window.HEDGE_FORMAT.fmtUsd(projNext) : "—"}</span></div>
          <div className="gcap">{L("hedge_projected_note")}</div>
          <div className="stat"><span className="k">{L("hedge_basis_pnl")}</span><span className="v">{basisPnl != null ? window.HEDGE_FORMAT.fmtUsd(basisPnl) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_spot_pnl")}</span><span className="v">{spotPnl != null ? window.HEDGE_FORMAT.fmtUsd(spotPnl) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_futures_pnl")}</span><span className="v">{futPnl != null ? window.HEDGE_FORMAT.fmtUsd(futPnl) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_known_fees")}</span><span className="v">{knownFees != null ? window.HEDGE_FORMAT.fmtUsd(knownFees) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_estimated_exit")}</span><span className="v">{estExit != null ? window.HEDGE_FORMAT.fmtUsd(estExit) : "—"}</span></div>
          <div className="stat"><span className="k">{L("hedge_net_before")}{fundingPartial ? " · PARTIAL" : ""}</span>
            <span className="v" data-testid="hedge-monitor-net-before">{netBefore != null && !fundingPartial ? window.HEDGE_FORMAT.fmtUsd(netBefore) : (knownSubtotal != null ? `${window.HEDGE_FORMAT.fmtUsd(knownSubtotal)} · PARTIAL` : "—")}</span></div>
          <div className="stat"><span className="k">{L("hedge_net_after")}</span><span className="v">{netAfter != null ? window.HEDGE_FORMAT.fmtUsd(netAfter) : "—"}</span></div>
        </div></div>

      <div className="panel" data-testid="hedge-monitor-protection">
        <div className="ph"><span className="tick">▸</span>Protection</div>
        <div className="pb"><div className="reason" data-testid="hedge-monitor-protection-status">{String(protectionStatus)}</div></div>
      </div>

      {(guideFut || guideSpot || exitErr || guideRaw) && (
        <div className="panel" data-testid="hedge-monitor-exit-guidance">
          <div className="ph"><span className="tick">▸</span>Exit guidance</div>
          <div className="pb">
            {exitErr && <div className="reason">{String(exitErr).slice(0, 200)}</div>}
            {guideFut && <div className="reason" data-testid="hedge-monitor-exit-futures">FUTURES_SHORT · {JSON.stringify(guideFut).slice(0, 300)}</div>}
            {guideSpot && <div className="reason" data-testid="hedge-monitor-exit-spot">SPOT_LONG · {JSON.stringify(guideSpot).slice(0, 300)}</div>}
            {!guideFut && !guideSpot && guideRaw && <div className="reason">{JSON.stringify(guideRaw).slice(0, 300)}</div>}
          </div>
        </div>
      )}

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
            <input className="pname" aria-label="fee currency" placeholder="feeCurrency (frozen legs default)" value={legForm.feeCurrency} onChange={(e) => setLegForm((f) => ({ ...f, feeCurrency: e.target.value }))} />
            <input className="pname" aria-label="fee amount" placeholder="feeAmount (empty=unknown, 0 explicit)" inputMode="decimal" value={legForm.feeAmount} onChange={(e) => setLegForm((f) => ({ ...f, feeAmount: e.target.value }))} />
            <input className="pname" aria-label="gas usd" placeholder="gasUsd (empty=unknown)" inputMode="decimal" value={legForm.gasUsd} onChange={(e) => setLegForm((f) => ({ ...f, gasUsd: e.target.value }))} />
            <input className="pname" aria-label="executed timestamp" placeholder="实际成交 UTC 时间戳（毫秒，默认现在）" inputMode="numeric" value={legForm.executedAtMs} onChange={(e) => setLegForm((f) => ({ ...f, executedAtMs: e.target.value }))} />
            {legForm.eventType === "FUNDING_RECEIPT" && <input className="pname" aria-label="funding amount" placeholder="实际收到资金费" value={legForm.amount} onChange={(e) => setLegForm((f) => ({ ...f, amount: e.target.value }))} />}
            <input className="pname" aria-label="client event id" placeholder="clientEventId" value={legForm.clientEventId} onChange={(e) => setLegForm((f) => ({ ...f, clientEventId: e.target.value }))} />
            <input className="pname" aria-label="expected version" placeholder="expectedPlanVersion" inputMode="numeric" value={legForm.expectedPlanVersion} onChange={(e) => setLegForm((f) => ({ ...f, expectedPlanVersion: e.target.value }))} />
            <button className="cta" disabled={writeDisabled} title={writeTitle} onClick={doLeg}>{L("hedge_apply_leg_btn")}</button>
          </div>
          <div className="gcap">{L("hedge_qty_string_hint")}</div>
          {legMsg && <div className="reason" data-testid="hedge-leg-msg">{String(legMsg).slice(0, 400)}</div>}
        </div></div>
    </div>
  );
}
