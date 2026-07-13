<!-- SCOPE: 将 Langfuse v4 作为 Bridle 结构化日志之外可选观测适配层的决策记录。 -->
<!-- DOC_KIND: record -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 修改观测开关、Langfuse 版本、外部 trace 发送或日志降级策略前阅读。 -->
<!-- SKIP_WHEN: 只需要本地日志字段或 API 路径时跳过。 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json -->

# ADR-004: 可选 Langfuse v4 观测适配器

## Quick Navigation

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-07-11 |
| Decision | 结构化项目日志为基础，Langfuse v4 作为可选外部适配器。 |
| Related | [Reference Hub](../README.md)、[架构](../../project/architecture.md)、[技术栈](../../project/tech_stack.md)、[运行手册](../../project/runbook.md) |

## Agent Entry

| Signal | Guidance |
|---|---|
| Read When | 外部 trace、观测开关、Langfuse host/keys 或失败降级变化。 |
| Preserve | 关闭或不可用 Langfuse 时，本地核心运行与结构化日志仍成立。 |
| Do Not Expose | `LANGFUSE_SECRET_KEY`、代理密钥或本地环境文件值。 |

## Context

Bridle 需要完整的本地日志流程，同时可在配置允许时把观测数据发送到 Langfuse v4。由于项目是本地优先运行时，外部观测不能成为核心项目、会话、地图或容器能力的启动前提。

## Decision

保留结构化项目日志作为基础观测面，通过 `BRIDLE_OBSERVABILITY_ENABLED` 控制可选 Langfuse v4 适配；连接信息由 `LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY` 与 `LANGFUSE_HOST` 提供。任何秘密仅来自运行环境，不进入文档或仓库。

## Rationale

| Reason | Project fit |
|---|---|
| 本地优先 | 未配置外部服务时仍可运行并保留本地诊断。 |
| 明确适配边界 | v4 语义被隔离在可选适配层，不扩散到业务模块。 |
| 配置可审计 | 启用开关、host 与 keys 的职责分离。 |

## Alternatives

| Alternative | Trade-off |
|---|---|
| 完全不接外部观测 | 边界最简单，但失去集中 trace 分析能力。 |
| 将 Langfuse 设为强依赖 | 外部观测更一致，但会破坏本地优先和离线可用性。 |
| 直接在业务模块调用 SDK | 初期接入快，但会让 v4 API 和失败处理耦合到业务逻辑。 |

## Consequences

| Positive | Cost / obligation |
|---|---|
| 外部观测可按环境启用。 | 必须维持 adapter 与 SDK v4 的兼容边界。 |
| 本地日志不依赖外部网络。 | 外部发送失败与本地业务失败要清楚区分。 |
| 密钥由运行环境注入。 | 日志、文档和错误输出必须持续避免泄密。 |

## Maintenance

**Last Updated:** 2026-07-11

**Update Triggers:** Langfuse 主版本、适配器、观测开关、host/key 变量或失败策略变化。

**Verification：** 对照 Context Store 的 observability 与环境变量名；不得读取或记录本地环境文件的值。本轮未执行外部 Langfuse 连接测试。
