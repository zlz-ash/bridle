"""Heartbeat permission boundary tests."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.helpers.plan_factory import two_node_plan


async def _create_run(client: AsyncClient) -> tuple[str, str]:
    task_resp = await client.post("/api/v1/tasks", json={"title": "HB Task"})
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
    return sel.json()["run_id"], node_id


@pytest.mark.asyncio
async def test_heartbeat_terminal_status_rejected(client: AsyncClient, db) -> None:
    from bridle.models.node_agent_run import NodeAgentRunRecord
    from sqlalchemy import select

    run_id, node_id = await _create_run(client)
    result = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
    run_rec = result.scalar_one()
    run_rec.status = "running"
    await db.commit()
    for terminal in ("completed", "failed", "timed_out", "cancelled"):
        resp = await client.post(
            f"/api/v1/node-agent-runs/{run_id}/heartbeat",
            json={
                "run_id": run_id,
                "node_id": node_id,
                "status": terminal,
                "phase": "editing",
                "message": "bad",
            },
        )
        assert resp.status_code == 422, terminal
