"""Project selection and registry API."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.features.projects.schemas import ProjectListSchema, ProjectOpenSchema
from bridle.features.projects.service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("")
async def list_projects(db: AsyncSession = Depends(get_db)) -> dict:
    """List project history; DB dependency input exits as a projects envelope."""
    projects = await ProjectService.list_projects(db)
    return ProjectListSchema(projects=projects).model_dump(mode="json")


@router.post("/open")
async def open_project(data: ProjectOpenSchema, db: AsyncSession = Depends(get_db)) -> dict:
    """Open a selected directory; request path input exits as registered project/map state."""
    project = await ProjectService.open_project(db, data.path)
    return project.model_dump(mode="json")


@router.post("/{project_id}/rescan")
async def rescan_project(project_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Rescan a selected project; project ID input exits as refreshed code-map state."""
    return await ProjectService.rescan_project(db, project_id)

