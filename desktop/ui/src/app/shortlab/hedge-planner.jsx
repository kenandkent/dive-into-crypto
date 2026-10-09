function normalizeHedgeSimulation(response) {
  if (!response || !response.result) return response;
  return { ...response.result, ...response };
}

function buildHedgeSimulationBody(form) {
    const b = {
      symbol: String(form.symbol || "").toUpperCase().trim(),
      mode: form.mode,
      futuresNotionalUsd: String(form.futuresNotionalUsd || "").trim(),
      preferredSpotVenue: form.preferredSpotVenue,
      futuresLeverage: String(form.futuresLeverage || "").trim(),
      marginMode: form.marginMode,
      marginUsd: String(form.marginUsd || "").trim(),
    };
    if (form.mode === "RELATIVE") {
      const hasRatio = String(form.hedgeRatio || "").trim() !== "";
      const hasBudget = String(form.stressUpPct || "").trim() !== "" || String(form.maxDirectionalLossUsd || "").trim() !== "";
      if (hasRatio && hasBudget) return { error: "HEDGE_RATIO_INVALID · ratio and risk budget are mutually exclusive (422)" };
      if (hasRatio) b.hedgeRatio = String(form.hedgeRatio || "").trim();
      else if (hasBudget) {
        b.stressUpPct = String(form.stressUpPct || "").trim();
        b.maxDirectionalLossUsd = String(form.maxDirectionalLossUsd || "").trim();
      } else {
        b.hedgeRatio = String(form.hedgeRatio || "").trim();
      }
    }
    if (String(form.liquidationPrice || "").trim() !== "") {
      b.liquidationPrice = String(form.liquidationPrice || "").trim();
      b.liquidationPriceSource = form.liquidationPriceSource || "USER_EXCHANGE";
      b.liquidationPriceUpdatedAtMs = Date.now();
    } else {
      b.liquidationPriceSource = form.liquidationPriceSource || "NONE";
    }
    b.stopPolicy = form.stopPolicy;
    if (String(form.stopTriggerPrice || "").trim() !== "") b.stopTriggerPrice = String(form.stopTriggerPrice || "").trim();
    if (String(form.plannedHoldDays || "").trim() !== "") b.plannedHoldDays = Number(form.plannedHoldDays);
    return { body: b };
}


/* ============================================================================
   short-lab — Desktop · Hedge Planner page (H09 · design B15/B16/B20/B32.3)

   Reads ONLY window.DIVE.hedgeVenues / hedgeSimulate / getHedgeSimulation /
   createHedgePlan (data.js). Never fetches Binance/Alpha/on-chain directly.
   Layout mirrors B34.2: Asset Summary · Planning state/Readiness/Risk
   validation · Funding · Venue Quotes · Mode · Position/Risk Inputs · Result ·
   Stress · Cost/Break-even · Manual Execution Guide · Warnings.

   Copy rules (B35): Target vs Actual vs Estimated vs Confirmed vs Reference
   vs User-entered stay distinct (hedge_* i18n keys). No-liq-price or chain
   indicative still renders DRAFT + LIMITED with a补项 guide — never hides the
   asset. Expired simulations stay readable; re-simulation mints a new ID;
   POST plan with an expired simulation surfaces QUOTE_EXPIRED. Quantity
   strings pass through verbatim (HEDGE_FORMAT.fmtQty, never Number).
   AbortController + sequence rejects late responses; 503/offline render
   honest copy, never mock. `initial` bypasses fetch for tests.
   ========================================================================== */

function HedgePlannerDefaults() {
  return {
    symbol: "BTCUSDT", mode: "ABSOLUTE",
    futuresNotionalUsd: "10000", hedgeRatio: "1",
    stressUpPct: "", maxDirectionalLossUsd: "",
    preferredSpotVenue: "AUTO",
    futuresLeverage: "1", marginMode: "ISOLATED", marginUsd: "10000",
    liquidationPrice: "", liquidationPriceSource: "NONE",
    stopPolicy: "ALERT_ONLY", stopTriggerPrice: "",
    plannedHoldDays: "",
  };
}

function HedgePlanner({ initial, onViewPlan, onGotoMonitor, candidate, fundingSelection, initialSymbol, initialSnapshotId }) {
  const [form, setForm] = React.useState(() => {
    const base = HedgePlannerDefaults();
    /* R13b: prefill routed symbol/snapshotId (funding carry or directional/
       balanced candidate via workflow-bindings hash) without guessing. */
    try {
      const candSym = (candidate && (candidate.symbol || candidate.Symbol))
        || (fundingSelection && (fundingSelection.symbol || fundingSelection.Symbol))
        || initialSymbol || (initial && (initial.symbol || initial.candidateSymbol));
      if (candSym && String(candSym).trim() !== "") base.symbol = String(candSym).toUpperCase().trim();
    } catch (e) { /* keep default; routed hash effect below still applies */ }
    return base;
  });
  const [routedSnapshotId, setRoutedSnapshotId] = React.useState(() => {
    try {
      const s = (candidate && (candidate.snapshotId != null ? candidate.snapshotId : candidate.snapshot_id))
        || (fundingSelection && (fundingSelection.snapshotId != null ? fundingSelection.snapshotId : fundingSelection.snapshot_id))
        || initialSnapshotId || (initial && (initial.snapshotId != null ? initial.snapshotId : initial.snapshot_id));
      return s != null ? String(s) : null;
    } catch (e) { return null; }
  });
  const [venues, setVenues] = React.useState(initial && initial.venues ? initial.venues : null);
  const [venuesErr, setVenuesErr] = React.useState(initial && initial.venuesError ? initial.venuesError : null);
  const [sim, setSim] = React.useState(initial && initial.simulation ? normalizeHedgeSimulation(initial.simulation) : null);
  const [simErr, setSimErr] = React.useState(initial && initial.simulationError ? initial.simulationError : null);
  const [plan, setPlan] = React.useState(initial && initial.plan ? initial.plan : null);
  const [planErr, setPlanErr] = React.useState(initial && initial.planError ? initial.planError : null);
  const [busy, setBusy] = React.useState(false);
  /* R13a: freeze the inputs that produced the current sim; any later edit
     makes the old sim STALE (D12.1). Initial fixtures have no fingerprint. */
  const [simInputs, setSimInputs] = React.useState(null);
  const seqRef = React.useRef(0);
  const abortRef = React.useRef(null);

  /* R13b: adopt routed planner pair from hash when no explicit candidate prop
     (funding analyze → planner via workflow-bindings shortlabPlannerSelection). */
  React.useEffect(() => {
    if ((candidate && candidate.symbol) || (fundingSelection && fundingSelection.symbol) || initialSymbol) return;
    let sel = null;
    try {
      if (typeof shortlabPlannerSelectionFromHash === "function") sel = shortlabPlannerSelectionFromHash();
      else if (typeof window !== "undefined" && window.WORKFLOW_BINDINGS && typeof window.WORKFLOW_BINDINGS.shortlabPlannerSelectionFromHash === "function") sel = window.WORKFLOW_BINDINGS.shortlabPlannerSelectionFromHash();
    } catch (e) { sel = null; }
    if (sel && sel.symbol && String(sel.symbol).trim() !== "") {
      const sym = String(sel.symbol).toUpperCase().trim();
      setForm((f) => (f.symbol === sym ? f : { ...f, symbol: sym }));
      if (sel.snapshotId != null) setRoutedSnapshotId(String(sel.snapshotId));
    }
  }, []);

  const set = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const expired = sim ? window.HEDGE_FORMAT.isExpired(sim) : false;
  const fingerprintForm = (f) => {
    const o = f || {};
    return JSON.stringify([
      String(o.symbol || "").toUpperCase(), o.mode,
      String(o.futuresNotionalUsd || ""), String(o.hedgeRatio || ""),
      String(o.stressUpPct || ""), String(o.maxDirectionalLossUsd || ""),
      o.preferredSpotVenue, String(o.futuresLeverage || ""), o.marginMode,
      String(o.marginUsd || ""), String(o.liquidationPrice || ""),
      o.liquidationPriceSource, o.stopPolicy, String(o.stopTriggerPrice || ""),
      String(o.plannedHoldDays || ""),
    ]);
  };
  const isSimStale = !!(sim && simInputs && fingerprintForm(form) !== fingerprintForm(simInputs));

  const doVenues = () => {
    if (initial && (initial.venues || initial.venuesError)) return;
    const sym = String(form.symbol || "").toUpperCase().trim();
    if (!sym) { setVenuesErr("hedgeVenues: empty symbol"); return; }
    const seq = ++seqRef.current;
    if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} }
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    abortRef.current = ctrl;
    setVenuesErr(null);
    const notional = Number(form.futuresNotionalUsd);
    const q = isFinite(notional) && notional > 0 ? { notionalUsd: notional } : {};
    window.DIVE.hedgeVenues(sym, q, ctrl ? { signal: ctrl.signal } : {})
      .then((res) => { if (seq === seqRef.current) setVenues(res); })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current) return;
        setVenuesErr((e && e.message) || String(e));
      });
  };

  const buildSimulateBody = () => buildHedgeSimulationBody(form);

  const doSimulate = () => {
    if (busy) return;
    const built = buildSimulateBody();
    if (built.error) { setSimErr(built.error); return; }
    const seq = ++seqRef.current;
    if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} }
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    abortRef.current = ctrl;
    setBusy(true); setSimErr(null); setPlanErr(null);
    const frozen = { ...form };
    window.DIVE.hedgeSimulate(built.body, ctrl ? { signal: ctrl.signal } : {})
      .then((res) => {
        if (seq !== seqRef.current) return;
        setSim(normalizeHedgeSimulation(res)); setBusy(false); setPlan(null);
        setSimInputs(frozen);
      })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current) return;
        setBusy(false);
        setSimErr((e && e.message) || String(e));
      });
  };

  const doReloadSim = () => {
    const id = sim && (sim.simulationId || sim.simulation_id);
    if (!id) return;
    const seq = ++seqRef.current;
    window.DIVE.getHedgeSimulation(id)
      .then((res) => { if (seq === seqRef.current) setSim(normalizeHedgeSimulation(res)); })
      .catch((e) => { if (seq === seqRef.current) setSimErr((e && e.message) || String(e)); });
  };

  const doCreatePlan = () => {
    const sid = sim && (sim.simulationId || sim.simulation_id);
    if (!sid) { setPlanErr("no simulation to freeze"); return; }
    /* R13b: real Gate/quote shape assertion before freezing (no stale/expired
       entry). Uses workflow-bindings assertFreshSimulation/assertQuoteShape
       when present; expired or malformed quotes block save with an honest
       error instead of a Fake plan. */
    try {
      const wf = (typeof window !== "undefined" && window.WORKFLOW_BINDINGS) || null;
      const assertFresh = (typeof assertFreshSimulation === "function") ? assertFreshSimulation
        : (wf && typeof wf.assertFreshSimulation === "function" ? wf.assertFreshSimulation : null);
      if (assertFresh && sim) assertFresh(sim, Date.now());
      const assertQ = (typeof assertQuoteShape === "function") ? assertQuoteShape
        : (wf && typeof wf.assertQuoteShape === "function" ? wf.assertQuoteShape : null);
      if (assertQ && Array.isArray(venueList) && venueList.length > 0) {
        // At least one venue quote must be fresh; stale quotes never grant entry.
        let fresh = 0;
        for (const q of venueList) { try { assertQ(q, Date.now()); fresh += 1; } catch (e) {} }
        if (fresh === 0) assertQ(venueList[0], Date.now());
      }
    } catch (e) {
      setPlanErr((e && e.message) || String(e));
      return;
    }
    const seq = ++seqRef.current;
    setBusy(true); setPlanErr(null);
    const clientRequestId = `plan-${sid}-${Date.now().toString(36)}`;
    window.DIVE.createHedgePlan({ simulationId: sid, clientRequestId }, {})
      .then((res) => { if (seq === seqRef.current) { setPlan(res.plan || res); setBusy(false); } })
      .catch((e) => {
        if (seq !== seqRef.current) return;
        setBusy(false);
        setPlanErr((e && e.message) || String(e));
      });
  };

  /* R13b: click-suggestion refresh entry point — re-reads the frozen decision
     plus fresh venue quotes via workflow-bindings refreshDecisionAndQuotes
     (real adapters, asserted shapes, superseded responses ignored). Planner
     calls this before trusting a routed snapshotId for entry. */
  const doRefreshGateQuotes = (decisionId) => {
    const did = String(decisionId || "").trim();
    const sym = String(form.symbol || "").toUpperCase().trim();
    if (!did || !sym) return Promise.reject(new Error("refreshDecisionAndQuotes: missing decisionId/symbol"));
    try {
      const wf = (typeof window !== "undefined" && window.WORKFLOW_BINDINGS) || null;
      const factory = (typeof createWorkflowBindings === "function") ? createWorkflowBindings
        : (wf && typeof wf.createWorkflowBindings === "function" ? wf.createWorkflowBindings : null);
      if (!factory) return Promise.reject(new Error("refreshDecisionAndQuotes: workflow bindings unavailable"));
      const bindings = factory({ dive: window.DIVE, navigate: (h) => h, nowMs: () => Date.now() });
      return bindings.refreshDecisionAndQuotes(did, sym, {});
    } catch (e) {
      return Promise.reject(e);
    }
  };

  React.useEffect(() => () => { if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} } }, []);

  const venueList = (venues && (venues.venues || venues.items || venues.quotes)) || [];
  const simId = sim && (sim.simulationId || sim.simulation_id);
  const readiness = sim && (sim.readiness || sim.Readiness);
  const riskValidation = sim && (sim.riskValidation || sim.risk_validation);
  const monitoringCapability = sim && (sim.monitoringCapability || sim.monitoring_capability);
  const targetQty = sim && (sim.targetSpotQty || sim.target_spot_qty);
  const futQty = sim && (sim.canonicalFuturesQty || sim.canonical_futures_qty || sim.futuresContractQty || sim.futures_contract_qty);
  const guides = (sim && (sim.orderGuidance || sim.order_guidance)) || [];
  const stresses = (sim && (sim.stressScenarios || sim.stress_scenarios)) || [];
  const noLiq = !form.liquidationPrice || String(form.liquidationPrice).trim() === "" || (form.liquidationPriceSource || "NONE") === "NONE";
  const showLimitedGuide = sim && (monitoringCapability === "LIMITED" || riskValidation === "LIMITED" || riskValidation === "UNKNOWN" || noLiq);
  const stopUnsupported = sim ? window.HEDGE_FORMAT.needsStopCopy({ ...sim, stopPolicy: form.stopPolicy, monitoringCapability }) : false;

  return (
    <div data-testid="hedge-planner">
      <div className="vhead"><span className="kicker">{L("hedge_planner_kicker")}</span><h1>{L("hedge_planner_title")}</h1>
        <div className="meta">{L("hedge_planner_meta")}</div></div>

      <div className="panel"><div className="ph"><span className="tick">▸</span>{L("hedge_inputs_title")}</div>
        <div className="pb">
          <div className="scanbar sl-filters">
            <input className="pname" aria-label="symbol" placeholder="SYMBOL" value={form.symbol} onChange={(e) => set("symbol", e.target.value)} />
            <select aria-label="mode" value={form.mode} onChange={(e) => set("mode", e.target.value)}>
              <option value="ABSOLUTE">ABSOLUTE</option>
              <option value="RELATIVE">RELATIVE</option>
            </select>
            <input className="pname" aria-label="futures notional" placeholder="futures notional USD" inputMode="decimal" value={form.futuresNotionalUsd} onChange={(e) => set("futuresNotionalUsd", e.target.value)} />
            {form.mode === "RELATIVE" && (
              <input className="pname" aria-label="hedge ratio" placeholder="hedge ratio 0..1" inputMode="decimal" value={form.hedgeRatio} onChange={(e) => set("hedgeRatio", e.target.value)} />
            )}
            <select aria-label="venue" value={form.preferredSpotVenue} onChange={(e) => set("preferredSpotVenue", e.target.value)}>
              {["AUTO", "BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"].map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            <input className="pname" aria-label="liquidation price" placeholder={L("hedge_ph_liq")} inputMode="decimal" value={form.liquidationPrice} onChange={(e) => set("liquidationPrice", e.target.value)} />
            <select aria-label="liq source" value={form.liquidationPriceSource} onChange={(e) => set("liquidationPriceSource", e.target.value)}>
              {["NONE", "USER_EXCHANGE", "ESTIMATED"].map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
            <select aria-label="stop policy" value={form.stopPolicy} onChange={(e) => set("stopPolicy", e.target.value)}>
              <option value="ALERT_ONLY">ALERT_ONLY</option>
              <option value="USER_PLATFORM_ORDERS">USER_PLATFORM_ORDERS</option>
            </select>
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button className="cta" onClick={doVenues}>{L("hedge_load_venues")}</button>
            <button className="cta" disabled={busy} onClick={doSimulate}>{busy ? L("hedge_simulating") : L("hedge_simulate_btn")}</button>
            {simId && <button className="chip" onClick={doReloadSim}>{L("hedge_reload_sim")}</button>}
          </div>
          {form.mode === "RELATIVE" && (
            <div className="gcap">{L("hedge_relative_hint")}</div>
          )}
        </div></div>

      {venuesErr && (
        <div className="state src-down" role="alert" data-testid="hedge-venues-error">
          <div className="sd-title">VENUES {L("sl_error_title")}</div>
          <div className="sd-body">{/503/.test(String(venuesErr)) ? L("hedge_venues_unavailable_body") : L("hedge_venues_error_body")}</div>
          <div className="sd-err">{String(venuesErr)}</div>
        </div>
      )}
      {venues && (
        <div className="panel" data-testid="hedge-venues">
          <div className="ph"><span className="tick">▸</span>{L("hedge_venues_title")}
            <span className="rt">{venueList.length} venues</span></div>
          <div className="pb" style={{ padding: 0 }}>
            {venueList.length === 0 && <div className="reason">{L("hedge_venues_empty")}</div>}
            {venueList.map((v, i) => {
              const venue = v.venue || v.Venue || `venue-${i}`;
              const bqty = v.buyExecutableQty != null ? v.buyExecutableQty : v.buy_executable_qty;
              const sqty = v.sellExecutableQty != null ? v.sellExecutableQty : v.sell_executable_qty;
              return (
                <div key={venue + i} className="stat" data-testid={`hedge-venue-${venue}`}>
                  <span className="k">{venue}</span>
                  <span className="v">{L("hedge_reference_buy")} {window.HEDGE_FORMAT.fmtQty(bqty)} · {L("hedge_reference_sell")} {window.HEDGE_FORMAT.fmtQty(sqty)}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {simErr && (
        <div className="state src-down" role="alert" data-testid={/503/.test(String(simErr)) ? "hedge-sim-unavailable" : "hedge-sim-error"}>
          <div className="sd-title">SIMULATION {L("sl_error_title")}</div>
          <div className="sd-body">{L("hedge_sim_error_body")}</div>
          <div className="sd-err">{String(simErr)}</div>
        </div>
      )}

      {sim && (
        <div data-testid="hedge-sim-result">
          <div className="panel"><div className="ph"><span className="tick">▸</span>{L("hedge_result_title")}
            <span className="rt">{simId ? String(simId).slice(0, 18) : ""}</span></div>
            <div className="pb" style={{ padding: 0 }}>
              <div className="stat"><span className="k">{L("hedge_target_ratio")}</span>
                <span className="v" data-testid="hedge-target-ratio">{window.HEDGE_FORMAT.fmtRatio(sim.targetHedgeRatio != null ? sim.targetHedgeRatio : sim.target_hedge_ratio)}</span></div>
              <div className="stat"><span className="k">{L("hedge_target_futures_qty")}</span>
                <span className="v" data-testid="hedge-target-futures-qty">{window.HEDGE_FORMAT.fmtQty(futQty)}</span></div>
              <div className="stat"><span className="k">{L("hedge_target_spot_qty")}</span>
                <span className="v" data-testid="hedge-target-spot-qty">{window.HEDGE_FORMAT.fmtQty(targetQty)}</span></div>
              <div className="stat"><span className="k">{L("hedge_readiness")}</span>
                <span className="v" data-testid="hedge-readiness">{window.HEDGE_FORMAT.readinessLabel(readiness)}</span></div>
              <div className="stat"><span className="k">{L("hedge_risk_validation")}</span>
                <span className="v" data-testid="hedge-risk-validation">{window.HEDGE_FORMAT.riskLabel(riskValidation)}</span></div>
              <div className="stat"><span className="k">{L("hedge_monitoring_capability")}</span>
                <span className="v" data-testid="hedge-monitoring-capability">{window.HEDGE_FORMAT.capabilityLabel(monitoringCapability)}</span></div>
              <div className="stat"><span className="k">{L("hedge_plan_safety")}</span>
                <span className="v">{sim.planSafetyScore != null ? slFmtScore(sim.planSafetyScore) : (sim.plan_safety_score != null ? slFmtScore(sim.plan_safety_score) : "—")}</span></div>
              <div className="stat"><span className="k">{L("hedge_estimated_cost")}</span>
                <span className="v">{sim.costMetrics ? JSON.stringify(sim.costMetrics).slice(0, 160) : (sim.cost_metrics ? JSON.stringify(sim.cost_metrics).slice(0, 160) : "—")}</span></div>
              <div className="stat"><span className="k">{L("hedge_break_even")}</span>
                <span className="v">{sim.breakEven ? JSON.stringify(sim.breakEven).slice(0, 160) : (sim.break_even ? JSON.stringify(sim.break_even).slice(0, 160) : "—")}</span></div>
            </div></div>

          {isSimStale && (
            <div className="provline" data-testid="hedge-sim-stale" role="alert">
              <span className="tag hot">STALE</span>
              <span>{L("repair_decision_stale_inputs")}</span>
              <button className="chip" onClick={doSimulate}>{L("hedge_resimulate_btn")}</button>
            </div>
          )}
          {expired && (
            <div className="provline" data-testid="hedge-sim-expired" role="alert">
              <span className="tag hot">EXPIRED</span>
              <span>{L("hedge_expired_body")}</span>
              <button className="chip" onClick={doSimulate}>{L("hedge_resimulate_btn")}</button>
            </div>
          )}
          {!expired && simId && (
            <div className="provline" data-testid="hedge-sim-fresh">
              <span className="tag">FRESH</span>
              <span>{L("hedge_quote_generated_at")} {window.HEDGE_FORMAT.fmtMs(sim.generatedAtMs != null ? sim.generatedAtMs : sim.generated_at_ms)} · {L("hedge_quote_expires_at")} {window.HEDGE_FORMAT.fmtMs(window.HEDGE_FORMAT.simExpiresAt(sim))}</span>
            </div>
          )}

          {showLimitedGuide && (
            <div className="panel" data-testid="hedge-limited-guide">
              <div className="ph"><span className="tick">▸</span>DRAFT + LIMITED</div>
              <div className="pb">
                <div className="reason">{noLiq ? L("hedge_no_liq_guide") : L("hedge_indicative_guide")}</div>
                <div className="gcap">{L("hedge_draft_limited_note")}</div>
              </div>
            </div>
          )}

          {guides.length > 0 && (
            <div className="panel" data-testid="hedge-order-guide">
              <div className="ph"><span className="tick">▸</span>{L("hedge_order_guide_title")}</div>
              <div className="pb" style={{ padding: 0 }}>
                {guides.map((g, i) => {
                  const leg = g.leg || g.legType || g.leg_type || `leg-${i}`;
                  const qty = g.quantity || g.qty || g.legalQuantity || g.legal_quantity || g.targetQty;
                  const refPrice = g.referenceLimitPrice != null ? g.referenceLimitPrice : (g.reference_limit_price != null ? g.reference_limit_price : (g.limitPrice != null ? g.limitPrice : g.limit_price));
                  return (
                    <div key={i} className="stat" data-testid={`hedge-order-${leg}-${i}`}>
                      <span className="k">{leg} · {g.side || ""}</span>
                      <span className="v" data-testid={`hedge-order-qty-${i}`}>{window.HEDGE_FORMAT.fmtQty(qty)} @ {L("hedge_reference_price")} {refPrice != null ? String(refPrice) : "—"}</span>
                    </div>
                  );
                })}
                <div className="gcap">{L("hedge_price_reference_only")} · {L("hedge_must_place_manually")}</div>
              </div>
            </div>
          )}

          {stopUnsupported && (
            <div className="reason" data-testid="hedge-no-stop">{L("hedge_no_stop")}</div>
          )}

          {stresses.length > 0 && (
            <div className="panel" data-testid="hedge-stress">
              <div className="ph"><span className="tick">▸</span>STRESS</div>
              <div className="pb" style={{ padding: 0 }}>
                {stresses.map((s, i) => (
                  <div key={i} className="stat">
                    <span className="k">{s.label || s.scenario || s.move || `stress-${i}`}</span>
                    <span className="v">{s.invalidAfterLiquidation || s.INVALID_AFTER_LIQUIDATION ? "INVALID_AFTER_LIQUIDATION" : (s.liqPathUnknown || s.LIQ_PATH_UNKNOWN ? "LIQ_PATH_UNKNOWN" : JSON.stringify(s).slice(0, 140))}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button className="cta" disabled={busy || expired || isSimStale} title={expired ? L("hedge_expired_body") : (isSimStale ? L("repair_decision_stale_inputs") : "")} onClick={doCreatePlan}>{L("hedge_create_plan_btn")}</button>
          </div>
          {planErr && (
            <div className="state src-down" role="alert" data-testid="hedge-plan-error">
              <div className="sd-title">PLAN {L("sl_error_title")}</div>
              <div className="sd-body">{/QUOTE_EXPIRED/.test(String(planErr)) ? L("hedge_quote_expired_body") : L("hedge_plan_error_body")}</div>
              <div className="sd-err">{String(planErr)}</div>
              {/QUOTE_EXPIRED/.test(String(planErr)) && <button className="cta" onClick={doSimulate}>{L("hedge_resimulate_btn")}</button>}
            </div>
          )}
          {plan && (
            <div className="panel" data-testid="hedge-plan-created">
              <div className="ph"><span className="tick">▸</span>{L("hedge_plan_created")}<span className="rt">{plan.planId || plan.plan_id || ""}</span></div>
              <div className="pb"><div className="reason">DRAFT · {L("hedge_draft_limited_note")}</div>
                <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                  <button className="chip" data-testid="planner-view-plan" onClick={() => {
                    if (typeof onViewPlan === "function") { try { onViewPlan(plan); } catch (e) {} return; }
                    try { location.hash = "#/shortlab/plans"; } catch (e) {}
                  }}>{L("repair_decision_view_plan")}</button>
                  <button className="chip" data-testid="planner-goto-monitor" onClick={() => {
                    if (typeof onGotoMonitor === "function") { try { onGotoMonitor(plan); } catch (e) {} return; }
                    try { location.hash = "#/shortlab/monitor"; } catch (e) {}
                  }}>{L("repair_decision_goto_monitor")}</button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
