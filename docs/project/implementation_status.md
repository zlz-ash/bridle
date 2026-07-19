<!-- SCOPE: Bridle 需求、架构决策与当前代码/测试证据的追踪状态 -->
<!-- DOC_KIND: record -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 判断某项能力是计划中、已有实现还是已被真实验证时 -->
<!-- SKIP_WHEN: 只需要需求定义、技术选型或 API 字段时 -->
<!-- PRIMARY_SOURCES: .ai-dev/project-docs/context-window.json, .ai-dev/project-docs/requirements.json, .ai-dev/history/run-stage, .ai-dev-runtime/batches/BATCH-CONTEXT-WINDOW-V1, docs/project/requirements.md, docs/reference/adrs -->

# 实现状态追踪

## Quick Navigation

| 目标 | 入口 |
|---|---|
| 状态口径 | [Status Definitions](#status-definitions) |
| 批次计划与归档完整性 | [Batch Plan Registry](#batch-plan-registry) |
| 需求与 ADR 证据 | [Traceability Matrix](#traceability-matrix) |
| 证据升级规则 | [Evidence Rules](#evidence-rules) |
| 可执行验证 | [Verification Commands](#verification-commands) |

## Agent Entry

本文只回答“当前能证明到什么程度”。`Implemented` 表示 Context Store 或当前源码边界记录了实现入口；`Verified` 必须附带本轮或可复查运行记录。BATCH-CONTEXT-WINDOW-V1 已通过 20 项目标测试、50 项相关回归、Ruff 及独立 Spec/Test、Quality、Test Contract 三类 clean 复审，当前为 `Verified`。BATCH-TOOLS-005-010 保留了确认合同、审批、impact-gate 和 2026-07-17 全后端 `local-verification` 通过记录，但原批次计划和最终审查汇总缺失，因此登记为 `In Progress`，等待证据归并后再判断能否提升。BATCH-AR-01～07 的范围与历史验证摘要仍保留在下方矩阵，但原计划链接已经失效，不能作为新的工作项级计划来源。

## Status Definitions

| 状态 | 定义 | 最低证据 |
|---|---|---|
| Planned | 已在需求、设计或 ADR 中定义，尚无已确认实现入口。 | 来源文档链接 |
| In Progress | 部分产物已经建立，但完整验收条件尚未满足。 | 来源文档与已完成部分的路径 |
| Implemented | 当前代码或配置中存在可追踪实现。 | 来源文档与真实代码/配置路径 |
| Verified | 实现已由实际测试、CI、人工验收或可复查报告证明。 | 来源、代码路径、测试/验证记录与日期 |
| Deprecated | 当前行为不再维护或已被替代。 | 来源文档与替代/移除说明 |

## Batch Plan Registry

| 批次 | 计划来源与范围 | 当前状态 | 归档完整性 |
|---|---|---|---|
| BATCH-AR-01～07 | [Agent Runtime 分批交付](agent_runtime.md#分批交付)；覆盖 Runtime/Mail/Outbox/Map/能力隔离与生命周期 | Verified（历史记录） | 本文保留范围、代码入口、历史测试数字和日期；原 `.ai-dev/batches/BATCH-AR-*` 计划目录当前不存在，不能恢复工作项级原计划。 |
| BATCH-TOOLS-005-010 | [需求合同](../../.ai-dev/project-docs/requirements.json)；覆盖状态驱动验证、候选发布、容器命令、证据链、观测、CLI/API/Tool 适配和异步节点执行 | In Progress | [审批目录](../../.ai-dev/approvals/full-scope/BATCH-TOOLS-005-010/) 与哈希化 run-stage/impact-gate 存在；最新全后端本地验证通过，但原批次计划和最终审查汇总缺失。 |
| BATCH-CONTEXT-WINDOW-V1 | [上下文合同](../../.ai-dev/project-docs/context-window.json)；覆盖动态窗口、记忆优化、工具收据、模型终态、异常看门狗和旧入口清理 | Verified | 可执行计划已固化在本节；目标、相关、Ruff 与三类独立 clean 复审证据均可复查。外部 CI、提交、推送与发布不在本批次已执行范围。 |

### BATCH-TOOLS-005-010 现存计划基线

本批次必须保留以下边界：任意模型命令只在候选容器执行且没有宿主机回退；探索命令不能产生 RED/GREEN/发布资格；控制面、CLI、API 与模型工具复用同一应用服务；不新增补丁应用 CLI，不持久化完整源码或大段最终 diff，也不把安全范围扩展成通用鉴权或渗透测试。

| 工作项 | 原因 | 修改范围与完成标准 | 测试与验证 |
|---|---|---|---|
| REQ-TOOL-004 状态驱动验证协调器 | 允许红测和候选提交状态需要自动触发权威测试，人工串联无法保证重启续跑与幂等。 | 原位改造验证协调与持久化状态：`RED_ALLOWED` 只触发一次有效红测，`SUBMITTED` 冻结候选并触发最终测试；重复事件幂等，探索命令不改变门禁。 | 覆盖状态迁移、重启、重复事件和探索隔离；验证 `python -m pytest backend/tests/unit/agent/runtime/test_verification_orchestrator.py -q`。 |
| REQ-TOOL-005 候选提交与原子发布 | 最终测试失败后的修复、再次提交、基线漂移和发布失败需要稳定生命周期。 | 改造候选服务与发布边界：失败后候选可修复并退回开发阶段；再次提交仅在基线未漂移时原子发布，冲突或失败不产生部分提交。 | 覆盖失败打回、再提交、基线冲突和原子失败；验证 `python -m pytest backend/tests/unit/agent/container/test_candidate_submission.py -q`。 |
| REQ-TOOL-006 候选容器命令边界 | 模型需要任意诊断命令，但宿主机执行和工具白名单都会破坏统一隔离语义。 | 所有命令只经候选容器执行；探索输出仅作诊断，只有冻结合同中的稳定命令 ID 可以改变验证资格。 | 覆盖任意命令、无宿主机回退和探索不授予资格；验证 `python -m pytest backend/tests/unit/agent/container/test_container_command_boundary.py -q`。 |
| REQ-TOOL-007 连续证据链 | 测试合同、候选、执行、审查和发布若没有共同身份绑定，缺失或漂移证据无法 fail-closed。 | 证据绑定稳定命令 ID、合同哈希、候选基线、执行身份和结果摘要；缺失、过期、漂移或伪造时关闭门禁。 | 覆盖证据完整、缺失、过期、漂移与伪造；验证 `python -m pytest backend/tests/unit/agent/container/test_verification_evidence.py -q`。 |
| REQ-TOOL-008 失败、恢复与观测 | 多阶段工作流需要统一错误分类、可恢复重试、并发失效和完整结构化日志。 | 每阶段记录稳定 ID、状态、边界、时序、结果、错误码、重试原因和引用；瞬时失败可重试，永久失败、漂移与预算耗尽进入明确终态。 | 覆盖重启恢复、重试分类、并发漂移和结构化观测；验证 `python -m pytest backend/tests/unit/agent/runtime/test_verification_recovery_and_observability.py -q`。 |
| REQ-TOOL-009 CLI/API/Tool 薄适配 | 多入口复制业务逻辑会导致状态和错误码漂移。 | 候选容器 CLI 统一代码观测，控制面 CLI 统一计划、Mail、候选和验证诊断；CLI、API、Tool 返回一致结果且不复制服务逻辑，也不存在补丁子命令。 | 覆盖 CLI 服务一致性与错误码；验证 `python -m pytest backend/tests/unit/test_cli_agent_commands.py backend/tests/integration/agent/test_cli_service_parity.py -q`。 |
| REQ-TOOL-010 异步节点执行 | `select_node` 只选择节点，不能表达完整工作流、持久化等待和唯一终态通知。 | 由 `execute_plan_node` 异步执行节点并立即返回持久化 `waiting`；重启复用同节点执行，中间测试/Review 不结束等待，终态先持久化 `ended` 再经 outbox 唯一投递 Mail，查询无副作用。 | 覆盖等待、重启、复用、内部循环、唯一通知和查询幂等；验证 `python -m pytest backend/tests/unit/agent/runtime/test_node_workflow_wait_signal.py backend/tests/integration/agent/test_async_node_execution.py -q`。 |

### BATCH-CONTEXT-WINDOW-V1 可执行计划与验收

1. 建立 session 动态短期记忆窗口
   - 原因：旧路径每轮从数据库重读全历史并重新压缩，且当前用户消息可能同时从历史与当前输入进入 prompt，处理量随会话增长。
   - 修改：在 `backend/src/bridle/agent/runtime/gateway.py`、`backend/src/bridle/features/sessions/service.py` 和 `backend/src/bridle/models/project_session_memory.py` 建立每 session 热窗口、摘要、检查点锚点和锚点后增量冷恢复；`backend/src/bridle/agent/context` 只渲染顶层 `short_term_memory`。
   - 保留：完整会话消息继续落库供前端展示；既有会话 API、角色、工具上下文、项目上下文和子 Agent 结果语义不变。
   - 完成标准：热态后续轮次不重读全历史，重启恢复不重复不遗漏；当前用户消息每次模型请求只出现一次；同 session 整轮串行且取消后锁可再次获取。
   - 测试：覆盖热态连续对话、窗口越线、检查点后增量、进程冷恢复、重复消息、同 session 并发与取消释放；不得用“数据库有记录”代替模型实际输入断言。
   - 验证：`python -m pytest backend/tests/agent/runtime/test_dynamic_session_memory.py backend/tests/features/sessions/test_session_memory_checkpoint.py backend/tests/agent/context/test_context_template.py -q`。

2. 只优化被窗口淘汰的对话
   - 原因：每轮让 AI 重压缩完整历史会重复消耗上下文，工具结果又不适合交给 AI 改写。
   - 修改：在 `backend/src/bridle/agent/providers/agent_provider.py`、`deepseek_agent_provider.py` 和 `memory/short_term_memory.py` 增加“已有摘要 + 本次淘汰消息”的无工具 optimizer 与确定性 fallback。
   - 保留：未越过水位时不调用 optimizer；工具结果遵循独立生命周期，不进入 AI 摘要；Fake provider 保持无网络可测。
   - 完成标准：optimizer 请求不含完整历史且 `tools=[]`；成功结果推进摘要，失败、超时、空输出或非法输出使用确定性回退；所有分支有结构化日志。
   - 测试：直接捕获 optimizer 请求并断言输入边界、无工具、调用时机和 fallback；不得只 mock 一个预期摘要字符串。
   - 验证：`python -m pytest backend/tests/agent/memory/test_short_term_memory.py backend/tests/agent/providers/test_deepseek_agent_provider.py -q`。

3. 实现工具结果的一次完整消费与确定性收据
   - 原因：工具返回字段本身有用，过早压缩会破坏当前轮推理；但完整历史结果永久回灌会让后续 prompt 持续膨胀。
   - 修改：在 `backend/src/bridle/agent/memory/short_term_memory.py` 实现白名单 `ToolResultReceiptBuilder`，在 `deepseek_agent_provider.py` 中仅于模型成功消费后替换历史 tool message。
   - 保留：紧接着的模型请求与本地 Langfuse generation 都看到完整结果；失败诊断字段保留；工具授权、执行和追踪链路不变。
   - 完成标准：相同输入产生字节一致的 JSON 收据；成功收据不含未知大字段；失败 `error_summary` 最多 240 个字符；未消费结果始终完整。
   - 测试：覆盖成功/失败、多轮稳定性、未知 payload、240 字符边界、首次完整观测和后续收据；不得在模型消费前构造收据。
   - 验证：`python -m pytest backend/tests/agent/tools/test_tool_result_receipts.py backend/tests/agent/providers/test_deepseek_agent_provider.py -q`。

4. 由模型终态决定正常退出，并保留异常看门狗
   - 原因：固定轮数或调用数会把尚未完成的工具循环误判为结束，无工具的任意文本也不能自动代表完成。
   - 修改：在 `backend/src/bridle/agent/runtime/schemas.py`、`providers/deepseek_agent_provider.py`、`runtime/gateway.py` 与 `agent/tools/budget.py` 建立 `completed/blocked` 合同、非法终态修复和覆盖整轮的绝对墙钟 deadline；移除 provider 的 round/call 正常退出配置。
   - 保留：`tool_calls` 继续原权限和执行链；单次 HTTP 请求 timeout 保留；父子 Agent 的授权调用上限仍是权限边界，不表达正常完成。
   - 完成标准：只有合法 `completed` 或带原因 `blocked` 正常返回；非法终态可修复；墙钟超时、请求超时、取消和关闭进入异常路径，主动取消阻塞 await 并释放锁。
   - 测试：覆盖工具调用后完成、非法终态修复、blocked、共享 deadline 不重置、阻塞 await 被取消和异常后 session 可继续；不得把达到固定轮数写成成功断言。
   - 验证：`python -m pytest backend/tests/agent/providers/test_deepseek_agent_provider.py backend/tests/agent/runtime/test_unified_agent_runtime_api.py backend/tests/agent/tools/test_tool_budget.py -q`。

5. 清理旧入口并分离业务上下文与本地观测
   - 原因：新旧记忆入口并存会再次造成重复 prompt；把业务窗口裁剪规则套到本地观测又会丢失排查所需的真实请求。
   - 修改：移除 `accessible_context` memory、无生产来源的 `long_term_memory/rag` 空壳和旧 `ToolResultSummarizer`；保留完整 provider `messages/tools/results` 的本地 Langfuse 记录，并为窗口、检查点、optimizer、收据、终态、看门狗和取消建立结构化日志。
   - 保留：不引入新的日志依赖，不新增完整上下文网络上传路径，不整理无关模板代码；运行态和流水线临时文件保持 Git 忽略。
   - 完成标准：生产代码只有一个短期记忆入口；旧参数、字段、配置和派生逻辑无生产引用；本地观测保真且失败不改变业务结果；目标测试、相关回归和 Ruff 全部通过。
   - 测试：覆盖模板唯一入口、完整 Langfuse 输入、日志事件、旧环境变量失效和 Git ignore；不得只用字符串搜索代替运行路径测试。
   - 验证：`python -m pytest backend/tests/agent/context/test_context_template.py backend/tests/agent/providers/test_agent_provider_factory.py backend/tests/agent/providers/test_deepseek_agent_provider.py backend/tests/agent/runtime/test_dynamic_session_memory.py -q`，并对变更范围运行 Ruff。

## Traceability Matrix

| ID | Source Doc | 范围 | Status | Code / Config Evidence | Test / Validation Evidence | Last Verified | 说明 |
|---|---|---|---|---|---|---|---|
| FR-BRD-001~004 | [requirements.md](requirements.md) | workspace 启动、本地绑定、Git 初始化与工作区读取 | Implemented | `backend/src/bridle/cli.py`, `backend/src/bridle/features/workspace` | `backend/tests/cli`（仅记录路径，未运行） | — | Context Store 确认 CLI、workspace feature 与 loopback 默认边界。 |
| FR-BRD-010~015 | [requirements.md](requirements.md) | 项目记录、增量/语义地图、候选与刷新 | Implemented | `backend/src/bridle/features/projects`, `backend/src/bridle/features/project_map` | `backend/tests/features/project_map`（仅记录路径，未运行） | — | Context Store 列出项目地图查询和写接口。 |
| FR-BRD-020~024 | [requirements.md](requirements.md) | 会话、消息、角色、能力与 provider | Implemented | `backend/src/bridle/features/sessions`, `backend/src/bridle/agent/providers` | 未在本轮运行 | — | Context Store 记录 projects、project_sessions、project_messages schema 与会话端点。 |
| FR-BRD-025~029 | [requirements.md](requirements.md) / 历史确认路径 `.ai-dev/evidence/requirements-agent-runtime-mail-map-20260713.json`（当前归档缺失） | 统一 Runtime、项目本地 Mail/Outbox、正式补丁、Map 幂等消费与每代能力隔离 | Verified | AR-01～07 均已有实现；AR-07 接入 lifespan 恢复、逐项目 `project_runtime_recovery` 降级、永久 shutdown latch、Forwarder/Runtime/finalizer 收口与正式补丁重投整链 | AR-01～07 均为 `BATCH_REVIEW_CLEAN`；AR-07 通过 17 项目标门禁、363 项相关回归、1224 项全量后端测试、59 文件变更范围 Ruff及独立双审 | 2026-07-15 | 现有 API schema/status 保持不变；全仓 Ruff 63 项历史基线未冒充 clean。 |
| BATCH-AR-01 | [分批交付摘要](agent_runtime.md#分批交付) | Runtime 持久化职责、投递登记、项目三库基础 schema 与日志关联字段 | Verified | `backend/src/bridle/models/agent_runtime.py`、`backend/src/bridle/agent/runtime/persistence.py`、`backend/src/bridle/logging`、`backend/src/bridle/features/project_map/store.py` | 15-test JUnit、本地验证及独立 Spec/Test、Quality 双审均为 clean | 2026-07-14 | 历史状态记录为 `BATCH_REVIEW_CLEAN`；原工作项级计划归档缺失，只保留 AR-01 范围摘要。 |
| BATCH-AR-02 | [分批交付摘要](agent_runtime.md#分批交付) | 项目本地持久化 Mailbox、canonical 地址/envelope、顺序、容量、lease fencing、ACK/NACK、无限重试与 loop wake | Verified | `backend/src/bridle/agent/runtime/mailbox.py`、`backend/src/bridle/agent/runtime/persistent_mailbox.py`、`backend/src/bridle/agent/runtime/project_storage.py` | 18-test 目标 JUnit、63-case 既有回归、5-case no-xfail、本地验证及独立 Spec/Test、Quality 双审均为 clean | 2026-07-14 | 历史状态记录为 `BATCH_REVIEW_CLEAN`；原工作项级计划归档缺失。 |
| BATCH-AR-03 | [分批交付摘要](agent_runtime.md#分批交付) | 统一 Host 生命周期、父/子/Map 单例规则、不可变 generation 能力视图与撤权换代 | Verified | `backend/src/bridle/agent/runtime/host.py`、`agent_runtime.py`、`capability_view.py`、Tool/Skill registry | 目标测试、本地验证及独立 Spec/Test、Quality 双审均为 clean | 2026-07-14 | 历史状态记录为 `BATCH_REVIEW_CLEAN`；原工作项级计划归档缺失。 |
| BATCH-AR-04 | [分批交付摘要](agent_runtime.md#分批交付) | 会话输入 relay、父子协调、结果回执、关闭/撤权与历史保留 | Verified | `backend/src/bridle/agent/runtime/input_relay.py`、`parent_child_runtime.py`、`gateway.py`、`backend/src/bridle/features/sessions` | 目标测试、本地验证及独立 Spec/Test、Quality 双审均为 clean | 2026-07-14 | 历史状态记录为 `BATCH_REVIEW_CLEAN`；原工作项级计划归档缺失。 |
| BATCH-AR-05 | [分批交付摘要](agent_runtime.md#分批交付) | 正式单文件原子补丁、项目 Outbox、可靠转发与多文件部分成功语义 | Verified | `backend/src/bridle/agent/runtime/change_outbox.py`、`backend/src/bridle/agent/tools/sandboxed_executor.py`、`gateway.py` | 114 项目标测试、201 项 Runtime 回归、本地验证及独立 Spec/Test、Quality 双审均为 clean | 2026-07-15 | 历史状态记录为 `BATCH_REVIEW_CLEAN`；原工作项级计划归档缺失，Mail 满/busy 与提交崩溃窗口保留可恢复 READY。 |
| BATCH-AR-06 | [分批交付摘要](agent_runtime.md#分批交付) | 按需 Map Runtime、事务消息回执、commit-then-ACK、空队列退休、wake 重试与持久化降级 | Verified | `backend/src/bridle/agent/runtime/project_map_agent.py`、`project_registry.py`、`change_outbox.py`、`backend/src/bridle/features/project_map/store.py` | 70 项扩展目标、144 项相关回归、5 项 no-xfail、Ruff 及独立 Spec/Test、Quality 复审均 clean | 2026-07-15 | 历史状态记录为 `BATCH_REVIEW_CLEAN`；原工作项级计划归档缺失，应用启动恢复与统一关闭属于 AR-07。 |
| BATCH-AR-07 | [分批交付摘要](agent_runtime.md#分批交付) | lifespan 启动恢复、逐项目隔离降级、统一关闭、兼容与真实整链验收 | Verified | `backend/src/bridle/app.py`、`agent/runtime/gateway.py`、`project_registry.py`、`models/project_runtime_recovery.py`、`features/projects/service.py` | 17 项 no-xfail 目标门禁、363 项相关回归、1224 项全量后端测试、59 文件变更范围 Ruff 及独立 Spec/Test、Quality 复审均 clean | 2026-07-15 | 历史状态记录为 `BATCH_REVIEW_CLEAN`；原工作项级计划归档缺失，全仓 Ruff 另有 63 项历史基线，不冒充通过。 |
| FR-BRD-030~034 | [requirements.md](requirements.md) | React UI、Vite 代理、地图同步与设计系统 | Implemented | `frontend/src/api`, `frontend/src/components`, `frontend/src/hooks`, `frontend/src/layout`, `frontend/src/lib` | `frontend/src/hooks/__tests__`（仅记录路径，未运行） | — | Context Store 确认前端域、React Query 与自定义 `brd-*` 组件系统。 |
| FR-BRD-040~046 | [requirements.md](requirements.md) | 结构化错误、观测、容器 evidence、Unicode 与 schema 口径 | Implemented | `backend/src/bridle/app.py`, `backend/src/bridle/observability`, `backend/src/bridle/agent/container`, `.github/workflows/container-docker-linux.yml`, `scripts/ci` | `backend/tests/observability`, `backend/tests/logging`, `backend/tests/agent/container`（均未在本轮运行） | — | 已确认实现入口存在，不等于测试已通过。 |
| FR-BRD-050~057 | [requirements.md](requirements.md) / [上下文合同](../../.ai-dev/project-docs/context-window.json) | 动态短期记忆、检查点增量冷恢复、无工具优化、工具收据、模型终态、整轮看门狗与本地完整观测 | Verified | `backend/src/bridle/agent/context`、`agent/memory/short_term_memory.py`、`agent/providers`、`agent/runtime/gateway.py`、`features/sessions/service.py`、`models/project_session_memory.py` | [20 项目标测试](../../.ai-dev/history/run-stage/b8259cd3007775f56277aeb1c9ada576d4d637703bb0927d0204ee6ad2017430.json)、[50 项相关回归](../../.ai-dev/history/run-stage/4ef915cddc90880421bacfa9fdf05839cae376f1ea28a5f851818980e6287462.json)、[Ruff](../../.ai-dev/history/run-stage/e5cf7b40f193dcbf870f012b1ac4f91acf3ce064110e85b4a923d6dfe0e57daf.json) 与三类 clean 复审 | 2026-07-19 | 正常结束由模型决定；不引入完整 prompt 预算；本地 Langfuse 完整观测与后续 prompt 裁剪分层。 |
| BATCH-CONTEXT-WINDOW-V1 | [可执行计划](#batch-context-window-v1-可执行计划与验收) | 五项上下文改造工作与旧入口清理 | Verified | 同 FR-BRD-050~057，并包含 `.gitignore` 与稳定 pipeline command catalog | [Spec/Test clean](../../.ai-dev-runtime/batches/BATCH-CONTEXT-WINDOW-V1/spec-test-review-v6.json)、[Quality clean](../../.ai-dev-runtime/batches/BATCH-CONTEXT-WINDOW-V1/quality-review-v4.json)、[Test Contract clean](../../.ai-dev-runtime/batches/BATCH-CONTEXT-WINDOW-V1/test-contract-review-v13.json) | 2026-07-19 | 状态为 `BATCH_REVIEW_CLEAN`；外部 CI、提交、推送与发布未执行。 |
| REQ-TOOL-004~010 / BATCH-TOOLS-005-010 | [需求合同](../../.ai-dev/project-docs/requirements.json) / [现存计划基线](#batch-tools-005-010-现存计划基线) | 状态驱动验证、候选发布、容器命令、证据链、失败恢复、薄适配与异步节点执行 | In Progress | 当前源码保留验证协调、候选容器、控制面 CLI/API/Tool 与异步执行入口；原批次工作项计划归档缺失 | [全后端 local-verification 通过](../../.ai-dev/history/run-stage/ddd1402aa5580b7a8b9405e88be0610f3aa030f1bba5caed11da726a1d98084c.json)，审批与 impact-gate 历史仍在 | 2026-07-17 | 已有真实通过记录，但需求合同仍为 pending 且最终审查汇总不可复查；完成证据归并前不提升为 Verified。 |
| REQ-DOC-001 / AC-DOC-001 | 历史基线路径 `.ai-dev/spec/requirements.json`（当前归档缺失） | 重写 docs 并通过集中质量门 | In Progress | `docs/**/*.md` | 尚无集中校验报告 | — | 当前只是 ln-112 核心文档子集，不能代表全部 docs 完成。 |
| REQ-MAP-CI-001 / AC-MAP-CI-001 | 历史基线路径 `.ai-dev/spec/requirements.json`（当前归档缺失） | 后端 project-map 与前端地图同步 CI 门禁 | Planned | Context Store 明确记录门禁尚未存在 | 尚无评审后 CASE 目录与运行证据 | — | 必须先完成测试合同、RED/GREEN 与双评审。 |
| REQ-CONT-CI-001 | 历史基线路径 `.ai-dev/spec/requirements.json`（当前归档缺失） | 平台无关容器快速门禁 | Planned | `backend/tests/agent/container` | 尚无新门禁运行记录 | — | 测试路径存在不代表新门禁已编排。 |
| REQ-CONT-CI-002 / SEC-CONT-CI-001 | 历史基线路径 `.ai-dev/spec/requirements.json`（当前归档缺失） | Linux/Docker trusted evidence 最终门禁 | Implemented | `.github/workflows/container-docker-linux.yml`, `scripts/ci`, `backend/src/bridle/agent/container` | 尚未在本轮执行真实 Docker 链 | — | 当前 workflow 与证据链存在；本轮增强验收仍为 pending。 |
| REQ-CI-TEST-001 / AC-CI-TEST-001 | 历史基线路径 `.ai-dev/spec/requirements.json`（当前归档缺失） | 评审测试合同、真实 RED、最小 GREEN 与稳定 CASE ID | Planned | `.ai-dev/ci/` 目标边界 | 尚无完整批准目录 | — | CI Author 不得用 workflow 修改替代业务测试合同。 |
| NFR-CI-DET-001 / OPS-CI-OBS-001 | 历史基线路径 `.ai-dev/spec/requirements.json`（当前归档缺失） | CI 可重复选择与结构化脱敏证据 | Planned | `.ai-dev/ci/` 目标边界 | 尚无 catalog 指纹与审计产物 | — | 只有全部指纹绑定后才能形成可复查批准证据。 |
| SEC-CI-AUTH-001 / AC-CI-AUTH-001 | 历史基线路径 `.ai-dev/spec/requirements.json`（当前归档缺失） | CI Author 路径限制与批准指纹 | Planned | 允许前缀记录在需求基线 | 尚无完整 contract validation 记录 | — | 产品代码和业务测试不在 CI Author 修改范围。 |
| ADR-001 | [adr-001-fastapi-backend.md](../reference/adrs/adr-001-fastapi-backend.md) | FastAPI + Uvicorn 后端 | Implemented | `backend/src/bridle/app.py`, `backend/src/bridle/cli.py` | 未在本轮运行 | — | 当前入口与 ADR 方向一致。 |
| ADR-002 | [adr-002-local-sqlite-sqlalchemy.md](../reference/adrs/adr-002-local-sqlite-sqlalchemy.md) | workspace-local SQLite + SQLAlchemy async | Implemented | `backend/src/bridle/database.py` | 未在本轮运行 | — | 当前没有活动迁移工作流。 |
| ADR-003 | [adr-003-react-vite-frontend.md](../reference/adrs/adr-003-react-vite-frontend.md) | React + Vite 前端 | Implemented | `frontend/src`, `frontend/vite.config.ts` | 未在本轮运行 | — | 浏览器级视觉与交互验收尚未执行。 |
| ADR-004 | [adr-004-langfuse-v4-observability.md](../reference/adrs/adr-004-langfuse-v4-observability.md) | 结构化日志与可选 Langfuse v4 | Implemented | `backend/src/bridle/observability` | 未在本轮运行 | — | 外部观测不能成为默认启动依赖。 |
| ADR-005 | [adr-005-trusted-docker-gate.md](../reference/adrs/adr-005-trusted-docker-gate.md) | trusted Docker security gate | Implemented | `.github/workflows/container-docker-linux.yml`, `scripts/ci` | 真实 Docker 链未在本轮运行 | — | 仅确认结构存在，不声明 CI 通过。 |

## Evidence Rules

| 规则 | 约束 |
|---|---|
| 来源先行 | 每行必须链接需求、架构章节或 ADR；口头描述不能替代来源。 |
| Implemented 边界 | 需要真实代码、配置或 workflow 路径；测试文件存在不是运行证据。 |
| Verified 更严格 | 必须记录实际命令、退出状态、可复查报告或人工验收，以及执行日期。 |
| 失败也保留 | RED、失败 CI 和 fail-closed 结果属于证据，但不能证明功能已通过。 |
| 证据与观测分层 | 仓库内结构化日志和审计产物不得包含凭据、secret 或无界 payload；配置的本地 Langfuse 可以保留完整 provider 上下文，但不得把该数据写入 Git 归档或回灌到后续 prompt。 |
| 降级规则 | 入口删除、语义变化、测试失效或证据不可复查时，先把状态降级。 |

## Verification Commands

以下命令来自当前需求基线；运行前必须按项目规则预估耗时。命令列出不等于已经执行。

| 范围 | 命令 | 预估 |
|---|---|---|
| 后端地图 | `cd backend; pytest tests/features/project_map` | 2–5 分钟 |
| 前端地图同步 | `cd frontend; npm test -- --run src/hooks/__tests__/mapLayerSync.test.ts src/hooks/__tests__/mapSyncLogger.test.ts src/hooks/__tests__/useProjectMapLayers.test.ts src/hooks/__tests__/useProjectMapLayers.retry.test.tsx src/hooks/__tests__/useProjectMapLayers.sync.test.tsx` | 1–3 分钟 |
| 容器合同 | `cd backend; pytest tests/agent/container` | 2–5 分钟 |
| 上下文目标合同 | `python -m pytest backend/tests/agent/runtime/test_dynamic_session_memory.py backend/tests/features/sessions/test_session_memory_checkpoint.py backend/tests/agent/memory/test_short_term_memory.py backend/tests/agent/tools/test_tool_result_receipts.py backend/tests/agent/providers/test_deepseek_agent_provider.py -q` | 1–3 分钟 |
| 上下文相关回归 | `python -m pytest backend/tests/agent/context backend/tests/agent/memory backend/tests/agent/providers backend/tests/agent/runtime backend/tests/features/sessions/test_session_memory_checkpoint.py -q` | 2–5 分钟 |
| 上下文变更 Ruff | `python -m ruff check backend/src/bridle/agent backend/src/bridle/features/sessions/service.py backend/src/bridle/models/project_session_memory.py backend/tests/agent backend/tests/features/sessions/test_session_memory_checkpoint.py` | 1–2 分钟 |
| 旧工具批次全后端复核 | `python -m pytest backend/tests -q` | 7–12 分钟 |

集中 docs-quality 校验器的精确命令未由 Context Store 固定，因此本文不写猜测命令；实际运行记录应在流水线审计产物中保存。

## Maintenance

**Update Triggers:**

- FR、REQ、AC、SEC、NFR、OPS 或 ADR 的状态发生变化。
- 代码、workflow、测试符号、CASE ID 或证据路径移动。
- pytest、Vitest、Docker gate、docs-quality 或人工验收产生新记录。
- 批次计划、审批、impact-gate、run-stage 或独立审查归档出现、移动或缺失。

**Verification:**

- 状态只能使用 Planned、In Progress、Implemented、Verified、Deprecated。
- 任一 `Verified` 行必须同时具备代码证据、运行证据和日期。
- 与 [requirements.md](requirements.md)、[architecture.md](architecture.md) 和 [ADR 目录](../reference/adrs/) 保持一致。
