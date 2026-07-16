<!-- SCOPE: Canonical writing, structure, source, and verification requirements for Bridle project documentation -->
<!-- DOC_KIND: reference -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: Read when creating, updating, reviewing, or validating project documentation -->
<!-- SKIP_WHEN: Skip when you only need a product behavior, API contract, or run command -->
<!-- PRIMARY_SOURCES: AGENTS.md, docs/README.md, docs/principles.md -->
<!-- NO_CODE_EXAMPLES: This document defines documentation contracts and contains no implementation snippets. -->

# Documentation Standards

## Quick Navigation

| Need | Section |
|---|---|
| Choose an owner and document type | [Governance and Ownership](#1-governance-and-ownership), [Document Types and Roles](#6-document-types-and-roles) |
| Create the required header and entry sections | [Opening Contract](#2-opening-contract), [Navigation and Agent Entry](#3-navigation-and-agent-entry) |
| Verify claims and paths | [Source Actuality and Path Integrity](#4-source-actuality-and-path-integrity) |
| Write concise, useful content | [Progressive Disclosure and Writing](#5-progressive-disclosure-and-writing) |
| Document commands and security gates | [Operational Facts](#8-commands-configuration-and-operational-facts), [Security and Test Evidence](#9-security-and-test-evidence) |
| Publish or maintain a document | [Review and Quality Gates](#11-review-and-quality-gates), [Lifecycle and Maintenance](#12-lifecycle-and-maintenance) |

## Agent Entry

This document is the canonical reference for documentation quality. Before editing, identify the owning document and its primary sources. During editing, keep claims no broader than the evidence. Before publishing, apply the quality gate in section 11. Project facts belong in `docs/project/`; durable decisions and reusable guidance belong in `docs/reference/`; workflow state and planning rules belong in `docs/tasks/`.

## Quick Reference

The following 60 requirements form the publishable documentation baseline.

| Range | Category | Required Outcome |
|---|---|---|
| DS-01–DS-05 | Governance | One owner, bounded scope, and explicit authority |
| DS-06–DS-10 | Opening contract | Complete machine-readable metadata near the top |
| DS-11–DS-15 | Navigation | Fast routing through required entry sections |
| DS-16–DS-20 | Actuality | Claims, links, and repository paths are current |
| DS-21–DS-25 | Writing | Progressive disclosure, concise language, no duplicated implementation |
| DS-26–DS-30 | Types and roles | Correct Diátaxis-aligned kind and graph role |
| DS-31–DS-35 | Links and references | Resolvable links and canonical references |
| DS-36–DS-40 | Operations | Exact commands, configuration boundaries, and environment semantics |
| DS-41–DS-45 | Security and tests | Real threat actors, evidence chains, and failure semantics |
| DS-46–DS-50 | Accessibility and language | Scannable, UTF-8-safe, inclusive content |
| DS-51–DS-55 | Quality gates | Deterministic pre-publication checks |
| DS-56–DS-60 | Lifecycle | Explicit update triggers, verification, and retirement behavior |

## 1. Governance and Ownership

Documentation stays trustworthy only when each fact has one clear owner. A navigation page may summarize where a fact lives, but it must not become a second implementation specification.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-01 | Assign one canonical document to each durable topic. | The topic has one authoritative destination. |
| DS-02 | Bound each document with a narrow `SCOPE`. | The scope states both purpose and boundary. |
| DS-03 | Keep navigation, canonical, working, and derived roles distinct. | `DOC_ROLE` matches how readers use the file. |
| DS-04 | Preserve authority of source, configuration, tests, and CI over explanatory prose. | Conflicting prose is corrected against current repository evidence. |
| DS-05 | Route semantic repairs to the document owner. | Reviews name the owning file rather than duplicating a fix elsewhere. |

### Governance Verification

| Check | Pass Condition |
|---|---|
| Ownership | No competing canonical document exists for the same fact. |
| Boundary | Adjacent topics link outward rather than expanding the scope. |
| Authority | `PRIMARY_SOURCES` names the evidence used for current claims. |

## 2. Opening Contract

Every published Markdown file under `docs/` begins with machine-readable comments near the top. The comments support deterministic routing without adding visible prose noise.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-06 | Include `SCOPE`. | The field appears in the first 12 lines. |
| DS-07 | Include one allowed `DOC_KIND`. | The value is `index`, `reference`, `how-to`, `explanation`, or `record`. |
| DS-08 | Include one allowed `DOC_ROLE`. | The value is `canonical`, `navigation`, `working`, or `derived`. |
| DS-09 | Include `READ_WHEN` and `SKIP_WHEN`. | Both fields give distinct routing conditions. |
| DS-10 | Include `PRIMARY_SOURCES` and the no-code marker. | Sources are real paths and implementation snippets are explicitly excluded. |

### Opening Contract Verification

| Check | Pass Condition |
|---|---|
| Position | All required metadata is near the document top. |
| Syntax | Each field uses one complete HTML comment. |
| Content | No template variable or sample metadata remains. |

## 3. Navigation and Agent Entry

Readers should understand what a document is for and where to go next before reading details. The top sections are an interface, not decoration.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-11 | Provide `Quick Navigation`. | The section points to major in-file or canonical destinations. |
| DS-12 | Provide `Agent Entry`. | The section states purpose, reading boundary, and next source. |
| DS-13 | Put routing before detailed exposition. | The first detailed section follows the entry material. |
| DS-14 | Keep index pages focused on ownership and destinations. | The page does not restate entire child documents. |
| DS-15 | Make link labels describe the destination. | Readers can choose a link without parsing its raw path. |

### Navigation Verification

| Check | Pass Condition |
|---|---|
| First screen | Purpose and primary destinations are immediately visible. |
| Reading order | A reader can stop after routing if no detail is needed. |
| Graph shape | Canonical documents do not create circular ownership. |

## 4. Source Actuality and Path Integrity

Repository facts drift quickly. Documentation must cite current evidence and avoid implying that planned behavior already exists.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-16 | Verify every current repository path. | Each referenced file or directory exists. |
| DS-17 | Derive commands from active project configuration. | Commands match `backend/pyproject.toml`, `frontend/package.json`, or CI. |
| DS-18 | Distinguish implemented, absent, and planned behavior. | Status language matches repository evidence. |
| DS-19 | Use exact dates for time-sensitive statements. | Relative time expressions do not carry the fact. |
| DS-20 | Recheck facts after source behavior changes. | The maintenance trigger covers the owning source. |

### Actuality Verification

| Check | Pass Condition |
|---|---|
| Paths | All current-path references resolve. |
| Behavior | Public claims match source, tests, configuration, and CI. |
| Status | Missing features are not described as active. |

## 5. Progressive Disclosure and Writing

Good project documentation gives the smallest sufficient answer first and leaves implementation details in source. Explanations should preserve causality without copying code.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-21 | Lead with outcome, contract, or routing. | Readers do not search through history for the current answer. |
| DS-22 | Prefer tables for repeated mappings. | Comparable fields use a consistent compact shape. |
| DS-23 | Use lists only for finite sequences or checks. | Lists are not fragmented prose paragraphs. |
| DS-24 | Link to source instead of embedding implementation. | No implementation snippet becomes a second truth source. |
| DS-25 | Remove redundancy and speculative flexibility. | Every section directly supports the document scope. |

### Writing Verification

| Check | Pass Condition |
|---|---|
| Clarity | Terms are concrete and unexplained jargon is avoided. |
| Economy | Repeated facts are replaced with canonical links. |
| Causality | Decisions explain why the constraint exists when that affects safe use. |

## 6. Document Types and Roles

Document kind describes the reader's task. Document role describes the file's position in the knowledge graph. They are independent and both are required.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-26 | Use `index` for routing maps. | The document primarily connects readers to owners. |
| DS-27 | Use `reference` for exact stable facts. | Readers can look up a contract without following a tutorial. |
| DS-28 | Use `how-to` for goal-oriented procedures. | The document has prerequisites, ordered actions, and verification. |
| DS-29 | Use `explanation` for rationale and trade-offs. | The document teaches relationships rather than commands. |
| DS-30 | Use `record` for decisions or historical evidence. | Identity, status, context, decision, and consequences remain traceable. |

### Type and Role Verification

| Check | Pass Condition |
|---|---|
| Diátaxis | A single document does not mix incompatible reader tasks. |
| Role | Canonical and navigation responsibilities are not confused. |
| History | Records stay immutable except for status and supersession metadata. |

## 7. Links and References

Links are part of the documentation contract. A broken relative link is a publishing failure, and a vague link label is a usability defect.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-31 | Use relative links for repository documents. | Links remain valid when the repository root moves. |
| DS-32 | Resolve every internal Markdown target. | The target file and relevant anchor exist. |
| DS-33 | Link to canonical owners, not convenient duplicates. | Readers arrive at the authoritative document. |
| DS-34 | Use official external sources appropriate to the stack. | Python, FastAPI, React, Vite, and related references use primary documentation. |
| DS-35 | Avoid decorative or redundant links. | Every link answers a routing or evidence need. |

### Link Verification

| Check | Pass Condition |
|---|---|
| Internal | Relative file targets exist. |
| Anchors | Heading anchors match the destination heading. |
| External | The source is authoritative and supports the associated claim. |

## 8. Commands, Configuration, and Operational Facts

Operational documentation is executable guidance. Commands, host assumptions, configuration names, and failure outcomes must match the current runtime.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-36 | Copy command semantics from active project configuration. | Install, serve, lint, build, and test commands match current sources. |
| DS-37 | State the working directory for commands. | A reader knows where the command runs. |
| DS-38 | Separate platform-neutral and platform-specific behavior. | Windows, POSIX, and Docker claims do not substitute for one another. |
| DS-39 | Name configuration variables without exposing secrets. | Variable purpose is clear and values remain private. |
| DS-40 | State expected success and failure signals. | Exit status, evidence, or observable state defines completion. |

### Operational Verification

| Check | Pass Condition |
|---|---|
| Reproducibility | A maintainer can identify prerequisites and working directory. |
| Configuration | Names match the current application and CI definitions. |
| Failure | Missing dependencies, timeouts, and nonzero exits are not described as success. |

## 9. Security and Test Evidence

Security documentation must preserve the real causal chain. A test name or host-side simulation is not proof that an untrusted actor crossed the intended boundary.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-41 | Identify the real threat actor and entry point. | The initiating identity and boundary are explicit. |
| DS-42 | Describe trusted and untrusted resources separately. | Candidate input is never presented as the trust root. |
| DS-43 | Bind evidence to one run and its source and image digests. | Stale or mismatched evidence fails closed. |
| DS-44 | Preserve collection, cleanup, and timeout failures. | A partial security run cannot report success. |
| DS-45 | Map tests to business contracts, not only test counts. | Assertions prove the intended end state and failure semantics. |

### Security Evidence Verification

| Check | Pass Condition |
|---|---|
| Actor | The behavior originates from the subject under test. |
| Evidence | Trusted collection is isolated from the attacked resource. |
| Gate | Missing, duplicate, mismatched, or skipped critical cases fail deterministically. |

## 10. Accessibility, Language, and Encoding

Documentation must remain readable in text-only tools, assistive technology, and terminals with imperfect display behavior. Encoding checks distinguish corrupt bytes from display-only mojibake.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-46 | Store Markdown as UTF-8 with one final newline. | Deterministic UTF-8 inspection succeeds. |
| DS-47 | Keep heading levels sequential and descriptive. | The outline is navigable without visual styling. |
| DS-48 | Give tables meaningful headers and compact cells. | Relationships remain understandable when read linearly. |
| DS-49 | Use inclusive, direct language and define project-specific terms. | Readers do not need unstated team context. |
| DS-50 | Verify CJK text through the safe UTF-8 workflow. | Terminal mojibake is not copied into edits. |

### Accessibility and Encoding Verification

| Check | Pass Condition |
|---|---|
| Outline | Heading hierarchy has no unexplained jumps. |
| Tables | Headers identify each column's meaning. |
| Encoding | UTF-8 decoding and escaped Unicode inspection agree. |

## 11. Review and Quality Gates

A document is publishable only when critical and high-severity documentation findings are resolved. Review checks structure, truth, navigation, and maintenance together.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-51 | Check the full opening and top-section contract. | Metadata, `Quick Navigation`, `Agent Entry`, and `Maintenance` are present. |
| DS-52 | Scan for unfinished or template content. | No forbidden marker appears in published prose. |
| DS-53 | Validate internal links and current repository paths. | Every target resolves. |
| DS-54 | Review semantic accuracy against primary sources. | Claims match the current repository. |
| DS-55 | Reject unresolved critical or high findings. | The quality report has no blocking item. |

### Quality Gate Verification

| Check | Pass Condition |
|---|---|
| Structural | Required metadata and sections pass deterministic checks. |
| Mechanical | Links, paths, encoding, final newline, and placeholders pass. |
| Semantic | Ownership, actuality, and operational meaning pass review. |

### Review Severity

| Severity | Meaning | Publication Result |
|---|---|---|
| Critical | Unsafe or fundamentally false instruction | Block publication. |
| High | Broken contract, path, link, or materially stale fact | Block publication. |
| Medium | Ambiguity, weak navigation, or incomplete verification | Repair before the next release. |
| Low | Local clarity or consistency improvement | Track without changing the documented contract. |

## 12. Lifecycle and Maintenance

Maintenance is part of the document contract. A last-updated date alone is insufficient; the document must name which source changes make it stale and how to revalidate it.

| ID | Requirement | Acceptance Check |
|---|---|---|
| DS-56 | End canonical and navigation documents with `Maintenance`. | The heading is present and complete. |
| DS-57 | List source-based update triggers. | Triggers identify concrete repository changes. |
| DS-58 | List deterministic verification actions. | Each check produces a clear pass or fail. |
| DS-59 | Use an exact last-updated date. | The date uses `YYYY-MM-DD`. |
| DS-60 | Retire content through replacement or supersession links. | Removed facts do not leave dangling navigation or silent history loss. |

### Lifecycle Verification

| Check | Pass Condition |
|---|---|
| Trigger | Every primary source category has a corresponding update trigger. |
| Check | Verification covers structure, links, paths, encoding, and semantics. |
| Retirement | Superseded documents point to the new canonical owner. |

## Standards Alignment

| Standard or Framework | Applied Constraint |
|---|---|
| ISO/IEC/IEEE 29148:2018 | Requirements are necessary, unambiguous, feasible, and verifiable. |
| ISO/IEC/IEEE 42010:2022 | Architecture descriptions identify stakeholders, concerns, viewpoints, and decisions. |
| Diátaxis | Reader tasks determine whether content is tutorial, how-to, reference, or explanation. |
| arc42 | Architecture knowledge uses consistent, bounded sections and explicit decisions. |
| C4 Model | Context, container, component, and code-level responsibilities are not collapsed. |
| ADR | Durable decisions retain context, decision, status, and consequences. |

## Publishable Document Checklist

- [ ] The document has one canonical owner and a bounded scope.
- [ ] The opening metadata contract is complete in the first 12 lines.
- [ ] `Quick Navigation`, `Agent Entry`, and `Maintenance` are present.
- [ ] Document kind and role match the reader task and graph position.
- [ ] Every internal link and current repository path resolves.
- [ ] Current claims match source, configuration, tests, and CI.
- [ ] No implementation snippet or unfinished template content remains.
- [ ] Commands state their working directory and observable outcome.
- [ ] Security and test claims preserve actor, boundary, evidence, and failure semantics.
- [ ] UTF-8, headings, tables, and final newline are valid.
- [ ] The exact update date and source-based triggers are current.
- [ ] No critical or high-severity finding remains.

## Maintenance

**Update Triggers:**

- The shared documentation quality contract changes.
- `AGENTS.md` changes repository-wide documentation, language, planning, logging, testing, or verification rules.
- The documentation graph gains, removes, renames, or reassigns a canonical document.
- A recurring review finding requires a new deterministic quality rule.

**Verification:**

- [ ] The quick-reference ranges still map to exactly 60 requirements.
- [ ] All 12 requirement categories remain present and internally consistent.
- [ ] The opening contract and required top sections match the shared quality contract.
- [ ] All internal links and `PRIMARY_SOURCES` paths resolve.
- [ ] The document contains no implementation snippets or unfinished template content.
- [ ] UTF-8 inspection succeeds and the file ends with one newline.

**Last Updated:** 2026-07-11
