"""Tests for RunRecord persistence."""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node import NodeRecord
from bridle.models.plan import PlanRecord
from bridle.models.run import RunRecord
from bridle.models.task import TaskRecord


@pytest_asyncio.fixture
async def run(db: AsyncSession) -> RunRecord:
    task = TaskRecord(title="Run Test", status="running")
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

    run = RunRecord(node_id=node.id, status="started", started_at=datetime.now())
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


class TestRunPersistence:
    async def test_create_run(self, run: RunRecord) -> None:
        assert run.id is not None
        assert run.status == "started"
        assert run.started_at is not None

    async def test_update_run_completion(self, db: AsyncSession, run: RunRecord) -> None:
        run.status = "completed"
        run.exit_code = 0
        run.finished_at = datetime.now()
        run.duration_ms = 1234
        await db.commit()
        await db.refresh(run)

        assert run.status == "completed"
        assert run.exit_code == 0
        assert run.duration_ms == 1234

    async def test_run_with_stderr_path(self, db: AsyncSession, run: RunRecord) -> None:
        run.stdout_path = ".aicoding/runs/test-run/stdout.log"
        run.stderr_path = ".aicoding/runs/test-run/stderr.log"
        await db.commit()
        await db.refresh(run)

        assert run.stdout_path is not None
        assert run.stderr_path is not None

    async def test_run_node_relationship(self, db: AsyncSession, run: RunRecord) -> None:
        await db.refresh(run, ["node"])
        assert run.node is not None

    async def test_delete_run(self, db: AsyncSession, run: RunRecord) -> None:
        run_id = run.id
        await db.delete(run)
        await db.commit()

        result = await db.execute(select(RunRecord).where(RunRecord.id == run_id))
        assert result.scalar_one_or_none() is None
