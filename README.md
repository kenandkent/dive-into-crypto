# short-lab

short-lab 是面向 Binance USDT-M 永续合约的本地行情分析与做空研究工具。桌面端使用 Python/FastAPI 获取行情、计算指标并提供 API，React 页面通过同一个服务显示结果。它同时提供多时框技术扫描和 Short-Lab 长周期研究：前者分析当前多空信号，后者分别评估长期做空结构（LTSS）、当前入场条件（Entry）、数据质量与执行风险。

项目不连接交易账户、不自动下单。Binance 公共行情无需交易 API Key；CoinGecko 及其他扩展 provider 是否需要数据服务 Key，取决于配置和服务商。数据缺失、陈旧或获取失败会明确标记，不自动用演示行情填补。

本文按当前仓库代码说明功能与结构。设计文档和实施计划用于描述契约与开发要求；其中的目标不代表所有外部数据源和运行时任务已经完成实网接入。

## 快速启动

桌面端需要 Python **3.12+** 和 `uv`。预构建 UI 位于 `desktop/ui/dist/`，直接运行后端不需要 Node.js；修改 UI 或重新构建时需要 Node.js **22.x**（`engines >=22 <23`，CI `setup-node: 22`；浏览器验收另需 `@playwright/test 1.56.0` dev 依赖，不打进生产包）。

在项目根目录执行：

```bash
cd desktop/backend
uv sync
uv run short-lab
```

默认服务地址为 **http://127.0.0.1:46408/**，启动后自动打开浏览器。可通过参数关闭自动打开或指定地址和端口：

```bash
uv run short-lab --no-open
uv run short-lab --host 127.0.0.1 --port 46408
```

`uv run dive-desktop` 是同一入口的兼容命令。Python distribution 名为 `short-lab-desktop`，Python import 路径仍为 `diveintocrypto_desktop`；UI package 名为 `short-lab-desktop-ui`。升级旧环境时应先卸载 `diveintocrypto-desktop`，避免两个 distribution 在同一环境写入相同 import 路径。

首次 `uv sync` 需要下载依赖，包括固定提交版本的 [Crypcodile](https://github.com/nazmiefearmutcu/Crypcodile)。运行行情功能还需要能访问 Binance Futures/Spot 等外部数据服务。完整部署步骤见[本地开发环境部署](docs/本地开发环境部署.md)和[生产环境部署](docs/生产环境部署.md)。

## 功能概览

### 桌面界面

桌面端沿用 Depth Terminal 风格，提供中文、英文、土耳其语切换，以及 Phosphor、Amber、Ice、Paper 四种主题。导航、hash 路由和命令面板支持在以下视图之间切换：

| 页面 | 功能 |
| --- | --- |
| 扫描 Scanner | 按技术信号强度扫描市场，展示价格、方向、置信度、多时框结果、鲸鱼背离与筛选结果；支持大范围异步扫描和进度查询 |
| 单币 Panel | K 线、主时框共识、指标明细、多时框网格、微观结构、OI/L/S/funding 序列，以及可获取的 CVD、basis、盘口、现货与期权附加分析 |
| 资金流 Flow | OI、仓位比例、taker 与市场领涨/领跌信息 |
| 信号 Signal | 选定资产的技术指标和时框信号明细 |
| 证据 Evidence | 技术扫描历史表现、分桶统计、校准、稳定性、回放、IC 与只读权重建议 |
| Short Lab | LTSS/Entry/DQ 候选表、风险状态、筛选排序、刷新作业、provider 状态和单币研究详情 |
| 对比 Compare | 多资产并排比较与时框结果查看 |
| 热力图 Map | 用矩阵视图查看市场信号、价格与资产分布 |
| 组合 Portfolio | 浏览器本地持仓账本、手动录入与盈亏估算；不读取交易所账户 |
| 市场结构 Structure | BTC beta/相关性、相关性聚类、相对强弱与波动率结构 |
| 网络日志 Logs | 查看实际后端请求、错误和扫描信息 |
| 设置 Settings | 主题与语言配置；偏好保存在浏览器本地 |

组合账本、界面偏好和部分人工标注使用浏览器 `localStorage`，切换浏览器或清除站点数据会影响这些本地记录。键盘 `Ctrl+K` 打开命令面板，`1–8` 为常用视图快捷键；Short Lab 的路由为 `#/shortlab`。

### 技术指标、共识与市场扫描

`engine/signal_service.py` 注册 **60 个技术指标**，输入为真实 OHLCV。指标输出五级信号：`STRONG_BUY`、`BUY`、`NEUTRAL`、`SELL`、`STRONG_SELL`。共识层按配置权重汇总，结合冲突处理、参与度、置信度和风险规则输出结论；ATR filter 的权重为 0，用于风险提示。

| 指标组 | 已注册指标 |
| --- | --- |
| 核心 15 项 | RSI、MACD、Bollinger Bands、SMA Cross、EMA Cross、Stochastic、ADX/DI、CCI、Williams %R、ROC、MFI、ATR Filter、Ichimoku、PSAR、OBV |
| 趋势、动量与结构 | Supertrend、Awesome Oscillator、Vortex、Keltner Breakout、Donchian Breakout、Elder Ray、TRIX、Coppock、KST、DPO、Aroon、Schaff Trend Cycle、WaveTrend、Relative Vigor、CMO、TSI、Qstick、Kalman Trend、Engulfing、Liquidity Sweep、Pivot Structure |
| 成交量与资金流 | CMF、VWAP、Chaikin Oscillator、Klinger Oscillator、Accumulation/Distribution、VWMA Cross、Force Index |
| 波动率、震荡与统计 | Squeeze、Choppiness、Fisher Transform、Connors RSI、Stoch RSI、Ultimate Oscillator、Balance of Power、Mass Index、Bollinger %B、Z-score Reversion、Linear Regression Slope、ATR Percentile、Historical Volatility Percentile、Hurst、Range Expansion、Half-life Reversion、Rolling Sharpe |

扫描使用 12 个时框：`1m / 3m / 5m / 15m / 30m / 1h / 2h / 4h / 6h / 8h / 12h / 1d`。桌面扫描器先对 universe 做 `4h/12h/1d` 粗筛，再为高排名资产补齐其余时框，减少全市场深扫成本。Universe 仅取可交易的 USDT 永续合约，并排除配置中的稳定币/法币基础资产。

三个附加 overlay 保持独立于原共识：

- **Microstructure**：OI 与价格背离、突破确认、funding、taker 流、L/S 拥挤及不同群体仓位差异。
- **Regime**：根据 ADX 与 Choppiness 判断 TREND/RANGE/MIXED，并提供自适应权重结果。
- **MTF Confluence**：多时框方向与高时框一致性分数、方向和 gate。

鲸鱼背离模块将价格与 top-trader position L/S 按时间对齐；与技术方向冲突的候选会被标记或淘汰。扫描还可附加 BTC beta/相关性、聚类、相对强弱、波动率锥与 term structure。单币构建可附加 funding lens、期现 basis、盘口失衡、L/S term、现货 lead/lag、CVD 和 cascade proxy；数据不足时附加块返回 unavailable。

`/api/scan` 的 universe 上限为 500，大扫描耗时和请求量明显高于单币查询。建议使用 `async=1`，通过 scan progress/result 接口查看进度；并发与上游重试采用共享限流和退避。

### 技术扫描的 Evidence、Replay 与 Claims

每次技术扫描的 verdict 可追加到本地 JSONL 证据档案。已有证据层支持 `1h/4h/24h` grading、方向收益、置信度分桶、Wilson 区间、ECE/Brier 校准、稳定性、baseline 和指标相关性统计。

Replay 根据已归档决策和权重 hash 执行研究回放；IC 和 suggest-weights 接口提供研究报告，权重建议不会自动修改生产引擎。Claims 模块保存研究主张和状态，决策查询支持翻页。样本不足、历史缺字段或行情失败会保持不可用/样本门槛状态。

单币 API 支持 `tf` 和 `end_ms`。历史 K 线截点能力不等于所有衍生品来源均可历史重建；Short-Lab 的历史 Entry 必须依靠已存 point-in-time 快照。

### Short-Lab 长周期研究

Short-Lab 是同一 FastAPI 内的 Python 模块，新增 API 均位于 `/api/short/*`，不要求 Redis、PostgreSQL 或独立 worker 服务。

| 能力 | 当前代码内容 |
| --- | --- |
| 资产身份 | Canonical asset、Binance Futures/Spot 与外部 provider ID 映射、人工 overrides、映射置信度、合约倍率与来源校验 |
| 长期特征 | Lifecycle、Carry、Valuation、Tradeability；另有 Unlock/Supply 和 Narrative 扩展特征 |
| LTSS | `MEME_LITE`、`GENERAL_LITE`、`LOW_FLOAT_VC_LITE`，以及对应 FULL profile；分项贡献、配置 hash 与版本可追溯 |
| Entry | 复用技术引擎与行情客户端计算六组件入场分数，限制并发/请求预算，保存独立 Entry 输入和来源快照 |
| 风险与 DQ | BLOCK/PAUSE/WARN、字段覆盖率和 TTL/grace、身份和单位未验证限制、读取时陈旧数据投影 |
| 状态 | `candidateStatus` 表示长期候选价值，`executionStatus` 表示当前执行条件，展示状态含 EXCLUDED/WATCH/CANDIDATE/READY/PAUSED/BLOCKED |
| 刷新作业 | 同进程 asyncio scheduler、异步 refresh job、同类任务互斥、generation 原子发布、进度与结果统计 |
| 存储 | DuckDB 保存资产/合约元数据、funding events、基本面、feature/Entry/score 快照、forward outcome 与 job runs |
| 长周期 Evidence | 7D/30D/90D grader、资金费 carry、成本假设、MAE/MFE、PENDING/COMPLETE/CENSORED/UNAVAILABLE 结果和聚合函数 |

默认配置为 **LITE**，universe 上限 500、shortlist 50、每轮自动 Entry top 10、Entry 并发 2、缓存 3600 秒、请求预算 240 次。Funding history 默认预算为 80 次/5 分钟；首轮数据收集和资产映射可能让候选暂时处于 BLOCKED 或 NOT_READY。READY 是规则结果，不是下单指令或收益保证。

**当前实网接入与运行时限制：**

- CoinGecko 使用真实公开 API 地址；身份映射和 provider ID 仍须正确配置，限流、Key 权限或资产不匹配会造成字段不可用。
- Unlock、Social、Catalyst 的接口、解析、缓存和 FULL 算法已有代码，但 provider 的默认 URL 仍为 `.example` 占位地址。默认配置也关闭这三个 provider；仅填写 Key 不足以完成真实服务商接入。
- 默认 runtime 注册周期性的 `score_refresh`；7D/30D/90D grader 和 metrics 函数已有实现，但没有默认自动 grader 作业及 metrics provider 接线。默认 `/api/short/evidence/summary` 会在未接线时返回 503。
- 当前 refresh 的部分 DQ 来源仍标记 `BASIS_NOT_WIRED`/`DEPTH_NOT_WIRED` 等不可用状态。是否能形成 READY 要以实际 provider、字段覆盖和 API 返回为准。

这些限制与设计目标应分别看待。详细契约见[设计方案](docs/ShortLab_Detailed_Design_CN.md)，任务与验收要求见[实施计划](docs/ShortLab_Implementation_Plan_CN.md)。

### 现货对冲研究（Hedge，需显式启用）

默认 `hedge.enabled: false`，对冲链路默认关闭；开启后新增 API 均位于 `/api/short/hedge/*`，UI 增加 planner/monitor/alerts 与 funding 视图。链路只做**参考参数计算与人工登记**，不连接交易账户、不自动下单：

| 能力 | 当前代码内容 |
| --- | --- |
| 机会与报价 | Funding 机会扫描、按名义金额的三场所比价（BINANCE_SPOT / BINANCE_ALPHA / ONCHAIN_DEX 同屏返回，未配置的场所如实返回 `UNAVAILABLE` + `CHAIN_PROVIDER_UNCONFIGURED`，不编造报价） |
| 模拟与计划 | `simulate` 生成带 60 秒有效期的参考模拟 → `plans` 登记为本地计划（幂等 `clientRequestId`、版本冲突 409、过期/错配 409） |
| 人工成交 | 唯一的执行记录方式是用户手工录入 fill（`PATCH legs`，`USER_ENTERED`）；activate/close 只改本地计划状态 |
| 监控与提醒 | 持仓比例/敞口/资金费/基差/强平距离快照、alert 生成与 ack（ack 只确认收到，不把问题标为已解决） |
| 对冲证据 | 按策略/期限聚合的已实现与未实现结果；未接线时 503 `HEDGE_EVIDENCE_UNAVAILABLE` |

**场所能力单列（Alpha / 0x）：**

- `binance_spot`：默认启用（`providers.binance_spot.enabled: true`），首个可用版本支持 Binance Spot 两腿规划与手工记录。
- `binance_alpha`：默认关闭（`enabled: false`），需 fixture + 实网双重证据后才启用；未启用时报价端返回不可用，不计入可用能力。
- `onchain`（Ethereum 0x v2）：默认关闭，需配置 `SHORTLAB_0X_API_KEY` 且 `provider: ETHEREUM_0X_PRICE_V2`；无 Key 时 `/api/short/health` 的 `hedgeChains.onchain` 为 false，场所状态为 `CHAIN_PROVIDER_UNCONFIGURED`，**记为 UNCONFIGURED，不计入通过**。

**明确局限（不宣传）：** 项目不做自动交易；不承诺无损、对冲后保本或强平免疫；强平距离是基于当时标记价格与用户录入仓位的参考计算，未经“强平已验证”类实盘验证，不得作为安全保证引用。READY/计划存在不等于建议下单。

### Short-Lab 发布能力、启用与边界（R16）

默认能力：Short-Lab 默认 `LITE` 评分、默认 `hedge.enabled: false` 且 Funding/Hedge 相关开关默认关闭；默认 runtime 只注册 `score_refresh`，7D/30D/90D grader 与 metrics 无默认自动作业，未接线时 `/api/short/evidence/summary` 返回 503。`binance_spot` 默认启用，`binance_alpha` 与 `onchain`（0x）默认关闭；未配置的场所一律返回 `UNAVAILABLE` + `CHAIN_PROVIDER_UNCONFIGURED`，记为 UNCONFIGURED，不计入通过。

启用方式：复制 `shortlab/default.yaml` 后以 `SHORTLAB_CONFIG_PATH` 指向覆盖文件显式启用对应 provider/hedge/funding；provider Key 只经环境变量（如 `DIVE_COINGECKO_API_KEY`、`SHORTLAB_0X_API_KEY`）注入，不写入包内。仅填 Key 而 endpoint 仍为 `.example` 占位时不算接通，以 API 实际返回为准。

研究评分边界：全部建议初始标 `RULE_BASED_UNVALIDATED`，是规则建议不是收益概率；READY 是规则结果，不是下单指令。正费率检查默认开启：Funding Gate 要求当前费率为正，双负不得 READY；历史覆盖不足时降为 `HISTORY_BOOTSTRAPPING`/`NOT_READY`，不向前延伸生效时点。

原生强平单位：强平距离与数量按原生交易单位计算，1000 倍合约（如 1000PEPE）不改变名义口径；USDC 现货与 Base 费用不改变原生单位；舍入按 tick（如 0.005）与 Dust 规则执行，未经验证的倍率标 `HEDGE_MULTIPLIER_UNVERIFIED` 并拒绝。

手工保护与成交：保护能力缺失时如实返回 `UNKNOWN`，不宣称已挂单；唯一的执行记录是用户手工录入 fill（`USER_ENTERED`），activate/close 只改本地状态；保护确认以位置 hash 为准，版本递增不误失效。

部分退出与数据过期：退出指导按当前剩余数量计算，支持部分退出；剩余为零后不再给出有效终值。数据过期按 TTL/grace 处理：陈旧 READY 投射为 `CANDIDATE`/`NOT_READY`（`stale: true`）而不改写历史；`NO_HEDGE` 状态不能保存配对计划；证据不足时返回 `PENDING`/`CENSORED`/`UNAVAILABLE`，缺 mark/FX 或退市不缩放补造。任何 BLOCKED 功能不得宣传为已实现；离线缺网/缺 Key 记为 UNVERIFIED/UNCONFIGURED，不计入通过。

## 数据源与配置

| 数据源 | 用途 | 代码入口 |
| --- | --- | --- |
| Binance USDT-M Futures | Universe、K 线、ticker、OI、funding、L/S、taker、盘口、basis、aggTrades/CVD | `desktop/backend/src/diveintocrypto_desktop/data/` |
| Binance Spot | 现货日线 quote volume、价格、市场存在性、期现比较 | `data/spot.py` |
| CoinGecko | MC、FDV、供给、ATH 与分类 | `shortlab/providers/coingecko.py` |
| Alternative.me、DefiLlama、Binance ticker | Fear & Greed、稳定币供应量与稳定币成交占比代理 | `data/sentiment.py` |
| Deribit | BTC/ETH 期权、波动率与期限结构附加数据 | `data/deribit.py` |
| Binance Composite Index | 指数合约成分与权重附加信息 | `data/index_info.py` |
| 可选 FULL provider | Unlock、Social、Catalyst 扩展契约 | `shortlab/providers/{unlock,social,catalyst}.py` |

传统技术引擎配置为 `engine/config/default.yaml`；Short-Lab 配置为 `shortlab/default.yaml`，人工资产映射为 `shortlab/identity/asset_overrides.yaml`。它们均相对 Python package `desktop/backend/src/diveintocrypto_desktop/`。

常用环境变量：

| 变量 | 作用 |
| --- | --- |
| `SHORTLAB_CONFIG_PATH` | 指定 Short-Lab YAML 覆盖文件 |
| `SHORTLAB_DATA_DIR` | 指定 Short-Lab 可写数据目录 |
| `DIVE_FAPI_BASE` | 覆盖 Binance Futures 数据 host |
| `DIVE_SPOT_BASE` | 覆盖 Binance Spot 数据 host |
| `DIVE_DERIBIT_BASE` | 覆盖 Deribit 数据 host |
| `DIVE_COINGECKO_API_KEY` | CoinGecko 数据服务 Key，具体要求取决于服务方案 |
| `DIVE_TOKENOMIST_API_KEY`、`DIVE_LUNARCRUSH_API_KEY` | FULL provider 的 Key 配置；仍需实际 endpoint 接线 |
| `DIVE_EVIDENCE_PATH`、`DIVE_EVIDENCE_GRADES_PATH` | 覆盖原技术 Evidence JSONL 文件位置 |

外部服务 Key 只由后端读取。根 README 不提供密钥示例；配置、provider 状态和 API 返回不应暴露 Key。

## 本地数据与运行目录

源码运行时 Short-Lab 默认数据库为 `desktop/backend/runtime/shortlab.duckdb`。PyInstaller 打包运行时使用用户数据目录：

| 系统 | Short-Lab 默认目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%/short-lab/` |
| macOS | `~/Library/Application Support/short-lab/` |
| Linux | `$XDG_DATA_HOME/short-lab/`，未设置时为 `~/.local/share/short-lab/` |

原技术 Evidence 使用 `desktop/backend/runtime/evidence.jsonl` 及相关 JSONL/报告文件，环境变量可覆盖位置。Short-Lab DuckDB 与原技术 Evidence 是两套不同存储。运行目录包含历史研究证据，备份和迁移前应先停止正在写入的实例；清空目录会丢失本地档案。冻结资源目录 `_internal` 不用于 Short-Lab 数据写入。

## API 入口

后端同时提供 REST 和 `/api/live` WebSocket。浏览器可查看 `http://127.0.0.1:46408/docs` 的 FastAPI 交互文档；完整字段说明见 [docs/api.md](docs/api.md)。

| 接口组 | 主要路径 |
| --- | --- |
| 健康与市场 | `/api/health`、`/api/universe`、`/api/leaders`、`/api/pulse`、`/api/logs` |
| 技术扫描 | `/api/scan`、`/api/scan/progress`、`/api/scan/result` |
| 单币与附加分析 | `/api/symbol/{symbol}`、`/api/structure`、`/api/macro`、`/api/options`、WebSocket `/api/live` |
| 技术证据 | `/api/evidence`、`/api/evidence/grade`、`/api/evidence/stability`、`/api/evidence/decisions` |
| 回放和研究 | `/api/evidence/replay`、`/api/evidence/ic`、`/api/evidence/suggest-weights`、`/api/claims` |
| Short-Lab | `/api/short/health`、`/api/short/candidates`、`/api/short/providers`、`/api/short/symbol/{symbol}` 及 `/history` |
| Short-Lab 作业与证据 | `/api/short/refresh`、`/api/short/refresh/{job_id}`、`/api/short/evidence/summary` |
| 对冲研究（需启用 hedge） | `/api/short/funding-opportunities`、`/api/short/hedge/venues/{symbol}`、`/api/short/hedge/simulate`、`/api/short/hedge/simulations/{id}`、`/api/short/hedge/plans*`（登记/列表/详情/legs/activate/close/monitor）、`/api/short/hedge/alerts*`（列表/ack）、`/api/short/hedge/evidence/summary` |

`/api/health` 的 `service` 保留 `dive-into-crypto-desktop` 兼容值，`product` 为 `short-lab`。Short-Lab refresh 提交返回 202 和 job ID；同类在途任务会复用。候选分页支持 generation 固定、状态/profile/category、LTSS/Entry、funding、ATH drawdown、DQ 过滤及稳定排序。

## 代码结构

```text
short-lab/
├── README.md                         项目总览与运行入口
├── desktop/
│   ├── backend/
│   │   ├── pyproject.toml / uv.lock   Python 依赖与 CLI
│   │   ├── short-lab.spec            PyInstaller 桌面打包
│   │   ├── src/diveintocrypto_desktop/
│   │   │   ├── __main__.py           服务启动、参数与浏览器打开
│   │   │   ├── api/                  传统 API、Short-Lab router 与 lifespan
│   │   │   ├── data/                 公共行情适配、缓存、限流、重试
│   │   │   ├── engine/               60 指标、共识、风险、配置和工具
│   │   │   ├── scan/                 扫描、overlay、背离、结构、Evidence/Replay/Claims
│   │   │   └── shortlab/             长周期研究业务模块
│   │   ├── tests/                    后端离线、fixture、API 与集成测试
│   │   └── runtime/                  本地生成的数据与报告（通常不入版本控制）
│   └── ui/
│       ├── src/app/desktop-app.jsx   主界面、导航、图表和各视图
│       ├── src/app/data.js           API 适配、共享状态、本地账本/标注
│       ├── src/app/i18n.js           中/英/土耳其语文案
│       ├── src/app/shortlab/         Short Lab 列表、详情、表格与格式化
│       ├── src/app/mock.js           手动演示数据
│       ├── src/styles.css           主题与样式
│       ├── build.mjs                esbuild 构建入口
│       ├── test/                    Node UI 测试
│       └── dist/                    后端直接服务的预构建 UI
├── android/                          现有 Kotlin/Compose 独立客户端
├── tests/                            仓库级离线 E2E 与对抗/边界场景
├── docs/                             API、部署、用户手册、设计、计划、证据主张和截图
├── .github/workflows/                CI 与 Desktop/Android 发布流程
├── run_tests.sh                      全仓测试入口（包含 Android）
├── CONTRIBUTING.md                   贡献与发布约定
└── LICENSE                           MIT 许可证
```

Short-Lab 内部职责：

| 目录/文件 | 职责 |
| --- | --- |
| `models.py`、`config.py`、`default.yaml` | 统一 DTO、配置校验与配置 hash |
| `paths.py`、`repository.py`、`migrations/` | 可写路径、串行 DuckDB 访问、001–006 迁移（含 H01 对冲顾问 005 与 R01 修复 006） |
| `resources.py` | 唯一打包资源入口 `read_resource_text`（importlib.resources，`_MEIPASS` 回退仅成品实测） |
| `maintenance.py` | 基础保留回调 `async maintain(context)`，仅调 `maintain_retention`，交 F06b 接线 |
| `identity/` | Canonical 映射、倍率和人工覆盖 |
| `providers/` | 统一 ProviderResult/registry、CoinGecko 与可选 FULL provider |
| `features/` | Lifecycle、Carry、Valuation、Supply、Narrative、Tradeability 纯特征 |
| `scoring/` | Profile、LITE/FULL LTSS 与评分版本 |
| `entry.py` | 轻量技术输入、入场评分、预算、缓存与 Entry 快照 |
| `quality.py`、`risk/` | 字段覆盖/新鲜度、风险规则和双状态 |
| `service.py`、`runtime.py`、`scheduler.py` | 刷新流水线、依赖装配、启动关闭和周期任务 |
| `evidence/` | 长周期收益、carry、成本、删失与统计 |

## 开发与验证

在项目根目录分别运行三个桌面测试集：

```bash
# 后端自身的离线测试
(cd desktop/backend && uv sync && uv run pytest -q)

# 根目录 tests/ 的 E2E；与上面的后端测试集不同
uv run --project desktop/backend pytest tests/ -q

# UI 测试与重新构建
(cd desktop/ui && npm ci && npm test && npm run build)
```

实网测试需显式选择，在能够访问上游数据源的环境执行：

```bash
(cd desktop/backend && uv run pytest -m live -q)
```

`run_tests.sh` 还会执行 Android Gradle 测试；只处理桌面端时使用上述三个独立入口。测试说明、fixture 和验收方法见 [docs/testing.md](docs/testing.md)。

## 打包与发布

桌面端使用 PyInstaller onedir 打包：

```bash
cd desktop/backend
uv run --with pyinstaller pyinstaller short-lab.spec --noconfirm
```

输出目录为 `desktop/backend/dist/short-lab/`，Windows 发布包为 `short-lab-windows-x64.zip`。打包包括 React dist、DuckDB 运行库以及 `resources.read_resource_text` 可读的 engine 配置、Short-Lab 默认配置、两套 identity YAML 与 001–006 迁移 SQL（含 H01 对冲顾问 005 与 R01 修复 006：D13 七表八索引）；运行数据写入用户目录。跨平台打包应在目标系统验证，当前 release workflow 的 Desktop job 使用 Windows runner（Node 22 重建 UI dist，frozen smoke 两次启动 schema 6 在 zip/发布前失败阻断）。

`.github/workflows/release.yml` 分开处理两种 tag：`short-lab-v*` 发布 Desktop 包，`v*` 发布 Android APK/AAB。手动 `workflow_dispatch` 勾选 `package_desktop` 仅生成 Desktop artifact。具体路径、签名和 smoke 方法见 [docs/packaging.md](docs/packaging.md)。

## Android 代码范围

`android/` 是独立的 Kotlin Multiplatform/Jetpack Compose 客户端，以 Python 技术引擎为 fixture 对照，直接访问 Binance REST/WebSocket。已有技术扫描、单币分析、微观结构、Evidence、组合、提醒、后台扫描与 widget 等模块。共享逻辑位于 `app/src/commonMain/`，Android 平台入口、worker、通知、分享和 widget 位于 `app/src/androidMain/`，对应测试位于 `commonTest/`。

Short-Lab 的 Python/DuckDB 功能属于桌面端，不能因同仓存在 Android 就视为已完成 Kotlin parity。Android 的安装、构建和功能说明见 [android/README.md](android/README.md)。

## 文档导航

- [用户手册](docs/用户手册.md)：页面操作、字段与状态解释。
- [本地开发环境部署](docs/本地开发环境部署.md)：源码环境和开发步骤。
- [生产环境部署](docs/生产环境部署.md)：运行和部署约定。
- [Desktop 说明](desktop/README.md)、[Backend 说明](desktop/backend/README.md)：桌面专项入口。
- [API](docs/api.md)、[测试](docs/testing.md)、[打包与发布](docs/packaging.md)：开发与维护参考。
- [Short-Lab 设计](docs/ShortLab_Detailed_Design_CN.md)、[实施计划](docs/ShortLab_Implementation_Plan_CN.md)：数据、评分、持久化和验收合同。
- [Claims](docs/claims/README.md)：研究主张登记。

## 许可证

项目采用 [MIT](LICENSE) 许可证，保留上游版权声明。行情分析和评分只用于研究；数据源、网络条件与模型假设都会影响结果，项目不保证准确性或收益。
