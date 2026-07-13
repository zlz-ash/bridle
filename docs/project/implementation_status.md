<!-- SCOPE: Bridle 需求、架构决策与当前代码/测试证据的追踪状态 -->
<!-- DOC_KIND: record -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 判断某项能力是计划中、已有实现还是已被真实验证时 -->
<!-- SKIP_WHEN: 只需要需求定义、技术选型或 API 字段时 -->
<!-- PRIMARY_SOURCES: .ai-dev/spec/requirements.json, .ai-dev/docs/ln-110/context-store.json, docs/project/requirements.md, docs/reference/adrs -->

# 实现状态追踪

## Quick Navigation

| 目标 | 入口 |
|---|---|
| 状态口径 | [Status Definitions](#status-definitions) |
| 需求与 ADR 证据 | [Traceability Matrix](#traceability-matrix) |
| 证据升级规则 | [Evidence Rules](#evidence-rules) |
| 可执行验证 | [Verification Commands](#verification-commands) |

## Agent Entry

本文只回答“当前能证明到什么程度”。`Implemented` 表示 Context Store 或当前源码边界记录了实现入口；`Verified` 必须附带本轮或可复查运行记录。2026-07-11 的文档重写阶段没有运行产品测试，因此本文不把任何条目提升为 `Verified`。任务基线中的 `pending` 也不会因为文档已经提及就自动完成。

## Status Definitions

| 状态 | 定义 | 最低证据 |
|---|---|---|
| Planned | 已在需求、设计或 ADR 中定义，尚无已确认实现入口。 | 来源文档链接 |
| In Progress | 部分产物已经建立，但完整验收条件尚未满足。 | 来源文档与已完成部分的路径 |
| Implemented | 当前代码或配置中存在可追踪实现。 | 来源文档与真实代码/配置路径 |
| Verified | 实现已由实际测试、CI、人工验收或可复查报告证明。 | 来源、代码路径、测试/验证记录与日期 |
| Deprecated | 当前行为不再维护或已被替代。 | 来源文档与替代/移除说明 |

## Traceability Matrix

| ID | Source Doc | 范围 | Status | Code / Config Evidence | Test / Validation Evidence | Last Verified | 说明 |
|---|---|---|---|---|---|---|---|
| FR-BRD-001~004 | [requirements.md](requirements.md) | workspace 启动、本地绑定、Git 初始化与工作区读取 | Implemented | `backend/src/bridle/cli.py`, `backend/src/bridle/features/workspace` | `backend/tests/cli`（仅记录路径，未运行） | — | Context Store 确认 CLI、workspace feature 与 loopback 默认边界。 |
| FR-BRD-010~015 | [requirements.md](requirements.md) | 项目记录、增量/语义地图、候选与刷新 | Implemented | `backend/src/bridle/features/projects`, `backend/src/bridle/features/project_map` | `backend/tests/features/project_map`（仅记录路径，未运行） | — | Context Store 列出项目地图查询和写接口。 |
| FR-BRD-020~024 | [requirements.md](requirements.md) | 会话、消息、角色、能力与 provider | Implemented | `backend/src/bridle/features/sessions`, `backend/src/bridle/agent/providers` | 未在本轮运行 | — | Context Store 记录 projects、project_sessions、project_messages schema 与会话端点。 |
| FR-BRD-030~034 | [requirements.md](requirements.md) | React UI、Vite 代理、地图同步与设计系统 | Implemented | `frontend/src/api`, `frontend/src/components`, `frontend/src/hooks`, `frontend/src/layout`, `frontend/src/lib` | `frontend/src/hooks/__tests__`（仅记录路径，未运行） | — | Context Store 确认前端域、React Query 与自定义 `brd-*` 组件系统。 |
| FR-BRD-040~046 | [requirements.md](requirements.md) | 结构化错误、观测、容器 evidence、Unicode 与 schema 口径 | Implemented | `backend/src/bridle/app.py`, `backend/src/bridle/observability`, `backend/src/bridle/agent/container`, `.github/workflows/container-docker-linux.yml`, `scripts/ci` | `backend/tests/observability`, `backend/tests/logging`, `backend/tests/agent/container`（均未在本轮运行） | — | 已确认实现入口存在，不等于测试已通过。 |
| REQ-MAPPER-001 / SEC-AUTH-001 / REQ-LIFE-001 | [agent_runtime.md](agent_runtime.md) / [.ai-dev spec](../../.ai-dev/spec/agent-runtime-requirements.json) | 项目级 Mapper、RBAC+ABAC Grant、会话子 Agent 与统一销毁协议 | Planned | 当前存在分散的 gateway、role、tool、skill 与 project-map 入口，尚未形成目标运行时 | 尚无目标契约测试或运行证据 | — | 必须按授权基础、项目 Mapper、会话子 Agent、生命周期收口分批通过 TDD 和独立评审。 |
| REQ-DOC-001 / AC-DOC-001 | [.ai-dev/spec/requirements.json](../../.ai-dev/spec/requirements.json) | 重写 docs 并通过集中质量门 | In Progress | `docs/**/*.md` | 尚无集中校验报告 | — | 当前只是 ln-112 核心文档子集，不能代表全部 docs 完成。 |
| REQ-MAP-CI-001 / AC-MAP-CI-001 | [.ai-dev/spec/requirements.json](../../.ai-dev/spec/requirements.json) | 后端 project-map 与前端地图同步 CI 门禁 | Planned | Context Store 明确记录门禁尚未存在 | 尚无评审后 CASE 目录与运行证据 | — | 必须先完成测试合同、RED/GREEN 与双评审。 |
| REQ-CONT-CI-001 | [.ai-dev/spec/requirements.json](../../.ai-dev/spec/requirements.json) | 平台无关容器快速门禁 | Planned | `backend/tests/agent/container` | 尚无新门禁运行记录 | — | 测试路径存在不代表新门禁已编排。 |
| REQ-CONT-CI-002 / SEC-CONT-CI-001 | [.ai-dev/spec/requirements.json](../../.ai-dev/spec/requirements.json) | Linux/Docker trusted evidence 最终门禁 | Implemented | `.github/workflows/container-docker-linux.yml`, `scripts/ci`, `backend/src/bridle/agent/container` | 尚未在本轮执行真实 Docker 链 | — | 当前 workflow 与证据链存在；本轮增强验收仍为 pending。 |
| REQ-CI-TEST-001 / AC-CI-TEST-001 | [.ai-dev/spec/requirements.json](../../.ai-dev/spec/requirements.json) | 评审测试合同、真实 RED、最小 GREEN 与稳定 CASE ID | Planned | `.ai-dev/ci/` 目标边界 | 尚无完整批准目录 | — | CI Author 不得用 workflow 修改替代业务测试合同。 |
| NFR-CI-DET-001 / OPS-CI-OBS-001 | [.ai-dev/spec/requirements.json](../../.ai-dev/spec/requirements.json) | CI 可重复选择与结构化脱敏证据 | Planned | `.ai-dev/ci/` 目标边界 | 尚无 catalog 指纹与审计产物 | — | 只有全部指纹绑定后才能形成可复查批准证据。 |
| SEC-CI-AUTH-001 / AC-CI-AUTH-001 | [.ai-dev/spec/requirements.json](../../.ai-dev/spec/requirements.json) | CI Author 路径限制与批准指纹 | Planned | 允许前缀记录在需求基线 | 尚无完整 contract validation 记录 | — | 产品代码和业务测试不在 CI Author 修改范围。 |
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
| 证据脱敏 | 日志和审计产物不得包含凭据、secret、完整会话或无界输出。 |
| 降级规则 | 入口删除、语义变化、测试失效或证据不可复查时，先把状态降级。 |

## Verification Commands

以下命令来自当前需求基线；运行前必须按项目规则预估耗时。命令列出不等于已经执行。

| 范围 | 命令 | 预估 |
|---|---|---|
| 后端地图 | `cd backend; pytest tests/features/project_map` | 2–5 分钟 |
| 前端地图同步 | `cd frontend; npm test -- --run src/hooks/__tests__/mapLayerSync.test.ts src/hooks/__tests__/mapSyncLogger.test.ts src/hooks/__tests__/useProjectMapLayers.test.ts src/hooks/__tests__/useProjectMapLayers.retry.test.tsx src/hooks/__tests__/useProjectMapLayers.sync.test.tsx` | 1–3 分钟 |
| 容器合同 | `cd backend; pytest tests/agent/container` | 2–5 分钟 |

集中 docs-quality 校验器的精确命令未由 Context Store 固定，因此本文不写猜测命令；实际运行记录应在流水线审计产物中保存。

## Maintenance

**Update Triggers:**

- FR、REQ、AC、SEC、NFR、OPS 或 ADR 的状态发生变化。
- 代码、workflow、测试符号、CASE ID 或证据路径移动。
- pytest、Vitest、Docker gate、docs-quality 或人工验收产生新记录。

**Verification:**

- 状态只能使用 Planned、In Progress、Implemented、Verified、Deprecated。
- 任一 `Verified` 行必须同时具备代码证据、运行证据和日期。
- 与 [requirements.md](requirements.md)、[architecture.md](architecture.md) 和 [ADR 目录](../reference/adrs/) 保持一致。
