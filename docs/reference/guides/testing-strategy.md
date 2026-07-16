<!-- SCOPE: Risk-based testing, evidence, and CI mapping rules for the current Bridle repository -->
<!-- DOC_KIND: how-to -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: Planning, writing, reviewing, or mapping Bridle tests into CI -->
<!-- SKIP_WHEN: You only need one command from the runbook -->
<!-- PRIMARY_SOURCES: .ai-dev/spec/requirements.json, backend/pyproject.toml, frontend/package.json, backend/tests, frontend/src/**/__tests__, scripts/ci, .github/workflows/container-docker-linux.yml -->

# Testing Strategy

## Quick Navigation

| Area | Primary suite |
|---|---|
| Project map backend | `backend/tests/features/project_map` |
| Project map frontend | Map synchronization tests under `frontend/src/hooks/__tests__` |
| Container contracts | `backend/tests/agent/container` |
| Real Docker evidence | `protected-docker-posix-gate` in the existing container workflow |
| Pipeline requirements | [.ai-dev specification](../../../.ai-dev/spec/requirements.json) |

## Agent Entry

Every test must trace to a stable REQ, AC, SEC, NFR, or OPS identifier. Coverage gaps are repaired through an approved test contract, genuine RED evidence, minimal GREEN implementation, local verification, and independent review. CI Author maps only reviewed tests; it does not create or weaken business tests.

## Evidence Lifecycle

| Stage | Required evidence | Rejection condition |
|---|---|---|
| Requirement | Stable identifier, statement, acceptance references, dependencies, and evidence strategy | Ambiguous scope or an unknown dependency |
| Test contract | Source path, test symbol, purpose, preconditions, input, expected result, level, real components, mocked components, command, risk | A happy-path-only case or an assertion unrelated to the business contract |
| RED | Exact command, non-zero exit code, non-environment failure, and reason attributable to the target behavior | Missing dependency, permission error, or fabricated failure |
| GREEN | Minimal implementation and zero-exit target command | Weakened assertion, skipped test, or unrelated refactor |
| Local verification | Reviewed command, duration, exit status, and bounded diagnostics | “No exception” without contract assertions |
| Review | Clean specification/test review and clean quality review from fresh context | Reviewer and author share the same unreviewed assumptions |
| CI mapping | Stable case ID, reviewed requirement IDs, source hash, command, CI job, and approval status | Unknown requirement, missing case field, duplicate case, or workflow drift |

## Project-Map Gate Scope

The confirmed map scope crosses the backend and frontend contract.

| Layer | Included behavior | Required failure signal |
|---|---|---|
| Backend | Map overview, paging, search, relations, semantic annotations, entities, blind spots, boundaries, candidates, arbitration, indexing, incremental refresh, and persistence contracts represented by reviewed tests | Any mapped test failure or required test not collected |
| Frontend | `mapLayerSync`, `mapSyncLogger`, and `useProjectMapLayers` base, retry, and synchronization behavior | Any mapped Vitest failure or omitted reviewed test |
| Excluded | Chat queue, draft chat, workspace reset, and unrelated runtime hooks | The deterministic selector must not collect unrelated suites |

The map CI workflow is not current repository behavior until the pipeline reaches the CI Author stage and its local validation succeeds.

## Container Gate Scope

Container testing has two required layers.

| Layer | Purpose | Environment |
|---|---|---|
| Platform-neutral unit and contract tests | Validate request boundaries, path guards, lifecycle state, evidence parsing, runner behavior, protected build rules, and failure diagnostics quickly | Local pytest environment; Docker-specific cases may skip only for explicit platform reasons |
| Real Linux/Docker gate | Validate trusted harness sourcing, candidate isolation, image identity, bind mounts, daemon behavior, critical test observation, evidence integrity, and cleanup | Linux runner with Docker through `.github/workflows/container-docker-linux.yml` |

The final container verdict must fail closed when trusted inputs, required observations, source or image digests, or structured evidence are missing, duplicated, mismatched, or tampered.

## Commands

Before each terminal invocation, announce an execution estimate and investigate any overrun.

| Scope | Command | Typical local estimate |
|---|---|---|
| Backend project map | `cd backend; python -m pytest tests/features/project_map` | 1–5 minutes |
| Frontend map synchronization | `cd frontend; npm test -- --run src/hooks/__tests__/mapLayerSync.test.ts src/hooks/__tests__/mapSyncLogger.test.ts src/hooks/__tests__/useProjectMapLayers.test.ts src/hooks/__tests__/useProjectMapLayers.retry.test.tsx src/hooks/__tests__/useProjectMapLayers.sync.test.tsx` | 1–3 minutes |
| Container contract suite | `cd backend; python -m pytest tests/agent/container` | 2–10 minutes depending on platform skips |
| Backend lint | `cd backend; python -m ruff check src tests` | 10–60 seconds |
| Frontend build | `cd frontend; npm run build` | 30 seconds–3 minutes |
| Real Docker gate | GitHub Actions job `protected-docker-posix-gate` | Runner and Docker-build dependent; remote execution requires separate authorization |

## Logging and Diagnostics

Every gate command must retain stage, command, duration, exit status, timeout state, and redacted failure diagnostics. Container evidence also retains run, container, worker, source digest, image digest, primary failure, and cleanup outcome identities. Logs must be bounded and must not contain secrets or full conversations.

## Review Checklist

- The case asserts the final business or security contract, not merely process survival.
- Real components and mocks are named; security behavior is not replaced by a fake.
- Required negative, boundary, timeout, tampering, and cleanup paths are represented according to risk.
- Test selection is deterministic and excludes unrelated suites.
- A missing or uncollected required test fails the gate.
- CI Author changes remain inside its allowed prefixes.
- No test, workflow, or document claims remote success without retained evidence.

## Maintenance

**Update Triggers:**

- Requirements, test contracts, test locations, or commands change.
- The trusted Docker evidence model or workflow changes.
- CI Author catalog fields or approval fingerprint inputs change.

**Verification:**

- Compare commands with `backend/pyproject.toml` and `frontend/package.json`.
- Compare map selectors with the confirmed scope in `.ai-dev/spec/requirements.json`.
- Compare container evidence rules with the existing workflow and reviewed tests.

**Last Updated:** 2026-07-11
