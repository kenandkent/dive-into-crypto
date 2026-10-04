# ShortLab 代码修复与验证说明

日期：2026-10-04。

## 范围与结论

针对《ShortLab 代码实现审查报告》的 CR01–CR20 完成代码修复，并处理集成复核发现的相对对冲参数、模拟数量与报价规模、冻结身份、响应 envelope、定时任务状态及数据库阻塞时的内存降级问题。

修改保留在当前工作区，没有提交或推送 Git。实现基线为 HEAD 与工作区共同构成，不能只用 HEAD 判断修复状态。

## 验证结果

| 验证 | 实际结果 |
| --- | --- |
| 后端完整离线测试 | 1396 passed，6 deselected，1 warning |
| 根目录完整测试 | 95 passed，1 warning |
| UI 完整测试 | 52 passed，0 failed |
| UI 构建 | 通过 |
| 真实 macOS PyInstaller ONEDIR 构建 | 通过 |
| 真实 macOS 成品 smoke | PASS：两次启动、只读安装目录、全资源、005 十一张表、用户数据持久化、UI、process health |
| 设计合同校验 | YAML/Markdown、canonical hash、DuckDB 迁移回滚和重复执行、章节/验收 ID 通过 |
| Git diff whitespace 校验 | 通过 |

后端 warning 为现有 Starlette/httpx 弃用提示。默认测试排除了 6 项 live 测试；本次没有用离线结果代替在线 provider 验证。

## 逐项对应

测试文件未注明路径时位于 `desktop/backend/tests/`。

| 问题 | 修复行为 | 验证覆盖 |
| --- | --- | --- |
| CR01 | 真实行情与共享缓存；缺失/数据库延迟明确降级，保留持仓；10 秒内存计算、60 秒持久采样 | test_shortlab_hedge_production_jobs.py、test_shortlab_hedge_worker_integration.py |
| CR02 | runtime 接入 Spot/Alpha/0x、Funding、FX、规则与历史 provider；模拟量按真实 lot step 计算 | test_shortlab_hedge_market.py、test_shortlab_hedge_api.py、test_review_service_regressions.py |
| CR03 | UI Decimal 字段保留字符串，正确展开真实模拟响应 envelope | desktop/ui/test/hedge.test.mjs |
| CR04 | 无 planId 时显示可操作入口 | desktop/ui/test/hedge.test.mjs |
| CR05 | 人工事件按 wrapped DTO、真实枚举、成交时间及计划版本发送 | desktop/ui/test/hedge.test.mjs、test_shortlab_hedge_api.py |
| CR06 | 仓位读取/解析失败禁止关闭；状态提交后刷新监控镜像 | test_review_service_regressions.py、test_shortlab_hedge_api.py |
| CR07 | 取消排队项清理；执行中的 transaction 不提前释放 worker | test_shortlab_storage_regressions.py |
| CR08 | 生产 Observed 输入、冻结 DQ/risk policy、基础价格/ATH 与 OI 美元单位一致 | test_shortlab_production_inputs.py、test_review_service_regressions.py |
| CR09 | 真实响应完成时刻与缓存 provenance；冻结输入重算；跨 UTC 日不拼接旧窗口 | test_shortlab_entry_completion.py、test_shortlab_default_wiring.py、test_review_service_regressions.py |
| CR10 | FCS/venue/settlement/alerts 执行实际采集、计算、持久化；定时状态不依赖逐 tick job row | test_shortlab_hedge_production_jobs.py、test_review_service_regressions.py |
| CR11 | 默认历史 provider、真实 known_at、共享预算与 grading cutoff 接线；等待收盘/暂时传输失败可重试 | test_shortlab_review_evidence.py、test_shortlab_hedge_evidence.py |
| CR12 | Decimal canonical 数量和价格；1000 倍 native-only 账本及 Evidence 单位转换 | test_shortlab_storage_regressions.py、test_shortlab_hedge_decimal_ledger.py、test_shortlab_review_evidence.py |
| CR13 | 无同步 Spot 历史路径不推断双腿回撤为零，输出 null 与来源原因 | test_shortlab_review_evidence.py |
| CR14 | 仅最终发送点扣账；Entry 重试/分页受同一轮实际 attempt cap 约束 | test_shortlab_http_budget.py、test_shortlab_entry_completion.py |
| CR15 | 评分读完整历史存量；只补缺口，缓存失败不刷新原数据 freshness | test_shortlab_runtime.py、test_review_service_regressions.py |
| CR16 | outcome 与引用原子保存，失败禁止去引用重试 | test_shortlab_review_evidence.py |
| CR17 | 独立 host/请求权重窗口；实际 URL 决定 host；scanner 与 Entry 使用 runtime 预算 | test_shortlab_storage_regressions.py、test_shortlab_http_budget.py、test_review_service_regressions.py |
| CR18 | 保留 API 顶层 positions/alerts；Monitor 优先返回内存最新快照 | desktop/ui/test/hedge.test.mjs、test_review_service_regressions.py |
| CR19 | 非法 JSON/null/array 返回 422，不执行状态变更 | test_review_service_regressions.py |
| CR20 | 真正冻结 binary 双启动、资源/005/持久化/只读安装验收；Windows 发布 zip 前强制门禁 | test_shortlab_review_packaged.py、scripts/smoke_shortlab_packaged.py；本机 native smoke PASS |

## 版本与数据兼容

- Hedge Evidence 使用 `hedge_evidence_v2`，新结果与旧版本分开存储，避免复用旧错误结果；原历史记录保持只读。
- 计划从模拟快照冻结身份、倍率与来源；之后的目录变化不改变该计划的单位。模拟引用的原始报价与 simulation 一并建立保留关联。
- Decimal 原始事件为余额计算依据，DOUBLE 展示列不参与精确余量校验。
- 缺行情、历史 FX、同步双腿路径或费用时，返回 null/明确不可用或等待状态；不以零、固定价格或虚构深度补足数据。

## 验证边界

Windows 真成品检查已放在发布 workflow 的压缩/发布之前，失败会阻止后续发布。本机执行的是 macOS 原生成品，不能视为 Windows ACL、运行库和可执行文件的实际验收结果。

在线 Spot/Alpha/0x、地域限制及带 API key 的受控 PoC 未在本次执行。缺配置保持明确 UNCONFIGURED/UNAVAILABLE；链上 indicative 数据不被提升为可确认退出能力。本项目仍只提供数据、风险提醒和人工操作指导。

## 可核对的产物

- [验证清单与产物 SHA256](../desktop/backend/runtime/verification/code-repair/manifest.json)
- [工作区源文件 SHA256 清单](../desktop/backend/runtime/verification/code-repair/source-files.json)
- [后端测试日志](../desktop/backend/runtime/review_tmp/full-backend-final.log)
- [根目录测试日志](../desktop/backend/runtime/review_tmp/root-final.log)
- [UI 测试日志](../desktop/ui/test/.tmp/ui-final.log)
- [最终真实成品 smoke](../desktop/backend/runtime/review_tmp/native-smoke-verified/packaged-smoke.json)

以上运行日志和 manifest 位于项目内的运行目录。交付时应随代码一起导出这些产物；报告中的测试数量和 SHA256 均可独立核对。

本次源树 SHA256：`cecbb900af70e39c8ea8a9aa980375e7581beaecd64c7511f77f2407ea2f9d8c`。
