/* ============================================================================
   R13a — pure display / status model (D07/D12.1, D18.3 + R00 fixtures).
   No HTTP, no R12 symbols: components take props/callbacks only.
   Real wiring belongs to R13b.
   Usage: node --test desktop/ui/test/repair-workflow.test.mjs
   ========================================================================== */
import { test } from "node:test";
import assert from "node:assert";
import esbuild from "esbuild";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

const UI_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const APP = join(UI_ROOT, "src", "app");
const FILES = [
  "data.js", "mock.js", "i18n.js",
  "shortlab/short-lab-format.js",
  "shortlab/hedge-format.js",
  "shortlab/short-lab-table.jsx",
  "shortlab/short-lab-detail.jsx",
  "shortlab/short-lab-view.jsx",
  "shortlab/short-lab-evidence.jsx",
  "shortlab/funding-view.jsx",
  "shortlab/hedge-planner.jsx",
  "shortlab/hedge-monitor.jsx",
  "shortlab/hedge-alerts.jsx",
  "shortlab/decision-panel.jsx",
  "shortlab/plans-view.jsx",
  "desktop-app.jsx",
];

let _seq = 13000;
async function loadApp() {
  const prelude =
    "import React from 'react';\n" +
    "const ReactDOM = { createRoot: () => ({ render(){} }) };\n" +
    "globalThis.React = React; globalThis.ReactDOM = ReactDOM;\n";
  const contents =
    prelude +
    FILES.map((f) => `\n/* ==== ${f} ==== */\n` + readFileSync(join(APP, f), "utf8")).join("\n");
  const out = await esbuild.build({
    stdin: { contents, loader: "jsx", resolveDir: UI_ROOT },
    bundle: true, format: "esm", write: false, target: ["es2020"],
    jsx: "transform", jsxFactory: "React.createElement", jsxFragment: "React.Fragment",
    external: ["react", "react-dom"], logLevel: "silent",
  });
  globalThis.window = globalThis;
  globalThis.document = {
    documentElement: { setAttribute() {}, getAttribute: () => null },
    getElementById: () => null,
  };
  globalThis.location = { hash: "" };
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };
  const dir = join(UI_ROOT, "test", ".tmp");
  mkdirSync(dir, { recursive: true });
  const file = join(dir, `repair-${++_seq}.mjs`);
  writeFileSync(file, out.outputFiles[0].text);
  await import(pathToFileURL(file).href);
  return globalThis.DIVE_APP;
}

function getCanSave(app) {
  return app.canSavePairedPlan || globalThis.canSavePairedPlan;
}

/* D18.3 HTTP example shape (camelCase). IDs/hashes are structural demos. */
function mkD183Decision(over = {}) {
  return {
    contractSchemaVersion: "repair-contract-v1",
    decisionId: "dec-example",
    generatedAtMs: 1791417600000,
    expiresAtMs: 1791417620000,
    recommendation: "DATA_INSUFFICIENT",
    validationLevel: "RULE_BASED_UNVALIDATED",
    selectedProposal: null,
    alternatives: [],
    reasons: ["FUNDING_SCHEDULE_UNKNOWN"],
    assumptions: [],
    contextRefs: { identity: "identity-example", fcs: "fcs-example" },
    formulaVersion: "hedge-decision-v1",
    decisionPolicyHash: "sha256-of-canonical-policy",
    request: {
      symbol: "1000PEPEUSDT",
      goal: "CARRY_CAPTURE",
      futuresNotionalUsd: "10000",
      plannedHoldDays: 30,
      availableCapitalUsd: "25000",
      maxScenarioLossUsd: "1000",
      marginUsd: "12000",
      liquidationPrice: "0.02",
      liquidationPriceUpdatedAtMs: 1791417590000,
      preferredSpotVenue: "AUTO",
    },
    ...over,
  };
}

function mkProposal(over = {}) {
  return {
    target_ratio: "0.5",
    targetRatio: "0.5",
    actual_ratio: "0.5",
    actualRatio: "0.5",
    futures_contract_qty: "100",
    canonical_futures_qty: "100000",
    spot_net_qty: "50000",
    spot_venue: "BINANCE_SPOT",
    quote_refs: { spot: "q-1", futures: "q-2" },
    economics: { net_carry_usd: "241.0" },
    scenarios: [],
    ...over,
  };
}

function mkH0Decision() {
  return mkD183Decision({
    decisionId: "dec-h0",
    recommendation: "NO_HEDGE",
    selectedProposal: null,
    selected_proposal: null,
    reasons: ["H0_SINGLE_LEG_ONLY"],
    request: {
      symbol: "1000PEPEUSDT", goal: "CARRY_CAPTURE",
      futuresNotionalUsd: "10000", plannedHoldDays: 30,
      availableCapitalUsd: "25000", maxScenarioLossUsd: "1000",
      marginUsd: "12000", liquidationPrice: "0.02",
      liquidationPriceUpdatedAtMs: 1791417590000, preferredSpotVenue: "AUTO",
    },
  });
}

function mkPartialDecision() {
  const p = mkProposal({ target_ratio: "0.5", actual_ratio: "0.5", targetRatio: "0.5", actualRatio: "0.5" });
  return mkD183Decision({
    decisionId: "dec-partial",
    recommendation: "PARTIAL_HEDGE",
    selectedProposal: p,
    selected_proposal: p,
    reasons: [],
    assumptions: ["SCENARIO_ASSUMPTION_CAPACITY"],
    expiresAtMs: Date.now() + 60000,
    request: {
      symbol: "1000PEPEUSDT", goal: "DIRECTIONAL_SHORT",
      futuresNotionalUsd: "10000", plannedHoldDays: 30,
      availableCapitalUsd: "25000", maxScenarioLossUsd: "1000",
      marginUsd: "12000", liquidationPrice: "0.02",
      liquidationPriceUpdatedAtMs: 1791417590000, preferredSpotVenue: "BINANCE_SPOT",
    },
  });
}

function mkFullDecision() {
  const p = mkProposal({ target_ratio: "1", actual_ratio: "1", targetRatio: "1", actualRatio: "1", spot_net_qty: "100000" });
  return mkD183Decision({
    decisionId: "dec-full",
    recommendation: "FULL_HEDGE",
    selectedProposal: p,
    selected_proposal: p,
    reasons: [],
    expiresAtMs: Date.now() + 60000,
    request: {
      symbol: "1000PEPEUSDT", goal: "BALANCED",
      futuresNotionalUsd: "10000", plannedHoldDays: 30,
      availableCapitalUsd: "25000", maxScenarioLossUsd: "1000",
      marginUsd: "12000", liquidationPrice: "0.02",
      liquidationPriceUpdatedAtMs: 1791417590000, preferredSpotVenue: "AUTO",
    },
  });
}

function mkPlans() {
  return [
    { planId: "plan-1", symbol: "1000PEPEUSDT", status: "DRAFT", targetHedgeRatio: "0.5", planVersion: 1 },
    { planId: "plan-2", symbol: "BTCUSDT", status: "DRAFT", targetHedgeRatio: "1", planVersion: 2 },
  ];
}

/* ── R13a contract: paired-plan permission ─────────────────────────────── */
test("canSavePairedPlan forbids NO_HEDGE and DATA_INSUFFICIENT", async () => {
  const app = await loadApp();
  const canSavePairedPlan = getCanSave(app);
  assert.equal(typeof canSavePairedPlan, "function");
  assert.equal(canSavePairedPlan({ recommendation: "NO_HEDGE" }), false);
  assert.equal(canSavePairedPlan({ recommendation: "DATA_INSUFFICIENT" }), false);
});

test("canSavePairedPlan allows only PARTIAL/FULL hedge", async () => {
  const app = await loadApp();
  const canSavePairedPlan = getCanSave(app);
  assert.equal(canSavePairedPlan({ recommendation: "PARTIAL_HEDGE" }), true);
  assert.equal(canSavePairedPlan({ recommendation: "FULL_HEDGE" }), true);
  assert.equal(canSavePairedPlan({ recommendation: "AVOID" }), false);
  assert.equal(canSavePairedPlan({ recommendation: "MANUAL_REVIEW" }), false);
  assert.equal(canSavePairedPlan(null), false);
  assert.equal(canSavePairedPlan({}), false);
});

/* ── h0 read-only: goal/hold/budget/native/reasons, no save, no probability ─ */
test("h0 decision is read-only guidance without save button", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.DecisionPanel, {
    decision: mkH0Decision(),
  }));
  assert.ok(html.includes('data-testid="decision-panel"'), "panel marker");
  assert.ok(html.includes('data-testid="decision-h0-guide"'), "h0 guide marker");
  assert.ok(!html.includes('data-testid="decision-save"'), "h0 has no save button");
  // goal / hold / budget / native liq / reasons visible
  assert.ok(html.includes("1000PEPEUSDT"), "symbol visible");
  assert.ok(html.includes("CARRY_CAPTURE") || html.includes("Carry") || html.includes("套利"), "goal visible");
  assert.ok(html.includes("30"), "hold days visible");
  assert.ok(html.includes("25000") || html.includes("10000"), "budget/capital visible");
  assert.ok(html.includes("0.02"), "native liquidation visible");
  assert.ok(html.includes("H0_SINGLE_LEG_ONLY") || html.includes("只读"), "reason visible");
  // no predicted probability / guaranteed return
  assert.ok(!/预测.*概率|保证.*收益|guaranteed.*profit|win.*probability/i.test(html), "no probability guarantee");
  assert.ok(html.includes('data-testid="decision-no-probability"'), "explicit no-probability note");
});

/* ── h>0 can simulate/save DRAFT + E2E button rendering ─────────────────── */
test("h>0 decision can simulate and save DRAFT with E2E buttons", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  let saved = null;
  let simulated = null;
  const html = renderToStaticMarkup(React.createElement(app.DecisionPanel, {
    decision: mkPartialDecision(),
    onSimulate: () => { simulated = true; },
    onSave: (d) => { saved = d; },
  }));
  assert.ok(html.includes('data-testid="decision-panel"'));
  assert.ok(html.includes('data-testid="decision-simulate"'), "simulate button for E2E");
  assert.ok(html.includes('data-testid="decision-save"'), "save button for E2E");
  assert.ok(html.includes("DRAFT"), "DRAFT named, never ACTIVE");
  assert.ok(!html.includes('data-testid="decision-h0-guide"'), "h>0 has no h0 guide");
  // E2E: buttons are real <button> elements
  assert.ok(/<button[^>]*data-testid="decision-save"/.test(html), "save is a button");
  assert.ok(/<button[^>]*data-testid="decision-simulate"/.test(html), "simulate is a button");
  assert.equal(typeof simulated, "object", "callbacks are props (no HTTP in panel)");
  assert.equal(saved, null, "static render does not auto-save");
});

/* ── input change invalidates old decision ─────────────────────────────── */
test("input change invalidates the old decision", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const decision = mkPartialDecision();
  const staleFn = app.isDecisionStaleForInputs || globalThis.isDecisionStaleForInputs;
  assert.equal(typeof staleFn, "function");
  assert.equal(staleFn(decision, { ...decision.request }), false, "same inputs stay fresh");
  assert.equal(staleFn(decision, { ...decision.request, symbol: "BTCUSDT" }), true, "symbol change stales");
  assert.equal(staleFn(decision, { ...decision.request, futuresNotionalUsd: "9999" }), true, "notional change stales");
  const html = renderToStaticMarkup(React.createElement(app.DecisionPanel, {
    decision,
    currentInputs: { ...decision.request, symbol: "BTCUSDT" },
  }));
  assert.ok(html.includes('data-testid="decision-stale-inputs"'), "stale-inputs warning marker");
  assert.ok(!html.includes('data-testid="decision-save"'), "stale decision disables save");
});

/* ── no capability explains config state ──────────────────────────────── */
test("missing capability explains configuration without recommending", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.DecisionPanel, {
    decision: mkPartialDecision(),
    capability: { hedgeEnabled: false, fundingCaptureEnabled: false, reason: "HEDGE_DISABLED" },
  }));
  assert.ok(html.includes('data-testid="decision-capability"'), "capability marker");
  assert.ok(!html.includes('data-testid="decision-save"'), "no save when incapable");
  // explains config state, never falls back to fake success
  assert.ok(/HEDGE_DISABLED|未启用|disabled/i.test(html), "config reason explained");
  const html2 = renderToStaticMarkup(React.createElement(app.DecisionPanel, {
    decision: mkPartialDecision(),
    capability: { hedgeEnabled: true, monitoringCapability: "UNKNOWN" },
  }));
  assert.ok(html2.includes('data-testid="decision-capability"') || html2.includes('data-testid="decision-unknown"'), "UNKNOWN capability surfaced");
  assert.ok(!html2.includes('data-testid="decision-save"'), "UNKNOWN forbids paired save");
});

/* ── decision expiry disables save but stays readable ─────────────────── */
test("expired decision stays readable but forbids save", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const expired = mkPartialDecision();
  expired.expiresAtMs = 1;
  expired.expires_at_ms = 1;
  const fn = app.isDecisionExpired || globalThis.isDecisionExpired;
  assert.equal(fn(expired, Date.now()), true);
  assert.equal(fn(mkPartialDecision(), Date.now()), false);
  const html = renderToStaticMarkup(React.createElement(app.DecisionPanel, { decision: expired }));
  assert.ok(html.includes('data-testid="decision-expired"'), "expired marker");
  assert.ok(html.includes('data-testid="decision-panel"'), "frozen snapshot still readable");
  assert.ok(!html.includes('data-testid="decision-save"'), "expired forbids save");
});

/* ── funding: include_stale + STALE tag + analyze carries symbol/snapshot ─ */
test("funding view requests include_stale and marks STALE with analyze", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  // include_stale intent: capture query passed to DIVE
  let captured = null;
  const w = globalThis.window;
  const orig = w.DIVE && w.DIVE.fundingOpportunities;
  w.DIVE = w.DIVE || {};
  w.DIVE.fundingOpportunities = async (q) => { captured = q; return { asOf: 1, items: [], total: 0 }; };
  const { renderToString } = await import("react-dom/server");
  void renderToString;
  // mount via initial to avoid fetch, then inspect source for query key
  const src = readFileSync(join(APP, "shortlab", "funding-view.jsx"), "utf8");
  assert.ok(/include_stale|includeStale/.test(src), "funding builds include_stale query (D12.1)");
  if (orig) w.DIVE.fundingOpportunities = orig;
  else delete w.DIVE.fundingOpportunities;

  const staleItem = {
    symbol: "1000PEPEUSDT", canonicalId: "pepe", fcs: 82.5,
    funding7d: 0.004, funding30d: 0.018, funding90d: 0.05,
    positiveRatio30d: 0.85, bestVenue: "BINANCE_SPOT",
    roundTripCostPct: 0.003, breakEvenDays: 12.5,
    readiness: "NOT_READY", reasons: ["FUNDING_SCHEDULE_UNKNOWN"],
    stale: true, snapshotId: "fcs-MEME_FULL_VALID",
    snapshot_id: "fcs-MEME_FULL_VALID",
  };
  const html = renderToStaticMarkup(React.createElement(app.FundingView, {
    initial: { data: { asOf: 1791417600000, items: [staleItem], total: 1 } },
  }));
  assert.ok(html.includes('data-testid="funding-view"'));
  assert.ok(html.includes("STALE"), "STALE tag fixed (D12.1)");
  assert.ok(html.includes('data-testid="funding-stale-1000PEPEUSDT"') || html.includes('data-testid="funding-row-1000PEPEUSDT"'), "stale row marker");
  assert.ok(html.includes('data-testid="funding-analyze-1000PEPEUSDT"'), "analyze button carries symbol");
  assert.ok(html.includes("fcs-MEME_FULL_VALID") || html.includes("FUNDING_SCHEDULE_UNKNOWN"), "snapshot/reason visible");
});

/* ── plans view renders only real props, never localStorage ───────────── */
test("plans view renders only real props and never fabricates", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const src = readFileSync(join(APP, "shortlab", "plans-view.jsx"), "utf8");
  assert.ok(!/localStorage\s*\.\s*(getItem|setItem|removeItem|clear)/.test(src), "plans-view must not touch persistent store");
  assert.ok(!/fetch\(|window\.DIVE/.test(src), "plans-view is props-only, no HTTP");
  const html = renderToStaticMarkup(React.createElement(app.PlansView, {
    plans: mkPlans(), total: 2,
  }));
  assert.ok(html.includes('data-testid="plans-view"'));
  assert.ok(html.includes("plan-1") && html.includes("plan-2"), "real plans render");
  assert.ok(html.includes("DRAFT"), "DRAFT status distinct");
  assert.ok(html.includes('data-testid="plans-view-plan-1"'), "view button E2E");
  assert.ok(html.includes('data-testid="plans-monitor-plan-1"'), "monitor button E2E");
  const empty = renderToStaticMarkup(React.createElement(app.PlansView, { plans: [] }));
  assert.ok(empty.includes('data-testid="plans-empty"'), "empty honest, no fabricated plan");
  assert.ok(!empty.includes("plan-1"), "empty never invents a plan");
});

/* ── i18n: all R13a keys frozen in ZH/TR/EN ────────────────────────────── */
test("R13a Chinese keys are frozen for R13b in all languages", async () => {
  const app = await loadApp();
  const w = globalThis.window;
  const keys = [
    "repair_tab_decision", "repair_tab_plans",
    "repair_decision_title", "repair_decision_goal", "repair_decision_hold_days",
    "repair_decision_capital", "repair_decision_max_loss", "repair_decision_margin",
    "repair_decision_liq", "repair_decision_reasons",
    "repair_decision_no_probability", "repair_decision_h0_guide",
    "repair_decision_no_hedge_guide", "repair_decision_insufficient_guide",
    "repair_decision_stale_inputs", "repair_decision_expired",
    "repair_decision_simulate_btn", "repair_decision_save_btn",
    "repair_decision_view_plan", "repair_decision_goto_monitor",
    "repair_decision_capability_unavailable", "repair_decision_unknown_no_action",
    "repair_plans_title", "repair_plans_empty", "repair_plans_view", "repair_plans_monitor",
    "repair_funding_analyze_btn", "repair_funding_stale_reason",
    "repair_rec_data_insufficient", "repair_rec_manual_review", "repair_rec_avoid",
    "repair_rec_no_hedge", "repair_rec_partial_hedge", "repair_rec_full_hedge",
    "repair_goal_carry", "repair_goal_directional", "repair_goal_balanced",
  ];
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => { store.set(k, String(v)); } };
  for (const lang of ["zh", "tr", "en"]) {
    store.set("dive_lang", lang);
    assert.equal(w.diveLang(), lang);
    for (const k of keys) {
      const v = w.L(k);
      assert.ok(v && v !== k, `${lang}.${k} must be translated`);
    }
  }
  store.set("dive_lang", "zh");
  assert.ok(w.L("repair_decision_title").match(/[\u4e00-\u9fff]/), "ZH decision title is Chinese");
  assert.ok(w.L("repair_rec_no_hedge").match(/[\u4e00-\u9fff]/), "ZH recommendation is Chinese");
  assert.ok(w.L("repair_plans_title").match(/[\u4e00-\u9fff]/), "ZH plans title is Chinese");
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };
});

/* ── shell subnav/routing keeps old tabs and adds decision/plans ───────── */
test("shell keeps H09 subnav and routes decision/plans inside shortlab", async () => {
  const app = await loadApp();
  assert.deepEqual(app.SHORTLAB_SUB_TABS, ["candidates", "funding", "planner", "monitor", "alerts", "evidence"], "H09 tabs unchanged");
  assert.ok(Array.isArray(app.SHORTLAB_REPAIR_TABS), "repair tabs exported");
  assert.ok(app.SHORTLAB_REPAIR_TABS.includes("decision") && app.SHORTLAB_REPAIR_TABS.includes("plans"));
  globalThis.location.hash = "#/shortlab/decision";
  assert.equal(app.shortlabTabFromHash(), "decision");
  globalThis.location.hash = "#/shortlab/plans";
  assert.equal(app.shortlabTabFromHash(), "plans");
  globalThis.location.hash = "#/shortlab/funding";
  assert.equal(app.shortlabTabFromHash(), "funding");
  assert.equal(app.VIEW_HASH.shortlab, "shortlab");
  assert.ok(!app.VIEW_HASH.decision, "decision is not top-level");
  assert.ok(!app.VIEW_HASH.plans, "plans is not top-level");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const shell = renderToStaticMarkup(React.createElement(app.ShortLabShell, { initialTab: "decision" }));
  assert.ok(shell.includes('data-testid="shortlab-tab-decision"'), "decision subnav button");
  assert.ok(shell.includes('data-testid="shortlab-tab-plans"'), "plans subnav button");
  assert.ok(shell.includes('data-testid="shortlab-page-decision"'), "decision page slot");
  for (const t of ["candidates", "funding", "planner", "monitor", "alerts", "evidence"]) {
    assert.ok(shell.includes(`data-testid="shortlab-tab-${t}"`), `H09 tab ${t} kept`);
  }
  assert.equal(typeof app.DecisionPanel, "function");
  assert.equal(typeof app.PlansView, "function");
});

/* ── planner: stale inputs + view/monitor links ───────────────────────── */
test("planner invalidates stale simulation and links to plans/monitor", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const sim = {
    simulationId: "sim-1", generatedAtMs: Date.now(), expiresAtMs: Date.now() + 60000,
    expired: false, symbol: "1000PEPEUSDT", targetHedgeRatio: 0.5,
    targetSpotQty: "50000", canonicalFuturesQty: "100",
    readiness: "READY", riskValidation: "VERIFIED", monitoringCapability: "FULL",
  };
  const html = renderToStaticMarkup(React.createElement(app.HedgePlanner, {
    initial: { simulation: sim, plan: { planId: "plan-1", status: "DRAFT", planVersion: 1 } },
  }));
  assert.ok(html.includes('data-testid="hedge-planner"'));
  // view/monitor links after save (R13a pure display, no HTTP)
  assert.ok(html.includes('data-testid="planner-view-plan"') || html.includes('data-testid="hedge-plan-created"'), "view-plan entry present");
  const src = readFileSync(join(APP, "shortlab", "hedge-planner.jsx"), "utf8");
  assert.ok(/repair_decision_stale_inputs|decision-stale|simStale|isSimStale/.test(src), "planner tracks stale inputs");
});
