"""API tests for node run container diagnostics."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.services.node_agent_worker import NodeAgentWorkerService
from tests.helpers.plan_factory import code_change_node


@pytest.mark.asyncio
async def test_node_runs_returns_container_diagnostics(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    task_resp = await client.post("/api/v1/tasks", json={"title": "Runs API"})
    task_id = task_resp.json()["id"]
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={"goal": "runs", "nodes": [code_change_node("n1", files=["src/x.py"])]},
    )
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={
            "intent": "select_node",
            "node_id": node_id,
            "reason": "run",
            "expected_action": "create_proposal",
        },
    )
    run_id = sel.json()["run_id"]
    await NodeAgentWorkerService.run_once(run_id, db=db)

    resp = await client.get(f"/api/v1/nodes/{node_id}/runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) >= 1
    latest = runs[0]
    assert latest["container_id"] is not None
    assert latest["container_health"] == "healthy"
    assert latest.get("container_logs_summary")
