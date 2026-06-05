"""Health check API."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.services.health_service import HealthService

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    started_at = getattr(request.app.state, "started_at", time.time())
    result = await HealthService.check(db, started_at=started_at)
    return result.model_dump()
