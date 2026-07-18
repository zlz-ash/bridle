"""Container runner abstractions for isolated agent execution."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

logger = logging.getLogger("bridle")


class ContainerRemoveError(RuntimeError):
    """Raised when docker rm -f returns a non-zero exit code or times out."""

    def __init__(
        self,
        message: str,
        *,
        container_id: str,
        exit_code: int | None,
        stderr: str = "",
        stdout: str = "",
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.container_id = container_id
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout
        self.timed_out = timed_out


NetworkMode = Literal["bridge", "none"]
ContainerStatus = Literal["created", "running", "stopped", "failed"]
ContainerRole = Literal["agent", "service"]


@dataclass(frozen=True)
class ContainerMount:
    source: Path
    target: str
    readonly: bool = True


@dataclass(frozen=True)
class ContainerRequest:
    name: str
    image: str
    network_mode: str = "none"
    mounts: list[ContainerMount] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    role: ContainerRole = "agent"
    timeout_seconds: int = 300
    health_check: list[str] = field(default_factory=list)
    privileged: bool = False
    cap_drop: tuple[str, ...] = ("ALL",)
    security_opt: tuple[str, ...] = ("no-new-privileges",)
    allowed_mount_roots: list[str] = field(default_factory=list)
    extra_hosts: list[str] | None = None
    module_id: str = ""
    boundary_fingerprint: str = ""
    image_version: str = "local"
    image_id: str = ""
    run_user: str = "1000"
    read_only_root: bool = True
    memory: str = "512m"
    cpus: str = "1.0"
    pids_limit: int = 256
    module_mount_root: str = ""
    keep_alive: bool = False
    labels: dict[str, str] = field(default_factory=dict)


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

    def remove(self, container_id: str) -> None:
        ...

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        ...


class FakeContainerRunner:
    """In-memory runner used by tests and dry runs."""

    _SENSITIVE_DIR_NAMES = frozenset({".git", ".ssh", ".docker"})
    _SENSITIVE_TARGETS = frozenset({"/proc", "/sys", "/dev", "/etc", "/var/run/docker.sock"})

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        self._containers: dict[str, tuple[ContainerRequest, ContainerResult]] = {}
        self._logs: dict[str, list[str]] = {}
        self._next_id = 1

    def create(self, request: ContainerRequest) -> ContainerResult:
        network_mode = self._validate_request(request)
        container_id = f"fake-container-{self._next_id}"
        self._next_id += 1
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
            started_at=datetime.now(UTC),
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
            finished_at=datetime.now(UTC),
            exit_code=0,
        )
        self._containers[container_id] = (request, result)
        self._logs[container_id].append(f"stopped {container_id}")
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
            finished_at=datetime.now(UTC),
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

    def find_by_name(self, name: str) -> ContainerResult | None:
        for _, (_, result) in self._containers.items():
            if result.name == name and result.status == "running":
                return result
        return None

    def get_stored_request(self, container_id: str) -> ContainerRequest | None:
        if container_id not in self._containers:
            return None
        request, _ = self._containers[container_id]
        return request

    def rebuild_request_from_inspect(self, container_id: str) -> ContainerRequest | None:
        return self.get_stored_request(container_id)

    def list_by_module_labels(
        self, project_label: str, module_id: str
    ) -> list[tuple[str, ContainerRequest, ContainerResult]]:
        matches: list[tuple[str, ContainerRequest, ContainerResult]] = []
        for container_id, (request, result) in self._containers.items():
            if request.labels.get("bridle.project") != project_label:
                continue
            if request.labels.get("bridle.module") != module_id:
                continue
            matches.append((container_id, request, result))
        return matches

    def exists(self, container_id: str) -> bool:
        return container_id in self._containers

    def collect_logs(self, container_id: str) -> list[str]:
        self._load(container_id)
        return list(self._logs[container_id])

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        if timeout_seconds <= 0:
            raise TimeoutError("container_wait_timeout")
        request, current = self._load(container_id)
        cmd_text = " ".join(command)
        env = environment or {}
        exit_code = 0
        stdout = f"exec:{cmd_text}"
        stderr = ""
        if "--run-task" in command and request.module_mount_root:
            import subprocess
            import sys

            child_env = dict(os.environ)
            for key, value in env.items():
                child_env[key] = str(value)
            child_env["BRIDLE_SLOT_ROOT"] = str(request.module_mount_root)
            child_env["BRIDLE_TASK_TIMEOUT"] = str(timeout_seconds)
            child_env["BRIDLE_FAKE_CONTAINER_RUNNER"] = "1"
            child_env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                [sys.executable, "-m", "bridle.agent.container.entrypoint", "--run-task"],
                env=child_env,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = int(proc.returncode)
            stdout = proc.stdout.decode("utf-8", errors="replace")
            stderr = proc.stderr.decode("utf-8", errors="replace")
        self._logs[container_id].append(stdout or f"exec:{cmd_text} exit={exit_code}")
        result = ContainerResult(
            container_id=container_id,
            name=current.name,
            status="running" if current.status == "running" else "stopped",
            network_mode=current.network_mode,
            health="healthy" if exit_code == 0 else "failed",
            finished_at=datetime.now(UTC),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )
        self._containers[container_id] = (request, result)
        return result

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

    def _validate_request_safety(self, request: ContainerRequest) -> None:
        if request.privileged:
            raise ValueError("Container must not be privileged")
        if request.timeout_seconds <= 0 or request.timeout_seconds > 3600:
            raise ValueError("Container timeout_seconds must be between 1 and 3600")
        if request.role == "agent" and not request.allowed_mount_roots:
            logger.info(
                "container_mount_rejected",
                extra={
                    "action": "container_mount_rejected",
                    "status": "rejected",
                    "detail": {"role": request.role, "reject_reason": "allowed_mount_roots_empty"},
                },
            )
            raise ValueError("Agent container allowed_mount_roots must not be empty")
        for mount in request.mounts:
            if request.role == "agent":
                self._validate_agent_target(mount)
            source_text = mount.source.as_posix().lower()
            target_text = mount.target.lower()
            if source_text.endswith("/var/run/docker.sock") or target_text == "/var/run/docker.sock":
                raise ValueError("Docker socket mount is not allowed")
            if request.role == "agent":
                self._validate_agent_mount(mount, request)
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

    def _validate_agent_mount(self, mount: ContainerMount, request: ContainerRequest) -> None:
        try:
            resolved_source = mount.source.resolve()
        except OSError:
            resolved_source = mount.source

        if self.workspace_root is not None:
            try:
                if resolved_source == self.workspace_root.resolve():
                    self._log_mount_rejected(mount, request, "workspace root")
                    raise ValueError("Agent container must not mount workspace root")
            except OSError:
                pass

        if resolved_source.anchor == str(resolved_source):
            self._log_mount_rejected(mount, request, "host root")
            raise ValueError("Agent container must not mount host root")

        home_dir = Path.home()
        try:
            if resolved_source == home_dir or any(
                resolved_source == (home_dir / d).resolve() for d in self._SENSITIVE_DIR_NAMES
            ):
                self._log_mount_rejected(mount, request, f"sensitive path: {mount.source}")
                raise ValueError(f"Agent container must not mount sensitive path: {mount.source}")
        except OSError:
            pass

        try:
            if self.workspace_root is not None:
                git_dir = self.workspace_root.resolve() / ".git"
                if resolved_source == git_dir:
                    self._log_mount_rejected(mount, request, f"sensitive path: {mount.source}")
                    raise ValueError(f"Agent container must not mount sensitive path: {mount.source}")
        except OSError:
            pass

        allowed = any(
            resolved_source == Path(root).resolve() or self._is_subpath(resolved_source, Path(root).resolve())
            for root in request.allowed_mount_roots
        )
        if not allowed:
            self._log_mount_rejected(mount, request, f"not in allowed_mount_roots: {mount.source}")
            raise ValueError(f"Agent container mount source {mount.source} is not in allowed_mount_roots")

    def _validate_agent_target(self, mount: ContainerMount) -> None:
        target_lower = mount.target.lower()
        for sensitive in self._SENSITIVE_TARGETS:
            if target_lower == sensitive or target_lower.startswith(sensitive + "/"):
                self._log_mount_rejected(mount, None, f"sensitive target: {mount.target}")
                raise ValueError(f"Agent container must not mount sensitive target: {mount.target}")

    def _log_mount_rejected(self, mount: ContainerMount, request: ContainerRequest | None, reason: str) -> None:
        logger.info(
            "container_mount_rejected",
            extra={
                "action": "container_mount_rejected",
                "status": "rejected",
                "detail": {
                    "role": request.role if request else "agent",
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


def _format_bind_mount(mount: ContainerMount) -> str:
    source = mount.source.resolve()
    if mount.readonly:
        return f"type=bind,src={source},dst={mount.target},readonly"
    return f"type=bind,src={source},dst={mount.target}"


class LocalContainerRuntimeRunner(FakeContainerRunner):
    """Executes Docker CLI for container lifecycle when use_docker=True."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        executable: str = "docker",
        use_docker: bool | None = None,
    ) -> None:
        super().__init__(workspace_root=workspace_root)
        self.executable = executable
        self.use_docker = use_docker if use_docker is not None else shutil.which(executable) is not None

    def build_create_command(self, request: ContainerRequest) -> list[str]:
        self._validate_request(request)
        command = [
            self.executable,
            "create",
            "--name",
            request.name,
            "--network",
            request.network_mode,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(request.pids_limit),
            "--memory",
            request.memory,
            "--cpus",
            request.cpus,
        ]
        if request.read_only_root:
            command.append("--read-only")
            command.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"])
        if request.run_user:
            command.extend(["--user", request.run_user])
        for key, value in sorted(request.environment.items()):
            command.extend(["--env", f"{key}={value}"])
        for label_key, label_value in sorted(request.labels.items()):
            command.extend(["--label", f"{label_key}={label_value}"])
        for mount in request.mounts:
            command.extend(["--mount", _format_bind_mount(mount)])
        for host_mapping in request.extra_hosts or []:
            command.extend(["--add-host", host_mapping])
        if request.health_check:
            command.extend(["--health-cmd", " ".join(request.health_check)])
        command.extend(["--stop-timeout", str(request.timeout_seconds)])
        command.append(request.image)
        command.extend(request.command)
        return command

    def _normalize_container_id(self, container_id: str) -> str:
        try:
            result = self._run_command(
                [self.executable, "inspect", "-f", "{{.Id}}", container_id],
                timeout=10,
            )
        except RuntimeError:
            return container_id
        if result.returncode != 0 or not result.stdout.strip():
            return container_id
        return result.stdout.strip()

    def find_by_name(self, name: str) -> ContainerResult | None:
        if self.use_docker:
            result = self._run_command(
                [self.executable, "ps", "-aq", "--filter", f"name=^{name}$", "--filter", "status=running"]
            )
            if result.returncode != 0 or not result.stdout.strip():
                return super().find_by_name(name)
            container_id = self._normalize_container_id(result.stdout.strip().splitlines()[0].strip())
            inspected = self.inspect(container_id)
            if inspected.health == "missing":
                return None
            if container_id not in self._containers:
                placeholder = ContainerRequest(name=name, image="", labels={})
                self._containers[container_id] = (
                    placeholder,
                    ContainerResult(
                        container_id=container_id,
                        name=name,
                        status=inspected.status,
                        network_mode=inspected.network_mode,
                        health=inspected.health,
                    ),
                )
            return inspected
        return super().find_by_name(name)

    def rebuild_request_from_inspect(self, container_id: str) -> ContainerRequest | None:
        if not self.use_docker:
            return super().get_stored_request(container_id)
        from bridle.agent.container.docker_inspect import inspect_container_request

        return inspect_container_request(executable=self.executable, container_id=container_id)

    def get_stored_request(self, container_id: str) -> ContainerRequest | None:
        stored = super().get_stored_request(container_id)
        if stored is not None:
            return stored
        if self.use_docker:
            return self.rebuild_request_from_inspect(container_id)
        return None

    def list_by_module_labels(
        self, project_label: str, module_id: str
    ) -> list[tuple[str, ContainerRequest, ContainerResult]]:
        if not self.use_docker:
            return super().list_by_module_labels(project_label, module_id)
        filter_module = f"label=bridle.module={module_id}"
        filter_project = f"label=bridle.project={project_label}"
        result = self._run_command(
            [self.executable, "ps", "-aq", "--filter", filter_module, "--filter", filter_project]
        )
        if result.returncode != 0:
            return []
        matches: list[tuple[str, ContainerRequest, ContainerResult]] = []
        for container_id in [line.strip() for line in result.stdout.splitlines() if line.strip()]:
            container_id = self._normalize_container_id(container_id)
            stored = self.rebuild_request_from_inspect(container_id)
            inspected = self.inspect(container_id)
            if stored is None:
                matches.append((container_id, None, inspected))  # type: ignore[arg-type]
                continue
            matches.append((container_id, stored, inspected))
        return matches

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        if not self.use_docker:
            return super().exec(
                container_id,
                command,
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
        self._load(container_id)
        exec_cmd = [self.executable, "exec"]
        for key, value in sorted((environment or {}).items()):
            exec_cmd.extend(["-e", f"{key}={value}"])
        exec_cmd.extend([container_id, *command])
        try:
            result = self._run_command(exec_cmd, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("container_wait_timeout") from exc
        request, current = self._load(container_id)
        exit_code = result.returncode
        return ContainerResult(
            container_id=container_id,
            name=current.name,
            status="running" if current.status == "running" else "stopped",
            network_mode=current.network_mode,
            health="healthy" if exit_code == 0 else "failed",
            exit_code=exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            finished_at=datetime.now(UTC),
        )

    def _run_command(self, command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except OSError as exc:
            raise RuntimeError(f"container_runtime_unavailable: {self.executable}") from exc

    def create(self, request: ContainerRequest) -> ContainerResult:
        if not self.use_docker:
            return super().create(request)
        command = self.build_create_command(request)
        result = self._run_command(command)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "container_create_failed")
        container_id = self._normalize_container_id(result.stdout.strip())
        network_mode = self._validate_request(request)
        container_result = ContainerResult(
            container_id=container_id,
            name=request.name,
            status="created",
            network_mode=network_mode,
            health="starting",
        )
        self._containers[container_id] = (request, container_result)
        self._logs[container_id] = [f"created {request.name}"]
        return container_result

    def start(self, container_id: str) -> ContainerResult:
        if not self.use_docker:
            return super().start(container_id)
        request, current = self._load(container_id)
        result = self._run_command([self.executable, "start", container_id])
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "container_start_failed")
        started = ContainerResult(
            container_id=container_id,
            name=request.name,
            status="running",
            network_mode=current.network_mode,
            health="healthy",
            started_at=datetime.now(UTC),
        )
        self._containers[container_id] = (request, started)
        self._logs[container_id].append(f"started {container_id}")
        return started

    def inspect(self, container_id: str) -> ContainerResult:
        if not self.use_docker:
            return super().inspect(container_id)
        container_id = self._normalize_container_id(container_id)
        status_result = self._run_command(
            [self.executable, "inspect", "-f", "{{.State.Status}}|{{.Name}}|{{.HostConfig.NetworkMode}}", container_id]
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

    def stop(self, container_id: str) -> ContainerResult:
        if not self.use_docker:
            return super().stop(container_id)
        self._load(container_id)
        try:
            result = self._run_command(
                [self.executable, "stop", "-t", "1", container_id],
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "container_stop_failed")
        except subprocess.TimeoutExpired:
            logger.info(
                "container_stop_timeout",
                extra={
                    "action": "container_stop_timeout",
                    "status": "timeout",
                    "detail": {"container_id": container_id},
                },
            )
        return super().stop(container_id)

    def remove(self, container_id: str) -> None:
        if self.use_docker:
            try:
                result = self._run_command([self.executable, "rm", "-f", container_id])
            except subprocess.TimeoutExpired as exc:
                raise ContainerRemoveError(
                    "container_remove_timeout",
                    container_id=container_id,
                    exit_code=None,
                    stdout=str(exc.stdout or ""),
                    stderr=str(exc.stderr or ""),
                    timed_out=True,
                ) from exc
            if result.returncode != 0:
                raise ContainerRemoveError(
                    result.stderr.strip() or "container_remove_failed",
                    container_id=container_id,
                    exit_code=int(result.returncode),
                    stderr=result.stderr,
                    stdout=result.stdout,
                )
        self._containers.pop(container_id, None)
        self._logs.pop(container_id, None)

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
        try:
            exit_code = int(result.stdout.strip())
        except ValueError:
            exit_code = None
        waited = ContainerResult(
            container_id=container_id,
            name=current.name,
            status="stopped" if exit_code in (0, None) else "failed",
            network_mode=current.network_mode,
            health="healthy" if exit_code in (0, None) else "failed",
            exit_code=exit_code,
            finished_at=datetime.now(UTC),
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
