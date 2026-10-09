/* ============================================================================
 * R13b — workflow bindings (D07/D12.1, real routing + adapter wiring).
 *
 * Pure helpers + real-DIVE binding factory. No React, no fetch directly, no
 * localStorage, no R13a/R12 source imports: components reference these via
 * typeof/window guards so H09-only bundles still load.
 *
 * Contract: R13a pure components (decision-panel/plans-view), R12 real
 * data.js adapters (hedgeDecisionCreate/Get, hedgeVenues, createHedgePlan,
 * hedgePlans, shortCapabilities), R10a HTTP schema (camelCase bodies verbatim,
 * Decimal strings untouched).
 *
 * NOTE (R07/R10b unmerged): real Decision/Gate assertions below use R00
 * fixture stand-ins (MEME_FULL_VALID / gate_cases.json shapes). They validate
 * structure only and do NOT prove producer computation. Final real-line
 * acceptance is pending R15b (true producers + browser).
 * ========================================================================== */

const WORKFLOW_GOALS = ["CARRY_CAPTURE", "DIRECTIONAL_SHORT", "BALANCED"];
const WORKFLOW_GATE_STATUSES = ["PASS", "FAIL", "UNKNOWN"];
const WORKFLOW_RECOMMENDATIONS = [
  "DATA_INSUFFICIENT",
  "MANUAL_REVIEW",
  "AVOID",
  "NO_HEDGE",
  "PARTIAL_HEDGE",
  "FULL_HEDGE",
];
const WORKFLOW_VALIDATION_LEVEL = "RULE_BASED_UNVALIDATED";

function _str(v) {
  if (v == null) return "";
  try { return String(v); } catch (e) { return ""; }
}

function _nonEmptyStr(v) {
  const s = _str(v).trim();
  return s === "" ? null : s;
}

/* ── routing: planner hash carries symbol + snapshotId (D12.1) ─────────── */
function formatPlannerHash(symbol, snapshotId) {
  const sym = _nonEmptyStr(symbol) || "";
  const snap = snapshotId != null ? _str(snapshotId) : "";
  if (!sym && !snap) return "#/shortlab/planner";
  if (!snap) return "#/shortlab/planner/" + encodeURIComponent(sym);
  return "#/shortlab/planner/" + encodeURIComponent(sym) + "/" + encodeURIComponent(snap);
}

function formatPlansHash() {
  return "#/shortlab/plans";
}

function formatMonitorHash() {
  return "#/shortlab/monitor";
}

function parsePlannerHash(hash) {
  const h = _str(hash == null ? (typeof location !== "undefined" ? location.hash : "") : hash);
  const clean = h.replace(/^#\/?/, "");
  const parts = clean.split("/");
  // ["shortlab","planner","SYM","SNAP..."] — SNAP may itself contain slashes (encoded); rejoin tail.
  if (parts[0] !== "shortlab" || parts[1] !== "planner") return { symbol: null, snapshotId: null };
  let sym = null;
  let snap = null;
  try { sym = parts[2] != null && parts[2] !== "" ? decodeURIComponent(parts[2]) : null; } catch (e) { sym = parts[2] || null; }
  if (parts.length > 3) {
    const tail = parts.slice(3).join("/");
    try { snap = tail !== "" ? decodeURIComponent(tail) : null; } catch (e) { snap = tail || null; }
    if (snap === "") snap = null;
  }
  if (sym === "") sym = null;
  return { symbol: sym, snapshotId: snap };
}

function shortlabPlannerSelectionFromHash(hash) {
  return parsePlannerHash(hash);
}

/* ── candidate extraction (funding carry + directional/balanced) ───────── */
function extractCandidateSymbol(candidate) {
  if (!candidate || typeof candidate !== "object") return null;
  return _nonEmptyStr(candidate.symbol != null ? candidate.symbol : candidate.Symbol);
}

function extractCandidateSnapshotId(candidate) {
  if (!candidate || typeof candidate !== "object") return null;
  const cands = [
    candidate.snapshotId,
    candidate.snapshot_id,
    candidate.snapshotID,
    candidate.fcsSnapshotId,
    candidate.fcs_snapshot_id,
    candidate.fcsSnapshotID,
    candidate.snapshot_id_ms,
    candidate.id,
  ];
  for (const c of cands) {
    if (c != null && _str(c).trim() !== "") return _str(c);
  }
  return null;
}

function normalizeWorkflowGoal(v, fallback) {
  const g = _str(v).toUpperCase().trim();
  if (WORKFLOW_GOALS.includes(g)) return g;
  const fb = _str(fallback).toUpperCase().trim();
  if (WORKFLOW_GOALS.includes(fb)) return fb;
  return "CARRY_CAPTURE";
}

/* Build a POST /hedge/decisions body from either a funding-carry row or a
 * directional/balanced candidate. Symbol + snapshotId always carried;
 * amounts default to the R00 MEME_FULL_VALID stand-in (pending R15b).
 * Bodies stay camelCase with Decimal strings verbatim (never Number). */
function buildDecisionRequestFromCandidate(candidate, overrides) {
  const over = overrides && typeof overrides === "object" ? overrides : {};
  const cand = candidate && typeof candidate === "object" ? candidate : {};
  const symbolRaw = _nonEmptyStr(over.symbol != null ? over.symbol : extractCandidateSymbol(cand));
  if (!symbolRaw) throw new Error("buildDecisionRequestFromCandidate: empty symbol");
  const symbol = symbolRaw.toUpperCase();
  const snapshotId = over.snapshotId != null
    ? _str(over.snapshotId)
    : (over.snapshot_id != null ? _str(over.snapshot_id) : extractCandidateSnapshotId(cand));
  const goal = normalizeWorkflowGoal(
    over.goal != null ? over.goal : (cand.goal != null ? cand.goal : cand.goalCode),
    over.fallbackGoal,
  );
  const pickStr = (v, dflt) => {
    if (v != null && _str(v).trim() !== "") return _str(v).trim();
    return dflt;
  };
  const nowMs = Date.now();
  const request = {
    symbol,
    goal,
    futuresNotionalUsd: pickStr(over.futuresNotionalUsd != null ? over.futuresNotionalUsd : over.futures_notional_usd, "10000"),
    plannedHoldDays: over.plannedHoldDays != null
      ? Number(over.plannedHoldDays)
      : (over.planned_hold_days != null ? Number(over.planned_hold_days) : 30),
    availableCapitalUsd: pickStr(over.availableCapitalUsd != null ? over.availableCapitalUsd : over.available_capital_usd, "25000"),
    maxScenarioLossUsd: pickStr(over.maxScenarioLossUsd != null ? over.maxScenarioLossUsd : over.max_scenario_loss_usd, "1000"),
    marginUsd: pickStr(over.marginUsd != null ? over.marginUsd : over.margin_usd, "12000"),
    liquidationPrice: pickStr(over.liquidationPrice != null ? over.liquidationPrice : over.liquidation_price, "0.025"),
    liquidationPriceUpdatedAtMs: over.liquidationPriceUpdatedAtMs != null
      ? Number(over.liquidationPriceUpdatedAtMs)
      : (over.liquidation_price_updated_at_ms != null ? Number(over.liquidation_price_updated_at_ms) : nowMs - 3600000),
    preferredSpotVenue: pickStr(over.preferredSpotVenue != null ? over.preferredSpotVenue : over.preferred_spot_venue, "AUTO").toUpperCase(),
  };
  const contextRefs = {};
  if (snapshotId != null && snapshotId !== "") contextRefs.fcs = snapshotId;
  if (over.identitySnapshotId != null && _str(over.identitySnapshotId) !== "") contextRefs.identity = _str(over.identitySnapshotId);
  return { symbol, snapshotId: snapshotId != null ? snapshotId : null, goal, request, contextRefs };
}

/* ── stale: input change invalidates the old decision (D12.1) ──────────── */
function _wfReqField(obj, camel, snake) {
  if (!obj || typeof obj !== "object") return undefined;
  if (obj[camel] !== undefined) return obj[camel];
  if (obj[snake] !== undefined) return obj[snake];
  return undefined;
}

function workflowDecisionIsStale(decision, currentInputs) {
  try {
    if (typeof isDecisionStaleForInputs === "function") {
      return !!isDecisionStaleForInputs(decision, currentInputs);
    }
  } catch (e) { /* fall through to local compare */ }
  try {
    if (typeof window !== "undefined" && window && typeof window.isDecisionStaleForInputs === "function") {
      return !!window.isDecisionStaleForInputs(decision, currentInputs);
    }
  } catch (e) { /* local */ }
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
    const a = _wfReqField(req, camel, snake);
    const b = _wfReqField(currentInputs, camel, snake);
    const sa = a == null ? "" : _str(a).trim();
    const sb = b == null ? "" : _str(b).trim();
    if (camel === "symbol" || camel === "goal" || camel === "preferredSpotVenue") {
      if (sa.toUpperCase() !== sb.toUpperCase()) return true;
    } else if (sa !== sb) {
      return true;
    }
  }
  return false;
}

/* ── h0 / savable guard: only PARTIAL/FULL with nonzero actual ratio ───── */
function getWorkflowActualRatio(decision) {
  try {
    if (typeof getDecisionActualRatio === "function") {
      return getDecisionActualRatio(decision);
    }
  } catch (e) { /* local */ }
  try {
    if (typeof window !== "undefined" && window && typeof window.getDecisionActualRatio === "function") {
      return window.getDecisionActualRatio(decision);
    }
  } catch (e) { /* local */ }
  if (!decision || typeof decision !== "object") return null;
  const p = decision.selectedProposal != null ? decision.selectedProposal : decision.selected_proposal;
  if (!p || typeof p !== "object") return null;
  const raw = p.actual_ratio != null ? p.actual_ratio : p.actualRatio;
  if (raw == null || raw === "") return null;
  return _str(raw);
}

function canSaveWorkflowDecision(decision) {
  let base = false;
  try {
    if (typeof canSavePairedPlan === "function") base = !!canSavePairedPlan(decision);
    else if (typeof window !== "undefined" && window && typeof window.canSavePairedPlan === "function") {
      base = !!window.canSavePairedPlan(decision);
    } else {
      const r = decision && (decision.recommendation || decision.recommendationCode || decision.rec);
      base = _str(r).toUpperCase() === "PARTIAL_HEDGE" || _str(r).toUpperCase() === "FULL_HEDGE";
    }
  } catch (e) {
    base = false;
  }
  if (!base) return false;
  // h0 single-leg never saves a two-leg plan even when recommendation slips through.
  const actualRaw = getWorkflowActualRatio(decision);
  if (actualRaw != null && actualRaw !== "") {
    const n = Number(actualRaw);
    if (isFinite(n) && n === 0) return false;
  } else {
    // No proposal at all (h0 / NO_HEDGE shape) is not savable.
    const rec = decision && _str(decision.recommendation || decision.recommendationCode || "").toUpperCase();
    if (rec === "NO_HEDGE") return false;
    if (decision && (decision.selectedProposal == null && decision.selected_proposal == null)) return false;
  }
  const rec2 = decision && _str(decision.recommendation || "").toUpperCase();
  if (rec2 === "NO_HEDGE" || rec2 === "DATA_INSUFFICIENT" || rec2 === "AVOID" || rec2 === "MANUAL_REVIEW") {
    // canSavePairedPlan already forbids these; double-guard for direct callers.
    if (rec2 !== "PARTIAL_HEDGE" && rec2 !== "FULL_HEDGE") return false;
  }
  return true;
}

/* ── shape assertions (R00 stand-in shapes, pending R15b real producers) ── */
function assertGateShape(gate) {
  if (!gate || typeof gate !== "object" || Array.isArray(gate)) {
    throw new Error("assertGateShape: gate must be an object");
  }
  const statusRaw = gate.status != null ? gate.status : (gate.gateStatus != null ? gate.gateStatus : gate.gate_status);
  const status = _str(statusRaw).toUpperCase();
  if (!WORKFLOW_GATE_STATUSES.includes(status)) {
    throw new Error("assertGateShape: unknown status " + _str(statusRaw));
  }
  const reasons = gate.reasons != null ? gate.reasons : [];
  if (!Array.isArray(reasons)) throw new Error("assertGateShape: reasons must be an array");
  for (const r of reasons) {
    if (typeof r !== "string" || r.trim() === "") throw new Error("assertGateShape: reason must be non-empty string");
  }
  const checkedRaw = gate.checkedAtMs != null ? gate.checkedAtMs : (gate.checked_at_ms != null ? gate.checked_at_ms : gate.checkedAt);
  if (checkedRaw != null && checkedRaw !== "") {
    const n = Number(checkedRaw);
    if (!isFinite(n) || n < 0) throw new Error("assertGateShape: checked_at_ms must be finite >=0");
  }
  return true;
}

function assertDecisionShape(decision) {
  const d = decision && typeof decision === "object" && decision.decision && typeof decision.decision === "object"
    ? decision.decision
    : decision;
  if (!d || typeof d !== "object" || Array.isArray(d)) throw new Error("assertDecisionShape: decision must be an object");
  const did = d.decisionId != null ? d.decisionId : d.decision_id;
  if (_str(did).trim() === "") throw new Error("assertDecisionShape: missing decisionId");
  const genRaw = d.generatedAtMs != null ? d.generatedAtMs : d.generated_at_ms;
  const expRaw = d.expiresAtMs != null ? d.expiresAtMs : (d.expires_at_ms != null ? d.expires_at_ms : (d.expiresAt != null ? d.expiresAt : d.expires_at));
  const gen = Number(genRaw);
  const exp = Number(expRaw);
  if (!isFinite(gen) || gen < 0) throw new Error("assertDecisionShape: bad generatedAtMs");
  if (!isFinite(exp) || exp <= gen) throw new Error("assertDecisionShape: expiresAtMs must be > generatedAtMs");
  const rec = _str(d.recommendation || d.recommendationCode || "").toUpperCase();
  if (!WORKFLOW_RECOMMENDATIONS.includes(rec)) throw new Error("assertDecisionShape: unknown recommendation " + _str(d.recommendation));
  const lvl = d.validationLevel != null ? d.validationLevel : d.validation_level;
  if (_str(lvl) !== WORKFLOW_VALIDATION_LEVEL) throw new Error("assertDecisionShape: validationLevel must be RULE_BASED_UNVALIDATED");
  const req = d.request || d.decisionRequest || d.decision_request;
  if (!req || typeof req !== "object") throw new Error("assertDecisionShape: missing request");
  const sym = _wfReqField(req, "symbol", "symbol");
  if (_str(sym).trim() === "") throw new Error("assertDecisionShape: request.symbol empty");
  const goal = _str(_wfReqField(req, "goal", "goal")).toUpperCase();
  if (!WORKFLOW_GOALS.includes(goal)) throw new Error("assertDecisionShape: request.goal unknown");
  // selectedProposal may be null (h0 / insufficient) — when present it must carry ratio strings.
  const sel = d.selectedProposal != null ? d.selectedProposal : d.selected_proposal;
  if (sel != null) {
    if (typeof sel !== "object" || Array.isArray(sel)) throw new Error("assertDecisionShape: selectedProposal must be object|null");
    const ar = sel.actual_ratio != null ? sel.actual_ratio : sel.actualRatio;
    const tr = sel.target_ratio != null ? sel.target_ratio : sel.targetRatio;
    if (ar != null && ar !== "" && !isFinite(Number(ar))) throw new Error("assertDecisionShape: actual_ratio not numeric");
    if (tr != null && tr !== "" && !isFinite(Number(tr))) throw new Error("assertDecisionShape: target_ratio not numeric");
  }
  return true;
}

function _pickExpiry(obj) {
  if (!obj || typeof obj !== "object") return null;
  const cands = [obj.expiresAtMs, obj.expires_at_ms, obj.expiresAt, obj.expires_at];
  for (const c of cands) {
    if (c != null && c !== "" && isFinite(Number(c))) return Number(c);
  }
  return null;
}

function assertQuoteShape(quote, nowMs) {
  if (!quote || typeof quote !== "object" || Array.isArray(quote)) {
    throw new Error("assertQuoteShape: quote must be an object");
  }
  const now = nowMs != null && isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
  const exp = _pickExpiry(quote);
  if (exp == null) throw new Error("assertQuoteShape: missing expiresAtMs");
  if (!(exp > now)) throw new Error("assertQuoteShape: quote expired (expiresAtMs <= nowMs)");
  // Executable quantities, when present, stay verbatim strings (never Number-converted here).
  const qtyKeys = [
    ["buyExecutableQty", "buy_executable_qty"],
    ["sellExecutableQty", "sell_executable_qty"],
    ["buyQty", "buy_qty"],
    ["sellQty", "sell_qty"],
    ["quantity", "qty"],
  ];
  for (const [camel, snake] of qtyKeys) {
    const v = quote[camel] != null ? quote[camel] : quote[snake];
    if (v != null && v !== "" && typeof v !== "string") {
      throw new Error("assertQuoteShape: quantity must stay a decimal string");
    }
  }
  return true;
}

function assertFreshSimulation(sim, nowMs) {
  if (!sim || typeof sim !== "object") throw new Error("assertFreshSimulation: simulation must be an object");
  const sid = sim.simulationId != null ? sim.simulationId : sim.simulation_id;
  if (_str(sid).trim() === "") throw new Error("assertFreshSimulation: missing simulationId");
  return assertQuoteShape(sim, nowMs);
}

function isUnavailableError(err) {
  const s = _str(err && err.message ? err.message : err);
  return /503|UNAVAILABLE|IMPLEMENTATION_UNAVAILABLE|HEDGE_DISABLED|FUNDING_CAPTURE_DISABLED|capabilit/i.test(s);
}

/* ── binding factory over the REAL R12 data.js adapters ────────────────── */
function createWorkflowBindings(opts) {
  const o = opts && typeof opts === "object" ? opts : {};
  const dive = o.dive || (typeof window !== "undefined" && window.DIVE ? window.DIVE : null);
  const navigate = typeof o.navigate === "function"
    ? o.navigate
    : ((hash) => { try { if (typeof location !== "undefined") location.hash = hash; } catch (e) {} return hash; });
  const nowFn = typeof o.nowMs === "function" ? o.nowMs : (() => Date.now());
  if (!dive) throw new Error("createWorkflowBindings: missing dive (real R12 DIVE required)");
  let generation = 0;
  let currentCtl = null;

  function begin() {
    generation += 1;
    if (currentCtl) { try { currentCtl.abort(); } catch (e) {} }
    const ctl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    currentCtl = ctl;
    return { gen: generation, ctl };
  }

  function check(gen) {
    if (gen !== generation) {
      const e = new Error("WORKFLOW_SUPERSEDED · late response ignored (generation guard)");
      e.code = "WORKFLOW_SUPERSEDED";
      throw e;
    }
  }

  function signalFor(ctl, fetchOpts) {
    if (fetchOpts && fetchOpts.signal) return fetchOpts.signal;
    if (ctl && ctl.signal) return ctl.signal;
    return undefined;
  }

  /* Funding research row → planner hash (no HTTP here; planner refreshes Gate/quotes). */
  function onAnalyzeFunding(item) {
    const sym = extractCandidateSymbol(item);
    const snap = extractCandidateSnapshotId(item);
    if (!sym) throw new Error("onAnalyzeFunding: empty symbol");
    const hash = formatPlannerHash(sym, snap);
    navigate(hash);
    return { symbol: sym.toUpperCase(), snapshotId: snap, hash };
  }

  /* Candidate → real POST /hedge/decisions via R12 adapter (fake only stubs HTTP). */
  async function requestDecision(candidate, overrides, fetchOpts) {
    const { gen, ctl } = begin();
    const built = buildDecisionRequestFromCandidate(candidate, overrides);
    const signal = signalFor(ctl, fetchOpts);
    let res;
    try {
      res = await dive.hedgeDecisionCreate(built.request, signal ? { signal } : {});
    } catch (e) {
      // Never fall back to Fake success: 503/unbound stays a throw.
      throw e;
    }
    check(gen);
    const decision = res && res.decision && typeof res.decision === "object" ? res.decision : res;
    assertDecisionShape(decision);
    return { gen, request: built.request, symbol: built.symbol, snapshotId: built.snapshotId, response: res, decision };
  }

  /* Click suggestion refresh: re-read frozen decision + fresh venue quotes, assert shapes. */
  async function refreshDecisionAndQuotes(decisionId, symbol, fetchOpts) {
    const { gen, ctl } = begin();
    const did = _str(decisionId).trim();
    if (!did) throw new Error("refreshDecisionAndQuotes: empty decisionId");
    const sym = _str(symbol).trim().toUpperCase();
    if (!sym) throw new Error("refreshDecisionAndQuotes: empty symbol");
    const signal = signalFor(ctl, fetchOpts);
    const signalObj = signal ? { signal } : {};
    const [decRes, venues] = await Promise.all([
      dive.hedgeDecisionGet(did, signalObj),
      dive.hedgeVenues(sym, signal ? { signal } : {}),
    ]);
    check(gen);
    const decision = decRes && decRes.decision && typeof decRes.decision === "object" ? decRes.decision : decRes;
    assertDecisionShape(decision);
    const list = (venues && (venues.venues || venues.items || venues.quotes)) || [];
    if (!Array.isArray(list) || list.length === 0) throw new Error("refreshDecisionAndQuotes: no venue quotes");
    const now = nowFn();
    // At least one venue quote must be fresh; expired quotes never grant entry.
    let fresh = 0;
    for (const q of list) {
      try { assertQuoteShape(q, now); fresh += 1; } catch (e) { /* stale quote counted, not thrown yet */ }
    }
    if (fresh === 0) {
      // Surface the first quote error so callers see expiry rather than Fake success.
      assertQuoteShape(list[0], now);
    }
    return { gen, decision, venues, freshQuotes: fresh };
  }

  /* Save → verify via real list_plans → monitor (never localStorage). */
  async function savePlanAndNavigate(args, fetchOpts) {
    const a = args && typeof args === "object" ? args : {};
    const decision = a.decision || null;
    if (decision && !canSaveWorkflowDecision(decision)) {
      const e = new Error("WORKFLOW_H0_READ_ONLY · h0/NO_HEDGE never saves a two-leg plan");
      e.code = "WORKFLOW_H0_READ_ONLY";
      throw e;
    }
    const { gen, ctl } = begin();
    const signal = signalFor(ctl, fetchOpts);
    const signalObj = signal ? { signal } : {};
    const simulationId = _str(a.simulationId != null ? a.simulationId : (a.simulation_id != null ? a.simulation_id : "")).trim();
    const decisionId = _str(a.decisionId != null ? a.decisionId : (a.decision_id != null ? a.decision_id : "")).trim();
    if (!simulationId && !decisionId) throw new Error("savePlanAndNavigate: missing simulationId/decisionId");
    const clientRequestId = _str(a.clientRequestId).trim() !== ""
      ? _str(a.clientRequestId)
      : ("plan-" + (simulationId || decisionId) + "-" + _str(nowFn(), 36));
    const body = {};
    if (simulationId) body.simulationId = simulationId;
    if (decisionId) body.decisionId = decisionId;
    body.clientRequestId = clientRequestId;
    let saved;
    try {
      saved = await dive.createHedgePlan(body, signalObj);
    } catch (e) {
      throw e;
    }
    check(gen);
    const plan = saved && saved.plan && typeof saved.plan === "object" ? saved.plan : saved;
    const planId = _str(plan && (plan.planId != null ? plan.planId : plan.plan_id)).trim();
    if (!planId) throw new Error("savePlanAndNavigate: response missing planId (no Fake plan)");
    // Real verification: the saved plan must appear in list_plans (not localStorage).
    let listed = null;
    try {
      listed = await dive.hedgePlans({}, signalObj);
    } catch (e) {
      throw e;
    }
    check(gen);
    const items = (listed && (listed.items || listed.plans)) || [];
    const found = Array.isArray(items) ? items.some((it) => _str(it && (it.planId != null ? it.planId : it.plan_id)) === planId) : false;
    if (!found) throw new Error("savePlanAndNavigate: saved plan missing from real list_plans");
    const hash = formatMonitorHash();
    navigate(hash);
    const navigation = { planId, hash };
    return { gen, plan, planId, response: saved, listed, navigation, hash };
  }

  async function loadPlansAndNavigate(fetchOpts) {
    const { gen, ctl } = begin();
    const signal = signalFor(ctl, fetchOpts);
    const res = await dive.hedgePlans({}, signal ? { signal } : {});
    check(gen);
    const hash = formatPlansHash();
    navigate(hash);
    return { gen, plans: res, hash, navigation: { hash } };
  }

  return {
    get generation() { return generation; },
    onAnalyzeFunding,
    requestDecision,
    refreshDecisionAndQuotes,
    savePlanAndNavigate,
    loadPlansAndNavigate,
  };
}

const WORKFLOW_BINDINGS_API = {
  WORKFLOW_GOALS,
  WORKFLOW_GATE_STATUSES,
  WORKFLOW_RECOMMENDATIONS,
  WORKFLOW_VALIDATION_LEVEL,
  formatPlannerHash,
  formatPlansHash,
  formatMonitorHash,
  parsePlannerHash,
  shortlabPlannerSelectionFromHash,
  extractCandidateSymbol,
  extractCandidateSnapshotId,
  normalizeWorkflowGoal,
  buildDecisionRequestFromCandidate,
  workflowDecisionIsStale,
  canSaveWorkflowDecision,
  getWorkflowActualRatio,
  assertGateShape,
  assertDecisionShape,
  assertQuoteShape,
  assertFreshSimulation,
  isUnavailableError,
  createWorkflowBindings,
};

try {
  const G = typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : null);
  if (G) {
    G.WORKFLOW_BINDINGS = WORKFLOW_BINDINGS_API;
    if (G.formatPlannerHash == null) G.formatPlannerHash = formatPlannerHash;
    if (G.parsePlannerHash == null) G.parsePlannerHash = parsePlannerHash;
    if (G.shortlabPlannerSelectionFromHash == null) G.shortlabPlannerSelectionFromHash = shortlabPlannerSelectionFromHash;
    if (G.buildDecisionRequestFromCandidate == null) G.buildDecisionRequestFromCandidate = buildDecisionRequestFromCandidate;
    if (G.workflowDecisionIsStale == null) G.workflowDecisionIsStale = workflowDecisionIsStale;
    if (G.canSaveWorkflowDecision == null) G.canSaveWorkflowDecision = canSaveWorkflowDecision;
    if (G.assertGateShape == null) G.assertGateShape = assertGateShape;
    if (G.assertDecisionShape == null) G.assertDecisionShape = assertDecisionShape;
    if (G.assertQuoteShape == null) G.assertQuoteShape = assertQuoteShape;
    if (G.createWorkflowBindings == null) G.createWorkflowBindings = createWorkflowBindings;
  }
} catch (e) { /* pure import path keeps working without globals */ }

export {
  WORKFLOW_GOALS,
  WORKFLOW_GATE_STATUSES,
  WORKFLOW_RECOMMENDATIONS,
  WORKFLOW_VALIDATION_LEVEL,
  formatPlannerHash,
  formatPlansHash,
  formatMonitorHash,
  parsePlannerHash,
  shortlabPlannerSelectionFromHash,
  extractCandidateSymbol,
  extractCandidateSnapshotId,
  normalizeWorkflowGoal,
  buildDecisionRequestFromCandidate,
  workflowDecisionIsStale,
  canSaveWorkflowDecision,
  getWorkflowActualRatio,
  assertGateShape,
  assertDecisionShape,
  assertQuoteShape,
  assertFreshSimulation,
  isUnavailableError,
  createWorkflowBindings,
};

export default WORKFLOW_BINDINGS_API;
