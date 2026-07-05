"""Tests for AgentToolRegistry."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.registry import AgentToolRegistry
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor


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
    executor = SandboxedToolExecutor(policy)
    executor.tdd_state.disable_enforcement()
    return AgentToolRegistry(executor)


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
    async def test_propose_patch_applies_in_sandbox(
        self,
        registry: AgentToolRegistry,
        test_workspace: Path,
    ) -> None:
        path = test_workspace / "src/tool.py"
        result = await registry.execute(
            "propose_file_patch",
            {"path": "src/tool.py", "change_type": "modify",
             "diff": "@@ -1,1 +1,1 @@\n-tool content\n+tool modified\n"},
            tool_call_id="tc3",
        )
        assert result["status"] == "completed"
        assert path.read_text(encoding="utf-8") == "tool modified\n"
        assert result["patch_applied"] is True

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

    @pytest.mark.asyncio
    async def test_grep_code_empty_query_rejected(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("grep_code", {"query": ""}, tool_call_id="tc7")
        assert result["status"] == "failed"
        assert result["error_code"] == "invalid_tool_arguments"

    @pytest.mark.asyncio
    async def test_web_search_empty_query_rejected(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("web_search", {"query": ""}, tool_call_id="tc8")
        assert result["status"] == "failed"
        assert result["error_code"] == "invalid_tool_arguments"


class TestProposeFilePatchDescriptor:
    def test_descriptor_describes_sandbox_apply_semantics(self) -> None:
        descriptors = AgentToolRegistry.tool_descriptors()
        patch = next(d for d in descriptors if d.name == "propose_file_patch")
        combined = " ".join(
            (patch.purpose, patch.when_to_use, patch.output_summary, patch.constraints)
        ).lower()
        assert "sandbox" in combined
        assert "allowed test" in combined or "run_allowed_tests" in combined
        assert "production" in combined
        assert "without writing to disk" not in combined
        assert "does not write to disk" not in combined


class TestRunAllowedTestsDescriptor:
    def test_descriptor_forbids_cd_and_requires_exact_allowlist(self) -> None:
        descriptors = AgentToolRegistry.tool_descriptors()
        tests_tool = next(d for d in descriptors if d.name == "run_allowed_tests")
        combined = " ".join(
            (tests_tool.purpose, tests_tool.when_to_use, tests_tool.output_summary, tests_tool.constraints)
        ).lower()
        assert "sandbox" in combined
        assert "allowlist" in combined or "allowlisted" in combined
        assert "cd" in combined
        assert "&&" in combined or "shell" in combined
        assert "exact" in combined or "verbatim" in combined or "precise" in combined


class TestFromContextNetworkAllowed:
    def test_from_context_preserves_network_allowed_true(self, test_workspace: Path) -> None:
        from bridle.agent.runtime.schemas import AgentContext

        ctx = AgentContext(
            instruction="test",
            node={"id": "n1", "title": "t", "goal": "g"},
            allowed_files=["src/a.py"],
            tests=["echo ok"],
            metrics={},
            constraints={},
            review_checks=[],
            expected_outputs={},
            accessible_context={},
            tool_capabilities={
                "sandbox": {
                    "run_id": "r1",
                    "node_id": "n1",
                    "workspace_root": str(test_workspace),
                    "allowed_files": ["src/a.py"],
                    "network_allowed": True,
                },
            },
        )
        registry = AgentToolRegistry.from_context(ctx)
        assert registry._policy.network_allowed is True

    def test_from_context_defaults_network_allowed_false(self, test_workspace: Path) -> None:
        from bridle.agent.runtime.schemas import AgentContext

        ctx = AgentContext(
            instruction="test",
            node={"id": "n1", "title": "t", "goal": "g"},
            allowed_files=["src/a.py"],
            tests=["echo ok"],
            metrics={},
            constraints={},
            review_checks=[],
            expected_outputs={},
            accessible_context={},
            tool_capabilities={
                "sandbox": {
                    "run_id": "r2",
                    "node_id": "n2",
                    "workspace_root": str(test_workspace),
                    "allowed_files": ["src/a.py"],
                },
            },
        )
        registry = AgentToolRegistry.from_context(ctx)
        assert registry._policy.network_allowed is False


class TestToolErrorClassification:
    @pytest.mark.asyncio
    async def test_invalid_args_has_argument_category(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("read_allowed_file", {}, tool_call_id="tc10")
        assert result["status"] == "failed"
        assert result["category"] == "argument"
        assert result["retryable"] is False

    @pytest.mark.asyncio
    async def test_path_boundary_has_policy_category(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("read_allowed_file", {"path": "../x.py"}, tool_call_id="tc11")
        assert result["status"] == "failed"
        assert result["category"] == "policy"
        assert result["retryable"] is False

    @pytest.mark.asyncio
    async def test_unknown_tool_has_argument_category(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("nonexistent_tool", {}, tool_call_id="tc12")
        assert result["status"] == "failed"
        assert result["category"] == "argument"
        assert result["retryable"] is False

    @pytest.mark.asyncio
    async def test_completed_has_success_category(self, registry: AgentToolRegistry) -> None:
        result = await registry.execute("read_allowed_file", {"path": "src/tool.py"}, tool_call_id="tc13")
        assert result["status"] == "completed"
        assert result["category"] == "success"
        assert result["retryable"] is False

