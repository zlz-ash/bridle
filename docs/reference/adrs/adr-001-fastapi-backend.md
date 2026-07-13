<!-- SCOPE: 采用 FastAPI、Uvicorn、Pydantic 与 Typer 作为 Bridle 后端运行时的决策记录。 -->
<!-- DOC_KIND: record -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 修改后端框架、ASGI server、CLI 入口或 API 运行边界前阅读。 -->
<!-- SKIP_WHEN: 只需要当前 endpoint 表或启动命令时跳过。 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json -->

# ADR-001: FastAPI 后端运行时

## Quick Navigation

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-07-11 |
| Decision | FastAPI ≥0.115、Uvicorn ≥0.32、Pydantic ≥2.10、Typer ≥0.15。 |
| Related | [Reference Hub](../README.md)、[API 契约](../../project/api_spec.md)、[架构](../../project/architecture.md)、[技术栈](../../project/tech_stack.md) |

## Agent Entry

| Signal | Guidance |
|---|---|
| Read When | 后端框架、请求契约、CLI 启动或 loopback 边界可能变化。 |
| Preserve | `/api/v1` REST、SSE、workspace-first 本地运行和默认 loopback-only。 |
| Do Not Infer | Context Store 未记录的控制器实现、状态码或响应 schema。 |

## Context

Bridle 是本地优先的项目地图工作区运行时，需要同时承载项目、会话、工作区、事件和项目地图 API。后端还需与异步 SQLite 持久化、结构化日志、可选观测能力及 CLI 启动入口配合。

## Decision

使用 FastAPI 作为 API 框架、Uvicorn 作为 ASGI server、Pydantic 作为数据校验与契约基础，并由 Typer 提供 CLI 入口。服务通过 `/api/v1` 提供 REST，并包含 SSE；CLI 默认保持 loopback-only 本地服务边界。

## Rationale

| Reason | Project fit |
|---|---|
| 异步运行模型 | 与 SQLAlchemy async、aiosqlite 和事件流方向一致。 |
| 类型化契约 | 与 Pydantic 驱动的本地 API 边界一致。 |
| 本地启动路径 | Uvicorn 与 Typer 能组成清晰的 CLI 到 ASGI 运行链。 |

## Alternatives

| Alternative | Trade-off |
|---|---|
| Flask | 更轻量，但异步、类型化 schema 与 API 契约需要更多项目级约束。 |
| Django | 提供完整平台能力，但对当前本地优先工作区 API 的范围更重。 |
| 直接使用 Starlette | 运行层更薄，但会放弃 FastAPI 与 Pydantic 的集成契约。 |

## Consequences

| Positive | Cost / obligation |
|---|---|
| 后端 API、校验和异步链路使用一致的 Python 运行模型。 | 框架或 Pydantic 升级必须同步检查 API 契约。 |
| CLI 可明确提供工作区、host 与 port。 | 没有应用认证时必须维持默认 loopback-only。 |
| FastAPI 可承载 REST 与 SSE。 | 不能把运行时可能生成的 schema 当成已经在文档中核验的响应契约。 |

## Maintenance

**Last Updated:** 2026-07-11

**Update Triggers:** FastAPI、Uvicorn、Pydantic、Typer、API 前缀、SSE 或网络边界变化。

**Verification：** 对照 Context Store 的后端技术栈、API 类型与认证边界；本轮文档重写未执行产品测试或 CI。
