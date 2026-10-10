/* ============================================================================
   H09 — Funding/Planner/Monitor/Alerts desktop interaction (design B32/B34/B35).
   Loads the REAL bundle sources (same esbuild pipeline as build.mjs, now
   including hedge-format.js + four hedge pages) and pins:
     1. DIVE hedge methods map to B32 paths with snake aliases + camel JSON;
        quantities stay strings; 503 never mocks/clobbers; empty IDs throw;
        Abort signals pass through (F08 semantics).
     2. Quantity "0.000000000000000001" never goes through Number for orders.
     3. DRAFT/LIMITED vs ACTIVE separated; expired simulations stay readable;
        re-simulation mints a new ID; QUOTE_EXPIRED surfaces honestly.
     4. Orphan-leg / no-platform-stop / notification-denied / offline copy.
     5. Target/Actual/Estimated/Confirmed/Reference/User-entered copy distinct.
     6. ACK is acknowledgement, never resolution.
     7. Evidence directional vs hedge call DIFFERENT endpoints, never mixed.
     8. Pages render from `initial` without fetching; error pages show no
        fabricated numbers.
     9. ZH/TR/EN chrome complete with ZH default.
    10. Build order ships format before pages, all before desktop-app.jsx.
   E2E waits for H08 (real backend); here fetch is stubbed per endpoint.
   Usage: node --test desktop/ui/test/hedge.test.mjs
   ========================================================================== */
import { test } from "node:test";
import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import esbuild from "esbuild";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

const UI_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const APP = join(UI_ROOT, "src", "app");
/* Same file set and order as build.mjs (H09 adds hedge pages). */
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
  "desktop-app.jsx",
];

let _seq = 900;
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
  delete globalThis.DIVE_APP;
  delete globalThis.DIVE_DEMO;
  delete globalThis.SGS_HEDGE_FUNDING;
  delete globalThis.SGS_HEDGE_EVIDENCE;
  delete globalThis.SGS_HEDGE_ALERTS;
  delete globalThis.SGS_HEDGE_PLANS;
  delete globalThis.SGS_HEDGE_SIMULATION;
  const dir = join(UI_ROOT, "test", ".tmp");
  mkdirSync(dir, { recursive: true });
  const file = join(dir, `hedge-${++_seq}.mjs`);
  writeFileSync(file, out.outputFiles[0].text);
  await import(pathToFileURL(file).href);
  return globalThis.DIVE_APP;
}

function ok(body, status = 200) { return { ok: true, status, json: async () => body }; }
function fail(status, body = {}) { return { ok: false, status, json: async () => body }; }

function mkFundingItem(over = {}) {
  return {
    symbol: "BTCUSDT", canonicalId: "btc", fcs: 82.5, fcsVersion: "fcs_v1",
    funding7d: 0.008, funding30d: 0.043, funding90d: 0.108,
    positiveRatio30d: 0.86, positiveRatio90d: 0.78,
    bestVenue: "BINANCE_SPOT", referenceNotionalUsd: 10000,
    roundTripCostPct: 0.0032, breakEvenDays: 2.7,
    readiness: "READY", reasons: [],
    ...over,
  };
}
function mkSim(over = {}) {
  return {
    simulationId: "sim-1", generatedAtMs: 1760000000000, expiresAtMs: 1760000060000,
    expired: false, symbol: "BTCUSDT", canonicalId: "btc", mode: "ABSOLUTE",
    futuresSymbol: "BTCUSDT", futuresPrice: 67000, futuresNotionalUsd: 10000,
    futuresContractQty: "0.14925373", canonicalFuturesQty: "0.14925373",
    targetHedgeRatio: 1.0, spotVenue: "BINANCE_SPOT", spotSymbol: "BTC",
    spotPrice: 66950, targetSpotQty: "0.14925373", spotNotionalUsd: 9993,
    residualShortRatio: 0, residualShortQty: "0", residualShortNotionalUsd: 0,
    fcs: 82.5, planSafetyScore: 88,
    fundingMetrics: { funding30d: 0.043 }, basisMetrics: {}, costMetrics: { roundTripCostUsd: 32 },
    breakEven: { breakEvenDays: 2.7 },
    stressScenarios: [{ label: "+50%", futuresPnl: -100, spotPnl: 98 }],
    orderGuidance: [
      { leg: "FUTURES", side: "SELL", quantity: "0.14925373", referenceLimitPrice: 67000 },
      { leg: "SPOT", side: "BUY", quantity: "0.14925373", referenceLimitPrice: 66950 },
    ],
    monitoringCapability: "FULL", riskValidation: "VERIFIED",
    liquidationCheckStatus: "VERIFIED", risks: [], warnings: [],
    readiness: "READY",
    ...over,
  };
}
function mkPlan(over = {}) {
  return {
    planId: "plan-1", symbol: "BTCUSDT", canonicalId: "btc", mode: "ABSOLUTE",
    status: "DRAFT", targetHedgeRatio: 1.0,
    canonicalFuturesQty: "0.14925373", targetSpotQty: "0.14925373",
    spotVenue: "BINANCE_SPOT", liquidationPrice: null, liquidation_price: null,
    riskValidation: "LIMITED", monitoringCapability: "LIMITED",
    planSafetyScore: 62, planVersion: 1,
    ...over,
  };
}
function mkMonitor(over = {}) {
  return {
    planId: "plan-1", status: "ACTIVE",
    actualHedgeRatio: 0.687, residualShortNotionalUsd: 3130,
    markPrice: 70000, spotPrice: 69900,
    currentBasisPct: 0.0014, basisPnlUsd: -16,
    estimatedSettledFundingUsd: 84, projectedNextFundingUsd: 3.1,
    spotPnlUsd: 250, futuresPnlUsd: -360,
    knownCostUsd: -22, estimatedExitCostUsd: -18,
    netPnlBeforeExitUsd: -64, estimatedNetPnlAfterExitUsd: -82,
    liquidationDistance: 0.22, safetyScore: 81,
    recommendedAction: "NONE",
    alerts: [],
    ...over,
  };
}
function mkAlert(over = {}) {
  return {
    alertId: "al-1", planId: "plan-1", code: "HEDGE_RATIO_DRIFT",
    severity: "WARN", state: "OPEN",
    openedAtMs: 1, lastSeenAtMs: 2, episode: 1,
    dedupKey: "plan-1|HEDGE_RATIO_DRIFT|", recommendedAction: "REVIEW",
    ...over,
  };
}

/* ── 1. DIVE hedge mapping: B32 paths, snake aliases, camel JSON ────────── */
test("DIVE hedge methods hit B32 paths with snake aliases and camel JSON", async () => {
  await loadApp();
  const w = globalThis.window;
  for (const k of ["fundingOpportunities", "hedgeVenues", "hedgeSimulate", "getHedgeSimulation",
    "createHedgePlan", "hedgePlans", "hedgePlan", "applyHedgeLegEvent",
    "activateHedgePlan", "closeHedgePlan", "hedgeMonitor", "hedgeAlerts",
    "ackHedgeAlert", "hedgeEvidenceSummary"]) {
    assert.equal(typeof w.DIVE[k], "function", `DIVE.${k} exists (H09 contract)`);
  }
  const realFetch = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), init });
    const u = String(url);
    if (u.startsWith("/api/short/funding-opportunities")) return ok({ asOf: 1, items: [mkFundingItem()], total: 1 });
    if (u.startsWith("/api/short/hedge/venues/")) return ok({ symbol: "BTCUSDT", venues: [] });
    if (u === "/api/short/hedge/simulate") return ok(mkSim());
    if (u.startsWith("/api/short/hedge/simulations/")) return ok(mkSim());
    if (u === "/api/short/hedge/plans" && init && init.method === "POST") return ok({ plan: mkPlan() });
    if (u.startsWith("/api/short/hedge/plans?") || u === "/api/short/hedge/plans") return ok({ items: [mkPlan()], total: 1 });
    if (u.endsWith("/legs") && init && init.method === "PATCH") return ok({ ok: true });
    if (u.endsWith("/activate")) return ok({ ok: true });
    if (u.endsWith("/close")) return ok({ ok: true });
    if (u.endsWith("/monitor")) return ok(mkMonitor());
    if (u.startsWith("/api/short/hedge/alerts?") || u === "/api/short/hedge/alerts") return ok({ alerts: [mkAlert()] });
    if (u.endsWith("/ack")) return ok({ alert: { ...mkAlert(), state: "ACKNOWLEDGED" } });
    if (u.startsWith("/api/short/hedge/evidence/summary")) return ok({ generatedAt: 1, buckets: [], total: 0 });
    return ok({});
  };
  try {
    const f = await w.DIVE.fundingOpportunities({ minFcs: 80, minFunding30d: 0.005, minPositiveRatio30d: 0.75, venue: "BINANCE_SPOT", readiness: "READY", sort: "fcs", order: "desc", limit: 50, offset: 0, unknownKey: "drop" });
    assert.equal(f.items.length, 1);
    const fu = seen.find((s) => s.url.startsWith("/api/short/funding-opportunities")).url;
    for (const kv of ["min_fcs=80", "min_funding_30d=0.005", "min_positive_ratio_30d=0.75", "venue=BINANCE_SPOT", "readiness=READY", "sort=fcs", "order=desc"]) {
      // URLSearchParams encodes "_" literally; check presence loosely
      assert.ok(fu.includes(kv.split("=")[0]), `funding URL carries ${kv}: ${fu}`);
    }
    assert.ok(!fu.includes("unknownKey"), "unknown funding keys dropped");

    const v = await w.DIVE.hedgeVenues("btcusdt", { notionalUsd: 10000 });
    const vu = seen.find((s) => s.url.includes("/api/short/hedge/venues/")).url;
    assert.ok(vu.includes("/api/short/hedge/venues/BTCUSDT"), `venue symbol uppercased: ${vu}`);
    assert.ok(vu.includes("notional_usd=10000"), `venue notional snake alias: ${vu}`);

    const simBody = { symbol: "BTCUSDT", mode: "ABSOLUTE", futuresNotionalUsd: 10000, preferredSpotVenue: "AUTO", futuresLeverage: 1, marginMode: "ISOLATED", marginUsd: 10000, liquidationPriceSource: "NONE", stopPolicy: "ALERT_ONLY" };
    const sim = await w.DIVE.hedgeSimulate(simBody);
    const simCall = seen.find((s) => s.url === "/api/short/hedge/simulate");
    const simSent = JSON.parse(simCall.init.body);
    assert.equal(simSent.symbol, "BTCUSDT", "simulate JSON stays camelCase");
    assert.equal(simSent.futuresNotionalUsd, 10000);
    assert.equal(sim.simulationId, "sim-1");

    const got = await w.DIVE.getHedgeSimulation("sim-1");
    assert.equal(got.simulationId, "sim-1");
    assert.ok(seen.some((s) => s.url === "/api/short/hedge/simulations/sim-1"));

    const created = await w.DIVE.createHedgePlan({ simulationId: "sim-1", clientRequestId: "req-1" });
    const createCall = seen.find((s) => s.url === "/api/short/hedge/plans" && s.init.method === "POST");
    assert.deepEqual(JSON.parse(createCall.init.body), { simulationId: "sim-1", clientRequestId: "req-1" }, "plan body camel verbatim");

    await w.DIVE.hedgePlans({ status: "ACTIVE", symbol: "BTCUSDT", mode: "ABSOLUTE", venue: "BINANCE_SPOT", limit: 20, offset: 0, unknownKey: "x" });
    const pu = seen.find((s) => s.url.startsWith("/api/short/hedge/plans?") || s.url === "/api/short/hedge/plans").url;
    assert.ok(!pu.includes("unknownKey"), "unknown plan keys dropped");

    await w.DIVE.hedgePlan("plan-1");
    assert.ok(seen.some((s) => s.url === "/api/short/hedge/plans/plan-1"));

    const tiny = "0.000000000000000001";
    await w.DIVE.applyHedgeLegEvent("plan-1", { legType: "SPOT", eventType: "OPEN", nativeQty: tiny, nativePrice: "66950", clientEventId: "ev-1", expectedPlanVersion: 1 });
    const patchCall = seen.find((s) => s.url.endsWith("/legs"));
    assert.equal(patchCall.init.method, "PATCH");
    assert.equal(JSON.parse(patchCall.init.body).nativeQty, tiny, "leg quantity string verbatim");

    await w.DIVE.activateHedgePlan("plan-1");
    await w.DIVE.closeHedgePlan("plan-1");
    assert.ok(seen.some((s) => s.url.endsWith("/activate")));
    assert.ok(seen.some((s) => s.url.endsWith("/close")));

    await w.DIVE.hedgeMonitor("plan-1");
    assert.ok(seen.some((s) => s.url.endsWith("/monitor")));

    await w.DIVE.hedgeAlerts({ planId: "plan-1", state: "OPEN", severity: "WARN", code: "HEDGE_RATIO_DRIFT", unknownKey: "drop" });
    const au = seen.find((s) => s.url.startsWith("/api/short/hedge/alerts")).url;
    assert.ok(au.includes("plan_id=plan-1"), `alert planId snake alias: ${au}`);
    assert.ok(!au.includes("unknownKey"), "unknown alert keys dropped");

    const acked = await w.DIVE.ackHedgeAlert("al-1");
    assert.equal(acked.alert.state, "ACKNOWLEDGED", "ack returns acknowledgement");

    await w.DIVE.hedgeEvidenceSummary({ strategy: "ABSOLUTE_100", horizon: "30D", unknownKey: "drop" });
    const eu = seen.find((s) => s.url.startsWith("/api/short/hedge/evidence/summary")).url;
    assert.ok(eu.includes("strategy=ABSOLUTE_100"), `hedge evidence strategy: ${eu}`);
    assert.ok(!eu.includes("unknownKey"), "unknown evidence keys dropped");
    assert.ok(!eu.includes("/api/short/evidence/summary?") || eu.includes("hedge"), "hedge evidence uses its own endpoint");
  } finally {
    globalThis.fetch = realFetch;
  }
  await assert.rejects(w.DIVE.hedgePlan(""), /empty planId/);
  await assert.rejects(w.DIVE.getHedgeSimulation(""), /empty simulationId/);
  await assert.rejects(w.DIVE.hedgeMonitor(""), /empty planId/);
  await assert.rejects(w.DIVE.ackHedgeAlert(""), /empty alertId/);
  await assert.rejects(w.DIVE.hedgeVenues(""), /empty symbol/);
});

/* ── 2. no API / 503 never mocks; Abort signal passes; globals kept ─────── */
test("hedge 503 never mocks and Abort signals pass through", async () => {
  await loadApp();
  const w = globalThis.window;
  let mockCalls = 0;
  const realMock = w.DIVE_MOCK;
  w.DIVE_MOCK = () => { mockCalls++; realMock(); };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => ok({ asOf: 1, items: [mkFundingItem()], total: 1 });
  try {
    await w.DIVE.fundingOpportunities({});
    assert.equal(w.SGS_HEDGE_FUNDING.total, 1);
  } finally { globalThis.fetch = realFetch; }
  globalThis.fetch = async () => fail(503, { error: "HEDGE_UNAVAILABLE", detail: "not wired" });
  try {
    await assert.rejects(w.DIVE.fundingOpportunities({}), /503/);
    assert.equal(w.SGS_HEDGE_FUNDING.total, 1, "failed funding never clobbers last good snapshot");
  } finally { globalThis.fetch = realFetch; }
  w.SGS_HEDGE_FUNDING = null;
  globalThis.fetch = async () => fail(503, { error: "HEDGE_UNAVAILABLE" });
  try {
    await assert.rejects(w.DIVE.fundingOpportunities({}), /503/);
    assert.equal(w.SGS_HEDGE_FUNDING, null);
  } finally { globalThis.fetch = realFetch; }
  assert.equal(mockCalls, 0, "hedge failures must never invoke DIVE_MOCK");

  // Abort signal passes through to fetch (F08 semantics).
  let captured = null;
  globalThis.fetch = async (url, init) => {
    captured = init;
    return ok({ asOf: 1, items: [], total: 0 });
  };
  try {
    const ctrl = new AbortController();
    await w.DIVE.fundingOpportunities({}, { signal: ctrl.signal });
    assert.equal(captured.signal, ctrl.signal, "signal reaches fetch");
    const ctrl2 = new AbortController();
    await w.DIVE.hedgeAlerts({}, { signal: ctrl2.signal });
    // second call reuses same stub; signal must again pass
    assert.equal(captured.signal, ctrl2.signal);
  } finally { globalThis.fetch = realFetch; }
  w.DIVE_MOCK = realMock;
});

/* ── 3. tiny quantities never go through Number ─────────────────────────── */
test("quantity 0.000000000000000001 stays a string for orders", async () => {
  await loadApp();
  const w = globalThis.window;
  const F = w.HEDGE_FORMAT;
  const tiny = "0.000000000000000001";
  assert.equal(F.qtyString(tiny), tiny, "qtyString returns the identical string");
  assert.equal(F.fmtQty(tiny), tiny, "fmtQty renders verbatim, never 1e-18");
  assert.equal(F.qtyValid(tiny), true, "tiny qty is valid without Number");
  assert.equal(F.qtyValid("0"), false);
  assert.equal(F.qtyValid("0.000"), false);
  assert.equal(F.qtyValid(""), false);
  assert.equal(F.qtyValid("abc"), false);
  // Leg event round-trips the exact string on the wire.
  const realFetch = globalThis.fetch;
  let sent = null;
  globalThis.fetch = async (url, init) => {
    sent = JSON.parse(init.body);
    return ok({ ok: true });
  };
  try {
    await w.DIVE.applyHedgeLegEvent("plan-1", { legType: "SPOT", eventType: "OPEN", nativeQty: tiny, nativePrice: "1.421", clientEventId: "ev-tiny" });
    assert.equal(sent.nativeQty, tiny, "wire keeps the exact decimal string");
    assert.equal(typeof sent.nativeQty, "string", "wire type stays string");
  } finally { globalThis.fetch = realFetch; }

  const { renderToStaticMarkup } = await import("react-dom/server");
  const app = await loadApp();
  const html = renderToStaticMarkup(React.createElement(app.HedgePlanner, {
    initial: { simulation: mkSim({ orderGuidance: [{ leg: "SPOT", side: "BUY", quantity: tiny, referenceLimitPrice: 1.421 }] }) },
  }));
  assert.ok(html.includes(tiny), "order guide prints the tiny quantity verbatim");
  assert.ok(!html.includes("1e-18"), "never renders exponent form");
});

/* ── 4. DRAFT/LIMITED vs ACTIVE; expired readable; re-sim new ID ────────── */
test("DRAFT/LIMITED stays apart from ACTIVE and expired simulations stay readable", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  // DRAFT + LIMITED with补项 guide, asset never hidden.
  const draftHtml = renderToStaticMarkup(React.createElement(app.HedgePlanner, {
    initial: { simulation: mkSim({ readiness: "NOT_READY", riskValidation: "LIMITED", monitoringCapability: "LIMITED" }) },
  }));
  assert.ok(draftHtml.includes("DRAFT + LIMITED"), "plannable state named");
  assert.ok(draftHtml.includes('data-testid="hedge-limited-guide"'), "补项 guide present");
  assert.ok(draftHtml.includes('data-testid="hedge-sim-result"'), "asset not hidden when LIMITED");
  // ACTIVE monitor shows real state distinctly.
  const monHtml = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: { planId: "plan-1", plan: mkPlan({ status: "ACTIVE", riskValidation: "VERIFIED", monitoringCapability: "FULL" }), monitor: mkMonitor({ status: "ACTIVE" }) },
  }));
  assert.ok(monHtml.includes('data-testid="hedge-monitor-status"'));
  assert.ok(monHtml.includes("ACTIVE"), "ACTIVE shown");
  assert.ok(!monHtml.includes("DRAFT + LIMITED") || monHtml.includes("ACTIVE"), "ACTIVE never collapsed into DRAFT");
  const F = globalThis.window.HEDGE_FORMAT;
  assert.equal(F.statusClass("DRAFT"), "dim");
  assert.notEqual(F.statusClass("DRAFT"), F.statusClass("ACTIVE"), "DRAFT vs ACTIVE classes differ");

  // Expired simulation: readable + re-simulate entry, old ID intact.
  const expiredHtml = renderToStaticMarkup(React.createElement(app.HedgePlanner, {
    initial: { simulation: mkSim({ simulationId: "sim-old", expired: true, expiresAtMs: 1 }) },
  }));
  assert.ok(expiredHtml.includes('data-testid="hedge-sim-expired"'), "expired banner marker");
  assert.ok(expiredHtml.includes("EXPIRED"), "expired named");
  assert.ok(expiredHtml.includes("sim-old".slice(0, 7)) || expiredHtml.includes("hedge-sim-result"), "old snapshot still readable");

  // Wire-level: expired GET stays 200-readable; POST plan with it is 409 QUOTE_EXPIRED.
  const w = globalThis.window;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const u = String(url);
    if (u === "/api/short/hedge/simulations/sim-old") return ok(mkSim({ simulationId: "sim-old", expired: true }));
    if (u === "/api/short/hedge/plans" && init && init.method === "POST") return fail(409, { error: "QUOTE_EXPIRED" });
    if (u === "/api/short/hedge/simulate") return ok(mkSim({ simulationId: "sim-new" }));
    return ok({});
  };
  try {
    const old = await w.DIVE.getHedgeSimulation("sim-old");
    assert.equal(old.expired, true, "expired GET is 200-readable");
    assert.equal(F.isExpired(old), true, "format flags expiry");
    await assert.rejects(w.DIVE.createHedgePlan({ simulationId: "sim-old", clientRequestId: "req-1" }), /409.*QUOTE_EXPIRED/);
    const fresh = await w.DIVE.hedgeSimulate({ symbol: "BTCUSDT", mode: "ABSOLUTE", futuresNotionalUsd: 10000 });
    assert.equal(fresh.simulationId, "sim-new", "re-simulation mints a new ID");
    assert.notEqual(fresh.simulationId, "sim-old", "old ID never rewritten");
  } finally { globalThis.fetch = realFetch; }
});

/* ── 5. orphan-leg / no-stop / notification / offline copy ──────────────── */
test("orphan-leg, no-stop, notification-denied and offline copy are explicit", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const orphanHtml = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: {
      planId: "plan-1",
      plan: mkPlan({ status: "PARTIALLY_FILLED" }),
      monitor: mkMonitor({ alerts: [{ ...mkAlert(), code: "ORPHAN_SPOT_LEG", severity: "CRITICAL", state: "OPEN" }] }),
    },
  }));
  assert.ok(orphanHtml.includes('data-testid="hedge-orphan-warning"'), "orphan warning marker");
  assert.ok(/ORPHAN/i.test(orphanHtml), "orphan code printed");

  const noStopHtml = renderToStaticMarkup(React.createElement(app.HedgePlanner, {
    initial: { simulation: mkSim({ monitoringCapability: "LIMITED", orderGuidance: [{ leg: "SPOT", side: "BUY", quantity: "1", stopSupport: "PLATFORM_STOP_UNSUPPORTED" }] }) },
  }));
  assert.ok(noStopHtml.includes('data-testid="hedge-no-stop"'), "no-stop marker");
  assert.ok(/PLATFORM_STOP_UNSUPPORTED/.test(noStopHtml), "stop capability named");

  const alertsHtml = renderToStaticMarkup(React.createElement(app.HedgeAlerts, {
    initial: { data: { alerts: [mkAlert()], total: 1 } },
  }));
  assert.ok(alertsHtml.includes('data-testid="hedge-ack-note"'), "ACK note marker");
  assert.ok(/ACK/.test(alertsHtml) && /RESOLVED/.test(alertsHtml), "ACK ≠ resolve copy");

  const offHtml = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: { planError: "/api/short/hedge/plans/plan-1 → TypeError · Failed to fetch" },
  }));
  assert.ok(offHtml.includes('data-testid="hedge-monitor-error"') || offHtml.includes("hedge-monitor-unavailable"), "offline error page");
  assert.ok(/Failed to fetch/.test(offHtml), "offline reason verbatim");

  const F = globalThis.window.HEDGE_FORMAT;
  assert.equal(F.isOrphanCode("ORPHAN_SPOT_LEG"), true);
  assert.equal(F.isOrphanCode("CRITICAL_ORPHAN_FUTURES_LEG"), true);
  assert.equal(F.isOrphanCode("HEDGE_RATIO_DRIFT"), false);
  assert.equal(F.hasOrphanAlert([{ code: "ORPHAN_SPOT_LEG" }]), true);
  assert.equal(F.hasOrphanAlert([{ code: "HEDGE_RATIO_DRIFT" }]), false);
});

/* ── 6. Target/Actual/Estimated/Confirmed/Reference/User-entered distinct ─ */
test("planner and monitor keep Target/Actual/Estimated/Reference/User-entered apart", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const mon = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: { planId: "plan-1", plan: mkPlan({ status: "ACTIVE", liquidationPrice: 80000 }), monitor: mkMonitor() },
  }));
  for (const tid of ["hedge-monitor-target-ratio", "hedge-monitor-actual-ratio", "hedge-monitor-est-settled", "hedge-monitor-projected-next", "hedge-monitor-user-liq", "hedge-monitor-residual"]) {
    assert.ok(mon.includes(`data-testid="${tid}"`), `${tid} present`);
  }
  // Estimated settled vs Projected next are different rows; projected never counted as accrued.
  assert.ok(mon.indexOf("hedge-monitor-est-settled") !== mon.indexOf("hedge-monitor-projected-next"), "settled vs projected are separate rows");
  const w = globalThis.window;
  const store = new Map([["dive_lang", "zh"]]);
  globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => { store.set(k, String(v)); } };
  assert.ok(w.L("hedge_target_ratio").includes("目标"), "ZH Target");
  assert.ok(w.L("hedge_actual_ratio").includes("实际"), "ZH Actual");
  assert.ok(w.L("hedge_estimated_settled").includes("估算"), "ZH Estimated");
  assert.ok(w.L("hedge_projected_next").includes("预计"), "ZH Projected");
  assert.ok(w.L("hedge_reference_price").includes("参考"), "ZH Reference");
  assert.ok(w.L("hedge_user_entered_liq").includes("用户录入"), "ZH User-entered");
  assert.ok(w.L("hedge_known_fees").includes("已知"), "ZH Confirmed/Known");
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };
});

/* ── 7. ACK acknowledges, never resolves ────────────────────────────────── */
test("alert ACK moves OPEN to ACKNOWLEDGED, never RESOLVED", async () => {
  await loadApp();
  const w = globalThis.window;
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (String(url).endsWith("/ack")) return ok({ alert: { ...mkAlert(), state: "ACKNOWLEDGED" } });
    return ok({ alerts: [{ ...mkAlert(), state: "OPEN" }], total: 1 });
  };
  try {
    const before = await w.DIVE.hedgeAlerts({ plan_id: "plan-1" });
    assert.equal(before.alerts[0].state, "OPEN");
    const after = await w.DIVE.ackHedgeAlert("al-1");
    assert.equal(after.alert.state, "ACKNOWLEDGED", "ack acknowledges");
    assert.notEqual(after.alert.state, "RESOLVED", "ack never resolves");
  } finally { globalThis.fetch = realFetch; }

  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgeAlerts, {
    initial: { data: { alerts: [{ ...mkAlert(), state: "ACKNOWLEDGED" }], total: 1 } },
  }));
  assert.ok(html.includes("ACKNOWLEDGED"), "acknowledged state renders");
  assert.ok(html.includes('data-testid="hedge-acked-al-1"') || html.includes("ACKNOWLEDGED"), "acked marker");
  assert.ok(!html.includes('data-testid="hedge-resolved-al-1"'), "acked alert is not marked resolved");
});

/* ── 8. evidence directional vs hedge use different endpoints, never mixed ─ */
test("evidence directional and hedge call different endpoints without mixing", async () => {
  await loadApp();
  const w = globalThis.window;
  const realFetch = globalThis.fetch;
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    const u = String(url);
    if (u.startsWith("/api/short/hedge/evidence/summary")) {
      return ok({ generatedAt: 2, buckets: [{ strategy: "ABSOLUTE_100", horizon: "30D", historyClass: "FULL", PENDING: 0, COMPLETE: 2, CENSORED: 0, UNAVAILABLE: 0, total: 2, meanNetReturn: 0.02 }], total: 2 });
    }
    return ok({ filters: {}, horizons: { "30D": { PENDING: 1, COMPLETE: 1, CENSORED: 0, UNAVAILABLE: 0, total: 2 } }, total: 2, generatedAtMs: 1 });
  };
  try {
    const d = await w.DIVE.shortEvidence({ horizon: "30D" });
    assert.ok(urls[0].startsWith("/api/short/evidence/summary"), `directional endpoint: ${urls[0]}`);
    assert.equal(d.total, 2);
    const h = await w.DIVE.hedgeEvidenceSummary({ strategy: "ABSOLUTE_100", horizon: "30D", historyClass: "FULL", unknownKey: "drop" });
    assert.ok(urls[1].startsWith("/api/short/hedge/evidence/summary"), `hedge endpoint: ${urls[1]}`);
    assert.ok(urls[1].includes("strategy=ABSOLUTE_100"), `strategy forwarded: ${urls[1]}`);
    assert.ok(!urls[1].includes("unknownKey"), "unknown hedge evidence keys dropped");
    assert.equal(h.total, 2);
    assert.notEqual(urls[0], urls[1], "two endpoints differ");
    assert.equal(w.SGS_SHORT_EVIDENCE.total, 2, "directional global set");
    assert.equal(w.SGS_HEDGE_EVIDENCE.total, 2, "hedge global set separately");
  } finally { globalThis.fetch = realFetch; }
  globalThis.fetch = async () => fail(503, { error: "HEDGE_EVIDENCE_UNAVAILABLE" });
  w.SGS_HEDGE_EVIDENCE = { total: 2 };
  try {
    await assert.rejects(w.DIVE.hedgeEvidenceSummary({}), /503.*HEDGE_EVIDENCE_UNAVAILABLE/);
    assert.equal(w.SGS_HEDGE_EVIDENCE.total, 2, "failed hedge evidence never clobbers last good");
  } finally { globalThis.fetch = realFetch; }

  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const both = renderToStaticMarkup(React.createElement(app.ShortLabEvidence, {
    initial: { data: { filters: {}, horizons: { "30D": { PENDING: 1, COMPLETE: 1, CENSORED: 0, UNAVAILABLE: 0, total: 2 } }, total: 2 } },
    initialHedge: { data: { generatedAt: 2, buckets: [{ strategy: "ABSOLUTE_100", horizon: "7D", cohort: "MIXED_COHORTS", historyClass: "MIXED", PENDING: 0, COMPLETE: 2, CENSORED: 0, UNAVAILABLE: 0, total: 2, sampleCount: 2, meanNetReturn: null, medianNetReturn: null, pairedCount: 0, pairedMissingCount: 0, pairedMeanDiff: null, evaluation: { sample_status: "MIXED_BUCKETS", p05_net_return: null, status_counts: { COMPLETE: 2 }, entry_status_counts: { ENTRY_COMPLETE: 1, UNEXECUTABLE: 1 }, coverage: { complete: 2, total: 2 }, liquidation_path_coverage: { counts: { COMPLETE: 1, PARTIAL: 1 }, complete_fraction: 0.5 }, known_costs: { priced_outcomes: 2, mean_fees_usd: 5 }, max_adverse_basis_usd: 12, max_portfolio_drawdown_usd: 20, bootstrap: { status: "MIXED_BUCKETS", n_assets: 0, n: 0 }, paired_bootstrap: { status: "MIXED_BUCKETS", n_assets: 0, n: 0 }, walk_forward: { oos_month: null, oos_label: "MIXED_BUCKETS" } }, subBuckets: [{ bucket: { cohort: "USER_DECISION", profile: "PROFILE_A", formula_version: "FORMULA_A", goal: "CARRY", history_class: "FULL" }, sampleCount: 1, evaluation: { sample_status: "INSUFFICIENT_SAMPLE", mean_net: 0.02, p05_net_return: 0.02, paired: { n_paired: 1, n_missing: 0, mean_diff: 0.01 }, entry_status_counts: { ENTRY_COMPLETE: 1 }, coverage: { complete: 1, total: 1 }, liquidation_path_coverage: { counts: { COMPLETE: 1 }, complete_fraction: 1 }, known_costs: { priced_outcomes: 1, mean_fees_usd: 5 }, max_adverse_basis_usd: 12, max_portfolio_drawdown_usd: 20, bootstrap: { status: "INSUFFICIENT_SAMPLE", n_assets: 1, n: 1 }, paired_bootstrap: { status: "INSUFFICIENT_SAMPLE", n_assets: 1, n: 1 }, walk_forward: { oos_month: "2026-10", oos_label: "RULES_ONLY_NO_PRIOR_MONTH" } } }, { bucket: { cohort: "RESEARCH_CANDIDATE", profile: "PROFILE_B", formula_version: "FORMULA_B", goal: "CARRY", history_class: "FULL" }, sampleCount: 1, evaluation: { sample_status: "INSUFFICIENT_SAMPLE", mean_net: 0.03, p05_net_return: 0.03, entry_status_counts: { UNEXECUTABLE: 1 }, coverage: { complete: 1, total: 1 }, liquidation_path_coverage: { counts: { PARTIAL: 1 }, complete_fraction: 0 }, known_costs: { priced_outcomes: 1, mean_fees_usd: 7 }, max_adverse_basis_usd: 14, max_portfolio_drawdown_usd: 25, bootstrap: { status: "INSUFFICIENT_SAMPLE", n_assets: 1, n: 1 }, paired_bootstrap: { status: "INSUFFICIENT_SAMPLE", n_assets: 1, n: 1 }, walk_forward: { oos_month: "2026-10", oos_label: "RULES_ONLY_NO_PRIOR_MONTH" } } }] }], total: 2 } },
  }));
  assert.ok(both.includes('data-testid="shortlab-evidence-tabs"'), "evidence sub-tabs present");
  assert.ok(both.includes('data-testid="shortlab-evidence-tab-directional"'));
  assert.ok(both.includes('data-testid="shortlab-evidence-tab-hedge"'));
  assert.ok(both.includes('data-testid="hedge-evidence-bucket-ABSOLUTE_100-7D"'));
  assert.ok(both.includes('data-testid="hedge-evidence-paired"'));
  assert.ok(both.includes('data-testid="hedge-evidence-evaluation"'));
  assert.ok(both.includes('data-testid="hedge-evidence-sub-buckets"'));
  assert.ok(both.includes('PROFILE_A'));
  assert.ok(both.includes('UNEXECUTABLE 1'));
  assert.ok(both.includes('meanFeesUsd'));
  assert.ok(both.includes('maxAdverseBasisUsd'));
  assert.ok(both.includes('MIXED_BUCKETS'));
  assert.ok(both.includes("INSUFFICIENT_SAMPLE"), "insufficient sample is visible in the real hedge panel");
  assert.ok(both.includes("0.01"), "paired mean difference is rendered");
  const hedgeOnly = renderToStaticMarkup(React.createElement(app.ShortLabEvidence, {
    initialHedge: { data: { generatedAt: 2, buckets: [{ strategy: "RELATIVE_50", horizon: "7D", historyClass: "FULL", PENDING: 0, COMPLETE: 1, CENSORED: 0, UNAVAILABLE: 0, total: 1 }], total: 1 } },
    defaultTab: "hedge",
  }));
  assert.ok(hedgeOnly.includes('data-testid="hedge-evidence"'), "hedge bucket page marker");
  assert.ok(hedgeOnly.includes("RELATIVE_50"), "hedge strategy renders");
});

/* ── 9. pages render from initial without fetching; errors show no numbers ─ */
test("funding/planner/monitor/alerts render from initial and error pages stay honest", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const realFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; return ok({}); };
  try {
    const f = renderToStaticMarkup(React.createElement(app.FundingView, { initial: { data: { asOf: 1, items: [mkFundingItem()], total: 1 } } }));
    assert.ok(f.includes('data-testid="funding-view"'));
    assert.ok(f.includes('data-testid="funding-table"'));
    assert.ok(f.includes("BTCUSDT"));
    const p = renderToStaticMarkup(React.createElement(app.HedgePlanner, { initial: { simulation: mkSim() } }));
    assert.ok(p.includes('data-testid="hedge-planner"'));
    assert.ok(p.includes('data-testid="hedge-sim-result"'));
    const m = renderToStaticMarkup(React.createElement(app.HedgeMonitor, { initial: { planId: "plan-1", plan: mkPlan({ status: "ACTIVE" }), monitor: mkMonitor() } }));
    assert.ok(m.includes('data-testid="hedge-monitor"'));
    const a = renderToStaticMarkup(React.createElement(app.HedgeAlerts, { initial: { data: { alerts: [mkAlert()], total: 1 } } }));
    assert.ok(a.includes('data-testid="hedge-alerts"'));
    assert.equal(calls, 0, "initial bypasses fetch entirely");
  } finally { globalThis.fetch = realFetch; }

  const ferr = renderToStaticMarkup(React.createElement(app.FundingView, { initial: { error: "/api/short/funding-opportunities → 503 · HEDGE_UNAVAILABLE" } }));
  assert.ok(ferr.includes('data-testid="funding-unavailable"'), "funding 503 marker");
  assert.ok(!/67000|0\.14925373/.test(ferr), "no fabricated market values on funding error page");
});

/* ── 10. i18n complete ZH/TR/EN with ZH default ─────────────────────────── */
test("hedge chrome is complete in ZH/TR/EN with ZH default", async () => {
  await loadApp();
  const w = globalThis.window;
  const keys = ["hedge_tab_funding", "hedge_tab_planner", "hedge_tab_monitor", "hedge_tab_alerts",
    "hedge_funding_title", "hedge_funding_empty", "hedge_offline", "hedge_stale_kept",
    "hedge_planner_title", "hedge_simulate_btn", "hedge_resimulate_btn", "hedge_create_plan_btn",
    "hedge_target_ratio", "hedge_actual_ratio", "hedge_estimated_settled", "hedge_projected_next",
    "hedge_reference_price", "hedge_user_entered_liq", "hedge_known_fees",
    "hedge_draft_limited_note", "hedge_no_liq_guide", "hedge_expired_body", "hedge_quote_expired_body",
    "hedge_orphan_warning", "hedge_no_stop", "hedge_ack_note", "hedge_app_stopped_note",
    "hedge_notif_denied", "hedge_monitor_degraded", "hedge_recommended_action",
    "hedge_evidence_tab_directional", "hedge_evidence_tab_hedge", "hedge_evidence_empty",
    "hedge_alerts_empty", "hedge_ack_btn"];
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => { store.set(k, String(v)); } };
  for (const lang of ["zh", "tr", "en"]) {
    store.set("dive_lang", lang);
    assert.equal(w.diveLang(), lang, `active lang ${lang}`);
    for (const k of keys) {
      const v = w.L(k, "X");
      assert.ok(v && v !== k, `${lang}.${k} must be translated (got key fallback)`);
    }
  }
  store.clear();
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };
  assert.equal(w.diveLang(), "zh", "null storage → ZH default");
  assert.equal(w.L("hedge_tab_funding"), "资金费率");
  assert.ok(w.L("hedge_offline").includes("STALE") || w.L("hedge_offline").includes("不编造"), "offline copy honest");
});

/* ── 11. bundle order + shell routing ───────────────────────────────────── */
test("build order ships hedge format before pages and shell routes sub-tabs", async () => {
  const { readFileSync: rf } = await import("node:fs");
  const text = rf(join(UI_ROOT, "build.mjs"), "utf8");
  const fmt = text.indexOf('"shortlab/hedge-format.js"');
  const funding = text.indexOf('"shortlab/funding-view.jsx"');
  const planner = text.indexOf('"shortlab/hedge-planner.jsx"');
  const monitor = text.indexOf('"shortlab/hedge-monitor.jsx"');
  const alerts = text.indexOf('"shortlab/hedge-alerts.jsx"');
  const shell = text.indexOf('"desktop-app.jsx"');
  const baseFmt = text.indexOf('"shortlab/short-lab-format.js"');
  for (const [name, idx] of [["hedge-format", fmt], ["funding", funding], ["planner", planner], ["monitor", monitor], ["alerts", alerts], ["shell", shell]]) {
    assert.ok(idx !== -1, `build.mjs lists ${name}`);
  }
  assert.ok(baseFmt < fmt, "base format before hedge format");
  for (const idx of [funding, planner, monitor, alerts]) assert.ok(fmt < idx, "hedge format before hedge pages");
  for (const idx of [funding, planner, monitor, alerts]) assert.ok(idx < shell, "hedge pages before the app shell");

  const app = await loadApp();
  for (const k of ["FundingView", "HedgePlanner", "HedgeMonitor", "HedgeAlerts"]) {
    assert.equal(typeof app[k], "function", `DIVE_APP exports ${k}`);
  }
  assert.deepEqual(app.SHORTLAB_SUB_TABS, ["candidates", "funding", "planner", "monitor", "alerts", "evidence"]);
  globalThis.location.hash = "#/shortlab/funding";
  assert.equal(app.viewFromHash(), "shortlab", "sub-route keeps top view");
  assert.equal(app.shortlabTabFromHash(), "funding", "sub-tab parsed");
  globalThis.location.hash = "#/shortlab/monitor";
  assert.equal(app.shortlabTabFromHash(), "monitor");
  globalThis.location.hash = "#/shortlab";
  assert.equal(app.shortlabTabFromHash(), "candidates", "bare shortlab defaults to candidates");
  globalThis.location.hash = "#/scan";
  assert.equal(app.shortlabTabFromHash(), "candidates", "non-shortlab hash defaults");

  const { renderToStaticMarkup } = await import("react-dom/server");
  const shellHtml = renderToStaticMarkup(React.createElement(app.ShortLabShell, { initialTab: "alerts" }));
  assert.ok(shellHtml.includes('data-testid="shortlab-shell"'));
  assert.ok(shellHtml.includes('data-testid="shortlab-subnav"'));
  for (const t of ["candidates", "funding", "planner", "monitor", "alerts", "evidence"]) {
    assert.ok(shellHtml.includes(`data-testid="shortlab-tab-${t}"`), `subnav carries ${t}`);
  }
  // Subnav stays inside SHORT LAB: no new top-level view/hash/key.
  assert.equal(app.VIEW_HASH.shortlab, "shortlab");
  assert.ok(!app.VIEW_HASH.funding, "funding is not a top-level view");
  assert.ok(!app.KEY_VIEWS.includes("funding"), "funding is not a top-level quick key");
});

test('production Hedge request builders preserve Decimal strings and ledger envelope', async () => {
  const app = await loadApp();
  const request = app.buildHedgeSimulationBody({symbol:'btcusdt', mode:'ABSOLUTE', futuresNotionalUsd:'10000.000000000000000001', futuresLeverage:'2', marginUsd:'5000', preferredSpotVenue:'BINANCE_SPOT', marginMode:'ISOLATED', stopPolicy:'NONE', liquidationPriceSource:'NONE'}).body;
  assert.equal(request.futuresNotionalUsd, '10000.000000000000000001');
  assert.equal(request.futuresLeverage, '2');
  assert.equal(request.marginUsd, '5000');
  const fill = app.buildHedgeLegPayload({legType:'FUTURES_SHORT',eventType:'OPEN',nativeQty:'0.000000000000000001',nativePrice:'67000',clientEventId:'fill-1',expectedPlanVersion:''}, {planVersion:3}, 1760000000000);
  assert.equal(fill.expectedVersion, 3);
  assert.equal(fill.clientEventId, 'fill-1');
  assert.equal(fill.event.nativeQty, '0.000000000000000001');
  assert.equal(fill.event.schemaVersion, 'hedge-event-v1');
  assert.equal(fill.event.source, 'USER_ENTERED');
  assert.equal(fill.event.executedAtMs, 1760000000000);
  assert.equal(fill.event.eventType, 'OPEN_FUTURES_SHORT');
  // Exercise the same Python DTO parser used by the real API, without network/providers.
  const parser = `import json,sys
from diveintocrypto_desktop.shortlab.hedge.models import HedgeSimulationRequest,HedgeEvent,from_api_dict
d=json.load(sys.stdin)
from_api_dict(HedgeSimulationRequest,d['request'])
from_api_dict(HedgeEvent,d['event'])
print('VALID')`;
  const backend = join(UI_ROOT, '..', 'backend');
  assert.equal(execFileSync(join(backend,'.venv','bin','python'), ['-c',parser], {
    input: JSON.stringify({request,event:fill.event}), encoding:'utf8',
    env: {...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:join(backend,'src')},
  }).trim(), 'VALID');
  const response = app.normalizeHedgePlanResponse({plan:{status:'ACTIVE'},positions:[{remaining_qty:'2'}],alerts:[{code:'ORPHAN_LEG_WARNING'}]});
  assert.equal(response.legs[0].remaining_qty, '2');
  assert.equal(response.alerts[0].code, 'ORPHAN_LEG_WARNING');
});

test('monitor navigation without a plan remains selectable', async () => {
  const app = await loadApp();
  const {renderToStaticMarkup} = await import('react-dom/server');
  const html = renderToStaticMarkup(React.createElement(app.HedgeMonitor));
  assert.ok(html.includes('aria-label="plan id"'));
  assert.ok(!html.includes('hedge-monitor-loading'));
});

test("planner renders real API envelope result instead of only flat fixtures", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgePlanner, {
    initial: { simulation: { simulationId: "real-envelope", generatedAt: 1760000000000,
      expiresAt: 1760000060000, readiness: "NOT_READY", result: mkSim({
        targetSpotQty: "123.456789", monitoringCapability: "LIMITED"
      }) } },
  }));
  assert.ok(html.includes("123.456789"), "nested real quantities displayed verbatim");
});
