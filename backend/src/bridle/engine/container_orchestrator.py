"""ContainerOrchestrator — unified container lifecycle orchestration.

Encapsulates the create → start → wait → inspect → collect_logs → cleanup
flow shared by MainAgentContainerService and NodeContainerOrchestrator.
Service layers construct ContainerRequest with business parameters and
consume OrchestratedContainerResult; this module handles the mechanics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bridle.engine.container_lifecycle import cleanup_container as _cleanup
from bridle.engine.container_runner import ContainerRequest, ContainerRunner

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
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class _OrchestrationError(Exception):
    error_code: str
    container_id: str | None = None
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.error_code


class ContainerOrchestrator:
    def __init__(self, runner: ContainerRunner, workspace_root: Path) -> None:
        self._runner = runner
        self._workspace_root = workspace_root

    def run_and_wait(
        self,
        request: ContainerRequest,
        *,
        diag_dir: Path | None = None,
    ) -> OrchestratedContainerResult:
        """create -> start -> wait -> inspect -> collect_logs.

        On any failure (startup, timeout, non-zero exit, unhealthy,
        runner errors), automatically cleans up the container and
        writes diagnostic files.
        Raises _OrchestrationError on failure.
        """
        if diag_dir is not None:
            diag_dir.mkdir(parents=True, exist_ok=True)

        container_id: str | None = None
        try:
            created = self._runner.create(request)
            container_id = created.container_id
            started = self._runner.start(created.container_id)
        except (ValueError, RuntimeError) as exc:
            if container_id:
                self.cleanup(container_id)
            self._write_diag(diag_dir, "startup.error", str(exc))
            logger.info(
                "container_startup_failed",
                extra={
                    "action": "container_startup_failed",
                    "status": "failed",
                    "detail": {"name": request.name, "error": str(exc)},
                },
            )
            raise _OrchestrationError(
                error_code="container_start_failed",
                container_id=container_id,
                detail={"name": request.name, "error": str(exc)},
            ) from exc

        try:
            waited = self._runner.wait(started.container_id, request.timeout_seconds)
        except TimeoutError as exc:
            self.cleanup(started.container_id)
            self._write_diag(diag_dir, "wait.error", str(exc))
            logger.info(
                "container_wait_timeout",
                extra={
                    "action": "container_wait_timeout",
                    "status": "failed",
                    "detail": {"container_id": started.container_id, "name": request.name},
                },
            )
            raise _OrchestrationError(
                error_code="container_wait_timeout",
                container_id=started.container_id,
                detail={"name": request.name, "error": str(exc)},
            ) from exc
        except (ValueError, RuntimeError) as exc:
            self.cleanup(started.container_id)
            self._write_diag(diag_dir, "wait.error", str(exc))
            logger.info(
                "container_wait_failed",
                extra={
                    "action": "container_wait_failed",
                    "status": "failed",
                    "detail": {"container_id": started.container_id, "name": request.name, "error": str(exc)},
                },
            )
            raise _OrchestrationError(
                error_code="container_wait_failed",
                container_id=started.container_id,
                detail={"name": request.name, "error": str(exc)},
            ) from exc

        if waited.exit_code not in (0, None):
            self.cleanup(waited.container_id)
            self._write_diag(diag_dir, "exit.error", f"exit_code={waited.exit_code}\n")
            logger.info(
                "container_exit_failed",
                extra={
                    "action": "container_exit_failed",
                    "status": "failed",
                    "detail": {
                        "container_id": waited.container_id,
                        "exit_code": waited.exit_code,
                    },
                },
            )
            raise _OrchestrationError(
                error_code="container_exit_failed",
                container_id=waited.container_id,
                detail={"exit_code": waited.exit_code},
            )

        try:
            inspected = self._runner.inspect(started.container_id)
        except (ValueError, RuntimeError) as exc:
            self.cleanup(started.container_id)
            self._write_diag(diag_dir, "inspect.error", str(exc))
            logger.info(
                "container_inspect_failed",
                extra={
                    "action": "container_inspect_failed",
                    "status": "failed",
                    "detail": {"container_id": started.container_id, "error": str(exc)},
                },
            )
            raise _OrchestrationError(
                error_code="container_inspect_failed",
                container_id=started.container_id,
                detail={"error": str(exc)},
            ) from exc

        if inspected.health == "unhealthy":
            self.cleanup(inspected.container_id)
            self._write_diag(
                diag_dir,
                "health.error",
                f"status={inspected.status} health={inspected.health}\n",
            )
            logger.info(
                "container_health_failed",
                extra={
                    "action": "container_health_failed",
                    "status": "failed",
                    "detail": {
                        "container_id": inspected.container_id,
                        "health": inspected.health,
                    },
                },
            )
            raise _OrchestrationError(
                error_code="container_health_failed",
                container_id=inspected.container_id,
                detail={"health": inspected.health},
            )

        try:
            logs = self._runner.collect_logs(started.container_id)
        except (ValueError, RuntimeError) as exc:
            self.cleanup(started.container_id)
            self._write_diag(diag_dir, "logs.error", str(exc))
            logger.info(
                "container_collect_logs_failed",
                extra={
                    "action": "container_collect_logs_failed",
                    "status": "failed",
                    "detail": {"container_id": started.container_id, "error": str(exc)},
                },
            )
            raise _OrchestrationError(
                error_code="container_collect_logs_failed",
                container_id=started.container_id,
                detail={"error": str(exc)},
            ) from exc

        self._write_diag(diag_dir, "container.log", "\n".join(logs))

        logger.info(
            "container_run_completed",
            extra={
                "action": "container_run_completed",
                "status": "completed",
                "detail": {
                    "container_id": started.container_id,
                    "name": request.name,
                    "health": inspected.health,
                },
            },
        )

        effective_health = inspected.health
        if waited.exit_code == 0 and effective_health in _NORMALIZED_HEALTHY:
            effective_health = "healthy"

        return OrchestratedContainerResult(
            container_id=started.container_id,
            name=started.name,
            status=waited.status,
            network_mode=started.network_mode,
            health=effective_health,
            logs=list(logs),
            logs_summary="; ".join(logs[-5:]) if logs else "",
            diagnostic_path=diag_dir,
            exit_code=waited.exit_code,
            started_at=started.started_at,
            finished_at=waited.finished_at,
        )

    def start_detached(
        self,
        request: ContainerRequest,
        *,
        diag_dir: Path | None = None,
    ) -> OrchestratedContainerResult:
        """create -> start -> inspect -> collect_logs (no wait).

        For long-running containers like the main agent.
        On startup failure or unhealthy inspect, automatically cleans
        up and writes diagnostic files.
        Raises _OrchestrationError on failure.
        """
        if diag_dir is not None:
            diag_dir.mkdir(parents=True, exist_ok=True)

        container_id: str | None = None
        try:
            created = self._runner.create(request)
            container_id = created.container_id
            started = self._runner.start(created.container_id)
        except (ValueError, RuntimeError) as exc:
            if container_id:
                self.cleanup(container_id)
            self._write_diag(diag_dir, "startup.error", str(exc))
            logger.info(
                "container_startup_failed",
                extra={
                    "action": "container_startup_failed",
                    "status": "failed",
                    "detail": {"name": request.name, "error": str(exc)},
                },
            )
            raise _OrchestrationError(
                error_code="container_start_failed",
                container_id=container_id,
                detail={"name": request.name, "error": str(exc)},
            ) from exc

        try:
            inspected = self._runner.inspect(started.container_id)
        except (ValueError, RuntimeError) as exc:
            self.cleanup(started.container_id)
            self._write_diag(diag_dir, "inspect.error", str(exc))
            logger.info(
                "container_inspect_failed",
                extra={
                    "action": "container_inspect_failed",
                    "status": "failed",
                    "detail": {"container_id": started.container_id, "error": str(exc)},
                },
            )
            raise _OrchestrationError(
                error_code="container_inspect_failed",
                container_id=started.container_id,
                detail={"error": str(exc)},
            ) from exc

        if inspected.health == "unhealthy" or inspected.status in ("failed", "exited") or inspected.health == "exited":
            self.cleanup(started.container_id)
            self._write_diag(
                diag_dir,
                "health.error",
                f"status={inspected.status} health={inspected.health}\n",
            )
            logger.info(
                "container_health_failed",
                extra={
                    "action": "container_health_failed",
                    "status": "failed",
                    "detail": {
                        "container_id": started.container_id,
                        "health": inspected.health,
                        "status": inspected.status,
                    },
                },
            )
            raise _OrchestrationError(
                error_code="container_health_failed",
                container_id=started.container_id,
                detail={"health": inspected.health, "status": inspected.status},
            )

        try:
            logs = self._runner.collect_logs(started.container_id)
        except (ValueError, RuntimeError) as exc:
            self.cleanup(started.container_id)
            self._write_diag(diag_dir, "logs.error", str(exc))
            logger.info(
                "container_collect_logs_failed",
                extra={
                    "action": "container_collect_logs_failed",
                    "status": "failed",
                    "detail": {"container_id": started.container_id, "error": str(exc)},
                },
            )
            raise _OrchestrationError(
                error_code="container_collect_logs_failed",
                container_id=started.container_id,
                detail={"error": str(exc)},
            ) from exc

        logger.info(
            "container_started_detached",
            extra={
                "action": "container_started_detached",
                "status": "completed",
                "detail": {
                    "container_id": started.container_id,
                    "name": request.name,
                    "health": inspected.health,
                },
            },
        )

        return OrchestratedContainerResult(
            container_id=started.container_id,
            name=started.name,
            status=started.status,
            network_mode=started.network_mode,
            health=inspected.health,
            logs=list(logs),
            logs_summary="; ".join(logs[-3:]) if logs else "",
            diagnostic_path=diag_dir,
            started_at=started.started_at,
        )

    def cleanup(self, container_id: str) -> None:
        _cleanup(self._runner, container_id)

    @staticmethod
    def _write_diag(diag_dir: Path | None, filename: str, content: str) -> None:
        if diag_dir is None:
            return
        diag_dir.mkdir(parents=True, exist_ok=True)
        (diag_dir / filename).write_text(content, encoding="utf-8")
