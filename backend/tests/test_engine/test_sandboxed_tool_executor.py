"""Tests for SandboxedToolExecutor."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.engine.sandbox_policy import SandboxPolicy
from bridle.engine.sandboxed_tool_executor import SandboxedToolExecutor


@pytest.fixture
def sandbox_setup(test_workspace: Path) -> tuple[SandboxPolicy, SandboxedToolExecutor]:
    allowed = "src/read_me.py"
    target = test_workspace / allowed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello sandbox", encoding="utf-8")

    policy = SandboxPolicy.for_run(
        run_id="run-exec",
        node_id="node-exec",
        workspace_root=test_workspace,
        allowed_files=[allowed],
        node_tests=["echo sandbox-test"],
    )
    return policy, SandboxedToolExecutor(policy)


class TestSandboxedToolExecutorRead:
    @pytest.mark.asyncio
    async def test_read_allowed_file(self, sandbox_setup: tuple[SandboxPolicy, SandboxedToolExecutor]) -> None:
        _policy, executor = sandbox_setup
        result = await executor.read_allowed_file("src/read_me.py")
        assert result["status"] == "completed"
        assert "hello sandbox" in result["content"]

    @pytest.mark.asyncio
    async def test_read_rejects_out_of_boundary(self, sandbox_setup: tuple[SandboxPolicy, SandboxedToolExecutor]) -> None:
        _policy, executor = sandbox_setup
        result = await executor.read_allowed_file("../outside.py")
        assert result["status"] == "failed"
        assert result["error_code"] == "PathBoundaryError"


class TestSandboxedToolExecutorPatch:
    @pytest.mark.asyncio
    async def test_propose_patch_does_not_write_disk(
        self,
        sandbox_setup: tuple[SandboxPolicy, SandboxedToolExecutor],
        test_workspace: Path,
    ) -> None:
        _policy, executor = sandbox_setup
        path = test_workspace / "src/read_me.py"
        before = path.read_text(encoding="utf-8")
        result = await executor.propose_file_patch(
            "src/read_me.py",
            diff="--- a\n+++ b\n@@\n+changed\n",
            change_type="modify",
        )
        assert result["status"] == "completed"
        assert path.read_text(encoding="utf-8") == before
        assert result["patch"]["path"] == "src/read_me.py"


class TestSandboxedToolExecutorTests:
    @pytest.mark.asyncio
    async def test_run_allowed_tests_echo(
        self,
        sandbox_setup: tuple[SandboxPolicy, SandboxedToolExecutor],
    ) -> None:
        _policy, executor = sandbox_setup
        result = await executor.run_allowed_tests(["echo sandbox-test"])
        assert result["status"] == "completed"
        assert result["results"][0]["exit_code"] == 0
        assert "sandbox-test" in result["results"][0]["stdout_preview"]

    @pytest.mark.asyncio
    async def test_rejects_npm_install(self, sandbox_setup: tuple[SandboxPolicy, SandboxedToolExecutor]) -> None:
        policy, executor = sandbox_setup
        policy_with_install = SandboxPolicy.for_run(
            run_id=policy.run_id,
            node_id=policy.node_id,
            workspace_root=policy.workspace_root,
            allowed_files=list(policy.allowed_files),
            node_tests=["npm install foo"],
        )
        bad = SandboxedToolExecutor(policy_with_install)
        result = await bad.run_allowed_tests(["npm install foo"])
        assert result["status"] == "failed"
        assert result["results"][0]["policy_rejected"] is True

    @pytest.mark.asyncio
    async def test_rejects_command_not_in_node_tests(
        self,
        sandbox_setup: tuple[SandboxPolicy, SandboxedToolExecutor],
    ) -> None:
        _policy, executor = sandbox_setup
        result = await executor.run_allowed_tests(["echo other-cmd"])
        assert result["status"] == "failed"

    @pytest.mark.asyncio
    async def test_stdout_preview_truncated(
        self,
        test_workspace: Path,
    ) -> None:
        policy = SandboxPolicy.for_run(
            run_id="run-trunc",
            node_id="n",
            workspace_root=test_workspace,
            allowed_files=[],
            node_tests=['echo "' + ("x" * 5000) + '"'],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.run_allowed_tests(['echo "' + ("x" * 5000) + '"'])
        preview = result["results"][0]["stdout_preview"]
        assert len(preview) <= executor.stdout_preview_limit + 50

    @pytest.mark.asyncio
    async def test_sandbox_command_cannot_read_custom_secret_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
        test_workspace: Path,
    ) -> None:
        monkeypatch.setenv("BRIDLE_SECRET_TOKEN", "super-secret-value")
        command = "echo %BRIDLE_SECRET_TOKEN%"
        policy = SandboxPolicy.for_run(
            run_id="run-env",
            node_id="n",
            workspace_root=test_workspace,
            allowed_files=[],
            node_tests=[command],
        )
        executor = SandboxedToolExecutor(policy)

        result = await executor.run_allowed_tests([command])

        assert result["status"] == "completed"
        assert "super-secret-value" not in result["results"][0]["stdout_preview"]
