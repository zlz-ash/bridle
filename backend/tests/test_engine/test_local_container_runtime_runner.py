"""Tests for LocalContainerRuntimeRunner real docker subprocess path."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bridle.engine.container_runner import ContainerMount, ContainerRequest, LocalContainerRuntimeRunner


class TestLocalContainerRuntimeRunner:
    def test_create_start_inspect_logs_invoke_docker(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(test_workspace, use_docker=True)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            if cmd[1] == "create":
                result.stdout = "abc123container\n"
            elif cmd[1] == "inspect":
                result.stdout = "running\n"
            elif cmd[1] == "logs":
                result.stdout = "hello from container\n"
            elif cmd[1] == "wait":
                result.stdout = "0\n"
            else:
                result.stdout = ""
            result.stderr = ""
            return result

        runner._run_command = fake_run  # type: ignore[method-assign]
        request = ContainerRequest(
            name="bridle-test-node",
            image="alpine:3.20",
            network_mode="none",
            mounts=[
                ContainerMount(
                    source=test_workspace / ".aicoding" / "mount",
                    target="/data",
                    readonly=True,
                )
            ],
            command=["echo", "ok"],
            role="node",
            allowed_mount_roots=[str((test_workspace / ".aicoding" / "mount").resolve())],
        )
        (test_workspace / ".aicoding" / "mount").mkdir(parents=True, exist_ok=True)

        created = runner.create(request)
        started = runner.start(created.container_id)
        inspected = runner.inspect(started.container_id)
        logs = runner.collect_logs(started.container_id)
        waited = runner.wait(started.container_id, timeout_seconds=5)

        assert created.container_id == "abc123container"
        assert started.status == "running"
        assert inspected.status == "running"
        assert waited.status == "stopped"
        assert waited.exit_code == 0
        assert "hello from container" in logs[0]
        assert any(c[1] == "create" for c in calls)
        assert any(c[1] == "start" for c in calls)
        assert any(c[1] == "inspect" for c in calls)
        assert any(c[1] == "logs" for c in calls)
        assert any(c[1] == "wait" for c in calls)

    def test_create_failure_raises_runtime_error(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(test_workspace, use_docker=True)

        def fail_run(cmd: list[str], **_kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "create failed"
            return result

        runner._run_command = fail_run  # type: ignore[method-assign]
        request = ContainerRequest(
            name="bridle-fail",
            image="alpine:3.20",
            network_mode="none",
            mounts=[],
            role="main",
        )
        with pytest.raises(RuntimeError, match="create failed"):
            runner.create(request)

    def test_use_docker_false_delegates_to_fake_lifecycle(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(test_workspace, use_docker=False)
        request = ContainerRequest(name="fake-only", image="alpine:3.20", role="main")
        created = runner.create(request)
        assert created.container_id.startswith("fake-container-")
