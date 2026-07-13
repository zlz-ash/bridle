# Agent Runtime、Mail 与 MapAgent 按消息唤醒实施计划

状态：待实施。本文只记录未完成工作；每完成并验收一项，就从本文删除该项，不保留历史完成记录。

## 目标与已确认约束

- 父 Agent、子 Agent、MapAgent 共用同一套 `AgentRuntime` 实现和 `AgentRuntimeHost` 基础设施，但每个 Agent 都拥有独立 Runtime 实例，不共享可变执行状态。
- Runtime 实例独立持有 Mailbox、`AgentGrant`、执行上下文、Memory 视图、预算、取消令牌和执行任务；Host 只共享不可变定义、工厂、路由、策略和实例监管能力。
- 权限采用 RBAC：父 Agent 的子 Agent 权限只能从父授权派生且不能扩权；MapAgent 是项目级根 Agent，授权来自项目策略，不属于某次会话或父 Agent。
- Mail 采用通用 MPSC 模型：允许多线程/多协程写入，每个 Runtime 只有一个消费者顺序取出；消息类型、路由、Agent 类型和策略通过注册表分配，避免集中到单个万能注册表。
- MapAgent 的项目级持久 Mailbox 可以常驻，但 MapAgent Runtime 不常驻。只有 `CodeChanged(paths)` 到达时才单飞唤醒；处理、提交并 ACK 全部消息后销毁 Runtime。
- MapAgent 的唯一代码变化生产者是实际代码补丁/插桩落盘链路。业务消息只包含发生变化的项目相对路径；消息编号、项目编号、序号、重试次数等属于 Mail 信封，不进入业务载荷。
- MapAgent 内部完成地图分析并直接提交 Map Store；不发送 `MAP_ANALYSIS_STARTED`、`MAP_ANALYSIS_COMPLETED` 等生命周期消息。其他模块读取 Map Store 获得分析结果。
- 保留项目打开时的 Map Store 初始化，但项目打开不再创建或常驻 MapAgent Runtime；现有轮询式 `CodeMapRefreshWatcher` 从该链路移除。
- 使用现有日志/可观测性设施，不新增第三方日志依赖。全链路日志必须可由 `trace_id`、`message_id`、`project_id`、`agent_id` 和 `generation` 串联，且不记录源代码或完整 diff。
- 每个实施批次按输入范围和 checkpoint 生成唯一执行收据；相同范围、依赖、策略与 checkpoint 已成功的阶段不得重复执行。首次业务代码修改前仍需 ash 明确确认本计划。

## 未完成工作

1. 固化 Runtime、Mail 和生命周期契约，并先建立失败测试
   - 原因：当前项目只有授权基础和专用 `ProjectMapAgent`，没有能够同时承载父、子、项目级 Agent 的通用 Runtime 实例契约；如果先写实现，容易继续把 Agent 身份、线程和会话生命周期混在一起。
   - 修改：在 `backend/src/bridle/agent/runtime/schemas.py` 定义最小契约，包括 `AgentType`、`AgentAddress`、`AgentSpec`、`AgentHandle`、`AgentLifecycleState`、`MailEnvelope`、`CodeChanged` 和终止原因；新增 `backend/tests/agent/runtime/test_runtime_contracts.py`，先写 RED 测试。生命周期至少覆盖 `CREATED -> STARTING -> RUNNING -> DRAINING -> STOPPED/FAILED`，MapAgent 的“休眠”表示 Runtime 实例不存在而 Mailbox 存在，不额外维持空转线程。
   - 保留：保留现有 API Schema 的返回字段和 `AgentAuthorizationService` 的授权数据含义；不在这一项引入网络传输、A2A 协议或分布式消息队列。
   - 完成标准：父、子、Map 三类 Agent 可以用统一 Spec 描述；地址能区分项目、会话和实例；非法状态跃迁、跨项目地址和缺少项目归属的 MapAgent 在创建前被拒绝；`CodeChanged` 只接受去重、规范化后的项目相对路径集合。
   - 测试：覆盖三类 Spec、路径去重、空路径拒绝、绝对路径/`..` 拒绝、非法状态跃迁和信封/业务载荷边界；避免只断言对象能构造而不验证状态和安全约束。
   - 验证：预计 5–15 秒，运行 `.\.venv\Scripts\python.exe -m pytest backend/tests/agent/runtime/test_runtime_contracts.py -q`。

2. 实现通用 MPSC Mailbox、持久化适配器与注册式路由
   - 原因：`asyncio.Queue` 不能由任意线程直接安全写入，现有 MapAgent Mailbox 也只承载停止信号，无法支持多生产者、单消费者、崩溃重投和项目级常驻 Mailbox。
   - 修改：新增 `backend/src/bridle/agent/runtime/mailbox.py`，提供 `Mailbox`/`MailboxStore` 协议、异步 `send`、线程安全 `send_from_thread`、单消费者 `claim`、`ack`、`nack` 和有界背压；新增 `backend/src/bridle/agent/runtime/persistent_mailbox.py`，实现工作区派生路径下的 SQLite/WAL 项目 Mailbox、租约和未 ACK 重投；新增 `backend/src/bridle/agent/runtime/registries.py`，分别维护 Agent 类型工厂、活跃实例、RBAC 策略和消息路由，定义注册表在启动后冻结、实例注册表动态变化。新增 `test_mailbox.py` 与 `test_runtime_registries.py`。
   - 保留：Mail 只定义本地进程内路由与持久存储接口，不绑定 MapAgent 业务；普通会话 Agent 可使用内存适配器，项目级 MapAgent 使用持久适配器；不把所有注册职责合并为一个 God Registry。
   - 完成标准：多个线程/协程并发写入不会丢消息；每个 Mailbox 同时只有一个消费者；同一 Mailbox 的序号单调；队列满时返回明确背压结果；消费者崩溃或租约过期后未 ACK 消息可重投；进程重启后项目 Mailbox 仍可恢复。
   - 测试：用真实线程并发写入并核对消息 ID 集合、序号和单消费约束；模拟 claim 后崩溃、ACK 后重启、重复发送、队列满和 SQLite 锁竞争；禁止用串行循环伪造多线程场景。
   - 日志：记录 `mail.enqueued/claimed/acked/nacked/redelivered/backpressure`，包含地址、序号、尝试次数和耗时，不记录消息正文。
   - 验证：预计 15–40 秒，运行 `.\.venv\Scripts\python.exe -m pytest backend/tests/agent/runtime/test_mailbox.py backend/tests/agent/runtime/test_runtime_registries.py -q`。

3. 实现共享 Host、独立 Runtime 实例和统一销毁协议
   - 原因：父子 Agent 需要共用实现而不能共用实例；现有 `ProjectRuntimeRegistry` 的并发启动、回滚和停止屏障可复用，但其目标仍是“项目打开即启动一个专用 MapAgent”。
   - 修改：新增 `backend/src/bridle/agent/runtime/agent_runtime.py` 实现每实例独立的执行状态和单 Mailbox 消费循环；新增 `backend/src/bridle/agent/runtime/host.py` 实现 RuntimeFactory、Supervisor、单飞创建、取消、等待和销毁；将 `backend/src/bridle/agent/runtime/project_registry.py` 中已验证的锁、启动回滚、停止屏障和 generation 思路下沉到 Host/Supervisor，避免复制两套生命周期代码；在 `backend/src/bridle/agent/runtime/__init__.py` 只导出稳定公共接口。
   - 保留：每个 Runtime 使用独立 `AgentGrant`、ExecutionContext、Memory 视图、Budget、CancellationToken、任务和 Mailbox 消费权；Host 仅共享只读工具/Skill 定义、策略和路由。现有 Registry 的并发安全、失败回滚和关闭幂等语义必须保留。
   - 完成标准：同一 Host 可同时创建父、子、Map Runtime，三者对象和可变状态完全独立；同一 Agent 地址的并发创建只有一个成功实例；启动失败不残留注册项或任务；销毁完成后实例注册表、任务、租约和授权视图全部释放。
   - 测试：覆盖并发创建、启动中取消、执行中失败、重复 stop、stop 超时、创建失败回滚、状态隔离和资源释放；用弱引用或任务枚举验证没有悬挂 Runtime，而不是只检查状态字段。
   - 日志：记录 `runtime.create_requested/started/draining/stopped/failed/destroyed` 及 generation、终止原因、耗时和未处理消息数。
   - 验证：预计 20–60 秒，运行 `.\.venv\Scripts\python.exe -m pytest backend/tests/agent/runtime/test_agent_runtime_host.py backend/tests/agent/runtime/test_project_runtime_registry.py -q`。

4. 将 RBAC 绑定到 Runtime 创建和每次 Tool/Skill 调用
   - 原因：仅在创建时检查权限不足以阻止运行中越权；同时父子 Agent 和项目级 MapAgent 的授权来源不同，不能用会话继承关系描述 MapAgent。
   - 修改：扩展 `backend/src/bridle/agent/runtime/authorization.py`、`role_policy.py` 和 `capability_policy.py`，由 Host 在创建 Runtime 时生成只读授权视图，并在每次 Tool/Skill 调用前重新校验撤销状态；接入 `backend/src/bridle/agent/tools/registry.py` 与 `backend/src/bridle/agent/skills/registry.py`，按 Grant 返回过滤后的定义视图；父到子继续使用 `AgentAuthorizationService.derive`，MapAgent 通过项目策略独立签发最小 Grant。
   - 保留：子 Agent 的 project/session 归属以及资源、Tool、Skill、预算均不得超过父 Grant；工具执行器仍负责沙箱和路径校验，RBAC 不替代现有安全策略；MapAgent 只获得读取变化文件、更新 Map Store 和 Mail ACK 所需能力。
   - 完成标准：任何子 Agent 扩权请求都在 Runtime 启动前失败；父 Grant 被撤销后，子 Runtime 的下一次受保护操作失败并进入取消/销毁流程；MapAgent 不依赖父会话且不能调用未授权业务工具；共享定义对象不会携带某个 Runtime 的可变权限状态。
   - 测试：扩展 `backend/tests/agent/runtime/test_authorization.py` 和 `test_runtime_role_policy.py`，覆盖 Tool、Skill、目录、预算分别越权、父撤权级联、跨项目派生、Map 独立授权和 TOCTOU 撤权；避免只测策略函数而不经过 Runtime 调用门。
   - 日志：允许记录能力名称和拒绝原因，输出 `rbac.grant_issued/derive_denied/invocation_denied/revoked`，不得记录 Skill 内容或工具参数中的源码。
   - 验证：预计 15–45 秒，运行 `.\.venv\Scripts\python.exe -m pytest backend/tests/agent/runtime/test_authorization.py backend/tests/agent/runtime/test_runtime_role_policy.py backend/tests/agent/runtime/test_unified_agent_runtime_api.py -q`。

5. 接通父 Agent 创建子 Agent、Mail 通信与分类型销毁
   - 原因：当前 `ModifyLoopService.dispatch_child_agent` 主要更新计划节点状态，工具入口存在但没有真正创建子 Runtime；需要把“请求创建”落到 Host，而不是把 Runtime 生命周期塞进工具层。
   - 修改：调整 `backend/src/bridle/agent/runtime/gateway.py`，使父 Agent 通过 Host 提交 `SpawnAgent` 请求并获得 `AgentHandle`；调整 `backend/src/bridle/features/project_map/modify_loop_service.py` 的 dispatch 链路，仅负责业务状态和调用 Runtime API，不自行构造线程或 Runtime；子 Agent 的结果、失败和取消通过父 Mailbox 返回。新增 `backend/tests/agent/runtime/test_parent_child_runtime.py`，扩展 `backend/tests/features/project_map/test_modify_loop.py`。
   - 保留：现有 dispatch API 的输入、计划节点状态机和调用入口；Tool 只是受 RBAC 管控的入口，真正的 Runtime 创建、监管和销毁全部由 Host 完成。
   - 完成标准：父 Agent 可创建多个互相隔离的子 Runtime；子成功/失败后先投递终态结果再销毁；父取消、会话关闭或父授权撤销会级联取消并等待全部子 Runtime；单个子失败不会直接销毁仍有效的父 Runtime；超时销毁产生明确终止原因且不遗留任务。
   - 测试：覆盖成功结果、异常结果、父取消、多子并发、子超时、结果投递失败重试、重复销毁和父先退出的竞态；断言 Registry、Mailbox、授权与任务最终状态，而不只断言 HTTP 200。
   - 日志：记录 `child.spawn_requested/spawned/result_sent/cancelled/destroyed`，并包含 parent_agent_id、child_agent_id 和原因。
   - 验证：预计 20–60 秒，运行 `.\.venv\Scripts\python.exe -m pytest backend/tests/agent/runtime/test_parent_child_runtime.py backend/tests/features/project_map/test_modify_loop.py -q`。

6. 在真实补丁落盘边界增加可靠 CodeChanged Outbox
   - 原因：只在文件写完后临时发 Mail，进程可能在“代码已改变、消息未持久化”的窗口崩溃；只在写前发消息又会在写入失败时产生假变化。需要可靠收据消除静默丢事件，同时保持业务载荷只有路径。
   - 修改：在 `backend/src/bridle/agent/tools/sandboxed_executor.py::_apply_patch_to_workspace` 注入通用变化发布接口；新增 `backend/src/bridle/agent/runtime/change_outbox.py`，在写入前持久化 PREPARED 收据（内部可含前后摘要），实际落盘成功后标记 READY 并向项目 Mailbox 发布 `CodeChanged(paths)`；恢复时依据目标文件摘要把 READY/已落盘 PREPARED 补投，把确认未落盘的 PREPARED 标记失败。扩展 `backend/tests/agent/tools/test_sandboxed_tool_executor.py`，新增 `backend/tests/agent/runtime/test_change_outbox.py`。
   - 保留：`unified_diff.py` 的校验语义、SandboxPolicy、TDD 状态和工具返回结构；工具层依赖发布接口而非具体 MapAgent。创建、修改、删除均发送规范化相对路径；若现有流程以“删除旧路径 + 创建新路径”表达重命名，则一次合并事件包含旧、新两个路径，不额外扩展独立 rename 工具语义。
   - 完成标准：校验失败、沙箱拒绝、dry-run 或实际写入失败均不产生 READY 消息；成功写入最终至少产生一次可去重事件；崩溃窗口恢复后不会静默遗漏已落盘变化；相同 outbox 收据重复投递由 message_id 去重；对 MapAgent 可见的 payload 始终只有 `paths`。
   - 测试：在 PREPARED 后、文件替换后、READY 后和 Mail 入队后分别注入崩溃；覆盖创建、修改、删除、批量路径、重复恢复和写入失败；断言磁盘内容、outbox 状态和 Mailbox 消息三者一致，禁止 mock 掉真实临时工作区写入。
   - 日志：记录 `change_outbox.prepared/write_committed/ready/published/recovered/failed`，只记录摘要、路径数和相对路径，不记录 diff 内容。
   - 验证：预计 30–90 秒，运行 `.\.venv\Scripts\python.exe -m pytest backend/tests/agent/runtime/test_change_outbox.py backend/tests/agent/tools/test_sandboxed_tool_executor.py -q`。

7. 将 MapAgent 改为项目 Mailbox 驱动的按需唤醒消费者
   - 原因：当前 `ProjectService.open_project -> ProjectRuntimeRegistry.ensure_started -> ProjectMapAgent.start` 会在项目打开后常驻，并由 `CodeMapRefreshWatcher` 轮询文件再直接调用 `refresh_code_paths`；这与“只有真实补丁落盘才发消息、无消息不运行”的目标冲突。
   - 修改：重构 `backend/src/bridle/agent/runtime/project_map_agent.py`，使其只消费 `CodeChanged`、合并路径并调用 `ProjectPlanStore.refresh_code_paths(paths)`；重构 `project_registry.py` 为项目 Mailbox 激活协调器，消息入队或进程启动发现非空 Mailbox 时单飞创建一个 Map Runtime；修改 `backend/src/bridle/features/projects/service.py`，项目打开只初始化 Map Store 和注册项目 Mailbox，不再 `ensure_started`；确认无调用者后删除 `backend/src/bridle/features/project_map/watcher.py` 及其专用测试，改为 `backend/tests/features/project_map/test_map_agent_activation.py`。
   - 保留：`ProjectPlanStore.initialize`、增量索引算法、现有 Map 查询/API 和应用关闭时的统一清理入口；MapAgent 在消费时根据文件系统现状判断路径是新增、修改还是删除，不要求生产者携带操作类型。
   - 完成标准：空 Mailbox 时不存在 MapAgent Runtime/线程/轮询任务；第一条消息只唤醒一个 generation；运行中到达的新消息被同一消费者继续合并处理；Map Store 事务提交成功后才 ACK；Mailbox 清空后 Runtime 进入 draining 并销毁，项目 Mailbox 继续存在；分析过程不产生额外 Mail 消息。
   - 测试：覆盖项目打开不启动、首次消息唤醒、多消息路径合并、重复路径、删除文件、刷新失败 NACK、提交成功 ACK、运行中再入队、draining/stop 窗口再入队和进程重启后非空 Mailbox 自动恢复；断言每个项目任意时刻最多一个活跃 Map Runtime。
   - 日志：完整记录 `map.wake_requested/runtime_started/batch_claimed/refresh_started/refresh_committed/batch_acked/draining/runtime_destroyed`，包含 path_count、generation、耗时与失败原因。
   - 验证：预计 30–90 秒，运行 `.\.venv\Scripts\python.exe -m pytest backend/tests/features/project_map/test_map_agent_activation.py backend/tests/features/project_map/test_incremental_reindex.py backend/tests/agent/runtime/test_project_runtime_registry.py -q`。

8. 完成崩溃恢复、代际隔离和三类 Agent 的销毁边界
   - 原因：消息驱动 Runtime 的主要风险在停止竞态：旧 generation 不能 ACK 新 generation 的消息，应用退出不能丢失 claim，父子级联不能留下后台任务，项目级 Mailbox 又不能随 Map Runtime 一起销毁。
   - 修改：在 Host、持久 Mailbox和 `backend/src/bridle/app.py` 统一关停顺序：停止接收新 Runtime 创建，取消/等待会话父子 Runtime，停止 Map 消费，将未 ACK claim 释放为可重投，最后关闭共享存储；generation 写入 claim 并在 ACK 时校验。定义销毁策略：父 Runtime 随会话关闭/取消销毁并级联子级；子 Runtime 随终态/超时/取消销毁；Map Runtime 随 Mailbox 清空销毁，但项目 Mailbox 仅在明确删除项目数据时清理。
   - 保留：stop/stop_all 幂等、应用 shutdown 不无限等待、项目关闭或前端离开页面不会删除尚未处理的项目消息。
   - 完成标准：旧 generation ACK 被拒绝；stop 期间新消息要么被当前 generation 接管，要么保持未 ACK 并触发下一 generation；强制退出后重启可重投；所有 Runtime 销毁后事件循环无悬挂任务，持久 Mailbox 数据符合各类型清理策略。
   - 测试：新增 `backend/tests/agent/runtime/test_runtime_shutdown_recovery.py`，使用事件屏障复现 claim/commit/ACK/stop 各窗口；覆盖父子级联、Map 重启、旧 generation ACK、shutdown 超时和重复 stop，禁止用 sleep 猜测竞态顺序。
   - 日志：记录关停阶段、等待数量、强制取消、重投数量和 generation 冲突，异常路径必须带堆栈且只记录一次主错误。
   - 验证：预计 20–60 秒，运行 `.\.venv\Scripts\python.exe -m pytest backend/tests/agent/runtime/test_runtime_shutdown_recovery.py backend/tests/agent/runtime/test_agent_runtime_host.py -q`。

9. 兼容现有 Gateway/API 并完成端到端验收
   - 原因：Runtime 架构改动不能破坏现有会话对话、项目打开、Map 查询、工具返回和前端增量同步契约；单元测试通过也不能证明真实落盘到地图刷新的链路成立。
   - 修改：让 `backend/src/bridle/agent/runtime/gateway.py` 的现有会话入口委托统一 Host，同时保持外部响应 Schema；更新 `backend/tests/agent/runtime/test_unified_agent_runtime_api.py`、`backend/tests/features/projects/test_project_service.py`、项目 Map API 测试，并新增真实临时工作区端到端用例：父 Runtime 受权调用补丁工具 -> 文件落盘 -> outbox/Mail -> Map Runtime 唤醒 -> Map Store 更新 -> ACK -> Runtime 销毁。
   - 保留：外部 HTTP 路由、状态码、项目/会话 ID 语义、Map 增量序号以及前端消费格式；不在本批次增加跨进程 A2A、远程 Broker、通用调度 UI 或新的 Agent 类型。
   - 完成标准：现有 API 契约测试全部通过；真实端到端链路只刷新变化路径；未变化文件索引保持稳定；最终 Mailbox 为空、Map Runtime 不存在、父/子资源按策略释放；关键日志能用同一 trace 串起完整路径。
   - 测试：必须使用真实临时 D 盘工作区、真实 SQLite Mailbox/Map Store 和真实补丁应用；只替换模型响应，不 mock Mailbox、文件写入或 `refresh_code_paths`；同时覆盖失败补丁不会唤醒 MapAgent。
   - 验证：预计 1–3 分钟，运行 `.\.venv\Scripts\python.exe -m pytest backend/tests/agent/runtime backend/tests/agent/tools/test_sandboxed_tool_executor.py backend/tests/features/project_map backend/tests/features/projects/test_project_service.py -q`。

10. 执行独立审查、全量回归和 CI 审批门禁
   - 原因：并发、授权和持久化改动风险高，需要与实现上下文隔离的审查者检查权限扩张、竞态、丢消息和资源泄漏；同时避免重复执行已经有相同 checkpoint 成功收据的阶段。
   - 修改：按 `project-development-pipeline` 对当前批次运行 impact、TDD 收据、fresh reviewer claim、CI 审计和审批；审查只针对本计划变更范围。远程写操作前先做 GitHub capability preflight，connector 不可用时按插件的 `github-gcm-rest` 受信适配器降级，`gh` 只作诊断，不再到写 Issue 时逐级试错。
   - 保留：没有 ash 明确授权不提交、不推送、不创建 PR/Issue；审查发现的无关旧问题只报告，不顺手重构。相同 batch/scope/input/dependency/policy/checkpoint 已成功的阶段直接复用收据，不重新做历史工作；fresh review、人工审批和最终审计不跨批次复用。
   - 完成标准：不存在未处理的 P0/P1 或高风险并发/RBAC finding；所有本批次测试和仓库 CI 通过；审查证据绑定当前 commit、影响范围和执行收据；`plan.md` 中已完成项在验收后删除。
   - 测试：独立审查重点验证子 Agent 不能扩权、每 Agent 一 Runtime、Map 单飞、无消息不运行、ACK 在 Map Store 提交之后、崩溃可重投、旧 generation 不可 ACK、新旧 API 兼容以及日志无源码泄漏。
   - 验证：预计 3–10 分钟，先运行 `.\.venv\Scripts\python.exe -m pytest backend/tests -q`；再执行仓库现有 CI 检查。若实际耗时超过预估，立即检查卡住的测试、后台任务或锁等待并向 ash 上报，不盲目继续等待。

## 实施顺序与门禁

严格按 1 -> 10 执行。每项遵循“先写能证明业务契约的失败测试，再写最小实现，再运行该项验证命令”的顺序；上一项没有可复现的通过证据，不进入下一项。任何导致架构约束变化、需要新增外部依赖、改变 API 或扩大文件范围的情况，都先停止并请 ash 决策。
