"""Tests for agent proposal API endpoints."""
from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from bridle.engine.blocker import BlockResult
from tests.plan_helpers import ensure_plan_payload


class TestProposalCreateAPI:
    """API-level tests for POST /nodes/{node_id}/agent/proposals."""

    def _expose_dict(self, name="auth_context"):
        return {"name": name, "fields": [{"name": "user_id", "type": "string"}], "endpoints": [{"name": "get_user", "method": "GET", "path": "/users/me"}]}

    def _consume_dict(self, node_id="n1", interface_name="auth_context"):
        return {"node_id": node_id, "interface_name": interface_name, "fields": ["user_id"], "endpoints": ["get_user"]}

    async def test_create_proposal_success(self, client: AsyncClient) -> None:
        """POST generates a proposal for an active node."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Proposal Task"})
        task_id = task_resp.json()["id"]

        plan = {
            "goal": "Test",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "files": ["src/main.py"], "tests": ["pytest tests/"], "metrics": {},
                 "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                 "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            ],
        }
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan))
        node_id = import_resp.json()["nodes"][0]["id"]

        resp = await client.post(f"/api/v1/nodes/{node_id}/agent/proposals", json={
            "instruction": "Implement code_change node",
        })
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "proposed"
        assert data["source"] == "agent"
        assert data["plan_node_id"] == "n1"
        assert "proposal" in data
        assert "summary" in data["proposal"]
        assert "file_patches" in data["proposal"]
        assert "tests_to_run" in data["proposal"]

    async def test_create_proposal_node_not_found(self, client: AsyncClient) -> None:
        """POST returns 404 for non-existent node."""
        resp = await client.post("/api/v1/nodes/nonexistent/agent/proposals", json={
            "instruction": "Do something",
        })
        assert resp.status_code == 404

    async def test_create_proposal_archived_node(self, client: AsyncClient) -> None:
        """POST returns 404 for a node in an archived plan."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Archive Proposal"})
        task_id = task_resp.json()["id"]

        plan1 = {
            "goal": "Old",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "files": ["src/a.py"]},
            ],
        }
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan1))
        old_node_id = import_resp.json()["nodes"][0]["id"]

        # Archive by importing a new plan
        plan2 = {
            "goal": "New",
            "nodes": [
                {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change",
                 "files": ["src/b.py"]},
            ],
        }
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan2))

        resp = await client.post(f"/api/v1/nodes/{old_node_id}/agent/proposals", json={
            "instruction": "Do something",
        })
        assert resp.status_code == 404

    async def test_create_proposal_empty_instruction_fails(self, client: AsyncClient) -> None:
        """POST returns 422 for empty instruction."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Empty Instruction"})
        task_id = task_resp.json()["id"]

        plan = {
            "goal": "Test",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "files": ["src/main.py"]},
            ],
        }
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan))
        node_id = import_resp.json()["nodes"][0]["id"]

        resp = await client.post(f"/api/v1/nodes/{node_id}/agent/proposals", json={
            "instruction": "",
        })
        assert resp.status_code == 422

    async def test_create_proposal_blocked_node_fails(self, client: AsyncClient) -> None:
        """POST returns 409 for a blocked node."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Blocked Proposal"})
        task_id = task_resp.json()["id"]

        plan = {
            "goal": "Test",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "G1 with clear acceptance criteria for reviewers",
                 "node_type": "code_change",
                 "files": ["src/main.py"], "tests": ["pytest tests/test_main.py -q"],
                 "constraints": {"c": True},
                 "metrics": {}, "review_checks": [], "expected_outputs": {}},
            ],
        }
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan))
        node_id = import_resp.json()["nodes"][0]["id"]

        with patch(
            "bridle.services.agent_gateway.Blocker.check",
            return_value=BlockResult(blocked=True, reason="Missing test definitions"),
        ):
            resp = await client.post(f"/api/v1/nodes/{node_id}/agent/proposals", json={
                "instruction": "Do something",
            })
        assert resp.status_code == 409

    async def test_create_proposal_allowed_files_only_node_files(self, client: AsyncClient) -> None:
        """The allowed_files in the proposal response must only contain node.files."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Files Boundary"})
        task_id = task_resp.json()["id"]

        plan = {
            "goal": "Test",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "files": ["src/a.py", "src/b.py"], "tests": ["pytest tests/"],
                 "metrics": {}, "constraints": {"c": True}, "review_checks": [], "expected_outputs": {}},
            ],
        }
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan))
        node_id = import_resp.json()["nodes"][0]["id"]

        resp = await client.post(f"/api/v1/nodes/{node_id}/agent/proposals", json={
            "instruction": "Do something",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["allowed_files"]) == {"src/a.py", "src/b.py"}

    async def test_create_proposal_no_mutation(self, client: AsyncClient) -> None:
        """Generating a proposal does NOT change node status or source files."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "No Mutation"})
        task_id = task_resp.json()["id"]

        plan = {
            "goal": "Test",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "files": ["src/main.py"], "tests": ["pytest tests/"],
                 "metrics": {}, "constraints": {"c": True}, "review_checks": [], "expected_outputs": {}},
            ],
        }
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan))
        node_id = import_resp.json()["nodes"][0]["id"]

        # Snapshot node state before proposal
        before = await client.get(f"/api/v1/nodes/{node_id}")
        before_status = before.json()["status"]

        await client.post(f"/api/v1/nodes/{node_id}/agent/proposals", json={
            "instruction": "Do something",
        })

        after = await client.get(f"/api/v1/nodes/{node_id}")
        assert after.json()["status"] == before_status


class TestProposalListAPI:
    """API-level tests for GET /nodes/{node_id}/agent/proposals."""

    def _expose_dict(self, name="auth_context"):
        return {"name": name, "fields": [{"name": "user_id", "type": "string"}], "endpoints": [{"name": "get_user", "method": "GET", "path": "/users/me"}]}

    async def test_list_proposals_empty(self, client: AsyncClient) -> None:
        """GET returns empty list when no proposals exist."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Empty Proposals"})
        task_id = task_resp.json()["id"]

        plan = {
            "goal": "Test",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "files": ["src/main.py"]},
            ],
        }
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan))
        node_id = import_resp.json()["nodes"][0]["id"]

        resp = await client.get(f"/api/v1/nodes/{node_id}/agent/proposals")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_proposals_after_create(self, client: AsyncClient) -> None:
        """GET returns proposals after they are created."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "List Proposals"})
        task_id = task_resp.json()["id"]

        plan = {
            "goal": "Test",
            "nodes": [
                {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
                 "files": ["src/main.py"], "tests": ["pytest tests/"],
                 "metrics": {}, "constraints": {"c": True}, "review_checks": [], "expected_outputs": {}},
            ],
        }
        import_resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=ensure_plan_payload(plan))
        node_id = import_resp.json()["nodes"][0]["id"]

        # Create a proposal
        await client.post(f"/api/v1/nodes/{node_id}/agent/proposals", json={
            "instruction": "First proposal",
        })

        resp = await client.get(f"/api/v1/nodes/{node_id}/agent/proposals")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["status"] == "proposed"
        assert data[0]["instruction"] == "First proposal"

    async def test_list_proposals_node_not_found(self, client: AsyncClient) -> None:
        """GET returns 404 for non-existent node."""
        resp = await client.get("/api/v1/nodes/nonexistent/agent/proposals")
        assert resp.status_code == 404
