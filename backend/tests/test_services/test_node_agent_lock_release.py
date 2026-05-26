"""Tests that terminal NodeAgentRun releases node_agent_run_locks."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_run_lock import NodeAgentRunLockRecord
from bridle.services.node_agent_worker import NodeAgentWorkerService
from tests.helpers.plan_factory import two_node_plan


@pytest.mark.asyncio
async def test_worker_completed_releases_lock(db: AsyncSession, client: AsyncClient) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Lock Release"})
    imp = await client.post(
        f"/api/v1/tasks/{task_resp.json()['id']}/plan/import",
        json=two_node_plan(),
    )
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={
            "intent": "select_node",
            "node_id": node_id,
            "reason": "go",
            "expected_action": "create_proposal",
        },
    )
    run_id = sel.json()["run_id"]
    lock_before = await db.execute(
        select(NodeAgentRunLockRecord).where(NodeAgentRunLockRecord.node_id == node_id)
    )
    assert lock_before.scalar_one_or_none() is not None

    await NodeAgentWorkerService.run_once(run_id, db=db)

    lock_after = await db.execute(
        select(NodeAgentRunLockRecord).where(NodeAgentRunLockRecord.node_id == node_id)
    )
    assert lock_after.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_can_select_node_again_after_worker_completes(
    db: AsyncSession, client: AsyncClient,
) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Re-select"})
    imp = await client.post(
        f"/api/v1/tasks/{task_resp.json()['id']}/plan/import",
        json=two_node_plan(),
    )
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post(
        "/api/v1/agent/coding-sessions",
        json={"plan_id": plan_id, "auto_continue_budget": 3},
    )
    session_id = session.json()["session_id"]
    body = {
        "intent": "select_node",
        "node_id": node_id,
        "reason": "first",
        "expected_action": "create_proposal",
    }
    first = await client.post(f"/api/v1/agent/coding-sessions/{session_id}/select-node", json=body)
    run_id = first.json()["run_id"]
    await NodeAgentWorkerService.run_once(run_id, db=db)

    second = await client.post(f"/api/v1/agent/coding-sessions/{session_id}/select-node", json=body)
    assert second.status_code == 200, second.text
