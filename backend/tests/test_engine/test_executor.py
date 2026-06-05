"""Tests for the execution engine: blocker and executor."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.engine.blocker import Blocker, BlockResult
from bridle.models.node import NodeRecord


def _make_node(
    node_type: str = "code_change",
    depends_on: list | None = None,
    tests: list | None = None,
    metrics: dict | list | None = None,
    constraints: dict | list | None = None,
    **kwargs,
) -> NodeRecord:
    return NodeRecord(
        id=kwargs.get("id", "n1"),
        plan_id=kwargs.get("plan_id", "p1"),
        title=kwargs.get("title", "Test Node"),
        goal=kwargs.get("goal", "Test"),
        node_type=node_type,
        order=kwargs.get("order", 0),
        depends_on=depends_on or [],
        files=kwargs.get("files", []),
        tests=tests if tests is not None else ["pytest"],
        metrics=metrics if metrics is not None else {"cov": 80},
        constraints=constraints if constraints is not None else {"no_print": True},
        review_checks=kwargs.get("review_checks", ["check1"]),
        expected_outputs=kwargs.get("expected_outputs", {"exit": 0}),
        status=kwargs.get("status", "pending"),
    )


class TestBlocker:
    async def test_node_ready(self) -> None:
        node = _make_node()
        completed_ids: set[str] = set()
        result = Blocker.check(node, completed_ids)
        assert result.blocked is False

    async def test_node_blocked_by_unmet_dependency(self) -> None:
        node = _make_node(depends_on=["n2"])
        completed_ids: set[str] = set()
        result = Blocker.check(node, completed_ids)
        assert result.blocked is True
        assert "n2" in result.reason

    async def test_node_ready_with_met_dependency(self) -> None:
        node = _make_node(depends_on=["n2"])
        completed_ids: set[str] = {"n2"}
        result = Blocker.check(node, completed_ids)
        assert result.blocked is False

    async def test_node_blocked_missing_tests(self) -> None:
        node = _make_node(tests=[])
        result = Blocker.check(node, set())
        assert result.blocked is True
        assert "test" in result.reason.lower()

    async def test_metric_validation_blocked_missing_metrics(self) -> None:
        node = _make_node(node_type="metric_validation", metrics={})
        result = Blocker.check(node, set())
        assert result.blocked is True
        assert "metric" in result.reason.lower()

    async def test_code_change_missing_constraints_blocked(self) -> None:
        node = _make_node(node_type="code_change", constraints={})
        result = Blocker.check(node, set())
        assert result.blocked is True
        assert "constraint" in result.reason.lower()

    async def test_review_gate_blocked_missing_review_checks(self) -> None:
        node = _make_node(node_type="review_gate", review_checks=[])
        result = Blocker.check(node, set())
        assert result.blocked is True
        assert "review" in result.reason.lower()

    async def test_test_validation_without_tests_is_allowed(self) -> None:
        node = _make_node(node_type="test_validation", tests=[], constraints={})
        result = Blocker.check(node, set())
        assert result.blocked is False

    async def test_code_change_without_tests_still_blocked(self) -> None:
        node = _make_node(node_type="code_change", tests=[])
        result = Blocker.check(node, set())
        assert result.blocked is True
        assert "code_change" in result.reason

    async def test_review_gate_still_requires_review_checks(self) -> None:
        node = _make_node(node_type="review_gate", tests=[], review_checks=[])
        result = Blocker.check(node, set())
        assert result.blocked is True
        assert "review" in result.reason.lower()

    async def test_metric_validation_still_requires_metrics(self) -> None:
        node = _make_node(node_type="metric_validation", tests=[], metrics={})
        result = Blocker.check(node, set())
        assert result.blocked is True
        assert "metric" in result.reason.lower()

    async def test_test_validation_with_tests_ok(self) -> None:
        node = _make_node(node_type="test_validation", tests=["pytest"])
        result = Blocker.check(node, set())
        assert result.blocked is False


class TestExecutorPythonCommand:
    async def test_run_python_command_uses_subprocess_run_via_to_thread(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio
        import subprocess
        from unittest.mock import MagicMock

        from bridle.engine.executor import Executor

        run_called = False
        to_thread_called = False

        def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess:
            nonlocal run_called
            run_called = True
            return subprocess.CompletedProcess(
                args=["python", "-m", "pytest", "test_x.py"],
                returncode=0,
                stdout="ok\n",
                stderr="",
            )

        async def fake_to_thread(fn: object) -> subprocess.CompletedProcess:
            nonlocal to_thread_called
            to_thread_called = True
            assert callable(fn)
            return fn()  # type: ignore[misc]

        monkeypatch.setattr("bridle.engine.executor.subprocess.run", fake_run)
        monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(
            "bridle.engine.executor.shutil.which",
            MagicMock(return_value="python"),
        )

        executor = Executor(workspace=str(test_workspace))
        result = await executor.run_python_command("python -m pytest test_x.py")

        assert to_thread_called is True
        assert run_called is True
        assert result["exit_code"] == 0
        assert "ok" in result["stdout"]

    async def test_run_python_command_works_under_selector_event_loop(
        self,
        test_workspace: Path,
    ) -> None:
        import asyncio
        import sys

        from bridle.engine.executor import Executor

        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)
        try:
            executor = Executor(workspace=str(test_workspace))
            result = await executor.run_python_command(f"{sys.executable} -V")
            assert result["exit_code"] == 0
            assert "Python" in result["stdout"]
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    async def test_except_includes_exception_type_when_message_empty(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from unittest.mock import MagicMock

        from bridle.engine.executor import Executor

        def raise_not_implemented(*_args: object, **_kwargs: object) -> None:
            raise NotImplementedError()

        monkeypatch.setattr("bridle.engine.executor.subprocess.run", raise_not_implemented)
        monkeypatch.setattr(
            "bridle.engine.executor.shutil.which",
            MagicMock(return_value="python"),
        )

        executor = Executor(workspace=str(test_workspace))
        result = await executor.run_python_command("python -V")

        assert result["exit_code"] == -1
        assert "NotImplementedError" in result["stderr"]
        assert "(empty exception message)" in result["stderr"]

    async def test_run_command_adds_diagnostic_on_silent_negative_exit(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from bridle.engine.executor import Executor

        async def fake_shell(*_args, **_kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = -1
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)

        executor = Executor(workspace=str(test_workspace))
        result = await executor.run_command("python -m pytest test_x.py", env={"PATH": "C:\\fake"})

        assert result["exit_code"] == -1
        assert "subprocess returned" in result["stderr"]


class TestExecutor:
    async def test_execute_simple_command(self, test_workspace: Path) -> None:
        from bridle.engine.executor import Executor

        executor = Executor(workspace=str(test_workspace))
        result = await executor.run_command("echo hello")
        assert result["exit_code"] == 0
        assert "hello" in result["stdout"]

    async def test_execute_failing_command(self, test_workspace: Path) -> None:
        from bridle.engine.executor import Executor

        executor = Executor(workspace=str(test_workspace))
        result = await executor.run_command("exit 1")
        assert result["exit_code"] == 1

    async def test_execute_creates_run_dir(self, test_workspace: Path) -> None:
        from bridle.engine.executor import Executor

        executor = Executor(workspace=str(test_workspace))
        run_id = "test-run-001"
        result = await executor.run_command("echo test", run_id=run_id)
        assert result["exit_code"] == 0

    async def test_execute_captures_stderr(self, test_workspace: Path) -> None:
        import sys

        from bridle.engine.executor import Executor

        executor = Executor(workspace=str(test_workspace))
        result = await executor.run_command(
            f'"{sys.executable}" -c "import sys; sys.stderr.write(\'err\\n\')"'
        )
        assert "err" in result.get("stderr", "")


class TestExecutorOutputFallback:
    async def test_explicit_runs_dir_wins_over_global_config(self, test_workspace: Path) -> None:
        from bridle.config import get_config
        from bridle.engine.executor import Executor

        explicit_root = test_workspace / "explicit-runs"
        executor = Executor(workspace=str(test_workspace), runs_dir=explicit_root)
        result = await executor.run_command("echo explicit", run_id="explicit-run")
        assert result["exit_code"] == 0
        assert result["stdout_path"]
        assert Path(result["stdout_path"]) == (explicit_root / "explicit-run" / "stdout.log")
        assert Path(result["stdout_path"]).is_file()

        # sanity: global config exists, but is not used in this case
        assert Path(result["stdout_path"]) != (get_config().runs_dir / "explicit-run" / "stdout.log")

    async def test_workspace_does_not_override_global_runs_dir(self, test_workspace: Path) -> None:
        from bridle.config import get_config
        from bridle.engine.executor import Executor

        executor = Executor(workspace=str(test_workspace))
        result = await executor.run_command("echo global2", run_id="global-run-2")
        assert result["exit_code"] == 0
        assert result["stdout_path"]
        run_dir = get_config().runs_dir / "global-run-2"
        assert Path(result["stdout_path"]) == run_dir / "stdout.log"
        assert ".bridle-runs" not in result["stdout_path"]

    async def test_writes_output_without_global_workspace(self, tmp_path: Path) -> None:
        import bridle.config as cfg

        from bridle.engine.executor import Executor

        cfg._global_config = None
        executor = Executor(workspace=str(tmp_path))
        result = await executor.run_command("exit 2", run_id="fallback-run")
        assert result["exit_code"] == 2
        assert "Workspace not configured" not in result.get("stderr", "")
        assert result["stdout_path"]
        assert result["stderr_path"]
        assert Path(result["stdout_path"]).resolve().is_relative_to(tmp_path.resolve())
        assert ".bridle-runs" in result["stdout_path"]
        assert Path(result["stdout_path"]).is_file()
        assert Path(result["stderr_path"]).is_file()

    async def test_uses_global_runs_dir_when_no_workspace(self, test_workspace: Path) -> None:
        from bridle.config import get_config
        from bridle.engine.executor import Executor

        executor = Executor()
        result = await executor.run_command("echo global", run_id="global-run")
        assert result["exit_code"] == 0
        assert result["stdout_path"]
        run_dir = get_config().runs_dir / "global-run"
        assert Path(result["stdout_path"]) == run_dir / "stdout.log"
