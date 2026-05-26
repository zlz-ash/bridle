"""Node status contract tests."""
from __future__ import annotations

from datetime import datetime

import pytest

from typing import get_args

from bridle.schemas.node import NodeReadSchema, NodeStatusLiteral
from bridle.services.node_service import NodeService, ALLOWED_NODE_STATUSES


def test_node_status_literal_includes_needs_review() -> None:
    assert "needs_review" in get_args(NodeStatusLiteral)


def test_node_read_schema_accepts_needs_review() -> None:
    now = datetime.utcnow()
    data = NodeReadSchema(
        id="n",
        plan_id="p",
        plan_node_id="n1",
        title="t",
        goal="g",
        node_type="code_change",
        order=0,
        depends_on=[],
        files=[],
        tests=[],
        metrics={},
        constraints={},
        review_checks=[],
        expected_outputs={},
        interfaces={},
        status="needs_review",
        created_at=now,
        updated_at=now,
    )
    assert data.status == "needs_review"


@pytest.mark.asyncio
async def test_update_status_rejects_unknown(db) -> None:
    from bridle.models.node import NodeRecord
    from bridle.models.plan import PlanRecord
    from bridle.models.task import TaskRecord

    task = TaskRecord(title="T")
    db.add(task)
    await db.flush()
    plan = PlanRecord(task_id=task.id, goal="G", status="active")
    db.add(plan)
    await db.flush()
    node = NodeRecord(
        plan_id=plan.id,
        plan_node_id="n1",
        title="N",
        goal="G",
        node_type="code_change",
        depends_on=[],
        files=[],
        tests=["pytest"],
        constraints={"x": True},
        status="pending",
    )
    db.add(node)
    await db.commit()

    with pytest.raises(ValueError, match="Unknown node status"):
        await NodeService.update_status(db, node.id, "not_a_real_status")

    assert "pending" in ALLOWED_NODE_STATUSES
