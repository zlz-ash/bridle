# 基础设施：Bridle

<!-- SCOPE: 部署拓扑、主机与网络约束、端口、已部署服务、制品与 CI/CD 现状。 -->
<!-- DOC_KIND: explanation -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 需要确认 Bridle 的运行位置、端口、容器资产或当前 CI 门禁时阅读。 -->
<!-- SKIP_WHEN: 只需要本地启动、测试或故障处理步骤时跳过，改读 runbook.md。 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json -->

> **状态：** 当前事实基线  
> **最后更新：** 2026-07-11

## Quick Navigation

- [文档中心](../README.md)
- [架构](architecture.md)
- [技术栈](tech_stack.md)
- [运行手册](runbook.md)

## Agent Entry

| 信号 | 内容 |
|---|---|
| 用途 | 说明本地运行拓扑、端口、容器资产和 CI/CD 现状。 |
| 何时阅读 | 判断服务在哪里运行、需要什么主机能力、哪些门禁已经存在时。 |
| 何时跳过 | 需要执行启动、测试、排障命令时。 |
| 规范性 | 是；基础设施现状以本文为文档入口。 |
| 下一步 | [运行手册](runbook.md)、[架构](architecture.md)、[技术栈](tech_stack.md) |
| 事实来源 | `.ai-dev/docs/ln-110/context-store.json` |

## 1. Server Inventory

Bridle 当前按单一本地工作区运行，Context Store 未记录远程服务器、生产主机或托管部署目标。

| 环境 | 位置 | 角色 | 边界 |
|---|---|---|---|
| 本地开发工作区 | `D:\代码仓\Bridle-dev` | FastAPI 后端、React/Vite 前端、工作区本地 SQLite | 后端默认使用回环地址；下载和存储不得改写到 C 盘。 |

没有可发布的 SSH、CPU、内存或磁盘规格清单。引入远程主机后，应先更新 Context Store，再更新本文。

## 2. Domain & DNS

当前没有域名、DNS、反向代理或 TLS 终止配置。Bridle 的已确认边界是本地工作区运行，不应从本文推断公网入口。

## 3. Port Allocation

以下端口属于本地开发进程，不是 Docker Compose 端口映射。

| 端口 | 进程 | 协议 | 作用域 |
|---:|---|---|---|
| `8900` | Bridle FastAPI/Uvicorn 后端 | HTTP/TCP | 默认绑定 `127.0.0.1`，承载 `/api/v1` 接口。 |
| `5173` | Vite 前端开发服务器 | HTTP/TCP | 本地前端开发入口。 |

仓库当前没有 Docker Compose 服务，因此也没有 Compose 声明的服务端口映射。

## 4. Deployed Services

Context Store 中的 Docker 服务清单为空；当前没有可列为“已部署”的 Compose 服务。后端、前端和 SQLite 都是本地开发工作区中的运行时组成，不等同于生产部署服务。

### 容器资产

仓库包含以下 Dockerfile，用于代理容器执行和受保护的 Linux CI 证据链；它们不是 Compose 服务。

| 资产 | 用途边界 |
|---|---|
| [`backend/src/bridle/agent/container/agent.Dockerfile`](../../backend/src/bridle/agent/container/agent.Dockerfile) | 代理容器运行镜像定义。 |
| [`scripts/ci/protected/agent.Dockerfile`](../../scripts/ci/protected/agent.Dockerfile) | 受保护 CI 中的代理镜像定义。 |
| [`scripts/ci/protected/worker.Dockerfile`](../../scripts/ci/protected/worker.Dockerfile) | 受保护 CI 中的工作容器镜像定义。 |

## 5. Artifact Repository

当前没有配置外部制品仓库或镜像仓库。CI 使用的镜像与摘要属于门禁运行证据，不代表存在长期制品发布通道。

## 6. CI/CD Pipeline

| 属性 | 当前值 |
|---|---|
| 平台 | GitHub Actions |
| 已存在 workflow | [`.github/workflows/container-docker-linux.yml`](../../.github/workflows/container-docker-linux.yml) |
| 运行环境 | Linux runner，并要求可用的 Docker daemon |
| 目的 | 使用受信任的基础/默认 harness、候选代码 staging、镜像摘要、隔离 Docker 测试与 evidence 校验保护容器边界。 |
| 部署路径 | 无；该 workflow 是验证门禁，不是部署流水线。 |

### 门禁现状

| 模块 | 状态 | 说明 |
|---|---|---|
| 容器 | 已存在 | 当前唯一已登记的 CI workflow，执行真实 Linux/Docker 可信证据校验。 |
| 地图 | 尚未存在 | 已确认的目标范围是后端 project-map 与前端地图同步测试；在门禁文件实际生成并通过评审前，不属于当前 CI 能力。 |

## 7. Host Requirements

| 能力 | 要求 | 说明 |
|---|---|---|
| 后端运行时 | Python 3.12 或更高版本 | 支持 FastAPI、Uvicorn 与 pytest 工具链。 |
| 前端运行时 | 与 Vite 5 和 npm 兼容的 Node.js | 支持 React/Vite 开发、构建和 Vitest。 |
| 容器门禁 | Linux runner 与 Docker | 真实容器隔离及 evidence 校验所需。 |
| GPU | 不需要 | 当前没有 GPU 运行时或 NVIDIA 资源声明。 |

Context Store 没有给出可验证的最低 CPU、内存或磁盘数值，因此本文不声明推测值。

## 8. Storage & Network Constraints

| 约束 | 规则 |
|---|---|
| 工作区盘符 | 当前工作区位于 D 盘；下载、生成物和持久化路径不得写到 C 盘。 |
| 网络代理 | 需要联网下载或终端网络连接时，使用本机 `127.0.0.1:7890` 代理。 |
| 应用暴露 | 应用没有认证层；CLI 默认以回环地址限制本地服务边界。 |
| 数据 | SQLite 采用工作区本地持久化；当前没有活动的数据库迁移流程。 |

## Maintenance

**Last Updated:** 2026-07-11

**Update Triggers:**

- 新增、删除或重命名 `.github/workflows/` 下的 workflow。
- 新增 Docker Compose 服务、端口映射或生产部署目标。
- 本地端口、D 盘存储边界或 `7890` 代理约束改变。
- 新增服务器、域名、制品仓库或资源下限。

**Verification：**

- 以 `.ai-dev/docs/ln-110/context-store.json` 重新核对 workflow、Docker 服务、主机、端口与环境边界。
- 确认 [`.github/workflows/container-docker-linux.yml`](../../.github/workflows/container-docker-linux.yml) 仍是当前已登记的容器门禁。
- 地图 workflow 只有在文件实际存在并完成流水线评审后，才能从“尚未存在”改为“已存在”。
