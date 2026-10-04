/* Build the desktop UI into a single offline bundle (no CDN, no in-browser Babel).
   Concatenates the prototype scripts (which rely on global load order, not ES
   modules) behind a prelude that pins React, then esbuild transpiles the JSX. */
import esbuild from "esbuild";
import { readFileSync, writeFileSync, mkdirSync, copyFileSync } from "fs";

const APP = "src/app";
const FILES = [
  "data.js",         // live backend adapter (window.DIVE + globals)
  "mock.js",         // offline demo fallback (window.DIVE_MOCK)
  "i18n.js",         // TR/EN catalogs + L() helper (window.DIVE_I18N / dive_lang)
  // Short-Lab views (Task 15): format helpers → table → detail → scanner view.
  // F08 adds the evidence subpage (real API, no mock). H09 adds hedge pages.
  // Order matters: format helpers load before pages, everything loads before
  // desktop-app.jsx which references these globals; nothing here may
  // fetch — all Short-Lab I/O goes through data.js (window.DIVE.*).
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
  "desktop-app.jsx", // "Depth Terminal" UI — shell + screens
];

const prelude =
  "import React from 'react';\n" +
  "import { createRoot } from 'react-dom/client';\n" +
  "const ReactDOM = { createRoot };\n" +
  "globalThis.React = React; globalThis.ReactDOM = ReactDOM;\n";

const contents =
  prelude +
  FILES.map((f) => `\n/* ==================== ${f} ==================== */\n` + readFileSync(`${APP}/${f}`, "utf8")).join("\n");

mkdirSync("dist", { recursive: true });

await esbuild.build({
  stdin: { contents, loader: "jsx", resolveDir: process.cwd() },
  bundle: true,
  format: "iife",
  minify: true,
  sourcemap: false,
  target: ["es2020"],
  jsx: "transform",
  jsxFactory: "React.createElement",
  jsxFragment: "React.Fragment",
  outfile: "dist/bundle.js",
  logLevel: "info",
});

copyFileSync("src/styles.css", "dist/styles.css");
copyFileSync("src/index.html", "dist/index.html");
console.log("✓ built dist/{bundle.js, styles.css, index.html}");
