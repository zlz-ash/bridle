"""Tests for NodeRecord persistence."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node import NodeRecord
from bridle.models.plan import PlanRecord
from bridle.models.task import TaskRecord


@pytest_asyncio.fixture
async def node(db: AsyncSession) -> NodeRecord:
    task = TaskRecord(title="Node Test Task", status="planned")
    db.add(task)
    await db.flush()

    plan = PlanRecord(task_id=task.id, goal="Node test plan", status="active")
    db.add(plan)
    await db.flush()

    node = NodeRecord(
        plan_id=plan.id,
        title="Test Node",
        goal="Test goal",
        node_type="code_change",
        order=1,
        depends_on=[],
        files=["src/main.py"],
        tests=["pytest tests/"],
        metrics={"coverage": 80},
        constraints={"no_print": True},
        review_checks=["no hardcoded secrets"],
        expected_outputs={"exit_code": 0},
        status="pending",
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return node


class TestNodePersistence:
    async def test_create_node(self, node: NodeRecord) -> None:
        assert node.id is not None
        assert node.title == "Test Node"
        assert node.node_type == "code_change"
        assert node.order == 1

    async def test_node_json_fields(self, node: NodeRecord) -> None:
        assert node.depends_on == []
        assert node.files == ["src/main.py"]
        assert node.tests == ["pytest tests/"]
        assert node.metrics == {"coverage": 80}
        assert node.constraints == {"no_print": True}
        assert node.review_checks == ["no hardcoded secrets"]
        assert node.expected_outputs == {"exit_code": 0}

    async def test_node_default_status(self, db: AsyncSession) -> None:
        task = TaskRecord(title="T", status="created")
        db.add(task)
        await db.flush()
        plan = PlanRecord(task_id=task.id, goal="G", status="draft")
        db.add(plan)
        await db.flush()

        node = NodeRecord(
            plan_id=plan.id, title="N", goal="G", node_type="test_validation", order=0,
            depends_on=[], files=[], tests=[], metrics={}, constraints=[],
            review_checks=[], expected_outputs={},
        )
        db.add(node)
        await db.commit()

        assert node.status == "pending"

    async def test_node_depends_on(self, db: AsyncSession) -> None:
        task = TaskRecord(title="T", status="created")
        db.add(task)
        await db.flush()
        plan = PlanRecord(task_id=task.id, goal="G", status="active")
        db.add(plan)
        await db.flush()

        node_a = NodeRecord(
            plan_id=plan.id, title="A", goal="G", node_type="code_change", order=1,
            depends_on=[], files=[], tests=[], metrics={}, constraints=[],
            review_checks=[], expected_outputs={}, status="completed",
        )
        db.add(node_a)
        await db.flush()

        node_b = NodeRecord(
            plan_id=plan.id, title="B", goal="G", node_type="test_validation", order=2,
            depends_on=[node_a.id], files=[], tests=[], metrics={}, constraints=[],
            review_checks=[], expected_outputs={}, status="pending",
        )
        db.add(node_b)
        await db.commit()
        await db.refresh(node_b)

        assert node_b.depends_on == [node_a.id]

    async def test_node_all_types(self, db: AsyncSession) -> None:
        """All 4 node types should be persistable."""
        task = TaskRecord(title="T", status="created")
        db.add(task)
        await db.flush()
        plan = PlanRecord(task_id=task.id, goal="G", status="active")
        db.add(plan)
        await db.flush()

        for i, ntype in enumerate(["code_change", "test_validation", "metric_validation", "review_gate"]):
            node = NodeRecord(
                plan_id=plan.id, title=f"Node-{ntype}", goal="G", node_type=ntype, order=i,
                depends_on=[], files=[], tests=[], metrics={}, constraints=[],
                review_checks=[], expected_outputs={}, status="pending",
            )
            db.add(node)

        await db.commit()

        result = await db.execute(select(NodeRecord).where(NodeRecord.plan_id == plan.id))
        nodes = result.scalars().all()
        assert len(nodes) == 4

    async def test_delete_node(self, db: AsyncSession, node: NodeRecord) -> None:
        node_id = node.id
        await db.delete(node)
        await db.commit()

        result = await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))
        assert result.scalar_one_or_none() is None
