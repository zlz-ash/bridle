"""Containerized-only execution branch tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.models.proposal import ProposalRecord
from bridle.services.node_agent_worker import NodeAgentWorkerService
from tests.helpers.plan_factory import code_change_node


@pytest.mark.asyncio
async def test_containerized_run_does_not_call_provider(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    provider_mock = AsyncMock(side_effect=AssertionError("provider must not run in containerized mode"))
    with patch("bridle.services.node_agent_worker.AgentProviderFactory.create", provider_mock):
        task_resp = await client.post("/api/v1/tasks", json={"title": "No Provider"})
        task_id = task_resp.json()["id"]
        imp = await client.post(
            f"/api/v1/tasks/{task_id}/plan/import",
            json={"goal": "container only", "nodes": [code_change_node("n1", files=["src/x.py"])]},
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

    provider_mock.assert_not_called()


@pytest.mark.asyncio
async def test_containerized_completes_with_manifest_not_proposal(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    task_resp = await client.post("/api/v1/tasks", json={"title": "Manifest Complete"})
    task_id = task_resp.json()["id"]
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={"goal": "manifest", "nodes": [code_change_node("n1", files=["src/x.py"])]},
    )
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    session_id = session.json()["session_id"]
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session_id}/select-node",
        json={
            "intent": "select_node",
            "node_id": node_id,
            "reason": "run",
            "expected_action": "create_proposal",
        },
    )
    run_id = sel.json()["run_id"]
    await NodeAgentWorkerService.run_once(run_id, db=db)

    run = (await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))).scalar_one()
    assert run.status == "completed"
    assert run.result_summary == "container execution completed"
    prop = (await db.execute(select(ProposalRecord).where(ProposalRecord.node_id == node_id))).scalar_one_or_none()
    assert prop is None
