/* ============================================================================
   short-lab — Desktop · hedge formatting (H09 · design B32/B34/B35)

   CONTRACT:
   - Backend JSON is camelCase; queries are snake_case aliases (mapped in
     data.js). This file never fetches and never converts quantity strings
     through Number — order quantities travel as decimal strings verbatim
     (e.g. "0.000000000000000001") so tiny values never lose precision.
   - Target vs Actual vs Estimated vs Confirmed vs Reference vs User-entered
     copy stays distinct (B35): this file only formats values; the WORDS
     themselves live in i18n.js (hedge_* keys) so ZH/TR/EN stay complete.
   - DRAFT/LIMITED (plannable,补项引导) vs ACTIVE (real fills) never share a
     label or class. Expired simulations stay readable; re-simulation mints a
     new ID. ACK is acknowledgement, never resolution.
   Pure functions only (no React, no fetch) so node --test can import directly.
   ========================================================================== */

const HEDGE_NULL_TEXT = "—";

function hedgeFinite(v) {
  if (v == null || typeof v === "boolean") return null;
  const n = Number(v);
  return isFinite(n) ? n : null;
}

/* Quantity strings travel verbatim. Strings are returned untouched (no trim
   that could hide user input, no Number, no toFixed). Numbers are stringified
   without precision loss beyond what JS already holds — callers must prefer
   strings for order quantities. null/undefined/"" → null (caller renders "—"). */
function hedgeQtyString(v) {
  if (v == null) return null;
  if (typeof v === "string") {
    if (v === "") return null;
    return v;
  }
  if (typeof v === "number") {
    if (!isFinite(v)) return null;
    return String(v);
  }
  return String(v);
}

/* Display a quantity WITHOUT Number conversion: strings render verbatim so
   "0.000000000000000001" never becomes "1e-18" or "0.00". */
function hedgeFmtQty(v) {
  const s = hedgeQtyString(v);
  return s == null ? HEDGE_NULL_TEXT : s;
}

/* Quantity validity without Number: /^\d+(\.\d+)?$/ and at least one non-zero
   digit. Tiny values like "0.000000000000000001" are valid; "0", "0.0", "" are
   not. Never calls Number so precision is irrelevant to the verdict. */
function hedgeQtyValid(v) {
  if (typeof v !== "string") return false;
  if (!/^\d+(\.\d+)?$/.test(v)) return false;
  return /[1-9]/.test(v);
}

function hedgeFmtRatio(v, digits) {
  const n = hedgeFinite(v);
  if (n == null) return HEDGE_NULL_TEXT;
  const k = digits == null ? 1 : digits;
  return (n * 100).toFixed(k) + "%";
}

function hedgeFmtUsd(v) {
  const n = hedgeFinite(v);
  if (n == null) return HEDGE_NULL_TEXT;
  const sign = n < 0 ? "-" : "+";
  const a = Math.abs(n);
  if (a >= 1e9) return sign + (a / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return sign + (a / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return sign + (a / 1e3).toFixed(2) + "K";
  if (a >= 100) return sign + a.toFixed(0);
  return sign + a.toFixed(2);
}

function hedgeFmtAbsUsd(v) {
  const n = hedgeFinite(v);
  if (n == null) return HEDGE_NULL_TEXT;
  const a = Math.abs(n);
  if (a >= 1e9) return (a / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (a / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return (a / 1e3).toFixed(2) + "K";
  if (a >= 100) return a.toFixed(0);
  return a.toFixed(2);
}

function hedgeFmtMs(ms) {
  const n = hedgeFinite(ms);
  if (n == null) return HEDGE_NULL_TEXT;
  try {
    return new Date(Math.round(n)).toISOString().replace("T", " ").slice(0, 19) + "Z";
  } catch (e) { return HEDGE_NULL_TEXT; }
}

/* Plan lifecycle never collapses DRAFT/LIMITED planning state into ACTIVE. */
function hedgeStatusClass(status) {
  const s = String(status || "").toUpperCase();
  if (s === "READY") return "good";
  if (s === "ACTIVE") return "info";
  if (s === "DRAFT") return "dim";
  if (s === "PARTIALLY_FILLED" || s === "CLOSING") return "hot";
  if (s === "CLOSED") return "dim";
  if (s === "INVALID" || s === "BLOCKED") return "bad";
  return "";
}

function hedgeIsActiveStatus(status) {
  const s = String(status || "").toUpperCase();
  return s === "ACTIVE" || s === "PARTIALLY_FILLED" || s === "CLOSING";
}

function hedgeIsDraftStatus(status) {
  const s = String(status || "").toUpperCase();
  return s === "DRAFT" || s === "READY" || s === "INVALID";
}

function hedgeCapabilityLabel(cap) {
  const c = String(cap || "").toUpperCase();
  if (c === "FULL" || c === "LIMITED" || c === "UNKNOWN") return c;
  return cap == null || cap === "" ? HEDGE_NULL_TEXT : String(cap);
}

function hedgeRiskLabel(risk) {
  const r = String(risk || "").toUpperCase();
  if (r === "VERIFIED" || r === "LIMITED" || r === "UNKNOWN") return r;
  return risk == null || risk === "" ? HEDGE_NULL_TEXT : String(risk);
}

function hedgeReadinessLabel(readiness) {
  const r = String(readiness || "").toUpperCase();
  if (r === "READY" || r === "NOT_READY" || r === "BLOCKED") return r;
  return readiness == null || readiness === "" ? HEDGE_NULL_TEXT : String(readiness);
}

function hedgeAlertSeverityClass(sev) {
  const s = String(sev || "").toUpperCase();
  if (s === "CRITICAL") return "bad";
  if (s === "WARN") return "hot";
  if (s === "INFO") return "";
  return "";
}

function hedgeAlertStateLabel(state) {
  const s = String(state || "").toUpperCase();
  if (s === "OPEN" || s === "ACKNOWLEDGED" || s === "RESOLVED") return s;
  return state == null || state === "" ? HEDGE_NULL_TEXT : String(state);
}

/* Simulation expiry: backend `expired:true` wins; otherwise compare
   expiresAtMs/expires_at_ms/expiresAt against nowMs. Missing expiry → not
   expired (caller shows "unknown", never fabricates expiry). Readable either
   way — expiry never hides the frozen snapshot. */
function hedgeSimExpiresAt(sim) {
  if (!sim || typeof sim !== "object") return null;
  const cands = [sim.expiresAtMs, sim.expires_at_ms, sim.expiresAt, sim.expires_at];
  for (const c of cands) {
    const n = hedgeFinite(c);
    if (n != null) return n;
  }
  return null;
}

function hedgeIsExpired(sim, nowMs) {
  if (!sim || typeof sim !== "object") return false;
  if (sim.expired === true) return true;
  const exp = hedgeSimExpiresAt(sim);
  if (exp == null) return false;
  const now = hedgeFinite(nowMs) != null ? Number(nowMs) : Date.now();
  return now >= exp;
}

/* Orphan-leg predicates are display-only: the backend owns ORPHAN_* codes.
   These helpers only classify already-computed alert/leg shapes for copy. */
function hedgeIsOrphanCode(code) {
  const c = String(code || "").toUpperCase();
  return c === "ORPHAN_FUTURES_LEG" || c === "ORPHAN_SPOT_LEG" ||
    c === "CRITICAL_ORPHAN_FUTURES_LEG" || c === "CRITICAL_ORPHAN_SPOT_LEG" ||
    c === "ORPHAN_LEG_WARNING";
}

function hedgeHasOrphanAlert(alerts) {
  const list = Array.isArray(alerts) ? alerts : [];
  return list.some((a) => a && hedgeIsOrphanCode(a.code || a.Code));
}

function hedgeNeedsStopCopy(simOrPlan) {
  const o = simOrPlan || {};
  const guides = Array.isArray(o.orderGuidance) ? o.orderGuidance :
    Array.isArray(o.order_guidance) ? o.order_guidance : null;
  if (guides) {
    return guides.some((g) => {
      const s = String((g && (g.stopSupport || g.stop_support || g.capability)) || "").toUpperCase();
      return s.includes("UNSUPPORTED") || s.includes("PLATFORM_STOP_UNSUPPORTED");
    });
  }
  const pol = String(o.stopPolicy || o.stop_policy || "").toUpperCase();
  if (pol === "ALERT_ONLY") return true;
  const cap = String(o.monitoringCapability || o.monitoring_capability || "").toUpperCase();
  return cap === "LIMITED";
}

window.HEDGE_FORMAT = {
  NULL_TEXT: HEDGE_NULL_TEXT,
  finite: hedgeFinite,
  qtyString: hedgeQtyString,
  fmtQty: hedgeFmtQty,
  qtyValid: hedgeQtyValid,
  fmtRatio: hedgeFmtRatio,
  fmtUsd: hedgeFmtUsd,
  fmtAbsUsd: hedgeFmtAbsUsd,
  fmtMs: hedgeFmtMs,
  statusClass: hedgeStatusClass,
  isActiveStatus: hedgeIsActiveStatus,
  isDraftStatus: hedgeIsDraftStatus,
  capabilityLabel: hedgeCapabilityLabel,
  riskLabel: hedgeRiskLabel,
  readinessLabel: hedgeReadinessLabel,
  alertSeverityClass: hedgeAlertSeverityClass,
  alertStateLabel: hedgeAlertStateLabel,
  simExpiresAt: hedgeSimExpiresAt,
  isExpired: hedgeIsExpired,
  isOrphanCode: hedgeIsOrphanCode,
  hasOrphanAlert: hedgeHasOrphanAlert,
  needsStopCopy: hedgeNeedsStopCopy,
};
