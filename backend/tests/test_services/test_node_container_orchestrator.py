"""Tests for node container orchestration."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.engine.container_runner import ContainerResult, FakeContainerRunner
from bridle.engine.container_workspace import ContainerWorkspaceBuilder
from bridle.services.node_container_orchestrator import NodeContainerError, NodeContainerOrchestrator


class TestNodeContainerOrchestrator:
    def test_waits_for_container_output_before_returning(self, test_workspace: Path) -> None:
        workspace = ContainerWorkspaceBuilder(test_workspace).build_node_workspace(
            run_id="run-wait",
            node_id="node-1",
            read_set=[],
            write_set=[],
            readonly_context=[],
            interfaces={},
            tests=[],
            metrics={},
            conflict_contributions=[],
        )
        runner = FakeContainerRunner(workspace_root=test_workspace)
        wait_called = False

        def wait(container_id: str, timeout_seconds: int):
            nonlocal wait_called
            wait_called = True
            output = workspace.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "manifest.json").write_text("{}", encoding="utf-8")
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="stopped",
                network_mode=current.network_mode,
                health="healthy",
                exit_code=0,
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.wait = wait  # type: ignore[attr-defined]
        result = NodeContainerOrchestrator(test_workspace, runner=runner).run_node_container(
            run_id="run-wait",
            node_id="node-1",
            workspace_root=workspace.root,
        )

        assert wait_called
        assert result["container_status"] == "stopped"
        assert result["container_health"] == "healthy"

    def test_wait_timeout_cleans_up_container(self, test_workspace: Path) -> None:
        workspace = ContainerWorkspaceBuilder(test_workspace).build_node_workspace(
            run_id="run-timeout",
            node_id="node-1",
            read_set=[],
            write_set=[],
            readonly_context=[],
            interfaces={},
            tests=[],
            metrics={},
            conflict_contributions=[],
        )
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def wait(_container_id: str, _timeout_seconds: int):
            raise TimeoutError("timed out")

        runner.wait = wait  # type: ignore[attr-defined]
        with pytest.raises(NodeContainerError, match="container_wait_timeout") as exc_info:
            NodeContainerOrchestrator(test_workspace, runner=runner).run_node_container(
                run_id="run-timeout",
                node_id="node-1",
                workspace_root=workspace.root,
            )

        container_id = exc_info.value.detail["container_id"]
        assert runner._containers[container_id][1].status == "stopped"
        assert (workspace.root / "diagnostics" / "wait.error").exists()

    def test_starts_node_container_with_workspace_mount(self, test_workspace: Path) -> None:
        (test_workspace / "src").mkdir(exist_ok=True)
        (test_workspace / "src" / "a.py").write_text("x\n", encoding="utf-8")
        workspace = ContainerWorkspaceBuilder(test_workspace).build_node_workspace(
            run_id="run-1",
            node_id="node-1",
            read_set=[],
            write_set=["src/a.py"],
            readonly_context=[],
            interfaces={},
            tests=[],
            metrics={},
            conflict_contributions=[],
        )
        runner = FakeContainerRunner(workspace_root=test_workspace)
        result = NodeContainerOrchestrator(test_workspace, runner=runner).run_node_container(
            run_id="run-1",
            node_id="node-1",
            workspace_root=workspace.root,
        )

        assert result["container_id"].startswith("fake-container-")
        assert result["container_status"] == "stopped"
        assert result["container_health"] == "healthy"
        assert result["diagnostic_path"]

    def test_cleans_up_on_unhealthy_inspect(self, test_workspace: Path) -> None:
        workspace = ContainerWorkspaceBuilder(test_workspace).build_node_workspace(
            run_id="run-unhealthy",
            node_id="node-1",
            read_set=[],
            write_set=[],
            readonly_context=[],
            interfaces={},
            tests=[],
            metrics={},
            conflict_contributions=[],
        )
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def unhealthy_inspect(container_id: str):
            from bridle.engine.container_runner import ContainerResult

            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="failed",
                network_mode=current.network_mode,
                health="unhealthy",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = unhealthy_inspect  # type: ignore[method-assign]
        orch = NodeContainerOrchestrator(test_workspace, runner=runner)
        with pytest.raises(NodeContainerError, match="container_health_failed") as exc_info:
            orch.run_node_container(
                run_id="run-unhealthy",
                node_id="node-1",
                workspace_root=workspace.root,
            )
        container_id = exc_info.value.detail["container_id"]
        assert runner._containers[container_id][1].status == "stopped"

    def test_rejects_workspace_root_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        with pytest.raises(NodeContainerError, match="workspace root"):
            NodeContainerOrchestrator(test_workspace, runner=runner).run_node_container(
                run_id="run-bad",
                node_id="node-1",
                workspace_root=test_workspace,
            )
