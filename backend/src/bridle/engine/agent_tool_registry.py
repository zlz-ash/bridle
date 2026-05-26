"""AgentToolRegistry — bridge DeepSeek tool calls to SandboxedToolExecutor."""
from __future__ import annotations

import json
from typing import Any

from bridle.engine.context_types import ToolDescriptor
from bridle.engine.deepseek_tools_schema import V1_TOOL_NAMES
from bridle.engine.proposal_test_validator import resolve_allowed_test_commands
from bridle.engine.sandbox_policy import SandboxPolicy
from bridle.engine.sandboxed_tool_executor import SandboxedToolExecutor
from bridle.logging.jsonl import log_event
from bridle.schemas.proposal import AgentContext


class AgentToolRegistry:
    """Execute registered tools through sandbox policy."""

    _TOOL_DESCRIPTORS: list[ToolDescriptor] = [
        ToolDescriptor(
            name="read_allowed_file",
            purpose="Read one file that is explicitly allowed for this node run.",
            when_to_use=(
                "When you need to inspect the current content of an allowed file before proposing changes."
            ),
            input_summary="path: string — relative path of the file to read.",
            output_summary="File content as a string, or an error if the path is not allowed.",
            constraints="Can only read files listed in allowed_files. Cannot read files outside the node boundary.",
        ),
        ToolDescriptor(
            name="propose_file_patch",
            purpose="Propose a patch for an allowed file without writing to disk.",
            when_to_use=(
                "When you have decided what changes to make to a file and want to record them as a diff."
            ),
            input_summary="path: string, change_type: string (modify|add|remove), diff: string — unified diff.",
            output_summary="Confirmation of the proposed patch with status, or an error if the path is not allowed.",
            constraints=(
                "Can only patch files listed in allowed_files. "
                "Does not write to disk. Diff must be valid unified format."
            ),
        ),
        ToolDescriptor(
            name="run_allowed_tests",
            purpose="Run test commands from the node allowlist via sandbox policy.",
            when_to_use=(
                "When you have proposed patches and want to verify them by running the allowed test commands."
            ),
            input_summary="commands: array of strings — test commands to execute.",
            output_summary="Test execution results including pass/fail status for each command.",
            constraints=(
                "Can only run commands listed in the node's tests allowlist. Commands run in sandbox with timeout."
            ),
        ),
        ToolDescriptor(
            name="report_blocked",
            purpose="Report a blocking issue without changing node status.",
            when_to_use=(
                "When you cannot proceed due to missing dependencies, ambiguous requirements, or access denial."
            ),
            input_summary="reason: string — why you are blocked. evidence: object — supporting evidence (optional).",
            output_summary="Confirmation that the blocked status was recorded.",
            constraints="Does not modify any files. Use only when genuinely unable to proceed.",
        ),
        ToolDescriptor(
            name="child_agent_result_summary",
            purpose="Read result summaries from child or adjacent node agents.",
            when_to_use=(
                "When you need to review results from prerequisite or adjacent nodes to inform your own work."
            ),
            input_summary="node_ids: array of strings — node IDs whose results to read.",
            output_summary="Array of result summaries with status, test summary, and metrics summary per node.",
            constraints=(
                "Can only read results from nodes you are allowed to access per visibility rules. "
                "Reserved — not yet callable."
            ),
            reserved=True,
        ),
    ]

    def __init__(self, executor: SandboxedToolExecutor) -> None:
        self._executor = executor
        self._policy = executor.policy

    @classmethod
    def from_context(cls, context: AgentContext) -> AgentToolRegistry:
        snap = context.tool_capabilities.get("sandbox", {}) if context.tool_capabilities else {}
        allowed_tests = list(resolve_allowed_test_commands(snap, context.tests))
        policy = SandboxPolicy.for_run(
            run_id=str(snap.get("run_id", "unknown")),
            node_id=str(snap.get("node_id", "unknown")),
            workspace_root=str(snap.get("workspace_root", ".")),
            allowed_files=list(snap.get("allowed_files") or context.allowed_files),
            node_tests=allowed_tests,
            command_timeout_seconds=int(snap.get("command_timeout_seconds", 60)),
        )
        return cls(SandboxedToolExecutor(policy))

    @classmethod
    def tool_descriptors(cls) -> list[ToolDescriptor]:
        return list(cls._TOOL_DESCRIPTORS)

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

        if tool_name not in V1_TOOL_NAMES:
            result = {
                "status": "failed",
                "error_code": "unknown_tool",
                "message": f"Tool '{tool_name}' is not registered",
            }
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
        if status == "completed":
            if "content" in raw:
                out["content"] = raw["content"]
            if "patch" in raw:
                out["patch"] = raw["patch"]
            if "results" in raw:
                out["results"] = raw["results"]
            if "reason" in raw:
                out["reason"] = raw["reason"]
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
            },
        )
