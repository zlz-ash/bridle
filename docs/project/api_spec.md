# API 契约：Bridle

<!-- SCOPE: 当前 Bridle FastAPI 应用通过 /api/v1 暴露的 REST 与服务器发送事件契约。 -->
<!-- DOC_KIND: reference -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 需要确认 API 方法、路径、资源分组、认证边界或已知请求/响应契约时阅读。 -->
<!-- SKIP_WHEN: 只需要 SQLite 表结构、前端行为或控制器实现时跳过。 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json -->

> **状态：** 当前事实基线  
> **最后更新：** 2026-07-11

## Quick Navigation

- [文档中心](../README.md)
- [架构](architecture.md)
- [技术栈](tech_stack.md)
- [数据库结构](database_schema.md)
- [运行手册](runbook.md)

## Agent Entry

| 信号 | 内容 |
|---|---|
| 用途 | 提供 OpenAPI 3.x 风格的 HTTP 契约索引，不描述控制器、服务或校验类实现。 |
| 何时阅读 | 客户端需要选择方法、路径，或判断本地服务安全边界时。 |
| 何时跳过 | 需要字段级实现、内部调用链或数据库持久化细节时。 |
| 规范性 | 是；仅包含 Context Store 已确认的 API 事实。 |
| 下一步 | [数据库结构](database_schema.md)、[架构](architecture.md)、[运行手册](runbook.md) |
| 事实来源 | `.ai-dev/docs/ln-110/context-store.json` |

## 1. Contract Metadata

| 属性 | 当前契约 |
|---|---|
| 风格 | REST，基础前缀为 `/api/v1`。 |
| 事件传输 | 服务器发送事件（SSE）。 |
| 本地 Base URL | `http://127.0.0.1:8900/api/v1`。 |
| 应用认证 | 未实现应用层认证或授权。 |
| 默认暴露边界 | CLI 默认强制 loopback-only 本地服务边界。 |
| 文档结构 | 使用 OpenAPI 3.x 的路径、操作、参数与响应分区；Context Store 未提供的 schema 不予推测。 |

没有应用认证并不表示 API 可安全公开。调用方必须把 `127.0.0.1` 回环边界视为当前安全前提。

## 2. Endpoint Groups

| 分组 | 已确认入口 | 作用 |
|---|---|---|
| Health | `GET /api/v1/health` | 本地服务健康检查。 |
| Workspace | `/api/v1/workspace/*` | 工作区文件与概览。 |
| Projects | `/api/v1/projects*` | 项目列举、打开与重扫。 |
| Sessions | `/api/v1/sessions*` | 会话、角色、消息、对话与能力。 |
| Project Map | `/api/v1/projects/{project_id}/map*` | 项目地图读取、变更与语义/执行刷新。 |
| Events | `GET /api/v1/events` | SSE 事件入口。 |

## 3. Confirmed Paths

### 3.1 Health & Workspace

| Method | Path | Contract intent |
|---|---|---|
| `GET` | `/api/v1/health` | 查询服务健康状态。 |
| `GET` | `/api/v1/workspace/files` | 查询当前工作区文件。 |
| `GET` | `/api/v1/workspace/overview` | 查询当前工作区概览。 |

### 3.2 Projects

| Method | Path | Contract intent |
|---|---|---|
| `GET` | `/api/v1/projects` | 列举项目。 |
| `POST` | `/api/v1/projects/open` | 打开或登记项目。 |
| `POST` | `/api/v1/projects/{project_id}/rescan` | 重新扫描指定项目。 |

### 3.3 Sessions

| Method | Path | Contract intent |
|---|---|---|
| `POST` | `/api/v1/sessions` | 创建会话。 |
| `GET` | `/api/v1/sessions` | 查询会话。 |
| `POST` | `/api/v1/sessions/{session_id}/role` | 更新会话角色。 |
| `POST` | `/api/v1/sessions/{session_id}/messages` | 向会话写入消息。 |
| `POST` | `/api/v1/sessions/{session_id}/converse` | 发起会话对话。 |
| `GET` | `/api/v1/sessions/{session_id}/messages` | 查询会话消息。 |
| `GET` | `/api/v1/sessions/{session_id}/capabilities` | 查询会话能力。 |

### 3.4 Project Map

所有下列后缀均位于 `/api/v1/projects/{project_id}/map` 下。

| Method | Suffix | Contract intent |
|---|---|---|
| `PATCH` | 根路径 | 修改项目地图。 |
| `GET` | `/overview` | 查询地图概览。 |
| `GET` | `/children` | 查询子节点。 |
| `GET` | `/node` | 查询节点。 |
| `GET` | `/search` | 搜索地图。 |
| `GET` | `/subgraph` | 查询子图。 |
| `GET` | `/changes` | 查询地图变化。 |
| `GET` | `/path-slice` | 查询路径切片。 |
| `GET` | `/code-relations` | 查询代码关系。 |
| `GET` | `/semantic-annotations` | 查询语义标注。 |
| `GET` | `/code-entities` | 查询代码实体。 |
| `GET` | `/blind-spots` | 查询盲点。 |
| `GET` | `/boundaries` | 查询边界。 |
| `POST` | `/semantic-map/refresh` | 刷新语义地图。 |
| `POST` | `/execution-refresh` | 刷新执行态地图。 |

Context Store 还确认了模块候选、接口候选、mock 与仲裁资源包含 `GET` 和 `POST` 操作，但没有保存其逐条子路径。本文不从旧文档或实现代码补猜这些路径。

### 3.5 Events

| Method | Path | Transport |
|---|---|---|
| `GET` | `/api/v1/events` | SSE。 |

Context Store 未提供事件名称、字段或重连语义，因此客户端不得依赖本文未列出的事件 payload。

## 4. Parameters, Requests & Responses

### 4.1 Path parameters

| Parameter | Applies to | Meaning |
|---|---|---|
| `project_id` | 项目重扫与所有 project-map 路径 | 目标项目标识。 |
| `session_id` | 会话角色、消息、对话与能力路径 | 目标会话标识。 |

### 4.2 Request example

以下示例只展示已确认的方法与 URL，不声明未知 header 或 body：

```text
GET http://127.0.0.1:8900/api/v1/health
```

`POST` 与 `PATCH` 操作的请求体 schema 未记录在 Context Store。调用方应从当前运行时契约或后续经过源码核验的文档取得 schema，不能从操作名称推断字段。

### 4.3 Response contract

| Area | Current documented contract |
|---|---|
| Status codes | Context Store 未提供逐操作状态码。 |
| JSON bodies | Context Store 未提供响应字段 schema。 |
| Error envelope | Context Store 未提供统一错误 envelope。 |
| SSE payload | Context Store 只确认传输类型，未提供事件 schema。 |

因此本文不提供虚构的 JSON 响应示例。任何新增字段、状态码或错误码都必须先进入可验证事实来源。

## 5. Security Boundary

| Concern | Current behavior |
|---|---|
| Authentication | 无应用层认证。 |
| Authorization | 无已确认的应用层授权契约。 |
| Network boundary | CLI 默认只允许 loopback 本地服务。 |
| Public exposure | 当前契约不支持直接公网暴露。 |
| Secrets | 不从 `backend/.env` 或其他本地环境文件读取、复制或发布值。 |

## Maintenance

**Last Updated:** 2026-07-11

**Update Triggers:**

- `/api/v1` 路径、HTTP 方法、SSE 入口或路径参数变化。
- 应用认证、授权或 loopback-only 边界变化。
- 请求、响应、错误或事件 schema 被纳入经过验证的 Context Store。
- 模块候选、接口候选、mock 或仲裁资源的逐条路径得到正式核验。

**Verification：**

- 逐项对照 `.ai-dev/docs/ln-110/context-store.json` 的 `API_TYPE`、`API_ENDPOINTS` 与 `AUTH_SCHEME`。
- 不使用旧控制器文档补齐 Context Store 未保存的路径或 schema。
- 不读取或记录 `backend/.env` 的任何值。
- 不把计划中的 CI 门禁或未来 API 描述成当前已实现契约。
