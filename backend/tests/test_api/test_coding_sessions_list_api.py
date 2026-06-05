"""Coding sessions list API tests."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from bridle.models.agent_coding_session import AgentCodingSessionRecord
from tests.helpers.plan_factory import two_node_plan


async def _create_plan(client: AsyncClient) -> str:
    task_resp = await client.post("/api/v1/tasks", json={"title": "List sessions"})
    plan_resp = await client.post(
        f"/api/v1/tasks/{task_resp.json()['id']}/plan/import",
        json=two_node_plan(),
    )
    return plan_resp.json()["plan_id"]


@pytest.mark.asyncio
async def test_list_sessions_empty(client: AsyncClient) -> None:
    response = await client.get("/api/v1/agent/coding-sessions")
    assert response.status_code == 200
    body = response.json()
    assert body == {"sessions": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.asyncio
async def test_list_sessions_filter_and_paginate(
    client: AsyncClient,
    db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
    plan_id = await _create_plan(client)

    s1 = (await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})).json()
    s2_id = (await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})).json()["session_id"]
    await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    await client.post(f"/api/v1/agent/coding-sessions/{s2_id}/cancel")

    result = await db.execute(select(AgentCodingSessionRecord).where(AgentCodingSessionRecord.id == s1["session_id"]))
    row = result.scalar_one()
    row.status = "completed"
    await db.commit()

    all_resp = await client.get("/api/v1/agent/coding-sessions?status=all")
    assert all_resp.status_code == 200
    assert all_resp.json()["total"] == 3

    active_resp = await client.get("/api/v1/agent/coding-sessions?status=active")
    assert active_resp.json()["total"] == 1

    cancelled_resp = await client.get("/api/v1/agent/coding-sessions?status=cancelled")
    assert cancelled_resp.json()["total"] == 1

    page = await client.get("/api/v1/agent/coding-sessions?limit=1&offset=1")
    assert page.json()["total"] == 3
    assert len(page.json()["sessions"]) == 1

    by_plan = await client.get(f"/api/v1/agent/coding-sessions?plan_id={plan_id}")
    assert by_plan.json()["total"] == 3

    missing_plan = await client.get("/api/v1/agent/coding-sessions?plan_id=missing-plan")
    assert missing_plan.json()["total"] == 0


@pytest.mark.asyncio
async def test_list_sessions_invalid_status_returns_422(client: AsyncClient) -> None:
    response = await client.get("/api/v1/agent/coding-sessions?status=invalid")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_list_sessions_limit_validation(client: AsyncClient) -> None:
    assert (await client.get("/api/v1/agent/coding-sessions?limit=201")).status_code == 422
    assert (await client.get("/api/v1/agent/coding-sessions?offset=-1")).status_code == 422
