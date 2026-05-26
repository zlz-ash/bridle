"""Create and start peer-level node agent containers."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bridle.engine.container_orchestrator import ContainerOrchestrator, _OrchestrationError
from bridle.engine.container_runner import ContainerMount, ContainerRequest, ContainerRunner
from bridle.engine.container_runner_factory import resolve_container_runner

logger = logging.getLogger("bridle")


class NodeContainerError(Exception):
    def __init__(self, error_code: str, *, message: str = "", detail: dict[str, Any] | None = None) -> None:
        self.error_code = error_code
        self.detail = detail or {}
        super().__init__(message or error_code)


class NodeContainerOrchestrator:
    def __init__(
        self,
        workspace_root: str | Path,
        *,
        runner: ContainerRunner | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        resolved_runner = resolve_container_runner(workspace_root, runner=runner)
        self._orchestrator = ContainerOrchestrator(resolved_runner, self.workspace_root)

    def run_node_container(
        self,
        *,
        run_id: str,
        node_id: str,
        workspace_root: Path,
    ) -> dict[str, Any]:
        mount_root = workspace_root.resolve()
        diag_dir = mount_root / "diagnostics"

        request = ContainerRequest(
            name=f"node-agent-{run_id}",
            image="bridle-node-agent:local",
            network_mode="none",
            mounts=[
                ContainerMount(
                    source=mount_root,
                    target="/container",
                    readonly=False,
                )
            ],
            environment={
                "BRIDLE_RUN_ID": run_id,
                "BRIDLE_NODE_ID": node_id,
            },
            command=["bridle-node-agent"],
            role="node",
            allowed_mount_roots=[str(mount_root)],
        )

        try:
            result = self._orchestrator.run_and_wait(request, diag_dir=diag_dir)
        except _OrchestrationError as exc:
            original_error = exc.detail.get("error", "")
            message = f"{exc.error_code}: {original_error}" if original_error else exc.error_code
            logger.info(
                "node_container_failed",
                extra={
                    "action": "node_container_failed",
                    "status": "failed",
                    "detail": {"run_id": run_id, "node_id": node_id, "error_code": exc.error_code},
                },
            )
            raise NodeContainerError(
                exc.error_code,
                message=message,
                detail={**exc.detail, "run_id": run_id, "node_id": node_id, "container_id": exc.container_id},
            ) from exc

        logger.info(
            "node_container_started",
            extra={
                "action": "node_container_started",
                "status": "completed",
                "detail": {
                    "run_id": run_id,
                    "node_id": node_id,
                    "container_id": result.container_id,
                    "health": result.health,
                },
            },
        )
        return {
            "container_id": result.container_id,
            "container_status": result.status,
            "container_health": result.health,
            "container_error": None,
            "exit_code": result.exit_code,
            "logs_summary": result.logs_summary[:500],
            "diagnostic_path": str(diag_dir),
        }

    def cleanup_container(self, container_id: str) -> None:
        self._orchestrator.cleanup(container_id)
        logger.info(
            "node_container_cleaned",
            extra={
                "action": "node_container_cleaned",
                "status": "completed",
                "detail": {"container_id": container_id},
            },
        )
