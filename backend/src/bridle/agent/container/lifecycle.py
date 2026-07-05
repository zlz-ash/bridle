"""Module-scoped container lifecycle and reuse."""
from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from bridle.agent.container.container_control import (
    EXECUTION_EXITED,
    EXECUTION_FAILED_BEFORE_EXEC,
    EXECUTION_PHASE_CLEANUP,
    EXECUTION_PHASE_COLLECT,
    EXECUTION_PHASE_CREATE,
    EXECUTION_PHASE_EXEC,
    EXECUTION_PHASE_START,
    EXECUTION_STARTED_UNKNOWN,
    EXECUTION_TIMED_OUT,
    SECONDARY_COLLECT_ERROR_CODE,
    SECONDARY_START_CLEANUP_ERROR_CODE,
)
from bridle.agent.container.container_identity import validate_container_identity
from bridle.agent.container.runner import (
    ContainerRemoveError,
    ContainerRequest,
    ContainerResult,
    ContainerRunner,
    LocalContainerRuntimeRunner,
)

logger = logging.getLogger("bridle")

_ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_MAX_LOG_BYTES = 256_000


class ModuleContainerState(StrEnum):
    IDLE = "idle"
    QUEUED = "queued"
    RUNNING = "running"
    TAINTED = "tainted"
    REPLACING = "replacing"


@dataclass
class ModuleContainerRecord:
    container_id: str
    name: str
    module_id: str
    boundary_fingerprint: str
    image_version: str
    state: ModuleContainerState = ModuleContainerState.IDLE
    taint_reason: str | None = None
    last_run_id: str | None = None


class AcquireAction(StrEnum):
    CREATED = "created"
    REGISTRY_REUSED = "registry_reused"
    DAEMON_ADOPTED = "daemon_adopted"


class ModuleTransactionFailure(Exception):
    def __init__(
        self,
        message: str,
        *,
        execution_phase: str,
        execution_state: str,
        side_effect_possible: bool,
        container_id: str | None = None,
        exit_code: int | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.execution_phase = execution_phase
        self.execution_state = execution_state
        self.side_effect_possible = side_effect_possible
        self.container_id = container_id
        self.exit_code = exit_code
        self.run_id = run_id
        self.secondary_execution_phase: str | None = None
        self.secondary_error_code: str | None = None
        self.secondary_detail: str | None = None
        self.start_cleanup_failure: str | None = None
        self.resource_may_remain: bool = False
        self.secondary_diagnostics: dict[str, object] | None = None

    def attach_collect_secondary(self, exc: Exception) -> None:
        self.secondary_execution_phase = EXECUTION_PHASE_COLLECT
        self.secondary_error_code = SECONDARY_COLLECT_ERROR_CODE
        self.secondary_detail = str(exc)

    def attach_cleanup_secondary(self, outcome: CleanupOutcome) -> None:
        if not outcome.needs_secondary:
            return
        self.secondary_execution_phase = EXECUTION_PHASE_CLEANUP
        self.secondary_error_code = SECONDARY_START_CLEANUP_ERROR_CODE
        summary = outcome.summary()
        self.secondary_detail = summary
        self.start_cleanup_failure = summary
        self.resource_may_remain = outcome.resource_may_remain
        self.secondary_diagnostics = outcome.to_diagnostics()


@dataclass
class CleanupOutcome:
    container_id: str = ""
    stop_failed: bool = False
    stop_detail: str | None = None
    stop_exit_code: int | None = None
    stop_stdout: str | None = None
    stop_stderr: str | None = None
    stop_timed_out: bool | None = None
    remove_executed: bool = False
    remove_outcome: str = "success"  # "success" | "failed" | "unknown"
    remove_detail: str | None = None
    remove_exit_code: int | None = None
    remove_stdout: str | None = None
    remove_stderr: str | None = None
    remove_timed_out: bool | None = None

    @property
    def remove_failed(self) -> bool:
        return self.remove_outcome != "success"

    @property
    def needs_secondary(self) -> bool:
        return self.stop_failed or self.remove_failed

    @property
    def resource_may_remain(self) -> bool:
        return self.remove_failed

    def summary(self) -> str:
        parts: list[str] = []
        if self.stop_failed and self.stop_detail:
            parts.append(f"stop: {self.stop_detail}")
        if self.remove_failed:
            if self.remove_outcome == "unknown":
                parts.append("remove: capability missing")
            elif self.remove_detail:
                parts.append(f"remove: {self.remove_detail}")
            else:
                parts.append("remove: failed")
        return "; ".join(parts) if parts else "cleanup failed"

    def to_diagnostics(self) -> dict[str, object]:
        diagnostics: dict[str, object] = {
            "container_id": self.container_id,
            "stop_failed": self.stop_failed,
            "remove_executed": self.remove_executed,
            "remove_outcome": self.remove_outcome,
            "resource_may_remain": self.resource_may_remain,
        }
        if self.stop_detail is not None:
            diagnostics["stop_detail"] = self.stop_detail
        if self.stop_failed or self.stop_exit_code is not None:
            # Stop was attempted: keep structured subprocess fields with explicit
            # null so downstream consumers do not parse ``stop_detail`` strings.
            diagnostics["stop_exit_code"] = self.stop_exit_code
            diagnostics["stop_stdout"] = self.stop_stdout
            diagnostics["stop_stderr"] = self.stop_stderr
            diagnostics["stop_timed_out"] = self.stop_timed_out
        if self.remove_detail is not None:
            diagnostics["remove_detail"] = self.remove_detail
        if self.remove_executed:
            # Remove was attempted: always emit exit_code/null, stdout, stderr and
            # timed_out as structured fields, even when the adapter lost the value.
            diagnostics["remove_exit_code"] = self.remove_exit_code
            diagnostics["remove_stdout"] = self.remove_stdout
            diagnostics["remove_stderr"] = self.remove_stderr
            diagnostics["remove_timed_out"] = self.remove_timed_out
        return diagnostics


class ModuleTransactionCreateError(ModuleTransactionFailure):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            execution_phase=EXECUTION_PHASE_CREATE,
            execution_state=EXECUTION_FAILED_BEFORE_EXEC,
            side_effect_possible=False,
        )


class ModuleTransactionStartError(ModuleTransactionFailure):
    def __init__(
        self,
        message: str,
        *,
        container_id: str,
    ) -> None:
        super().__init__(
            message,
            execution_phase=EXECUTION_PHASE_START,
            execution_state=EXECUTION_FAILED_BEFORE_EXEC,
            side_effect_possible=True,
            container_id=container_id,
        )


class ModuleTransactionExecError(ModuleTransactionFailure):
    def __init__(
        self,
        message: str,
        *,
        container_id: str,
        timed_out: bool = False,
        exit_code: int | None = None,
        run_id: str | None = None,
        exit_failed: bool = False,
        stdout_excerpt: str = "",
        stderr_excerpt: str = "",
    ) -> None:
        if timed_out:
            execution_state = EXECUTION_TIMED_OUT
        elif exit_failed or exit_code is not None:
            execution_state = EXECUTION_EXITED
        else:
            execution_state = EXECUTION_STARTED_UNKNOWN
        super().__init__(
            message,
            execution_phase=EXECUTION_PHASE_EXEC,
            execution_state=execution_state,
            side_effect_possible=True,
            container_id=container_id,
            exit_code=exit_code,
            run_id=run_id,
        )
        self.timed_out = timed_out
        self.exit_failed = exit_failed
        self.stdout_excerpt = stdout_excerpt
        self.stderr_excerpt = stderr_excerpt

    @classmethod
    def from_nonzero_exit(
        cls,
        exec_result: ContainerResult,
        *,
        container_id: str,
        run_id: str,
    ) -> ModuleTransactionExecError:
        exit_code = int(exec_result.exit_code) if exec_result.exit_code is not None else None
        return cls(
            f"exit_code={exit_code}",
            container_id=container_id,
            exit_code=exit_code,
            run_id=run_id,
            exit_failed=True,
            stdout_excerpt=(exec_result.stdout or "")[:4096],
            stderr_excerpt=(exec_result.stderr or "")[:4096],
        )


class ModuleTransactionCollectError(ModuleTransactionFailure):
    def __init__(
        self,
        message: str,
        *,
        container_id: str,
        exit_code: int | None,
        run_id: str,
    ) -> None:
        execution_state = EXECUTION_EXITED if exit_code is not None else EXECUTION_STARTED_UNKNOWN
        super().__init__(
            message,
            execution_phase=EXECUTION_PHASE_COLLECT,
            execution_state=execution_state,
            side_effect_possible=True,
            container_id=container_id,
            exit_code=exit_code,
            run_id=run_id,
        )


@dataclass(frozen=True)
class ModuleAcquireResult:
    record: ModuleContainerRecord
    action: AcquireAction


@dataclass
class ModuleContainerRegistry:
    """Track reusable module containers keyed by stable boundary identity."""

    records: dict[str, ModuleContainerRecord] = field(default_factory=dict)
    module_active_key: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def registry_key(*, project_id: str, module_id: str, boundary_fingerprint: str, image_version: str) -> str:
        raw = f"{project_id}:{module_id}:{boundary_fingerprint}:{image_version}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def module_identity_key(*, project_id: str, module_id: str) -> str:
        return hashlib.sha256(f"{project_id}:{module_id}".encode()).hexdigest()

    def get(self, key: str) -> ModuleContainerRecord | None:
        return self.records.get(key)

    def upsert(self, key: str, record: ModuleContainerRecord) -> None:
        self.records[key] = record

    def mark_tainted(self, key: str, reason: str) -> None:
        record = self.records.get(key)
        if record is None:
            return
        record.state = ModuleContainerState.TAINTED
        record.taint_reason = reason


def build_module_container_name(
    *,
    project_root: Path,
    module_id: str,
    boundary_fingerprint: str,
    image_version: str,
) -> str:
    project_hash = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:12]
    module_slug = re.sub(r"[^a-zA-Z0-9_.-]", "-", module_id)[:32]
    fp = boundary_fingerprint[:12]
    version_slug = re.sub(r"[^a-zA-Z0-9_.-]", "-", image_version)[:16]
    return f"bridle-mod-{project_hash}-{module_slug}-{fp}-{version_slug}"[:128]


def validate_container_request_extended(request: ContainerRequest) -> None:
    for key in request.environment:
        if not _ENV_KEY_PATTERN.match(key):
            raise ValueError(f"Invalid environment variable name: {key}")
    if request.network_mode not in {"bridge", "none"}:
        raise ValueError("Container network_mode must be bridge or none")
    if request.role == "agent" and request.environment.get("BRIDLE_AGENT_API_KEY"):
        raise ValueError("Agent container must not receive BRIDLE_AGENT_API_KEY")
    for host in request.extra_hosts or []:
        if ":" not in host:
            raise ValueError("Invalid extra_hosts entry")


def _container_ids_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return longer.startswith(shorter)


def _format_remove_failure(exc: Exception) -> str:
    if isinstance(exc, ContainerRemoveError):
        parts: list[str] = []
        if exc.timed_out:
            parts.append("timed_out=true")
        else:
            parts.append(f"exit_code={exc.exit_code}")
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        if stdout:
            parts.append(f"stdout={stdout}")
        if stderr:
            parts.append(f"stderr={stderr}")
        return " ".join(parts)
    return str(exc)


def _remove_diagnostics_from_exc(exc: Exception) -> dict[str, object]:
    if isinstance(exc, ContainerRemoveError):
        return {
            "exit_code": exc.exit_code,
            "stdout": exc.stdout,
            "stderr": exc.stderr,
            "timed_out": exc.timed_out,
        }
    return {"exit_code": None, "stdout": None, "stderr": str(exc), "timed_out": None}


def strict_cleanup_container(runner: ContainerRunner, container_id: str) -> CleanupOutcome:
    """Stop and remove a container; return structured cleanup outcome without raising."""
    outcome = CleanupOutcome(container_id=container_id)
    if not container_id:
        return outcome
    if isinstance(runner, LocalContainerRuntimeRunner) and runner.use_docker:
        outcome.remove_executed = True
        try:
            runner.remove(container_id)
        except Exception as exc:
            outcome.remove_outcome = "failed"
            outcome.remove_detail = _format_remove_failure(exc)
            for key, value in _remove_diagnostics_from_exc(exc).items():
                setattr(outcome, f"remove_{key}", value)
        return outcome
    try:
        runner.stop(container_id)
    except Exception as exc:
        outcome.stop_failed = True
        outcome.stop_detail = str(exc)
    remove_fn = getattr(runner, "remove", None)
    if not callable(remove_fn):
        outcome.remove_outcome = "unknown"
        return outcome
    outcome.remove_executed = True
    try:
        remove_fn(container_id)  # type: ignore[operator]
    except Exception as exc:
        outcome.remove_outcome = "failed"
        outcome.remove_detail = _format_remove_failure(exc)
        for key, value in _remove_diagnostics_from_exc(exc).items():
            setattr(outcome, f"remove_{key}", value)
    return outcome


def cleanup_container(runner: ContainerRunner, container_id: str) -> None:
    """Stop and remove a container; cleanup errors are logged and swallowed."""
    if not container_id:
        return
    if isinstance(runner, LocalContainerRuntimeRunner) and runner.use_docker:
        try:
            runner.remove(container_id)
        except Exception as exc:
            logger.info(
                "container_remove_failed",
                extra={
                    "action": "container_remove_failed",
                    "status": "failed",
                    "detail": {"container_id": container_id, "error": str(exc)},
                },
            )
        return
    try:
        runner.stop(container_id)
    except (ValueError, KeyError, RuntimeError) as exc:
        logger.info(
            "container_stop_skipped",
            extra={
                "action": "container_stop_skipped",
                "status": "skipped",
                "detail": {"container_id": container_id, "error": str(exc)},
            },
        )
    if hasattr(runner, "remove"):
        try:
            runner.remove(container_id)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.info(
                "container_remove_failed",
                extra={
                    "action": "container_remove_failed",
                    "status": "failed",
                    "detail": {"container_id": container_id, "error": str(exc)},
                },
            )


def truncate_logs(lines: list[str], *, max_bytes: int = _MAX_LOG_BYTES) -> tuple[list[str], bool]:
    total = 0
    kept: list[str] = []
    truncated = False
    for line in lines:
        size = len(line.encode("utf-8", errors="replace")) + 1
        if total + size > max_bytes:
            truncated = True
            break
        kept.append(line)
        total += size
    return kept, truncated


class ModuleContainerManager:
    """Acquire, reuse, or replace module-bound containers."""

    def __init__(self, runner: ContainerRunner, project_root: Path, *, project_id: str = "local") -> None:
        self._runner = runner
        self._project_root = project_root.resolve()
        self._project_id = project_id
        self._registry = ModuleContainerRegistry()
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)

    @property
    def registry(self) -> ModuleContainerRegistry:
        return self._registry

    def _module_lock(self, module_id: str) -> threading.RLock:
        identity = ModuleContainerRegistry.module_identity_key(
            project_id=self._project_id,
            module_id=module_id,
        )
        return self._locks[identity]

    def acquire(
        self,
        request: ContainerRequest,
        *,
        registry_key: str,
        replace: bool = False,
    ) -> ModuleAcquireResult:
        with self._module_lock(request.module_id):
            return self._acquire_locked(request, registry_key=registry_key, replace=replace)

    def _retire_other_module_containers(self, module_id: str, active_key: str) -> None:
        module_identity = ModuleContainerRegistry.module_identity_key(
            project_id=self._project_id,
            module_id=module_id,
        )
        previous_key = self._registry.module_active_key.get(module_identity)
        if previous_key and previous_key != active_key:
            stale = self._registry.get(previous_key)
            if stale is not None:
                stale.state = ModuleContainerState.REPLACING
                cleanup_container(self._runner, stale.container_id)
            self._registry.records.pop(previous_key, None)
        for key, record in list(self._registry.records.items()):
            if record.module_id != module_id or key == active_key:
                continue
            record.state = ModuleContainerState.REPLACING
            cleanup_container(self._runner, record.container_id)
            self._registry.records.pop(key, None)

    def _retire_discovered_module_containers(self, request: ContainerRequest, active_key: str) -> None:
        list_fn = getattr(self._runner, "list_by_module_labels", None)
        if list_fn is None:
            return
        project_label = request.labels.get("bridle.project", "")
        if not project_label:
            return
        for container_id, _stored_req, _result in list_fn(project_label, request.module_id):
            rebuild = getattr(self._runner, "rebuild_request_from_inspect", None) or getattr(
                self._runner, "get_stored_request", None
            )
            inspected_req = rebuild(container_id) if rebuild is not None else None
            if inspected_req is None:
                registered = self._registry.get(active_key)
                if registered is not None and _container_ids_match(container_id, registered.container_id):
                    continue
                cleanup_container(self._runner, container_id)
                continue
            inspected_key = ModuleContainerRegistry.registry_key(
                project_id=self._project_id,
                module_id=inspected_req.module_id,
                boundary_fingerprint=inspected_req.boundary_fingerprint,
                image_version=inspected_req.image_version,
            )
            if inspected_key == active_key and inspected_req.name == request.name:
                continue
            cleanup_container(self._runner, container_id)
            self._registry.records.pop(inspected_key, None)
            if hasattr(self._runner, "remove"):
                with contextlib.suppress(Exception):
                    self._runner.remove(container_id)  # type: ignore[attr-defined]

    def _adopt_existing_by_name(self, request: ContainerRequest, registry_key: str) -> ModuleContainerRecord | None:
        finder = getattr(self._runner, "find_by_name", None)
        if finder is None:
            return None
        inspected = finder(request.name)
        if inspected is None:
            return None
        get_request = getattr(self._runner, "rebuild_request_from_inspect", None) or getattr(
            self._runner, "get_stored_request", None
        )
        stored_req = get_request(inspected.container_id) if get_request is not None else None
        if stored_req is None:
            cleanup_container(self._runner, inspected.container_id)
            return None
        mismatches = validate_container_identity(request, stored_req)
        if mismatches:
            logger.info(
                "module_container_adopt_rejected",
                extra={
                    "action": "module_container_adopt_rejected",
                    "status": "rejected",
                    "detail": {
                        "container_id": inspected.container_id,
                        "module_id": request.module_id,
                        "reasons": mismatches,
                    },
                },
            )
            cleanup_container(self._runner, inspected.container_id)
            return None
        if inspected.status != "running" or inspected.health in {"missing", "unhealthy"}:
            cleanup_container(self._runner, inspected.container_id)
            return None
        record = ModuleContainerRecord(
            container_id=inspected.container_id,
            name=request.name,
            module_id=request.module_id,
            boundary_fingerprint=request.boundary_fingerprint,
            image_version=request.image_version,
            state=ModuleContainerState.IDLE,
        )
        self._registry.upsert(registry_key, record)
        logger.info(
            "module_container_adopted",
            extra={
                "action": "module_container_adopted",
                "status": "completed",
                "detail": {
                    "container_id": record.container_id,
                    "module_id": request.module_id,
                    "name": request.name,
                },
            },
        )
        return record

    def _acquire_locked(
        self,
        request: ContainerRequest,
        *,
        registry_key: str,
        replace: bool,
    ) -> ModuleAcquireResult:
        validate_container_request_extended(request)
        self._retire_other_module_containers(request.module_id, registry_key)
        self._retire_discovered_module_containers(request, registry_key)
        existing = self._registry.get(registry_key)
        tainted_states = {ModuleContainerState.TAINTED, ModuleContainerState.REPLACING}
        if existing and not replace and existing.state not in tainted_states:
            if existing.state == ModuleContainerState.RUNNING:
                raise ModuleTransactionStartError(
                    "module_container_busy",
                    container_id=existing.container_id,
                )
            try:
                inspected = self._runner.inspect(existing.container_id)
            except (KeyError, ValueError, RuntimeError):
                inspected = ContainerResult(
                    container_id=existing.container_id,
                    name=existing.name,
                    status="failed",
                    network_mode="none",
                    health="missing",
                )
            if inspected.status == "running" and inspected.health in {"healthy", "starting", "unknown"}:
                existing.state = ModuleContainerState.IDLE
                self._registry.module_active_key[
                    ModuleContainerRegistry.module_identity_key(
                        project_id=self._project_id,
                        module_id=request.module_id,
                    )
                ] = registry_key
                logger.info(
                    "module_container_reused",
                    extra={
                        "action": "module_container_reused",
                        "status": "completed",
                        "detail": {
                            "container_id": existing.container_id,
                            "module_id": request.module_id,
                            "boundary_fingerprint": request.boundary_fingerprint,
                        },
                    },
                )
                return ModuleAcquireResult(existing, AcquireAction.REGISTRY_REUSED)
            existing.state = ModuleContainerState.TAINTED
            existing.taint_reason = "health_check_failed"

        if existing and existing.state == ModuleContainerState.TAINTED:
            replace = True

        if existing and replace:
            existing.state = ModuleContainerState.REPLACING
            cleanup_container(self._runner, existing.container_id)
            self._registry.records.pop(registry_key, None)

        adopted = self._adopt_existing_by_name(request, registry_key)
        if adopted is not None and not replace:
            self._registry.module_active_key[
                ModuleContainerRegistry.module_identity_key(
                    project_id=self._project_id,
                    module_id=request.module_id,
                )
            ] = registry_key
            return ModuleAcquireResult(adopted, AcquireAction.DAEMON_ADOPTED)

        try:
            created = self._runner.create(request)
        except Exception as exc:
            raise ModuleTransactionCreateError(str(exc)) from exc
        try:
            started = self._runner.start(created.container_id)
        except Exception as exc:
            start_error = ModuleTransactionStartError(
                str(exc),
                container_id=created.container_id,
            )
            cleanup_outcome = strict_cleanup_container(self._runner, created.container_id)
            if cleanup_outcome.needs_secondary:
                start_error.attach_cleanup_secondary(cleanup_outcome)
            raise start_error from exc
        record = ModuleContainerRecord(
            container_id=started.container_id,
            name=request.name,
            module_id=request.module_id,
            boundary_fingerprint=request.boundary_fingerprint,
            image_version=request.image_version,
            state=ModuleContainerState.IDLE,
        )
        self._registry.upsert(registry_key, record)
        self._registry.module_active_key[
            ModuleContainerRegistry.module_identity_key(
                project_id=self._project_id,
                module_id=request.module_id,
            )
        ] = registry_key
        logger.info(
            "module_container_created",
            extra={
                "action": "module_container_created",
                "status": "completed",
                "detail": {
                    "container_id": record.container_id,
                    "module_id": request.module_id,
                    "boundary_fingerprint": request.boundary_fingerprint,
                },
            },
        )
        return ModuleAcquireResult(record, AcquireAction.CREATED)

    def exec_task(
        self,
        record: ModuleContainerRecord,
        *,
        command: list[str],
        timeout_seconds: int,
        run_id: str,
        environment: dict[str, str] | None = None,
        registry_key: str | None = None,
        module_id: str | None = None,
    ) -> ContainerResult:
        lock_module = module_id or record.module_id
        with self._module_lock(lock_module):
            return self._exec_task_unlocked(
                record,
                command=command,
                timeout_seconds=timeout_seconds,
                run_id=run_id,
                environment=environment,
            )

    def _exec_task_unlocked(
        self,
        record: ModuleContainerRecord,
        *,
        command: list[str],
        timeout_seconds: int,
        run_id: str,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        if record.state == ModuleContainerState.RUNNING:
            raise RuntimeError("module_container_busy")
        record.state = ModuleContainerState.RUNNING
        record.last_run_id = run_id
        try:
            if hasattr(self._runner, "exec"):
                result = self._runner.exec(  # type: ignore[attr-defined]
                    record.container_id,
                    command,
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                )
            else:
                raise RuntimeError("container_exec_unsupported")
            record.state = ModuleContainerState.IDLE
            return result
        except TimeoutError:
            record.state = ModuleContainerState.TAINTED
            record.taint_reason = "exec_timeout"
            raise
        except Exception as exc:
            if not self._recover_idle(record):
                record.state = ModuleContainerState.TAINTED
                record.taint_reason = str(exc)
            raise

    def mark_slot_root_compromised(self, registry_key: str, *, reason: str) -> None:
        record = self._registry.get(registry_key)
        if record is not None:
            record.state = ModuleContainerState.TAINTED
            record.taint_reason = reason
        logger.info(
            "module_container_slot_root_compromised",
            extra={
                "action": "module_container_slot_root_compromised",
                "status": "tainted",
                "detail": {"registry_key": registry_key, "reason": reason},
            },
        )

    def run_module_transaction(
        self,
        module_id: str,
        *,
        prepare: Callable[[], None],
        collect: Callable[[], None],
        request_builder: Callable[[], ContainerRequest],
        registry_key: str,
        replace: bool,
        run_id: str,
        command: list[str],
        environment: Callable[[], dict[str, str]] | dict[str, str] | None = None,
    ) -> tuple[ModuleContainerRecord, ContainerResult, AcquireAction]:
        with self._module_lock(module_id):
            prepared = False
            record: ModuleContainerRecord | None = None
            exec_result: ContainerResult | None = None
            action = AcquireAction.CREATED
            failure: ModuleTransactionFailure | None = None
            try:
                prepare()
                prepared = True
                request = request_builder()
                try:
                    acquire_result = self._acquire_locked(
                        request, registry_key=registry_key, replace=replace
                    )
                except ModuleTransactionCreateError as exc:
                    failure = exc
                except ModuleTransactionStartError as exc:
                    failure = exc
                else:
                    record = acquire_result.record
                    action = acquire_result.action
                    if failure is None:
                        env = environment() if callable(environment) else dict(environment or {})
                        try:
                            exec_result = self._exec_task_unlocked(
                                record,
                                command=command,
                                timeout_seconds=request.timeout_seconds,
                                run_id=run_id,
                                environment=env,
                            )
                        except TimeoutError as exc:
                            failure = ModuleTransactionExecError(
                                str(exc),
                                container_id=record.container_id,
                                timed_out=True,
                                run_id=run_id,
                            )
                        except Exception as exc:
                            failure = ModuleTransactionExecError(
                                str(exc),
                                container_id=record.container_id,
                                run_id=run_id,
                            )
                        else:
                            if exec_result.exit_code not in (0, None):
                                failure = ModuleTransactionExecError.from_nonzero_exit(
                                    exec_result,
                                    container_id=record.container_id,
                                    run_id=run_id,
                                )
            finally:
                if prepared:
                    try:
                        collect()
                    except Exception as exc:
                        logger.info(
                            "active_slot_collect_failed",
                            extra={
                                "action": "active_slot_collect_failed",
                                "status": "failed",
                                "detail": {"module_id": module_id, "run_id": run_id, "error": str(exc)},
                            },
                        )
                        if failure is not None:
                            failure.attach_collect_secondary(exc)
                        elif record is not None and exec_result is not None:
                            if exec_result.exit_code in (0, None):
                                failure = ModuleTransactionCollectError(
                                    str(exc),
                                    container_id=record.container_id,
                                    exit_code=exec_result.exit_code,
                                    run_id=run_id,
                                )
                            else:
                                failure = ModuleTransactionExecError.from_nonzero_exit(
                                    exec_result,
                                    container_id=record.container_id,
                                    run_id=run_id,
                                )
                                failure.attach_collect_secondary(exc)
                        else:
                            raise
            if failure is not None:
                raise failure
            if record is None or exec_result is None:
                raise RuntimeError("module_transaction_incomplete")
            return record, exec_result, action

    def mark_run_failed(self, registry_key: str) -> None:
        record = self._registry.get(registry_key)
        if record is not None and record.state != ModuleContainerState.RUNNING:
            record.state = ModuleContainerState.IDLE

    def _recover_idle(self, record: ModuleContainerRecord) -> bool:
        try:
            inspected = self._runner.inspect(record.container_id)
        except (KeyError, ValueError, RuntimeError):
            return False
        if inspected.status == "running" and inspected.health in {"healthy", "running", "unknown"}:
            record.state = ModuleContainerState.IDLE
            return True
        return False
