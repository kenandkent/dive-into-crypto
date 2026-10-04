# ShortLab 代码实现审查报告

审查日期：2026-10-03。审查对象为当前工作区实际代码；Git HEAD 为 `92999cf2f7c2a1491cdd071b68053d2c50cd46c6`，大量实现尚在工作区，因此不能把该 HEAD 单独视为本报告实现基线。

依据：`ShortLab_Integrated_Upgrade_Design_CN.md` 与 `ShortLab_Integrated_Implementation_Plan_CN.md`。本次仅审查并记录问题，没有修改业务代码。

## 结论

当前实现尚不能认定为全部按实施计划完善交付。主要缺口集中在默认生产调用链、对冲市场数据、人工操作UI、账本状态保护、历史Evidence和真实打包验收。模块存在、注入fixture的测试通过，不能替代默认运行路径的验收。

## 验证结果与边界

- 后端测试：1317 passed，6 deselected，1 warning，67.16秒。
- 根目录测试：95 passed，1 warning，3.15秒。
- UI测试：49 passed，0 failed。
- 进行了离线调用复现及默认接线静态审查；未访问私人账户、发起交易或进行在线行情/限流验证。
- 未运行真实Windows冻结成品。生产接线缺失、占位数据和异常分支问题不因当前测试全绿而消失。

优先级：P0为会输出虚构关键风险数据的阻断问题；P1为重要功能不可用、状态/数据错误或必需交付门禁缺失；P2为预算、审计及请求合同等仍需修正的问题。

## 问题清单

### CR01 [P0] 生产 Monitor 使用固定价格和虚构退出容量

- 对应任务/合同：H07/H08，AC16–AC18。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/service.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/service.py:1735)。
- 问题与影响：run_hedge_monitor 为所有资产构造 futures mark=1、spot sell VWAP=1、退出数量=1000000、滑点=1bps，并将时间标为当前；没有读取真实市场缓存，传入的结算 Funding 事件也为空。启用并存在活跃计划时，会输出不可信的 PnL、基差、强平距离和退出能力。
- 建议修复及验收：移除默认假行情；消费真实 observation/cache，缺失或超时必须降级并保留持仓状态。以非 1 行情、缺失报价和负 Funding 的完整默认调用链验收。

### CR02 [P1] 默认 Hedge provider 未接入生产服务

- 对应任务/合同：H02–H05/H08。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/runtime.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/runtime.py:450)。
- 问题与影响：runtime 创建 ShortLabService 时未提供 hedge_mark_fn、hedge_quote_fn 或对应规则/Funding回调。service.py:3286–3297 在回调为空时直接抛出 provider unavailable；适配器文件存在不代表已接入。配置启用后默认模拟/报价仍不可用。
- 建议修复及验收：在 runtime 连接真实适配器及缓存/预算，验证无测试注入的服务路径；未配置 provider 应明确报告不可用。

### CR03 [P1] Planner 请求数值类型违反 Decimal 字符串合同

- 对应任务/合同：H09/H08。
- 代码定位：[desktop/ui/src/app/shortlab/hedge-planner.jsx](/Users/ken/workspace/short-lab/desktop/ui/src/app/shortlab/hedge-planner.jsx:73)。
- 问题与影响：UI 使用 Number() 发送名义金额、保证金和比率，后端 Decimal DTO 要求非空十进制字符串。离线复现默认 UI 金额 10000 得到 HEDGE_INPUT_INVALID，在访问 provider 前即失败。
- 建议修复及验收：UI 按字符串序列化全部 Decimal 字段；用真实 API schema 检查 UI 默认模拟请求。

### CR04 [P1] Monitor 页面默认导航永久停在加载状态

- 对应任务/合同：H09。
- 代码定位：[desktop/ui/src/app/shortlab/hedge-monitor.jsx](/Users/ken/workspace/short-lab/desktop/ui/src/app/shortlab/hedge-monitor.jsx:62)。
- 问题与影响：desktop-app.jsx:182 渲染 HedgeMonitor 未传 planId；组件只有 planId 非空才加载，但 loading 初始为 true，且先返回 spinner，再渲染 planId 输入控件。正常进入页面无法选择计划。
- 建议修复及验收：先展示计划选择或加载列表，并为无 planId 设置可操作空态；测试真实导航而非 initial 数据注入。

### CR05 [P1] 人工成交登记 UI 与 API 请求结构及枚举不兼容

- 对应任务/合同：H06/H08/H09。
- 代码定位：[desktop/ui/src/app/shortlab/hedge-monitor.jsx](/Users/ken/workspace/short-lab/desktop/ui/src/app/shortlab/hedge-monitor.jsx:68)。
- 问题与影响：UI 发 flat legType/eventType/nativeQty/nativePrice/clientEventId。service.py:4199–4213 要求 event/event_json 包装及版本字段；实际请求报 UNKNOWN_FIELD legType。UI FUTURES/SPOT 也与 FUTURES_SHORT/SPOT_LONG 不同。
- 建议修复及验收：按冻结事件 DTO 构造请求，补时间、来源、schema/version 与乐观锁字段；端到端验证开仓、减仓和重试幂等。

### CR06 [P1] 关闭计划在读取失败时错误认定已平仓

- 对应任务/合同：H06/H08。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/service.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/service.py:4402)。
- 问题与影响：aggregate_hedge_position 异常被替换为空 positions，随后更新 CLOSED；Decimal 解析异常也被当作余量 0。离线复现仓位读取 RuntimeError 后 ACTIVE 计划仍变 CLOSED，停止后续活跃监控。
- 建议修复及验收：读取或解析失败必须返回错误，禁止改变状态；只有可验证的 Decimal 零余量可关闭，增加忙队列/坏数据测试。

### CR07 [P1] 取消排队写操作会永久堵塞 repository 队列

- 对应任务/合同：F01/H08。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/repository.py:798)。
- 问题与影响：等待 _cond 的请求取消时没有移除 _pending 中的 entry。离线复现 A 执行、B 排队后取消、C 入队：A 完成后 busy=False，但 C 超时，队列保留取消项。后续读写与关闭 drain 可持续挂起。
- 建议修复及验收：在等待取消及失败时清理队列项并通知；测试中间/队首取消和后续请求可继续。

### CR08 [P1] 生产基础评分没有使用新输入归一化与冻结策略

- 对应任务/合同：F05/F06，AC02/AC06/AC08。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/service.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/service.py:2361)。
- 问题与影响：生产路径仍调用旧 _build_inputs，不调用 inputs.build_feature_inputs。current_price 在2826取合约原价，ATH在2857取基础资产价；1000倍率案例原价0.01/ATH0.00002得到 drawdown499而正确值为-0.5。funding_rates_30d 恒为None，DQ/risk调用也未传冻结 policy。
- 建议修复及验收：将生产路径连接统一输入与策略合同，按1000单位、30D分布及非默认策略做服务级验收。

### CR09 [P1] Entry 通过改时间戳伪装与逐币 cutoff 一致

- 对应任务/合同：F06/F07，PIT合同。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/service.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/service.py:2220)。
- 问题与影响：Entry在2191按批次 as_of_ms计算；基础评分逐币冻结 decision_cutoff。持久化时仅 dataclasses.replace 修改 Entry snapshot_id/as_of_ms/created_at_ms，没有按逐币 cutoff 重算来源。跨日或长批次可出现快照声明时间与计算窗口不一致。
- 建议修复及验收：按逐币 cutoff 构建 Entry，或保持真实独立时点及显式关联，禁止只改标签；跨午夜测试同时检查输入窗口和来源时间。

### CR10 [P1] FCS、venue 刷新和 Funding settlement 后台任务未实际执行工作

- 对应任务/合同：H02/H07/H08。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/service.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/service.py:1587)。
- 问题与影响：opportunity job 只读已有结果；venue job记录 refreshed=0；settlement记录 checked=0。生产无 save_funding_capture_snapshot、save_spot_venue_snapshot 或 upsert_hedge_alert 调用点，evaluate_alerts 也未接入。启用后不会自动产生机会/报价/负Funding提醒。
- 建议修复及验收：接入采集、评分、持久化和提醒评估，并测试默认 scheduler 运行后由空库产生真实结果及提醒。

### CR11 [P1] Hedge 历史 Evidence 无市场 provider，结果不可计算

- 对应任务/合同：H10/H08。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_grader.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_grader.py:1623)。
- 问题与影响：JobContext 没有 market_provider；runtime直接注册run_due，传入context不含历史provider。read_price_bars等只有协议无生产实现，缺bars进入NO_ENTRY_BAR；同版本终态结果还会阻止后续重试。
- 建议修复及验收：实现并接入历史provider，区分暂不可用和可重试终态，并提供修复后重算机制。

### CR12 [P1] native-only 账本事件未按1000倍率转换数量

- 对应任务/合同：H01/H06。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/ledger.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/hedge/ledger.py:350)。
- 问题与影响：effective_qty_str直接返回native_qty，导致后面的倍率分支不能执行；repository也有相同fallback。离线复现native_qty=2、multiplier=1000得到remaining_qty=2而应2000，影响对冲率、余量和关闭判断。
- 建议修复及验收：统一Decimal canonical quantity转换；验证native-only、显式canonical、1000倍率与dust事件。

### CR13 [P1] 双腿 Evidence 用单一期货价格路径计算回撤

- 对应任务/合同：H10。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_grader.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_grader.py:1239)。
- 问题与影响：使用同一个期货px同时计算spot_qty*(px-spot_entry)+futures_qty*(futures_entry-px)。等量且同入场价时任意路径都抵消成0；复现100→150→50输出最大回撤0。现货真实路径不同，不能据此报告双腿风险为0。
- 建议修复及验收：读取时间对齐的双腿价格路径；缺现货路径返回不可计算及原因，禁止以期货路径代替现货。

### CR14 [P2] Funding 回填同一发送存在双重预算扣减

- 对应任务/合同：F03/F06。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/service.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/service.py:1363)。
- 问题与影响：业务层先reserve，再在1405将同一budget置入HTTP上下文；发送层另reserve/mark_sent，1422业务层再次mark_sent。一页成功请求消耗两次计数，多页多消耗一次，最后一个额度可能在真正发送前被阻断。
- 建议修复及验收：预算只在最终发送层记账；用一页、多页、429重试真实HTTP调用路径断言。

### CR15 [P2] 逐轮评分重新请求90D Funding，未消费增量回填库

- 对应任务/合同：F03/F06。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/service.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/service.py:2483)。
- 问题与影响：每次_score_symbol从as_of-90D直接调用history_fn，未读已经持久化的Funding历史。即使回填库完整，80币窗口以外仍deferred/null，重复消耗预算并降低覆盖。
- 建议修复及验收：先读持久化事件/缓存，按缺口补齐；验证完整500币缓存下无需500次历史请求且评分不因回填预算缺失。

### CR16 [P2] 保存 Evidence 失败后去掉 quote 引用重试

- 对应任务/合同：H01/H10。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_grader.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/evidence/hedge_grader.py:1566)。
- 问题与影响：save_hedge_outcome(record, refs)异常后以[]再次保存相同结果，可保留COMPLETE但失去quote pin。保留策略可能删除原始证据，破坏审计。
- 建议修复及验收：结果和引用保持原子写入；失败显式报告且重试保留相同refs。

### CR17 [P2] RequestBudget 未实现按host和权重的限流

- 对应任务/合同：F01/F03。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/shortlab/request_budget.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/shortlab/request_budget.py:422)。
- 问题与影响：容量检查只比较发送次数，weight_used只是统计；host只保存在permit而没有独立host预算。不能落实不同上游的权重窗口，多个host相互占用发送限额。此项为静态合同差异，未进行在线限流实验。
- 建议修复及验收：区分host窗口并按endpoint权重预留/扣减；测试高权重、不同host及429退避。

### CR18 [P2] Monitor 丢弃 API 返回的 positions/alerts

- 对应任务/合同：H09。
- 代码定位：[desktop/ui/src/app/shortlab/hedge-monitor.jsx](/Users/ken/workspace/short-lab/desktop/ui/src/app/shortlab/hedge-monitor.jsx:41)。
- 问题与影响：get_plan在service.py:4186返回顶层positions/monitor/alerts，UI只保留p.plan || p并从legs字段绘制。真实已登记仓位及提醒无法按合同显示。
- 建议修复及验收：保留完整response或显式映射字段，使用真实API形状fixture验证展示。

### CR19 [P2] 状态变更接口容忍无效JSON并继续执行

- 对应任务/合同：H08。
- 代码定位：[desktop/backend/src/diveintocrypto_desktop/api/shortlab.py](/Users/ken/workspace/short-lab/desktop/backend/src/diveintocrypto_desktop/api/shortlab.py:1720)。
- 问题与影响：activate和close在JSON解析异常、null或非对象时替换为{}，服务允许缺失expectedVersion，从而无效请求仍可改变状态。
- 建议修复及验收：无效JSON/非对象返回422，按合同验证状态变更版本；测试malformed/null/array请求。

### CR20 [P1] 打包验收实际运行源码，发布流程未设置成品验收门禁

- 对应任务/合同：F09/H11，AC22。
- 代码定位：[scripts/smoke_shortlab_packaged.py](/Users/ken/workspace/short-lab/scripts/smoke_shortlab_packaged.py:390)。
- 问题与影响：smoke设置PYTHONPATH=源码并启动sys.executable -m uvicorn；其他阶段也直接导入源码和注入fixture。未运行冻结exe，不能验证资源/只读安装目录/成品两次启动。release.yml:155–176直接打包压缩，未执行此类成品门禁，222仅上传已有verification。
- 建议修复及验收：在目标平台运行真正dist成品，核验001–005资源、可写路径、46408、两次启动；发布前强制新生成的manifest与退出码。

## 验收建议

先修复虚构Monitor数据、provider接线、UI请求合同、仓位关闭与队列取消问题，再连接基础评分/PIT/FCS/历史Evidence默认链路。之后补充不注入业务结果的端到端验收：默认runtime→HTTP→UI请求、scheduler→行情→snapshot→alert、人工事件→Decimal账本→monitor→close、历史双腿数据→outcome→quote pin，以及真实冻结成品两次启动。

修复验收应以本次实际失败条件为测试输入，并保留source commit、命令、退出码和产物校验值。当前完整测试结果可作为回归基线，不足以替代上述功能验收。
