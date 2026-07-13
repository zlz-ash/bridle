# 运行手册：Bridle

<!-- SCOPE: 本地环境准备、启动、测试、构建、容器门禁运行边界、健康检查与故障处理。 -->
<!-- DOC_KIND: how-to -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 需要在本地启动、验证或排查 Bridle，或确认容器 CI 操作边界时阅读。 -->
<!-- SKIP_WHEN: 只需要静态拓扑、端口或 CI 现状清单时跳过，改读 infrastructure.md。 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json -->

> **文档版本：** 2.0  
> **状态：** 当前事实基线  
> **日期：** 2026-07-11

## Quick Navigation

- [文档中心](../README.md)
- [基础设施](infrastructure.md)
- [架构](architecture.md)
- [技术栈](tech_stack.md)
- [API 规范](api_spec.md)
- [数据库结构](database_schema.md)

## Agent Entry

| 信号 | 内容 |
|---|---|
| 用途 | 提供可执行的本地准备、启动、测试、构建、健康检查和排障步骤。 |
| 何时阅读 | 需要运行命令或判断 CI/容器操作是否在当前支持范围内时。 |
| 何时跳过 | 只需要服务器、端口、域名、部署服务或门禁现状清单时。 |
| 规范性 | 是；运维步骤以本文为文档入口。 |
| 下一步 | [基础设施](infrastructure.md)、[架构](architecture.md)、[技术栈](tech_stack.md) |
| 事实来源 | `.ai-dev/docs/ln-110/context-store.json` |

## 1. Local Development Setup

### 1.1 前置条件

| 工具或条件 | 要求 | 用途 |
|---|---|---|
| Python | 3.12 或更高版本 | 后端安装、服务与 pytest。 |
| Node.js 与 npm | 与 Vite 5 兼容 | 前端开发、构建与 Vitest。 |
| Docker daemon | 仅真实容器执行和 Linux 容器门禁需要 | 不用于 Compose 启动；仓库当前没有 Compose 服务。 |
| 工作区 | `D:\代码仓\Bridle-dev` | 所有下载、生成物和本地持久化均保留在 D 盘。 |
| 网络代理 | `http://127.0.0.1:7890` | 涉及网络下载或终端网络连接时使用。 |

当前没有可发布的生产账号、SSH 入口或运维联系人清单。

### 1.2 网络操作前设置代理

仅在命令需要联网时，在当前 PowerShell 会话中设置代理：

```shell
$env:HTTP_PROXY='http://127.0.0.1:7890'
$env:HTTPS_PROXY='http://127.0.0.1:7890'
```

不要把下载缓存、临时构建目录或持久化数据改写到 C 盘。

### 1.3 安装后端

从仓库根目录执行：

```shell
python -m pip install -e backend
```

Context Store 未登记独立的前端安装命令。执行前端命令前，应确保 `frontend` 工作区依赖已经准备好；不要在本文中臆造包管理流程。

### 1.4 启动后端

```shell
bridle serve --workspace 'D:\代码仓\Bridle-dev' --host 127.0.0.1 --port 8900
```

服务启动后，后端入口为 `http://127.0.0.1:8900`。应用没有认证层，因此不要把默认回环绑定改成公网监听。

### 1.5 启动前端

在另一个 PowerShell 会话中执行：

```shell
Set-Location frontend
npm run dev
```

前端开发端口为 `5173`。需要完整 UI 联调时，先确认 `8900` 后端健康，再打开前端开发入口。

## 2. Environment Variables

Context Store 只确认了变量名，没有提供可公开的取值或必填性。密钥不得写入仓库；代理变量只在需要网络访问的进程中设置。

| 变量 | 用途 |
|---|---|
| `BRIDLE_WORKSPACE` | 指定 Bridle 工作区路径；当前工作区在 D 盘。 |
| `BRIDLE_AGENT_PROVIDER` | 选择代理模型提供方。 |
| `BRIDLE_AGENT_MODEL` | 指定代理模型。 |
| `BRIDLE_AGENT_API_KEY` | 提供方 API 密钥；按敏感信息处理。 |
| `BRIDLE_AGENT_BASE_URL` | 覆盖代理提供方基础地址。 |
| `BRIDLE_AGENT_BETA_BASE_URL` | 覆盖代理提供方 beta 基础地址。 |
| `BRIDLE_OBSERVABILITY_ENABLED` | 控制可选观测能力是否启用。 |
| `LANGFUSE_PUBLIC_KEY` | Langfuse v4 公钥。 |
| `LANGFUSE_SECRET_KEY` | Langfuse v4 密钥；按敏感信息处理。 |
| `LANGFUSE_HOST` | Langfuse v4 服务地址。 |
| `HTTP_PROXY` | HTTP 网络代理；本机约束为 `http://127.0.0.1:7890`。 |
| `HTTPS_PROXY` | HTTPS 网络代理；本机约束为 `http://127.0.0.1:7890`。 |
| `NO_PROXY` | 指定不经过代理的地址；本地回环访问应保持直连。 |
| `BRIDLE_CONTAINER_RUNNER` | 选择容器运行器。 |
| `BRIDLE_CONTAINER_DRY_RUN` | 控制容器运行的 dry-run 行为。 |

## 3. Testing & Build

执行每条命令前先向用户说明预计耗时；如果超过预估，检查执行逻辑，无法解决时报告阻塞。

### 3.1 后端验证

```shell
Set-Location backend; pytest
```

```shell
Set-Location backend; ruff check src tests
```

### 3.2 前端验证

```shell
Set-Location frontend; npm test -- --run
```

```shell
Set-Location frontend; npm run build
```

上述命令分别对应已确认的 pytest、Ruff、Vitest 和 Vite 工具链。若命令涉及首次下载依赖，先设置 `7890` 代理，并确保缓存或临时路径位于 D 盘工作区内。

## 4. Container CI Gate

当前唯一已登记的 CI workflow 是 [`.github/workflows/container-docker-linux.yml`](../../.github/workflows/container-docker-linux.yml)。它在 Linux runner 上执行受信任的基础/默认 harness、候选代码 staging、镜像摘要、隔离 Docker 测试和 evidence 校验。

操作边界：

- 本地普通测试不等同于真实 Docker evidence 门禁。
- 真实门禁需要 Linux runner 和可用 Docker daemon。
- 仓库没有 Docker Compose 服务，不要使用 `docker compose up` 作为 Bridle 的启动步骤。
- 地图门禁当前尚未存在；目标范围虽然已确认，但只有实际 workflow 生成并完成评审后才可作为 CI 操作入口。

容器镜像定义见 [`backend/src/bridle/agent/container/agent.Dockerfile`](../../backend/src/bridle/agent/container/agent.Dockerfile)、[`scripts/ci/protected/agent.Dockerfile`](../../scripts/ci/protected/agent.Dockerfile) 和 [`scripts/ci/protected/worker.Dockerfile`](../../scripts/ci/protected/worker.Dockerfile)。本文不复制 Dockerfile 或 workflow 实现。

## 5. Deployment

当前没有生产部署目标、远程服务器、域名、制品仓库或 Compose 服务，因此没有可执行的生产部署、重启或回滚步骤。[`.github/workflows/container-docker-linux.yml`](../../.github/workflows/container-docker-linux.yml) 是验证门禁，不是部署流水线。

在部署目标被正式加入 Context Store 前，不要：

- 把本地 `8900` 或 `5173` 端口描述成生产端口；
- 把容器门禁镜像描述成已发布制品；
- 臆造 SSH、DNS、TLS、备份或回滚命令。

## 6. Health Checks

后端启动后，在新的 PowerShell 会话中执行：

```shell
Invoke-RestMethod 'http://127.0.0.1:8900/api/v1/health'
```

健康检查失败时，先确认后端进程仍在运行且监听 `127.0.0.1:8900`，再检查端口占用和启动日志。前端联调还需确认 `5173` 开发进程正在运行。

## 7. Troubleshooting

### 7.1 端口被占用

```shell
Get-NetTCPConnection -LocalPort 8900,5173 -ErrorAction SilentlyContinue
```

确认占用进程后，由用户决定是否停止；不要擅自终止未知进程。后端端口变更时需同步前端连接配置与本文。

### 7.2 后端无法启动

| 检查项 | 处理 |
|---|---|
| Python 版本 | 确认使用 Python 3.12 或更高版本。 |
| 可编辑安装 | 在仓库根目录重新执行已登记的后端安装命令。 |
| 工作区路径 | 使用 D 盘绝对路径，并确认进程对工作区可读写。 |
| 绑定地址 | 本地默认保持 `127.0.0.1`，不要为排障改成公网监听。 |

### 7.3 前端启动或构建失败

先确认当前目录为 `frontend`，Node.js 与 npm 兼容 Vite 5，并检查依赖是否已准备。若需要联网恢复依赖，使用 `7890` 代理，且下载缓存和临时目录不得落到 C 盘。

### 7.4 模型提供方或观测连接失败

核对对应的 `BRIDLE_AGENT_*`、`BRIDLE_OBSERVABILITY_ENABLED` 与 `LANGFUSE_*` 变量。密钥只通过运行环境注入，不写入日志、文档或提交内容。涉及外部连接时同时核对代理变量。

### 7.5 容器 evidence 门禁失败

按 workflow 日志区分 Docker daemon、镜像构建/摘要、候选 staging、隔离测试与 evidence 校验阶段。不要用普通单元测试通过替代真实 Linux/Docker 证据链通过。

### 7.6 SQLite 与工作区状态

SQLite 使用工作区本地持久化，启动采用当前 metadata creation 语义，仓库没有活动迁移流程。Context Store 未登记备份/恢复命令，因此不要执行推测性的数据库删除、迁移或恢复操作。

## 8. Service Dependencies & Port Mapping

| 项目 | 当前状态 |
|---|---|
| Compose 服务依赖 | 无；Docker 服务清单为空。 |
| Compose 端口映射 | 无。 |
| 本地后端 | `127.0.0.1:8900`。 |
| 本地前端 | `5173`。 |
| 外部可选依赖 | 模型提供方、Langfuse v4、真实容器执行所需 Docker daemon。 |

## Maintenance

**Last Updated:** 2026-07-11

**Update Triggers:**

- 本地启动、测试、构建命令或端口发生变化。
- 环境变量、代理规则或 D 盘存储约束发生变化。
- 新增 Docker Compose、生产部署目标、备份/恢复流程或运维联系人。
- 容器 workflow 或地图门禁状态发生变化。

**Verification：**

- 每条命令都能追溯到 `.ai-dev/docs/ln-110/context-store.json` 中的当前命令或运行边界。
- 长耗时命令执行前已说明预估时间；联网命令使用 `7890` 代理。
- 文档没有把 Compose、生产部署或地图门禁描述成当前已存在的能力。
- 环境变量表包含 Context Store 登记的全部变量，且没有公开密钥值。
