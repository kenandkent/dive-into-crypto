# Short-Lab Task 0 — Desktop Baseline Freeze (Phase 0)

> Scope: freeze the `short-meme` desktop baseline and pin two offline
> exchange source contracts for Task 3/4. No functional changes; no original
> test files were modified (only new files added under
> `desktop/backend/tests/` plus this document).

## 1. Baseline identity

- `git rev-parse HEAD`: `9b6146c0c5aa4e502696e11051b684a6367f064f`
  (`chore(platform): release pipeline, packaging, dependabot, i18n scaffolding docs`)
- `git branch --show-current`: `task/0-baseline`
- `git status --short` (before Task 0 writes): clean, no output.
- Versions:
  - System Python: `Python 3.13.13` (Homebrew miniconda)
  - Backend venv (`desktop/backend/.venv`, `uv run`): `CPython 3.12.11`
    (repo pins `3.12` via `desktop/backend/.python-version`)
  - `uv`: `0.7.6 (Homebrew 2025-05-19)`
  - `node`: `v24.18.0`, `npm`: `11.16.0`

## 2. Task 18 file existence (`rg --files` + direct check)

All nine paths Task 18 will modify exist; no substitute files were created:

| Path | Exists |
| --- | --- |
| `README.md` | yes |
| `desktop/README.md` | yes |
| `desktop/backend/README.md` | yes |
| `docs/api.md` | yes |
| `docs/packaging.md` | yes |
| `docs/testing.md` | yes |
| `desktop/backend/requirements-freeze.md` | yes |
| `desktop/backend/dive.spec` | yes |
| `.github/workflows/release.yml` | yes |

Note: plain `rg --files` (without `--hidden`) does not list `.github/`
because ripgrep skips hidden directories by default; `rg --files --hidden`
does list `.github/workflows/release.yml`, and `[ -e .github/workflows/release.yml ]`
confirms it exists. All other eight paths appear in plain `rg --files` output.

## 3. Regression baseline (commands, exit codes, summaries)

Backend `desktop/backend/tests/` and root `tests/` are different suites and
were run separately. The Android-inclusive root `run_tests.sh` was NOT run.

### 3a. Backend suite — `cd desktop/backend && uv run pytest -q`

- First attempt: exit 1 — environment/network failure, not a code failure:
  `uv` could not fetch the `hatchling` build backend from PyPI
  (`tls handshake eof` for `https://pypi.org/simple/hatchling/`).
  No tests ran. Full output kept in the agent's scratch log.
- After `uv sync --offline` (cache-backed, exit 0), retry of the exact
  command `uv run pytest -q`: **exit 0 — `307 passed, 6 deselected`**
  (6 deselected are `live` marker tests requiring Binance network access).
  Warning only: `StarletteDeprecationWarning` about `httpx` vs `httpx2` in
  `fastapi/testclient.py` (pre-existing, unrelated).
- Conclusion: backend regression baseline is GREEN.

### 3b. Root suite — `uv run --project desktop/backend pytest tests/ -q` (from repo root)

- **Exit 0 — `95 passed`**, same pre-existing `httpx` deprecation warning.
- Conclusion: root regression baseline is GREEN.

### 3c. UI — `cd desktop/ui && npm test && npm run build`

- `npm test` first run: exit 1 — environment failure, not a code failure:
  fresh worktree had no `node_modules`; `test/demo-mode.test.mjs` and
  `test/v3.test.mjs` failed with
  `Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'esbuild'`
  (4 pass / 2 fail of 6 file-level entries at that point).
- Ran `npm install` (exit 0; npm registry reachable). Side effect: it rewrote
  `desktop/ui/package-lock.json` header metadata (`0.1.0` → `0.3.0` version
  sync, no dependency change); this churn was reverted with
  `git checkout -- desktop/ui/package-lock.json` as it is outside Task 0 scope.
- `npm test` re-run: **exit 0 — `22 passed, 0 failed`**.
- `npm run build`: **exit 0** — `dist/bundle.js 306.6kb`,
  `✓ built dist/{bundle.js, styles.css, index.html}`; `git status` shows no
  `dist/` modifications (build is reproducible).
- Conclusion: UI test + build baseline is GREEN (after installing deps).

### 3d. Summary

| Suite | Command | Exit | Result |
| --- | --- | --- | --- |
| backend | `cd desktop/backend && uv run pytest -q` | 0 (retry; first attempt env-fail) | 307 passed, 6 live deselected |
| root | `uv run --project desktop/backend pytest tests/ -q` | 0 | 95 passed |
| UI test | `cd desktop/ui && npm test` | 0 (after `npm install`) | 22 passed |
| UI build | `cd desktop/ui && npm run build` | 0 | dist rebuilt, no diff |

Environment issues vs code failures are separated above: all observed
failures were missing-dependency/network environment issues in a fresh
worktree; zero code failures.

## 4. Pinned source contracts (offline fixtures)

New files (Task 3/4 reuse; raw exchange field names preserved, no DTO conversion):

- `desktop/backend/tests/fixtures/shortlab/funding_page.json` — raw
  USDⓈ-M `/fapi/v1/fundingRate` rows (`symbol/fundingTime/fundingRate/markPrice`),
  2 rows for `BTCUSDT` at `1699999999000` / `1700000000000`.
- `desktop/backend/tests/fixtures/shortlab/futures_1d_kline.json` — raw
  USDⓈ-M kline array; index 5 = base volume (`"1000"`), index 7 = quote
  asset volume (`"105000"`), deliberately different.
- `desktop/backend/tests/test_shortlab_source_contracts.py` — pins:
  1. funding page sorted ascending by `fundingTime`, next page resumes from
     `last fundingTime + 1`;
  2. `float(kline[5]) != float(kline[7])` (base vs quote volume);
  3. `int(kline[0]) < int(kline[6])` (open ms < close ms).
- Target test: `cd desktop/backend && uv run pytest -q tests/test_shortlab_source_contracts.py`
  → **exit 0, 2 passed**.

## 5. Funding pagination budget (Binance USDⓈ-M)

- Official `/fapi/v1/fundingRate` page cap: **1000 rows**, ascending by time,
  driven with `startTime/endTime/limit`; after a full 1000-row page resume
  from `last fundingTime + 1`, dedupe repeated timestamps, stop on empty page.
- Shared rate limit with `/fapi/v1/fundingInfo`: **500 requests / 5 min / IP**.
- Short-Lab self-imposed budget: **80 requests / 5 min**, max 80 symbols per
  5-minute batch. For a 500-symbol universe: `ceil(500/80) = 7` batches
  minimum → **at least 7 batches, shortest ≈ 30–35 minutes** (6 inter-batch
  waits × 5 min), excluding retries.
- Limits are variable (exchange may change them without notice): at runtime
  always honor **429 + `Retry-After`** with backoff and keep recording used
  requests plus remaining queue; a first backfill job may span batches.
  Never treat a partial window as a complete 30D/90D window.
