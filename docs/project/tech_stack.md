<!-- SCOPE: Bridle 当前采用的运行时、框架、存储、测试与辅助技术 -->
<!-- DOC_KIND: reference -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 安装依赖、评估升级、排查版本兼容或选择实现入口时 -->
<!-- SKIP_WHEN: 只需要业务需求或架构运行链路时 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json, backend/pyproject.toml, frontend/package.json -->

# 技术栈

## Quick Navigation

| 类别 | 入口 |
|---|---|
| 后端 | [Backend Technologies](#backend-technologies) |
| 前端 | [Frontend Technologies](#frontend-technologies) |
| 数据库 | [Database](#database) |
| 测试、容器与观测 | [Additional Technologies](#additional-technologies) |
| 开发命令 | [Development Commands](#development-commands) |
| 升级策略 | [Version and Upgrade Strategy](#version-and-upgrade-strategy) |

## Agent Entry

本文只记录 Context Store 已确认的技术与版本。未被 Context Store 固定的底层版本使用“随运行时提供”或“未在项目资料中固定”的中性描述，不推测精确版本。架构原因以 ADR 为准，命令以项目配置为准。

## Backend Technologies

| 技术 | 版本 | 用途 | 选择理由 | ADR |
|---|---|---|---|---|
| Python | `>=3.12` | 后端运行时 | async 生态、类型工具与项目本地 CLI 适合单 workspace 服务。 | [ADR-001](../reference/adrs/adr-001-fastapi-backend.md) |
| FastAPI | `>=0.115` | REST API 与依赖注入边界 | Pydantic 集成和 async 路由适合按功能域组织接口。 | [ADR-001](../reference/adrs/adr-001-fastapi-backend.md) |
| Uvicorn | `>=0.32` | ASGI server | 为 FastAPI 提供本地 loopback 服务入口。 | [ADR-001](../reference/adrs/adr-001-fastapi-backend.md) |
| Pydantic | `>=2.10` | 请求、响应和配置校验 | 统一 schema 与结构化 validation error。 | [ADR-001](../reference/adrs/adr-001-fastapi-backend.md) |
| Typer | `>=0.15` | `bridle` CLI | 用类型签名表达 workspace、host、port 等启动参数。 | [ADR-001](../reference/adrs/adr-001-fastapi-backend.md) |

后端还使用 Rich、sse-starlette、python-dotenv、httpx、tree-sitter、tree-sitter-python 与 tree-sitter-typescript。Context Store 确认这些依赖存在，但没有固定其精确版本，因此本文不制造版本号。

## Frontend Technologies

| 技术 | 版本 | 用途 | 选择理由 | ADR |
|---|---|---|---|---|
| React | `18.3.1` | 项目地图、会话与检查器 UI | 组件组合与 hook 模型适合持续同步本地运行时状态。 | [ADR-003](../reference/adrs/adr-003-react-vite-frontend.md) |
| TypeScript | `5.7.2` | 前端语言 | 为 API schema、地图层和 hook 状态提供静态约束。 | [ADR-003](../reference/adrs/adr-003-react-vite-frontend.md) |
| Vite | `5.4.11` | 开发服务与构建 | 提供本地快速开发入口和 `/api` 代理。 | [ADR-003](../reference/adrs/adr-003-react-vite-frontend.md) |
| TanStack React Query | `5.62.7` | 服务端状态 | 管理请求生命周期、缓存与失效，避免 UI 直接持有远端状态。 | [ADR-003](../reference/adrs/adr-003-react-vite-frontend.md) |
| Vitest | `2.1.8` | jsdom 前端测试 | 与 Vite 配置和 TypeScript 模块边界一致。 | [ADR-003](../reference/adrs/adr-003-react-vite-frontend.md) |

Axios、Geist Variable 与 Geist Mono Variable 也在前端依赖中；Context Store 未给出其精确版本。自定义 `brd-*` 组件系统位于 `frontend/src/components/ds`，token 位于 `frontend/src/styles/tokens`。

## Database

| 技术 | 版本 | 用途 | 选择理由 | ADR |
|---|---|---|---|---|
| SQLite | 随 Python 运行时提供，项目未单独固定 | workspace-local 数据库 | 单机、单 workspace 场景无需独立数据库服务，便于本地部署。 | [ADR-002](../reference/adrs/adr-002-local-sqlite-sqlalchemy.md) |
| SQLAlchemy async | `>=2.0` | ORM 与异步数据访问 | 为 projects、project_sessions、project_messages 提供统一模型与会话边界。 | [ADR-002](../reference/adrs/adr-002-local-sqlite-sqlalchemy.md) |
| aiosqlite | `>=0.20` | SQLite async driver | 使 FastAPI async 路由不需要同步数据库适配层。 | [ADR-002](../reference/adrs/adr-002-local-sqlite-sqlalchemy.md) |

当前没有活动迁移工作流；启动使用 metadata creation 语义。Alembic 是后端依赖之一，但不能据此宣称迁移链已经启用。

## Additional Technologies

| 类别 | 技术与版本 | 用途 | 选择理由 |
|---|---|---|---|
| 后端测试 | pytest `>=8.0`、pytest-asyncio `>=0.25` | API、CLI、Agent、地图与容器合同测试 | 支持同步和 async 测试，并能按目录形成稳定门禁范围。 |
| 前端测试 | Vitest `2.1.8`、Testing Library（版本未在 Context Store 固定） | hooks 与组件行为 | 从用户可观察行为验证 React 组件和同步逻辑。 |
| 容器 | Linux runner 与 Docker（项目资料未固定 daemon 版本） | 真实镜像执行与 trusted evidence gate | 只有真实 daemon 能验证镜像身份、隔离和 evidence 链。 |
| 可观测 | 结构化项目日志、Langfuse v4 adapter | 本地诊断与可选外部 trace | 保持业务控制面独立，同时允许审计关键阶段。 |
| 代码分析 | tree-sitter Python/TypeScript bindings（版本未固定） | 项目地图代码实体与关系分析 | 支持多语言 AST 级索引。 |
| 事件 | sse-starlette（版本未固定） | `/api/v1/events` server-sent events | 为浏览器提供单向实时事件通道。 |

## Development Commands

| 任务 | 命令 | 预期用途 |
|---|---|---|
| 安装后端 | `python -m pip install -e backend` | 以 editable 模式安装 `bridle`。 |
| 启动后端 | `bridle serve --workspace <path> --host 127.0.0.1 --port 8900` | 启动指定 workspace 的本地服务。 |
| 后端测试 | `cd backend; pytest` | 运行完整后端 pytest。 |
| 后端 lint | `cd backend; ruff check src tests` | 检查后端源码与测试。 |
| 前端开发 | `cd frontend; npm run dev` | 启动 Vite 开发服务。 |
| 前端构建 | `cd frontend; npm run build` | 类型检查并构建前端产物。 |
| 前端测试 | `cd frontend; npm test -- --run` | 非 watch 模式运行 Vitest。 |

## Configuration

| 边界 | 主要变量 |
|---|---|
| Workspace | `BRIDLE_WORKSPACE` |
| Agent | `BRIDLE_AGENT_PROVIDER`, `BRIDLE_AGENT_MODEL`, `BRIDLE_AGENT_API_KEY`, `BRIDLE_AGENT_BASE_URL`, `BRIDLE_AGENT_BETA_BASE_URL` |
| Observability | `BRIDLE_OBSERVABILITY_ENABLED`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` |
| Network proxy | `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` |
| Container | `BRIDLE_CONTAINER_RUNNER`, `BRIDLE_CONTAINER_DRY_RUN` |

配置文档和日志不得输出 API key、secret 或完整对话内容。本机需要网络下载或终端连接时使用项目约定的 `7890` 代理端口。

## Version and Upgrade Strategy

| 策略 | 规则 |
|---|---|
| 已固定前端版本 | React、TypeScript、Vite、React Query 与 Vitest 升级时一起验证构建、hooks 和组件测试。 |
| 后端下界 | Python、FastAPI、Uvicorn、Pydantic、Typer、SQLAlchemy、aiosqlite、pytest 与 pytest-asyncio 以声明下界为兼容基线。 |
| 未固定依赖 | 不在文档中猜测版本；升级评估以实际依赖清单和安装解析结果为准。 |
| 架构级升级 | 改变后端框架、数据库模型、前端框架、观测适配或容器信任边界时先更新对应 ADR。 |
| 验证要求 | 只有实际运行构建或测试并保留记录后，才能在 [implementation_status.md](implementation_status.md) 标记 Verified。 |

## Maintenance

**Last Updated:** 2026-07-11

**Update Triggers:**

- 后端依赖下界、前端精确版本或 Node/Python 主版本变化。
- 新增数据库、缓存、消息队列、搜索、文件存储或部署服务。
- Docker、Langfuse、tree-sitter 或 SSE 从可选边界变成业务关键路径。

**Verification:**

- 版本只来自 Context Store 或当前依赖配置，不从记忆补全。
- 每个核心技术说明用途、理由和适用 ADR。
- 命令保持可复制；运行前仍按项目规则预估耗时。
