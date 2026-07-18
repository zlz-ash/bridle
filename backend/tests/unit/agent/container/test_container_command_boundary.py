from __future__ import annotations

from pathlib import Path

import pytest

from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor


class _ExploratoryContainerBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, SandboxPolicy]] = []

    async def run_command(self, command: str, *, policy: SandboxPolicy):
        self.calls.append((command, policy))
        return {
            "status": "completed",
            "command": command,
            "exit_code": 7,
            "duration_ms": 12,
            "stdout_preview": "x" * 2048,
            "stderr_preview": "expected failure",
            "container": {
                "workspace": str(policy.workspace_root),
                "cwd": "/workspace/project",
                "non_root": True,
                "network_allowed": policy.network_allowed,
                "cpu_limit": 1,
                "memory_limit_mb": 512,
                "pid_limit": 128,
                "timeout_seconds": policy.command_timeout_seconds,
                "output_limit": 2048,
                "secrets_redacted": True,
                "cleanup_status": "completed",
            },
        }


@pytest.mark.asyncio
async def test_exploratory_command_obeys_candidate_container_boundary(
    test_workspace: Path,
) -> None:
    policy = SandboxPolicy.for_run(
        run_id="run-command",
        node_id="node-command",
        workspace_root=test_workspace,
        allowed_files=["src/a.py"],
        node_tests=["python -m pytest -q"],
        network_allowed=False,
        command_timeout_seconds=9,
    )
    backend = _ExploratoryContainerBackend()
    executor = SandboxedToolExecutor(policy, test_backend=backend)

    assert not hasattr(executor, "_executor")
    result = await executor.run_command(
        "python -c \"import sys; print('arbitrary'); sys.exit(7)\""
    )

    assert result["status"] == "completed"
    assert result["authority"] == "exploratory"
    assert result["exit_code"] == 7
    assert result["stdout_preview"] == "x" * 2048
    assert result["container"] == {
        "workspace": str(test_workspace),
        "cwd": "/workspace/project",
        "non_root": True,
        "network_allowed": False,
        "cpu_limit": 1,
        "memory_limit_mb": 512,
        "pid_limit": 128,
        "timeout_seconds": 9,
        "output_limit": 2048,
        "secrets_redacted": True,
        "cleanup_status": "completed",
    }
    assert [call[0] for call in backend.calls] == [
        "python -c \"import sys; print('arbitrary'); sys.exit(7)\""
    ]

    forged = await executor.run_command(
        "echo still-exploratory",
        authority="authoritative",
        command_id="FORGED",
    )
    assert forged["status"] == "failed"
    assert forged["error_code"] == "exploratory_authority_fixed"

    empty = await executor.run_command("   ")
    assert empty["status"] == "failed"
    assert empty["error_code"] == "command_required"

    missing = SandboxedToolExecutor(policy)
    assert not hasattr(missing, "_executor")
    unavailable = await missing.run_command("echo no-host-fallback")
    assert unavailable["status"] == "failed"
    assert unavailable["error_code"] == "container_backend_required"
    assert unavailable["authority"] == "exploratory"
