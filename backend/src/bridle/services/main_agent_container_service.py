"""Persist main-agent container metadata per coding session."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bridle.engine.container_orchestrator import ContainerOrchestrator, _OrchestrationError
from bridle.engine.container_runner import ContainerMount, ContainerRequest, ContainerRunner
from bridle.engine.container_runner_factory import resolve_container_runner
from bridle.engine.git_workspace_policy import GitWorkspacePolicy

logger = logging.getLogger("bridle")


class MainAgentContainerService:
    def __init__(
        self,
        workspace_root: str | Path,
        *,
        runner: ContainerRunner | None = None,
        git_policy: GitWorkspacePolicy | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        resolved_runner = resolve_container_runner(workspace_root, runner=runner)
        self.runner = resolved_runner
        self._orchestrator = ContainerOrchestrator(resolved_runner, self.workspace_root)
        self.git_policy = git_policy or GitWorkspacePolicy()

    def record_for_session(self, *, session_id: str, plan_id: str) -> dict[str, Any]:
        preflight = self.git_policy.evaluate(self.workspace_root)
        if not preflight.ok:
            logger.info(
                "main_agent_git_preflight_failed",
                extra={
                    "action": "main_agent_git_preflight_failed",
                    "status": "rejected",
                    "detail": {"session_id": session_id, "error_code": preflight.error_code},
                },
            )
            raise ValueError(preflight.error_code or "git_preflight_failed")

        request = ContainerRequest(
            name=f"main-agent-{session_id}",
            image="bridle-main-agent:local",
            network_mode="bridge",
            mounts=[
                ContainerMount(
                    source=self.workspace_root,
                    target="/workspace",
                    readonly=False,
                )
            ],
            environment={
                "BRIDLE_SESSION_ID": session_id,
                "BRIDLE_PLAN_ID": plan_id,
            },
            command=["bridle-main-agent"],
            role="main",
        )
        diag_dir = self._diagnostic_dir(session_id)

        try:
            result = self._orchestrator.start_detached(request, diag_dir=diag_dir)
        except _OrchestrationError as exc:
            error_code = str(exc).split(":")[0] if ":" in str(exc) else exc.error_code
            logger.info(
                "main_agent_container_startup_failed",
                extra={
                    "action": "main_agent_container_startup_failed",
                    "status": "failed",
                    "detail": {"session_id": session_id, "error_code": error_code},
                },
            )
            raise ValueError(error_code) from exc

        metadata: dict[str, Any] = {
            "session_id": session_id,
            "plan_id": plan_id,
            "container_id": result.container_id,
            "status": result.status,
            "network_mode": result.network_mode,
            "health": result.health,
            "baseline_revision": preflight.baseline_revision,
            "git_baseline_revision": preflight.baseline_revision,
            "workspace_path": str(self.workspace_root),
            "created_at": datetime.now(UTC).isoformat(),
            "logs": result.logs,
            "logs_summary": result.logs_summary,
            "diagnostic_path": str(diag_dir),
            "error_code": None,
        }
        self._metadata_path(session_id).write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "main_agent_container_recorded",
            extra={
                "action": "main_agent_container_recorded",
                "status": "completed",
                "detail": {
                    "session_id": session_id,
                    "plan_id": plan_id,
                    "container_id": result.container_id,
                },
            },
        )
        return metadata

    def read_for_session(self, session_id: str) -> dict[str, Any] | None:
        path = self._metadata_path(session_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _metadata_path(self, session_id: str) -> Path:
        root = self.workspace_root / ".aicoding" / "main-agent-containers"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{session_id}.json"

    def _diagnostic_dir(self, session_id: str) -> Path:
        path = self.workspace_root / ".aicoding" / "main-agent-containers" / session_id / "diagnostics"
        path.mkdir(parents=True, exist_ok=True)
        return path
