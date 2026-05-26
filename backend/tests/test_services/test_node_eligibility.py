"""Unit tests for NodeEligibilityService."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node import NodeRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.models.plan import PlanRecord
from bridle.models.task import TaskRecord
from bridle.services.node_eligibility import NodeEligibilityService


@pytest.mark.asyncio
async def test_completed_dependency_makes_second_node_eligible(db: AsyncSession) -> None:
    task = TaskRecord(title="T")
    db.add(task)
    await db.flush()
    plan = PlanRecord(task_id=task.id, goal="G", status="active")
    db.add(plan)
    await db.flush()
    n1 = NodeRecord(
        plan_id=plan.id,
        plan_node_id="n1",
        title="N1",
        goal="G1",
        node_type="code_change",
        depends_on=[],
        files=["a.py"],
        tests=["pytest"],
        constraints={"x": True},
        status="completed",
    )
    n2 = NodeRecord(
        plan_id=plan.id,
        plan_node_id="n2",
        title="N2",
        goal="G2",
        node_type="code_change",
        depends_on=["n1"],
        files=["b.py"],
        tests=["pytest"],
        constraints={"x": True},
        status="pending",
    )
    db.add_all([n1, n2])
    await db.commit()

    eligible, blocked = await NodeEligibilityService.compute(db, plan.id)
    assert {e.plan_node_id for e in eligible} == {"n2"}
    assert {b.plan_node_id for b in blocked} == set()


@pytest.mark.asyncio
async def test_active_node_agent_run_blocks_eligibility(db: AsyncSession) -> None:
    task = TaskRecord(title="T2")
    db.add(task)
    await db.flush()
    plan = PlanRecord(task_id=task.id, goal="G", status="active")
    db.add(plan)
    await db.flush()
    session_id = "00000000-0000-0000-0000-000000000099"
    from bridle.models.agent_coding_session import AgentCodingSessionRecord

    sess = AgentCodingSessionRecord(id=session_id, plan_id=plan.id, status="active")
    node = NodeRecord(
        plan_id=plan.id,
        plan_node_id="n1",
        title="N1",
        goal="G1",
        node_type="code_change",
        depends_on=[],
        files=["a.py"],
        tests=["pytest"],
        constraints={"x": True},
        status="pending",
    )
    db.add_all([sess, node])
    await db.flush()
    run = NodeAgentRunRecord(session_id=session_id, node_id=node.id, plan_node_id="n1", status="running")
    db.add(run)
    await db.commit()

    eligible, blocked = await NodeEligibilityService.compute(db, plan.id)
    assert eligible == []
    assert any(b.reason == "node_already_running" for b in blocked)
