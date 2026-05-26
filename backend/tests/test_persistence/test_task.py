"""Tests for TaskRecord persistence."""
from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bridle.database import configure_sqlite_engine
from bridle.models.base import Base
from bridle.models.task import TaskRecord


@pytest_asyncio.fixture
async def task(db: AsyncSession) -> TaskRecord:
    """Create and return a sample TaskRecord."""
    record = TaskRecord(title="Test Task", goal="Test goal", status="created")
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


class TestTaskPersistence:
    async def test_create_task(self, db: AsyncSession) -> None:
        task = TaskRecord(title="My Task", goal="Do something", status="created")
        db.add(task)
        await db.commit()

        assert task.id is not None
        assert task.title == "My Task"
        assert task.status == "created"
        assert task.created_at is not None
        assert task.updated_at is not None

    async def test_read_task(self, db: AsyncSession, task: TaskRecord) -> None:
        result = await db.execute(select(TaskRecord).where(TaskRecord.id == task.id))
        fetched = result.scalar_one()

        assert fetched.id == task.id
        assert fetched.title == "Test Task"

    async def test_update_task_status(self, db: AsyncSession, task: TaskRecord) -> None:
        task.status = "planned"
        await db.commit()
        await db.refresh(task)

        assert task.status == "planned"

    async def test_delete_task(self, db: AsyncSession, task: TaskRecord) -> None:
        task_id = task.id
        await db.delete(task)
        await db.commit()

        result = await db.execute(select(TaskRecord).where(TaskRecord.id == task_id))
        assert result.scalar_one_or_none() is None

    async def test_task_has_uuid_id(self, db: AsyncSession) -> None:
        task = TaskRecord(title="UUID Test", status="created")
        db.add(task)
        await db.commit()

        # UUID format: 8-4-4-4-12 hex chars
        parts = task.id.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8

    async def test_task_default_status(self, db: AsyncSession) -> None:
        task = TaskRecord(title="Default Status")
        db.add(task)
        await db.commit()

        assert task.status == "created"

    async def test_restart_recovery(self, recovery_db_path: Path) -> None:
        """Data persists after creating a new session on the same engine.

        Uses a file-based SQLite under the test workspace so that data
        survives across sessions.
        """
        db_path = recovery_db_path
        recovery_engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path.as_posix()}",
            echo=False,
        )
        configure_sqlite_engine(recovery_engine)
        async with recovery_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(recovery_engine, class_=AsyncSession, expire_on_commit=False)

        async with factory() as s1:
            task = TaskRecord(title="Recovery Test", goal="Survives restart", status="running")
            s1.add(task)
            await s1.commit()
            task_id = task.id

        async with factory() as s2:
            result = await s2.execute(select(TaskRecord).where(TaskRecord.id == task_id))
            fetched = result.scalar_one()

            assert fetched.title == "Recovery Test"
            assert fetched.status == "running"

        await recovery_engine.dispose()
