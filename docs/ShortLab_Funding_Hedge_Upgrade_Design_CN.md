# short-lab Funding Capture / Hedge Advisor 升级优化详细设计方案

**文档版本**：1.0\
**日期**：2026-10-02\
**适用基线**：当前 short-lab 项目；既有设计与实施计划作为接口参考，实际完成范围以本节代码核验与实施前基线测试为准\
**本次性质**：增量功能扩展；不重做 Tasks 0–18；不改变既有 LTSS / Entry / DQ / VETO 数学和语义\
**目标读者**：第三方开发、Reviewer、QA、Agentic coding worker\
**交付目标**：第三方可直接按本文拆任务开发、测试、验收，最大程度减少架构、算法、数据单位和风险语义上的误读。

---

# 0. 执行规则与兼容优先级

本文不是原设计的替代，而是其后续升级规范。发生冲突时按以下优先级处理：

1. 当前仓库已实现代码、现有自动化测试和公开 API 的兼容行为；
2. 本文标记为 `MUST / MUST NOT` 的新增要求；
3. 已完成的 `ShortLab_Detailed_Design_CN.md`；
4. 已完成的 `ShortLab_Implementation_Plan_CN.md`；
5. 本文中的 `SHOULD / MAY` 建议。

实施前必须记录并保存基线：

```bash
git rev-parse --show-toplevel
git rev-parse HEAD
git branch --show-current
git status --short
(cd desktop/backend && uv run pytest -q)
uv run --project desktop/backend pytest tests/ -q
(cd desktop/ui && npm test && npm run build)
```

**新增工作沿用 Phase 7 / Task 19 起的编号。编号延续不代表 Tasks 0–18 已全部验收；先记录缺失接线与新增功能依赖，仅补齐本功能必要依赖。禁止以编号作为已有能力可用的证据。**

---

# 1. 当前系统与本次升级定位

当前 short-lab 已经具备：

- 单 FastAPI + React 桌面架构；
- Binance USDT-M universe、K 线、Funding、OI、L/S、Taker、Basis、Orderbook、Spot；
- AssetIdentity、合约倍率、CoinGecko、DuckDB、Scheduler；
- LTSS、Entry Score、DQ、VETO/PAUSE/WARN；
- Point-in-time snapshot、7D/30D/90D Evidence 计算模块；默认运行时尚未完整接入长期 Evidence 自动任务与 summary provider；
- `/api/short/*` 与 Short Lab UI。

本次新增第三套独立研究能力：

- **FCS / Funding Capture Score**：资产是否适合通过“永续空 + 现货多”捕获正资金费率；
- **Hedge Planner**：计算绝对/相对对冲数量、风险、成本、Break-even 和人工执行指引；
- **Hedge Monitor**：用户手工执行后，持续监控 Funding、Basis、Hedge Ratio、强平距离、退出流动性；
- **Hedge Alerts**：给出调整、暂停、成对退出和孤腿风险提醒。

最终形成三个策略入口：

```text
1. Directional Short
   LTSS + Entry
   目标：价格下跌 + Funding

2. Funding Capture
   Perp Short + Spot Long
   目标：尽量降低价格 Delta，主要赚 Funding

3. Hedged Short
   Perp Short + Partial Spot Long
   目标：赚 Funding，同时保留部分净空头敞口
```

---

## 1.1 方案判断与推荐交付方式

“永续空 + 同资产现货多”捕获正 Funding 的方向成立；相对对冲还保留方向性净空头风险。推荐先交付 Binance Spot 的数量规划、手工账本和监控，再接入 Alpha，最后按具体链接入真实只读报价 provider。三类 Venue 使用相同业务接口，但按能力单独验收，不因缺少链上 provider 阻塞已验证的 Binance Spot 功能。

一比一是**持有的同一底层资产数量匹配**，不是两边投入相同保证金，也不是两边下单金额始终相同。现货盈利不会自动成为隔离合约保证金；一比一资产组合仍可能先在合约腿发生强平。正 Funding 只能作为当前观察值和入场门槛，不能保证整个持仓期间持续为正。相对对冲比例来自用户风险预算，系统不输出“几乎不可能翻倍”的概率结论。

## 1.2 已核验代码与新增缺口

| 能力 | 当前代码 | 本方案要求 |
|---|---|---|
| 当前 Funding/mark | `data/funding.py:premium_index()`，含交易所时间、当前期费率与下次时间 | 扩展 Funding interval/cap/floor 读取；结算前复核 |
| 历史 Funding | `funding_history_range()` 与 coverage，已有 `sl_funding_event` | 保持原消费方语义，新增对冲统计与现金流覆盖元数据 |
| 现货历史 | `data/spot.py:spot_history()`，身份、市场存在性、日线量与归一化价格 | 同文件新增交易规则、原始深度及目标数量双向 VWAP；历史指标不等于执行报价 |
| 合约元数据 | `data/universe.py:contract_metadata_all()` | 保留交易规则及价格/数量 filters 供 Planner 使用 |
| 盘口 | `data/orderbook.py` 当前以聚合指标为主 | 增加原始买卖档位与时间合同，不能从 1% 总深度反推目标量 VWAP |
| 存储/任务 | `shortlab/repository.py`、`scheduler.py`、`runtime.py` | 同进程增量扩展；当前默认周期任务主要为 score_refresh |
| 现货 Alpha/链上/持仓账本 | 未发现本方案所需现成业务模块 | 明确新增，不将建议接口描述为当前可用功能 |

本文是升级设计，不是实网联通或收益验证报告。现有功能回归由实施前测试建立基线；下面新增字段、表和任务均属于待实现合同。

---

# 2. 强制架构边界

## 2.1 MUST：继续单体架构

本次扩展继续运行在：

```text
desktop/backend/src/diveintocrypto_desktop/
```

同一个 FastAPI 进程中。禁止新增：

- 第二个 FastAPI / uvicorn；
- Celery、Redis、PostgreSQL；
- 独立 Hedge Service；
- 第二端口；
- 独立交易守护进程。

启动方式仍为：

```bash
uv run short-lab
```

`uv run dive-desktop` 兼容入口继续保留。

## 2.2 MUST NOT：项目不执行交易

禁止实现：

```text
Binance private order API
Binance account/position API
Wallet private key / seed phrase
Wallet signature
ERC20 approve
DEX swap transaction broadcast
自动开仓 / 平仓 / 补保证金
自动止盈止损
```

允许实现：

```text
公共行情读取
只读报价和深度
现货市场发现
链上只读 quote
数量/价格/成本计算
人工下单指引
用户手工录入真实成交
风险监控与提醒
```

## 2.3 MUST：禁止“无风险/无损/保证收益”文案

1:1 对冲只能称为：

- Delta-Neutral；
- 近似价格中性；
- 完全数量对冲。

原因：仍存在 Basis、强平路径、Funding 反转、两腿成交差、手续费、滑点、Gas、退出流动性、退市和 provider 风险。

---

# 3. Canonical Quantity 与 Hedge Ratio

所有 Hedge 计算必须以 canonical underlying quantity 为核心。

已有 `AssetIdentity.contract_multiplier` 继续作为倍率安全边界。倍率只能来自交易所明确元数据或人工验证 override，不能根据 `1000PEPE` 名称猜测。

```text
canonical_futures_qty
  = futures_contract_qty × contract_multiplier
```

例：

```text
1000PEPEUSDT
contract_multiplier = 1000
futures qty = 100

canonical_futures_qty = 100,000 PEPE
```

以下字段若本来就是 USD/USDT 名义金额，禁止乘除 multiplier：

```text
OI USD
quote volume
market cap
notional USD
```

Hedge Ratio：

```text
h = spot_canonical_qty / futures_canonical_qty
```

残余净空头：

```text
residual_short_ratio = 1 - h
residual_short_qty
  = futures_canonical_qty - spot_canonical_qty
```

API/UI 必须同时显示：

```text
Target Hedge Ratio
Actual Hedge Ratio
Residual Short %
Residual Short Qty
Residual Short Notional
```

---

# 4. 两种对冲模式

## 4.1 ABSOLUTE

```text
h = 1.0
Perp Short Canonical Qty = Q
Spot Long Canonical Qty  = Q
```

目标：尽可能抵消资产价格方向风险，使主要收益来源转为 Funding 与 Basis 变化。

价格腿：

```text
FuturesPnL = Q × (Pf_entry - Pf_now)
SpotPnL    = Q × (Ps_now - Ps_entry)

PricePnL =
  Q × [(Pf_entry - Ps_entry)
     - (Pf_now - Ps_now)]
```

所以 ABSOLUTE 并非严格 PnL=0，残余来自 Basis、成交时间差、rounding、fee/slippage 和 liquidation path。

## 4.2 RELATIVE

```text
0 < h < 1
```

例：

```text
Futures Short = $10,000 equivalent
Target Hedge Ratio = 60%
Spot Long ≈ $6,000 equivalent
Residual net short ≈ $4,000 equivalent
```

近似方向 PnL：

```text
DirectionalPnL
≈ -(1 - h) × FuturesNotional × PriceReturn
```

Funding按每次结算时实际持有空腿的mark名义金额产生，与现货对冲比例无关；现货投入额应按quantity×spot price计算，不能直接用h×期货金额替代。

---

# 5. Relative Hedge 风险预算反推

用户不应只能凭感觉填 `30% / 50% / 70%`。

输入：

```text
Futures Notional N
Stress Up Move S
Max Allowed Directional Loss L
```

最低对冲比例：

```text
h_min = 1 - L / (N × S)
h_target = clamp(h_min, 0, 1)
# 若结果为 0 或 1，分别提示无对冲方向性方案或 ABSOLUTE，须用户按对应mode重新提交，不得伪称 RELATIVE
```

例：

```text
N = $10,000
S = +100% = 1.0
L = $2,000

h_min = 80%
```

UI 支持两种互斥输入：

1. 直接指定 Hedge Ratio；
2. 输入 Stress Up Move + Max Loss，由系统反推。

两种模式同时提交返回 `422`。`S` 使用小数（100%=1.0），要求 `S>0`、`L>=0`、`N>0`。该反推只覆盖共同价格上涨的方向损失，不包括 Basis、成本、强平或退出失败损失；总损失预算另保留这些项的缓冲。

---

# 6. Funding Capture Entry Gate

Funding 数据必须复用既有：

```text
data/funding.py
sl_funding_event
7D / 30D / 90D coverage
```

必须区分：

```text
lastSettledFundingRate
currentFundingRate / predictedFunding
nextFundingTime
funding7d
funding30d
funding90d
positiveFundingRatio30d
positiveFundingRatio90d
fundingStd30d
longestNegativeStreak30d
```

`predicted/current funding` 不是已锁定收益。

默认配置：

```yaml
funding_capture:
  entry_gate:
    require_current_positive: true
    require_last_settled_positive: true
    min_funding_7d: 0.0
    min_funding_30d: 0.0
    min_positive_ratio_30d: 0.75
    min_positive_ratio_90d: 0.60
    min_30d_coverage: 0.90
    min_90d_coverage: 0.80
```

规则：

```text
current <= 0
→ NOT_READY_FUNDING_NOT_POSITIVE

last settled <= 0
→ NOT_READY_LAST_SETTLED_NOT_POSITIVE

30D cumulative <= 0
→ NOT_READY_CARRY_30D_NON_POSITIVE

30D positive ratio < 75%
→ NOT_READY_FUNDING_PERSISTENCE

coverage 不足
→ NOT_READY_FUNDING_COVERAGE
```

资产有可靠上市时间且未满 90D 时，90D 可为 `NOT_APPLICABLE/INSUFFICIENT_ASSET_AGE`，但 30D 仍必须满足完整 gate。未知上市日期或抓取不足不得用资产年龄不足掩盖缺失。7D累计必须严格大于配置门槛；90D适用时，其 coverage 与正费率比例同样必须通过，否则 NOT_READY。非正当前费率不得新建 READY 计划。

---

## 6.1 Funding 统计与结算口径

窗口固定为 `(as_of_ms-D×86400000, as_of_ms]` 的已结算事件，按 `(symbol,fundingTime)` 去重、升序；当前期 rate 不插入历史。必须复用历史抓取完整性判定：`complete=false` 时，窗口累计/比例/标准差不得伪装成可用值，即便 coverage_fraction 达到门槛。既有 coverage 规则不变；本模块增加 interval 元数据和间隔异常提示，不假设每个币每天恒定结算三次。

`fundingDd=sum(rate)` 是固定名义规模的简单历史费率和，不是账户实际收益。正费率比例为 `count(rate>0)/event_count`，0不算正；负费率连续次数只统计连续 `<0`，0中断序列。FCS 的 `fundingStd30d` 使用结算事件的 8h 等效费率 `rate×8/interval_hours` 的总体标准差（ddof=0）；interval来自当时保存的元数据或可验证相邻事件间隔，无法确认则该指标缺失并使 FCS=null。8h 等效只用于稳定性比较，不改变现金流与累计费率。

滚动7D APR：每个完整 UTC 日结束时计算此前完整7D窗口，窗口结束不得晚于 as_of；30D/90D 内只保留完整有效窗口，P25采用排序后线性插值 `index=(n-1)×0.25`，要求分别至少20/60个窗口。不足时保守 APR=null、NOT_READY，不外推缺口。APR 是**相对于期货名义金额的历史简单年化**。另输出 `capital_at_risk=spot_buy_cash+futures_margin+cost_reserve` 和按该投入计算的情景收益率，避免把高杠杆保证金收益率误当低风险资产收益率。

必须新增每合约 Funding interval/cap/floor 与 nextFundingTime 快照，并共享既有历史请求限额；当前 interval 不能用于重写过去全部事件。结算前10分钟开始每30秒复核当前费率与时间，翻负立即提醒；此复核仅在后端运行、网络正常时有效。临近结算的开平仓不得认定必然拿到当期 Funding，需标 `SETTLEMENT_BOUNDARY_UNCERTAIN`。

---

# 7. Funding 退出规则

默认：

```yaml
funding_capture:
  exit:
    negative_settlement_streak: 2
    weakening_7d_vs_30d_ratio: 0.30
```

```text
current funding <= 0
→ WARN_FUNDING_TURNED_NON_POSITIVE

连续 2 次 settled funding < 0
→ EXIT_FUNDING_NEGATIVE_STREAK

7D average daily funding <
30D average daily funding × 30%
→ WARN_FUNDING_WEAKENING
```

若保守预期 Funding 已不足以覆盖退出成本：

```text
EXIT_FUNDING_EDGE_GONE
```

持有判断使用从现在起的增量收益和退出风险，不因未收回入场成本强迫继续持有。`planned_hold_days` 已到期或用户剩余计划时长内保守 Funding 小于预计退出成本时给出退出建议；时长缺失仅提示评估不足，不自行推断用户持有期限。


# 8. Funding Capture Score（FCS）

## 8.1 定位与版本

`FCS` 与 `LTSS`、`Entry` 完全独立：

```text
LTSS  → 长期方向性空头价值
Entry → 当前方向性做空时机
FCS   → Funding Capture 适配度
```

禁止：

- 将 FCS 合并进 LTSS；
- 用 FCS 改 Existing Consensus；
- 用 Entry 作为 ABSOLUTE Funding Hedge 的硬门槛。

首版：

```text
fcs_version = "fcs_v1"
```

任何改变历史分数的数学改动必须升级版本。

## 8.2 Reference Notional

资产级 FCS 用固定参考规模评估现货可执行性：

```yaml
funding_capture:
  reference_notional_usd: 10000
```

FCS 表示：

> 在约 $10,000 参考规模下，该资产是否适合 Funding Capture。

真实用户计划必须再计算 `Plan Safety Score`。

## 8.3 精确评分

总分 100：

| 模块 | 分值 |
|---|---:|
| Funding Yield | 25 |
| Funding Persistence | 25 |
| Funding Stability | 15 |
| Hedge Venue Quality | 15 |
| Basis Quality | 10 |
| Operational / Contract Safety | 10 |

### Funding Yield：25

使用已结算 30D cumulative funding：

| 30D Funding | 分 |
|---:|---:|
| >= 4.0% | 25 |
| >= 2.5% | 20 |
| >= 1.5% | 15 |
| >= 0.75% | 8 |
| > 0 | 3 |
| <=0 / unavailable | 0 |

coverage 不足时，本模块 `unavailable`，整个 FCS=`null`；禁止把缺失权重重新分配。

### Funding Persistence：25

30D Positive Ratio：15 分：

| Ratio | 分 |
|---:|---:|
| >=90% | 15 |
| >=80% | 12 |
| >=75% | 9 |
| >=65% | 5 |
| <65% | 0 |

90D Positive Ratio：10 分：

| Ratio | 分 |
|---:|---:|
| >=90% | 10 |
| >=80% | 8 |
| >=70% | 5 |
| >=60% | 2 |
| <60% | 0 |

若资产年龄不足 90D，90D 为 N/A，不重分 10 分；保留0..100固定尺度，该子项贡献0但状态是 N/A，满分上限90；UI 显示 `FCS_PARTIAL_HISTORY` 和 available_max_score。所有适用项缺失均令 FCS=null，不能用0代替未知。

### Funding Stability：15

`fundingStd30d`：8 分：

| Std | 分 |
|---:|---:|
| <=0.0003 | 8 |
| <=0.0008 | 5 |
| <=0.0015 | 2 |
| >0.0015 | 0 |

`longestNegativeStreak30d`：7 分：

| 连续负结算次数 | 分 |
|---:|---:|
| 0 | 7 |
| 1 | 6 |
| 2 | 3 |
| 3 | 1 |
| >3 | 0 |

### Hedge Venue Quality：15

基于 `reference_notional_usd` 的最佳已验证 Venue。

双向可执行：7 分：

```text
buy >= reference 且 sell >= reference → 7
两边 >= reference×0.75 且未到reference → 4
两边 >= reference×0.50 且未到reference×0.75 → 2
否则 → 0
```

Roundtrip execution cost：5 分：

```text
<=0.30% → 5
<=0.60% → 3
<=1.00% → 1
>1.00% → 0
```

Exit feasibility：3 分：

```text
CONFIRMED → 3
PARTIAL   → 1
UNKNOWN/NO → 0
```

### Basis Quality：10

```text
0 <= b <= 0.005       → 10
0.005 < b <= 0.015    → 8
0.015 < b <= 0.030    → 5
b > 0.030            → 2
-0.005 <= b < 0      → 3
b < -0.005           → 0
```

这里只表示当前入场 Basis 质量，不代表 Basis 必然收敛。

### Operational / Contract Safety：10

```text
Futures status TRADING           2
No confirmed delisting           2
Identity VERIFIED/HIGH           2
Multiplier verified              2
Spot quote fresh                 1
Next funding time known          1
```

BLOCK 风险优先于 FCS。FCS 是确定性的规则评分，不是赚钱概率；高正 Basis 分数仅表示入场结构假设，极高正 Basis 仍必须进行扩大压力测试与强平预警。Venue Quality 基于现货原始双向报价，roundtrip成本包括该现货Venue买卖费用/Gas/滑点，不含期货腿；机会列表总体成本则包含两腿，字段分别命名避免混淆。

---

# 9. Spot Venue Resolver

## 9.1 支持三类 Venue

```text
BINANCE_SPOT
BINANCE_ALPHA
ONCHAIN_DEX
```

统一业务接口，不允许在 Planner/Monitor 中散落大量 venue-specific 分支。

建议目录：

```text
shortlab/hedge/venues/
├── base.py
├── binance_spot.py
├── binance_alpha.py
└── onchain.py
```

## 9.2 SpotVenueQuote DTO

本DTO的symbol为venue instrument ID，不是canonical身份。mid/VWAP统一返回每个canonical token的USD价格，同时quote JSON保留native价格/币种/汇率/qty输入。buy/sell executable qty是在最大price impact内可覆盖的canonical数量；仅ticker不能产生这两个字段。ProviderResult继续沿用现有DTO，不新增一套错误外壳。

```python
@dataclass(frozen=True)
class SpotVenueQuote:
    venue: str
    canonical_id: str
    symbol: str | None
    chain: str | None
    contract_address: str | None

    as_of_ms: int
    expires_at_ms: int | None

    reference_notional_usd: float

    mid_price: float | None
    buy_vwap: float | None
    sell_vwap: float | None

    buy_executable_qty: float | None
    sell_executable_qty: float | None

    buy_slippage_bps: float | None
    sell_slippage_bps: float | None

    estimated_fee_usd: float | None
    estimated_gas_usd: float | None

    entry_feasible: bool
    exit_feasible: bool
    exit_feasibility: str  # CONFIRMED | PARTIAL | UNKNOWN | NO
    quote_currency: str
    quote_to_usd: float | None
    source_timestamp_ms: int | None
    fetched_at_ms: int
    requested_canonical_qty: float
    trading_rules: dict
    capabilities: dict

    identity_confidence: str
    status: str
    reason_code: str | None
```

所有 quantity 都是 canonical token quantity。

## 9.3 Venue 排名

不能简单硬编码：

```text
Binance Spot > Alpha > Onchain
```

默认排序依据：

1. identity verified；
2. 可买；
3. 两种模式均必须可卖；
4. 能覆盖目标 quantity；
5. roundtrip cost；
6. sell-side exit depth；
7. quote freshness。

API 返回所有 Venue，同时标记 `bestVenue`。先筛出满足身份、双向全量、有效报价的Venue，再按roundtrip成本升序、退出容量降序、时间降序、venue ID升序稳定排序。部分对冲仍需要退出全部所持现货，不能放宽可卖Gate。卖出报价必须请求买入计划最终取得的同一数量；不能用两次独立10,000美元报价冒充同量回转。

---

# 10. Binance Spot

必须继续复用已有 `data/spot.py`，不得创建重复 Binance Spot client。

Hedge 需要：

- market existence；
- best bid/ask；
- depth；
- reference notional VWAP；
- buy/sell executable quantity；
- spot price；
- normalized basis。

只允许通过：

```text
AssetIdentity.binance_spot_symbol
```

访问。

禁止：

```text
futures_symbol.replace(...)
1000 前缀猜测
```

---

# 11. Binance Alpha

## 11.1 用途

用于：

> Binance Futures 有永续，但 Binance 主 Spot 没有对应市场，而 Binance Alpha 存在现货市场。

必须只使用 Binance Alpha **公共市场数据**，不使用交易/账户接口。

底层 adapter 建议：

```text
data/binance_alpha.py
```

业务包装：

```text
shortlab/hedge/venues/binance_alpha.py
```

## 11.2 至少获取

```text
Token list / identity metadata
Exchange info / trading rules
Current price
24h stats
Kline
Best bid/ask
Depth / full depth（官方接口可用时）
```

Binance 官方 Alpha Trading 已公开 token list、exchange info、Kline、ticker和Full Depth/盘口流，新增adapter采用这些公共端点并保存raw fixture。详见[Alpha公共行情文档](https://developers.binance.com/en/docs/catalog/advanced-trading-alpha-trading/api/rest-api/market-data)。文档存在不代表本地网络实测通过，实施需验证host、参数、限额与地区可用性。Alpha orderbook market与钱包内链上购买分开记录；tokenId与合约地址不能互相推导。

## 11.3 Alpha Identity

Venue identity 固定保存于新增 `sl_hedge_venue_mapping`，不修改旧身份表。字段：

```text
binance_alpha_symbol
binance_alpha_token_id
```

映射同时保存 canonical_id、venue、chain_id、contract_address、decimals、quote_asset、verification_source、verified_at_ms 和 mapping_version；钱包余额不可读取，执行权限和Gas余额由用户自行确认。

如果 Alpha 只有 symbol 且存在多候选：

```text
UNRESOLVED
```

不得作为 ABSOLUTE READY venue。

---

# 12. On-chain Spot

## 12.1 范围

On-chain 仅允许：

```text
read-only quote
liquidity inspection
buy/sell quote
gas/cost estimate
```

不得生成、签名、广播交易。

## 12.2 Provider 抽象

```python
class OnchainQuoteProvider(Protocol):
    async def quote_buy(...) -> ProviderResult[OnchainQuote]: ...
    async def quote_sell(...) -> ProviderResult[OnchainQuote]: ...
    async def health(...) -> ProviderResult[dict]: ...
```

可以有：

```text
EVM adapter
Solana adapter
其他链 adapter
```

业务层不绑定具体 aggregator。

## 12.3 ABSOLUTE 硬 Gate

只有同时满足：

```text
BUY quote available
SELL quote available
same verified contract
buy size executable
sell size executable
gas estimate available
price impact below maximum
quote fresh
```

两种模式都执行此Gate。双向 quote 只能验证当前路由报价，不保证未来成交；余额、allowance、交易权限、transfer-tax、sell限制或quote simulation未验证时标 PARTIAL，不能输出已确认可执行。Rebase、fee-on-transfer、冻结/黑名单或资产版本转换不清晰的Token不得进入数量对冲 READY。

才允许：

```text
HEDGE_VENUE_READY
```

否则：

```text
NOT_READY_ONCHAIN_EXIT_UNVERIFIED
```

原因是“可以买”不等于“之后可以卖”。

## 12.4 Contract 验证

必须使用：

```text
chain + contract_address
```

不能只靠 symbol/name。

没有 verified contract：

```text
ONCHAIN = UNAVAILABLE
```

---

# 13. Execution Cost Model

项目不读取用户 VIP 等级，因此手续费只能作为研究假设或用户配置。

```yaml
hedge:
  costs:
    futures_entry_fee_rate: 0.0005
    futures_exit_fee_rate: 0.0005
    spot_entry_fee_rate: 0.001
    spot_exit_fee_rate: 0.001
    alpha_entry_fee_rate: 0.001
    alpha_exit_fee_rate: 0.001
    onchain_extra_buffer_bps: 20
```

计算：

```text
EntryCost =
    FuturesEntryFee
  + SpotEntryFee
  + FuturesEntrySlippage
  + SpotEntrySlippage
  + EntryGas

ExitCost =
    FuturesExitFee
  + SpotExitFee
  + FuturesExitSlippage
  + SpotExitSlippage
  + ExitGas

RoundTripCost = EntryCost + ExitCost
```

输出：

```text
roundTripCostUsd
roundTripCostPctOfFuturesNotional
```

---

# 14. Break-even 与保守 Funding

```text
BreakEvenFundingPct =
  RoundTripCostUsd / FuturesNotionalUsd
```

```text
EstimatedBreakEvenDays =
  BreakEvenFundingPct / (ConservativeAPR / 365)
```

若保守 daily funding <=0：

```text
BreakEvenDays = null
NOT_READY_NO_POSITIVE_CONSERVATIVE_CARRY
```

禁止用 `current funding × 365` 作为主决策值。

必须输出：

```text
HistoricalSimpleAPR7D
HistoricalSimpleAPR30D
HistoricalSimpleAPR90D
ConservativeAPR
```

定义：

```text
APR_30D = funding30d × 365 / 30
APR_90D = funding90d × 365 / 90
```

有足够滚动窗口时：

```text
ConservativeAPR =
  max(0,
      min(APR_30D,
          APR_90D,
          P25Rolling7DAPR90D))
```

若 90D 因资产年龄不足不可用：

```text
ConservativeAPR =
  max(0,
      min(APR_30D,
          P25Rolling7DAPR30D))
```

必须返回：

```text
conservativeAprMethod
historyCoverage
```

---

# 15. Hedge Planner

## 15.1 输入

```python
HedgeSimulationRequest {
    symbol

    mode: ABSOLUTE | RELATIVE

    futures_notional_usd

    # RELATIVE 二选一
    hedge_ratio
    # 或
    stress_up_pct
    max_directional_loss_usd

    preferred_spot_venue: AUTO | BINANCE_SPOT | BINANCE_ALPHA | ONCHAIN_DEX

    futures_leverage
    margin_mode
    margin_usd

    liquidation_price | null
    liquidation_price_source: USER_EXCHANGE | ESTIMATED | NONE
    liquidation_price_updated_at_ms | null
    stop_policy: ALERT_ONLY | USER_PLATFORM_ORDERS
    stop_trigger_price | null
    stop_trigger_basis: MARK_PRICE | CONTRACT_PRICE | null
    maximum_pair_loss_usd | null

    planned_hold_days | null
    fee_overrides | null
}
```

## 15.2 输出

```python
HedgeSimulation {
    symbol
    canonical_id
    mode

    futures_symbol
    futures_price
    futures_notional_usd
    futures_contract_qty
    canonical_futures_qty

    target_hedge_ratio

    spot_venue
    spot_symbol
    spot_chain
    spot_contract
    spot_price
    target_spot_qty
    spot_notional_usd

    residual_short_ratio
    residual_short_qty
    residual_short_notional_usd

    fcs
    plan_safety_score

    funding_metrics
    basis_metrics
    cost_metrics
    break_even

    stress_scenarios[]
    order_guidance[]
    monitoring_capability
    liquidation_check_status
    risks[]
    warnings[]
    readiness
}
```

---

# 16. Quantity Rounding

计算顺序必须固定：

1. 根据 futures notional 求 futures quantity；
2. 按 futures step size 向下取合法值；
3. 转 canonical futures qty；
4. 根据 target hedge ratio 求 raw spot qty；
5. 按 spot venue step/decimals 向下取合法值；
6. 用最终计划 quantity 重新算 hedge ratio；
7. 检查最小/最大数量、MIN_NOTIONAL/NOTIONAL、PRICE_FILTER及market lot规则；用Decimal计算quantity/price并以十进制字符串传输，下单参数不从DOUBLE反算；
8. rounding drift 超阈值时告警；超过5个百分点不得 READY。ABSOLUTE不足精度不能仍称严格1:1。

默认：

```yaml
hedge:
  ratio:
    drift_warn_pct: 0.02
    drift_critical_pct: 0.05
```

例：

```text
target = 100%
actual = 97%
drift = 3%
→ WARN
```

---

# 17. Stress Simulator

至少输出：

```text
-25%
-50%
+25%
+50%
+100%
```

RELATIVE 额外输出：

```text
+200%
```

每个场景：

```text
spotPnl
futuresPnl
directionalPnl
residualExposurePnl
```

Stress同时输出共同价格冲击、Basis扩大（±1%/±3%）、退出滑点扩大以及期货先平/现货先平的孤腿路径。以当前mark和用户强平价判断路径是否穿过强平：穿过则线性终点PnL标 `INVALID_AFTER_LIQUIDATION`，不得展示为可实现对冲收益。无法验证强平价时输出 `LIQ_PATH_UNKNOWN`。

Stress PnL 不包含未来未知 Funding。可以另列历史 carry 假设，但不能混成“保证结果”。


# 18. Plan Safety Score

FCS 是资产级；`Plan Safety Score` 是具体计划级，必须使用真实目标 notional、目标 hedge ratio 和用户输入的保证金参数。

总分 100：

| 模块 | 分值 |
|---|---:|
| Hedge Ratio / Drift | 20 |
| Liquidation Buffer | 25 |
| Exit Liquidity | 20 |
| Cost / Break-even | 15 |
| Funding Edge | 10 |
| Data Freshness | 10 |

## 18.1 Hedge Ratio / Drift：20

未激活计划：

```text
rounding 后 drift <=1% → 20
<=2% → 15
<=5% → 5
>5% → 0
```

激活后使用 actual hedge ratio。

## 18.2 Liquidation Buffer：25

若没有用户提供交易所真实 liquidation price：

- ESTIMATED有可复算模型时本模块最高10/25；NONE则0分并标UNKNOWN；
- 标记 `LIQ_PRICE_NOT_VERIFIED`。

Short：

```text
liqDistance =
  (liquidationPrice - markPrice) / markPrice
```

| Distance | 分 |
|---:|---:|
| >=50% | 25 |
| >=30% | 20 |
| >=20% | 15 |
| >=10% | 8 |
| >=5% | 2 |
| <5% | 0 |

## 18.3 Exit Liquidity：20

同时检查 Futures 和 Spot：

```text
两腿退出 size 均可在最大 price impact 内完成 → 20
最差一腿仅覆盖 75%~100% → 10
最差一腿 <75% → 0
```

## 18.4 Cost / Break-even：15

```text
Break-even <=2d  → 15
<=5d             → 12
<=10d            → 8
<=20d            → 3
>20d / unavailable → 0
```

## 18.5 Funding Edge：10

```text
全部 Funding Entry Gate 通过 → 10
任一硬 Gate 失败 → 0
# 两种取值，不设未定义的“最低门槛”中间档
```

## 18.6 Data Freshness：10

```text
Futures mark fresh            2
Spot quote fresh              2
Spot exit depth/quote fresh   2
Funding current fresh         2
Contract state fresh          2
```

任一关键字段超过 grace：

```text
execution readiness = NOT_READY
```

---

# 19. Hedge Plan 与 Actual Fill

## 19.1 Plan 和实际成交必须分开

不得把“计划价/计划数量”当成用户真实仓位。

状态：

```text
DRAFT
READY
PARTIALLY_FILLED
ACTIVE
CLOSING
CLOSED
INVALID
```

## 19.2 用户手工录入实际成交

录入使用不可变事件，而非覆盖平均价：每次事件记录 `event_id/client_event_id/plan_id/leg_type/event_type(OPEN/CLOSE/LIQUIDATION/ADJUSTMENT/FUNDING_RECEIPT)/executed_at_ms/native_qty/canonical_qty/native_price/fee_amount/fee_currency/fee_usd/gas_usd/source(USER_ENTERED)/recorded_at_ms`。client_event_id幂等，plan_version乐观锁冲突返回409；更正以撤销引用和替代事件完成。相同时间的两腿成交按event_id确定聚合顺序，不使用当前余额回填历史。FUNDING_RECEIPT记录用户手工确认的结算时间、金额、币种及引用公共事件ID，不改变quantity；撤销/替代使用ADJUSTMENT并明确被引用事件。

下面是事件聚合后的两腿投影：

Futures leg：

```text
side = SHORT
qty
canonical_qty
avg_entry_price
actual_fee_usd optional
leverage
margin_mode
margin_usd
exchange_liquidation_price optional
opened_at_ms
closed_qty
avg_exit_price
closed_at_ms
```

Spot leg：

```text
side = LONG
venue
qty
canonical_qty
avg_entry_price
actual_fee_usd optional
gas_usd optional
opened_at_ms
closed_qty
avg_exit_price
closed_at_ms
```

## 19.3 ACTIVE Gate

只有用户录入两条腿真实成交、剩余quantity>0且actual ratio与目标偏差不超过5个百分点后才能 ACTIVE。退出建议是独立 `recommended_action`；不据此改写账本持仓状态。INVALID仅限未成交计划失效；已成交计划遇到数据失效保留持仓状态并显示DEGRADED。

只录入一条腿：

```text
PARTIALLY_FILLED
```

并产生：

```text
ORPHAN_LEG_WARNING
```

---

# 20. Manual Execution Guide

项目不下单，但要生成清晰的人工操作指引。

例：

```text
ABSOLUTE 100%

Futures:
  BTWUSDT SHORT
  Qty: 7,000 BTW
  Reference price: 1.425

Spot:
  Binance Alpha
  BUY 7,000 BTW
  Reference price: 1.421

Suggested:
  Execute in paired chunks
  Suggested chunk: 1,000 BTW
```

必须同时显示：

```text
Quote generated at
Quote expires at
Price is reference only
User must place orders manually
```

## 20.1 Paired Chunk

默认执行提示：

```text
PAIR_IN_CHUNKS
```

chunk 上限：

```text
chunkNotional <= min(
  futuresNotionalAtMaxImpact,
  spotNotionalAtMaxImpact,
  config.maxManualChunkUsd
)
```

默认：

```yaml
hedge:
  execution:
    max_price_impact_bps: 30
    max_manual_chunk_usd: 5000
```

系统仅给建议，不自动下单。

---

## 20.2 买卖单与止损单的人工参数指引

每次模拟返回结构化 `order_guidance`：leg、venue、instrument、side、order_type、合法quantity、参考limit_price、trigger_price、trigger_basis、reduce_only_or_close_position、有效期、价格偏离上限和venue capability。仅生成平台操作参数，不生成交易请求或链上交易payload。

| 阶段 | 合约空腿 | 现货多腿 | 注意事项 |
|---|---|---|---|
| 建仓 | SELL开空，数量按合约原生单位 | BUY同资产净数量 | 按成对小批执行；每批人工确认实际成交，未成交前不可当作持仓 |
| 正常平仓 | BUY关闭剩余空仓，核对reduce-only/close position | SELL对应现货剩余数量 | RELATIVE按目标比例关闭，检查实际成交而非挂单状态 |
| 上涨风险保护 | 用户在平台设置BUY止损平空；优先MARK_PRICE触发并确保低于用户录入的强平价 | 支持上涨触发卖出的venue可设置对应SELL条件单；不支持则仅提供人工提醒 | 两个独立条件单无法保证同时成交；现货止损限价/止盈单可能未成交或残留 |
| 下跌方向获利 | RELATIVE按风险预算给出BUY止盈/分批平空参数 | 同批释放对应现货 | ABSOLUTE不单独用期货止盈后保留现货，避免形成裸多 |
| 强平/一腿已平 | 用户确认后登记事件 | 对剩余孤腿给出紧急退出数量/参考价 | 无私有接口不能自动发现真实强平，也无法联动另一平台 |

止损触发价格必须由用户输入或基于其明确最大组合损失/安全缓冲计算，禁止把评分转换成保证不爆仓的价格。空单上行止损满足 `current_mark < stop_trigger < user_liquidation_price`；建议安全缓冲至少5%强平距离并显示当前盘口冲击及手动响应压力，但跳空、触发保护与订单拒绝仍可能越过此价。无用户强平价或该价过期时只输出参考风险价，不输出已验证的强平前保护。

跨Venue现货条件价按当前canonical Basis映射仅作参考，返回basis_as_of和允许偏差，不能把期货MARK_PRICE原数值复制到现货。Alpha/DEX无对应条件单能力时明确 `PLATFORM_STOP_UNSUPPORTED`，提供人工操作步骤并将监控覆盖标为LIMITED。监控软件提醒不替代平台订单。

合约先触发止损后现货单应由用户立即检查和取消/重设；现货先成交后空单成为裸空同样处理。系统保留用户登记的订单参数、设置时间、是否已在平台设置，但未读取平台订单，因此“已设置”只标USER_CONFIRMED，不能称LIVE_VERIFIED。

---

# 21. Pair Exit

正常退出必须强调：

> 两条腿成对关闭。

不能把“期货爆仓后再卖现货”作为常规策略。爆仓后的现货处理只能是 emergency fallback。

RELATIVE按各自剩余数量分批退出；一条腿提前清零应产生孤腿提醒。现货dust必须显示数量和市值，不作为完全对冲持仓。

正常退出流程：

```text
1. 刷新 Futures/Spot 价格与深度
2. 确认 Spot 可退出数量和滑点
3. 按 paired chunks 关闭两腿
4. 用户录入实际 exit fills
5. 两腿各自剩余数量为0（允许明确记录交易规则以内dust）后标 CLOSED；RELATIVE两腿数量本来就不相等，禁止检查两腿closed quantity相等
```

若用户确认 futures 已 liquidation：

```text
FUTURES = CLOSED / LIQUIDATED
SPOT = ACTIVE
→ CRITICAL_ORPHAN_SPOT_LEG
```

若 Spot 已卖但 futures 空仓仍存在：

```text
CRITICAL_ORPHAN_FUTURES_LEG
```

---

# 22. Liquidation Risk Monitor

## 22.1 数据来源

项目不读账户，强平价优先级：

```text
1. USER_EXCHANGE
2. ESTIMATED
3. NONE
```

只有 `USER_EXCHANGE` 可称“用户从交易所界面录入的强平价”，仍不是实时账户验证。每次加减仓/保证金变动必须重新录入，超过24小时标STALE。V1不根据leverage单独推算精确强平价；无maintenance margin tier/费用/账户模式输入时不提供ESTIMATED数值。CROSSED受其他持仓影响，默认仅LIMITED风险检查。

ESTIMATED 必须标注：

```text
Estimate only
```

当mark达到用户强平价，仅提醒 `LIQUIDATION_POSSIBLE_UNCONFIRMED` 并紧急复核，不自动登记LIQUIDATION、不将空腿数量清零。Funding流入、账户手续费、保证金调整均可能改变真实强平价。

## 22.2 Short Liq Distance

```text
liqDistancePct =
  (liqPrice - markPrice) / markPrice
```

默认：

```yaml
hedge:
  liquidation:
    warning_distance: 0.20
    critical_distance: 0.10
    emergency_distance: 0.05
    user_price_max_age_sec: 86400
```

状态：

```text
>=20%  NORMAL
10~20% WARNING
5~10%  CRITICAL
<5%    EMERGENCY
```

CRITICAL：

```text
PAIR_EXIT_RECOMMENDED
```

EMERGENCY：

```text
PAIR_EXIT_URGENT
```

系统必须在接近强平前提醒，而不是等推测已爆仓才第一次提示。

---

# 23. Basis Monitor

统一 canonical price 后：

```text
basisPct =
  (canonicalPerpPrice - spotPrice) / spotPrice
```

保存：

```text
entryBasisPct
currentBasisPct
basisChangePct
basisPnlUsd
```

ABSOLUTE：

```text
basisPnl =
  Q × [(Pf_entry - Ps_entry)
     - (Pf_now - Ps_now)]
```

默认：

```yaml
hedge:
  basis:
    warning_vs_accrued_funding: 0.50
    exit_vs_accrued_funding: 1.00
    warning_min_usd: 10
    warning_min_notional_ratio: 0.001
    exit_min_usd: 20
    exit_min_notional_ratio: 0.002
```

```text
adverseBasisLoss >= max(10 USD, 0.1% of futures entry notional, 50% of max(known settled funding,0))
→ WARN_BASIS_CONSUMING_CARRY

>= max(20 USD, 0.2% of futures entry notional, 100% of max(known settled funding,0))
→ EXIT_BASIS_CONSUMED_CARRY
```

只对 adverse basis 触发；亏损为0不触发。Funding完整累计缺失时使用上述固定金额/名义金额门槛，并标CARRY_COMPARISON_UNAVAILABLE，不凭未知分母展示收益耗尽。

---

# 24. Spot Exit Liquidity Monitor

ACTIVE 计划定期重新评估：

```text
remaining Spot Qty 是否能卖出
estimated exit slippage
estimated gas
```

默认：

```yaml
hedge:
  liquidity_monitor:
    max_exit_slippage_bps: 100
    min_exit_coverage_ratio: 1.0
```

规则：

```text
sell executable qty < remaining spot qty
→ CRITICAL_SPOT_EXIT_CAPACITY

estimated exit slippage > max
→ WARN_SPOT_EXIT_SLIPPAGE
```

On-chain 计划优先级更高。

---

# 25. Hedge Monitor PnL

ACTIVE 计划持续计算：

```text
Spot PnL
Futures PnL
Directional PnL
Basis PnL
Estimated Settled Funding
Known Fees
Estimated Exit Cost
Gas
Net PnL Before Exit
Estimated Net PnL After Exit
```

**Funding Accrued 基于公共已结算事件和用户手工成交时间推算，命名为 `estimatedSettledFundingUsd`；无账户流水不能宣称实际到账。**

每事件现金流：`carry_i=short_native_qty_at_ti × exchange_mark_price_i × settled_rate_i`；负率扣款，native qty和交易所mark直接相乘不重复乘倍率。使用事件账本在当时的剩余空单量，不能拿当前量乘完整历史。缺mark、缺持仓历史或结算边界成交的事件标不确定；完整累计=null，同时另列known subtotal及coverage，禁止补0。用户可手工登记 actual funding receipt 并独立显示，不与估算重复相加。

价格PnL拆分（canonical）：`matched_qty=min(Qf,Qs)`；matched basis PnL 加未匹配期货空腿/现货多腿的方向PnL等于两腿总价格PnL。Basis PnL已经包含在Spot+Futures总PnL中，不得再相加。费用Token扣减现货时净quantity进入ratio；Gas/fee统一计一次，VWAP已含滑点的执行价不得另扣同一滑点。

Net after exit = realized price PnL + unrealized price PnL + estimated settled carry - known fees/gas - estimated remaining exit cost；原始费用缺失时输出估算标签，任何关键未知使完整净值=null。所有金额显示结算币种与USD转换来源，USDT/USDC不能默认永远等于1美元。

预测 funding 单独显示：

```text
Projected Next Funding
```

不得计入 accrued PnL。

---

# 26. Alerts

至少支持：

```text
FUNDING_TURNED_NON_POSITIVE
FUNDING_NEGATIVE_STREAK
FUNDING_WEAKENING
FUNDING_EDGE_GONE

HEDGE_RATIO_DRIFT
HEDGE_RATIO_CRITICAL

BASIS_CONSUMING_CARRY
BASIS_CONSUMED_CARRY

LIQ_DISTANCE_WARNING
LIQ_DISTANCE_CRITICAL
LIQ_DISTANCE_EMERGENCY

SPOT_EXIT_SLIPPAGE
SPOT_EXIT_CAPACITY

CONTRACT_DELISTING
VENUE_UNAVAILABLE

ORPHAN_FUTURES_LEG
ORPHAN_SPOT_LEG

MANUAL_FILL_STALE
QUOTE_STALE
```

Severity：

```text
INFO
WARN
CRITICAL
```

Alert 生命周期：

```text
OPEN
ACKNOWLEDGED
RESOLVED
```

同一 plan/condition 不能每个 monitor tick 新建 alert。建议 dedup key：

```text
(plan_id, alert_code, leg_type_or_venue)
```

同条件 OPEN/ACKNOWLEDGED 期间更新last_seen/context，不重复创建；RESOLVED后再次触发新episode。条件恢复需连续2个成功且新鲜tick才标RESOLVED；抓取失败不能视为恢复。严重级别和建议动作分离：severity=INFO/WARN/CRITICAL，recommended_action=NONE/REVIEW/PAIR_EXIT/URGENT_PAIR_EXIT，EXIT_RECOMMENDED是动作而非比CRITICAL更高的严重级别。

---

# 27. 浏览器提醒

V1 支持：

```text
Alert Center
UI Toast
Browser Notification
Optional sound
```

浏览器 Notification 必须由用户主动授权。

Email / Telegram / Slack 不作为 V1 前置。

---

# 28. DuckDB 增量设计

现有迁移：

```text
001_init.sql
002_unlock_social.sql
003_catalyst.sql
```

本次必须新增：

```text
004_hedge_advisor.sql
```

**禁止修改已经执行的 001–003。**

## 28.0 数据类型与账本边界

下面DOUBLE用于分析投影，不作为可下单原始精度。原始quantity/price/fee数值以十进制字符串保存于事件/quote JSON；源时间、fetch时间、formula/config/mapping版本、引用quote snapshot IDs随plan_config_json固化。sl_hedge_leg是事件聚合投影，不能替代不可变成交表。

004同时创建以下表（均由Task19负责，应用层在同一worker事务内维护引用完整性）：

```sql
CREATE TABLE IF NOT EXISTS sl_hedge_venue_mapping (
  mapping_id VARCHAR PRIMARY KEY,
  canonical_id VARCHAR NOT NULL,
  venue VARCHAR NOT NULL,
  instrument_id VARCHAR NOT NULL,
  mapping_version VARCHAR NOT NULL,
  verification_json VARCHAR NOT NULL,
  verified_at_ms BIGINT NOT NULL,
  UNIQUE(canonical_id, venue, instrument_id, mapping_version)
);
CREATE TABLE IF NOT EXISTS sl_hedge_fill_event (
  event_id VARCHAR PRIMARY KEY,
  plan_id VARCHAR NOT NULL,
  client_event_id VARCHAR NOT NULL,
  leg_type VARCHAR NOT NULL,
  event_type VARCHAR NOT NULL,
  executed_at_ms BIGINT NOT NULL,
  recorded_at_ms BIGINT NOT NULL,
  event_json VARCHAR NOT NULL,
  UNIQUE(plan_id, client_event_id)
);
CREATE INDEX IF NOT EXISTS idx_sl_hedge_fill_time
ON sl_hedge_fill_event(plan_id, executed_at_ms, event_id);
CREATE TABLE IF NOT EXISTS sl_hedge_outcome (
  outcome_id VARCHAR PRIMARY KEY,
  fcs_snapshot_id VARCHAR NOT NULL,
  strategy VARCHAR NOT NULL,
  horizon_days INTEGER NOT NULL,
  outcome_status VARCHAR NOT NULL,
  reason_code VARCHAR,
  evidence_version VARCHAR NOT NULL,
  cost_config_hash VARCHAR NOT NULL,
  outcome_json VARCHAR NOT NULL,
  updated_at_ms BIGINT NOT NULL,
  UNIQUE(fcs_snapshot_id, strategy, horizon_days, evidence_version, cost_config_hash)
);
```

## 28.1 sl_funding_capture_snapshot

```sql
CREATE TABLE IF NOT EXISTS sl_funding_capture_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  symbol VARCHAR NOT NULL,
  canonical_id VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,

  fcs_version VARCHAR NOT NULL,
  fcs_config_hash VARCHAR NOT NULL,
  reference_notional_usd DOUBLE NOT NULL,

  fcs DOUBLE,
  module_scores_json VARCHAR NOT NULL,
  funding_metrics_json VARCHAR NOT NULL,
  venue_summary_json VARCHAR NOT NULL,
  basis_json VARCHAR,
  risk_json VARCHAR NOT NULL,

  readiness VARCHAR NOT NULL,
  reasons_json VARCHAR NOT NULL,

  created_at_ms BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sl_fcs_symbol_time
ON sl_funding_capture_snapshot(symbol, as_of_ms);
```

## 28.2 sl_spot_venue_snapshot

```sql
CREATE TABLE IF NOT EXISTS sl_spot_venue_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  canonical_id VARCHAR NOT NULL,
  venue VARCHAR NOT NULL,
  venue_symbol VARCHAR,
  chain VARCHAR,
  contract_address VARCHAR,

  as_of_ms BIGINT NOT NULL,
  fetched_at_ms BIGINT NOT NULL,
  expires_at_ms BIGINT,

  reference_notional_usd DOUBLE NOT NULL,

  quote_json VARCHAR NOT NULL,
  status VARCHAR NOT NULL,
  reason_code VARCHAR
);
```

## 28.3 sl_hedge_plan

```sql
CREATE TABLE IF NOT EXISTS sl_hedge_plan (
  plan_id VARCHAR PRIMARY KEY,

  symbol VARCHAR NOT NULL,
  canonical_id VARCHAR NOT NULL,

  mode VARCHAR NOT NULL,
  status VARCHAR NOT NULL,

  target_hedge_ratio DOUBLE NOT NULL,

  futures_notional_usd DOUBLE NOT NULL,
  futures_contract_qty DOUBLE NOT NULL,
  canonical_futures_qty DOUBLE NOT NULL,

  spot_venue VARCHAR NOT NULL,
  spot_symbol VARCHAR,
  spot_chain VARCHAR,
  spot_contract VARCHAR,

  target_spot_qty DOUBLE NOT NULL,
  target_spot_notional_usd DOUBLE,

  leverage DOUBLE,
  margin_mode VARCHAR,
  margin_usd DOUBLE,

  liquidation_price DOUBLE,
  liquidation_price_source VARCHAR,

  planned_hold_days INTEGER,

  fcs_snapshot_id VARCHAR,
  plan_safety_score DOUBLE,

  plan_config_json VARCHAR NOT NULL,

  created_at_ms BIGINT NOT NULL,
  activated_at_ms BIGINT,
  closed_at_ms BIGINT,
  updated_at_ms BIGINT NOT NULL,
  plan_version BIGINT NOT NULL DEFAULT 1
);
```

## 28.4 sl_hedge_leg

```sql
CREATE TABLE IF NOT EXISTS sl_hedge_leg (
  leg_id VARCHAR PRIMARY KEY,
  plan_id VARCHAR NOT NULL,

  leg_type VARCHAR NOT NULL,
  venue VARCHAR NOT NULL,
  side VARCHAR NOT NULL,

  qty DOUBLE NOT NULL,
  canonical_qty DOUBLE NOT NULL,
  avg_entry_price DOUBLE,
  actual_fee_usd DOUBLE,
  actual_gas_usd DOUBLE,

  opened_at_ms BIGINT,

  closed_qty DOUBLE NOT NULL DEFAULT 0,
  avg_exit_price DOUBLE,
  exit_fee_usd DOUBLE,
  exit_gas_usd DOUBLE,
  closed_at_ms BIGINT,

  state VARCHAR NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
```

## 28.5 sl_hedge_monitor_snapshot

```sql
CREATE TABLE IF NOT EXISTS sl_hedge_monitor_snapshot (
  snapshot_id VARCHAR PRIMARY KEY,
  plan_id VARCHAR NOT NULL,
  as_of_ms BIGINT NOT NULL,

  actual_hedge_ratio DOUBLE,
  residual_short_notional_usd DOUBLE,

  mark_price DOUBLE,
  spot_price DOUBLE,

  current_basis_pct DOUBLE,
  basis_pnl_usd DOUBLE,

  estimated_settled_funding_usd DOUBLE,
  projected_next_funding_usd DOUBLE,

  spot_pnl_usd DOUBLE,
  futures_pnl_usd DOUBLE,

  known_cost_usd DOUBLE,
  estimated_exit_cost_usd DOUBLE,

  net_pnl_before_exit_usd DOUBLE,
  estimated_net_pnl_after_exit_usd DOUBLE,

  liquidation_distance DOUBLE,

  exit_liquidity_json VARCHAR,
  safety_score DOUBLE,

  status VARCHAR NOT NULL,
  created_at_ms BIGINT NOT NULL
);
```

## 28.6 sl_hedge_alert

```sql
CREATE TABLE IF NOT EXISTS sl_hedge_alert (
  alert_id VARCHAR PRIMARY KEY,
  plan_id VARCHAR NOT NULL,

  code VARCHAR NOT NULL,
  severity VARCHAR NOT NULL,
  state VARCHAR NOT NULL,

  opened_at_ms BIGINT NOT NULL,
  last_seen_at_ms BIGINT NOT NULL,
  acknowledged_at_ms BIGINT,
  resolved_at_ms BIGINT,

  dedup_key VARCHAR NOT NULL,
  episode INTEGER NOT NULL,
  recommended_action VARCHAR NOT NULL,
  context_json VARCHAR NOT NULL,
  UNIQUE(plan_id, dedup_key, episode)
);
```


## 28.7 索引、约束与保留

004增加 `sl_hedge_plan(status,updated_at_ms,plan_id)`、`sl_hedge_leg(plan_id,leg_type)` unique、`sl_spot_venue_snapshot(canonical_id,venue,as_of_ms)`、`sl_hedge_monitor_snapshot(plan_id,as_of_ms)`、`sl_hedge_alert(plan_id,state,last_seen_at_ms)`索引。Alert增加dedup_key/episode字段，更新与打开新episode在同一事务中执行。API验证enum、所有有限数值和quantity非负；不得关闭超过剩余数量。真实账本状态与事件写入、leg投影、plan_version更新原子提交。

监控每10秒计算但仅每60秒或风险状态变化时持久化；完整分钟快照保留30天，之后按小时保留365天，再按日保留。未关闭计划、成交事件、未解决告警、计划版本、用于Evidence的入场和退出快照不删除；降采样记录规则版本，不改变冻结证据。指标查询使用稳定时间/id次序。

# 29. Repository 扩展

继续扩展现有：

```text
shortlab/repository.py
```

不得新增第二套 DB repository 服务。

新增 async 方法建议：

```python
save_funding_capture_snapshot(...)
list_funding_opportunities(...)

save_spot_venue_snapshot(...)
list_spot_venues(...)

create_hedge_plan(...)
get_hedge_plan(...)
list_hedge_plans(...)
update_hedge_plan(...)

append_hedge_fill_event(...)  # Task19存储；Task24领域调用
correct_hedge_fill_event(...)  # 显式撤销/替代事件
aggregate_hedge_position(...)

save_hedge_monitor_snapshot(...)
latest_hedge_monitor(...)

upsert_hedge_alert(...)
ack_hedge_alert(...)
resolve_hedge_alert(...)
list_hedge_alerts(...)
```

继续遵守既有仓库约束：

- public DB 方法 async；
- 内部单 worker 串行 DuckDB；
- 写操作使用 transaction；
- async request handler 中不得直接执行大批量同步 SQL；
- 004在事务中执行，失败回滚并令新增Hedge能力unavailable；不得在失败的repository对象上继续调用新表查询。现有001–003正常完成时原API仍运行。

---

# 30. Runtime / Scheduler

继续复用：

```text
shortlab/runtime.py
shortlab/scheduler.py
shortlab/service.py
```

禁止新增独立 Scheduler。

新增 job 类型：

```text
funding_capture_refresh
hedge_venue_refresh
```

ACTIVE 高频 monitor 不建议每 10 秒写 `sl_job_run`，避免数据库膨胀。

默认：

```yaml
hedge:
  refresh:
    opportunity_sec: 1800
    active_mark_sec: 10
    active_basis_sec: 10
    active_funding_sec: 60
    active_depth_sec: 30
    active_onchain_quote_sec: 60
    active_alpha_quote_sec: 10
    contract_status_sec: 600
    funding_settlement_check_sec: 30
    monitor_persist_sec: 60
```

只对：

```text
PARTIALLY_FILLED
ACTIVE
CLOSING
```

计划进行高频 monitor。

**禁止对 500 个 universe 每 10 秒做全量 depth/Alpha/onchain quote。**

建议流水线：

```text
Existing Universe / Funding
        |
        v
cheap Funding prefilter
        |
        v
FCS candidate set
        |
        v
reference-notional venue quotes
        |
        v
Funding Opportunities
```

ACTIVE：

```text
Active Plan
  ├─ mark
  ├─ spot quote
  ├─ basis
  ├─ funding
  ├─ liq distance
  ├─ exit liquidity
  └─ alerts
```

---

# 31. Data Freshness

默认：

```yaml
hedge:
  freshness:
    futures_mark: {ttl: 20, grace: 60}
    spot_quote: {ttl: 20, grace: 60}
    spot_depth: {ttl: 60, grace: 180}
    alpha_quote: {ttl: 20, grace: 60}
    funding_current: {ttl: 120, grace: 300}
    onchain_quote: {ttl: 90, grace: 180}
    contract_status: {ttl: 900, grace: 1800}
```

超过 TTL：

```text
stale=true
```

超过 grace：

```text
NOT_READY / MONITOR_DEGRADED
```

不能继续显示绿色 NORMAL。

---

# 32. API 设计

所有接口继续属于现有 FastAPI：

```text
/api/short/*
```

## 32.1 GET /api/short/funding-opportunities

Query：

```text
min_fcs=0..100
min_funding_30d=<decimal>
min_positive_ratio_30d=0..1
venue=BINANCE_SPOT|BINANCE_ALPHA|ONCHAIN_DEX
readiness=READY|NOT_READY|BLOCKED
sort=fcs|funding30d|breakEvenDays|positiveRatio30d
order=asc|desc
limit
offset
```

返回示例：

```json
{
  "asOf": 0,
  "items": [
    {
      "symbol": "BTWUSDT",
      "canonicalId": "btw",
      "fcs": 89.0,
      "fcsVersion": "fcs_v1",
      "funding30d": 0.043,
      "funding90d": 0.108,
      "positiveRatio30d": 0.96,
      "positiveRatio90d": 0.92,
      "bestVenue": "BINANCE_ALPHA",
      "referenceNotionalUsd": 10000,
      "roundTripCostPct": 0.0032,
      "breakEvenDays": 2.7,
      "readiness": "READY",
      "reasons": []
    }
  ]
}
```

## 32.2 GET /api/short/hedge/venues/{symbol}

Query：

```text
notional_usd
```

返回全部可用 Venue，不只返回 best。

## 32.3 POST /api/short/hedge/simulate

只计算，不写入 Plan。

Body：

```json
{
  "symbol": "BTWUSDT",
  "mode": "RELATIVE",
  "futuresNotionalUsd": 10000,
  "hedgeRatio": 0.7,
  "preferredSpotVenue": "AUTO",
  "futuresLeverage": 1,
  "marginMode": "ISOLATED",
  "marginUsd": 10000,
  "liquidationPrice": 2.1,
  "liquidationPriceSource": "USER_EXCHANGE"
}
```

## 32.4 POST /api/short/hedge/plans

保存用户确认的 plan。**不下单。**

## 32.5 GET /api/short/hedge/plans

支持：

```text
status
symbol
mode
venue
limit
offset
```

## 32.6 GET /api/short/hedge/plans/{plan_id}

返回完整 Plan + Actual Legs + 最新 Monitor + Alerts。

## 32.7 PATCH /api/short/hedge/plans/{plan_id}/legs

用途：

> 用户手工登记实际成交/部分成交/退出成交。

不得触发交易 API。

## 32.8 POST /api/short/hedge/plans/{plan_id}/activate

只改变本地状态。

若两腿不完整：

```text
409 HEDGE_LEGS_INCOMPLETE
```

## 32.9 POST /api/short/hedge/plans/{plan_id}/close

只在用户已手工完成两腿退出后标 CLOSED。

若仍有 open qty：

```text
409 OPEN_LEGS_REMAIN
```

## 32.10 GET /api/short/hedge/plans/{plan_id}/monitor

返回：

```text
actual ratio
residual exposure
spot/futures pnl
estimated settled funding
projected next funding
basis
liq distance
exit liquidity
plan safety
alerts
```

## 32.11 GET /api/short/hedge/alerts

Query：

```text
plan_id
state
severity
code
```

## 32.12 POST /api/short/hedge/alerts/{alert_id}/ack

只确认本地 alert。

---

# 33. API 错误与 Reason Code

至少：

```text
HEDGE_SYMBOL_NOT_FOUND
HEDGE_IDENTITY_UNVERIFIED
HEDGE_MULTIPLIER_UNVERIFIED

FUNDING_HISTORY_INCOMPLETE
FUNDING_NOT_POSITIVE
FUNDING_PERSISTENCE_LOW

NO_SPOT_VENUE
SPOT_EXIT_UNVERIFIED
SPOT_QUOTE_STALE

HEDGE_RATIO_INVALID
HEDGE_RISK_BUDGET_INVALID

HEDGE_PLAN_NOT_FOUND
HEDGE_LEGS_INCOMPLETE
OPEN_LEGS_REMAIN

LIQUIDATION_PRICE_INVALID

ONCHAIN_PROVIDER_UNAVAILABLE
ONCHAIN_CONTRACT_UNVERIFIED

ALPHA_MARKET_UNAVAILABLE
```

第三方 provider 错误不得导致：

```text
/api/scan
/api/short/candidates
```

整体失败。

---

# 34. UI 信息架构

现有 `SHORT LAB` 内增加子导航：

```text
Candidates
Funding
Hedge Plans
Alerts
Evidence
```

不新建独立顶层产品。

## 34.1 Funding 页面

表格：

| Token | FCS | Funding 7D | 30D | 90D | Positive 30D | Best Venue | Cost | BE Days | State |
|---|---:|---:|---:|---:|---:|---|---:|---:|---|

支持筛选：

```text
FCS
Funding
Positive ratio
Venue
Break-even
Readiness
```

## 34.2 Hedge Planner 页面

布局：

```text
Asset Summary
Funding
Venue Quotes
Mode
Position / Risk Inputs
Result
Stress
Cost / Break-even
Manual Execution Guide
Warnings
```

## 34.3 Active Monitor

必须同时显示 Target 和 Actual：

```text
Target ratio      70%
Actual ratio      68.7%
Residual short    $3,130

Estimated settled +$84
Basis PnL         -$16
Spot PnL          +$250
Futures PnL       -$360
Known fees        -$22
Estimated exit    -$18

Estimated net after exit
                  -$82
```

## 34.4 Alerts

按优先级：

```text
CRITICAL
WARN
INFO
```

---

# 35. UI 文案规则

必须明确区分：

```text
Historical
Projected
Estimated
Reference
User-entered
Accrued
```

正确：

```text
Historical 30D Funding
Projected Next Funding
Estimated Settled Funding
User-entered Liquidation Price
Estimated Exit Slippage
Reference Buy Price
```

禁止把 Projected 当作 Accrued。

---

# 36. 与现有 Portfolio 的关系

现有普通 Portfolio 可继续使用浏览器 `localStorage`。

Hedge Plan **禁止只保存在 localStorage**，因为：

- Backend Scheduler 要监控；
- 需要 DuckDB 历史；
- 浏览器关闭后不能丢失；
- 需要 Alert 生命周期。

Hedge Plan 必须存 DuckDB。

UI theme/filter 偏好仍可 localStorage。

---

# 37. 与 LTSS / Entry / VETO 的兼容关系

## 37.1 ABSOLUTE

ABSOLUTE READY 不依赖 Entry Score。

例：

```text
LTSS  75
Entry 25
FCS   92
```

允许：

```text
Directional Short = NOT_READY
Funding Capture   = READY
```

## 37.2 RELATIVE

RELATIVE 保留净空头，因此可使用 LTSS/Entry 作为附加警告。

默认：

```text
LTSS < 60
→ WARN_RELATIVE_DIRECTIONAL_EDGE_WEAK

Entry < 40
→ WARN_RELATIVE_ENTRY_NOT_BEARISH
```

如果既有：

```text
PAUSE_SQUEEZE
```

则新建 RELATIVE plan 的 readiness：

```text
NOT_READY
```

ABSOLUTE 则仍根据 Funding/Basis/Venue 独立判断。

## 37.3 Existing BLOCK

这些继续适用于 Hedge：

```text
VETO_DATA_IDENTITY
VETO_CONTRACT_DELISTING
```

Hedge使用自己的HedgeDQ与关键字段Gate，不直接继承方向性VETO_LOW_DATA_QUALITY（其非对冲必需provider会造成误阻塞）。但 Hedge 还必须重新按**目标 notional 的双腿退出能力**检查流动性。

---

# 38. Hedge Data Quality

不要修改现有 LTSS `DataQuality` 语义。

新增：

```text
HedgeDataQuality
```

默认组：

| 组 | 权重 |
|---|---:|
| Funding | 30 |
| Futures | 20 |
| Spot Venue | 25 |
| Identity / Units | 15 |
| Contract / Time | 10 |

READY：

```text
HedgeDQ >= 80
```

ACTIVE Monitor 若：

```text
HedgeDQ < 60
```

显示：

```text
MONITOR_DEGRADED
```

但不能自动改变用户持仓状态。

---

## 38.1 字段权重与缺失规则

| 组 | 组内字段权重（合计100） |
|---|---|
| Funding | 当前期rate/time 20；nextFundingTime/interval 10；7D完整历史10；30D完整历史30；90D完整历史20；标准差/滚动窗口可计算10 |
| Futures | mark/time 25；买卖退出VWAP/深度40；交易规则25；symbol状态10 |
| Spot Venue | 同quantity买入quote25；卖出quote35；rules/decimals20；fee/Gas成本20 |
| Identity / Units | 同底层身份50；multiplier验证30；quote/USD汇率20 |
| Contract / Time | 合约生命周期50；数据时间可验证30；本地监控心跳20 |

字段只有已验证、有效且新鲜才贡献1，缺失/错误/超过TTL贡献0；Funding历史字段贡献 `coverage_fraction` 但仍需complete=true，其他非Funding字段不按条数折分。90D仅年龄有证据不足时N/A，其组内分母剔除并归一，身份和退出quote永不可N/A。Onchain quote超过provider expires_at立即失效，取本地TTL与provider期限更早者；futures/spot用于Basis的源时间偏差最多5秒，超过时Basis=null并NOT_READY。

HedgeDQ=sum(group_weight×applicable_field_weighted_fraction)。展示组和字段原因。HedgeDQ是数据质量，不含强平距离；执行Gate另检查关键字段、目标量双向可退出与用户强平价，因此即使总分80也可能NOT_READY。按原方向性字段定义得出的DQ/VETO不混入此公式。

---

# 39. Evidence 扩展

既有方向性 7D/30D/90D Evidence 保持不变。

新增 Hedge Opportunity Evidence。

## 39.1 Point-in-time Snapshot

每次 FCS snapshot 固化：

```text
Funding
Venue
Basis
Costs
FCS
```

## 39.2 固定模拟策略

至少：

```text
ABSOLUTE_100
RELATIVE_75
RELATIVE_50
RELATIVE_25
```

## 39.3 Outcome

7D / 30D / 90D：

```text
spot_pnl
futures_pnl
basis_pnl
funding_carry
fees
slippage
gas
net_return

max_portfolio_drawdown
max_adverse_basis
negative_funding_settlements
funding_coverage
```

禁止使用未来现货 quote 回填入场成本。snapshot同时冻结原始quote、双腿quantity/price、FX、rule/mapping版本、Funding输入与cost hash。缺历史Alpha/链上退出quote时标UNAVAILABLE，不借当前价回填；退市或无法继续观测标CENSORED保留样本。未到期PENDING，到期且双腿、Funding及成本完整才COMPLETE。

net_return分母为冻结capital_at_risk，不是保证金单项；同时输出对futures notional的回报。组合drawdown仅在同时间轴完整双腿路径可用时计算；不得拼接异步spot/perp日高低得到虚构极值。清算路径无法模拟时不宣称长期对冲实际可执行收益；用户实盘手工登记PnL与固定策略模拟Evidence分开统计。

目标是后续真实回答：

```text
FCS 80+ 是否优于 60~70？
ABSOLUTE 的稳定性如何？
RELATIVE_50 是否提高收益但显著提高 MAE？
哪个 Venue 的真实退出成本更高？
```

---

# 40. 配置新增

append 到现有 `shortlab/default.yaml`，不要另建平行 config：

```yaml
funding_capture:
  enabled: false  # 新功能完成验收后由用户启用
  fcs_version: fcs_v1
  reference_notional_usd: 10000

  entry_gate:
    require_current_positive: true
    require_last_settled_positive: true
    min_funding_7d: 0.0
    min_funding_30d: 0.0
    min_positive_ratio_30d: 0.75
    min_positive_ratio_90d: 0.60
    min_30d_coverage: 0.90
    min_90d_coverage: 0.80

  exit:
    negative_settlement_streak: 2
    weakening_7d_vs_30d_ratio: 0.30

hedge:
  enabled: false  # 新功能完成验收后由用户启用

  ratio:
    drift_warn_pct: 0.02
    drift_critical_pct: 0.05

  execution:
    max_price_impact_bps: 30
    max_manual_chunk_usd: 5000

  liquidation:
    warning_distance: 0.20
    critical_distance: 0.10
    emergency_distance: 0.05
    user_price_max_age_sec: 86400

  basis:
    warning_vs_accrued_funding: 0.50
    exit_vs_accrued_funding: 1.00
    warning_min_usd: 10
    warning_min_notional_ratio: 0.001
    exit_min_usd: 20
    exit_min_notional_ratio: 0.002

  liquidity_monitor:
    max_exit_slippage_bps: 100
    min_exit_coverage_ratio: 1.0

  freshness:
    futures_mark: {ttl: 20, grace: 60}
    spot_quote: {ttl: 20, grace: 60}
    spot_depth: {ttl: 60, grace: 180}
    alpha_quote: {ttl: 20, grace: 60}
    funding_current: {ttl: 120, grace: 300}
    onchain_quote: {ttl: 90, grace: 180}
    contract_status: {ttl: 900, grace: 1800}

  refresh:
    opportunity_sec: 1800
    active_mark_sec: 10
    active_basis_sec: 10
    active_funding_sec: 60
    active_depth_sec: 30
    active_onchain_quote_sec: 60
    active_alpha_quote_sec: 10
    contract_status_sec: 600
    funding_settlement_check_sec: 30
    monitor_persist_sec: 60

  costs:
    futures_entry_fee_rate: 0.0005
    futures_exit_fee_rate: 0.0005
    spot_entry_fee_rate: 0.001
    spot_exit_fee_rate: 0.001
    alpha_entry_fee_rate: 0.001
    alpha_exit_fee_rate: 0.001
    onchain_extra_buffer_bps: 20

  providers:
    binance_spot:
      enabled: true
    binance_alpha:
      enabled: false  # 公共行情/规则/身份fixture与实网验收后启用
    onchain:
      enabled: false
```

必须扩展现有 Config validation。

新增：

```text
fcs_config_hash
hedge_cost_config_hash
```

禁止污染既有 LTSS `config_hash`。配置读取扩展已知键schema但不扩大旧hash输入集合。`fcs_config_hash`覆盖FCS评分版本、Funding gate、参考notional、DQ字段规则、报价成本及freshness；`hedge_cost_config_hash`覆盖成本和FX估值规则；plan另存 `hedge_formula_version`、完整风险/止损参数、mapping版本和引用snapshot IDs。canonical JSON采用与现有hash相同的排序/紧凑格式/UTF-8/禁止NaN规则，排除密钥、provider URL、本地路径和调度频率。评分阈值与组内DQ权重必须形成冻结fixture，代码和文档逐项一致。

---

# 41. 版本与兼容

新增：

```text
FCS_VERSION = "fcs_v1"
HEDGE_FORMULA_VERSION = "hedge_v1"
HEDGE_EVIDENCE_VERSION = "hedge_evidence_v1"
```

旧字段保持：

```text
feature_version
score_version
entry_version
config_hash
```

任何会改变历史可重算结果的数学修改都必须升级相应版本。


# 42. 推荐代码结构

```text
desktop/backend/src/diveintocrypto_desktop/

├── data/
│   ├── spot.py                  # 现有，继续复用/扩展
│   └── binance_alpha.py         # 新增公共 Alpha market-data adapter
│
├── shortlab/
│   ├── ...                      # 现有模块保持
│   │
│   ├── hedge/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── funding_score.py
│   │   ├── opportunity.py
│   │   ├── planner.py
│   │   ├── simulator.py
│   │   ├── ledger.py
│   │   ├── monitor.py
│   │   ├── alerts.py
│   │   └── venues/
│   │       ├── base.py
│   │       ├── binance_spot.py
│   │       ├── binance_alpha.py
│   │       └── onchain.py
│   │
│   └── migrations/
│       └── 004_hedge_advisor.sql
│
└── api/
    └── shortlab.py              # 继续同一 router，增量 endpoint
```

UI：

```text
desktop/ui/src/app/shortlab/

├── existing files...
├── funding-view.jsx
├── hedge-planner.jsx
├── hedge-monitor.jsx
├── hedge-alerts.jsx
└── hedge-format.js
```

明确禁止：

```text
desktop/hedge-service
new FastAPI
new database service
React direct Binance Alpha fetch
React direct onchain quote fetch
```

---

# 43. Phase 7–11 实施计划

## Phase 7：领域、配置、存储、Funding Opportunity

### Task 19：Hedge Domain + Config + Migration

**修改/新增：**

```text
shortlab/hedge/models.py
shortlab/default.yaml
shortlab/config.py
shortlab/migrations/004_hedge_advisor.sql
shortlab/repository.py
```

**测试：**

```text
test_shortlab_hedge_models.py
test_shortlab_hedge_config.py
test_shortlab_hedge_repository.py
```

**验收：**

- 旧 001→004 正向升级；
- 004 重复启动幂等；
- 旧 LTSS/Entry/Outcome 正常读取；
- Hedge plan/fill/alert 可持久化；
- 不创建第二数据库；
- 新配置异常只令 Hedge 功能 unavailable，不破坏原 App。

### Task 20：Spot Venue Abstraction + Binance Alpha

**新增：**

```text
data/binance_alpha.py
shortlab/hedge/venues/base.py
shortlab/hedge/venues/binance_spot.py
shortlab/hedge/venues/binance_alpha.py
```

**测试：**

```text
test_shortlab_hedge_venues.py
test_shortlab_binance_alpha.py
```

**验收：**

- Binance Spot 使用既有 identity；
- Alpha identity 不模糊猜测；
- reference notional 买卖双向 quote；
- fee/slippage/depth 单位正确；
- provider 失败只令对应 venue unavailable；
- `/api/scan` 回归通过。

### Task 21：FCS + Opportunity Scanner

**新增：**

```text
shortlab/hedge/funding_score.py
shortlab/hedge/opportunity.py
```

**接线交付：**

```text
Task21交付opportunity领域入口；service/scheduler/runtime由Task26统一接线
```

**测试：**

```text
test_shortlab_fcs.py
test_shortlab_funding_opportunity.py
```

**验收：**

- 固定 fixture FCS 完全确定；
- 重复 100 次结果相同；
- FCS 与 LTSS/Entry 无耦合；
- current/settled Funding 分开；
- reference notional 固定；
- 缺数据不重分权重。

---

# 44. Phase 8：Planner / Simulation / On-chain

### Task 22：Absolute / Relative Hedge Planner

**新增：**

```text
hedge/planner.py
hedge/simulator.py
```

**测试：**

```text
test_shortlab_hedge_planner.py
test_shortlab_hedge_simulator.py
```

必须覆盖：

```text
ABSOLUTE 100%
RELATIVE 25/50/75%
risk budget 反推
1000PEPE multiplier
futures/spot step rounding
stress +100/+200
roundtrip cost
break-even
basis
```

### Task 23：On-chain Read-only Quote

**新增/扩展：**

```text
hedge/venues/onchain.py
```

至少完成一个真实、可配置的 read-only quote adapter 后，`ONCHAIN_DEX` 才可在 UI 标 READY。

必须测试：

```text
verified contract
buy quote
sell quote
gas
stale quote
no sell route
insufficient depth
provider timeout
429
```

---

# 45. Phase 9：Manual Ledger + Active Monitor

### Task 24：Hedge Plan / Actual Fill

**新增与接线交付：**

```text
hedge/ledger.py
repository合同由Task19提供；service/api由Task26统一接线
```

**测试：**

```text
test_shortlab_hedge_plan.py
```

必须覆盖：

```text
DRAFT
READY
one leg only
PARTIALLY_FILLED
ACTIVE
partial close
orphan leg
CLOSING
CLOSED gate
```

### Task 25：Monitor + Alerts

**新增：**

```text
hedge/monitor.py
hedge/alerts.py
```

**接线交付：**

```text
Task25交付monitor/alerts领域入口；runtime/scheduler/service由Task26统一接线
```

**测试：**

```text
test_shortlab_hedge_monitor.py
test_shortlab_hedge_alerts.py
```

覆盖：

```text
funding reversal
basis adverse
liq distance
ratio drift
exit liquidity
contract delisting
alert dedup
ack
resolve
```

---

# 46. Phase 10：API / UI

### Task 26：API

扩展现有：

```text
api/shortlab.py
```

负责service/runtime/scheduler及现有router的最终接线，不新建第二FastAPI应用；新增domain文件所有权依第47.1节。

测试：

```text
test_shortlab_hedge_api.py
```

### Task 27：React

新增：

```text
Funding
Hedge Planner
Hedge Monitor
Alerts
```

所有 API 调用继续通过：

```text
desktop/ui/src/app/data.js
```

组件不得直接访问 Binance / Alpha / Onchain provider。

执行：

```bash
cd desktop/ui
npm test
npm run build
```

---

# 47. Phase 11：Evidence / Regression / Documentation

### Task 28：Hedge Evidence

扩展：

```text
shortlab/evidence/
```

使用004定义的 `sl_hedge_outcome`，不改变旧 `sl_forward_outcome`。

### Task 29：Full Regression

必须执行：

```bash
(cd desktop/backend && uv run pytest -q)
uv run --project desktop/backend pytest tests/ -q
(cd desktop/ui && npm test && npm run build)
```

实网 smoke：

```text
Binance Futures
Binance Spot
Binance Alpha
configured Onchain Quote Provider
```

任意 provider 不可达时：

- 明确 unavailable；
- 不切 mock；
- `/api/scan` 不受影响。

### Task 30：Documentation

更新：

```text
README.md
docs/api.md
docs/testing.md
docs/用户手册.md
docs/ShortLab_Detailed_Design_CN.md
docs/ShortLab_Implementation_Plan_CN.md
```

本升级文档与原设计按功能边界互相引用；不得将新增能力描述成已经实现或在旧实施计划中无依据地勾选完成。

---

## 47.1 依赖与文件所有权

上述Task是拆分建议，实际并行须遵循：19冻结DTO/004/Repository/config合同 → 20/23各Venue adapter并行 → 21/22纯计算并行 → 24账本 → 25监控 → 26统一API接线 → 27UI → 28Evidence与29回归 → 30文档。23失败仅其Venue禁用；26/27不能因未配置链上provider阻塞Spot流程。

共享文件唯一owner：Task19负责repository/config/default.yaml/004，后续存储需求回交19；Task26负责service/runtime/scheduler/api的最终接线，Task21/24/25交付独立domain模块和合同测试，不并行修改这些文件；Task20负责data/spot.py及data/binance_alpha.py，Task22负责planner/simulator与人工订单指引；Task28独占hedge Evidence计算模块。Task19须在004预留本文全部表与索引，已发布后新增变化另建005，不修改已执行迁移。

Task19验收增加哈希不变/事件幂等/版本冲突/故障回滚；Task20/23增加同量双向quote、quote currency/FX、token tax与能力不足；Task21增加interval变化/完整性/资产年龄缺失/90D N/A；Task22增加方向风险预算边界、Decimal、平台stop能力和清算路径；Task24/25增加部分加减仓Funding积分、结算边界不确定、Basis零carry门槛、ACK去重、后端睡眠恢复；Task26/27增加current stale不显示READY以及退出推荐与持仓状态分离；Task28增加退市CENSORED和历史quote缺失。每项断言落入对应附录B测试，不以实网偶然成功替代合同测试。

---

# 48. 测试矩阵

## 48.1 数学

- ABSOLUTE h=1；
- RELATIVE h=0.25/0.5/0.75；
- risk budget 反推；
- multiplier；
- quantity rounding；
- basis PnL；
- residual exposure；
- Funding carry；
- Break-even；
- FCS；
- Plan Safety。

## 48.2 Provider

- Binance Spot OK；
- Binance Spot N/A；
- Alpha OK；
- Alpha unavailable；
- Onchain buy only；
- Onchain buy+sell；
- Onchain gas missing；
- 429；
- timeout；
- stale；
- identity conflict。

## 48.3 Lifecycle

- DRAFT；
- READY；
- PARTIALLY_FILLED；
- ACTIVE；
- 退出建议与ACTIVE持仓状态分离；
- CLOSING；
- CLOSED；
- INVALID。

## 48.4 Alert

每个 alert code 至少覆盖：

```text
trigger
dedup
persist
ack
recover
resolve
```

## 48.5 Compatibility

必须证明：

```text
LTSS unchanged
Entry unchanged
DQ unchanged
existing Candidates unchanged
existing Evidence unchanged
/api/scan unchanged
/api/symbol unchanged
old DB opens
001–003 untouched
Android untouched
```

---

# 49. 关键边界测试

## 49.1 1000 Token

```text
1000PEPE futures qty = 100
multiplier = 1000
spot target = 100,000 PEPE
```

Quote Volume/OI USD 不乘 multiplier。

## 49.2 No Binance Spot, Alpha Exists

```text
Binance Spot = NOT_APPLICABLE
Alpha = READY
```

允许使用 Alpha。

## 49.3 Onchain 没有 Sell Quote

即使 Buy quote 正常：

```text
ABSOLUTE = NOT_READY
```

## 49.4 Funding 突然转负

ACTIVE Plan：

```text
WARN / EXIT_RECOMMENDED
```

绝不自动关闭。

## 49.5 Mark 接近强平价

产生 alert，不执行交易。

## 49.6 One Leg Closed

立即：

```text
ORPHAN_SPOT_LEG
```

或：

```text
ORPHAN_FUTURES_LEG
```

## 49.7 Browser Closed

仅当独立后端进程仍运行时继续监控；应用退出、电脑睡眠或断网期间无法监控或可靠发送浏览器通知。重启恢复账本并补抓历史，但不补发“曾实时观察”的告警。UI显示监控心跳、最后成功采样时间和未覆盖时段。浏览器通知仅作辅助，止损保护应由用户在交易平台配置。

---

# 50. 性能约束

新增功能不能拖慢原 Scanner。

要求：

- Opportunity refresh 后台化；
- Active 高频 monitor 只针对 active symbols；
- quote cache；
- 同 symbol/venue 请求去重；
- Alpha token/exchange info 全局缓存；
- Onchain 独立 rate limiter；
- 不在基础 500-universe refresh 中做全量 depth/onchain quote。

推荐：

```text
all universe
  ↓
cheap funding prefilter
  ↓
FCS shortlist
  ↓
deep venue quote
```

---

# 51. 安全与隐私

项目不得存：

```text
Binance API Secret
Wallet private key
Seed phrase
Wallet signature
```

用户手工录入：

```text
position qty
entry price
liquidation price
margin
```

只保存在本地 DuckDB。

API 默认 localhost。

日志不得打印 secret。

如果输入字段中检测到疑似私钥/助记词：

```text
reject + do not persist
```

---

# 52. 降级原则

任意 Provider 失败：

```text
原 Scanner 正常
LTSS 正常（原数据正常时）
FCS 对应数据 unavailable
对应 Hedge Venue unavailable
ACTIVE Monitor 显示 degraded
```

绝不：

```text
mock live fallback
0 值伪装
默认“安全”
默认“可卖”
```

---

# 53. READY 定义

## 53.1 Funding Opportunity READY

至少：

```text
FCS != null
Funding Entry Gate pass
HedgeDQ >= 80
Identity VERIFIED/HIGH
Multiplier verified when required
At least one two-sided Spot Venue
Futures contract TRADING
No confirmed delisting
No Hedge-specific BLOCK
```

## 53.2 Hedge Simulation READY

还需：

```text
valid legal quantities
ratio valid
spot exit feasible
cost calculable
break-even calculable
critical data fresh
plan safety >= 80
verified fresh USER_EXCHANGE liquidation price for isolated mode
```

无可核对强平价/CROSSED/平台止损不支持仍允许DRAFT规划和实际持仓登记，但simulation readiness=NOT_READY、monitoring capability=LIMITED；不得用Plan Safety总分掩盖关键未知。

## 53.3 Active Plan NORMAL

`NORMAL` 不是安全保证，只表示：

```text
当前 monitor 未触发 CRITICAL，recommended_action不要求PAIR_EXIT/URGENT_PAIR_EXIT
```

---

# 54. Definition of Done

以下全部满足才算本次升级完成：

1. Tasks 0–18 既有测试全部保持通过；
2. 001–003 migration 不修改；
3. 004 migration 可从现有数据库安全升级；
4. FCS deterministic；
5. FCS 与 LTSS/Entry 完全独立；
6. ABSOLUTE / RELATIVE 数量与倍率正确；
7. Binance Spot / Alpha 使用统一 canonical identity；
8. Onchain 不执行交易；
9. Onchain ABSOLUTE 必须验证 buy+sell 两方向；
10. Roundtrip cost 包含 fee/slippage/gas；
11. Break-even 可复算；
12. Planner 只给指导，不下单；
13. Actual Fill 与 Target 分离；
14. ACTIVE Monitor 使用 Actual Fill；
15. Funding按已结算事件和当时手工持仓估算，实际到账独立登记；
16. projected funding 单独展示；
17. liquidation price 来源明确；
18. paired exit 指引完整；
19. orphan-leg alert 可触发；
20. Funding negative / weakening alert 可触发；
21. Basis adverse alert 可触发；
22. Spot exit liquidity alert 可触发；
23. 浏览器关闭后 ACTIVE Plan 仍由 Backend 监控；
24. React 不直连第三方；
25. Provider 失败不影响原 `/api/scan`；
26. live failure 不 fallback mock；
27. API/UI 无“无风险/无损/保证收益”文案；
28. `desktop/ui/dist` 重建；
29. PyInstaller package smoke 通过；
30. README/API/用户手册/测试文档更新。

---

# 55. 最终架构

```text
                           Existing short-lab
                                  |
                 +----------------+----------------+
                 |                                 |
           Directional Short                 Funding / Hedge
                 |                                 |
          LTSS + Entry                        Funding History
                 |                                 |
                 |                         Funding Capture Score
                 |                                 |
                 |                         Spot Venue Resolver
                 |                    +------------+------------+
                 |                    |            |            |
                 |                Binance Spot   Alpha       Onchain
                 |                    |            |            |
                 |                    +------------+------------+
                 |                                 |
                 |                           Hedge Planner
                 |                         /               \
                 |                  ABSOLUTE              RELATIVE
                 |                   h=1.0                 0<h<1
                 |                         \               /
                 |                          Stress / Cost
                 |                               |
                 |                         Manual Guide
                 |                               |
                 +-------------------------------+
                                                 |
                                        User enters fills
                                                 |
                                          Active Monitor
                            +----------+----------+----------+
                            |          |          |          |
                         Funding     Basis      Liq       Liquidity
                            |          |          |          |
                            +----------+----------+----------+
                                                 |
                                               Alerts
```

---

# 56. 最终产品语义

升级后的 Short-Lab 分别回答：

### Directional Short

```text
这个币长期值得空吗？
现在适合裸空吗？
```

### Funding Capture

```text
正 Funding 是否有持续性？
现货是否能建立并可靠退出？
成本多久能通过 Funding 回本？
```

### Hedged Short

```text
如果仍然看空，应该买多少现货降低暴涨风险？
在 +50% / +100% / +200% 压力下风险是多少？
```

### Active Hedge Monitor

```text
两腿还匹配吗？
Funding 还值得继续拿吗？
Basis 是否在吞掉 Carry？
Futures 离强平还有多远？
Spot 现在还能否合理退出？
是否应成对退出？
```

---

# 附录 A：第三方开发硬约束清单

开发前必须逐项确认：

- [ ] 本次是 Tasks 19+，不重做 0–18；
- [ ] 单 FastAPI；
- [ ] 单 DuckDB；
- [ ] Android 不修改；
- [ ] 不自动交易；
- [ ] 不读用户账户；
- [ ] 不使用 private trading API；
- [ ] 不保存钱包私钥；
- [ ] Funding 数据复用；
- [ ] Spot 数据复用；
- [ ] AssetIdentity 复用；
- [ ] multiplier 不猜测；
- [ ] FCS 独立；
- [ ] Plan Safety 独立；
- [ ] ABSOLUTE 不等于“无风险”；
- [ ] RELATIVE 必须显示 residual short；
- [ ] Actual Fill 与 Target 分离；
- [ ] Onchain 必须双向 quote；
- [ ] Funding估算只用settled与当时持仓，实际到账单列；
- [ ] Predicted Funding 不计 accrued；
- [ ] Liq 来源明确；
- [ ] paired exit；
- [ ] orphan-leg alert；
- [ ] Provider failure 不击穿旧系统；
- [ ] 001–003 不修改；
- [ ] 004 只能 additive migration；
- [ ] React 只调用 Backend；
- [ ] live fail 不切 mock。

---

# 附录 B：建议测试文件

```text
desktop/backend/tests/

test_shortlab_hedge_models.py
test_shortlab_hedge_config.py
test_shortlab_hedge_repository.py

test_shortlab_binance_alpha.py
test_shortlab_hedge_venues.py
test_shortlab_onchain_quote.py

test_shortlab_fcs.py
test_shortlab_funding_opportunity.py

test_shortlab_hedge_planner.py
test_shortlab_hedge_simulator.py

test_shortlab_hedge_plan.py
test_shortlab_hedge_monitor.py
test_shortlab_hedge_alerts.py

test_shortlab_hedge_api.py
test_shortlab_hedge_evidence.py
```

---

# 附录 C：建议新增版本标识

```text
FCS_VERSION = "fcs_v1"
HEDGE_FORMULA_VERSION = "hedge_v1"
HEDGE_EVIDENCE_VERSION = "hedge_evidence_v1"
```

影响历史可重算性的数学改变必须升级版本。

---

# 附录 D：明确不在本次范围内

- 自动交易执行；
- Binance 账户同步；
- 钱包连接；
- 自动签名；
- 自动补保证金；
- 自动 liquidation rescue；
- 跨交易所做市；
- 借币/融币；
- 现货做空；
- 税务；
- CeFi 交易所信用风险模型；
- 链上 MEV 精确预测；
- 保证收益。

未来如扩展，必须单独立项，不得静默塞入 Hedge Advisor。


---

# 附录 E：公开资料与验证边界

- [Binance资金费率规则](https://www.binance.com/en/support/faq/detail/360033525031)：正费率通常由多方向空方支付，结算周期可调整，结算边界与持仓保证金会影响实际收入和清算风险。
- [Binance USDⓈ-M公共行情](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)：Funding history、Funding info、mark、盘口与交易规则作为adapter合同来源；历史事件与当前期观察分别保存。
- [Binance Alpha公共行情](https://developers.binance.com/en/docs/catalog/advanced-trading-alpha-trading/api/rest-api/market-data)：用于验证公共现货市场数据能力；不包含用户可交易权限或持仓证明。

核验日期2026-10-02。上述来源用于机制和API能力核对，本方案没有实网测得成交成功率或资金费套利收益。验收需保存固定fixture和网络联通证据；真实手工订单与成交由用户在平台核对。
