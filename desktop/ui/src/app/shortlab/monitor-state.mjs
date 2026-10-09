/* ============================================================================
 * short-lab — Desktop · Monitor pure state helpers (R12 · D12.1)
 *
 * Pure functions only: no fetch, no React, no ledger caching, no module-level
 * mutable state. The component (hedge-monitor.jsx) owns AbortController /
 * timers; this file only computes next state / staleness / display selection
 * so node --test can import it directly.
 *
 * Backend JSON is camelCase with snake_case aliases; every selector accepts
 * both. Quantity strings stay verbatim (never Number). Unknown stays null
 * (never 0, never hardcoded "USDT").
 * ========================================================================== */

export const MONITOR_POLL_MS = 10000;
export const MONITOR_MARK_TTL_MS = 20000;
export const MONITOR_MARK_GRACE_MS = 60000;

export function nextGeneration(gen) {
  const n = Number(gen);
  return (Number.isSafeInteger(n) && n >= 0 ? n : 0) + 1;
}

export function isCurrent(respGen, currentGen) {
  return respGen === currentGen;
}

export function resolveMonitorAsOf(monitor) {
  if (!monitor || typeof monitor !== "object") return null;
  const cands = [
    monitor.asOf,
    monitor.as_of_ms,
    monitor.asOfMs,
    monitor.generatedAtMs,
    monitor.generated_at_ms,
  ];
  for (const c of cands) {
    const n = Number(c);
    if (Number.isSafeInteger(n) && n > 0) return n;
  }
  const meta =
    monitor.sourceMeta || monitor.source_meta || monitor.source_meta_json || monitor.sourceMetaJson;
  if (meta && typeof meta === "object") {
    for (const k of ["known_at_ms", "knownAtMs", "knownAt", "asOf", "as_of_ms"]) {
      const n = Number(meta[k]);
      if (Number.isSafeInteger(n) && n > 0) return n;
    }
    const fut = Number(meta.futures_mark_as_of_ms != null ? meta.futures_mark_as_of_ms : meta.futuresMarkAsOfMs);
    const spot = Number(meta.spot_quote_as_of_ms != null ? meta.spot_quote_as_of_ms : meta.spotQuoteAsOfMs);
    const best = Math.max(
      Number.isSafeInteger(fut) && fut > 0 ? fut : 0,
      Number.isSafeInteger(spot) && spot > 0 ? spot : 0,
    );
    if (best > 0) return best;
  }
  const metrics = monitor.metrics || monitor.metrics_json || monitor.metricsJson;
  if (metrics && typeof metrics === "object") {
    const n = Number(metrics.known_at_ms != null ? metrics.known_at_ms : metrics.knownAtMs);
    if (Number.isSafeInteger(n) && n > 0) return n;
  }
  return null;
}

export function computeSourceAge(nowMs, asOfMs) {
  const now = Number(nowMs);
  const asOf = Number(asOfMs);
  if (!Number.isSafeInteger(now) || now <= 0) return null;
  if (!Number.isSafeInteger(asOf) || asOf <= 0) return null;
  const age = now - asOf;
  if (!Number.isSafeInteger(age)) return null;
  return age < 0 ? 0 : age;
}

export function isStaleSource(sourceAgeMs, ttlMs) {
  const ttl = ttlMs == null ? MONITOR_MARK_TTL_MS : Number(ttlMs);
  const age = Number(sourceAgeMs);
  if (!Number.isSafeInteger(age) || age < 0) return false;
  if (!Number.isSafeInteger(ttl) || ttl < 0) return false;
  return age > ttl;
}

export function isBeyondGrace(sourceAgeMs, graceMs) {
  const grace = graceMs == null ? MONITOR_MARK_GRACE_MS : Number(graceMs);
  const age = Number(sourceAgeMs);
  if (!Number.isSafeInteger(age) || age < 0) return false;
  if (!Number.isSafeInteger(grace) || grace < 0) return false;
  return age > grace;
}

export function shouldPoll({ hidden, loadedPlanId, inFlight }) {
  if (hidden) return false;
  if (inFlight) return false;
  const id = typeof loadedPlanId === "string" ? loadedPlanId.trim() : loadedPlanId;
  if (!id) return false;
  return true;
}

function _firstNonEmptyString(cands) {
  for (const c of cands) {
    if (typeof c === "string" && c.trim() !== "") return c.trim();
  }
  return null;
}

function _legList(plan) {
  if (!plan || typeof plan !== "object") return [];
  for (const k of ["legs", "positions", "actualLegs", "actual_legs", "frozenLegs", "frozen_legs"]) {
    const v = plan[k];
    if (Array.isArray(v)) return v;
  }
  return [];
}

/* Fee/price currency comes from the frozen legs only. Unknown stays null —
 * never hardcoded "USDT" (USDC legs must not be rewritten to USDT). */
export function resolveFeeCurrency(plan, fallback) {
  const fb = typeof fallback === "string" && fallback.trim() !== "" ? fallback.trim() : null;
  const legs = _legList(plan);
  for (const leg of legs) {
    if (!leg || typeof leg !== "object") continue;
    const cur = _firstNonEmptyString([
      leg.feeCurrency,
      leg.fee_currency,
      leg.priceCurrency,
      leg.price_currency,
      leg.currency,
      leg.quoteAsset,
      leg.quote_asset,
      leg.settlementCurrency,
      leg.settlement_currency,
    ]);
    if (cur) return cur;
  }
  const top = plan && typeof plan === "object"
    ? _firstNonEmptyString([
      plan.feeCurrency,
      plan.fee_currency,
      plan.priceCurrency,
      plan.price_currency,
      plan.settlementCurrency,
      plan.settlement_currency,
      plan.quoteAsset,
      plan.quote_asset,
    ])
    : null;
  if (top) return top;
  return fb;
}

export function resolveLegCurrency(plan, legType, fallback) {
  const fb = typeof fallback === "string" && fallback.trim() !== "" ? fallback.trim() : null;
  const legs = _legList(plan);
  const want = String(legType || "").toUpperCase();
  const pool = want
    ? legs.filter((l) => l && String(l.legType || l.leg_type || "").toUpperCase() === want)
    : legs;
  for (const leg of pool) {
    if (!leg || typeof leg !== "object") continue;
    const cur = _firstNonEmptyString([
      leg.feeCurrency,
      leg.fee_currency,
      leg.priceCurrency,
      leg.price_currency,
      leg.currency,
      leg.quoteAsset,
      leg.quote_asset,
      leg.settlementCurrency,
      leg.settlement_currency,
    ]);
    if (cur) return cur;
  }
  if (want) return resolveFeeCurrency(plan, fb);
  return fb;
}

function _pick(obj, keys) {
  if (!obj || typeof obj !== "object") return undefined;
  for (const k of keys) {
    if (obj[k] !== undefined && obj[k] !== null) return obj[k];
  }
  return undefined;
}

/* Actual vs estimated funding. Estimated (complete total) is null on PARTIAL —
 * callers must render knownSubtotal + PARTIAL instead of 0. Actual user
 * receipts stay independent and are never added to the estimate. */
export function selectFundingDisplay(monitor) {
  if (!monitor || typeof monitor !== "object") {
    return { actual: null, estimated: null, projected: null, knownSubtotal: null, coverage: null, complete: false, partial: false };
  }
  const metrics = monitor.metrics || monitor.metrics_json || monitor.metricsJson || {};
  const estimated = _pick(monitor, ["estimatedSettledFundingUsd", "estimated_settled_funding_usd"]);
  const projected = _pick(monitor, ["projectedNextFundingUsd", "projected_next_funding_usd"]);
  let knownSubtotal = _pick(metrics, ["funding_known_subtotal_usd", "fundingKnownSubtotalUsd"]);
  if (knownSubtotal === undefined) {
    knownSubtotal = _pick(monitor, ["fundingKnownSubtotalUsd", "funding_known_subtotal_usd"]);
  }
  let actual = _pick(monitor, ["actualFundingReceiptsUsd", "actual_funding_receipts_usd"]);
  if (actual === undefined) actual = _pick(metrics, ["actual_funding_receipts_usd", "actualFundingReceiptsUsd"]);
  if (actual !== null && typeof actual === "object" && !Array.isArray(actual)) {
    const tot = _pick(actual, ["totalUsd", "total_usd", "total", "usd", "value"]);
    actual = tot !== undefined ? tot : null;
  }
  const coverage = _pick(metrics, ["funding_coverage", "fundingCoverage"])
    ?? _pick(monitor, ["fundingCoverage", "funding_coverage"])
    ?? null;
  let complete = metrics.funding_complete ?? metrics.fundingComplete;
  if (complete === undefined) complete = monitor.fundingComplete ?? monitor.funding_complete;
  if (typeof complete !== "boolean") {
    complete = estimated !== null && estimated !== undefined;
  }
  const flags = metrics.funding_flags || metrics.fundingFlags || [];
  const partialFlag = Array.isArray(flags) && flags.includes("FUNDING_COVERAGE_PARTIAL");
  const partial = partialFlag || (!complete && knownSubtotal !== null && knownSubtotal !== undefined);
  return {
    actual: actual ?? null,
    estimated: estimated ?? null,
    projected: projected ?? null,
    knownSubtotal: knownSubtotal ?? null,
    coverage: coverage ?? null,
    complete: Boolean(complete),
    partial: Boolean(partial),
  };
}

export function selectProtectionStatus(plan, protection) {
  const fromPlan = _pick(plan || {}, ["protectionStatus", "protection_status"]);
  if (typeof fromPlan === "string" && fromPlan.trim() !== "") return fromPlan.trim();
  if (protection && typeof protection === "object") {
    const s = _pick(protection, ["protectionStatus", "protection_status", "status"]);
    if (typeof s === "string" && s.trim() !== "") return s.trim();
    const inner = protection.confirmation || protection.protection;
    if (inner && typeof inner === "object") {
      const s2 = _pick(inner, ["protectionStatus", "protection_status", "status"]);
      if (typeof s2 === "string" && s2.trim() !== "") return s2.trim();
    }
  }
  return "UNKNOWN";
}

/* Bilateral exit guidance: group by FUTURES_SHORT / SPOT_LONG without
 * inventing legs. Returns { futures, spot, raw } where each side is the
 * matched rule object or null. */
export function selectExitGuidance(exitGuidance) {
  if (!exitGuidance || typeof exitGuidance !== "object") {
    return { futures: null, spot: null, raw: exitGuidance ?? null };
  }
  const rules = exitGuidance.rules || exitGuidance.guidance || exitGuidance.legs;
  if (rules && typeof rules === "object" && !Array.isArray(rules)) {
    const futures = rules.FUTURES_SHORT ?? rules.futures_short ?? rules.FUTURES ?? null;
    const spot = rules.SPOT_LONG ?? rules.spot_long ?? rules.SPOT ?? null;
    return { futures: futures ?? null, spot: spot ?? null, raw: exitGuidance };
  }
  if (Array.isArray(rules)) {
    let futures = null;
    let spot = null;
    for (const r of rules) {
      if (!r || typeof r !== "object") continue;
      const leg = String(r.leg || r.legType || r.leg_type || "").toUpperCase();
      if (leg.includes("FUTURE") && !futures) futures = r;
      else if (leg.includes("SPOT") && !spot) spot = r;
    }
    return { futures, spot, raw: exitGuidance };
  }
  return { futures: null, spot: null, raw: exitGuidance };
}

export function createInitialMonitorState(inputPlanId) {
  return {
    inputPlanId: typeof inputPlanId === "string" ? inputPlanId : "",
    loadedPlanId: null,
    generation: 0,
    inFlight: false,
    plan: null,
    monitor: null,
    exitGuidance: null,
    planError: null,
    monitorError: null,
    sourceAgeMs: null,
    stale: false,
  };
}

/* Switching input immediately clears risk display (no A-as-B) and bumps the
 * generation so late A responses are ignored. Writes must use loadedPlanId,
 * which stays null until the new plan succeeds. */
export function beginPlanLoad(state, nextInputId) {
  const base = state && typeof state === "object" ? state : createInitialMonitorState("");
  const gen = nextGeneration(base.generation);
  return {
    ...base,
    inputPlanId: typeof nextInputId === "string" ? nextInputId : base.inputPlanId,
    loadedPlanId: null,
    generation: gen,
    inFlight: true,
    plan: null,
    monitor: null,
    exitGuidance: null,
    planError: null,
    monitorError: null,
    sourceAgeMs: null,
    stale: false,
  };
}

export function applyPlanSuccess(state, gen, planId, plan) {
  const base = state && typeof state === "object" ? state : createInitialMonitorState("");
  if (gen !== base.generation) return base;
  return {
    ...base,
    loadedPlanId: String(planId || "").trim() || null,
    plan: plan ?? null,
    planError: null,
    inFlight: true,
  };
}

export function applyPlanFailure(state, gen, planId, error) {
  const base = state && typeof state === "object" ? state : createInitialMonitorState("");
  if (gen !== base.generation) return base;
  return {
    ...base,
    loadedPlanId: null,
    plan: null,
    monitor: null,
    exitGuidance: null,
    planError: error != null ? String((error && error.message) || error) : "load failed",
    inFlight: false,
    stale: false,
  };
}

export function applyMonitorSuccess(state, gen, planId, mon, nowMs) {
  const base = state && typeof state === "object" ? state : createInitialMonitorState("");
  if (gen !== base.generation) return base;
  if (base.loadedPlanId && String(planId || "").trim() && String(planId).trim() !== base.loadedPlanId) {
    return base;
  }
  const asOf = resolveMonitorAsOf(mon && typeof mon === "object" && mon.monitor ? mon.monitor : mon);
  const body = mon && typeof mon === "object" && mon.monitor ? mon.monitor : mon;
  const age = nowMs != null ? computeSourceAge(nowMs, asOf) : base.sourceAgeMs;
  return {
    ...base,
    monitor: body ?? null,
    monitorError: null,
    inFlight: false,
    sourceAgeMs: age,
    stale: age == null ? false : isStaleSource(age),
  };
}

/* Data errors never write: a failed poll keeps old values and only records
 * the error (caller renders kept values as STALE). */
export function applyMonitorFailure(state, gen, error) {
  const base = state && typeof state === "object" ? state : createInitialMonitorState("");
  if (gen !== base.generation) return base;
  return {
    ...base,
    monitorError: error != null ? String((error && error.message) || error) : "monitor failed",
    inFlight: false,
    stale: true,
  };
}

const MONITOR_STATE_API = {
  MONITOR_POLL_MS,
  MONITOR_MARK_TTL_MS,
  MONITOR_MARK_GRACE_MS,
  nextGeneration,
  isCurrent,
  resolveMonitorAsOf,
  computeSourceAge,
  isStaleSource,
  isBeyondGrace,
  shouldPoll,
  resolveFeeCurrency,
  resolveLegCurrency,
  selectFundingDisplay,
  selectProtectionStatus,
  selectExitGuidance,
  createInitialMonitorState,
  beginPlanLoad,
  applyPlanSuccess,
  applyPlanFailure,
  applyMonitorSuccess,
  applyMonitorFailure,
};

try {
  const G = typeof window !== "undefined" ? window : typeof globalThis !== "undefined" ? globalThis : null;
  if (G) G.MONITOR_STATE = MONITOR_STATE_API;
} catch (e) { /* pure import path keeps working without globals */ }

export default MONITOR_STATE_API;
