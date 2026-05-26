"""Tests for REST contract, replace preserving history, and delete node history."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient


def _make_plan_payload(**overrides) -> dict:
    base = dict(
        goal="Contract test",
        nodes=[
            {
                "id": "n1", "title": "N1", "goal": "G", "node_type": "code_change",
                "depends_on": [], "files": [], "tests": ["echo ok"], "metrics": {},
                "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
            }
        ],
    )
    base.update(overrides)
    return base


class TestRESTContract:
    """API endpoints must return unified error format and correct HTTP semantics."""

    async def test_404_has_unified_format(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/tasks/nonexistent-id")
        assert resp.status_code == 404
        body = resp.json()
        assert "code" in body
        assert "message" in body
        assert body["code"] == "not_found"

    async def test_422_validation_has_unified_format(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/tasks", json={"title": ""})
        assert resp.status_code == 422
        body = resp.json()
        assert "code" in body
        assert "message" in body
        assert body["code"] == "validation_error"

    async def test_404_plan_not_found_has_unified_format(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/plan/current")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "not_found"
        assert body["resource"] == "plan"

    async def test_500_has_unified_format(self) -> None:
        """Unhandled exceptions return unified error format with code=internal_error."""
        from bridle.app import create_app
        from unittest.mock import MagicMock

        app = create_app()
        handlers = app.exception_handlers
        assert Exception in handlers

        handler = handlers[Exception]
        request = MagicMock()
        exc = RuntimeError("boom")

        response = await handler(request, exc)
        assert response.status_code == 500
        body = json.loads(response.body.decode())
        assert body["code"] == "internal_error"
        assert "message" in body
        assert body["details"]["error_type"] == "RuntimeError"

    async def test_put_not_patch_semantics(self, client: AsyncClient) -> None:
        """PUT replaces entirely, PATCH modifies in place."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Semantics"})
        task_id = task_resp.json()["id"]

        plan1 = _make_plan_payload(goal="First")
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)
        plan1_id = import_resp.json()["plan_id"]

        plan2 = _make_plan_payload(goal="Second")
        put_resp = await client.put("/api/v1/plan/current", json=plan2)
        assert put_resp.status_code == 200
        assert put_resp.json()["goal"] == "Second"
        assert put_resp.json()["plan_id"] != plan1_id

    async def test_patch_same_plan_id(self, client: AsyncClient) -> None:
        """PATCH does NOT change the plan ID (same plan, modified in place)."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Same ID"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(goal="Original")
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        plan_id = import_resp.json()["plan_id"]

        patch_data = {"update_nodes": [{"id": "n1", "title": "Changed"}]}
        patch_resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert patch_resp.status_code == 200
        assert patch_resp.json()["plan_id"] == plan_id

    async def test_node_not_found_unified_format(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/nodes/nonexistent")
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "not_found"
        assert body["resource"] == "node"


class TestReplacePreservesHistory:
    """PUT replaces the plan but preserves old runs/evidence."""

    async def test_old_runs_preserved_after_replace(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "History"})
        task_id = task_resp.json()["id"]

        plan1 = _make_plan_payload(goal="First")
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)
        node_id = import_resp.json()["nodes"][0]["id"]

        run_resp = await client.post(f"/api/v1/nodes/{node_id}/run")
        assert run_resp.status_code == 200
        run_id = run_resp.json()["run_id"]

        plan2 = _make_plan_payload(goal="Second")
        await client.put("/api/v1/plan/current", json=plan2)

        assert (await client.get(f"/api/v1/nodes/{node_id}")).status_code == 404

        runs_resp = await client.get(f"/api/v1/nodes/{node_id}/runs")
        assert runs_resp.status_code == 200


class TestDeleteNodeHistory:
    """Deleting a node via PATCH should preserve its historical run/evidence records."""

    async def test_removed_node_not_in_current_view(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Delete Hist"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            nodes=[
                {
                    "id": "n1", "title": "A", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo ok"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                },
                {
                    "id": "n2", "title": "B", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo ok"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                },
            ],
        )
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        nodes = import_resp.json()["nodes"]
        n1_id = [n["id"] for n in nodes if n["plan_node_id"] == "n1"][0]

        await client.post(f"/api/v1/nodes/{n1_id}/run")

        patch_data = {"remove_node_ids": ["n1"]}
        patch_resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert patch_resp.status_code == 200

        active_pnids = [n["plan_node_id"] for n in patch_resp.json()["nodes"]]
        assert "n1" not in active_pnids

        get_resp = await client.get(f"/api/v1/nodes/{n1_id}")
        assert get_resp.status_code == 404


class TestFileWriteFailure:
    """When file mirror writes fail, DB state must remain valid."""

    async def test_import_succeeds_when_file_write_fails(self, client: AsyncClient) -> None:
        """Plan import returns 200 even if current-plan.json write fails."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "File Fail"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(goal="File fail test")
        with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        assert import_resp.status_code == 200
        assert import_resp.json()["goal"] == "File fail test"

    async def test_replace_succeeds_when_summary_write_fails(self, client: AsyncClient) -> None:
        """PUT returns 200 even if plan-summary.json write fails."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Summary Fail"})
        task_id = task_resp.json()["id"]

        plan1 = _make_plan_payload(goal="Old")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)

        plan2 = _make_plan_payload(goal="New")
        with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            put_resp = await client.put("/api/v1/plan/current", json=plan2)

        assert put_resp.status_code == 200
        assert put_resp.json()["goal"] == "New"
