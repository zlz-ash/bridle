"""Tests for Task and Node API endpoints."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.plan_helpers import ensure_plan_payload


def _make_plan_payload(**overrides) -> dict:
    """Build a minimal plan import payload (without task_id/task_title)."""
    base = dict(
        goal="Test plan",
        nodes=[
            {
                "id": "n1",
                "title": "Node 1",
                "goal": "Do something",
                "node_type": "code_change",
                "depends_on": [],
                "files": ["src/main.py"],
                "tests": ["pytest tests/"],
                "metrics": {"coverage": 80},
                "constraints": {"no_print": True},
                "review_checks": ["no secrets"],
                "expected_outputs": {"exit_code": 0},
            }
        ],
    )
    base.update(overrides)
    return base


class TestTaskAPI:
    async def test_create_task(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/tasks", json={"title": "API Task", "goal": "Test via API"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "API Task"
        assert data["status"] == "created"
        assert "id" in data

    async def test_create_task_empty_title_fails(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/tasks", json={"title": ""})
        assert resp.status_code == 422

    async def test_list_tasks(self, client: AsyncClient) -> None:
        # Create a task first
        await client.post("/api/v1/tasks", json={"title": "Task 1"})
        resp = await client.get("/api/v1/tasks")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1

    async def test_get_task(self, client: AsyncClient) -> None:
        create_resp = await client.post("/api/v1/tasks", json={"title": "Get Task"})
        task_id = create_resp.json()["id"]

        resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Get Task"

    async def test_get_task_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/tasks/nonexistent-id")
        assert resp.status_code == 404


class TestPlanImportAPI:
    async def test_import_plan(self, client: AsyncClient) -> None:
        # Create task first
        task_resp = await client.post("/api/v1/tasks", json={"title": "Plan Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload()
        resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        assert resp.status_code == 200
        data = resp.json()
        assert "plan_id" in data
        assert len(data["nodes"]) == 1
        assert data["task_id"] == task_id
        assert "complexity_validation" in data

    async def test_import_plan_too_granular_triggers_negotiation_then_succeeds(
        self, client: AsyncClient
    ) -> None:
        """estimated_minutes below min triggers stubbed negotiation; import ends pending."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Granular Plan"})
        task_id = task_resp.json()["id"]
        plan = _make_plan_payload(
            nodes=[
                {
                    "id": "n-small",
                    "title": "Small step",
                    "goal": "Implement with clear acceptance criteria for reviewers",
                    "node_type": "code_change",
                    "depends_on": [],
                    "files": ["src/a.py"],
                    "tests": ["pytest"],
                    "metrics": {},
                    "constraints": {"c": True},
                    "review_checks": [],
                    "expected_outputs": {},
                    "estimated_minutes": 15,
                }
            ],
        )
        resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        assert resp.status_code == 200
        data = resp.json()
        validation = {item["node_id"]: item for item in data["complexity_validation"]}
        assert validation["n-small"]["ok"] is True
        node = next(n for n in data["nodes"] if n["plan_node_id"] == "n-small")
        assert node["status"] == "pending"

    async def test_import_plan_invalid_dep(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Bad Plan"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            nodes=[
                {
                    "id": "n1",
                    "title": "N1",
                    "goal": "G",
                    "node_type": "code_change",
                    "depends_on": ["nonexistent"],
                    "files": [],
                    "tests": ["pytest"],
                    "metrics": {},
                    "constraints": {"c": True},
                    "review_checks": [],
                    "expected_outputs": {},
                }
            ],
        )
        resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        assert resp.status_code == 422

    async def test_get_task_graph(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Graph Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            goal="Graph test",
            nodes=[
                {
                    "id": "n1", "title": "A", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                },
                {
                    "id": "n2", "title": "B", "goal": "G", "node_type": "test_validation",
                    "depends_on": ["n1"], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                },
            ],
        )
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        resp = await client.get(f"/api/v1/tasks/{task_id}/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 2
        assert len(data["edges"]) == 1
        assert data["edges"][0]["source"] == "n1"

    async def test_get_task_graph_bidirectional_contracts(self, client: AsyncClient) -> None:
        """Graph edges include both source→target and target→source interface contracts."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Bidir Graph"})
        task_id = task_resp.json()["id"]

        plan = {
            "goal": "Bidirectional test",
            "nodes": [
                {
                    "id": "n1", "title": "A", "goal": "G", "node_type": "code_change",
                    "depends_on": [],
                    "interfaces": {
                        "exposes": [
                            {"name": "auth_context", "fields": [{"name": "user_id", "type": "string"}], "endpoints": [{"name": "get_user", "method": "GET", "path": "/users/me"}]}
                        ],
                        "consumes": [
                            {"node_id": "n2", "interface_name": "review_api", "fields": ["score"], "endpoints": ["get_review"]}
                        ],
                    },
                },
                {
                    "id": "n2", "title": "B", "goal": "G", "node_type": "test_validation",
                    "depends_on": ["n1"],
                    "interfaces": {
                        "exposes": [
                            {"name": "review_api", "fields": [{"name": "score", "type": "int"}], "endpoints": [{"name": "get_review", "method": "GET", "path": "/review"}]}
                        ],
                        "consumes": [
                            {"node_id": "n1", "interface_name": "auth_context", "fields": ["user_id"], "endpoints": ["get_user"]}
                        ],
                    },
                },
            ],
        }
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan))

        resp = await client.get(f"/api/v1/tasks/{task_id}/graph")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["edges"]) == 1
        edge = data["edges"][0]
        assert edge["source"] == "n1"
        assert edge["target"] == "n2"
        assert "interface_contracts" in edge
        contracts = edge["interface_contracts"]
        assert len(contracts) == 2

        # Find each direction
        by_dir = {c["direction"]: c for c in contracts}
        assert "source_to_target" in by_dir
        assert "target_to_source" in by_dir

        st = by_dir["source_to_target"]
        assert st["consumer"] == "n2"
        assert st["provider"] == "n1"
        assert st["interface_name"] == "auth_context"
        assert st["fields"] == ["user_id"]
        assert st["endpoints"] == ["get_user"]

        ts = by_dir["target_to_source"]
        assert ts["consumer"] == "n1"
        assert ts["provider"] == "n2"
        assert ts["interface_name"] == "review_api"
        assert ts["fields"] == ["score"]
        assert ts["endpoints"] == ["get_review"]

    async def test_get_task_graph_no_contracts(self, client: AsyncClient) -> None:
        """Graph edge without interface contracts returns no interface_contracts key."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "No Contract Graph"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            goal="No contracts",
            nodes=[
                {"id": "n1", "title": "A", "goal": "G", "node_type": "code_change",
                 "depends_on": [], "files": [], "tests": ["echo t"], "metrics": {},
                 "constraints": {"c": True}, "review_checks": [], "expected_outputs": {}},
                {"id": "n2", "title": "B", "goal": "G", "node_type": "test_validation",
                 "depends_on": ["n1"], "files": [], "tests": ["echo t"], "metrics": {},
                 "constraints": {"c": True}, "review_checks": [], "expected_outputs": {}},
            ],
        )
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        resp = await client.get(f"/api/v1/tasks/{task_id}/graph")
        assert resp.status_code == 200
        edge = resp.json()["edges"][0]
        assert "interface_contracts" not in edge or edge["interface_contracts"] == []

    async def test_import_plan_archives_previous(self, client: AsyncClient) -> None:
        """Importing a new plan should archive the old one."""
        # Create task and import first plan
        task_resp = await client.post("/api/v1/tasks", json={"title": "Archive Task"})
        task_id = task_resp.json()["id"]

        plan1 = _make_plan_payload(goal="First plan")
        resp1 = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)
        assert resp1.status_code == 200
        plan1_id = resp1.json()["plan_id"]

        # Import second plan — should archive the first
        plan2 = _make_plan_payload(goal="Second plan")
        resp2 = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan2)
        assert resp2.status_code == 200
        plan2_id = resp2.json()["plan_id"]

        # GET /plan/current should return the second plan
        current = await client.get("/api/v1/plan/current")
        assert current.status_code == 200
        assert current.json()["id"] == plan2_id
        assert current.json()["goal"] == "Second plan"

    async def test_get_current_plan_none(self, client: AsyncClient) -> None:
        """GET /plan/current returns 404 when no plan exists."""
        resp = await client.get("/api/v1/plan/current")
        assert resp.status_code == 404


class TestNodeAPI:
    async def test_get_node(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Node Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            goal="G",
            nodes=[
                {
                    "id": "n1", "title": "My Node", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["pytest"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                }
            ],
        )
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        node_id = import_resp.json()["nodes"][0]["id"]

        resp = await client.get(f"/api/v1/nodes/{node_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "My Node"

    async def test_get_node_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/nodes/nonexistent")
        assert resp.status_code == 404

    async def test_run_node(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Run Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            goal="G",
            nodes=[
                {
                    "id": "n1", "title": "Run Node", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo ok"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                }
            ],
        )
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        node_id = import_resp.json()["nodes"][0]["id"]

        resp = await client.post(f"/api/v1/nodes/{node_id}/run")
        assert resp.status_code == 200
        data = resp.json()
        assert "run_id" in data

    async def test_get_node_runs(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Runs Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            goal="G",
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

        # Run once
        await client.post(f"/api/v1/nodes/{node_id}/run")

        resp = await client.get(f"/api/v1/nodes/{node_id}/runs")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_get_node_report(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Report Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            goal="G",
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

        await client.post(f"/api/v1/nodes/{node_id}/run")

        resp = await client.get(f"/api/v1/nodes/{node_id}/report")
        assert resp.status_code == 200
        data = resp.json()
        assert "node" in data
        assert "runs" in data
        assert "evidences" in data
        assert "summary" in data

    async def test_archived_node_not_visible(self, client: AsyncClient) -> None:
        """Nodes from archived plans should not be accessible via GET /nodes/{id}."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Archive Node Task"})
        task_id = task_resp.json()["id"]

        # Import first plan
        plan1 = _make_plan_payload(
            goal="First",
            nodes=[
                {
                    "id": "n1", "title": "Old Node", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo ok"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                }
            ],
        )
        resp1 = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)
        old_node_id = resp1.json()["nodes"][0]["id"]

        # Import second plan (archives first)
        plan2 = _make_plan_payload(
            goal="Second",
            nodes=[
                {
                    "id": "n2", "title": "New Node", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo ok"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                }
            ],
        )
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan2)

        # Old node should no longer be accessible
        resp = await client.get(f"/api/v1/nodes/{old_node_id}")
        assert resp.status_code == 404
