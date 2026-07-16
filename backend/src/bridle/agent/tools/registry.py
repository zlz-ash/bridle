"""AgentToolRegistry bridge from DeepSeek tool calls to SandboxedToolExecutor."""
from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from typing import Any

from bridle.agent.context.types import ToolDescriptor
from bridle.agent.runtime.schemas import AgentContext
from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.deepseek_schema import V1_TOOL_NAMES
from bridle.agent.tools.proposal_test_validator import resolve_allowed_test_commands
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor
from bridle.logging.jsonl import log_event

RuntimeToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

_ROLE_CAPABILITY_BY_TOOL = {
    "read_allowed_file": "read_workspace_file",
    "grep_code": "search_code",
    "propose_file_patch": "propose_file_patch",
    "run_allowed_tests": "run_allowed_tests",
    "report_blocked": "report_blocked",
}


def classify_tool_error(error_code: str, raw: dict[str, Any] | None = None) -> tuple[str, bool]:
    _ARGUMENT_ERRORS = frozenset({
        "invalid_tool_arguments",
        "unknown_tool",
        "InvalidChangeType",
        "InvalidDiff",
    })
    _POLICY_ERRORS = frozenset({
        "PathBoundaryError",
        "CommandPolicyError",
        "NetworkDisabled",
        "FileNotFound",
        "PatchApplyError",
        "AccessRequestRequired",
    })
    _TIMEOUT_ERRORS = frozenset({
        "TestCommandTimeout",
        "WebSearchTimeout",
    })
    _TEST_FAILURE_ERRORS = frozenset({
        "TestCommandFailed",
    })
    _EXTERNAL_ERRORS = frozenset({
        "WebSearchError",
    })
    if error_code in _ARGUMENT_ERRORS:
        return "argument", False
    if error_code in _POLICY_ERRORS:
        return "policy", False
    if error_code in _TIMEOUT_ERRORS:
        return "runtime_timeout", True
    if error_code in _TEST_FAILURE_ERRORS:
        return "test_failure", True
    if error_code in _EXTERNAL_ERRORS:
        return "external", True
    if raw and raw.get("timed_out"):
        return "runtime_timeout", True
    if raw and raw.get("results"):
        for r in raw.get("results", []):
            if r.get("policy_rejected"):
                return "policy", False
            if r.get("timed_out"):
                return "runtime_timeout", True
    return "runtime", True


class AgentToolRegistry:
    """Execute registered tools through sandbox policy."""

    _TOOL_DESCRIPTORS: list[ToolDescriptor] = [
        ToolDescriptor(
            name="read_allowed_file",
            purpose="Read one file that is explicitly allowed for this node run.",
            when_to_use=(
                "When you need to inspect the current content of an allowed file before proposing changes."
            ),
            input_summary="path: string - relative path of the file to read.",
            output_summary="File content as a string, or an error if the path is not allowed.",
            constraints="Can only read files listed in allowed_files. Cannot read files outside the node boundary.",
        ),
        ToolDescriptor(
            name="propose_file_patch",
            purpose=(
                "Propose a patch for an allowed file and apply it to the controlled sandbox workspace."
            ),
            when_to_use=(
                "When you have decided what changes to make and need them in the sandbox "
                "before calling run_allowed_tests to verify the patch."
            ),
            input_summary="path: string, change_type: string (modify|add|remove), diff: string - unified diff.",
            output_summary=(
                "On success: patch staged and applied in the sandbox (patch_applied, applied_path, "
                "sandbox_inputs). On failure: validation or PatchApplyError details."
            ),
            constraints=(
                "Can only patch files listed in allowed_files. "
                "After path, permission, and diff validation, writes the patch into the sandbox "
                "workspace so allowed tests can run against the updated files. "
                "Does not write to production or final output directories; the runner persists "
                "approved output separately. Diff must be valid unified format."
            ),
        ),
        ToolDescriptor(
            name="run_allowed_tests",
            purpose=(
                "Run exact allowlisted test commands in the sandbox workspace root (no cd required)."
            ),
            when_to_use=(
                "After proposing patches, rerun the same test commands from context.tests to verify fixes. "
                "Do not wrap commands with cd, &&, or absolute paths."
            ),
            input_summary="commands: array of strings - must match node.tests allowlist verbatim.",
            output_summary="Per-command exit_code, stdout/stderr previews, timeout, or policy rejection details.",
            constraints=(
                "Commands execute automatically at the sandbox workspace root. "
                "Only pass commands exactly as listed in the node's tests allowlist - no extra arguments, "
                "no cd/chdir, no && or shell chaining, and no absolute paths. "
                "If tests fail, read files or patch code, then rerun the same allowlisted command verbatim."
            ),
        ),
        ToolDescriptor(
            name="report_blocked",
            purpose="Report a blocking issue without changing node status.",
            when_to_use=(
                "When you cannot proceed due to missing dependencies, ambiguous requirements, or access denial."
            ),
            input_summary="reason: string - why you are blocked. evidence: object - supporting evidence (optional).",
            output_summary="Confirmation that the blocked status was recorded.",
            constraints="Does not modify any files. Use only when genuinely unable to proceed.",
        ),
        ToolDescriptor(
            name="child_agent_result_summary",
            purpose="Read result summaries from prerequisite or adjacent agent runs.",
            when_to_use=(
                "When you need to review results from prerequisite or adjacent nodes to inform your own work."
            ),
            input_summary="node_ids: array of strings - node IDs whose results to read.",
            output_summary="Array of result summaries with status, test summary, and metrics summary per node.",
            constraints=(
                "Can only read results from nodes you are allowed to access per visibility rules. "
                "Reserved - not yet callable."
            ),
            reserved=True,
        ),
        ToolDescriptor(
            name="grep_code",
            purpose="Search for text patterns in allowed source files within the node boundary.",
            when_to_use=(
                "When you need to locate code, functions, or text patterns "
                "but don't know which file contains them."
            ),
            input_summary=(
                "query: string - search pattern. path_glob: string - optional file filter. "
                "case_sensitive: boolean. max_results: integer."
            ),
            output_summary=(
                "List of matches with file path, line number, and preview. "
                "Does not return full file content."
            ),
            constraints=(
                "Can only search files in allowed_files. Does not bypass node boundary. "
                "Results do not auto-authorize patches."
            ),
        ),
        ToolDescriptor(
            name="web_search",
            purpose=(
                "Search the web for documentation, error explanations, "
                "or reference material when local files are insufficient."
            ),
            when_to_use=(
                "When you need official docs, error explanations, "
                "or reference material not available in allowed files."
            ),
            input_summary=(
                "query: string - search query. "
                "allowed_domains: array of strings - restrict to these domains. "
                "max_results: integer - max results (default 5, max 10)."
            ),
            output_summary=(
                "List of search results with title, URL, snippet, and source domain. "
                "Requires network_allowed policy."
            ),
            constraints=(
                "Only available when network_allowed is enabled in sandbox policy. "
                "Returns NetworkDisabled otherwise. Does not bypass sandbox boundaries."
            ),
        ),
    ]

    _RUNTIME_TOOL_DESCRIPTORS: dict[str, ToolDescriptor] = {
        "read_project_map": ToolDescriptor(
            name="read_project_map",
            purpose="Read a bounded project map view from local SQLite.",
            when_to_use="Use overview first, then node/children/subgraph/search for only the needed area.",
            input_summary="mode plus bounded cursor/limit/depth and mode-specific IDs or query.",
            output_summary="Structured bounded map data with cursor or change metadata.",
            constraints="Never returns the entire map by default; limits and depth are server bounded.",
        ),
        "patch_plan_nodes": ToolDescriptor(
            name="patch_plan_nodes",
            purpose="Apply the existing local PlanPatchSchema to pending project nodes.",
            when_to_use="Use only after deciding a local add/update/remove/dependency change.",
            input_summary="PlanPatchSchema add_nodes/update_nodes/remove_node_ids/replace_dependencies.",
            output_summary="Changed node IDs and change_seq, or a structured state rejection.",
            constraints="Delegates PlanService.patch_current; running nodes and affected edges are immutable.",
        ),
        "select_node": ToolDescriptor(
            name="select_node",
            purpose="Atomically select and start one runnable plan node for execution.",
            when_to_use="Use after the user has confirmed executing and identified the target node.",
            input_summary="node_id: stable project plan node ID.",
            output_summary="The node in running state, or node_not_runnable.",
            constraints="Executing role only; dependencies must be completed and transition is compare-and-set.",
        ),
        "read_code_map": ToolDescriptor(
            name="read_code_map",
            purpose="Progressive code-map reads with node/neighbor/subgraph/span/blind_spot modes.",
            when_to_use="When exploring structure layer entities, relations, or open blind spots.",
            input_summary="mode, entity_id, seed_id (mapping), max_nodes, depth, max_tokens.",
            output_summary="Bounded map slice with truncation flags.",
            constraints="Mapping role requires open blind spot seed; budgets enforced server-side.",
        ),
        "propose_semantic_annotation": ToolDescriptor(
            name="propose_semantic_annotation",
            purpose="Propose one semantic annotation with confidence for approval.",
            when_to_use="In mapping role after reading blind spots to suggest semantic facts.",
            input_summary="source_id, summary, evidence, model, confidence, file_hash, risk.",
            output_summary="Annotation record with auto_adopt or objection routing.",
            constraints="Mapping role only; never mutates code_relations.",
        ),
        "dispatch_child_agent": ToolDescriptor(
            name="dispatch_child_agent",
            purpose="Dispatch a child agent into mapping or executing role for a divergent node.",
            when_to_use="After plan vs semantic comparison surfaces work on a node.",
            input_summary="node_id, target_role (mapping|executing).",
            output_summary="Updated node status after dispatch.",
            constraints="Planning role only.",
        ),
    }

    def __init__(
        self,
        executor: SandboxedToolExecutor,
        *,
        runtime_handlers: dict[str, RuntimeToolHandler] | None = None,
        role_capabilities: dict[str, Any] | None = None,
    ) -> None:
        self._executor = executor
        self._policy = executor.policy
        self._runtime_handlers = dict(runtime_handlers or {})
        self._role_capabilities = dict(role_capabilities or {})

    @classmethod
    def from_context(
        cls,
        context: AgentContext,
        *,
        runtime_handlers: dict[str, RuntimeToolHandler] | None = None,
        test_backend: Any | None = None,
    ) -> AgentToolRegistry:
        snap = context.tool_capabilities.get("sandbox", {}) if context.tool_capabilities else {}
        allowed_tests = list(resolve_allowed_test_commands(snap, context.tests))
        policy = SandboxPolicy.for_run(
            run_id=str(snap.get("run_id", "unknown")),
            node_id=str(snap.get("node_id", "unknown")),
            workspace_root=str(snap.get("workspace_root", ".")),
            allowed_files=list(snap.get("allowed_files") or context.allowed_files),
            node_tests=allowed_tests,
            command_timeout_seconds=int(snap.get("command_timeout_seconds", 60)),
            network_allowed=bool(snap.get("network_allowed", False)),
        )
        readonly = snap.get("readonly_files") or []
        if readonly:
            policy = policy.with_readonly_files(frozenset(str(p) for p in readonly))
        backend = test_backend or snap.get("_container_test_backend")
        return cls(
            SandboxedToolExecutor(
                policy,
                test_backend=backend,
                project_id=str(snap.get("project_id") or policy.run_id),
                agent_id=str(snap.get("agent_id") or policy.node_id),
                generation=int(snap.get("generation") or 1),
                trace_id=str(snap.get("trace_id") or f"patch-{policy.run_id}"),
                formal_workspace=bool(snap.get("formal_workspace", False)),
            ),
            runtime_handlers=runtime_handlers,
            role_capabilities=context.tool_capabilities,
        )

    @classmethod
    def tool_descriptors(cls) -> list[ToolDescriptor]:
        return list(cls._TOOL_DESCRIPTORS)

    def available_tool_descriptors(self) -> list[ToolDescriptor]:
        """List callable tools; handler/capability input exits as the exact provider-visible set."""
        descriptors = [item for item in self._TOOL_DESCRIPTORS if self._is_allowed(item.name)]
        descriptors.extend(
            self._RUNTIME_TOOL_DESCRIPTORS[name]
            for name in sorted(self._runtime_handlers)
            if name in self._RUNTIME_TOOL_DESCRIPTORS and self._is_allowed(name)
        )
        return descriptors

    def frozen_copy(self) -> AgentToolRegistry:
        """Detach one runtime generation from later registry mutations."""
        return type(self)(
            self._executor,
            runtime_handlers=dict(self._runtime_handlers),
            role_capabilities=copy.deepcopy(self._role_capabilities),
        )

    def _is_allowed(self, tool_name: str) -> bool:
        """Resolve one role capability; tool input exits allowed and defaults true for old node contexts."""
        capability_name = _ROLE_CAPABILITY_BY_TOOL.get(tool_name, tool_name)
        rule = self._role_capabilities.get(capability_name)
        return not isinstance(rule, dict) or rule.get("allowed") is not False

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tool_call_id: str,
    ) -> dict[str, Any]:
        log_event(
            "deepseek_tool_call_requested",
            "started",
            run_id=self._policy.run_id,
            node_id=self._policy.node_id,
            detail={"tool_name": tool_name, "tool_call_id": tool_call_id},
        )

        if not self._is_allowed(tool_name):
            result = self._normalize_tool_result({
                "status": "failed",
                "error_code": "role_capability_denied",
                "message": "Tool is not allowed for the current project role",
            })
            self._log_tool_done(tool_name, tool_call_id, result)
            return result

        if tool_name in self._runtime_handlers:
            try:
                raw = await self._runtime_handlers[tool_name](arguments)
            except Exception as exc:
                raw = {
                    "status": "failed",
                    "error_code": getattr(exc, "error_code", type(exc).__name__),
                    "message": str(exc),
                }
            result = self._normalize_tool_result(raw)
            self._log_tool_done(tool_name, tool_call_id, result)
            return result

        if tool_name not in V1_TOOL_NAMES:
            raw = {
                "status": "failed",
                "error_code": "unknown_tool",
                "message": f"Tool '{tool_name}' is not registered",
            }
            result = self._normalize_tool_result(raw)
            self._log_tool_done(tool_name, tool_call_id, result)
            return result

        try:
            if tool_name == "read_allowed_file":
                path = arguments.get("path")
                if not isinstance(path, str) or not path.strip():
                    raise ValueError("path is required")
                raw = await self._executor.read_allowed_file(path.strip())
            elif tool_name == "propose_file_patch":
                path = arguments.get("path")
                change_type = arguments.get("change_type")
                diff = arguments.get("diff", "")
                if not isinstance(path, str) or not path.strip():
                    raise ValueError("path is required")
                if not isinstance(change_type, str) or not change_type.strip():
                    raise ValueError("change_type is required")
                if not isinstance(diff, str):
                    raise ValueError("diff must be a string")
                raw = await self._executor.propose_file_patch(
                    path.strip(),
                    diff,
                    change_type.strip(),
                )
            elif tool_name == "run_allowed_tests":
                commands = arguments.get("commands")
                if not isinstance(commands, list) or not commands:
                    raise ValueError("commands must be a non-empty array")
                cmd_list = [str(c) for c in commands]
                raw = await self._executor.run_allowed_tests(cmd_list)
            elif tool_name == "report_blocked":
                reason = arguments.get("reason")
                evidence = arguments.get("evidence")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("reason is required")
                if evidence is not None and not isinstance(evidence, dict):
                    raise ValueError("evidence must be an object")
                raw = await self._executor.report_blocked(reason.strip(), evidence)
            elif tool_name == "grep_code":
                query = arguments.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("query is required")
                path_glob = arguments.get("path_glob")
                case_sensitive = arguments.get("case_sensitive", False)
                max_results = arguments.get("max_results", 20)
                raw = await self._executor.grep_code(
                    query.strip(),
                    path_glob=path_glob,
                    case_sensitive=bool(case_sensitive),
                    max_results=int(max_results),
                )
            elif tool_name == "web_search":
                query = arguments.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise ValueError("query is required")
                allowed_domains = arguments.get("allowed_domains")
                max_results = arguments.get("max_results", 5)
                raw = await self._executor.web_search(
                    query.strip(),
                    allowed_domains=allowed_domains,
                    max_results=int(max_results),
                )
            else:
                raw = {"status": "failed", "error_code": "unknown_tool"}
        except (ValueError, TypeError) as exc:
            raw = {
                "status": "failed",
                "error_code": "invalid_tool_arguments",
                "message": str(exc),
            }
        except Exception as exc:
            raw = {
                "status": "failed",
                "error_code": type(exc).__name__,
                "message": str(exc),
            }

        result = self._normalize_tool_result(raw)
        self._log_tool_done(tool_name, tool_call_id, result)
        return result

    def tool_result_content(self, result: dict[str, Any]) -> str:
        """JSON string for DeepSeek role=tool message."""
        return json.dumps(result, ensure_ascii=False, default=str)

    def _normalize_tool_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        status = raw.get("status", "failed")
        out: dict[str, Any] = {"status": status}
        if raw.get("error_code"):
            out["error_code"] = raw["error_code"]
        if raw.get("errors"):
            out["errors"] = raw["errors"]
        if raw.get("message"):
            out["message"] = raw["message"]
        if raw.get("tool_name"):
            out["tool_name"] = raw["tool_name"]
        if raw.get("duration_ms") is not None:
            out["duration_ms"] = raw["duration_ms"]
        if status == "failed":
            error_code = raw.get("error_code", "")
            category, retryable = classify_tool_error(error_code, raw)
            out["category"] = category
            out["retryable"] = raw.get("retryable", retryable)
            if raw.get("next_action"):
                out["next_action"] = raw["next_action"]
            if raw.get("results"):
                out["results"] = raw["results"]
            if raw.get("timed_out") is not None:
                out["timed_out"] = raw["timed_out"]
            if raw.get("exit_code") is not None:
                out["exit_code"] = raw["exit_code"]
            if raw.get("policy_rejected") is not None:
                out["policy_rejected"] = raw["policy_rejected"]
            if "access_request" in raw:
                out["access_request"] = raw["access_request"]
        if status == "completed":
            if "content" in raw:
                out["content"] = raw["content"]
            if "patch" in raw:
                out["patch"] = raw["patch"]
            if "results" in raw:
                out["results"] = raw["results"]
            if "reason" in raw:
                out["reason"] = raw["reason"]
            if "matches" in raw:
                out["matches"] = raw["matches"]
            if "total_matches" in raw:
                out["total_matches"] = raw["total_matches"]
            if "truncated" in raw:
                out["truncated"] = raw["truncated"]
            if "search_results" in raw:
                out["search_results"] = raw["search_results"]
            if "dry_run" in raw:
                out["dry_run"] = raw["dry_run"]
            for key in (
                "patch_staged",
                "patch_applied",
                "applied_path",
                "sandbox_workspace",
                "sandbox_inputs",
            ):
                if key in raw:
                    out[key] = raw[key]
            if "access_request" in raw:
                out["access_request"] = raw["access_request"]
            out["category"] = "success"
            out["retryable"] = False
        return out

    def drain_access_records(self) -> list[dict[str, Any]]:
        return self._executor.consume_access_records()

    def _log_tool_done(self, tool_name: str, tool_call_id: str, result: dict[str, Any]) -> None:
        status = result.get("status", "failed")
        action = "deepseek_tool_call_completed" if status == "completed" else "deepseek_tool_call_failed"
        log_event(
            action,
            status,
            run_id=self._policy.run_id,
            node_id=self._policy.node_id,
            detail={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "error_code": result.get("error_code"),
                "category": result.get("category"),
                "retryable": result.get("retryable"),
            },
        )

