<!-- SCOPE: 采用工作区本地 SQLite、SQLAlchemy async 与 aiosqlite 的持久化决策记录。 -->
<!-- DOC_KIND: record -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 修改数据库引擎、ORM、工作区持久化或 schema 生命周期前阅读。 -->
<!-- SKIP_WHEN: 只需要三张表的当前概览时跳过。 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json -->

# ADR-002: 本地 SQLite 与 SQLAlchemy async

## Quick Navigation

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-07-11 |
| Decision | 工作区本地 SQLite、SQLAlchemy async ≥2.0、aiosqlite ≥0.20。 |
| Related | [Reference Hub](../README.md)、[数据库结构](../../project/database_schema.md)、[架构](../../project/architecture.md)、[运行手册](../../project/runbook.md) |

## Agent Entry

| Signal | Guidance |
|---|---|
| Read When | 数据位置、数据库并发模型、ORM 或迁移语义可能变化。 |
| Preserve | 工作区本地持久化与 async 数据访问。 |
| Do Not Infer | 未核验的字段、索引、级联、备份或迁移命令。 |

## Context

Bridle 以单一本地工作区为运行边界，需要保存稳定项目身份、项目会话和会话消息，同时避免引入远程数据库服务作为本地使用前提。

## Decision

使用工作区本地 SQLite 保存 `projects`、`project_sessions` 与 `project_messages`，通过 SQLAlchemy async 和 aiosqlite 访问。启动采用当前 metadata creation 语义；项目目前没有活动的版本化迁移工作流。

## Rationale

| Reason | Project fit |
|---|---|
| 本地优先 | 数据与所选工作区绑定，不要求独立数据库服务。 |
| 异步一致性 | 与 FastAPI 后端的异步运行模型一致。 |
| 明确的持久化边界 | 三张表覆盖项目、会话与消息的核心本地状态。 |

## Alternatives

| Alternative | Trade-off |
|---|---|
| PostgreSQL 服务 | 更适合多用户和集中部署，但会增加服务、凭据和运维前提。 |
| 同步 ORM | 模型更简单，但会与当前异步后端链路形成两套并发语义。 |
| 直接使用 sqlite3 | 依赖更少，但关系映射和数据访问约束需要手工维护。 |

## Consequences

| Positive | Cost / obligation |
|---|---|
| 本地启动无需数据库服务。 | 多工作区、共享访问和生产扩展不能被默认假定。 |
| ORM 与异步 API 使用一致链路。 | 必须管理异步会话与事务生命周期。 |
| metadata creation 能建立当前结构。 | 它不是历史 schema 升级、数据转换或回滚链。 |

## Maintenance

**Last Updated:** 2026-07-11

**Update Triggers:** 数据库引擎、ORM、三表职责、工作区绑定或迁移机制变化。

**Verification：** 对照 Context Store 的 `DATABASE_TYPE`、`SCHEMA_OVERVIEW` 与数据库技术栈；本轮未运行数据库迁移或测试。
