"""Atomic NodeAgentRun lock tests."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.helpers.plan_factory import two_node_plan


@pytest.mark.asyncio
async def test_second_select_same_node_returns_already_running(client: AsyncClient) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Lock Task"})
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
    assert first.status_code == 200
    second = await client.post(f"/api/v1/agent/coding-sessions/{session_id}/select-node", json=body)
    assert second.status_code == 409
    assert second.json()["code"] == "node_already_running"
