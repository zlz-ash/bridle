"""Async adapter routing sandbox tests to the module container backend."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bridle.agent.container.backend import AgentContainerBackend, AgentContainerError
from bridle.agent.container.candidate_contract import CandidateExecutionRequest
from bridle.agent.safety.sandbox_policy import SandboxPolicy

logger = logging.getLogger("bridle")


class ContainerTestBackend(Protocol):
    async def run_allowed_tests(self, commands: list[str], *, policy: SandboxPolicy) -> dict[str, Any]:
        ...


@dataclass
class VerificationEvidence:
    test_runs: list[dict[str, Any]] = field(default_factory=list)
    container_runs: list[dict[str, Any]] = field(default_factory=list)
    required_commands: list[str] = field(default_factory=list)
    required_command_ids: list[str] = field(default_factory=list)
    executed_command_ids: list[str] = field(default_factory=list)
    passed_command_ids: list[str] = field(default_factory=list)
    failed_command_ids: list[str] = field(default_factory=list)
    all_required_passed: bool = False
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_runs": list(self.test_runs),
            "container_runs": list(self.container_runs),
            "required_commands": list(self.required_commands),
            "required_command_ids": list(self.required_command_ids),
            "executed_command_ids": list(self.executed_command_ids),
            "passed_command_ids": list(self.passed_command_ids),
            "failed_command_ids": list(self.failed_command_ids),
            "all_required_passed": self.all_required_passed,
            "error_code": self.error_code,
        }


class ModuleContainerTestBackend:
    """Execute allowed tests inside the module container for one candidate."""

    def __init__(
        self,
        backend: AgentContainerBackend,
        *,
        candidate_request: CandidateExecutionRequest,
        candidate_root: str,
        module_root: str,
        candidate_rel: str,
        test_entity_id: str,
        required_commands: list[str] | None = None,
        required_command_ids: list[str] | None = None,
        map_seq: int = 0,
    ) -> None:
        self._backend = backend
        self._request = candidate_request
        self._candidate_root = candidate_root
        self._module_root = module_root
        self._candidate_rel = candidate_rel
        self._test_entity_id = test_entity_id
        self._map_seq = map_seq
        self.required_commands = list(required_commands or [])
        self.required_command_ids = list(required_command_ids or [])
        self._passed_ids: set[str] = set()
        self._failed_ids: set[str] = set()
        self.evidence = VerificationEvidence(
            required_commands=list(self.required_commands),
            required_command_ids=list(self.required_command_ids),
        )

    def collect_evidence(self) -> VerificationEvidence:
        self._sync_evidence_state()
        return self.evidence

    def _sync_evidence_state(self) -> None:
        required = set(self.required_command_ids)
        self.evidence.passed_command_ids = sorted(self._passed_ids)
        self.evidence.failed_command_ids = sorted(self._failed_ids)
        self.evidence.executed_command_ids = sorted(self._passed_ids | self._failed_ids)
        if self._failed_ids:
            self.evidence.all_required_passed = False
            if self.evidence.error_code is None:
                self.evidence.error_code = "required_command_failed"
        elif required and required <= self._passed_ids:
            self.evidence.all_required_passed = True
            self.evidence.error_code = None
        else:
            self.evidence.all_required_passed = False

    async def run_allowed_tests(self, commands: list[str], *, policy: SandboxPolicy) -> dict[str, Any]:
        results: list[dict] = []
        for cmd in commands:
            policy_errors = policy.validate_test_command(cmd)
            if policy_errors:
                self.evidence.error_code = "CommandPolicyError"
                self.evidence.test_runs.append(
                    {
                        "commands": commands,
                        "policy_rejected": True,
                        "errors": policy_errors,
                    }
                )
                self._sync_evidence_state()
                logger.info(
                    "container_test_command_rejected",
                    extra={
                        "action": "container_test_command_rejected",
                        "status": "rejected",
                        "detail": {
                            "run_id": policy.run_id,
                            "node_id": policy.node_id,
                            "module_id": self._request.module_id,
                            "candidate_rel": self._candidate_rel,
                            "command": cmd,
                            "errors": policy_errors,
                        },
                    },
                )
                return {
                    "status": "failed",
                    "error_code": "CommandPolicyError",
                    "errors": policy_errors,
                    "results": [],
                }

        loop = asyncio.get_running_loop()

        def _run() -> dict[str, Any]:
            return self._backend.run_tests_in_candidate(
                candidate_root=Path(self._candidate_root),
                module_root=Path(self._module_root),
                candidate_rel=self._candidate_rel,
                run_id=policy.run_id,
                node_id=policy.node_id,
                module_id=self._request.module_id,
                boundary_fingerprint=self._request.boundary_fingerprint,
                test_commands=commands,
                write_set=list(self._request.write_set),
                test_entity_id=self._test_entity_id,
                map_seq=self._request.base_map_seq,
                timeout_seconds=policy.command_timeout_seconds,
                network_allowed=policy.network_allowed,
                image_version=self._request.image_version,
            )

        try:
            payload = await loop.run_in_executor(None, _run)
        except AgentContainerError as exc:
            self.evidence.error_code = exc.error_code
            self.evidence.test_runs.append(
                {
                    "commands": commands,
                    "error_code": exc.error_code,
                    "detail": exc.detail,
                }
            )
            self._sync_evidence_state()
            return {
                "status": "failed",
                "error_code": exc.error_code,
                "errors": [str(exc)],
                "results": _results_from_error(exc),
                "retryable": exc.error_code in {"container_wait_timeout", "container_exec_failed"},
            }

        manifest = payload.get("manifest") or {}
        results = []
        for item in payload.get("test_results") or []:
            command_id = str(item.get("command_id") or "")
            result_item = {
                "command_id": command_id or None,
                "command": item.get("raw_command") or " ".join(item.get("argv") or []),
                "policy_rejected": False,
                "exit_code": item.get("exit_code"),
                "duration_ms": item.get("duration_ms", 0),
                "stdout_preview": (item.get("stdout") or "")[:2048],
                "stderr_preview": (item.get("stderr") or "")[:2048],
                "timed_out": item.get("timed_out", False),
            }
            results.append(result_item)
            if not command_id:
                continue
            if item.get("exit_code") == 0 and not item.get("timed_out"):
                self._passed_ids.add(command_id)
                self._failed_ids.discard(command_id)
            else:
                self._failed_ids.add(command_id)

        self.evidence.test_runs.append({"commands": commands, "results": results})
        self.evidence.container_runs.append(
            {
                "container_id": payload.get("container_id"),
                "container_reused": payload.get("container_reused"),
                "boundary_fingerprint": payload.get("boundary_fingerprint"),
                "image_version": payload.get("image_version"),
                "candidate_rel": payload.get("candidate_rel"),
                "diagnostic_path": payload.get("diagnostic_path"),
            }
        )

        if manifest.get("status") != "completed":
            self.evidence.error_code = manifest.get("error_code") or "TestCommandFailed"
            self._sync_evidence_state()
            return {
                "status": "failed",
                "error_code": self.evidence.error_code,
                "results": results,
                "retryable": manifest.get("error_code") == "container_wait_timeout",
            }

        self._sync_evidence_state()
        return {"status": "completed", "results": results}


def _results_from_error(exc: AgentContainerError) -> list[dict[str, Any]]:
    return [
        {
            "command": "",
            "policy_rejected": False,
            "exit_code": -1,
            "stdout_preview": "",
            "stderr_preview": str(exc),
            "timed_out": exc.error_code == "container_wait_timeout",
        }
    ]
