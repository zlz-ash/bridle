"""Run service — business logic for run records."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node import NodeRecord
from bridle.models.run import RunRecord
from bridle.schemas.run import RunReadSchema


class RunService:
    @staticmethod
    async def create(db: AsyncSession, node_id: str) -> RunReadSchema:
        run = RunRecord(node_id=node_id, status="started", started_at=datetime.now())
        db.add(run)
        await db.commit()
        await db.refresh(run)
        return RunReadSchema.model_validate(run)

    @staticmethod
    async def complete(
        db: AsyncSession,
        run_id: str,
        exit_code: int,
        duration_ms: int,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
    ) -> RunReadSchema | None:
        result = await db.execute(select(RunRecord).where(RunRecord.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            return None
        run.status = "completed" if exit_code == 0 else "failed"
        run.exit_code = exit_code
        run.finished_at = datetime.now()
        run.duration_ms = duration_ms
        run.stdout_path = stdout_path
        run.stderr_path = stderr_path
        await db.commit()
        await db.refresh(run)
        return RunReadSchema.model_validate(run)

    @staticmethod
    async def list_by_node(db: AsyncSession, node_id: str) -> list[RunReadSchema]:
        result = await db.execute(
            select(RunRecord).where(RunRecord.node_id == node_id).order_by(RunRecord.started_at.desc())
        )
        return [RunReadSchema.model_validate(r) for r in result.scalars().all()]

    @staticmethod
    async def get_by_id(db: AsyncSession, run_id: str) -> RunReadSchema | None:
        result = await db.execute(select(RunRecord).where(RunRecord.id == run_id))
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return RunReadSchema.model_validate(record)
