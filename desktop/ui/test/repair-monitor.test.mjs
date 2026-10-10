/* R12 Monitor state bindings (D12.1): generation/Abort, loadedPlanId vs
 * input, foreground 10s poll with background pause, single in-flight, STALE /
 * sourceAge, activate/close expectedVersion, frozen-leg fee currency, actual /
 * estimated funding + protection + bilateral exit guidance.
 * Pure helpers come from the REAL monitor-state.mjs (no ledger caching);
 * component behaviour comes from the REAL bundle sources with fake DIVE.
 * Usage: node --test desktop/ui/test/repair-monitor.test.mjs (workdir desktop/ui)
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import esbuild from "esbuild";
import * as MS from "../src/app/shortlab/monitor-state.mjs";

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
  "desktop-app.jsx",
];

let _seq = 1300;
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
    hidden: false,
    addEventListener() {}, removeEventListener() {},
  };
  globalThis.location = { hash: "" };
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };
  const dir = join(UI_ROOT, "test", ".tmp");
  mkdirSync(dir, { recursive: true });
  const file = join(dir, `repair-monitor-${++_seq}.mjs`);
  writeFileSync(file, out.outputFiles[0].text);
  await import(pathToFileURL(file).href + `?t=${_seq}`);
  return globalThis.DIVE_APP;
}

function mkPlan(over = {}) {
  return {
    planId: "plan-a", symbol: "BTCUSDT", status: "DRAFT",
    targetHedgeRatio: 1.0, canonicalFuturesQty: "0.5", targetSpotQty: "0.5",
    planVersion: 1, protectionStatus: "PENDING",
    legs: [
      { legType: "FUTURES_SHORT", priceCurrency: "USDC", feeCurrency: "USDC" },
      { legType: "SPOT_LONG", priceCurrency: "USDC", feeCurrency: "USDC" },
    ],
    ...over,
  };
}
function mkMonitor(over = {}) {
  return {
    planId: "plan-a", status: "ACTIVE", asOf: 1791417600000,
    actualHedgeRatio: 0.9, residualShortNotionalUsd: 100,
    estimatedSettledFundingUsd: "12.5", projectedNextFundingUsd: "1.2",
    spotPnlUsd: 10, futuresPnlUsd: -5, knownCostUsd: 2,
    netPnlBeforeExitUsd: "15.5", estimatedNetPnlAfterExitUsd: "13",
    ...over,
  };
}

/* ── A迟到: late A ignored after B started ─────────────────────────────── */
test("R12 monitor-state: late A response never overwrites B", async () => {
  let s = MS.createInitialMonitorState("");
  assert.equal(s.generation, 0);
  s = MS.beginPlanLoad(s, "plan-a");
  const genA = s.generation;
  assert.equal(s.loadedPlanId, null, "switch clears loadedPlanId immediately");
  assert.equal(s.plan, null, "switch clears risk display (no A-as-B)");
  s = MS.beginPlanLoad(s, "plan-b");
  const genB = s.generation;
  assert.ok(genB > genA, "generation bumps on switch");
  assert.ok(MS.isCurrent(genB, s.generation));
  assert.ok(!MS.isCurrent(genA, s.generation), "A generation is stale");
  // Late A success with old generation is ignored (pure helper returns same state).
  const afterLateA = MS.applyPlanSuccess(s, genA, "plan-a", mkPlan({ planId: "plan-a" }));
  assert.equal(afterLateA, s, "late A ignored by generation guard");
  // Current B success applies and takes loadedPlanId from the request target.
  const afterB = MS.applyPlanSuccess(s, genB, "plan-b", mkPlan({ planId: "plan-b", planVersion: 2 }));
  assert.equal(afterB.loadedPlanId, "plan-b");
  assert.equal(afterB.plan.planId, "plan-b");
});

/* ── B失败: B failure never leaves A displayed as B ─────────────────────── */
test("R12 monitor-state: B failure keeps nothing as B", async () => {
  let s = MS.createInitialMonitorState("");
  s = MS.beginPlanLoad(s, "plan-a");
  const genA = s.generation;
  s = MS.applyPlanSuccess(s, genA, "plan-a", mkPlan({ planId: "plan-a" }));
  assert.equal(s.loadedPlanId, "plan-a");
  // Switch to B clears A immediately.
  s = MS.beginPlanLoad(s, "plan-b");
  assert.equal(s.plan, null, "A cleared on switch");
  assert.equal(s.loadedPlanId, null);
  const genB = s.generation;
  const afterFail = MS.applyPlanFailure(s, genB, "plan-b", new Error("404"));
  assert.equal(afterFail.loadedPlanId, null, "failed B leaves no loaded plan");
  assert.equal(afterFail.plan, null, "no A-as-B");
  assert.match(afterFail.planError, /404/);
  // Late B success after a newer generation is still ignored.
  s = MS.beginPlanLoad(afterFail, "plan-c");
  const staleB = MS.applyPlanSuccess(s, genB, "plan-b", mkPlan({ planId: "plan-b" }));
  assert.equal(staleB, s, "stale B ignored");
});

/* ── partial: plan ok + monitor fail keeps plan, flags STALE, no write ─── */
test("R12 monitor-state: partial load keeps plan and flags STALE without writing", async () => {
  let s = MS.createInitialMonitorState("");
  s = MS.beginPlanLoad(s, "plan-a");
  const gen = s.generation;
  s = MS.applyPlanSuccess(s, gen, "plan-a", mkPlan({ planId: "plan-a" }));
  assert.equal(s.loadedPlanId, "plan-a");
  s = MS.applyMonitorFailure(s, gen, new Error("monitor 503"));
  assert.equal(s.plan.planId, "plan-a", "plan kept on monitor failure");
  assert.equal(s.monitor, null, "failed monitor never writes");
  assert.equal(s.stale, true, "kept values flagged STALE");
  assert.match(s.monitorError, /503/);
  // A monitor success under the same generation writes and clears STALE by age.
  const now = 1791417600000 + 5000;
  s = MS.applyMonitorSuccess(s, gen, "plan-a", mkMonitor({ asOf: 1791417600000 }), now);
  assert.equal(s.monitor.planId, "plan-a");
  assert.equal(s.monitorError, null);
  assert.equal(s.stale, false, "fresh monitor clears STALE");
  assert.equal(s.sourceAgeMs, 5000);
});

/* ── unmount: aborted / stale generation never writes ──────────────────── */
test("R12 monitor-state: unmounted (stale generation) responses never write", async () => {
  let s = MS.createInitialMonitorState("");
  s = MS.beginPlanLoad(s, "plan-a");
  const gen = s.generation;
  // Simulate unmount by bumping generation (component seqRef++ on new load/unmount).
  s = MS.beginPlanLoad(s, "plan-b");
  const latePlan = MS.applyPlanSuccess(s, gen, "plan-a", mkPlan({ planId: "plan-a" }));
  assert.equal(latePlan, s, "unmounted plan response ignored");
  const lateMon = MS.applyMonitorSuccess(s, gen, "plan-a", mkMonitor(), Date.now());
  assert.equal(lateMon, s, "unmounted monitor response ignored");
  const lateFail = MS.applyMonitorFailure(s, gen, new Error("x"));
  assert.equal(lateFail, s, "unmounted failure ignored");
});

/* ── hidden: background pauses poll, foreground resumes ────────────────── */
test("R12 monitor-state: hidden pauses poll, foreground single-flight polls", async () => {
  assert.equal(MS.shouldPoll({ hidden: true, loadedPlanId: "plan-a", inFlight: false }), false, "hidden never polls");
  assert.equal(MS.shouldPoll({ hidden: false, loadedPlanId: "plan-a", inFlight: false }), true, "foreground polls");
  assert.equal(MS.shouldPoll({ hidden: false, loadedPlanId: "plan-a", inFlight: true }), false, "single in-flight");
  assert.equal(MS.shouldPoll({ hidden: false, loadedPlanId: "", inFlight: false }), false, "no plan never polls");
  assert.equal(MS.shouldPoll({ hidden: false, loadedPlanId: null, inFlight: false }), false);
  assert.equal(MS.MONITOR_POLL_MS, 10000, "foreground 10s poll");
});

/* ── sourceAge / STALE ─────────────────────────────────────────────────── */
test("R12 monitor-state: sourceAge and STALE from Mark/Quote TTL", async () => {
  assert.equal(MS.computeSourceAge(1000, 500), 500);
  assert.equal(MS.computeSourceAge(1000, null), null, "unknown stays null, never 0-fill");
  assert.equal(MS.isStaleSource(5000), false);
  assert.equal(MS.isStaleSource(25000), true, "beyond 20s TTL is STALE");
  assert.equal(MS.isStaleSource(null), false, "unknown age never STALE");
  assert.equal(MS.isBeyondGrace(30000), false);
  assert.equal(MS.isBeyondGrace(70000), true, "beyond 60s grace");
  assert.equal(MS.resolveMonitorAsOf(mkMonitor({ asOf: 123 })), 123);
  assert.equal(MS.resolveMonitorAsOf({}), null);
});

/* ── 费用币种来自冻结腿 (USDC never rewritten to USDT) ─────────────────── */
test("R12 monitor-state: fee currency comes from frozen legs, never hardcoded USDT", async () => {
  assert.equal(MS.resolveFeeCurrency(mkPlan()), "USDC");
  assert.equal(
    MS.resolveLegCurrency(mkPlan(), "FUTURES_SHORT"),
    "USDC",
  );
  // Unknown stays null — never "USDT".
  assert.equal(MS.resolveFeeCurrency({ planId: "x", status: "DRAFT" }), null);
  assert.equal(MS.resolveFeeCurrency({ legs: [{ legType: "FUTURES_SHORT" }] }), null);
  // USDT legs still resolve honestly when actually frozen as USDT.
  assert.equal(
    MS.resolveFeeCurrency({ legs: [{ legType: "FUTURES_SHORT", priceCurrency: "USDT" }] }),
    "USDT",
  );
});

/* ── 实际/估算资金费 + 保护 + 双边退出指导 ─────────────────────────────── */
test("R12 monitor-state: actual vs estimated funding, protection, bilateral guidance", async () => {
  const full = MS.selectFundingDisplay(mkMonitor({
    actualFundingReceiptsUsd: "3.5",
    metrics: { funding_known_subtotal_usd: "12.5", funding_coverage: "3/3", funding_complete: true },
  }));
  assert.equal(full.estimated, "12.5");
  assert.equal(full.actual, "3.5", "actual receipts independent, never added to estimate");
  assert.equal(full.projected, "1.2");
  assert.equal(full.complete, true);
  assert.equal(full.partial, false);
  const partial = MS.selectFundingDisplay({
    estimatedSettledFundingUsd: null,
    projectedNextFundingUsd: "1.2",
    metrics: { funding_known_subtotal_usd: "7.0", funding_coverage: "2/3", funding_flags: ["FUNDING_COVERAGE_PARTIAL"] },
  });
  assert.equal(partial.estimated, null, "partial complete total stays null, never 0");
  assert.equal(partial.knownSubtotal, "7.0");
  assert.equal(partial.partial, true);
  assert.equal(MS.selectProtectionStatus(mkPlan(), null), "PENDING");
  assert.equal(MS.selectProtectionStatus({}, { protectionStatus: "CONFIRMED" }), "CONFIRMED");
  assert.equal(MS.selectProtectionStatus({}, null), "UNKNOWN", "unknown never faked");
  const g = MS.selectExitGuidance({ rules: { FUTURES_SHORT: { side: "BUY" }, SPOT_LONG: { side: "SELL" } } });
  assert.ok(g.futures && g.spot, "bilateral guidance grouped");
  assert.equal(g.futures.side, "BUY");
  assert.equal(g.spot.side, "SELL");
});

/* ── 组件: fee currency / activate-close expectedVersion / 显示 ─────────── */
test("R12 component: leg payload uses frozen-leg currency and keeps Decimal strings", async () => {
  const app = await loadApp();
  const tiny = "0.000000000000000001";
  const fill = app.buildHedgeLegPayload(
    { legType: "FUTURES_SHORT", eventType: "OPEN", nativeQty: tiny, nativePrice: "67000", clientEventId: "fill-1", expectedPlanVersion: "", feeCurrency: "", feeAmount: "", gasUsd: "" },
    mkPlan({ planVersion: 3 }),
    1760000000000,
  );
  assert.equal(fill.expectedVersion, 3);
  assert.equal(fill.event.nativeQty, tiny, "quantity string verbatim");
  assert.equal(fill.event.priceCurrency, "USDC", "currency from frozen legs, not hardcoded USDT");
  assert.equal(fill.event.feeCurrency, null, "empty fee stays unknown null, never 0");
  assert.equal(fill.event.feeAmount, null);
  const fund = app.buildHedgeLegPayload(
    { legType: "FUTURES_SHORT", eventType: "FUNDING_RECEIPT", amount: "5.5", clientEventId: "ev-f", expectedPlanVersion: "2" },
    mkPlan({ planVersion: 9 }),
    1760000000000,
  );
  assert.equal(fund.event.currency, "USDC", "funding currency from frozen legs");
  assert.equal(fund.event.amount, "5.5");
});

test("R12 component: monitor shows actual/estimated funding, protection and bilateral guidance", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: {
      planId: "plan-a",
      plan: mkPlan({ status: "ACTIVE", planVersion: 2, protectionStatus: "CONFIRMED" }),
      monitor: mkMonitor({ asOf: Date.now() - 5000, actualFundingReceiptsUsd: "3.5", estimatedSettledFundingUsd: "12.5" }),
      exitGuidance: { rules: { FUTURES_SHORT: { side: "BUY", quantity: "0.5" }, SPOT_LONG: { side: "SELL", quantity: "0.5" } } },
    },
  }));
  for (const tid of ["hedge-monitor", "hedge-monitor-target-ratio", "hedge-monitor-actual-ratio",
    "hedge-monitor-est-settled", "hedge-monitor-projected-next",
    "hedge-monitor-actual-funding", "hedge-monitor-protection-status",
    "hedge-monitor-exit-futures", "hedge-monitor-exit-spot", "hedge-monitor-source-age"]) {
    assert.ok(html.includes(`data-testid="${tid}"`), `${tid} present`);
  }
  assert.ok(html.includes("CONFIRMED"), "protection status shown");
  assert.ok(html.includes("FUTURES_SHORT") && html.includes("SPOT_LONG"), "bilateral guidance shown");
});

test("R12 component: stale age shows STALE + sourceAge, errors never fabricate numbers", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const staleHtml = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: {
      planId: "plan-a",
      plan: mkPlan({ status: "ACTIVE" }),
      monitor: mkMonitor({ asOf: Date.now() - 60000 }),
    },
  }));
  assert.ok(staleHtml.includes('data-testid="hedge-monitor-stale"') || staleHtml.includes("STALE"), "old quote flagged STALE");
  assert.ok(staleHtml.includes('data-testid="hedge-monitor-source-age"'), "sourceAge shown");
  const errHtml = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: { planError: "/api/short/hedge/plans/plan-x → 503 · IMPLEMENTATION_UNAVAILABLE" },
  }));
  assert.ok(errHtml.includes("hedge-monitor-unavailable") || errHtml.includes("hedge-monitor-error"));
  assert.ok(!/67000|0\.14925373/.test(errHtml), "error page shows no fabricated numbers");
});

test("R12 component: activate/close use loadedPlanId + expectedVersion (never dropped)", async () => {
  const app = await loadApp();
  const w = globalThis.window;
  const seen = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), init });
    const u = String(url);
    if (u.endsWith("/activate")) return { ok: true, status: 200, json: async () => ({ planId: "plan-a", status: "ACTIVE", planVersion: 2 }) };
    if (u.endsWith("/close")) return { ok: true, status: 200, json: async () => ({ planId: "plan-a", status: "CLOSED", planVersion: 3 }) };
    return { ok: true, status: 200, json: async () => ({}) };
  };
  try {
    await w.DIVE.activateHedgePlan("plan-a", { expectedVersion: 2 }, {});
    const ac = seen.find((s) => s.url.endsWith("/activate"));
    assert.deepEqual(JSON.parse(ac.init.body), { expectedVersion: 2 });
    await w.DIVE.closeHedgePlan("plan-a", { expectedVersion: 3 }, {});
    const cc = seen.find((s) => s.url.endsWith("/close"));
    assert.deepEqual(JSON.parse(cc.init.body), { expectedVersion: 3 });
  } finally {
    globalThis.fetch = realFetch;
  }
});

/* ── CR19 写守卫: 新鲜计划允许一切写入 ─────────────────────────────── */
test("CR19 component: fresh loaded plan enables activate/close/apply-leg", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: {
      planId: "plan-a",
      plan: mkPlan({ status: "ACTIVE", planVersion: 2 }),
      monitor: mkMonitor({ asOf: Date.now() - 5000 }),
    },
  }));
  assert.ok(html.includes('data-testid="hedge-monitor"'), "monitor renders");
  const disabled = html.match(/disabled=""/g) || [];
  assert.equal(disabled.length, 0, "fresh plan: no write button disabled");
});

/* ── CR19 写守卫: STALE 禁用一切写入 ───────────────────────────────── */
test("CR19 component: STALE source age disables every write", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: {
      planId: "plan-a",
      plan: mkPlan({ status: "ACTIVE", planVersion: 2 }),
      monitor: mkMonitor({ asOf: Date.now() - 60000 }),
    },
  }));
  assert.ok(html.includes('data-testid="hedge-monitor-stale"'), "STALE banner shown");
  const disabled = html.match(/disabled=""/g) || [];
  assert.equal(disabled.length, 3, "activate + close + apply-leg all disabled on STALE");
});

/* ── CR19 写守卫: 保留旧值 + monitor 失败同样禁写 ──────────────────── */
test("CR19 component: kept plan with monitor error disables every write", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: {
      planId: "plan-a",
      plan: mkPlan({ status: "ACTIVE", planVersion: 2 }),
      monitor: mkMonitor({ asOf: Date.now() - 5000 }),
      monitorError: "monitor 503",
    },
  }));
  assert.ok(html.includes('data-testid="hedge-monitor-kept-stale"'), "kept-STALE banner shown");
  const disabled = html.match(/disabled=""/g) || [];
  assert.equal(disabled.length, 3, "activate + close + apply-leg all disabled on monitor error");
});

/* ── CR19 写守卫: 过期指导 (D10) 禁用一切写入 ──────────────────────── */
test("CR19 component: expired exit guidance disables every write", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: {
      planId: "plan-a",
      plan: mkPlan({ status: "ACTIVE", planVersion: 2 }),
      monitor: mkMonitor({ asOf: Date.now() - 5000 }),
      exitGuidance: {
        expired: true,
        rules: { FUTURES_SHORT: { side: "BUY" }, SPOT_LONG: { side: "SELL" } },
      },
    },
  }));
  assert.ok(html.includes('data-testid="hedge-monitor-expired"'), "EXPIRED banner shown");
  const disabled = html.match(/disabled=""/g) || [];
  assert.equal(disabled.length, 3, "activate + close + apply-leg all disabled on expiry");
});

/* ── CR19 写守卫: 无有效版本禁用一切写入 ──────────────────────────── */
test("CR19 component: missing planVersion disables every write", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: {
      planId: "plan-a",
      plan: mkPlan({ status: "ACTIVE", planVersion: undefined }),
      monitor: mkMonitor({ asOf: Date.now() - 5000 }),
    },
  }));
  assert.ok(html.includes('data-testid="hedge-monitor"'), "monitor still renders");
  const disabled = html.match(/disabled=""/g) || [];
  assert.equal(disabled.length, 3, "activate + close + apply-leg all disabled without a version");
});

/* ── CR19 写守卫: 加载失败无任何写按钮 ─────────────────────────────── */
test("CR19 component: load failure renders no write buttons at all", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgeMonitor, {
    initial: { planError: "/api/short/hedge/plans/plan-x → 503 · IMPLEMENTATION_UNAVAILABLE" },
  }));
  assert.ok(!html.includes('data-testid="hedge-monitor"'), "no monitor body on load failure");
  assert.ok(!html.includes("disabled"), "no disabled write buttons — none rendered");
});

/* ── CR19 写守卫: 切换中 (loading) 无任何写按钮 ────────────────────── */
test("CR19 component: switching/loading renders no write buttons at all", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.HedgeMonitor, { planId: "plan-a" }));
  assert.ok(html.includes('data-testid="hedge-monitor-loading"'), "loading state renders");
  assert.ok(!html.includes('data-testid="hedge-monitor"'), "no monitor body while switching");
});

/* ── CR19 写守卫纯函数矩阵 ─────────────────────────────────────────── */
test("CR19 write guard: loaded object / error / stale / in-flight / version / expiry matrix", async () => {
  await loadApp();
  const guard = globalThis.HEDGE_MONITOR_WRITE_GUARD;
  assert.ok(guard && typeof guard.resolveWrite === "function", "guard exposed for tests");
  const base = {
    loadedPlanId: "plan-a", plan: { planId: "plan-a" },
    planErr: null, monErr: null, stale: false, expired: false,
    loading: false, writing: false, versionOk: true,
  };
  const R = (over) => guard.resolveWrite({ ...base, ...(over || {}) });
  assert.equal(R().disabled, false, "fresh loaded plan with version enables writes");
  assert.equal(R().reason, "", "enabled carries no reason");
  const denials = [
    [{ loadedPlanId: "" }, "empty loadedPlanId"],
    [{ loadedPlanId: null }, "null loadedPlanId"],
    [{ plan: null }, "missing loaded object"],
    [{ loading: true }, "switching/loading"],
    [{ writing: true }, "write in flight"],
    [{ planErr: "boom" }, "plan load failure"],
    [{ monErr: "503" }, "monitor failure"],
    [{ stale: true }, "STALE"],
    [{ expired: true }, "expired"],
    [{ versionOk: false }, "missing/invalid version"],
  ];
  for (const [over, label] of denials) {
    const r = R(over);
    assert.equal(r.disabled, true, `${label} disables writes`);
    assert.ok(typeof r.reason === "string" && r.reason.length > 0, `${label} carries a button title`);
  }
});

/* ── CR19: 所有请求携带已构造的 AbortSignal opts, 不再传 {} ────────── */
test("CR19: every hedge-monitor request carries the constructed AbortSignal opts (never {})", async () => {
  const src = readFileSync(join(APP, "shortlab", "hedge-monitor.jsx"), "utf8");
  for (const re of [
    /window\.DIVE\.hedgeMonitor\(pid,\s*\{\}\)/,
    /window\.DIVE\.hedgeExitGuidance\(pid,\s*\{\}\)/,
    /applyHedgeLegEvent\(pid,\s*ev,\s*\{\}\)/,
    /activateHedgePlan\(pid,\s*\{[^}]*\},\s*\{\}\)/,
    /closeHedgePlan\(pid,\s*\{[^}]*\},\s*\{\}\)/,
  ]) {
    assert.ok(!re.test(src), `no literal {{}} opts: ${re}`);
  }
  assert.ok(/window\.DIVE\.hedgeMonitor\(pid,\s*opts\)/.test(src), "poll monitor carries opts");
  assert.ok(/window\.DIVE\.hedgeExitGuidance\(pid,\s*opts\)/.test(src), "poll guidance carries opts");
  assert.ok(/applyHedgeLegEvent\(pid,\s*ev,\s*wOpts\)/.test(src), "leg write carries signal opts");
  assert.ok(/activateHedgePlan\(pid,\s*\{[^}]*\},\s*wOpts\)/.test(src), "activate carries signal opts");
  assert.ok(/closeHedgePlan\(pid,\s*\{[^}]*\},\s*wOpts\)/.test(src), "close carries signal opts");
});
