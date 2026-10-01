# short-lab 桌面端二次开发详细设计方案

**基于当前 short-meme 分支、Dive Into Crypto v0.3.0 代码的单体内二次开发；面向第三方直接实施**

- **文档版本**：1.0
- **基线版本**：当前 short-meme 分支的 Dive Into Crypto v0.3.0 代码；不得擅自切换到 main
- **基线参考提交**：9b6146c0c5aa4e502696e11051b684a6367f064f（实施前必须以本地实际 HEAD 复核）
- **日期**：2026-10-01
- **交付目标**：可由第三方直接开发、测试、验收，最大程度减少架构误读与实现偏差

# 0. 文档使用规则与执行优先级

> **强制结论：** short-lab 是桌面端产品名，不是独立服务。必须作为现有 desktop/backend 的内部功能模块实现，继续使用同一个 FastAPI；默认监听 127.0.0.1:8780，保留现有 `--host`/`--port` 参数。V1 不修改 Android。对外名称和内部标识的迁移见 1.5 节。


本方案用于指导对本地 Short-Lab / Dive Into Crypto 项目的二次开发。第三方实施人员不得根据“常见微服务习惯”自行拆分服务，也不得为了方便重新实现已有 Binance/Futures 数据链路。

发生冲突时按以下优先级处理：① 当前本地代码与现有测试所定义的既有行为；② 本文中标记为“必须/禁止”的新需求；③ 本文其他设计建议。若本地代码与本文引用的文件名或结构存在差异，先完成基线差异报告，不得直接删除或重构现有实现。

| 级别 | 含义 | 处理要求 |

| --- | --- | --- |

| MUST / 必须 | 交付验收硬条件 | 未满足即不通过验收 |

| MUST NOT / 禁止 | 明确禁止的实现 | 发现即要求回退 |

| SHOULD / 应 | 推荐实现 | 如偏离必须在 PR 中写明原因与影响 |

| MAY / 可 | 实现自由度 | 不影响兼容性和验收即可 |



## 0.1 实施前必须完成的基线确认

- 记录 git rev-parse HEAD、git status、当前分支、Python/uv/Node 版本，并附在实施报告中。

- 运行现有后端离线测试、根目录 E2E、UI 测试。Android 不属于本次实施和验收范围。

- 确认 desktop/backend/src/diveintocrypto_desktop/api/app.py:create_app() 为当前 Desktop FastAPI 入口；如本地已有变化，以本地实现为准。

- 确认 desktop/ui/src/app/desktop-app.jsx 与 data.js 为现有 UI 主入口/数据适配层；不得跳过现有适配层直接在组件里散落 fetch。

- 保存“改造前测试结果 + commit + 配置摘要”，作为后续回归基准。

- 实施前重新记录 HEAD、分支和工作树；保留已有未提交文件，不得以“工作树干净”为前提覆盖它们。



```bash
# 以下命令均从项目根目录执行；backend 与根 tests/ 是两个不同测试集
git rev-parse HEAD
git status --short
(cd desktop/backend && uv run pytest -q)
uv run --project desktop/backend pytest tests/ -q
(cd desktop/ui && npm test && npm run build)
```

# 1. 项目现状基线与边界

## 1.1 当前产品形态

当前仓库不是传统“云端 Server + 手机客户端”的单一架构，而是两个 Edition。Desktop Edition 为本机 FastAPI + React Web UI；Android Edition 为 Kotlin/Compose 原生实现，直接访问公开 Binance 数据，并通过 fixture 与 Python 参考引擎保持指标级 parity。

| 维度 | Desktop Edition | Android Edition |

| --- | --- | --- |

| 栈 | Python 3.12+ / FastAPI + React Depth Terminal | Kotlin / Jetpack Compose |

| 运行方式 | 当前为本机 localhost、`uv run dive-desktop`；更名后推荐 `uv run short-lab` | 单 APK，在设备本地运行 |

| 数据 | Crypcodile + Binance Futures 公共端点 + 现有扩展数据 | Binance REST + WebSocket 直连 |

| 引擎 | Python 参考实现：60 指标 + 3 overlays + whale divergence | Kotlin 镜像实现，fixture 校验 parity |

| 本次改造 | 必须修改 | V1 禁止修改 |



## 1.2 当前 Desktop 核心能力（必须复用）

根据本地代码分析，现有 Desktop 已具备足够完整的行情、衍生品、扫描、证据与 UI 基础。Short-Lab 只补长期做空研究缺失的“资产基本面/供给/叙事/长期评分与验证”能力。

| 现有区域 | 当前能力 | Short-Lab 处理原则 |

| --- | --- | --- |

| engine/ | 约 60 个指标、ConsensusEngine、risk、regime、配置加载 | 不改指标公式；不把 LTSS 因子混入现有 consensus |

| scan/ | scanner、microstructure、MTF、divergence、symbol_builder、evidence/replay/claims/progress | 复用扫描结果、Entry Timing 与统计方法；不复制一套 scan |

| data/ | klines、universe、OI、ratios、funding、basis、orderbook、CVD、spot、sentiment、deribit、index_info、HTTP/backoff | 优先复用；外部新 Provider 通过 Short-Lab provider 层接入 |

| api/ | create_app()、现有 REST/WS、失败显式 unavailable | 仅增量注册 /api/short/*；不新增服务/端口 |

| ui/ | React Depth Terminal、data.js、mock.js、build.mjs | 新增 Short Lab view；不得用 mock 数据伪装 live |

| runtime/evidence.jsonl | 现有 verdict 证据、自评、ECE/Brier/IC 等 | 保留；Short-Lab 长周期数据用嵌入式 DuckDB，统计工具尽量复用 |



## 1.3 本次项目的目标

- 在 Binance USDT-M 永续 Universe 中筛选“中长期结构恶化、做空 carry 合理、且当前执行风险可控”的资产。

- 将“长期值不值得空（LTSS）”与“现在适不适合入场（Entry Score）”严格分离。

- 为每个候选给出可解释的模块分数、关键原始指标、VETO/PAUSE 原因和数据质量。

- 保存 point-in-time 特征快照，并对 7D/30D/90D 的空头净收益、资金费率、MAE/MFE 做前向验证。

- 最大限度复用现有引擎、扫描、数据、错误处理、UI 设计和 evidence 方法，不破坏既有功能。



## 1.4 明确非目标

- V1 不执行自动下单，不接入交易 API Key，不管理交易账户。

- V1 不修改 Android，不做 Python ↔ Kotlin 的 Short-Lab parity。

- V1 不新增独立 FastAPI、Celery、Redis、PostgreSQL、Docker 服务或第二端口。

- V1 不做多交易所统一网关；只围绕现有 Binance USDT-M universe。

- V1 不使用 LLM 直接给资产打分；评分必须 deterministic、可重算、可回测。

- V1 不为了 Short-Lab 重写现有 60 指标、microstructure、MTF、whale divergence。

## 1.5 项目更名与兼容边界

- 对外产品名称统一为 **short-lab**（桌面 UI 标题、导航、README、发布说明、打包产物名称）。仓库目录示例也使用 `short-lab/`。
- Python import 路径 `diveintocrypto_desktop`、现有配置环境变量 `DIVE_*`、已有 API 路径和 `uv run dive-desktop` 保留兼容；不能仅为改名重写 import、证据文件或历史数据。新 CLI 名 `short-lab` 指向同一入口，但旧命令必须继续可用。对外文档以 `short-lab` 为推荐命令。
- Python distribution 名改为 `short-lab-desktop`，UI package 名改为 `short-lab-desktop-ui`，打包可执行文件/目录改为 `short-lab`，应用标题与桌面 UI 显示 `short-lab`。Python import 路径 `diveintocrypto_desktop` 与旧 CLI 保留；`/api/health.service` 为兼容保留旧值，同时新增 `product: "short-lab"`；Network Log 文案显示新产品名但旧日志字段不改。升级文档必须提示旧 distribution `diveintocrypto-desktop` 与新 distribution 不应同时安装到同一环境，安装新包前卸载旧 distribution；不得用全局字符串替换。
- Android 目录和 Android 产品名不在本次更名范围。本文中的 `Dive`/`dive` 如指现有算法、包路径、旧入口或兼容 API，保留其技术含义；如指最终桌面产品，应改为 short-lab。



# 2. 强制架构约束

## 2.1 单体内扩展，不新增服务

```text
short-lab/
├─ desktop/
│  ├─ backend/
│  │  └─ src/diveintocrypto_desktop/
│  │     ├─ engine/          # 现有，保持语义
│  │     ├─ scan/            # 现有，复用
│  │     ├─ data/            # 现有市场数据层，复用
│  │     ├─ api/             # 现有 FastAPI，新增 short router
│  │     └─ shortlab/        # 新增：内部业务模块，不是服务
│  └─ ui/
│     └─ src/app/            # 现有 React，新增 Short Lab view
└─ android/                  # V1 不修改
```

Short-Lab 只能作为现有 Python package 的内部模块。不得启动新的 uvicorn 进程、不得引入 service discovery、不得把 Short-Lab 放到单独仓库。

## 2.2 同一应用入口与兼容命令

```bash
cd desktop/backend
uv run short-lab
# 仍由现有 FastAPI 监听 127.0.0.1:8780
# 仍由现有入口挂载/服务 React UI
```

实施时在 `pyproject.toml` 将 `short-lab` 指向现有 `__main__:main`，`dive-desktop` 保留为兼容别名；打包可执行文件名称同步更改。上述命令属于交付目标，须在源码运行与打包验收中验证。

## 2.3 FastAPI 生命周期集成

Short-Lab 的数据库初始化、后台刷新任务必须挂入现有 FastAPI lifespan/startup/shutdown 机制。不得使用单独 daemon。推荐在 app.state 中挂载 ShortLabRuntime，由 create_app() 统一构造。

```text
create_app()
  ├─ existing routes / websocket
  ├─ short_router -> /api/short/*
  └─ lifespan
      ├─ start existing runtime
      ├─ await shortlab_runtime.start()
      └─ shutdown -> await shortlab_runtime.stop()
```

> **兼容性：** 现有 API 的 path、字段语义和错误语义默认不得改变。Short-Lab 的新增字段优先放在新接口中；如确需给旧接口增加字段，只允许 additive optional field，并补回归测试。


# 3. 推荐代码结构与文件职责

## 3.1 Backend 新增目录

```text
desktop/backend/src/diveintocrypto_desktop/
└─ shortlab/
   ├─ __init__.py
   ├─ models.py              # 领域数据结构 / Enum / API 内部 DTO
   ├─ config.py              # Short-Lab 配置读取与校验
   ├─ default.yaml           # 唯一随包默认配置
   ├─ paths.py              # 源码态/打包态可写数据目录
   ├─ quality.py            # LITE/FULL DQ 与组内适用性
   ├─ runtime.py             # 生命周期、依赖装配
   ├─ service.py             # 用例编排：候选、详情、刷新、评分
   ├─ entry.py               # Entry 适配、预算与来源版本
   ├─ scheduler.py           # 同进程周期任务；无外部 scheduler 服务
   ├─ repository.py          # DuckDB 访问，屏蔽 SQL
   ├─ identity/
   │  ├─ resolver.py         # Binance symbol -> canonical asset
   │  ├─ overrides.py        # 手工映射覆盖
   │  └─ asset_overrides.yaml # 受版本控制的唯一人工映射表
   ├─ providers/
   │  ├─ base.py             # ProviderResult / Protocol / 错误规范
   │  ├─ coingecko.py        # V1：MC/FDV/Supply/ATH
   │  ├─ unlock.py           # Phase 5：接口 + NullProvider；Tokenomist 实现
   │  ├─ social.py           # Phase 5：接口 + NullProvider；LunarCrush 实现
   │  └─ catalyst.py         # Phase 6：结构化催化剂
   ├─ features/
   │  ├─ lifecycle.py
   │  ├─ carry.py
   │  ├─ valuation.py
   │  ├─ supply.py           # Phase 5 解锁压力；FULL Valuation/Supply 使用
   │  ├─ narrative.py
   │  └─ tradeability.py
   ├─ scoring/
   │  ├─ ltss.py
   │  ├─ profiles.py
   │  └─ versions.py
   ├─ risk/
   │  ├─ veto.py
   │  └─ squeeze.py
   └─ evidence/
      ├─ grader.py
      └─ metrics.py

# API 建议单独文件，最终仍注册到同一 create_app()
desktop/backend/src/diveintocrypto_desktop/api/shortlab.py
```

## 3.2 UI 新增目录

```text
desktop/ui/src/app/
├─ desktop-app.jsx           # 仅增加导航/路由/顶层状态接入
├─ data.js                   # 增加 short API adapter
└─ shortlab/
   ├─ short-lab-view.jsx     # Scanner 主视图
   ├─ short-lab-detail.jsx   # 详情视图
   ├─ short-lab-table.jsx    # 表格、排序、筛选
   └─ short-lab-format.js    # 统一百分比/货币/状态格式化

# 样式优先继续复用现有 styles.css 设计 token；必要时追加 sl-* scoped class
```

## 3.3 明确禁止的代码组织

- 禁止 desktop/short-service/、server-short/、microservice/ 等新服务目录。

- 禁止在 React 组件中直接调用 CoinGecko/Tokenomist/LunarCrush；第三方 API 只允许 Backend 调用。

- 禁止在 existing engine/consensus 中直接加入 FDV、Unlock、Social 等非技术因子。

- 禁止复制现有 funding.py/open_interest.py/ratios.py 形成 shortlab_* 的重复 Binance client。

- 禁止用 mock.js 为 Short-Lab 自动兜底 live 失败；失败必须展示 UNAVAILABLE/STALE。



# 4. 领域模型与数据契约

## 4.1 AssetIdentity

资产识别是安全边界。任何基本面、解锁、社交数据在进入评分前必须先绑定到明确的 canonical asset。不得仅使用 symbol 字符串模糊匹配。

```text
AssetIdentity {
  canonical_id: str
  display_symbol: str
  name: str | null
  binance_futures_symbol: str        # e.g. 1000PEPEUSDT
  binance_spot_symbol: str | null    # e.g. PEPEUSDT
  contract_multiplier: float | null  # e.g. 1000；须由交易所合约元数据或人工覆盖验证，不得仅按名称猜测
  multiplier_source: EXCHANGE|MANUAL|null
  coingecko_id: str | null
  unlock_provider_id: str | null
  social_provider_id: str | null
  chain: str | null
  contract_address: str | null
  categories: list[str]
  mapping_confidence: VERIFIED|HIGH|MEDIUM|LOW|UNRESOLVED
  mapping_source: MANUAL|CONTRACT|UNIQUE_SYMBOL|OTHER
}
```

## 4.2 映射优先级

| 优先级 | 规则 | 置信度 | 要求 |

| --- | --- | --- | --- |

| 1 | shortlab asset override 中明确配置 futures symbol -> provider id | VERIFIED | 直接采用；配置必须进版本控制 |

| 2 | 能通过明确 contract address 做精确匹配 | VERIFIED/HIGH | 地址、chain 必须同时匹配 |

| 3 | provider 中该 symbol 唯一且 name/category 可合理确认 | HIGH | 仅唯一候选时允许自动绑定 |

| 4 | 1000/k 等前缀标准化后仍出现多候选 | UNRESOLVED | 禁止自动猜测 |

| 5 | 模糊名称、相似字符串 | 禁止 | 不得用于生产映射 |



若 mapping_confidence 为 LOW 或 UNRESOLVED：基本面字段返回 null；触发 VETO_DATA_IDENTITY；执行状态为 BLOCKED，展示状态为 BLOCKED，不能 READY。

`MEDIUM` 表示 provider 候选仍需人工核对，不触发身份 BLOCK，但执行状态强制为 `NOT_READY`、展示状态最多为 `CANDIDATE`，并返回 `IDENTITY_REVIEW_REQUIRED`。`VERIFIED/HIGH` 才允许 READY。价格、数量单位必须分开：若合约报价表示 `contract_multiplier` 个基础币，则 `canonical_price = futures_price / contract_multiplier`，再与同一基础币的 spot price 比较；合约/现货 quote volume 与已经以 USD 计的 OI 不乘除该倍数。倍率未知时依赖跨源价格的指标为 null。不能将 `1000PEPEUSDT` 直接传给现货 API 猜测对应 `PEPEUSDT`。现有 `data/spot.py` 须扩展为接受已验证的 `spot_symbol` 和倍率，并返回来源、单位与 as-of；spot market 经交易所元数据确认不存在时为 `NOT_APPLICABLE`，网络失败为 `UNAVAILABLE`。

`contract_multiplier` 与 `multiplier_source` 必须来自交易所明确的合约单位元数据，或受版本控制的 `identity/asset_overrides.yaml` 人工核实条目；单靠 `1000` 前缀或现价接近不能自动验证。`contract_metadata_all()` 在交易所提供明确倍率时一并返回这两项，否则为 null；Resolver 合并人工覆盖并保存来源。倍率无已验证来源时跨源价格/ATH 比较与 spot premium 为 null，执行状态 NOT_READY，reason=`MULTIPLIER_UNVERIFIED`。

## 4.3 ProviderResult

```text
ProviderResult[T] {
  status: OK | PARTIAL | NOT_APPLICABLE | UNAVAILABLE | ERROR
  source: str
  fetched_at_ms: int
  as_of_ms: int | null
  data: T | null
  stale: bool
  reason_code: str | null    # OK 时为 null；N/A/失败时为稳定机器码
  error_message: str | null  # 可选脱敏诊断，不进入公开 API；不得含 API Key
}
```

`NOT_APPLICABLE` 仅用于有证据确认业务上不存在的数据（例如 Binance 无对应现货市场），须有 `reason_code` 和判定来源；只从所在 DQ 组的适用字段分母去除，不改变该组在总 DQ 中的固定权重；相关 feature 为 null/N/A，不得算 0 分或市场信号。未配置 provider、限流、超时、历史覆盖不足均不是 `NOT_APPLICABLE`。FULL 所需 Unlock/Social 数据若无法确认不适用，应保持 UNAVAILABLE，降低 DQ 并阻止 READY。

Provider 失败必须被封装为 ProviderResult；不得让第三方 API 异常直接击穿 /api/scan 或现有 symbol API。

## 4.4 百分比、时间与 null 约定

| 类型 | 统一约定 | 示例 |

| --- | --- | --- |

| 比率/百分比 | 后端内部与 API 使用 decimal；UI 再格式化为 % | 0.0172 = 1.72% |

| 时间 | UTC epoch milliseconds | 1760000000000 |

| 金额 | USD/USDT analytics 使用数值；字段名明确单位 | marketCapUsd |

| 不可用 | 必须为 null + availability/error；禁止用 0 代替 | unlock90d=null |

| symbol | Binance symbol 大写 | PEPEUSDT / 1000PEPEUSDT |



# 5. 数据源设计与复用策略

## 5.1 现有数据：必须复用

| 指标/数据 | 首选来源 | 禁止做法 |

| --- | --- | --- |

| OHLCV/价格 | 现有 binance_klines / Crypcodile 链路 | Short-Lab 再写独立 kline client |

| Funding | 扩展现有 funding.py 的 fundingRate 历史分页；premiumIndex 仅提供未结算预测值 | 仅取当前 funding 后直接年化 |

| OI | 现有 open_interest.py | 重复请求同一端点 |

| L/S、Taker | 现有 ratios.py | 绕过现有限流/错误处理 |

| Basis/Spot/Orderbook/CVD | 现有 basis.py、spot.py、orderbook.py、cvd.py | 新增不同语义的重复字段 |

| Regime/MTF/Micro/Whale | 现有 engine/scan 计算结果 | 在 Short-Lab 复制算法 |



现有 `data/spot.py` 只抓 48 根 1h K 线和 24h ticker，无法提供连续两个 30D 现货成交量窗口，也没有统一的 quote volume 序列。必须扩展该模块的 Spot `1d` K 线查询，获取至少 61 根已收盘日线，在同一数据路径计算 `spot_volume_30d`、`spot_volume_prev_30d`、`spot_volume_decay_30d`（后 30D/前 30D）与 `spot_quote_volume_24h`；不得另建 Short-Lab Spot client。窗口定义为截至 `as_of` 之前**最近一个已收盘 UTC 日**的 30 根日线，以及紧邻其前的 30 根日线；期货/现货都使用同一 `[UTC 00:00, 次日 00:00)` 日边界，排除未收盘日线。`spot_quote_volume_24h` 指最后一个已收盘 UTC 日的 qv，不是滚动 24h ticker。Spot 不存在时上述字段为 N/A，并按 ProviderResult 的 NOT_APPLICABLE 规则处理。期货/现货成交量窗口须对齐，且均以 quote USD/USDT 名义金额比较。

现有 `data/binance_klines.py` 的 K 线 DTO 只保留 `{t,o,h,l,c,v}`，其中 `v` 是 base volume；不能拿它充当 quote volume。须在现有 Binance K 线适配层从原始响应保留 quote asset volume（Binance K 线第 8 项），输出字段 `qv`，期货和现货共用同一单位规则；Spot 的日线解析同样保留 `qv`。以固定原始 K 线 fixture 验证 `v` 与 `qv` 不互换、未收盘 K 线被排除、UTC 窗口一致。

适配器接口固定为：保留现有 `fetch_klines(symbol, interval, limit, end_ms)` 与 `fetch_klines_range(symbol, interval, start_ms, end_ms, limit)` 的参数/返回语义，仅在每根 K 线附加可选 `qv`；新增 `data/funding.py:funding_history_range(symbol, start_ms, end_ms, limit=1000)`；新增 `data/universe.py:contract_metadata_all()`，返回未经过 TRADING 筛选的合约元数据；扩展 `data/spot.py:spot_history(identity, as_of_ms)`。消费方不得自行发 Binance 请求绕过这些函数。现货 API 返回 451/地区限制、网络超时或 429 均为 UNAVAILABLE；只有先用现货 exchangeInfo 确认没有对应 spot symbol，才能标 `NOT_APPLICABLE/no_spot_market`。K 线 `qv` 按原始开盘时间 `t` 键对齐解析结果，遇到重复 `t`、缺 `qv` 或时间不匹配时该根 `qv=null`，不得按数组下标猜配。

`spot_history()` 使用 Spot exchangeInfo 的全量 symbol 列表做存在性确认；列表成功获取后正向缓存 1 小时、确认不存在的 symbol 负向缓存 15 分钟，缓存键含现货 host 与 symbol。缓存过期时可在同一轮共享一次 exchangeInfo 请求，不得每个资产重复请求；451/429/超时不写负向缓存，返回 UNAVAILABLE。现有 `spot.snapshot()` 的展示用途仍按原有缓存规则运行，Short-Lab 的 N/A 判定只采用上述已确认元数据，不能仅从一次 `-1121` 推断不存在。

## 5.2 V1 新增：CoinGecko Fundamentals Provider

用于 market cap、FDV、circulating/total/max supply、ATH、ATH date 等。必须通过 AssetIdentity.coingecko_id 请求，不能直接以 Binance base symbol 猜测。

| 字段 | 用途 | 缓存建议 |

| --- | --- | --- |

| market_cap_usd | OI/MC、规模过滤 | 60 min |

| fdv_usd | 估值/低流通判断 | 60 min |

| circulating_supply | Float/Unlock pressure 分母 | 6 h |

| total_supply/max_supply | Float ratio | 6 h |

| ath_price / ath_change / ath_date | Lifecycle | 60 min |

| categories | Profile 分类辅助 | 6 h |



## 5.3 Phase 5：Unlock Provider

接口必须先于具体厂商实现。首选实现可为 Tokenomist，但业务层只能依赖 UnlockProvider Protocol。没有 API Key 时启用 NullProvider，数据明确为 unavailable。

```text
UnlockProvider.get_unlocks(identity, from_ms, to_ms) -> ProviderResult[list[UnlockEvent]]
UnlockEvent {
  ts_ms
  amount_tokens
  percent_of_current_float
  allocation_type: TEAM|SEED|PRIVATE|ADVISOR|ECOSYSTEM|TREASURY|COMMUNITY|STAKING|OTHER
  source
}
```

## 5.4 Phase 5：Social Provider

重点衡量 attention decay，不将主观 sentiment 直接作为 LTSS 主因。建议抽象 social volume、contributors、dominance、interactions 的时间序列。

## 5.5 Phase 6：Catalyst Provider

Catalyst 只生成结构化事件，最终是否 PAUSE/BLOCK 由规则引擎决定。LLM 如未来使用，只能作为“事件提取器”，不得直接输出 LTSS。

```text
CatalystEvent {
  event_id
  asset_id
  type: CEX_LISTING|DELISTING|MAINNET|BURN|BUYBACK|AIRDROP|PARTNERSHIP|PRODUCT|OTHER
  announced_at_ms
  effective_at_ms | null
  severity: INFO|MATERIAL|MAJOR
  confidence: float
  source_url | null
  title
}
```

# 6. 全量处理流水线

```text
Existing Binance USDT-M Universe
        |
        v
[1] cheap eligibility + liquidity prefilter
        |
        v
[2] AssetIdentity resolve
        |
        +---- unresolved -> BLOCKED (no guessing)
        v
[3] Existing market/futures features + cached fundamentals
        |
        v
[4] Feature extraction
    lifecycle / carry / valuation / supply / narrative / tradeability
    （LITE 只启用 lifecycle/carry/valuation/tradeability；
      Phase 5 增加 supply/narrative，Phase 6 开启 FULL）
        |
        v
[5] LTSS (LITE or FULL profile)
        |
        v
[6] VETO / PAUSE / WARN
        |
        v
[7] shortlist top N（保留 30~50 个 LTSS 排名资产）
        |
        v
[8] 按 shortlist symbol 调用 Short-Lab 轻量 Entry builder，复用既有数据客户端与 symbol_builder.assemble 纯计算
        |
        v
[9] 保存独立 Entry 输入/来源快照，再保存引用它的 feature/score 快照
        |
        v
[10] /api/short/* -> existing React UI
```

## 6.1 为什么 Entry Score 只对 shortlist 深算

现有 `scanner.scan()` 按 `netNss` 选择深扫对象，且只有至多 13 个候选带完整附加数据，不能保证 LTSS shortlist 在结果中。因此 Short-Lab 不得把 `/api/scan` 返回列表当作 shortlist 的完整 Entry 来源。先用廉价长期特征筛到 top N（建议 30~50），再对其中需要 Entry 的 `entry_depth_top` 个 symbol 使用 `shortlab/entry.py:build_entry_snapshot()`：复用现有 K 线、OI、ratio、funding 客户端和 `symbol_builder.assemble()` 的纯计算，不调用完整 `build_symbol()` 的 panel/CVD/divergence 网络链路。复用既有数据客户端和纯计算逻辑，按 symbol 限制并发、共享短期缓存并记录请求预算；不在每轮对 500 个 symbol 做 12 timeframe 深扫。其余 shortlist 项 `entryScore=null`，状态不得为 READY。

默认每轮自动深算 10 个、并发 2、缓存 3600 秒、Short-Lab 上游请求预算 240 次/轮；启动或定时刷新不得因预算耗尽继续发请求。轻量 Entry 每币计划请求 12 个 timeframe K 线 + 1 OI + 4 ratio + 1 settled funding，缓存全未命中时为 18 次；10 币约 180 次，240 次预算留 60 次安全余量。完整 `build_symbol()` 还会触发 divergence、CVD、ticker、funding lens、basis、book、16 个 L/S term 单元及 Spot 请求，不得用于批量 Entry；30 币轻量路径约 540 次，仍超过默认预算。预算耗尽时保留已有新鲜 Entry，否则标记 null 与 `ENTRY_BUDGET_EXHAUSTED`；手动按币刷新进入同一队列和预算，不绕过共享 rate limiter。记录每轮实际调用数、缓存命中数、耗时与被延迟 symbol。

| Entry 方案 | 每币最坏请求数（不含重试） | 10 币 | 30 币 | 默认处理 |
| --- | ---: | ---: | ---: | --- |
| 轻量 Entry：12 K 线 + OI 1 + ratio 4 + funding 1 | 18 | 180 | 540 | 自动仅 top 10；30 币超 240 预算，排队 |
| 完整 `build_symbol()` | 大于 18，额外含 panel 的 16 个 L/S 单元等 | 超出轻量预算 | 明显超预算 | 禁止批量使用 |

每次实际 HTTP 尝试（含重试）都计入 240；达到预算时停止新请求并返回可解释的延迟状态。共享 `/futures/data/*` 限流器仍可能让 10 币执行时间超过一分钟，作业应异步执行，不阻塞现有 `/api/scan`。Phase 0 固定离线 mock 验证各调用家族计数、缓存命中、429/backoff 和 10/30 币预算边界。

## 6.2 推荐默认容量

| 参数 | 默认值 | 说明 |

| --- | --- | --- |

| universe_limit | 500 | 沿用现有上限，不强制每轮跑满 |

| shortlist_size | 50 | LTSS 高分候选进入 Entry 计算 |

| entry_depth_top | 10 | 每轮自动深算上限；其余 shortlist 按需触发并排队，不能无限并发 |

| candidate_page_size | 50 | API 默认分页 |



# 7. Score 体系总览

必须同时输出 LTSS、Entry Score、Data Quality、Veto/Warnings。任何一个单分数都不能替代其他维度。

| 输出 | 范围 | 回答的问题 |

| --- | --- | --- |

| LTSS | 0-100 | 资产在中长期结构上是否值得作为做空候选 |

| Entry Score | 0-100 | 当前技术/衍生品状态是否适合开始建立空头 |

| Data Quality | 0-100 | 用于本次结论的预期数据覆盖与新鲜度 |

| VETO/PAUSE/WARN | 枚举 | 是否存在不能用分数抵消的风险 |

| Status | 状态机 | 最终展示：EXCLUDED/WATCH/CANDIDATE/READY/PAUSED/BLOCKED |



## 7.1 Score 版本化

每个 score snapshot 必须保存 score_version、profile、config_hash、feature_version。修改权重或阈值必须生成新版本，不能悄悄重算旧证据。

`score_version` 标识 LTSS，`entry_version` 单独标识 Entry；Entry 未计算时 `entry_version=null`。已有引擎的 `dive_weights_hash`、`dive_engine_version`、`dive_config_hash` 和 `primary_tf` 保存在独立 `sl_entry_snapshot`，score 通过 `entry_snapshot_id` 引用，不能以 LTSS 版本替代这些来源版本。

```text
score_version examples:
  ltss-lite-v1
  ltss-full-v1
  entry-v1

config_hash = SHA256(normalized shortlab config relevant to scoring)
```

# 8. LTSS-LITE（V1）详细设计

V1 尚未接入 Unlock/Social/Catalyst 时，不应假装是 Full。API/UI 必须带 analysisTier=LITE。LITE 使用明确的独立权重，不因为某个 provider 临时丢失而动态重分配权重。

| Profile | Lifecycle | Carry/Crowding | Valuation | Tradeability | 合计 |

| --- | --- | --- | --- | --- | --- |

| MEME_LITE | 40 | 45 | 5 | 10 | 100 |

| GENERAL_LITE | 35 | 40 | 15 | 10 | 100 |

| LOW_FLOAT_VC_LITE | 25 | 30 | 35 | 10 | 100 |



## 8.1 Profile 选择

| Profile | 初始判定条件 | 说明 |

| --- | --- | --- |

| MEME | manual override 或明确 Meme category | 纯 Meme 降低 FDV/Unlock 权重，强调生命周期/拥挤 |

| LOW_FLOAT_VC | float ratio 低、FDV/MC 高、非 Meme，或 manual override | 强调估值/解锁压力 |

| GENERAL_ALT | 其他可识别 alt | 默认 profile |



Profile 分类必须可人工 override。优先级固定为 manual override > 已验证 Meme 分类 > 已验证低流通条件 > GENERAL_ALT。Meme 与低流通同时命中时选 MEME，除非手工覆盖。低流通定义为 `float_ratio < 0.35` 且 `FDV/MC >= 2`；来源不可用时不猜测，选 GENERAL_ALT 并记录分类依据。不得用 LLM 在生产时实时决定 profile。`mapping_confidence=MEDIUM` 可显示研究数据，但不得为 READY；LOW/UNRESOLVED 触发 VETO_DATA_IDENTITY。

## 8.2 确定性评分合同

所有原始比例使用小数，模块得分先按下表求得固定的 `raw`，再计算 `module_score = raw / raw_max × profile_module_weight`；最终 `LTSS = Σ module_score`，最后一步四舍五入至 1 位。单个因子必须使用以下固定分箱或线性夹取，不得自行替换为主观规则。`clamp(x,0,1)=min(1,max(0,x))`；边界左闭右开，最后一档包含上界。缺失因子贡献 0，同时对应数据组降低 DQ；缺失关键因子（funding30d、ATH、OI 或 MC）时 `ltss=null`，不能以其他模块补权重。FULL 在 V1 后按同一模块原始得分重新套 FULL 权重，并额外加入 Narrative/Unlock；不得把 LITE 分数直接按比例变换为 FULL。

| 模块 | 因子及固定满分 | 原始满分 |
| --- | --- | --- |
| Lifecycle | ATH 回撤 7（9.1）；ATH age 3（<90d=0，90–179d=1，>=180d=3）；30D 跌幅 3（>=0=0，-10%~0=1，-30%~-10%=3，更低=1）；MA 结构 4（收盘<MA60、MA30<MA60、MA60 较 10 日前下降、最近 5 日均价<MA30 各 1）；Lower High 3（最近两个已确认日线 pivot high 降低=3，否则 0）；Spot Volume Decay 3（后 30D quote volume / 前 30D <=0.6=3，<=0.85=2，<1=1，否则 0）；Failed Bounce 2（8.3） | 25 |
| Carry | funding30d 8（10.3）；90D 正结算占比 3（>=0.7=3，>=0.55=2，>=0.4=1，否则 0）；30D 正结算占比 3（同前）；funding stability 2（最近 30D 结算费率标准差 <=0.0003=2，<=0.0008=1，否则 0）；OI/MC 3（>=0.15=3，>=0.08=2，>=0.03=1，否则 0）；Price↓+OI↑ 3（同期 7D 价格<-5% 且 OI>+5%=3，否则 0）；期现量比 2（>=5=2，>=2=1，否则 0）；多头拥挤 1（现有 top-position L/S >1.5=1，否则 0） | 25 |
| Valuation | FDV/MC 4（>=5=4，>=3=3，>=2=2，否则 0）；float ratio 4（<0.2=4，<0.35=3，<0.5=1，否则 0）；FDV/MC 与 float 条件同时满足 2（FDV/MC>=3 且 float<0.35） | 10 |
| Tradeability | 已收盘 UTC 1d K 线 futures `qv` 3（>=30M=3，>=20M=2，>=10M=1）；OI USD 2（>=5M=2，>=2M=1）；spread 2（<=0.15%=2，<=0.3%=1）；1% 深度双侧较小值 2（>=1M USD=2，>=0.25M=1）；合约状态/已结算 funding 1（TRADING 且可取得结算记录） | 10 |
| Narrative（FULL） | 30D social volume 衰退 5、contributors 衰退 3、dominance 衰退 3、price/social 背离 2、spot-volume/social 背离 2；各衰退量采用后 30D/前 30D：<=0.5 得满分、0.5~1 线性降至 0、>=1 为 0；背离项以价格同期上涨 >=10% 且对应 social/spot 指标下降 >=30% 为满分，否则 0 | 15 |
| Unlock（FULL） | 未来 90D 加权解锁 / 当前流通量：>=30%=10、>=15%=7、>=5%=3、否则 0；未来 30D 同比例 >=10%=5、>=5%=3、否则 0 | 15 |

FULL 的 Valuation/Supply 原始满分为 25，即上述 Valuation 10 + Unlock 15；FULL Meme、General、Low Float 的模块权重见第 14 节。不同模块的最大值不因缺失数据调整。OI/MC 使用现有 `open_interest.py` 返回的 `oi_value`（quote 名义价值，核对 parser 单位）折算 USD，不使用 `oi` 数量直接除 MC。期现量比使用同一完整 UTC 日窗口的 quote volume；现有 24h rolling ticker 不满足严格 UTC 对齐，须从双方 K 线求值。现有 orderbook `notional_1pct` 是双侧总和，Tradeability 的“双侧较小值”需从原始 bid/ask level 分别求值。严禁对这些 USD/USDT 名义金额乘除合约数量前缀。分箱边界、单位变换与缺失数据均属于评分验收。

FULL 的 Valuation/Supply 模块精确计算为 `valuation_supply_raw = valuation_raw_10 + unlock_raw_15`，`valuation_supply_score = valuation_supply_raw / 25 × profile.valuation_weight`；LITE 为 `valuation_score = valuation_raw_10 / 10 × profile.valuation_weight`。Narrative FULL 为 `narrative_raw_15 / 15 × profile.narrative_weight`。六个 profile 在所有因子满分 fixture 下 LTSS 必须恰为 100；Unlock N/A/UNAVAILABLE 时不能把 15 分重新分给 Valuation。

OI 单位验收使用原始 `sumOpenInterestValue` fixture 对应 parser 的 `oi_value`，并用时间差不超过 5 分钟的同步 `sumOpenInterest × mark_price` 做 5% 相对误差校验；校验不通过时 OI USD 为 null、reason=`OI_UNIT_UNVERIFIED`，不把 BTC/base 单位直接当 USD。盘口在中间价上下 1% 区间分别计算 `bid_notional_1pct=Σbid_price×bid_qty` 与 `ask_notional_1pct=Σask_price×ask_qty`，旧 `notional_1pct` 保留双侧合计；Tradeability 只取 `min(bid_notional_1pct, ask_notional_1pct)` 与 0.25M/1M USD 单侧门槛比较，任一侧缺失时该因子 null。

OI 校验使用与最近 OI 点时间差不超过 5 分钟的 `premiumIndex.markPrice`（适配器须保留响应时间）；不能拿几小时前的 funding event mark 或日 K 收盘价代替。校验式为 `abs(oi_value - oi_quantity×mark_price)/max(oi_value,1) <= 0.05`；缺同步 mark 或校验失败时 `OI_UNIT_UNVERIFIED`。盘口中间价明确为 `(best_bid+best_ask)/2`，分别以该价上下 1% 截取 bid/ask；两侧任一为空，双侧深度因子 null。对历史 OI，只有同一 endpoint 的原始值单位已通过当前同步 fixture 校验后才可引用，仍必须保留各点时间。

### 8.3 结构规则

日线 MA 仅用已收盘 K 线；MA60 至少需要 70 根日线。Pivot high 定义为中心日 high 严格高于前后各 3 根日线 high，右侧 3 根未收盘前不得确认。Failed Bounce 定义为近 20 根已收盘日线内，价格自局部低点反弹至少 10%，反弹最高收盘未越过前一个已确认 pivot high，随后连续 2 日收盘低于 MA10；满足得 2，否则 0。数据不足时为 unavailable。所有窗口、缺口和计算截点进入 feature snapshot。

# 9. Lifecycle / Structural Decay 特征

该模块不是“跌得越多越好”，而是识别已经完成主要炒作、处于结构性衰退但尚未极端归零的阶段。

| 特征 | 标准化建议 | 解释 |

| --- | --- | --- |

| ATH Drawdown | 甜蜜区间 -40%~-70% 得分最高；<-95% 回落到 0 | 避免在剩余下跌空间很小的位置追空 |

| ATH Age | >=90d、>=180d 分级 | 过滤刚上市/刚创新高资产 |

| 30D Price Trend | 负趋势得分；极端暴跌不无限加分 | 结构衰退 |

| MA Structure | price<MA60、MA30<MA60、MA60 slope<0 各一部分 | 中期趋势确认 |

| Lower High | 基于 pivot 识别 1~2 个 lower high | 反弹失败结构 |

| Spot Volume Decay | 30D 对前 30D 或 rolling baseline 的衰退 | 新增真实买盘弱化 |

| Failed Bounce | 出现反弹后未突破关键 pivot/MA 并重新转弱 | 提供候选与 Entry 的桥梁 |



## 9.1 ATH Drawdown 初始分箱（通用满分 7）

| ATH 回撤 | 分值 |

| --- | --- |

| 0% ~ -20% | 0 |

| -20% ~ -40% | 3 |

| -40% ~ -70% | 7 |

| -70% ~ -85% | 4 |

| -85% ~ -95% | 2 |

| < -95% | 0 |



## 9.2 Pivot / Lower High 的确定性实现

不要使用主观“看起来像 lower high”。建议复用现有结构/pivot 指标；若需 Short-Lab 自己判断，只能使用固定窗口 pivot 算法，并把窗口参数写入配置。所有结构判断必须由相同输入产生相同输出。

# 10. Carry & Derivatives Crowding 特征

该模块是策略核心。长期空头不只赚价格下跌，还需要判断 carry 是否正、拥挤方向是否支持空头。

## 10.1 Funding 必须使用历史实际结算

```text
funding_30d = SUM(actual funding_rate events in [t-30d, t])
funding_90d = SUM(actual funding_rate events in [t-90d, t])
funding_apr_30d = funding_30d * 365 / 30

禁止：currentFunding * 3 * 365
原因：结算间隔和资金费率都会变化。
```

现有 `funding_hist(symbol, limit=48)` 仅取尾部，不能直接提供 30/90 天数据。Phase 1 必须在现有 `data/funding.py` 增加按 `startTime/endTime/limit` 分页的公共函数，复用 `data/http.py` 限流/退避，并在 `sl_funding_event` 按 `(symbol, funding_time_ms)` 幂等归档。USDⓈ-M `/fapi/v1/fundingRate` 每页最多 1000 条、按时间升序，和 `/fapi/v1/fundingInfo` 共享 500 次/5 分钟/IP；Short-Lab 自身默认最多 80 次/5 分钟、每个 5 分钟批次最多 80 个 symbol；首次回填作业可连续运行多个批次，并记录已用请求与剩余预算，429 时按 Retry-After 暂停。单个 90D 窗口通常一页可覆盖，但必须按返回的最后时间推进并检测重复/空页，不得假定固定 8 小时结算。首次运行先受限速地回填至 90 天或交易所可提供的最早记录，随后按最近一次结算时间增量同步。每个窗口同时保存最早/最晚事件、预期与实际覆盖、缺口和样本数；覆盖不足时该窗口结果为 `null` 并降低 Data Quality，不得对部分窗口求和后标为完整 30D/90D。新上市币按实际存续期单独标记，不能以 90 天覆盖率惩罚其不存在的历史，再由 `PAUSE_NEW_TOKEN` 处理年龄风险。

覆盖判定使用 `window_start=max(as_of-lookback, valid_onboard_at_ms)`，窗口终点为 `as_of`；无有效上市日时仍使用完整 lookback。回填请求须实际覆盖 `[window_start, as_of]` 且无失败页；最早/最晚事件距两端均不超过 24 小时、相邻事件间隔不超过 24 小时、至少 3 个事件，方记 `coverage=1`。满足完整条件时 `coverage_fraction=1`；否则以有效首末事件覆盖时长减去所有超过 24 小时的相邻缺口超额，再除以窗口时长并夹到 [0,1]，记录 `coverage_fraction`、缺口区间与 `FUNDING_HISTORY_INCOMPLETE`，对应窗口指标 null。此 24 小时阈值是保守缺口检测，不用于推定固定结算次数；结算间隔变化可在 fixture 中验证。无有效上市日且窗口前无数据，不可用 first_seen 冒充上市日缩短窗口。

不完整窗口的公式固定为 `covered_ms=max(0,min(last_event,as_of)-max(first_event,window_start)-Σmax(0, adjacent_gap-24h))`，`coverage_fraction=clamp(covered_ms/(as_of-window_start),0,1)`。该式同时扣除首段和末段缺口；无事件、单事件、零长度窗口时 coverage 为 0。完整窗口按前述判定直接记 1；不得把晚到 10 天才出现首个事件的窗口仅按中间跨度当完整。

## 10.2 Carry 指标

| 特征 | 建议权重/含义 |

| --- | --- |

| 30D cumulative funding | 主要 carry 指标；持续正值越高越有利于空头 |

| 90D funding persistence | 避免单日极端值造成误判 |

| 30D positive funding ratio | 正 funding 结算次数 / 有效结算次数 |

| Funding stability | 惩罚频繁大幅正负翻转 |

| OI / Market Cap | 衡量合约杠杆相对资产规模 |

| Price↓ + OI↑ divergence | 价格走弱但杠杆仓位扩张，重点加分 |

| Futures / Spot volume | 合约交易远大于现货，偏“合约赌场”结构 |

| L/S crowding | 大量多头拥挤时有利于观察空头 carry，但不能单独作为做空理由 |

合约年龄与退市判断复用并扩展 `data/universe.py` 的 `exchangeInfo` 元数据，至少保留 `onboardDate`、`deliveryDate`、`status`、`contractType` 及抓取时间。交易所响应中的有效 `onboardDate` 优先作为上市时间；缺失/无效时可保存 `first_seen_ms`，但它只是首次观测时间，不能证明真正上市日，也不得据此判定“已上市足够久”。`deliveryDate` 为有效且接近当前时间，或状态进入终止流程时，按状态/窗口触发退市风险；远未来占位值不触发。当前 universe 只保留 base/quote 并筛掉非 TRADING 合约；Short-Lab 必须另存历史已见 symbol 的元数据，停止交易后仍可被风险判断和 forward grader 找到。未知上市日不得默认为老币；缺失状态不得默认为 TRADING。

首次保存某 futures symbol 时 `first_seen_ms = observed_at_ms`；之后每次元数据快照都沿用历史最小 `first_seen_ms`，不得随刷新重置。它是本地观测下界，不是上市日替代值。`contract_metadata_all()` 输出该字段，repository 为已见但停止交易的 symbol 保留全部历史状态。

`PAUSE_NEW_TOKEN` 只在有效 `onboardDate` 显示上市年龄小于 `new_token_days` 时触发。没有有效 `onboardDate` 时不以 first_seen 推断年龄，而是 `executionStatus=NOT_READY` 并返回 `LISTING_AGE_UNKNOWN`；人工核实并记录来源后才允许解除。`VETO_CONTRACT_DELISTING` 以状态非 TRADING、有效近期 `deliveryDate` 或已验证的交易所公告为依据；仅从 live universe 消失时先标 `CONTRACT_STATUS_UNVERIFIED` 并暂停执行，不伪称已退市。



## 10.3 初始 funding 分箱（可配置）

| 30D 累计 Funding | Carry 子分建议（满分 8） |

| --- | --- |

| <= 0 | 0 |

| 0 ~ 0.20% | 1 |

| 0.20% ~ 0.50% | 2 |

| 0.50% ~ 1.00% | 4 |

| 1.00% ~ 2.00% | 6 |

| > 2.00% | 8 |



上述阈值是初始参数，不是永久真理；后续必须用 Short-Lab Evidence 校准。

# 11. Valuation & Supply 特征

## 11.1 V1 Fundamentals

| 特征 | 公式/用途 |

| --- | --- |

| FDV / MC | fdv_usd / market_cap_usd；低流通高估值风险 |

| Float Ratio | circulating_supply / total_supply |

| ATH / ATH Date | 与 Lifecycle 共用，数据快照必须 point-in-time 保存 |



## 11.2 Phase 5 Unlock Pressure

解锁特征由 `shortlab/features/supply.py` 独占实现并输出 `unlock_raw_15`；`shortlab/features/valuation.py` 只负责 V1 的 `valuation_raw_10`。FULL 合成由 `shortlab/scoring/ltss.py` 完成，不能在两个 feature 文件各自再加一次解锁分。

```text
unlock_pressure_30d = unlock_tokens_30d / circulating_supply_at_snapshot
unlock_pressure_90d = unlock_tokens_90d / circulating_supply_at_snapshot
weighted_unlock = SUM(unlock_amount * allocation_weight) / circulating_supply
```

| Allocation | 初始 sell-pressure weight |

| --- | --- |

| SEED / PRIVATE | 1.0 |

| TEAM | 0.8 |

| ADVISOR | 0.8 |

| TREASURY | 0.4 |

| ECOSYSTEM | 0.3 |

| COMMUNITY | 0.2 |

| STAKING | 0.2 |

| OTHER | 0.3 |



权重必须配置化，并记录在 score_version/config_hash 中。

# 12. Narrative Decay（Phase 5）

Narrative 关注“注意力是否持续流失”，不把单次 sentiment 作为主因。

| 特征 | 建议权重（Full 通用满分 15） |

| --- | --- |

| 30D social volume decay | 5 |

| 30D contributors decay | 3 |

| 30D social dominance decay | 3 |

| price/social divergence | 2 |

| spot-volume/social divergence | 2 |



示例：价格反弹 +15%，但 social volume -45%、contributors -35%、spot volume -50%，可认为反弹没有得到注意力与真实交易参与的同步确认。

# 13. Tradeability 特征与硬性流动性门槛

“垃圾币”不等于“适合做空”。必须保证永续市场有足够成交、OI、深度，并且合约状态正常。

Tradeability 的 `24h futures quote volume` 和 10M/30M 门槛一律使用 `data/binance_klines.py` 最后一根**已收盘 UTC 1d K 线**的 `qv`，与 Spot `spot_quote_volume_24h` 使用同一个 `[UTC 00:00, 次日 00:00)` 窗口。`data/universe.py` 的 `quote_volume` 是滚动 24h ticker，仅用于廉价候选预筛，不进入 Tradeability 分数、硬门槛、DQ 或 point-in-time snapshot 的该字段。若日线 `qv` 缺失则该因子 null，并按缺失数据处理，不能回退到滚动 ticker。

| 特征 | Tradeability 分数建议 |

| --- | --- |

| 24h Futures Quote Volume | 0~3 |

| Open Interest | 0~2 |

| Spread | 0~2 |

| Orderbook Depth | 0~2 |

| Contract Status / Funding available | 0~1 |



## 13.1 V1 初始门槛

| 规则 | 默认值 | 结果 |

| --- | --- | --- |

| 24h futures volume < hard_min | 10M USDT | BLOCKED_LOW_LIQUIDITY |

| OI < hard_min | 2M USDT | BLOCKED_LOW_LIQUIDITY |

| 建议评分高质量门槛 | 30M volume / 5M OI | 达到后 Tradeability 才容易高分 |



不同市况下应通过配置调节，不得硬编码在 UI。

# 14. Full LTSS Profile（Phase 6 启用）

| Profile | Lifecycle | Carry | Valuation/Supply | Narrative | Tradeability | 合计 |

| --- | --- | --- | --- | --- | --- | --- |

| MEME_FULL | 30 | 35 | 5 | 20 | 10 | 100 |

| GENERAL_FULL | 25 | 30 | 20 | 15 | 10 | 100 |

| LOW_FLOAT_VC_FULL | 20 | 25 | 35 | 10 | 10 | 100 |



若 Full profile 中某个预期 provider 临时不可用，不得把缺失权重动态分摊给其他模块。缺失项不贡献正分，同时 Data Quality 降低；Data Quality 不达 READY 门槛时禁止 READY。

`requestedTier` 来自配置，`analysisTier` 表示实际计算层级。配置为 FULL 但 Unlock/Social/Catalyst 任一 provider 未启用、缺必需 key 或尚无可验证覆盖时，启动阶段仅启用 LITE，响应 `analysisTier=LITE` 并在 availability/warnings 中附 `FULL_PREREQUISITE_MISSING`；LITE 的 READY 规则仍可独立成立；不可返回“FULL 且零分”的假结果。已经进入 FULL 的进程遇到临时超时/429 时保持 FULL 快照、缺失项不加分、DQ 下降且强制 NOT_READY，并保留 provider 错误；不得回退到 LITE 后把两个版本结果混在一轮 generation。恢复后下一轮可重新 READY。

# 15. Entry Timing Score

Entry Score 只回答“现在是否适合开始建立空头”，必须尽可能复用现有 Dive 结果，不重新发明技术分析体系。

| 组成 | 权重 | 计算建议 |

| --- | --- | --- |

| Existing SHORT consensus | 30 | 使用 `finalSignal`、`confidence`；仅 SELL/STRONG_SELL 贡献 |

| MTF bearish confluence | 20 | 使用 `mtfConfluence.score/direction/gate`；gate 之外还须检查看空方向 |

| Microstructure bearish | 20 | 使用 `microstructure.score/label/active`；active=0 为不可用 |

| Regime | 10 | TREND 且方向向下最高；MIXED 次之；RANGE 低 |

| Failed bounce | 10 | 固定规则识别反弹失败 |

| Funding still positive | 10 | 正 funding 持续性越好越高 |



## 15.1 建议的可复现映射

```text
short_consensus_component:
  if finalSignal not in {SELL, STRONG_SELL}: 0
  else base = 30 * confidence / 100
       if finalSignal == STRONG_SELL: base += 3
       clamp(base, 0, 30)

mtf_component:
  raw = 20 * clamp(-mtfConfluence.score / 100, 0, 1)
  if mtfConfluence.direction != -1: raw = 0
  else if mtfConfluence.gate == false: cap raw at 10

micro_component:
  if microstructure.active == 0: unavailable
  else 20 * clamp(-microstructure.score / 100, 0, 1)

funding_component:
  10 * clamp(positiveFundingRatio30d, 0, 1)

regime_component:
  if regime.regime == TREND and regime.adaptive_score < 0: 10
  else if regime.regime == MIXED and regime.adaptive_score < 0: 5
  else: 0

failed_bounce_component:
  use the confirmed rule in 8.3; true=10, false=0, insufficient history=unavailable
```

如现有字段范围与这里不同，第三方应根据本地实际字段做等价归一，但必须把映射公式写入测试和文档，不允许“凭感觉调整”。

复用 `symbol_builder.assemble()` 产生的 `finalSignal` 是主 timeframe 共识，并非全市场 scanner 排名；详情须记录所用 timeframe、快照时间与构建结果引用。Entry 六个组成块（consensus、MTF、micro、regime、failed bounce、30D funding）全部必需；任一不可用时不得伪装为 0 分，`entryScore=null` 且禁止 READY。`microstructure.active=0` 视为不可用。实时 Entry 的输入及每组来源时间必须先保存到 `sl_entry_snapshot`，score 通过 `entry_snapshot_id` 引用；历史重放只读该快照，不从当前网络补齐。

当前 `build_symbol(end_ms=...)` 仅将 K 线截到历史时间；OI、ratio 与 funding 仍请求当前尾部。**禁止**直接用该函数回填历史 Entry。历史回放只使用当时持久化的完整原始/feature snapshot，或先将所有依赖数据源改为同一历史截点并证明无未来数据；缺少快照时返回不可回放。实时 Entry 的 source snapshot 保存 `weights_hash`、engine version、engine config hash、`primary_tf` 和所有输入的 `as_of_ms`，以便版本变化后追溯。

# 16. VETO / PAUSE / WARN 风险引擎

VETO 与 LTSS 独立。高 LTSS 不能抵消数据身份错误、退市、极端流动性不足或正在 short squeeze 等风险。

| 等级 | 语义 | 状态优先级 |

| --- | --- | --- |

| BLOCK | 禁止作为可执行候选 | 最终 status=BLOCKED |

| PAUSE | 长期逻辑可能成立，但当前禁止入场 | 最终 status=PAUSED |

| WARN | 允许继续，但 UI 必须显式提示 | 不改变基础状态 |



## 16.1 BLOCK 规则（V1）

| Code | 触发条件 |

| --- | --- |

| VETO_DATA_IDENTITY | mapping_confidence=LOW/UNRESOLVED 或 provider identity 冲突 |

| VETO_LOW_DATA_QUALITY | Data Quality < 60 |

| VETO_LOW_LIQUIDITY | 低于 hard volume/OI 门槛或合约不可交易 |

| VETO_CONTRACT_DELISTING | 确认进入退市/交割关闭窗口 |



## 16.2 PAUSE 规则（V1）

| Code | 初始触发条件（全部配置化） |

| --- | --- |

| PAUSE_BREAKOUT_24H | priceChange24h >= +35% |

| PAUSE_BREAKOUT_7D | priceChange7d >= +70% |

| PAUSE_SQUEEZE | price↑ + OI↑ + taker buy / microstructure 同向显著增强 |

| PAUSE_NEGATIVE_CARRY | 7D/30D funding 显著转负或 positive ratio 低于门槛 |

| PAUSE_NEW_TOKEN | 有效 Binance futures onboardDate 对应的上市年龄 < `new_token_days` |

| PAUSE_CONTRACT_STATUS_UNVERIFIED | 已见合约从 live universe 消失，但尚无已验证的终止状态 |

| PAUSE_MAJOR_CATALYST | Phase 6：重大 listing/mainnet/burn/buyback 等事件窗口内 |



## 16.3 WARN 规则

| Code | 示例 |

| --- | --- |

| WARN_HIGH_VOLATILITY | ATR/volatility 异常高 |

| WARN_FUNDING_WEAKENING | 30D 正，但近 7D 明显走弱 |

| WARN_PROVIDER_PARTIAL | Unlock/Social 等部分数据不完整 |

| WARN_HIGH_CONCENTRATION | 未来如接入 holder concentration 数据时使用 |



# 17. Status 状态机

状态由 `candidateStatus`（长期研究价值）与 `executionStatus`（当前能否执行）组成，Backend 统一返回两者及派生的展示 `status`。PAUSE 只作用于已有长期候选，不能把低 LTSS 的 EXCLUDED 升格为 PAUSED。禁止 UI 自行推导。

统一内部 DTO `CandidateState` 包含 `candidate_status`、`execution_status`、`status`、`reasons`、`vetoes`、`pauses`、`warnings` 七项；后三项均为去重且保持固定规则顺序的字符串列表。`reasons` 只拼 BLOCK、PAUSE、NOT_READY/身份/陈旧数据原因，不混入仅提示的 WARN。API 层独占 snake_case→camelCase 序列化，feature/scoring/risk 不直接生产 API JSON。

```text
candidateStatus:
  LTSS is null or LTSS < 60 -> EXCLUDED
  60 <= LTSS < 70           -> WATCH
  LTSS >= 70                -> CANDIDATE

executionStatus:
  any BLOCK -> BLOCKED
  else candidateStatus != CANDIDATE -> NOT_READY
  else if mapping_confidence == MEDIUM -> NOT_READY（IDENTITY_REVIEW_REQUIRED；PAUSE 仍在 reasons 中）
  else if required cross-source multiplier is unverified -> NOT_READY（MULTIPLIER_UNVERIFIED）
  else if valid onboardDate is unavailable -> NOT_READY（LISTING_AGE_UNKNOWN）
  else any PAUSE -> PAUSED
  else if not stale and LTSS >= 80 and Entry >= 70 and DataQuality >= 80
          and Tradeability >= ready_tradeability_score
          and mapping_confidence in {VERIFIED,HIGH} -> READY
  else -> NOT_READY

status (display/API legacy convenience field):
  BLOCKED -> BLOCKED
  else if candidateStatus == EXCLUDED -> EXCLUDED
  else if candidateStatus == WATCH -> WATCH
  else if executionStatus == PAUSED -> PAUSED
  else if executionStatus == READY -> READY
  else -> CANDIDATE

analysisTier=LITE 时 UI 显示 “READY · LITE”；
analysisTier=FULL 时显示 “READY”。
```

WARN 不改变主状态，但必须在候选行和详情页可见。

Entry 为 null 不满足 READY 条件。`ready_tradeability_score` 默认 7/10，进入配置。MEDIUM identity 的 `candidateStatus` 仍按 LTSS 计算，`executionStatus=NOT_READY`；其展示 `status` 最多 CANDIDATE。所有 BLOCK/PAUSE 原因仍需返回，即使展示 status 被更高优先级覆盖。

# 18. Data Quality 设计

Data Quality 是“预期数据覆盖 + 新鲜度”指标，不等于模型置信度。LITE 与 FULL 的 expected groups 不同，因此 LITE 不会因为尚未启用 Social/Unlock 被错误扣分。

| LITE 数据组 | 权重 |

| --- | --- |

| Existing market/futures | 35 |

| Funding history | 20 |

| Fundamentals/CoinGecko | 20 |

| Spot/Basis/Liquidity | 15 |

| Identity/Profile | 10 |



| FULL 数据组 | 权重 |
| --- | --- |
| Existing market/futures | 25 |
| Funding history | 15 |
| Fundamentals | 15 |
| Spot/liquidity | 10 |
| Identity/profile | 10 |
| Unlock | 10 |
| Social | 10 |
| Catalyst（启用 FULL 时必须有结构化事件覆盖） | 5 |

FULL 合计 100。未接入 Catalyst 的版本仅提供 LITE，不得把 FULL 数据组重分配或宣称 FULL READY。每组完整且新鲜得满权重，stale 得一半，unavailable 得 0；NOT_APPLICABLE 需明确来源，相关因子保持 N/A。组内字段及权重固定如下，单字段 `NOT_APPLICABLE` 时从该组分母去除该字段，其余字段的固定分数按原组权重归一；组内全部 N/A 时该组记 N/A、不得贡献 DQ 正分。UNAVAILABLE 不移出分母，也不补偿评分权重。FULL 的 Unlock/Social/Catalyst 缺 key 或未上线时不得启用 FULL。

| 组 | 组内字段及份额（合计 100%） |
| --- | --- |
| Market/futures | 日线价格 35%，24h futures quote volume 20%，OI USD 25%，合约状态 20% |
| Funding history | 7D 20%，30D 40%，90D 40% |
| Fundamentals | MC 30%，FDV 20%，supply/float 25%，ATH/ATH date 25% |
| Spot/liquidity | Spot 60D quote volume 40%，同步 24h spot quote volume 20%，basis 20%，双侧盘口 20% |
| Identity/profile | canonical 映射 60%，profile 分类依据 40% |
| Unlock | 30D 40%，90D 40%，allocation 20% |
| Social | volume 35%，contributors 25%，dominance 25%，与价格对齐时间窗 15% |
| Catalyst | 事件源覆盖 60%，事件时间/去重 40% |

LITE 使用相同组内字段定义，其中不存在的组不参与；只在明确没有 spot market 时 Spot 两字段可 N/A，basis/盘口仍按其各自实际可用性评分。`NOT_APPLICABLE` 不等于缺 key、空响应或零事件。

确认无现货市场时 `spot_volume_decay_30d`、`spot_quote_volume_24h`、`futuresSpotVolumeRatio` 和 spot premium 均为 null，availability=`NOT_APPLICABLE/no_spot_market`；对应 Lifecycle/Carry 因子不贡献正分，也不把 N/A 写成数值 0。Spot/liquidity DQ 组只移除 Spot 60D 与 24h 两字段（40%+20%）的组内分母，basis/双侧盘口各 20% 仍按实际状态评分。期货/现货成交量比的分母为 null 时不得给 Carry 的高比值档加分。

精确 DQ 公式：对每组固定份额 `p_i`，适用集合 `A` 排除有证据的 `NOT_APPLICABLE` 字段；`field_credit_i = coverage_i × freshness_i`，其中完整覆盖为 1、缺失为 0、PARTIAL 用已验证的 `coverage_i∈[0,1]`，新鲜为 1、stale 为 0.5、超过 grace 为 0。`group_credit = Σ(i∈A)(p_i × field_credit_i) / Σ(i∈A)p_i`；`A` 为空时 group_credit=0 且组状态 N/A。`DataQuality = Σ(group_weight × group_credit)`，只在最终舍入 1 位。NOT_APPLICABLE 只调整组内分母，绝不提高该组固定外层权重；所有字段的原始状态、coverage、fetchedAt、asOf 和 reason 均随快照保存。

除 Funding 外，PARTIAL 字段的 `coverage_i` 固定为 `valid_count / required_count`：70 根已收盘日线用于 market 日线价格，60 根用于 Spot 60D quote volume，其他单值字段为 1/1；supply/float 要求 circulating 与 total 两个值（各占一半），ATH/ATH date 两个值（各占一半），双侧盘口要求 bid/ask 两侧（各占一半）。分母在配置/feature version 中固定，不能按实际返回条数缩小。例如 60/70 根 market 日线的 coverage 为 0.857142…，随后乘 freshness；没有值为 0。Funding 单独使用第 10.1 节的时间覆盖公式。业务评分需要完整窗口的因子即使 DQ 为 PARTIAL，也必须返回 null，不拿短窗代替 30D/60D。

候选 API 顶层 `stale` 仅表示该条 score 快照超过 `refresh.score_sec` 或至少一个 READY 必需的适用字段进入 stale 区间；其他非关键字段的 stale 由详情 `dataAvailability` 标记。`stale=false` 不代表所有 provider 均新鲜，UI 必须展示字段级来源。READY 必需字段 stale 时执行状态为 NOT_READY，并返回 `READY_INPUT_STALE`；不可用字段按原 DQ/风险规则处理。

数据库中的 score/status/DQ 是决策截点时的不可变证据；API 在读取时按当前时间重算字段新鲜度、当前 `dataQuality` 和可执行状态，不覆写原快照。API 返回 `asOfStatus`、`snapshotDataQuality` 供审计，`status/dataQuality/stale` 表示读取时的展示投影。投影只允许因时间流逝降级，不重新计算 LTSS/Entry 或用新 provider 数据提高旧分数。过期快照即使当时 READY，读取时也不得继续显示 READY。



## 18.1 新鲜度规则

每组数据应定义 ttl 和 grace。age <= ttl 为满权重；ttl < age <= grace 记 stale，按 50% 权重计入 DQ；age > grace 记 unavailable，0 权重。默认值：market/futures 15/60 分钟，funding history 30/120 分钟（前提是窗口覆盖完整），fundamentals 的 MC/FDV/ATH 60/360 分钟，supply/float 字段另用 6/12 小时，spot/liquidity 60/180 分钟，identity/profile 24/72 小时，unlock 12/36 小时，social 3/12 小时，catalyst 30/120 分钟。配置使用秒，且 `grace > ttl > 0`。

# 19. 嵌入式持久化：DuckDB

> **架构约束：** DuckDB 是进程内嵌入式文件，不是新数据库服务。不得引入 PostgreSQL/Redis 作为 V1 前置条件。


数据库开发态默认放在 `desktop/backend/runtime/shortlab.duckdb`；打包态默认放在可写用户数据目录，Windows 为 `%LOCALAPPDATA%/short-lab/shortlab.duckdb`，其他桌面系统为用户数据目录下的 `short-lab/shortlab.duckdb`。可用 `SHORTLAB_DATA_DIR` 显式覆盖，路径须在启动时验证可创建/写入。`sys._MEIPASS` 和 PyInstaller `_internal` 只放只读资源，绝不能作为 DuckDB 写目录；现有 evidence JSONL 的路径规则不改。打包 smoke test 要在只读安装目录运行两次，第二次验证首轮快照仍在。

## 19.1 并发访问实现

DuckDB 是同步库。为避免阻塞 FastAPI event loop，同时减少多线程写入复杂度，ShortLabRepository 建议拥有单独 ThreadPoolExecutor(max_workers=1)，所有 DB 操作通过该 worker 串行执行；写入再加内部事务。不得在 async request handler 中直接做大批量同步 SQL。

## 19.2 表结构

```sql
CREATE TABLE IF NOT EXISTS sl_schema_version (
  version INTEGER PRIMARY KEY,
  applied_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_asset (
  canonical_id VARCHAR PRIMARY KEY,
  display_symbol VARCHAR NOT NULL,
  name VARCHAR,
  categories_json VARCHAR,
  created_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_asset_mapping (
  futures_symbol VARCHAR PRIMARY KEY,
  canonical_id VARCHAR,
  spot_symbol VARCHAR,
  contract_multiplier DOUBLE,
  multiplier_source VARCHAR,
  coingecko_id VARCHAR,
  unlock_provider_id VARCHAR,
  social_provider_id VARCHAR,
  mapping_confidence VARCHAR NOT NULL,
  mapping_source VARCHAR NOT NULL,
  updated_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_contract_lifecycle (
  futures_symbol VARCHAR NOT NULL,
  observed_at_ms BIGINT NOT NULL,
  onboard_at_ms BIGINT,
  first_seen_ms BIGINT NOT NULL,
  delivery_at_ms BIGINT,
  contract_type VARCHAR,
  exchange_status VARCHAR,
  contract_multiplier DOUBLE,
  multiplier_source VARCHAR,
  PRIMARY KEY(futures_symbol, observed_at_ms)
);

CREATE TABLE IF NOT EXISTS sl_funding_event (
  symbol VARCHAR NOT NULL,
  funding_time_ms BIGINT NOT NULL,
  funding_rate DOUBLE NOT NULL,
  mark_price DOUBLE,
  PRIMARY KEY(symbol, funding_time_ms)
);

CREATE TABLE IF NOT EXISTS sl_fundamental_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  canonical_id VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  fetched_at_ms BIGINT NOT NULL,
  market_cap_usd DOUBLE,
  fdv_usd DOUBLE,
  circulating_supply DOUBLE,
  total_supply DOUBLE,
  max_supply DOUBLE,
  ath_price DOUBLE,
  ath_date_ms BIGINT,
  source VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_feature_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  fundamental_snapshot_id VARCHAR,
  feature_version VARCHAR NOT NULL,
  features_json VARCHAR NOT NULL,
  source_meta_json VARCHAR NOT NULL,
  data_quality DOUBLE NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_entry_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  entry_version VARCHAR NOT NULL,
  dive_weights_hash VARCHAR NOT NULL,
  dive_engine_version VARCHAR NOT NULL,
  dive_config_hash VARCHAR NOT NULL,
  primary_tf VARCHAR NOT NULL,
  inputs_json VARCHAR NOT NULL,
  components_json VARCHAR NOT NULL,
  source_meta_json VARCHAR NOT NULL,
  entry_score DOUBLE,
  created_at_ms BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_score_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  generation_id VARCHAR NOT NULL,
  feature_snapshot_id VARCHAR NOT NULL,
  entry_snapshot_id VARCHAR,
  symbol VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  analysis_tier VARCHAR NOT NULL,
  profile VARCHAR NOT NULL,
  score_version VARCHAR NOT NULL,
  entry_version VARCHAR,
  feature_version VARCHAR NOT NULL,
  config_hash VARCHAR NOT NULL,
  ltss DOUBLE,
  entry_score DOUBLE,
  data_quality DOUBLE NOT NULL,
  candidate_status VARCHAR NOT NULL,
  execution_status VARCHAR NOT NULL,
  status VARCHAR NOT NULL,
  module_scores_json VARCHAR NOT NULL,
  vetoes_json VARCHAR NOT NULL,
  pauses_json VARCHAR NOT NULL,
  reasons_json VARCHAR NOT NULL,
  warnings_json VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS sl_forward_outcome (
  score_snapshot_id VARCHAR NOT NULL,
  horizon VARCHAR NOT NULL,
  outcome_status VARCHAR NOT NULL,
  reason_code VARCHAR,
  entry_ts_ms BIGINT,
  exit_ts_ms BIGINT,
  horizon_due_ms BIGINT NOT NULL,
  formula_version VARCHAR NOT NULL,
  cost_config_hash VARCHAR NOT NULL,
  funding_event_count INTEGER,
  funding_coverage DOUBLE,
  graded_at_ms BIGINT NOT NULL,
  entry_price DOUBLE,
  exit_price DOUBLE,
  price_short_return DOUBLE,
  funding_carry DOUBLE,
  fee_assumption DOUBLE,
  slippage_assumption DOUBLE,
  net_short_return DOUBLE,
  mae DOUBLE,
  mfe DOUBLE,
  PRIMARY KEY(score_snapshot_id, horizon, formula_version, cost_config_hash)
);

CREATE TABLE IF NOT EXISTS sl_job_run (
  job_id VARCHAR PRIMARY KEY,
  job_type VARCHAR NOT NULL,
  started_at_ms BIGINT NOT NULL,
  finished_at_ms BIGINT,
  status VARCHAR NOT NULL,
  stats_json VARCHAR,
  error_code VARCHAR
);
```

`migrations/001_init.sql` 只创建以上 V1 表。Phase 5 的 `migrations/002_unlock_social.sql` 创建以下两张表；Phase 6 的 `migrations/003_catalyst.sql` 创建最后一张表。每个文件由 `sl_schema_version` 控制仅应用一次，重复启动不得清表或覆盖旧快照。`known_at_ms` 表示本地首次能依据该来源得知事件的时间，历史回放不得使用晚于 score 截点的版本。

```sql
-- 002_unlock_social.sql（Phase 5）
CREATE TABLE IF NOT EXISTS sl_unlock_event (
  event_id VARCHAR NOT NULL,
  canonical_id VARCHAR NOT NULL,
  known_at_ms BIGINT NOT NULL,
  unlock_at_ms BIGINT NOT NULL,
  amount_tokens DOUBLE NOT NULL,
  allocation_type VARCHAR NOT NULL,
  source VARCHAR NOT NULL,
  fetched_at_ms BIGINT NOT NULL,
  PRIMARY KEY(event_id, known_at_ms)
);
CREATE TABLE IF NOT EXISTS sl_social_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  canonical_id VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,
  fetched_at_ms BIGINT NOT NULL,
  source VARCHAR NOT NULL,
  metrics_json VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sl_unlock_asset_time ON sl_unlock_event(canonical_id, unlock_at_ms, known_at_ms);
CREATE INDEX IF NOT EXISTS idx_sl_social_asset_time ON sl_social_snapshot(canonical_id, as_of_ms);
```

```sql
-- 003_catalyst.sql（Phase 6）
CREATE TABLE IF NOT EXISTS sl_catalyst_event (
  event_id VARCHAR NOT NULL,
  canonical_id VARCHAR NOT NULL,
  known_at_ms BIGINT NOT NULL,
  announced_at_ms BIGINT NOT NULL,
  effective_at_ms BIGINT,
  event_type VARCHAR NOT NULL,
  severity VARCHAR NOT NULL,
  confidence DOUBLE NOT NULL,
  source_url VARCHAR,
  title VARCHAR NOT NULL,
  PRIMARY KEY(event_id, known_at_ms)
);
CREATE INDEX IF NOT EXISTS idx_sl_catalyst_asset_time ON sl_catalyst_event(canonical_id, effective_at_ms, known_at_ms);
```

`snapshot_id` 为写入时生成的不可变 ID；同一 `symbol/as_of_ms` 在不同 profile、配置或重算版本下允许多条 score，不能覆盖旧记录。`sl_score_snapshot.feature_snapshot_id` 必须引用实际使用的 feature 快照；feature 快照须记录各原始来源的 snapshot ID 或其 `fetched_at_ms/as_of_ms`、状态和原始值。Score 写入时固定同一轮 refresh 的决策截点，任何数据的可用时间不得晚于该截点。`sl_score_snapshot.entry_snapshot_id` 指向独立 Entry 快照；Entry 未计算时该引用和 `entry_version/entry_score` 均为 null，已计算时三者必须一致。Entry 快照的 `source_meta_json` 逐组件保存 `fetched_at_ms/as_of_ms/status/coverage_fraction/reason_code/source`，`inputs_json` 保存评分所用的冻结输入及窗口数组，`components_json` 保存六项贡献；`dive_*` 字段记录旧引擎版本与权重/配置。缺任一来源元数据时 Entry 不可宣称可重算、不可 READY。查询“最新”须按明确的生成时间和版本选择，不能只按 `as_of_ms` 任意取一条。迁移实现应为引用和索引制定具体 SQL；这里的 DDL 是逻辑契约，不表示无需迁移测试。

Repository 接口固定为 `save_entry(entry_snapshot) -> snapshot_id`、`save_feature(feature_snapshot) -> snapshot_id`、`save_score(score_snapshot, entry_snapshot_id | null) -> snapshot_id`。`save_entry` 在 `save_score` 前完成并可从 DB 独立读回；score 与 Entry 的 symbol 和 as-of 必须匹配。历史重放只用这三类已存快照，绝不以一个 `entry_source_as_of_ms` 代替 Entry 输入。

同轮 refresh 的 score 共用 `generation_id`。Repository 必须建立 `sl_score_snapshot(generation_id, status, ltss, entry_score, data_quality, symbol, snapshot_id)`、`sl_score_snapshot(symbol, as_of_ms)`、`sl_feature_snapshot(symbol, as_of_ms)`、`sl_entry_snapshot(symbol, as_of_ms)`、`sl_funding_event(symbol, funding_time_ms)`、`sl_forward_outcome(outcome_status, horizon_due_ms)` 索引；`data_quality` 作为 score 列保存，分页排序不解析 JSON。写 score 前验证 feature snapshot 和非空的 entry snapshot 均存在且 symbol/as_of 匹配，写 outcome 前验证 score snapshot 存在；DuckDB 版本支持并通过迁移测试时可同时添加外键约束。候选分页只读同一个完整 generation，不能混合刷新中的半批结果。

`generation_id` 直接使用该轮 `sl_job_run.job_id`。在同一事务中写完该轮 score 后将 job 标为 `SUCCEEDED`；候选查询只选择 `job_type=score_refresh AND status=SUCCEEDED` 的 generation，按 `finished_at_ms DESC, job_id ASC` 选最新。失败/运行中的批次对分页不可见，旧完整批次继续服务并按快照时间标 stale。

事务边界固定为：Entry 与 feature 快照先作为不可变事实分别持久化，可留下未引用的孤立快照；单个 `score_refresh` generation 的所有 score 行及对应 job 的 `SUCCEEDED/finished_at_ms` 必须在**同一事务**提交。该事务失败则回滚该 generation 的 score 与成功状态，旧 generation 继续可见。读端只选择已提交的成功 generation；孤立 Entry/feature 快照不作为候选返回，可在有审计记录的维护任务中清理。

`source_meta_json` 必须是字段级字典，键与 `features_json` 的 feature/source ID 对应，值至少含 `status`、`fetched_at_ms`、`as_of_ms`、`coverage_fraction`、`reason_code`、`source`；不可用字段也要有元数据记录。`features_json` 只保存原始/派生数值与 reason，不将 `null` 序列化成 0。Repository 写入时校验 source meta 覆盖所有用于 DQ 和 READY 的字段，读时用这些时间执行第 18 节的 DQ/STALE 投影；缺元数据的旧快照不得显示 READY。

## 19.3 迁移要求

- 只允许向前迁移；迁移必须幂等或由 schema version 保证仅执行一次。

- 应用启动时先检查 schema version，迁移失败必须使 Short-Lab unavailable，但不能让原 Dive 服务整体无法启动；UI 显示 Short-Lab 初始化失败。

- 不得自动删除历史表或清空 runtime 数据。破坏性迁移必须显式工具/命令并有备份说明。

## 19.4 保留与压缩

Funding events、score/feature 快照及 forward outcome 属于可审计事实，默认永久保留，不自动删除。高频原始 provider payload 最多保留 90 天，保留 hash、来源时间与用于评分的归一化快照；每日统计聚合可另建表，但不得代替原始 score/outcome。每月记录 DuckDB 文件大小、行数与增长率，达到配置的磁盘告警阈值时只告警，不自动清库；归档/压缩操作须有备份、版本和恢复测试。



# 20. Point-in-time 与防未来数据泄漏

Short-Lab 的证据价值取决于“当时能知道什么”。不得在几个月后用今天的 ATH/FDV/Unlock/Social 状态去重算过去分数，并声称是历史回测。

- 每次 score 必须引用同一 as_of_ms 附近的 feature snapshot。

- 基本面、供给、social 都要按 snapshot 存储。

- forward grader 只能读取 score_as_of_ms 之后的数据。

- 历史 replay 只能使用当时已存储的 feature snapshot；缺失则显示不可回放，不能用当前 provider 数据补齐。

- 所有 score_snapshot 保存 feature_version/config_hash，并引用不可变 feature snapshot，避免参数变化后混淆。



# 21. Forward Evidence / Backtest 设计

## 21.1 评估周期

| Horizon | 用途 |

| --- | --- |

| 7D | 短期验证 Entry 与 squeeze 风险 |

| 30D | 核心中期验证 |

| 90D | 长期衰退与 carry 验证 |



## 21.2 收益公式

```text
price_short_return = 1 - exit_price / entry_price
funding_carry = SUM(event.funding_rate * event.mark_price / entry_price
                    for entry_ts < event.funding_time <= exit_ts)
net_short_return = price_short_return + funding_carry - fee_assumption - slippage_assumption
```

上述为**固定初始名义本金、不加仓、不复投**的研究收益口径，并非账户实际 PnL。入场取 `score_as_of_ms` 后第一个完整、可交易的 1h bar 开盘价，7D/30D/90D 退出取对应期限之后第一个完整 1h bar 开盘价；缺 bar 或合约提前终止时结果为未完成/不可用，不能向前猜价。Funding 按持仓期间每次实际结算事件和该事件的 mark price 计算收到的金额，再除以初始名义本金；不得直接把费率相加称为精确净收益。手续费与滑点以初始名义本金为分母，包含开仓和退出两侧假设。所有使用的原始 bar、结算事件、成本配置版本与计算公式版本应可追溯。

`sl_funding_event.mark_price` 缺失或非正数的结算事件不能用当前 ticker、相邻 K 线或费率本身补值；该事件不计入 carry，`funding_coverage` 下降并记录 `FUNDING_MARK_MISSING`。只要持仓区间有此缺口，`funding_carry/net_short_return=null`，price return 仍可独立展示；正常合约 outcome 为 UNAVAILABLE，退市合约保留 CENSORED 并注明净收益不可算。入场价和 funding mark 必须处于同一合约报价单位，倍率只用于跨源现货比较。

`outcome_status` 固定为 PENDING、COMPLETE、CENSORED、UNAVAILABLE。到期前为 PENDING；足额行情和 funding 覆盖后为 COMPLETE；合约交割/退市导致原定期限不可持有时为 CENSORED，记录最后可交易时间和有证据的最终可交易/结算价格，并与 COMPLETE 分开统计；无法可靠确定价格或覆盖时为 UNAVAILABLE。不能把 CENSORED/UNAVAILABLE 从样本中无痕删除，报告需列出各状态数量和原因，以免形成存活者偏差。

交易费与滑点不要写死为某个交易所当前费率。数据库保存原始 price return/funding carry，同时 grader 接收配置化 cost assumptions，并在 UI/报告明确展示假设。

默认研究假设为开仓手续费 0.0005、平仓手续费 0.0005、开仓滑点 0.001、平仓滑点 0.001，均以初始名义本金为分母，默认总成本 0.003（0.30%）；这不是当前交易所实际费率。`fee_assumption` 存双侧合计 0.001，`slippage_assumption` 存双侧合计 0.002，二者和 `cost_config_hash` 一起入库。改动任一成本参数只生成新 outcome 版本，原 score/原 outcome 不覆写。

## 21.3 MAE / MFE

```text
For a short position, use only completed 1h bars whose close_ts satisfies
entry_ts < close_ts <= exit_ts (entry bar included; exit bar after its open excluded):
MAE = max(0, max((future_high - entry_price) / entry_price))
MFE = max(0, max((entry_price - future_low) / entry_price))

示例：$1 开空 -> 中途 $3 -> 最终 $0.2
最终方向正确，但 MAE=+200%，说明“放着不管”策略执行风险极高。
```

## 21.4 Evidence 统计复用

尽量复用现有 evidence 中的 Wilson、ECE/Brier、IC、baseline、replay 工具，但必须为 7D/30D/90D 新 horizon 编写独立测试。现有 1h/4h/24h 逻辑不得被 Short-Lab 修改后破坏。

# 22. Backend Service 与 Scheduler

## 22.1 ShortLabRuntime

```text
ShortLabRuntime
  ├─ config
  ├─ repository
  ├─ identity_resolver
  ├─ provider_registry
  ├─ shortlab_service
  └─ scheduler

start():
  - open/migrate DB
  - warm identity/fundamental cache (non-blocking)
  - start periodic asyncio tasks

stop():
  - set shutdown event
  - cancel/await tasks
  - flush repository
  - close executor/connection
```

`shortlab/providers/base.py:ProviderRegistry` 是 provider 注册入口；runtime/service 只依赖该接口，Phase 5/6 在同一 runtime 注册 Unlock/Social/Catalyst 并按 `requestedTier/analysisTier` 切换，不能另起服务或复制 scheduler。`ShortLabService.evidence_summary(filters)` 是稳定服务接口：Phase 3 metrics 未就绪时返回 Unavailable 交给已注册 router 映射为 503；Phase 4 metrics 实现后返回真实聚合，不改 router。FULL 接线必须经现有 `/api/short/*` 集成测试证明可用，不能只交付评分纯函数。

## 22.2 禁止外部任务系统

V1 使用进程内 asyncio 周期任务即可。不得引入 Celery/Redis/APScheduler Server。可以使用 asyncio.create_task + shutdown event + per-job lock。

## 22.3 作业互斥

同一 job_type 只能有一个运行实例。用户手动 refresh 与定时 refresh 冲突时应复用/拒绝重复任务，返回已有 job id，而不是并发抓两遍全市场。

# 23. 推荐刷新计划

| 数据/任务 | 默认 TTL/周期 | 说明 |

| --- | --- | --- |

| 现有价格/live | 沿用现有 | Short-Lab 不改变 |

| OI / ratios / futures metrics | 5~15 min | 优先读取现有缓存 |

| Funding history 增量 | 15 min + settlement | 按 funding_time 去重 |

| CoinGecko market fields | 60 min | 全 universe 分批缓存 |

| Supply | 6 h | 变化慢 |

| LTSS-LITE | 30~60 min | 长期分数无需秒级 |

| Entry Score shortlist | 60 min / 按需 | 每轮自动只深算 top 10，余下排队且受同一预算约束 |

| Forward outcome grader | 6 h 或每天 | 只处理到期 horizon |

| Unlock Phase 5 | 6~12 h | 按 provider 限流 |

| Social Phase 5 | 1~3 h | 避免高频高成本 |

| Catalyst Phase 6 | 15~30 min | 事件型数据 |



所有周期必须可配置，并带随机 jitter，避免每次启动同时打满第三方 API。

周期性任务在每次计划执行前独立抽取 `uniform(0, refresh.jitter_sec)` 延迟；默认 `jitter_sec=300`，手动刷新不加 jitter，但仍受同一互斥/限流器约束。测试注入固定随机源，不使用真实等待。fundamental 市值/ATH 目标新鲜度 3600 秒，supply 目标 21600 秒；同一次 CoinGecko 响应若同时含 supply，就顺带更新 supply 缓存，不为 supply 另发重复请求。仅当 6 小时内未从 market 请求得到 supply 时才独立安排 supply 刷新。

# 24. 配置规范

现有 `engine/loader.py` 只读取 engine 的 `config/default.yaml`，且结果会被缓存；不得修改其返回对象或向 engine 配置塞入 Short-Lab 字段。新增独立 `shortlab/config.py`，读取随包分发的 `shortlab/default.yaml`，用同一 PyYAML 依赖解析并做严格校验。可通过 `SHORTLAB_CONFIG_PATH` 指定用户覆盖 YAML；覆盖层仅允许已知键、深度合并，未知键/类型错误令 Short-Lab unavailable，不影响旧服务。运行时只读取配置一次并记录 normalized config hash；密钥仍只读环境变量。

```yaml
shortlab:
  enabled: true
  analysis_tier: LITE

  universe:
    limit: 500
    shortlist_size: 50
    entry_depth_top: 10

  candidate:
    watch_ltss: 60
    candidate_ltss: 70
    ready_ltss: 80
    ready_entry: 70
    ready_data_quality: 80
    ready_tradeability_score: 7
    entry_required_blocks: [consensus, mtf, micro, regime, failed_bounce, funding]

  score_weights:
    MEME_LITE: {lifecycle: 40, carry: 45, valuation: 5, tradeability: 10}
    GENERAL_LITE: {lifecycle: 35, carry: 40, valuation: 15, tradeability: 10}
    LOW_FLOAT_VC_LITE: {lifecycle: 25, carry: 30, valuation: 35, tradeability: 10}
    # FULL 的 valuation 键表示 Valuation/Supply 合成模块 raw25
    MEME_FULL: {lifecycle: 30, carry: 35, valuation: 5, narrative: 20, tradeability: 10}
    GENERAL_FULL: {lifecycle: 25, carry: 30, valuation: 20, narrative: 15, tradeability: 10}
    LOW_FLOAT_VC_FULL: {lifecycle: 20, carry: 25, valuation: 35, narrative: 10, tradeability: 10}

  quality_freshness_sec:
    market_futures: {ttl: 900, grace: 3600}
    funding_history: {ttl: 1800, grace: 7200}
    fundamentals: {ttl: 3600, grace: 21600}
    spot_liquidity: {ttl: 3600, grace: 10800}
    identity_profile: {ttl: 86400, grace: 259200}
    unlock: {ttl: 43200, grace: 129600}
    social: {ttl: 10800, grace: 43200}
    catalyst: {ttl: 1800, grace: 7200}

  quality_field_overrides_sec:
    supply_float: {ttl: 21600, grace: 43200}

  liquidity:
    hard_min_futures_volume_usd: 10000000
    hard_min_open_interest_usd: 2000000
    preferred_futures_volume_usd: 30000000
    preferred_open_interest_usd: 5000000

  lifecycle:
    ideal_ath_drawdown_min: 0.40
    ideal_ath_drawdown_max: 0.70

  funding:
    lookbacks_days: [7, 30, 90]
    history_requests_per_5min: 80
    backfill_symbols_per_batch: 80

  evidence_cost:
    entry_fee: 0.0005
    exit_fee: 0.0005
    entry_slippage: 0.001
    exit_slippage: 0.001

  veto:
    breakout_24h: 0.35
    breakout_7d: 0.70
    new_token_days: 45

  refresh:
    fundamental_sec: 3600
    supply_sec: 21600
    score_sec: 1800
    entry_sec: 3600
    grader_sec: 21600
    jitter_sec: 300
    entry_concurrency: 2
    entry_max_upstream_calls_per_run: 240
    entry_cache_ttl_sec: 3600

  providers:
    coingecko:
      enabled: true
      api_key_env: DIVE_COINGECKO_API_KEY
    unlock:
      enabled: false
      api_key_env: DIVE_TOKENOMIST_API_KEY
    social:
      enabled: false
      api_key_env: DIVE_LUNARCRUSH_API_KEY
    catalyst:
      enabled: false
      api_key_env: null
```

`default.yaml` 必须与上例逐键一致；不得在代码中另藏一份不同默认值。环境变量固定为 `SHORTLAB_CONFIG_PATH`（可选覆盖配置文件）、`SHORTLAB_DATA_DIR`（可选数据目录）、各 provider 的 `api_key_env` 所指变量；路径和密钥值不写入持久化 score。`config_hash` 仅覆盖影响 score/status/DQ 的 `candidate`、`score_weights`、`quality_freshness_sec`、`quality_field_overrides_sec`、`liquidity`、`lifecycle`、`funding.lookbacks_days` 与 `veto`；`evidence_cost` 单独生成 `cost_config_hash`，`refresh`、provider key 名称和本机路径不进入 score hash。算法：解析并深度合并已知 YAML 键、校验并将整数/浮点规范化为 JSON 数值，按 UTF-8 的 `json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)` 序列化后 SHA256 十六进制全长。因子分箱与窗口属于 `feature_version/score_version` 的固定数学合同；变更它们必须升级版本并保留旧实现用于重放，不能悄悄只改 YAML。

以上默认 YAML 的评分相关子树按该算法得到 golden `config_hash=4909ffe7d43c294983d313cff65c6d73125b921944e403a1e1e152b91a869786`；配置测试须固定该值。`refresh`、`providers`、`SHORTLAB_CONFIG_PATH`、`SHORTLAB_DATA_DIR` 或密钥环境变量的变化不得改变此 hash；改评分相关键必须改变它。

## 24.1 配置校验

- 权重必须合计 100；不满足时启动 Short-Lab 失败并给明确错误。

- ready_ltss >= candidate_ltss >= watch_ltss。

- TTL/周期不得为负；provider enabled=true 且需要 key 但 key 缺失时 provider=UNAVAILABLE，不得使原 App 崩溃。

- API Key 只能来自环境变量/现有 secrets 机制，不允许写入 repo、UI localStorage 或 API 响应。



# 25. API 设计

所有接口继续属于现有 FastAPI。建议单独 router prefix=/api/short，最终由 create_app() include_router。

| Method | Path | 作用 |

| --- | --- | --- |

| GET | /api/short/health | Short-Lab 初始化、DB、provider 简要状态 |

| GET | /api/short/candidates | 分页候选、筛选、排序 |

| GET | /api/short/symbol/{symbol} | 单币完整 Short-Lab 详情 |

| GET | /api/short/symbol/{symbol}/history | score/feature 历史 |

| GET | /api/short/providers | provider 状态/新鲜度，不返回 secret |

| POST | /api/short/refresh | 提交异步 refresh job；返回 202 + job_id |

| GET | /api/short/refresh/{job_id} | 查询刷新进度/结果 |

| GET | /api/short/evidence/summary | 7D/30D/90D 聚合表现；Phase 4 上线 |

`api/shortlab.py` 由 API 任务唯一拥有。Phase 3 即注册 `/api/short/evidence/summary`，若 grader/summary service 尚未就绪则返回 `503 short_evidence_unavailable`，不伪造空统计；Phase 4 的 Evidence 任务只实现 `shortlab/evidence/metrics.py:summary(...)` 和 service 接口，router 通过运行时依赖在同一路径返回 200，Evidence 任务不得另改 router。验收分别测试 Phase 3 的 503 与 Phase 4 的真实 200。



## 25.1 GET /api/short/candidates

```text
Query params:
  status=READY|CANDIDATE|WATCH|EXCLUDED|PAUSED|BLOCKED
  candidate_status=EXCLUDED|WATCH|CANDIDATE
  execution_status=NOT_READY|READY|PAUSED|BLOCKED
  category=MEME|...
  profile=MEME_LITE|GENERAL_LITE|LOW_FLOAT_VC_LITE
  min_ltss=0..100
  min_entry=0..100
  min_funding_30d=<decimal，例 0.005 表示 0.5%>
  ath_drawdown_min=-1..0
  ath_drawdown_max=-1..0
  min_data_quality=0..100
  sort=ltss|entry|funding30d|dataQuality
  order=desc|asc
  generation_id=<上页返回的 generationId；首页省略以选最新完整批次>
  limit=1..200
  offset>=0
```

默认排序为展示状态优先级 `READY > CANDIDATE > WATCH > PAUSED > BLOCKED > EXCLUDED`，再按 `ltss DESC NULLS LAST`、`entry_score DESC NULLS LAST`、`data_quality DESC`、`symbol ASC`、`snapshot_id ASC`。显式 `sort` 时先按请求列和 `order` 排，null 永远末尾，再以 `symbol ASC, snapshot_id ASC` 破平。SQL 使用固定排序列白名单，不能把查询值拼成任意 SQL。首页固定最新**已完成** `generationId`，后续 offset 页必须传该 ID；未知或已归档 ID 返回 404，确保刷新并发时分页不漂移。

这套默认序是**可执行候选优先**，不表示风险严重度；PAUSED 排在 BLOCKED 前是为了先看可能恢复的候选。风险审查分别使用 `execution_status=BLOCKED` 或 `execution_status=PAUSED` 筛选并在行内显示红色 BLOCK/橙色 PAUSE 标识，不得因默认排序隐藏 BLOCK 原因。`ath_drawdown_min <= ath_drawdown_max`，所有 min/max 使用后端小数语义；过滤值缺失的行不满足该过滤条件。非法范围或未知 sort 返回 422。

## 25.2 Candidate 响应契约

```json
{
  "schemaVersion": "shortlab.api.v1",
  "generationId": "refresh-20261001T000000Z",
  "generatedAtMs": 1760000000000,
  "analysisTier": "LITE",
  "scoreVersion": "ltss-lite-v1",
  "items": [{
    "symbol": "1000PEPEUSDT",
    "canonicalId": "pepe",
    "profile": "MEME_LITE",
    "categories": ["MEME"],
    "status": "CANDIDATE",
    "asOfStatus": "CANDIDATE",
    "candidateStatus": "CANDIDATE",
    "executionStatus": "NOT_READY",
    "ltss": 84.2,
    "entryScore": 63.1,
    "dataQuality": 94.0,
    "snapshotDataQuality": 94.0,
    "moduleScores": {
      "lifecycle": 34.0,
      "carry": 38.2,
      "valuation": 3.0,
      "tradeability": 9.0
    },
    "metrics": {
      "funding30d": 0.0125,
      "positiveFundingRatio30d": 0.87,
      "athDrawdown": -0.58,
      "oiMarketCapRatio": 0.14,
      "futuresSpotVolumeRatio": 6.2
    },
    "vetoes": [],
    "warnings": ["WARN_FUNDING_WEAKENING"],
    "pauses": [],
    "reasons": ["ENTRY_BELOW_READY_THRESHOLD"],
    "dataAvailability": {"fundingHistory": "OK", "spot": "OK", "fundamentals": "OK"},
    "asOfMs": 1760000000000,
    "stale": false
  }],
  "total": 123
}
```

## 25.3 单币详情

详情必须同时返回原始关键指标、模块解释、Existing Dive verdict/MTF/Micro/Regime 引用、risk events、data availability。UI 不应根据候选简表再次拼业务逻辑。

候选及详情共用 `CandidateState` 合同：`candidateStatus/executionStatus/status/reasons`。`reasons` 是去重、稳定排序的机器码列表：先 BLOCK，后 PAUSE，再 NOT_READY 原因；独立 `vetoes`、`pauses`、`warnings` 各保留原始规则码。WARN 不进入 `reasons`，除非该警告本身导致 NOT_READY。`moduleScores` 为 LTSS 模块贡献，`metrics` 为原始输入，不得混装规则码。API 序列化只在 router 层由 Python snake_case 转 camelCase。

NOT_READY 原因码固定为 `LTSS_BELOW_READY`、`ENTRY_NOT_AVAILABLE`、`ENTRY_BUDGET_EXHAUSTED`、`ENTRY_BELOW_READY_THRESHOLD`、`DATA_QUALITY_BELOW_READY`、`TRADEABILITY_BELOW_READY`、`IDENTITY_REVIEW_REQUIRED`、`MULTIPLIER_UNVERIFIED`、`LISTING_AGE_UNKNOWN`、`READY_INPUT_STALE`；实现可同时返回多个，按此顺序排列。与 `VETO_*`、`PAUSE_*` 分开，避免 UI 推断业务语义。

## 25.4 错误语义

| 场景 | HTTP | Body 语义 |

| --- | --- | --- |

| 未知 Binance symbol | 404 | short_symbol_not_found |

| Short-Lab 未初始化 | 503 | shortlab_unavailable |

| provider 部分失败 | 200 | 相关字段 null + provider status；不能整页 500 |

| refresh 已有同类任务 | 202 | 返回 `{"jobId":"...","existing":true}`；请求已接受并复用在途任务 |

| 非法筛选参数 | 422 | FastAPI validation |



# 26. UI / UX 设计

## 26.1 导航

在现有 Depth Terminal 导航增加 SHORT LAB。不得重做整个 UI 框架，不引入第二套设计语言。

内部 view id 固定为 `shortlab`，hash 路由为 `#/shortlab`；NAV、命令面板和快捷键列表使用同一 ID。现有 `desktop-app.jsx` 对 `noData` 的全屏 `DataSourceDown` 门控必须让 Short Lab 走自己的 API 状态页；仅 Short-Lab view 接受此例外，其他现有视图维持原行为。

## 26.2 Scanner 表格

| 列 | 说明 |

| --- | --- |

| Symbol | Binance futures symbol + category badge |

| LTSS | 0~100，显示 LITE/FULL tier |

| Entry | 0~100 |

| 30D Fund | 30D 累计 funding |

| Fund+ | 30D 正 funding ratio |

| ATH DD | ATH 回撤 |

| OI/MC | OI / market cap |

| Spot/Fut | Futures/Spot volume ratio |

| Data | Data Quality |

| Status | READY/CANDIDATE/WATCH/PAUSED/BLOCKED |



## 26.3 过滤与排序

- Profile / Category / Status。

- LTSS 最小值、Entry 最小值。

- Funding 正值门槛、ATH drawdown 范围、Data Quality 门槛。

- 默认排序与第 25.1 节完全一致：READY > CANDIDATE > WATCH > PAUSED > BLOCKED > EXCLUDED，同状态 LTSS desc、Entry desc、DQ desc，最后 symbol/snapshot_id 升序。



## 26.4 Detail 页固定区块

| 区块 | 必须展示 |

| --- | --- |

| Summary | LTSS、Entry、Data Quality、Profile、Tier、Status、as-of |

| Structure | ATH DD、ATH age、30D price、MA、lower-high、spot volume decay |

| Carry | 7/30/90D funding、positive ratio、APR（标记为历史简单年化）、OI/MC、OI trend |

| Valuation | MC、FDV、FDV/MC、float；不可用显示 N/A |

| Tokenomics | Phase 5：30/90D unlock 与 allocation |

| Narrative | Phase 5：social volume/contributors/dominance decay |

| Existing Dive | verdict、confidence、MTF、micro、regime、whale |

| Risks | BLOCK/PAUSE/WARN，显示规则 code 与可读解释 |

| Data Sources | 每个 provider 的 fetchedAt/stale/unavailable |



## 26.5 失败与陈旧数据

UI 必须区分 LIVE / STALE / UNAVAILABLE。不得显示看似正常的 0.00，也不得切到 mock 数据。Short-Lab API 失败时只影响 Short-Lab view；其他现有页面继续工作。

当前 `desktop-app.jsx` 在 universe 启动失败且 `noData` 时，对设置页以外的所有视图显示全屏 `DataSourceDown`。接入 Short Lab 时必须让其拥有独立的数据加载与错误状态，避免该全局门控遮住 Short-Lab 的 STALE/UNAVAILABLE 页面；同时接入 `NAV`、`VIEW_HASH`、`KEY_VIEWS`、命令面板和相应轮询。不能只添加导航按钮。

# 27. 与现有 Evidence / Replay 的集成

Short-Lab 不直接修改 runtime/evidence.jsonl 现有 schema。建议在 DuckDB 中维护自身 feature/score/outcome 快照，并在统计实现层复用现有 evidence 数学工具。

## 27.1 必须保留的可验证性

- 每条 score 可追溯到 as_of、score_version、config_hash、feature snapshot。

- 每个模块都能返回 reason/evidence list，至少说明贡献最高的 3 个因子与扣分因子。

- Evidence 报告必须按 profile、LTSS bucket、Entry bucket、regime、funding bucket 切片。

- Suggest weights 只能 report-only，禁止自动修改生产权重。



# 28. 安全、隐私与外部 API Key

| 要求 | 实现 |

| --- | --- |

| 仍绑定 localhost | 默认 127.0.0.1；不要为了 Short-Lab 改成 0.0.0.0 |

| 不存交易 Key | V1 无任何交易账户/下单能力 |

| 第三方 Provider Key | 只读环境变量/现有 secrets；日志与错误必须脱敏 |

| 外部内容 | Catalyst/news 作为不可信输入，不直接 innerHTML 渲染 |

| 超时/大小 | 所有外部 HTTP 设置 connect/read timeout、响应大小与重试上限 |

| 依赖许可 | 新增 SDK/数据源实施前审查 license/terms；优先直接 HTTP + 现有 aiohttp |



# 29. 性能与限流

## 29.1 不得拖慢现有 /api/scan

Short-Lab 外部 provider 请求绝不能进入现有 /api/scan 同步关键路径。/api/scan 在 CoinGecko/Unlock/Social 全部失败时仍应与改造前表现一致。

## 29.2 缓存层级

- Existing data cache：沿用现有。

- Short-Lab in-memory cache：适合 provider metadata、latest candidate list。

- DuckDB snapshot：历史事实与冷启动复用。

- Provider HTTP：只有 cache miss / TTL 到期才调用。



## 29.3 Rate limit

优先复用现有 data/http.py 的 429/451 退避与 aiolimiter 模式；外部 provider 使用各自 limiter。429 不可无限立即重试；必须记录 retry-after/backoff，并降级为 stale cached data。

# 30. 打包与运行时兼容

现有 Desktop 支持预构建 React bundle 和 PyInstaller 打包。新增 DuckDB 与配置文件后，必须把“源码开发可运行”与“打包后可运行”都纳入验收。

## 30.1 pyproject / dependency

- V1 新增最小依赖优先：duckdb；HTTP 优先沿用现有 aiohttp/aiolimiter/pyyaml。

- 不要为了一个 API 引入大型 SDK，除非 SDK 明显解决签名/分页且 license 合适。



## 30.2 PyInstaller

- 将 `desktop/backend/dive.spec` 更名为 `desktop/backend/short-lab.spec`，更新 requirements freeze 以包含 duckdb 运行时库与 `shortlab/default.yaml`；spec 内 `EXE` 和 `COLLECT` 名称均为 `short-lab`。同步更新 `.github/workflows/release.yml` 中的构建命令、`dist/short-lab`、zip、artifact 与 smoke 路径；旧 CLI `dive-desktop` 仍保留。

- 验证 packaged app 能创建/迁移 shortlab.duckdb。

- 验证 runtime 路径可写，不能尝试写入冻结资源目录。

- 验证 UI dist 包含 Short-Lab 新 view。



# 31. 日志与可观测性

日志应接入现有 Network Log / backend logging 风格，不建立独立日志服务。

| 事件 | 必须记录 |

| --- | --- |

| refresh job | job_id、type、duration、success/fail counts |

| provider | source、status、latency、HTTP code；不记录 key |

| identity | unresolved/mismatch symbol |

| scoring | score_version、candidate count、ready/paused/blocked count |

| DB | migration version、失败原因 |



# 32. 测试策略

## 32.1 回归门槛

> **硬门槛：** 现有 backend、root E2E、UI 测试必须保持通过。本次不修改或验收 Android。


## 32.2 新增 Backend 测试文件合同

文件名与实施计划 Task 0–18 一致；每个任务可以在该文件内增加测试函数，不自行改名或拆成另一套清单。

```text
desktop/backend/tests/
  test_shortlab_source_contracts.py
  test_shortlab_config.py
  test_shortlab_models.py
  test_shortlab_repository.py
  test_shortlab_funding_history.py
  test_shortlab_quote_volume.py
  test_shortlab_identity.py
  test_shortlab_spot_history.py
  test_shortlab_liquidity_units.py
  test_shortlab_contract_lifecycle.py
  test_shortlab_coingecko.py
  test_shortlab_scoring.py
  test_shortlab_status.py
  test_shortlab_entry.py
  test_shortlab_runtime.py
  test_shortlab_api.py
  test_shortlab_evidence.py
  test_shortlab_full.py
  test_shortlab_packaging.py
  test_shortlab_release_workflow.py
```

UI 测试沿用 `desktop/ui` 的 Node test runner；根 E2E 与 backend pytest 是不同测试集，分别执行。

## 32.3 必测场景

| 测试类别 | 最低覆盖 |

| --- | --- |

| Determinism | 相同输入/配置产生完全相同 score/status |

| Missing data | null 不变 0；provider 失败不生成假数据 |

| Identity | 多 Coin 同 symbol 时必须 unresolved；manual override 生效 |

| Funding | 变量结算间隔仍按实际 events 求和 |

| VETO precedence | BLOCK > PAUSE > score status；WARN 不覆盖状态 |

| Data Quality | stale/grace/unavailable 的权重正确 |

| Point-in-time | grader 不读 t0 之后的 feature 生成 t0 score |

| Historical Entry | `build_symbol(end_ms)` 的现时 OI/ratio/funding 不得进入历史重算 |

| Unit normalization | 1000 合约价格归一，quote volume/OI 不乘除倍率；现货 symbol 由身份映射给出 |

| Candidate/execution status | 低 LTSS + PAUSE 仍显示 EXCLUDED；MEDIUM identity 不 READY；BLOCK 优先 |

| Provider applicability | 无现货 NOT_APPLICABLE 与断网 UNAVAILABLE 明确区分 |

| Delisted symbols | 退出 live universe 后仍能查询历史合同状态和 forward outcome |

| Outcome censoring | PENDING/COMPLETE/CENSORED/UNAVAILABLE、时间和成本版本正确 |

| API failure isolation | CoinGecko 500 时 /api/scan 仍正常 |

| DB migration | 空库、新版本、重复启动均正确 |

| Packaging smoke | 打包版启动、DB、Short-Lab 页面可用 |



## 32.4 测试数据

离线 CI 必须使用固定 fixture，不调用真实外网。live 测试继续使用仓库既有 marker/开关，不得让普通 pytest 因网络/地区限制失败。

# 33. 分阶段实施计划

## Phase 0 - 基线冻结与回归保护

- 输出 baseline.md：HEAD、版本、测试结果、关键目录与入口。

- 确认所有现有测试基线。

- 不改功能；继续使用当前分支，除非实施方获明确指示另建分支。列出 short-lab 对外更名清单与旧标识兼容测试。

- 在 `baseline.md` 记录 USDⓈ-M funding 分页的 `startTime/endTime/limit`、升序与 500/5min/IP 共享限制，给出 500 币按 80 次/5min 的回填时间预算；用固定 fixture 验证 K 线 quote volume 字段可从原始响应保留。两项均为离线/文档化验证，不把 live 网络测试放入常规 CI。



验收：基线报告可复现，原测试结果已归档，已有未提交文件已记录且未被覆盖；funding 分页/预算与 quote volume 适配验证各有固定 fixture 和结果。

## Phase 1 - Short-Lab 基础设施与数据

- 新增 shortlab package、config、runtime、repository、DuckDB migration。

- 实现 AssetIdentity/Resolver/Manual Override。

- 实现 CoinGecko Provider 与缓存。

- 对现有 funding 客户端增加有界分页回填，再增量归档 funding events；记录窗口覆盖与缺口。

- 实现 Data Quality LITE。



验收：不做 UI 也可通过 service/unit test 得到稳定 fundamentals/funding/identity；任何 provider 失败不影响现有 API。

## Phase 2 - LTSS-LITE / Entry / VETO / API

- Lifecycle、Carry、Valuation、Tradeability features。

- MEME_LITE / GENERAL_LITE / LOW_FLOAT_VC_LITE。

- Entry Score 复用 existing Dive outputs。

- VETO/PAUSE/WARN 与状态机。

- 完成 /api/short/candidates、symbol detail、providers、refresh。

- 扩展 `data/spot.py` 的日线成交量窗口、`data/universe.py` 的合约生命周期字段，并建立历史已见 symbol 记录。



验收：fixture 可生成完整 candidate JSON，分数/状态可解释、可重复；同样输入 100 次结果一致。

## Phase 3 - React Short Lab UI

- 新增导航、Scanner、Detail、过滤/排序。

- 显示 LITE/FULL、stale/unavailable、veto/warn。

- 不修改原有页面语义，不使用 mock 自动兜底。

- 重建 ui/dist。



验收：浏览器/打包版均可用，现有所有页面无回归。

## Phase 4 - Forward Evidence

- 校验 Phase 1–2 已保存的 point-in-time feature/score snapshots；补充必要来源引用，不得等到本阶段才开始保存。

- 实现 7D/30D/90D grader。

- 计算 price short return、funding carry、MAE/MFE。

- 记录 outcome 状态、缺失原因、删失合约和成本/公式版本；报告各状态数量，避免存活者偏差。

- 复用现有统计工具做 bucket/profile 报告。



验收：人工构造历史 fixture 能严格证明没有 future leakage。

## Phase 5 - Unlock + Social 数据接入

- 接入 UnlockProvider/Tokenomist 实现（或等价 provider）。

- 接入 SocialProvider/LunarCrush 实现（或等价 provider）。

- 实现 Narrative/Unlock 特征并完成离线验证；此阶段仍仅对外提供 LITE，FULL 待 Phase 6 的 Catalyst 覆盖和 DQ 校验通过后启用。

- Provider 未配置时仍可 LITE 正常运行。



## Phase 6 - Catalyst + FULL 启用 + 校准

- 结构化 CatalystEvent、重大事件 PAUSE；完成 FULL DQ 及五模块评分验证后启用 FULL profiles。

- 根据积累的 Evidence 校准阈值与权重。

- Suggest weights 保持 report-only；任何新权重进入新 score_version。



# 34. 第三方开发提交拆分建议

| PR/Commit 组 | 范围 | 禁止混入 |

| --- | --- | --- |

| A Baseline/Scaffold | shortlab package、config、DB、tests | UI、评分逻辑 |

| B Identity/Fundamentals | resolver、CoinGecko、fixtures | Entry/UI |

| C Features/Scoring | LTSS-LITE、DQ、VETO | 大规模 CSS 重构 |

| D API/Scheduler | router、jobs、cache | 修改旧 API schema |

| E UI | Short Lab views、data.js、dist | 后端评分重写 |

| F Evidence | forward grader、metrics | 生产权重自动修改 |



拆分的目的不是形式化，而是让 reviewer 可以独立验证“数据正确”“评分正确”“接口正确”“UI 正确”。

# 35. 验收标准（Acceptance Criteria）

## 35.1 架构验收

- 只有一个 FastAPI 服务与一个 127.0.0.1:8780。

- `uv run short-lab` 是推荐启动命令；`uv run dive-desktop` 作为兼容别名仍可用，均启动同一服务。

- 桌面 UI 标题、文档和打包产物使用 short-lab；已有 API 与 Python import 路径按 1.5 节兼容。

- 没有新增 Redis/Postgres/Celery/第二端口。

- Android 目录 V1 无功能修改。



## 35.2 兼容性验收

- 所有原 backend/root/UI 测试通过。

- 原 /api/scan、symbol、evidence、WS 行为无破坏性变化。

- 外部 provider 全部断网时，原 Dive 仍正常；Short-Lab 显示 unavailable/stale。



## 35.3 数据正确性验收

- Funding 7/30/90D 来自真实历史 events 求和。

- 首次安装的 funding 历史经过有界回填；窗口覆盖不足时返回 null 和缺口状态，不得产生完整窗口分数。

- CoinGecko 只能通过 resolved canonical id 绑定。

- 所有不可用值为 null，不以 0 伪装。

- Score snapshot 有 as_of/score_version/config_hash。

- 每条 score 引用不可变 feature snapshot；不同配置/profile 的同时间 score 不互相覆盖。



## 35.4 评分验收

- 相同 fixture + config 的 LTSS/Entry/Status 完全一致。

- LTSS shortlist 中的资产即使未进入现有 `/api/scan` 排名，也能按币得到 Entry 或明确的 null；状态机所有输入组合都有结果。

- BLOCK/PAUSE/WARN precedence 有测试。

- LITE/FULL 权重明确，不发生 runtime 动态偷偷重分权重。

- Score 解释至少列出主要正/负贡献。



## 35.5 UI 验收

- Short Lab Scanner/Detail 与现有主题一致。

- READY · LITE 与 READY FULL 可区分。

- STALE/UNAVAILABLE/VETO 明确显示。

- provider 失败不会出现 fabricated 数字。

- universe 启动失败不会遮蔽 Short Lab 自己的错误/陈旧数据页面。



## 35.6 Evidence 验收

- 7D/30D/90D forward grader 可重复。

- MAE/MFE 正确处理 short 方向。

- 历史 score 不使用未来 fundamentals/social/unlock。



# 36. Definition of Done（最终交付物）

| 交付物 | 必须包含 |

| --- | --- |

| 源码 | Backend + UI；Android V1 不改 |

| 配置 | shortlab 默认配置、asset override 示例，不含 secret |

| DB | schema/migration + 运行时创建逻辑 |

| 测试 | 新增 tests + 原回归测试结果 |

| API 文档 | /api/short/* 参数、响应、错误码 |

| 运行文档 | 开发态、打包态、Provider key 配置 |

| Evidence 文档 | score_version、forward grader、cost assumption 说明 |

| 变更日志 | 新增能力、兼容性、已知限制 |

| 验收报告 | 逐项对应第 35 节，附命令/截图/测试结果 |



> **DoD：** 代码“能跑”不等于完成。只有第 35 节验收项全部通过、原功能无回归、打包版验证通过，才可标记完成。


# 37. 风险清单与应对

| 风险 | 影响 | 缓解 |

| --- | --- | --- |

| Symbol 映射错误 | 把另一资产的 MC/FDV/Unlock 加到目标币，属于严重数据污染 | 强制 AssetIdentity；多候选不猜；manual override |

| Binance 地区限制/429 | 扫描失败或数据不全 | 沿用现有 backoff；stale cache；Short-Lab 与原 scan 隔离 |

| CoinGecko/provider 限流 | fundamentals 暂缺 | TTL cache、批量、provider limiter、DQ 降级 |

| DuckDB 打包/写路径 | 打包版无法持久化 | runtime path resolver + packaged smoke test |

| 评分过拟合 | 历史好看、实盘不稳 | 版本化、forward-only evidence、IC/bucket/stability、report-only weight suggestion |

| Future leakage | 产生虚假回测优势 | point-in-time snapshot + grader 时间边界测试 |

| Short squeeze | 长期方向对但中途爆仓 | 独立 Entry/VETO/MAE，不把 LTSS 当开仓信号 |

| 外部数据成本/许可 | Full 功能不可持续 | Provider interface、LITE 可独立运行、NullProvider |

| UI 误导 | 用户把 N/A 当 0 或把 LITE 当完整 | 显式 tier、DQ、stale/unavailable、risk code |



# 38. 第三方执行约束清单（必须逐条签收）

| # | 约束 |

| --- | --- |

| 1 | 不得新增独立后端服务、独立端口、Redis/Postgres/Celery。 |

| 2 | 必须在现有 desktop/backend FastAPI 进程内实现。 |

| 3 | 不得修改 parity-locked 60 指标公式以迎合 Short-Lab。 |

| 4 | 不得把 FDV/Unlock/Social 混进 Existing Consensus。 |

| 5 | 不得重复实现现有 Binance funding/OI/ratios/klines 数据客户端。 |

| 6 | 任何 external provider 失败不得导致原 /api/scan 失败。 |

| 7 | 不得 fabricated 数据；unavailable 必须是 null + 状态。 |

| 8 | Asset identity 不确定时不得自动猜测。 |

| 9 | 所有评分参数必须配置化并版本化。 |

| 10 | 必须保存 point-in-time 快照，禁止 retrospective current-data backfill 伪回测。 |

| 11 | V1 不修改 Android，不新增 Kotlin Short-Lab。 |

| 12 | V1 不接交易 API、不自动下单。 |

| 13 | UI 不得直接调用第三方 provider。 |

| 14 | API Key 不得进入 repo、日志、API 响应、前端 storage。 |

| 15 | 所有新增功能必须能通过 `uv run short-lab` 使用，旧 `uv run dive-desktop` 保留兼容。 |

| 16 | 打包版必须验证，不只验证源码运行。 |



# 39. 推荐最终用户流程

```text
1. 启动：`uv run short-lab`（旧 `uv run dive-desktop` 兼容）
2. 打开 Depth Terminal -> SHORT LAB
3. 默认看到 LTSS 排名和状态
4. 先筛 READY/CANDIDATE，而不是只按 LTSS 最大值追空
5. 点击资产：检查 Carry / Structure / Existing Dive / Risks / Data Sources
6. PAUSED/BLOCKED 不作为当前入场候选
7. 对 READY 仍使用仓位与风险管理；Short-Lab 不是自动交易系统
8. Evidence 页面持续观察 7D/30D/90D 的 forward 结果
9. 只有 Evidence 支持后，再调整生产权重并提升 score_version
```

# 40. 推荐实施后的发布定位

Desktop 与 Android 使用独立发布标识。当前 `.github/workflows/release.yml` 的 `v*` tag 只触发 Android release；Desktop 打包是手动任务，且固定引用 `dist/dive-desktop` 与 `dive-desktop-windows-x64.zip`。本次采用 `short-lab-v*` 作为 Desktop tag，增加独立 Desktop package/release job；保留 `v*` 仅供 Android。Workflow 的 `on.push.tags` 同时列 `v*` 与 `short-lab-v*`；Android job 的 `if` 为 `github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')`，Desktop job 的 `if` 为 `(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/short-lab-v')) || (github.event_name == 'workflow_dispatch' && inputs.package_desktop == true)`。静态/工作流测试必须证明 `short-lab-v1` 不启动 Android、`v0.4.0` 不启动 Desktop，手动勾选只启动 Desktop。spec 更名、目录、压缩包、上传 artifact、文档和安装 smoke 必须同批修改。发布说明标注 Desktop-only、research、非自动交易和 provider 依赖；Android 版本不随 Desktop tag 增长。

# 附录 A：模块责任矩阵

| 模块 | 负责 | 不负责 |

| --- | --- | --- |

| Existing engine | 技术指标、consensus、risk/regime | FDV/Unlock/Social/LTSS |

| Existing scan | 多时框扫描、micro/MTF/whale、evidence 基础 | 长期基本面 provider |

| ShortLab Identity | 跨源资产身份 | 行情计算 |

| ShortLab Providers | 外部基本面/供给/社交/事件 | 最终状态决策 |

| ShortLab Features | 把原始数据转为 point-in-time 特征 | 直接操作 UI |

| ShortLab Scoring | LTSS/Entry/Profile/DQ | 发网络请求 |

| ShortLab Risk | BLOCK/PAUSE/WARN | 修改 Existing Consensus |

| ShortLab Repository | 历史/快照/结果 | 业务评分 |

| ShortLab API | 参数校验/序列化/调用 Service | 自行抓第三方数据 |

| React Short Lab | 展示/交互 | 重新计算 score/status |



# 附录 B：PR Review 核对表

- [ ] 是否出现第二个服务/端口/数据库 daemon？如是，拒绝。

- [ ] 是否改了现有 60 指标/consensus 数学？如非必要，拒绝。

- [ ] 是否复用了 data/http 的错误/退避模式？

- [ ] 是否存在 symbol 模糊自动匹配？如是，拒绝。

- [ ] 是否用 0 代表 unavailable？如是，拒绝。

- [ ] 是否存在 current funding 直接年化作为历史收益？如是，拒绝。

- [ ] 是否所有 score 有 version/config_hash？

- [ ] 是否 point-in-time snapshot 后再 forward grade？

- [ ] 是否 external provider 断网时原 Dive 仍全功能可用？

- [ ] 是否 LITE/FULL 在 API 与 UI 都显式？

- [ ] 是否 README/API/配置/测试/打包同时更新？

- [ ] 是否所有新增 secrets 都只走环境变量/现有 secrets 机制？



# 附录 C：第三方开始开发前应输出的 1 页确认单

```text
Baseline commit: __________________________
Branch:          __________________________
Existing tests:  PASS / FAIL (attach report)
Backend entry:   __________________________
UI entry:        __________________________
Runtime path:    __________________________

Confirmed constraints:
[ ] one FastAPI only
[ ] 127.0.0.1:8780 unchanged
[ ] Android V1 untouched
[ ] no auto trading
[ ] existing consensus semantics unchanged
[ ] unavailable != 0
[ ] no fuzzy asset mapping
[ ] point-in-time snapshots required
[ ] packaged build is acceptance scope

Implementer: __________________ Date: __________
Reviewer:    __________________ Date: __________
```

—— 文档结束 ——
