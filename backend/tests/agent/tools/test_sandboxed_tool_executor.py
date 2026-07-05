"""Tests for SandboxedToolExecutor."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor


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
    executor = SandboxedToolExecutor(policy)
    # These tests exercise non-TDD behaviour (path validation, read access,
    # command policy). The TDD gate is covered by TestTDDEnforcement below.
    executor.tdd_state.bypass_for_test_setup()
    return policy, executor


class TestSandboxedToolExecutorRead:
    @pytest.mark.asyncio
    async def test_read_allowed_file(self, sandbox_setup: tuple[SandboxPolicy, SandboxedToolExecutor]) -> None:
        _policy, executor = sandbox_setup
        result = await executor.read_allowed_file("src/read_me.py")
        assert result["status"] == "completed"
        assert "hello sandbox" in result["content"]

    @pytest.mark.asyncio
    async def test_read_rejects_out_of_boundary(
        self, sandbox_setup: tuple[SandboxPolicy, SandboxedToolExecutor]
    ) -> None:
        _policy, executor = sandbox_setup
        result = await executor.read_allowed_file("../outside.py")
        assert result["status"] == "failed"
        assert result["error_code"] == "PathBoundaryError"


class TestSandboxedToolExecutorPatch:
    @pytest.mark.asyncio
    async def test_propose_patch_applies_to_sandbox_workspace(
        self,
        sandbox_setup: tuple[SandboxPolicy, SandboxedToolExecutor],
        test_workspace: Path,
    ) -> None:
        _policy, executor = sandbox_setup
        path = test_workspace / "src/read_me.py"
        result = await executor.propose_file_patch(
            "src/read_me.py",
            diff="@@ -1,1 +1,1 @@\n-hello sandbox\n+changed\n",
            change_type="modify",
        )
        assert result["status"] == "completed"
        assert path.read_text(encoding="utf-8") == "changed\n"
        assert result["patch"]["applied"] is True
        assert result["patch_staged"] is True
        assert result["patch_applied"] is True
        assert result["applied_path"] == "src/read_me.py"
        assert str(_policy.workspace_root) == result["sandbox_workspace"]
        assert "src/read_me.py" in result["sandbox_inputs"]


_CALC_ADD_DIFF = (
    "--- /dev/null\n"
    "+++ b/calc.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def add(a, b):\n"
    "+    return a + b\n"
)
_TEST_CALC_DIFF = (
    "--- /dev/null\n"
    "+++ b/calc_test.py\n"
    "@@ -0,0 +1,4 @@\n"
    "+import calc\n"
    "+\n"
    "+def test_add():\n"
    "+    assert calc.add(1, 2) == 3\n"
)


class TestSandboxPythonCommandRouting:
    @pytest.mark.asyncio
    async def test_python_test_command_routed_to_exec(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import AsyncMock

        policy = SandboxPolicy.for_run(
            run_id="run-python-route",
            node_id="node-python-route",
            workspace_root=test_workspace,
            allowed_files=[],
            node_tests=["python -m pytest test_x.py"],
        )
        executor = SandboxedToolExecutor(policy)

        run_python = AsyncMock(
            return_value={
                "exit_code": 0,
                "stdout": "1 passed",
                "stderr": "",
                "duration_ms": 10,
                "stdout_path": None,
                "stderr_path": None,
            }
        )
        run_shell = AsyncMock()
        monkeypatch.setattr(executor._executor, "run_python_command", run_python)
        monkeypatch.setattr(executor._executor, "run_command", run_shell)

        result = await executor.run_allowed_tests(["python -m pytest test_x.py"])

        run_python.assert_awaited_once()
        run_shell.assert_not_awaited()
        assert result["status"] == "completed"


class TestSandboxPatchStaging:
    @pytest.fixture
    def patch_staging_setup(
        self, test_workspace: Path
    ) -> tuple[SandboxPolicy, SandboxedToolExecutor, str]:
        calc = "calc.py"
        test_file = "calc_test.py"
        pytest_cmd = "python -m pytest --confcutdir=. calc_test.py -q"
        policy = SandboxPolicy.for_run(
            run_id="run-patch-stage",
            node_id="node-patch-stage",
            workspace_root=test_workspace,
            allowed_files=[calc, test_file],
            node_tests=[pytest_cmd],
        )
        executor = SandboxedToolExecutor(policy)
        # Patch-staging tests exercise diff/path semantics, not TDD order.
        executor.tdd_state.bypass_for_test_setup()
        return policy, executor, pytest_cmd

    @pytest.mark.asyncio
    async def test_patch_then_pytest_passes(
        self,
        patch_staging_setup: tuple[SandboxPolicy, SandboxedToolExecutor, str],
        test_workspace: Path,
    ) -> None:
        _policy, executor, pytest_cmd = patch_staging_setup

        r1 = await executor.propose_file_patch("calc.py", _CALC_ADD_DIFF, "add")
        assert r1["status"] == "completed"
        assert (test_workspace / "calc.py").is_file()

        r2 = await executor.propose_file_patch("calc_test.py", _TEST_CALC_DIFF, "add")
        assert r2["status"] == "completed"
        assert (test_workspace / "calc_test.py").is_file()

        test_result = await executor.run_allowed_tests([pytest_cmd])
        assert test_result["status"] == "completed", test_result
        assert test_result["results"][0]["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_rejects_c_drive_path(
        self,
        patch_staging_setup: tuple[SandboxPolicy, SandboxedToolExecutor, str],
    ) -> None:
        _policy, executor, _cmd = patch_staging_setup
        result = await executor.propose_file_patch(
            "C:/Windows/evil.py",
            _CALC_ADD_DIFF,
            "add",
        )
        assert result["status"] == "failed"
        assert result["error_code"] == "PathBoundaryError"

    @pytest.mark.asyncio
    async def test_rejects_path_outside_allowed_files(
        self,
        patch_staging_setup: tuple[SandboxPolicy, SandboxedToolExecutor, str],
    ) -> None:
        _policy, executor, _cmd = patch_staging_setup
        result = await executor.propose_file_patch("other.py", _CALC_ADD_DIFF, "add")
        assert result["status"] == "failed"
        assert result["error_code"] == "AccessRequestRequired"
        assert result["access_request"]["status"] == "pending_manual"

    @pytest.mark.asyncio
    async def test_rejects_invalid_diff(
        self,
        patch_staging_setup: tuple[SandboxPolicy, SandboxedToolExecutor, str],
    ) -> None:
        _policy, executor, _cmd = patch_staging_setup
        result = await executor.propose_file_patch("calc.py", "not a unified diff", "add")
        assert result["status"] == "failed"
        assert result["error_code"] == "InvalidDiff"
        assert any("recovery_hint" in err for err in result.get("errors", []))

    @pytest.mark.asyncio
    async def test_second_modify_updates_sandbox_file(
        self,
        patch_staging_setup: tuple[SandboxPolicy, SandboxedToolExecutor, str],
        test_workspace: Path,
    ) -> None:
        _policy, executor, _cmd = patch_staging_setup
        await executor.propose_file_patch("calc.py", _CALC_ADD_DIFF, "add")
        second = await executor.propose_file_patch(
            "calc.py",
            "@@ -1,2 +1,2 @@\n-def add(a, b):\n-    return a + b\n+def add(a, b):\n+    return a - b\n",
            "modify",
        )
        assert second["status"] == "completed"
        text = (test_workspace / "calc.py").read_text(encoding="utf-8")
        assert "return a - b" in text


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

    @pytest.mark.asyncio
    async def test_preserves_exit_code_without_global_workspace(self, tmp_path: Path) -> None:
        import bridle.config as cfg

        cfg._global_config = None
        policy = SandboxPolicy.for_run(
            run_id="no-global-ws",
            node_id="node-no-global",
            workspace_root=tmp_path,
            allowed_files=[],
            node_tests=["exit 4"],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.run_allowed_tests(["exit 4"])
        assert result["status"] == "failed"
        assert result["results"][0]["exit_code"] == 4
        assert "Workspace not configured" not in result["results"][0].get("stderr_preview", "")
        stdout_path = result["results"][0].get("stdout_path")
        assert stdout_path
        assert Path(stdout_path).resolve().is_relative_to(tmp_path.resolve())
        assert ".bridle-runs" in stdout_path

    @pytest.mark.asyncio
    async def test_nonzero_exit_marks_test_failure_retryable(self, tmp_path: Path) -> None:
        policy = SandboxPolicy.for_run(
            run_id="retryable-exit",
            node_id="node-retryable",
            workspace_root=tmp_path,
            allowed_files=[],
            node_tests=["exit 4"],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.run_allowed_tests(["exit 4"])
        assert result["status"] == "failed"
        assert result["error_code"] == "TestCommandFailed"
        assert result.get("retryable") is True
        assert result.get("next_action") == "patch_code_then_rerun_tests"
        assert result["results"][0]["exit_code"] == 4

    @pytest.mark.asyncio
    async def test_policy_rejected_command_not_retryable(self, test_workspace: Path) -> None:
        policy = SandboxPolicy.for_run(
            run_id="policy-no-retry",
            node_id="node-policy",
            workspace_root=test_workspace,
            allowed_files=[],
            node_tests=["echo ok"],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.run_allowed_tests(["npm install foo"])
        assert result["status"] == "failed"
        assert result["error_code"] == "CommandPolicyError"
        assert result.get("retryable") is not True
        assert result.get("next_action") != "patch_code_then_rerun_tests"


class TestSandboxedToolExecutorGrep:
    @pytest.mark.asyncio
    async def test_grep_finds_match_in_allowed_file(self, test_workspace: Path) -> None:
        allowed = "src/search_target.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("def hello():\n    pass\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-grep",
            node_id="node-grep",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.grep_code("hello")
        assert result["status"] == "completed"
        assert result["total_matches"] >= 1
        assert any(m["path"] == allowed for m in result["matches"])

    @pytest.mark.asyncio
    async def test_grep_does_not_search_invisible_files(self, test_workspace: Path) -> None:
        allowed = "src/visible.py"
        invisible = "src/hidden.py"
        (test_workspace / "src").mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("visible_func", encoding="utf-8")
        (test_workspace / invisible).write_text("hidden_func", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-grep2",
            node_id="node-grep2",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.grep_code("func")
        assert result["status"] == "completed"
        assert all(m["path"] != invisible for m in result["matches"])

    @pytest.mark.asyncio
    async def test_grep_case_insensitive_by_default(self, test_workspace: Path) -> None:
        allowed = "src/case_test.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("class MyClass:\n    pass\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-grep3",
            node_id="node-grep3",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.grep_code("myclass")
        assert result["status"] == "completed"
        assert result["total_matches"] >= 1

    @pytest.mark.asyncio
    async def test_grep_max_results_truncation(self, test_workspace: Path) -> None:
        allowed = "src/many_matches.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        lines = [f"target_keyword line {i}" for i in range(30)]
        (test_workspace / allowed).write_text("\n".join(lines), encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-grep4",
            node_id="node-grep4",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.grep_code("target_keyword", max_results=5)
        assert result["status"] == "completed"
        assert len(result["matches"]) == 5
        assert result["total_matches"] == 30
        assert result.get("truncated") is True

    @pytest.mark.asyncio
    async def test_grep_skips_binary_files(self, test_workspace: Path) -> None:
        allowed = "src/binary.bin"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_bytes(b"\x00\x01\x02\x03search\x00")
        policy = SandboxPolicy.for_run(
            run_id="run-grep5",
            node_id="node-grep5",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.grep_code("search")
        assert result["status"] == "completed"
        assert result["total_matches"] == 0

    @pytest.mark.asyncio
    async def test_grep_path_glob_filter(self, test_workspace: Path) -> None:
        py_file = "src/app.py"
        js_file = "src/app.js"
        (test_workspace / "src").mkdir(parents=True, exist_ok=True)
        (test_workspace / py_file).write_text("findme in python", encoding="utf-8")
        (test_workspace / js_file).write_text("findme in js", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-grep6",
            node_id="node-grep6",
            workspace_root=test_workspace,
            allowed_files=[py_file, js_file],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.grep_code("findme", path_glob="*.py")
        assert result["status"] == "completed"
        assert all(m["path"].endswith(".py") for m in result["matches"])


class TestSandboxedToolExecutorPatchValidation:
    @pytest.mark.asyncio
    async def test_propose_patch_rejects_invalid_diff(self, test_workspace: Path) -> None:
        allowed = "src/validate_me.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("line1\nline2\nline3\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-diff1",
            node_id="node-diff1",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state.bypass_for_test_setup()
        result = await executor.propose_file_patch(
            allowed,
            diff="not a valid diff",
            change_type="modify",
        )
        assert result["status"] == "failed"
        assert result["error_code"] == "InvalidDiff"

    @pytest.mark.asyncio
    async def test_propose_patch_rejects_mismatched_diff_header_path(self, test_workspace: Path) -> None:
        allowed = "src/validate_me.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("line1\nline2\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-diff-path",
            node_id="node-diff-path",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state.bypass_for_test_setup()
        result = await executor.propose_file_patch(
            allowed,
            diff="--- a/src/other.py\n+++ b/src/other.py\n@@ -1,2 +1,2 @@\n-line1\n+Line1\nline2\n",
            change_type="modify",
        )
        assert result["status"] == "failed"
        assert result["error_code"] == "InvalidDiff"
        assert "path" in result["errors"][0].lower()

    @pytest.mark.asyncio
    async def test_propose_patch_rejects_add_over_existing(self, test_workspace: Path) -> None:
        allowed = "src/existing.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("existing", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-diff2",
            node_id="node-diff2",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state.bypass_for_test_setup()
        result = await executor.propose_file_patch(
            allowed,
            diff="@@ -0,0 +1,1 @@\n+new\n",
            change_type="add",
        )
        assert result["status"] == "failed"
        assert result["error_code"] == "InvalidDiff"

    @pytest.mark.asyncio
    async def test_propose_patch_rejects_modify_nonexistent(self, test_workspace: Path) -> None:
        allowed = "src/nope.py"
        policy = SandboxPolicy.for_run(
            run_id="run-diff3",
            node_id="node-diff3",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state.bypass_for_test_setup()
        result = await executor.propose_file_patch(
            allowed,
            diff="@@ -1,1 +1,1 @@\n-old\n+new\n",
            change_type="modify",
        )
        assert result["status"] == "failed"
        assert result["error_code"] == "InvalidDiff"

    @pytest.mark.asyncio
    async def test_propose_patch_valid_modify_includes_dry_run(self, test_workspace: Path) -> None:
        allowed = "src/modify_me.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("hello\nworld\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-diff4",
            node_id="node-diff4",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state.bypass_for_test_setup()
        result = await executor.propose_file_patch(
            allowed,
            diff="@@ -1,2 +1,2 @@\n-hello\n+Hello\nworld\n",
            change_type="modify",
        )
        assert result["status"] == "completed"
        assert result["patch"]["applied"] is True
        assert "dry_run" in result
        assert result["dry_run"]["valid"] is True
        assert result["dry_run"]["hunk_count"] == 1
        assert result["dry_run"]["added_lines"] == 1
        assert result["dry_run"]["removed_lines"] == 1
        assert (test_workspace / allowed).read_text(encoding="utf-8").startswith("Hello")

    @pytest.mark.asyncio
    async def test_propose_patch_valid_remove_includes_valid_dry_run_without_new_text(
        self,
        test_workspace: Path,
    ) -> None:
        allowed = "src/remove_me.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("delete me\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-diff-remove",
            node_id="node-diff-remove",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state.bypass_for_test_setup()
        result = await executor.propose_file_patch(
            allowed,
            diff="--- a/src/remove_me.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-delete me\n",
            change_type="remove",
        )

        assert result["status"] == "completed"
        assert result["patch"]["applied"] is True
        assert result["dry_run"]["valid"] is True
        assert result["dry_run"]["hunk_count"] == 1
        assert result["dry_run"]["added_lines"] == 0
        assert result["dry_run"]["removed_lines"] == 1
        assert "new_text" not in result["dry_run"]
        assert not (test_workspace / allowed).is_file()

    @pytest.mark.asyncio
    async def test_propose_patch_writes_to_sandbox_workspace(
        self,
        test_workspace: Path,
    ) -> None:
        allowed = "src/no_write.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("original\n", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-diff5",
            node_id="node-diff5",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state.bypass_for_test_setup()
        await executor.propose_file_patch(
            allowed,
            diff="@@ -1,1 +1,1 @@\n-original\n+changed\n",
            change_type="modify",
        )
        assert (test_workspace / allowed).read_text(encoding="utf-8") == "changed\n"


class TestSandboxedToolExecutorWebSearch:
    @pytest.mark.asyncio
    async def test_web_search_disabled_when_network_not_allowed(self, test_workspace: Path) -> None:
        policy = SandboxPolicy.for_run(
            run_id="run-ws1",
            node_id="node-ws1",
            workspace_root=test_workspace,
            allowed_files=[],
            node_tests=[],
            network_allowed=False,
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.web_search("test query")
        assert result["status"] == "failed"
        assert result["error_code"] == "NetworkDisabled"

    @pytest.mark.asyncio
    async def test_web_search_uses_fake_client(self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        policy = SandboxPolicy.for_run(
            run_id="run-ws2",
            node_id="node-ws2",
            workspace_root=test_workspace,
            allowed_files=[],
            node_tests=[],
            network_allowed=True,
        )
        executor = SandboxedToolExecutor(policy)

        class FakeResponse:
            def read(self):
                return json.dumps({
                    "Abstract": "Python docs",
                    "AbstractURL": "https://docs.python.org/3/",
                    "RelatedTopics": [
                        {"Text": "Python tutorial", "FirstURL": "https://docs.python.org/3/tutorial/"},
                    ],
                }).encode()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        captured_proxy = {}

        original_build_opener = urllib.request.build_opener
        def fake_build_opener(*args, **kwargs):
            captured_proxy["called"] = True
            for arg in args:
                if isinstance(arg, urllib.request.ProxyHandler):
                    captured_proxy["handler"] = arg
            opener = original_build_opener()

            def fake_open(req, timeout=None):
                captured_proxy["proxy_in_env"] = True
                return FakeResponse()

            opener.open = fake_open
            return opener

        monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
        result = await executor.web_search("python tutorial")
        assert result["status"] == "completed"
        assert result["search_results"]
        assert captured_proxy.get("called") is True


class TestSandboxedToolExecutorResultFields:
    @pytest.mark.asyncio
    async def test_completed_result_has_tool_name_and_duration(self, test_workspace: Path) -> None:
        allowed = "src/field_test.py"
        (test_workspace / allowed).parent.mkdir(parents=True, exist_ok=True)
        (test_workspace / allowed).write_text("x=1", encoding="utf-8")
        policy = SandboxPolicy.for_run(
            run_id="run-fields",
            node_id="node-fields",
            workspace_root=test_workspace,
            allowed_files=[allowed],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        result = await executor.read_allowed_file(allowed)
        assert result["status"] == "completed"
        assert "tool_name" in result
        assert "duration_ms" in result


_IMPL_ADD_DIFF = (
    "--- /dev/null\n"
    "+++ b/src/sample.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def hello():\n"
    "+    return 'hi'\n"
)
_TEST_SAMPLE_DIFF = (
    "--- /dev/null\n"
    "+++ b/tests/test_sample.py\n"
    "@@ -0,0 +1,4 @@\n"
    "+from src import sample\n"
    "+\n"
    "+def test_hello():\n"
    "+    assert sample.hello() == 'hi'\n"
)


class TestTDDEnforcement:
    """The sandbox refuses propose_file_patch on implementation files until
    the agent has (a) written tests and (b) seen them fail (RED)."""

    @pytest.fixture
    def tdd_setup(self, test_workspace: Path) -> SandboxedToolExecutor:
        policy = SandboxPolicy.for_run(
            run_id="run-tdd",
            node_id="node-tdd",
            workspace_root=test_workspace,
            allowed_files=["src/sample.py", "tests/test_sample.py"],
            node_tests=["python -m pytest tests/test_sample.py -q"],
        )
        return SandboxedToolExecutor(policy)

    @pytest.mark.asyncio
    async def test_implementation_patch_rejected_when_no_test_patched(
        self, tdd_setup: SandboxedToolExecutor
    ) -> None:
        result = await tdd_setup.propose_file_patch(
            "src/sample.py", _IMPL_ADD_DIFF, "add"
        )
        assert result["status"] == "failed"
        assert result["error_code"] == "TDD_TEST_REQUIRED_FIRST"
        # Error message must guide the agent to the next step.
        assert any(
            "test" in err.lower() and "first" in err.lower()
            for err in result["errors"]
        )

    @pytest.mark.asyncio
    async def test_test_patch_always_allowed(
        self, tdd_setup: SandboxedToolExecutor
    ) -> None:
        result = await tdd_setup.propose_file_patch(
            "tests/test_sample.py", _TEST_SAMPLE_DIFF, "add"
        )
        assert result["status"] == "completed"
        assert tdd_setup.tdd_state.has_test_patch_applied is True
        # has_red_test_run not flipped yet -that requires actually running tests.
        assert tdd_setup.tdd_state.has_red_test_run is False

    @pytest.mark.asyncio
    async def test_implementation_rejected_after_test_patch_without_red_run(
        self, tdd_setup: SandboxedToolExecutor
    ) -> None:
        await tdd_setup.propose_file_patch(
            "tests/test_sample.py", _TEST_SAMPLE_DIFF, "add"
        )
        result = await tdd_setup.propose_file_patch(
            "src/sample.py", _IMPL_ADD_DIFF, "add"
        )
        assert result["status"] == "failed"
        assert result["error_code"] == "TDD_RED_REQUIRED"
        assert any(
            "run_allowed_tests" in err or "fail" in err.lower()
            for err in result["errors"]
        )

    @pytest.mark.asyncio
    async def test_implementation_allowed_after_red_test_run(
        self, tdd_setup: SandboxedToolExecutor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        await tdd_setup.propose_file_patch(
            "tests/test_sample.py", _TEST_SAMPLE_DIFF, "add"
        )
        # Simulate a RED test run without invoking the real pytest subprocess.
        red_run = AsyncMock(
            return_value={
                "exit_code": 1,
                "stdout": "1 failed",
                "stderr": "",
                "duration_ms": 5,
                "stdout_path": None,
                "stderr_path": None,
            }
        )
        monkeypatch.setattr(tdd_setup._executor, "run_python_command", red_run)
        test_result = await tdd_setup.run_allowed_tests(
            ["python -m pytest tests/test_sample.py -q"]
        )
        assert test_result["status"] == "failed"
        assert tdd_setup.tdd_state.has_red_test_run is True

        impl = await tdd_setup.propose_file_patch(
            "src/sample.py", _IMPL_ADD_DIFF, "add"
        )
        assert impl["status"] == "completed"

    @pytest.mark.asyncio
    async def test_green_run_records_green_state(
        self, tdd_setup: SandboxedToolExecutor, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        green_run = AsyncMock(
            return_value={
                "exit_code": 0,
                "stdout": "1 passed",
                "stderr": "",
                "duration_ms": 5,
                "stdout_path": None,
                "stderr_path": None,
            }
        )
        monkeypatch.setattr(tdd_setup._executor, "run_python_command", green_run)
        result = await tdd_setup.run_allowed_tests(
            ["python -m pytest tests/test_sample.py -q"]
        )
        assert result["status"] == "completed"
        assert tdd_setup.tdd_state.has_green_test_run is True
        assert tdd_setup.tdd_state.has_red_test_run is False

    @pytest.mark.asyncio
    async def test_state_does_not_leak_across_executors(
        self, test_workspace: Path
    ) -> None:
        # Two executors ->independent TDD state.
        policy = SandboxPolicy.for_run(
            run_id="run-iso",
            node_id="node-iso",
            workspace_root=test_workspace,
            allowed_files=["src/sample.py", "tests/test_sample.py"],
            node_tests=[],
        )
        a = SandboxedToolExecutor(policy)
        b = SandboxedToolExecutor(policy)
        a.tdd_state.bypass_for_test_setup()
        assert b.tdd_state.has_test_patch_applied is False
        assert b.tdd_state.has_red_test_run is False


class TestTDDDisableEnforcement:
    """Regression: trusted batch apply path must be able to disable the TDD
    gate so the backend's _run_sandbox_tests staging step doesn't reject
    src/ patches just because the staging executor never ran the tests."""

    @pytest.mark.asyncio
    async def test_disable_enforcement_lets_impl_patch_apply_directly(
        self, test_workspace: Path
    ) -> None:
        policy = SandboxPolicy.for_run(
            run_id="run-disable",
            node_id="node-disable",
            workspace_root=test_workspace,
            allowed_files=["src/sample.py", "tests/test_sample.py"],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state.disable_enforcement()

        # Apply impl directly without ever patching a test or running tests.
        # In agent-driven mode this would be rejected with TDD_TEST_REQUIRED_FIRST.
        result = await executor.propose_file_patch(
            "src/sample.py", _IMPL_ADD_DIFF, "add"
        )
        assert result["status"] == "completed"

    def test_bypass_for_test_setup_aliases_disable_enforcement(self) -> None:
        # Backwards-compat: old callsites use the original name.
        from bridle.agent.tools.sandboxed_executor import TDDStateTracker

        tracker = TDDStateTracker()
        tracker.bypass_for_test_setup()
        assert tracker.has_test_patch_applied is True


class RecordingContainerBackend:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def run_allowed_tests(self, commands: list[str], *, policy) -> dict[str, Any]:
        self.calls.append(list(commands))
        return {
            "status": "completed",
            "results": [
                {
                    "command": commands[0] if commands else "",
                    "policy_rejected": False,
                    "exit_code": 0,
                    "stdout_preview": "ok",
                    "stderr_preview": "",
                    "timed_out": False,
                }
            ],
        }


class TestSandboxedToolExecutorContainerRouting:
    @pytest.mark.asyncio
    async def test_run_allowed_tests_rejects_before_container_backend(self, test_workspace: Path) -> None:
        candidate = test_workspace / ".bridle" / "runtime" / "candidates" / "route-1" / "project"
        candidate.mkdir(parents=True)
        allowed = "python -m pytest tests/test_a.py -q"
        policy = SandboxPolicy.for_run(
            run_id="run-route",
            node_id="node-route",
            workspace_root=candidate,
            allowed_files=["src/a.py"],
            node_tests=[allowed],
        )
        recording = RecordingContainerBackend()
        executor = SandboxedToolExecutor(policy, test_backend=recording)
        result = await executor.run_allowed_tests(["echo forbidden"])
        assert result["status"] == "failed"
        assert result["error_code"] == "CommandPolicyError"
        assert recording.calls == []

    @pytest.mark.asyncio
    async def test_run_allowed_tests_routes_to_container_backend(self, test_workspace: Path) -> None:
        candidate = test_workspace / ".bridle" / "runtime" / "candidates" / "route-1" / "project"
        candidate.mkdir(parents=True)
        allowed = "python -m pytest tests/test_a.py -q"
        policy = SandboxPolicy.for_run(
            run_id="run-route",
            node_id="node-route",
            workspace_root=candidate,
            allowed_files=["src/a.py"],
            node_tests=[allowed],
        )
        recording = RecordingContainerBackend()
        executor = SandboxedToolExecutor(policy, test_backend=recording)
        result = await executor.run_allowed_tests([allowed])
        assert result["status"] == "completed"
        assert recording.calls == [[allowed]]

