"""SandboxedToolExecutor — policy-gated tools for NodeAgentRun."""
from __future__ import annotations

import fnmatch
import os
import shlex
import time
import urllib.parse
import urllib.request
from typing import Any

from bridle.engine.executor import Executor
from bridle.engine.proposal_path_validator import ProposalPathValidator
from bridle.engine.sandbox_policy import SandboxPolicy
from bridle.engine.unified_diff import ValidationResult
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
        runs_root = policy.workspace_root / ".bridle-runs"
        self._executor = Executor(
            workspace=str(policy.workspace_root),
            runs_dir=str(runs_root),
        )
        self._env = _sandbox_env(policy)
        self._staged_patches: dict[str, dict[str, str]] = {}
        self._granted_files: set[str] = set()
        self._access_records: list[dict[str, Any]] = []
        self._access_dedupe: dict[str, dict[str, Any]] = {}

    @property
    def effective_policy(self) -> SandboxPolicy:
        if self._granted_files:
            return self.policy.with_granted_files(self._granted_files)
        return self.policy

    def consume_access_records(self) -> list[dict[str, Any]]:
        records = list(self._access_records)
        self._access_records.clear()
        return records

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

    async def grep_code(
        self,
        query: str,
        *,
        path_glob: str | None = None,
        case_sensitive: bool = False,
        max_results: int = 20,
    ) -> dict[str, Any]:
        return await self._tool_call(
            "grep_code",
            {"query_len": len(query), "path_glob": path_glob, "max_results": max_results},
            self._grep_code_impl(query, path_glob=path_glob, case_sensitive=case_sensitive, max_results=max_results),
        )

    async def web_search(
        self,
        query: str,
        *,
        allowed_domains: list[str] | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        return await self._tool_call(
            "web_search",
            {
                "query_len": len(query),
                "domain_count": len(allowed_domains) if allowed_domains else 0,
                "max_results": max_results,
            },
            self._web_search_impl(query, allowed_domains=allowed_domains, max_results=max_results),
        )

    async def _report_blocked_impl(
        self,
        reason: str,
        evidence: dict | None,
    ) -> dict[str, Any]:
        return _completed({"reason": reason, "evidence": evidence or {}})

    async def _grep_code_impl(
        self,
        query: str,
        *,
        path_glob: str | None = None,
        case_sensitive: bool = False,
        max_results: int = 20,
    ) -> dict[str, Any]:
        active = self.effective_policy
        capped = min(max(1, max_results), 50)
        matches: list[dict] = []
        total_matches = 0
        for rel_path in sorted(active.allowed_files):
            if path_glob and not fnmatch.fnmatch(rel_path, path_glob):
                continue
            resolved = active.resolve_read_path(rel_path)
            if resolved is None or not resolved.is_file():
                continue
            try:
                content = resolved.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, ValueError):
                continue
            if "\x00" in content:
                continue
            for line_no, line in enumerate(content.splitlines(), start=1):
                hay = line if case_sensitive else line.lower()
                needle = query if case_sensitive else query.lower()
                if needle in hay:
                    total_matches += 1
                    if len(matches) < capped:
                        preview = line[:200]
                        matches.append({"path": rel_path, "line_number": line_no, "preview": preview})
        result: dict[str, Any] = {"matches": matches, "total_matches": total_matches}
        if total_matches > capped:
            result["truncated"] = True
        return _completed(result)

    async def _web_search_impl(
        self,
        query: str,
        *,
        allowed_domains: list[str] | None = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        if not self.policy.network_allowed:
            return _failed("NetworkDisabled", ["Network access is disabled in sandbox policy"])
        capped = min(max(1, max_results), 10)
        proxy_url = os.environ.get("HTTPS_PROXY", os.environ.get("HTTP_PROXY", "http://127.0.0.1:7890"))
        try:
            encoded_query = urllib.parse.quote_plus(query)
            url = f"https://api.duckduckgo.com/?q={encoded_query}&format=json&no_redirect=1"
            proxy_handler = urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url})
            opener = urllib.request.build_opener(proxy_handler)
            req = urllib.request.Request(url, headers={"User-Agent": "BridleAgent/1.0"})
            import json as _json
            with opener.open(req, timeout=15) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = _json.loads(body)
        except Exception as exc:
            return _failed("WebSearchError", [f"Search request failed: {type(exc).__name__}"])
        results: list[dict] = []
        for item in data.get("RelatedTopics", []):
            if len(results) >= capped:
                break
            if not isinstance(item, dict):
                continue
            title = item.get("Text", "")[:200]
            url_val = item.get("FirstURL", "")
            if not url_val:
                continue
            from urllib.parse import urlparse as _urlparse
            domain = _urlparse(url_val).netloc
            if allowed_domains and domain not in allowed_domains:
                continue
            results.append({"title": title, "url": url_val, "snippet": title[:150], "domain": domain})
        abstract = data.get("Abstract", "")
        abstract_url = data.get("AbstractURL", "")
        if abstract and abstract_url and len(results) < capped:
            from urllib.parse import urlparse as _urlparse
            domain = _urlparse(abstract_url).netloc
            if not allowed_domains or domain in allowed_domains:
                results.append({
                    "title": abstract[:200],
                    "url": abstract_url,
                    "snippet": abstract[:150],
                    "domain": domain,
                })
        return _completed({"search_results": results, "result_count": len(results)})

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
        errors = self.effective_policy.validate_read_path(path)
        if errors:
            return _failed("PathBoundaryError", errors)
        resolved = self.effective_policy.resolve_read_path(path)
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
        if change_type not in ("modify", "add", "remove"):
            return _failed("InvalidChangeType", [f"Unsupported change_type: {change_type}"])

        access_event: dict[str, Any] | None = None
        policy = self.effective_policy
        errors = policy.validate_patch_path(path)
        if errors:
            only_not_allowed = (
                len(errors) == 1 and "not in allowed_files" in errors[0]
            )
            if only_not_allowed:
                access_event = self._attempt_file_access_grant(
                    path,
                    change_type=change_type,
                    reason="propose_file_patch",
                )
                if access_event and access_event.get("status") == "auto_approved":
                    policy = self.effective_policy
                    errors = policy.validate_patch_path(path)
                elif access_event and access_event.get("status") == "pending_manual":
                    return _failed(
                        "AccessRequestRequired",
                        [str(access_event.get("decision_reason", "Manual approval required"))],
                        access_request=access_event,
                    )
            if errors:
                return _failed("PathBoundaryError", errors)

        from bridle.engine.unified_diff import validate_patch_for_path
        resolved = policy.resolve_read_path(path)
        file_exists = resolved is not None and resolved.is_file()
        original_text = None
        if file_exists:
            original_text = resolved.read_text(encoding="utf-8", errors="replace")
        validation = validate_patch_for_path(
            path, change_type, diff,
            original_text=original_text, file_exists=file_exists,
        )
        if not validation.valid:
            messages = [validation.error]
            if validation.recovery_hint:
                messages.append(f"recovery_hint: {validation.recovery_hint}")
            return _failed("InvalidDiff", messages)
        norm = ProposalPathValidator.normalize_workspace_relative(path)
        apply_error = self._apply_patch_to_workspace(norm, change_type, validation, policy)
        if apply_error is not None:
            return apply_error
        patch = {
            "path": norm,
            "change_type": change_type,
            "diff": diff,
            "applied": True,
            "staged": True,
        }
        self._staged_patches[norm] = {
            "path": norm,
            "change_type": change_type,
            "diff": diff,
        }
        result_payload: dict[str, Any] = {
            "patch": patch,
            "patch_staged": True,
            "patch_applied": True,
            "applied_path": norm,
            "sandbox_workspace": str(self.policy.workspace_root),
            "sandbox_inputs": self._sandbox_inputs_snapshot(policy),
        }
        if access_event is not None:
            result_payload["access_request"] = access_event
        if validation.dry_run is not None:
            dr = validation.dry_run
            result_payload["dry_run"] = {
                "valid": dr.valid,
                "hunk_count": dr.hunk_count,
                "added_lines": dr.added_lines,
                "removed_lines": dr.removed_lines,
            }
        log_event(
            "sandbox_patch_applied",
            "completed",
            run_id=self.policy.run_id,
            node_id=self.policy.node_id,
            detail={
                "applied_path": norm,
                "change_type": change_type,
                "sandbox_workspace": str(self.policy.workspace_root),
                "sandbox_input_count": len(result_payload["sandbox_inputs"]),
            },
        )
        return _completed(result_payload)

    def _attempt_file_access_grant(
        self,
        path: str,
        *,
        change_type: str,
        reason: str,
    ) -> dict[str, Any] | None:
        from bridle.engine.file_access_request import evaluate_file_access

        decision = evaluate_file_access(
            path,
            workspace_root=self.policy.workspace_root,
            allowed_files=self.effective_policy.allowed_files,
        )
        dedupe_key = f"{decision.normalized_path}:{decision.risk_level}"
        if dedupe_key in self._access_dedupe:
            return self._access_dedupe[dedupe_key]

        status = "auto_approved" if decision.auto_approve else "pending_manual"
        record = decision.to_request_payload(
            change_type=change_type,
            reason=reason,
            evidence={},
            node_id=self.policy.node_id,
            run_id=self.policy.run_id,
            status=status,
        )
        self._access_dedupe[dedupe_key] = record
        self._access_records.append(record)
        action = (
            "sandbox_file_access_auto_approved"
            if decision.auto_approve
            else "sandbox_file_access_pending_manual"
        )
        log_event(
            action,
            status,
            run_id=self.policy.run_id,
            node_id=self.policy.node_id,
            detail={
                "requested_path": decision.requested_path,
                "normalized_path": decision.normalized_path,
                "risk_level": decision.risk_level,
            },
        )
        if decision.auto_approve:
            self._granted_files.add(decision.normalized_path)
        return record

    def _resolve_patch_target(self, norm_path: str, policy: SandboxPolicy | None = None) -> Path | None:
        active = policy or self.effective_policy
        errors = active.validate_patch_path(norm_path)
        if errors:
            return None
        parts = norm_path.split("/")
        return self.policy.workspace_root.joinpath(*parts)

    def _sandbox_inputs_snapshot(self, policy: SandboxPolicy | None = None) -> list[str]:
        active = policy or self.effective_policy
        present: set[str] = set()
        for rel in active.allowed_files:
            target = self._resolve_patch_target(rel, active)
            if target is not None and target.is_file():
                present.add(rel)
        return sorted(present)

    def _apply_patch_to_workspace(
        self,
        norm_path: str,
        change_type: str,
        validation: ValidationResult,
        policy: SandboxPolicy | None = None,
    ) -> dict[str, Any] | None:
        target = self._resolve_patch_target(norm_path, policy)
        if target is None:
            return _failed("PatchApplyError", [f"Cannot resolve sandbox path: {norm_path}"])
        dry_run = validation.dry_run
        if dry_run is None or not dry_run.valid:
            return _failed("PatchApplyError", ["Patch validation missing dry-run result"])
        try:
            if change_type == "remove":
                if target.is_file():
                    target.unlink()
                return None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dry_run.new_text, encoding="utf-8")
            return None
        except OSError as exc:
            return _failed("PatchApplyError", [f"Failed to apply patch to sandbox: {exc}"])

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

            if _is_python_test_command(cmd):
                exec_result = await self._executor.run_python_command(
                    cmd,
                    run_id=self.policy.run_id,
                    timeout_seconds=self.policy.command_timeout_seconds,
                    env=self._env,
                )
            else:
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
                failed_result: dict[str, Any] = {
                    "status": "failed",
                    "error_code": "TestCommandFailed",
                    "results": results,
                }
                if exec_result.get("timed_out"):
                    failed_result["timed_out"] = True
                    failed_result["error_code"] = "TestCommandTimeout"
                else:
                    failed_result["retryable"] = True
                    failed_result["next_action"] = "patch_code_then_rerun_tests"
                return failed_result
        return _completed({"results": results})


def _preview(text: str) -> str:
    if len(text) <= STDOUT_PREVIEW_LIMIT:
        return text
    return text[:STDOUT_PREVIEW_LIMIT] + "\n...[truncated]"


def _find_venv_scripts_dir(start: Path) -> Path | None:
    current = start.resolve()
    for _ in range(12):
        scripts = current / ".venv" / "Scripts"
        if scripts.is_dir():
            return scripts
        if current.parent == current:
            break
        current = current.parent
    return None


def _sandbox_env(policy: SandboxPolicy) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in SANDBOX_ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value:
            env[key] = value

    venv_scripts = _find_venv_scripts_dir(policy.workspace_root)
    if venv_scripts is not None:
        existing = env.get("PATH", "")
        env["PATH"] = str(venv_scripts) + (os.pathsep + existing if existing else "")

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


def _is_python_test_command(command: str) -> bool:
    tokens = shlex.split(command.strip(), posix=False)
    if not tokens:
        return False
    return tokens[0].lower() in {"python", "pytest"}


def _completed(payload: dict) -> dict[str, Any]:
    return {"status": "completed", **payload}


def _failed(
    error_code: str,
    errors: list[str],
    *,
    results: list | None = None,
    access_request: dict | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "failed",
        "error_code": error_code,
        "errors": errors,
    }
    if results is not None:
        out["results"] = results
    if access_request is not None:
        out["access_request"] = access_request
    return out
