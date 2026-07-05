"""Tests for agent container runner abstractions."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bridle.agent.container.container_identity import validate_container_identity
from bridle.agent.container.docker_inspect import request_from_inspect_data
from bridle.agent.container.runner import (
    ContainerMount,
    ContainerRequest,
    FakeContainerRunner,
    LocalContainerRuntimeRunner,
)


def _agent_request(workspace_dir: Path, **overrides) -> ContainerRequest:
    defaults = dict(
        name="agent-run-1",
        image="bridle-agent:local",
        network_mode="none",
        mounts=[ContainerMount(source=workspace_dir, target="/container", readonly=False)],
        environment={"BRIDLE_RUN_ID": "run-1"},
        command=["bridle-agent"],
        role="agent",
        allowed_mount_roots=[str(workspace_dir)],
        module_id="mod-a",
        boundary_fingerprint="fp-1",
        image_version="local",
    )
    defaults.update(overrides)
    return ContainerRequest(**defaults)


class TestFakeContainerRunner:
    def test_records_container_lifecycle(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".bridle" / "runtime" / "candidates" / "run-1"
        workspace_dir.mkdir(parents=True)
        request = _agent_request(workspace_dir)

        created = runner.create(request)
        started = runner.start(created.container_id)
        inspected = runner.inspect(created.container_id)

        assert created.container_id == "fake-container-1"
        assert started.status == "running"
        assert inspected.health == "healthy"
        assert inspected.network_mode == "none"
        assert runner.collect_logs(created.container_id) == ["created agent-run-1", "started fake-container-1"]

    def test_exec_records_command(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".bridle" / "runtime" / "candidates" / "run-1"
        workspace_dir.mkdir(parents=True)
        created = runner.create(_agent_request(workspace_dir))
        runner.start(created.container_id)
        result = runner.exec(created.container_id, ["python", "-m", "pytest"], timeout_seconds=30)
        assert result.exit_code == 0
        assert "exec:python -m pytest" in runner.collect_logs(created.container_id)[-1]

    def test_rejects_host_network(self) -> None:
        runner = FakeContainerRunner()
        with pytest.raises(ValueError, match="bridge or none"):
            runner.create(ContainerRequest(name="bad", image="img", network_mode="host"))

    def test_rejects_node_with_empty_allowlist(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".bridle" / "runtime" / "candidates" / "run-1"
        workspace_dir.mkdir(parents=True)
        with pytest.raises(ValueError, match="allowed_mount_roots must not be empty"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="img",
                    mounts=[ContainerMount(source=workspace_dir, target="/container", readonly=False)],
                    role="agent",
                    allowed_mount_roots=[],
                )
            )

    def test_rejects_workspace_root_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        with pytest.raises(ValueError, match="workspace root"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="img",
                    mounts=[ContainerMount(source=test_workspace, target="/workspace", readonly=True)],
                    role="agent",
                    allowed_mount_roots=[str(test_workspace)],
                )
            )

    def test_rejects_docker_socket_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".bridle" / "runtime" / "candidates" / "run-1"
        workspace_dir.mkdir(parents=True)
        with pytest.raises(ValueError, match="Docker socket|sensitive target"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="img",
                    mounts=[ContainerMount(source=Path("/var/run/docker.sock"), target="/var/run/docker.sock")],
                    role="agent",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_local_runtime_builds_hardened_docker_command(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, executable="docker")
        workspace_dir = test_workspace / ".bridle" / "runtime" / "candidates" / "run-1"
        workspace_dir.mkdir(parents=True)
        command = runner.build_create_command(_agent_request(workspace_dir, timeout_seconds=120))

        assert command[:4] == ["docker", "create", "--name", "agent-run-1"]
        assert "--network" in command
        assert "--network none" in " ".join(command)
        assert "--read-only" in command
        assert "--cap-drop" in command
        assert "--security-opt" in command
        assert "--pids-limit" in command
        assert "--memory" in command
        assert "--cpus" in command
        assert "--privileged" not in command
        assert "bridle-agent:local" in command

    def test_missing_docker_runtime_reports_unavailable(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(
            workspace_root=test_workspace,
            executable="bridle-missing-docker-for-test",
            use_docker=True,
        )
        workspace_dir = test_workspace / ".bridle" / "runtime" / "candidates" / "run-1"
        workspace_dir.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="container_runtime_unavailable"):
            runner.create(
                ContainerRequest(
                    name="agent-run-1",
                    image="bridle-agent:local",
                    mounts=[ContainerMount(source=workspace_dir, target="/container", readonly=False)],
                    role="agent",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_create_command_includes_labels(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, executable="docker")
        workspace_dir = test_workspace / ".bridle" / "runtime" / "candidates" / "run-1"
        workspace_dir.mkdir(parents=True)
        labels = {"bridle.module": "mod-a", "bridle.project": "abc123"}
        command = runner.build_create_command(
            _agent_request(workspace_dir, labels=labels)
        )
        assert "bridle.module=mod-a" in " ".join(command)

    def test_bind_mount_format_uses_readonly_flag_not_rw_suffix(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, executable="docker")
        rw_dir = test_workspace / "slot-rw"
        ro_dir = test_workspace / "slot-ro"
        rw_dir.mkdir(parents=True)
        ro_dir.mkdir(parents=True)
        request = _agent_request(
            rw_dir,
            mounts=[
                ContainerMount(source=rw_dir, target="/workspace/project", readonly=False),
                ContainerMount(source=ro_dir, target="/workspace/baseline", readonly=True),
            ],
            allowed_mount_roots=[str(rw_dir), str(ro_dir)],
        )
        command = runner.build_create_command(request)
        mounts = [part for idx, part in enumerate(command) if command[idx - 1] == "--mount"]
        rw_mount = next(m for m in mounts if "/workspace/project" in m)
        ro_mount = next(m for m in mounts if "/workspace/baseline" in m)
        assert rw_mount.endswith("/workspace/project")
        assert ",rw" not in rw_mount
        assert not rw_mount.endswith(",readonly")
        assert ro_mount.endswith(",readonly")

    def test_list_by_module_labels(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".bridle" / "runtime" / "modules" / "mod-a" / "_active"
        workspace_dir.mkdir(parents=True)
        labels = {"bridle.project": "proj-1", "bridle.module": "mod-a", "bridle.boundary_fp": "fp-1"}
        created = runner.create(_agent_request(workspace_dir, labels=labels, module_id="mod-a"))
        runner.start(created.container_id)
        found = runner.list_by_module_labels("proj-1", "mod-a")
        assert len(found) == 1
        assert found[0][0] == created.container_id

    def test_inspect_missing_container_returns_missing_health(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, executable="docker", use_docker=True)
        runner._run_command = MagicMock(return_value=MagicMock(returncode=1, stdout="", stderr="No such object"))  # type: ignore[method-assign]
        inspected = runner.inspect("gone-container-id")
        assert inspected.health == "missing"
        assert inspected.status == "failed"


class TestInspectIdentity:
    def test_request_from_inspect_rebuilds_mount_readonly(self, test_workspace: Path) -> None:
        slot = test_workspace / "slot" / "baseline"
        slot.mkdir(parents=True)
        payload = {
            "Name": "/bridle-test",
            "Image": "sha256:abc123",
            "Config": {
                "Labels": {
                    "bridle.schema": "v1",
                    "bridle.project": "proj123",
                    "bridle.module": "mod-a",
                    "bridle.boundary_fp": "fp-1",
                    "bridle.image_version": "local",
                    "bridle.mount_id": "abcd",
                },
                "Cmd": ["python", "-m", "bridle.agent.container.entrypoint", "--keep-alive"],
                "User": "1000",
            },
            "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True},
            "Mounts": [
                {"Source": str(slot), "Destination": "/workspace/baseline", "RW": False},
            ],
        }
        rebuilt = request_from_inspect_data(payload)
        assert rebuilt is not None
        assert rebuilt.image_id == "sha256:abc123"
        assert rebuilt.run_user == "1000"
        assert rebuilt.network_mode == "none"
        assert rebuilt.keep_alive is True
        assert rebuilt.read_only_root is True
        assert len(rebuilt.mounts) == 1
        assert rebuilt.mounts[0].readonly is True

    def test_validate_identity_rejects_boundary_mismatch(self, test_workspace: Path) -> None:
        workspace_dir = test_workspace / "ws"
        workspace_dir.mkdir(parents=True)
        mount = ContainerMount(source=workspace_dir, target="/workspace/project", readonly=False)
        base_labels = {
            "bridle.schema": "v1",
            "bridle.project": "proj",
            "bridle.module": "mod",
            "bridle.image_version": "local",
            "bridle.mount_id": "mount1",
        }
        expected = ContainerRequest(
            name="n1",
            image="img:1",
            network_mode="none",
            mounts=[mount],
            labels={**base_labels, "bridle.boundary_fp": "fp-a"},
            module_id="mod",
            boundary_fingerprint="fp-a",
            keep_alive=True,
        )
        actual = ContainerRequest(
            name="n1",
            image="img:1",
            network_mode="none",
            mounts=[mount],
            labels={**base_labels, "bridle.boundary_fp": "fp-b"},
            module_id="mod",
            boundary_fingerprint="fp-b",
            keep_alive=True,
        )
        errors = validate_container_identity(expected, actual)
        assert "label_mismatch:boundary_fp" in errors
