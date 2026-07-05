"""Integration tests for sandbox file access request flow."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor

_INIT_ADD_DIFF = (
    "--- /dev/null\n"
    "+++ b/src/__init__.py\n"
    "@@ -0,0 +1 @@\n+\"\"\"init\"\"\"\n"
)
_NEW_MODULE_DIFF = (
    "--- /dev/null\n"
    "+++ b/src/new_module.py\n"
    "@@ -0,0 +1 @@\n+x = 1\n"
)


def _executor_without_tdd(policy: SandboxPolicy) -> SandboxedToolExecutor:
    executor = SandboxedToolExecutor(policy)
    executor.tdd_state.disable_enforcement()
    return executor


class TestAccessRequestFlow:
    @pytest.mark.asyncio
    async def test_duplicate_pending_request_is_deduped(self, test_workspace: Path) -> None:
        policy = SandboxPolicy.for_run(
            run_id="run-dedup",
            node_id="node-dedup",
            workspace_root=test_workspace,
            allowed_files=["src/main.py"],
            node_tests=["echo ok"],
        )
        executor = _executor_without_tdd(policy)
        for _ in range(2):
            result = await executor.propose_file_patch(
                "src/new_module.py",
                _NEW_MODULE_DIFF,
                "add",
            )
            assert result["status"] == "failed"
            assert result["error_code"] == "AccessRequestRequired"
        records = executor.consume_access_records()
        matching = [r for r in records if r.get("normalized_path") == "src/new_module.py"]
        assert len(matching) == 1
        assert not (test_workspace / "src/new_module.py").exists()

    @pytest.mark.asyncio
    async def test_auto_approve_then_patch_applies(self, test_workspace: Path) -> None:
        (test_workspace / "src").mkdir(parents=True, exist_ok=True)
        (test_workspace / "src/main.py").write_text("v = 0\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-approve",
            node_id="node-approve",
            workspace_root=test_workspace,
            allowed_files=["src/main.py"],
            node_tests=["echo ok"],
        )
        executor = _executor_without_tdd(policy)
        add_result = await executor.propose_file_patch(
            "src/__init__.py",
            _INIT_ADD_DIFF,
            "add",
        )
        assert add_result["status"] == "completed"
        assert add_result.get("access_request", {}).get("status") == "auto_approved"
        assert (test_workspace / "src/__init__.py").is_file()

        modify_result = await executor.propose_file_patch(
            "src/__init__.py",
            "@@ -1,1 +1,1 @@\n-\"\"\"init\"\"\"\n+\"\"\"pkg\"\"\"\n",
            "modify",
        )
        assert modify_result["status"] == "completed"
        assert (test_workspace / "src/__init__.py").read_text(encoding="utf-8") == "\"\"\"pkg\"\"\"\n"

    @pytest.mark.asyncio
    async def test_unapproved_high_risk_does_not_apply_patch(self, test_workspace: Path) -> None:
        policy = SandboxPolicy.for_run(
            run_id="run-block",
            node_id="node-block",
            workspace_root=test_workspace,
            allowed_files=["src/main.py"],
            node_tests=["echo ok"],
        )
        executor = _executor_without_tdd(policy)
        result = await executor.propose_file_patch(
            "src/new_module.py",
            _NEW_MODULE_DIFF,
            "add",
        )
        assert result["status"] == "failed"
        assert result["access_request"]["status"] == "pending_manual"
        assert not (test_workspace / "src/new_module.py").exists()

    @pytest.mark.asyncio
    async def test_grep_finds_auto_approved_file_after_patch(self, test_workspace: Path) -> None:
        (test_workspace / "src").mkdir(parents=True, exist_ok=True)
        (test_workspace / "src/main.py").write_text("main_only = True\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-grep",
            node_id="node-grep",
            workspace_root=test_workspace,
            allowed_files=["src/main.py"],
            node_tests=["echo ok"],
        )
        executor = _executor_without_tdd(policy)
        patch_result = await executor.propose_file_patch(
            "src/__init__.py",
            _INIT_ADD_DIFF,
            "add",
        )
        assert patch_result["status"] == "completed"

        grep_result = await executor.grep_code('"""init"""')
        assert grep_result["status"] == "completed"
        paths = {m["path"] for m in grep_result["matches"]}
        assert "src/__init__.py" in paths

    @pytest.mark.asyncio
    async def test_grep_does_not_find_pending_manual_file(self, test_workspace: Path) -> None:
        (test_workspace / "src").mkdir(parents=True, exist_ok=True)
        (test_workspace / "src/main.py").write_text("main_only = True\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-grep-block",
            node_id="node-grep-block",
            workspace_root=test_workspace,
            allowed_files=["src/main.py"],
            node_tests=["echo ok"],
        )
        executor = _executor_without_tdd(policy)
        await executor.propose_file_patch("src/new_module.py", _NEW_MODULE_DIFF, "add")

        grep_result = await executor.grep_code("x = 1")
        assert grep_result["status"] == "completed"
        paths = {m["path"] for m in grep_result["matches"]}
        assert "src/new_module.py" not in paths
        assert not (test_workspace / "src/new_module.py").exists()

