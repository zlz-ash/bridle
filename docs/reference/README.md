<!-- SCOPE: Bridle 的长期参考文档索引，覆盖 ADR、项目指南、包手册与调研记录。 -->
<!-- DOC_KIND: index -->
<!-- DOC_ROLE: navigation -->
<!-- READ_WHEN: 需要查找长期技术决策、可复用项目知识或参考资料状态时阅读。 -->
<!-- SKIP_WHEN: 只需要当前启动命令、API 路径或实现状态时跳过。 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json, .ai-dev/docs/ln-110/aggregate-summary.json -->

# 参考文档中心

> **状态：** 当前事实基线  
> **最后更新：** 2026-07-11

## Quick Navigation

| 类型 | 目录 | 当前内容 |
|---|---|---|
| Architecture Decision Records | [adrs/](adrs/) | 5 个已接受的技术决策。 |
| Project Guides | [guides/](guides/) | 3 份跨模块指南。 |
| Package Manuals | [manuals/](manuals/) | 当前无独立包手册。 |
| Research | [research/](research/) | 当前无独立调研记录。 |

## Agent Entry

| 信号 | 内容 |
|---|---|
| 用途 | 将长期决策与复用规则路由到 ADR 和指南。 |
| 何时阅读 | 需要理解“为什么选用某技术”或避免重复项目问题时。 |
| 何时跳过 | 需要最新 API、数据库、运行命令或实现进度时，应先读 `docs/project/`。 |
| 规范性 | 本页是 reference registry；项目现状仍以 project docs 为事实入口。 |
| 事实来源 | `.ai-dev/docs/ln-110/context-store.json` 与 `.ai-dev/docs/ln-110/aggregate-summary.json`。 |

## Architecture Decision Records (ADRs)

| ADR | Decision | Status |
|---|---|---|
| [ADR-001: FastAPI 后端运行时](adrs/adr-001-fastapi-backend.md) | 使用 FastAPI、Uvicorn、Pydantic 与 Typer 组成本地 API 运行时。 | Accepted |
| [ADR-002: 本地 SQLite 与 SQLAlchemy async](adrs/adr-002-local-sqlite-sqlalchemy.md) | 使用工作区本地 SQLite、SQLAlchemy async 与 aiosqlite。 | Accepted |
| [ADR-003: React 与 Vite 前端](adrs/adr-003-react-vite-frontend.md) | 使用 React、TypeScript、Vite 与 TanStack React Query。 | Accepted |
| [ADR-004: 可选 Langfuse v4 适配器](adrs/adr-004-langfuse-v4-observability.md) | 将 Langfuse v4 保持为结构化日志之外的可选观测适配层。 | Accepted |
| [ADR-005: 受信任 Docker 门禁](adrs/adr-005-trusted-docker-gate.md) | 使用受信任控制面与不受信候选边界验证真实 Linux/Docker 证据链。 | Accepted |

## Project Guides

| Guide | Purpose |
|---|---|
| [Project Pitfalls](guides/project-pitfalls.md) | 汇总安全边界、持久化、CI 事实与文档编码的高风险误区。 |
| [Testing Strategy](guides/testing-strategy.md) | 定义后端、前端、地图与容器验证的分层证据要求。 |
| [Workspace Runtime Patterns](guides/workspace-runtime-patterns.md) | 说明 workspace-first、loopback、本地持久化和可选外部系统模式。 |

## Package Manuals

当前没有独立包手册。现有依赖的项目级取舍由 ADR 记录；只有当某个外部 API 的调用约束超过 ADR 与指南的承载范围时，才应创建手册。

## Research

当前没有独立调研记录。本轮重写没有进行外部研究，也没有为现有决定追加无法由 Context Store 验证的市场或性能结论。

## Maintenance

**更新触发条件：**

- 技术选择被接受、替代、弃用或显著改变。
- 新增或删除 ADR、指南、手册或调研记录。
- project docs 与 reference 中的事实状态出现差异。

**Verification：**

- registry 中的 5 个 ADR 与 3 个指南链接全部可解析。
- manuals 与 research 的空态和目录实际内容一致。
- 不把计划、未运行验证或外部推测写成长久项目事实。

