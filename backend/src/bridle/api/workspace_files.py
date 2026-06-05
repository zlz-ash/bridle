"""Workspace file read API."""
from __future__ import annotations

from fastapi import APIRouter, Query

from bridle.config import get_config
from bridle.services.workspace_file_service import WorkspaceFileService

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("/files")
async def read_workspace_file(path: str = Query(..., min_length=1)) -> dict:
    result = WorkspaceFileService.read_text(get_config().workspace, path)
    return result.model_dump()


@router.get("/overview")
async def workspace_overview() -> dict:
    from bridle.services.workspace_overview_service import WorkspaceOverviewService

    return WorkspaceOverviewService.summarize(get_config().workspace)
