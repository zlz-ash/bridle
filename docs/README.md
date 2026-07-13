<!-- SCOPE: Documentation hub and navigation layer for Bridle maintainers, agents, and reviewers -->
<!-- DOC_KIND: index -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: Read when you need the canonical map of Bridle project documentation -->
<!-- SKIP_WHEN: Skip when you already know the exact canonical document or only need a root README command -->
<!-- PRIMARY_SOURCES: README.md, AGENTS.md, backend/pyproject.toml, frontend/package.json -->
<!-- NO_CODE_EXAMPLES: This document provides navigation and standards, not implementation snippets. -->

# Bridle Documentation Hub

## Quick Navigation

| Area | Canonical Documents | Purpose |
|---|---|---|
| Standards | [Documentation Standards](documentation_standards.md), [Development Principles](principles.md) | Rules for writing, reviewing, and maintaining project knowledge |
| Project | [Requirements](project/requirements.md), [Architecture](project/architecture.md), [Agent Runtime](project/agent_runtime.md), [Technology Stack](project/tech_stack.md), [Implementation Status](project/implementation_status.md) | Current product facts and explicitly marked target runtime design |
| Interfaces | [API Specification](project/api_spec.md), [Database Schema](project/database_schema.md), [Design Guidelines](project/design_guidelines.md) | External and internal contracts |
| Operations | [Infrastructure](project/infrastructure.md), [Runbook](project/runbook.md) | Local operation, CI, container, and recovery guidance |
| Reference | [Reference Index](reference/README.md), [Project Pitfalls](reference/guides/project-pitfalls.md), [Testing Strategy](reference/guides/testing-strategy.md) | Stable decisions, reusable guidance, and verification strategy |
| Tasks | [Task Index](tasks/README.md) | Planning, status flow, and review conventions |
| Test Entry | [Test Documentation](../tests/README.md) | Test layout and execution entry point |

## Agent Entry

`AGENTS.md` is the machine-facing project entry point and the authority for collaboration rules. Read it before changing code or documentation. Then use this page to reach the narrowest canonical document for the task. Do not read every document by default: start with the relevant index, follow only the links needed for the current decision, and return to source files when exact runtime behavior matters.

| Task | Read First | Read Next |
|---|---|---|
| Understand product scope | [Requirements](project/requirements.md) | [Implementation Status](project/implementation_status.md) |
| Trace system boundaries | [Architecture](project/architecture.md) | [Technology Stack](project/tech_stack.md) |
| Design Agent types, authorization, or lifecycle | [Agent Runtime](project/agent_runtime.md) | [Implementation Status](project/implementation_status.md) and [Testing Strategy](reference/guides/testing-strategy.md) |
| Change an API or model | [API Specification](project/api_spec.md) | [Database Schema](project/database_schema.md) |
| Work on project-map behavior | [Architecture](project/architecture.md) | [Testing Strategy](reference/guides/testing-strategy.md) |
| Work on container safety | [Infrastructure](project/infrastructure.md) | [Runbook](project/runbook.md) and [Testing Strategy](reference/guides/testing-strategy.md) |
| Plan or review work | [Task Index](tasks/README.md) | [Development Principles](principles.md) |

## Overview

Bridle documentation is organized as a small directed graph rather than a flat collection. `docs/project/` owns current product and runtime facts. `docs/reference/` owns durable decisions, guides, and reusable knowledge. `docs/tasks/` owns workflow and live planning conventions. `tests/README.md` owns the test-directory entry point. This page is navigation only; detailed facts remain in their domain documents or the current source, configuration, tests, and CI definitions.

## Documentation Map

| Area | Owns | Does Not Own |
|---|---|---|
| `docs/project/` | Requirements, architecture, stack, implementation status, API, persistence, frontend guidance, infrastructure, runbook, patterns | Long-lived decision history, task status, test implementation |
| `docs/reference/` | ADRs, guides, manuals, research, reusable verification knowledge | Current product status or live work tracking |
| `docs/tasks/` | Task workflow, board semantics, review and planning conventions | Product requirements or implementation details |
| `tests/README.md` | Test layout and command entry points | Product requirements, architecture, or source code |

## General Documentation Standards

### SCOPE Tags

Every published Markdown document in this system starts with the shared metadata contract: `SCOPE`, `DOC_KIND`, `DOC_ROLE`, `READ_WHEN`, `SKIP_WHEN`, and `PRIMARY_SOURCES`. The scope states what the document may cover and protects it from becoming a duplicate catch-all.

### Maintenance Sections

Every canonical or navigational document ends with a `Maintenance` section that names update triggers, deterministic verification checks, and a concrete last-updated date. Maintenance rules describe when the document becomes stale, not a generic reminder to review it occasionally.

### Sequential Numbering

Use stable numeric identifiers only where order or audit history is part of the contract, such as ADRs and requirement records. Navigation lists and explanatory sections should use descriptive names so inserting a new item does not force unrelated renumbering.

### Unfinished Content

Published documents must not contain template variables, undecided markers, sample-only metadata, or promises of future content. If a fact is not available from current sources, omit the claim and point readers to the authoritative source rather than inventing a value.

## Writing Guidelines

Use progressive disclosure: put routing, purpose, ownership, and the most important contract first; move supporting detail into the owning document; and link to source rather than copying implementation. Prefer compact tables for repeated mappings, short lists for finite checks, and prose only when explaining causality or trade-offs. This keeps the documentation graph efficient for both maintainers and agents while preserving enough context to make safe decisions.

Follow [Documentation Standards](documentation_standards.md) for the complete writing and validation contract. Use [Development Principles](principles.md) when a documentation choice conflicts with simplicity, security, correctness, or maintainability.

## Standards Compliance

| Standard or Model | Application in This Documentation |
|---|---|
| ISO/IEC/IEEE 29148:2018 | Requirements are explicit, traceable, and verifiable |
| ISO/IEC/IEEE 42010:2022 | Architecture documents identify concerns, boundaries, and decisions |
| arc42 | Architecture knowledge is split into navigable, concern-focused sections |
| C4 Model | System context, containers, components, and code responsibilities remain distinct |
| Diátaxis | Tutorials, how-to guidance, reference facts, and explanations are not mixed |
| ADR format | Durable technical decisions have stable records and consequences |
| MoSCoW | Requirement priority is expressed without confusing priority with implementation status |

## Contributing to Documentation

1. Read `AGENTS.md` and the target document's `Agent Entry` before editing.
2. Confirm the target document owns the fact; otherwise update the canonical owner and link to it.
3. Preserve the complete opening metadata contract and the `NO_CODE_EXAMPLES` marker.
4. Update `PRIMARY_SOURCES` only with repository paths that currently exist.
5. Use an exact date in `Maintenance` and revise update triggers when ownership changes.
6. Keep ADR identifiers stable and update navigation when documents are added, removed, or renamed.
7. Validate UTF-8, internal links, referenced paths, forbidden placeholders, and the required top sections before publishing.

## Maintenance

**Update Triggers:**

- A canonical document is added, removed, renamed, or changes ownership.
- Backend APIs, the project-map index, persistence, frontend synchronization, CI gates, or container boundaries change which document is authoritative.
- Documentation quality rules, project principles, task workflow, or test entry points change.

**Verification:**

- [ ] Every relative link resolves to an existing repository document.
- [ ] Every listed source path exists and remains authoritative.
- [ ] The opening contract, `Quick Navigation`, `Agent Entry`, and `Maintenance` sections are present.
- [ ] No unfinished markers or template variables remain.
- [ ] UTF-8 content is readable through the repository's safe inspection workflow.

**Last Updated:** 2026-07-11
