"""Master skill assignment at select-node time."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.services.master_skill_assignment import assign_skill_for_node, load_assignment
from tests.helpers.plan_factory import code_change_node, two_node_plan


async def _start_run(client: AsyncClient) -> tuple[str, str]:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Skill assign"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=two_node_plan())
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
    return node_id, sel.json()["run_id"]


@pytest.mark.asyncio
async def test_select_node_persists_master_skill_assignment(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    with patch("bridle.services.agent_coding_session_service.NodeAgentWorkerService.start"):
        _, run_id = await _start_run(client)

    assignment = load_assignment(run_id)
    assert assignment is not None
    assert assignment.get("assigned_by") == "master"
    assert "submodule" in assignment


@pytest.mark.asyncio
async def test_worker_prefers_master_assignment_over_fallback(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bridle.models.node import NodeRecord
    from bridle.services.node_agent_worker import NodeAgentWorkerService

    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    with patch("bridle.services.agent_coding_session_service.NodeAgentWorkerService.start"):
        _, run_id = await _start_run(client)

    master = load_assignment(run_id)
    assert master is not None

    from uuid import uuid4

    node = NodeRecord(
        id=str(uuid4()),
        plan_id=str(uuid4()),
        plan_node_id="pn",
        title="t",
        goal="g",
        node_type="python",
        order=0,
        status="pending",
        tests=["pytest -q"],
        files=["src/a.py"],
    )
    enriched = NodeAgentWorkerService._enrich_accessible_context(
        {"accessible": []},
        run_id=run_id,
        node=node,
        node_tests=["pytest -q"],
    )
    assert enriched["skill_assignment_source"] == "master"
    assert enriched["skill_guidance"]["submodule"] == master["submodule"]


def test_assign_skill_for_node_marks_master() -> None:
    from uuid import uuid4

    from bridle.models.node import NodeRecord

    node = NodeRecord(
        id=str(uuid4()),
        plan_id=str(uuid4()),
        plan_node_id="pn",
        title="t",
        goal="implement",
        node_type="code",
        order=0,
        status="pending",
        tests=["pytest -q"],
        files=["src/a.py", "tests/test_a.py"],
    )
    assignment = assign_skill_for_node(node)
    assert assignment["assigned_by"] == "master"
    assert assignment["use_skill"] is True
    assert assignment["env_layout"] == "python_src_package"
