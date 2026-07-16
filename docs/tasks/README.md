<!-- SCOPE: Bridle local task planning, pipeline states, review gates, and task artifact rules -->
<!-- DOC_KIND: index -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: Planning, reviewing, executing, or resuming work in this repository -->
<!-- SKIP_WHEN: You only need architecture, API, or one test command -->
<!-- PRIMARY_SOURCES: AGENTS.md, plan.md, .ai-dev/state/pipeline.json, .ai-dev/spec/requirements.json, docs/reference/guides/testing-strategy.md -->

# Task Management Rules

## Quick Navigation

| Need | Source |
|---|---|
| Current unfinished work | [`plan.md`](../../plan.md) |
| Pipeline state | [`.ai-dev/state/pipeline.json`](../../.ai-dev/state/pipeline.json) |
| Approved requirement baseline | [`.ai-dev/spec/requirements.json`](../../.ai-dev/spec/requirements.json) |
| Testing and CI evidence | [Testing strategy](../reference/guides/testing-strategy.md) |
| Project status | [Implementation status](../project/implementation_status.md) |

## Agent Entry

Bridle currently uses local file-backed task and pipeline evidence. No Linear team, GitHub Project, or other external tracker is configured, so this documentation does not create `kanban_board.md` or invent provider identifiers. `plan.md` is the human-readable list of unfinished work; `.ai-dev` holds structured state, requirements, reviews, CI catalogs, and audit evidence.

## Task Provider Integration

| Concern | Current source of truth | Rule |
|---|---|---|
| Unfinished plan | `plan.md` | Keep only unfinished items; remove an item after its completion evidence passes. |
| Pipeline state | `.ai-dev/state/pipeline.json` | Advance only with the installed pipeline CLI and validator evidence; never edit state directly. |
| Requirement identity | `.ai-dev/spec/requirements.json` | Use stable REQ, AC, SEC, NFR, and OPS IDs. |
| Decisions and approvals | `.ai-dev/evidence/` | Persist material user confirmations and validator results. |
| Test traceability | `.ai-dev/traceability/` and batch evidence | Map every test contract item to reviewed requirements. |
| CI audit | `.ai-dev/ci/` | Store stable cases, catalogs, fingerprints, and approval status. |
| External tracker | Not configured | Add one only after ash supplies and confirms the real provider configuration. |

The current project pipeline is advisory because the host did not create a SessionStart marker and the capability report records `strict_mode.available=false`. Deterministic validators remain authoritative for state transitions; advisory mode must not be presented as universal tool interception.

## Plan File Contract

Every `plan.md` item must be executable and auditable.

| Field | Required content |
|---|---|
| Reason | The actual business or technical cause and the behavior chain that makes the change necessary. |
| Change | Exact files, modules, functions, artifacts, and semantics to change. |
| Preserve | Existing behavior and neighboring scope that must not change. |
| Completion standard | Observable final state, value, status, log, UI, document, or artifact. |
| Tests | Inputs, preconditions, real and mocked components, and contract assertions; include negative and boundary cases when risk requires them. |
| Verification | Exact commands and expected duration; investigate an overrun before continuing. |

The plan never doubles as a completed-history ledger. Completion evidence belongs under `.ai-dev` and in the relevant project document; completed plan items are removed.

## Task Workflow

| Stage | Entry evidence | Exit gate |
|---|---|---|
| Project profile | Repository facts and capability report | `PROJECT_PROFILED` transition passes. |
| Requirements | Stable IDs, boundaries, alternatives, and explicit user decisions | `REQUIREMENTS_CONFIRMED` transition passes. |
| Project documents | Worker summaries, owners, quality manifest, and verifier report | `PROJECT_DOCS_READY` evidence is valid. |
| Batch planning | Bounded files, requirements, dependencies, risks, commands, and completion conditions | Fresh-context plan review is clean. |
| Test contract | Reviewed case paths, symbols, inputs, expected results, levels, real components, mocks, and commands | Fresh-context test-contract review is clean. |
| RED | Exact non-zero command failure attributable to the target behavior | RED validator confirms it is not an environment failure. |
| GREEN | Minimum implementation needed for the approved contract | Target command exits zero. |
| Local verification | Reviewed commands, durations, exit codes, and bounded diagnostics | All required local checks pass. |
| Specification/test review | Requirements and tests remain aligned | Fresh-context verdict is clean. |
| Quality review | Implementation is correct, scoped, safe, and maintainable | Fresh-context verdict is clean. |
| CI Author | Clean reviewed tests and CI author path contract | Deterministic jobs and catalog validate. |
| Final audit | Empty plan, complete docs, valid approvals, no active agents, full test evidence | Final audit is clean; remote actions still need separate authorization. |

Any review finding returns the work to the state named by the validator. Do not patch around a failed transition or reinterpret a review verdict as approval.

## Review Workflow

| Review | Reviewer independence | Required focus |
|---|---|---|
| Plan review | Fresh-context agent | Requirement coverage, file ownership, dependencies, risks, commands, and measurable completion. |
| Test-contract review | Fresh-context agent | Behavioral value, real causal path, failure cases, assertions, mocks, and genuine RED feasibility. |
| Specification/test review | Fresh-context agent | Stable IDs, acceptance coverage, missing or duplicate cases, and evidence traceability. |
| Quality review | Fresh-context agent | Correctness, security boundaries, logging, cleanup outcomes, minimal scope, and regression risk. |
| Final audit | Fresh-context agent | Plan empty, docs complete, approvals current, full test zero, CI catalog aligned, and no active agents. |

A reviewer reports findings with exact evidence and does not modify the artifact it is judging unless the pipeline explicitly returns the work for remediation.

## Task Templates

### Batch item

| Field | Meaning |
|---|---|
| Batch ID | Stable identifier for one reviewable unit. |
| Requirement IDs | Approved requirements and acceptance criteria owned by the batch. |
| Allowed paths | Exact change boundary; CI Author uses its stricter runtime allowlist. |
| Dependencies | Earlier batches or artifacts that must be complete. |
| Risks | Security, API, migration, browser, observability, platform, or operations risks. |
| Commands | Target RED/GREEN and local verification commands. |
| Completion | Measurable final behavior and retained evidence. |

### Test-contract case

| Field | Meaning |
|---|---|
| Requirement ID | Stable REQ, AC, SEC, NFR, or OPS identity. |
| Source path and symbol | Exact test file and test function or case symbol. |
| Purpose | The contract failure the case detects. |
| Preconditions and input | Reproducible setup and triggering action. |
| Expected | Final business or security result, not merely lack of an exception. |
| Level | Unit, contract, integration, frontend, or real environment. |
| Real and mocked components | Explicit causal boundary; security-critical behavior is not replaced by a fake. |
| Command | Deterministic invocation used for RED, GREEN, and CI mapping. |
| Risk | Why the case earns maintenance and execution cost. |

## CI and Remote Authorization

CI Author runs only after both review stages are clean. It may modify only `.github/workflows/`, `scripts/verify*`, and `.ai-dev/ci/`; product code and business tests are prohibited. A coverage escape returns to the test-contract stage.

Creating or closing an approval issue, triggering remote CI, committing, pushing, opening a pull request, or publishing requires the matching explicit authorization. Local task approval is not remote-write authorization.

## Maintenance

**Update Triggers:**

- Pipeline states, evidence schemas, review requirements, or CI Author boundaries change.
- A real external task provider is configured.
- `plan.md`, test-contract, or final-audit rules change.

**Verification:**

- Confirm `plan.md` contains only unfinished work.
- Confirm structured state was advanced through the official pipeline CLI.
- Confirm no external provider or remote approval is claimed without evidence.
- Compare test-contract and CI rules with [testing-strategy.md](../reference/guides/testing-strategy.md).

**Last Updated:** 2026-07-11
