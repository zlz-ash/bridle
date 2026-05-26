"""Helpers for containerized pipeline end-to-end tests."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_run_lock import NodeAgentRunLockRecord
from tests.helpers.plan_factory import code_change_node


def setup_fake_git(workspace: Path, revision: str | None = None) -> str:
    rev = revision or ("a" * 40)
    git_dir = workspace / ".git" / "refs" / "heads"
    git_dir.mkdir(parents=True, exist_ok=True)
    (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "main").write_text(rev + "\n", encoding="utf-8")
    return rev


async def start_containerized_run(
    *,
    db: AsyncSession,
    client: AsyncClient,
    test_workspace: Path,
    monkeypatch: Any,
    plan_json: dict | None = None,
    disable_simulator: bool = False,
) -> dict[str, Any]:
    monkeypatch.setenv("BRIDLE_NODE_AGENT_RUN_MODE", "containerized")
    if disable_simulator:
        monkeypatch.setattr(
            "bridle.services.node_agent_worker.ContainerOutputSimulator.should_simulate",
            lambda _workspace: False,
        )
    setup_fake_git(test_workspace)
    task_resp = await client.post("/api/v1/tasks", json={"title": "Container E2E"})
    task_id = task_resp.json()["id"]
    body = plan_json or {
        "goal": "e2e",
        "nodes": [code_change_node("n1", files=["src/e2e.py"])],
    }
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=body)
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
    main_meta = json.loads(
        (test_workspace / ".aicoding" / "main-agent-containers" / f"{session_id}.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        "run_id": run_id,
        "node_id": node_id,
        "session_id": session_id,
        "main_meta": main_meta,
        "task_id": task_id,
    }


async def assert_lock_released(db: AsyncSession, node_id: str) -> None:
    from sqlalchemy import select

    lock = await db.execute(
        select(NodeAgentRunLockRecord).where(NodeAgentRunLockRecord.node_id == node_id)
    )
    assert lock.scalar_one_or_none() is None
