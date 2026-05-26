"""Minimal end-to-end containerized pipeline: session → container → integrate → run history."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.services.node_agent_worker import NodeAgentWorkerService
from tests.helpers.plan_factory import code_change_node


@pytest.mark.asyncio
async def test_containerized_pipeline_happy_path_e2e(
    db: AsyncSession,
    client: AsyncClient,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    git_dir = test_workspace / ".git" / "refs" / "heads"
    git_dir.mkdir(parents=True, exist_ok=True)
    (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")

    task_resp = await client.post("/api/v1/tasks", json={"title": "E2E Container"})
    task_id = task_resp.json()["id"]
    imp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json={"goal": "e2e", "nodes": [code_change_node("n1", files=["src/e2e.py"])]},
    )
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": imp.json()["plan_id"]})
    session_id = session.json()["session_id"]
    main_meta = json.loads(
        (test_workspace / ".aicoding" / "main-agent-containers" / f"{session_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert main_meta.get("container_id")
    assert main_meta.get("git_baseline_revision")

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
    assert not (test_workspace / ".bridle").exists()
    await NodeAgentWorkerService.run_once(run_id, db=db)

    run = (await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))).scalar_one()
    assert run.status == "completed"
    assert (test_workspace / "src" / "e2e.py").read_text(encoding="utf-8") == "after\n"

    runs_resp = await client.get(f"/api/v1/nodes/{node_id}/runs")
    assert runs_resp.status_code == 200
    latest = runs_resp.json()[0]
    assert latest["container_id"]
    assert latest.get("test_summary")
    assert latest.get("metrics_summary")
    assert latest.get("integration_result", {}).get("status") == "integrated"
