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
    "run_command": "run_command",
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
            name="run_command",
            purpose=(
                "Run an arbitrary exploratory Bash command inside the isolated candidate container."
            ),
            when_to_use=(
                "When you need to inspect, edit, build, or diagnose the candidate workspace. "
                "Authoritative red and final tests are started by the workflow, not by this tool."
            ),
            input_summary="command: string - Bash command executed from /workspace/project.",
            output_summary="Exit code, bounded stdout/stderr previews, timeout, and container boundary metadata.",
            constraints=(
                "Always exploratory; callers cannot provide authoritative identity or command IDs. "
                "Requires the candidate container and never falls back to a host executor."
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
            input_summary="mode plus bounded cursor/limit/depth and mode-specific IDs, query, or wait_id.",
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
        "execute_plan_node": ToolDescriptor(
            name="execute_plan_node",
            purpose="Create or reuse a durable background workflow and return its wait signal.",
            when_to_use="Use after the user has confirmed executing the fixed plan node.",
            input_summary="node_id: stable project plan node ID.",
            output_summary="wait_id, execution_id, node_id, waiting state, phase, and revision.",
            constraints="Executing role only; duplicate active calls reuse the same durable execution.",
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
            if tool_name == "run_command":
                command = arguments.get("command")
                if not isinstance(command, str) or not command.strip():
                    raise ValueError("command must be a non-empty string")
                raw = await self._executor.run_command(command)
            elif tool_name == "report_blocked":
                reason = arguments.get("reason")
                evidence = arguments.get("evidence")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("reason is required")
                if evidence is not None and not isinstance(evidence, dict):
                    raise ValueError("evidence must be an object")
                raw = await self._executor.report_blocked(reason.strip(), evidence)
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
        out: dict[str, Any] = dict(raw)
        out["status"] = status
        if status == "failed":
            error_code = raw.get("error_code", "")
            category, retryable = classify_tool_error(error_code, raw)
            out.setdefault("category", category)
            out.setdefault("retryable", retryable)
        if status == "completed":
            out.setdefault("category", "success")
            out.setdefault("retryable", False)
        return out

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

