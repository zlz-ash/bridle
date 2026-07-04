"""Tests for module container lifecycle cleanup semantics."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from bridle.agent.container.lifecycle import strict_cleanup_container
from bridle.agent.container.runner import (
    ContainerRemoveError,
    ContainerRequest,
    FakeContainerRunner,
    LocalContainerRuntimeRunner,
)


class StopFailRemoveOkRunner(FakeContainerRunner):
    def stop(self, container_id: str):
        raise OSError("simulated stop failure")

    def remove(self, container_id: str) -> None:
        super().remove(container_id)


class RemoveFailRunner(FakeContainerRunner):
    def remove(self, container_id: str) -> None:
        raise RuntimeError("simulated remove failure")


class StopWeirdFailRemoveOkRunner(FakeContainerRunner):
    def stop(self, container_id: str):
        raise TypeError("simulated non-runtime stop failure")


class RemoveCapabilityMissingRunner(FakeContainerRunner):
    """A runner that does not expose a usable remove capability.

    `remove` is set to a non-callable so that ``getattr(runner, "remove", None)``
    returns a value that fails the ``callable`` check in ``strict_cleanup_container``,
    exercising the fail-closed "capability missing" branch.
    """

    remove = None  # type: ignore[assignment]


class TestStrictCleanupContainer:
    def _create_container(self, runner: FakeContainerRunner) -> str:
        created = runner.create(ContainerRequest(name="c1", image="img", role="service"))
        return created.container_id

    def test_stop_and_remove_success_has_no_secondary(self, test_workspace) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        container_id = self._create_container(runner)
        outcome = strict_cleanup_container(runner, container_id)
        assert outcome.needs_secondary is False
        assert outcome.resource_may_remain is False
        assert not runner.exists(container_id)

    def test_stop_failure_remove_success_reports_secondary_without_leak(self, test_workspace) -> None:
        runner = StopFailRemoveOkRunner(workspace_root=test_workspace)
        container_id = self._create_container(runner)
        outcome = strict_cleanup_container(runner, container_id)
        assert outcome.needs_secondary is True
        assert outcome.stop_failed is True
        assert outcome.remove_failed is False
        assert outcome.resource_may_remain is False
        assert not runner.exists(container_id)

    def test_remove_failure_reports_leak(self, test_workspace) -> None:
        runner = RemoveFailRunner(workspace_root=test_workspace)
        container_id = self._create_container(runner)
        outcome = strict_cleanup_container(runner, container_id)
        assert outcome.needs_secondary is True
        assert outcome.remove_failed is True
        assert outcome.resource_may_remain is True
        assert runner.exists(container_id)

    def test_non_runtime_stop_exception_is_normalized(self, test_workspace) -> None:
        runner = StopWeirdFailRemoveOkRunner(workspace_root=test_workspace)
        container_id = self._create_container(runner)
        outcome = strict_cleanup_container(runner, container_id)
        assert outcome.stop_failed is True
        assert outcome.remove_failed is False
        assert outcome.resource_may_remain is False
        assert not runner.exists(container_id)

    def test_missing_remove_capability_reports_unknown_and_leak(self, test_workspace) -> None:
        runner = RemoveCapabilityMissingRunner(workspace_root=test_workspace)
        container_id = self._create_container(runner)
        outcome = strict_cleanup_container(runner, container_id)
        assert outcome.needs_secondary is True
        assert outcome.remove_executed is False
        assert outcome.remove_outcome == "unknown"
        assert outcome.remove_failed is True
        assert outcome.resource_may_remain is True
        assert outcome.container_id == container_id
        assert runner.exists(container_id)


class TestLocalContainerRuntimeRunnerRemove:
    def test_nonzero_docker_rm_preserves_registry(self, test_workspace) -> None:
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, use_docker=True)
        request = ContainerRequest(name="c1", image="img")
        container_id = "docker-remove-fail-id"
        runner._containers[container_id] = (
            request,
            MagicMock(container_id=container_id, name="c1"),
        )
        runner._logs[container_id] = ["created c1"]
        runner._run_command = MagicMock(  # type: ignore[method-assign]
            return_value=subprocess.CompletedProcess(
                ["docker", "rm", "-f", container_id],
                1,
                stdout="",
                stderr="permission denied",
            )
        )
        with pytest.raises(ContainerRemoveError) as exc_info:
            runner.remove(container_id)
        assert exc_info.value.exit_code == 1
        assert exc_info.value.stderr == "permission denied"
        assert exc_info.value.container_id == container_id
        assert container_id in runner._containers
        assert container_id in runner._logs

    def test_docker_rm_timeout_preserves_registry_and_diagnosis(self, test_workspace) -> None:
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, use_docker=True)
        request = ContainerRequest(name="c1", image="img")
        container_id = "docker-remove-timeout-id"
        runner._containers[container_id] = (
            request,
            MagicMock(container_id=container_id, name="c1"),
        )
        runner._logs[container_id] = ["created c1"]
        runner._run_command = MagicMock(  # type: ignore[method-assign]
            side_effect=subprocess.TimeoutExpired(
                cmd=["docker", "rm", "-f", container_id],
                timeout=5,
            )
        )
        with pytest.raises(ContainerRemoveError) as exc_info:
            runner.remove(container_id)
        assert exc_info.value.timed_out is True
        assert exc_info.value.exit_code is None
        assert exc_info.value.container_id == container_id
        assert container_id in runner._containers
        assert container_id in runner._logs
