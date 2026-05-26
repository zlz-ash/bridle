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

    async def test_test_validation_needs_tests(self) -> None:
        node = _make_node(node_type="test_validation", tests=[])
        result = Blocker.check(node, set())
        assert result.blocked is True

    async def test_test_validation_with_tests_ok(self) -> None:
        node = _make_node(node_type="test_validation", tests=["pytest"])
        result = Blocker.check(node, set())
        assert result.blocked is False


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
