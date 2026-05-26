"""Containerized worker output protocol tests."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.services.node_agent_worker import NodeAgentWorkerService
from tests.helpers.plan_factory import code_change_node


@pytest.mark.asyncio
async def test_containerized_run_fails_when_manifest_missing(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    monkeypatch.setattr(
        "bridle.services.node_agent_worker.ContainerOutputSimulator.should_simulate",
        lambda _workspace: False,
    )
    task_resp = await client.post("/api/v1/tasks", json={"title": "Missing Manifest"})
    task_id = task_resp.json()["id"]
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={"goal": "missing manifest", "nodes": [code_change_node("n1", files=["src/x.py"])]},
    )
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={
            "intent": "select_node",
            "node_id": imp.json()["nodes"][0]["id"],
            "reason": "run",
            "expected_action": "create_proposal",
        },
    )
    run_id = sel.json()["run_id"]
    await NodeAgentWorkerService.run_once(run_id, db=db)

    run_row = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
    run = run_row.scalar_one()
    assert run.status == "failed"
    assert run.blocked_reason == "container_output_missing"
