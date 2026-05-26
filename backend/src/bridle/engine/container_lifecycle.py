"""Shared container stop/remove helpers for orchestration services."""
from __future__ import annotations

import logging
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bridle.engine.container_runner import ContainerRunner, LocalContainerRuntimeRunner

logger = logging.getLogger("bridle")


def cleanup_container(runner: "ContainerRunner", container_id: str) -> None:
    """Stop and remove a container; errors are swallowed after logging."""
    if not container_id:
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
    from bridle.engine.container_runner import LocalContainerRuntimeRunner

    if isinstance(runner, LocalContainerRuntimeRunner) and runner.use_docker:
        try:
            runner._run_command([runner.executable, "rm", "-f", container_id])
        except Exception as exc:
            logger.info(
                "container_remove_failed",
                extra={
                    "action": "container_remove_failed",
                    "status": "failed",
                    "detail": {"container_id": container_id, "error": str(exc)},
                },
            )
