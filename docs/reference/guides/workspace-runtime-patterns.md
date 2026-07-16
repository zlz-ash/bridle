<!-- SCOPE: Reusable Bridle workspace-first runtime patterns and review checks -->
<!-- DOC_KIND: reference -->
<!-- DOC_ROLE: canonical -->
<!-- READ_WHEN: Changing workspace binding, local API startup, persistence paths, file access, or runtime documentation -->
<!-- SKIP_WHEN: You only need a single start command from the runbook -->
<!-- PRIMARY_SOURCES: backend/src/bridle/cli.py, backend/src/bridle/config.py, backend/src/bridle/database.py, backend/src/bridle/features/workspace, docs/project/runbook.md -->

# Workspace Runtime Patterns

## Quick Navigation

| Topic | Canonical source |
|---|---|
| Runtime architecture | [Architecture](../../project/architecture.md) |
| Commands and troubleshooting | [Runbook](../../project/runbook.md) |
| Persistence contract | [Database schema](../../project/database_schema.md) |
| Repeated failures | [Project pitfalls](project-pitfalls.md) |

## Agent Entry

Bridle is a local-first, workspace-anchored runtime. A change is safe only when the selected workspace, filesystem boundary, network exposure, persistence location, and diagnostic output remain explicit. On this D-drive project, generated and downloaded task artifacts must not silently move to the C drive.

## Patterns

| Pattern | Do | Avoid | Evidence |
|---|---|---|---|
| Explicit workspace anchor | Pass `--workspace <path>` or use the documented test fixture; resolve the path before access | Infer the security boundary from an incidental current directory | CLI and workspace tests show the selected path |
| Loopback control plane | Keep the default host at `127.0.0.1`; reject non-loopback binding while no application authentication exists | Bind to `0.0.0.0` or a public interface as a convenience | Network-boundary tests assert the rejection behavior |
| Workspace-local persistence | Derive the SQLite location from the selected workspace and document the three current tables | Write persistent state to an unrelated user or system directory | Startup and database tests use the selected workspace |
| Current schema semantics | Describe current metadata/table creation accurately | Present the dependency on Alembic as proof of an active migration chain | Database docs and implementation status state the same limitation |
| Scoped file APIs | Resolve file listing and overview operations inside the current workspace | Return host-wide paths or traverse outside the selected root | Workspace API tests assert scoped results |
| D-drive storage | Keep generated, downloaded, temporary task artifacts inside the D-drive workspace unless ash names another location | Hard-code a C-drive output path | Resolved paths are checked before writes |
| Proxy-aware networking | Use `HTTP_PROXY` and `HTTPS_PROXY` with local port `7890` for network downloads or terminal connections | Retry network downloads without the configured proxy or without an estimate | The command environment and logs record the proxy without secrets |
| Secret-safe environment docs | Document variable names and behavior, never values from `backend/.env` | Copy API keys, cookies, tokens, or full environment files into docs or logs | Redaction checks and scoped source review pass |
| Structured runtime logging | Record startup, stage, failure, recovery, exit status, duration, and bounded detail | Emit only an exception string or lose the primary failure during cleanup | Logging and observability contract tests retain structured fields |
| UTF-8 source verification | Use `read-chinese-safely` before quoting or patching Chinese files | Copy mojibake from terminal display | `unicode_escape` confirms the intended text |

## Review Questions

| Question | Passing answer |
|---|---|
| Which workspace is read or written? | The resolved path and containment check are explicit. |
| Can the local control plane become externally reachable? | The default remains loopback, or a separately reviewed authentication and authorization contract exists. |
| Where is persistent state stored? | It is anchored to the selected workspace on the approved drive. |
| Does the change claim a migration or deployment capability? | The claim points to current executable evidence rather than a dependency or plan. |
| Does the action use the network? | The estimate and `7890` proxy configuration are stated before execution. |
| Can diagnostics leak secrets? | Sensitive values are excluded or redacted, and full environment files are not read into output. |
| Can a cleanup failure hide the primary failure? | Both outcomes remain visible with explicit resource-unknown state where necessary. |

## Maintenance

**Update Triggers:**

- Workspace selection, path resolution, file APIs, bind policy, or database placement changes.
- Environment loading, proxy behavior, or structured logging fields change.
- A versioned migration or authenticated remote-control boundary is actually implemented.

**Verification:**

- Run the CLI and workspace-scoping tests relevant to the changed boundary.
- Compare commands and ports with [runbook.md](../../project/runbook.md).
- Verify all cited paths exist and Chinese source material remains valid UTF-8.

**Last Updated:** 2026-07-11
