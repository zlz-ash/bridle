<!-- SCOPE: Test organization and execution guide for Bridle -->
<!-- DOC_KIND: how-to -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: You need where tests live and how to run them -->
<!-- SKIP_WHEN: You need testing philosophy, which belongs in docs/reference/guides/testing-strategy.md -->
<!-- PRIMARY_SOURCES: backend/tests, frontend/src/**/__tests__, backend/pyproject.toml, frontend/package.json -->

# 测试目录说明

## Quick Navigation

| 主题 | 文档 |
|---|---|
| 测试策略 | [../docs/reference/guides/testing-strategy.md](../docs/reference/guides/testing-strategy.md) |
| 后端测试 | [../backend/tests](../backend/tests) |
| 前端测试 | [../frontend/src](../frontend/src) |
| 手工测试 | [manual/](manual/) |

## Agent Entry

本目录是测试说明入口，不存放现有自动化测试代码。当前自动化测试分别位于 `backend/tests` 和 `frontend/src/**/__tests__`。手工验证记录放在 `tests/manual/`，结果文件写入 `tests/manual/results/`，该结果目录内容默认忽略。

## Test Organization

| 区域 | 路径 | 框架 | 说明 |
|---|---|---|---|
| Backend automated tests | `backend/tests` | pytest、pytest-asyncio、httpx | API、CLI、features、agent、container、observability、logging |
| Frontend automated tests | `frontend/src/**/__tests__` | Vitest、Testing Library、jsdom | hooks、layout、components、workspace picker |
| CI safety tests | `backend/tests/agent/container`、`scripts/ci` | pytest + GitHub Actions + Docker | trusted harness、isolated Docker、evidence gate |
| Manual tests | `tests/manual` | 人工执行 | 浏览器、交互、视觉和验收记录 |
| Manual results | `tests/manual/results` | 文件证据 | 本地结果文件，不进入版本控制 |

## Running Tests

| 范围 | 命令 | 预估时间 |
|---|---|---|
| 后端全部测试 | `cd backend; python -m pytest` | 1 到 5 分钟 |
| 后端静态检查 | `cd backend; python -m ruff check .` | 10 到 30 秒 |
| 前端测试 | `cd frontend; npm test -- --run` | 30 秒到 2 分钟 |
| 前端构建 | `cd frontend; npm run build` | 30 秒到 2 分钟 |

## Naming Conventions

| 区域 | 约定 |
|---|---|
| Python tests | `test_*.py` |
| Frontend tests | `*.test.ts`、`*.test.tsx` |
| Manual notes | 使用描述性 Markdown 文件名，结果文件放入 `manual/results/` |

## Story-Level Test Task Pattern

测试集中在 Story 末尾形成独立验证任务。实现期间可以运行局部测试，但最终验收要覆盖完整业务契约、失败路径、证据来源和清理结果。安全测试必须从真实主体和真实入口开始。

## Manual Testing

| 目录 | 用途 |
|---|---|
| `tests/manual/` | 手工测试计划、步骤和结论 |
| `tests/manual/results/` | 截图、日志、导出文件等结果证据 |

`tests/manual/results/` 的内容由 `.gitignore` 忽略；需要分享结果时应在总结中描述关键事实，避免把本地临时证据误提交。

## Maintenance

**Last Updated:** 2026-07-08

**Update Triggers:**
- 测试目录迁移或新增测试框架。
- package scripts、pytest 配置、CI 门禁变化。
- 手工测试结果目录规则变化。

**Verification:**
- 运行 `rg --files backend/tests frontend/src -g "*test*" -g "*spec*"` 确认测试布局。
- 检查 `.gitignore` 包含 `tests/manual/results/*` 并保留 `.gitkeep`。
- 与测试策略文档保持命令一致。
