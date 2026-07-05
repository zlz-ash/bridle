"""Project-local plan mutation service."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from bridle.features.project_map.patch_schemas import PlanPatchSchema
from bridle.features.project_map.service import ProjectMapService


class PlanService:
    """Expose the existing plan PATCH name over the project-local SQLite store."""

    @staticmethod
    async def patch_current(db: AsyncSession, project_id: str, data: PlanPatchSchema) -> dict:
        """Apply a local project patch; DB/project/schema inputs exit as bounded change metadata."""
        store = await ProjectMapService.store_for(db, project_id)
        return store.patch(data)

