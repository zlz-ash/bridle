"""Tests for agent coding orchestration (sessions, eligibility, node runs, plan change)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node import NodeRecord
from bridle.models.node_agent_result import NodeAgentResultRecord
from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.services.node_agent_watchdog import NodeAgentWatchdog
from tests.helpers.plan_factory import code_change_node, two_node_plan


async def _create_task_with_plan(client: AsyncClient, plan: dict | None = None) -> tuple[str, str, list[dict]]:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Coding Task"})
    task_id = task_resp.json()["id"]
    import_resp = await client.post(
        f"/api/v1/tasks/{task_id}/plan/import",
        json=plan or two_node_plan(),
    )
    data = import_resp.json()
    return task_id, data["plan_id"], data["nodes"]


def _setup_git(workspace: Path) -> None:
    git_dir = workspace / ".git" / "refs" / "heads"
    git_dir.mkdir(parents=True, exist_ok=True)
    (workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")


class TestCodingSessionAPI:
    async def test_create_coding_session(self, client: AsyncClient, test_workspace: Path) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, _nodes = await _create_task_with_plan(client)
        resp = await client.post(
            "/api/v1/agent/coding-sessions",
            json={"plan_id": plan_id, "auto_continue_budget": 2},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "active"
        assert data["mode"] == "coding"
        assert data["plan_id"] == plan_id
        assert data["auto_continue_budget"] == 2
        assert "list_eligible_nodes" in data["capabilities"]

    async def test_get_coding_session(self, client: AsyncClient, test_workspace: Path) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, _nodes = await _create_task_with_plan(client)
        create = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = create.json()["session_id"]
        resp = await client.get(f"/api/v1/agent/coding-sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["session_id"] == session_id

    async def test_get_coding_session_includes_main_agent_container_metadata(
        self,
        client: AsyncClient,
        test_workspace: Path,
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, _nodes = await _create_task_with_plan(client)
        create = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = create.json()["session_id"]

        resp = await client.get(f"/api/v1/agent/coding-sessions/{session_id}")

        assert resp.status_code == 200
        assert resp.json()["main_agent_container"] is not None
        assert resp.json()["main_agent_container"]["container_id"] is not None

    async def test_create_coding_session_starts_main_agent_container_by_default(
        self,
        client: AsyncClient,
        test_workspace: Path,
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, _nodes = await _create_task_with_plan(client)

        resp = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})

        assert resp.status_code == 200, resp.text
        metadata = resp.json()["main_agent_container"]
        assert metadata is not None
        assert metadata["status"] == "running"
        assert metadata["git_baseline_revision"] == "a" * 40
        assert metadata["workspace_path"] == str(test_workspace.resolve())

    async def test_disable_main_agent_container_skips_startup(
        self,
        client: AsyncClient,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BRIDLE_DISABLE_MAIN_AGENT_CONTAINER", "1")
        _setup_git(test_workspace)
        _task_id, plan_id, _nodes = await _create_task_with_plan(client)

        resp = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})

        assert resp.status_code == 200, resp.text
        assert resp.json()["main_agent_container"] is None

    async def test_git_preflight_failure_marks_session_failed(
        self,
        client: AsyncClient,
        test_workspace: Path,
    ) -> None:
        import shutil
        shutil.rmtree(test_workspace / ".git", ignore_errors=True)
        _task_id, plan_id, _nodes = await _create_task_with_plan(client)

        resp = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "failed"
        assert data["main_agent_container"] is None


class TestEligibleNodesAPI:
    async def test_first_node_eligible_second_blocked_by_dependency(
        self, client: AsyncClient, test_workspace: Path
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = session.json()["session_id"]
        resp = await client.get(f"/api/v1/agent/coding-sessions/{session_id}/eligible-nodes")
        assert resp.status_code == 200
        body = resp.json()
        eligible_ids = {n["plan_node_id"] for n in body["eligible_nodes"]}
        blocked_ids = {n["plan_node_id"] for n in body["blocked_nodes"]}
        assert "n1" in eligible_ids
        assert "n2" in blocked_ids

    async def test_archived_node_never_eligible(
        self, client: AsyncClient, db: AsyncSession, test_workspace: Path
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        node_id = nodes[0]["id"]
        result = await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))
        node = result.scalar_one()
        node.status = "archived"
        await db.commit()

        session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = session.json()["session_id"]
        resp = await client.get(f"/api/v1/agent/coding-sessions/{session_id}/eligible-nodes")
        eligible_ids = {n["plan_node_id"] for n in resp.json()["eligible_nodes"]}
        assert "n1" not in eligible_ids


@pytest.mark.asyncio
async def test_recent_failed_runs_endpoint(
    client: AsyncClient,
    db: AsyncSession,
    test_workspace: Path,
) -> None:
    """F17: return recent failed/timed_out runs only, newest first."""
    _setup_git(test_workspace)
    _task_id, plan_id, nodes = await _create_task_with_plan(client)
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    session_id = session.json()["session_id"]
    node_id = nodes[0]["id"]
    node_row = (await db.execute(select(NodeRecord).where(NodeRecord.id == node_id))).scalar_one()

    failed_old = NodeAgentRunRecord(
        session_id=session_id,
        node_id=node_id,
        plan_node_id="n1",
        status="failed",
        phase="finalizing",
        attempt=1,
        blocked_reason="tests_failed",
        result_summary="Tests failed (exit=1): pytest old.py",
        finished_at=datetime(2026, 1, 1, 10, 0, 0),
    )
    failed_new = NodeAgentRunRecord(
        session_id=session_id,
        node_id=node_id,
        plan_node_id="n1",
        status="failed",
        phase="finalizing",
        attempt=2,
        blocked_reason="tests_failed",
        result_summary="Tests failed (exit=4): pytest new.py",
        finished_at=datetime(2026, 1, 2, 10, 0, 0),
    )
    completed = NodeAgentRunRecord(
        session_id=session_id,
        node_id=node_id,
        plan_node_id="n1",
        status="completed",
        phase="finalizing",
        attempt=1,
        finished_at=datetime(2026, 1, 3, 10, 0, 0),
    )
    db.add_all([failed_old, failed_new, completed])
    await db.flush()
    db.add(
        NodeAgentResultRecord(
            run_id=failed_new.id,
            node_id=node_id,
            status="failed",
            result_type="tests_failed",
            summary=failed_new.result_summary or "",
            recommended_next_action="needs_test_files_or_fix",
        )
    )
    await db.commit()

    resp = await client.get(
        f"/api/v1/agent/coding-sessions/{session_id}/recent-failed-runs",
        params={"limit": "3"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["result_summary"].startswith("Tests failed (exit=4)")
    assert body[0]["title"] == node_row.title
    assert body[0]["result_type"] == "tests_failed"
    assert body[1]["result_summary"].startswith("Tests failed (exit=1)")


class TestSelectNodeAPI:
    async def test_select_eligible_node_creates_run(
        self, client: AsyncClient, test_workspace: Path
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = session.json()["session_id"]
        node_id = nodes[0]["id"]
        resp = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            json={
                "intent": "select_node",
                "node_id": node_id,
                "reason": "Ready to work",
                "expected_action": "create_proposal",
            },
        )
        assert resp.status_code == 200, resp.text
        run = resp.json()
        assert run["status"] == "queued"
        assert run["node_id"] == node_id
        assert run["session_id"] == session_id

    async def test_select_node_rejects_3rd_attempt(
        self, client: AsyncClient, db: AsyncSession, test_workspace: Path
    ) -> None:
        """F16: two prior runs exhaust node attempts; third select returns 409."""
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = session.json()["session_id"]
        node_id = nodes[0]["id"]

        for _ in range(2):
            db.add(
                NodeAgentRunRecord(
                    session_id=session_id,
                    node_id=node_id,
                    plan_node_id="n1",
                    status="failed",
                    phase="finalizing",
                    attempt=1,
                    blocked_reason="tests_failed",
                )
            )
        await db.commit()

        resp = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            json={
                "intent": "select_node",
                "node_id": node_id,
                "reason": "third try",
                "expected_action": "create_proposal",
            },
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "node_attempts_exhausted"

    async def test_select_non_eligible_node_returns_structured_error(
        self, client: AsyncClient, test_workspace: Path
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = session.json()["session_id"]
        n2_id = nodes[1]["id"]
        resp = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            json={
                "intent": "select_node",
                "node_id": n2_id,
                "reason": "Should fail",
                "expected_action": "create_proposal",
            },
        )
        assert resp.status_code == 409
        err = resp.json()
        assert err["code"] == "node_not_eligible"

    async def test_session_budget_exceeded(
        self, client: AsyncClient, test_workspace: Path
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        session = await client.post(
            "/api/v1/agent/coding-sessions",
            json={"plan_id": plan_id, "auto_continue_budget": 0},
        )
        session_id = session.json()["session_id"]
        resp = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            json={
                "intent": "select_node",
                "node_id": nodes[0]["id"],
                "reason": "No budget",
                "expected_action": "create_proposal",
            },
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "session_budget_exceeded"


class TestNodeAgentRunHeartbeat:
    async def test_heartbeat_updates_last_heartbeat_at(
        self, client: AsyncClient, db: AsyncSession, test_workspace: Path
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = session.json()["session_id"]
        sel = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            json={
                "intent": "select_node",
                "node_id": nodes[0]["id"],
                "reason": "go",
                "expected_action": "create_proposal",
            },
        )
        run_id = sel.json()["run_id"]
        result = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
        run_rec = result.scalar_one()
        run_rec.status = "running"
        await db.commit()
        hb = await client.post(
            f"/api/v1/node-agent-runs/{run_id}/heartbeat",
            json={
                "run_id": run_id,
                "node_id": nodes[0]["id"],
                "status": "running",
                "phase": "editing",
                "message": "Working",
                "progress": 0.5,
            },
        )
        assert hb.status_code == 200, hb.text
        assert hb.json()["last_heartbeat_at"] is not None

        result = await db.execute(select(NodeRecord).where(NodeRecord.id == nodes[0]["id"]))
        node = result.scalar_one()
        assert node.status != "completed"

    async def test_heartbeat_wrong_node_id_rejected(
        self, client: AsyncClient, test_workspace: Path
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = session.json()["session_id"]
        sel = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            json={
                "intent": "select_node",
                "node_id": nodes[0]["id"],
                "reason": "go",
                "expected_action": "create_proposal",
            },
        )
        run_id = sel.json()["run_id"]
        resp = await client.post(
            f"/api/v1/node-agent-runs/{run_id}/heartbeat",
            json={
                "run_id": run_id,
                "node_id": "wrong-node",
                "status": "running",
                "phase": "editing",
                "message": "x",
            },
        )
        assert resp.status_code == 422


class TestNodeAgentWatchdog:
    async def test_stale_heartbeat_marks_timed_out(
        self, client: AsyncClient, db: AsyncSession, test_workspace: Path
    ) -> None:
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = session.json()["session_id"]
        sel = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            json={
                "intent": "select_node",
                "node_id": nodes[0]["id"],
                "reason": "go",
                "expected_action": "create_proposal",
            },
        )
        run_id = sel.json()["run_id"]
        result = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
        run = result.scalar_one()
        stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).replace(tzinfo=None)
        run.status = "running"
        run.last_heartbeat_at = stale
        await db.commit()

        closed = await NodeAgentWatchdog.scan_and_close_stale(db)
        assert closed >= 1

        result = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
        run = result.scalar_one()
        assert run.status == "timed_out"
        assert run.blocked_reason == "heartbeat_stale"

    async def test_blocked_timeout_publishes_sse_events(
        self, client: AsyncClient, db: AsyncSession, test_workspace: Path
    ) -> None:
        from bridle.events.bus import EventBus

        EventBus._reset_instance()
        _setup_git(test_workspace)
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
        session_id = session.json()["session_id"]
        sel = await client.post(
            f"/api/v1/agent/coding-sessions/{session_id}/select-node",
            json={
                "intent": "select_node",
                "node_id": nodes[0]["id"],
                "reason": "go",
                "expected_action": "create_proposal",
            },
        )
        run_id = sel.json()["run_id"]
        result = await db.execute(select(NodeAgentRunRecord).where(NodeAgentRunRecord.id == run_id))
        run = result.scalar_one()
        stale = (datetime.now(timezone.utc) - timedelta(seconds=400)).replace(tzinfo=None)
        run.status = "blocked"
        run.last_heartbeat_at = stale
        await db.commit()

        closed = await NodeAgentWatchdog.scan_and_close_stale(db)
        assert closed >= 1

        event_types = {event.type for event in EventBus.instance()._ring}
        assert "node_agent_run_updated" in event_types
        assert "node_status_changed" in event_types


class TestPlanChangeProposalAPI:
    async def test_create_plan_change_proposal(self, client: AsyncClient) -> None:
        _task_id, plan_id, nodes = await _create_task_with_plan(client)
        resp = await client.post(
            "/api/v1/plan-change-proposals",
            json={
                "plan_id": plan_id,
                "proposal_type": "plan_change",
                "change_set": [
                    {
                        "operation": "update_node",
                        "node_id": "n1",
                        "fields": {"goal": "Updated goal"},
                        "reason": "Clarify scope",
                    }
                ],
                "risk_level": "low",
                "requires_human_review": True,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "proposed"

    async def test_cannot_apply_before_approval(self, client: AsyncClient) -> None:
        _task_id, plan_id, _nodes = await _create_task_with_plan(client)
        create = await client.post(
            "/api/v1/plan-change-proposals",
            json={
                "plan_id": plan_id,
                "proposal_type": "plan_change",
                "change_set": [
                    {
                        "operation": "update_node",
                        "node_id": "n1",
                        "fields": {"goal": "X"},
                        "reason": "r",
                    }
                ],
                "risk_level": "low",
            },
        )
        proposal_id = create.json()["proposal_id"]
        resp = await client.post(f"/api/v1/plan-change-proposals/{proposal_id}/apply")
        assert resp.status_code == 409

    async def test_approved_proposal_applies(self, client: AsyncClient) -> None:
        _task_id, plan_id, _nodes = await _create_task_with_plan(client)
        create = await client.post(
            "/api/v1/plan-change-proposals",
            json={
                "plan_id": plan_id,
                "proposal_type": "plan_change",
                "change_set": [
                    {
                        "operation": "update_node",
                        "node_id": "n1",
                        "fields": {"goal": "Applied goal"},
                        "reason": "r",
                    }
                ],
                "risk_level": "low",
            },
        )
        proposal_id = create.json()["proposal_id"]
        approve = await client.post(f"/api/v1/plan-change-proposals/{proposal_id}/approve")
        assert approve.status_code == 200
        assert approve.json()["status"] == "approved"
        apply_resp = await client.post(f"/api/v1/plan-change-proposals/{proposal_id}/apply")
        assert apply_resp.status_code == 200
        assert apply_resp.json()["status"] == "applied"
