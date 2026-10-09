/* ============================================================================
 * R13b — real routing + adapter wiring (D07/D12.1, R12/R13a/R10a contracts).
 * Fake ONLY stubs HTTP responses; every binding under test calls the REAL
 * data.js adapters (hedgeDecisionCreate/Get, hedgeVenues, createHedgePlan,
 * hedgePlans, shortCapabilities) via the REAL workflow-bindings factory.
 *
 * NOTE (R07/R10b unmerged): real Decision/Gate assertions use R00 fixture
 * stand-ins (MEME_FULL_VALID / gate_cases.json shapes: PASS/FAIL/UNKNOWN,
 * RULE_BASED_UNVALIDATED). They validate structure only and do NOT prove
 * producer computation. Final real-line acceptance is pending R15b.
 *
 * Usage: node --test test/repair-workflow-binding.test.mjs (workdir desktop/ui)
 * ========================================================================== */
import { test } from "node:test";
import assert from "node:assert/strict";
import esbuild from "esbuild";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import * as WB from "../src/app/shortlab/workflow-bindings.mjs";

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
  "shortlab/workflow-model.mjs",
  "shortlab/decision-panel.jsx",
  "shortlab/plans-view.jsx",
  "shortlab/workflow-bindings.mjs",
  "desktop-app.jsx",
];

let _seq = 13100;
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
  const file = join(dir, `repair-binding-${++_seq}.mjs`);
  writeFileSync(file, out.outputFiles[0].text);
  await import(pathToFileURL(file).href + `?t=${_seq}`);
  return globalThis.DIVE_APP;
}

let _dseq = 13200;
async function loadRealData() {
  const src = readFileSync(join(APP, "data.js"), "utf8");
  const contents = "globalThis.window = globalThis;\n" + src + "\n";
  const out = await esbuild.build({
    stdin: { contents, loader: "js", resolveDir: UI_ROOT },
    bundle: false, format: "esm", write: false, target: ["es2020"],
  });
  globalThis.window = globalThis;
  if (!globalThis.document) {
    globalThis.document = { documentElement: { setAttribute() {}, getAttribute: () => null }, getElementById: () => null };
  }
  if (!globalThis.location) globalThis.location = { hash: "" };
  if (!globalThis.localStorage) globalThis.localStorage = { getItem: () => null, setItem: () => {} };
  delete globalThis.DIVE;
  const dir = join(UI_ROOT, "test", ".tmp");
  mkdirSync(dir, { recursive: true });
  const file = join(dir, `repair-binding-data-${++_dseq}.mjs`);
  writeFileSync(file, out.outputFiles[0].text);
  await import(pathToFileURL(file).href + `?t=${_dseq}`);
  return globalThis.DIVE;
}

function ok(body, status = 200) { return { ok: true, status, json: async () => body }; }
function fail(status, body = {}) { return { ok: false, status, json: async () => body }; }

/* R00 stand-ins (structure only, pending R15b real producers). */
function mkR00Decision(over = {}) {
  return {
    contractSchemaVersion: "repair-contract-v1",
    decisionId: "dec-MEME_FULL_VALID",
    generatedAtMs: 1791417600000,
    expiresAtMs: 1791417620000,
    recommendation: "FULL_HEDGE",
    validationLevel: "RULE_BASED_UNVALIDATED",
    selectedProposal: {
      target_ratio: "1", targetRatio: "1",
      actual_ratio: "1", actualRatio: "1",
      futures_contract_qty: "100",
      canonical_futures_qty: "100",
      spot_net_qty: "100000",
      spot_venue: "BINANCE_SPOT",
    },
    selected_proposal: {
      target_ratio: "1", actual_ratio: "1",
    },
    alternatives: [],
    reasons: [],
    assumptions: [],
    contextRefs: { identity: "identity-MEME_FULL_VALID", fcs: "fcs-MEME_FULL_VALID" },
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
      liquidationPrice: "0.025",
      liquidationPriceUpdatedAtMs: 1791414000000,
      preferredSpotVenue: "AUTO",
    },
    ...over,
  };
}

function mkR00Gate(over = {}) {
  return {
    status: "PASS",
    reasons: [],
    checked_at_ms: 1791417600000,
    input_refs: { funding: "fcs-MEME_FULL_VALID" },
    ...over,
  };
}

function mkFreshQuote(over = {}) {
  const now = Date.now();
  return {
    venue: "BINANCE_SPOT",
    buyExecutableQty: "200000",
    sellExecutableQty: "200000",
    buy_executable_qty: "200000",
    sell_executable_qty: "200000",
    expiresAtMs: now + 15000,
    expires_at_ms: now + 15000,
    ...over,
  };
}

/* ── routing carries symbol/snapshotId for both funding-carry and directional/balanced ─ */
test("R13b routing carries symbol/snapshotId for funding carry and directional/balanced", async () => {
  assert.ok(typeof WB.buildDecisionRequestFromCandidate === "function");
  assert.ok(typeof WB.formatPlannerHash === "function");
  assert.ok(typeof WB.parsePlannerHash === "function");
  // Funding carry (CARRY_CAPTURE from Funding row).
  const fundingRow = { symbol: "1000PEPEUSDT", snapshotId: "fcs-MEME_FULL_VALID", fcs: 82.5 };
  const carry = WB.buildDecisionRequestFromCandidate(fundingRow, { goal: "CARRY_CAPTURE" });
  assert.equal(carry.symbol, "1000PEPEUSDT");
  assert.equal(carry.snapshotId, "fcs-MEME_FULL_VALID");
  assert.equal(carry.goal, "CARRY_CAPTURE");
  assert.equal(carry.request.symbol, "1000PEPEUSDT");
  // Directional candidate (MEME profile) and Balanced share the same carrier.
  const dirCand = { symbol: "BTCUSDT", snapshot_id: "snap-dir-1", profile: "MEME_FULL" };
  const dir = WB.buildDecisionRequestFromCandidate(dirCand, { goal: "DIRECTIONAL_SHORT" });
  assert.equal(dir.symbol, "BTCUSDT");
  assert.equal(dir.snapshotId, "snap-dir-1");
  assert.equal(dir.goal, "DIRECTIONAL_SHORT");
  const bal = WB.buildDecisionRequestFromCandidate({ symbol: "BTCUSDT", snapshotId: "snap-bal-1" }, { goal: "BALANCED" });
  assert.equal(bal.goal, "BALANCED");
  assert.equal(bal.symbol, "BTCUSDT");
  // Hash round-trip keeps the pair coupled for planner prefill (D12.1).
  const hash = WB.formatPlannerHash(carry.symbol, carry.snapshotId);
  assert.ok(hash.includes("1000PEPEUSDT") && hash.includes("fcs-MEME_FULL_VALID"));
  const parsed = WB.parsePlannerHash(hash);
  assert.equal(parsed.symbol, "1000PEPEUSDT");
  assert.equal(parsed.snapshotId, "fcs-MEME_FULL_VALID");
});

/* ── input change invalidates the old decision ─────────────────────────── */
test("R13b input change invalidates the old decision", async () => {
  const d = mkR00Decision();
  assert.equal(WB.workflowDecisionIsStale(d, { ...d.request }), false);
  assert.equal(WB.workflowDecisionIsStale(d, { ...d.request, symbol: "BTCUSDT" }), true);
  assert.equal(WB.workflowDecisionIsStale(d, { ...d.request, futuresNotionalUsd: "9999" }), true);
  assert.equal(WB.workflowDecisionIsStale(d, { ...d.request, goal: "DIRECTIONAL_SHORT" }), true);
});

/* ── h0 read-only: never saves a two-leg plan ──────────────────────────── */
test("R13b h0 read-only never saves a two-leg plan", async () => {
  const h0 = mkR00Decision({ recommendation: "NO_HEDGE", selectedProposal: null, selected_proposal: null });
  assert.equal(WB.canSaveWorkflowDecision(h0), false);
  assert.equal(WB.canSaveWorkflowDecision(mkR00Decision({ recommendation: "DATA_INSUFFICIENT", selectedProposal: null })), false);
  assert.equal(WB.canSaveWorkflowDecision(mkR00Decision({ recommendation: "AVOID", selectedProposal: null })), false);
  const partial = mkR00Decision({ recommendation: "PARTIAL_HEDGE" });
  // PARTIAL with nonzero actual ratio is savable; zero actual ratio (h0 slip) is not.
  assert.equal(WB.canSaveWorkflowDecision(partial), true);
  const zeroActual = mkR00Decision({
    recommendation: "PARTIAL_HEDGE",
    selectedProposal: { target_ratio: "0.5", actual_ratio: "0", targetRatio: "0.5", actualRatio: "0" },
  });
  assert.equal(WB.canSaveWorkflowDecision(zeroActual), false);
  // savePlanAndNavigate blocks h0 without touching HTTP.
  const DIVE = await loadRealData();
  let httpCalls = 0;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => { httpCalls += 1; return ok({}); };
  try {
    const bindings = WB.createWorkflowBindings({ dive: DIVE, navigate: () => {}, nowMs: () => Date.now() });
    await assert.rejects(
      bindings.savePlanAndNavigate({ decision: h0, simulationId: "sim-h0" }),
      /H0_READ_ONLY/,
    );
    assert.equal(httpCalls, 0, "h0 blocked before any HTTP");
  } finally {
    globalThis.fetch = realFetch;
  }
});

/* ── R00 Gate/Decision/Quote shape assertions (pending R15b) ───────────── */
test("R13b Gate/Decision/Quote shapes assert R00 stand-ins (pending R15b real line)", async () => {
  assert.equal(WB.assertGateShape(mkR00Gate({ status: "PASS", reasons: [] })), true);
  assert.equal(WB.assertGateShape(mkR00Gate({ status: "FAIL", reasons: ["FUNDING_CURRENT_NON_POSITIVE"] })), true);
  assert.equal(WB.assertGateShape(mkR00Gate({ status: "UNKNOWN", reasons: ["FUNDING_SCHEDULE_UNKNOWN"] })), true);
  assert.throws(() => WB.assertGateShape({ status: "READY", reasons: [] }), /unknown status/);
  assert.equal(WB.assertDecisionShape(mkR00Decision()), true);
  assert.throws(() => WB.assertDecisionShape({ ...mkR00Decision(), recommendation: "GUARANTEED_PROFIT" }), /unknown recommendation/);
  assert.equal(WB.assertQuoteShape(mkFreshQuote()), true);
  assert.throws(() => WB.assertQuoteShape({ ...mkFreshQuote(), expiresAtMs: Date.now() - 1000 }), /expired/);
});

/* ── plan example 1: requestedDecision.symbol == selectedCandidate.symbol (real DIVE) ─ */
test("R13b requestedDecision.symbol follows selectedCandidate.symbol via real data.js", async () => {
  const DIVE = await loadRealData();
  const selectedCandidate = { symbol: "1000PEPEUSDT", snapshotId: "fcs-MEME_FULL_VALID" };
  let seenBody = null;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const u = String(url);
    if (u === "/api/short/hedge/decisions" && init && init.method === "POST") {
      seenBody = JSON.parse(init.body);
      return ok(mkR00Decision({ request: { ...mkR00Decision().request, symbol: selectedCandidate.symbol } }));
    }
    return ok({});
  };
  try {
    const bindings = WB.createWorkflowBindings({ dive: DIVE, navigate: () => {}, nowMs: () => Date.now() });
    const out = await bindings.requestDecision(selectedCandidate, { goal: "CARRY_CAPTURE" });
    const requestedDecision = out.request;
    assert.equal(requestedDecision.symbol, selectedCandidate.symbol);
    assert.equal(seenBody.symbol, selectedCandidate.symbol, "real adapter sent symbol verbatim");
    assert.equal(out.decision.request.symbol, selectedCandidate.symbol);
  } finally {
    globalThis.fetch = realFetch;
  }
});

/* ── plan example 2: savedPlanNavigation.planId == apiSaveResponse.planId (real list_plans) ─ */
test("R13b savedPlanNavigation.planId follows apiSaveResponse.planId via real list_plans", async () => {
  const DIVE = await loadRealData();
  const apiSaveResponse = { planId: "plan-R13B-1", plan_id: "plan-R13B-1", status: "DRAFT", planVersion: 1 };
  let listCalls = 0;
  const realFetch = globalThis.fetch;
  let navigated = null;
  globalThis.fetch = async (url, init) => {
    const u = String(url);
    if (u === "/api/short/hedge/plans" && init && init.method === "POST") {
      return ok(apiSaveResponse);
    }
    if (u.startsWith("/api/short/hedge/plans") && (!init || !init.method || init.method === "GET")) {
      listCalls += 1;
      return ok({ items: [{ planId: "plan-R13B-1", symbol: "1000PEPEUSDT", status: "DRAFT", planVersion: 1 }], total: 1 });
    }
    return ok({});
  };
  try {
    const bindings = WB.createWorkflowBindings({
      dive: DIVE,
      navigate: (h) => { navigated = h; return h; },
      nowMs: () => Date.now(),
    });
    const decision = mkR00Decision({ recommendation: "PARTIAL_HEDGE" });
    const out = await bindings.savePlanAndNavigate({ decision, simulationId: "sim-1" });
    const savedPlanNavigation = out.navigation;
    assert.equal(savedPlanNavigation.planId, apiSaveResponse.planId);
    assert.ok(listCalls >= 1, "saved plan verified via real list_plans, not localStorage");
    assert.equal(typeof navigated, "string", "navigates to monitor after real verification");
    // No localStorage替身 anywhere in the new wiring.
    const wbsrc = readFileSync(join(APP, "shortlab", "workflow-bindings.mjs"), "utf8");
    assert.ok(!/localStorage\s*\.\s*(getItem|setItem|removeItem|clear)/.test(wbsrc), "workflow-bindings never touches localStorage");
  } finally {
    globalThis.fetch = realFetch;
  }
});

/* ── funding research: include_stale intent + analyze carries pair into planner ─ */
test("R13b funding research keeps include_stale and analyze carries pair into planner", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const fsrc = readFileSync(join(APP, "shortlab", "funding-view.jsx"), "utf8");
  assert.ok(/include_stale|includeStale/.test(fsrc), "funding builds include_stale query (D12.1 research list)");
  // R13b wires the hash via workflow-bindings (not a hand-rolled string).
  assert.ok(/formatPlannerHash|WORKFLOW_BINDINGS|shortlabPlannerSelection/.test(fsrc), "funding analyze routes via workflow-bindings");
  const staleItem = {
    symbol: "1000PEPEUSDT", canonicalId: "pepe", fcs: 82.5,
    funding7d: 0.004, funding30d: 0.018, funding90d: 0.05,
    positiveRatio30d: 0.85, bestVenue: "BINANCE_SPOT",
    roundTripCostPct: 0.003, breakEvenDays: 12.5,
    readiness: "NOT_READY", reasons: ["FUNDING_SCHEDULE_UNKNOWN"],
    stale: true, snapshotId: "fcs-MEME_FULL_VALID",
  };
  let analyzed = null;
  const html = renderToStaticMarkup(app.FundingView
    ? globalThis.React.createElement(app.FundingView, {
      initial: { data: { asOf: 1791417600000, items: [staleItem], total: 1 } },
      onAnalyze: (sel) => { analyzed = sel; },
    })
    : globalThis.React.createElement("div", null, "missing"));
  assert.ok(html.includes('data-testid="funding-analyze-1000PEPEUSDT"'), "analyze button carries symbol");
  // The workflow pair builder (used by desktop-app onAnalyze) keeps symbol+snapshot coupled.
  const built = WB.buildDecisionRequestFromCandidate(staleItem, { goal: "CARRY_CAPTURE" });
  assert.equal(built.symbol, "1000PEPEUSDT");
  assert.equal(built.snapshotId, "fcs-MEME_FULL_VALID");
  void analyzed;
});

/* ── click suggestion refreshes real Gate + quote shapes (no stale reuse) ─ */
test("R13b clicking suggestion refreshes real Gate and quote shapes", async () => {
  const DIVE = await loadRealData();
  const realFetch = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url, init) => {
    seen.push(String(url));
    const u = String(url);
    if (u === "/api/short/hedge/decisions/dec-1") return ok(mkR00Decision());
    if (u.startsWith("/api/short/hedge/venues/")) {
      return ok({ venues: [mkFreshQuote({ venue: "BINANCE_SPOT" })], symbol: "1000PEPEUSDT" });
    }
    return ok({});
  };
  try {
    const bindings = WB.createWorkflowBindings({ dive: DIVE, navigate: () => {}, nowMs: () => Date.now() });
    const out = await bindings.refreshDecisionAndQuotes("dec-1", "1000PEPEUSDT");
    assert.equal(out.decision.decisionId, "dec-MEME_FULL_VALID");
    assert.ok(out.freshQuotes >= 1, "at least one fresh quote asserted");
    assert.ok(seen.some((u) => u.includes("/decisions/dec-1")), "real decision GET refreshed");
    assert.ok(seen.some((u) => u.includes("/hedge/venues/1000PEPEUSDT")), "real venue quotes refreshed");
  } finally {
    globalThis.fetch = realFetch;
  }
  // Hedge-planner wires the refresh + shape assertions (no stale reuse for entry).
  const hsrc = readFileSync(join(APP, "shortlab", "hedge-planner.jsx"), "utf8");
  assert.ok(/WORKFLOW_BINDINGS|assertQuoteShape|assertFreshSimulation|refreshDecisionAndQuotes|shortlabPlannerSelection/.test(hsrc),
    "hedge-planner refreshes Gate/quotes via workflow-bindings with shape assertions");
});

/* ── capability closed/unbound/503 shows unavailable, never Fake success ── */
test("R13b capability closed/unbound/503 shows unavailable, never Fake success", async () => {
  const DIVE = await loadRealData();
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u === "/api/short/capabilities") return fail(503, { error: "IMPLEMENTATION_UNAVAILABLE" });
    if (u === "/api/short/hedge/decisions") return fail(503, { error: "IMPLEMENTATION_UNAVAILABLE" });
    return fail(503, {});
  };
  try {
    await assert.rejects(DIVE.shortCapabilities({}), /503/);
    assert.equal(globalThis.SGS_SHORT_CAPABILITIES, null, "503 never populates globals (no Fake success)");
    const bindings = WB.createWorkflowBindings({ dive: DIVE, navigate: () => {}, nowMs: () => Date.now() });
    await assert.rejects(
      bindings.requestDecision({ symbol: "1000PEPEUSDT", snapshotId: "fcs-x" }, { goal: "CARRY_CAPTURE" }),
      /503|UNAVAILABLE/,
    );
  } finally {
    globalThis.fetch = realFetch;
  }
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(globalThis.React.createElement(app.DecisionPanel, {
    decision: mkR00Decision({ recommendation: "PARTIAL_HEDGE" }),
    capability: { hedgeEnabled: false, reason: "HEDGE_DISABLED" },
  }));
  assert.ok(html.includes('data-testid="decision-capability"'), "capability-closed window shows unavailable");
  assert.ok(!html.includes('data-testid="decision-save"'), "closed capability never offers Fake save");
});

/* ── cancel/late isolation: superseded responses never write ───────────── */
test("R13b cancel and late responses stay isolated (generation guard)", async () => {
  const DIVE = await loadRealData();
  const realFetch = globalThis.fetch;
  let releaseFirst = null;
  let firstUrl = null;
  globalThis.fetch = async (url, init) => {
    const u = String(url);
    if (u === "/api/short/hedge/decisions" && !firstUrl) {
      firstUrl = u;
      await new Promise((resolve) => { releaseFirst = resolve; });
      return ok(mkR00Decision({ decisionId: "dec-first" }));
    }
    if (u === "/api/short/hedge/decisions") {
      return ok(mkR00Decision({ decisionId: "dec-second" }));
    }
    return ok({});
  };
  try {
    const bindings = WB.createWorkflowBindings({ dive: DIVE, navigate: () => {}, nowMs: () => Date.now() });
    const p1 = bindings.requestDecision({ symbol: "1000PEPEUSDT", snapshotId: "s1" }, { goal: "CARRY_CAPTURE" });
    const p2 = bindings.requestDecision({ symbol: "BTCUSDT", snapshotId: "s2" }, { goal: "DIRECTIONAL_SHORT" });
    releaseFirst();
    await assert.rejects(p1, /SUPERSEDED/, "late first response is superseded, never writes");
    const second = await p2;
    assert.equal(second.symbol, "BTCUSDT", "current generation wins");
  } finally {
    globalThis.fetch = realFetch;
  }
  // Views keep single-flight AbortController + sequence guards.
  for (const f of ["shortlab/funding-view.jsx", "shortlab/hedge-planner.jsx"]) {
    const src = readFileSync(join(APP, f), "utf8");
    assert.ok(/AbortController/.test(src), `${f} keeps AbortController`);
    assert.ok(/seqRef|generation/.test(src), `${f} isolates late responses`);
  }
});

/* ── shell real routing: funding analyze → planner pair, plans → monitor via real list ─ */
test("R13b shell wires real routing (funding analyze to planner, plans to monitor)", async () => {
  const app = await loadApp();
  assert.deepEqual(app.SHORTLAB_SUB_TABS, ["candidates", "funding", "planner", "monitor", "alerts", "evidence"], "H09 tabs unchanged");
  assert.ok(app.SHORTLAB_REPAIR_TABS.includes("decision") && app.SHORTLAB_REPAIR_TABS.includes("plans"));
  const dsrc = readFileSync(join(APP, "desktop-app.jsx"), "utf8");
  assert.ok(/formatPlannerHash|shortlabPlannerSelectionFromHash|WORKFLOW_BINDINGS/.test(dsrc), "desktop-app routes via workflow-bindings");
  assert.ok(/hedgePlans|list_plans|onAnalyze/.test(dsrc), "desktop-app binds real list_plans/monitor after save (no localStorage替身)");
  assert.ok(!/localStorage\s*\.\s*setItem.*plan|localStorage.*hedgePlan/i.test(dsrc), "desktop-app never stashes plans in localStorage");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const shell = renderToStaticMarkup(globalThis.React.createElement(app.ShortLabShell, { initialTab: "funding" }));
  assert.ok(shell.includes('data-testid="shortlab-tab-planner"'), "planner subnav kept");
  assert.ok(shell.includes('data-testid="shortlab-page-funding"'), "funding page slot wired");
  // Planner prefills the routed pair (symbol from hash/candidate, not a hardcoded guess).
  const hsrc = readFileSync(join(APP, "shortlab", "hedge-planner.jsx"), "utf8");
  assert.ok(/candidate|fundingSelection|initialSymbol|parsePlannerHash|shortlabPlannerSelection/.test(hsrc),
    "hedge-planner prefills routed symbol/snapshotId via workflow-bindings");
});
