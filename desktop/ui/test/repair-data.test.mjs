/* R12 data adapter contract (D12.1/D18.3/R10a HTTP schema).
 * Uses the REAL data.js with a fake fetch (never touches the network).
 * Covers: activate/close (planId, body, opts) with expectedVersion never
 * dropped, hedgeDecisionCreate/Get, hedgeExitGuidance, hedgeProtectionSave,
 * shortCapabilities. Bodies pass through verbatim; globals populate ONLY on
 * success; empty IDs throw; Abort signals pass through.
 * Usage: node --test desktop/ui/test/repair-data.test.mjs (workdir desktop/ui)
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import esbuild from "esbuild";

const UI_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const APP = join(UI_ROOT, "src", "app");

let _seq = 1200;
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
  const file = join(dir, `repair-data-${++_seq}.mjs`);
  writeFileSync(file, out.outputFiles[0].text);
  await import(pathToFileURL(file).href + `?t=${_seq}`);
  return globalThis.DIVE;
}

function ok(body, status = 200) { return { ok: true, status, json: async () => body }; }
function fail(status, body = {}) { return { ok: false, status, json: async () => body }; }

/* Plan-spec helper: capture the real activate wire request with fake fetch. */
async function captureActivateRequest(planId, body, opts) {
  const DIVE = await loadRealData();
  const calls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), body: init && init.body, method: init && init.method, signal: init && init.signal });
    return ok({ planId, status: "ACTIVE", planVersion: 2 });
  };
  try {
    await DIVE.activateHedgePlan(planId, body, opts);
  } finally {
    globalThis.fetch = realFetch;
  }
  return calls;
}

async function captureCloseRequest(planId, body, opts) {
  const DIVE = await loadRealData();
  const calls = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), body: init && init.body, method: init && init.method });
    return ok({ planId, status: "CLOSED", planVersion: 3 });
  };
  try {
    await DIVE.closeHedgePlan(planId, body, opts);
  } finally {
    globalThis.fetch = realFetch;
  }
  return calls;
}

test("R12 spec: captureActivateRequest sends expectedVersion verbatim", async () => {
  const calls = await captureActivateRequest("plan-b", { expectedVersion: 7 });
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].body), { expectedVersion: 7 });
  assert.equal(calls[0].url.endsWith("/plan-b/activate"), true);
});

test("R12: activate/close signature is (planId, body, opts) and never drops expectedVersion", async () => {
  const DIVE = await loadRealData();
  assert.equal(typeof DIVE.activateHedgePlan, "function");
  assert.equal(typeof DIVE.closeHedgePlan, "function");
  // New canonical form with explicit body + opts (signal passes through).
  const seen = [];
  const realFetch = globalThis.fetch;
  const ctrl = new AbortController();
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), init });
    return ok({ ok: true });
  };
  try {
    await DIVE.activateHedgePlan("plan-1", { expectedVersion: 3 }, { signal: ctrl.signal });
    const ac = seen.find((s) => s.url.endsWith("/activate"));
    assert.deepEqual(JSON.parse(ac.init.body), { expectedVersion: 3 }, "activate body verbatim");
    assert.equal(ac.init.signal, ctrl.signal, "activate signal passes");
    await DIVE.closeHedgePlan("plan-1", { expectedVersion: 4 }, {});
    const cc = seen.find((s) => s.url.endsWith("/close"));
    assert.deepEqual(JSON.parse(cc.init.body), { expectedVersion: 4 }, "close body verbatim");
    // Legacy single-arg still works (H09 regression) and sends {}.
    await DIVE.activateHedgePlan("plan-legacy");
    const leg = seen.filter((s) => s.url.endsWith("/activate")).pop();
    assert.deepEqual(JSON.parse(leg.init.body), {}, "legacy no-body sends {}");
  } finally {
    globalThis.fetch = realFetch;
  }
  await assert.rejects(DIVE.activateHedgePlan(""), /empty planId/);
  await assert.rejects(DIVE.closeHedgePlan(""), /empty planId/);
});

test("R12: close request mirrors activate contract", async () => {
  const calls = await captureCloseRequest("plan-c", { expectedVersion: 9 });
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].body), { expectedVersion: 9 });
  assert.equal(calls[0].url.endsWith("/plan-c/close"), true);
});

test("R12: hedgeDecisionCreate/Get bind frozen HTTP contract", async () => {
  const DIVE = await loadRealData();
  assert.equal(typeof DIVE.hedgeDecisionCreate, "function");
  assert.equal(typeof DIVE.hedgeDecisionGet, "function");
  const seen = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), init });
    const u = String(url);
    if (u === "/api/short/hedge/decisions" && init.method === "POST") {
      return ok({ decisionId: "dec-1", recommendation: "DATA_INSUFFICIENT" });
    }
    if (u === "/api/short/hedge/decisions/dec-1") {
      return ok({ decisionId: "dec-1", recommendation: "DATA_INSUFFICIENT" });
    }
    return ok({});
  };
  try {
    const body = { symbol: "1000PEPEUSDT", goal: "CARRY_CAPTURE", futuresNotionalUsd: "10000", plannedHoldDays: 30 };
    const created = await DIVE.hedgeDecisionCreate(body);
    const post = seen.find((s) => s.url === "/api/short/hedge/decisions");
    assert.deepEqual(JSON.parse(post.init.body), body, "decision body verbatim (camel, strings untouched)");
    assert.equal(created.decisionId, "dec-1");
    const got = await DIVE.hedgeDecisionGet("dec-1");
    assert.ok(seen.some((s) => s.url === "/api/short/hedge/decisions/dec-1"));
    assert.equal(got.decisionId, "dec-1");
  } finally {
    globalThis.fetch = realFetch;
  }
  await assert.rejects(DIVE.hedgeDecisionGet(""), /empty decisionId/);
  await assert.rejects(DIVE.hedgeDecisionCreate(null), /body must be an object/);
});

test("R12: hedgeExitGuidance binds GET exit-guidance", async () => {
  const DIVE = await loadRealData();
  assert.equal(typeof DIVE.hedgeExitGuidance, "function");
  const realFetch = globalThis.fetch;
  let urlSeen = "";
  globalThis.fetch = async (url) => {
    urlSeen = String(url);
    return ok({ planId: "plan-1", rules: { FUTURES_SHORT: { side: "BUY" }, SPOT_LONG: { side: "SELL" } } });
  };
  try {
    const g = await DIVE.hedgeExitGuidance("plan-1");
    assert.equal(urlSeen, "/api/short/hedge/plans/plan-1/exit-guidance");
    assert.ok(g.rules.FUTURES_SHORT && g.rules.SPOT_LONG, "bilateral rules present");
  } finally {
    globalThis.fetch = realFetch;
  }
  await assert.rejects(DIVE.hedgeExitGuidance(""), /empty planId/);
});

test("R12: hedgeProtectionSave requires expectedVersion and posts verbatim", async () => {
  const DIVE = await loadRealData();
  assert.equal(typeof DIVE.hedgeProtectionSave, "function");
  const seen = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), init });
    return ok({ confirmationId: "cf-1", resultingPlanVersion: 2 });
  };
  try {
    const body = {
      expectedVersion: 2,
      clientRequestId: "req-1",
      confirmedAtMs: 1791417600000,
      futures: { orderReference: "f-1", nativeQty: "10", triggerPrice: "0.01", triggerBasis: "MARK", status: "CONFIRMED" },
      spot: { exitMode: "MANUAL_EXIT_ONLY", nativeQty: "10", status: "CONFIRMED" },
    };
    const res = await DIVE.hedgeProtectionSave("plan-1", body);
    const post = seen.find((s) => s.url.endsWith("/protection"));
    assert.equal(post.url, "/api/short/hedge/plans/plan-1/protection");
    assert.deepEqual(JSON.parse(post.init.body), body, "protection body verbatim, expectedVersion kept");
    assert.equal(res.confirmationId, "cf-1");
  } finally {
    globalThis.fetch = realFetch;
  }
  await assert.rejects(DIVE.hedgeProtectionSave("plan-1", {}), /missing expectedVersion/);
  await assert.rejects(DIVE.hedgeProtectionSave("plan-1", { expectedVersion: 0 }), /missing expectedVersion/);
  await assert.rejects(DIVE.hedgeProtectionSave("", { expectedVersion: 1 }), /empty planId/);
});

test("R12: shortCapabilities binds GET /capabilities", async () => {
  const DIVE = await loadRealData();
  assert.equal(typeof DIVE.shortCapabilities, "function");
  const realFetch = globalThis.fetch;
  let urlSeen = "";
  globalThis.fetch = async (url) => {
    urlSeen = String(url);
    return ok({ contractSchemaVersion: "repair-contract-v1", readiness: "NOT_READY", reasons: ["IMPLEMENTATION_UNAVAILABLE"] });
  };
  try {
    const caps = await DIVE.shortCapabilities({});
    assert.equal(urlSeen, "/api/short/capabilities");
    assert.equal(caps.contractSchemaVersion, "repair-contract-v1");
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("R12: 503 never populates globals and signals pass through", async () => {
  const DIVE = await loadRealData();
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => ok({ contractSchemaVersion: "repair-contract-v1", readiness: "READY" });
  try {
    await DIVE.shortCapabilities({});
    assert.equal(globalThis.SGS_SHORT_CAPABILITIES.readiness, "READY");
  } finally {
    globalThis.fetch = realFetch;
  }
  globalThis.fetch = async () => fail(503, { error: "IMPLEMENTATION_UNAVAILABLE" });
  try {
    await assert.rejects(DIVE.shortCapabilities({}), /503/);
    assert.equal(globalThis.SGS_SHORT_CAPABILITIES.readiness, "READY", "failed capabilities never clobbers last good");
  } finally {
    globalThis.fetch = realFetch;
  }
  globalThis.SGS_SHORT_CAPABILITIES = null;
  globalThis.fetch = async () => fail(503, { error: "IMPLEMENTATION_UNAVAILABLE" });
  try {
    await assert.rejects(DIVE.shortCapabilities({}), /503/);
    assert.equal(globalThis.SGS_SHORT_CAPABILITIES, null);
  } finally {
    globalThis.fetch = realFetch;
  }
  // Signal passes for the new adapters.
  let captured = null;
  globalThis.fetch = async (url, init) => {
    captured = init;
    const u = String(url);
    if (u.includes("/exit-guidance")) return ok({ planId: "p" });
    if (u.includes("/decisions/")) return ok({ decisionId: "d" });
    return ok({});
  };
  try {
    const c1 = new AbortController();
    await DIVE.hedgeExitGuidance("p", { signal: c1.signal });
    assert.equal(captured.signal, c1.signal);
    const c2 = new AbortController();
    await DIVE.hedgeDecisionGet("d", { signal: c2.signal });
    assert.equal(captured.signal, c2.signal);
  } finally {
    globalThis.fetch = realFetch;
  }
});
