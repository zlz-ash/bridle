<!-- SCOPE: Bridle 当前代码中重复出现的架构与实现模式 -->
<!-- DOC_KIND: reference -->
<!-- DOC_ROLE: working -->
<!-- READ_WHEN: 重构、评审或新增模块前需要确认现有模式时 -->
<!-- SKIP_WHEN: 只需要已经接受的架构决策时 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json, backend/src/bridle, frontend/src, scripts/ci -->

# 模式目录

<!-- Auto-detected by ln-112, audit with ln-640 -->

## Quick Navigation

| 目标 | 入口 |
|---|---|
| 已发现模式 | [Pattern Inventory](#pattern-inventory) |
| 评分口径 | [Four-Score Review Model](#four-score-review-model) |
| 当前基线 | [Current Scores](#current-scores) |
| 趋势 | [Trend Tracking](#trend-tracking) |
| 正式决策 | [ADR 目录](../reference/adrs/) |

## Agent Entry

本目录是自动发现后的工作记录，不是审计结论。所有模式状态均为 `Detected`，表示 Context Store 已记录对应源码结构；分数只表达文档化与决策成熟度，不等于测试已运行。模式升级为正式约束时必须建立或更新 ADR，并由独立审计复核。

## Pattern Inventory

| 模式 | Status | 位置 | 适用场景 | 主要风险 | ADR |
|---|---|---|---|---|---|
| Feature-scoped Router + Service/Store | Detected | `backend/src/bridle/features` | 按 projects、project_map、sessions、workspace、system 分离 API 与业务状态。 | router、service 和 store 责任漂移会造成跨域耦合。 | [ADR-001](../reference/adrs/adr-001-fastapi-backend.md) |
| Workspace-anchored Local Persistence | Detected | `backend/src/bridle/cli.py`, `backend/src/bridle/database.py` | 把服务、SQLite 与文件读取锚定到单个 workspace。 | workspace 根与数据库路径不一致会污染项目身份。 | [ADR-002](../reference/adrs/adr-002-local-sqlite-sqlalchemy.md) |
| Incremental + Semantic Project-map Indexing | Detected | `backend/src/bridle/features/project_map` | 同时支持增量变化、代码关系、语义标注、候选与执行刷新。 | 前端水位与后端变化序列漂移会丢失或重复图层。 | — |
| Event Bus + Observability Facade | Detected | `backend/src/bridle/events`, `backend/src/bridle/observability` | 统一事件通知与可选外部 trace。 | 高频读取或 adapter 失败可能污染业务控制面。 | [ADR-004](../reference/adrs/adr-004-langfuse-v4-observability.md) |
| Agent Provider Strategy | Detected | `backend/src/bridle/agent/providers` | 在 fake、stub 与外部 provider 之间切换。 | 真实 provider 进入默认启动路径会破坏本地可运行性。 | — |
| Trusted Controller / Untrusted Candidate | Detected | `backend/src/bridle/agent/container`, `scripts/ci` | 隔离候选代码并验证镜像身份、测试观察和 evidence。 | 候选控制 harness 或证据输入会使门禁失去可信度。 | [ADR-005](../reference/adrs/adr-005-trusted-docker-gate.md) |
| Hook-based Map Synchronization | Detected | `frontend/src/hooks` | 封装地图分页、水位、重试、取消和运行时服务状态。 | 副作用散落到组件会导致竞态和难以收集的测试。 | [ADR-003](../reference/adrs/adr-003-react-vite-frontend.md) |
| Tokenized Component System | Detected | `frontend/src/styles/tokens`, `frontend/src/components/ds` | 统一 `brd-*` 视觉 token 与基础组件。 | legacy 样式与 token 并存时可能产生不一致。 | [ADR-003](../reference/adrs/adr-003-react-vite-frontend.md) |

## Four-Score Review Model

四级成熟度使用 1–4；未发现的候选不进入目录。

| Score | 含义 | 需要的证据 |
|---|---|---|
| 1 | Detected | Context Store 或源码结构记录了重复模式。 |
| 2 | Documented | canonical/working 文档说明边界、用途和风险。 |
| 3 | Decided | ADR 或等价决策记录接受该模式。 |
| 4 | Enforced | 测试、类型、lint 或 CI 合同强制该模式，并有可复查运行证据。 |

## Current Scores

| 模式 | Score | 当前依据 | 升级条件 |
|---|---|---|---|
| Feature-scoped Router + Service/Store | 3 | 本目录、[architecture.md](../project/architecture.md)、ADR-001 | 需要独立审计确认所有功能域遵守相同边界。 |
| Workspace-anchored Local Persistence | 3 | 本目录、架构文档、ADR-002 | 需要可复查测试运行证明路径与数据库边界。 |
| Incremental + Semantic Project-map Indexing | 2 | 本目录与架构文档 | 形成独立 ADR 并绑定后端/前端地图 CASE。 |
| Event Bus + Observability Facade | 3 | 本目录、架构文档、ADR-004 | 需要运行证据证明观测失败不改变业务结果。 |
| Agent Provider Strategy | 2 | 本目录与架构文档 | 明确 provider 选择决策并验证默认本地替代路径。 |
| Trusted Controller / Untrusted Candidate | 3 | 本目录、架构文档、ADR-005 | 完成真实 Linux/Docker 门禁并保留可复查 evidence 后才可评 4。 |
| Hook-based Map Synchronization | 3 | 本目录、架构文档、ADR-003 | 地图 CI 收集并通过全部已评审前端同步 CASE。 |
| Tokenized Component System | 2 | 本目录与设计系统位置 | 建立专门设计决策与浏览器级一致性验证。 |

## Trend Tracking

| 日期 | 基线 | 变化说明 | 证据状态 |
|---|---|---|---|
| 2026-07-11 | ln-112 rewrite | 从 Context Store 重建 8 个模式，统一为 Detected；分数按文档和 ADR 保守计算。 | 未运行测试，不能升级为 Enforced。 |

## Maintenance

**Update Triggers:**

- Context Store 出现新的跨模块模式，或现有模式位置/责任发生变化。
- 模式被 ADR 接受、替代或废弃。
- 测试、类型、lint 或 CI 开始强制某个模式，并产生可复查运行证据。

**Verification:**

- `Detected` 不得写成“已审计”或“已验证”。
- Score 3 必须有可解析 ADR；Score 4 必须同时有强制机制和运行证据。
- 模式路径、架构链接和 ADR 链接必须解析到当前仓库。
