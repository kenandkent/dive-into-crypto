# short-lab 一体化升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 本计划由第三方执行；编写计划不代表授权在当前会话启动开发。

**Goal / 目标**：补齐当前Short-Lab基础数据与审计链，在同一桌面应用中交付资金费机会、现货对冲规划、手工成交账本和风险提醒。\
**Architecture / 架构**：一个FastAPI、一个进程内Scheduler、一个DuckDB连接与串行worker。旧Scanner保持兼容，基础来源/纯计算/账本/API/UI按固定合同分层，禁止直接执行交易。\
**Tech Stack / 技术栈**：Python>=3.12、FastAPI、aiohttp、DuckDB、PyYAML、Decimal、React 18、Node>=18、esbuild、PyInstaller。\
**Spec / 唯一设计合同**：[ShortLab_Integrated_Upgrade_Design_CN.md](ShortLab_Integrated_Upgrade_Design_CN.md)；以A/B/C章节与AC01–AC23编号定位，不以文档行数定位。

## 0. 使用规则

- 设计规定业务行为，本计划规定执行顺序/接口/文件责任/测试证据；不改变设计公式、阈值、表名或API。
- 任何本计划示例只是执行与断言合同，不表示这些功能当前已实现。既有设计验证脚本只检查设计，不等于F/H任务完成。
- 本计划冻结的新增公开域接口由其owner按指定签名实现；H01/F01统一DTO，不允许consumer自行定义近似字段。字典输入必须按设计schema验证。
- 所有路径以项目根为基准；Python包前缀desktop/backend/src/diveintocrypto_desktop，backend tests前缀desktop/backend/tests，UI前缀desktop/ui。
- 文件边界：项目根内可按授权修改；项目根外只读，除非用户明确授权精确路径。必须把边界规则交给每个子agent。

## 1. Global Constraints / 全局约束

- 产品short-lab；默认127.0.0.1:46408；保留dive-desktop和diveintocrypto_desktop导入包。
- 单FastAPI/Scheduler/DuckDB worker；不新增Redis/Celery/独立交易服务/第二端口，Android不修改。
- 禁止私有订单/账户/持仓API、钱包密钥/签名、approve、swap广播、自动补保证金或自动平仓。
- 001–003只读；004基础、005对冲，每文件单事务与schema行原子提交；005失败基础4可用。
- 全局universe500、shortlist50、Entry10/并发2/240实际send；Fund80尝试/300秒，page/retry均扣。
- 数量/原生价Decimal字符串；账本从事件精确累计，DOUBLE仅投影；USD名义金额不重复乘倍率。
- 来源unknown不补0/N/A；known_at不能晚于冻结cutoff；历史重放只用当时存值与policy。
- FCS独立，固定满分100，可靠90D N/A上限90；不能归一。费用、Basis和Funding不重复计入。
- 10秒内存monitor、60秒/状态变化持久、jitter0，队列滞后5秒degraded不改真实持仓。
- 首链Ethereum/0x v2 price仅INDICATIVE；无key零请求，Gas/账户证明不足LIMITED，未验证不启用。
- 默认中文/TR/EN，quantity不得转JS Number作为订单或余额校验；产品失联不fallback mock。

## 2. Review Focus / 必须额外覆盖的风险

| 输入/条件 | 固定预期 | owner |
|---|---|---|
| 响应晚于cutoff且恰逢UTC跨日 | 重新对齐/明确缺失，不能沿用旧日期时间宣称新鲜 | F02/F06 |
| 同事件并发提交、HTTP超时后重试、simulation已经过期 | 同client ID原内容返回既有结果，版本校验在事务，不能重复成交 | H01/H06/H08 |
| 部分平仓后出现tiny dust、base币手续费 | 精确净量和剩余量，不能用float或epsilon把余额吞掉 | H01/H06 |
| 网络失败/DB队列滞后/电脑睡眠 | monitor degraded、真实状态保留、失败不是恢复，critical alert不能静默丢 | H07/H08 |
| 有当前买quote但无sell/Gas/历史quote/FX | DRAFT/LIMITED或UNAVAILABLE；不能用未来值填历史或显示READY | H02/H05/H10 |

## 3. 启动基线与环境

- [ ] 执行git rev-parse HEAD、git branch --show-current、git status --short并记录工作区内容SHA；已有未提交修改不覆盖/清理。
- [ ] 从项目根执行以下不同测试集；无依赖明确ENVIRONMENT_UNAVAILABLE，不用系统Python代替backend环境。只在获准且网络可用时运行uv sync/npm ci。

```text
uv run --project desktop/backend pytest desktop/backend/tests/ -q
uv run --project desktop/backend pytest tests/ -q
npm --prefix desktop/ui test
npm --prefix desktop/ui run build
```

构建会改变已提交dist，启动基线阶段保存产物差异但不混入其他任务提交。无网络可使用已安装desktop/backend/.venv/bin/python执行相同pytest路径。Windows用.venv/Scripts/python.exe。验证临时目录统一项目内backend/runtime/verification/<build-id>/tmp；执行前创建并设置TMPDIR/TEMP/TMP指向它，不修改HOME/CODEX_HOME。

## 4. 并行与合并编排

| 波次 | 可执行任务 | 共享文件策略 |
|---|---|---|
| 0 | 基线、固定fixture准备 | 仅准备合同数据，不提前实现域逻辑 |
| 1 | F01→F02→F03→F04→F05串行合并 | 同名DTO/HTTP/Spot/配置按C2交接 |
| 2 | F06a先交基础运行合同；F07先冻结summary DTO+fixture，再与F08a实现并行，F09维护模块/资源可并行 | F06独占service/runtime；F09资源调用点等owner交接 |
| 3 | F09资源/维护与F07回调交付后，F06b统一接线；F08默认运行E2E | 基础最终gate后F09交repository/config给H01 |
| 4 | H01冻结；H02/H03/H04/H05/H09测试UI并行；H10纯域在H03后开发 | 每agent独占域文件；不编辑service/router/repo/config/dist |
| 5 | H06→H07；H10无router依赖可并行完成 | 账本先于PnL/告警，接口fixture已冻 |
| 6 | H08统一接线；H09对真实API验收 | H05未配置不能阻塞Spot，不能计PoC通过 |
| 7 | H11统一回归、dist单独提交、成品与证据 | 不同UI分支禁止各自重建bundle |

F06a/F06b是同一任务的两个交付门槛，不是两个owner；F07/F08/F09对F06的依赖指a合同，F06最终完成必须通过b，因此不存在“等待grader但grader等待完整Runtime”的环。F09可先交域模块/资源，再等b做基础成品验收。H10只依赖冻结repo/域合同，不依赖H08；H08消费H10回调。

### 4.1 子agent工作包与公共文件锁

| parent / 子工作包 | 专属写文件 | 消费合同 | 合并负责人 |
|---|---|---|---|
| F08a API | api/app.py、api/shortlab.py、backend API测试 | F06服务/F07已冻结summary DTO+fixture | F08 |
| F08b UI | data.js、desktop-app.jsx、i18n.js、ShortLab页面、build.mjs、UI测试 | F08a冻结response/query | F08 |
| H02a rules/Spot | data/trading_rules.py、data/spot.py、venues/base.py/binance_spot.py | H01 DTO/F04 identity | H02 |
| H02b Alpha | data/binance_alpha.py、venues/binance_alpha.py、独立Alpha测试/fixture | H02a已冻结parser签名，可用fixture先开发 | H02 |
| H09a Funding页 | funding-view.jsx、对应test/hedge-funding.test.mjs | DIVE.fundingOpportunities/hedgeVenues | H09 |
| H09b Planner页 | hedge-planner.jsx、test/hedge-planner.test.mjs | DIVE.hedgeSimulate/getHedgeSimulation/createHedgePlan | H09 |
| H09c Monitor页 | hedge-monitor.jsx、test/hedge-monitor.test.mjs | DIVE.hedgePlan/hedgeMonitor/applyHedgeLegEvent | H09 |
| H09d Alerts页 | hedge-alerts.jsx、test/hedge-alerts.test.mjs | DIVE.hedgeAlerts/ackHedgeAlert | H09 |
| H09e 公共UI接线 | short-lab-evidence.jsx（F08交接）、data.js、desktop-app.jsx、i18n.js、hedge-format.js、build.mjs、test/hedge.test.mjs | 各页事件/props与B32合同 | H09，其他页面owner不可写这些文件 |

F08a/b和H02a/b都是同一parent允许路径的受控委派，parent manifest列child/path，不产生第二owner；只合并已通过合同的子包。H09e先冻结DIVE方法/format接口供页面消费，页面独立测试可以stub这些方法；e最后汇总语言key清单、导航、真实API适配与build顺序。任何子agent新增公共字段请求回交parent/H01；不得绕过H01自改DTO。

UI遵当前build.mjs的全局拼接结构：不把新页面擅自改成ES模块体系。F08b把short-lab-evidence.jsx加入FILES，H09e把hedge-format.js与四页面加入，全部位于desktop-app.jsx之前且format先于页面；保留React/L()/window.DIVE公共访问方式。H11独占最终dist生成，不在页面PR顺手提交bundle。

页面函数合同冻结为FundingView({onSelectSymbol,onPlan,query})、HedgePlanner({symbol,simulationId,onPlanSaved})、HedgeMonitor({planId,onBack})、HedgeAlerts({planId,onOpenPlan})；事件回调传symbol/planId字符串，不传整份未经验证API body。单页不得重新定义全局formatter、API client或语言catalog。

## 5. 每任务统一执行与交付程序

每任务下列步骤均需打勾，任务自己的断言列表是强制测试，不可用统一步骤替代具体内容。

- [ ] 用只读owner工具验证baseline、前置commit、文件清单、handoff；按codex/fNN-/codex/hNN-创建分支。F01 bootstrap例外只允许它自身。
- [ ] 读取指定设计段与本任务接口；保存固定输入fixture（raw金额/时间/units显式），编写后文列明的失败测试。
- [ ] 执行任务测试确认red来自目标缺行为/断言，不能把缺依赖/收集失败当red。
- [ ] 本任务测试命令所列backend/tests和UI/test文件默认列入本任务白名单；ownership_policy按具体路径/task登记，不能全局放行tests/**。自己的新增fixture使用fixtures/shortlab/<task_id>/或fixtures/hedge/<task_id>/，公共fixture不覆盖；重复测试文件遵F06→H08、F08→H09、F09→H11、H01→H06交接。按本任务算法步骤逐段实现，每段只包含一个合同变化；不复制第二default、第二DTO或第二客户端。
- [ ] 执行同测试确认green；接线任务必须经过默认Runtime/真实FastAPI，不仅依赖人工注入fake service。
- [ ] 再运行直接依赖的回归测试；只为具体风险扩大验证范围，无key/live/成品未运行分别记状态。
- [ ] 保存manifest、输出SHA、命令/exit code、工作区SHA与ownership；提交只包含允许路径，PR粘贴证据摘要与handoff，待review后合并。

建议每个可独立核验的小步骤一次提交；命令git add必须显式文件，禁止git add .带入其他agent变更。与本任务无关的现有修改保留。

## 6. 共享类型、fixture与测试约定

F01为基础Record合同owner，F02为ObservationMeta/Observed owner，F05为FeatureInputs/QualityPolicy owner，H01为全部Hedge DTO owner。consumer只import，不重新定义。async为I/O边界，纯score/planner/aggregate为同步无网络。

Fixture根固定backend/tests/fixtures/shortlab和hedge：Funding升序raw页、Kline index5与7判别数组、规则filters、真假identity、两侧盘口、0x price/Gas缺失、旧schema、三hash JSON/.sha256。每个fixture伴meta.json记录单位/时间/来源/schema/checksum/live-or-synthetic，不在产品读取测试fixture。H01负责hedge公共fixture命名；H02/H05增加raw provider子目录，禁止覆写公共币单位。

所有任务测试文件完整前缀backend/tests；UI:标记对应desktop/ui/test。consumer unit可用自己文件内Fake，但返回必须满足冻结DTO；不同agent不得修改全局conftest.py。需要公共fixture helper时回交F01/H01，不偷偷改共享测试设施。

---

## F01：合同、来源持久化与执行权限

**依赖**：启动基线通过。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC05 AC20**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `scripts/check_shortlab_owner.py`
- `docs/contracts/shortlab_ownership_policy.json`
- `docs/contracts/ownership.schema.json`
- `docs/contracts/shortlab_pr_evidence_template.md`
- `desktop/backend/pyproject.toml`
- `desktop/backend/uv.lock`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/models.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/migrations/004_core_completion.sql`

**输入/输出接口合同**

保留ProviderResult已有签名。Repository新增async upsert_asset(record)、upsert_asset_mapping(record)、save_identity_snapshot(record)->str、get_identity_snapshot(snapshot_id)->record|None、save_contract_rules_snapshot(record)->str、get_contract_rules_snapshot(snapshot_id)->record|None、latest_contract_rules(symbol,cutoff_ms)->record|None、save_contract_lifecycle(record)、latest_contract_lifecycle(symbol)->record|None、list_tracked_symbols()->tuple[str,...]、upsert_funding_events(events)、list_funding_events(symbol,start_ms,end_ms)->tuple、save_funding_observation(record)、save_fundamental_snapshot(record)->str、get_fundamental_before(canonical_id,cutoff_ms)->record|None、save_config_snapshot(record)->str、get_config_snapshot(policy_hash)->record|None、save_cursor(job_type,key,value)、load_cursor(job_type,key)->dict|None、list_scores_for_evidence(filters,limit,offset)->page、list_due_scores(now_ms,limit)->tuple、maintain_retention(policy,now_ms,limit=1000)->RetentionStats(deleted,retained,errors,as_of_ms)。record字段由设计A6原表/DDL逐列映射，所有dict须strict字段校验；冻结Record dataclass在repository.py定义并export，后续不得另造类型。

**实施步骤与禁止替代方案**

- [ ] F01.1：逐文件读取001–003，不修改内容
- [ ] F01.2：004逐字采用A6.2。SCHEMA_VERSION/MIGRATIONS加入4，schema_target默认4。每文件单事务，版本行同事务。一个连接、一个容量256待执行操作的优先worker队列；ingestion.db_queue_limit与hedge.runtime.db_queue_limit启用时必须相等，队列满精确HTTP503 LOCAL_WRITE_BUSY，无事务提交。256不是每批1000记录限制
- [ ] F01.3：同优先FIFO、每30秒提升档次，提交方法携带priority与trace。初始RUNNING恢复为API兼容FAILED/PROCESS_INTERRUPTED。所有immutable记录同ID同内容幂等、异内容拒绝
- [ ] F01.4：Fund公共事件冲突保留observation版本。owner脚本按C3.2检查git祖先、目标分支前置、path与handoff，返回0/1/2，参数数组调用git
- [ ] F01.5：仅F01 bootstrap可无已存在工具。声明eth-hash[pycryptodome]并更新lock，不安装交易SDK。

**必须编写的行为断言**

- [ ] 1/3 schema升级4、重复执行版本不重写；中段DDL失败回滚
- [ ] 基础表写入后重启读回且原score仍可查
- [ ] 未知/越权path及未合并前置commit均失败；正确handoff通过
- [ ] 队列FIFO/aging、事务引用与失败不留下半记录

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_repository.py desktop/backend/tests/test_shortlab_core_persistence.py desktop/backend/tests/test_shortlab_owner_check.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 1/3 schema升级4、重复执行版本不重写；中段DDL失败回滚 | 1/3 schema升级4、重复执行版本不重写；中段DDL失败回滚 |
| 基础表写入后重启读回且原score仍可查 | 基础表写入后重启读回且原score仍可查 |
| 未知/越权path及未合并前置commit均失败；正确handoff通过 | 未知/越权path及未合并前置commit均失败；正确handoff通过 |
| 队列FIFO/aging、事务引用与失败不留下半记录 | 队列FIFO/aging、事务引用与失败不留下半记录 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC05 AC20证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## F02：观测包装、单位、规则原文与UTC窗口

**依赖**：F01合同合并。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC02 AC03**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/observations.py`
- `desktop/backend/src/diveintocrypto_desktop/data/universe.py`
- `desktop/backend/src/diveintocrypto_desktop/data/open_interest.py`
- `desktop/backend/src/diveintocrypto_desktop/data/binance_klines.py`
- `desktop/backend/src/diveintocrypto_desktop/data/funding.py`
- `desktop/backend/src/diveintocrypto_desktop/data/spot.py`

**输入/输出接口合同**

ObservationMeta/Observed[T]冻结dataclass放observations.py；Observed.value为原返回对象、meta遵守A4。to_legacy(observed)->T返回原value。validate_observation(observed,cutoff_ms,max_future_skew_sec=2)->Observed；canonical_price(native_price:Decimal,multiplier:Decimal,fx:Decimal)->Decimal；canonical_qty(native_qty:Decimal,multiplier:Decimal)->Decimal。wrapper函数接as_of_ms/接收时钟/identity ID，旧data函数签名与类型保留。

**实施步骤与禁止替代方案**

- [ ] F02.1：只先包装Funding/Kline/OI/Spot四链。known_at取响应完成时间，cache命中保留原时间，源time未知为null。存exchangeInfo原filters/time而不在本任务构建第二parser。qv取raw[7]、闭日UTC对齐，缺日不得代0。OI7D两端误差至多5分钟
- [ ] F02.2：原生quantity与USD名义金额分开
- [ ] F02.3：量纲未知返回带reason的null。跨日冻结重新对齐两端，禁止负age归零。

**必须编写的行为断言**

- [ ] native .006/m1000/ATH .000012回撤应-.5；名义OI/qv不缩放
- [ ] raw index5与7不同、重复/缺失/未闭日均识别
- [ ] 未来known_at拒绝、to_legacy对象/类型保持、cache不更新known_at
- [ ] OI与价格窗口错位超过5分钟不可用

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_liquidity_units.py desktop/backend/tests/test_shortlab_quote_volume.py desktop/backend/tests/test_shortlab_decision_time.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| native .006/m1000/ATH .000012回撤应-.5；名义OI/qv不缩放 | native .006/m1000/ATH .000012回撤应-.5；名义OI/qv不缩放 |
| raw index5与7不同、重复/缺失/未闭日均识别 | raw index5与7不同、重复/缺失/未闭日均识别 |
| 未来known_at拒绝、to_legacy对象/类型保持、cache不更新known_at | 未来known_at拒绝、to_legacy对象/类型保持、cache不更新known_at |
| OI与价格窗口错位超过5分钟不可用 | OI与价格窗口错位超过5分钟不可用 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC02 AC03证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## F03：真实HTTP预算、唯一重试与跨轮缓存

**依赖**：F02合并并交接funding.py。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC03 AC04 AC07**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/data/http.py`
- `desktop/backend/src/diveintocrypto_desktop/data/funding.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/request_budget.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/entry.py`

**输入/输出接口合同**

RequestBudget.try_acquire(host,weight,job_type,endpoint_family)->Permit|Denied；Permit.mark_sent()/release_unsent()；RequestContext含job budget/host family/trace。get_json新增可选request_context，其余参数兼容；EntryBudget接受共享cache及request context，guarded只查cache/调用factory，不再retry。cache.get(key,cutoff_ms)->Observed|None，put(key,observed,expires_at_ms)，共享实例由F06持有。

**实施步骤与禁止替代方案**

- [ ] F03.1：HTTP唯一send点原子预留、实际发送标sent
- [ ] F03.2：重试/页均计，已发送取消不退。保留HTTP Retry-After与已有ratio/OI保护，未知endpoint weight禁止发。weight清单版本化且按limit参数选项，不能仅看exchangeInfo rateLimits猜endpoint权重。monitor20%、Scanner30%保留预算，背景只用剩余。Entry18叶请求不是重试上界
- [ ] F03.3：10币180理想/240硬上限，30币540排队。Fund80真实尝试/300秒共享窗口。

**必须编写的行为断言**

- [ ] budget1最终send<=1，budget240<=240，两个并发job不超窗
- [ ] 连续429、满页终止、分页取消均计数
- [ ] 重试层只留一套；共享cache跨两轮命中且源时间不刷新
- [ ] 30币queued明确且无额外隐形请求

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_http_retries.py desktop/backend/tests/test_shortlab_entry.py desktop/backend/tests/test_shortlab_http_budget.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| budget1最终send<=1 | budget1最终send<=1，budget240<=240，两个并发job不超窗 |
| 连续429、满页终止、分页取消均计数 | 连续429、满页终止、分页取消均计数 |
| 重试层只留一套；共享cache跨两轮命中且源时间不刷新 | 重试层只留一套；共享cache跨两轮命中且源时间不刷新 |
| 30币queued明确且无额外隐形请求 | 30币queued明确且无额外隐形请求 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC03 AC04 AC07证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## F04：生产身份目录与安全地址映射

**依赖**：F03合并，F02交接Spot包装。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC02 AC11 AC12**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/identity/resolver.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/identity/overrides.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/identity/catalog.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/identity/verified_assets.yaml`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/providers/base.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/providers/coingecko.py`
- `desktop/backend/src/diveintocrypto_desktop/data/spot.py`

**输入/输出接口合同**

IdentityCatalog.candidates(symbol,cutoff_ms)->tuple[Mapping,...]；refresh(request_context)->ProviderResult；load_verified_assets()->mapping。normalize_chain_address(chain_id,address)->NormalizedAddress(canonical_key,display,validation_status)。load_effective_overrides(path,embedded)->versioned mapping。已有resolve_identity入口保持。

**实施步骤与禁止替代方案**

- [ ] F04.1：内置验证集启动先用，普通1x与1000币明确来源。后台coins/list include_platform=false缓存24h/grace72h/32MiB上限
- [ ] F04.2：至多50绑定资产补platform详情。DEMO与PRO host/header严格匹配，无key只本地验证集且网络UNCONFIGURED。overlay优先且语法错不可静默置空。EVM使用Ethereum Keccak校验EIP55
- [ ] F04.3：Solana32-byte base58保留大小写
- [ ] F04.4：未知链不lower。Spot不存在需可信exchangeInfo证明，403/451/429不作N/A。FULL no-fetcher/.example不得注册为已验证能力。

**必须编写的行为断言**

- [ ] 重名/跨链冲突不自动匹配；1000不猜倍率
- [ ] EIP55正确/错checksum、Solana大小写不同key不可合并
- [ ] 目录失败保留旧known_at、overlay错误明确失败
- [ ] 无key不请求另一host，FULL骨架不具备READY能力

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_identity.py desktop/backend/tests/test_shortlab_spot_history.py desktop/backend/tests/test_shortlab_identity_catalog.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 重名/跨链冲突不自动匹配；1000不猜倍率 | 重名/跨链冲突不自动匹配；1000不猜倍率 |
| EIP55正确/错checksum、Solana大小写不同key不可合并 | EIP55正确/错checksum、Solana大小写不同key不可合并 |
| 目录失败保留旧known_at、overlay错误明确失败 | 目录失败保留旧known_at、overlay错误明确失败 |
| 无key不请求另一host | 无key不请求另一host，FULL骨架不具备READY能力 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC02 AC11 AC12证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## F05：冻结配置、完整输入与DQ/风险

**依赖**：F04及F02/F03接口合并。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC02 AC06 AC08**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/config.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/default.yaml`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/quality.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/risk/veto.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/scoring/versions.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/inputs.py`

**输入/输出接口合同**

build_feature_inputs(symbol,observations,identity,decision_as_of_ms,policy)->FeatureInputs；build_field_states(inputs,policy)->tuple[FieldState,...]；quality.data_quality(tier,states,as_of_ms,policy=None) additive；evaluate_risks/derive_status新增显式policy阈值参数，默认旧值。配置快照和policy hash由F01存，new inputs版本features-v2、Entry标entry-v2。

**实施步骤与禁止替代方案**

- [ ] F05.1：A10默认与B40扩展的层级以设计为准，本任务只加入基础键
- [ ] F05.2：Hedge键H01后加。current_price/ATH统一canonical USD
- [ ] F05.3：rates30D完整升序、OI7D与price7D同包、Basis/双侧深度真实状态。watch/candidate/veto/TTL均一次读取冻结policy。保留原LTSS/Entry分箱和舍入，不重分权重
- [ ] F05.4：legacy policy缺失不得用今天配置重算历史。

**必须编写的行为断言**

- [ ] 完整funding序列不再None，7D风险用同一计算值
- [ ] 改watch/candidate/TTL/veto后新结果变化，旧快照仍相同
- [ ] 盘口成功DQ不能NOT_WIRED、单侧缺失不伪造双侧
- [ ] 旧4909ffe7…hash及数学golden保持，错误输入修复以features-v2区分

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_config.py desktop/backend/tests/test_shortlab_scoring.py desktop/backend/tests/test_shortlab_status.py desktop/backend/tests/test_shortlab_production_inputs.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 完整funding序列不再None | 完整funding序列不再None，7D风险用同一计算值 |
| 改watch/candidate/TTL/veto后新结果变化 | 改watch/candidate/TTL/veto后新结果变化，旧快照仍相同 |
| 盘口成功DQ不能NOT_WIRED、单侧缺失不伪造双侧 | 盘口成功DQ不能NOT_WIRED、单侧缺失不伪造双侧 |
| 旧4909ffe7…hash及数学golden保持 | 旧4909ffe7…hash及数学golden保持，错误输入修复以features-v2区分 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC02 AC06 AC08证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## F06：基础来源落库、后台公平回填与生命周期

**依赖**：F05合并；F07/F09按本任务冻结callback合同开发。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC03 AC05 AC07 AC08**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/service.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/runtime.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/scheduler.py`

**输入/输出接口合同**

Runtime注入IdentityCatalog、RequestBudget、ObservedCache、Repository、QualityPolicy、clock；导出JobContext(repository,config,clock_ms,request_budget,trace_id,data_dir)供所有后台域消费，返回现有JobStatus，consumer不能自造context。job adapters统一async run(context)->JobStatus；grader callback async run_due(context)->JobStatus由F07提供，retention callback async maintain(context)->JobStatus由F09提供。service保持refresh/job_status/candidates/detail公开入口。

**实施步骤与禁止替代方案**

- [ ] F06.1：分F06a与F06b：a先合并数据/运行合同与采集链，让F07/F08/F09开发
- [ ] F06.2：b待F07/F09交付后由同owner接入回调并跑default wiring，不由域任务改runtime。Fund页队列/cursor/nextAllowed持久
- [ ] F06.3：cache TTL内不因无额度清空。tracked=live+历史分数+有仓资产
- [ ] F06.4：逐币收集冻结后decision cutoff再score。基础表先写并引用ID，score批与SUCCEEDED原子提交。stop等待scheduler/service任务后关闭DB，重启恢复游标。

**必须编写的行为断言**

- [ ] 默认Runtime+fake HTTP实际写所有基础表并含引用
- [ ] 500币80页/5分钟公平轮转、重启不断头、缺口增量不重抓90D
- [ ] 每币cutoff可不同，跨日不能错位，孤立feature不可作完成代
- [ ] F07/F09接入后默认grader和维护注册且不另起service

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_runtime.py desktop/backend/tests/test_shortlab_default_wiring.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 默认Runtime+fake HTTP实际写所有基础表并含引用 | 默认Runtime+fake HTTP实际写所有基础表并含引用 |
| 500币80页/5分钟公平轮转、重启不断头、缺口增量不重抓90D | 500币80页/5分钟公平轮转、重启不断头、缺口增量不重抓90D |
| 每币cutoff可不同 | 每币cutoff可不同，跨日不能错位，孤立feature不可作完成代 |
| F07/F09接入后默认grader和维护注册且不另起service | F07/F09接入后默认grader和维护注册且不另起service |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC03 AC05 AC07 AC08证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## F07：长期Evidence与历史视图防live污染

**依赖**：F06a合同已合并；不等待F06b。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC09**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/grader.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/metrics.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/jobs.py`
- `desktop/backend/src/diveintocrypto_desktop/scan/symbol_builder.py`

**输入/输出接口合同**

先交现有EvidenceSummary扩展DTO与summary fixture（字段与API aliases），F08a确认合同后与F07逻辑并行，不等待全部grader完成。async run_due(context)->JobStatus及build_metrics_provider(repository,config,clock)输出交F06b；Repository.list_due_scores/list_scores_for_evidence由F01提供。现有grade入口与OutcomeRecord不改名。

**实施步骤与禁止替代方案**

- [ ] F07.1：默认180天SUCCEEDED历史，当前feature/Entry/cost版本分桶
- [ ] F07.2：每symbol/profile/UTC日首个预先符合策略资格样本，原全snapshot统计另列。7/30/90D，未到期PENDING、到期未处理UNAVAILABLE/NOT_GRADED_DUE、缺mark不补0、退市CENSORED保留。旧历史symbol_builder无法截止OI/ratio/funding则显式unavailable，K线仍可算
- [ ] F07.3：不借build_symbol历史重建Entry。

**必须编写的行为断言**

- [ ] 两个跨7D旧批次进入默认汇总，新批次PENDING不能掩盖旧COMPLETE
- [ ] 四状态计数合计且版本/成本不混
- [ ] 缺mark/Funding/退出价按原因保留样本
- [ ] 改变live OI/ratio不改变保存历史Entry

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_evidence.py desktop/backend/tests/test_symbol_builder.py desktop/backend/tests/test_shortlab_evidence_history.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 两个跨7D旧批次进入默认汇总 | 两个跨7D旧批次进入默认汇总，新批次PENDING不能掩盖旧COMPLETE |
| 四状态计数合计且版本/成本不混 | 四状态计数合计且版本/成本不混 |
| 缺mark/Funding/退出价按原因保留样本 | 缺mark/Funding/退出价按原因保留样本 |
| 改变live OI/ratio不改变保存历史Entry | 改变live OI/ratio不改变保存历史Entry |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC09证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## F08：基础API、换代UI与本地写保护

**依赖**：F06a+F07冻结DTO；可与F09资源工作并行，测试接F06b。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC06 AC10 AC21**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/api/shortlab.py`
- `desktop/backend/src/diveintocrypto_desktop/api/app.py`
- `desktop/ui/build.mjs`
- `desktop/ui/src/app/data.js`
- `desktop/ui/src/app/desktop-app.jsx`
- `desktop/ui/src/app/i18n.js`
- `desktop/ui/src/app/shortlab/short-lab-view.jsx`
- `desktop/ui/src/app/shortlab/short-lab-detail.jsx`
- `desktop/ui/src/app/shortlab/short-lab-evidence.jsx`

**输入/输出接口合同**

DIVE.shortRefreshStatus(jobId)->Promise<JobStatus>；shortEvidence(filters)->Promise<EvidenceSummary>；latest generation使用health.lastSuccessfulGeneration。原API fields保持，health additive capabilities/jobs/schema_version。请求状态由页面自身管理，App不另轮询同query。

**实施步骤与禁止替代方案**

- [ ] F08.1：手动202不当完成
- [ ] F08.2：2秒job status至成功/失败/超时，成功后offset0换generation。普通分页锁代，新代提示。AbortController/request sequence拒绝晚回响应，失败保留旧body并stale。中文默认TR/EN完整，Evidence子页真实API。Origin/Host/JSON校验
- [ ] F08.3：同源正常，localhost开发跨源OPTIONS须允许PATCH。不修改dist。

**必须编写的行为断言**

- [ ] 旧generation分页稳定，job完成切新代且晚响应不覆盖
- [ ] 接口503不mock，Evidence缺数据原因可见
- [ ] 同源PATCH与受支持跨源preflight、恶意Origin拒绝
- [ ] 路由中文/i18n与现有导航不回退

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_api.py -q
```

```text
node --test desktop/ui/test/shortlab.test.mjs desktop/ui/test/shortlab-refresh.test.mjs
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 旧generation分页稳定 | 旧generation分页稳定，job完成切新代且晚响应不覆盖 |
| 接口503不mock | 接口503不mock，Evidence缺数据原因可见 |
| 同源PATCH与受支持跨源preflight、恶意Origin拒绝 | 同源PATCH与受支持跨源preflight、恶意Origin拒绝 |
| 路由中文/i18n与现有导航不回退 | 路由中文/i18n与现有导航不回退 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC06 AC10 AC21证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## F09：资源、维护与基础发布门槛

**依赖**：F07/F08交付、F06a；维护callback交F06b，源码资源交H01前冻结。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC01 AC20 AC22 AC23**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/short-lab.spec`
- `desktop/backend/src/diveintocrypto_desktop/__main__.py`
- `desktop/backend/src/diveintocrypto_desktop/resources.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/maintenance.py`
- `desktop/backend/src/diveintocrypto_desktop/engine/loader.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/config.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/identity/overrides.py`
- `.github/workflows/release.yml`
- `desktop/backend/pyproject.toml`
- `README.md`
- `docs/api.md`
- `docs/testing.md`
- `docs/packaging.md`

**输入/输出接口合同**

read_resource_text(relative_name)->str统一importlib.resources，fallback仅经成品测；async maintain(context)->JobStatus交F06b。资源调用点可改但不得改域算法；只调用F01已交付的maintain_retention方法，不新增SQL/query或接管Repo域方法；H01在接管后扩展同名方法的005 pin保护，维护callback仍不变。禁止旁开DB连接。

**实施步骤与禁止替代方案**

- [ ] F09.1：打包engine/两套identity YAML/全部001–005资源/UI/DuckDB。此阶段005尚未实现时spec按已存在资源收集，H11必须验证完整005清单，不能制造占位005。端口46408 AST及发行说明一致。基础180天引用保护、每批1000与aging
- [ ] F09.2：readonly安装目录真实process两次启动。来源配置/tmp全部项目内
- [ ] F09.3：short-lab-v*桌面、v*Android，不改Android逻辑。

**必须编写的行为断言**

- [ ] engine/default/identity/迁移可在无源码checkout成品读到
- [ ] 只读安装、空数据目录、两次启动一致
- [ ] retention保护未到期outcome/关联policy、批次有限
- [ ] 标签互斥与默认46408，bootstrap/基础成品不要求Hedge已存在

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_packaging.py desktop/backend/tests/test_shortlab_release_workflow.py desktop/backend/tests/test_shortlab_retention.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| engine/default/identity/迁移可在无源码checkout成品读到 | engine/default/identity/迁移可在无源码checkout成品读到 |
| 只读安装、空数据目录、两次启动一致 | 只读安装、空数据目录、两次启动一致 |
| retention保护未到期outcome/关联policy、批次有限 | retention保护未到期outcome/关联policy、批次有限 |
| 标签互斥与默认46408 | 标签互斥与默认46408，bootstrap/基础成品不要求Hedge已存在 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC01 AC20 AC22 AC23证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H01：对冲DTO、精确账本存储、005与hash

**依赖**：F09资源交接+F06b基础验收合并。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC06 AC15 AC20**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/short-lab.spec`（仅005迁移资源收集，F09 review）
- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/__init__.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/models.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/config.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/default.yaml`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/migrations/005_hedge_advisor.sql`
- `desktop/backend/tests/fixtures/hedge/config/fcs_config.json`
- `desktop/backend/tests/fixtures/hedge/config/hedge_cost_config.json`
- `desktop/backend/tests/fixtures/hedge/config/hedge_policy.json`

**输入/输出接口合同**

冻结B9 SpotVenueQuote、TradingRulesSnapshot、OnchainQuote、HedgeSimulationRequest/Result、HedgeEvent/Position/Alert/Monitor DTO及FundingMetrics/FCSResult/DQResult/OrderGuidance/Readiness/LedgerResult/PlanState/HedgeOutcome/HedgeEvidenceSummary、HistoricalMarketProvider/HistoricalPriceBar/HistoricalFundingEvent/HistoricalLifecycle（协议与字段逐项采用设计B39.1.1）（所有H02–H10输出类型均在本任务定义，不由consumer创建）；内部snake_case，API aliases camelCase。qty/price原生字符串。Repo async save/get_hedge_simulation、create/get/list_hedge_plan、apply_hedge_event(plan_id,client_event_id,expected_version,event)->LedgerResult、save/list_snapshot_references、save/latest_monitor、upsert/ack/resolve_alert、save/list_fcs、save/list_venue、save/list_hedge_outcomes。输入记录与返回typed DTO在hedge/models.py定义，B28原列映射不改名。

**实施步骤与禁止替代方案**

- [ ] H01.1：005采用B28十一表/索引/引用合同，每文件单事务版本5，失败基础4可用。config添加B40严格子树三个hash及.sha256，显式声明providers.onchain.api_key_env=SHORTLAB_0X_API_KEY（key/URL/路径不入hash、不落日志）；检查spec已包含005，若F09为整目录动态收集则无需重复条目，但必须由H01测试证明成品资源清单包含005，修改仅限该资源项并交F09 review
- [ ] H01.2：用json loads/type normalization/canonical重算，不比较排版。数量从事件系数指数整数累计，DOUBLE投影不作余额判断。client_id先查同payload幂等再校验version/expiry
- [ ] H01.3：引用写同事务。source/config/identity链pin，孤立simulation30天、普通quote1天，plan引用长期保存。

**必须编写的行为断言**

- [ ] 004→005故障回滚/重复执行，十一表存在且无partial schema5
- [ ] 0.1+0.2=.3、极小dust、污染DOUBLE不影响余额，重启重放相同
- [ ] POST重复client_id在simulation过期后仍返回既有plan
- [ ] 三个hash既定值及CRLF/缩进不变，改变值变化且排除secret

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_hedge_models.py desktop/backend/tests/test_shortlab_hedge_config.py desktop/backend/tests/test_shortlab_hedge_repository.py desktop/backend/tests/test_shortlab_hedge_decimal_ledger.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 004→005故障回滚/重复执行 | 004→005故障回滚/重复执行，十一表存在且无partial schema5 |
| 0.1+0.2=.3、极小dust、污染DOUBLE不影响余额 | 0.1+0.2=.3、极小dust、污染DOUBLE不影响余额，重启重放相同 |
| POST重复client_id在simulation过期后仍返回既有plan | POST重复client_id在simulation过期后仍返回既有plan |
| 三个hash既定值及CRLF/缩进不变 | 三个hash既定值及CRLF/缩进不变，改变值变化且排除secret |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC06 AC15 AC20证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H02：Spot/Alpha双向报价与统一规则

**依赖**：H01合同冻结；Alpha live无权限只交UNCONFIGURED证据不启用。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC12 AC13**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/data/spot.py`
- `desktop/backend/src/diveintocrypto_desktop/data/binance_alpha.py`
- `desktop/backend/src/diveintocrypto_desktop/data/trading_rules.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/base.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/binance_spot.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/binance_alpha.py`

**输入/输出接口合同**

parse_trading_rules(raw,venue,instrument,meta)->TradingRulesSnapshot；TradingRulesCache.get/put保持版本；Venue.quote(identity,canonical_qty:str,request_context)->ProviderResult[SpotVenueQuote]返回同量buy/sell。DepthSnapshot原price/qty字符串并含source/接收区间。cache key含venue/identity/rules/quantity。

**实施步骤与禁止替代方案**

- [ ] H02.1：Futures/Spot/Alpha三类filters共用parser，LIMIT/MARKET规则分别检查，precision不能代tick/step。先买目标量再对同净量卖出
- [ ] H02.2：盘口档位VWAP，不能从1%总深度反推。Alpha www.binance.com官方tokenList/exchangeInfo/ticker/fullDepth，code000000/schema检查，默认depth100不足预算内升500/1000。451/403/429不N/A
- [ ] H02.3：fixture+联网证据才enable。

**必须编写的行为断言**

- [ ] 同量双向、不同quantity不可复用小额quote
- [ ] LOT_SIZE/MARKET_LOT_SIZE/MIN_NOTIONAL/PRICE_FILTER非法拒绝
- [ ] API200业务失败、地域403/451、单侧薄盘诚实
- [ ] 倍率1000现货symbol只取identity，不strip前缀

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_hedge_venues.py desktop/backend/tests/test_shortlab_binance_alpha.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 同量双向、不同quantity不可复用小额quote | 同量双向、不同quantity不可复用小额quote |
| LOT_SIZE/MARKET_LOT_SIZE/MIN_NOTIONAL/PRICE_FILTER非法拒绝 | LOT_SIZE/MARKET_LOT_SIZE/MIN_NOTIONAL/PRICE_FILTER非法拒绝 |
| API200业务失败、地域403/451、单侧薄盘诚实 | API200业务失败、地域403/451、单侧薄盘诚实 |
| 倍率1000现货symbol只取identity | 倍率1000现货symbol只取identity，不strip前缀 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC12 AC13证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H03：Funding统计、FCS和机会排名

**依赖**：H01；纯函数不依赖H02网络，fixture遵守H01 DTO。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC14**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/funding_score.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/opportunity.py`

**输入/输出接口合同**

compute_funding_metrics(events,interval_observations,cutoff_ms,listing_age)->FundingMetrics；score_fcs(metrics,venue,basis,identity,policy)->FCSResult；compute_hedge_quality(field_states,policy,now_ms)->DQResult；rank_opportunities(results,sort,order)->tuple。ConservativeAPR方法/coverage/class随result保存。

**实施步骤与禁止替代方案**

- [ ] H03.1：B6窗口左开右闭、去重排序、未完整null
- [ ] H03.2：Std8h等效ddof0与原LTSS区别
- [ ] H03.3：rolling7D UTC日末P25线性(n-1)*.25且20/60窗口要求。B8/B40分箱逐项读取默认policy，不调权重
- [ ] H03.4：90D可靠N/A固定上限90不归一
- [ ] H03.5：普通缺历史null。排序数值/NULLS LAST/symbol/ID。真实dump50完整90D加年轻组，n不足如实标，分布报告不是收益优化。

**必须编写的行为断言**

- [ ] 每个bin边界和全满100；年龄N/A上限90、未知年龄不N/A
- [ ] coverage fraction高但complete=false仍null
- [ ] interval变化/未知、20/60窗口临界、重复事件处理
- [ ] 真实分布报告source/dump SHA；未完成验收enabled仍false

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_fcs.py desktop/backend/tests/test_shortlab_funding_opportunity.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 每个bin边界和全满100；年龄N/A上限90、未知年龄不N/A | 每个bin边界和全满100；年龄N/A上限90、未知年龄不N/A |
| coverage fraction高但complete=false仍null | coverage fraction高但complete=false仍null |
| interval变化/未知、20/60窗口临界、重复事件处理 | interval变化/未知、20/60窗口临界、重复事件处理 |
| 真实分布报告source/dump SHA；未完成验收enabled仍false | 真实分布报告source/dump SHA；未完成验收enabled仍false |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC14证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H04：两腿规划、成本、压力与人工订单参数

**依赖**：H01；H02规则/quote可由合同fixture提供。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC13 AC17**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/planner.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/simulator.py`

**输入/输出接口合同**

simulate_hedge(request:HedgeSimulationRequest,quotes,identity,rules,funding,policy,now_ms)->HedgeSimulation；build_order_guidance(simulation,rules)->tuple[OrderGuidance]；validate_simulation(result,now_ms)->Readiness。函数不联网、不写plan、不获取账户。

**实施步骤与禁止替代方案**

- [ ] H04.1：ABSOLUTE h1，RELATIVE两种输入互斥
- [ ] H04.2：h_min=1-L/(N*S)，端点0/1提示换mode重提。原生合法lot向下、canonical、Spot合法qty、ratio重算，八步固定。成本按native notional、VWAP包含滑点仅扣一次。收益相对资本投入单列。+100/+200路径过liquidation标INVALID_AFTER_LIQUIDATION，不给虚假终点收益。平台不支持stop或缺强平价只能DRAFT/LIMITED，仍输出人工参数。

**必须编写的行为断言**

- [ ] 1000币、25/50/75%、h端点、同时输入422语义
- [ ] 最小量/价步长/quote过期、rounding drift超过5%不READY
- [ ] 费/滑点/Gas不双计、保守Funding<=0 break-even null
- [ ] 无强平价Safety<=75；CROSSED/无条件单不伪verified

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_hedge_planner.py desktop/backend/tests/test_shortlab_hedge_simulator.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 1000币、25/50/75%、h端点、同时输入422语义 | 1000币、25/50/75%、h端点、同时输入422语义 |
| 最小量/价步长/quote过期、rounding drift超过5%不READY | 最小量/价步长/quote过期、rounding drift超过5%不READY |
| 费/滑点/Gas不双计、保守Funding<=0 break-even null | 费/滑点/Gas不双计、保守Funding<=0 break-even null |
| 无强平价Safety<=75；CROSSED/无条件单不伪verified | 无强平价Safety<=75；CROSSED/无条件单不伪verified |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC13 AC17证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H05：Ethereum/0x只读price适配与独立PoC

**依赖**：H01+F04 checksum依赖；不阻塞Spot/Alpha集成。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC12**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/onchain.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/ethereum_0x.py`

**输入/输出接口合同**

OnchainQuoteProvider.quote_buy/quote_sell(identity,requested_canonical_qty:str,quote_asset,as_of_ms,request_context)->ProviderResult[OnchainQuote]；health(request_context)->ProviderResult[dict]。quote_kind=INDICATIVE。接口HTTP仅getPrice，不含钱包信息。

**实施步骤与禁止替代方案**

- [ ] H05.1：固定chain1/api.0x.org/swap/allowance-holder/price/v2。key用SHORTLAB_0X_API_KEY
- [ ] H05.2：无key零网络health UNCONFIGURED。1RPS并发1，buyAmount和sellAmount互斥原子字符串，sell同净量。Gas可能null
- [ ] H05.3：无账户simulation证明永不自称READY，LIMITED还能参考规划。TTL缺provider值则本地30秒来源明确。冻结官方URL/title/endpoint/schema SHA，其他链UNAVAILABLE，不获取getQuote/calldata/approve。

**必须编写的行为断言**

- [ ] 无key实际测试返回UNCONFIGURED且send0，不skip算通过
- [ ] EIP55正确/错/unknown地址不开网络
- [ ] Gas缺失、无sell route、403/429及过期不补0
- [ ] 有keyPoC独立报告、无key记录未验证不声明完成

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_onchain_quote.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 无key实际测试返回UNCONFIGURED且send0 | 无key实际测试返回UNCONFIGURED且send0，不skip算通过 |
| EIP55正确/错/unknown地址不开网络 | EIP55正确/错/unknown地址不开网络 |
| Gas缺失、无sell route、403/429及过期不补0 | Gas缺失、无sell route、403/429及过期不补0 |
| 有keyPoC独立报告、无key记录未验证不声明完成 | 有keyPoC独立报告、无key记录未验证不声明完成 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC12证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H06：不可变手工成交账本与状态机

**依赖**：H01；H02/H03/H04合同冻结，无网络依赖。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC15**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/ledger.py`

**输入/输出接口合同**

Ledger.apply_event(plan_id,client_event_id,expected_version,event)->LedgerResult调用H01单worker事务；aggregate_events(events,identity,rules)->HedgePosition；evaluate_position_state(position,target_ratio)->PlanState。loop-bound per-plan asyncio.Lock只覆盖提交，不覆盖provider请求。

**实施步骤与禁止替代方案**

- [ ] H06.1：OPEN/CLOSE/LIQUIDATION/ADJUSTMENT/FUNDING_RECEIPT。精确数量系数指数累计，不读DOUBLE SUM余额
- [ ] H06.2：更正事件引用同plan且只撤销一次。真实部分成交/费用扣量/transfer tax净量，dust明确登记。ACTIVE两腿正量且漂移<=5%
- [ ] H06.3：单腿PARTIALLY_FILLED，退出建议独立action
- [ ] H06.4：CLOSED两腿各自剩余0或规则dust，RELATIVE不要求数量相等。实际收据不与估算相加。

**必须编写的行为断言**

- [ ] 并发同版本仅一个提交，重复ID相同payload幂等不同payload409
- [ ] 0.1+.2精确，tiny dust不epsilon吞掉、超量拒绝
- [ ] 部分开/平/更正重启重放一致、期货清算后孤现货
- [ ] 未知数据不阻止记录真实成交、不改真实状态

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_hedge_plan.py desktop/backend/tests/test_shortlab_hedge_decimal_ledger.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 并发同版本仅一个提交 | 并发同版本仅一个提交，重复ID相同payload幂等不同payload409 |
| 0.1+.2精确 | 0.1+.2精确，tiny dust不epsilon吞掉、超量拒绝 |
| 部分开/平/更正重启重放一致、期货清算后孤现货 | 部分开/平/更正重启重放一致、期货清算后孤现货 |
| 未知数据不阻止记录真实成交、不改真实状态 | 未知数据不阻止记录真实成交、不改真实状态 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC15证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H07：缓存监控、PnL、告警与负载harness

**依赖**：H06；H03/H04纯函数合同可用。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC16 AC17 AC18**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/monitor.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/alerts.py`
- `desktop/backend/tests/helpers/hedge_load_harness.py`

**输入/输出接口合同**

compute_monitor(plan,position,market_cache,settled_events,policy,now_ms)->MonitorSnapshot；evaluate_alerts(previous,current,policy)->tuple[AlertChange]；AlertManager.apply(changes)->tuple；Harness.InjectedClock.advance(seconds)、FakeWorker.block/release、fixture builder。

**实施步骤与禁止替代方案**

- [ ] H07.1：10秒内存计算、60秒/风险变化持久，队列合并latest，不读全DB。Funding按当时空量*native mark*rate及当时FX，缺关键项总值null+known subtotal。Basis已在双腿PnL内不重复加
- [ ] H07.2：FX残差独立。severity/action分离，同条件episode、ACK不反复、失败不resolve、2fresh tick恢复。mark过用户强平价仅unconfirmed。5秒DB滞后degraded不丢真实仓位/critical alert，10symbol容量真实显示。

**必须编写的行为断言**

- [ ] 部分加减仓现金流、负funding、缺mark/FX和settlement边界
- [ ] 零accrued固定basis门槛、5..30秒异步参考但新READY不得通过
- [ ] 10symbol/500评分virtual600秒，计算/写频率与0jitter
- [ ] 阻塞worker后degraded，恢复全部critical alert、仓位不变

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_hedge_monitor.py desktop/backend/tests/test_shortlab_hedge_alerts.py desktop/backend/tests/test_shortlab_hedge_load.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 部分加减仓现金流、负funding、缺mark/FX和settlement边界 | 部分加减仓现金流、负funding、缺mark/FX和settlement边界 |
| 零accrued固定basis门槛、5..30秒异步参考但新READY不得通过 | 零accrued固定basis门槛、5..30秒异步参考但新READY不得通过 |
| 10symbol/500评分virtual600秒 | 10symbol/500评分virtual600秒，计算/写频率与0jitter |
| 阻塞worker后degraded | 阻塞worker后degraded，恢复全部critical alert、仓位不变 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC16 AC17 AC18证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H08：对冲API与默认运行时最终接线

**依赖**：H02/H03/H04/H06/H07/H10域测试合并；H05可禁用。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC18 AC21**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/service.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/runtime.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/scheduler.py`
- `desktop/backend/src/diveintocrypto_desktop/api/shortlab.py`
- `desktop/backend/src/diveintocrypto_desktop/api/app.py`

**输入/输出接口合同**

复用现有router，实施设计B32全部14类接口（含B32.3.1 GET simulation）。service async simulate/save_plan/get_simulation/apply_leg_event/activate/close/monitor/alerts；均调用已冻结域服务与Repository，不重写公式或建连接。

**实施步骤与禁止替代方案**

- [ ] H08.1：005失败只禁Hedge基础4仍可用。POST simulate存snapshot非交易
- [ ] H08.2：POST plan先idempotency后expiry，过期409
- [ ] H08.3：GETexpired200，missing404
- [ ] H08.4：PATCH带event/client/version。回调jitter0、同key采集共享、生命周期恢复/stop等待。CORS/Origin/JSON保护、未知字段422，503 LOCAL_WRITE_BUSY不提交/不报成功。H10 metrics/grader注入真实callback；独立GET hedge/evidence/summary，forward_grader按21600秒组合directional/hedge子阶段并分状态统计，禁用Hedge不关闭原grader。未配置chain capabilities诚实。

**必须编写的行为断言**

- [ ] 正常/非法/过期/重复client/version冲突API码完整
- [ ] 默认Runtime生产接线而非仅注入service的单测
- [ ] 单worker混合500score/10plans真实DB耗时独立报告
- [ ] 原scan路由不受Hedge迁移/外部provider失败影响

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_hedge_api.py desktop/backend/tests/test_shortlab_hedge_worker_integration.py desktop/backend/tests/test_shortlab_default_wiring.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 正常/非法/过期/重复client/version冲突API码完整 | 正常/非法/过期/重复client/version冲突API码完整 |
| 默认Runtime生产接线而非仅注入service的单测 | 默认Runtime生产接线而非仅注入service的单测 |
| 单worker混合500score/10plans真实DB耗时独立报告 | 单worker混合500score/10plans真实DB耗时独立报告 |
| 原scan路由不受Hedge迁移/外部provider失败影响 | 原scan路由不受Hedge迁移/外部provider失败影响 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC18 AC21证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H09：Funding/Planner/Monitor/Alerts桌面交互

**依赖**：H01 DTO冻结即可离线开发；合并/E2E需H08。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC10 AC13 AC18 AC21**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/ui/build.mjs`
- `desktop/ui/src/app/shortlab/short-lab-evidence.jsx`
- `desktop/ui/src/app/data.js`
- `desktop/ui/src/app/desktop-app.jsx`
- `desktop/ui/src/app/i18n.js`
- `desktop/ui/src/app/shortlab/funding-view.jsx`
- `desktop/ui/src/app/shortlab/hedge-planner.jsx`
- `desktop/ui/src/app/shortlab/hedge-monitor.jsx`
- `desktop/ui/src/app/shortlab/hedge-alerts.jsx`
- `desktop/ui/src/app/shortlab/hedge-format.js`

**输入/输出接口合同**

DIVE.hedgeEvidenceSummary(filters)对应GET /api/short/hedge/evidence/summary；DIVE.fundingOpportunities/hedgeVenues/hedgeSimulate/getHedgeSimulation/createHedgePlan/hedgePlans/hedgePlan/applyHedgeLegEvent/activateHedgePlan/closeHedgePlan/hedgeMonitor/hedgeAlerts/ackHedgeAlert对应B32路径。统一query snake aliases和JSON camel DTO；所有组件只经data.js，quantity保持字符串。

**实施步骤与禁止替代方案**

- [ ] H09.1：ShortLab子导航，不独立产品；Evidence页面方向性/对冲两子页各自调用不同endpoint，不混合结果。分别显示plan status/readiness/riskValidation、目标/实际量、参考/估算/用户收据。无强平价/chain indicative可DRAFT+LIMITED，引导补项且不隐藏资产。模拟过期重新模拟新ID，重复提交clientid保持
- [ ] H09.2：告警ACK不当解决。中文/TR/EN、浏览器权限用户主动授予，软件停止不宣称保护仍在。Abort与generation锁遵F08，不改dist。

**必须编写的行为断言**

- [ ] 无API或503不mock，可信旧数据标stale
- [ ] quantity0.000000000000000001不Number转换用于订单
- [ ] DRAFT/LIMITED与ACTIVE真实状态分开，expired simulation可读
- [ ] 两腿订单/孤腿/无平台stop、通知拒绝与断网文案清晰

**测试文件与执行命令**

```text
node --test desktop/ui/test/hedge.test.mjs desktop/ui/test/shortlab-refresh.test.mjs
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 无API或503不mock | 无API或503不mock，可信旧数据标stale |
| quantity0.000000000000000001不Number转换用于订单 | quantity0.000000000000000001不Number转换用于订单 |
| DRAFT/LIMITED与ACTIVE真实状态分开 | DRAFT/LIMITED与ACTIVE真实状态分开，expired simulation可读 |
| 两腿订单/孤腿/无平台stop、通知拒绝与断网文案清晰 | 两腿订单/孤腿/无平台stop、通知拒绝与断网文案清晰 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC10 AC13 AC18 AC21证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H10：独立对冲Evidence域计算

**依赖**：H01+H03+H06合同；不依赖H08 router，H08负责最终注册。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC19 AC20**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_grader.py`
- `desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_metrics.py`

**输入/输出接口合同**

async grade_hedge(snapshot_id,strategy,horizon,as_of_ms,repository,market_provider:HistoricalMarketProvider,policy)->HedgeOutcome；async hedge_summary(filters,repository,policy)->HedgeEvidenceSummary；callback async run_due(context:JobContext)->JobStatus。H08组合到现有forward_grader子阶段，纯模块不注册Scheduler；API汇总DTO遵设计B32.13。四策略100/75/50/25%，Outcome写H01 sl_hedge_outcome，非旧sl_forward_outcome。

**实施步骤与禁止替代方案**

- [ ] H10.1：按设计B39.1.1消费H01冻结HistoricalMarketProvider（read_frozen_quote/find_frozen_quote/read_price_bars/read_settled_funding/read_lifecycle完整方法），入场/退出quote只读冻结snapshot；bars/funding可由已验证区间adapter补抓，缺历史quote不从当前price推算。冻结入场数量/price/capital/quote/cost/FX/source版本
- [ ] H10.2：7/30/90D，未来quote不能回填。缺历史quote/mark/FX UNAVAILABLE，退市CENSORED不剔除。模拟与实盘手工账本分开，完整净值分母capital投入不是仅margin。价格时间轴完整才drawdown，不能拼两端日高低。引用全部入场/退出证据以pin长期保留。

**必须编写的行为断言**

- [ ] 完整/未到期/缺price或quote/退市四状态
- [ ] 资本分母/两腿cost/funding一致，旧directional表不改变
- [ ] 同snapshot不同cost/version新row，不覆盖历史
- [ ] 跨30天被引用quote/simulation仍可查询

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_hedge_evidence.py -q
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| 完整/未到期/缺price或quote/退市四状态 | 完整/未到期/缺price或quote/退市四状态 |
| 资本分母/两腿cost/funding一致 | 资本分母/两腿cost/funding一致，旧directional表不改变 |
| 同snapshot不同cost/version新row | 同snapshot不同cost/version新row，不覆盖历史 |
| 跨30天被引用quote/simulation仍可查询 | 跨30天被引用quote/simulation仍可查询 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC19 AC20证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## H11：最终回归、成品、dist与第三方交付

**依赖**：F/H各域、H08真实接线、H09源码验收；H05能力单列。\
**设计依据**：设计C2对应同编号任务与相关A/B段；验收映射 **AC01 AC22 AC23**。\
**允许修改/创建文件**（当前已存在者Modify，其余Create；不允许修改列表外共享文件）：

- `scripts/smoke_shortlab_packaged.py`
- `desktop/ui/dist/bundle.js`
- `desktop/ui/dist/`
- `.github/workflows/release.yml`
- `README.md`
- `docs/api.md`
- `docs/testing.md`
- `docs/packaging.md`
- `docs/用户手册.md`

**输入/输出接口合同**

成品smoke以真实exe+public HTTP fixture stub，不注入产品demo模式。CI提交manifest/ownership/command/log SHA及run/artifact URL；更新文档仅描述实际已实现能力。

**实施步骤与禁止替代方案**

- [ ] H11.1：从H09批准source SHA单独build dist提交，记录node/esbuild lock、产物SHA。Windows只读安装目录+空用户数据，第一次迁移5、engine资源、规划事件，退出第二次恢复plan/alert。全部23AC对应证据
- [ ] H11.2：无key/451明确UNCONFIGURED/UNVERIFIED不计PoC通过。桌面标签不触Android，artifact上传always且无文件报错。所有资源与默认port46408复核，旧Scanner回归独立。

**必须编写的行为断言**

- [ ] backend+root+UI全测试，live独立记录
- [ ] 全资源005实际存在、成品两次启动无源码目录依赖
- [ ] 所有23AC manifest可下载，owner脚本与PR摘要一致
- [ ] 源码与dist来源hash绑定，无provider完成虚报

**测试文件与执行命令**

```text
uv run --project desktop/backend pytest desktop/backend/tests/test_shortlab_packaging.py desktop/backend/tests/test_shortlab_release_workflow.py -q
```

```text
node --test desktop/ui/test/hedge.test.mjs
```

**最小场景表（在该任务测试文件中逐行落断言，字段使用上面的冻结DTO）**

| 场景 | 预期 |
|---|---|
| backend+root+UI全测试 | backend+root+UI全测试，live独立记录 |
| 全资源005实际存在、成品两次启动无源码目录依赖 | 全资源005实际存在、成品两次启动无源码目录依赖 |
| 所有23AC manifest可下载 | 所有23AC manifest可下载，owner脚本与PR摘要一致 |
| 源码与dist来源hash绑定 | 源码与dist来源hash绑定，无provider完成虚报 |

**完成与交接**

- [ ] 本任务全部断言通过且返回类型与producer一致；没有schema/fixtures私有分叉。
- [ ] PR列明AC01 AC22 AC23证据、实现/验证分别计状态；无网络证据不得写live通过。
- [ ] 下游需要的接口、source/config版本、文件handoff与未启用能力列入manifest；完成合并后旧owner停止编辑。

---

## 6.1 可直接落地的关键测试写法

以下代码放入对应owner测试文件；它们测试独立行为预期，不要求实现者复制内部算法。新模块尚不存在时先补测试，失败应是目标功能未提供；F02函数签名已经在本计划冻结。

```python
# F02: desktop/backend/tests/test_shortlab_liquidity_units.py
from decimal import Decimal
from diveintocrypto_desktop.shortlab.observations import canonical_price, canonical_qty

def test_multiplier_normalization_preserves_economic_quantity():
    price = canonical_price(Decimal("0.006"), Decimal("1000"), Decimal("1"))
    qty = canonical_qty(Decimal("100"), Decimal("1000"))
    assert price == Decimal("0.000006")
    assert qty == Decimal("100000")
    assert price * qty == Decimal("0.600000")
    assert (price - Decimal("0.000012")) / Decimal("0.000012") == Decimal("-0.5")
```

```python
# H01: desktop/backend/tests/test_shortlab_hedge_config.py
import hashlib
import json
from pathlib import Path
import pytest

@pytest.mark.parametrize("name", ["fcs_config", "hedge_cost_config", "hedge_policy"])
def test_default_policy_vector_is_layout_independent(name):
    folder = Path(__file__).parent / "fixtures" / "hedge" / "config"
    value = json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))
    expected = (folder / f"{name}.sha256").read_text(encoding="utf-8").strip()
    for text in [json.dumps(value), json.dumps(value, indent=4).replace("\n", "\r\n")]:
        actual = hashlib.sha256(json.dumps(json.loads(text), sort_keys=True,
            separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        assert actual == expected
```

```python
# F09: desktop/backend/tests/test_shortlab_packaging.py
import ast
from pathlib import Path

def test_runtime_default_port_matches_delivery_contract():
    package = Path(__file__).parent.parent / "src" / "diveintocrypto_desktop"
    tree = ast.parse((package / "__main__.py").read_text(encoding="utf-8"))
    values = [keyword.value.value
        for node in ast.walk(tree) if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "add_argument"
        and node.args and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "--port"
        for keyword in node.keywords if keyword.arg == "default"]
    assert values == [46408]
```

复杂账本/API/harness测试不得只断言类型或非null。每任务场景表全部落实；H06必须用真实H01 Repository事务测试并发/重启，H08使用默认Runtime+FastAPI客户端，H07 fake-timer测试另配真实worker集成，H10核对具体资本分母和四状态计数。测试不能为便于green删除unknown/partial样本或把异常catch后伪造0。

## 7. 不可变数学与黄金向量

以下固定向量由H01写入fixture，解析JSON→schema规范化→sort_keys/紧凑separators/UTF-8→SHA256，不对排版文本哈希：

| 输入 | 期望 |
|---|---|
| 旧LTSS默认投影 | 4909ffe7d43c294983d313cff65c6d73125b921944e403a1e1e152b91a869786 |
| fcs_config | 72eca2e7214ac6dfdf908541d1dd185c6b6ce882e30de3297b495c8f1d61c136 |
| hedge_cost_config | 9915b1468e5d0e4fc02ee71b4883c5445351928b0e7509f0dc4e6727fcca37dc |
| hedge_policy | ff7adb8e321486bfc4c5d8a8c5645b26874be15577bebe63d336ba81980c8f08 |

版本features-v2/entry-v2与quality-policy-v2区分修复路径；LTSS原数学版本不因数据接线修复擅自改分箱。provider URL/key/path/refresh/enable排除FCS/cost投影，但source能力与实际snapshot仍保存，不代表它们可忽略。

## 7.1 设计要求到任务的完整覆盖

| 设计范围 | 实施owner | 接线/最终验收 |
|---|---|---|
| A1–A3 当前基线/边界 | 启动基线、F01、F09 | H11原功能回归 |
| A4 观测/量纲/时点 | F02/F03/F04/F05 | F06默认采集链，AC02/03 |
| A5 LTSS/Entry/DQ/风险 | F05 | F06/F08，AC06/08 |
| A6 身份/存储/增量 | F01/F04/F06 | AC05/07/20 |
| A7 网络/worker/scheduler | F01/F03/F06 | H07/H08混合负载，AC04/18 |
| A8 长期Evidence | F07 | F06b/F08，AC09 |
| A9–A10 API/UI/资源/配置 | F05/F08/F09 | H08/H11，AC10/21/22/23 |
| B1–B7 模式/风险预算/Funding门槛 | H03/H04 | H08/H09 |
| B8 FCS分箱/分布 | H03/H01配置 | AC14 |
| B9–B12 Venue/身份/Alpha/链上 | H01/H02/H05 | H08能力状态、AC12 |
| B13–B18 成本/APR/数量/压力/Safety | H03/H04 | AC13/17 |
| B19–B21 账本/人工订单/成对退出 | H01/H04/H06 | H08/H09，AC15 |
| B22–B27 monitor/PnL/alerts/通知 | H07/H09 | H08，AC16/18 |
| B28–B29 schema/Repo/引用pin | H01 | F09维护回调/H08/AC20 |
| B30–B33 Runtime/freshness/API/error | H08 | AC18/21 |
| B34–B38 UI/兼容/质量 | H09/H03 | H08默认路由 |
| B39 对冲Evidence | H10 | H08新汇总接口/H09 Evidence子页、AC19 |
| B40–B42 配置/版本/结构 | H01及各域owner | H11资源/source manifest |
| B43–B51 测试/性能/安全/降级/完成 | 对应各任务全部强制断言 | H11全矩阵 |
| B附录A–F、C1–C4 | F01/H01合同与工具、F09/H11证据 | 全部AC01–AC23 |

新增Evidence endpoint不修改原/api/short/evidence/summary；保留原4h技术Evidence与7D/30D/90D方向性结果的独立含义。软件通知只是辅助，平台保护订单由用户手工设置，不能将本计划任何验收当成套利收益保证。

## 8. 第三方验收与发布状态

实施验收采用设计C3全部AC01–AC23行；本计划任务映射不删减任何行。manifest必须列task_id/source_commit/base SHA/acceptance_id/command/exit_code/artifact path+SHA/CI URL/live flag/implementation_status/verification_status。artifact在runtime/verification，提交模板/policy位于docs/contracts，可审源测试在tests。

- 仅设计脚本通过：LOCAL_DOC_PASS，F/H不能标完成。
- 域单测通过但生产接线缺失：DOMAIN_TEST_PASS，不能发布该功能。
- 无key/地区不支持：UNCONFIGURED/UNVERIFIED，不能算live PASS。
- 全API/UI默认运行通过但成品未测：INTEGRATION_PASS，不能算PACKAGED_PASS。
- 首次发布最低能力Binance Spot；Alpha与Ethereum/0x独立列能力，不因一个总绿表掩盖缺失。

H11提交交付包包含源码commit、设计/计划内容SHA、全部测试报告、ownership通过记录、资源与dist manifest、Windows两次成品smoke、实际启用provider清单及局限。README/用户手册不得宣传自动交易、同步平仓、无损收益或真实账户强平已验证。

## 9. 已冻结的歧义处理

- body需要snake/camel转换只在API边界；DTO字段以设计B32响应/请求aliases为准，内部统一snake。
- 因设计缺实际commercial FULL provider选择，保持禁用，不要求执行者二次选型；首链已固定Ethereum/0x。
- 005未发布可按H01合同更新，发布后新改动006+；不改001–003。
- Runtime共享文件仅F06/H08各阶段owner编辑，F07/F09/H10交callback；需补repo方法回交当时owner，不绕开数据库层。
- 所有外部路径只读；测试用户数据目录/临时安装目录置于项目根内，不能修改真实用户目录证明成品行为。
- 运行环境不可用先记录阻塞原因并保留源文档/代码，不用mock或系统缺依赖失败证明验证成功。
