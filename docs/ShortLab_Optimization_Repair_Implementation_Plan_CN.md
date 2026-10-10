# short-lab 优化修复实施计划

交付日期：2026-10-09

适用基线：`edd81adcc2cc1436c33f86db1b2d5a8bd21e3ad5`

> 执行人员与子agent必须同时阅读配套设计和本计划，按任务验收后交接。使用agent实施时，可采用 subagent-driven-development；单人执行可采用 executing-plans。任务完成状态只能以实现、测试和证据判断，不能以文档提交判断。

**目标：**实现可信 Meme 做空筛选、可解释对冲建议、完整手工账本/提醒与可比较的证据链。

**架构：**复用既有Provider、Observed、Identity、Decimal账本和单worker DuckDB；新增纯Gate/经济/决策/退出/PnL模块，由唯一Service集成人接线。HTTP新增字段保持明确兼容，旧历史只读解释，不提升未知信用。

**技术栈：**Python>=3.12、FastAPI、DuckDB>=1.5.3、React18.3.1、esbuild和Node测试。开发、生产UI构建和浏览器验收统一Node22.x（engines为>=22 <23，CI固定22）；浏览器使用Node22及固定开发依赖Playwright1.56.0，不增加交易SDK或生产浏览器依赖。

**设计：**[优化修复设计方案](ShortLab_Optimization_Repair_Design_CN.md)。D01–D19为唯一行为合同，V01–V16为验收向量；本计划只细化执行。

## P01. 全局约束

- 只修改执行 checkout 项目根内文件。根外只读；临时目录、数据库、UV/npm缓存、浏览器下载与日志都放项目根内。
- 默认端口46408；产品short-lab；保留Python包和dive-desktop兼容入口；Android不修改。
- 不交易、不索取私钥、不增加交易账户权限；所有成交与保护确认是用户手工登记。
- DTO/单位/状态/配置/数据库按D03/D13/D15/D18，不允许任务自产同名第二合同。
- Universe500、Shortlist50、EntryTop10、并发2、240次实际发送/轮；Funding80次/300s；Monitor10s、落盘60s、队列256、超时5s、aging30s。
- 001–005迁移不改；新增006并真实打包；默认Funding/Hedge仍关闭，schema存在不表示功能启用。
- Decimal字符串计算账本与风险；缺失为null+reason；缓存/重启不刷新来源时间；历史不使用当前报价回填。
- Readiness表示公开条件，不表示已挂保护单；ACTIVE须满足D12的人工确认；RULE_BASED_UNVALIDATED不转成概率。
- 新版本公式和旧历史分桶；完整执行测试不得靠修改期望绕过缺陷。

## P02. 开工、命令与证据规则

### P02.1 基线和测试入口

每任务开工前记录当前HEAD、合并前置SHA及interface_dependencies合同SHA与文件owner。基于已合并merge_prerequisites的HEAD创建 `codex/rNN-<topic>` 或 `codex/rNNa-<topic>`/`codex/rNNb-<topic>` 分支，task_id须与分支对应；不从未合并兄弟分支建立隐含依赖。已有执行分支可复用，但必须记录相同信息。

从项目根执行环境准备（一次），先以node --version确认v22.*；不足则记录环境UNVERIFIED，不自动在根外安装：

```bash
mkdir -p desktop/backend/runtime/repair_tmp desktop/backend/runtime/verification/repair desktop/backend/runtime/repair_checkouts desktop/ui/test/.tmp
git check-ignore desktop/backend/runtime/repair_checkouts desktop/backend/runtime/verification/repair
export npm_config_cache="$PWD/desktop/ui/test/.tmp/npm-cache"
export PLAYWRIGHT_BROWSERS_PATH="$PWD/desktop/ui/test/.tmp/ms-playwright"
export PYINSTALLER_CONFIG_DIR="$PWD/desktop/backend/runtime/pyinstaller-cache"
UV_CACHE_DIR="$PWD/desktop/backend/runtime/uv-cache" uv sync --project desktop/backend --group dev
npm ci --prefix desktop/ui --cache desktop/ui/test/.tmp/npm-cache
```

独立checkout由协调者按D19.7在上述repair_checkouts目录创建，已有runtime/ ignore覆盖；各子agent在自己clone根重跑环境准备，禁止引用主仓库缓存作为可写目录。任务完成后先收集commit/SHA/证据，再由协调者删除自己创建的对应clone；主工作区手动文档维护和其他runtime数据保持原样。

后端单任务命令在 `desktop/backend` 目录运行：

```bash
TMPDIR="$PWD/runtime/repair_tmp" UV_CACHE_DIR="$PWD/runtime/uv-cache" uv run python -m pytest tests/test_shortlab_repair_contracts.py -q
```

上述root环境设置在每个执行终端先运行，子进程继承绝对缓存位置，不修改HOME或用户全局配置。任务表给出具体测试文件，替换上面文件名。完整后端仍从backend运行 `uv run python -m pytest tests -q`；根目录测试从项目根运行：

```bash
TMPDIR="$PWD/desktop/backend/runtime/repair_tmp" UV_CACHE_DIR="$PWD/desktop/backend/runtime/uv-cache" uv run --project desktop/backend python -m pytest tests/ -q
```

UI Node单任务命令在 `desktop/ui` 运行 `node --test test/<指定文件>.test.mjs`；完整测试 `npm test`；构建 `npm run build`。后端与根目录为不同测试集，不重复列出等效命令。

### P02.2 每任务统一执行步骤

- [ ] 读取指定设计节、前置接口和源代码；记录baseline_sha、merge_prerequisites、interface_dependencies、contract_sha与prerequisite_shas；读取未合并Producer代码不是使用其实现的许可。
- [ ] 创建本任务的失败回归用例；运行并确认是目标缺陷/缺接口导致失败，不是缺依赖。
- [ ] 按本任务文件清单实现；纯模块不读系统时间、不请求网络、不直接写DB，时钟通过参数注入。
- [ ] 运行指定新增测试与被改模块原有测试；保留退出码、命令、环境和输出。
- [ ] 自审source→wiring→API/UI的实际交接，不仅检查纯函数返回；对任务所承诺的生产入口添加集成用例。
- [ ] 生成manifest和handoff；文件owner检查通过后提交独立commit，接收任务基于合并SHA继续。

任务命令所列测试文件和对应新Fixture默认归属该任务；每个现有测试文件由其被测源模块owner修改，跨任务共享Fixture仅R00可改。禁止广泛 `tests/**` 双owner，ownership policy要列出精确新路径及受控fixture子目录。

### P02.3 可审核证据

每任务输出到 `desktop/backend/runtime/verification/repair/<task>/<source-sha>/`：test.log、manifest.json、handoff.json。UI/浏览器可先写自己的临时目录，验收时复制结果到该证据目录。

manifest.json必含：task_id、source_commit、baseline_sha、merge_prerequisites、interface_dependencies、contract_sha、prerequisite_shas、design_sections、tests（command/exit_code/passed/failed/skipped/log_path）、artifacts（relative_path/sha256）、limitations、ci_run_url。未跑项目不得填PASS；ci_run_url未有CI时null，禁止伪造。

这些runtime文件不进Git；PR描述粘贴manifest摘要和artifact定位/SHA；模板与ownership policy位于可审的 `docs/contracts/`。另输出ownership.json，字段task_id、branch、baseline_sha、prerequisite_commits（仅合并依赖SHA）、interface_dependencies（生产者角色与contract_sha）、handoffs、allowed_paths；checker兼容当前required字段，不把测试manifest直接当ownership.json。CI上传artifact，`if-no-files-found: error`；缺关键证据本任务不验收。

## P03. Review Focus

1. **跨币/跨计划与迟到响应：**资产或计划切换后不得使用旧数据写新对象——R10b/R12/R15b测试。
2. **来源时间和重启：**缓存、完整档案、回填不能重置首次已知时间，也不能获得过去决策信用——R03/R05/R15b测试。
3. **倍率、FX、舍入与Dust：**1000倍合约、USDC现货和Base费用不能改变原生交易单位——R06a/R06b/R08a/R08b/R15b测试。
4. **能力/数据未知：**缺费、缺schedule、缺保护能力或历史报价不得当0、PASS或无风险——R05/R06a/R06b/R07/R14a/R14b测试。
5. **饥饿与撤销/超时：**背景扫描不抢监控保留额度，数据库超时不清账，确认数量变化失效——R01/R08a/R08b/R11a/R11b/R15b测试。

## P04. 合同依赖、合并依赖与并行波次

任务ID以本表为准，R02/R06/R08/R10/R11/R13/R14/R15是工作包族，不是可派发ID。合同依赖表示消费R00冻结签名/Fixture，可独立开发；只有合并依赖要求对应实现SHA已在基线。协调者按D19.8显式fetch子包分支、验证SHA、merge --no-ff保留祖先；子包禁push不限制协调者fetch。并发任务使用D19的独立clone/checkout，独立.git；禁止共享工作目录切分支或相互改文件。每任务manifest同时记录两种依赖，不能把合同依赖全部写成prerequisite_commits。

### P04.1 波次

| 波次 | 可并行任务 | 放行条件 |
|---|---|---|
| 0 | R00 | 冻结DTO/ports/调用点schema/Fixture/owner policy/新旧版本兼容 |
| 1 | R01/R02a/R02b/R03/R05/R06a/R08a/R09/R10a/R12/R13a/R15a | 只要求R00合并；使用合同端口/Fixture独立测试 |
| 2 | R04/R06b/R07/R08b/R11a/R11b/R13b/R14a/R14b | R00合并即可开工；不等待逻辑Producer合并；需已发布其使用的合同（R00统一冻结） |
| 3 | R10b → R15b → R16 | R10b合并全部真实Producer+R10a；R15b验真实绑定；R16发布验收 |

R13a和R12均可波次1；R13b波次2只消费其冻结接口，可在工作树没有R13a实现时用R00组件/HTTP Fixture测试，合并最终分支前补齐真实组件。未合并模块在隔离测试build中通过R00组件/HTTP alias替身；生产build禁止这些alias，最终集成前补齐实际文件。R10a/R15a也按R00边界合同并行，未绑定生产不返回Fake成功。最迟在R10b前合并实际Producer，不能把这项最终门槛反过来当消费者开工门槛。

### P04.2 任务与owner

| ID | 工作包 | 合并依赖 | 合同依赖 | 独占源文件职责 |
|---|---|---|---|---|
| R00 | 合同/配置/版本/Fixture | 无 | 无 | 共享DTO/协议、Context增量、owner检查 |
| R01 | DB/迁移/当前查询/月预算 | R00 | R09投射schema | repository/maintenance/006；spec仅006 |
| R02a | Resolver/Override | R00 | R02b候选 | resolver/overrides/asset_overrides |
| R02b | Catalog | R00 | R02a身份输出 | catalog |
| R03 | 数据源Observation/parser | R00 | R01/R11a端口 | observations及列明data文件 |
| R04 | 输入/分类/DQ/共识风险 | R00 | R02a/R03/R05 | inputs/quality/risk/profiles/carry/engine共识 |
| R05 | Funding Gate/coverage/FCS | R00 | R03来源 | funding_schedule/entry_gate/funding_score |
| R06a | Units/Economics | R00 | R05 Gate | units/economics |
| R06b | Protection/Planner | R00 | R05/R06a | protection/planner/simulator/venue能力 |
| R07 | Decision | R00 | R02a/R05/R06a/R06b | decision |
| R08a | Ledger/PnL | R00 | R01/R06a | ledger/pnl |
| R08b | Monitor/Exit/Alerts | R00 | R08a/R06b | monitor/exit_guidance/alerts |
| R09 | Opportunity projection | R00 | R01/R05/R06a | projection/opportunity |
| R10a | Service/API边界 | R00 | 所有producer schema | service/runtime/API初始阶段 |
| R10b | Service/API真实接线 | R00/R01/R02a/R02b/R03/R04/R05/R06a/R06b/R07/R08a/R08b/R09/R10a/R11a/R11b/R14a/R14b | 无替身依赖 | service/runtime/API顺序交接阶段 |
| R11a | Budget/HTTP/month | R00 | R01 RepositoryPort | request_budget/data.http/coingecko |
| R11b | Market/Jobs | R00 | R03/R04/R05/R06a/R08a/R08b/R09/R11a/R14a | hedge.jobs/market/scheduler |
| R12 | Monitor/data adapter | R00 | R10a HTTP schema | data.js/hedge-monitor/monitor-state |
| R13a | 纯组件/文案 | R00 | R12 DTO | decision-panel/plans-view/workflow-model/i18n |
| R13b | UI实际接线 | R00 | R12/R13a/R10a | desktop-app/funding-view/hedge-planner/workflow-bindings |
| R14a | Capture | R00 | R01/R03/R05/R06a/R11b | evidence.capture |
| R14b | Historical/Grader/Metrics | R00 | R01/R03/R05/R06a/R14a | 其余列明evidence文件 |
| R15a | Harness/浏览器设施 | R00 | R10a边界schema | 隔离server/fixture测试入口/UI开发依赖 |
| R15b | 真实整体验收 | R10b/R12/R13a/R13b/R15a | 所有真实producer | 执行已交付测试/产证据，不改生产源 |
| R16 | 发布/打包/文档 | R15b/R01/R15a | 无 | release/smoke/README/docs，spec顺序交接 |

后端路径一律写完整项目相对路径；下文任务的源码字段不使用hedge/或evidence/裸前缀。每个新增测试只归创建任务，R15b可执行全部测试但不能擅自修改。

顺序owner明确：request_budget.py=[R00,R11a]；service.py/runtime.py/api/app.py/api/shortlab.py/api/shortlab_repair.py=[R10a,R10b]；short-lab.spec=[R01,R16]；smoke_shortlab_packaged.py/release.yml=[R16]；i18n.js=[R13a]；全部版本模块=[R00]；desktop/ui/package.json与package-lock.json=[R15a,R16]，ci.yml=[R16]。checker按阶段检查前序合并与handoff，其他文件只一个owner。

## P05. 任务明细

### R00：冻结类型、配置、版本、Fixture与owner policy

**读取：**D03、D13、D15、D18、D19。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/config.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/default.yaml`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/models.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/__init__.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/scoring/versions.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/request_budget.py`、`scripts/check_shortlab_owner.py`。
**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/repair_contracts.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/repair_ports.py`、`docs/contracts/shortlab_repair_ownership_policy.json`、`docs/contracts/shortlab_repair_manifest_template.json`、`docs/contracts/shortlab_repair_service_calls.json`、`desktop/backend/tests/repair_fixtures.py`、`desktop/backend/tests/fixtures/repair/`、`desktop/ui/test/fixtures/repair/`（仅组件/HTTP合同替身）。

**接口产出：**D18全部DTO；D15配置合并与decision_policy_hash；扩展原有Hedge Result嵌套readiness、HistoricalPriceBar.price_basis与read_mark_price_bars协议、strategy/ref/plan状态枚举，保留原字段。R00不实现Provider或策略计算；冻结D18全部签名（含两个async Capture接口、build_ratio_proposal）、CaptureContext/CaptureResult/QuoteCollectionResult、RepositoryPort 20方法与MarketPort/RepairPorts，生成service_calls.json和Fake conformance。

- [ ] 定义必需/null字段、enum、Decimal序列化、未知JSON和重复键校验；导出load_json_strict(text)，配置YAML也拒绝重复键；frozen对象嵌套映射也不能外部篡改。
- [ ] 合并D15新增子树，旧用户配置补默认；解除仅fcs_v1硬校验，旧fcs_v1配置规范化当前fcs_v2，hash投射只引用R00规范常量；非法键、ratio越界、期限/资本非法拒绝。
- [ ] 更新版本为fcs_v2/hedge_v2/NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V2/hedge_evidence_v3；Feature features-v3、Entry entry-v3，Safety PLAN_SAFETY_V2、Venue VERIFIED_TWO_SIDED_COST_V2，旧版本解码继续支持。
- [ ] RequestContext保留原六字段与构造顺序，仅追加job_id/deadline_ms；make_request_context同步keyword；repair_contracts只re-export。所有Grader版本fallback登记给R14b删除，不能新旧两套版本常量。
- [ ] 按D19.8完成R00 HEAD自检：新checker/new policy同时随首提交落盘，自覆盖条目齐全，prerequisite_commits/interface_dependencies/handoffs空数组合法，基线为提交前SHA；不使用旧policy或跳过owner检查。自检0/缺自覆盖1/结构错误2均有测试。
- [ ] owner checker同时读取ownership.json与测试manifest；prerequisite_commits只包含merge_prerequisites，interface_dependencies不强制生产者祖先；分支pattern、stage_prerequisites、spec/smoke/release/测试精确归属在R00首提交可运行；按D19.6解析生产import/动态目标/build alias，发现测试路径或替身替换报PRODUCTION_TEST_ALIAS，测试入口不误判。
- [ ] 生成完整默认配置、policy canonical JSON、hash golden文件；格式重排/CRLF/末尾换行不改变结构hash；关键政策改变应改变hash，API key/路径/refresh不改变hash。
- [ ] 共享Fixture含：identity普通/1000/歧义；ticker与receipt；90D连续8h/删槽/4h变更；负当前费率；双向报价和费用；1000强平；部分成交和纠错；API完整Projection。原始Funding/Kline复用既有`desktop/backend/tests/fixtures/shortlab/`并记录SHA，不另造不同索引口径。
- [ ] 按D19.6冻结make_ports(**overrides)及全部默认callback来源/白名单；Fixture helper固定导出 `make_identity`、`make_funding_context`、`make_decision_context`、`make_decision_request`、`make_events`、`make_market_context`、`make_event_fx`，额外make_ports提供合同协作者Fake；helper的case选择枚举场景，kwargs可覆盖DTO字段或既有FundingMetrics字段，其他键拒绝。make_decision_context与make_decision_request默认案例ID统一MEME_FULL_VALID，UP_100是该案例必须通过的默认压力场景，不是案例ID；强平价/深度/资本保证默认正例可行，不通过缩减默认压力情景构造正例。
- [ ] policy精确覆盖本计划源文件、新测试/fixture和后续交接；分支regex按D19允许rNN及a/b子包并验证task_id；owner_sequence/stage_prerequisites按P04冻结，路径外/owner错误退出1、policy结构错退出2。

测试：`desktop/backend/tests/test_shortlab_repair_contracts.py`、`desktop/backend/tests/test_shortlab_repair_config.py`、`desktop/backend/tests/test_shortlab_repair_owner.py`。至少含：

```python
from decimal import Decimal
from diveintocrypto_desktop.shortlab.repair_contracts import GateResult

def test_unknown_gate_is_not_pass():
    gate = GateResult('UNKNOWN', ('FUNDING_SCHEDULE_UNKNOWN',), 1000, {})
    assert gate.status != 'PASS'
    assert Decimal('0.000001') * Decimal('1000') == Decimal('0.001')
```

交付：contract-fixtures.json清单与SHA；hash测试结果；policy和manifest模板可在PR审阅，fixture conformance与全部服务调用点schema随R00首提交发布。R00是后续任务开工门槛；逻辑Producer实现不是独立消费者的开工条件。


### R01：006迁移、查询、来源归档与保留

**读取：**D08、D13、D18.2。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/maintenance.py`、`desktop/backend/short-lab.spec`。
**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/migrations/006_optimization_repair.sql`。

**消费：**R00 DTO与JSON。**产出：**D13.1全部仓库方法、target6与完整事务。

- [ ] 按D13完整DDL创建7表、8个新增索引；禁止重写001–005或假006空文件。
- [ ] 实现来源/FX/schedule/Decision/保护/Entry/QuoteTask读写与引用校验；事件FX映射使用market observation的EVENT_FX记录。
- [ ] Current机会SQL先ROW_NUMBER每symbol选最新再过滤分页；从risk_json.projection_v2读取冻结投射，并按expires_at_ms调整stale/readiness后筛选。仓库不导入尚未实现的R09模块，不建立R01→R09依赖；R09负责生产写出该合同。不得先取历史200条。
- [ ] 按D19.4全状态表实现reserve_provider_request/finish_provider_request，BUDGET_COUNTER属于20方法合同：UTC月键、transition_seq、幂等/预留/确认未发取消/重启保留/跨月重预留、重复finish/未知ID/终态冲突及GC边界；handoff逐项列全部20方法签名与测试，不绕用私有_run。
- [ ] 保护保存同时CAS检查、幂等、记录resulting version、增加planVersion；确认是否有效用位置hash，不因activate状态版本递增误失效。
- [ ] Quote task原子claim；重启RUNNING恢复为PENDING带PROCESS_INTERRUPTED；deadline超期不可再次claim。
- [ ] pin先于retention，同事务检查引用；未引用Decision30d、BOOK3d、MARK/TICKER14d、OI/日线/规则180d、MARK_BAR_1H与Entry至少365d；ledger/plan不自动删。
- [ ] 加真实006打包datas及assert，不等R16最后才补资源。

测试：`desktop/backend/tests/test_shortlab_repair_repository.py`、`desktop/backend/tests/test_shortlab_repair_migration.py`。DuckDB真实临时文件，不能用sqlite替代。

```python
async def test_migration_rolls_back(repository_v5, inject_bad_006):
    with pytest.raises(MigrationError):
        await repository_v5.migrate(target_version=6)
    assert await read_schema_version(repository_v5) == 5
    assert await original_event_count(repository_v5) == 3
```

以上测试helper在本任务测试文件定义：repository_v5建立实际001–005；inject_bad_006只替换测试资源读取，插入合法首条DDL+非法第二条；read_schema_version与original_event_count通过repo单worker查询。再测试重复执行006、取消后事务完成、错误引用回滚、队列满503语义与引用保留。

交付：migration-summary.json（旧记录校验和、前后版本、回滚证据）、repository-contract.json。


### R02a：Resolver、倍率与人工覆盖

**读取：**D04.1/D04.2/D19。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/identity/resolver.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/identity/overrides.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/identity/asset_overrides.yaml`。

**创建：**无新增源文件。

**合同：**resolve_asset_context；Catalog输入为R00候选Fixture；产出唯一AssetIdentity，不读Catalog私有内部。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 保留人工/核验/地址/唯一符号优先级；核验倍率MANUAL进入解析结果，冲突UNKNOWN
- [ ] Profile覆盖接受MEME/GENERAL/LOW_FLOAT_VC；临时Fundamentals失败不改变已核验类别
- [ ] 保留EIP-55/Solana地址规则，禁止猜1000倍率；Hedge适配mapping_confidence

测试：`desktop/backend/tests/test_shortlab_repair_identity.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
result = resolve_asset_context('BTCUSDT', {}, [make_verified_candidate('BTCUSDT')], {})
assert result.contract_multiplier == 1
assert result.multiplier_source == 'MANUAL'
assert result.mapping_confidence == 'VERIFIED'
```

交付：identity-handoff.json；make_verified_candidate作为本任务测试helper从R00identity Fixture读取，另测1000/歧义/冲突；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R02b：Catalog候选与来源稳定性

**读取：**D04.1/D19。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/identity/catalog.py`。

**创建：**无新增源文件。

**合同：**Catalog.candidates(symbol,as_of_ms)原签名保留，输出R00候选schema；Resolver仅合同依赖。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 核验目录、网络目录与overlay候选携来源/known_at，不返回未核验第一条冒充解析Identity
- [ ] 缓存保持原receipt，歧义候选全部保留；与overrides冲突交给Resolver
- [ ] 向R10b交付默认Service候选函数绑定点，测试真实目录文件读入

测试：`desktop/backend/tests/test_shortlab_repair_catalog.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
candidates = catalog.candidates('BTCUSDT', fixture_now)
assert len(candidates) == 1
assert candidates[0]['multiplier_source'] == 'MANUAL'
assert candidates[0]['contract_multiplier'] == 1
```

交付：catalog-handoff.json、缓存/overlay/目录失败测试结果；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R03：Provider来源时间与存量恢复

**读取：**D03、D04.3、D05.1、D18.2。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/observations.py`、`desktop/backend/src/diveintocrypto_desktop/data/universe.py`、`desktop/backend/src/diveintocrypto_desktop/data/funding.py`、`desktop/backend/src/diveintocrypto_desktop/data/binance_klines.py`、`desktop/backend/src/diveintocrypto_desktop/data/open_interest.py`、`desktop/backend/src/diveintocrypto_desktop/data/spot.py`、`desktop/backend/src/diveintocrypto_desktop/data/orderbook.py`。

**消费：**R00 RequestContext，仓库协议用本任务Fake验证封装，R01合并后R10b/R15b验证真实repo。**产出：**Observed的ticker/mark/funding/OI/spot，以及保留原receipt的档案读取桥。

- [ ] Universe保留Ticker closeTime/response completion；原API兼容值通过to_legacy取出，不丢Observation。
- [ ] Funding raw observation写receipt，重读关联observation恢复；历史legacy缺receipt明确UNVERIFIED。不得为完整archive加“now fetched”修补。
- [ ] 分页endTime/limit/去重/429 budget遵循已有发送预算；当前fundingInfo采集并输出ScheduleSegment，未声明历史生效时点不向前延伸；缺历史档案显示HISTORY_BOOTSTRAPPING，不能假装安装当天有完整READY。
- [ ] Futures/Spot Kline保留raw index7 qv与源close，排除未收盘日线；OI时间和美元单位来源保留；原始book两侧分开保存，R06a/R06b/R11a/R11b可算对应数量VWAP，不能只剩总量；按D18实现fetch_mark_klines_range供R14a/R14b归档真实Mark历史，TRADE Kline不能当Mark。
- [ ] 所有新增生产读取接受keyword `request_context`，传至R11a/R11b HTTP发送层，不在数据模块重复扣预算。

测试：`desktop/backend/tests/test_shortlab_repair_observations.py`。

```python
async def test_archive_restart_preserves_receipt(repo, collector):
    observed = await collector.collect_funding()
    await collector.persist(observed, repo)
    restored = await collector.restore(repo)
    assert restored.meta.known_at_ms == observed.meta.known_at_ms
    assert restored.meta.source_as_of_ms == observed.meta.source_as_of_ms
```

collector为本任务测试桥，调用实际parser与仓库协议；R15b再验真实Runtime。补44.744/+40%与31.96日线、cache hit、receipt晚于cutoff、源时钟快3s、缺index7、451不N/A。

交付：provider-envelope.json与新参数清单，R10b消费真实封装而非raw row。


### R04：冻结输入与做空风险/分类

**读取：**D04、D05.2。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/inputs.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/quality.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/scoring/profiles.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/risk/veto.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/risk/squeeze.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/features/carry.py`、`desktop/backend/src/diveintocrypto_desktop/engine/consensus/engine.py`、`desktop/backend/src/diveintocrypto_desktop/engine/consensus/risk.py`、`desktop/backend/src/diveintocrypto_desktop/engine/config/default.yaml`。

**消费：**R02a/R02b身份、R03Observed、R05覆盖率。**产出：**正确7D/UTC Volume、冻结Risk输入、配置阈值和严重冲突中性。

- [ ] 价格D/D−7收盘与OI对应时点；每端只向前查5min历史，8D拒绝。
- [ ] Ticker当前价/24h涨幅来自Observation；缺风险输入NOT_READY。
- [ ] Micro/Taker与price/OI同轮冻结交给Squeeze；迟到确认不得进入历史。
- [ ] 配置watch/candidate/ready均生效；人工Profile优先，类别不受临时MC缺失改变。
- [ ] 共识threshold=.4，最高.5；严重冲突中性/0置信度，保留真实冲突指标供UI解释。
- [ ] Carry复用R05覆盖而不是另造max24h完整规则。

测试：`desktop/backend/tests/test_shortlab_repair_directional.py`。

```python
def test_severe_equal_conflict_is_neutral():
    config = {'consensus': {'conflict_ratio_threshold': 0.4}}
    result = ConsensusEngine(config).evaluate(equal_buy_sell_fixture)
    assert result['final_signal'] == 'NEUTRAL'
    assert result['confidence'] == 0
```

ConsensusEngine从现有engine.consensus.engine导入，equal_buy_sell_fixture用现有IndicatorResult构造等权双边信号，不新建生产同义引擎。额外覆盖V02/V03、+20% price/OI并Micro确认、缺确认、阈值20/30时LTSS50可成候选。

交付：directional-input-handoff.json，列出风险元数据与生产调用点。


### R05：Funding覆盖、入场Gate和FCS语义

**读取：**D05、D03.3。

**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/funding_schedule.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/entry_gate.py`。
**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/funding_score.py`。

**产出：**compute_schedule_coverage、evaluate_funding_entry_gate、统一FundingContext和ReadinessBreakdown。

- [ ] 生成确认结算槽，首尾缺失计入；未知历史schedule null而非推断24h或8h。
- [ ] event匹配±60s，schedule边界不重复；去重选择known_by前可知的有效event。
- [ ] 用同listingAge生成FULL/PARTIAL/INSUFFICIENT，30–89D不套完整90D门槛；unknown listing不把first_seen当上市时间。
- [ ] current与last settled正零负未知矩阵；current120s、last按预期槽匹配。
- [ ] 全部entry_gate参数消费；FCS仍100分排序，硬门槛独立。不得加“FCS高即覆盖FAIL”。

测试：`desktop/backend/tests/test_shortlab_repair_funding.py`。

```python
def test_recent_negative_blocks_high_history():
    context = make_funding_context(current_rate='-0.0005', last_settled_rate='-0.0005')
    result = evaluate_funding_entry_gate(context, default_gate_policy, context_as_of)
    assert result.status == 'FAIL'
    assert 'FUNDING_CURRENT_NON_POSITIVE' in result.reasons
```

default_gate_policy/context_as_of由本任务Fixture读取D15合并配置与固定时钟。另测全部阈值边界、schedule缺首尾、合法4h、缺mark只影响美元Carry、不影响费率本身。

交付：funding-gate-matrix.json（16交叉及配置开关结果）、coverage.json。


### R06a：单位、数量、VWAP与期限经济性

**读取：**D06.1/D06.2/D07.2/D18/D19。

**修改：**无既有源文件。

**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/units.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/economics.py`。

**合同：**native_liquidation_distance/native_stop_reference/compute_contract_vwap/evaluate_economics/build_ratio_proposal；Funding Gate通过ports接入。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 原生价/Canonical/FX分开，OI和Quote Volume不乘倍率；STOP BUY按原生Tick向上合法化
- [ ] bid/ask分别算contracts VWAP与覆盖，合法数量floor后重算实际h、资本及风险
- [ ] Unknown成本先UNKNOWN；Decimal净Carry严格大于门槛，hold1/60不同；费用不双计
- [ ] 六场景用真实数量/场景退出金额，穿越强平终值null；h0只读proposal不伪造Spot

测试：`desktop/backend/tests/test_shortlab_repair_units.py`、`desktop/backend/tests/test_shortlab_repair_economics.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
assert native_liquidation_distance('10','15') == '0.5'
assert native_stop_reference('10','15','0.005') == '12.5'
```

交付：native-unit-matrix.json、economics.json；倍率1/1000×FX1/.97与BE54.75向量；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R06b：保护能力、Planner与模拟入口

**读取：**D06.3/D12/D18/D19。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/planner.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/simulator.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/base.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/binance_spot.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/binance_alpha.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/ethereum_0x.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/venues/onchain.py`。

**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/protection.py`。

**合同：**保护模块为新建文件，其他为修改；真实入口simulate_hedge新增keyword futures_quote=None/ports=None，调用R06a端口；历史Request兼容。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 合约STOP能力未知不VERIFIED；平台能力/用户选择/已确认订单区分，Spot人工退出预案LIMITED
- [ ] protected_position_hash绑定数量/强平/STOP/规则；状态版本递增不单独失效
- [ ] simulate_hedge消费goal/期限/Funding Gate/原生报价，缺ports不授新READY
- [ ] Quote费用包含标记诚实；Alpha/0x仍indicative，不新增交易能力

测试：`desktop/backend/tests/test_shortlab_repair_planner.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
result = simulate_hedge(request, futures_mark=mark, spot_quote=quote,
    now_ms=fixture_now, futures_quote=futures_quote, ports=make_ports())
assert result.risk_validation != 'VERIFIED'
assert result.readiness == 'NOT_READY'
```

交付：capabilities.json、planner-conformance.json；示例采用明确UNKNOWN保护能力Fixture；另测UNKNOWN/UNSUPPORTED/SUPPORTED三态与模拟数值；protection.py按Create登记；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R07：联合规则建议

**读取：**D07、D18。

**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/decision.py`。

**消费：**R05 Gate、R06a build_ratio_proposal（通过RepairPorts）；**产出：**recommend_hedge。

- [ ] 实现Carry/Directional/Balanced不同输入门槛和可行比例选择；不把FCS当价格概率。
- [ ] 六个默认场景全部执行，Carry压力信用0，强平场景不能自动假设保护单成交。
- [ ] 资本/风险/数量/反向容量以实际proposal为准；tie-break固定成本/有效期/venue。
- [ ] DATA_INSUFFICIENT/AVOID/MANUAL_REVIEW与三种比例建议区分；source和reason完整冻结。
- [ ] h0无需Spot数据；仅输出只读指导，不保存两腿计划。输入改变/到期必须重算。

测试：`desktop/backend/tests/test_shortlab_repair_decision.py`。

```python
def test_carry_never_selects_partial():
    request = make_decision_request(goal='CARRY_CAPTURE')
    result = recommend_hedge(request, make_decision_context(), default_policy, ports=make_ports())
    assert result.recommendation == 'FULL_HEDGE'
    assert result.selected_proposal.target_ratio == '1'
```

必须另有可行FULL、可行PARTIAL、可行NO_HEDGE、无资本AVOID、未知DATA_INSUFFICIENT、强平MANUAL_REVIEW六个确定预期例，默认正例也必须选中FULL，不能用条件断言绕过None。默认policy包含D15且来自R00，不自设低风险场景使其通过。

交付：decision-cases.json，每例输入引用、所有候选、淘汰理由、选中结果与policy hash。


### R08a：事件账本与实际PnL

**读取：**D09/D18/D19。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/ledger.py`。

**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/pnl.py`。

**合同：**compute_ledger_pnl/有效事件聚合；仓库为RepositoryPort Fake，单位为合同端口。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 先resolve reversal/supersede再移动加权成本；全部数量Decimal，不读DOUBLE余额
- [ ] Spot美元entry成本冻结，Futures按结算币损益；Base费只计一次
- [ ] 实际FundingReceipt、未确认估算、费用未知分别显示；funding_basis按D09真值表
- [ ] 双腿已填不能自动ACTIVE，新状态FUNDED_PENDING_ACTIVATION并持续有仓位标识

测试：`desktop/backend/tests/test_shortlab_repair_pnl.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
pnl = compute_ledger_pnl(make_events(case='partial_close'), make_identity(),
    make_event_fx(), make_market_context())
assert pnl.realized_futures_usd == '40'
assert pnl.realized_spot_usd == '40'
```

交付：ledger-pnl.json；补USDC/第三币费用/Correction/Dust/零和未知费用；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R08b：Monitor、提醒与双边退出指导

**读取：**D09/D10/D12/D19。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/monitor.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/alerts.py`。

**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/exit_guidance.py`。

**合同：**LedgerPnl为R00冻结Fixture/端口输入；产出PairExitGuidance和Monitor必需cache，不要求PnL实现先合并。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 填入实际费用/退出成本/已知小计，unknown不当0；估算/实际资金费不双计
- [ ] Futures BUY reduceOnly和Spot SELL使用剩余原生数量/Step；Dust独立，不自动清零
- [ ] 手动强平后Spot孤腿URGENT_PAIR_EXIT；Mark越界只疑似未确认
- [ ] 所有有剩余仓位状态监控；过期/容量/保护失效降级，ACK不resolve风险

测试：`desktop/backend/tests/test_shortlab_repair_exit.py`、`desktop/backend/tests/test_shortlab_repair_monitor.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
guidance = build_pair_exit_guidance(plan, positions, market, rules, fixture_now)
assert all(Decimal(leg['native_qty']) <= remaining[leg['leg_type']]
           for leg in guidance.legs)
```

交付：exit-guidance.json、monitor-conformance.json；remaining由本任务Fixture的真实事件聚合取得；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R09：真实机会投射和当前筛选

**读取：**D08/D03.3。

**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/projection.py`。
**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/opportunity.py`。

**消费：**R05上下文、R06a/R06b经济；R01当前查询。**产出：**project_opportunity和risk_json.projection_v2。

- [ ] 写7D、bestVenue、BE、age/class、fcsConfigHash与真实expiresAt；defaultHold30，不用未传入的虚构期限。
- [ ] Venue按同合法Notional/能力/成本比较；只有indicative不得选成可执行bestVenue。
- [ ] 时间过期动态NOT_READY；旧JSON无v2明确LEGACY；研究模式include_stale=true不冒充当前READY。
- [ ] 当前列表先最新后筛选，null最后，symbol/id稳定排序；等分和分页反复请求不漂移。

测试：`desktop/backend/tests/test_shortlab_repair_opportunity.py`。

```python
async def test_latest_not_ready_hides_old_ready(repo):
    await seed_fcs(repo, symbol='PEPEUSDT', states=['READY','NOT_READY'])
    page = await repo.list_current_funding_opportunities(
        OpportunityQuery(readiness='READY'), as_of_ms=fixture_now)
    assert all(item['symbol'] != 'PEPEUSDT' for item in page.items)
```

seed_fcs写实际v2Projection且新记录时间更晚。补200同币历史、45D、nullBE、venue过滤、sourceAge和total事务一致性。

交付：opportunity-page.json、过滤/排序预期矩阵。


### R10a：Service/API边界与兼容窗口

**读取：**D02/D12/D18/D19。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/service.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/runtime.py`、`desktop/backend/src/diveintocrypto_desktop/api/app.py`、`desktop/backend/src/diveintocrypto_desktop/api/shortlab.py`。

**创建：**`desktop/backend/src/diveintocrypto_desktop/api/shortlab_repair.py`。

**合同：**RepairPorts/RepositoryPort/MarketPort与service_calls.json；生产未绑定返回IMPLEMENTATION_UNAVAILABLE，Fake只在tests。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 发布所有新增路由/DTO alias/error schema与producer调用点；严格原始JSON body、PATCH/CORS和expectedVersion
- [ ] 新增边界采用显式注入，旧档案读取不依赖新端口；未绑定不产生Fake READY
- [ ] 按D19.1冻结create_app(*,shortlab_runtime_factory=None)工厂；lifespan统一start/stop；Runtime新增repair_ports/allow_test_bindings keyword默认None/False，生产禁止True；R15a按相同工厂调用并验证生命周期次数
- [ ] 向R12/R13a/R13b发布HTTP合同与测试Fake，成功示例和错误示例由R00Fixture冻结

测试：`desktop/backend/tests/test_shortlab_repair_boundary.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
response = await client.post('/api/short/hedge/decisions', json=valid_request)
assert response.status_code == 503
assert response.json()['reasonCode'] == 'IMPLEMENTATION_UNAVAILABLE'
```

交付：api-boundary.json、service-calls-conformance.json；完成后Service/API文件仅R10b继续修改；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R10b：统一生产接线与API

**读取：**D02/D12/D18。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/service.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/runtime.py`、`desktop/backend/src/diveintocrypto_desktop/api/app.py`、`desktop/backend/src/diveintocrypto_desktop/api/shortlab.py`、`desktop/backend/src/diveintocrypto_desktop/api/shortlab_repair.py`。

**消费：**全部模块handoff。**产出：**真正默认Runtime可用的完整链路和新增API。

- [ ] 默认身份从Catalog→Resolver→保存Snapshot；Hedge同样消费解析结果，不取raw first candidate。
- [ ] _fetch_funding、_assemble_observed_inputs消费真实Observation及receipt；价格/风险同轮冻结。
- [ ] 注入Directional metrics provider，注册R11b/R14a/R14b callbacks；空库200/0，不能永久“not wired”。
- [ ] 实现Decision POST/GET、ExitGuidance、Protection与Capabilities；原始请求body用load_json_strict解析，单Provider缺数据返回合法DATA_INSUFFICIENT，系统未就绪才503；新增router注册、CORS含PATCH，不扩自动交易。
- [ ] 现有Opportunity改用当前查询；simulate/plan检查新Gate、有效期/引用/版本；activate按实际已成交头寸获取当前检查，不因原入场模拟20s过期永久拒绝已成交计划，保存ACTIVATION_CHECK；NO_HEDGE计划422。
- [ ] 按D19.6核验真实callback来源和test标记，默认禁止测试绑定；缺失/被拒绝绑定时API503 IMPLEMENTATION_UNAVAILABLE，不能授READY。
- [ ] 实际PnL/成本从R08a/R08b接Monitor，不能手填计算结果cache；事件币种和FX映射持久化。
- [ ] 兼容默认关闭读取历史；新建议503 HEDGE_DISABLED/FUNDING_CAPTURE_DISABLED，不能误表示源抓取异常。

测试：`desktop/backend/tests/test_shortlab_repair_service.py`、`desktop/backend/tests/test_shortlab_repair_api.py`。

```python
async def test_stale_version_cannot_activate(api_client, active_plan):
    response = await api_client.post(
        f'/api/short/hedge/plans/{active_plan.id}/activate',
        json={'expectedVersion': active_plan.version - 1})
    assert response.status_code == 409
    assert response.json()['reasonCode'] == 'HEDGE_VERSION_CONFLICT'
```

响应沿用现有错误包络并确保reasonCode别名；同时覆盖空body422、unknown字段422、重复请求幂等、保护hash变化、旧模拟只读、Decision过期、Provider451未知、退市BLOCKED。

交付：api-contract.json和正常Runtime wiring.log。不能仅构造定制Service跳过默认Runtime测试。


### R11a：预算映射、权重与HTTP发送

**读取：**D11/D19。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/request_budget.py`、`desktop/backend/src/diveintocrypto_desktop/data/http.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/providers/coingecko.py`。

**创建：**无新增源文件。

**合同：**RequestContext由R00增量完成；实现job_type档位、endpoint-weights-v3、RepositoryPort月预算，不另建Context。新增Spot `/api/v3/ticker/bookTicker` 的 `spotBookTicker` family：单symbol权重2、symbols或无symbol权重4，symbol与symbols同时传入拒绝发送；以官方接口核验记录为准。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

Spot `/api/v3/ticker/24hr` 的 `spotTicker` 同步采用已核验官方权重：单个 `symbol` 权重2；`symbols`数组1..100按本地保守权重40计，101个及以上按80计（官方权重分别为1..20个2、21..100个40、101个及以上80）；未提供symbol列表时权重80。畸形JSON、空数组、同时提供 `symbol` 与 `symbols` 均拒绝发送。Futures `ticker/24hr` 维持原权重不变。

- [ ] 冻结monitor/scanner/background映射，interactive在scanner；未知任务拒绝，不静默background
- [ ] 全部Mark/FundingInfo/Spot/Alpha/CoinGecko/0x允许URL注册family，未知host/path/limit拒绝
- [ ] 一次真实transport调用计费，retry新request_id，cache不计；发送前deadline检查
- [ ] 由data/http.py在每次transport发送前（所有预算/排队await结束后）重读UTC月键，执行旧ID取消/新UUID预留；在途完成归原月，Caller不重预留；补23:59:59排队→00:00:00发送、午夜在途返回、换月新额度失败不发送测试。
- [ ] 逐项测试D19.4状态表：重复reserve不再授许可、同终态finish幂等、逆终态/未知ID拒绝、CANCELLED不可复用；UTC跨月旧ID取消后新UUID预留，restart遗留计SENT，R01负责180天GC
- [ ] CoinGecko批量/分TTL/额度，缺FX保持UNKNOWN

测试：`desktop/backend/tests/test_shortlab_repair_runtime_budget.py`、`desktop/backend/tests/test_shortlab_repair_endpoint_weights.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
assert budget_class('opportunity') == 'BACKGROUND'
assert budget_class('interactive') == 'SCANNER'
assert budget_class('monitor') == 'MONITOR'
```

交付：http-counts.json、endpoint-map.json、monthly-budget.json；budget_class由本任务实现并用于实际预算，不只测试映射常量；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R11b：生产Market、Jobs与公平调度

**读取：**D11/D18/D19。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/jobs.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/market.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/scheduler.py`。

**创建：**无新增源文件。

**合同：**实现MarketPort；调用预算/计算/采集用RepairPorts和RepositoryPort，tests用合同替身。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 真实任务名上下文不可变传递，MARK/BOOK/FX/Funding均保留源时间并保存Quote引用
- [ ] 按Symbol合并Mark，10币深度、4并发/8s deadline、fair rotation，未服务容量降级
- [ ] 真实FCS/Projection/ListingAge/Hash和LedgerPnl传生产cache，不能只填symbol/mark
- [ ] 风险变化立即写，其他60s落盘，5s队列超时DEGRADED不清账；有资金暴露所有状态监控
- [ ] 独立调用capture/到期任务，不以用户有持仓为采集条件

测试：`desktop/backend/tests/test_shortlab_repair_load.py`、`desktop/backend/tests/test_shortlab_repair_market.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
report = await drive_ticks(symbol_count=11, plan_count=50, score_count=500)
assert report.max_tick_ms <= 8000
assert report.unserved_symbols == []
```

交付：load-report.json、market-conformance.json；drive_ticks为fake-clock测试driver，断言有界轮次与丢服务降级，不把真实慢网络算PASS；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R12：Monitor状态绑定、轮询、版本与API适配器

**读取：**D12.1/D18.3。

**修改：**`desktop/ui/src/app/data.js`、`desktop/ui/src/app/shortlab/hedge-monitor.jsx`。
**创建：**`desktop/ui/src/app/shortlab/monitor-state.mjs`。

**产出：**新增API适配器、正确activate/close body；Monitor可靠状态交接。

- [ ] 统一导出 `hedgeDecisionCreate/Get`、`hedgeExitGuidance`、`hedgeProtectionSave`、`shortCapabilities`，签名body/opts明确，保留现有API名称。
- [ ] loadedPlanId与输入分开；generation/AbortController；B失败不留A为B，所有写入从loadedPlanId取目标。
- [ ] 前台10s轮询、background暂停、恢复立即取；单在途、卸载取消、sourceAge/STALE，数据错误禁写。
- [ ] activate/close发送expectedVersion；手工记录币种/费用/执行时间来自冻结腿，USDC不能硬写USDT。
- [ ] 显示实际/估算资金费、Partial净收益、人工保护状态及双边退出指导。

测试：`desktop/ui/test/repair-monitor.test.mjs`、`desktop/ui/test/repair-data.test.mjs`。

```javascript
const calls = await captureActivateRequest('plan-b', {expectedVersion: 7});
assert.deepEqual(JSON.parse(calls[0].body), {expectedVersion: 7});
assert.equal(calls[0].url.endsWith('/plan-b/activate'), true);
```

captureActivateRequest使用真实data.js与fake fetch；状态测试覆盖A迟到/B失败/partialload/unmount/hidden页面。R15b再通过真实React浏览器操作验证。

交付：adapter-contract.json和状态时间线；i18n新增key由R13a集中添加。


### R13a：纯展示、状态模型与文案

**读取：**D07/D12.1/D19。

**修改：**`desktop/ui/src/app/i18n.js`。

**创建：**`desktop/ui/src/app/shortlab/decision-panel.jsx`、`desktop/ui/src/app/shortlab/plans-view.jsx`、`desktop/ui/src/app/shortlab/workflow-model.mjs`。

**合同：**组件只收props/callback，无HTTP；消费R00DTO与R12适配器合同，不要求R07实现。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 展示goal/期限/预算/原生单位/规则建议与淘汰原因；禁止预测概率或保证收益
- [ ] NO_HEDGE禁保存两腿计划，UNKNOWN禁推荐操作；STALE研究列表标签固定
- [ ] 全部状态/理由中文key一次冻结供R13b使用；i18n只有R13a owner
- [ ] plans-view只渲染真实数据props，不能自造localStorage计划

测试：`desktop/ui/test/repair-workflow.test.mjs`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```javascript
// 此任务为JavaScript测试，使用node:assert
assert.equal(canSavePairedPlan({recommendation: 'NO_HEDGE'}), false);
assert.equal(canSavePairedPlan({recommendation: 'DATA_INSUFFICIENT'}), false);
```

交付：workflow-fixtures.json、中文key清单；采用实际JavaScript/React测试入口；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R13b：实际路由和用户流程接线

**读取：**D07/D12.1/D19。

**修改：**`desktop/ui/src/app/desktop-app.jsx`、`desktop/ui/src/app/shortlab/funding-view.jsx`、`desktop/ui/src/app/shortlab/hedge-planner.jsx`。

**创建：**`desktop/ui/src/app/shortlab/workflow-bindings.mjs`。

**合同：**R13a组件、R12实际data.js导出、R10a HTTP合同；纯合同依赖，真实后端在R15b终验。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 候选Directional/Balanced与Funding Carry携symbol/snapshotId；输入改变使旧Decision失效
- [ ] HTTP实际适配器绑定props/callback；保存后真实list_plans→monitor，不用localStorage替身
- [ ] 研究Funding显式include_stale=true，点击建议刷新；h0只读指导，无两腿保存
- [ ] 能力关闭/未绑定/503兼容窗口显示不可用，绝不fallback Fake成功；取消/迟到请求隔离

测试：`desktop/ui/test/repair-workflow-binding.test.mjs`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```javascript
// Fake仅截获HTTP响应，实际组件绑定调用真实data.js
assert.equal(savedPlanNavigation.planId, apiSaveResponse.planId);
assert.equal(requestedDecision.symbol, selectedCandidate.symbol);
```

交付：workflow-binding.json；R15b真实浏览器验证，R13b不修改i18n或R13a组件源；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R14a：策略采样与精确数量报价任务

**读取：**D14/D19。

**修改：**无既有源文件。

**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/capture.py`。

**合同：**D18.1的capture_strategy_entries(CaptureContext,RepositoryPort,MarketPort,RequestContext)->CaptureResult与collect_due_quotes(RepositoryPort,MarketPort,as_of_ms,RequestContext)->QuoteCollectionResult；都是async，不能自定义返回dict或改参数位置。只有R00是合并前置；其他Producer是interface_dependencies，用R00的ports/Fixture测试，不要求其实现先合并。

- [ ] 四cohort、六策略，SYSTEM复制冻结proposal，不重算比例；按Decision同组采样
- [ ] 入SELL出BUY Futures、Spot入BUY出SELL，报价跨度<=5s；Entry只保存最终immutable结果
- [ ] h0无Spot依赖；精确ratio数量生成7/30/90任务；budget DEFERRED到deadline
- [ ] 旧缺Quote/FX不可缩放/当前回填；引用pin与来源保存

测试：`desktop/backend/tests/test_shortlab_repair_capture.py`。从P02规定目录运行对应pytest/Node入口；既有被测模块测试由此文件owner维护。

```python
assert system_entry['actual_ratio'] == decision.selected_proposal.actual_ratio
assert paired_baseline['source_snapshot_id'] == system_entry['source_snapshot_id']
```

交付：capture-timeline.json，精确数量/到期/预算/配对conformance；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R14b：历史结算、费用与指标

**读取：**D14/D13引用与保留。

**修改：**`desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/grader.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/metrics.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_grader.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_metrics.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/historical_market.py`、`desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/jobs.py`。
**创建：**`desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/evaluation.py`。

**产出：**HistoricalMarketProvider.read_mark_price_bars与分桶Metrics；消费R14a冻结Entry/任务合同，用R00Fixture独立结算，不负责capture实现。

- [ ] 退出手续费用exit Notional；缺Mark降低priced funding coverage；MAE/MFE只取完整区间内bar，部分首尾不能借其全小时极值；路径Partial及退市CENSORED正确。
- [ ] Metrics配对基线、成本后风险、Coverage/删失与资产bootstrap；不足30资产/100样本标不足，不宣称模型有效。
- [ ] HistoricalMarketProvider实现read_mark_price_bars，保存price_basis=MARK与真实receipt，固定cohort现金参考/USER_DECISION静态强平假设分别报告；提供build_metrics_provider正常Runtime注入说明，R10b接线；删除Grader版本fallback字面量，只import R00规范版本。RepositoryHistoricalMarketProvider以mark_price_bars_fn注入R03接口，未绑定不使用TRADE冒充MARK。

测试：`desktop/backend/tests/test_shortlab_repair_evidence.py`。

```python
def test_exit_fees_follow_exit_notional():
    result = settle_fixture(entry_price='100', exit_price='200', qty='10', fee_rate='0.001')
    assert result.entry_fee_usd == '1'
    assert result.exit_fee_usd == '2'
```

settle_fixture为实际Grader费用函数的测试包装，FX1、明确费用不含VWAP。再测V14/V15、SYSTEM同样本基线、无Mark路径、不足样本和删失不填0。

交付：outcome-fees.json、evaluation-report.json，不使用平均收益作为唯一验收。


### R15a：隔离Harness与浏览器测试设施

**读取：**D16/D19及R00/R10a合同（R10a是接口依赖）。

**创建：**`desktop/backend/tests/repair_harness.py`、`desktop/backend/tests/repair_server.py`、`tests/e2e/test_shortlab_repair_flow.py`、`desktop/ui/e2e/repair.spec.js`、`desktop/ui/playwright.repair.config.mjs`。

**修改：**`desktop/ui/package.json`、`desktop/ui/package-lock.json`仅增加开发依赖 `@playwright/test: 1.56.0` 与 `test:repair-e2e` script，不改生产依赖。

Harness使用真实App/Runtime/Service/Repo与scheduler，HTTP Provider返回R00raw fixtures；测试服务在独立后端Python进程启动，隔离根tests/conftest.py对crypcodile/aiolimiter/httpx的全局Mock；根E2E使用标准库urllib访问该进程，不使用被patch的httpx transport。没有直接灌“评分结果/净收益”替代计算。测试服务仅harness注册、127.0.0.1:46409，不进入生产入口。测试场景normal/negative-funding/unknown-schedule/slow-provider/plan-switch，所有记录位于测试data_dir。

冻结helper：create_repair_test_app(data_dir: Path,scenario: str,clock_ms: Callable[[],int],*,bindings: RepairPorts | None=None)->FastAPI；通过create_app(shortlab_runtime_factory=closure)构造真实ShortLabRuntime，clock_ms传其已有clock参数。closure显式传repair_ports=bindings、allow_test_bindings=(bindings is not None)；bindings=None是R15b真实组合/False，非None只能是已标TEST_FAKE的边界测试ports/True，非None真实ports拒绝TEST_BINDINGS_REQUIRED；不能用于最终验收。测试assert factory调用1次、start/stop各1次，无HTTP触发重复构造。


浏览器命令从项目根运行：

```bash
PLAYWRIGHT_BROWSERS_PATH="$PWD/desktop/ui/test/.tmp/ms-playwright" npm exec --prefix desktop/ui -- playwright install chromium
PLAYWRIGHT_BROWSERS_PATH="$PWD/desktop/ui/test/.tmp/ms-playwright" npm --prefix desktop/ui run test:repair-e2e
```

playwright config令outputDir为UI/test/.tmp/repair-browser、reporter输出JSON及失败trace；webServer启动repair_server，环境含项目内data_dir，超时60s，测试结束停止，不遗留进程。正常情况下每个浏览器用例timeout30s，单次load压力测试timeout60s。

测试设施自验：`desktop/backend/tests/test_shortlab_repair_harness.py`；完整根E2E/浏览器用例由本任务创建，R15b只执行不修改。必须断言控制URL origin恰为http://127.0.0.1:46409；只能urllib客户端访问独立后端进程。

```javascript
await page.getByRole('button', {name: '进入监控'}).click();
await expect(page.getByTestId('loaded-plan-id')).toHaveText(savedPlanId);
await advanceHarnessClock(10000);
await expect(page.getByTestId('risk-state')).toContainText('单腿');
```

advanceHarnessClock只调用测试harness时钟控制；该控制路由生产不存在。savedPlanId通过实际保存API响应取得，不能写死。

交付：harness-conformance.json、browser-install.json；当前阶段只验隔离进程/46409/urllib和测试工具可用，不将真实功能用例标PASS。完整用例文件与测试函数已写但等待R15b执行；离线无法安装浏览器记录UNVERIFIED。


### R15b：真实绑定后的全功能验收

**读取：**D16/D19/P06。

**修改：**无既有源文件。

**创建：**无新增源文件。

**合同：**所有实现已合并+R10b实际bindings+R15a已交付测试设施；本任务为测试/证据，不拥有生产源。合并前置严格为P04的R10b/R12/R13a/R13b/R15a；此阶段禁止Producer替身。

- [ ] 执行真实候选→Decision→Simulation→DRAFT→成交→保护→ACTIVE→退出→CLOSED，禁止成品Score/PnL/Outcome替身
- [ ] 正常Runtime无需额外Identity/Metrics函数才能工作，只有原始HTTP Fixture可注入
- [ ] 真实React计划切换/失败/迟到/10s风险/版本409与币种，浏览器安装失败UNVERIFIED
- [ ] 推进真实采集/归档/Grader与11币/500评分竞争，数值/来源/配对检查
- [ ] 根E2E仅urllib，URL origin/46409必断言，禁止httpx/websockets全局Mock获得假通过

测试：`tests/e2e/test_shortlab_repair_flow.py`、`desktop/ui/e2e/repair.spec.js`。从P02规定目录运行对应pytest/Node入口；测试由R15a维护，业务缺陷回其源文件owner；R15b不修改测试。

```python
assert test_origin == 'http://127.0.0.1:46409'
assert response_source == 'isolated-backend-process'
assert bindings_mode == 'REAL_PRODUCERS'
```

交付：end-to-end.json、browser-report.json/trace、load.json；问题回对应owner，不能修改其他任务测试降低要求；conformance.json记录调用点schema、合同SHA、测试命令及真实/替身模式。最终真实集成由R10b/R15b验证。


### R16：最终回归、CI、打包与第三方交付

**读取：**D01/D13/D16和P06矩阵。

**修改：**`.github/workflows/ci.yml`、`.github/workflows/release.yml`、`desktop/ui/package.json`、`desktop/ui/package-lock.json`、`desktop/backend/short-lab.spec`、`scripts/smoke_shortlab_packaged.py`、`README.md`、`desktop/README.md`、`docs/api.md`、`docs/packaging.md`、`docs/testing.md`。
**产出：**真实CI artifact、完整打包、中文使用说明；不新增架构或参数。

- [ ] 顺序接收R15a package/lock：engines固定>=22 <23且lock根engine同步；ci.yml及release桌面UI构建job显式setup-node22；Node22 npm install --package-lock-only不漂移R15a依赖，随后npm ci。静态测试断言engine/CI，manifest记录node/npm版本。
- [ ] 全后端、根目录、UI Node与浏览器测试；报告具体deselected/live未运行，不用总数替代范围。
- [ ] 按D19.6检验生产esbuild metafile无test/e2e/stub/Playwright模块，PyInstaller清单无backend/tests/repair_fixtures；默认factory真实绑定，缺绑定503；不能用测试alias构建生产产物。
- [ ] 构建真实UI dist并核对路由资源；PyInstaller资源含001–006/default/identity/DuckDB；既有端口和CLI兼容assert。
- [ ] Windows frozen只读解压目录启动两次，用户可写目录保存/迁移/读取同DB；006失败不损旧账本。其他平台未验证不得列PASS。
- [ ] Desktop tag `short-lab-v*` 不触发Android；Android `v*` 不触发Desktop；保留现有实际workflow条件并加静态测试。
- [ ] CI上传P06全部证据，文件缺失error；owner policy/manifest摘要可审核；网络POC缺环境报告UNVERIFIED。
- [ ] README中文包含默认能力、启用方式、研究评分边界、正费率检查、原生强平单位、手工保护/成交、部分退出、数据过期、NO_HEDGE不能保存配对计划与证据不足。

新增测试：`tests/test_shortlab_repair_packaging.py`（项目根）、`tests/static_analysis/test_shortlab_repair_release.py`。执行完整套件按P02；UI构建在UI目录 `npm run build`；打包在backend目录：

```bash
UV_CACHE_DIR="$PWD/runtime/uv-cache" uv run --with pyinstaller pyinstaller short-lab.spec --noconfirm
```

Frozen smoke调用既有smoke脚本并扩展实际006/二次启动断言；准确CLI参数从脚本argparse冻结到交付manifest.command，不能写未存在选项。Windows数据位置遵循现有path resolver：实际两次frozen启动用项目内SHORTLAB_DATA_DIR；默认LOCALAPPDATA规则另以resolver的env映射把测试profile放项目内核验，并在独立frozen子进程的测试环境映射下验证默认路径。不得写真实用户根外目录，不需要为执行测试请求额外位置授权。

交付：release-manifest.json、全部功能矩阵、安装/配置/限制说明；任何BLOCKED功能不得宣传为已实现。

## P06. 设计—任务—测试—证据验收矩阵

| 向量 | 主任务/联调 | 测试文件（项目根相对路径） | 证据artifact |
|---|---|---|---|
| V01 身份 | R02a/R02b/R10b/R15b | desktop/backend/tests/test_shortlab_repair_identity.py、desktop/backend/tests/test_shortlab_repair_service.py、tests/e2e/test_shortlab_repair_flow.py | identity-handoff.json、catalog-handoff.json、end-to-end.json |
| V02 时间/重启 | R03/R04/R15b | desktop/backend/tests/test_shortlab_repair_observations.py、desktop/backend/tests/test_shortlab_repair_directional.py | provider-envelope.json、end-to-end.json |
| V03 窗口/风险 | R04 | desktop/backend/tests/test_shortlab_repair_directional.py | directional-input-handoff.json |
| V04 schedule | R05/R14a/R14b | desktop/backend/tests/test_shortlab_repair_funding.py、desktop/backend/tests/test_shortlab_repair_evidence.py | coverage.json、capture-timeline.json |
| V05 Gate | R05/R10b | desktop/backend/tests/test_shortlab_repair_funding.py、desktop/backend/tests/test_shortlab_repair_api.py | funding-gate-matrix.json、api-contract.json |
| V06 单位 | R06a/R06b/R08a/R08b | desktop/backend/tests/test_shortlab_repair_units.py、desktop/backend/tests/test_shortlab_repair_planner.py、desktop/backend/tests/test_shortlab_repair_exit.py | native-unit-matrix.json、exit-guidance.json |
| V07 期限 | R06a/R06b/R09 | desktop/backend/tests/test_shortlab_repair_economics.py、desktop/backend/tests/test_shortlab_repair_opportunity.py | economics.json、opportunity-page.json |
| V08 能力 | R06a/R06b/R10b | desktop/backend/tests/test_shortlab_repair_planner.py、desktop/backend/tests/test_shortlab_repair_api.py | capabilities.json、api-contract.json |
| V09 建议 | R07/R15b | desktop/backend/tests/test_shortlab_repair_decision.py、tests/e2e/test_shortlab_repair_flow.py | decision-cases.json、end-to-end.json |
| V10 最新机会 | R01/R09 | desktop/backend/tests/test_shortlab_repair_repository.py、desktop/backend/tests/test_shortlab_repair_opportunity.py | repository-contract.json、opportunity-page.json |
| V11 PnL | R08a/R08b/R15b | desktop/backend/tests/test_shortlab_repair_pnl.py、tests/e2e/test_shortlab_repair_flow.py | ledger-pnl.json、end-to-end.json |
| V12 UI安全 | R12/R15b | desktop/ui/test/repair-monitor.test.mjs、desktop/ui/test/repair-data.test.mjs、desktop/ui/e2e/repair.spec.js | adapter-contract.json、browser-report.json |
| V13 负载 | R11a/R11b/R15b | desktop/backend/tests/test_shortlab_repair_load.py、desktop/backend/tests/test_shortlab_repair_runtime_budget.py | load-report.json、http-counts.json、load.json |
| V14 capture | R14a/R14b/R15b | desktop/backend/tests/test_shortlab_repair_capture.py、tests/e2e/test_shortlab_repair_flow.py | capture-timeline.json、end-to-end.json |
| V15 费用/删失 | R14a/R14b | desktop/backend/tests/test_shortlab_repair_evidence.py | outcome-fees.json、evaluation-report.json |
| V16 迁移/打包 | R01/R16 | desktop/backend/tests/test_shortlab_repair_migration.py、tests/test_shortlab_repair_packaging.py、tests/static_analysis/test_shortlab_repair_release.py | migration-summary.json、release-manifest.json |

所有路径均相对于项目根；V16两项tests/路径为根测试例外，不加backend前缀。验收manifest必须记录完整命令及工作目录。

## P07. 问题覆盖与交付门槛

| 审查项 | 实现owner | 生产验证 |
|---|---|---|
| S01/S02 | R02a/R02b、R10b | R15b默认Runtime |
| S03/S04 | R03、R10b | R15b重启/时间 |
| S05/S06/S07/S08/S09 | R04（身份覆盖R02a/R02b） | R15b选币/风险 |
| H01/H02/H07 | R05 | R10b双入口Gate、R15b |
| H03/H04/H05/H06 | R06a/R06b（最终选择R07） | R15b单位/经济/确认 |
| H08 | R09、R11a/R11b | R10b机会API |
| M01/M06 | R08a/R08b、R10b（导航R13a/R13b） | R15b净收益/退出 |
| M02/M03/M04/M07 | R12 | R15b浏览器 |
| M05 | R07 | R15b三个目标/六类输出 |
| E01 | R01、R09 | R10b查询 |
| E02/E03 | R11a/R11b | R15b混合负载 |
| E04/E05/E06/E07 | R14a/R14b、R10b | R15b采集/评估 |
| 默认能力/外部配额 | R00/R11a/R11b/R13a/R13b/R16 | capabilities、预算及交付说明 |

最终门槛：全部适用V01–V16通过；任何未验证网络/平台能力明确列限制；不存在丢失的文件owner/DTO/来源引用；旧数据库迁移和账本保留验证；新页面不是仅Mock演示；仓库代码、UI dist与打包来源同一source commit；所有测试/证据可复核。

交付第三方的最小包：两份本文档、source commit、配置示例/完整默认配置、合同与Fixture清单、任务manifest索引、API示例、真实构建/测试/打包结果、未验证能力列表。不得用文档合并或规则高分代替功能实现和策略验证。
