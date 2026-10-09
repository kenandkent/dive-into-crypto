# Testing

Dive Into Crypto has **four test suites** — two Python, one JavaScript, one Kotlin/Gradle.
All of them run offline by default; live-network tests are opt-in. This page consolidates the
former root-level `TEST_INFRA.md` and `TEST_READY.md`.

## Suite inventory

Test counts are **approximate** — they move as features land. The CI workflow
(`.github/workflows/ci.yml`) runs every suite on push to `main` and on every pull request.

| # | Suite | Command (from repo root) | ~Tests | What it covers |
|---|---|---|---|---|
| 1 | Backend engine | `cd desktop/backend && uv sync && uv run pytest -q` | ~1300 | The reference engine: all 60 indicators + 3 overlays, consensus/verdict logic, data parsers, cross-language parity fixtures, per-endpoint client behaviour (offline), plus the Short-Lab/Hedge contract suites |
| 2 | Root E2E | `uv run --project desktop/backend pytest tests/ -q` | ~95 | Opaque-box end-to-end behaviour against the packaged engine (offline: `tests/conftest.py` mocks the data layer), adversarial math, Gradle signing static analysis |
| 3 | UI | `cd desktop/ui && npm ci && npm test` (then `node build.mjs`) | ~96 | The "nothing is synthesised" guarantees of the React front-end: failed fetches never reach the demo generator, and demo mode always renders its banner + per-value markers; Short-Lab/hedge view contracts |
| 4 | Android | `cd android && ./gradlew :app:test` (JDK 17) | ~129 | The Kotlin engine unit tests, including the fixture-verified parity mirror of the Python reference |

CI additionally builds the debug APK (`:app:assembleDebug`) and enforces the **dist-drift
gate**: `desktop/ui/dist/` is committed on purpose (the backend serves it directly), so CI
rebuilds it and fails if the committed bundle differs from a fresh build.

## Running every suite locally

```bash
# 1 · Backend engine suite (~1300 tests, offline)
cd desktop/backend && uv sync && uv run pytest -q

# 2 · Root E2E suite (~95 tests, offline — data layer is mocked)
uv run --project desktop/backend pytest tests/ -q

# 3 · UI suite (~96 tests) + rebuild the committed bundle
cd desktop/ui && npm ci && npm test && node build.mjs

# 4 · Android unit tests (~129 tests, needs JDK 17)
cd android && ./gradlew :app:test
```

Or everything at once: `./run_tests.sh` (runs all four in order and prints a summary).

## Root E2E suite: tier methodology

The `tests/` suite is **opaque-box and requirement-driven** — it asserts behaviour through the
public interfaces, not implementation internals. Methodology: **Category-Partition + BVA +
Pairwise + Workload Testing**, organised in four tiers (plus static analysis):

| Tier | File | Focus | ~Tests |
|---|---|---|---|
| 1 | `tests/e2e/test_tier1_coverage.py` | Feature coverage — 5 tests per feature across the 7 tracked features | 35 |
| 2 | `tests/e2e/test_tier2_boundary.py` | Boundary & corner cases (BVA) — 5 tests per feature | 35 |
| 3 | `tests/e2e/test_tier3_pairwise.py` | Pairwise combinations of the major features | 7 |
| 4 | `tests/e2e/test_tier4_scenarios.py` | Real-world scenarios: high-volatility scan with WS caching, parallel-request cache throttling, WS disconnect + cache recovery, clean-room Gradle signing, regime shift (chop vs trend) | 5 |
| — | `tests/e2e/test_adversarial_math.py` | Adversarial/degenerate numeric inputs to the engine | — |
| — | `tests/static_analysis/test_gradle_signing.py` | Release-signing env fallback in `build.gradle.kts` (static analysis) | 1 |

The 7 tracked features: Zero-Lag EMA (ZLEMA), Bessel's correction (N−1 variance), O(N+M)
two-pointer data alignment, regex `swapKeywords` GC optimization, Binance WebSocket + local
cache, secure-keystore env fallback, and client–server parity (Z-Score thresholds / ADX regimes).

## Short-Lab acceptance tests

The Short-Lab contract tests live in `desktop/backend/tests/test_shortlab_*.py` (config,
repository, funding, quote volume, identity, spot, liquidity, lifecycle, providers,
scoring, status, entry, runtime, API, evidence, FULL) plus, for naming/packaging/release:

| File | What it pins |
|---|---|
| `test_shortlab_packaging.py` | distribution `short-lab-desktop` with both `short-lab` and `dive-desktop` scripts on one entry; UI package `short-lab-desktop-ui`; `/api/health` keeps `service` and gains `product: "short-lab"`; `short-lab.spec` EXE/COLLECT names, DuckDB + `default.yaml` + UI dist, fixed build command, no stale product paths; frozen read-only boots share one writable `shortlab.duckdb` home |
| `test_shortlab_release_workflow.py` | `v*` = Android-only, `short-lab-v*` = Desktop-only, ticked manual dispatch = Desktop artifact only; tag Desktop builds publish `short-lab-windows-x64.zip` to the Release |
| `test_shortlab_retention.py` | F09 base gate only (schema 4, no Hedge/005): `resources.read_resource_text` reads engine/default/identity/001–004 without a source checkout; spec lists exactly those resources with no 005 placeholder; read-only install + empty data dir boots twice consistently; `maintenance.maintain` (import `diveintocrypto_desktop.shortlab.maintenance`) only calls `maintain_retention` with 180-day TTL and limit ≤ 1000; tag mutual exclusion and argparse default `46408` (AST, not comments) |
| `tests/test_shortlab_repair_packaging.py` (R16, 根) | spec 全集 001–006/default/identity/DuckDB/UI；smoke 含 006 七表 + schema 6 + 二次启动同一 DB/只读树不变；UI dist 路由资源；CLI argparse 冻结；006 七表八索引 |
| `tests/static_analysis/test_shortlab_repair_release.py` (R16) | tag 互斥矩阵；frozen smoke 在 zip/发布前失败阻断；P06 证据 `if-no-files-found: error`；Node `>=22 <23` + `@playwright/test 1.56.0` + `test:repair-e2e` + release `setup-node: 22`；生产构建无 test 别名；中文边界 BLOCKED 禁宣传 |

Release acceptance (design §35–36, plan H11) runs, offline where possible:

```bash
# backend全套件 (incl. Short-Lab + Hedge, offline default; live excluded)
cd desktop/backend && uv run pytest -q

# 根 tests/ E2E (from repo root)
uv run --project desktop/backend pytest tests/ -q

# UI四文件 + 全量UI + 重新构建 (dist重建后UI测试重跑)
cd desktop/ui && node --test test/shortlab.test.mjs test/shortlab-refresh.test.mjs test/hedge.test.mjs test/v3.test.mjs
npm test && npm run build

# 成品smoke (只读安装目录 + 空用户数据, 真实进程health, 迁移到6,
# engine资源 + 006七表, plan登记, 二次启动同一DB/只读树不变; fixture stub, 非demo模式)
# CLI 参数以 argparse 为准 (--executable/--output-dir/--fixture)，交付 manifest.command 只记真实选项。
desktop/backend/.venv/bin/python scripts/smoke_shortlab_packaged.py \
  --executable <frozen-exe> --fixture \
  --output-dir desktop/backend/runtime/verification/<build-id>
```

### R16 默认能力、启用与边界（中文）

默认能力：Short-Lab 默认 `LITE`、默认 `hedge.enabled: false`，Funding/Hedge 开关默认关闭；grader/metrics 无默认自动作业，未接线证据端 503。启用靠 `SHORTLAB_CONFIG_PATH` + 环境变量 Key。研究评分为 `RULE_BASED_UNVALIDATED`；正费率检查默认开启，双负不得 READY；数量与强平按原生交易单位（含 1000 倍合约、tick/Dust），缺保护能力 UNKNOWN；执行靠手工录入 fill，支持部分退出；数据过期投射为 `CANDIDATE`/`NOT_READY`，`NO_HEDGE` 不能保存配对计划，证据不足为 `PENDING`/`CENSORED`/`UNAVAILABLE`。任何 BLOCKED 功能不得宣传为已实现；`deselected`/`live` 分别报告，缺网/缺 Key 记为 UNVERIFIED/UNCONFIGURED，不计入通过。工具链：Node `>=22 <23`，`@playwright/test 1.56.0` 为 dev 依赖（`test:repair-e2e`），不打进生产包。

Record the command, exit code and commit for every row of the §35 matrix; a row without
evidence is not marked done. `desktop/backend/runtime/verification/<build-id>/`
holds `manifest.json` (all 23 AC rows: task / source commit / AC line / command /
exit code / artifact SHA; CI run/artifact URLs stay null until real CI produces
them), `baseline-tests.txt`, `packaged-smoke.txt` (+`.json`) and
`release-routing.txt`. Unconfigured providers (Alpha / 0x without keys) are
recorded as `UNCONFIGURED`, and 451/unreachable live paths as `UNVERIFIED` —
neither is counted as passed. Live tests are recorded separately and never
merged into the offline gate:

```bash
cd desktop/backend && uv run pytest -m live -q   # network-gated, reported apart
```

Real PyInstaller bundling (`uv run --with pyinstaller
pyinstaller short-lab.spec --noconfirm`) and the read-only extraction smoke need a
networked Windows runner, so in an offline sandbox they stay statically covered and are
reported as not-executed — never as passed.

## Offline by default; live tests are opt-in

Everything above runs **offline**: the backend and root suites mock/stub the network layer, and
the UI suite runs on plain Node with no backend. Tests that genuinely hit live Binance endpoints
are marked `live` (see the `live` marker in `desktop/backend/pyproject.toml`) and are **excluded
from the default run and from CI**.

Run them explicitly when you want to exercise the real network path:

```bash
cd desktop/backend
uv run pytest -m live -q
```

> Binance market data is geo-restricted in some regions — a live-marker failure may be a network
> condition, not a code bug. It is shown as unavailable, never faked (see
> ["Nothing is synthesised"](../README.md#nothing-is-synthesised) in the README).
