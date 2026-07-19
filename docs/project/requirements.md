<!-- SCOPE: Bridle 产品能力与已确认工程门禁的功能需求 -->
<!-- DOC_KIND: explanation -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 规划功能、测试合同、CI 用例或验收范围时 -->
<!-- SKIP_WHEN: 只需要技术实现细节或运行命令时 -->
<!-- PRIMARY_SOURCES: .ai-dev/project-docs/context-window.json, .ai-dev/project-docs/requirements.json, backend/src/bridle/agent, backend/src/bridle/features/sessions -->

# 项目需求

## Quick Navigation

| 领域 | 需求范围 |
|---|---|
| 工作区运行时 | [FR-BRD-001 到 FR-BRD-004](#functional-requirements) |
| 项目地图 | [FR-BRD-010 到 FR-BRD-015](#functional-requirements) |
| 会话与 Agent Runtime | [FR-BRD-020 到 FR-BRD-029](#functional-requirements) |
| 前端体验 | [FR-BRD-030 到 FR-BRD-034](#functional-requirements) |
| 错误、观测与安全 | [FR-BRD-040 到 FR-BRD-046](#functional-requirements) |
| 上下文窗口、工具结果与模型终止 | [FR-BRD-050 到 FR-BRD-057](#functional-requirements) |
| 地图与容器门禁 | [FR-CI-001 到 FR-CI-006](#functional-requirements) |
| 实现证据 | [implementation_status.md](implementation_status.md) |

## Agent Entry

本文是 Bridle 功能需求的 canonical 来源。新增功能、测试合同和 CI CASE ID 必须引用稳定的 FR ID；架构约束写入 [architecture.md](architecture.md)，实现状态与证据写入 [implementation_status.md](implementation_status.md)。本文件只描述系统必须提供什么，不承载性能目标、部署细节或测试通过结论。

## Functional Requirements

| ID | MoSCoW | 功能需求 | 来源边界 | 验收行为 |
|---|---|---|---|---|
| FR-BRD-001 | MUST | CLI 必须以 workspace 为锚点启动 API 服务。 | `backend/src/bridle/cli.py` | `bridle serve --workspace <path>` 设置当前 workspace，并为该 workspace 准备本地数据存储。 |
| FR-BRD-002 | MUST | CLI 默认只能把本地服务绑定到 loopback 地址。 | `backend/src/bridle/cli.py` | 默认 host 为 `127.0.0.1`；非 loopback 绑定由 CLI 边界拒绝。 |
| FR-BRD-003 | MUST | 启动流程必须支持为 workspace 初始化 Git 仓库，并允许显式禁用。 | `backend/src/bridle/features/workspace` | 默认执行本地初始化；显式关闭时不改变目标 workspace 的 Git 状态。 |
| FR-BRD-004 | MUST | workspace 文件与概览接口只能读取当前 workspace 的信息。 | `backend/src/bridle/features/workspace` | `/api/v1/workspace/files` 与 `/api/v1/workspace/overview` 均以当前 workspace 为根。 |
| FR-BRD-010 | MUST | 系统必须为打开的项目路径维护稳定项目记录。 | `backend/src/bridle/features/projects` | 重复打开同一路径时复用项目身份，并更新最近打开信息。 |
| FR-BRD-011 | MUST | 项目地图必须提供概览、子节点、节点、搜索、子图、变化和路径切片查询。 | `backend/src/bridle/features/project_map` | 每类查询均通过 `/api/v1/projects/{project_id}/map` 边界返回项目内数据。 |
| FR-BRD-012 | MUST | 项目地图必须表达代码实体、代码关系和语义标注。 | `backend/src/bridle/features/project_map` | 客户端可以分别读取实体、关系与语义标注。 |
| FR-BRD-013 | SHOULD | 项目地图应识别边界、盲点、模块候选、接口候选、mock 与仲裁结果。 | `backend/src/bridle/features/project_map` | 候选信息可查询；规范允许的候选状态可通过对应写接口更新。 |
| FR-BRD-014 | SHOULD | 用户应能手动刷新当前项目的语义地图。 | `backend/src/bridle/features/project_map` | 调用 `semantic-map/refresh` 后生成当前项目的新语义视图。 |
| FR-BRD-015 | SHOULD | 用户应能把执行态快照刷新到项目地图。 | `backend/src/bridle/features/project_map` | 调用 `execution-refresh` 后，执行阶段信息进入项目地图视图。 |
| FR-BRD-020 | MUST | 系统必须持久化项目会话和消息。 | `backend/src/bridle/features/sessions` | 会话标题、角色、状态以及消息内容、工具调用和工具结果与项目关联保存。 |
| FR-BRD-021 | MUST | 会话必须支持角色变更。 | `backend/src/bridle/features/sessions` | `/sessions/{session_id}/role` 写入当前会话角色。 |
| FR-BRD-022 | MUST | 用户消息和 Agent 回复必须通过 API 进入会话历史。 | `backend/src/bridle/features/sessions` | `/messages` 接收消息，`/converse` 产生对话输出，历史接口可再次读取。 |
| FR-BRD-023 | SHOULD | 会话 API 应暴露当前运行时能力。 | `backend/src/bridle/features/sessions` | `/sessions/{session_id}/capabilities` 返回当前会话可用能力。 |
| FR-BRD-024 | MUST | Agent provider 必须支持不依赖外部模型的本地替代实现。 | `backend/src/bridle/agent/providers` | 本地默认路径可使用 fake 或 stub provider 完成受控执行。 |
| FR-BRD-025 | MUST | 父 Agent、子 Agent 与项目 Map Agent 必须复用统一 Runtime 实现，并把会话与 Runtime 事实持久化在既有应用数据库。 | `.ai-dev/evidence/requirements-agent-runtime-mail-map-20260713.json` | 每个会话最多一个活动父 Runtime、每个项目最多一个活动 Map Runtime；子 Runtime 可并发；启动时遗留活动态改为 `INTERRUPTED`，父/子 Runtime 不自动恢复；关闭会话停止其父/子 Runtime 但保留会话、消息、记忆和运行历史。 |
| FR-BRD-026 | MUST | Runtime 间通信必须使用每项目 `.bridle/mail.db` 的持久化 Mailbox，并提供至少一次交付。 | `.ai-dev/evidence/requirements-agent-runtime-mail-map-20260713.json` | Mail 只保存 envelope、地址、单调序号、lease、ACK/NACK 与投递状态；claim、续租、ACK/NACK 均受 lease token 栅栏保护；重启或 lease 过期后消息可重领；队列满或处理失败时保留消息并进行有界频率的指数重试，不丢弃、不伪造 ACK、不转入 dead-letter。 |
| FR-BRD-027 | MUST | `CodeChanged` 只能由 Bridle 正式单文件补丁提交边界生成，并通过独立 `.bridle/change_outbox.db` 可靠转发。 | `.ai-dev/evidence/requirements-agent-runtime-mail-map-20260713.json` | 写文件前先持久化预留 outbox 容量；容量不足时拒绝或重试且不写文件；单文件使用临时文件、flush、fsync、replace 原子提交，成功文件各自产生事件；多文件不承诺全局原子性；IDE、Git、手工修改和 `.bridle/**` 变化不产生事件。 |
| FR-BRD-028 | MUST | Map Runtime 必须按 Mail 序号批量消费 `CodeChanged`，并以地图事务保证副作用幂等。 | `.ai-dev/evidence/requirements-agent-runtime-mail-map-20260713.json` | 同一批消息合并路径后，在 `.bridle/plan.db` 的一个事务中更新地图并记录所有 `message_id`；提交成功后才 ACK，重复投递不得重复增加 `change_seq`；失败时保留消息重试并把 Map 标记为 `degraded`；同代 Runtime 连续两次原子空检查后销毁，不使用 sleep/debounce。 |
| FR-BRD-029 | MUST | 每代 Runtime 必须只看见创建时授权的 Tool/Skill 能力集合。 | `.ai-dev/evidence/requirements-agent-runtime-mail-map-20260713.json` | Host 创建每代 Runtime 时构造不可变能力注册表；未授权能力不进入上下文、manifest、prompt 或注册表，查询未知或未授权能力统一返回 `unknown capability`；调用时只查本代注册表，不逐次查询数据库/RBAC；撤权或策略变化时 Host 取消并销毁该代，父撤权级联子代，下一代获得新视图。 |
| FR-BRD-030 | MUST | 前端必须通过 Vite 开发代理访问后端 API。 | `frontend/vite.config.ts` | `/api` 请求被转发到本地 Bridle 服务。 |
| FR-BRD-031 | MUST | 前端必须组合项目地图、workspace 切换、会话输入和右侧检查器。 | `frontend/src/components`, `frontend/src/layout` | 用户可在同一项目界面浏览地图、切换 workspace、输入消息并查看检查信息。 |
| FR-BRD-032 | SHOULD | 前端地图同步应支持分页、水位推进、重试和取消。 | `frontend/src/hooks` | 同步 hook 能继续分页，在失败后按合同重试，并在请求失效时终止旧请求。 |
| FR-BRD-033 | SHOULD | 前端组件应复用自定义 `brd-*` 设计系统。 | `frontend/src/components/ds`, `frontend/src/styles/tokens` | Button、Card、Tabs、Switch、Toast、Tooltip 等组件共享 token 与组件样式。 |
| FR-BRD-034 | SHOULD | 前端应支持本地 workspace 路径展示和目录选择降级。 | `frontend/src/lib` | 目录选择取消或能力不可用时保持可恢复状态，不制造无效 workspace。 |
| FR-BRD-040 | MUST | 可观测中间件必须只为变更型 HTTP 方法创建根 trace。 | `backend/src/bridle/app.py`, `backend/src/bridle/observability` | GET、HEAD、OPTIONS、文档和健康检查请求不创建 HTTP 根 trace。 |
| FR-BRD-041 | MUST | 业务错误必须返回结构化 API 错误。 | `backend/src/bridle` | 响应保留状态码、错误代码、消息与详情字段。 |
| FR-BRD-042 | MUST | 请求校验失败必须返回 `validation_error`。 | `backend/src/bridle` | 422 响应包含字段位置、消息和类型。 |
| FR-BRD-043 | MUST | 容器安全门禁必须把 trusted harness、candidate 源码、镜像身份与 evidence 分开验证。 | `.github/workflows/container-docker-linux.yml`, `scripts/ci` | 任一证据缺失、不匹配、重复或被篡改时门禁失败。 |
| FR-BRD-044 | MUST | 中文与 Unicode CLI 输出必须基于真实编码内容检查。 | `backend/tests/cli`, `AGENTS.md` | 验收不以终端代码页的表面显示代替 UTF-8 内容检查。 |
| FR-BRD-045 | MUST | 数据库初始化必须如实使用当前 metadata creation 行为。 | `backend/src/bridle/database.py`, `backend/src/bridle/cli.py` | 启动创建当前 schema，但不把该行为描述成完整迁移工作流。 |
| FR-BRD-046 | MUST | 日志和 observability 不得改变业务返回值或控制面结果。 | `backend/src/bridle/observability` | 日志、sink 或外部观测适配失败时，业务合同仍由原始执行结果决定。 |
| FR-BRD-050 | MUST | 顶层 `short_term_memory` 必须是模型输入的唯一会话记忆入口，当前用户消息在一次模型请求中只出现一次。 | `.ai-dev/project-docs/context-window.json` | Context 模板不再从 `accessible_context` 或其他旧字段重复渲染会话记忆；同一请求中的当前用户消息无重复副本。 |
| FR-BRD-051 | MUST | 父 Runtime 必须为每个 session 维护动态短期记忆窗口，并用摘要、检查点锚点和锚点后增量支持持久化与冷恢复。 | `.ai-dev/project-docs/context-window.json` | 热态后续轮次只读取进程内窗口；首次加载或进程重启时从持久化检查点与锚点后增量恢复，不把全会话历史重新送入压缩流程，也不重复或遗漏消息。 |
| FR-BRD-052 | MUST | 窗口超过水位时，记忆优化器只能处理已有摘要和本次被淘汰的对话消息。 | `.ai-dev/project-docs/context-window.json` | 优化请求不携带完整历史且不提供工具；优化失败、超时、空输出或非法输出时使用确定性回退并记录结构化日志。 |
| FR-BRD-053 | MUST | 工具结果必须完整保留到紧接着的一次模型请求，成功消费后再由代码替换为确定性收据。 | `.ai-dev/project-docs/context-window.json` | 未消费结果完整可见；已消费历史只保留实际存在的白名单诊断字段，相同输入生成字节稳定的收据，失败 `error_summary` 最多 240 个字符，未知大字段不进入收据。 |
| FR-BRD-054 | MUST | 模型必须以可校验的 `completed` 或 `blocked` 结构决定正常退出。 | `.ai-dev/project-docs/context-window.json` | `tool_calls` 继续既有执行链；非法或空终态进入模型修复路径；`blocked` 必须携带原因；只有合法终态可以正常返回。 |
| FR-BRD-055 | MUST | Agent 正常完成不得由完整 prompt token 预算、固定工具轮数或固定工具调用数决定。 | `.ai-dev/project-docs/context-window.json` | provider 整轮只保留绝对墙钟看门狗，单次 HTTP 请求保留独立 timeout；墙钟超时、请求超时、取消、关闭和权限预算耗尽均属于异常终止，不能伪装为 `completed`。 |
| FR-BRD-056 | MUST | 模型业务上下文与本地观测必须分层处理。 | `.ai-dev/project-docs/context-window.json` | 模型后续轮次使用窗口与工具收据控制上下文；配置的本地 Langfuse 继续接收完整 `messages`、`tools`、响应和首次完整工具结果，观测失败不改变业务结果。 |
| FR-BRD-057 | MUST | 上下文改造完成后必须移除旧的重复记忆入口，不保留新旧双实现。 | `.ai-dev/project-docs/context-window.json` | `accessible_context` 不再生产 memory；确认无生产来源的 `long_term_memory` 与 `rag` 空壳字段被移除；新增运行态、日志与流水线临时文件保持 Git 忽略且不新增网络上传路径。 |
| FR-CI-001 | MUST | 地图门禁必须执行后端 project-map 测试和已确认的前端地图同步测试。 | `.ai-dev/spec/requirements.json` | 门禁不把无关聊天或 workspace 测试混入地图用例集合。 |
| FR-CI-002 | MUST | 地图门禁必须检查每个已评审地图用例均被收集并通过。 | `.ai-dev/spec/requirements.json` | 任一映射用例失败或未收集时 job 失败；全部通过时才成功。 |
| FR-CI-003 | MUST | 容器门禁必须先执行平台无关的容器单元与合同测试。 | `.ai-dev/spec/requirements.json` | `backend/tests/agent/container` 中映射到门禁的评审用例全部被收集并通过。 |
| FR-CI-004 | MUST | 容器门禁必须保留 Linux/Docker trusted-candidate 隔离与 evidence 验证链。 | `.ai-dev/spec/requirements.json` | 快速测试和真实 Docker 证据链均成功后，容器 job 才成功。 |
| FR-CI-005 | MUST | CI 目录必须为每个用例记录需求、测试符号、命令、预期结果和 RED/GREEN 证据。 | `.ai-dev/spec/requirements.json` | 每个 CASE ID 能回溯到已评审需求与测试合同。 |
| FR-CI-006 | MUST | 本地验证与 CI job 必须输出阶段、命令、耗时、退出状态和脱敏失败诊断。 | `.ai-dev/spec/requirements.json` | 成功与失败运行都保留有界、可审计且不泄漏凭据或完整对话的证据。 |

## Requirement Boundaries

| 边界 | 说明 |
|---|---|
| 功能与质量分离 | 本文件只定义可观察功能；质量目标和风险在 [architecture.md](architecture.md) 中维护。 |
| 远程写入 | 创建 Issue、触发 GitHub Actions、提交、推送、PR 或发布不属于已授权功能范围。 |
| CI Author | CI Author 的允许路径由 `.ai-dev/spec/requirements.json` 控制，不得修改产品代码或业务测试。 |
| 状态证据 | “存在源码”只足以标记 Implemented；只有实际运行记录才能标记 Verified。 |
| 上下文与观测分层 | prompt 内的动态窗口和工具收据负责控制模型上下文；本地 Langfuse 的完整观测不回灌到后续 prompt，也不受业务窗口压缩规则约束。 |
| 正常与异常退出 | `completed`、`blocked` 是模型正常终态；墙钟超时、请求超时、取消、关闭和权限拒绝是异常看门狗结果，二者不得混用。 |
| 本阶段排除项 | 不新增 Runtime/Mail UI 或远程 API，不引入远程 broker，不监听外部文件变化，不自动恢复父/子 Runtime，也不承诺多文件全局事务。 |

## Maintenance

**Last Updated:** 2026-07-19

**Update Triggers:**

- API、CLI、地图查询、会话行为、Runtime/Mail/Outbox、上下文窗口、工具结果生命周期、模型终止、能力隔离、前端同步或容器边界发生变化。
- 需求基线新增、合并或废弃稳定 ID。
- 已评审测试合同改变某个 CI CASE ID 的覆盖范围。

**Verification:**

- 每个表格行使用唯一的 `FR-XXX-NNN` ID 和大写 MoSCoW 标签。
- 验收行为能够由 API、CLI、UI 或门禁结果直接判断。
- 实现状态只在 [implementation_status.md](implementation_status.md) 更新，本文件不声明测试已通过。
