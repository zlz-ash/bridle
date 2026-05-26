"""Tests for execution closure — node dependency blocking, run lifecycle, and evidence."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient


def _make_plan_with_deps() -> dict:
    """Plan with two nodes: n1 (no deps) → n2 (depends on n1)."""
    return dict(
        goal="Dependency test",
        nodes=[
            {
                "id": "n1", "title": "Setup", "goal": "Setup code", "node_type": "code_change",
                "depends_on": [], "files": [], "tests": ["echo setup"], "metrics": {},
                "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
            },
            {
                "id": "n2", "title": "Verify", "goal": "Verify code", "node_type": "test_validation",
                "depends_on": ["n1"], "files": [], "tests": ["echo verify"], "metrics": {},
                "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
            },
        ],
    )


class TestDependencyBlocking:
    async def test_node_blocked_when_dependency_not_completed(self, client: AsyncClient) -> None:
        """n2 should be blocked when n1 hasn't been completed yet."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Dep Block"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_with_deps()
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        assert import_resp.status_code == 200

        nodes = import_resp.json()["nodes"]
        n2_id = [n["id"] for n in nodes if n["plan_node_id"] == "n2"][0]

        # Try to run n2 — should be blocked
        resp = await client.post(f"/api/v1/nodes/{n2_id}/run")
        assert resp.status_code == 409
        assert "blocked" in resp.json()["message"].lower()

    async def test_node_unblocked_after_dependency_completed(self, client: AsyncClient) -> None:
        """n2 should be runnable after n1 is completed."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Dep Unblock"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_with_deps()
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        nodes = import_resp.json()["nodes"]
        n1_id = [n["id"] for n in nodes if n["plan_node_id"] == "n1"][0]
        n2_id = [n["id"] for n in nodes if n["plan_node_id"] == "n2"][0]

        # Run n1 first — should succeed
        resp1 = await client.post(f"/api/v1/nodes/{n1_id}/run")
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "completed"

        # Now n2 should be runnable
        resp2 = await client.post(f"/api/v1/nodes/{n2_id}/run")
        assert resp2.status_code == 200


class TestRunLifecycle:
    async def test_run_creates_evidence(self, client: AsyncClient) -> None:
        """Running a node should create evidence records."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Evidence"})
        task_id = task_resp.json()["id"]

        plan = dict(
            goal="Evidence test",
            nodes=[
                {
                    "id": "n1", "title": "N", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo ok"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                }
            ],
        )
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        node_id = import_resp.json()["nodes"][0]["id"]

        run_resp = await client.post(f"/api/v1/nodes/{node_id}/run")
        assert run_resp.status_code == 200

        # Check report has evidence
        report_resp = await client.get(f"/api/v1/nodes/{node_id}/report")
        assert report_resp.status_code == 200
        report = report_resp.json()
        assert len(report["evidences"]) >= 1

    async def test_failed_run_marks_node_failed(self, client: AsyncClient) -> None:
        """Running a node with a failing test should mark it as failed."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Fail"})
        task_id = task_resp.json()["id"]

        plan = dict(
            goal="Fail test",
            nodes=[
                {
                    "id": "n1", "title": "N", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["exit 1"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                }
            ],
        )
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        node_id = import_resp.json()["nodes"][0]["id"]

        run_resp = await client.post(f"/api/v1/nodes/{node_id}/run")
        assert run_resp.status_code == 200
        assert run_resp.json()["status"] == "failed"

    async def test_node_run_uses_sandbox_network_policy(self, client: AsyncClient) -> None:
        """The legacy node run endpoint should reject network-marked commands through sandbox."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Sandbox Network"})
        task_id = task_resp.json()["id"]
        plan = dict(
            goal="Network sandbox",
            nodes=[
                {
                    "id": "n1", "title": "N", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [],
                    "tests": ["echo wget requires_network"],
                    "metrics": {}, "constraints": {"c": True}, "review_checks": [],
                    "expected_outputs": {},
                }
            ],
        )
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        node_id = import_resp.json()["nodes"][0]["id"]

        run_resp = await client.post(f"/api/v1/nodes/{node_id}/run")

        assert run_resp.status_code == 200
        assert run_resp.json()["status"] == "failed"

    async def test_multiple_runs_recorded(self, client: AsyncClient) -> None:
        """Multiple runs of the same node should be recorded."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Multi"})
        task_id = task_resp.json()["id"]

        plan = dict(
            goal="Multi run",
            nodes=[
                {
                    "id": "n1", "title": "N", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo ok"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                }
            ],
        )
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        node_id = import_resp.json()["nodes"][0]["id"]

        # Run twice
        await client.post(f"/api/v1/nodes/{node_id}/run")
        await client.post(f"/api/v1/nodes/{node_id}/run")

        runs_resp = await client.get(f"/api/v1/nodes/{node_id}/runs")
        assert runs_resp.status_code == 200
        assert len(runs_resp.json()) >= 2


class TestGraphConsistency:
    async def test_graph_uses_plan_node_ids(self, client: AsyncClient) -> None:
        """Graph edges should use plan_node_id consistently (not DB UUIDs)."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Graph"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_with_deps()
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        resp = await client.get(f"/api/v1/tasks/{task_id}/graph")
        assert resp.status_code == 200
        data = resp.json()

        # Edges should use plan_node_ids
        assert len(data["edges"]) == 1
        edge = data["edges"][0]
        assert edge["source"] == "n1"
        assert edge["target"] == "n2"
