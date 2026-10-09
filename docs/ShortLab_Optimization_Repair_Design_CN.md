# short-lab 优化修复设计方案

交付日期：2026-10-09

适用基线：`edd81adcc2cc1436c33f86db1b2d5a8bd21e3ad5`，桌面端

配套计划：[优化修复实施计划](ShortLab_Optimization_Repair_Implementation_Plan_CN.md)

## D01. 文档职责、目标与交付边界

本方案是当前优化修复的实施合同；配套计划规定任务与文件归属。本方案定义行为、模型和验收，计划不能另行定义同名字段、阈值或状态。现有一体化方案仅作为既有系统背景；本次改动按本方案执行。代码位置以函数/类定位，不依赖审查报告行号。

目标：修复数据、风险、账本、查询和提醒的确定缺陷，增加可解释的对冲建议，使用户能从 Meme 候选进入同币规划、手工记账、监控和双边退出指导，并建立可比较的策略证据。

交付范围：Python/FastAPI/DuckDB 桌面服务、React 桌面页面、测试、桌面打包资源和使用文档。继续使用产品名 `short-lab`、端口 46408、现有 Python 包 `diveintocrypto_desktop` 和兼容 CLI `dive-desktop`。

禁止事项：自动下单/撤单/转账、私钥或助记词、为实现提醒而增加交易账户权限、Android 修改、当前数据回填历史决策、评分转成未经校准的概率、承诺无损或永不强平。本次不把 Alpha/链上 indicative 数据提升为可执行能力。

审查背景：[项目功能与代码审查](ShortLab_Project_Function_Review_CN.md)。本文覆盖其中 S01–S09、H01–H08、M01–M07、E01–E07 及默认能力/数据预算事项。问题编号用于追溯，不是执行任务编号。

### D01.1 推荐实现路线

采用“修复现有生产链 + 独立纯决策模块 + 统一接线”的路线。保持现有模块分工，提取共享单位/经济性函数，不重写扫描器、不更换数据库。

仅修门槛不能完成对冲判断；另建完整扫描系统会重复身份、行情和账本。所选路线用既有 LTSS/Entry/FCS 作为冻结输入，用新增决策层选择规则建议，用现有 Planner 检查实际数量与执行可行性。

### D01.2 能力声明

所有建议初始标 `RULE_BASED_UNVALIDATED`。这是规则建议，不是收益概率。默认正资金费门槛维持开启；Funding/Hedge 开关维持默认关闭，UI 必须说明未启用。用户在配置中明确启用后才能启动对应任务；开关关闭时仍可读取已有档案，不提供新建议或新模拟。

## D02. 架构与文件约束

包根：`desktop/backend/src/diveintocrypto_desktop/`，本文 `shortlab/...`、`data/...`、`engine/...` 均相对于该根。

```text
Provider/缓存 → Observed + 持久来源记录 → IdentitySnapshot
 → 冻结 DirectionalInput → LTSS/Entry/DQ/Risk
 → 冻结 FundingContext → Gate/FCS/成本/机会
 → DecisionContext + 用户目标/期限/预算 → DecisionResult
 → 双腿 Planner/Simulation → DRAFT → 手工事件/保护确认 → ACTIVE
 → Ledger PnL + 当前退出报价 → Monitor/Alerts/PairExitGuidance
 → 同时点策略样本/历史归档 → Outcome → 分桶 Metrics
```

新增模块职责：

| 文件 | 唯一职责 |
|---|---|
| `shortlab/repair_ports.py` | 冻结协作者/Repository/Market协议，运行时真实绑定与独立开发测试缝 |
| `shortlab/repair_contracts.py` | 本方案新增冻结 DTO、枚举、JSON 校验/版本；复用既有类型 |
| `shortlab/funding_schedule.py` | 结算槽位与覆盖率，供做空 Carry、FCS、Grader 共用 |
| `shortlab/hedge/entry_gate.py` | 资金费入场硬门槛 |
| `shortlab/hedge/units.py` | Native/Canonical/FX 与价格、数量合法化 |
| `shortlab/hedge/economics.py` | 期限净 Carry、成本、场景资金需求 |
| `shortlab/hedge/protection.py` | 平台能力与人工保护确认 |
| `shortlab/hedge/decision.py` | 联合对冲规则建议，不访问网络/数据库 |
| `shortlab/hedge/pnl.py` | 事件账本的成本基础、已实现盈亏与费用 |
| `shortlab/hedge/exit_guidance.py` | 当前剩余数量的双边退出指导 |
| `shortlab/hedge/projection.py` | 当前机会、能力和经济字段投射 |
| `shortlab/evidence/capture.py` | 策略采样、精确数量报价任务和到期采集 |
| `shortlab/evidence/evaluation.py` | 状态/版本/基线对比与覆盖率统计 |
| `api/shortlab_repair.py` | 新增 API 路由；现有路由按本文增补 |

已有 `Observed/ObservationMeta`、`AssetIdentity`、`HedgeEvent/HedgePosition`、Planner Request/Result 必须复用。不得新增同义 DataObservation/Identity 类来绕开现有类型。核心 `service.py` 只编排，新增计算放上述纯模块。

## D03. 通用数据、时间、金额与状态合同

### D03.1 序列化

内部 snake_case，HTTP camelCase；时间 UTC 毫秒整数。价格、数量、比例、费率、金额、FX 采用有限 Decimal 字符串，禁止 bool、NaN、Infinity。评分和统计显示值可以 float，但账户余额、成本基础、舍入和风险比较不得通过 float/DOUBLE 回读计算。

金额字符串使用十进制定点、不带指数，删除无意义小数尾零，`-0` 输出 `0`。任意 null 必须在相应 `reasons` 或 `unknown_components` 中说明；不可把缺失当零、无风险或没有费用。JSON对象禁止重复键，snake/camel同义字段也不得同时出现。R00提供load_json_strict和严格YAML Loader；R10对变更API读取原始body并严格解析后再构造DTO，不能等普通JSON解析已丢重复键后才检查。

### D03.2 来源时间

复用 `Observed[T]`/`ObservationMeta`：`source_as_of_ms` 是源数据时点，`fetched_at_ms=known_at_ms` 是该响应完成时的本地接收时点；来源无时间仍保留 null，不能补本地时钟。缓存命中返回原始时间。

每次决策先采集，再固定 `as_of_ms`；参与决策的记录必须 `known_at_ms <= as_of_ms`。源时钟最多允许比接收时间快 2 秒；已知时间晚于决策不允许容差。新鲜度使用来源时点；若接口无源时点，只能标 `RECEIPT_ONLY` 并用于明确允许此口径的规则快照，不能代替实时行情时点。

历史回填收到的数据只在接收后可用于新决策，不得认为过去已知。重启读取档案保留首次接收时间。相同原始数据被重新请求可建立新 receipt，但不能覆盖旧 receipt；历史回放按当时 receipt 查询。

### D03.3 统一三态 Gate

新增 `GateResult(status, reasons, checked_at_ms, input_refs)`：`status=PASS/FAIL/UNKNOWN`；`reasons` 为排序去重机器码数组，`input_refs` 为角色→快照 ID。FAIL 是明确不满足，UNKNOWN 是不能核验，两者都不能当 PASS。

新增 `ReadinessBreakdown`：

| 字段 | 类型/含义 |
|---|---|
| `data_complete` | bool，必需输入均可核验 |
| `funding_gate` | GateResult |
| `execution_gate` | GateResult，公开交易状态、规则、报价及数量可行 |
| `economic_gate` | GateResult，期限净 Carry 满足目标 |
| `protection_status` | `CONFIRMED/PENDING/UNSUPPORTED/UNKNOWN/EXPIRED` |
| `readiness` | 兼容既有 `READY/NOT_READY/BLOCKED` |

Readiness：明确退市/不交易/身份 LOW 或 UNRESOLVED 为 BLOCKED；其余必需 Gate 非 PASS 为 NOT_READY；必需 Gate 全 PASS 为 READY。机会 READY 的执行含义是公开市场条件，不证明用户挂单；模拟 READY 可以保存 DRAFT。ACTIVE 另要求 D12 的人工保护确认。Carry机会、CARRY_CAPTURE和BALANCED模拟要求经济Gate PASS；DIRECTIONAL_SHORT的经济Gate只报告Carry是否回本，不作为必需Gate，但成本未知仍使执行/风险Gate UNKNOWN。手工ABSOLUTE默认goal=CARRY_CAPTURE，RELATIVE默认goal=BALANCED，新增goal字段可明确选择DIRECTIONAL_SHORT；ABSOLUTE只能CARRY_CAPTURE。旧模拟无goal按此推导只读解释，不能重写历史。

MID/MEDIUM 身份可显示与研究，执行 Gate UNKNOWN，不能 READY。HIGH/VERIFIED 还需明确倍率来源与同资产绑定。

## D04. 身份、分类与做空链修复

### D04.1 统一身份入口

新增 `resolve_asset_context(symbol, exchange_meta, catalog_candidates, overrides) -> AssetIdentity`，由 `identity/resolver.py` 实现；保留 `resolve_identity` 为兼容包装。Service 默认候选从注入 Catalog 获取，只有显式 `_identity_candidates_fn` 才覆盖默认来源；做空与 Hedge 使用同一次解析结果并保存 `sl_identity_snapshot`。

优先级：人工 override > 核验目录 > 已核对 chain/address > 唯一符号候选 > 未解决。倍率只有 EXCHANGE/MANUAL 有效；相同可信来源冲突为 UNKNOWN 并记录 `IDENTITY_UNIT_CONFLICT`，不静默选一个。核验目录中 `contract_multiplier`/`multiplier_source` 必须进入解析结果。禁止直接用目录第一条创建 Hedge Identity。

保留 `AssetIdentity.mapping_confidence` 为唯一字段；Hedge 边界适配成已有 `identity_confidence`，保存时两者不得冲突。倍率计算转换为 Decimal 字符串，不通过 float 运算产生金额。

### D04.2 Profile

`asset_overrides.yaml` 增加可选 `profile`，枚举 `MEME/GENERAL/LOW_FLOAT_VC`；不包含 LITE/FULL 后缀，后缀由 analysis_tier 决定。Override 校验接受该字段。优先级为人工 Profile > 已核验类别 > 现有自动分类规则。已知 Meme 类别不能因 MC/ATH 暂时缺失变为 GENERAL，缺数据只改变 DQ。Profile 原因和 mapping_version 写入快照。

### D04.3 价格/OI/风险

- Universe Ticker 保留源 `closeTime` 与响应 receipt；实时 Mark 另用 Mark Provider，禁止把滚动 Ticker Volume 当完整 UTC 日成交量。
- 24h Breakout 使用同一冻结 Ticker 涨幅；缺失显示 `RISK_INPUT_UNVERIFIED`，execution NOT_READY。
- 7D 价格比较最近已收盘 UTC 日 D 与 D−7 的收盘价；OI 比较两个对应 close 时刻，允许每端向前取最近 5min 内已知样本，不用未来样本。范围为 7×24h，不从 D−7 的开盘扩大为 8D。
- Squeeze 输入冻结 price/OI/Taker/Micro 与来源引用；已有条件的数值保留，不能用 Entry 总分代替 Micro。Micro 确认晚于截止时点不参与。当价格/OI均达到Squeeze前置阈值而确认未知时execution NOT_READY，理由 `SQUEEZE_CHECK_UNVERIFIED`；若已知价格/OI明确不满足前置条件，不强求额外确认。前置输入自身未知则风险UNKNOWN。
- 交易量特征采用已收盘 UTC 日 qv，价格/现货量采用共同完整窗口；缺日不补零。
- WATCH/CANDIDATE/READY 所有阈值消费配置；验证 `0<=watch<=candidate<=ready<=100`。
- 共识冲突比例仍为 minority/(buy+sell)，严重冲突默认阈值改 0.4，合法范围 `(0,0.5]`；达到阈值输出中性方向及置信度 0，严重冲突理由保存。没有双边票时比例 0；所有票缺失为 UNKNOWN。

## D05. Funding 历史、结算间隔与入场门槛

### D05.1 来源和归档

保留 `sl_funding_event` 与 `sl_funding_observation`；读取事件必须关联原 observation receipt，不能仅查数值事件。新增 Repository `list_funding_observations(symbol,start_ms,end_ms,known_by_ms)`，按 event time 和 known time 选择当时最新合法 receipt。

新增 `FundingScheduleSegment`：`schedule_id,symbol,effective_from_ms,effective_to_ms,interval_hours,anchor_ms,known_at_ms,source,evidence_ref,verification`，verification 为 `CONFIRMED/INFERRED/UNKNOWN`。只有 CONFIRMED 可授完整覆盖。当前 fundingInfo 只返回发生调整的symbol，缺某symbol不能推断历史间隔。成功完整响应+已归档官方当前默认制度可确认当前未调整symbol的8h制度，但须同时满足：完整HTTP200且业务成功、symbol为TRADING且该symbol不在调整列表、所引用官方当前默认制度明确为8h且已记录版本/receipt。缺任一谓词为UNKNOWN；不能以“列表没它”单独认定8h。生效起点仍是观察边界；不完整/错误响应保持UNKNOWN。当前 fundingInfo 没有可信历史生效时点时，effective_from 使用本次已观察生效边界，不能向更早历史延伸；孤立事件间距不证明历史结算制度。官方来源/经核验历史档案可导入，但必须记录真实 receipt。没有可验证旧制度档案时标HISTORY_BOOTSTRAPPING，展示历史统计但不授完整入场Gate；持续记录足够30D窗口后才可能满足30D门槛，不能承诺安装后立即有READY。上市年龄优先exchangeInfo.onboardDate并记录来源，缺字段需核验档案，否则未知；first_seen只表示本系统首次观察。

### D05.2 覆盖率和收益

同symbol/同effective_from的制度修订按known_at<=cutoff中最新known_at、schedule_id ASC选择；修订使用新schedule_id，不修改旧记录。制度段重叠或生效边界无法核验时，相应区间为UNKNOWN而非重复生成槽位。

统计窗口 `(start,end]`，start/end 可以是决策时点，**不强制 UTC 日对齐**；它与日线成交量的完整 UTC 日窗口属于不同合同。每个确认段生成 `anchor + k×interval` 的预期槽位；段边界采用 `[effective_from,effective_to)`，预期时点只分配一次。

`expected_count` 为所有已核验槽位数；`received_count` 为有合法率/时间、去重且在容差 ±60s 内匹配的槽位数。一个事件只能匹配一槽。`coverage_fraction=received/expected`，窗口任意部分 schedule 未知或 expected=0 时为 null，附 `FUNDING_SCHEDULE_UNKNOWN`；另输出 `schedule_coverage_fraction=确认段时长/窗口时长`。首尾缺事件同样降低 received_count。

历史事件属于不可变档案，不因receipt年龄而全部失效；用窗口槽位完整性、来源可追溯性及近期interval/current观测证明统计可用，不能用数据库读取时间重新授实时新鲜度。实时资金费仍按120s门槛。缺 mark 的 Funding 事件可参与费率统计，不可参与美元 Carry；美元 Carry 另输出 `priced_event_coverage`。统计按实际事件 sum(rate)；std 采用已有 8h 归一化方法，但不据此假设实际 8h 结算。90D 历史按上市年龄：>=90D FULL_90D，30–89D PARTIAL_90D，<30D INSUFFICIENT。PARTIAL不填造90D：90D Gate为N/A，FCS90D正值比例子项用既有not_applicable_score=0且记录available_score，不能因此让整个FCS为null，也不把缩短历史冒充完整90D；年轻币保守收益使用既有方法并在conservative_method注明；未知上市时点为 HISTORY_CLASS_UNKNOWN，不用 first_seen 当上市日。

### D05.3 共用入场 Gate

`evaluate_funding_entry_gate(context: FundingContext, policy: Mapping, as_of_ms: int) -> GateResult`。

消费所有已有 entry_gate：current/last settled 严格 >0（开关 false 只取消对应条件）；7D/30D rate sum >=配置 min；positiveRatio30D/90D 与 coverage30D/90D >=门槛。当前 rate 超 120s 不可 PASS；上次结算不是“当前值”，要验证其来自最近预期已结算槽位，允许 60s 时间匹配，不用 120s TTL 判断其费率时点。

缺失、未知 schedule/历史类型、所需比率未知返回 UNKNOWN；任何明确失败返回 FAIL。PARTIAL_90D 不执行 90D 数值门槛，记录 N/A 原因并继续执行 30D 门槛。机会、Planner、Decision 调用同一函数，不能各自复制逻辑。FCS 的 100 分权重保留；改变门槛不通过扣分替代拒绝。

固定理由包括 `FUNDING_CURRENT_NON_POSITIVE/FUNDING_LAST_NON_POSITIVE/FUNDING_CURRENT_STALE/FUNDING_HISTORY_BELOW_MIN/FUNDING_POSITIVE_RATIO_LOW/FUNDING_COVERAGE_LOW/FUNDING_SCHEDULE_UNKNOWN/HISTORY_CLASS_UNKNOWN`。

## D06. 单位、强平、经济性与保护能力

### D06.1 单位

`P_canonical_usd=P_native×FX/m`；`Q_canonical=Q_contract×m`；合约 Notional=`P_native×Q_contract×FX`。Quote Volume/OI Notional 不再次按 m 缩放。Spot 数量是对应资产的净数量。

请求 liquidation_price、stop_trigger_price 均为交易所合约原生 quote 价格。强平距离=`(L_native−Mark_native)/Mark_native`；`L<=Mark` 为已越界/输入错误。每个场景原生压力价与 L 比较，达到或超过强平价为 `INVALID_AFTER_LIQUIDATION`，终值净 PnL 为 null，不计算“强平后继续持有收益”。

默认 STOP 参考中点 `(Mark+L)/2`；使用 STOP_MARKET 原生 Tick，BUY stop 向上取整，必须 `Mark<Stop<L`。tick 太粗无合法值返回 `STOP_PRICE_UNREPRESENTABLE`。这是参考指导，不认定未来必成交。

### D06.2 经济性

`evaluate_economics(proposal, funding_context, hold_days, cost_policy) -> EconomicsResult`，hold_days 1..365；新建议必须提供，旧手工模拟未提供则 UNKNOWN/NOT_READY。机会用 reference_hold_days=30。

`conservative_carry=N_actual×APR_conservative×days/365`。
`roundtrip_cost=entry_fees+exit_fees+explicit_slippage+gas`。
`net_carry=conservative_carry−roundtrip_cost`。
`break_even_days=roundtrip_cost/(N_actual×APR/365)`，APR<=0 时 null/`NON_POSITIVE_CARRY`。

数量舍入后重算实际 N；当前价估退出成本明确 `CURRENT_PRICE_REFERENCE`。场景费用和历史结算采用该场景/真实退出金额；VWAP 已含价格冲击，不再加一遍冲击成本。新增SpotVenueQuote/OnchainQuote字段 `fees_included: bool | None`（旧数据默认null），配置手续费率是POLICY_ESTIMATE，不证明用户实际账户费率，报告标估算；费用是否已含在 Quote 中由 `fees_included` 明确；缺字段为未知，不能默认免费。资金费和方向 PnL 分开展示。

经济PASS要求net_carry>optimization.decision.min_net_carry_usd（默认0，允许配置>=0），等于门槛为FAIL；成本/FX未知为UNKNOWN。资本=`spot_cash+futures_margin+entry_cost+reserve`，`reserve=(spot_cash+futures_margin)×capital_reserve_fraction`；不把合约未实现盈利当可立即调用保证金。

### D06.3 保护

`resolve_stop_capability(rules) -> SUPPORTED/UNSUPPORTED/UNKNOWN`，必须显式规则证据；用户选 stop_policy 不改变平台能力。模拟只展示能力/建议，不宣称已有保护单。合约STOP能力UNKNOWN/UNSUPPORTED使execution Gate非PASS；现货支持人工退出预案时仍可LIMITED，不提升FULL。

人工保护确认分两腿：合约 STOP/数量/触发基准/平台回执引用；现货用户的配对退出预案/剩余数量/平台能力。默认 24h 后 EXPIRED；计划剩余数量、强平/触发参数或保护规则变化使确认失效；普通状态变更造成的版本递增不单独使确认失效。确认JSON保存 `protected_position_hash`（两腿剩余量、强平/STOP参数、规则ID的canonical hash），激活/监控用该hash核验。确认来源为 `USER_CONFIRMED`，不是 PRIVATE_API_VERIFIED。若平台不支持独立现货条件单，可记录人工退出预案，状态 `MANUAL_EXIT_ONLY`，ACTIVE 允许但 monitoring LIMITED，必须持续告警；未知平台能力需先明确人工预案，不能提升 FULL。

## D07. 联合对冲决策

### D07.1 请求与冻结上下文

`DecisionRequest` 必需字段：symbol、goal、futures_notional_usd、planned_hold_days、available_capital_usd、max_scenario_loss_usd、margin_usd、liquidation_price、liquidation_price_updated_at_ms；可选 preferred_spot_venue=AUTO。goal 枚举 `CARRY_CAPTURE/DIRECTIONAL_SHORT/BALANCED`。金额 >0，hold 1..365；强平价须在24h内用户更新，来源写 `USER_ENTERED`。

`DecisionContext`：identity_snapshot_id、directional_score_id（Carry 可 null）、fcs_snapshot_id、funding_context、futures_mark、futures_quote、futures_rules、venue_quotes、source_refs、as_of_ms。由 Service 获取，不允许 API 客户端自报 LTSS/FCS/FX/数据质量。方向目标须 MEME Profile、LTSS>=ready_ltss、Entry>=ready_entry、DQ>=ready_data_quality、Tradeability>=ready_tradeability_score、ExecutionStatus READY，且无 BLOCK/PAUSE。Carry不要求方向性LTSS，但交易状态、身份、正资金费及new_token_days=45上市年龄暂停等公共门槛仍需通过。未知上市时点不能用first_seen替代；30–44天可计算PARTIAL历史，但新币暂停仍阻止入场。

### D07.2 候选与选择

`recommend_hedge(request,context,policy,*,ports=None) -> DecisionResult`；内部只计算，不抓网络。候选 h=`0,0.25,0.5,0.75,1`，以合约合法数量为基准生成净 Spot 数量并舍入；实际 h 与目标差异绝对值 >0.02 时淘汰，不能展示理论 h 冒充实际值。

Carry 只比较 h=1；Directional 比较0..1，目标为保留最多负方向敞口，选择满足全部硬约束的最小目标h，并明确输出其合法化后实际h；Balanced 比较0.25..1，要求净 Carry>0，仍选最小可行目标h并报告实际h。实际比例偏差定义为abs(actual_ratio-target_ratio)>0.02（绝对比例差，不是相对误差）；同目标h多Venue按economics.roundtrip_cost_usd升序、所用全部Quote最早expires_at_ms降序、venue枚举字母升序决胜，金额一律Decimal比较；成本未知不进入可执行选择。不存在“预测收益最大化”，因为没有经验证价格概率。

全部候选检查：资金费 Gate、身份/时效、资本、合法数量、反向容量、压力损失预算、强平。以下默认场景均需覆盖（futures/spot move 为相对各自初始价格；FX shock 对两腿 quote FX 施加）：

| 场景 ID | Futures move | Spot move | FX shock |
|---|---:|---:|---:|
| UP_50 | +0.50 | +0.50 | 0 |
| UP_100 | +1.00 | +1.00 | 0 |
| DOWN_50 | -0.50 | -0.50 | 0 |
| BASIS_UP | +0.20 | +0.10 | 0 |
| BASIS_DOWN | -0.10 | -0.20 | 0 |
| FX_DOWN | 0 | 0 | -0.03 |

每个场景用实际数量与该场景退出金额算费用；额外退出折价 100bps（Spot sell 减价，Futures buy 加价），Carry 压力信用为0，不把未来正资金费兜底风险。未来成交容量不可证明时为 scenario assumption，报告明示；当前退出容量仍必须足够。任一场景达强平：候选风险未通过，Decision 不能自动假设止损成交解决。可展示 MANUAL_REVIEW 与缩小金额/增加保证金/人工重新检查动作，但不输出可执行比例。这会有意拒绝部分高杠杆方案，不能为了增加 READY 率删除 UP_100。

### D07.3 输出

持久化DTO序列化使用R00的 `to_record_dict` 注入repair-contract-v1，反序列化先验证顶层版本再构造DTO；HTTP输出额外contractSchemaVersion，不允许其覆盖字段。

`DecisionResult`：decision_id、generated_at_ms、expires_at_ms、request、context_refs、recommendation、selected_proposal、alternatives、reasons、assumptions、validation_level、decision_policy_hash、formula_version。

recommendation 枚举 `DATA_INSUFFICIENT/MANUAL_REVIEW/AVOID/NO_HEDGE/PARTIAL_HEDGE/FULL_HEDGE`：公共必需输入未知优先 DATA_INSUFFICIENT；明确公共禁止或无资本/成本可行候选为 AVOID；候选仅因强平/保护待人工处理而失败为 MANUAL_REVIEW；其余由选中实际比例确定。h=1需在2%偏差内，h=0无Spot，其他为PARTIAL。

`NO_HEDGE` 只提供只读合约指导并保存 Decision，不创建两腿 Hedge Plan；POST plans 收到该 decision_id 返回422 `UNHEDGED_PLAN_UNSUPPORTED`。裸空历史策略仍可独立评分。不得制造现货腿来绕过账本规则。

Decision 有效期=min(生成+20s、所用当前 Mark/Quote 到期)；过去 score/FCS snapshot 的指标寿命按既有 DQ 检查，旧机会不得获得当前费率信用。公共必需输入不包括Spot：h=0只要求合约数据；其他比例的Spot缺失逐候选淘汰/未知，不能阻止本来可行的NO_HEDGE。重新模拟强制重新读取当前报价和 Gate；旧 Decision 只读可审计。

## D08. Funding 机会与查询

新增 `OpportunityQuery`：symbol、venue、readiness、min_fcs、min_funding_30d、min_positive_ratio_30d、sort、order、limit、offset、include_stale。保留已有别名；limit1..200默认50，offset>=0；sort接受fcs/funding30d/breakEvenDays/positiveRatio30d；order asc/desc；include_stale默认false。

当前机会定义为 **每 futures_symbol 最新一条**，按 `(as_of_ms DESC,created_at_ms DESC,snapshot_id ASC)` 决胜，不在选最新前过滤 READY。SQL 先ROW_NUMBER选最新，再解析 typed projection/filter/order/page；total为同一读事务过滤后数量，不能先截断历史200行。排序 null最后，再symbol ASC、snapshot_id ASC。历史查询保持独立，不能为更新失败回退旧READY。

持久化 `risk_json.projection_v2` 使用snake_case：snapshot_id,symbol,canonical_id,as_of_ms,expires_at_ms,stale,fcs,fcs_config_hash,funding_7d,funding_30d,positive_ratio_30d,history_class,best_venue,break_even_days,conservative_apr,readiness_breakdown,reasons。仓库返回该内部形状；API显式将as_of_ms→asOf、expires_at_ms→expiresAt，其余按camel转换。

HTTP `FundingOpportunity` 必含 snapshotId、symbol、canonicalId、asOf、expiresAt、stale、fcs、fcsConfigHash、funding7d、funding30d、positiveRatio30d、historyClass、bestVenue、breakEvenDays、conservativeApr、readinessBreakdown、reasons。全部金额/率按D03。expiresAt用当前Funding/Mark/Quote最早到期；后台机会1800s刷新不是实时入场保证。列表旧结果可标 STALE，点击建议/模拟时刷新当前 Gate和报价。

机会列表stale条件为as_of_ms>=expires_at_ms，边界包含到期，无grace；grace仅用于Monitor旧值展示，不授执行信用。include_stale=true返回过期项但readiness=NOT_READY；顶部 asOf 是结果中最早 source cutoff，空结果null，不填“现在”冒充源时间。failureReason说明当前刷新失败。

## D09. Decimal 账本与净收益

复用 `HedgeEvent` v1，不修改旧 event_json。新增事件仍保存十进制字符串；费用/FX证据放新来源表，按 event_id 关联。Correction 沿用既有 reversal/supersede 规则，先解析有效事件再计算，不在 SQL SUM DOUBLE。

`compute_ledger_pnl(events,identity,event_fx,market_context) -> LedgerPnl` 使用每腿移动加权成本：

- Futures 开空增加 native contracts和quote成本；买入平空 realized_quote=`closed_contracts×(avg_entry_native−exit_native)`，USD 用平仓执行时 quote FX，未平部分用当前 FX/Mark计算。Futures以其结算币计合约损益，开仓保证金的独立FX损益不并入合约方向PnL，资本口径另显示。
- Spot 买入将净到帐资产作为quantity。手续费扣Base：总买入支出分摊到净资产成本基础，不再重复扣该Base费；Quote/第三币费用独立成本，USD采用执行时 FX。卖出时Base费会额外消耗库存，必须含在close quantity检查中。
- Spot realized_quote=`sell_gross_proceeds−allocated_entry_cost_basis`；USD realized=`sell_gross_proceeds×exit_FX−allocated_entry_cost_basis_usd`，美元成本基础在每次买入以entry_FX冻结。当前USD unrealized=`remaining_net_qty×current_sell_vwap×current_FX−remaining_cost_basis_usd`。输出native与USD，禁止用退出/当前FX重写历史美元成本。
- fee_amount 或明确 fee_usd 只能计一次；二者同时存在需验证FX一致。费用未知为null/Partial，不补零。Gas同理；0需要用户显式输入。
- FundingReceipt按真实 amount/currency/event FX求和；公开Funding估算另显示，已确认同一public_event_id覆盖估算而不双计；没有关联的手工总额不能随意抵扣某笔估算，展示差异待确认。

`LedgerPnl`：realized_futures_usd、realized_spot_usd、unrealized_futures_usd、unrealized_spot_usd、actual_funding_usd、estimated_unconfirmed_funding_usd、known_cost_usd、estimated_exit_cost_usd、known_net_subtotal_usd、net_before_exit_usd、net_after_exit_usd、unknown_components、coverage、as_of_ms。

funding_basis真值表：任一未确认估算事件参与显示（即使估算金额为0）为ESTIMATED；所有资金费项目都来自有效FundingReceipt且没有未确认估算为ACTUAL_RECEIPTS_ONLY；有未知事件/FX时金额Partial/null，basis仍按前两条来源规则标注，不把未知宣称已实收。未知必要项目时 net=null，known subtotal可见但不能叫完整净收益。退出滑点已包含当前反向VWAP，不重复减；费用另按规则计。当前Monitor用BUY Futures/SELL Spot深度；新入场/Decision还要SELL Futures与BUY Spot。

## D10. 双边退出指导

`build_pair_exit_guidance(plan,positions,market_context,rules,now_ms) -> PairExitGuidance`。只读，不产生交易/平仓事件。

每腿固定字段：leg_type、venue、side、native_qty、qty_currency、price_currency、vwap_native?、notional_usd?、reduce_only?、coverage?、capability（CONFIRMED/PARTIAL/UNKNOWN/NO）、source_refs、reasons。缺报价不隐藏已知剩余数量，能力UNKNOWN并禁止声明可执行。

内容：plan_id、plan_version、generated_at_ms、expires_at_ms、legs、unexecutable_dust、reasons、confirmation_required。Futures 指导 BUY/减空，reduceOnly=true（若平台当前模式需要不同字段只展示人工核对提示）；Spot 指导 SELL。原生数量不得大于真实remaining，按原生Step floor；不可执行Dust单独列明，不能称仓位已清零。每腿包括原生币种、价格参考、规则来源、覆盖率、费/Gas、风险及可用能力。

手动 LIQUIDATION 只关闭合约真实登记数量；Spot剩余触发 URGENT_PAIR_EXIT，给Spot单腿SELL指导。仅Mark触阈值输出 POSSIBLE_LIQUIDATION_UNCONFIRMED，不自动更改数量。平仓按钮仍是账本完成确认，不是平台交易按钮。

## D11. 预算、调度、归档和数据成本

沿用一次真实HTTP发送一次计费，重试也计费；禁止内外层重复重试。复用shortlab/request_budget.py已有frozen RequestContext，保留budget/host/job_type/endpoint_family/trace_id/identity_snapshot_id及原默认值和位置参数顺序；仅末尾追加可选job_id/deadline_ms（默认None），make_request_context同步追加keyword参数。repair_contracts.py只re-export，不另建同名类型。monitor/opportunity/interactive/evidence/backfill仅为本节示例，不是完整枚举；完整允许值与档位以D19.3唯一表为准，未知值拒绝。上下文不可变，不用共享对象可变字段；market不得把所有调用标monitor。

默认Universe500、Shortlist50、EntryTop10、EntryConcurrency2、每轮240实际调用、Funding历史80次/300s保持。Monitor保留20%、Scanner保留30%，Evidence属background；交互不抢monitor保留额度。预算不足返回DEFERRED并保留任务，不能伪造Quote。

监控10s tick，Futures Mark按symbol合并，深度每30s、Funding每60s、监控落盘60s；风险状态变化立即落盘/告警。每tick深Quote最多10个不同symbol，同symbol不同数量逐项计费，在4路并发/8s总deadline内执行；公平rotation按last_served升序再symbol。未及时服务标容量/时效降级，保留数量但不显示实时退出可行。11币的连续轮次必须能服务到尾部，不能固定只取前10。

DuckDB单worker保留队列256、5s持久化等待、aging30s；monitor/alert/ledger优先级0、交互1、score/source/evidence2、backfill3、retention4。超时 `MONITOR_DEGRADED`，不清账、不覆盖已提交事件。队列满HTTP503 `LOCAL_WRITE_BUSY`。

CoinGecko新增account_monthly_limit=10000、local_requests_per_minute=30、reserve_fraction=0.1。可按真实账户上调；预算使用持久月计数，月边界UTC，不重启归零。若计划与限额不符Provider标BUDGET_LIMITED。Fundamentals批量市场接口优先；Supply独立6h TTL；FX60s有效但不强制每分钟逐币拉CoinGecko，复用合法同币种观测。没有足够新FX保持UNKNOWN，不能为省配额猜USDT=1。

## D12. API、人工保护确认与客户端合同

API统一前缀 `/api/short`，既有路径不删除。新增路由由 `api/shortlab_repair.py` 提供并在App注册。

| 方法/路径 | 请求 | 成功 | 关键失败 |
|---|---|---|---|
| POST `/hedge/decisions` | DecisionRequest，金额camelCase字符串 | 201 DecisionResult（AVOID也是合法结果） | 422非法；503功能关闭/DB忙/服务未就绪；单Provider失败返回201 DATA_INSUFFICIENT并记录原因 |
| GET `/hedge/decisions/{id}` | 无 | 200冻结结果，含expired | 404不存在 |
| GET `/hedge/plans/{id}/exit-guidance` | 无 | 200只读指导 | 404；503服务不可用；报价缺失返回200 UNKNOWN指导，旧报价标过期 |
| POST `/hedge/plans/{id}/protection` | expectedVersion、clientRequestId、两腿确认内容 | 200确认记录及新planVersion | 409版本/幂等冲突；422缺字段 |
| GET `/capabilities` | 无 | 200开关、schema、providers、来源时间、降级原因 | 不暴露key/path敏感值 |

既有simulate加可选decisionId，服务重新取当前数据验证其资产/目标/数量和有效期，保存simulation和引用；旧手工请求不带decisionId继续允许，但仍执行新Gate。既有POST plans加可选decisionId；不能用无效/过期建议创建READY计划。保存后初始DRAFT；新计划双腿已成交而尚未显式激活时使用新增状态 `FUNDED_PENDING_ACTIVATION`，不得由aggregate_events自动提升ACTIVE。该状态和所有有剩余仓位的计划都要监控。激活在同一检查cutoff重检六项：解析身份/倍率、当前Mark与原生强平价、两腿精确剩余量的双向深度、共用Funding Gate、实际剩余数量的资本/风险/期限经济计算、protected_position_hash与确认有效期；任一必需未知/失败不得ACTIVE。原入场simulation的20s有效期不作为已成交计划的激活期限，原快照保持入场证据，新的检查保存为ACTIVATION_CHECK Observation并引用。不能信保存时旧READY。现货人工退出预案可允许LIMITED的ACTIVE，清楚显示风险。

activate/close请求必须 `{expectedVersion: integer>=1}`，不再接受空body；close需剩余两腿0或确认Dust处理，版本冲突409 `HEDGE_VERSION_CONFLICT`；不把Dust当实际卖出。幂等冲突409 `HEDGE_IDEMPOTENCY_MISMATCH`，重复同键同内容返回原结果。

Protection请求：expectedVersion、clientRequestId、confirmedAtMs、futures{orderReference,nativeQty,triggerPrice,triggerBasis,status}、spot{exitMode,orderReference?,nativeQty,status}；status CONFIRMED/CANCELLED，exitMode PLATFORM_ORDER/MANUAL_EXIT_ONLY。确认必须对当前两腿剩余量，未来时间拒绝；确认和planVersion递增同事务，记录clientRequestId唯一。

### D12.1 页面流程与安全

Funding页面显式以include_stale=true显示研究列表，旧行显示STALE和不可入场原因；不是默认API无过期项筛选的隐式变更。Funding/候选行“分析对冲”携symbol和snapshotId进入Planner；Carry目标从Funding进入，Directional/Balanced从Meme候选进入。Planner展示目标、期限、资本/预算、原生强平价和建议依据，输入改变使旧建议失效；保存后提供“查看计划/进入监控”。增加计划列表，由已有list_plansAPI读取，不靠localStorage。

Monitor输入ID与loadedPlanId分离；Plan和Monitor使用同ID/版本，request generation+AbortController阻止旧响应。切换立即禁写并清空风险展示；读取失败显示错误，不将A数据冠为B。所有写入用loadedPlanId和服务返回版本。

页面前台10s poll，后台暂停；恢复立即刷新；同一计划最多一个在途请求。超过Mark/Quote TTL显示STALE和sourceAge，超grace风险UNKNOWN；错误保留旧值必须显著标过期。提醒必须包含未对冲腿、数据失效与人工行动，不把ACK当风险解决。

成交表单币种来自冻结腿，支持实际手续费/费用币种/Gas/执行时间；unknown费用勾选为未知，零显式输入。activate/close适配器签名 `(planId,body,opts)`，请求不得丢expectedVersion。

## D13. 数据库存储与迁移

新增 `006_optimization_repair.sql`，既有001–005不修改。schema上限6，ShortLab启用的目标统一6，Funding/Hedge仍按开关判断功能；因此普通启用也顺序创建005的空表，不表示Hedge启用。每个迁移文件独立事务，版本行同事务；失败保持原版本，不宣称部分006可用。既有打包清单必须加入真实006文件。

raw_sha256定义为Provider兼容payload的canonical JSON字节SHA256，用于内容去重/完整性，不宣称交易所原始字节签名；Fixture SHA另按原文件字节计算。

新增以下7表。所有持久化JSON顶层含 `schema_version`，嵌套DTO不重复注入；相关金额仅JSON十进制字符串。使用IF NOT EXISTS，应用层校验引用，无新增DB级外键。

```sql
CREATE TABLE IF NOT EXISTS sl_market_observation (
 observation_id VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL,
 kind VARCHAR NOT NULL, source_as_of_ms BIGINT, known_at_ms BIGINT NOT NULL,
 value_json VARCHAR NOT NULL, meta_json VARCHAR NOT NULL, raw_sha256 VARCHAR NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sl_market_observation_cutoff
 ON sl_market_observation(symbol,kind,known_at_ms,source_as_of_ms);
CREATE TABLE IF NOT EXISTS sl_funding_schedule (
 schedule_id VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL,
 effective_from_ms BIGINT NOT NULL, effective_to_ms BIGINT,
 known_at_ms BIGINT NOT NULL, schedule_json VARCHAR NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sl_funding_schedule_cutoff
 ON sl_funding_schedule(symbol,known_at_ms,effective_from_ms);
CREATE TABLE IF NOT EXISTS sl_fx_observation (
 fx_id VARCHAR PRIMARY KEY, currency VARCHAR NOT NULL,
 source_as_of_ms BIGINT NOT NULL, known_at_ms BIGINT NOT NULL,
 rate_str VARCHAR NOT NULL, source_json VARCHAR NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sl_fx_observation_cutoff
 ON sl_fx_observation(currency,source_as_of_ms,known_at_ms);
CREATE TABLE IF NOT EXISTS sl_hedge_decision_snapshot (
 decision_id VARCHAR PRIMARY KEY, symbol VARCHAR NOT NULL,
 generated_at_ms BIGINT NOT NULL, expires_at_ms BIGINT NOT NULL,
 decision_policy_hash VARCHAR NOT NULL, decision_json VARCHAR NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sl_hedge_decision_symbol
 ON sl_hedge_decision_snapshot(symbol,generated_at_ms,decision_id);
CREATE TABLE IF NOT EXISTS sl_hedge_protection_confirmation (
 confirmation_id VARCHAR PRIMARY KEY, plan_id VARCHAR NOT NULL,
 plan_version BIGINT NOT NULL, client_request_id VARCHAR NOT NULL,
 confirmed_at_ms BIGINT NOT NULL, expires_at_ms BIGINT NOT NULL,
 confirmation_json VARCHAR NOT NULL, UNIQUE(plan_id,client_request_id));
CREATE INDEX IF NOT EXISTS idx_sl_protection_plan
 ON sl_hedge_protection_confirmation(plan_id,plan_version,confirmed_at_ms);
CREATE TABLE IF NOT EXISTS sl_strategy_entry_snapshot (
 entry_id VARCHAR PRIMARY KEY, cohort VARCHAR NOT NULL,
 symbol VARCHAR NOT NULL, source_snapshot_id VARCHAR NOT NULL,
 strategy VARCHAR NOT NULL, decision_as_of_ms BIGINT NOT NULL,
 executed_as_of_ms BIGINT, entry_json VARCHAR NOT NULL,
 UNIQUE(cohort,source_snapshot_id,strategy));
CREATE INDEX IF NOT EXISTS idx_sl_strategy_entry_time
 ON sl_strategy_entry_snapshot(cohort,symbol,decision_as_of_ms);
CREATE TABLE IF NOT EXISTS sl_strategy_quote_task (
 task_id VARCHAR PRIMARY KEY, entry_id VARCHAR NOT NULL,
 horizon_days INTEGER NOT NULL, purpose VARCHAR NOT NULL,
 due_ms BIGINT NOT NULL, status VARCHAR NOT NULL,
 task_json VARCHAR NOT NULL, updated_at_ms BIGINT NOT NULL,
 UNIQUE(entry_id,horizon_days,purpose));
CREATE INDEX IF NOT EXISTS idx_sl_quote_task_due
 ON sl_strategy_quote_task(status,due_ms,task_id);
CREATE INDEX IF NOT EXISTS idx_sl_fcs_current
 ON sl_funding_capture_snapshot(symbol,as_of_ms,created_at_ms,snapshot_id);
```

### D13.1 Repository增量接口

以下全部async，record为D03 JSON合同的Mapping，保存返回str ID；校验失败ValidationError，队列忙LocalWriteBusyError。所有查询已知时间约束在仓库执行，不能消费端查全部再猜。

```python
save_market_observation(record) -> str
get_market_observation(id: str) -> Mapping | None
list_market_observations(symbol: str, kind: str, start_ms: int,
                        end_ms: int, known_by_ms: int) -> tuple[Mapping, ...]
list_funding_observations(symbol: str, start_ms: int, end_ms: int,
                         known_by_ms: int) -> tuple[Mapping, ...]
save_funding_schedule(record) -> str
list_funding_schedules(symbol: str, known_by_ms: int) -> tuple[Mapping, ...]
save_fx_observation(record) -> str
get_fx_at(currency: str, event_ms: int, known_by_ms: int,
          max_age_ms: int = 60000) -> Mapping | None
save_hedge_decision(record, references: tuple[Mapping, ...]) -> str
get_hedge_decision(id: str) -> Mapping | None
save_protection_confirmation(plan_id: str, expected_version: int,
                             record: Mapping) -> Mapping
get_protection_confirmation(plan_id: str) -> Mapping | None
reserve_provider_request(provider: str, month_key: str, request_id: str,
                         monthly_limit: int, as_of_ms: int) -> Mapping
finish_provider_request(request_id: str, sent: bool, as_of_ms: int) -> None
list_current_funding_opportunities(query: OpportunityQuery,
                                   as_of_ms: int) -> OpportunityPage
save_strategy_entry(record, references: tuple[Mapping, ...]) -> str
list_strategy_entries(cohort: str, start_ms: int, end_ms: int) -> tuple[Mapping, ...]
save_strategy_quote_task(record) -> str
claim_due_quote_tasks(as_of_ms: int, limit: int = 20) -> tuple[Mapping, ...]
finish_quote_task(task_id: str, status: str, result: Mapping) -> None
```

引用使用既有 `sl_hedge_snapshot_reference`，新增类型 DECISION/STRATEGY_ENTRY/MARKET_OBSERVATION/FX/PROTECTION；更新枚举和应用校验。保存Decision/Entry及引用同事务；保护确认、幂等检查和planVersion更新同事务。Quote任务claim使用单worker事务置RUNNING，启动修复未完成RUNNING为PENDING/PROCESS_INTERRUPTED。

月预算使用上述两方法与sl_market_observation的BUDGET_COUNTER，D19定义原子状态与月键；不得只在R11进程内累加。

`OpportunityPage` 为 `{items,total,as_of_ms}`，items包含D08投射；无需新增SQL DOUBLE列。既有save_funding_capture_snapshot保存 `risk_json.projection_v2` 为完整投射，旧无v2记录可看历史但当前列表标LEGACY/NOT_READY。

### D13.2 保留与兼容

活跃计划及任何Evidence引用的快照/FX/观察长期pin，引用完整性先于时间清理。未引用Decision/Simulation30天；未引用raw BOOK保留3天、MARK/TICKER14天、OI/日线/规则180天；MARK_BAR_1H按策略证据至少365天；策略Entry/Outcome/任务保留至少365天；Funding事件/已引用schedule不自动删；关闭计划、手工事件和保护确认不自动删。retention每批1000、优先级4，删除前同事务检查引用。

Hedge关闭时只读既有Plan/Simulation/Decision/Monitor/Outcome；新模拟、建议、事件和状态变更返回HEDGE_DISABLED，历史Monitor标过期。ShortLab整体关闭则SHORTLAB_DISABLED；读历史不启动HTTP。旧Plan在Hedge启用时继续读、监控和登记事件；priceUnit未知的历史强平值标LEGACY_UNIT_UNVERIFIED，由用户重新确认，不悄悄改旧JSON。保存保护确认时记录的plan_version为CAS完成后的新版本；后续核验以protected_position_hash为准。旧未提供Hold/成本/来源的Simulation只读，不自动升级READY。旧ACTIVE不因缺新确认被清账/自动改仓位，标LEGACY_PROTECTION_UNKNOWN并持续监控；有资金暴露的PARTIALLY_FILLED/FUNDED_PENDING_ACTIVATION/CLOSING均纳入高频任务。新公式版本独立于旧版本；旧Outcome不覆盖，新计算建新版本桶。

## D14. 证据采集、基线与统计

### D14.1 样本设计

独立保留 RESEARCH_CANDIDATE、EXECUTABLE_DIRECTIONAL、FUNDING_CARRY、USER_DECISION 四个cohort。方向可执行桶仅当时READY，无BLOCK/PAUSE；同asset/profile/UTC日取首个符合该桶的样本。Research桶可包含WATCH/PAUSED/BLOCKED但必须按状态分层，不能合并为实盘效果。FUNDING桶只取当时Funding Gate PASS与公共市场执行PASS。

策略枚举扩展为 `UNHEDGED_0/ABSOLUTE_100/RELATIVE_75/RELATIVE_50/RELATIVE_25/SYSTEM_POLICY`，h0不要求SpotQuote。USER_DECISION的source_snapshot_id使用decision_id，六个策略共享该Decision及同组真实执行Quote；SYSTEM_POLICY只对有实际用户Decision且选中proposal的样本采集，保存当时goal/预算/比例；不得事后使用当前模型选择。SYSTEM_POLICY复制冻结选中proposal的合法数量/实际ratio，不重新选比例；按(decision_id,horizon_days)与同组固定比例配对，缺同组报价不找别的日样本替代。系统策略与基线只比较相同Decision样本，不能拿全市场均值作其基线。

Entry为“决策后首个真实取得的报价集”，decision_as_of与executed_as_of分开，报价必须实际可取得而非假设在过去收盘成交。同一组策略entry报价收集跨度<=5s；失败则该组不可比。Entry Snapshot在采集完成或明确失败后一次保存，状态只能ENTRY_COMPLETE/UNEXECUTABLE/UNAVAILABLE，PENDING_ENTRY仅允许内存过程，不能先保存immutable快照再修改。Futures入场使用SELL VWAP、退出使用BUY VWAP；Spot入场BUY、退出SELL。Mark仅用于强平/资金费，不代替可执行成交价；缺入场深度则UNEXECUTABLE。canonical Futures qty固定，Spot各ratio合法化；同时采对应数量的buy与sell及Futures反向深度。Reference Notional默认10000，可按配置降低但不可无版本混样。

### D14.2 到期和缺失

horizon7/30/90天从executed_as_of开始；到期任务在due起采集，允许5min延迟；exit时间采用真实取得时点，不强行改成due。未及时取得精确数量反向报价则UNAVAILABLE，不用参考100%报价比例缩放。Quote任务20条/轮，budget拒绝DEFERRED保留；超过deadline明确失败。

FX使用event_ms之前最近来源，age<=60s。入场决策要求known_at<=decision_as_of，入场执行Quote的FX要求known_at<=executed_as_of；资金费/平仓后验核算要求known_at<=该Outcome评价cutoff，不强求网络receipt早于结算事件本身。事件后才查询到且source_as_of晚于事件的当前FX不能回填旧资金费/旧成交。event无Mark不计美元Carry，coverage降低；不得直接把费率相加当USD。存量无足够历史FX/报价的结果保持UNAVAILABLE，而不是制造证据。

压力/MAE/MFE仅使用完整包含在执行区间的已收盘1hbar：open_time>=entry_ts且close_time<=exit_ts；不能把入场前或退出后的部分小时高低价计入。部分首尾小时没有更细可信Mark记录时，extrema/path coverage标PARTIAL，不能宣称完整区间无强平。固定比例cohort使用现金保证金参考资本，不声称还原某个真实杠杆账户；USER_DECISION使用其冻结margin/用户原生强平价进行静态阈值路径研究，label为MODELED_LIQUIDATION，不称真实已强平，后续保证金调整未知必须披露。强平路径要求Mark对应历史，缺Mark路径标UNKNOWN，不能用Spot/last_price断言未强平。退市结果CENSORED并说明可靠最后价/结算价或未知终值，不从样本删除。

### D14.3 Metrics

连接真实Directional Metrics Provider；空库200/0样本，错误503及reason。所有报告按公式、配置hash、Profile、状态、cohort、goal和historyClass分桶。

报告 COMPLETE/PENDING/CENSORED/UNAVAILABLE/UNEXECUTABLE数量、净收益中位数/均值、5%分位、最大不利变动、强平路径覆盖率、成本、可成交率与配对基线差异。只在完整同组样本比较收益，同时列出缺失比例；未知终值不填0。删失有可靠上/下界时显示，无法界定则不可据此宣称优势。

资产聚类bootstrap1000次、固定seed=20261008、95%区间；<30资产或<100可比样本标INSUFFICIENT_SAMPLE；达到样本量也不自动变“概率模型”。按月滚动评估，配置/阈值拟合只用较早月份，最新月为样本外；初期没有训练/校准只输出规则实验结果。

## D15. 配置与版本合同

仅一处配置：既有 `shortlab/default.yaml`，由现有 `shortlab/config.py` 合并/校验；不改engine loader加载ShortLab。以下为完整新增子树，其余默认数值沿用现有文件；只有D04严重冲突阈值修改engine默认配置。

```yaml
shortlab:
  funding_capture:
    fcs_version: fcs_v2
    reference_hold_days: 30
  optimization:
    schema_version: repair-contract-v1
    funding_schedule:
      event_match_tolerance_sec: 60
    decision:
      enabled: true
      version: hedge-decision-v1
      validation_level: RULE_BASED_UNVALIDATED
      ratios: ['0', '0.25', '0.5', '0.75', '1']
      ratio_tolerance: '0.02'
      min_net_carry_usd: '0'
      quote_valid_sec: 20
      exit_stress_bps: 100
      capital_reserve_fraction: '0.05'
      scenarios:
        UP_50: {futures_move: '0.50', spot_move: '0.50', fx_shock: '0'}
        UP_100: {futures_move: '1', spot_move: '1', fx_shock: '0'}
        DOWN_50: {futures_move: '-0.50', spot_move: '-0.50', fx_shock: '0'}
        BASIS_UP: {futures_move: '0.20', spot_move: '0.10', fx_shock: '0'}
        BASIS_DOWN: {futures_move: '-0.10', spot_move: '-0.20', fx_shock: '0'}
        FX_DOWN: {futures_move: '0', spot_move: '0', fx_shock: '-0.03'}
    protection:
      confirmation_ttl_sec: 86400
    evidence:
      version: hedge_evidence_v3
      horizons_days: [7, 30, 90]
      quote_group_skew_sec: 5
      exit_delay_sec: 300
      quote_task_batch: 20
      retention_days: 365
      bootstrap_samples: 1000
      bootstrap_seed: 20261008
      min_assets: 30
      min_comparable_samples: 100
    providers:
      coingecko:
        account_monthly_limit: 10000
        local_requests_per_minute: 30
        reserve_fraction: '0.1'
```

decision.enabled=true不绕过hedge.enabled=false；新Decision API需Hedge和FundingCapture都启用。配置未知键启动拒绝；缺少新增键用以上默认补齐，旧用户文件不必手工迁移。版本定义唯一owner为R00：shortlab/hedge/__init__.py、scoring/versions.py以及config.py的投射版本来源；Grader只import，不定义fallback版本字面量。Import失败为DEPENDENCY_UNAVAILABLE，不回退旧版本计算。

配置接受fcs_v1/fcs_v2输入；旧fcs_v1用户配置规范化为当前计算fcs_v2，旧JSON版本仍按旧桶解码、不重写。取消config.py仅接受fcs_v1的硬校验；四hash投射从规范版本模块导入当前常量，既有*_H01常量只作legacy解码别名，不得进入新计算hash。

版本：观察复用observations-v1；新JSONrepair-contract-v1；Feature features-v3；Entry entry-v3；LTSS权重仍ltss-lite-v1/ltss-full-v1；Plan资金单位/经济公式hedge_v2；成本NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V2；Evidence hedge_evidence_v3；FCS fcs_v2；Safety PLAN_SAFETY_V2；Venue选择VERIFIED_TWO_SIDED_COST_V2。只增新版本桶，不改历史。

TradingRulesSnapshot新增 `stop_orders_supported: bool | None`、`conditional_orders_source_ref: str | None`；orderTypes明确含STOP/STOP_MARKET可作为SUPPORTED证据，仅不含STOP不能直接认定UNSUPPORTED。

HedgeSimulationRequest新增 `goal: str | None` 与 `decision_id: str | None`，HTTP别名goal/decisionId，goal与mode验证遵守D03.3。HedgeSimulationResult新增 `readiness_breakdown` 和 `economics` 字段，ALLOWED_PLAN_STATUS新增FUNDED_PENDING_ACTIVATION。旧JSON缺字段解析为LEGACY；新消费者不得靠原readiness一项认定已挂保护单。

config.py新增 `decision_policy_hash(config: ShortLabConfig | Mapping) -> str`，按现有配置正规化入口转换；沿用既有四hash职责及算法，新decision_policy_hash单独保存，覆盖decision策略+candidate方向门槛+funding entry_gate+hedge成本/数量/强平能力政策+相关版本；排除providers/refresh/path/secret和runtime budget。Canonical JSON递归sort keys、separators(',',':')、ensure_ascii=False，Decimal定点字符串，UTF-8无末尾换行→SHA256。数组保持顺序；金标准从结构重新canonical，不对文档排版字符串比对。

## D16. 验收向量与证据

下列V01–V16是最低功能门槛，实施计划逐项落实，离线Fixture不得冒充真实HTTP抓取。

| 编号 | 输入/过程 | 必需结果 |
|---|---|---|
| V01 | 默认Runtime核验BTC/1000PEPE；歧义同符号候选 | 正确倍率/来源/置信度；歧义不可READY |
| V02 | Ticker44.744/+40%、日线31.96；采集重启 | 价格来源不丢；24h暂停；同receipt恢复 |
| V03 | D与D−7收盘、7D OI；晚到Micro | 7D通过、8D失败；禁止前视确认 |
| V04 | 8h连续90D→每天仅1笔；首尾缺槽；合法4h变更 | 缺槽降覆盖；schedule未知null；合法分段无重复 |
| V05 | 高历史APR，current/last分别正零负未知 | Gate矩阵一致；双负不得READY |
| V06 | nativeMark10/L15，m1/1000、FX1/.97 | 距离0.5；STOP12.5（tick.005）；+50/+100无有效终值 |
| V07 | N10000，APR.02，roundtrip30，hold1/60 | carry约.547945/32.876712；净负/正，BE54.75天 |
| V08 | 仅LIMIT/MARKET；无STOP字段；用户选择保护 | 能力UNKNOWN/UNSUPPORTED诚实；不能宣称已挂单 |
| V09 | h0..1舍入、Dust、资本/情景预算不足 | 最终数量再验；无可行方案AVOID/人工核验 |
| V10 | 旧READY→新NOT_READY，同币200历史快照 | 最新过滤；不重复；过期不冒充当前 |
| V11 | Futures开10@100/平4@90，Spot开10@100/平4@110；FX1 | futures/spot realized各40，剩余各6；实际费只计一次 |
| V12 | A→B读失败/迟到、后台风险变化、版本v→v+1 | 不跨计划写；10s正常更新；旧版本409 |
| V13 | 11币/50计划、8s慢provider、队列超5s | tick有界、轮转、公平；降级不改持仓 |
| V14 | h0/.25/.5/.75/1真实采样→7/30/90D到期 | 精确数量/FX/来源；缺失不缩放补造；h0无Spot依赖 |
| V15 | exit价2倍/0.5倍，缺mark/FX，退市 | exit费随实际金额；Partial/Censored正确 |
| V16 | v4/v5数据库→v6，失败/重复、Frozen两次启动 | 事务回滚、记录保持、006真实打包、用户目录可写 |

金额断言Decimal误差<=`0.00000001` USD（测试显示允许取8位）；数量、版本、来源时间和Gate完全相等，不用宽容差掩盖单位错误。真实网络POC单独报告账号配置/环境/原始响应/采集时间，451/429、缺key或缺网络不得算通过；离线门槛可独立完成。

## D17. 外部规则与资料

- 正/负Funding收付方向与结算间隔变化依据：[Binance资金费规则](https://www.binance.com/en/support/faq/detail/360033525031)。
- FundingInfo、OI历史限制、Mark/Ticker源时间依据：[Binance USDⓈ-M市场数据](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)。不能用当前间隔推断全历史，也不能期待接口提供任意90D OI。
- 账户配额按实际套餐核对：[CoinGecko API Pricing](https://www.coingecko.com/en/api/pricing)。本方案本地默认是保守工程限额，不声称该数值等于所有套餐当前限额。

以上资料约束数据含义，不证明代码已实现。最终交付以配套计划的测试和证据矩阵验收。开发、UI构建与浏览器验收统一Node22.x，package engines固定>=22 <23、CI node-version固定22；开发依赖Playwright1.56.0，测试目录与生产构建分离；依据[Playwright发布说明](https://playwright.dev/docs/release-notes#version-156)，不把浏览器依赖打进桌面生产包。

## D18. 新增 DTO、纯接口与记录字段冻结表

本节是R00交付的类型合同。DTO均frozen dataclass；嵌套JSON在构造时校验并防止外部可变对象修改快照。下表逗号分隔的字段均为必需键；可null的字段用 `?` 标明，非null字段缺失拒绝。除另注外，记录ID/version/code为str、时间为int、金额/数量/率为Decimal字符串；reasons/refs遵守D03。

| DTO | 字段 |
|---|---|
| `GateResult` | status:enum, reasons:tuple[str], checked_at_ms:int, input_refs:Mapping[str,str] |
| `ReadinessBreakdown` | data_complete:bool, funding_gate:GateResult, execution_gate:GateResult, economic_gate:GateResult, protection_status:enum, readiness:enum |
| `FundingScheduleSegment` | D05.1所有字段；effective_to_ms:int?；interval_hours:int>0；anchor_ms:int；verification:enum |
| `FundingCoverage` | window_start_ms:int,window_end_ms:int,expected_count:int?,received_count:int,coverage_fraction:Decimal?,schedule_coverage_fraction:Decimal,missing_slots:tuple[int],reasons:tuple[str] |
| `FundingContext` | metrics:既有FundingMetrics,coverage_7d/30d/90d:FundingCoverage,history_class:enum,listing_age_days:int?,conservative_apr:Decimal?,conservative_method:str,current_observation:Observed,last_settled_observation:Observed?,schedule_refs:tuple[str],input_refs:Mapping[str,str] |
| `FuturesExecutionQuote` | quote_id:str,symbol:str,requested_contract_qty:Decimal,buy_vwap_native/sell_vwap_native:Decimal?,buy_executable_qty/sell_executable_qty:Decimal,quote_currency:str,quote_to_usd:Decimal?,as_of_ms/known_at_ms/expires_at_ms:int,book_observation_id:str,fees_included:bool? |
| `EconomicsResult` | hold_days:int?,actual_futures_notional_usd:Decimal,conservative_carry_usd:Decimal?,roundtrip_cost_usd:Decimal?,net_carry_usd:Decimal?,break_even_days:Decimal?,capital_required_usd:Decimal?,cost_basis:str,gate:GateResult,unknown_components:tuple[str] |
| `RatioProposal` | target_ratio:Decimal,actual_ratio:Decimal,futures_contract_qty:Decimal,canonical_futures_qty:Decimal,spot_net_qty:Decimal,spot_venue:str?,quote_refs:Mapping[str,str],economics:EconomicsResult,scenarios:tuple[ScenarioResult],execution_gate:GateResult,risk_gate:GateResult,order_guidance:tuple[Mapping] |
| `ScenarioResult` | scenario_id:str,futures_move/spot_move/fx_shock:Decimal,status:VALID/INVALID_AFTER_LIQUIDATION/UNKNOWN,net_pnl_usd:Decimal?,loss_usd:Decimal?,reasons:tuple[str] |
| `DecisionRequest` | D07.1字段；symbol/goal:str；preferred_spot_venue默认AUTO；其余金额Decimal、hold int |
| `DecisionContext` | D07.1字段；funding_context:FundingContext，directional_score_id:str?，identity:AssetIdentity，futures_mark:Observed，futures_quote:FuturesExecutionQuote?，futures_rules:既有TradingRulesSnapshot，venue_quotes:tuple[既有SpotVenueQuote]，directional:Mapping? |
| `DecisionResult` | D07.3字段；selected_proposal:RatioProposal?，alternatives:tuple[RatioProposal]；推荐与validation_level为enum；request:DecisionRequest，context_refs:Mapping |
| `OpportunityQuery` | D08参数；数值最小门槛Decimal?、min_fcs float?、string filters可null、分页int、include_stale bool |
| `OpportunityPage` | items:tuple[Mapping],total:int,as_of_ms:int? |
| `LedgerPnl` | D09所有字段；coverage:Mapping{priced_events,total_events,status},unknown_components:tuple[str]；新增funding_basis:ACTUAL_RECEIPTS_ONLY/ESTIMATED |
| `PairExitGuidance` | D10字段；legs:tuple[Mapping],unexecutable_dust:Mapping[str,Decimal],confirmation_required:bool |
| `CaptureContext` | cohort:四cohort枚举,source_snapshot_id:str,symbol:str,identity:AssetIdentity,identity_snapshot_id:str,funding_context:FundingContext,futures_contract_qty/canonical_futures_qty:str,strategies:tuple[str],decision:DecisionResult?,decision_as_of_ms:int,policy:Mapping,rule_refs/source_refs:Mapping[str,str] |
| `CaptureResult` | entry_ids:tuple[str],status:COMPLETE/PARTIAL/UNAVAILABLE,executed_as_of_ms:int?,reasons:tuple[str]；entry_ids按context.strategies顺序 |
| `QuoteCollectionResult` | claimed/complete/deferred/unavailable:int>=0,task_ids:tuple[str],as_of_ms:int,reasons:tuple[str] |
| `RepairPorts` | D19的9个callback键，默认None；require(name)拒绝未绑定；frozen且只在Runtime绑定真实实现，test Fake不进入production |
| `RequestContext` | 复用request_budget.RequestContext：budget:RequestBudget?,host:str="fapi",job_type:str="entry",endpoint_family:str?,trace_id:str?,identity_snapshot_id:str?；末尾追加job_id:str?=None,deadline_ms:int?=None，旧构造不变 |

DecisionContext.directional在非null时必含：profile、ltss、entry、data_quality、tradeability、candidate_status、execution_status、vetoes、pauses、score_as_of_ms、config_hash；评分为float?；vetoes/pauses为code数组。只有不可用数据可以null，不能缺键。

FundingContext.metrics沿用既有字段；7/30/90累计费率、正比例、最近费率从该对象读取。需要补现有metrics字段时由R00统一扩展，旧JSON缺字段解析为null并LEGACY，不允许消费者各自另写键名。

### D18.1 纯函数接口

```python
load_json_strict(text: str | bytes) -> Mapping | Sequence
resolve_asset_context(symbol: str, exchange_meta: Mapping,
                      catalog_candidates: Sequence[Mapping], overrides: Mapping) -> AssetIdentity
compute_schedule_coverage(events: Sequence[Mapping], schedules: Sequence[FundingScheduleSegment],
                          start_ms: int, end_ms: int, known_by_ms: int) -> FundingCoverage
evaluate_funding_entry_gate(context: FundingContext, policy: Mapping,
                            as_of_ms: int) -> GateResult
native_liquidation_distance(mark: str, liquidation: str) -> str
native_stop_reference(mark: str, liquidation: str, tick: str) -> str | None
evaluate_economics(proposal: Mapping, funding_context: FundingContext,
                   hold_days: int | None, cost_policy: Mapping) -> EconomicsResult
resolve_stop_capability(rules: Mapping) -> str
validate_protection_confirmation(record: Mapping, plan: Mapping,
                                 positions: Sequence[Mapping], now_ms: int) -> GateResult
recommend_hedge(request: DecisionRequest, context: DecisionContext,
                policy: Mapping, *, ports: RepairPorts | None = None) -> DecisionResult
compute_ledger_pnl(events: Sequence[Mapping], identity: AssetIdentity,
                   event_fx: Mapping[str, Mapping], market_context: Mapping) -> LedgerPnl
build_pair_exit_guidance(plan: Mapping, positions: Sequence[Mapping],
                         market_context: Mapping, rules: Mapping, now_ms: int) -> PairExitGuidance
project_opportunity(snapshot: Mapping, as_of_ms: int) -> Mapping
build_ratio_proposal(request: DecisionRequest, context: DecisionContext,
    target_ratio: str, policy: Mapping, *, ports: RepairPorts | None = None) -> RatioProposal
async capture_strategy_entries(context: CaptureContext, repository: RepositoryPort,
    market: MarketPort, request_context: RequestContext) -> CaptureResult
async collect_due_quotes(repository: RepositoryPort, market: MarketPort,
    as_of_ms: int, request_context: RequestContext) -> QuoteCollectionResult
simulate_hedge(request: HedgeSimulationRequest, *, futures_mark: Any,
    spot_quote: SpotVenueQuote | Mapping, futures_rules=None, spot_rules=None,
    funding=None, identity=None, policy=None, now_ms: int,
    futures_quote: FuturesExecutionQuote | None = None, ports: RepairPorts | None = None) -> HedgeSimulationResult
```

新增数据接口 `fetch_mark_klines_range(symbol, interval, start_ms, end_ms, *, request_context=None) -> Observed[Sequence[Mapping]]` 位于data/binance_klines.py，使用公开Mark Price Kline端点、真实原生OHLC及close时间；不得从交易价K线伪造。HistoricalMarketProvider增加async `read_mark_price_bars(symbol,start_ms,end_ms,request_context) -> tuple[HistoricalPriceBar,...]`，R14b适配并归档MARK_BAR_1H；HistoricalPriceBar新增price_basis=TRADE/MARK（旧默认TRADE），Mark路径只消费MARK。该历史后验数据可在评价时回填，但不得进入过去入场决策。

新增 `compute_contract_vwap(book: Observed, contract_qty: str, side: str) -> Mapping` 位于units.py，side为BUY（asks）/SELL（bids）；按原生contracts累计，返回vwap_native、executable_contract_qty、coverage，bid/ask不能复用合并总量。R11b用它构造FuturesExecutionQuote并保存BOOK引用。simulate_hedge新增keyword futures_quote，可null用于旧研究计算，但新execution Gate不能在缺双向深度时PASS。

`evaluate_economics.proposal`必须含actual_futures_notional_usd、spot_cash_usd、margin_usd、entry_fee_usd、exit_fee_usd、slippage_usd、gas_usd、fees_included、reserve_fraction；金额未知null，fees_included为明确bool，不可缺。若成本已含在VWAP/Quote，对应独立金额应为0且来源解释，不再重复扣。

build_ratio_proposal的唯一签名见D18.1，位于shortlab/hedge/economics.py，与Planner共用数量/成本实现。h0构造合约单腿只读proposal，不调用拒绝h0的双腿Plan保存接口。

`compute_ledger_pnl.market_context`含now_ms、futures_mark_native、futures_quote_fx、spot_sell_vwap_native、spot_quote_fx、estimated_exit_fee_usd、exit_quote_refs；可用金额为字符串、未知null。event_fx keyed by event_id，值为{price_fx_id,price_fx,fee_fx_id?,fee_fx?,funding_fx_id?,funding_fx?}，每个FX引用通过get_fx_at约束，真实人工账本的known_by_ms采用本次核算cutoff（不要求用户在成交前已经登记）；来源时点仍<=执行时点且age<=60s，实际用户FX证明保存USER_ENTERED及原凭据、不能称公开源已验证；USD币种本身1无需外部报价，但USDT/USDC不得套用。

`build_pair_exit_guidance.rules`按FUTURES_SHORT/SPOT_LONG分组；market_context复用上述并含futures_buy_vwap_native、futures_exit_coverage、spot_exit_coverage、expires_at_ms。生成时 positions来自有效事件聚合，不能接受UI自报。

### D18.2 新表JSON结构

- market value_json为原始兼容值；meta_json为observations-v1的完整ObservationMeta+`repair_schema_version=repair-contract-v1`。kind枚举TICKER/MARK/MARK_BAR_1H/OI/BOOK/RULES/FUNDING_INFO/ACTIVATION_CHECK/EVENT_FX/BUDGET_COUNTER；EVENT_FX的value为event_id及D18.1 FX映射，引用写入同一事务。
- schedule_json为完整FundingScheduleSegment，不从SQL列丢失interval/anchor/verification。
- fx source_json含source、currency、rate_str、source_as_of_ms、known_at_ms、raw_sha256、schema_version；rate_str>0。
- decision_json为完整DecisionResult；request和source_refs不可省略。
- confirmation_json含D12完整请求、confirmation_id、resulting_plan_version、protected_position_hash、schema_version。
- entry_json含schema_version、cohort、strategy、decision_snapshot_id?、source_snapshot_id、identity_snapshot_id、decision_as_of_ms、executed_as_of_ms?、target_ratio、actual_ratio、native_futures_qty、canonical_futures_qty、spot_net_qty、quote_refs、rule_refs、fx_refs、goal、policy_hash、formula_version、status、reasons。h0的spot数量为0、spot refs空；STATUS为ENTRY_COMPLETE/UNEXECUTABLE/UNAVAILABLE。
- task_json含schema_version、entry_id、purpose=EXIT、strategy、horizon_days、due_ms、deadline_ms、requested_futures_qty、requested_spot_qty、venue、attempt_count、quote_refs、reason_code；状态PENDING/RUNNING/DEFERRED/COMPLETE/UNAVAILABLE。尝试受预算，不固定次数即无条件循环；到期deadline前轮转可重试，超时终止。

### D18.3 HTTP输出示例形状

```json
{
  "contractSchemaVersion":"repair-contract-v1",
  "decisionId":"dec-example",
  "generatedAtMs":1791417600000,
  "expiresAtMs":1791417620000,
  "recommendation":"DATA_INSUFFICIENT",
  "validationLevel":"RULE_BASED_UNVALIDATED",
  "selectedProposal":null,
  "alternatives":[],
  "reasons":["FUNDING_SCHEDULE_UNKNOWN"],
  "assumptions":[],
  "contextRefs":{"identity":"identity-example","fcs":"fcs-example"},
  "formulaVersion":"hedge-decision-v1",
  "decisionPolicyHash":"sha256-of-canonical-policy",
  "request":{
    "symbol":"1000PEPEUSDT","goal":"CARRY_CAPTURE",
    "futuresNotionalUsd":"10000","plannedHoldDays":30,
    "availableCapitalUsd":"25000","maxScenarioLossUsd":"1000",
    "marginUsd":"12000","liquidationPrice":"0.02",
    "liquidationPriceUpdatedAtMs":1791417590000,
    "preferredSpotVenue":"AUTO"
  }
}
```

示例ID/hash为结构演示，不是金标准或真实行情。测试金标准必须使用Fixture实际输入与canonical重算，不把示例字符串当SHA256。

## D19. 实施兼容、并行合同与明确边界

### D19.1 合同依赖与合并依赖

R00冻结repair_contracts.py、repair_ports.py、共享Fixture、仓库async协议、Service调用点schema和版本/hash金标准。计算/采集消费者只依赖这些合同，不以“生产者实现先合并”为开工条件。跨模块协作者通过不可变RepairPorts注入；生产实现由R10b绑定，未绑定返回IMPLEMENTATION_UNAVAILABLE/UNKNOWN，不得默认PASS或在生产使用Fake。

RepairPorts仅含可选callback与require(name)方法，require未绑定抛RepairDependencyUnavailable；callback键固定为compute_schedule_coverage、evaluate_funding_entry_gate、build_ratio_proposal、compute_ledger_pnl、build_pair_exit_guidance、project_opportunity、capture_strategy_entries、collect_due_quotes、simulate_hedge。各签名由D18及下文冻结，推荐/规划/输入/生产jobs需要跨模块callback时增量keyword `ports=None`，纯模块内部不直接导入未合并的生产者。recommend_hedge增加keyword ports；simulate_hedge保留现有参数并追加keyword futures_quote=None、ports=None。调用者未传ports可以进行不授信用的旧研究计算，但新Gate不能PASS；正式Runtime显式注入完整真实ports。

repair_ports.py定义RepositoryPort，完整包含D13.1全部20个async方法；复用原Repository公开读写，不调用私有_run绕开事务。MarketPort冻结collect_futures(symbol,contract_qty,request_context)、collect_spot(identity,venue,canonical_qty,request_context)、collect_funding(symbol,as_of_ms,request_context)，均async，分别返回Futures Mark/ExecutionQuote/Rules的Mapping、SpotVenueQuote、FundingContext；数据不可用返回带原因的Observed/UNKNOWN，实际预算拒绝可抛BudgetExhausted。ProductionHedgeMarket通过显式新增方法/兼容适配实现这些入口，保留原方法，禁止消费者各造get_quote同义协议。

create_app唯一新增工厂缝签名为 `create_app(*, shortlab_runtime_factory: Callable[[], ShortLabRuntime] | None = None) -> FastAPI`，现有无参调用完全保留。Runtime增量keyword repair_ports=None、allow_test_bindings=False，均不改变旧构造参数；allow_test_bindings只可由tests的显式factory传True，不是配置/env/API字段，生产默认False且不能fallback到True。factory同步构造未start的Runtime，App lifespan负责start/stop，factory不接受其他参数；测试用closure携带data_dir/scenario/clock，返回真实Runtime，只替换HTTP transport。R15a冻结 `create_repair_test_app(data_dir: Path, scenario: str, clock_ms: Callable[[], int], *, bindings: RepairPorts | None = None) -> FastAPI`，用closure构造ShortLabRuntime(data_dir=data_dir,clock=clock_ms,...)；bindings=None用真实默认绑定且allow_test_bindings=False，bindings显式测试Fake才True。该helper必须用shortlab_runtime_factory keyword，不直接替换App.state为预制Service。None时使用真实默认构造，不从环境变量偷偷启用Fake。测试helper的显式公式：closure构造ShortLabRuntime(...,repair_ports=bindings,allow_test_bindings=(bindings is not None))；factory不接受参数，clock_ms通过Runtime.clock传入。helper非None bindings必须是D19.6的显式测试Fake，否则拒绝TEST_BINDINGS_REQUIRED；真实Producer组合只能用bindings=None触发默认真实绑定，禁止把非None真实ports误标测试。

R10a在第一开发波次提交Service/API边界与测试Fake；R10b独占同一文件的最终接线阶段。R10a测试Fake位于tests，不进production；默认已启用新功能但未绑定实现时HTTP503 IMPLEMENTATION_UNAVAILABLE，历史读取不受新功能未绑定影响。R12/R13a/R13b先对固定HTTP合同离线实现，不要求R07/R10b代码先合并；真实运行符合性由R15b统一验收。

服务调用点schema包含角色、Producer文件/函数、参数名/别名、返回DTO、可用性、超时、reason/error映射、Source引用和默认开关；不能仅列一个函数名称。R00同时提供UI组件props/callback schema及仅测试用component-stubs；UI消费者在隔离测试build中alias替身，生产build禁止alias/fallback。R14b的RepositoryHistoricalMarketProvider新增可选mark_price_bars_fn，由R10b注入R03真实fetch_mark_klines_range，未注入Mark路径UNKNOWN，不在模块顶层import尚未合并新函数。

每个并发实现包必须使用独立checkout，禁止在同一工作目录并发切分支/reset。严格文件边界场景使用主项目desktop/backend/runtime/repair_checkouts/<task_id>中的独立完整clone（git clone --no-hardlinks，独立.git，不写其他子包/主仓库）；协调者在主仓库fetch子包commit后集成。每子包root内存放依赖缓存/临时数据，根外只读；不将共享.git元数据写入权限隐含委派给子agent。

每个实现包交付conformance测试，R10b验证全部真实Producer与同一合同一致后绑定。

### D19.2 子工作包与所有权

拆分：R02a Resolver/Override、R02b Catalog；R06a Units/Economics、R06b Protection/Planner；R08a Ledger/PnL、R08b Monitor/Exit；R11a Budget/HTTP、R11b Jobs/Market；R14a Capture、R14b Historical/Metrics；R13a纯展示/状态模型、R13b实际路由与数据接线；R15a Harness、R15b整体验收。R10a/R10b为同一逻辑owner的顺序阶段，不能同时修改Service/API。

独占共享文件：config/model/fixture由R00；i18n.js由R13a；Service/Runtime/API按R10a→R10b交接；request_budget.py按R00（增量类型）→R11a（预算行为）交接；short-lab.spec按R01（006）→R16（发布）交接；smoke脚本/release workflow/ci.yml仅R16；desktop/ui/package.json与package-lock.json按R15a（测试依赖/script）→R16（engines与对应lockfile）交接。其余子包文件互不重叠。evidence/jobs.py与hedge/jobs.py使用完整项目相对路径，禁止缩写成同一个jobs.py。

owner policy采用显式owner_sequence及stage_prerequisites；checker必须识别codex/rNN、rNNa、rNNb分支。分支正则：`^codex/r(?:0[0-9]|1[0-6])(?:[ab])?-[a-z0-9][a-z0-9-]*$`，再校验task_id确在任务表。契约依赖只要求R00已合并、声明interface_dependencies和合同SHA；merge_prerequisites才进入旧checker的prerequisite_commits祖先检查。顺序owner的前序阶段必须已合并；不得把所有逻辑Producer都填入prerequisite_commits导致串行。

### D19.3 请求类型、host/family与权重

RequestContext由request_budget.py唯一声明；R00增量字段必须兼容原六字段frozen结构和make_request_context。job_type→预算档固定如下，旧名保留：

| job_type | 档位/总额度上界 |
|---|---|
| monitor/active_monitor/critical | MONITOR，100% |
| scanner/user_scanner | SCANNER，扣除floor(total×0.2)，正常额度80% |
| interactive | SCANNER，扣除floor(total×0.2)，正常额度80%，不得使用monitor保留 |
| opportunity/evidence/entry/backfill/funding_backfill/retention/background | BACKGROUND，扣除floor(total×0.2)+floor(total×0.3)，正常额度50% |
| 其他未知值 | JOB_TYPE_UNKNOWN，拒绝发送 |

数据源配置的DIVE_FAPI_BASE镜像若存在，只有该明确配置的HTTPS origin作为fapi允许别名且共用fapi预算；任意其他host不因path相同获准。

权重版本升级endpoint-weights-v2；识别URL使用允许host+完整path，不用路径含exchangeInfo就接受任意host。既有families与limit桶保留，新增/补齐以下本地保守权重。权重是本地请求token；平台官方IP限额另按已知资料执行，不宣称Alpha/CoinGecko token为Binance官方IP权重。发送次数、权重窗口、单轮240、Funding80/300s、provider RPS/月额均独立约束。FAPI/Spot最终权重取max(本地保守token,Fixture冻结的已核对官方IP权重)，资料未知不能默认为0；Alpha/CoinGecko/0x不套用FAPI的官方IP桶，使用独立host本地RPS/token限额并诚实标平台限额未知。

| host/path | family | 本地权重 |
|---|---|---|
| fapi /fapi/v1/fundingInfo | fundingInfo | 5，仍占Funding共享次数 |
| fapi /fapi/v1/markPriceKlines | markKlines | 与klines limit桶相同；limit1..1500 |
| fapi /fapi/v1/depth | futuresDepth | limit<=100:20；<=500:30；<=1000:50 |
| api.binance.com /api/v3/klines | spotKlines | 与klines limit桶相同；limit1..1000 |
| api.binance.com /api/v3/depth | spotDepth | limit<=100:20；<=500:30；<=1000:50 |
| api.binance.com /api/v3/ticker/24hr | spotTicker | 单symbol2，全列表40 |
| www.binance.com五个既有公开Alpha路径 | alphaTokenList/alphaExchangeInfo/alphaTicker/alphaDepth/alphaKlines | 每次10；具体path从data/binance_alpha.py五个PATH常量冻结到Fixture |
| api.coingecko.com或pro-api.coingecko.com /api/v3/coins/list、/coins/markets、/coins/{id}、/simple/price | cgDirectory/cgMarkets/cgCoin/cgFx | 每次1，另占月/RPM限制；coins/list/markets优先于动态{id}匹配 |
| api.0x.org只读allowance-holder price路径（适配器PRICE_PATH及公开v2 header无后缀路径） | onchainPrice | 1，另1RPS/并发1 |

新代码所有URL/params在R00 endpoint-contract Fixture中有样本，R11a实现registry并通过未知host/path/limit拒绝测试。R03/R14开发时用合同HTTP Fake，不先发真实未注册请求；R10b绑定前必须已合并R11a，实际启动不得UNBUDGETED_ENDPOINT。不因host匹配允许交易/approve/calldata路径。网络404/451/429、缺key保持诚实不可用，不以注册family认定平台PoC通过。

### D19.4 月预算原子性

Repository新增月预算方法计入R00 RepositoryPort和R01实现。CoinGecko monthly_limit=floor(account_monthly_limit×(1-reserve_fraction))，默认9000，降低额度不重置历史计数。month_key=UTC YYYY-MM，reserve时必须与as_of_ms匹配；不同步拒绝MONTH_KEY_MISMATCH。request_id为全局唯一内部发送尝试ID（新尝试用新UUID），不接受外部API客户端控制。

BUDGET_COUNTER value_json：provider/month_key/request_id/state/limit_snapshot/transition_seq/recorded_at_ms。RESERVED seq1；SENT/CANCELLED seq2；状态变化追加新Observation，同毫秒按seq选择，终态不可互转。SENT+RESERVED占额度、CANCELLED不占。计数、比较、插入由同一个单worker事务完成。

reserve返回{admitted,request_id,month_key,sent_count,reserved_count,remaining,reason_code}；admitted表示**本次是否获得一个新的发送许可**，不是“过去是否曾预留”。重复调用不再次授发送许可，不改变计数。

| 现状/调用 | 状态变更/返回 |
|---|---|
| 不存在+额度足够+参数合法 | 新增RESERVED，admitted=true，reason_code=null |
| 不存在+额度不足 | 不新增状态，admitted=false，BUDGET_MONTHLY_EXHAUSTED |
| RESERVED+同provider/月/limit再次reserve | 状态不变，admitted=false，BUDGET_RESERVATION_ALREADY_HELD |
| SENT+同参数再次reserve | 状态不变，admitted=false，BUDGET_REQUEST_ALREADY_SENT |
| CANCELLED+同参数再次reserve | 状态不变，admitted=false，BUDGET_REQUEST_CANCELLED；再次尝试用新ID |
| 相同ID但provider/月/limit不同 | ValidationError BUDGET_REQUEST_ID_CONFLICT，不写入 |
| 不存在ID的finish | ValidationError BUDGET_REQUEST_NOT_FOUND，不创建隐式预留 |
| RESERVED+finish(sent=true) | 新增SENT seq2，实际发送或网络结果未知均计费 |
| RESERVED+finish(sent=false) | 新增CANCELLED seq2；只允许transport尚未调用的取消 |
| SENT+重复finish(true)，CANCELLED+重复finish(false) | 幂等成功，无新行、计数不变 |
| SENT+finish(false)，CANCELLED+finish(true) | ValidationError BUDGET_STATE_CONFLICT，不写入 |

finish的as_of_ms不得早于reservation时间，倒退拒绝CLOCK_SKEW。跨月finish归属原reservation月，不迁移已发请求。午夜跨月的未发预留由R11a的HTTP发送适配层（desktop/backend/src/diveintocrypto_desktop/data/http.py）执行：在缓存判断、月预留、host/RPM排队结束后，每次即将首次进入transport发送协程前重新读取UTC clock；若月已变化，先旧ID finish(false)，再新UUID/新月reserve，重检deadline和额度，不获新许可则取消未dispatch的host permit并不发送。任何await之后再次检查，不能让等待中的旧月预留直接发送。R11b/Caller只传RequestContext，不自行换月。计费发送时点定义为本地首次进入transport协程的时点，不是响应完成；已进入transport即为在途，跨月返回仍finish(true)归原预留月，未知结果同样计原月，不取消/重预留。每retry新ID、cache不预留，host拒绝且未调用transport可取消。

进程恢复时，R01启动恢复把遗留RESERVED保守追加SENT（reason=PROCESS_INTERRUPTED_SEND_UNKNOWN），因为不能证明是否已发送；不能自动退款或清零。该恢复限于获得DB唯一写入权的启动阶段，不处理当前存活worker的预留。

GC由R01的maintenance执行、R16验收。保留阈值为max(该月结束UTC时点+180天,最新状态recorded_at_ms+180天)；当前月永不删除，未完成恢复的RESERVED永不删除，事务中复核全部请求状态再整月清理。幂等历史保证在保留窗口内；超过窗口的未知finish仍NOT_FOUND，Producer不重用旧UUID。所有拒绝均不调用transport，R11a按失败码报告、延迟或终止，不绕过拒绝再发。

### D19.5 边界判定与验收语义

- economics先判断输入是否完整，unknown_cost/FX/APR→UNKNOWN；仅完整时Decimal net_carry严格大于min_net_carry_usd→PASS，等于/小于→FAIL，不先用float判断正负。
- D08 stale没有grace；Monitor的grace只可显示旧值，不显示当前可执行。expires_at_ms等于as_of_ms即过期。
- R00固定七个数据Fixture helper：make_identity、make_funding_context、make_decision_context、make_decision_request、make_events、make_market_context、make_event_fx；额外make_ports提供合同协作者Fake，生产不可导入tests。make_identity默认BTC/m1；make_events/make_event_fx/make_market_context默认同一partial_close事件集、FX1和明确零费用；make_decision_context/make_decision_request默认同一MEME_FULL_VALID案例（1000PEPE、强平价高于2×Mark、合法双向深度、充足资本、正净Carry），类型/ID对应，不能为通过默认UP_100修改压力配置。
- R15根E2E仅urllib访问独立46409后端进程，断言URL origin/端口，禁止被根conftest修改的httpx/websockets伪装生产网络。浏览器二进制无法安装/离线环境不足为UNVERIFIED，不能用Node状态测试代替真实React验收并填写PASS。
- V16同时覆盖项目根tests/test_shortlab_repair_packaging.py及tests/static_analysis/test_shortlab_repair_release.py；二者均由R16创建，明确是根测试，不属于backend/tests。其余测试按实施计划完整项目相对路径定位。
- 001–005加本文006仍为新增7表/8索引；D13.1共20个新增async方法（包括2个月预算方法），R01 handoff逐项列签名、实现、测试，不按口头计数判断完成。

### D19.6 合同测试替身与生产构建隔离

`make_ports(**overrides) -> RepairPorts`只定义于desktop/backend/tests/repair_fixtures.py。覆盖白名单恰为D19.1九键；未知键/非callable抛ValueError，None允许作为显式未绑定测试，覆盖不改变其他八键。函数每次返回新frozen RepairPorts，不共享可变事件集。下面的fake_*是同一测试文件的函数，不能导入生产模块作为默认，否则会重新形成合并依赖。

| callback键 | 默认测试函数/来源 | 返回合同 |
|---|---|---|
| compute_schedule_coverage | fake_schedule_coverage；coverage_cases.json按窗口/案例读取 | FundingCoverage |
| evaluate_funding_entry_gate | fake_funding_gate；gate_cases.json按正/零/负/未知组合读取 | GateResult |
| build_ratio_proposal | fake_ratio_proposal；ratio_cases.json按案例ID/target_ratio读取 | RatioProposal |
| compute_ledger_pnl | fake_ledger_pnl；ledger_cases.json按事件ID集读取 | LedgerPnl |
| build_pair_exit_guidance | fake_pair_exit；exit_cases.json按plan/剩余量读取 | PairExitGuidance |
| project_opportunity | fake_projection；opportunity_cases.json按snapshot_id读取 | 内部snake_case Mapping |
| capture_strategy_entries | async fake_capture_entries；capture_cases.json按source/策略集读取 | CaptureResult |
| collect_due_quotes | async fake_due_quotes；due_quote_cases.json按as_of/任务案例读取 | QuoteCollectionResult |
| simulate_hedge | fake_simulation；simulation_cases.json按合法request案例读取 | HedgeSimulationResult |

这九个JSON由R00生成于desktop/backend/tests/fixtures/repair/ports/，标记binding_kind=TEST_FAKE。未知案例/不匹配资产/数量/窗口抛FixtureCaseNotFound，不返回通用PASS；默认MEME_FULL_VALID的ratio表含五个ratio的完整输入/返回，h1合法、净Carry正、UP_100仍在强平价以下。Gate正/零/负/未知和不支持保护例有独立行，不能用默认正例覆盖所有输入。Fake不证明算法正确；Producer自身用真实函数做数值测试，消费者先做协议测试，R10b/R15b再验真实组合。

CaptureContext的SYSTEM_POLICY仅允许USER_DECISION且decision.selected_proposal非null；policy/身份/输入引用齐全。CaptureResult COMPLETE表示所有要求策略都有ENTRY_COMPLETE；至少一条有效、其他不可用为PARTIAL；无有效条目为UNAVAILABLE。QuoteCollectionResult claimed=complete+deferred+unavailable，task_ids顺序为due_ms ASC/task_id ASC；complete仅真实任务完成，预算拒绝计deferred。这些结构由R00冻结，R14a不发明返回键。

**alias定义：**测试alias指把生产模块解析到tests/fixture/stub，或把运行时真实callback替换为测试Fake。合法camelCase字段别名、RequestContext re-export和dive-desktop兼容CLI不属于此禁令。测试alias只在隔离test build的resolver/plugin/loader及测试factory使用，禁止把条件fallback写入desktop/ui/src、后端生产模块或生产build配置。

R00修改checker规则：检查新增/修改生产文件的Python导入和JS import/export/dynamic-import，不得指向路径组件tests/test/e2e、repair_fixtures、component-stubs；生产build.mjs不得配置测试onResolve/alias/loader。规则使用语法/解析路径，不能仅对注释出现Fake单词就拒绝。同一commit含开发测试依赖合法，前提生产import graph不包含该依赖。未知动态目标无法证明为生产模块时需给出明确允许清单，否则失败PRODUCTION_TEST_ALIAS。

R10b的绑定验证除静态规则还检查实际callback来源（解包partial/decorator的被包装函数）：来自tests/stub、binding_kind=TEST_FAKE、或不符合service_calls.json的Producer角色来源，都拒绝FAKE_BINDING_REJECTED；函数不能只靠改__module__绕过。未绑定/被拒绝的新功能API HTTP503 IMPLEMENTATION_UNAVAILABLE，公开响应不暴露路径；None默认factory仍是真实Runtime。R10a及R15a的测试factory可以注入Fake用于边界测试，但与生产默认启动隔离。

R16生产构建验收：启用esbuild metafile，断言实际依赖图无desktop/ui/test、e2e、component-stubs及Playwright；打包资源/模块清单无backend/tests或repair_fixtures。Frozen通过正常入口不传test factory启动，/capabilities列真实Producer绑定来源摘要，缺任一必需绑定不得READY；未绑定503在R10a/R15a的显式测试factory中构造repair_ports=RepairPorts()且allow_test_bindings=False验证，不新增生产CLI/config/env开关；Frozen正常启动只检查真实绑定与能力状态，不能因为页面可渲染而PASS。测试build alias只在其专用测试入口有效，不改production build.mjs语义。

### D19.7 并行checkout准备与清理

协调者从主项目根创建desktop/backend/runtime/repair_checkouts；该目录受已存在desktop/backend/.gitignore的runtime/规则覆盖，不需要新的全局ignore。R00/R16用git check-ignore验证checkout、临时数据、manifest不会进主仓库。每个checkout来自已提交合同/代码基线，禁止复制主工作区未提交修改。

协调者命令的cwd是主项目根，先设置repair_primary_root=$(pwd -P)，再对每个已选任务设置repair_task_id与repair_base_sha；确认目标不存在后使用 `git clone --no-hardlinks "$repair_primary_root" "desktop/backend/runtime/repair_checkouts/$repair_task_id"`，进入该clone执行 `git switch -c "codex/<对应任务小写>-<topic>" "$repair_base_sha"`。任务ID和分支以P04/owner policy校验，不使用主工作区共享.git。每个子agent的project root是该独立clone，运行P02缓存/临时目录准备，根外只读。

协调者在D19.8选定的干净集成checkout只fetch已审核task commit并按merge_prerequisites集成；子agent不得push回主仓库或改兄弟clone。完成并核验commit与证据已收集后，协调者只删除本次创建且位于repair_checkouts内的对应目录；未收集/未合并/仍运行的任务保留，主项目手动文档维护和其他runtime数据不清理。清理前核对规范路径、task_id和已收集source SHA，不用通配符清理整个runtime。


### D19.8 集成、checker自检与Node工具链

**集成主体：**仅协调者在主集成checkout执行fetch/merge，子包禁push回主仓库不妨碍协调者读取子包。主集成checkout若有用户未提交维护内容，保留这些内容，使用repair_checkouts/_integration独立clone作集成，不自动stash/reset/提交用户改动：协调者在主项目根执行git clone --no-hardlinks "$repair_primary_root" desktop/backend/runtime/repair_checkouts/_integration，再进入该clone建立codex/repair-integration分支；目录已存在时只复用已登记的协调者checkout，未知目录拒绝覆盖。以下命令的cwd为已选干净集成checkout，repair_primary_root是主项目绝对根，repair_task_id/repair_task_branch/repair_task_sha来自已审核ownership/manifest；先校验路径仍位于主项目repair_checkouts内，分支与task_id匹配、无未提交差异。

```bash
git fetch --no-tags "$repair_primary_root/desktop/backend/runtime/repair_checkouts/$repair_task_id" "$repair_task_branch"
repair_fetched_sha=$(git rev-parse FETCH_HEAD)
test "$repair_fetched_sha" = "$repair_task_sha"
git merge --no-ff --no-edit "$repair_task_sha"
git merge-base --is-ancestor "$repair_task_sha" HEAD
```

不cherry-pick，因为它改变SHA并破坏prerequisite_commits祖先约束。按P04 merge_prerequisites拓扑集成：R00先，独立子包相互可任意顺序；全部真实Producer和R10a完成后R10b，随后R15b，再R16。contract-only依赖不强制先合并。对顺序共享文件必须满足其前序owner已合并。出现冲突停止当前merge并由相关owner修复源分支，不能在协调者处擅改跨owner逻辑；验收新的source SHA后重新fetch。每次成功合并记录集成HEAD和task SHA，最终integration manifest提供全部映射。

**R00自检：**R00先提交checker、新policy、合同与测试，再在该提交HEAD使用HEAD版本checker和policy验证自身；不使用旧F01 policy，不设置skip-owner/bootstrap绕过。new policy必须含自身路径docs/contracts/shortlab_repair_ownership_policy.json、scripts/check_shortlab_owner.py，以及R00全部新建合同/schema/fixture/测试路径，owner_sequence=[R00]，stage_prerequisites为空。R00 ownership的prerequisite_commits=[]、interface_dependencies=[]、handoffs=[]合法，不为R00要求R00先祖；baseline_sha为R00提交前基线。

```bash
repair_r00_source_sha=$(git rev-parse HEAD)
python scripts/check_shortlab_owner.py --repo . --policy docs/contracts/shortlab_repair_ownership_policy.json --manifest "desktop/backend/runtime/verification/repair/R00/$repair_r00_source_sha/ownership.json" --base-ref "$repair_r00_base_sha" --head-ref HEAD
```

工作树checker/policy字节必须与git show HEAD对应blob一致；manifest位于runtime不进入被测diff。测试覆盖自检0、policy缺自覆盖项1、非法/缺required字段2、空prerequisite合法、非R00越权1。R00产自检manifest/source SHA后其他任务才开工；实现更正产生新HEAD必须再自检，不能复用旧结果。

**Node：**统一开发/构建/验收为Node22.x；desktop/ui/package.json engines为>=22 <23，package-lock.json根包engine同步。R15a只添加Playwright1.56.0与测试script/对应lockfile；R16基于其合并版本修改engines/lockfile，并将.github/workflows/ci.yml及release.yml所有Node setup/build job固定22，桌面打包中需构建UI的job也显式setup-node22。R16不得覆盖或重新选择R15a依赖版本；运行Node22的npm install --package-lock-only后核验依赖版本未漂移，再npm ci/test/build/browser测试。成品桌面应用运行不要求用户安装Node。

R00 policy对package.json/package-lock.json写owner_sequence=[R15a,R16]及R16前置R15a；ci.yml/release.yml owner=[R16]。P02准备先检查node --version为v22.*，不自动安装到根外；不足则环境UNVERIFIED而非拿Node20产物冒充。R16静态测试冻结engines/lock根engine与CI22，实际manifest记录node/npm版本。
