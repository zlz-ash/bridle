"""Health check service."""
from __future__ import annotations

import time

import bridle
from bridle.config import get_config
from bridle.events.bus import EventBus
from bridle.schemas.health import HealthResponseSchema
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class HealthService:
    @staticmethod
    async def check(db: AsyncSession, *, started_at: float) -> HealthResponseSchema:
        db_status = "ok"
        overall = "ok"
        try:
            await db.execute(text("SELECT 1"))
        except Exception as exc:
            db_status = f"error: {type(exc).__name__}"
            overall = "degraded"

        uptime = max(0, int(time.time() - started_at))
        return HealthResponseSchema(
            status=overall,
            version=bridle.__version__,
            workspace=str(get_config().workspace.resolve()),
            db=db_status,
            uptime_seconds=uptime,
            events_subscribers=EventBus.instance().subscriber_count(),
        )
