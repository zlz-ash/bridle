<!-- SCOPE: 采用 React、TypeScript、Vite 与 TanStack React Query 构建 Bridle 前端的决策记录。 -->
<!-- DOC_KIND: record -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 修改前端框架、构建工具、服务端状态管理或地图同步模式前阅读。 -->
<!-- SKIP_WHEN: 只需要视觉规范或单个组件行为时跳过。 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json -->

# ADR-003: React 与 Vite 前端

## Quick Navigation

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-07-11 |
| Decision | React 18.3.1、TypeScript 5.7.2、Vite 5.4.11、TanStack React Query 5.62.7。 |
| Related | [Reference Hub](../README.md)、[设计规范](../../project/design_guidelines.md)、[架构](../../project/architecture.md)、[技术栈](../../project/tech_stack.md) |

## Agent Entry

| Signal | Guidance |
|---|---|
| Read When | 前端框架、构建、数据获取、地图同步或测试环境变化。 |
| Preserve | React 组件模型、TypeScript 契约、Vite 构建与 hook-based map synchronization。 |
| Do Not Infer | 未在 Context Store 中确认的路由框架、SSR 或部署平台。 |

## Context

Bridle 前端需要呈现项目地图与会话 UI，并以 hook 方式同步后端地图状态。项目还需要快速本地开发、类型化接口消费和可重复的 jsdom 组件测试环境。

## Decision

使用 React 与 TypeScript 构建 UI，Vite 负责本地开发和构建，TanStack React Query 管理服务端状态，Vitest 与 Testing Library 承担前端测试。

## Rationale

| Reason | Project fit |
|---|---|
| 组件化交互 | 适合地图、检查器、会话卡片与设计系统组件的组合。 |
| 服务端状态分离 | React Query 能将远程项目/地图数据与局部 UI 状态区分。 |
| 本地反馈速度 | Vite 与 Vitest 共享现代 TypeScript 工具链。 |

## Alternatives

| Alternative | Trade-off |
|---|---|
| Vue + Vite | 同样适合 SPA，但会替换现有 React 组件、hooks 与测试生态。 |
| Angular | 提供更完整约束，但对当前定制组件系统和本地工具 UI 更重。 |
| Next.js | 适合服务端渲染和全栈路由，但 Context Store 未显示 Bridle 需要这些能力。 |

## Consequences

| Positive | Cost / obligation |
|---|---|
| 地图和会话 UI 可按组件与 hooks 分层。 | hooks 与查询缓存必须保持明确的同步和失效语义。 |
| 前端使用一致的 TypeScript 构建/测试链。 | 版本升级需同时验证 Vite、Vitest 与 jsdom 兼容性。 |
| 服务端状态由 React Query 统一处理。 | 不能把所有 UI 状态都塞入远程查询缓存。 |

## Maintenance

**Last Updated:** 2026-07-11

**Update Triggers:** React、TypeScript、Vite、React Query、Vitest、地图同步或构建模式变化。

**Verification：** 对照 Context Store 的 frontend 与 testing 技术栈；本轮文档重写未执行 `npm test` 或 `npm run build`。
