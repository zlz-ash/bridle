<!-- SCOPE: Canonical development principles and trade-off rules used for Bridle implementation, documentation, testing, and review decisions -->
<!-- DOC_KIND: explanation -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: Read when planning, implementing, reviewing, or resolving competing technical constraints -->
<!-- SKIP_WHEN: Skip when you only need an exact API, schema, or run command -->
<!-- PRIMARY_SOURCES: AGENTS.md, backend/tests, .github/workflows/container-docker-linux.yml -->
<!-- NO_CODE_EXAMPLES: This document explains principles and contains no implementation snippets. -->

# Development Principles

## Quick Navigation

| Need | Section |
|---|---|
| Understand the governing values | [Core Principles](#core-principles) |
| Resolve competing constraints | [Decision-Making Framework](#decision-making-framework) and [Trade-offs](#trade-offs) |
| Apply project-specific safety expectations | [Bridle Safety and Verification Contracts](#bridle-safety-and-verification-contracts) |
| Identify weak approaches | [Anti-Patterns to Avoid](#anti-patterns-to-avoid) |
| Review a proposed change | [Verification Checklist](#verification-checklist) |

## Agent Entry

`AGENTS.md` remains authoritative for repository instructions. This document explains the decision principles behind those rules so maintainers and reviewers can resolve trade-offs consistently. Apply the principles to the smallest in-scope change, then verify the result against current source, configuration, tests, and CI. Do not use a principle as permission to expand the requested scope.

## Core Principles

| # | Principle | Bridle Application |
|---|---|---|
| 1 | Standards First | Use established platform and industry contracts when they improve interoperability, auditability, or safety. |
| 2 | YAGNI | Build only the behavior required by an approved need; avoid speculative extension points. |
| 3 | KISS | Prefer the smallest design that preserves correctness, evidence, and clear failure semantics. |
| 4 | DRY | Keep one authority per fact, contract, or decision and link to it from dependent artifacts. |
| 5 | Consumer-First Design | Judge APIs, project-map data, logs, and evidence from the needs of their actual caller or reviewer. |
| 6 | No Legacy Code | Do not add compatibility paths without an active consumer and an explicit retirement contract. |
| 7 | Documentation-as-Code | Update documentation with the source, test, configuration, or CI behavior it describes. |
| 8 | Security by Design | Preserve trust boundaries, real threat actors, isolated evidence, and fail-closed outcomes from the start. |
| 9 | Auto-Generated Migrations Only | If schema migration workflow is introduced, derive migrations from the authoritative model and review the generated change; the current startup path still uses metadata creation semantics. |

## Principle Details

### 1. Standards First

Standards are a constraint when they reduce ambiguity and a tool when they make evidence portable. They do not justify unnecessary machinery. Use the narrowest applicable standard and record a durable exception when Bridle must diverge.

### 2. YAGNI

Unrequested flexibility increases maintenance and test burden before it creates value. Add configuration, abstraction, or compatibility only when a current requirement or repeated repository pattern demonstrates the need.

### 3. KISS

Simple means easy to reason about under real failure conditions. A shorter path that hides timeout, cleanup, digest, or evidence failure is not simpler; it is incomplete.

### 4. DRY

Duplicate code and duplicate knowledge both drift. Reuse an existing abstraction when it already matches the semantics. For documentation, keep the detailed fact in one canonical owner and route readers there.

### 5. Consumer-First Design

The correct shape follows the consumer's decision. Project-map interfaces should expose stable identity and useful relationships. CI evidence should let a reviewer determine what ran, against which source and image, and why the gate passed or failed.

### 6. No Legacy Code

Compatibility has a cost in branches, tests, and mental models. Preserve an old path only for an identified active consumer, with explicit behavior and a removal condition. Do not delete unrelated existing compatibility while making a scoped change.

### 7. Documentation-as-Code

Documentation changes pass structural and semantic review just like source. Paths, links, commands, dates, and contracts must remain verifiable. Explanatory prose never outranks current source, tests, configuration, or CI.

### 8. Security by Design

Security claims require a real causal chain: the correct actor, the intended entry point, the protected boundary, trusted evidence collection, and a final observable outcome. Missing evidence, incomplete cleanup, or a skipped critical case is not success.

### 9. Auto-Generated Migrations Only

Bridle currently has no active migration workflow. If persistent schema evolution later requires migrations, the authoritative model should drive generation, generated operations must be reviewed, and deployment and rollback evidence must be explicit.

## Decision-Making Framework

Apply these priorities in order. A lower priority may refine a higher one but cannot silently override it.

1. **Security:** Reject choices that weaken trust boundaries, secret handling, evidence isolation, or fail-closed behavior.
2. **Standards:** Prefer compatible, documented platform behavior unless a verified project constraint requires an exception.
3. **Correctness:** Preserve the full business contract, including negative paths, cleanup, and state transitions.
4. **Simplicity:** Choose the smallest design that still meets the first three priorities.
5. **Necessity:** Remove work that does not trace to an approved requirement or verified defect.
6. **Maintainability:** Keep ownership, logs, tests, and documentation understandable to the next maintainer.
7. **Performance:** Optimize measured bottlenecks without obscuring correctness or safety evidence.

## Trade-offs

| Conflict | Higher Priority | Resolution |
|---|---|---|
| Shorter execution versus complete evidence | Complete evidence | Keep source identity, digests, exit status, timeout, and cleanup results even when the gate takes longer. |
| Reuse versus semantic mismatch | Correctness | Prefer a small local implementation over forcing unrelated behavior through an abstraction with the wrong contract. |
| Backward compatibility versus simplicity | Active consumer need | Preserve compatibility only when a real consumer and removal condition are documented. |
| Fast feedback versus real platform proof | Layered verification | Use platform-neutral contract tests for speed and Linux real-Docker evidence for the final container safety claim. |
| Detailed documentation versus drift risk | Canonical source | Explain the contract and link to source instead of copying implementation details. |

## Bridle Safety and Verification Contracts

| Domain | Required Decision Principle |
|---|---|
| Local service boundary | The default control plane remains loopback-only while no application authentication layer exists. |
| Project map | Backend indexing and frontend synchronization are one user-facing contract and require compatible verification. |
| Container review | Trusted controller and untrusted candidate responsibilities remain separated. |
| Docker evidence | Source and image digests, isolated execution, evidence collection, and cleanup bind to the same run. |
| Platform behavior | Windows, Linux POSIX, and Docker-specific behavior require evidence on the platform whose semantics are claimed. |
| Subprocesses | Exit status, standard output, standard error, timeout, and cleanup failures remain visible. |
| Logging | Runtime logic has a complete structured log flow with start, material transitions, failure, and completion. |
| Documentation | Claims remain traceable to current repository sources and pass UTF-8, link, path, and placeholder checks. |

## Anti-Patterns to Avoid

| Anti-Pattern | Why It Fails |
|---|---|
| Designing extension points before a current consumer exists | Adds branches and review surface without verified value. |
| Treating a skipped critical test as a successful gate | Hides missing evidence and turns environment gaps into false confidence. |
| Using host-side simulation to prove an untrusted container action | Breaks the threat-actor and boundary contract. |
| Copying a trusted harness from candidate source | Promotes reviewed input into the trust root. |
| Accepting stale evidence from an earlier run | Leaks an old success state into the current result. |
| Duplicating source behavior in documentation | Creates a second truth source that drifts from implementation. |
| Refactoring adjacent code during a scoped change | Increases risk and obscures which lines satisfy the approved requirement. |
| Optimizing before measuring the bottleneck | Trades clarity and evidence for an unverified gain. |

## Verification Checklist

- [ ] **Standards First:** Applicable platform and industry contracts were identified, and any exception is explicit.
- [ ] **YAGNI:** Every changed behavior traces to an approved requirement or verified defect.
- [ ] **KISS:** The design is the smallest one that preserves correctness, safety, logs, and failure semantics.
- [ ] **DRY:** Facts and decisions have one canonical owner, with links instead of duplicate detail.
- [ ] **Consumer-First Design:** APIs, map data, logs, and evidence answer the real consumer's decision.
- [ ] **No Legacy Code:** Compatibility exists only for an active consumer with a removal condition.
- [ ] **Documentation-as-Code:** Relevant docs, links, paths, commands, and dates match current repository behavior.
- [ ] **Security by Design:** Actor, entry point, trust boundary, evidence, cleanup, and fail-closed outcomes are verified.
- [ ] **Auto-Generated Migrations Only:** Any migration is derived from the authoritative model and reviewed; otherwise the current no-migration status is stated accurately.

## Maintenance

**Update Triggers:**

- `AGENTS.md` adds or changes a repository-wide implementation, review, logging, planning, or verification rule.
- Project-map synchronization, local-service exposure, persistence, agent runtime, container isolation, or CI evidence semantics change.
- Review identifies a recurring trade-off or anti-pattern not resolved by the current framework.
- An active schema migration workflow is introduced or the startup persistence contract changes.

**Verification:**

- [ ] The nine core principles and their checklist entries remain aligned.
- [ ] The seven decision priorities still resolve project trade-offs in the intended order.
- [ ] Project-specific safety contracts match current source, tests, and CI.
- [ ] Anti-patterns remain language-agnostic and contain no implementation snippets.
- [ ] Internal links, source paths, UTF-8 encoding, and the final newline pass validation.

**Last Updated:** 2026-07-11
