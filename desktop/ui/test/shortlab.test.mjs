/* ============================================================================
   Task 15 — React Short Lab Scanner/Detail (design §25–26, §35.5).

   Loads the REAL bundle sources (same esbuild pipeline as build.mjs,
   including shortlab/*) and pins:
     1. data.js Short-Lab adapters map UI keys → /api/short/* params verbatim
        (minFunding30d travels as backend decimal; percent conversion lives
        only in short-lab-format.js); failures never populate globals/mock.
     2. format helpers: decimal→% text, READY·LITE vs READY, "—" vs "N/A".
     3. table: EXCLUDED/PAUSED/BLOCKED print reasons, tier chips, N/A cells.
     4. view: 503 → own UNAVAILABLE page (no mock); stale → STALE + NOT_READY.
     5. routing gate: universe boot failure blocks old views but NOT shortlab;
        view id / hash / NAV / KEY_VIEWS / palette / polling all wired.
   Usage: npm test   (node --test, Node 18+)
   ========================================================================== */
import { test } from "node:test";
import assert from "node:assert/strict";
import esbuild from "esbuild";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";

const UI_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const APP = join(UI_ROOT, "src", "app");
/* Same file set and order as build.mjs — the bundle under test IS the bundle
   that ships in dist/. */
const FILES = [
  "data.js", "mock.js", "i18n.js",
  "shortlab/short-lab-format.js",
  "shortlab/short-lab-table.jsx",
  "shortlab/short-lab-detail.jsx",
  "shortlab/short-lab-view.jsx",
  "desktop-app.jsx",
];

let _seq = 100;
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
  const dir = join(UI_ROOT, "test", ".tmp");
  mkdirSync(dir, { recursive: true });
  const file = join(dir, `shortlab-${++_seq}.mjs`);
  writeFileSync(file, out.outputFiles[0].text);
  await import(pathToFileURL(file).href);
  return globalThis.DIVE_APP;
}

/* ── fixtures shaped like the frozen Task 14 API (§25.2) ─────────────────── */
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
function mkBody(items) {
  return {
    schemaVersion: "shortlab.api.v1", generationId: "job-20261001", generatedAtMs: 1760000001000,
    analysisTier: "LITE", scoreVersion: "ltss-lite-v1", items, total: items.length,
  };
}
function ok(body, status = 200) { return { ok: true, status, json: async () => body }; }
function fail(status, body = {}) { return { ok: false, status, json: async () => body }; }

/* ── 1. adapters: exact param mapping, honest globals, no mock ───────────── */
test("shortCandidates maps UI keys to API params and never touches mock on failure", async () => {
  await loadApp();
  const w = globalThis.window;
  assert.equal(w.SGS_SHORT, null, "SGS_SHORT starts null");
  assert.equal(typeof w.DIVE.shortCandidates, "function");
  assert.equal(typeof w.DIVE.shortDetail, "function");
  assert.equal(typeof w.DIVE.shortRefresh, "function");

  let mockCalls = 0;
  const realMock = w.DIVE_MOCK;
  w.DIVE_MOCK = () => { mockCalls++; realMock(); };

  const realFetch = globalThis.fetch;
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    return ok(mkBody([mkItem()]));
  };
  try {
    const res = await w.DIVE.shortCandidates({
      status: "READY", executionStatus: "BLOCKED", profile: "MEME_LITE",
      minLtss: 70, minFunding30d: 0.005, athDrawdownMin: -0.7, athDrawdownMax: -0.4,
      minDataQuality: 80, sort: "ltss", order: "desc", limit: 50, offset: 0,
      unknownKey: "must-be-dropped",
    });
    assert.equal(res.items.length, 1);
    const u = urls[0];
    assert.ok(u.startsWith("/api/short/candidates?"), `unexpected path: ${u}`);
    // decimals travel verbatim — 0.005 stays 0.005 (0.5%), never 0.5 or 50.
    for (const kv of ["status=READY", "execution_status=BLOCKED", "profile=MEME_LITE",
      "min_ltss=70", "min_funding_30d=0.005",
      "ath_drawdown_min=-0.7", "ath_drawdown_max=-0.4",
      "min_data_quality=80", "sort=ltss", "order=desc", "limit=50", "offset=0"])
      assert.ok(u.includes(kv), `URL must carry ${kv}: ${u}`);
    assert.ok(!u.includes("unknownKey"), "unknown keys are dropped, never forwarded");
    assert.equal(w.SGS_SHORT.items.length, 1, "success populates SGS_SHORT");
  } finally {
    globalThis.fetch = realFetch;
  }

  // failure: 503 leaves the global untouched and never calls mock.
  globalThis.fetch = async () => fail(503, { error: "shortlab_unavailable", reason: "db_not_migrated" });
  try {
    await assert.rejects(w.DIVE.shortCandidates({}), /503.*shortlab_unavailable/);
    assert.equal(w.SGS_SHORT.items.length, 1, "failed fetch must not clobber the last good snapshot");
  } finally {
    globalThis.fetch = realFetch;
  }
  globalThis.fetch = async () => fail(503, { error: "shortlab_unavailable" });
  w.SGS_SHORT = null;
  try {
    await assert.rejects(w.DIVE.shortCandidates({}), /503/);
    assert.equal(w.SGS_SHORT, null, "with no prior snapshot the global stays null");
  } finally {
    globalThis.fetch = realFetch;
  }
  assert.equal(mockCalls, 0, "Short-Lab failures must never invoke DIVE_MOCK");
});

test("shortDetail caches per symbol; shortRefresh POSTs jobType and surfaces existing", async () => {
  await loadApp();
  const w = globalThis.window;
  const realFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    const u = String(url);
    if (u.includes("/api/short/symbol/")) return ok({ ...mkItem({ symbol: "DOGEUSDT" }), generationId: "job-1", dataSources: {}, feature: null, entry: null });
    if (u.includes("/api/short/refresh")) return ok({ jobId: "job-9", jobType: "score_refresh", existing: true }, 202);
    return ok({});
  };
  try {
    const d = await w.DIVE.shortDetail("dogeusdt");
    assert.equal(d.symbol, "DOGEUSDT");
    assert.ok(calls[0].url.includes("/api/short/symbol/DOGEUSDT"), "symbol uppercased in path");
    assert.equal(w.SGS_SHORT_DETAIL.DOGEUSDT.symbol, "DOGEUSDT", "detail cached per symbol on success");
    const r = await w.DIVE.shortRefresh();
    assert.equal(r.existing, true, "in-flight refresh reuses the job (existing:true)");
    assert.equal(w.SGS_SHORT_JOB.jobId, "job-9");
    const post = calls.find((c) => c.url.includes("/api/short/refresh"));
    assert.equal(post.init.method, "POST");
    assert.equal(post.init.headers["Content-Type"], "application/json");
    await assert.rejects(w.DIVE.shortDetail(""), /empty symbol/, "empty symbol rejected, never fetched");
  } finally {
    globalThis.fetch = realFetch;
  }
});

/* ── 2. format: decimals↔% only here; "—" vs "N/A"; READY·LITE vs READY ───── */
test("format helpers convert decimals once and keep null apart from N/A", async () => {
  await loadApp();
  const F = globalThis.window.SL_FORMAT;
  assert.equal(F.pctInputToDecimal("0.5"), 0.005, "UI percent text → wire decimal");
  assert.equal(F.pctInputToDecimal("-70"), -0.7);
  assert.equal(F.pctInputToDecimal(""), null, "empty is null, never 0");
  assert.equal(F.pctInputToDecimal("abc"), null, "invalid is null, never coerced");
  assert.equal(F.decimalToPctInput(0.005), "0.5");
  assert.equal(F.fmtFundingDecimal(0.0125), "+1.25%");
  assert.equal(F.fmtFundingDecimal(null), "—");
  assert.equal(F.fmtAthDD(-0.58), "-58.0%");
  assert.equal(F.statusLabel("READY", "LITE"), "READY · LITE");
  assert.equal(F.statusLabel("READY", "FULL"), "READY");
  assert.equal(F.statusLabel("PAUSED", "LITE"), "PAUSED");
  assert.equal(F.fieldText(null, F.fmtRatio, "NOT_APPLICABLE"), "N/A", "confirmed absence");
  assert.equal(F.fieldText(null, F.fmtRatio, "UNAVAILABLE"), "—", "outage stays a dash");
  assert.equal(F.fieldText(null, F.fmtRatio, "PARTIAL"), "—");
  assert.equal(F.fieldText(6.2, F.fmtRatio), "6.20");
  assert.equal(F.availabilityLabel("NOT_APPLICABLE"), "N/A");
  assert.equal(F.availabilityLabel("UNAVAILABLE"), "UNAVAILABLE");
  assert.ok(F.DEFAULT_SORT_CAPTION.includes("READY > CANDIDATE > WATCH > PAUSED > BLOCKED > EXCLUDED"),
    "default-order caption matches §25.1");
});

/* ── 3. table: reasons on risk rows, tier chips, N/A vs "—" ───────────────── */
test("table prints risk reasons, tier chips and distinct N/A cells", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const items = [
    mkItem({ symbol: "DOGEUSDT", status: "READY", executionStatus: "READY", reasons: [] }),
    mkItem({ symbol: "PEPEUSDT", snapshotId: "snap-2", status: "PAUSED", executionStatus: "PAUSED", pauses: ["PAUSE_BREAKOUT_24H"], reasons: ["PAUSE_BREAKOUT_24H"], warnings: ["WARN_FUNDING_WEAKENING"] }),
    mkItem({ symbol: "XYZUSDT", snapshotId: "snap-3", status: "BLOCKED", executionStatus: "BLOCKED", vetoes: ["VETO_LOW_LIQUIDITY"], reasons: ["VETO_LOW_LIQUIDITY"], ltss: null, entryScore: null }),
    mkItem({ symbol: "NOSPOTUSDT", snapshotId: "snap-4", status: "CANDIDATE", metrics: { funding30d: null, positiveFundingRatio30d: null, athDrawdown: null, oiMarketCapRatio: null, futuresSpotVolumeRatio: null }, dataAvailability: { fundingHistory: "PARTIAL", spot: "NOT_APPLICABLE", fundamentals: "OK" } }),
  ];
  const html = renderToStaticMarkup(React.createElement(app.ShortLabTable, { items, onPick: () => {} }));
  assert.ok(html.includes("READY · LITE"), "LITE READY reads READY · LITE");
  assert.ok(html.includes("PAUSE_BREAKOUT_24H"), "PAUSED row prints its rule code");
  assert.ok(html.includes("VETO_LOW_LIQUIDITY"), "BLOCKED row prints its veto");
  assert.ok(html.includes("WARN_FUNDING_WEAKENING"), "warnings stay visible without changing status");
  assert.ok(html.includes("+1.25%"), "funding decimal renders as percent");
  assert.ok(html.includes("N/A"), "confirmed no-spot market renders N/A");
  assert.ok(html.includes("—"), "plain missing values render —");
  assert.ok(html.includes('data-testid="shortlab-table"'));
});

/* ── 4. view states: UNAVAILABLE page vs stale rows ───────────────────────── */
test("view renders its own UNAVAILABLE page on 503 — no mock, no fake numbers", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const html = renderToStaticMarkup(React.createElement(app.ShortLabView,
    { initial: { error: "/api/short/candidates → 503 · shortlab_unavailable · db_not_migrated" } }));
  assert.ok(html.includes('data-testid="shortlab-unavailable"'), "own UNAVAILABLE marker");
  assert.ok(html.includes("UNAVAILABLE"), "says unavailable");
  assert.ok(html.includes("TEKRAR DENE"), "retry button present");
  assert.ok(html.includes("mock") && html.includes("uydurmaz"), "states it never fabricates");
  assert.ok(!/84\.2|STRONG_SELL/.test(html), "no fabricated market values");
  assert.equal(globalThis.window.SGS_SHORT, null, "rendering the error page has no side effects on globals");
});

test("view flags top-level STALE + NOT_READY but keeps field staleness in detail", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const items = [
    mkItem({ stale: true, executionStatus: "NOT_READY", reasons: ["ENTRY_BELOW_READY_THRESHOLD", "READY_INPUT_STALE"] }),
  ];
  const html = renderToStaticMarkup(React.createElement(app.ShortLabView, { initial: { data: mkBody(items) } }));
  assert.ok(html.includes('data-testid="shortlab-stale"'), "stale chip at top level");
  assert.ok(html.includes("READY_INPUT_STALE"), "stale reason printed on the row");
  assert.ok(html.includes("NOT_READY") || html.includes("CANDIDATE"), "downgraded status visible");
  assert.ok(html.includes("DATA SOURCES"), "detail hint for field-level staleness");
});

/* ── 5. detail blocks: fixed sections, N/A vs UNAVAILABLE copy ────────────── */
test("detail renders the §26.4 blocks with independent N/A and UNAVAILABLE copy", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  const detail = {
    ...mkItem({ symbol: "DOGEUSDT", stale: true }),
    generationId: "job-1",
    dataSources: {
      funding_30d: { status: "OK", fetchedAtMs: 1760000000000, asOfMs: 1760000000000, coverageFraction: 1, reasonCode: null, source: "binance" },
      spot_volume_60d: { status: "NOT_APPLICABLE", fetchedAtMs: null, asOfMs: null, coverageFraction: 0, reasonCode: "no_spot_market", source: "binance-spot" },
      market_cap: { status: "UNAVAILABLE", fetchedAtMs: null, asOfMs: null, coverageFraction: 0, reasonCode: "coingecko_429", source: "coingecko" },
    },
    feature: { snapshotId: "f1", asOfMs: 1760000000000, featureVersion: "feat-v1", features: {}, sourceMeta: {}, dataQuality: 94 },
    entry: null,
  };
  const html = renderToStaticMarkup(React.createElement(app.ShortLabDetail, { symbol: "DOGEUSDT", initial: { data: detail } }));
  for (const block of ["SUMMARY", "STRUCTURE", "CARRY", "VALUATION", "TOKENOMICS", "EXISTING DIVE", "RISKS", "DATA SOURCES"])
    assert.ok(html.includes(block), `detail must render the ${block} block`);
  assert.ok(html.includes("N/A"), "confirmed absence renders N/A");
  assert.ok(html.includes("UNAVAILABLE"), "outage renders UNAVAILABLE — a different word");
  assert.ok(html.includes("STALE"), "stale snapshot flagged");
  assert.ok(html.includes("entryScore null") || html.includes("hesaplanmadı"), "null entry is honest, never 0");
});

/* ── 6. routing gate: boot failure blocks old views, not shortlab ─────────── */
test("shortlab is reachable by hash/NAV/palette/keys and bypasses the no-data gate", async () => {
  const app = await loadApp();
  const { renderToStaticMarkup } = await import("react-dom/server");
  assert.equal(app.VIEW_HASH.shortlab, "shortlab", "view id + hash fixed");
  globalThis.location.hash = "#/shortlab";
  assert.equal(app.viewFromHash(), "shortlab", "#/shortlab resolves");
  globalThis.location.hash = "#/scan";
  assert.equal(app.viewFromHash(), "scan", "old routes unaffected");
  assert.ok(app.NAV.some((n) => n.id === "shortlab"), "rail NAV carries SHORT LAB");
  assert.ok(app.KEY_VIEWS.includes("shortlab"), "quick-key list carries shortlab");
  assert.equal(app.shortlabBypassesNoData("shortlab"), true, "gate bypass for shortlab");
  assert.equal(app.shortlabBypassesNoData("scan"), false, "old views keep the gate");
  assert.equal(app.shortlabBypassesNoData("panel"), false);
  assert.equal(app.shortlabBypassesNoData("settings"), false, "settings keeps its own exemption path");

  const pal = renderToStaticMarkup(React.createElement(app.CommandPalette,
    { open: true, onClose: () => {}, onGo: () => {}, onPickSymbol: () => {}, onScan: () => {}, onEvidence: () => {}, onCompare: () => {}, onTheme: () => {}, onLang: () => {} }));
  assert.ok(pal.includes("SHORT LAB"), "command palette lists SHORT LAB");

  // universe boot failure + Short-Lab available: shortlab renders its own view.
  const failing = { universe: async () => { throw new Error("fetch failed: /api/universe → 451"); } };
  const boot = await app.bootUniverse(failing);
  assert.equal(boot.ok, false);
  assert.equal(globalThis.window.SGS_SHORT, null, "boot failure never fabricates Short-Lab data either");
  globalThis.location.hash = "#/shortlab";
  assert.equal(app.viewFromHash(), "shortlab", "hash still routes to shortlab after boot failure");
});
