"""Tests for AgentToolRegistry."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.engine.agent_tool_registry import AgentToolRegistry
from bridle.engine.sandbox_policy import SandboxPolicy
from bridle.engine.sandboxed_tool_executor import SandboxedToolExecutor


@pytest.fixture
def registry(test_workspace: Path) -> AgentToolRegistry:
    allowed = "src/tool.py"
    (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
    (test_workspace / allowed).write_text("tool content", encoding="utf-8")
    policy = SandboxPolicy.for_run(
        run_id="r1",
        node_id="n1",
        workspace_root=test_workspace,
        allowed_files=[allowed],
        node_tests=["echo registry-ok"],
    )
    return AgentToolRegistry(SandboxedToolExecutor(policy))


class TestAgentToolRegistry:
    @pytest.mark.asyncio
    async def test_read_allowed_file(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("read_allowed_file", {"path": "src/tool.py"}, tool_call_id="tc1")
        assert result["status"] == "completed"
        assert "tool content" in result["content"]

    @pytest.mark.asyncio
    async def test_read_rejects_boundary(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("read_allowed_file", {"path": "../x.py"}, tool_call_id="tc2")
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_propose_patch_no_disk_write(
        self,
        registry: AgentToolRegistry,
        test_workspace: Path,
    ) -> None:
        path = test_workspace / "src/tool.py"
        before = path.read_text(encoding="utf-8")
        result = await registry.execute(
            "propose_file_patch",
            {"path": "src/tool.py", "change_type": "modify", "diff": "---\n+++"},
            tool_call_id="tc3",
        )
        assert result["status"] == "completed"
        assert path.read_text(encoding="utf-8") == before

    @pytest.mark.asyncio
    async def test_run_allowed_tests(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute(
            "run_allowed_tests",
            {"commands": ["echo registry-ok"]},
            tool_call_id="tc4",
        )
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_unknown_tool_rejected(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("apply_patch", {}, tool_call_id="tc5")
        assert result["status"] == "failed"
        assert result["error_code"] == "unknown_tool"

    @pytest.mark.asyncio
    async def test_invalid_args_rejected(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("read_allowed_file", {}, tool_call_id="tc6")
        assert result["status"] == "failed"
        assert result["error_code"] == "invalid_tool_arguments"
