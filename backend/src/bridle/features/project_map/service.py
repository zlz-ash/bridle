"""Resolve registered projects to their local plan stores."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.errors import ConflictError
from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.projects.service import ProjectService
from bridle.features.workspace.overview_service import WorkspaceOverviewService
from bridle.logging.facade import LoggingFacade, get_logging_facade


class ProjectMapService:
    """Bridge global project records to local SQLite maps; project input exits as a validated store."""

    @staticmethod
    async def store_for(db: AsyncSession, project_id: str) -> ProjectPlanStore:
        """Resolve an available project; DB/ID input returns its initialized local map store."""
        project = await ProjectService.get_record(db, project_id)
        root = Path(project.path)
        if not root.is_dir():
            raise ConflictError(
                resource="project",
                message="Project path is unavailable",
                error_code="project_unavailable_read_only",
            )
        store = ProjectPlanStore(root, project_id=project.id)
        if not store.database_path.is_file():
            store.initialize()
        else:
            store.ensure_schema()
        return store


class ControlService:
    """Shared synchronous application-service facade used by CLI, API, and tools."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        facade: LoggingFacade | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._facade = facade or get_logging_facade()

    def invoke(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Invoke one existing application service and return a stable control result."""
        started = time.perf_counter()
        self._facade.info_event(
            "control_service_invoke",
            "started",
            detail={"operation": operation, "workspace": str(self.workspace)},
        )
        try:
            data = self._dispatch(operation, payload)
        except Exception as exc:
            result = {
                "status": "failed",
                "operation": operation,
                "error_code": getattr(exc, "error_code", type(exc).__name__),
                "message": str(exc),
                "changed_ids": [],
                "artifact_ref": None,
            }
            self._facade.error_event(
                "control_service_invoke",
                "failed",
                duration_ms=int((time.perf_counter() - started) * 1000),
                detail={"operation": operation, "error_code": result["error_code"]},
            )
            return result
        result = {
            "status": "completed",
            "operation": operation,
            "data": data,
            "changed_ids": [],
            "artifact_ref": None,
            "error_code": None,
        }
        self._facade.info_event(
            "control_service_invoke",
            "completed",
            duration_ms=int((time.perf_counter() - started) * 1000),
            detail={"operation": operation},
        )
        return result

    def _dispatch(self, operation: str, payload: dict[str, Any]) -> Any:
        if operation.startswith("code."):
            return self._invoke_code(operation.removeprefix("code."), payload)
        if operation == "plan":
            return self._invoke_plan(payload)
        if operation == "candidate":
            return self._invoke_candidate(payload)
        if operation == "verify":
            store = ProjectPlanStore.open_existing(self.workspace)
            node_id = str(payload.get("node_id") or "")
            if payload.get("view") == "evidence":
                return store.validate_evidence_chain(node_id)
            return store.latest_verification_run(node_id)
        if operation == "agent":
            wait_id = str(payload.get("wait_id") or "")
            if wait_id:
                return ProjectPlanStore.open_existing(self.workspace).read_execution(wait_id)
            trace_id = str(payload.get("trace_id") or "")
            return ProjectPlanStore.open_existing(self.workspace).list_stage_events(trace_id)
        raise ValueError(f"unsupported_control_operation:{operation}")

    def _invoke_code(self, operation: str, payload: dict[str, Any]) -> Any:
        limit = max(1, min(int(payload.get("limit", 20)), 200))
        if operation == "inspect":
            path = payload.get("path")
            if path:
                return WorkspaceOverviewService.scan_paths(self.workspace, [str(path)])
            return WorkspaceOverviewService.summarize(self.workspace, max_files=limit)
        if operation == "search":
            query = str(payload.get("query") or "").casefold()
            entities = WorkspaceOverviewService.scan_entities(self.workspace)
            matches = [
                item
                for item in entities
                if query in json.dumps(item, ensure_ascii=False, default=str).casefold()
            ]
            return {"items": matches[:limit], "total_matches": len(matches)}
        if operation == "graph":
            store = ProjectPlanStore.open_existing(self.workspace)
            return store.map_subgraph(
                str(payload.get("entity_id") or ""),
                depth=int(payload.get("depth", 1)),
                max_nodes=limit,
            )
        raise ValueError(f"unsupported_code_operation:{operation}")

    def _invoke_plan(self, payload: dict[str, Any]) -> Any:
        store = ProjectPlanStore.open_existing(self.workspace)
        mode = str(payload.get("mode") or "overview")
        if mode == "overview":
            return store.overview()
        if mode == "node":
            return store.get_node(str(payload.get("node_id") or ""))
        if mode == "search":
            return store.search(
                str(payload.get("query") or ""),
                cursor=payload.get("cursor"),
                limit=int(payload.get("limit", 20)),
            )
        raise ValueError(f"unsupported_plan_mode:{mode}")

    def _invoke_candidate(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        candidate_id = str(payload.get("candidate_id") or "")
        if not candidate_id:
            raise ValueError("candidate_id_required")
        directory = self.workspace / ".bridle" / "candidate-submissions" / candidate_id
        records = sorted(directory.glob("*.json")) if directory.is_dir() else []
        if not records:
            return None
        requested = str(payload.get("submission_id") or "")
        selected = records[-1]
        if requested:
            selected = next(
                (
                    path
                    for path in records
                    if json.loads(path.read_text(encoding="utf-8")).get("submission_id")
                    == requested
                ),
                selected,
            )
        raw = json.loads(selected.read_text(encoding="utf-8"))
        allowed = {
            "submission_id",
            "candidate_id",
            "node_id",
            "revision",
            "baseline_tree_hash",
            "candidate_tree_hash",
            "changed_paths",
            "created_at",
        }
        return {key: raw[key] for key in allowed if key in raw}

