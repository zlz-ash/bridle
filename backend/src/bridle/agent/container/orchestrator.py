"""Unified container lifecycle orchestration for agent execution."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bridle.agent.container.active_slot import ActiveSlotLayout, collect_active_slot, prepare_active_slot
from bridle.agent.container.candidate_path_guard import CandidatePathError
from bridle.agent.container.container_control import (
    EXECUTION_EXITED,
    EXECUTION_FAILED_BEFORE_EXEC,
    EXECUTION_PHASE_CLEANUP,
    EXECUTION_PHASE_COLLECT,
    EXECUTION_PHASE_EXEC,
    EXECUTION_PHASE_PREPARE,
    EXECUTION_PHASE_START,
    EXECUTION_STARTED_UNKNOWN,
    EXECUTION_TIMED_OUT,
)
from bridle.agent.container.lifecycle import (
    AcquireAction,
    ModuleContainerManager,
    ModuleContainerRegistry,
    ModuleTransactionCollectError,
    ModuleTransactionCreateError,
    ModuleTransactionExecError,
    ModuleTransactionFailure,
    ModuleTransactionStartError,
    cleanup_container,
    truncate_logs,
)
from bridle.agent.container.runner import ContainerRequest, ContainerRunner

logger = logging.getLogger("bridle")

_NORMALIZED_HEALTHY = frozenset({"unknown", "exited", "stopped", "healthy"})


@dataclass(frozen=True)
class OrchestratedContainerResult:
    container_id: str
    name: str
    status: str
    network_mode: str
    health: str
    logs: list[str]
    logs_summary: str
    diagnostic_path: Path | None
    exit_code: int | None = None
    exec_stdout: str = ""
    exec_stderr: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    logs_truncated: bool = False
    reused: bool = False
    execution_phase: str = EXECUTION_PHASE_EXEC
    execution_state: str = EXECUTION_EXITED
    side_effect_possible: bool = True


@dataclass
class OrchestrationError(Exception):
    error_code: str
    container_id: str | None = None
    detail: dict = field(default_factory=dict)
    execution_phase: str = ""
    execution_state: str = ""
    side_effect_possible: bool = False
    exit_code: int | None = None
    secondary_execution_phase: str | None = None
    secondary_error_code: str | None = None
    secondary_detail: str | None = None
    start_cleanup_failure: str | None = None
    resource_may_remain: bool = False
    secondary_diagnostics: dict[str, object] | None = None

    def __str__(self) -> str:
        return self.error_code


def _primary_error_code(failure: ModuleTransactionFailure) -> str:
    if isinstance(failure, ModuleTransactionCreateError):
        return "container_create_failed"
    if isinstance(failure, ModuleTransactionStartError):
        return "container_start_failed"
    if isinstance(failure, ModuleTransactionCollectError):
        return "active_slot_collect_failed"
    if isinstance(failure, ModuleTransactionExecError):
        if failure.timed_out:
            return "container_wait_timeout"
        if failure.exit_failed:
            return "container_exit_failed"
        return "container_exec_failed"
    return "container_exec_failed"


def _orchestration_error_from_failure(
    failure: ModuleTransactionFailure,
    *,
    candidate_rel: str,
    module_root: str | None = None,
) -> OrchestrationError:
    detail: dict[str, object] = {"error": str(failure), "candidate_rel": candidate_rel}
    if failure.run_id:
        detail["run_id"] = failure.run_id
    if module_root is not None:
        detail["module_root"] = module_root
    if failure.start_cleanup_failure:
        detail["start_cleanup_failure"] = failure.start_cleanup_failure
    if failure.resource_may_remain:
        detail["resource_may_remain"] = True
    if failure.secondary_diagnostics is not None:
        detail["secondary_diagnostics"] = dict(failure.secondary_diagnostics)
    if failure.secondary_detail:
        if failure.secondary_execution_phase == EXECUTION_PHASE_CLEANUP:
            detail["secondary_cleanup_error"] = failure.secondary_detail
        elif failure.secondary_execution_phase == EXECUTION_PHASE_COLLECT:
            detail["secondary_collect_error"] = failure.secondary_detail
        else:
            detail["secondary_detail"] = failure.secondary_detail
    if isinstance(failure, ModuleTransactionExecError) and failure.exit_failed:
        detail["stdout"] = failure.stdout_excerpt
        detail["stderr"] = failure.stderr_excerpt
    exit_raw = failure.exit_code
    exec_exit_code = int(exit_raw) if exit_raw is not None else None
    return _orchestration_error(
        _primary_error_code(failure),
        failure.container_id,
        detail,
        execution_phase=failure.execution_phase,
        execution_state=failure.execution_state,
        side_effect_possible=failure.side_effect_possible,
        exit_code=exec_exit_code,
        secondary_execution_phase=failure.secondary_execution_phase,
        secondary_error_code=failure.secondary_error_code,
        secondary_detail=failure.secondary_detail,
        start_cleanup_failure=failure.start_cleanup_failure,
        resource_may_remain=failure.resource_may_remain,
        secondary_diagnostics=failure.secondary_diagnostics,
    )


def _orchestration_error(
    error_code: str,
    container_id: str | None,
    detail: dict[str, object],
    *,
    execution_phase: str,
    execution_state: str,
    side_effect_possible: bool,
    exit_code: int | None = None,
    secondary_execution_phase: str | None = None,
    secondary_error_code: str | None = None,
    secondary_detail: str | None = None,
    start_cleanup_failure: str | None = None,
    resource_may_remain: bool = False,
    secondary_diagnostics: dict[str, object] | None = None,
) -> OrchestrationError:
    return OrchestrationError(
        error_code=error_code,
        container_id=container_id,
        detail=dict(detail),
        execution_phase=execution_phase,
        execution_state=execution_state,
        side_effect_possible=side_effect_possible,
        exit_code=exit_code,
        secondary_execution_phase=secondary_execution_phase,
        secondary_error_code=secondary_error_code,
        secondary_detail=secondary_detail,
        start_cleanup_failure=start_cleanup_failure,
        resource_may_remain=resource_may_remain,
        secondary_diagnostics=secondary_diagnostics,
    )


class ContainerOrchestrator:
    def __init__(
        self,
        runner: ContainerRunner,
        workspace_root: Path,
        *,
        project_id: str = "local",
    ) -> None:
        self._runner = runner
        self._workspace_root = workspace_root
        self._module_manager = ModuleContainerManager(
            runner,
            workspace_root,
            project_id=str(workspace_root.resolve()),
        )

    @property
    def module_manager(self) -> ModuleContainerManager:
        return self._module_manager

    def run_module_exec(
        self,
        request: ContainerRequest,
        *,
        module_root: Path,
        candidate_rel: str,
        run_id: str,
        command: list[str],
        diag_dir: Path | None = None,
        replace_container: bool = False,
        exec_environment: dict[str, str] | None = None,
    ) -> OrchestratedContainerResult:
        """Reuse or create a module container and exec one task inside it."""
        if diag_dir is not None:
            diag_dir.mkdir(parents=True, exist_ok=True)

        registry_key = ModuleContainerRegistry.registry_key(
            project_id=str(self._workspace_root),
            module_id=request.module_id,
            boundary_fingerprint=request.boundary_fingerprint,
            image_version=request.image_version,
        )

        try:
            acquire_result = self._module_manager.acquire(request, registry_key=registry_key, replace=replace_container)
        except ModuleTransactionCreateError as exc:
            self._write_diag(diag_dir, "create.error", str(exc))
            raise _orchestration_error_from_failure(
                exc,
                candidate_rel=candidate_rel,
                module_root=str(module_root),
            ) from exc
        except ModuleTransactionStartError as exc:
            self._write_diag(diag_dir, "startup.error", str(exc))
            raise _orchestration_error_from_failure(
                exc,
                candidate_rel=candidate_rel,
                module_root=str(module_root),
            ) from exc
        except (ValueError, RuntimeError) as exc:
            self._write_diag(diag_dir, "startup.error", str(exc))
            raise _orchestration_error(
                "container_start_failed",
                None,
                {"name": request.name, "error": str(exc), "module_root": str(module_root)},
                execution_phase=EXECUTION_PHASE_START,
                execution_state=EXECUTION_FAILED_BEFORE_EXEC,
                side_effect_possible=False,
            ) from exc

        record = acquire_result.record
        reused = acquire_result.action != AcquireAction.CREATED
        env = dict(exec_environment or {})
        if env.get("BRIDLE_ACTIVE_SLOT") != "1":
            env.setdefault("BRIDLE_CANDIDATE_REL", candidate_rel)
        try:
            exec_result = self._module_manager.exec_task(
                record,
                command=command,
                timeout_seconds=request.timeout_seconds,
                run_id=run_id,
                environment=env,
                registry_key=registry_key,
                module_id=request.module_id,
            )
        except TimeoutError as exc:
            self._write_diag(diag_dir, "wait.error", str(exc))
            raise _orchestration_error(
                "container_wait_timeout",
                record.container_id,
                {"name": request.name, "error": str(exc), "candidate_rel": candidate_rel},
                execution_phase=EXECUTION_PHASE_EXEC,
                execution_state=EXECUTION_TIMED_OUT,
                side_effect_possible=True,
            ) from exc
        except Exception as exc:
            self._module_manager.mark_run_failed(registry_key)
            self._write_diag(diag_dir, "exec.error", str(exc))
            raise _orchestration_error(
                "container_exec_failed",
                record.container_id,
                {"name": request.name, "error": str(exc), "candidate_rel": candidate_rel},
                execution_phase=EXECUTION_PHASE_EXEC,
                execution_state=EXECUTION_STARTED_UNKNOWN,
                side_effect_possible=True,
            ) from exc

        return self._finalize_exec_result(
            request=request,
            record=record,
            exec_result=exec_result,
            registry_key=registry_key,
            reused=reused,
            candidate_rel=candidate_rel,
            module_root=module_root,
            run_id=run_id,
            diag_dir=diag_dir,
        )

    def run_candidate_test_transaction(
        self,
        *,
        module_id: str,
        module_root: Path,
        candidate_root: Path,
        candidate_rel: str,
        run_id: str,
        boundary_fingerprint: str,
        image_version: str,
        build_request: Callable[[ActiveSlotLayout], ContainerRequest],
        command: list[str],
        diag_dir: Path | None = None,
        replace_container: bool = False,
        exec_environment: Callable[[ActiveSlotLayout], dict[str, str]] | None = None,
    ) -> OrchestratedContainerResult:
        if diag_dir is not None:
            diag_dir.mkdir(parents=True, exist_ok=True)

        layout_holder: dict[str, ActiveSlotLayout] = {}
        registry_key = ModuleContainerRegistry.registry_key(
            project_id=str(self._workspace_root.resolve()),
            module_id=module_id,
            boundary_fingerprint=boundary_fingerprint,
            image_version=image_version,
        )

        def prepare() -> None:
            layout_holder["layout"] = prepare_active_slot(
                module_root,
                candidate_root,
                project_root=self._workspace_root,
                candidate_rel=candidate_rel,
                run_id=run_id,
            )

        def collect() -> None:
            collect_active_slot(module_root, candidate_root, project_root=self._workspace_root)

        def request_builder() -> ContainerRequest:
            return build_request(layout_holder["layout"])

        def environment() -> dict[str, str]:
            if exec_environment is None:
                return {}
            return exec_environment(layout_holder["layout"])

        try:
            record, exec_result, action = self._module_manager.run_module_transaction(
                module_id,
                prepare=prepare,
                collect=collect,
                request_builder=request_builder,
                registry_key=registry_key,
                replace=replace_container,
                run_id=run_id,
                command=command,
                environment=environment,
            )
        except ModuleTransactionFailure as exc:
            if isinstance(exc, ModuleTransactionExecError):
                self._module_manager.mark_run_failed(registry_key)
                if exc.exit_failed:
                    self._write_diag(
                        diag_dir,
                        "exit.error",
                        f"exit_code={exc.exit_code}\nstderr={exc.stderr_excerpt}\nstdout={exc.stdout_excerpt}\n",
                    )
                else:
                    self._write_diag(diag_dir, "exec.error", str(exc))
            elif isinstance(exc, ModuleTransactionCollectError):
                self._write_diag(diag_dir, "collect.error", str(exc))
            elif isinstance(exc, ModuleTransactionStartError):
                self._write_diag(diag_dir, "startup.error", str(exc))
            elif isinstance(exc, ModuleTransactionCreateError):
                self._write_diag(diag_dir, "create.error", str(exc))
            raise _orchestration_error_from_failure(
                exc,
                candidate_rel=candidate_rel,
                module_root=str(module_root),
            ) from exc
        except CandidatePathError as exc:
            if exc.error_code == "active_slot_root_link":
                self._module_manager.mark_slot_root_compromised(registry_key, reason=str(exc))
                self._write_diag(diag_dir, "active_slot_root_link.error", str(exc))
                raise _orchestration_error(
                    "active_slot_root_link",
                    None,
                    {
                        "error": str(exc),
                        "candidate_rel": candidate_rel,
                        "module_root": str(module_root),
                        "error_code": exc.error_code,
                    },
                    execution_phase=EXECUTION_PHASE_PREPARE,
                    execution_state=EXECUTION_FAILED_BEFORE_EXEC,
                    side_effect_possible=False,
                ) from exc
            if exc.error_code == "active_slot_root_permission":
                self._module_manager.mark_slot_root_compromised(registry_key, reason=str(exc))
                self._write_diag(diag_dir, "active_slot_root_permission.error", str(exc))
                raise _orchestration_error(
                    "active_slot_root_permission",
                    None,
                    {
                        "error": str(exc),
                        "candidate_rel": candidate_rel,
                        "module_root": str(module_root),
                        "error_code": exc.error_code,
                    },
                    execution_phase=EXECUTION_PHASE_PREPARE,
                    execution_state=EXECUTION_FAILED_BEFORE_EXEC,
                    side_effect_possible=False,
                ) from exc
            self._write_diag(diag_dir, "prepare.error", str(exc))
            raise _orchestration_error(
                "active_slot_prepare_failed",
                None,
                {"error": str(exc), "candidate_rel": candidate_rel, "error_code": exc.error_code},
                execution_phase=EXECUTION_PHASE_PREPARE,
                execution_state=EXECUTION_FAILED_BEFORE_EXEC,
                side_effect_possible=False,
            ) from exc

        request = build_request(layout_holder["layout"])
        reused = action != AcquireAction.CREATED
        return self._finalize_exec_result(
            request=request,
            record=record,
            exec_result=exec_result,
            registry_key=registry_key,
            reused=reused,
            candidate_rel=candidate_rel,
            module_root=module_root,
            run_id=run_id,
            diag_dir=diag_dir,
        )

    def _finalize_exec_result(
        self,
        *,
        request: ContainerRequest,
        record,
        exec_result,
        registry_key: str,
        reused: bool,
        candidate_rel: str,
        module_root: Path,
        run_id: str,
        diag_dir: Path | None,
    ) -> OrchestratedContainerResult:
        try:
            logs = self._runner.collect_logs(record.container_id)
        except Exception as exc:
            self._write_diag(diag_dir, "logs.error", str(exc))
            logs = [str(exc)]

        trimmed, truncated = truncate_logs(logs)
        self._write_diag(diag_dir, "container.log", "\n".join(trimmed))

        if exec_result.exit_code not in (0, None):
            self._module_manager.mark_run_failed(registry_key)
            stderr_excerpt = (exec_result.stderr or "")[:4096]
            stdout_excerpt = (exec_result.stdout or "")[:4096]
            self._write_diag(
                diag_dir,
                "exit.error",
                f"exit_code={exec_result.exit_code}\nstderr={stderr_excerpt}\nstdout={stdout_excerpt}\n",
            )
            raise _orchestration_error(
                "container_exit_failed",
                record.container_id,
                {
                    "exit_code": exec_result.exit_code,
                    "candidate_rel": candidate_rel,
                    "module_root": str(module_root),
                    "stderr": stderr_excerpt,
                    "stdout": stdout_excerpt,
                },
                execution_phase=EXECUTION_PHASE_EXEC,
                execution_state=EXECUTION_EXITED,
                side_effect_possible=True,
                exit_code=int(exec_result.exit_code) if exec_result.exit_code is not None else None,
            )

        logger.info(
            "container_module_exec_completed",
            extra={
                "action": "container_module_exec_completed",
                "status": "completed",
                "detail": {
                    "container_id": record.container_id,
                    "module_id": request.module_id,
                    "reused": reused,
                    "run_id": run_id,
                    "candidate_rel": candidate_rel,
                },
            },
        )
        return OrchestratedContainerResult(
            container_id=record.container_id,
            name=record.name,
            status="running",
            network_mode=request.network_mode,
            health="healthy",
            logs=list(trimmed),
            logs_summary="; ".join(trimmed[-5:]) if trimmed else "",
            diagnostic_path=diag_dir,
            exit_code=exec_result.exit_code,
            exec_stdout=exec_result.stdout or "",
            exec_stderr=exec_result.stderr or "",
            finished_at=exec_result.finished_at,
            logs_truncated=truncated,
            reused=reused,
        )

    def run_and_wait(
        self,
        request: ContainerRequest,
        *,
        diag_dir: Path | None = None,
    ) -> OrchestratedContainerResult:
        """Create, start, wait, inspect, collect logs, and clean up on failure."""
        if diag_dir is not None:
            diag_dir.mkdir(parents=True, exist_ok=True)

        container_id: str | None = None
        try:
            created = self._runner.create(request)
            container_id = created.container_id
            started = self._runner.start(created.container_id)
        except Exception as exc:
            if container_id:
                cleanup_container(self._runner, container_id)
            self._write_diag(diag_dir, "startup.error", str(exc))
            logger.info(
                "container_startup_failed",
                extra={
                    "action": "container_startup_failed",
                    "status": "failed",
                    "detail": {"name": request.name, "error": str(exc)},
                },
            )
            raise _orchestration_error(
                "container_start_failed",
                container_id,
                {"name": request.name, "error": str(exc)},
                execution_phase=EXECUTION_PHASE_START,
                execution_state=EXECUTION_FAILED_BEFORE_EXEC,
                side_effect_possible=False,
            ) from exc

        try:
            waited = self._runner.wait(started.container_id, request.timeout_seconds)
        except TimeoutError as exc:
            cleanup_container(self._runner, started.container_id)
            self._write_diag(diag_dir, "wait.error", str(exc))
            raise _orchestration_error(
                "container_wait_timeout",
                started.container_id,
                {"name": request.name, "error": str(exc)},
                execution_phase=EXECUTION_PHASE_EXEC,
                execution_state=EXECUTION_TIMED_OUT,
                side_effect_possible=True,
            ) from exc
        except Exception as exc:
            cleanup_container(self._runner, started.container_id)
            self._write_diag(diag_dir, "wait.error", str(exc))
            raise _orchestration_error(
                "container_wait_failed",
                started.container_id,
                {"name": request.name, "error": str(exc)},
                execution_phase=EXECUTION_PHASE_EXEC,
                execution_state=EXECUTION_STARTED_UNKNOWN,
                side_effect_possible=True,
            ) from exc

        if waited.exit_code not in (0, None):
            cleanup_container(self._runner, waited.container_id)
            self._write_diag(diag_dir, "exit.error", f"exit_code={waited.exit_code}\n")
            raise _orchestration_error(
                "container_exit_failed",
                waited.container_id,
                {"exit_code": waited.exit_code},
                execution_phase=EXECUTION_PHASE_EXEC,
                execution_state=EXECUTION_EXITED,
                side_effect_possible=True,
                exit_code=int(waited.exit_code) if waited.exit_code is not None else None,
            )

        try:
            inspected = self._runner.inspect(started.container_id)
        except Exception as exc:
            cleanup_container(self._runner, started.container_id)
            self._write_diag(diag_dir, "inspect.error", str(exc))
            raise _orchestration_error(
                "container_inspect_failed",
                started.container_id,
                {"error": str(exc)},
                execution_phase=EXECUTION_PHASE_EXEC,
                execution_state=EXECUTION_STARTED_UNKNOWN,
                side_effect_possible=True,
            ) from exc

        if inspected.health == "unhealthy":
            cleanup_container(self._runner, inspected.container_id)
            self._write_diag(diag_dir, "health.error", f"status={inspected.status} health={inspected.health}\n")
            raise _orchestration_error(
                "container_health_failed",
                inspected.container_id,
                {"health": inspected.health},
                execution_phase=EXECUTION_PHASE_EXEC,
                execution_state=EXECUTION_STARTED_UNKNOWN,
                side_effect_possible=True,
            )

        try:
            logs = self._runner.collect_logs(started.container_id)
        except Exception as exc:
            cleanup_container(self._runner, started.container_id)
            self._write_diag(diag_dir, "logs.error", str(exc))
            raise _orchestration_error(
                "container_collect_logs_failed",
                started.container_id,
                {"error": str(exc)},
                execution_phase=EXECUTION_PHASE_COLLECT,
                execution_state=EXECUTION_STARTED_UNKNOWN,
                side_effect_possible=True,
            ) from exc

        trimmed, truncated = truncate_logs(logs)
        self._write_diag(diag_dir, "container.log", "\n".join(trimmed))

        effective_health = inspected.health
        if waited.exit_code == 0 and effective_health in _NORMALIZED_HEALTHY:
            effective_health = "healthy"

        logger.info(
            "container_run_completed",
            extra={
                "action": "container_run_completed",
                "status": "completed",
                "detail": {"container_id": started.container_id, "name": request.name, "health": effective_health},
            },
        )
        return OrchestratedContainerResult(
            container_id=started.container_id,
            name=started.name,
            status=waited.status,
            network_mode=started.network_mode,
            health=effective_health,
            logs=list(trimmed),
            logs_summary="; ".join(trimmed[-5:]) if trimmed else "",
            diagnostic_path=diag_dir,
            exit_code=waited.exit_code,
            started_at=started.started_at,
            finished_at=waited.finished_at,
            logs_truncated=truncated,
        )

    def cleanup(self, container_id: str) -> None:
        cleanup_container(self._runner, container_id)

    @staticmethod
    def _write_diag(diag_dir: Path | None, filename: str, content: str) -> None:
        if diag_dir is None:
            return
        diag_dir.mkdir(parents=True, exist_ok=True)
        (diag_dir / filename).write_text(content, encoding="utf-8")
