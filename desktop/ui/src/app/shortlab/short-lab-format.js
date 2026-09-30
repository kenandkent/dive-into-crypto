/* ============================================================================
   short-lab — Desktop · shared formatting (Task 15 · design §4.4 / §25–26)

   CONTRACT: the backend speaks decimals on the wire (0.005 = 0.5%) and UTC
   epoch ms. Every percent↔decimal conversion in the whole UI lives in THIS
   file — data.js and the components pass decimals through verbatim and never
   convert. Missing values stay honest: null renders "—", a confirmed
   NOT_APPLICABLE renders "N/A", never 0.
   Pure functions only (no React, no fetch) so node --test can import directly.
   ========================================================================== */

const SL_NULL_TEXT = "—";
const SL_NA_TEXT = "N/A";
const SL_UNAVAILABLE_TEXT = "UNAVAILABLE";
const SL_ERROR_TEXT = "ERROR";
const SL_PARTIAL_TEXT = "PARTIAL";
const SL_OK_TEXT = "OK";

/* Default ordering mirrors design §25.1 exactly: executable-first, nulls last.
   The server applies it when no explicit sort is sent; this caption only names
   it so the UI never implies severity ordering. */
const SL_DEFAULT_SORT_CAPTION =
  "READY > CANDIDATE > WATCH > PAUSED > BLOCKED > EXCLUDED · LTSS ↓ · Entry ↓ · DQ ↓";

function slFinite(v) {
  if (v == null || typeof v === "boolean") return null;
  const n = Number(v);
  return isFinite(n) ? n : null;
}

/* Scores / DQ: one decimal, null → "—". */
function slFmtScore(v) {
  const n = slFinite(v);
  return n == null ? SL_NULL_TEXT : n.toFixed(1);
}
function slFmtEntry(v) { return slFmtScore(v); }
function slFmtDQ(v) { return slFmtScore(v); }

/* Backend decimal → screen percent. 0.0125 → "+1.25%". */
function slFmtFundingDecimal(d, digits) {
  const n = slFinite(d);
  if (n == null) return SL_NULL_TEXT;
  const k = digits == null ? 2 : digits;
  return (n >= 0 ? "+" : "") + (n * 100).toFixed(k) + "%";
}

/* Screen percent input ("0.5" meaning 0.5%) → backend decimal 0.005.
   Empty/invalid → null (the caller shows a validation message, never coerces). */
function slPctInputToDecimal(text) {
  if (text == null) return null;
  const t = String(text).trim().replace("%", "").replace(",", ".");
  if (t === "") return null;
  const n = Number(t);
  return isFinite(n) ? n / 100 : null;
}

/* Backend decimal → percent-input prefill ("0.005" → "0.5"). */
function slDecimalToPctInput(d) {
  const n = slFinite(d);
  return n == null ? "" : String(n * 100);
}

/* ATH drawdown decimal (-0.58 → "-58.0%"). */
function slFmtAthDD(d, digits) {
  const n = slFinite(d);
  if (n == null) return SL_NULL_TEXT;
  const k = digits == null ? 1 : digits;
  return (n * 100).toFixed(k) + "%";
}

/* Plain ratios (OI/MC, futures/spot). */
function slFmtRatio(v, digits) {
  const n = slFinite(v);
  if (n == null) return SL_NULL_TEXT;
  return n.toFixed(digits == null ? 2 : digits);
}

/* 0..1 share → "87%". */
function slFmtShare(v) {
  const n = slFinite(v);
  return n == null ? SL_NULL_TEXT : Math.round(n * 100) + "%";
}

function slFmtUsd(v) {
  const n = slFinite(v);
  if (n == null) return SL_NULL_TEXT;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(2) + "K";
  return String(Math.round(n));
}

function slFmtMs(ms) {
  const n = slFinite(ms);
  if (n == null) return SL_NULL_TEXT;
  try {
    return new Date(Math.round(n)).toISOString().replace("T", " ").slice(0, 19) + "Z";
  } catch (e) { return SL_NULL_TEXT; }
}

function slTierTag(tier) {
  return tier === "FULL" ? "FULL" : "LITE";
}

/* Design §17: LITE READY reads "READY · LITE", FULL READY reads "READY". */
function slStatusLabel(status, tier) {
  if (status === "READY") return tier === "FULL" ? "READY" : "READY · LITE";
  return status || SL_NULL_TEXT;
}

function slStatusClass(status) {
  if (status === "READY") return "good";
  if (status === "CANDIDATE") return "info";
  if (status === "PAUSED") return "hot";
  if (status === "BLOCKED") return "bad";
  if (status === "EXCLUDED") return "dim";
  return "";
}

/* Field cell: a real value formats; a confirmed NOT_APPLICABLE is "N/A";
   anything else missing is "—". The two missing kinds never share a label. */
function slFieldText(value, fmtFn, groupStatus) {
  const n = slFinite(value);
  if (n != null) return fmtFn(n);
  if (value != null && typeof value !== "number") return String(value);
  if (groupStatus === "NOT_APPLICABLE") return SL_NA_TEXT;
  return SL_NULL_TEXT;
}

function slAvailabilityLabel(status) {
  if (status === "NOT_APPLICABLE") return SL_NA_TEXT;
  if (status === "UNAVAILABLE") return SL_UNAVAILABLE_TEXT;
  if (status === "ERROR") return SL_ERROR_TEXT;
  if (status === "PARTIAL") return SL_PARTIAL_TEXT;
  if (status === "OK") return SL_OK_TEXT;
  return status || SL_UNAVAILABLE_TEXT;
}

/* Rule-code → one-line readable explanation. Codes stay verbatim in the UI;
   this only adds the human gloss (design §26.4 Risks block). */
const SL_REASON_TEXT = {
  VETO_DATA_IDENTITY: "identity unresolved — no guessing across providers",
  VETO_LOW_DATA_QUALITY: "data quality below the hard floor",
  VETO_LOW_LIQUIDITY: "below hard volume/OI floor or untradeable contract",
  VETO_CONTRACT_DELISTING: "confirmed delisting / delivery shutdown window",
  PAUSE_BREAKOUT_24H: "24h surge — breakout risk, entry paused",
  PAUSE_BREAKOUT_7D: "7d surge — breakout risk, entry paused",
  PAUSE_SQUEEZE: "price↑ + OI↑ squeeze signature",
  PAUSE_NEGATIVE_CARRY: "carry turned negative — shorts pay to hold",
  PAUSE_NEW_TOKEN: "listed too recently to judge structurally",
  PAUSE_CONTRACT_STATUS_UNVERIFIED: "vanished from live universe, no verified end-state",
  PAUSE_MAJOR_CATALYST: "major event window (listing/mainnet/burn/buyback)",
  LTSS_BELOW_READY: "LTSS below the READY threshold",
  ENTRY_NOT_AVAILABLE: "no Entry computed for this symbol",
  ENTRY_BUDGET_EXHAUSTED: "Entry build deferred — per-round request budget exhausted",
  ENTRY_BELOW_READY_THRESHOLD: "Entry below the READY threshold",
  DATA_QUALITY_BELOW_READY: "data quality below the READY threshold",
  TRADEABILITY_BELOW_READY: "tradeability below the READY threshold",
  IDENTITY_REVIEW_REQUIRED: "MEDIUM identity confidence — needs manual review",
  MULTIPLIER_UNVERIFIED: "contract multiplier unverified — cross-source prices withheld",
  LISTING_AGE_UNKNOWN: "no valid listing date — age cannot be verified",
  READY_INPUT_STALE: "a READY-required input went stale — downgraded, not recomputed",
  FULL_PREREQUISITE_MISSING: "FULL requested but a required provider is missing — serving LITE",
  WARN_HIGH_VOLATILITY: "abnormally high volatility",
  WARN_FUNDING_WEAKENING: "30D funding positive but last 7D weakening",
  WARN_PROVIDER_PARTIAL: "a provider returned partial data",
  WARN_HIGH_CONCENTRATION: "high holder concentration (when wired)",
};

function slReasonText(code) {
  return SL_REASON_TEXT[code] || String(code || "");
}

/* Exposed on window for the node --test suite (same pattern as data.js);
   the bundle itself uses the bare names above. */
window.SL_FORMAT = {
  NULL_TEXT: SL_NULL_TEXT, NA_TEXT: SL_NA_TEXT, UNAVAILABLE_TEXT: SL_UNAVAILABLE_TEXT,
  DEFAULT_SORT_CAPTION: SL_DEFAULT_SORT_CAPTION,
  finite: slFinite, fmtScore: slFmtScore, fmtEntry: slFmtEntry, fmtDQ: slFmtDQ,
  fmtFundingDecimal: slFmtFundingDecimal, pctInputToDecimal: slPctInputToDecimal,
  decimalToPctInput: slDecimalToPctInput, fmtAthDD: slFmtAthDD, fmtRatio: slFmtRatio,
  fmtShare: slFmtShare, fmtUsd: slFmtUsd, fmtMs: slFmtMs,
  tierTag: slTierTag, statusLabel: slStatusLabel, statusClass: slStatusClass,
  fieldText: slFieldText, availabilityLabel: slAvailabilityLabel,
  REASON_TEXT: SL_REASON_TEXT, reasonText: slReasonText,
};
