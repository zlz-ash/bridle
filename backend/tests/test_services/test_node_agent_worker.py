"""Tests for NodeAgentWorkerService."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.engine.deepseek_agent_provider import DeepSeekProviderError
from bridle.models.node_agent_result import NodeAgentResultRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.models.proposal import ProposalRecord
from bridle.services.node_agent_worker import NodeAgentWorkerService
from tests.helpers.plan_factory import two_node_plan


@pytest.fixture(autouse=True)
def _force_fake_agent_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker API tests must not call real DeepSeek when host .env sets deepseek + API key."""
    monkeypatch.setenv("BRIDLE_AGENT_PROVIDER", "fake")


@pytest.mark.asyncio
async def test_worker_completes_run_and_creates_proposal(db: AsyncSession, client: AsyncClient) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Worker Task"})
    task_id = task_resp.json()["id"]
    plan = two_node_plan()
    plan["nodes"][0]["tests"] = ["echo worker-ok"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]

    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    session_id = session.json()["session_id"]
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session_id}/select-node",
        json={
            "intent": "select_node",
            "node_id": node_id,
            "reason": "go",
            "expected_action": "create_proposal",
        },
    )
    run_id = sel.json()["run_id"]
    await NodeAgentWorkerService.run_once(run_id, db=db)

    result = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
    run = result.scalar_one()
    assert run.status == "completed"

    prop = await db.execute(select(ProposalRecord).where(ProposalRecord.node_id == node_id))
    assert prop.scalars().first() is not None


@pytest.mark.asyncio
async def test_worker_persists_budget_exhausted_evidence(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    budget_report = {
        "error_code": "tool_budget_exhausted",
        "budget": {
            "type": "rounds",
            "limits": {"max_rounds": 1, "max_tool_calls": 8, "max_wall_seconds": 300.0},
            "used": {"rounds_used": 1, "tool_calls_used": 1, "wall_seconds_used": 0.5},
        },
        "changed_files": ["src/a.py"],
        "last_test_result": None,
        "last_tool_call": {"tool_name": "read_allowed_file", "args_summary": "path=src/a.py"},
        "needs_replan": True,
        "suggested_split": ["types/schema", "tests"],
    }
    exc = DeepSeekProviderError(
        "tool_budget_exhausted",
        "Tool budget exhausted: rounds",
        response_debug=budget_report,
    )
    provider = MagicMock()
    provider.name = "deepseek"
    provider.generate = AsyncMock(side_effect=exc)

    task_resp = await client.post("/api/v1/tasks", json={"title": "Budget Exhausted Worker"})
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
    run_id = sel.json()["run_id"]

    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        return_value=provider,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    run = (await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))).scalar_one()
    assert run.status == "failed"
    assert run.blocked_reason == "tool_budget_exhausted"
    assert "rounds" in (run.result_summary or "")

    result = (
        await db.execute(
            select(NodeAgentResultRecord).where(NodeAgentResultRecord.run_id == run_id)
        )
    ).scalar_one()
    assert result.result_type == "budget_exhausted"
    assert result.recommended_next_action == "replan_required"
    assert result.payload["error_code"] == "tool_budget_exhausted"
    assert result.payload["budget_report"]["needs_replan"] is True
    assert result.payload["replan_decision"]["replan_required"] is True
    assert result.payload["replan_decision"]["budget"]["type"] == "rounds"

    runs_resp = await client.get(f"/api/v1/nodes/{node_id}/runs")
    assert runs_resp.status_code == 200
    api_run = next(item for item in runs_resp.json() if item.get("id") == run_id)
    assert api_run["budget_report"]["budget"]["type"] == "rounds"
    assert api_run["replan_decision"]["replan_required"] is True

    # F14: node.status synced + lock released
    from bridle.models.node import NodeRecord
    from bridle.models.node_agent_run_lock import NodeAgentRunLockRecord
    node = (
        await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))
    ).scalar_one()
    assert node.status == "failed_retryable"
    locks = (
        await db.execute(select(NodeAgentRunLockRecord).where(NodeAgentRunLockRecord.node_id == node_id))
    ).scalars().all()
    assert locks == []


@pytest.mark.asyncio
async def test_worker_uses_node_budget(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F13: budget must scale with node.metrics.complexity.estimate.estimated_minutes."""
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    monkeypatch.delenv("BRIDLE_DEEPSEEK_MAX_TOOL_ROUNDS", raising=False)
    monkeypatch.delenv("BRIDLE_DEEPSEEK_MAX_TOOL_CALLS", raising=False)
    monkeypatch.delenv("BRIDLE_DEEPSEEK_MAX_WALL_SECONDS", raising=False)

    provider = MagicMock()
    provider.name = "deepseek"
    provider.generate = AsyncMock(
        side_effect=DeepSeekProviderError("provider_invocation_failed", "boom")
    )

    task_resp = await client.post("/api/v1/tasks", json={"title": "Node Budget Worker"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=two_node_plan())
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]

    # Pin node estimated_minutes=90 via direct DB write.
    from bridle.models.node import NodeRecord
    from sqlalchemy.orm.attributes import flag_modified
    node_rec = (await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))).scalar_one()
    metrics = dict(node_rec.metrics) if isinstance(node_rec.metrics, dict) else {}
    complexity = dict(metrics.get("complexity") or {})
    estimate = dict(complexity.get("estimate") or {})
    estimate["estimated_minutes"] = 90
    complexity["estimate"] = estimate
    metrics["complexity"] = complexity
    node_rec.metrics = metrics
    flag_modified(node_rec, "metrics")
    await db.commit()

    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={"intent": "select_node", "node_id": node_id, "reason": "go"},
    )
    run_id = sel.json()["run_id"]

    create_calls: list[dict] = []

    def _spy(context=None, *, budget_override=None):
        create_calls.append({"budget_override": budget_override})
        return provider

    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        side_effect=_spy,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    assert len(create_calls) == 1
    override = create_calls[0]["budget_override"]
    assert override is not None, "worker must pass budget_override"
    # est=90 → rounds=30, calls=120, wall=1080
    assert override["max_rounds"] == 30
    assert override["max_tool_calls"] == 120
    assert override["max_wall_seconds"] == 1080.0


@pytest.mark.asyncio
async def test_worker_generic_failure_sets_node_failed_retryable(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F14: 非 budget_exhausted 的失败路径（走 _fail_run）也要同步 node.status + 释放 lock。"""
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    provider = MagicMock()
    provider.name = "deepseek"
    provider.generate = AsyncMock(
        side_effect=DeepSeekProviderError("provider_invocation_failed", "boom")
    )

    task_resp = await client.post("/api/v1/tasks", json={"title": "Generic Failure Worker"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=two_node_plan())
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={"intent": "select_node", "node_id": node_id, "reason": "go"},
    )
    run_id = sel.json()["run_id"]

    with patch(
        "bridle.services.node_agent_worker.AgentProviderFactory.create",
        return_value=provider,
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    run = (await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))).scalar_one()
    assert run.status == "failed"

    from bridle.models.node import NodeRecord
    from bridle.models.node_agent_run_lock import NodeAgentRunLockRecord
    node = (await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))).scalar_one()
    assert node.status == "failed_retryable"
    locks = (
        await db.execute(select(NodeAgentRunLockRecord).where(NodeAgentRunLockRecord.node_id == node_id))
    ).scalars().all()
    assert locks == []


@pytest.mark.asyncio
async def test_sandbox_uses_node_tests_not_proposal(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F16: sandbox executes plan node.tests, not proposal.tests_to_run."""
    from bridle.engine.sandboxed_tool_executor import SandboxedToolExecutor
    from bridle.models.node import NodeRecord
    from bridle.schemas.proposal import AgentProposalSchema

    captured: dict[str, list[str]] = {}

    async def fake_run_allowed_tests(self, commands: list[str]) -> dict:
        captured["commands"] = list(commands)
        return {"status": "completed", "results": []}

    monkeypatch.setattr(SandboxedToolExecutor, "run_allowed_tests", fake_run_allowed_tests)

    node = NodeRecord(
        plan_id="plan-1",
        plan_node_id="n1",
        title="N",
        goal="implement",
        node_type="code_change",
        files=["src/a.py"],
        tests=["pytest a.py"],
        constraints={"write_set": ["src/a.py"]},
    )
    proposal = AgentProposalSchema(
        summary="ok",
        file_patches=[],
        tests_to_run=["pytest b.py"],
    )

    await NodeAgentWorkerService._run_sandbox_tests("run-1", "node-1", node, proposal)

    assert captured["commands"] == ["pytest a.py"]


@pytest.mark.asyncio
async def test_worker_fails_when_sandbox_tests_fail(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F16: sandbox status=failed must not complete the run."""
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    failed_results = {
        "status": "failed",
        "results": [
            {
                "command": "pytest x.py",
                "exit_code": 1,
                "stderr_preview": "E: file not found",
            }
        ],
    }

    task_resp = await client.post("/api/v1/tasks", json={"title": "Sandbox Test Fail"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=two_node_plan())
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={"intent": "select_node", "node_id": node_id, "reason": "go"},
    )
    run_id = sel.json()["run_id"]

    with patch.object(
        NodeAgentWorkerService,
        "_run_sandbox_tests",
        AsyncMock(return_value=failed_results),
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    run = (await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))).scalar_one()
    assert run.status == "failed"
    assert run.blocked_reason == "tests_failed"
    assert "pytest x.py" in (run.result_summary or "")

    result = (
        await db.execute(
            select(NodeAgentResultRecord).where(NodeAgentResultRecord.run_id == run_id)
        )
    ).scalar_one()
    assert result.result_type == "tests_failed"
    assert result.recommended_next_action == "needs_test_files_or_fix"

    from bridle.models.node import NodeRecord

    node = (await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))).scalar_one()
    assert node.status == "failed_retryable"

    prop = await db.execute(select(ProposalRecord).where(ProposalRecord.node_id == node_id))
    assert prop.scalars().first() is None

    from bridle.models.chat_message import ChatMessageRecord

    chat_rows = (
        await db.execute(
            select(ChatMessageRecord).where(
                ChatMessageRecord.session_id == session.json()["session_id"],
                ChatMessageRecord.role == "assistant",
            )
        )
    ).scalars().all()
    assert len(chat_rows) == 1
    assert "⚠️ 节点" in chat_rows[0].content
    assert "pytest x.py" in chat_rows[0].content


@pytest.mark.asyncio
async def test_failure_chat_message_bypasses_reconcile(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F17: ORM write must not trigger SessionReconciler.ensure_main_agent_alive."""
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    failed_results = {"status": "failed", "results": [{"command": "pytest x.py", "exit_code": 1}]}

    task_resp = await client.post("/api/v1/tasks", json={"title": "Bypass Reconcile"})
    imp = await client.post(f"/api/v1/tasks/{task_resp.json()['id']}/plan/import", json=two_node_plan())
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={"intent": "select_node", "node_id": node_id, "reason": "go"},
    )
    run_id = sel.json()["run_id"]

    with patch.object(
        NodeAgentWorkerService,
        "_run_sandbox_tests",
        AsyncMock(return_value=failed_results),
    ), patch(
        "bridle.services.session_reconciler.SessionReconciler.ensure_main_agent_alive",
        new_callable=AsyncMock,
    ) as reconcile_mock:
        await NodeAgentWorkerService.run_once(run_id, db=db)

    reconcile_mock.assert_not_called()


@pytest.mark.asyncio
async def test_worker_completes_when_sandbox_tests_pass(
    db: AsyncSession,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F16: sandbox status=completed still completes the run (regression guard)."""
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "provider_only")
    passed_results = {
        "status": "completed",
        "results": [{"command": "pytest tests/", "exit_code": 0}],
    }

    task_resp = await client.post("/api/v1/tasks", json={"title": "Sandbox Test Pass"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=two_node_plan())
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    sel = await client.post(
        f"/api/v1/agent/coding-sessions/{session.json()['session_id']}/select-node",
        json={"intent": "select_node", "node_id": node_id, "reason": "go"},
    )
    run_id = sel.json()["run_id"]

    with patch.object(
        NodeAgentWorkerService,
        "_run_sandbox_tests",
        AsyncMock(return_value=passed_results),
    ):
        await NodeAgentWorkerService.run_once(run_id, db=db)

    run = (await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))).scalar_one()
    assert run.status == "completed"

    result = (
        await db.execute(
            select(NodeAgentResultRecord).where(NodeAgentResultRecord.run_id == run_id)
        )
    ).scalar_one()
    assert result.status == "completed"
    assert result.payload.get("sandbox_test_results") == passed_results
