"""Reports API router."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.api.errors import NotFoundError
from bridle.services.report_service import ReportService

router = APIRouter(tags=["reports"])


@router.get("/nodes/{node_id}/report")
async def get_node_report(node_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    report = await ReportService.generate_node_report(db, node_id)
    if report is None:
        raise NotFoundError(resource="node", message="Node not found")
    return report
