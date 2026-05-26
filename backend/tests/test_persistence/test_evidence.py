"""Tests for EvidenceRecord persistence."""
from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.evidence import EvidenceRecord
from bridle.models.node import NodeRecord
from bridle.models.plan import PlanRecord
from bridle.models.run import RunRecord
from bridle.models.task import TaskRecord


@pytest_asyncio.fixture
async def evidence(db: AsyncSession) -> EvidenceRecord:
    task = TaskRecord(title="Evidence Test", status="running")
    db.add(task)
    await db.flush()
    plan = PlanRecord(task_id=task.id, goal="G", status="active")
    db.add(plan)
    await db.flush()
    node = NodeRecord(
        plan_id=plan.id, title="N", goal="G", node_type="test_validation", order=1,
        depends_on=[], files=[], tests=[], metrics={}, constraints=[],
        review_checks=[], expected_outputs={}, status="running",
    )
    db.add(node)
    await db.flush()
    run = RunRecord(node_id=node.id, status="completed", started_at=datetime.now())
    db.add(run)
    await db.flush()

    ev = EvidenceRecord(
        run_id=run.id,
        node_id=node.id,
        evidence_type="test_result",
        content={"passed": 10, "failed": 0, "total": 10},
        status="collected",
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    return ev


class TestEvidencePersistence:
    async def test_create_evidence(self, evidence: EvidenceRecord) -> None:
        assert evidence.id is not None
        assert evidence.evidence_type == "test_result"
        assert evidence.content["passed"] == 10
        assert evidence.status == "collected"

    async def test_evidence_types(self, db: AsyncSession) -> None:
        """Multiple evidence types should be persistable."""
        task = TaskRecord(title="T", status="running")
        db.add(task)
        await db.flush()
        plan = PlanRecord(task_id=task.id, goal="G", status="active")
        db.add(plan)
        await db.flush()
        node = NodeRecord(
            plan_id=plan.id, title="N", goal="G", node_type="metric_validation", order=1,
            depends_on=[], files=[], tests=[], metrics={}, constraints=[],
            review_checks=[], expected_outputs={}, status="running",
        )
        db.add(node)
        await db.flush()
        run = RunRecord(node_id=node.id, status="completed", started_at=datetime.now())
        db.add(run)
        await db.flush()

        for etype in ["test_result", "metric", "log", "artifact"]:
            ev = EvidenceRecord(
                run_id=run.id, node_id=node.id, evidence_type=etype,
                content={"type": etype}, status="collected",
            )
            db.add(ev)

        await db.commit()
        result = await db.execute(select(EvidenceRecord).where(EvidenceRecord.run_id == run.id))
        assert len(result.scalars().all()) == 4

    async def test_missing_evidence_status(self, db: AsyncSession) -> None:
        task = TaskRecord(title="T", status="running")
        db.add(task)
        await db.flush()
        plan = PlanRecord(task_id=task.id, goal="G", status="active")
        db.add(plan)
        await db.flush()
        node = NodeRecord(
            plan_id=plan.id, title="N", goal="G", node_type="review_gate", order=1,
            depends_on=[], files=[], tests=[], metrics={}, constraints=[],
            review_checks=[], expected_outputs={}, status="running",
        )
        db.add(node)
        await db.flush()
        run = RunRecord(node_id=node.id, status="completed", started_at=datetime.now())
        db.add(run)
        await db.flush()

        ev = EvidenceRecord(
            run_id=run.id, node_id=node.id, evidence_type="artifact",
            content={}, status="missing_evidence",
        )
        db.add(ev)
        await db.commit()

        assert ev.status == "missing_evidence"

    async def test_evidence_run_relationship(self, db: AsyncSession, evidence: EvidenceRecord) -> None:
        await db.refresh(evidence, ["run"])
        assert evidence.run is not None

    async def test_delete_evidence(self, db: AsyncSession, evidence: EvidenceRecord) -> None:
        ev_id = evidence.id
        await db.delete(evidence)
        await db.commit()

        result = await db.execute(select(EvidenceRecord).where(EvidenceRecord.id == ev_id))
        assert result.scalar_one_or_none() is None
