"""Full session flow: main-agent + node-agent containers (gated)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node import NodeRecord
from tests.helpers.plan_factory import code_change_node

pytestmark = pytest.mark.skipif(
    not os.getenv("BRIDLE_RUN_DOCKER_TESTS"),
    reason="needs docker",
)


def _minimal_plan() -> dict:
    return {
        "goal": "Implement a tiny roman numeral helper",
        "nodes": [
            code_change_node(
                "node-001",
                files=["roman_converter.py"],
                tests=[],
                constraints={"bounded": True},
            ),
        ],
    }


async def _wait_for_node_completed(
    client: AsyncClient,
    *,
    plan_node_id: str,
    timeout_seconds: float = 300.0,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        plan_resp = await client.get("/api/v1/plan/current")
        if plan_resp.status_code == 200:
            nodes = plan_resp.json().get("nodes") or []
            match = next((n for n in nodes if n.get("plan_node_id") == plan_node_id), None)
            if match and match.get("status") == "completed":
                return match
        await asyncio.sleep(3)
    raise AssertionError(f"node {plan_node_id} did not complete within {timeout_seconds}s")


@pytest.mark.asyncio
async def test_full_session_flow(
    docker_backend_on_8900: tuple[AsyncClient, object],
    test_workspace: Path,
    db: AsyncSession,
    require_docker_images: None,
    require_agent_api_key: None,
) -> None:
    client, _server = docker_backend_on_8900

    task_resp = await client.post("/api/v1/tasks", json={"title": "Docker E2E"})
    assert task_resp.status_code == 201, task_resp.text
    task_id = task_resp.json()["id"]

    plan_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=_minimal_plan())
    assert plan_resp.status_code == 200, plan_resp.text
    plan_id = plan_resp.json()["plan_id"]
    plan_node_id = plan_resp.json()["nodes"][0]["plan_node_id"]

    session_resp = await client.post(
        "/api/v1/agent/coding-sessions",
        json={"plan_id": plan_id, "auto_continue_budget": 2},
    )
    assert session_resp.status_code == 200, session_resp.text
    session_id = session_resp.json()["session_id"]
    main_meta = session_resp.json().get("main_agent_container") or {}
    assert main_meta.get("container_id"), "main-agent container should start"

    msg_resp = await client.post(
        f"/api/v1/agent/coding-sessions/{session_id}/messages",
        json={"role": "user", "content": "开始执行"},
    )
    assert msg_resp.status_code == 201, msg_resp.text

    completed = await _wait_for_node_completed(client, plan_node_id=plan_node_id, timeout_seconds=300.0)
    assert completed["status"] == "completed"

    messages_resp = await client.get(f"/api/v1/agent/coding-sessions/{session_id}/messages")
    assert messages_resp.status_code == 200
    roles = [m["role"] for m in messages_resp.json()]
    assert roles.count("assistant") >= 1

    ps = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    names = ps.stdout or ""
    assert f"main-agent-{session_id}" in names or "main-agent-" in names

    cancel_resp = await client.post(f"/api/v1/agent/coding-sessions/{session_id}/cancel")
    assert cancel_resp.status_code == 200, cancel_resp.text

    for _ in range(20):
        ps2 = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        running = ps2.stdout or ""
        if f"main-agent-{session_id}" not in running:
            break
        await asyncio.sleep(0.5)
    else:
        pytest.fail("main-agent container still running 10s after cancel")

    node_row = await db.execute(
        select(NodeRecord).where(NodeRecord.plan_node_id == plan_node_id)
    )
    node = node_row.scalar_one()
    assert node.status == "completed"
