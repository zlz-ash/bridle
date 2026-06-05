"""Tests for container runner abstractions."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bridle.engine.container_runner import (
    ContainerMount,
    ContainerRequest,
    FakeContainerRunner,
    LocalContainerRuntimeRunner,
)


class TestFakeContainerRunner:
    def test_records_bridge_container_lifecycle(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner()
        request = ContainerRequest(
            name="main-agent-1",
            image="bridle-main-agent:local",
            network_mode="bridge",
            mounts=[
                ContainerMount(
                    source=test_workspace / ".aicoding" / "container-workspaces" / "run-1",
                    target="/container",
                    readonly=False,
                )
            ],
            environment={"BRIDLE_RUN_ID": "run-1"},
            command=["python", "-m", "bridle_agent"],
            role="main",
        )

        created = runner.create(request)
        started = runner.start(created.container_id)
        inspected = runner.inspect(created.container_id)

        assert created.container_id == "fake-container-1"
        assert started.status == "running"
        assert inspected.health == "healthy"
        assert inspected.network_mode == "bridge"
        assert runner.collect_logs(created.container_id) == ["created main-agent-1", "started fake-container-1"]

    def test_rejects_host_network(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner()

        with pytest.raises(ValueError, match="bridge or none"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="host",
                    mounts=[
                        ContainerMount(
                            source=test_workspace,
                            target="/container",
                            readonly=True,
                        )
                    ],
                    role="main",
                )
            )

    def test_rejects_docker_socket_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="Docker socket|sensitive target"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(
                            source=Path("/var/run/docker.sock"),
                            target="/var/run/docker.sock",
                            readonly=False,
                        )
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_rejects_node_workspace_root_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="workspace root"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(
                            source=test_workspace,
                            target="/workspace",
                            readonly=True,
                        )
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_local_runtime_builds_docker_command(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, executable="docker")
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        request = ContainerRequest(
            name="node-run-1",
            image="bridle-node-agent:local",
            network_mode="bridge",
            mounts=[
                ContainerMount(
                    source=workspace_dir,
                    target="/container",
                    readonly=False,
                )
            ],
            environment={"BRIDLE_RUN_ID": "run-1"},
            command=["bridle-node-agent"],
            role="node",
            timeout_seconds=120,
            allowed_mount_roots=[str(workspace_dir)],
        )

        command = runner.build_create_command(request)

        assert command[:4] == ["docker", "create", "--name", "node-run-1"]
        assert "--network" in command
        assert "bridge" in command
        assert "--privileged" not in command
        assert "bridle-node-agent:local" in command

    def test_collect_logs_requires_known_container(self) -> None:
        runner = FakeContainerRunner()

        with pytest.raises(KeyError, match="missing"):
            runner.collect_logs("missing")

    def test_inspect_without_in_memory_uses_docker(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(
            workspace_root=test_workspace,
            executable="docker",
            use_docker=True,
        )
        mock_result = MagicMock(returncode=0, stdout="running|/main-agent-s1|bridge\n")
        runner._run_command = MagicMock(return_value=mock_result)  # type: ignore[method-assign]

        inspected = runner.inspect("orphan-container-id")

        assert inspected.status == "running"
        assert inspected.health == "healthy"
        assert inspected.name == "main-agent-s1"
        runner._run_command.assert_called_once()
        assert "inspect" in runner._run_command.call_args[0][0]

    def test_inspect_missing_container_returns_missing_health(self, test_workspace: Path) -> None:
        runner = LocalContainerRuntimeRunner(
            workspace_root=test_workspace,
            executable="docker",
            use_docker=True,
        )
        mock_result = MagicMock(returncode=1, stdout="", stderr="No such object")
        runner._run_command = MagicMock(return_value=mock_result)  # type: ignore[method-assign]

        inspected = runner.inspect("gone-container-id")

        assert inspected.health == "missing"
        assert inspected.status == "failed"


class TestNodeMountAllowlist:
    def test_allows_container_workspace_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        request = ContainerRequest(
            name="node-run-1",
            image="bridle-node-agent:local",
            network_mode="bridge",
            mounts=[
                ContainerMount(source=workspace_dir, target="/container", readonly=False),
            ],
            role="node",
            allowed_mount_roots=[str(workspace_dir)],
        )

        created = runner.create(request)
        assert created.status == "created"

    def test_allows_declared_readonly_context(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        context_dir = test_workspace / "src"
        context_dir.mkdir(parents=True, exist_ok=True)
        request = ContainerRequest(
            name="node-run-1",
            image="bridle-node-agent:local",
            network_mode="bridge",
            mounts=[
                ContainerMount(source=workspace_dir, target="/container", readonly=False),
                ContainerMount(source=context_dir, target="/context", readonly=True),
            ],
            role="node",
            allowed_mount_roots=[str(workspace_dir), str(context_dir)],
        )

        created = runner.create(request)
        assert created.status == "created"

    def test_rejects_git_directory_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(parents=True, exist_ok=True)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="sensitive path"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/container", readonly=False),
                        ContainerMount(source=git_dir, target="/git", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_rejects_home_directory_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        home_dir = Path.home()

        with pytest.raises(ValueError, match="sensitive path"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/container", readonly=False),
                        ContainerMount(source=home_dir, target="/home", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_rejects_ssh_directory_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        ssh_dir = Path.home() / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="sensitive path"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/container", readonly=False),
                        ContainerMount(source=ssh_dir, target="/ssh", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_rejects_docker_config_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        docker_dir = Path.home() / ".docker"
        docker_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="sensitive path"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/container", readonly=False),
                        ContainerMount(source=docker_dir, target="/docker", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_rejects_workspace_root_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="workspace root"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=test_workspace, target="/workspace", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_rejects_undeclared_mount_path(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        undeclared_dir = test_workspace / "random"
        undeclared_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="not in allowed_mount_roots"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/container", readonly=False),
                        ContainerMount(source=undeclared_dir, target="/random", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_rejects_host_root_mount(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="host root"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=Path("/"), target="/host", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_main_role_bypasses_allowlist(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        request = ContainerRequest(
            name="main-agent-1",
            image="bridle-main-agent:local",
            network_mode="bridge",
            mounts=[
                ContainerMount(source=test_workspace, target="/workspace", readonly=False),
            ],
            role="main",
        )

        created = runner.create(request)
        assert created.status == "created"

    def test_rejects_node_with_empty_allowlist(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="allowed_mount_roots must not be empty"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/container", readonly=False),
                    ],
                    role="node",
                    allowed_mount_roots=[],
                )
            )

    def test_empty_allowlist_logs_container_mount_rejected(self, test_workspace: Path, caplog) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with caplog.at_level("INFO"), pytest.raises(ValueError, match="allowed_mount_roots must not be empty"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/container", readonly=False),
                    ],
                    role="node",
                    allowed_mount_roots=[],
                )
            )

        rejected_logs = [r for r in caplog.records if "container_mount_rejected" in r.message]
        assert len(rejected_logs) >= 1
        detail = rejected_logs[0].__dict__
        assert detail.get("reject_reason") == "allowed_mount_roots_empty" or any(
            "allowed_mount_roots_empty" in str(v) for v in detail.values()
        )

    def test_rejects_node_mount_target_proc(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="sensitive target"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/proc", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_rejects_node_mount_target_sys(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="sensitive target"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/sys", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_rejects_node_mount_target_docker_sock(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="sensitive target"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/var/run/docker.sock", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

    def test_valid_mount_logs_allowed(self, test_workspace: Path, caplog) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        request = ContainerRequest(
            name="node-run-1",
            image="bridle-node-agent:local",
            network_mode="bridge",
            mounts=[
                ContainerMount(source=workspace_dir, target="/container", readonly=False),
            ],
            role="node",
            allowed_mount_roots=[str(workspace_dir)],
        )

        with caplog.at_level("INFO"):
            runner.create(request)

        assert any("container_mount_allowed" in r.message for r in caplog.records)

    def test_rejected_mount_logs_rejected(self, test_workspace: Path, caplog) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)

        with caplog.at_level("INFO"), pytest.raises(ValueError, match="sensitive target"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/proc", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

        assert any("container_mount_rejected" in r.message for r in caplog.records)

    def test_rejected_source_mount_logs_rejected(self, test_workspace: Path, caplog) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        workspace_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        undeclared_dir = test_workspace / "random"
        undeclared_dir.mkdir(parents=True, exist_ok=True)

        with caplog.at_level("INFO"), pytest.raises(ValueError, match="not in allowed_mount_roots"):
            runner.create(
                ContainerRequest(
                    name="bad",
                    image="bridle-node-agent:local",
                    network_mode="bridge",
                    mounts=[
                        ContainerMount(source=workspace_dir, target="/container", readonly=False),
                        ContainerMount(source=undeclared_dir, target="/random", readonly=True),
                    ],
                    role="node",
                    allowed_mount_roots=[str(workspace_dir)],
                )
            )

        assert any("container_mount_rejected" in r.message for r in caplog.records)
