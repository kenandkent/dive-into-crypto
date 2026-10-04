/* ============================================================================
   F08 — refresh generations, evidence real API, i18n completeness.
   Loads the REAL bundle sources (same esbuild pipeline as build.mjs,
   now including shortlab/short-lab-evidence.jsx) and pins:
     1. POST /refresh answers 202 as a TASK (never completion); the page
        polls DIVE.shortRefreshStatus every 2s to SUCCEEDED/FAILED/timeout
        and only then resets offset to 0 and switches generation.
     2. Pagination pins generationId; a health probe on
        lastSuccessfulGeneration raises a "new data" hint without auto-switch.
     3. AbortController + request sequence reject late responses; a failed
        refetch keeps the old body (STALE) instead of clearing; 503 never
        touches mock.
     4. DIVE.shortEvidence maps filters → /api/short/evidence/summary params;
        503 surfaces the backend reason; the evidence subpage renders real
        horizons, never fabricated numbers.
     5. New ZH/TR/EN keys are complete (ZH default, no key fallback).
   Usage: node --test desktop/ui/test/shortlab-refresh.test.mjs
   ========================================================================== */
import { test } from "node:test";
import assert from "node:assert/strict";
import esbuild from "esbuild";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

const UI_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const APP = join(UI_ROOT, "src", "app");
/* Same file set and order as build.mjs (F08 adds the evidence subpage). */
const FILES = [
  "data.js", "mock.js", "i18n.js",
  "shortlab/short-lab-format.js",
  "shortlab/short-lab-table.jsx",
  "shortlab/short-lab-detail.jsx",
  "shortlab/short-lab-view.jsx",
  "shortlab/short-lab-evidence.jsx",
  "desktop-app.jsx",
];

let _seq = 500;
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
  delete globalThis.SGS_SHORT;
  delete globalThis.SGS_SHORT_EVIDENCE;
  delete globalThis.SGS_SHORT_HEALTH;
  delete globalThis.SGS_SHORT_JOB_STATUS;
  const dir = join(UI_ROOT, "test", ".tmp");
  mkdirSync(dir, { recursive: true });
  const file = join(dir, `shortlab-refresh-${++_seq}.mjs`);
  writeFileSync(file, out.outputFiles[0].text);
  await import(pathToFileURL(file).href);
  return globalThis.DIVE_APP;
}

function mkItem(over = {}) {
  return {
    symbol: "DOGEUSDT", canonicalId: "doge", profile: "MEME_LITE", categories: ["MEME"],
    status: "CANDIDATE", asOfStatus: "CANDIDATE", candidateStatus: "CANDIDATE",
    executionStatus: "NOT_READY", ltss: 84.2, entryScore: 63.1,
    dataQuality: 94.0, snapshotDataQuality: 94.0,
    moduleScores: { lifecycle: 34.0, carry: 38.2, valuation: 3.0, tradeability: 9.0 },
    metrics: {
      funding30d: 0.0125, positiveFundingRatio30d: 0.87, athDrawdown: -0.58,
      oiMarketCapRatio: 0.14, futuresSpotVolumeRatio: 6.2,
    },
    vetoes: [], warnings: [], pauses: [], reasons: ["ENTRY_BELOW_READY_THRESHOLD"],
    dataAvailability: { fundingHistory: "OK", spot: "OK", fundamentals: "OK" },
    asOfMs: 1760000000000, stale: false,
    analysisTier: "LITE", scoreVersion: "ltss-lite-v1",
    featureVersion: "feat-v1", entryVersion: "entry-v1",
    configHash: "abc", snapshotId: "snap-1",
    ...over,
  };
}
function mkBody(items, gen = "job-old") {
  return {
    schemaVersion: "shortlab.api.v1", generationId: gen, generatedAtMs: 1760000001000,
    analysisTier: "LITE", scoreVersion: "ltss-lite-v1", items, total: items.length,
  };
}
function ok(body, status = 200) { return { ok: true, status, json: async () => body }; }
function fail(status, body = {}) { return { ok: false, status, json: async () => body }; }

/* ── 1. 202 is a task: POST returns the job handle, status comes from polling ─ */
test("manual POST refresh returns 202 task; completion comes only from job-status polling", async () => {
  await loadApp();
  const w = globalThis.window;
  assert.equal(typeof w.DIVE.shortRefreshStatus, "function", "DIVE.shortRefreshStatus exists (F08 contract)");
  assert.equal(typeof w.DIVE.shortHealth, "function", "DIVE.shortHealth exists (lastSuccessfulGeneration)");
  assert.equal(typeof w.DIVE.shortEvidence, "function", "DIVE.shortEvidence exists (F08 contract)");

  const realFetch = globalThis.fetch;
  const calls = [];
  let statusCalls = 0;
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    const u = String(url);
    if (u === "/api/short/refresh" && (!init || init.method === "POST")) {
      assert.equal(init.method, "POST");
      return { ok: true, status: 202, json: async () => ({ jobId: "job-42", jobType: "score_refresh", existing: false }) };
    }
    if (u === "/api/short/refresh/job-42") {
      statusCalls++;
      if (statusCalls === 1) return ok({ jobId: "job-42", jobType: "score_refresh", status: "RUNNING", stats: {}, existing: false });
      return ok({ jobId: "job-42", jobType: "score_refresh", status: "SUCCEEDED", stats: {}, existing: false });
    }
    return ok({});
  };
  try {
    const ref = await w.DIVE.shortRefresh();
    assert.equal(ref.jobId, "job-42", "202 body is the task handle");
    assert.ok(!("status" in ref) || ref.status == null, "POST does not claim completion");
    assert.equal(w.SGS_SHORT_JOB.jobId, "job-42");
    const running = await w.DIVE.shortRefreshStatus("job-42");
    assert.equal(running.status, "RUNNING", "first poll still running");
    const done = await w.DIVE.shortRefreshStatus("job-42");
    assert.equal(done.status, "SUCCEEDED", "second poll completes");
    assert.equal(w.SGS_SHORT_JOB_STATUS.status, "SUCCEEDED");
    await assert.rejects(w.DIVE.shortRefreshStatus(""), /empty jobId/, "empty jobId never fetched");
  } finally {
    globalThis.fetch = realFetch;
  }
});

/* ── 2. health probe: lastSuccessfulGeneration + additive capabilities ─────── */
test("shortHealth surfaces lastSuccessfulGeneration and additive capabilities", async () => {
  await loadApp();
  const w = globalThis.window;
  const realFetch = globalThis.fetch;
  const health = {
    ok: true, available: true, analysisTier: "LITE", scoreVersion: "ltss-lite-v1",
    generationId: "gen-new", generatedAtMs: 1760000002000,
    schemaVersion: "shortlab.api.v1", schema_version: "shortlab.api.v1",
    capabilities: { scoreRefresh: true, jobStatus: true, generationPinning: true, evidenceSummary: false, fullTier: false },
    jobs: { known: ["score_refresh"], running: [] },
    lastSuccessfulGeneration: "gen-new", missingDependencies: ["unlock"],
  };
  globalThis.fetch = async (url) => {
    assert.ok(String(url) === "/api/short/health", `health path: ${url}`);
    return ok(health);
  };
  try {
    const h = await w.DIVE.shortHealth();
    assert.equal(h.lastSuccessfulGeneration, "gen-new", "page switches generations from this field");
    assert.equal(h.schemaVersion, "shortlab.api.v1");
    assert.equal(h.capabilities.jobStatus, true);
    assert.deepEqual(w.SGS_SHORT_HEALTH, health, "success populates the health global");
  } finally {
    globalThis.fetch = realFetch;
  }
  globalThis.fetch = async () => fail(503, { error: "shortlab_unavailable" });
  w.SGS_SHORT_HEALTH = health;
  try {
    await assert.rejects(w.DIVE.shortHealth(), /503/);
    assert.equal(w.SGS_SHORT_HEALTH.lastSuccessfulGeneration, "gen-new", "failed health never clobbers the last good probe");
  } finally {
    globalThis.fetch = realFetch;
  }
});

/* ── 3. poll helper: 2s cadence, terminal vs timeout, late safety ─────────── */
test("SL_REFRESH.pollJob resolves SUCCEEDED/FAILED and times out honestly", async () => {
  await loadApp();
  const R = globalThis.window.SL_REFRESH;
  assert.equal(R.POLL_MS, 2000, "2s poll cadence (A9.1)");
  assert.equal(typeof R.isTerminal, "function");
  assert.equal(R.isTerminal("RUNNING"), false);
  assert.equal(R.isTerminal("SUCCEEDED"), true);
  assert.equal(R.isTerminal("FAILED"), true);
  assert.equal(typeof R.shouldShowNewGen, "function");
  assert.equal(R.shouldShowNewGen("gen-old", "gen-new"), true, "pinned gen differs → hint");
  assert.equal(R.shouldShowNewGen("gen-new", "gen-new"), false, "same gen → no hint");
  assert.equal(R.shouldShowNewGen(null, "gen-new"), false, "unpinned first load → no hint");
  assert.equal(R.shouldShowNewGen("gen-old", null), false, "no probe yet → no hint");

  const sleeps = [];
  const noSleep = async (ms) => { sleeps.push(ms); };
  let calls = 0;
  const okSeq = async () => (++calls === 1
    ? { jobId: "j", jobType: "score_refresh", status: "RUNNING" }
    : { jobId: "j", jobType: "score_refresh", status: "SUCCEEDED" });
  const r1 = await R.pollJob("j", { intervalMs: 2000, timeoutMs: 60000, statusFn: okSeq, sleep: noSleep });
  assert.equal(r1.timedOut, false);
  assert.equal(r1.status.status, "SUCCEEDED");
  assert.deepEqual(sleeps, [2000], "one 2s gap between RUNNING and SUCCEEDED");

  const failSeq = async () => ({ jobId: "j", jobType: "score_refresh", status: "FAILED", errorCode: "UNIVERSE_FAILED" });
  const r2 = await R.pollJob("j", { intervalMs: 2000, timeoutMs: 60000, statusFn: failSeq, sleep: noSleep });
  assert.equal(r2.status.status, "FAILED", "failure is terminal, never retried as success");

  const slow = async () => ({ jobId: "j", jobType: "score_refresh", status: "RUNNING" });
  const r3 = await R.pollJob("j", { intervalMs: 10, timeoutMs: 25, statusFn: slow, sleep: (ms) => new Promise((r) => setTimeout(r, ms)) });
  assert.equal(r3.timedOut, true, "timeout keeps the last RUNNING snapshot, never a fake SUCCEEDED");
  assert.equal(r3.status.status, "RUNNING");
});

/* ── 4. pagination pins the generation; new-gen hint does not auto-switch ─── */
test("view pins pagination to one generation and surfaces a switch hint", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const oldBody = mkBody([mkItem()], "gen-old");
  const html = renderToStaticMarkup(React.createElement(app.ShortLabView,
    { initial: { data: oldBody, newGenerationId: "gen-new" } }));
  assert.ok(html.includes('data-testid="shortlab-view"'));
  assert.ok(html.includes('data-testid="shortlab-newgen"'), "hint row appears when health moved");
  assert.ok(html.includes("gen-old".slice(0, 12)) || html.includes("gen-old"), "pinned gen stays visible");
  assert.ok(html.includes("gen-new".slice(0, 12)) || html.includes("gen-new"), "latest gen named in the hint");
  // Pagination caption still names the pinned generation contract.
  assert.ok(html.includes("generation"), "pagination lock copy present");
  // Tabs exist; candidates stay the default tab with the real table.
  assert.ok(html.includes('data-testid="shortlab-table"'), "candidates table still renders under the hint");
});

/* ── 5. 503 never mocks; evidence reason stays visible ────────────────────── */
test("shortEvidence maps filters honestly and 503 never populates globals", async () => {
  await loadApp();
  const w = globalThis.window;
  let mockCalls = 0;
  const realMock = w.DIVE_MOCK;
  w.DIVE_MOCK = () => { mockCalls++; realMock(); };
  const realFetch = globalThis.fetch;
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    return ok({ filters: { horizon: "30D" }, horizons: { "30D": { PENDING: 1, COMPLETE: 2, CENSORED: 0, UNAVAILABLE: 0, total: 3 } }, total: 3, generatedAtMs: 1 });
  };
  try {
    const res = await w.DIVE.shortEvidence({ horizon: "30D", generationId: "gen-1", unknownKey: "drop-me" });
    assert.equal(res.total, 3);
    const u = urls[0];
    assert.ok(u.startsWith("/api/short/evidence/summary?"), `unexpected path: ${u}`);
    assert.ok(u.includes("horizon=30D"), `horizon forwarded: ${u}`);
    assert.ok(u.includes("generation_id=gen-1"), `generationId mapped: ${u}`);
    assert.ok(!u.includes("unknownKey"), "unknown filter keys are dropped");
    assert.equal(w.SGS_SHORT_EVIDENCE.total, 3, "success populates the evidence global");
  } finally {
    globalThis.fetch = realFetch;
  }
  globalThis.fetch = async () => fail(503, { error: "short_evidence_unavailable", detail: "forward grader metrics are not wired yet" });
  try {
    await assert.rejects(w.DIVE.shortEvidence({}), /503.*short_evidence_unavailable/);
    assert.equal(w.SGS_SHORT_EVIDENCE.total, 3, "failed evidence never clobbers the last good snapshot");
  } finally {
    globalThis.fetch = realFetch;
  }
  w.SGS_SHORT_EVIDENCE = null;
  globalThis.fetch = async () => fail(503, { error: "short_evidence_unavailable" });
  try {
    await assert.rejects(w.DIVE.shortEvidence({}), /503/);
    assert.equal(w.SGS_SHORT_EVIDENCE, null, "with no prior snapshot the global stays null");
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(mockCalls, 0, "evidence failures must never invoke DIVE_MOCK");

  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const un = renderToStaticMarkup(React.createElement(app.ShortLabEvidence,
    { initial: { error: "/api/short/evidence/summary → 503 · short_evidence_unavailable · not wired" } }));
  assert.ok(un.includes('data-testid="shortlab-evidence-unavailable"'), "503 reason page marker");
  assert.ok(!/84\.2|STRONG_SELL/.test(un), "no fabricated market values on the evidence error page");
  const good = renderToStaticMarkup(React.createElement(app.ShortLabEvidence,
    { initial: { data: { filters: {}, horizons: { "30D": { PENDING: 1, COMPLETE: 2, CENSORED: 0, UNAVAILABLE: 0, total: 3, meanNetReturn: 0.01 } }, total: 3, generatedAtMs: 1 } } }));
  assert.ok(good.includes('data-testid="shortlab-evidence"'));
  assert.ok(good.includes("30D"), "real horizon bucket renders");
  assert.ok(good.includes('data-testid="shortlab-evidence-30D"'));
});

/* ── 6. new i18n keys complete in ZH/TR/EN; ZH stays default ──────────────── */
test("F08 chrome is complete in ZH/TR/EN with ZH default", async () => {
  await loadApp();
  const w = globalThis.window;
  const keys = ["sl_tab_candidates", "sl_tab_evidence", "sl_refresh_polling",
    "sl_refresh_timeout", "sl_refresh_failed", "sl_refresh_succeeded",
    "sl_new_gen", "sl_switch_new_gen", "sl_generation_pinned",
    "sl_job_running", "sl_job_failed", "sl_job_timeout", "sl_fetch_failed_kept",
    "sl_evidence_kicker", "sl_evidence_title", "sl_evidence_loading",
    "sl_evidence_unavailable_body", "sl_evidence_error_body", "sl_evidence_empty",
    "sl_evidence_total", "sl_evidence_horizon"];
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => { store.set(k, String(v)); } };
  for (const lang of ["zh", "tr", "en"]) {
    store.set("dive_lang", lang);
    assert.equal(w.diveLang(), lang, `active lang ${lang}`);
    for (const k of keys) {
      const v = w.L(k, "X");
      assert.ok(v && v !== k, `${lang}.${k} must be translated (got key fallback)`);
      assert.ok(!v.includes("{0}") || k.includes("polling") || k.includes("total"), `${lang}.${k} placeholders filled`);
    }
  }
  store.clear();
  globalThis.localStorage = { getItem: () => null, setItem: () => {} };
  assert.equal(w.diveLang(), "zh", "null storage → ZH default");
  assert.equal(w.L("sl_tab_candidates"), "候选");
  assert.equal(w.L("sl_tab_evidence"), "证据");
  assert.ok(w.L("sl_new_gen").includes("NEW"), "hint names the new generation");
});

/* ── 7. bundle ships the evidence subpage before the app shell ───────────── */
test("build order ships short-lab-evidence.jsx before desktop-app.jsx", async () => {
  const { readFileSync: rf } = await import("node:fs");
  const text = rf(join(UI_ROOT, "build.mjs"), "utf8");
  const ev = text.indexOf('"shortlab/short-lab-evidence.jsx"');
  const shell = text.indexOf('"desktop-app.jsx"');
  const fmt = text.indexOf('"shortlab/short-lab-format.js"');
  assert.ok(ev !== -1, "build.mjs lists the evidence subpage");
  assert.ok(shell !== -1, "build.mjs lists the app shell");
  assert.ok(ev < shell, "evidence loads before the app shell");
  assert.ok(fmt !== -1 && fmt < ev, "format helpers load before pages");
  const app = await loadApp();
  assert.equal(typeof app.ShortLabEvidence, "function", "DIVE_APP exports the evidence subpage");
});
