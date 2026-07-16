<!-- SCOPE: Confirmed, repeatable Bridle project pitfalls and prevention checks -->
<!-- DOC_KIND: reference -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: A change touches documentation, workspace paths, pipeline state, project-map CI, or the trusted Docker gate -->
<!-- SKIP_WHEN: You only need general development principles or a single run command -->
<!-- PRIMARY_SOURCES: AGENTS.md, .ai-dev/runtime/capability-report.json, .ai-dev/spec/requirements.json, docs/project/runbook.md, docs/reference/guides/testing-strategy.md -->

# Project Pitfalls

## Quick Navigation

| Need | Canonical source |
|---|---|
| Project boundaries | [Development principles](../../principles.md) |
| Runtime commands | [Runbook](../../project/runbook.md) |
| Test evidence | [Testing strategy](testing-strategy.md) |
| Architecture decisions | [Reference hub](../README.md) |
| Active work rules | [Task rules](../../tasks/README.md) |

## Agent Entry

This guide records only observed or repository-proven failure patterns. A suspected problem belongs in the active plan until evidence confirms its cause. Read the matching row before changing the affected boundary, then execute its prevention and verification checks.

## Confirmed Pitfalls

| Area | Failure pattern | Confirmed cause | Prevention | Verification |
|---|---|---|---|---|
| Chinese Markdown | PowerShell displays valid UTF-8 as mojibake and the displayed text is copied into a patch | The terminal display path and code page can differ from the file encoding | Run `read-chinese-safely` before and after every Chinese Markdown edit; trust `unicode_escape`, not raw terminal rendering | `python D:\ash\skills\read-chinese-safely\scripts\inspect_utf8.py <path> --show-escapes` succeeds |
| D-drive workspace | Generated or downloaded artifacts are written to a C-drive path | A default cache or temporary path was accepted without checking the project boundary | Resolve storage paths before writing; keep task artifacts inside the D-drive workspace unless the user explicitly names another location | The resolved output path remains under `D:\代码仓\Bridle-dev` |
| Existing work | A cleanup or rewrite overwrites unrelated uncommitted changes | The working tree is treated as disposable or a broad rewrite expands beyond the requested files | Inspect `git status`, preserve user changes, and assign non-overlapping file ownership | The final diff contains only requested paths plus pipeline evidence |
| CodeGraph | A recently created or removed file is reported with stale state | The watcher index trails filesystem changes | Use CodeGraph for stable structural facts; use the current filesystem for recently changed file existence and workflow configuration | Compare the named file with `Test-Path` or a scoped file listing when freshness matters |
| Pipeline enforcement | Advisory evidence is reported as strict host enforcement | The installed host did not create `session-marker.json`, and the plugin reports `strict_mode=false` | Record the capability result and use deterministic validators without claiming universal interception | `.ai-dev/runtime/capability-report.json` records `strict_mode.available=false` |
| Documentation runtime | Coordinator checkpoints are reported as runtime-enforced although no runtime CLI is installed | The documentation skill package contains contracts but no executable coordinator runtime | Persist Context Store, worker summaries, owner routing, and verifier results as advisory evidence | `.ai-dev/docs/ln-110/runtime-capability.json` records the fallback explicitly |
| SQLite schema | Current table initialization is described as a versioned migration system | The repository uses current metadata creation semantics; a migration dependency alone is not an active migration workflow | Describe the three current tables and state that no active versioned migration chain is evidenced | [Database schema](../../project/database_schema.md) and [implementation status](../../project/implementation_status.md) agree |
| Project-map CI | Planned map coverage is described as an existing GitHub workflow | Requirements and design intent are confused with current repository state | Until CI Author completes, describe backend and frontend map coverage as confirmed scope, not implemented CI | The filesystem contains no map workflow and docs use planned/pending language |
| CI Author | CI authoring changes product code or business tests | Coverage repair is attempted after the clean review gate | Return coverage gaps to the test-contract/TDD stages; restrict CI Author to `.github/workflows/`, `scripts/verify*`, and `.ai-dev/ci/` | The CI Author contract validator reports no forbidden paths or actions |
| Docker evidence | Candidate-controlled tests or configuration are treated as trusted evidence | The trusted/candidate boundary is bypassed for convenience | Preserve trusted harness sourcing, candidate staging, image and source digests, independent observations, and fail-closed validation | Container contract tests and the Linux Docker evidence gate both pass |

## Entry Rule

A new pitfall entry must identify a reproduced symptom, a confirmed cause, a bounded trigger, a prevention action, and a deterministic verification method. Do not add generic advice, unverified guesses, or transient one-off failures.

## Maintenance

**Update Triggers:**

- A repeated failure is reproduced and its root cause is confirmed.
- A listed capability gap is removed or changes behavior.
- A source path, verifier, workflow, or safety boundary changes.

**Verification:**

- Check every referenced path exists in the current repository.
- Remove entries whose stated cause is no longer true.
- Confirm Chinese source material with `read-chinese-safely` before quoting it.

**Last Updated:** 2026-07-11
