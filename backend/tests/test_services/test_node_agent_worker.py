"""Tests for NodeAgentWorkerService."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.models.proposal import ProposalRecord
from bridle.services.node_agent_worker import NodeAgentWorkerService
from tests.helpers.plan_factory import two_node_plan


@pytest.mark.asyncio
async def test_worker_completes_run_and_creates_proposal(db: AsyncSession, client: AsyncClient) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Worker Task"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=two_node_plan())
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]

    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    session_id = session.json()["session_id"]
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session_id}/select-node",
        json={
            "intent": "select_node",
            "node_id": node_id,
            "reason": "go",
            "expected_action": "create_proposal",
        },
    )
    run_id = sel.json()["run_id"]
    await NodeAgentWorkerService.run_once(run_id, db=db)

    result = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
    run = result.scalar_one()
    assert run.status == "completed"

    prop = await db.execute(select(ProposalRecord).where(ProposalRecord.node_id == node_id))
    assert prop.scalars().first() is not None
