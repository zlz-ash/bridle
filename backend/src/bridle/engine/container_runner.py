"""Container runner abstractions for agent execution."""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Literal

logger = logging.getLogger("bridle")

NetworkMode = Literal["bridge", "none"]
ContainerStatus = Literal["created", "running", "stopped", "failed"]
ContainerRole = Literal["main", "node"]


@dataclass(frozen=True)
class ContainerMount:
    source: Path
    target: str
    readonly: bool = True


@dataclass(frozen=True)
class ContainerRequest:
    name: str
    image: str
    network_mode: str = "bridge"
    mounts: list[ContainerMount] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    role: ContainerRole = "node"
    timeout_seconds: int = 300
    health_check: list[str] = field(default_factory=list)
    privileged: bool = False
    allowed_mount_roots: list[str] = field(default_factory=list)
    extra_hosts: list[str] | None = None


@dataclass(frozen=True)
class ContainerResult:
    container_id: str
    name: str
    status: ContainerStatus
    network_mode: NetworkMode
    health: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ContainerRunner(Protocol):
    def create(self, request: ContainerRequest) -> ContainerResult:
        ...

    def start(self, container_id: str) -> ContainerResult:
        ...

    def stop(self, container_id: str) -> ContainerResult:
        ...

    def wait(self, container_id: str, timeout_seconds: int) -> ContainerResult:
        ...

    def inspect(self, container_id: str) -> ContainerResult:
        ...

    def collect_logs(self, container_id: str) -> list[str]:
        ...


class FakeContainerRunner:
    """In-memory runner used by tests and container-orchestration dry runs."""

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        self._containers: dict[str, tuple[ContainerRequest, ContainerResult]] = {}
        self._logs: dict[str, list[str]] = {}

    def create(self, request: ContainerRequest) -> ContainerResult:
        network_mode = self._validate_request(request)
        container_id = f"fake-container-{len(self._containers) + 1}"
        result = ContainerResult(
            container_id=container_id,
            name=request.name,
            status="created",
            network_mode=network_mode,
            health="starting",
        )
        self._containers[container_id] = (request, result)
        self._logs[container_id] = [f"created {request.name}"]
        logger.info(
            "container_created",
            extra={
                "action": "container_created",
                "status": "completed",
                "detail": {
                    "container_id": container_id,
                    "name": request.name,
                    "network_mode": network_mode,
                },
            },
        )
        return result

    def start(self, container_id: str) -> ContainerResult:
        request, current = self._load(container_id)
        result = ContainerResult(
            container_id=container_id,
            name=current.name,
            status="running",
            network_mode=current.network_mode,
            health="healthy",
            started_at=datetime.now(timezone.utc),
        )
        self._containers[container_id] = (request, result)
        self._logs[container_id].append(f"started {container_id}")
        logger.info(
            "container_started",
            extra={
                "action": "container_started",
                "status": "completed",
                "detail": {"container_id": container_id, "name": current.name},
            },
        )
        return result

    def stop(self, container_id: str) -> ContainerResult:
        request, current = self._load(container_id)
        result = ContainerResult(
            container_id=container_id,
            name=current.name,
            status="stopped",
            network_mode=current.network_mode,
            health="stopped",
            finished_at=datetime.now(timezone.utc),
            exit_code=0,
        )
        self._containers[container_id] = (request, result)
        self._logs[container_id].append(f"stopped {container_id}")
        logger.info(
            "container_stopped",
            extra={
                "action": "container_stopped",
                "status": "completed",
                "detail": {"container_id": container_id, "name": current.name},
            },
        )
        return result

    def wait(self, container_id: str, timeout_seconds: int) -> ContainerResult:
        if timeout_seconds <= 0:
            raise TimeoutError("container_wait_timeout")
        request, current = self._load(container_id)
        result = ContainerResult(
            container_id=container_id,
            name=current.name,
            status="stopped",
            network_mode=current.network_mode,
            health="healthy",
            finished_at=datetime.now(timezone.utc),
            exit_code=0,
        )
        self._containers[container_id] = (request, result)
        self._logs[container_id].append(f"waited {container_id} exit_code=0")
        return result

    def inspect(self, container_id: str) -> ContainerResult:
        _, result = self._load(container_id)
        return result

    def remove(self, container_id: str) -> None:
        self._containers.pop(container_id, None)
        self._logs.pop(container_id, None)

    def collect_logs(self, container_id: str) -> list[str]:
        self._load(container_id)
        return list(self._logs[container_id])

    def _load(self, container_id: str) -> tuple[ContainerRequest, ContainerResult]:
        if container_id not in self._containers:
            raise KeyError(container_id)
        return self._containers[container_id]

    def _validate_network_mode(self, value: str) -> NetworkMode:
        if value not in {"bridge", "none"}:
            raise ValueError("Container network_mode must be bridge or none")
        return value  # type: ignore[return-value]

    def _validate_mounts(self, mounts: list[ContainerMount]) -> None:
        for mount in mounts:
            if not mount.target.startswith("/"):
                raise ValueError("Container mount target must be absolute")
            if str(mount.source).strip() == "":
                raise ValueError("Container mount source is required")

    _SENSITIVE_DIR_NAMES = frozenset({".git", ".ssh", ".docker"})
    _SENSITIVE_TARGETS = frozenset({"/proc", "/sys", "/dev", "/etc", "/var/run/docker.sock"})

    def _validate_request_safety(self, request: ContainerRequest) -> None:
        if request.privileged:
            raise ValueError("Container must not be privileged")
        if request.timeout_seconds <= 0 or request.timeout_seconds > 3600:
            raise ValueError("Container timeout_seconds must be between 1 and 3600")
        if request.role == "node" and not request.allowed_mount_roots:
            logger.info(
                "container_mount_rejected",
                extra={
                    "action": "container_mount_rejected",
                    "status": "rejected",
                    "detail": {
                        "role": "node",
                        "source": "",
                        "target": "",
                        "readonly": None,
                        "allowed_roots": [],
                        "reject_reason": "allowed_mount_roots_empty",
                    },
                },
            )
            raise ValueError("Node container allowed_mount_roots must not be empty")
        for mount in request.mounts:
            if request.role == "node":
                self._validate_node_target(mount)
            source_text = mount.source.as_posix().lower()
            target_text = mount.target.lower()
            if source_text.endswith("/var/run/docker.sock") or target_text == "/var/run/docker.sock":
                raise ValueError("Docker socket mount is not allowed")
            if request.role == "node":
                self._validate_node_mount(mount, request)
            logger.info(
                "container_mount_allowed",
                extra={
                    "action": "container_mount_allowed",
                    "status": "completed",
                    "detail": {
                        "role": request.role,
                        "source": str(mount.source),
                        "target": mount.target,
                        "readonly": mount.readonly,
                    },
                },
            )

    def _validate_node_mount(self, mount: ContainerMount, request: ContainerRequest) -> None:
        try:
            resolved_source = mount.source.resolve()
        except OSError:
            resolved_source = mount.source

        if self.workspace_root is not None:
            try:
                if resolved_source == self.workspace_root.resolve():
                    self._log_mount_rejected(mount, request, "workspace root")
                    raise ValueError("Node container must not mount workspace root")
            except OSError:
                pass

        if resolved_source.anchor == str(resolved_source):
            self._log_mount_rejected(mount, request, "host root")
            raise ValueError("Node container must not mount host root")

        home_dir = Path.home()
        try:
            if resolved_source == home_dir or any(
                resolved_source == (home_dir / d).resolve() for d in self._SENSITIVE_DIR_NAMES
            ):
                self._log_mount_rejected(mount, request, f"sensitive path: {mount.source}")
                raise ValueError(f"Node container must not mount sensitive path: {mount.source}")
        except OSError:
            pass

        try:
            if self.workspace_root is not None:
                git_dir = self.workspace_root.resolve() / ".git"
                if resolved_source == git_dir:
                    self._log_mount_rejected(mount, request, f"sensitive path: {mount.source}")
                    raise ValueError(f"Node container must not mount sensitive path: {mount.source}")
        except OSError:
            pass

        if request.allowed_mount_roots:
            allowed = any(
                resolved_source == Path(root).resolve() or self._is_subpath(resolved_source, Path(root).resolve())
                for root in request.allowed_mount_roots
            )
            if not allowed:
                self._log_mount_rejected(mount, request, f"not in allowed_mount_roots: {mount.source}")
                raise ValueError(
                    f"Node container mount source {mount.source} is not in allowed_mount_roots"
                )

    def _validate_node_target(self, mount: ContainerMount) -> None:
        target_lower = mount.target.lower()
        for sensitive in self._SENSITIVE_TARGETS:
            if target_lower == sensitive or target_lower.startswith(sensitive + "/"):
                self._log_mount_rejected(mount, None, f"sensitive target: {mount.target}")
                raise ValueError(f"Node container must not mount sensitive target: {mount.target}")

    def _log_mount_rejected(self, mount: ContainerMount, request: ContainerRequest | None, reason: str) -> None:
        logger.info(
            "container_mount_rejected",
            extra={
                "action": "container_mount_rejected",
                "status": "rejected",
                "detail": {
                    "role": request.role if request else "node",
                    "source": str(mount.source),
                    "target": mount.target,
                    "reason": reason,
                },
            },
        )

    @staticmethod
    def _is_subpath(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _validate_request(self, request: ContainerRequest) -> NetworkMode:
        network_mode = self._validate_network_mode(request.network_mode)
        self._validate_mounts(request.mounts)
        self._validate_request_safety(request)
        return network_mode


class LocalContainerRuntimeRunner(FakeContainerRunner):
    """Executes docker CLI for container lifecycle when use_docker=True."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        executable: str = "docker",
        use_docker: bool | None = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root)
        self.executable = executable
        self.use_docker = (
            use_docker if use_docker is not None else shutil.which(executable) is not None
        )
        self._docker_requests: dict[str, ContainerRequest] = {}

    def build_create_command(self, request: ContainerRequest) -> list[str]:
        self._validate_request(request)
        command = [
            self.executable,
            "create",
            "--name",
            request.name,
            "--network",
            request.network_mode,
        ]
        for key, value in sorted(request.environment.items()):
            command.extend(["--env", f"{key}={value}"])
        for mount in request.mounts:
            mode = "ro" if mount.readonly else "rw"
            command.extend(["--mount", f"type=bind,src={mount.source},dst={mount.target},{mode}"])
        for host_mapping in request.extra_hosts or []:
            command.extend(["--add-host", host_mapping])
        if request.health_check:
            command.extend(["--health-cmd", " ".join(request.health_check)])
        command.extend(["--stop-timeout", str(request.timeout_seconds)])
        command.append(request.image)
        command.extend(request.command)
        return command

    def _run_command(self, command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout)

    def create(self, request: ContainerRequest) -> ContainerResult:
        if not self.use_docker:
            return super().create(request)
        command = self.build_create_command(request)
        result = self._run_command(command)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "container_create_failed")
        container_id = result.stdout.strip()
        network_mode = self._validate_request(request)
        container_result = ContainerResult(
            container_id=container_id,
            name=request.name,
            status="created",
            network_mode=network_mode,
            health="starting",
        )
        self._docker_requests[container_id] = request
        self._containers[container_id] = (request, container_result)
        self._logs[container_id] = [f"created {request.name}"]
        logger.info(
            "container_created",
            extra={
                "action": "container_created",
                "status": "completed",
                "detail": {"container_id": container_id, "name": request.name, "runtime": "docker"},
            },
        )
        return container_result

    def start(self, container_id: str) -> ContainerResult:
        if not self.use_docker:
            return super().start(container_id)
        request, _current = self._load(container_id)
        result = self._run_command([self.executable, "start", container_id])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "container_start_failed")
        started = ContainerResult(
            container_id=container_id,
            name=request.name,
            status="running",
            network_mode=_current.network_mode,
            health="healthy",
            started_at=datetime.now(timezone.utc),
        )
        self._containers[container_id] = (request, started)
        self._logs[container_id].append(f"started {container_id}")
        return started

    def inspect(self, container_id: str) -> ContainerResult:
        if not self.use_docker:
            return super().inspect(container_id)
        status_result = self._run_command(
            [
                self.executable,
                "inspect",
                "-f",
                "{{.State.Status}}|{{.Name}}|{{.HostConfig.NetworkMode}}",
                container_id,
            ]
        )
        if status_result.returncode != 0:
            return ContainerResult(
                container_id=container_id,
                name="",
                status="failed",
                network_mode="bridge",
                health="missing",
            )
        state, name, net_mode = (status_result.stdout.strip() + "||").split("|", 2)[:3]
        network_mode: NetworkMode = "none" if net_mode == "none" else "bridge"
        health = "healthy" if state == "running" else state
        return ContainerResult(
            container_id=container_id,
            name=name.lstrip("/"),
            status="running" if health == "healthy" else "failed",
            network_mode=network_mode,
            health=health,
        )

    def remove(self, container_id: str) -> None:
        if not self.use_docker:
            self._containers.pop(container_id, None)
            self._logs.pop(container_id, None)
            return
        self._run_command([self.executable, "rm", "-f", container_id])
        self._containers.pop(container_id, None)
        self._logs.pop(container_id, None)

    def stop(self, container_id: str) -> ContainerResult:
        if not self.use_docker:
            return super().stop(container_id)
        self._load(container_id)
        result = self._run_command([self.executable, "stop", container_id])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "container_stop_failed")
        return super().stop(container_id)

    def wait(self, container_id: str, timeout_seconds: int) -> ContainerResult:
        if not self.use_docker:
            return super().wait(container_id, timeout_seconds)
        request, current = self._load(container_id)
        try:
            result = self._run_command([self.executable, "wait", container_id], timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("container_wait_timeout") from exc
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "container_wait_failed")
        stdout = result.stdout.strip()
        try:
            exit_code = int(stdout)
        except ValueError:
            exit_code = None
        waited = ContainerResult(
            container_id=container_id,
            name=current.name,
            status="stopped" if exit_code in (0, None) else "failed",
            network_mode=current.network_mode,
            health="healthy" if exit_code in (0, None) else "failed",
            exit_code=exit_code,
            finished_at=datetime.now(timezone.utc),
        )
        self._containers[container_id] = (request, waited)
        self._logs.setdefault(container_id, []).append(f"waited {container_id} exit_code={exit_code}")
        return waited

    def collect_logs(self, container_id: str) -> list[str]:
        if not self.use_docker:
            return super().collect_logs(container_id)
        self._load(container_id)
        result = self._run_command([self.executable, "logs", container_id])
        if result.returncode != 0:
            return list(self._logs.get(container_id, []))
        lines = [line for line in result.stdout.splitlines() if line]
        self._logs[container_id] = lines or list(self._logs.get(container_id, []))
        return list(self._logs[container_id])
