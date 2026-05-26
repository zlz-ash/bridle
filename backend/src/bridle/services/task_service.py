"""Task service — business logic for TaskRecord CRUD."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.task import TaskRecord
from bridle.schemas.task import TaskCreateSchema, TaskReadSchema


class TaskService:
    @staticmethod
    async def create(db: AsyncSession, data: TaskCreateSchema) -> TaskReadSchema:
        record = TaskRecord(**data.model_dump())
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return TaskReadSchema.model_validate(record)

    @staticmethod
    async def get_by_id(db: AsyncSession, task_id: str) -> TaskReadSchema | None:
        result = await db.execute(select(TaskRecord).where(TaskRecord.id == task_id))
        record = result.scalar_one_or_none()
        if record is None:
            return None
        return TaskReadSchema.model_validate(record)

    @staticmethod
    async def list_all(db: AsyncSession) -> list[TaskReadSchema]:
        result = await db.execute(select(TaskRecord).order_by(TaskRecord.created_at.desc()))
        return [TaskReadSchema.model_validate(r) for r in result.scalars().all()]

    @staticmethod
    async def update_status(db: AsyncSession, task_id: str, status: str) -> TaskReadSchema | None:
        result = await db.execute(select(TaskRecord).where(TaskRecord.id == task_id))
        record = result.scalar_one_or_none()
        if record is None:
            return None
        record.status = status
        await db.commit()
        await db.refresh(record)
        return TaskReadSchema.model_validate(record)
