"""SandboxedToolExecutor — policy-gated tools for NodeAgentRun."""
from __future__ import annotations

import os
import time
from typing import Any

from bridle.engine.executor import Executor
from bridle.engine.sandbox_policy import SandboxPolicy
from bridle.logging.jsonl import log_event

STDOUT_PREVIEW_LIMIT = 2048
SANDBOX_ENV_ALLOWLIST = frozenset({
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
})


class SandboxedToolExecutor:
    """Execute sandbox tools with audit logging."""

    stdout_preview_limit = STDOUT_PREVIEW_LIMIT

    def __init__(self, policy: SandboxPolicy) -> None:
        self.policy = policy
        self._executor = Executor(workspace=str(policy.workspace_root))
        self._env = _sandbox_env(policy)

    async def read_allowed_file(self, path: str) -> dict[str, Any]:
        return await self._tool_call(
            "read_allowed_file",
            {"path": path},
            self._read_allowed_file_impl(path),
        )

    async def propose_file_patch(
        self,
        path: str,
        diff: str,
        change_type: str,
    ) -> dict[str, Any]:
        return await self._tool_call(
            "propose_file_patch",
            {"path": path, "change_type": change_type, "diff_len": len(diff)},
            self._propose_file_patch_impl(path, diff, change_type),
        )

    async def run_allowed_tests(self, commands: list[str]) -> dict[str, Any]:
        return await self._tool_call(
            "run_allowed_tests",
            {"command_count": len(commands)},
            self._run_allowed_tests_impl(commands),
        )

    async def report_blocked(self, reason: str, evidence: dict | None = None) -> dict[str, Any]:
        return await self._tool_call(
            "report_blocked",
            {"reason": reason},
            self._report_blocked_impl(reason, evidence),
        )

    async def _report_blocked_impl(
        self,
        reason: str,
        evidence: dict | None,
    ) -> dict[str, Any]:
        return _completed({"reason": reason, "evidence": evidence or {}})

    async def _tool_call(
        self,
        tool_name: str,
        input_summary: dict,
        coro,
    ) -> dict[str, Any]:
        started = time.monotonic()
        log_event(
            "sandbox_tool_started",
            "started",
            run_id=self.policy.run_id,
            node_id=self.policy.node_id,
            detail={"tool_name": tool_name, "input_summary": input_summary},
        )
        try:
            result = await coro
            duration_ms = int((time.monotonic() - started) * 1000)
            status = result.get("status", "completed")
            log_event(
                "sandbox_tool_completed",
                status,
                run_id=self.policy.run_id,
                node_id=self.policy.node_id,
                duration_ms=duration_ms,
                detail={
                    "tool_name": tool_name,
                    "error_code": result.get("error_code"),
                    "exit_code": result.get("exit_code"),
                },
            )
            result["duration_ms"] = duration_ms
            result["tool_name"] = tool_name
            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            log_event(
                "sandbox_tool_failed",
                "failed",
                run_id=self.policy.run_id,
                node_id=self.policy.node_id,
                duration_ms=duration_ms,
                detail={"tool_name": tool_name, "error_code": type(exc).__name__},
            )
            return {
                "status": "failed",
                "tool_name": tool_name,
                "error_code": type(exc).__name__,
                "message": str(exc),
                "duration_ms": duration_ms,
            }

    async def _read_allowed_file_impl(self, path: str) -> dict[str, Any]:
        errors = self.policy.validate_read_path(path)
        if errors:
            return _failed("PathBoundaryError", errors)
        resolved = self.policy.resolve_read_path(path)
        if resolved is None or not resolved.is_file():
            return _failed("FileNotFound", [f"File not found: {path}"])
        content = resolved.read_text(encoding="utf-8", errors="replace")
        return _completed({"path": path, "content": content, "size": len(content)})

    async def _propose_file_patch_impl(
        self,
        path: str,
        diff: str,
        change_type: str,
    ) -> dict[str, Any]:
        errors = self.policy.validate_patch_path(path)
        if errors:
            return _failed("PathBoundaryError", errors)
        if change_type not in ("modify", "add", "remove"):
            return _failed("InvalidChangeType", [f"Unsupported change_type: {change_type}"])
        norm = path
        patch = {
            "path": norm,
            "change_type": change_type,
            "diff": diff,
            "applied": False,
        }
        return _completed({"patch": patch})

    async def _run_allowed_tests_impl(self, commands: list[str]) -> dict[str, Any]:
        results: list[dict] = []
        for cmd in commands:
            policy_errors = self.policy.validate_test_command(cmd)
            if policy_errors:
                log_event(
                    "sandbox_command_rejected",
                    "rejected",
                    run_id=self.policy.run_id,
                    node_id=self.policy.node_id,
                    detail={
                        "command": cmd,
                        "errors": policy_errors,
                        "cwd": str(self.policy.workspace_root),
                    },
                )
                results.append({
                    "command": cmd,
                    "policy_rejected": True,
                    "errors": policy_errors,
                    "exit_code": None,
                    "stdout_preview": "",
                    "stderr_preview": "",
                })
                return _failed("CommandPolicyError", policy_errors, results=results)

            exec_result = await self._executor.run_command(
                cmd,
                run_id=self.policy.run_id,
                timeout_seconds=self.policy.command_timeout_seconds,
                env=self._env,
            )
            results.append({
                "command": cmd,
                "policy_rejected": False,
                "exit_code": exec_result.get("exit_code"),
                "duration_ms": exec_result.get("duration_ms"),
                "stdout_preview": _preview(exec_result.get("stdout", "")),
                "stderr_preview": _preview(exec_result.get("stderr", "")),
                "stdout_path": exec_result.get("stdout_path"),
                "stderr_path": exec_result.get("stderr_path"),
                "timed_out": exec_result.get("timed_out", False),
            })
            if exec_result.get("exit_code") != 0 or exec_result.get("timed_out"):
                return {
                    "status": "failed",
                    "error_code": "TestCommandFailed",
                    "results": results,
                }
        return _completed({"results": results})


def _preview(text: str) -> str:
    if len(text) <= STDOUT_PREVIEW_LIMIT:
        return text
    return text[:STDOUT_PREVIEW_LIMIT] + "\n...[truncated]"


def _sandbox_env(policy: SandboxPolicy) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in SANDBOX_ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value:
            env[key] = value

    tmp_dir = policy.workspace_root / ".aicoding" / "tmp" / policy.run_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = str(tmp_dir)
    env["TMP"] = str(tmp_dir)
    return env


def sandbox_results_to_command_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for item in result.get("results", []) or []:
        stdout = item.get("stdout_preview", "")
        stderr = item.get("stderr_preview", "")
        converted.append({
            "exit_code": item.get("exit_code") if item.get("exit_code") is not None else -1,
            "duration_ms": item.get("duration_ms", 0),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_path": item.get("stdout_path"),
            "stderr_path": item.get("stderr_path"),
            "policy_rejected": item.get("policy_rejected", False),
            "timed_out": item.get("timed_out", False),
        })
    return converted


def _completed(payload: dict) -> dict[str, Any]:
    return {"status": "completed", **payload}


def _failed(
    error_code: str,
    errors: list[str],
    *,
    results: list | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "failed",
        "error_code": error_code,
        "errors": errors,
    }
    if results is not None:
        out["results"] = results
    return out
