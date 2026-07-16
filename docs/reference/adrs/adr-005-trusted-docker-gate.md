<!-- SCOPE: 使用受信任控制面验证不受信候选容器链与真实 Linux/Docker evidence 的决策记录。 -->
<!-- DOC_KIND: record -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: 修改容器边界、CI harness、镜像摘要、隔离测试或 evidence 校验前阅读。 -->
<!-- SKIP_WHEN: 只需要普通后端/前端测试命令时跳过。 -->
<!-- PRIMARY_SOURCES: .ai-dev/docs/ln-110/context-store.json -->

# ADR-005: 受信任 Docker 安全门禁

## Quick Navigation

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 2026-07-11 |
| Decision | 使用受信任 base/default harness、候选 staging、镜像摘要、隔离 Docker 测试与 evidence 校验。 |
| Current Workflow | [`.github/workflows/container-docker-linux.yml`](../../../.github/workflows/container-docker-linux.yml) |
| Related | [Reference Hub](../README.md)、[基础设施](../../project/infrastructure.md)、[运行手册](../../project/runbook.md)、[测试指南](../guides/testing-strategy.md) |

## Agent Entry

| Signal | Guidance |
|---|---|
| Read When | 候选代码、trusted harness、Docker image、隔离或 evidence 契约变化。 |
| Preserve | trusted controller 与 untrusted candidate 的边界，以及真实 Linux/Docker 证据链。 |
| Current CI Fact | 当前仅登记容器 Linux workflow；地图 CI 尚未存在。 |

## Context

Bridle 能执行代理容器，而候选代码不能被允许定义、替换或伪造自己的安全判定。普通单元测试也不能证明真实 Docker daemon、镜像、容器隔离和 evidence 链按预期工作。

## Decision

容器门禁由受信任控制面持有 base/default harness，将候选代码单独 staging，并绑定镜像摘要、隔离 Docker 测试与 evidence 校验。当前实现入口是 Linux GitHub Actions workflow `.github/workflows/container-docker-linux.yml`。

## Rationale

| Reason | Project fit |
|---|---|
| 信任边界明确 | 候选代码不能控制最终安全判定。 |
| 真实运行证据 | Linux runner 与 Docker daemon 提供普通 mock 无法替代的隔离证据。 |
| 可关联性 | 镜像摘要、候选 staging 与 evidence 可绑定同一次门禁运行。 |

## Alternatives

| Alternative | Trade-off |
|---|---|
| 只运行平台无关单元测试 | 反馈快，但不能证明真实 Docker 隔离与 evidence。 |
| 允许候选代码携带门禁测试 | 灵活，但候选可以弱化、跳过或替换安全断言。 |
| 仅人工检查 Docker 行为 | 能发现部分问题，但缺少确定性、重复性与不可变证据。 |

## Consequences

| Positive | Cost / obligation |
|---|---|
| 容器安全结论来自受信任 harness。 | 真实 Linux/Docker 门禁比普通测试更慢、环境要求更高。 |
| evidence 与镜像/候选来源可关联。 | 摘要、staging 与证据缺失必须 fail closed。 |
| 快速合同测试与真实门禁职责分离。 | 不能用普通测试通过替代 Docker 门禁通过。 |

## Scope Boundary

容器门禁是当前已存在的 CI 能力。地图门禁的目标范围虽已确认为后端 project-map 与前端地图同步测试，但 workflow 尚未存在，不属于本 ADR 的当前实现事实。

## Maintenance

**Last Updated:** 2026-07-11

**Update Triggers:** trusted harness、候选 staging、镜像摘要、Docker 隔离、evidence schema 或当前 workflow 变化。

**Verification：** 对照 Context Store 的 `CI_CD_PIPELINE` 与容器测试描述；aggregate summary 明确本轮文档生成未运行产品测试或 CI。
