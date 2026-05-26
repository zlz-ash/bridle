"""Worker integration tests for sandbox policy wiring."""
from __future__ import annotations

import pytest
import json
from pathlib import Path
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_result import NodeAgentResultRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.services.node_agent_worker import NodeAgentWorkerService
from tests.helpers.plan_factory import code_change_node, two_node_plan


@pytest.mark.asyncio
async def test_worker_context_includes_tool_capabilities(db: AsyncSession, client: AsyncClient) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Sandbox Ctx"})
    task_id = task_resp.json()["id"]
    plan = two_node_plan()
    plan["nodes"][0]["files"] = ["src/n1.py"]
    plan["nodes"][0]["tests"] = ["echo worker-cap"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
    run_id = None
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={
            "intent": "select_node",
            "node_id": imp.json()["nodes"][0]["id"],
            "reason": "sandbox",
            "expected_action": "create_proposal",
        },
    )
    run_id = sel.json()["run_id"]
    ctx, _node, _instruction = await NodeAgentWorkerService.build_context(db, run_id)
    assert ctx.tool_capabilities
    tools = ctx.tool_capabilities.get("tool_capabilities", {})
    assert tools.get("read_allowed_file", {}).get("allowed") is True
    assert tools.get("apply_patch", {}).get("allowed") is False
    assert "src/n1.py" in tools.get("read_allowed_file", {}).get("paths", [])


@pytest.mark.asyncio
async def test_worker_context_builds_container_workspace_from_boundary(
    db: AsyncSession,
    client: AsyncClient,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    (test_workspace / "src").mkdir(exist_ok=True)
    (test_workspace / "src" / "write.py").write_text("write\n", encoding="utf-8")
    (test_workspace / "src" / "read.py").write_text("read\n", encoding="utf-8")

    task_resp = await client.post("/api/v1/tasks", json={"title": "Container Workspace"})
    task_id = task_resp.json()["id"]
    node = code_change_node(
        "n1",
        files=["src/legacy.py"],
        tests=["echo ok"],
    )
    node["read_set"] = ["src/read.py"]
    node["write_set"] = ["src/write.py"]
    node["readonly_context"] = []
    node["conflict_contributions"] = [
        {
            "aggregate_target": "src/router.json",
            "contribution_path": ".bridle/aggregate/src/router.json/n1.json",
        }
    ]
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={
            "goal": "container workspace",
            "aggregate_files": [
                {
                    "target_path": "src/router.json",
                    "contribution_dir": ".bridle/aggregate/src/router.json",
                    "merge_strategy": "json_list",
                    "owner": "main-agent",
                    "contributors": ["n1"],
                    "validation": {"unique_key": "path"},
                }
            ],
            "nodes": [node],
        },
    )
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={
            "intent": "select_node",
            "node_id": imp.json()["nodes"][0]["id"],
            "reason": "container",
            "expected_action": "create_proposal",
        },
    )

    ctx, _node, _instruction = await NodeAgentWorkerService.build_context(db, sel.json()["run_id"])

    assert ctx.allowed_files == ["src/write.py"]
    container_workspace = ctx.tool_capabilities["container_workspace"]
    manifest_path = Path(container_workspace["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["mounts"]["write"] == ["src/write.py"]
    assert manifest["mounts"]["read"] == ["src/read.py"]
    assert manifest["mounts"]["aggregate"] == [".bridle/aggregate/src/router.json/n1.json"]


@pytest.mark.asyncio
async def test_provider_only_context_does_not_build_container_workspace(
    db: AsyncSession,
    client: AsyncClient,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    task_resp = await client.post("/api/v1/tasks", json={"title": "Provider Only"})
    task_id = task_resp.json()["id"]
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={"goal": "provider only", "nodes": [code_change_node("n1", files=["src/x.py"])]},
    )
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={
            "intent": "select_node",
            "node_id": imp.json()["nodes"][0]["id"],
            "reason": "provider",
            "expected_action": "create_proposal",
        },
    )

    ctx, _node, _instruction = await NodeAgentWorkerService.build_context(db, sel.json()["run_id"])

    assert "container_workspace" not in ctx.tool_capabilities
    assert not (test_workspace / ".aicoding" / "container-workspaces" / sel.json()["run_id"]).exists()


@pytest.mark.asyncio
async def test_containerized_context_builds_container_workspace(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    task_resp = await client.post("/api/v1/tasks", json={"title": "Containerized"})
    task_id = task_resp.json()["id"]
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={"goal": "containerized", "nodes": [code_change_node("n1", files=["src/x.py"])]},
    )
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={
            "intent": "select_node",
            "node_id": imp.json()["nodes"][0]["id"],
            "reason": "container",
            "expected_action": "create_proposal",
        },
    )

    ctx, _node, _instruction = await NodeAgentWorkerService.build_context(db, sel.json()["run_id"])

    assert "container_workspace" in ctx.tool_capabilities


@pytest.mark.asyncio
async def test_containerized_run_starts_node_container(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    task_resp = await client.post("/api/v1/tasks", json={"title": "Container Run"})
    task_id = task_resp.json()["id"]
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={"goal": "container run", "nodes": [code_change_node("n1", files=["src/x.py"])]},
    )
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={
            "intent": "select_node",
            "node_id": imp.json()["nodes"][0]["id"],
            "reason": "container",
            "expected_action": "create_proposal",
        },
    )
    run_id = sel.json()["run_id"]
    await NodeAgentWorkerService.run_once(run_id, db=db)

    run_row = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
    run = run_row.scalar_one()
    assert run.container_id is not None
    assert run.container_health == "healthy"


@pytest.mark.asyncio
async def test_worker_records_sandbox_test_results(db: AsyncSession, client: AsyncClient) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Sandbox Tests"})
    task_id = task_resp.json()["id"]
    node = code_change_node(
        "n1",
        files=["src/x.py"],
        tests=["echo sandbox-ok"],
    )
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={"goal": "sandbox tests", "nodes": [node]},
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
    assert run_row.scalar_one().status == "completed"

    result_row = await db.execute(
        select(NodeAgentResultRecord).where(NodeAgentResultRecord.run_id == run_id)
    )
    payload = result_row.scalar_one().payload
    assert "sandbox_test_results" in payload


@pytest.mark.asyncio
async def test_worker_fails_on_command_policy_error(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid tests_to_run must fail run; must not create completed result."""
    from bridle.schemas.proposal import AgentProposalSchema

    class BadTestsProvider:
        name = "fake"

        async def generate(self, ctx):  # noqa: ANN001
            return AgentProposalSchema(
                summary="policy violation",
                file_patches=[],
                tests_to_run=["echo blocked-cmd"],
            )

    original_build = NodeAgentWorkerService.build_context

    async def build_with_empty_allowlist(db_session: AsyncSession, run_id: str):
        ctx, node, instruction = await original_build(db_session, run_id)
        caps = dict(ctx.tool_capabilities)
        sandbox = dict(caps.get("sandbox", {}))
        sandbox["allowed_test_commands"] = []
        caps["sandbox"] = sandbox
        return ctx.model_copy(update={"tool_capabilities": caps}), node, instruction

    monkeypatch.setattr(NodeAgentWorkerService, "build_context", build_with_empty_allowlist)
    monkeypatch.setattr(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        lambda context=None: BadTestsProvider(),
    )

    task_resp = await client.post("/api/v1/tasks", json={"title": "Cmd Policy Fail"})
    task_id = task_resp.json()["id"]
    node = code_change_node("n1", files=["src/x.py"], tests=["echo allowed-only"])
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={"goal": "cmd policy", "nodes": [node]},
    )
    assert imp.status_code == 200, imp.text
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={
            "intent": "select_node",
            "node_id": imp.json()["nodes"][0]["id"],
            "reason": "policy",
            "expected_action": "create_proposal",
        },
    )
    run_id = sel.json()["run_id"]
    await NodeAgentWorkerService.run_once(run_id, db=db)

    run_row = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
    run = run_row.scalar_one()
    assert run.status == "failed"
    assert run.blocked_reason == "CommandPolicyError"

    result_row = await db.execute(
        select(NodeAgentResultRecord).where(NodeAgentResultRecord.run_id == run_id)
    )
    assert result_row.scalar_one_or_none() is None
