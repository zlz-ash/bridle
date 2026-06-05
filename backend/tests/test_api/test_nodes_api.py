"""API tests for /nodes/{node_id}/runs budget replan evidence."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from bridle.engine.deepseek_agent_provider import DeepSeekProviderError
from bridle.models.node import NodeRecord
from bridle.schemas.proposal import AgentProposalSchema
from bridle.services.node_agent_worker import NodeAgentWorkerService
from tests.helpers.plan_factory import code_change_node, two_node_plan


def _budget_exhausted_report(*, with_secret: bool = False) -> dict:
    args_summary = "path=src/a.py"
    if with_secret:
        args_summary = '{"path": "src/a.py", "API_KEY": ***}'
    return {
        "error_code": "tool_budget_exhausted",
        "budget": {
            "type": "rounds",
            "limits": {"max_rounds": 1, "max_tool_calls": 8, "max_wall_seconds": 300.0},
            "used": {"rounds_used": 1, "tool_calls_used": 1, "wall_seconds_used": 0.5},
        },
        "changed_files": ["src/a.py"],
        "last_test_result": None,
        "last_tool_call": {"tool_name": "read_allowed_file", "args_summary": args_summary},
        "needs_replan": True,
        "suggested_split": ["types/schema", "tests"],
    }


def _echo_test_plan() -> dict:
    plan = two_node_plan()
    plan["nodes"][0]["tests"] = ["echo nodes-api-ok"]
    plan["nodes"][1]["tests"] = ["echo nodes-api-ok"]
    return plan


async def _start_agent_run(
    client: AsyncClient,
    *,
    plan: dict | None = None,
) -> tuple[str, str]:
    plan_payload = plan or _echo_test_plan()
    task_resp = await client.post("/api/v1/tasks", json={"title": "Nodes API Run"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan_payload)
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
async def test_get_node_runs_exposes_budget_replan_evidence(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    budget_report = _budget_exhausted_report()
    exc = DeepSeekProviderError(
        "tool_budget_exhausted",
        "Tool budget exhausted: rounds",
        response_debug=budget_report,
    )
    provider = MagicMock()
    provider.name = "deepseek"
    provider.generate = AsyncMock(side_effect=exc)

    node_id, run_id = await _start_agent_run(client)
    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        return_value=provider,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    resp = await client.get(f"/api/v1/nodes/{node_id}/runs")
    assert resp.status_code == 200
    runs = resp.json()
    target = next(item for item in runs if item.get("id") == run_id)

    assert target["status"] == "failed"
    assert target["error_code"] == "tool_budget_exhausted"
    assert target["budget_report"]["budget"]["type"] == "rounds"
    assert target["budget_report"]["budget"]["used"]["rounds_used"] == 1
    assert target["budget_report"]["last_tool_call"]["tool_name"] == "read_allowed_file"
    assert target["replan_decision"]["replan_required"] is True
    assert target["replan_decision"]["needs_replan"] is True


@pytest.mark.asyncio
async def test_get_node_runs_success_run_has_no_budget_evidence(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    proposal = AgentProposalSchema(
        summary="done",
        file_patches=[],
        tests_to_run=[],
    )
    provider = MagicMock()
    provider.name = "deepseek"
    provider.generate = AsyncMock(return_value=proposal)

    node_id, run_id = await _start_agent_run(client)
    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        return_value=provider,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    resp = await client.get(f"/api/v1/nodes/{node_id}/runs")
    assert resp.status_code == 200
    target = next(item for item in resp.json() if item.get("id") == run_id)

    assert target["status"] == "completed"
    assert target.get("budget_report") is None
    assert target.get("replan_decision") is None


@pytest.mark.asyncio
async def test_get_node_report_includes_agent_runs_budget_evidence(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    budget_report = _budget_exhausted_report(with_secret=True)
    exc = DeepSeekProviderError(
        "tool_budget_exhausted",
        "exhausted",
        response_debug=budget_report,
    )
    provider = MagicMock()
    provider.name = "deepseek"
    provider.generate = AsyncMock(side_effect=exc)

    node_id, run_id = await _start_agent_run(client)
    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        return_value=provider,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    resp = await client.get(f"/api/v1/nodes/{node_id}/report")
    assert resp.status_code == 200
    data = resp.json()
    assert "agent_runs" in data
    target = next(item for item in data["agent_runs"] if item.get("run_id") == run_id)
    assert target["budget_report"]["budget"]["type"] == "rounds"
    assert target["replan_decision"]["replan_required"] is True
    summary = target["budget_report"]["last_tool_call"]["args_summary"]
    assert "secret-value" not in summary


@pytest.mark.asyncio
async def test_get_node_report_summary_counts_budget_exhausted_agent_run(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    exc = DeepSeekProviderError(
        "tool_budget_exhausted",
        "exhausted",
        response_debug=_budget_exhausted_report(),
    )
    provider = MagicMock()
    provider.name = "deepseek"
    provider.generate = AsyncMock(side_effect=exc)

    node_id, run_id = await _start_agent_run(client)
    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        return_value=provider,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    resp = await client.get(f"/api/v1/nodes/{node_id}/report")
    assert resp.status_code == 200
    data = resp.json()
    summary = data["summary"]

    assert summary["legacy_run_count"] == 0
    assert summary["agent_run_count"] == 1
    assert summary["agent_failed_runs"] == 1
    assert summary["agent_completed_runs"] == 0
    assert summary["total_runs"] == 1
    assert summary["failed_runs"] == 1
    assert summary["completed_runs"] == 0

    target = next(item for item in data["agent_runs"] if item.get("run_id") == run_id)
    assert target["budget_report"]["budget"]["type"] == "rounds"
    assert target["replan_decision"]["replan_required"] is True


@pytest.mark.asyncio
async def test_get_node_report_summary_counts_successful_agent_run(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    proposal = AgentProposalSchema(
        summary="done",
        file_patches=[],
        tests_to_run=[],
    )
    provider = MagicMock()
    provider.name = "deepseek"
    provider.generate = AsyncMock(return_value=proposal)

    node_id, run_id = await _start_agent_run(client)
    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        return_value=provider,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    resp = await client.get(f"/api/v1/nodes/{node_id}/report")
    assert resp.status_code == 200
    summary = resp.json()["summary"]

    assert summary["legacy_run_count"] == 0
    assert summary["agent_run_count"] == 1
    assert summary["agent_completed_runs"] == 1
    assert summary["agent_failed_runs"] == 0
    assert summary["total_runs"] == 1
    assert summary["completed_runs"] == 1
    assert summary["failed_runs"] == 0


@pytest.mark.asyncio
async def test_get_node_report_summary_counts_mixed_legacy_and_agent_runs(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    mixed_plan = {
        "goal": "Mixed legacy and agent runs",
        "nodes": [
            code_change_node("n1", tests=["echo ok"]),
            code_change_node("n2", depends_on=["n1"], tests=["echo ok"]),
        ],
    }

    task_resp = await client.post("/api/v1/tasks", json={"title": "Mixed Report Task"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=mixed_plan)
    node_id = imp.json()["nodes"][0]["id"]

    exc = DeepSeekProviderError(
        "tool_budget_exhausted",
        "exhausted",
        response_debug=_budget_exhausted_report(),
    )
    provider = MagicMock()
    provider.name = "deepseek"
    provider.generate = AsyncMock(side_effect=exc)

    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
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
    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        return_value=provider,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    legacy_resp = await client.post(f"/api/v1/nodes/{node_id}/run")
    assert legacy_resp.status_code == 200

    resp = await client.get(f"/api/v1/nodes/{node_id}/report")
    assert resp.status_code == 200
    data = resp.json()
    summary = data["summary"]

    assert len(data["runs"]) == 1
    assert len(data["agent_runs"]) == 1
    assert summary["legacy_run_count"] == 1
    assert summary["legacy_completed_runs"] == 1
    assert summary["legacy_failed_runs"] == 0
    assert summary["agent_run_count"] == 1
    assert summary["agent_completed_runs"] == 0
    assert summary["agent_failed_runs"] == 1
    assert summary["total_runs"] == 2
    assert summary["completed_runs"] == 1
    assert summary["failed_runs"] == 1

    agent_target = next(item for item in data["agent_runs"] if item.get("run_id") == run_id)
    assert agent_target["budget_report"]["needs_replan"] is True
    assert agent_target["replan_decision"]["replan_required"] is True
    assert data["baseline_run"] is not None
    assert data["baseline_run"]["status"] == "completed"


@pytest.mark.asyncio
async def test_latest_run_404_when_no_runs(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
    task_resp = await client.post("/api/v1/tasks", json={"title": "Latest run empty"})
    plan_resp = await client.post(
        f"/api/v1/tasks/{task_resp.json()['id']}/plan/import",
        json=two_node_plan(),
    )
    node_id = plan_resp.json()["nodes"][0]["id"]
    response = await client.get(f"/api/v1/nodes/{node_id}/runs/latest")
    assert response.status_code == 404
    assert response.json()["code"] == "no_run_for_node"


@pytest.mark.asyncio
async def test_latest_run_matches_list_head(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
    provider = MagicMock()
    provider.name = "stub"
    provider.generate = AsyncMock(
        return_value=AgentProposalSchema(summary="ok", file_patches=[], tests_to_run=[]),
    )

    node_id, run_id = await _start_agent_run(client)
    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        return_value=provider,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    latest = await client.get(f"/api/v1/nodes/{node_id}/runs/latest")
    runs = await client.get(f"/api/v1/nodes/{node_id}/runs")
    assert latest.status_code == 200
    assert runs.status_code == 200
    head = runs.json()[0]
    latest_body = latest.json()
    assert latest_body["run_id"] == head["id"]
    assert latest_body["status"] == head["status"]
    assert latest_body["phase"] == head["phase"]


@pytest.mark.asyncio
async def test_latest_run_returns_newest_of_two_runs(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F16 caps node attempts at 2; latest run is the second completed run."""
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
    provider = MagicMock()
    provider.name = "stub"
    provider.generate = AsyncMock(
        return_value=AgentProposalSchema(summary="ok", file_patches=[], tests_to_run=[]),
    )

    plan_payload = _echo_test_plan()
    task_resp = await client.post("/api/v1/tasks", json={"title": "Latest run x2"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan_payload)
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post(
        "/api/v1/agent/coding-sessions",
        json={"plan_id": plan_id, "auto_continue_budget": 5},
    )
    session_id = session.json()["session_id"]
    select_payload = {
        "intent": "select_node",
        "node_id": node_id,
        "reason": "go",
        "expected_action": "create_proposal",
    }

    run_ids: list[str] = []
    for _ in range(2):
        sel = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            json=select_payload,
        )
        assert sel.status_code == 200, sel.text
        run_id = sel.json()["run_id"]
        with patch(
            "bridle.services.node_agent_worker.AgentProviderFactory.create",
            return_value=provider,
        ):
            await NodeAgentWorkerService.run_once(run_id, db=db)
        node_rec = (
            await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))
        ).scalar_one()
        node_rec.status = "failed_retryable"
        await db.commit()
        run_ids.append(run_id)

    latest = await client.get(f"/api/v1/nodes/{node_id}/runs/latest")
    runs = await client.get(f"/api/v1/nodes/{node_id}/runs")
    assert latest.status_code == 200
    assert runs.status_code == 200
    assert latest.json()["run_id"] == runs.json()[0]["id"]
    assert latest.json()["run_id"] == run_ids[-1]


@pytest.mark.asyncio
async def test_latest_run_unknown_node_404(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nodes/missing-node/runs/latest")
    assert response.status_code == 404
    assert response.json()["resource"] == "node"
