"""Optional real Docker container contract tests."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from bridle.engine.container_runner import LocalContainerRuntimeRunner
from bridle.engine.container_runner_factory import resolve_container_runner


@pytest.mark.skipif(
    os.getenv("BRIDLE_RUN_CONTAINER_TESTS", "").strip() != "1",
    reason="Set BRIDLE_RUN_CONTAINER_TESTS=1 to run real container tests",
)
class TestRealContainerIntegration:
    def test_skips_when_docker_unavailable(self, test_workspace: Path) -> None:
        if shutil.which("docker") is None:
            pytest.skip("docker executable not found")

    def test_local_runtime_runner_lifecycle_with_docker(self, test_workspace: Path) -> None:
        if shutil.which("docker") is None:
            pytest.skip("docker executable not found")
        from bridle.engine.container_runner import ContainerMount, ContainerRequest

        runner = LocalContainerRuntimeRunner(test_workspace, use_docker=True)
        mount_dir = test_workspace / ".aicoding" / "contract-mount"
        mount_dir.mkdir(parents=True, exist_ok=True)
        name = f"bridle-contract-{mount_dir.name[:8]}"
        request = ContainerRequest(
            name=name,
            image="alpine:3.20",
            network_mode="none",
            mounts=[ContainerMount(source=mount_dir, target="/data", readonly=True)],
            command=["echo", "ok"],
            role="node",
            allowed_mount_roots=[str(mount_dir.resolve())],
        )
        created = runner.create(request)
        started = runner.start(created.container_id)
        inspected = runner.inspect(started.container_id)
        logs = runner.collect_logs(created.container_id)
        runner.stop(created.container_id)
        import subprocess

        subprocess.run(
            ["docker", "rm", "-f", created.container_id],
            capture_output=True,
            text=True,
            check=False,
        )
        assert started.status == "running"
        assert inspected.health in {"healthy", "running"}
        assert logs

    def test_rejects_docker_socket_mount(self, test_workspace: Path) -> None:
        if shutil.which("docker") is None:
            pytest.skip("docker executable not found")
        from bridle.engine.container_runner import ContainerMount, ContainerRequest

        runner = LocalContainerRuntimeRunner(test_workspace, use_docker=True)
        sock = "/var/run/docker.sock"
        if not Path(sock).exists():
            pytest.skip("docker socket not present on host")
        request = ContainerRequest(
            name="bridle-socket-reject",
            image="alpine:3.20",
            network_mode="none",
            mounts=[ContainerMount(source=sock, target="/var/run/docker.sock", readonly=True)],
            command=["true"],
            role="node",
            allowed_mount_roots=[sock],
        )
        with pytest.raises(ValueError, match="sensitive path"):
            runner.create(request)


def test_factory_skips_real_tests_without_env(test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bridle.api.deps as deps

    monkeypatch.setattr(deps, "_test_db", None)
    monkeypatch.delenv("BRIDLE_RUN_CONTAINER_TESTS", raising=False)
    monkeypatch.setenv("BRIDLE_CONTAINER_RUNNER", "fake")
    assert resolve_container_runner(test_workspace).__class__.__name__ == "FakeContainerRunner"
