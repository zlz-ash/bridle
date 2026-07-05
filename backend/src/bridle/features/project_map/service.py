"""Resolve registered projects to their local plan stores."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.errors import ConflictError
from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.projects.service import ProjectService


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
        return store

