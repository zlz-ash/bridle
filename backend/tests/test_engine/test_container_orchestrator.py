"""Tests for ContainerOrchestrator — unified container lifecycle orchestration."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.engine.container_orchestrator import (
    ContainerOrchestrator,
    OrchestratedContainerResult,
    _OrchestrationError,
)
from bridle.engine.container_runner import (
    ContainerMount,
    ContainerRequest,
    ContainerResult,
    FakeContainerRunner,
)


def _node_request(test_workspace: Path, **overrides) -> ContainerRequest:
    defaults = dict(
        name="test-node",
        image="bridle-node-agent:local",
        network_mode="none",
        mounts=[
            ContainerMount(
                source=test_workspace / ".aicoding" / "container-workspaces" / "run-1",
                target="/container",
                readonly=False,
            )
        ],
        environment={"BRIDLE_RUN_ID": "run-1", "BRIDLE_NODE_ID": "node-1"},
        command=["bridle-node-agent"],
        role="node",
        allowed_mount_roots=[
            str(test_workspace / ".aicoding" / "container-workspaces" / "run-1")
        ],
    )
    defaults.update(overrides)
    return ContainerRequest(**defaults)


def _main_request(test_workspace: Path) -> ContainerRequest:
    return ContainerRequest(
        name="main-agent-session-1",
        image="bridle-main-agent:local",
        network_mode="bridge",
        mounts=[
            ContainerMount(source=test_workspace, target="/workspace", readonly=False),
        ],
        environment={"BRIDLE_SESSION_ID": "session-1", "BRIDLE_PLAN_ID": "plan-1"},
        command=["bridle-main-agent"],
        role="main",
    )


class TestRunAndWait:
    def test_success_path(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request)

        assert isinstance(result, OrchestratedContainerResult)
        assert result.container_id.startswith("fake-container-")
        assert result.status == "stopped"
        assert result.health == "healthy"
        assert result.exit_code == 0
        assert result.logs
        assert result.network_mode == "none"

    def test_create_failure_raises_and_cleans_up(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def bad_create(_request):
            raise RuntimeError("docker create failed")

        runner.create = bad_create
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_start_failed"):
            orch.run_and_wait(request)

    def test_wait_timeout_cleans_up_and_writes_diag(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def timeout_wait(_cid, _timeout):
            raise TimeoutError("timed out")

        runner.wait = timeout_wait
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _node_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_wait_timeout") as exc_info:
            orch.run_and_wait(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "wait.error").exists()

    def test_wait_runtime_error_cleans_up_and_writes_diag(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def bad_wait(_cid, _timeout):
            raise RuntimeError("docker wait crashed")

        runner.wait = bad_wait
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _node_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_wait_failed") as exc_info:
            orch.run_and_wait(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "wait.error").exists()

    def test_nonzero_exit_cleans_up_and_writes_diag(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        from datetime import UTC, datetime

        def fail_wait(container_id, timeout_seconds):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="failed",
                network_mode=current.network_mode,
                health="unhealthy",
                exit_code=1,
                finished_at=datetime.now(UTC),
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.wait = fail_wait
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _node_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_exit_failed"):
            orch.run_and_wait(request, diag_dir=diag_dir)

        assert (diag_dir / "exit.error").exists()

    def test_unhealthy_inspect_cleans_up_and_writes_diag(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def unhealthy_inspect(container_id):
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

        runner.inspect = unhealthy_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _node_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_health_failed"):
            orch.run_and_wait(request, diag_dir=diag_dir)

        assert (diag_dir / "health.error").exists()

    def test_exit0_with_exited_status_succeeds(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def exited_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="failed",
                network_mode=current.network_mode,
                health="exited",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = exited_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request)

        assert result.exit_code == 0
        assert result.health == "healthy"

    def test_exit0_with_unhealthy_inspect_fails(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def unhealthy_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="exited",
                network_mode=current.network_mode,
                health="unhealthy",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = unhealthy_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _node_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_health_failed"):
            orch.run_and_wait(request, diag_dir=diag_dir)

        assert (diag_dir / "health.error").exists()

    def test_exit0_with_unknown_health_normalizes_to_healthy(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def unknown_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="exited",
                network_mode=current.network_mode,
                health="unknown",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = unknown_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request)

        assert result.exit_code == 0
        assert result.health == "healthy"

    def test_exit_code_none_with_exited_health_not_normalized(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        from datetime import UTC, datetime

        def none_exit_wait(container_id, timeout_seconds):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="stopped",
                network_mode=current.network_mode,
                health="healthy",
                exit_code=None,
                finished_at=datetime.now(UTC),
            )
            runner._containers[container_id] = (request, result)
            return result

        def exited_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="exited",
                network_mode=current.network_mode,
                health="exited",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.wait = none_exit_wait
        runner.inspect = exited_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request)

        assert result.exit_code is None
        assert result.health == "exited"

    def test_exit0_with_dead_health_not_normalized(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def dead_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="dead",
                network_mode=current.network_mode,
                health="dead",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = dead_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request)

        assert result.exit_code == 0
        assert result.health == "dead"

    def test_exit0_with_failed_health_not_normalized(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def failed_health_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="failed",
                network_mode=current.network_mode,
                health="failed",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = failed_health_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request)

        assert result.exit_code == 0
        assert result.health == "failed"

    def test_exit0_with_restarting_health_not_normalized(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def restarting_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="restarting",
                network_mode=current.network_mode,
                health="restarting",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = restarting_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request)

        assert result.exit_code == 0
        assert result.health == "restarting"

    def test_inspect_runtime_error_cleans_up_and_writes_diag(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def bad_inspect(_cid):
            raise RuntimeError("docker inspect crashed")

        runner.inspect = bad_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _node_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_inspect_failed") as exc_info:
            orch.run_and_wait(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "inspect.error").exists()

    def test_collect_logs_runtime_error_cleans_up_and_writes_diag(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def bad_logs(_cid):
            raise RuntimeError("docker logs crashed")

        runner.collect_logs = bad_logs
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _node_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_collect_logs_failed") as exc_info:
            orch.run_and_wait(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "logs.error").exists()

    def test_collects_logs_and_summary(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request)

        assert isinstance(result.logs, list)
        assert len(result.logs) > 0
        assert isinstance(result.logs_summary, str)

    def test_writes_container_log_diag(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _node_request(test_workspace)

        orch.run_and_wait(request, diag_dir=diag_dir)

        assert (diag_dir / "container.log").exists()

    def test_no_diag_dir_skips_diag_writes(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request, diag_dir=None)

        assert result.diagnostic_path is None


class TestStartDetached:
    def test_success_path(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _main_request(test_workspace)

        result = orch.start_detached(request)

        assert isinstance(result, OrchestratedContainerResult)
        assert result.container_id.startswith("fake-container-")
        assert result.status == "running"
        assert result.health == "healthy"
        assert result.exit_code is None
        assert result.network_mode == "bridge"

    def test_create_failure_raises_and_writes_diag(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def bad_create(_request):
            raise RuntimeError("docker create failed")

        runner.create = bad_create
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _main_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_start_failed"):
            orch.start_detached(request, diag_dir=diag_dir)

        assert (diag_dir / "startup.error").exists()

    def test_unhealthy_inspect_raises_and_cleans_up(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def unhealthy_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="running",
                network_mode=current.network_mode,
                health="unhealthy",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = unhealthy_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _main_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_health_failed") as exc_info:
            orch.start_detached(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "health.error").exists()

    def test_failed_status_raises_and_cleans_up(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def failed_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="failed",
                network_mode=current.network_mode,
                health="healthy",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = failed_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _main_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_health_failed") as exc_info:
            orch.start_detached(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "health.error").exists()

    def test_detached_exited_status_raises_and_cleans_up(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def exited_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="exited",
                network_mode=current.network_mode,
                health="exited",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = exited_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _main_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_health_failed") as exc_info:
            orch.start_detached(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "health.error").exists()

    def test_detached_exited_health_raises_and_cleans_up(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def exited_health_inspect(container_id):
            request, current = runner._load(container_id)
            result = ContainerResult(
                container_id=container_id,
                name=current.name,
                status="running",
                network_mode=current.network_mode,
                health="exited",
            )
            runner._containers[container_id] = (request, result)
            return result

        runner.inspect = exited_health_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _main_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_health_failed") as exc_info:
            orch.start_detached(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "health.error").exists()

    def test_inspect_runtime_error_raises_and_cleans_up(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def bad_inspect(_cid):
            raise RuntimeError("docker inspect crashed")

        runner.inspect = bad_inspect
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _main_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_inspect_failed") as exc_info:
            orch.start_detached(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "inspect.error").exists()

    def test_collect_logs_runtime_error_raises_and_cleans_up(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)

        def bad_logs(_cid):
            raise RuntimeError("docker logs crashed")

        runner.collect_logs = bad_logs
        orch = ContainerOrchestrator(runner, test_workspace)
        diag_dir = test_workspace / ".aicoding" / "diag"
        request = _main_request(test_workspace)

        with pytest.raises(_OrchestrationError, match="container_collect_logs_failed") as exc_info:
            orch.start_detached(request, diag_dir=diag_dir)

        assert exc_info.value.container_id is not None
        assert runner._containers[exc_info.value.container_id][1].status == "stopped"
        assert (diag_dir / "logs.error").exists()

    def test_collects_logs_and_summary(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _main_request(test_workspace)

        result = orch.start_detached(request)

        assert isinstance(result.logs, list)
        assert len(result.logs) > 0
        assert isinstance(result.logs_summary, str)


class TestCleanup:
    def test_cleanup_stops_container(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        request = _node_request(test_workspace)

        result = orch.run_and_wait(request)
        orch.cleanup(result.container_id)

        assert runner._containers[result.container_id][1].status == "stopped"
