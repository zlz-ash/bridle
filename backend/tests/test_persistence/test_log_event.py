"""Tests for LogEventRecord persistence."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.log_event import LogEventRecord
from bridle.models.node import NodeRecord
from bridle.models.plan import PlanRecord
from bridle.models.run import RunRecord
from bridle.models.task import TaskRecord


class TestLogEventPersistence:
    async def test_create_log_event(self, db: AsyncSession) -> None:
        log = LogEventRecord(action="task.create", status="success")
        db.add(log)
        await db.commit()

        assert log.id is not None
        assert log.action == "task.create"
        assert log.status == "success"
        assert log.created_at is not None

    async def test_log_event_with_all_ids(self, db: AsyncSession) -> None:
        task = TaskRecord(title="T", status="running")
        db.add(task)
        await db.flush()
        plan = PlanRecord(task_id=task.id, goal="G", status="active")
        db.add(plan)
        await db.flush()
        node = NodeRecord(
            plan_id=plan.id, title="N", goal="G", node_type="code_change", order=1,
            depends_on=[], files=[], tests=[], metrics={}, constraints=[],
            review_checks=[], expected_outputs={}, status="running",
        )
        db.add(node)
        await db.flush()
        from datetime import datetime

        run = RunRecord(node_id=node.id, status="started", started_at=datetime.now())
        db.add(run)
        await db.flush()

        log = LogEventRecord(
            task_id=task.id, node_id=node.id, run_id=run.id,
            action="node.run", status="started", duration_ms=0,
            detail={"command": "pytest"},
        )
        db.add(log)
        await db.commit()

        assert log.task_id == task.id
        assert log.node_id == node.id
        assert log.run_id == run.id
        assert log.duration_ms == 0
        assert log.detail == {"command": "pytest"}

    async def test_log_event_optional_fields(self, db: AsyncSession) -> None:
        log = LogEventRecord(action="app.start", status="ok")
        db.add(log)
        await db.commit()

        assert log.task_id is None
        assert log.node_id is None
        assert log.run_id is None
        assert log.duration_ms is None
        assert log.detail is None

    async def test_log_event_required_fields(self, db: AsyncSession) -> None:
        """action and status are required."""
        log = LogEventRecord(action="test.run", status="completed", duration_ms=500)
        db.add(log)
        await db.commit()

        result = await db.execute(select(LogEventRecord).where(LogEventRecord.id == log.id))
        fetched = result.scalar_one()
        assert fetched.action == "test.run"
        assert fetched.status == "completed"
        assert fetched.duration_ms == 500
