"""Tests for PlanRecord persistence."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.plan import PlanRecord
from bridle.models.task import TaskRecord


@pytest_asyncio.fixture
async def task_with_plan(db: AsyncSession) -> tuple[TaskRecord, PlanRecord]:
    task = TaskRecord(title="Plan Test Task", status="planned")
    db.add(task)
    await db.flush()

    plan = PlanRecord(task_id=task.id, goal="Test plan goal", status="draft")
    db.add(plan)
    await db.commit()
    await db.refresh(task)
    await db.refresh(plan)
    return task, plan


class TestPlanPersistence:
    async def test_create_plan(self, db: AsyncSession) -> None:
        task = TaskRecord(title="T", status="created")
        db.add(task)
        await db.flush()

        plan = PlanRecord(task_id=task.id, goal="My Plan", status="draft")
        db.add(plan)
        await db.commit()

        assert plan.id is not None
        assert plan.task_id == task.id
        assert plan.goal == "My Plan"

    async def test_plan_task_relationship(self, db: AsyncSession, task_with_plan: tuple) -> None:
        task, plan = task_with_plan
        await db.refresh(task, ["plan"])

        assert task.plan is not None
        assert task.plan.id == plan.id

    async def test_plan_default_status(self, db: AsyncSession) -> None:
        task = TaskRecord(title="T", status="created")
        db.add(task)
        await db.flush()

        plan = PlanRecord(task_id=task.id, goal="G")
        db.add(plan)
        await db.commit()

        assert plan.status == "draft"

    async def test_delete_plan(self, db: AsyncSession, task_with_plan: tuple) -> None:
        _, plan = task_with_plan
        plan_id = plan.id
        await db.delete(plan)
        await db.commit()

        result = await db.execute(select(PlanRecord).where(PlanRecord.id == plan_id))
        assert result.scalar_one_or_none() is None
