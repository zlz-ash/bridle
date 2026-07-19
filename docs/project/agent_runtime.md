<!-- SCOPE: Bridle Agent Runtime、动态上下文窗口、工具结果生命周期、持久化 Mail、CodeChanged Outbox、Map 消费、授权与生命周期的目标契约 -->
<!-- DOC_KIND: design -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 设计或修改父/子/Map Runtime、对话上下文、工具结果、模型终止、Runtime 间通信、正式补丁、工具/Skill 授权或资源回收时 -->
<!-- SKIP_WHEN: 只需要当前已实现的 HTTP 字段或前端视觉规范时 -->
<!-- PRIMARY_SOURCES: .ai-dev/project-docs/context-window.json, docs/project/requirements.md, backend/src/bridle/agent, backend/src/bridle/features/sessions, backend/src/bridle/features/project_map -->
<!-- NO_CODE_EXAMPLES: 本文定义目标契约与边界，具体实现以源码和测试为准。 -->

# Agent Runtime 目标架构

> **状态：** Verified；BATCH-AR-01～07 与 BATCH-CONTEXT-WINDOW-V1 的已登记范围均有验证记录。
> **最后更新：** 2026-07-19

## Quick Navigation

| 目标 | 入口 |
|---|---|
| Runtime 类型、所有权与并发 | [统一 Runtime 与所有权](#统一-runtime-与所有权) |
| 理解会话上下文的四层边界 | [会话上下文窗口](#会话上下文窗口) |
| 理解工具结果与正常退出 | [工具结果生命周期](#工具结果生命周期)、[模型终止与异常看门狗](#模型终止与异常看门狗) |
| 持久化通信与可靠性 | [Mail 与 Outbox](#mail-与-outbox) |
| 理解项目级 Map 执行 | [Map Runtime 运行模型](#map-runtime-运行模型) |
| 检查能力隔离 | [每代能力视图](#每代能力视图) |
| 检查恢复和销毁 | [生命周期与恢复](#生命周期与恢复) |
| 确认持久化位置 | [数据归属](#数据归属) |
| 查看完成标准 | [验收与验证](#验收与验证) |

## Agent Entry

> **实现边界：** 会话消息与 `runtime_input_deliveries` 在既有应用 SQLite 的同一事务生成稳定 `message_id` 并记录目标 Runtime、投递状态、Mail 执行 attempt 与应用回执。relay 以同一 `message_id` 幂等写入项目 Mail；lease、ACK 与 NACK 仍只属于 `.bridle/mail.db`。

本文是 Agent Runtime 架构的权威入口。Runtime/Mail 基线为 [FR-BRD-025~029](requirements.md#functional-requirements)，上下文窗口、工具结果和模型终止基线为 [FR-BRD-050~057](requirements.md#functional-requirements)，后者的确认合同保存在 `.ai-dev/project-docs/context-window.json`。BATCH-CONTEXT-WINDOW-V1 已通过 20 项目标测试、50 项相关回归、Ruff 及独立 Spec/Test、Quality、Test Contract 三类 clean 复审；可复查状态与证据见 [实现状态](implementation_status.md)。

## 设计不变量

- 父 Agent、子 Agent 与 Map Agent 复用同一个 Runtime 实现；差异由所有者、能力注册表和输入处理器决定，不复制生命周期框架。
- Runtime 与会话是同一应用域中的不同事实，统一存入既有应用数据库；会话消息负责历史展示，记忆检查点负责冷恢复，进程内动态窗口负责热态后续轮次，三者都不复制到 Mail。
- Mail 只传递信息，不保存会话、记忆、地图或 Runtime 业务状态。
- 投递语义是至少一次；所有业务副作用必须以稳定 `message_id` 幂等。
- 既有 API schema、状态码和对外行为保持兼容；本阶段不新增 Runtime/Mail UI 或公共远程接口。
- 只使用既有日志设施，不引入新的日志依赖。
- 模型业务上下文和本地 Langfuse 观测是两个边界：前者使用动态窗口与工具收据，后者保留完整请求与响应，但不会回灌到后续 prompt。

## 统一 Runtime 与所有权

| 类型 | 所有者 | 并发约束 | 输入 | 持久化结果 |
|---|---|---|---|---|
| 父 Runtime | 会话 | 每个会话最多一个活动实例；新用户输入进入 Mail 排队 | 用户/Host 消息 | 会话、消息、Runtime 状态与结果摘要进入应用数据库 |
| 子 Runtime | 父 Runtime 与任务 | 同一父 Runtime 下可并发多个 | 父 Runtime 投递的任务消息 | Runtime 状态与结果摘要进入应用数据库，结果通过 Mail 返回 |
| Map Runtime | 项目 | 每个项目最多一个活动实例 | `CodeChanged` 消息 | 地图与消息处理回执进入项目 `.bridle/plan.db` |

稳定身份必须区分 `agent_id`、`generation`、所有者和项目。活动实例由进程内 registry 管理，持久化记录用于审计与恢复判定，不序列化 task、provider 请求、锁或数据库连接。

## Mail 与 Outbox

每个已注册项目独立拥有 `.bridle/mail.db` 与 `.bridle/change_outbox.db`。二者不能与主应用数据库或 `.bridle/plan.db` 混合：Mail 是传输层，Outbox 是正式文件提交到 Mail 的可靠桥接，地图数据库只保存 Map 业务事实。

### Mailbox 合同

- envelope 至少携带 `message_id`、项目、源/目标地址、消息类型、payload、单调 `seq`、创建时间和投递尝试信息。
- claim 在数据库事务内按 `seq` 获取消息并写入新的 `lease_owner`、不可复用 `lease_token` 与 `lease_expires_at`；续租、ACK 和 NACK 必须同时匹配当前 token，否则视为失去所有权。
- ACK 只表示消费方业务事务已经提交。进程崩溃或 lease 过期后，未 ACK 消息重新变为可领取。
- 队列满、Mail 暂不可写或消费失败时消息继续持久化，并以有上限频率的指数退避重试；不存在永久失败、丢弃、伪造 ACK 或 dead-letter 状态。
- 进程内唤醒只是降低延迟的提示，不是真相来源。跨线程生产者通过事件循环安全入口发信号；关闭时注销信号源并取消等待者。消费者被唤醒后始终重新查询数据库。

### 正式补丁与 Outbox 合同

只有 Bridle 正式补丁/插桩的单文件提交边界可以产生 `CodeChanged`。IDE 保存、Git 操作、手工改文件、启动磁盘扫描和 `.bridle/**` 写入都不是生产者。

单文件提交顺序为：

1. 在 outbox 持久化预留容量、`message_id`、规范化路径和预期内容摘要；预留失败时不触碰目标文件。
2. 在目标文件同目录写临时文件，完成 flush 与 fsync 后使用 replace 原子替换。
3. 将预留项标记为 `ready`；若第 2 步失败则释放预留。若进程在 replace 后、标记前崩溃，启动恢复通过目标摘要将预留推进为 `ready`，不得漏发。
4. 转发器把 `ready` 项写入 Mail；Mail 已有相同 `message_id` 时视为成功，随后把 outbox 项标记为 delivered。

多文件补丁逐文件执行上述合同。每个成功文件独立产生消息；失败文件不产生成功事件，也不承诺整组文件回滚或全局原子性。Mail 满时 `ready` 项保留在 outbox 重试。

## Map Runtime 运行模型

Map Runtime 不是随项目打开常驻的 actor。启动时 Host 扫描已注册项目的 Mail；收到待处理 `CodeChanged` 且该项目没有活动 Map Runtime 时，创建一代 Runtime。运行期间新消息由同一代继续处理。

每个批次执行：

1. 按 `seq` claim 一批 `CodeChanged`，合并为规范化路径集合。
2. 在 `.bridle/plan.db` 的单个业务事务中刷新这些路径，并为批内每个 `message_id` 写入已应用回执。
3. 已存在回执的重复消息跳过副作用，因此不得再次增加 `change_seq`。
4. 事务提交后逐条 ACK；若提交后 ACK 前崩溃，重投时由回执证明副作用已完成。

处理失败不 ACK，消息保留重试，并通过既有 readiness/status 将 Map 标记为 `degraded`；普通会话不能因此终止。Runtime 完成一个批次后执行两次受 Mail 唤醒序列保护的原子空检查；两次均为空才销毁，不使用 sleep 或 debounce。检查期间到达的新消息必须由当前代接管或触发下一代，不能滞留。

## 每代能力视图

Host 在创建 Runtime 时根据角色、所有者、项目、路径与父代能力构造不可变 Tool/Skill registry。未授权能力从一开始就不进入上下文、manifest、prompt、枚举结果或 registry。

- 工具和 Skill 调用只在本代 registry 中按 ID 查找，不在每次调用前查询数据库或重新执行 RBAC/ABAC 判断。
- 未授权 ID 与不存在的 ID 对外统一为 `unknown capability`，不能暴露能力是否安装或为何未授权。
- 子代能力必须是父代能力的子集；创建时若请求扩权则失败。
- Grant 在一代内不可变。授权撤销或策略变化时，Host 取消并销毁当前代；父代撤销级联其活动子代。之后创建的新一代获得新视图。
- registry 中的执行包装器仍负责取消、资源跟踪、路径/命令参数约束和结构化日志，但不能把已隐藏能力重新暴露出来。

## 会话上下文窗口

会话上下文分成四层，任何实现都不得把它们重新合并成“每轮从数据库重读全历史并重压缩”的单一路径。

| 层级 | 真相与职责 | 进入模型的规则 |
|---|---|---|
| 会话历史与记忆检查点 | 应用 SQLite 保存完整会话消息，并保存滚动摘要、锚点和恢复所需元数据；数据库同时服务前端历史展示与进程冷恢复。 | 热态后续轮次不重读全历史；仅首次加载或进程重启时读取检查点及锚点后增量。 |
| session 热窗口 | 父 Runtime 为每个 session 保存进程内窗口，包含已有摘要、保留消息和持久化锚点。 | 每轮只合并新消息并推进窗口，不随会话总长度重复线性处理。 |
| 渲染后的模型上下文 | Context 模板只渲染一个顶层 `short_term_memory`；当前用户消息由当前轮输入单独承载。 | 当前用户消息在同一请求中只出现一次；`accessible_context`、`long_term_memory` 和 `rag` 不再提供重复记忆入口。 |
| 本地观测 | 配置的本地 Langfuse 接收完整 `messages`、`tools`、模型响应和首次完整工具结果，用于排查真实模型行为。 | 观测数据不参与窗口压缩，也不自动成为下一轮模型上下文。 |

窗口达到水位时，只淘汰已经进入历史的最旧对话消息。记忆优化器只接收“已有摘要 + 本次淘汰消息”，不接收完整历史、不携带 tools，也不处理工具结果；优化失败、超时、空输出或非法输出时，窗口使用确定性回退摘要并记录失败原因。窗口更新、检查点写入和当前轮持久化受同一 session 整轮 admission lock 串行保护，取消或异常必须在 `finally` 路径释放锁，不能留下半推进状态。

## 工具结果生命周期

工具结果采用“一次完整消费，之后代码收据”的确定性生命周期：

1. 工具执行完成后，原始结果完整加入 provider 消息；在模型尚未成功消费该结果前不得压缩。
2. 紧接着的一次模型请求和对应本地 Langfuse generation 都看到完整工具结果。
3. 该模型请求成功后，provider 才把历史 tool message 替换为代码生成的稳定 JSON 收据；后续轮次只携带收据。
4. 收据只保留实际存在的白名单字段，包括工具名、成功/失败状态、标识、路径、哈希、游标、退出码和失败诊断。未知大字段不会进入收据；失败 `error_summary` 最多 240 个字符，相同输入必须生成字节一致的结果。

工具结果不交给 AI 做摘要。完整结果的本地观测与后续 prompt 的收据是两个独立产物，不能为了压缩 prompt 而破坏首次消费或观测保真。

## 模型终止与异常看门狗

provider 的正常循环只接受三类模型行为：存在 `tool_calls` 时继续既有授权、追踪与执行链；合法 `completed` 正常完成；合法且带原因的 `blocked` 明确停止。无工具调用但终态为空或结构非法时，校验反馈进入消息并允许模型修复，不能把任意文本当作完成。

不设置系统指令、对话、工具描述和工具结果的统一 prompt token 预算，也不使用固定工具轮数或工具调用数表达正常完成。`max_wall_seconds` 是覆盖整次 provider 执行的绝对异常看门狗，不能在每次 await 后重置；单次 HTTP 请求仍保留独立 timeout。墙钟超时、请求超时、用户取消、应用关闭和授权预算拒绝都属于异常终止，必须取消阻塞任务、释放 session 锁并保留结构化诊断，不能伪装成 `completed` 或 `blocked`。

## 生命周期与恢复

Runtime 使用统一状态语义：`CREATING → READY → RUNNING → STOPPING → COMPLETED/FAILED/CANCELLED → DESTROYED`。`INTERRUPTED` 是启动恢复写入的持久化终态，用于标识上次进程未正常收口的非终态记录。

销毁必须幂等：停止接收新输入、取消 provider/Tool/Skill 任务、按 LIFO 释放资源、持久化唯一终态并从活动 registry 移除。日志失败不得改变终态或业务返回值。

恢复与关闭规则：

- 应用启动时把遗留 `CREATING/READY/RUNNING/STOPPING` 记录改为 `INTERRUPTED`；父/子 Runtime 不自动恢复。
- 启动时逐项目恢复 outbox 并读取 `.bridle/mail.db`；READY Outbox 和待处理 Map 消息可以创建新的 Map Runtime，两者均空时不创建 generation，也不扫描项目文件差异。
- 单个项目路径或本地 SQLite 损坏时，仅在应用库 `project_runtime_recovery` 保存脱敏降级事实并映射既有 readiness；健康项目继续恢复，成功后清除对应降级行。
- 关闭会话停止其父 Runtime 并级联活动子 Runtime，但保留会话、消息、Memory 和 Runtime 历史。
- 应用关闭先永久关闭 Registry admission，再停止并 join Outbox Forwarder，随后销毁父/子/Map Runtime并等待自动退役 finalizer；有界等待超时只记录 forced，最终清理仍不可跳过。
- 关闭页面不删除数据；应用关闭释放 claim/lease 并保留持久化数据。
- 只有显式删除操作可以删除对应持久化业务数据。

## 数据归属

| 数据 | 目标位置 | 说明 |
|---|---|---|
| 项目、会话、消息、Runtime 记录 | 既有应用 SQLite | Runtime 类型、所有者、`agent_id`、`generation`、状态、时间和结果/错误摘要 |
| 会话历史与记忆检查点 | 既有应用 SQLite | 完整历史用于前端展示；滚动摘要、锚点和锚点后增量用于冷恢复，不参与每轮全历史重压缩 |
| session 热窗口 | 父 Runtime 进程内状态 | 保存当前摘要与保留消息；只处理当前轮增量，进程退出后由检查点恢复 |
| envelope、地址、seq、lease、ACK/NACK | 每项目 `.bridle/mail.db` | 只负责可靠传输与领取栅栏 |
| 文件提交预留与转发状态 | 每项目 `.bridle/change_outbox.db` | 连接正式原子文件提交与 Mail，支持崩溃恢复 |
| 地图、`change_seq`、Map 应用回执 | 每项目 `.bridle/plan.db` | Map 业务事务与 `message_id` 幂等边界 |
| 活跃 handle、task、连接与唤醒器 | 进程内 registry | 不持久化不可恢复的运行对象 |

`.bridle/**` 必须排除在代码索引和 `CodeChanged` 生产范围之外，避免系统管理写入自激。

## 观测与错误契约

创建、投递、claim、ACK/NACK、窗口冷/热更新、检查点读写、优化器成功或回退、工具结果消费与收据替换、模型终态、看门狗、取消、重试、降级、撤权和销毁都通过既有日志设施记录。跨边界字段至少包括适用的 `trace_id`、`message_id`、`project_id`、`session_id`、`agent_id` 和 `generation`，并附带稳定事件名、状态、有界结果或错误摘要。结构化应用日志不得记录 secret 或无界 payload；配置的本地 Langfuse 观测可以保留完整 provider `messages`、`tools`、响应和首次工具结果，观测失败仍不得改变业务返回值。

能力查询统一使用 `unknown capability`。持久化容量不足、lease 失效、处理失败、关闭超时和恢复失败使用现有内部错误与 readiness/status 边界；除非既有接口已经定义，不新增对外错误 schema 或状态码。

## 分批交付

1. 持久化与日志基线：建立 Runtime 记录和三类项目本地数据库的 schema/创建边界，扩展既有结构化日志字段。
2. 持久化 Mail：实现 seq、claim fencing、lease 恢复、ACK/NACK、重试与事件循环安全唤醒。
3. Runtime 核心与能力视图：实现统一状态机、所有权、每代 registry、撤权级联和幂等销毁。
4. 父/子 Runtime：接入会话输入、并发子代、结果回传、会话关闭与启动中断修复。
5. 正式补丁 Outbox：实现预留、单文件原子替换、崩溃恢复、转发与背压。
6. Map Runtime：实现按需单实例、批处理、事务回执、commit-then-ACK、降级与双空检查销毁。
7. 启动/关闭集成与兼容验收：打通注册项目扫描、恢复、应用 lifespan、现有 API 和全链路日志。
8. 上下文窗口改造：统一顶层短期记忆入口，建立 session 热窗口与检查点增量冷恢复，加入无工具记忆优化器、工具结果一次完整消费与确定性收据、模型结构化终态和整轮墙钟看门狗，并删除旧的重复入口。

每批必须独立完成 TDD、范围验证和独立审查；未完成的后续批次不能成为当前批次放宽可靠性或资源回收合同的理由。

## 验收与验证

| 范围 | 必测行为 | 完成标准 |
|---|---|---|
| Mail claim | 并发 claim、旧 token ACK、lease 过期、重启恢复、队列满 | 单一有效所有者；旧 token 不能改状态；消息不丢失、不永久失败 |
| 正式补丁 | outbox 满、写入失败、replace 后崩溃、Mail 满、多文件部分成功 | 满载时不写；成功文件最终恰好产生一个稳定 ID 的至少一次消息；失败文件无成功事件 |
| Map 幂等 | 正常批次、重复投递、commit 后 ACK 前崩溃、处理异常 | 每个消息回执与地图变更同事务；重复消息不增加 `change_seq`；失败保持消息并标记 degraded |
| Runtime 所有权 | 同会话重复创建、并发子代、同项目 Map 竞争、进程重启 | 父/Map 单实例约束成立；子代可并发；遗留活动态变为 `INTERRUPTED` |
| 能力隔离 | 枚举、prompt/manifest、未知与未授权 ID、子代扩权、运行中撤权 | 未授权能力不可见；两类查询同错；无逐调用验权；撤权销毁当前代并级联子代 |
| 动态上下文 | 热态连续对话、窗口越线、优化失败、进程冷恢复、同 session 并发 | 热态不读全历史；只优化被淘汰对话；失败确定性回退；恢复不重复不遗漏；整轮串行且锁可释放 |
| 工具结果 | 成功与失败结果、首次消费、连续后续轮次、未知大字段、本地观测 | 首次请求与观测保留完整结果；随后只留稳定白名单收据；失败摘要不超过 240 字符 |
| 模型终止 | `tool_calls`、非法终态修复、`completed`、`blocked`、墙钟超时与取消 | 只有合法模型终态正常返回；工具调用继续；异常看门狗不冒充完成且能释放资源 |
| 关闭与保留 | 会话关闭、页面关闭、应用关闭、重复 stop、显式删除 | 任务和资源正确收口；历史/Memory 默认保留；只有显式删除移除数据 |
| 日志与兼容 | 全链路成功/失败、日志 sink 失败、既有 API 回归 | 关联字段完整且脱敏；日志失败不改业务结果；既有 schema/status 不变 |

精确验证命令、批次计划和证据索引统一写在 [实现状态](implementation_status.md)。只有实际测试成功且独立审查无阻断 finding，才能把对应条目提升为 `Verified`。

## Maintenance

**Update Triggers:**

- Runtime 类型、所有权、并发约束、状态机或恢复语义发生变化。
- Mail claim/lease/ACK、Outbox 原子提交或 Map 幂等事务发生变化。
- Tool/Skill 可见性、generation 或撤权级联语义发生变化。
- session 热窗口、记忆检查点、工具结果收据、模型终态或墙钟看门狗语义发生变化。
- 应用数据库、`.bridle/mail.db`、`.bridle/change_outbox.db` 或 `.bridle/plan.db` 的职责发生变化。
- 既有 API、readiness/status 或结构化日志合同发生变化。

**Verification:**

- 对照 `.ai-dev/evidence/requirements-agent-runtime-mail-map-20260713.json` 与 `FR-BRD-025~029`。
- 确认文中状态没有在代码和测试证据之前从 `Planned` 提升。
- 确认 Mail 只传输、Runtime/会话同库存储、Map 副作用按 `message_id` 幂等。
- 确认未授权能力根本不可见，且调用路径没有逐次 RBAC/数据库检查。
- 确认模型 prompt 的动态窗口与本地 Langfuse 完整观测保持分层，工具原始结果只在首次消费后替换为确定性收据。
- 检查内部链接、metadata 与排除项。
