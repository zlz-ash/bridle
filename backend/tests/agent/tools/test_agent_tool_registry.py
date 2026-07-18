from __future__ import annotations

from pathlib import Path

import pytest

from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.registry import AgentToolRegistry
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor


class _ContainerBackend:
    async def run_command(self, command: str, *, policy: SandboxPolicy) -> dict:
        return {"status": "completed", "exit_code": 0, "command": command}


def _registry(root: Path, **kwargs) -> AgentToolRegistry:
    policy = SandboxPolicy.for_run(
        run_id="run-registry",
        node_id="node-registry",
        workspace_root=root,
        allowed_files=[],
        node_tests=[],
        network_allowed=False,
    )
    return AgentToolRegistry(
        SandboxedToolExecutor(policy, test_backend=_ContainerBackend()),
        **kwargs,
    )


def test_registry_describes_only_the_minimal_model_tool_set() -> None:
    assert [item.name for item in AgentToolRegistry.tool_descriptors()] == [
        "run_command",
        "report_blocked",
        "web_search",
    ]


@pytest.mark.asyncio
async def test_registry_executes_arbitrary_container_command(test_workspace: Path) -> None:
    result = await _registry(test_workspace).execute(
        "run_command",
        {"command": "python -V"},
        tool_call_id="tool-1",
    )

    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert result["category"] == "success"


@pytest.mark.asyncio
async def test_registry_rejects_invalid_or_unknown_calls(test_workspace: Path) -> None:
    registry = _registry(test_workspace)

    invalid = await registry.execute("run_command", {}, tool_call_id="tool-2")
    unknown = await registry.execute("obsolete_tool", {}, tool_call_id="tool-3")

    assert invalid["error_code"] == "invalid_tool_arguments"
    assert invalid["category"] == "argument"
    assert unknown["error_code"] == "unknown_tool"
    assert unknown["category"] == "argument"


@pytest.mark.asyncio
async def test_runtime_handler_reuses_registry_dispatch(test_workspace: Path) -> None:
    async def execute_node(arguments: dict) -> dict:
        return {
            "status": "completed",
            "node_id": arguments["node_id"],
            "wait_id": "wait-1",
            "state": "waiting",
        }

    registry = _registry(
        test_workspace,
        runtime_handlers={"execute_plan_node": execute_node},
    )

    result = await registry.execute(
        "execute_plan_node",
        {"node_id": "node-1"},
        tool_call_id="tool-4",
    )

    assert result["status"] == "completed"
    assert result["node_id"] == "node-1"
    assert result["wait_id"] == "wait-1"


@pytest.mark.asyncio
async def test_role_capability_denial_precedes_dispatch(test_workspace: Path) -> None:
    registry = _registry(
        test_workspace,
        role_capabilities={"run_command": {"allowed": False}},
    )

    result = await registry.execute(
        "run_command",
        {"command": "python -V"},
        tool_call_id="tool-5",
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "role_capability_denied"

