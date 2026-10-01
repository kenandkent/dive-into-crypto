# short-lab Desktop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前 `short-meme` 分支把 short-lab 作为现有 FastAPI 与 React 桌面应用的内部研究功能交付，保留旧接口和 `dive-desktop` 命令，Android 不在范围内。

**Architecture:** Python `diveintocrypto_desktop.shortlab` 负责身份、外部 provider、特征、评分、风险、DuckDB 与后台作业；`api/app.py:create_app()` 注册同进程路由与生命周期。React 通过现有 `data.js` 访问 `/api/short/*`。长期 LTSS 与实时 Entry 分开计算，所有结论引用不可变 point-in-time 快照。

**Tech Stack:** Python 3.12、FastAPI、aiohttp、PyYAML、DuckDB、pytest、React、`desktop/ui/build.mjs`（使用 esbuild）、PyInstaller。

**Spec:** `docs/ShortLab_Detailed_Design_CN.md`，以该文件第 0–40 节及附录为唯一产品/公式合同。实现前通读第 1.5、4、8–10、15–21、24–25、30、33、35 节。

## Global Constraints

- 项目根目录：任务开始时 `git rev-parse --show-toplevel` 返回的 checkout 根目录。根目录外只读；任何 agent 不得改动、清理或新建根目录外文件。文件路径均相对此根目录解释。
- 在当前分支实施，保留现有未提交文件；不得切到 `main`，不得改 Android。
- 一个 FastAPI 进程；默认 `127.0.0.1:8780`；保留现有 `--host`/`--port`、旧 API、Python import 与 `uv run dive-desktop`；新增推荐别名 `uv run short-lab`。
- 外部 provider 失败不得影响 `/api/scan`；缺失数据为 null + 原因，绝不伪造 0；无交易密钥或自动下单。
- LITE 先上线；Unlock/Social 在 Phase 5 接入，Catalyst、FULL DQ 和 FULL score 在 Phase 6 一起启用。未齐全时仅展示 LITE。
- Score、状态、收益和 DQ 遵从设计第 8.2、15、17、18、21 节；不按个人判断调整阈值。测试固定 fixture，不在普通 CI 访问公网。
- 不修改 parity-locked 60 指标和 Existing Consensus 语义。所有使用未来数据的历史 Entry 视为验收失败。
- 完成一个任务后先跑它的目标测试，再提交该任务所拥有的文件；跨任务共享文件只由指定 owner 修改。并行 agent 不共用未完成的工作树修改；合并前按下述接口合同审查。

## Review Focus

1. 1000 倍合约与普通现货：价格除已验证倍率，quote volume/OI 不除倍率；Task 5、7 测试。
2. 历史 `build_symbol(end_ms)` 仍读当前 OI/ratio/funding：不得回填历史 Entry；Task 12、16 测试。
3. Funding/Spot 历史不足和 provider 429：返回 null、coverage/stale，不显示完整 30D/90D；Task 3、6、11 测试。
4. 低 LTSS 同时 PAUSE、MEDIUM identity、BLOCK：两个状态字段与展示 status 必须稳定；Task 11、14 测试。
5. 退市币从 live universe 消失：保留历史合同、outcome 标记 CENSORED、统计中列出删失数；Task 8、16 测试。

## 文件所有权与并行规则

下表中的 `shortlab/...` 均指 `desktop/backend/src/diveintocrypto_desktop/shortlab/...`，`data/...` 与 `api/...` 均指同一 Python package 下的目录，`ui/...` 指 `desktop/ui/...`。测试文件统一放在 `desktop/backend/tests/`，fixtures 放在 `desktop/backend/tests/fixtures/shortlab/`。只在依赖任务的接口和测试已合并后开始下一波；共享工作树最多同时安排 3 个写文件的子 agent，主 agent 负责审查和集成。

| Task | 独占修改范围 | 依赖 | 可并行组 |
| --- | --- | --- | --- |
| 0 | `docs/baseline.md`、只读验证 fixture | 无 | A |
| 1 | `shortlab/{models,config}.py`、`shortlab/default.yaml`、合同/配置测试 | 0 | B |
| 2 | `shortlab/paths.py`、`shortlab/repository.py`、DB 测试 | 1 | C |
| 3 | `data/funding.py`、funding 测试 | 0 | B |
| 4 | `data/binance_klines.py`、K 线测试 | 0 | B |
| 5 | `shortlab/identity/*`、identity 测试 | 0 | B |
| 6 | `data/spot.py`、Spot 测试 | 1、4、5 | C |
| 7 | `data/open_interest.py`、`data/orderbook.py`、单位测试 | 3 | C |
| 8 | `data/universe.py`、生命周期测试 | 0 | B |
| 9 | `shortlab/providers/{base,coingecko}.py`、CoinGecko 测试 | 1、5 | C |
| 10 | `shortlab/features/{lifecycle,carry,valuation,tradeability}.py`、`shortlab/scoring/*`、评分测试 | 3–9 | D |
| 11 | `shortlab/risk/*`、`shortlab/quality.py`、状态/DQ 测试 | 1、3、5–8、10 | E |
| 12 | `shortlab/entry.py`、Entry 测试 | 1、2、3、5、7、10 | E |
| 13 | `shortlab/runtime.py`、`scheduler.py`、`service.py`、作业测试 | 1、2、9–12 | F |
| 14 | `api/shortlab.py`、`api/app.py`、API 测试 | 13 | G |
| 15 | `ui/src/app/shortlab/*`、`data.js`、`desktop-app.jsx`、UI 测试 | 14 | H |
| 16 | `shortlab/evidence/*`、grader 与既有路由集成测试 | 2、3、8、13、14 | H，可与 15/17 并行 |
| 17 | Unlock/Social/Catalyst provider、`features/{supply,narrative}.py`、002/003 migrations、repository/FULL 接线测试 | 9–11、13、14；接线等 16 | H，纯实现可与 15/16 并行 |
| 18 | `pyproject.toml`、`dive.spec`→`short-lab.spec`、release workflow、运行文档 | 14–17 | I |

同一时间同一文件只能有一个 owner；Task 2 先独占 `pyproject.toml` 加 DuckDB，合并并停止修改后 Task 18 接管该文件完成 CLI/发行名。一个 agent 只能领取一个 owner 范围；任务 6 依赖任务 4 的 `qv` 合同，任务 10 只消费已合并的数据适配结果。任务 15 由 UI owner 修改 `data.js` 与 `desktop-app.jsx`，Backend owner 不碰它们。Task 18 是唯一打包/发布 owner。每个任务交付提交号、目标测试命令/结果、对设计章节的核对结果；失败或不可用必须附具体 fixture 与原因。

### 跨任务 DTO 合同（Task 1 冻结，Task 2/9/14 消费）

```python
@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    status: Literal["OK", "PARTIAL", "NOT_APPLICABLE", "UNAVAILABLE", "ERROR"]
    source: str
    fetched_at_ms: int
    as_of_ms: int | None
    data: T | None
    stale: bool
    reason_code: str | None
    error_message: str | None  # 脱敏内部诊断，API 不暴露

@dataclass(frozen=True)
class CandidateState:
    candidate_status: Literal["EXCLUDED", "WATCH", "CANDIDATE"]
    execution_status: Literal["NOT_READY", "READY", "PAUSED", "BLOCKED"]
    status: Literal["EXCLUDED", "WATCH", "CANDIDATE", "READY", "PAUSED", "BLOCKED"]
    reasons: tuple[str, ...]
    vetoes: tuple[str, ...]
    pauses: tuple[str, ...]
    warnings: tuple[str, ...]
```

SQL 与 API 仍使用设计第 19/25 节的字段名；Python snake_case 到 API camelCase 的转换只在 Task 14 做。`ProviderResult.data=None` 与 `status=NOT_APPLICABLE` 的组合不等于数值 0。任何 DTO 变更须先修改此合同、设计文档和消费任务，待 owner 同意后再合并。

## Phase 0：基线与数据可行性

### Task 0：冻结桌面基线与两项数据验证

**Files:** Create `docs/baseline.md`; Test `desktop/backend/tests/test_shortlab_source_contracts.py`。

**Interfaces:** Produces 固定 JSON fixture：`desktop/backend/tests/fixtures/shortlab/funding_page.json`（含 `fundingTime/fundingRate/markPrice`）、`desktop/backend/tests/fixtures/shortlab/futures_1d_kline.json`（base volume 与 quote asset volume 不同），供 Task 3/4 复用。

- [ ] 记录 `git rev-parse HEAD`、`git branch --show-current`、`git status --short`、Python/uv/Node 版本；不修改原项目测试代码。
- [ ] 用 `rg --files` 确认 Task 18 将修改的现有文件为根 `README.md`、`desktop/README.md`、`desktop/backend/README.md`、`docs/api.md`、`docs/packaging.md`、`docs/testing.md`、`desktop/backend/requirements-freeze.md`、`desktop/backend/dive.spec` 和 `.github/workflows/release.yml`；不存在的路径不能凭名称创建替代文件。
- [ ] 运行 `cd desktop/backend && uv run pytest -q`、`uv run --project desktop/backend pytest tests/ -q`、`cd desktop/ui && npm test && npm run build`；把命令、退出码、失败摘要写入 `docs/baseline.md`。环境问题与代码失败分开记录。
- [ ] 后端 `desktop/backend/tests/` 与根目录 `tests/` 是不同测试集；baseline 分别记录两者，不能把根 E2E 命令当作后端重复执行或删掉任一项。不要运行包含 Android 的根 `run_tests.sh`。
- [ ] 在 `test_shortlab_source_contracts.py` 固定两项断言：funding 页按 `fundingTime` 升序且可用最后时间推进；原始 K 线索引 5 的 base volume 与索引 7 的 quote volume 不同。
- [ ] `docs/baseline.md` 写明 Binance USDⓈ-M 官方 funding 页上限 1000、共享 500 次/5 分钟/IP；按 Short-Lab 80 次/5 分钟，500 币至少 7 个批次，最短约 30–35 分钟，不含重试。记录限制可变，运行时仍按 429/Retry-After 退避。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_source_contracts.py`；提交基线、fixture、测试。若环境不允许运行，保留完整错误输出，Phase 1 不得把该项写成通过。

Phase 0 固定样例断言如下；fixture 应保留交易所原始字段名，而非先转换成内部 DTO：

`funding_page.json`：

```json
[{"symbol":"BTCUSDT","fundingTime":1699999999000,"fundingRate":"0.00010000","markPrice":"27000.0"},
 {"symbol":"BTCUSDT","fundingTime":1700000000000,"fundingRate":"-0.00005000","markPrice":"27100.0"}]
```

`futures_1d_kline.json`：

```json
[1699920000000,"100","110","90","105","1000",1700006399999,"105000",10,"500","52500","0"]
```

```python
assert [row["fundingTime"] for row in funding_page] == sorted(row["fundingTime"] for row in funding_page)
assert float(futures_1d_kline[5]) != float(futures_1d_kline[7])
assert int(futures_1d_kline[0]) < int(futures_1d_kline[6])  # open/close ms
```

## Phase 1：独立基础与适配器（Task 1–9 可按依赖并行）

### Task 1：Short-Lab 配置合同

**Files:** Create `desktop/backend/src/diveintocrypto_desktop/shortlab/models.py`, `shortlab/config.py`, `shortlab/default.yaml`, `desktop/backend/tests/test_shortlab_config.py`, `desktop/backend/tests/test_shortlab_models.py`。

**Interfaces:** `load_shortlab_config(path: Path | None = None) -> ShortLabConfig`; `config_hash(config: ShortLabConfig) -> str`。`SHORTLAB_CONFIG_PATH` 可覆盖 package YAML；不得修改 `engine/loader.py`。

- [ ] 先写测试：默认 `universe.limit=500`、`shortlist_size=50`、`entry_depth_top=10`、`entry_concurrency=2`、Entry 预算 240、funding `80/5min`、`refresh.jitter_sec=300`、`refresh.supply_sec=21600`；用户 YAML 覆盖一个键时保留其余默认值；未知键、负 TTL、权重合计不为 100 抛配置异常。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_config.py`，确认至少一个断言先失败。
- [ ] 定义有类型的 config DTO，按设计第 24 节加载、递归合并已知键、校验阈值顺序与整数范围；规范化 YAML 值再 SHA256，密钥值不得进入 hash/log/API。
- [ ] 将设计第 24 节的六个 `score_weights` profile、八组 `quality_freshness_sec` 及 `quality_field_overrides_sec.supply_float` 原样放入 `default.yaml`；逐 profile 求和为 100，逐组 `grace > ttl > 0`。禁止只测 `entry_depth_top` 而遗漏评分权重与 supply 字段 6/12h TTL。
- [ ] 冻结 `models.py` 中 `ProviderResult`、`CandidateState` 和身份/特征 DTO；用 `test_shortlab_models.py` 验证构造、枚举、`reason_code`、脱敏 `error_message`，Task 2/6/9/11/14 只导入这份定义。规范化配置 hash 按设计第 24 节 canonical JSON 算法固定 golden value。
- [ ] 默认配置评分子树的 SHA256 golden 固定为 `4909ffe7d43c294983d313cff65c6d73125b921944e403a1e1e152b91a869786`；更改 `refresh`、`providers`、`SHORTLAB_*` 路径/密钥环境变量时 hash 不变，更改 `candidate.ready_entry` 时必须变化。
- [ ] 重跑目标测试。另断言 `engine.loader.load_config()` 在读取 Short-Lab 配置前后相同，证明旧引擎不受影响。

### Task 2：DuckDB 可写路径、schema 与仓库

**Files:** Create `shortlab/paths.py`, `shortlab/repository.py`, `shortlab/migrations/001_init.sql`, `desktop/backend/tests/test_shortlab_repository.py`；Modify `desktop/backend/pyproject.toml` 中 duckdb 依赖（Task 18 仅负责 CLI/打包字段）。

**Interfaces:** `resolve_data_dir(frozen: bool, env: Mapping[str,str]) -> Path`; `ShortLabRepository.open()/close()/migrate()/save_feature()/save_entry()/save_score()/list_candidates()/save_outcome()`；所有 public DB 方法 async，内部单线程 executor。

- [ ] 先写路径测试：源码默认 `desktop/backend/runtime`；模拟 `sys._MEIPASS` 和只读安装目录时 Windows 选择 `%LOCALAPPDATA%/short-lab`；`SHORTLAB_DATA_DIR` 覆盖时须验证可写，不落入 `_internal`。
- [ ] 写空库/重复迁移、不可变 `snapshot_id`、同一 symbol/as-of 不同 config 并存、score 引用 feature 与独立 Entry 快照、outcome 四态可存的测试；运行目标测试确认失败。
- [ ] 按设计第 19.2 节建 `sl_entry_snapshot`（冻结输入、六组件、字段级来源、四项 Dive 版本信息）与 `save_entry()`；score 的 `entry_snapshot_id` 非空时校验 Entry 已存在且 symbol/as-of 匹配，为 null 时 `entry_score/entry_version` 也必须为 null。
- [ ] `source_meta_json` 必须逐字段含 `status/fetched_at_ms/as_of_ms/coverage_fraction/reason_code/source`；写入时拒绝缺 READY/DQ 必需字段元数据的快照，读取旧缺元数据快照只能展示 unavailable，不能投影 READY。
- [ ] 测试同 `generation_id` 的批量 score 只在完整写入后可分页，按设计第 19 节建包含 `sl_entry_snapshot(symbol,as_of_ms)` 的查询索引，应用层拒绝不存在的 feature/entry/score 引用；同 score/horizon 的不同成本 hash 并存。
- [ ] 事务故障注入：Entry/feature 已保存后写第 N 条 score 失败，整批 score 与 job `SUCCEEDED` 必须回滚，旧完整 generation 仍可查询；允许留下不可见的孤立 Entry/feature 快照。成功时整批 score 与 job `finished_at_ms/status` 同事务提交。
- [ ] 按设计第 19 节 DDL 创建表与索引；所有 DB 调用由单 worker 串行。迁移失败只令 Short-Lab unavailable，不由仓库直接终止 FastAPI。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_repository.py`；把 SQL schema 与设计表逐字段核对。

### Task 3：Funding 历史分页与覆盖率

**Files:** Modify `desktop/backend/src/diveintocrypto_desktop/data/funding.py`; Create `desktop/backend/tests/test_shortlab_funding_history.py`；数据库写入使用 Task 2 接口，不在本任务修改 repository。

**Interfaces:** `funding_history_range(symbol: str, start_ms: int, end_ms: int, limit: int = 1000) -> list[dict]`，每条包含 `t/funding_rate/mark_price`；`funding_coverage(events, start_ms, end_ms, onboard_ms) -> Coverage`；`premium_index()` 的 additive `time_ms` 为同步 OI 校验提供时标。

- [ ] 用 Task 0 fixture 写测试：`startTime/endTime/limit` 参数正确；1000 条满页后从最后 `fundingTime+1` 继续；重复时间去重；空页结束；429 honoring Retry-After；未满足完整窗口返回 null 与缺口。
- [ ] 加 1h/4h/8h 混合结算间隔 fixture：窗口首尾距边界不超过 24h、相邻间隔不超过 24h 且至少 3 个事件时 coverage=1；缺一段 >24h 时 funding 指标 null，`coverage_fraction` 按设计第 10.1 节计算。`first_seen` 不得用于缩短 90D 窗口。
- [ ] 运行目标测试确认失败；仅扩展原 `funding.py`，保留现有 `funding_hist()` 对旧调用者的行为。
- [ ] 通过共享 HTTP 路径发请求；另设 Short-Lab 80 次/5 分钟/IP 预算、每个 5 分钟批次最多 80 symbol；单个首次回填作业可跨批次继续，作业记录请求数与预计剩余队列。禁止将当前 predicted funding 当结算历史。`premium_index()` 追加响应 `time` 的 `time_ms` 字段；缺 `time` 时为 null，不以本机请求时间伪装交易所时标。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_funding_history.py` 和原 funding/scan 相关测试；检查 7/30/90D 实际结算窗口。

### Task 4：K 线 quote volume 保留

**Files:** Modify `desktop/backend/src/diveintocrypto_desktop/data/binance_klines.py`; Create `desktop/backend/tests/test_shortlab_quote_volume.py`。

**Interfaces:** 原 K 线 DTO 增加 `qv: float | None`（quote asset volume）；不改已有 `v` 的 base volume 语义。

- [ ] 用 Task 0 原始 K 线 fixture 写 `v != qv`、未收盘 K 线丢弃、`fetch_klines_range()` 同样保留 `qv` 的测试并确认失败。
- [ ] 在现有适配层从原始 Binance 响应第 8 项保留 `qv`；若 Crypcodile 解析对象未暴露该字段，用原始行的时间戳与解析结果对齐，不按位置假定已删除/未删除 K 线完全对应；不改 60 指标的输入语义。
- [ ] 原始 K 线用开盘时间建字典对齐，重复时间或解析后缺时间时对应 `qv=null` 并记数据质量原因；测试首尾未收盘 bar 删除、乱序、重复时间三种情况。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_quote_volume.py` 和原 K 线/扫描测试，确认旧 `{t,o,h,l,c,v}` 值未变化。

### Task 5：资产身份与倍率

**Files:** Create `shortlab/identity/resolver.py`, `shortlab/identity/overrides.py`, `shortlab/identity/asset_overrides.yaml`, `desktop/backend/tests/test_shortlab_identity.py`。

**Interfaces:** `resolve_identity(futures_symbol, exchange_meta, provider_candidates, overrides) -> AssetIdentity`; `canonical_price(futures_price, multiplier) -> float | None`。

- [ ] 测试唯一 symbol、多候选、`1000PEPEUSDT` 对 `PEPEUSDT`、无已验证倍率、MEDIUM/LOW/UNRESOLVED；确认无 fuzzy name 匹配。
- [ ] 手工覆盖只读版本化 YAML；自动绑定只在合约地址+链或唯一明确候选时允许 HIGH，倍率须带来源。数量/quote volume/OI 永不乘除倍率。
- [ ] 覆盖文件固定 `shortlab/identity/asset_overrides.yaml`；倍率仅来自明确交易所单位元数据或此文件的人工核实条目。`contract_metadata_all()` 返回 `contract_multiplier/multiplier_source` 或 null，缺可信倍率时 `MULTIPLIER_UNVERIFIED/NOT_READY`；测试不能凭 `1000` 前缀猜倍率。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_identity.py`，固定 `futures_price=0.006,multiplier=1000` 对应 canonical price `0.000006`。

### Task 6：Spot 60D 数据与期现单位

**Files:** Modify `desktop/backend/src/diveintocrypto_desktop/data/spot.py`; Create `desktop/backend/tests/test_shortlab_spot_history.py`。依赖 Task 4 的 `qv`、Task 5 的 `AssetIdentity`。

**Interfaces:** `spot_history(identity: AssetIdentity, as_of_ms: int) -> ProviderResult[SpotHistory]`，含前/后 30D quote volume、同步 24h quote volume、spot price、倍率归一 premium。

- [ ] 测试原 48h snapshot 行为仍成立；增加 61 根日线、前/后 30D 对齐、base/quote volume 不混用、无 spot 市场为 NOT_APPLICABLE、网络失败为 UNAVAILABLE。
- [ ] 固定 `as_of` 位于 UTC 当日中午的 fixture：只取此前最近 60 根已收盘 UTC 日线，前后各 30 根；期货/现货同日窗口，未收盘当日排除。无现货时期现量比分母及 premium 为 N/A，Carry 不加分，DQ 只从 Spot/liquidity 组内分母移除 40%+20%，basis/盘口仍计算。
- [ ] 在现有 `spot.py` 扩展获取，API symbol 只取 `identity.binance_spot_symbol`，禁止继续对 Short-Lab 传 futures symbol 猜 spot；相同截点计算期现比。
- [ ] Spot 451/429/超时一律 UNAVAILABLE；仅现货 exchangeInfo 能确认无 symbol 时为 NOT_APPLICABLE。加入固定 fixture 覆盖“现货接口不可达但币对实际存在”。
- [ ] Spot exchangeInfo 正向缓存 3600 秒、确认不存在的 symbol 负向缓存 900 秒，缓存键含 host+symbol；并发资产共享同一次成功的 exchangeInfo 获取。451/429/超时不建负向缓存；只收到 `-1121` 而元数据未确认时仍 UNAVAILABLE。用 fake clock/HTTP 断言缓存命中、过期和 host 切换。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_spot_history.py` 和原 Spot 测试。

### Task 7：OI 与盘口单位断言

**Files:** Modify `desktop/backend/src/diveintocrypto_desktop/data/open_interest.py`, `desktop/backend/src/diveintocrypto_desktop/data/orderbook.py`; Create `desktop/backend/tests/test_shortlab_liquidity_units.py`。

**Interfaces:** `oi_value_usd` 从 Crypcodile `open_interest_value` 对应 Binance `sumOpenInterestValue` 取得；`book_panel()` 增加 `bid_notional_1pct`、`ask_notional_1pct`，保留旧 `notional_1pct`。

- [ ] 用原始 OI fixture 对比 parser 输出 `oi` 数量与 `oi_value` 名义价值，明确价格/币种单位；用非对称盘口 fixture 断言 min(side) 与原总和不同。
- [ ] 若 parser 单位与 USD/USDT 不匹配，在 adapter 中显式折算并标来源；未知单位令 `oi_value_usd=null`。盘口补双侧值，不改变旧字段。
- [ ] USDT-M OI `oi_value` 用原始 `sumOpenInterestValue` fixture 核对；只有确认 quote USDT 名义值才直接作为 USD 近似，并用 `sumOpenInterest × 同时点 mark price` 作容差 5% 的断言，失败时 null + `OI_UNIT_UNVERIFIED`。盘口 1% bid/ask 分别以 `Σ(price×qty)` 计算；评分只取 `min(bid_notional_1pct, ask_notional_1pct)`，缺任一侧为 null。
- [ ] 校验 mark 使用 `premiumIndex.markPrice` 且响应时间距最近 OI 点不超过 5 分钟；缺同步时间或误差 >5% 时 OI USD 为 null。盘口 mid 固定 `(best_bid+best_ask)/2`，两侧按 mid±1% 截取；测试非对称盘口与任一侧空。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_liquidity_units.py` 和原 OI/orderbook 测试。

### Task 8：合约生命周期保留

**Files:** Modify `desktop/backend/src/diveintocrypto_desktop/data/universe.py`; Create `desktop/backend/tests/test_shortlab_contract_lifecycle.py`。历史持久化由 Task 2 repository 承担。

**Interfaces:** `contract_metadata_all() -> dict[str, ContractMetadata]` 含 `onboard_at_ms/first_seen_ms/delivery_at_ms/status/contract_type/observed_at_ms/contract_multiplier/multiplier_source`；缺明确交易所倍率时后二者为 null，旧 `list_universe()` 仍只返回 TRADING 永续。

- [ ] 测试 exchangeInfo 有 `onboardDate/deliveryDate/status` 时保留；缺 `onboardDate` 时 `first_seen_ms` 仅为首次观测，不证明年龄；停止 TRADING 的合约仍进入 metadata/history。
- [ ] 首次观测 `first_seen_ms=observed_at_ms`，后续刷新沿用最早值，即使合约状态变化也不重置；测试已停止交易 symbol 仍能从历史 metadata 查询。
- [ ] 测试缺有效 onboardDate 时返回 `LISTING_AGE_UNKNOWN/NOT_READY`，不以 first_seen 解除；live universe 消失但无证实终止状态时返回 `PAUSE_CONTRACT_STATUS_UNVERIFIED`，不误报已退市。
- [ ] 分离“全量合同元数据”和“可扫描交易 universe”过滤；保存状态变化快照；远未来 delivery 占位时间不触发退市。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_contract_lifecycle.py` 和原 universe 测试。

### Task 9：CoinGecko 基本面 provider

**Files:** Create `shortlab/providers/base.py`, `shortlab/providers/coingecko.py`, `desktop/backend/tests/test_shortlab_coingecko.py`。

**Interfaces:** 从 Task 1 `shortlab/models.py` 导入统一 `ProviderResult[T]`；`CoinGeckoProvider.fetch(identity) -> ProviderResult[Fundamentals]`；`providers/base.py:ProviderRegistry` 按 provider 名注册/查询并返回 NullProvider，供 Task 13 runtime 与 Task 17 FULL 扩展，不重定义 DTO。

- [ ] 测试必须使用 `identity.coingecko_id`、同名不同币不得猜测、429/500/超时后返回状态、旧 cache 可 STALE、日志/响应无 API key。
- [ ] 使用共享 aiohttp/backoff 风格并为 CoinGecko 独立 limiter；抓 MC、FDV、supply、ATH、ATH date、categories；原始请求和时间进入 snapshot。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_coingecko.py`；所有测试使用假 HTTP 响应。

## Phase 2：纯计算与运行时

### Task 10：特征、Profile、LTSS-LITE 数学

**Files:** Create `shortlab/features/{lifecycle,carry,valuation,tradeability}.py`, `shortlab/scoring/{ltss,profiles,versions}.py`, `desktop/backend/tests/test_shortlab_scoring.py`。

**Interfaces:** `extract_features(inputs, as_of_ms) -> FeatureSnapshot`; `select_profile(identity, fundamentals, overrides) -> Profile`; `score_lite(features, profile, config) -> ScoreBreakdown`。

- [ ] 写固定 fixture，逐因子断言设计第 8.2、8.3、9.1、10.3 节的分箱，`raw/raw_max × profile_weight` 及最终 1 位舍入；测试三 profile 最高分均为 100。
- [ ] 测试 60D Spot 缺失、30D funding 缺口、OI 单位未知、未确认 pivot、不适用现货：相关值 null，关键因子缺失使 LTSS=null，不重新分摊权重。
- [ ] 用同一币的 `universe.quote_volume`（滚动 24h）与已收盘 UTC `1d` K 线 `qv` 故意设置不同值；断言 Tradeability 的 10M 硬门槛和 0–3 分只取日线 `qv`，滚动值仅可用于 cheap prefilter，日线缺失时不得回退 ticker。
- [ ] 实现纯函数，不发 HTTP/SQL；所有窗口只用 `as_of_ms` 已收盘数据；输出每项原始值、贡献、扣分原因、profile 与版本。
- [ ] FULL fixture 固定 `valuation_supply_raw=valuation_raw_10+unlock_raw_15`、`module_score=raw/25×profile_weight`，Narrative 为 `raw/15×profile_weight`；Task 17 再验证三 FULL profile 满分均为 100。删掉无计算消费者的 `lifecycle.min_age_days`，年龄风险只使用 `veto.new_token_days`。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_scoring.py`；同输入重复 100 次结果完全相同。

### Task 11：DQ、VETO 与双状态

**Files:** Create `shortlab/quality.py`, `shortlab/risk/{veto,squeeze}.py`, `desktop/backend/tests/test_shortlab_status.py`。

**Interfaces:** `data_quality(tier, field_states, as_of_ms) -> DQBreakdown`; `evaluate_risks(features, metadata, dq) -> RiskResult`; `derive_status(ltss, entry, dq, tradeability, identity, risk, stale) -> CandidateState`。`CandidateState` 按冻结 DTO 同时返回 `reasons/vetoes/pauses/warnings`。

- [ ] 逐组断言 LITE/FULL 权重合计 100；各组字段份额、TTL/grace、NOT_APPLICABLE 从分母移除、UNAVAILABLE 留在分母；FULL 未齐 Catalyst 不启用。
- [ ] 测试 `LTSS=25 + PAUSE_BREAKOUT` 显示 EXCLUDED、`LTSS=84 + PAUSE` 为 PAUSED、MEDIUM identity 为 CANDIDATE/NOT_READY、BLOCK 覆盖其余执行状态、Entry=null 不 READY、tradeability 7/10 边界。
- [ ] 测试 DQ 公式 `Σ(组权重×组内适用份额加权 credit)`：NOT_APPLICABLE 只移除组内分母、全部 N/A 的组贡献 0，PARTIAL coverage×stale 0.5；关键字段进入 stale 时顶层 `stale=true` 且 NOT_READY，非关键字段 stale 仅在详情显示。
- [ ] 除 Funding 外，PARTIAL 用 `valid_count/required_count`：market 日线 70、Spot quote volume 60、supply/float 和 ATH 各 2、盘口双侧 2；测试 60/70=0.857142… 而非 0 或 1。Supply 字段用 6/12h override，其余 fundamental 字段用 1/6h。
- [ ] 风险触发使用可追溯的合约年龄/退市、资金费和价格/OI，同一组输入固定结果；输出 `candidateStatus/executionStatus/status` 与所有原因 code。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_status.py`。

### Task 12：Entry 单币构建与预算

**Files:** Create `shortlab/entry.py`, `desktop/backend/tests/test_shortlab_entry.py`；复用现有 data 客户端与 `scan/symbol_builder.py:assemble()` 纯计算，不调用完整 `build_symbol()`。

**Interfaces:** `build_entry_snapshot(symbol, primary_tf='1h') -> EntryResult`; `score_entry(snapshot, funding, bounce) -> float | None`; `EntryBudget(max_calls=240, concurrency=2, ttl_sec=3600)`。Task 12 生成并通过 Task 2 `save_entry()` 保存独立 Entry 快照，返回 `entry_snapshot_id` 给 Task 13 `save_score()`；不可只写单一 as-of。

- [ ] 测试真实字段 `finalSignal/confidence/mtfConfluence/microstructure/regime`、Regime 的 `adaptive_score` 负向、Failed Bounce；六组件合计 100。任一必需块缺失、`microstructure.active=0` 或 30D funding 不完整时 `entryScore=null`，不以 0 代替。
- [ ] 12 根请求的 timeframe 清单固定复用 `data/binance_klines.py:TF_LIST`（等同 `scan/constants.py:ALL_TFS`）：`1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d`；mock 逐项计数，缺一个 TF 时不可用，不写第二份硬编码清单。
- [ ] 测试 LTSS shortlist 币未在 `/api/scan` 输出仍可构建；每币只抓 12 K 线 + 1 OI + 4 ratio + 1 funding（缓存全未命中时 18 次），10 币约 180 次，30 币约 540 次；禁止调用 CVD/ticker/divergence/panel 的 book/ls_term/basis/spot。每轮 10 symbol、并发 2、预算 240、缓存 3600 秒，预算耗尽则 `ENTRY_BUDGET_EXHAUSTED` 与 null，不触发额外网络。
- [ ] 用 fake HTTP 分别跑 10 币、30 币和含 429 重试的批次；断言每次实际尝试都计预算，10 币最多 180 次基础请求，30 币在 240 次处停止并排队，其余等待下轮，429 不绕过共享 limiter。
- [ ] 对 `build_symbol(end_ms=旧时间)` 构造当前 OI/ratio/funding fixture，断言 Short-Lab 历史回放拒绝该路径，只读取当时已存快照。
- [ ] 保存 `weights_hash`、engine version/config hash、primary TF、六组冻结输入/分项贡献、每组 fetched-at/as-of；测试从 DB 读取后无需网络可重算同一 Entry。运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_entry.py`。

### Task 13：Service、Runtime、Scheduler

**Files:** Create `shortlab/{service,runtime,scheduler}.py`, `desktop/backend/tests/test_shortlab_runtime.py`。

**Interfaces:** `ShortLabRuntime.start()/stop()`；`ShortLabService.refresh(job_type) -> JobRef`、`candidates(query) -> CandidatePage`、`detail(symbol) -> CandidateDetail`、`evidence_summary(filters) -> EvidenceSummary | Unavailable`；scheduler 接受可注入 `jitter_fn(max_sec: float) -> float`，生产默认 `random.uniform(0,max_sec)`，测试传固定返回值。

- [ ] 测试 DB 迁移失败仅令 Short-Lab unavailable；同类手动/定时刷新复用 job id；关闭时 await/cancel 后台任务与 DB worker；provider 失败不修改旧 scan 缓存。
- [ ] runtime/service 预留可注入 `ProviderRegistry` 与 effective-tier 选择接口：默认 LITE，未注册 FULL provider 时不导入未来模块；`evidence_summary` 在 metrics 尚未提供时返回显式 Unavailable。Task 17 后续接入 FULL 不复制一套 scheduler，Task 16 只实现 metrics 文件。
- [ ] 流水线按 universe→cheap filter→identity→features→LTSS→risk→top10 Entry→`save_entry`→`save_feature`→`save_score(entry_snapshot_id)`；funding 每个 5 分钟批次 80 symbol、Entry 每轮 240 calls。作业统计成功/失败/延迟数。
- [ ] 周期任务每次独立注入 `uniform(0, refresh.jitter_sec)`，固定随机源测试 0/300 秒边界；手动刷新无 jitter 但同锁同限流。CoinGecko market 每 3600 秒刷新，supply 目标 21600 秒；market 响应已有 supply 时更新缓存，不额外重复请求。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_runtime.py`；模拟所有外部 provider 失败，原 service 仍可调用。

## Phase 3：API 与 UI

### Task 14：FastAPI `/api/short/*`

**Files:** Create `api/shortlab.py`, `desktop/backend/tests/test_shortlab_api.py`; Modify `api/app.py`（由本任务唯一 owner 修改）。

**Interfaces:** 设计第 25 节全部接口；候选返回 `candidateStatus/executionStatus/status`、tier、score 版本、provider/数据时间。

- [ ] 测试 404/503/422、provider 部分失败仍 200、`EXCLUDED` 可筛、分页排序稳定、refresh 重入固定 202 + `{jobId,existing:true}`。
- [ ] 第 25.1 节的 `min_funding_30d`、`ath_drawdown_min/max`、`min_data_quality` 与 status/profile/category/min_ltss/min_entry 一并实现；测试 0.005 表示 0.5%、ATH 区间边界、min/max 倒置 422、缺字段行不误入过滤结果。
- [ ] 测试同分数/同状态资产按 `symbol ASC,snapshot_id ASC` 固定顺序；两页使用同一个 `generationId`，并发新 refresh 不改变第二页；null 永远排末尾。API `reasons` 去重并按 BLOCK→PAUSE→NOT_READY 排，`warnings` 独立。
- [ ] 两个 SUCCEEDED `sl_job_run` 的 `finished_at_ms` 不同时选最新；相同时间按 `job_id ASC` 破平；RUNNING/FAILED 不可见。fixture 验证 `generation_id=job_id`，未完成批次不泄露到候选 API。
- [ ] 冻结时间测试：存储的 READY 快照过了 `refresh.score_sec` 后，数据库 `asOfStatus=READY/snapshotDataQuality` 不变，但 API 当前 `status=CANDIDATE`、`executionStatus=NOT_READY`、`stale=true`、`READY_INPUT_STALE`；读取不得覆写历史快照或用新 provider 值提高旧 LTSS。
- [ ] 在 `create_app()` include router，并在现有 lifespan 启停 runtime；沿用当前 FastAPI 与端口，不改旧 API path/schema；Short-Lab 失败只返回 Short-Lab 错误。Phase 3 注册 `/api/short/evidence/summary`，grader 未就绪时返回 `503 short_evidence_unavailable`；Task 16 上线后同一路由返回真实 200，Task 16 不改 router。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_api.py` 与已有 API 测试；验证 `/api/scan` 在 CoinGecko 500 时仍正常。

### Task 15：React Short Lab Scanner/Detail

**Files:** Create `desktop/ui/src/app/shortlab/{short-lab-view,short-lab-detail,short-lab-table,short-lab-format}.jsx`（format 可用 `.js`）; Modify `desktop/ui/src/app/data.js`, `desktop/ui/src/app/desktop-app.jsx`, `desktop/ui/src/styles.css`; Test `desktop/ui` 现有测试位置。

**Interfaces:** `window.DIVE.shortCandidates(query)`, `shortDetail(symbol)`, `shortRefresh()` 由 `data.js` 统一实现；组件只消费 `/api/short/*` 适配结果。

- [ ] 测试导航/hash/命令面板均可进入 SHORT LAB，EXCLUDED/PAUSED/BLOCKED 含原因，LITE/FULL 明示，null 与 N/A 分开，API 失败不切 mock。
- [ ] UI 的 funding 正值、ATH DD 区间和 DQ 门槛控件直接映射 `min_funding_30d/ath_drawdown_min/ath_drawdown_max/min_data_quality`，百分比显示转换只在 `short-lab-format.js` 完成；按 `execution_status=BLOCKED` 或 PAUSED 可单独审查风险，不依赖默认排序推断严重度。
- [ ] 测试 score 过期或 READY 必需字段 stale 时顶层 STALE、执行状态 NOT_READY；非关键 provider stale 只在 `dataAvailability` 展示，不把整行误标 STALE。字段级 N/A 与 UNAVAILABLE 各有独立文案。
- [ ] 增加独立 Short Lab 加载与错误状态；在 `desktop-app.jsx` 的 `if (noData && view!=="settings")` 全屏门控处让 `view==="shortlab"` 走 Short-Lab 自己的 STALE/UNAVAILABLE/重试页面，原有视图行为不变。用组件测试覆盖 universe boot 失败 + Short-Lab API 可用/不可用两种情况。
- [ ] `npm test`、`npm run build`，检查构建后的 `desktop/ui/dist` 含新视图；审查窄屏表格与详情滚动。

## Phase 4：Forward Evidence

### Task 16：7D/30D/90D grader 与统计

**Files:** Create `shortlab/evidence/{grader,metrics}.py`, `desktop/backend/tests/test_shortlab_evidence.py`；调用 Task 2 的 outcome repository，不修改旧 `scan/evidence.py` schema。

**Interfaces:** `grade(score_snapshot_id, horizon, as_of_ms) -> Outcome`；状态 PENDING/COMPLETE/CENSORED/UNAVAILABLE；`shortlab.evidence.metrics.summary(filters) -> EvidenceSummary` 由 Task 14 已注册 router 的 runtime/service 调用，不另改 API 文件。

- [ ] 测试入场/退出为指定时点后第一根完整 1h bar，短头 MAE/MFE、实际结算 funding、开平成本；成本和公式 hash 保存。
- [ ] Funding event 缺 `mark_price` 时返回 `FUNDING_MARK_MISSING`，不使用费率直接累加：`funding_coverage` 降低、funding/net return 为 null，正常合约 outcome UNAVAILABLE、退市合约仍 CENSORED；price return 独立保留。MAE/MFE 仅用 `entry_ts < close_ts <= exit_ts` 的已收盘 1h bar，覆盖 entry bar 而排除退出开盘后的 exit bar。
- [ ] 默认成本 fixture 固定双侧 fee=0.001、双侧 slippage=0.002、合计 0.003；改变成本 hash 可写入另一 outcome 版本，不覆盖旧结果。
- [ ] 测试未到期 PENDING、缺 bar UNAVAILABLE、退市 CENSORED 且有最后可交易价、无可靠退出价不能 COMPLETE；报表四态数量之和等于样本总数。
- [ ] 禁止调用当前 `build_symbol(end_ms)` 回填旧 Entry；只读 score 时当时 feature snapshot。运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_evidence.py`。
- [ ] 用现有 Task 14 router 的 TestClient 验证 `/api/short/evidence/summary` 从 Phase 3 的 `503 short_evidence_unavailable` 变为真实聚合 200，Task 16 不改 `api/shortlab.py`；并对每种 outcome 状态返回计数。

## Phase 5–6：扩展数据与 FULL

### Task 17：Unlock、Social、Catalyst 与 FULL

**Files:** Create `shortlab/providers/{unlock,social,catalyst}.py`, `shortlab/features/{supply,narrative}.py`, `shortlab/migrations/002_unlock_social.sql`, `shortlab/migrations/003_catalyst.sql`, `desktop/backend/tests/test_shortlab_full.py`; Modify `shortlab/scoring/ltss.py`, `shortlab/quality.py`, `shortlab/repository.py`, `shortlab/runtime.py`, `shortlab/service.py`（Task 2/10/11/13 已合并并停止修改后由此任务独占接线；Task 14 router 不修改）。

**Interfaces:** `UnlockProvider/SocialProvider/CatalystProvider` 返回统一 `ProviderResult`；`score_full()` 使用第 14 节 profile 权重；FULL DQ 使用第 18 节固定组权重。

- [ ] Phase 5 先用 NullProvider/无 key fixture 验证 LITE 不受影响；真实 provider 必须通过已验证 identity ID、限流缓存、时间截点和去重。
- [ ] 按设计第 19.2 节分别写 002（`sl_unlock_event`、`sl_social_snapshot`）和 003（`sl_catalyst_event`）迁移，repository 增加相应的幂等保存/按 known_at 读取方法。测试空库从 001→002→003、已有 001 库向前迁移、重复启动不重建旧表、在 t0 回放时排除 `known_at_ms>t0` 事件；Phase 5 不运行 003。
- [ ] `features/supply.py` 唯一负责 30/90D 解锁压力与 `unlock_raw_15`，`features/valuation.py` 保持 V1 `valuation_raw_10`；FULL 在 `scoring/ltss.py` 计算 `(valuation_raw_10+unlock_raw_15)/25×valuation_weight`。测试 30/90D unlock、social 两个 30D 窗口与事件严重性；缺 Catalyst 或任一必需 provider 时 `analysisTier` 仍 LITE，不能展示 FULL READY。
- [ ] Phase 6 加 Catalyst 覆盖后启用 FULL；测试三 profile 最高分 100、缺数据不重分配权重、DQ 阈值和事件 PAUSE。
- [ ] 将三个 provider 注册到 Task 13 的 `ProviderRegistry`，使 `runtime/service` 按 `requestedTier/effectiveTier` 选择 LITE/FULL、按第 23 节刷新并写相应快照；用 TestClient 经 Task 14 现有 API 验证 FULL provider 状态与候选字段真实变化，不能仅测 `score_full()` 纯函数。
- [ ] 测试配置 FULL 但任一必需 provider 缺 key/未启用时 `requestedTier=FULL, analysisTier=LITE, FULL_PREREQUISITE_MISSING`；已启用 FULL 后临时 429 时仍为 FULL、DQ 下降且 NOT_READY，不把同 generation 混成 LITE。
- [ ] 运行 `cd desktop/backend && uv run pytest -q tests/test_shortlab_full.py`，再跑 Task 10/11 回归测试。

## 发布与整体验收

### Task 18：桌面命名、打包、文档和最终验收

**Files:** Modify `desktop/backend/pyproject.toml`（仅 CLI alias/发行名；DuckDB 依赖由 Task 2 提交）、`.github/workflows/release.yml`, `desktop/ui/package.json`, `README.md`, `desktop/README.md`, `desktop/backend/README.md`, `docs/{api,packaging,testing}.md`, `desktop/backend/requirements-freeze.md`; Rename `desktop/backend/dive.spec` → `desktop/backend/short-lab.spec`; Test `desktop/backend/tests/test_shortlab_packaging.py`, `desktop/backend/tests/test_shortlab_release_workflow.py`。

**Interfaces:** `uv run short-lab` 与 `uv run dive-desktop` 指向同一 `__main__:main`；`short-lab.spec` 产出 `dist/short-lab`；tag `short-lab-v*` 仅触发 Desktop package/release，tag `v*` 仅触发 Android release。

- [ ] 测试 `uv run short-lab --help` 与 `uv run dive-desktop --help` 均指向同一应用；旧 `/api/health` 兼容字段和旧运行目录不受破坏。
- [ ] `pyproject.toml` 的 distribution 名固定 `short-lab-desktop`，`desktop/ui/package.json` 名固定 `short-lab-desktop-ui`；保留 Python import `diveintocrypto_desktop` 与旧 CLI。`/api/health.service` 保留旧值并新增 `product="short-lab"`；升级说明要求同一环境先卸载旧 distribution，测试新旧 CLI 均可启动。
- [ ] PyInstaller 命令固定为 `uv run --with pyinstaller pyinstaller short-lab.spec --noconfirm`；spec 包含 DuckDB 二进制、`shortlab/default.yaml`、React dist；`EXE/COLLECT` 均名为 `short-lab`。workflow 的输出固定 `dist/short-lab`、`short-lab-windows-x64.zip` 和同名 artifact，mirror UI 与 smoke 路径同步更新。
- [ ] spec 更名后逐行更新文件头部关于 `dive-desktop` 的命令、目录示意和冻结资源路径注释；静态测试搜索 spec/workflow/打包文档，不允许仍引用旧产物路径（兼容 CLI 的文字说明除外）。
- [ ] Workflow `on.push.tags` 同列 `v*` 与 `short-lab-v*`；Android job 使用 `github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')`；Desktop job 使用 `(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/short-lab-v')) || (github.event_name == 'workflow_dispatch' && inputs.package_desktop == true)`。tag 触发 Desktop 时上传桌面 zip 到对应 GitHub Release，手动触发只上传 artifact。静态测试断言 `short-lab-v1` 只进 Desktop、`v0.4.0` 只进 Android、手动勾选只进 Desktop。
- [ ] 在只读解压目录启动打包版，两次启动均能读写 `%LOCALAPPDATA%/short-lab/shortlab.duckdb`（或平台用户数据目录）；旧 `/api/scan`、Short Lab 页、缓存重启持久化均 smoke。
- [ ] 更新 API 参数、错误码、配置、数据状态、打包与发布说明；按设计第 35–36 节逐项写验收报告。运行 backend 全量离线测试、根 E2E、UI 测试/构建，记录命令、退出码、提交号。

## 集成门槛与冲突防护

1. Task 0 的 baseline、funding 分页预算和 `qv` fixture 经审查后才并行启动 B 组。若 `oi_value` 单位 fixture 不符，Task 7 必须先修正合同，再放行 Task 10。
2. 每组只合并已经通过目标测试的提交。接口名与 DTO 字段以本计划 **Interfaces** 和设计第 4/19/25 节为准；若实际代码必须改名，先同步设计和所有消费任务的合同，不允许局部偷偷偏离。
3. Task 14 合并后，Task 15/16 与 Task 17 的 provider/feature 纯实现可并行。Task 17 的 `runtime.py/service.py` 接线和集成测试必须等待 Task 16 测试/提交完成，避免共享工作树中 Task 16 TestClient 读到未完成 runtime；Task 16 只改 evidence 文件，二者不得修改 Task 14 的 router。Task 17 修改 `scoring/ltss.py`、`quality.py` 时，Task 10/11 必须已合并且停止写这些文件。Task 18 最后集成。
4. 交付前运行设计第 35 节验收矩阵：架构、兼容、数据正确性、评分、UI、Evidence、打包。每一行贴自动测试或人工 smoke 的可复现证据；不得把 Phase 5 的 provider 接入误报为 FULL 已可用。

## 验收矩阵（设计第 35 节的执行索引）

| 设计验收 | 责任任务 | 最低证据 |
| --- | --- | --- |
| 35.1 单进程、CLI、Desktop 命名与 Android 隔离 | 14、18 | `test_shortlab_api.py`、`test_shortlab_packaging.py`、`test_shortlab_release_workflow.py`，源码/打包两次启动记录 |
| 35.2 原 API/WS 与 provider 故障隔离 | 13、14、15 | `test_shortlab_runtime.py`、`test_shortlab_api.py`、原 backend/root/UI 回归命令及退出码 |
| 35.3 Funding/identity/单位/PIT 数据正确 | 0、2–9 | `test_shortlab_source_contracts.py`、`test_shortlab_funding_history.py`、`test_shortlab_quote_volume.py`、`test_shortlab_identity.py`、`test_shortlab_liquidity_units.py` |
| 35.4 LTSS/Entry/DQ/状态确定性 | 10–12、17 | `test_shortlab_scoring.py`、`test_shortlab_status.py`、`test_shortlab_entry.py`、`test_shortlab_full.py`，100 次同输入同输出 |
| 35.5 UI 的 LITE/FULL、N/A、stale 和错误隔离 | 14、15 | `npm test`、`npm run build`、浏览器和打包 UI smoke 截图 |
| 35.6 7D/30D/90D Evidence 与删失 | 2、8、16 | `test_shortlab_repository.py`、`test_shortlab_evidence.py`，四态样本计数和成本版本 fixture |

验收报告逐行填写提交号、测试命令、退出码和实际证据路径；任一行缺证据即不能标记完成。所有证据文件只能写入项目根目录内。

## 附录：默认评分配置哈希测试向量

此 JSON 是设计第 24 节默认 YAML 的评分相关子树经过规定规范化后的完整 UTF-8 输入；Task 1 测试对**下列 JSON 内容行的 UTF-8 字节（不含尾部换行符）**求 SHA256，期望 `4909ffe7d43c294983d313cff65c6d73125b921944e403a1e1e152b91a869786`。`refresh/providers/evidence_cost` 与本机路径、密钥均不在向量中。

```json
{"candidate":{"candidate_ltss":70,"entry_required_blocks":["consensus","mtf","micro","regime","failed_bounce","funding"],"ready_data_quality":80,"ready_entry":70,"ready_ltss":80,"ready_tradeability_score":7,"watch_ltss":60},"funding":{"lookbacks_days":[7,30,90]},"lifecycle":{"ideal_ath_drawdown_max":0.7,"ideal_ath_drawdown_min":0.4},"liquidity":{"hard_min_futures_volume_usd":10000000,"hard_min_open_interest_usd":2000000,"preferred_futures_volume_usd":30000000,"preferred_open_interest_usd":5000000},"quality_field_overrides_sec":{"supply_float":{"grace":43200,"ttl":21600}},"quality_freshness_sec":{"catalyst":{"grace":7200,"ttl":1800},"fundamentals":{"grace":21600,"ttl":3600},"funding_history":{"grace":7200,"ttl":1800},"identity_profile":{"grace":259200,"ttl":86400},"market_futures":{"grace":3600,"ttl":900},"social":{"grace":43200,"ttl":10800},"spot_liquidity":{"grace":10800,"ttl":3600},"unlock":{"grace":129600,"ttl":43200}},"score_weights":{"GENERAL_FULL":{"carry":30,"lifecycle":25,"narrative":15,"tradeability":10,"valuation":20},"GENERAL_LITE":{"carry":40,"lifecycle":35,"tradeability":10,"valuation":15},"LOW_FLOAT_VC_FULL":{"carry":25,"lifecycle":20,"narrative":10,"tradeability":10,"valuation":35},"LOW_FLOAT_VC_LITE":{"carry":30,"lifecycle":25,"tradeability":10,"valuation":35},"MEME_FULL":{"carry":35,"lifecycle":30,"narrative":20,"tradeability":10,"valuation":5},"MEME_LITE":{"carry":45,"lifecycle":40,"tradeability":10,"valuation":5}},"veto":{"breakout_24h":0.35,"breakout_7d":0.7,"new_token_days":45}}
```
