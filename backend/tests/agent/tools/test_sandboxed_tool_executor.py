from __future__ import annotations

from pathlib import Path

import pytest

from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor


class _ContainerBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, SandboxPolicy]] = []

    async def run_command(self, command: str, *, policy: SandboxPolicy) -> dict:
        self.calls.append((command, policy))
        return {
            "status": "completed",
            "exit_code": 0,
            "stdout_preview": "ok",
            "stderr_preview": "",
        }


def _policy(root: Path, *, network_allowed: bool = False) -> SandboxPolicy:
    return SandboxPolicy.for_run(
        run_id="run-tools",
        node_id="node-tools",
        workspace_root=root,
        allowed_files=["src/tool.py"],
        node_tests=[],
        network_allowed=network_allowed,
        command_timeout_seconds=30,
    )


@pytest.mark.asyncio
async def test_run_command_routes_only_to_candidate_container(test_workspace: Path) -> None:
    backend = _ContainerBackend()
    policy = _policy(test_workspace)
    executor = SandboxedToolExecutor(policy, test_backend=backend)

    result = await executor.run_command("python -c \"print('ok')\"")

    assert result["status"] == "completed"
    assert result["authority"] == "exploratory"
    assert result["tool_name"] == "run_command"
    assert result["exit_code"] == 0
    assert backend.calls == [("python -c \"print('ok')\"", policy)]


@pytest.mark.asyncio
async def test_run_command_has_no_host_fallback(test_workspace: Path) -> None:
    executor = SandboxedToolExecutor(_policy(test_workspace))

    result = await executor.run_command("python -V")

    assert result["status"] == "failed"
    assert result["error_code"] == "container_backend_required"
    assert result["authority"] == "exploratory"


@pytest.mark.asyncio
async def test_run_command_authority_is_not_model_selectable(test_workspace: Path) -> None:
    executor = SandboxedToolExecutor(
        _policy(test_workspace),
        test_backend=_ContainerBackend(),
    )

    result = await executor.run_command("python -V", authority="formal")

    assert result["status"] == "failed"
    assert result["error_code"] == "exploratory_authority_fixed"
    assert result["authority"] == "exploratory"


@pytest.mark.asyncio
async def test_report_blocked_is_structured_and_non_mutating(test_workspace: Path) -> None:
    executor = SandboxedToolExecutor(_policy(test_workspace))

    result = await executor.report_blocked("dependency unavailable", {"attempt": 2})

    assert result["status"] == "completed"
    assert result["tool_name"] == "report_blocked"
    assert result["reason"] == "dependency unavailable"
    assert result["evidence"] == {"attempt": 2}


@pytest.mark.asyncio
async def test_web_search_respects_container_network_state(test_workspace: Path) -> None:
    executor = SandboxedToolExecutor(_policy(test_workspace, network_allowed=False))

    result = await executor.web_search("python asyncio")

    assert result["status"] == "failed"
    assert result["tool_name"] == "web_search"
    assert result["error_code"] == "NetworkDisabled"

