# Short-Lab 代码审查与验证报告

日期：2026-10-10
审查范围：当前分支源码、数据库迁移、桌面 UI、生产接线、测试设施和打包配置。
源码基线：`66af9f07856d811df2203c43cdb4499021a7ddaa` 加当前工作区修复。本报告随修复一并提交，验证源码以包含本报告的修复提交为准；不能将基线提交单独视为本报告验证的源码。
对应合同：[优化修复设计方案](ShortLab_Optimization_Repair_Design_CN.md)、[优化修复实施计划](ShortLab_Optimization_Repair_Implementation_Plan_CN.md)。

## 1. 结论与适用范围

23 项问题均已按当前函数签名、真实 DTO、默认 Service/Runtime 接线重新核查；涉及的代码缺陷已修复，并通过本地回归。最终审查未发现需要继续修复的已知源码功能问题。

本地验证覆盖做空评分、资金费率制度与入场门禁、对冲建议、手工成交账本、保护确认、激活检查、持仓监控、双边退出指导、策略采样、证据结算、统计展示和桌面资源加载。系统提供研究和操作指导，不执行真实交易。没有将未知数据补成零、将历史 READY 回退为当前机会，或将测试计算结果替换为生产结果。

**代码与离线验证通过不等于全部外部交付验收完成。** Windows 成品、真实地域/密钥下的 Provider 联调和远程 CI artifact 尚未在本次环境执行；这些项目列为 UNVERIFIED，不列 PASS。

## 2. 最终实测结果

| 验证范围 | 实测结果 | 证据 |
|---|---|---|
| 后端完整测试 | 1871 passed，6 deselected，0 failed | `desktop/backend/runtime/audit20261010/backend-final.log`、`backend.xml` |
| 项目根完整测试，包括 API E2E 与静态检查 | 120 passed，0 failed | `desktop/backend/runtime/audit20261010/root-final.log`、`root.xml` |
| UI Node 测试 | 105 passed，0 failed | `desktop/backend/runtime/audit20261010/ui-final.log` |
| 真实 React/Chromium 浏览器验收 | 8 passed，0 failed | `desktop/backend/runtime/audit20261010/browser-final.log`、`desktop/ui/test/.tmp/repair-browser/report.json` |
| UI 正式构建 | 成功 | `desktop/backend/runtime/audit20261010/ui-build.log` |
| macOS PyInstaller 成品构建 | 成功 | `desktop/backend/runtime/audit20261010/packaging-build.log` |
| macOS 只读安装目录、两次 frozen 启动、同一数据库和资源检查 | PASS | `desktop/backend/runtime/audit20261010/packaged-final/packaged-smoke.json` |
| 差异格式检查 | `git diff --check` 通过 | 本地交付 manifest |

运行环境：Python 3.12.11、Node 22.20.0、PyInstaller 6.22.3。后端与根测试各有一个 Starlette/httpx 弃用提示，没有测试失败。6 个 deselected 是需外网的 live 测试，不计入已验证功能。

证据清单、完整命令、工作目录、退出码和 SHA256 保存在 `desktop/backend/runtime/audit20261010/manifest.json`。该目录是项目内本地证据目录；远程审核需携带 manifest 和实际 artifact，不能仅引用此路径或测试数量。

## 3. 23 项问题核查

表中的“通过”指当前源码审查及本地验证范围；在线 Provider 和 Windows 的验证边界仍遵循第 6 节。

| ID | 当前实现合同 | 主要验证入口 | 结论 |
|---|---|---|---|
| CR01 | 激活使用严格 expectedVersion，实际检查身份、Mark、深度、Funding Gate、经济性和保护；UNKNOWN/异常不能进入 ACTIVE | `test_shortlab_repair_service.py`、`test_shortlab_hedge_api.py`、根 API E2E | 通过 |
| CR02 | FundingEventRecord 与 Mapping 均正确读取；每个事件保存真实响应 receipt，不以聚合事件伪造观测时间/间隔 | `test_shortlab_repair_data_producers.py`、`test_shortlab_repair_funding.py` | 通过 |
| CR03 | 正常回填和评分采集真实 fundingInfo/事件 receipt 并归档；默认制度只从有效的当前确认起生效 | `test_shortlab_repair_observations.py`、`test_shortlab_repair_data_producers.py`、生产接线审查 | 通过 |
| CR04 | 当前投射为空保持空集，不回退历史 READY | `test_shortlab_repair_opportunity.py`、`test_shortlab_repair_repository.py` | 通过 |
| CR05 | 正常 Monitor 消费实际 PnL、保护确认和六项检查；降级不清空持仓、不伪造安全状态 | `test_shortlab_repair_service.py`、`test_shortlab_hedge_monitor.py`、API E2E | 通过 |
| CR06 | 双边退出消费实际 Quote/Rules DTO；期货原生数量与现货 canonical 数量只换算一次；检查精确数量、规则、Venue、费用和最早到期时间 | `test_shortlab_repair_exit.py`、`test_shortlab_repair_units.py`、1000 倍合约服务回归 | 通过 |
| CR07 | 默认新评分使用 receipt/schedule 覆盖；旧 max24h 条件不能单独授予新决策完整性 | `test_shortlab_repair_directional.py`、`test_shortlab_repair_funding.py`、默认接线回归 | 通过 |
| CR08 | 人工 Profile override 进入真实解析和评分路径 | `test_shortlab_repair_identity.py`、`test_shortlab_repair_service.py` | 通过 |
| CR09 | 最近结算与已确认制度的最后预期槽核对，不能凭 event/source 自洽接受旧槽 | `test_shortlab_repair_funding.py`、实际 Market/FundingContext 接线 | 通过 |
| CR10 | 新 Feature/Entry 使用集中声明的当前版本，不以旧常量默认写入旧桶 | `test_shortlab_repair_directional.py`、`test_shortlab_repair_service.py`、静态合同检查 | 通过 |
| CR11 | 正常机会 Producer 保存当前投射及其版本、来源与评分字段 | `test_shortlab_hedge_production_jobs.py`、`test_shortlab_repair_opportunity.py` | 通过 |
| CR12 | 手工成交保存实际来源时间、响应 receipt 和 FX ID；账本、Monitor、激活和退出消费 EVENT_FX；历史 FX 缺失保持 UNKNOWN | 延迟 FX 服务回归、`test_shortlab_repair_pnl.py`、API E2E | 通过 |
| CR13 | 默认 FX Provider 和 Catalog 注入持久月预算；实际重试逐次计数、缓存不额外占额，耗尽阻止发送 | `test_shortlab_repair_data_producers.py`、`test_shortlab_identity_catalog.py`、`test_shortlab_repair_runtime_budget.py` | 通过 |
| CR14 | 后台任务保留实际 job_type/job_id/deadline；正常采集使用 MarketPort，不借用 monitor 档位 | `test_shortlab_hedge_production_jobs.py`、`test_shortlab_repair_runtime_budget.py`、真实发送 E2E | 通过 |
| CR15 | 评分/机会/人工决策具有对应采样入口；真实 Capture owner 和 Repository 保存 Entry/QuoteTask；到期结算读取任务中的精确数量和冻结来源 | `test_shortlab_production_capture_integration.py`、`test_shortlab_repair_capture.py`、`test_shortlab_review_evidence.py`、API E2E | 通过 |
| CR16 | TRADE bars 不充当 MARK 强平路径；函数存在不代表历史覆盖完整 | `test_shortlab_repair_evidence.py`、`test_shortlab_review_evidence.py` | 通过 |
| CR17 | 路径覆盖按实际小时区间、首尾边界和内部缺口判定，单根 bar 不授予 7D 完整性 | `test_shortlab_repair_evidence.py`、`test_shortlab_review_evidence.py` | 通过 |
| CR18 | Evidence 净收益实际扣除冻结的入场费用、退出费用和每事件 Funding/FX；缺失价格、FX 或成本不补零 | `test_shortlab_repair_evidence.py`、`test_shortlab_hedge_evidence.py` | 通过 |
| CR19 | 成功加载同一计划后出现 STALE/轮询失败，全部写按钮禁用；强制点击也不能发送写请求 | `desktop/ui/e2e/repair.spec.js`、UI 状态测试 | 通过 |
| CR20 | 验收只在原始 HTTP 响应边界提供 fixture，保留实际发送预算、解析、换算、门禁与账本；外网阻断；每次浏览器验收使用独立数据库 | `test_shortlab_repair_harness.py`、根 API E2E、8 项浏览器验收 | 通过 |
| CR21 | Playwright 从正式声明的依赖导入，未引用临时安装目录 | package/lock/Playwright 配置、Node22 实测 | 通过 |
| CR22 | CI 使用 Node22，运行真实浏览器验收并上传测试结果；构建产物与提交的 dist 一致性由 CI 检查 | `.github/workflows/ci.yml`、发布静态检查、本机正式构建 | 代码及本地检查通过；远程 CI 未执行 |
| CR23 | Micro/Taker 确认具有真实同轮采集和观测时间，受 EntryTop10/发送预算约束并冻结进入输入 | `test_shortlab_repair_data_producers.py`、`test_shortlab_repair_directional.py`、Service 生产接线审查 | 通过 |

上表测试文件未注明根路径时，均位于 `desktop/backend/tests/`。生产自动采样测试验证真实 Service/Capture/Repository 落库；原始 HTTP E2E 验证实际 Market、FX、发送预算和完整 API 流程，两类证据分别说明自己的边界。

## 4. 数学、数据与统计一致性

- **持仓成本：**部分平仓只消耗当前库存；之后再次加仓按剩余库存计算移动加权均价。计算使用 80 位 Decimal 局部上下文，不改变调用方上下文。完全平仓后缺少可恢复成本的冲正保持未知。
- **币种与 FX：**线性期货以原生报价币的价格差乘原生数量，再使用退出时 FX 换算；现货使用历史美元成本与退出美元价值。期货不将历史 FX 本金变化计为价格盈亏。1000 倍合约有独立数量和不同入/出场 FX 回归。
- **保护与退出：**部分平仓改变数量后，旧保护确认不能继续当作当前保护；需重新确认。退出报价的数量、规则、深度、费用和到期时间同步验证。
- **时间与不可变快照：**响应 receipt 在真实请求完成后读取；采集不把 batch 开始时间写成 known_at。FX 来源不能晚于成交时间；晚到的响应只推进知识时间，不回填来源时间。
- **证据隔离：**Entry ID 包含 cohort，不同 cohort 使用同一 source ID 不覆盖。结算和基线配对按确切 Entry/source/horizon 关联，六种策略均可保存、读取和统计。
- **统计分桶：**公式、配置、Profile、cohort、goal、historyClass、Venue 不同的样本分别统计；混合顶层仅作计数，不展示混合收益/置信区间。资产聚类 bootstrap 重采样完整资产簇，固定 seed 和次数；样本不足明确 INSUFFICIENT_SAMPLE。月度输出保持规则实验/样本外标签，不宣称已校准概率模型。
- **API 与 UI：**实际仓库统计通过真实 API 序列化送到 React，展示入场状态、可成交率、费用、Funding Mark 覆盖、路径覆盖、不利变动、回撤、配对差异、bootstrap 与 OOS；未知值显示未知。
- **预算合同：**端点注册按 host/full path/params 验证，未知或非法参数拒绝。现货 bookTicker 和 24hr ticker 的权重已按[Binance 官方 Market 文档](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market)核验，设计与计划使用同一 `endpoint-weights-v3` 合同。

## 5. 实施任务与验收向量覆盖

| 任务 | 本地核查与验证范围 |
|---|---|
| R00 | DTO、配置、集中版本、函数签名、Fixture、owner policy 和生产构建隔离 |
| R01 | 006 迁移、查询、不可变来源归档、引用保留和故障回滚 |
| R02a / R02b | 身份解析、倍率、人工覆盖、Catalog 来源与配额 |
| R03 | 来源时间、receipt、制度采集与已存记录恢复 |
| R04 | 冻结输入、PIT 窗口、风险分类、Micro/Taker、评分版本 |
| R05 | Funding schedule、预期槽、覆盖、Gate、FCS 和未知语义 |
| R06a / R06b | Decimal 单位、精确数量、VWAP、经济性、场景风险、保护能力与模拟 |
| R07 | 联合建议、目标与风险/期限约束、未知输入拒绝 |
| R08a / R08b | 事件账本、实际 Funding、FX/PnL、Monitor、提醒与双边退出 |
| R09 | 当前机会投射、最新代际和筛选，不回退旧机会 |
| R10a / R10b | API/Service 合同、默认生产接线、版本冲突、嵌套计划决策引用 |
| R11a / R11b | 实际发送预算、重试与月额、Market、后台调度、并发/负载和降级 |
| R12 | Monitor 轮询、请求取消、计划切换、版本及全部写操作约束 |
| R13a / R13b | 展示状态、真实路由、操作流程与未知/不可用提示 |
| R14a / R14b | 策略采样、精确报价任务、历史结算、费用、删失、完整统计与分桶 |
| R15a / R15b | 隔离原始 HTTP Harness、真实 API E2E、真实浏览器流程与失败场景 |
| R16 | 完整本地回归、Node22、正式 UI 和 macOS 成品；Windows/远程 CI/live 验收见第 6 节 |

V01–V15 的对应测试均已执行；V16 的迁移、资源、发布静态检查和 macOS 成品已验证。V16 中 Windows 实机成品验收尚未验证，不能将该向量整体宣传为跨平台通过。

设计与实施计划已对照核查：任务分工、函数合同、状态、版本、数量/费用/时间语义及预算约束一致。现货端点权重的定义同时落在设计、计划、代码和测试中，没有保留二选一事项。

## 6. 必须携带的验证边界

1. **Windows frozen：UNVERIFIED。** 本次是 macOS 成品验证；Windows 两次启动、只读安装目录和默认 LOCALAPPDATA 行为需使用 Windows CI/成品 artifact 验收。
2. **真实在线 Provider/地域/密钥：UNVERIFIED。** 本次不执行真实资金操作；Alpha、Ethereum/0x 等环境相关链路的原始响应合同测试不代替有环境的受控 PoC。缺配置、地域不可达或无可执行报价仍应返回不可用。
3. **远程 CI/发布 artifact：UNVERIFIED。** 本报告记录本地验收；没有附本次远程 run/artifact 链接。CI 配置与本地执行证据已核查，不虚构远程验收结果。
4. **研究有效性：不由源码测试证明。** 得分、正资金费率或对冲比例不证明未来下跌概率或无损收益；报告使用真实覆盖、缺失、删失、场景和配对样本，不把规则实验描述成概率保证。

本报告证明上述明确范围内的代码行为与测试结果，不承诺不存在任何潜在缺陷，也不将缺少实际外部验收的事项归类为已通过。
