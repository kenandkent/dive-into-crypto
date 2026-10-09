/* ============================================================================
   R13a — workflow-model.mjs (pure status model, D07/D12.1).
   No React, no HTTP, no localStorage, no L()/DIVE dependency.
   Consumed by decision-panel.jsx / plans-view.jsx via global concatenation
   (build.mjs loads this file before the pages); tests concatenate it first.
   ========================================================================== */

function canSavePairedPlan(decision) {
  if (!decision || typeof decision !== "object") return false;
  const r = decision.recommendation || decision.recommendationCode || decision.rec;
  return r === "PARTIAL_HEDGE" || r === "FULL_HEDGE";
}

function getDecisionActualRatio(decision) {
  if (!decision || typeof decision !== "object") return null;
  const p = decision.selectedProposal != null ? decision.selectedProposal : decision.selected_proposal;
  if (!p || typeof p !== "object") return null;
  const raw = (p.actual_ratio != null ? p.actual_ratio : p.actualRatio);
  if (raw == null || raw === "") return null;
  return raw;
}

function isDecisionExpired(decision, nowMs) {
  if (!decision || typeof decision !== "object") return false;
  if (decision.expired === true) return true;
  const cands = [decision.expiresAtMs, decision.expires_at_ms, decision.expiresAt, decision.expires_at];
  let exp = null;
  for (const c of cands) {
    if (c != null && c !== "" && isFinite(Number(c))) { exp = Number(c); break; }
  }
  if (exp == null) return false;
  const now = (nowMs != null && isFinite(Number(nowMs))) ? Number(nowMs) : Date.now();
  return now >= exp;
}

function _reqField(obj, camel, snake) {
  if (!obj || typeof obj !== "object") return undefined;
  if (obj[camel] !== undefined) return obj[camel];
  if (obj[snake] !== undefined) return obj[snake];
  return undefined;
}

function isDecisionStaleForInputs(decision, currentInputs) {
  if (!decision || typeof decision !== "object") return false;
  if (!currentInputs || typeof currentInputs !== "object") return false;
  const req = decision.request || decision.decisionRequest || decision.decision_request || {};
  const keys = [
    ["symbol", "symbol"],
    ["goal", "goal"],
    ["futuresNotionalUsd", "futures_notional_usd"],
    ["plannedHoldDays", "planned_hold_days"],
    ["availableCapitalUsd", "available_capital_usd"],
    ["maxScenarioLossUsd", "max_scenario_loss_usd"],
    ["marginUsd", "margin_usd"],
    ["liquidationPrice", "liquidation_price"],
    ["preferredSpotVenue", "preferred_spot_venue"],
  ];
  for (const [camel, snake] of keys) {
    const a = _reqField(req, camel, snake);
    const b = _reqField(currentInputs, camel, snake);
    const sa = (a == null ? "" : String(a).trim());
    const sb = (b == null ? "" : String(b).trim());
    if (camel === "symbol" || camel === "goal" || camel === "preferredSpotVenue") {
      if (sa.toUpperCase() !== sb.toUpperCase()) return true;
    } else {
      if (sa !== sb) return true;
    }
  }
  return false;
}

function isDecisionCapabilityBlocked(capability) {
  if (!capability || typeof capability !== "object") return false;
  if (capability.hedgeEnabled === false) return true;
  if (capability.hedge_enabled === false) return true;
  if (capability.enabled === false) return true;
  if (capability.available === false) return true;
  const mc = capability.monitoringCapability != null
    ? capability.monitoringCapability
    : capability.monitoring_capability;
  if (mc != null && String(mc).toUpperCase() === "UNKNOWN") return true;
  const cap2 = capability.capability != null ? capability.capability : capability.status;
  if (cap2 != null && String(cap2).toUpperCase() === "UNKNOWN") return true;
  return false;
}
