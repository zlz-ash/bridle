"""Tests for Plan API endpoints: PATCH, PUT, summary, current-plan.json."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient


def _make_plan_payload(**overrides) -> dict:
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


class TestPlanPatchAPI:
    async def test_patch_update_node(self, client: AsyncClient) -> None:
        """PATCH can update node fields."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Patch Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload()
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        # Patch: update node title
        patch_data = {
            "update_nodes": [
                {"id": "n1", "title": "Updated Node 1"}
            ]
        }
        resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert resp.status_code == 200
        data = resp.json()
        # Find the updated node by plan_node_id
        updated = [n for n in data["nodes"] if n["plan_node_id"] == "n1"]
        assert len(updated) == 1
        assert updated[0]["title"] == "Updated Node 1"

    async def test_patch_updates_container_boundary_fields(self, client: AsyncClient) -> None:
        """PATCH can update container boundary fields and returns them."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Boundary Patch Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload()
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        resp = await client.patch("/api/v1/plan/current", json={
            "update_nodes": [
                {
                    "id": "n1",
                    "read_set": ["src/context.py"],
                    "write_set": ["src/main.py"],
                    "readonly_context": ["docs/api.md"],
                    "conflict_contributions": [
                        {
                            "aggregate_target": "src/router.py",
                            "contribution_path": ".bridle/aggregate/src/router.py/n1.json",
                        }
                    ],
                    "container_policy": {"network_mode": "bridge", "timeout_seconds": 90},
                }
            ]
        })

        assert resp.status_code == 200
        node = [n for n in resp.json()["nodes"] if n["plan_node_id"] == "n1"][0]
        assert node["read_set"] == ["src/context.py"]
        assert node["write_set"] == ["src/main.py"]
        assert node["container_policy"]["network_mode"] == "bridge"

    async def test_patch_add_node(self, client: AsyncClient) -> None:
        """PATCH can add new nodes."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Add Node Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload()
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        # Patch: add a new node
        patch_data = {
            "add_nodes": [
                {
                    "id": "n2",
                    "title": "New Node",
                    "goal": "New goal",
                    "node_type": "test_validation",
                    "depends_on": ["n1"],
                    "files": [],
                    "tests": ["pytest new/"],
                    "metrics": {},
                    "constraints": {"c": True},
                    "review_checks": [],
                    "expected_outputs": {},
                }
            ]
        }
        resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["nodes"]) == 2

    async def test_patch_remove_node(self, client: AsyncClient) -> None:
        """PATCH can remove (archive) nodes."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Remove Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
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

        # Patch: remove n1 (n2 depends on it — should auto-cleanup)
        patch_data = {"remove_node_ids": ["n1"]}
        resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert resp.status_code == 200
        data = resp.json()
        # n1 should be archived (not in active nodes)
        active_pnids = [n["plan_node_id"] for n in data["nodes"]]
        assert "n1" not in active_pnids
        assert "n2" in active_pnids
        # n2's depends_on should have n1 removed
        n2 = [n for n in data["nodes"] if n["plan_node_id"] == "n2"][0]
        assert "n1" not in n2["depends_on"]

    async def test_patch_replace_dependencies(self, client: AsyncClient) -> None:
        """PATCH can replace a node's dependency list."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Dep Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            nodes=[
                {
                    "id": "n1", "title": "A", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                },
                {
                    "id": "n2", "title": "B", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                },
                {
                    "id": "n3", "title": "C", "goal": "G", "node_type": "test_validation",
                    "depends_on": ["n1"], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                },
            ],
        )
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        # Replace n3's dependencies from [n1] to [n2]
        patch_data = {
            "replace_dependencies": [
                {"node_id": "n3", "depends_on": ["n2"]}
            ]
        }
        resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert resp.status_code == 200
        data = resp.json()
        n3 = [n for n in data["nodes"] if n["plan_node_id"] == "n3"][0]
        assert n3["depends_on"] == ["n2"]

    async def test_patch_rejects_circular_dependency(self, client: AsyncClient) -> None:
        """PATCH rejects changes that would create a circular dependency."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Cycle Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            nodes=[
                {
                    "id": "n1", "title": "A", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                },
                {
                    "id": "n2", "title": "B", "goal": "G", "node_type": "code_change",
                    "depends_on": ["n1"], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                },
            ],
        )
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        # Try to create n1 → n2 → n1 cycle
        patch_data = {
            "replace_dependencies": [
                {"node_id": "n1", "depends_on": ["n2"]}
            ]
        }
        resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert resp.status_code == 422
        detail = resp.json()["message"].lower()
        assert "ircular" in detail or "cycle" in detail

    async def test_patch_no_active_plan(self, client: AsyncClient) -> None:
        """PATCH returns 404 when no active plan exists."""
        patch_data = {"update_nodes": [{"id": "n1", "title": "X"}]}
        resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert resp.status_code == 404

    async def test_patch_change_node_type_to_blocked(self, client: AsyncClient) -> None:
        """Changing node_type to one that requires missing fields sets node to blocked."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Type Change"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            nodes=[
                {
                    "id": "n1", "title": "A", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                }
            ],
        )
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        # Change to review_gate — but review_checks is empty → blocked
        patch_data = {
            "update_nodes": [{"id": "n1", "node_type": "review_gate"}]
        }
        resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert resp.status_code == 200
        node = [n for n in resp.json()["nodes"] if n["plan_node_id"] == "n1"][0]
        assert node["status"] == "blocked"
        assert node["node_type"] == "review_gate"

    async def test_patch_change_node_type_with_required_fields(self, client: AsyncClient) -> None:
        """Changing node_type with all required fields does not block."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Type OK"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            nodes=[
                {
                    "id": "n1", "title": "A", "goal": "G", "node_type": "code_change",
                    "depends_on": [], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": ["check1"], "expected_outputs": {},
                }
            ],
        )
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        # Change to review_gate — review_checks is present → not blocked
        patch_data = {
            "update_nodes": [{"id": "n1", "node_type": "review_gate"}]
        }
        resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert resp.status_code == 200
        node = [n for n in resp.json()["nodes"] if n["plan_node_id"] == "n1"][0]
        assert node["node_type"] == "review_gate"
        assert node["status"] != "blocked"

    async def test_patch_unknown_dependency_rejected(self, client: AsyncClient) -> None:
        """PATCH with a dependency referencing a non-existent node is rejected."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Bad Dep"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload()
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        # Add node with unknown dependency
        patch_data = {
            "add_nodes": [
                {
                    "id": "n2", "title": "New", "goal": "G", "node_type": "code_change",
                    "depends_on": ["nonexistent"], "files": [], "tests": ["echo t"], "metrics": {},
                    "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
                }
            ]
        }
        resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert resp.status_code == 422


class TestPlanPutAPI:
    async def test_put_replace_plan(self, client: AsyncClient) -> None:
        """PUT fully replaces the current plan."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Replace Task"})
        task_id = task_resp.json()["id"]

        # Import first plan
        plan1 = _make_plan_payload(goal="First plan")
        resp1 = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)
        assert resp1.status_code == 200

        # Replace with second plan
        plan2 = _make_plan_payload(goal="Second plan")
        resp2 = await client.put("/api/v1/plan/current", json=plan2)
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["goal"] == "Second plan"

    async def test_put_generates_summary(self, client: AsyncClient) -> None:
        """PUT generates a plan-summary.json for the old plan."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Summary Task"})
        task_id = task_resp.json()["id"]

        # Import first plan
        plan1 = _make_plan_payload(goal="Old plan")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)

        # Replace — should generate summary
        plan2 = _make_plan_payload(goal="New plan")
        resp = await client.put("/api/v1/plan/current", json=plan2)
        assert resp.status_code == 200
        data = resp.json()
        # Should have replaced_summary
        assert "replaced_summary" in data
        summary = data["replaced_summary"]
        assert summary["goal"] == "Old plan"
        assert summary["node_count"] >= 1

    async def test_put_no_active_plan(self, client: AsyncClient) -> None:
        """PUT returns 404 when no active plan exists."""
        plan = _make_plan_payload()
        resp = await client.put("/api/v1/plan/current", json=plan)
        assert resp.status_code == 404


class TestContainerBoundaryImportAPI:
    async def test_import_returns_container_boundary_fields(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Boundary Import Task"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(
            aggregate_files=[
                {
                    "target_path": "src/router.py",
                    "contribution_dir": ".bridle/aggregate/src/router.py",
                    "merge_strategy": "json_list",
                    "owner": "main_agent",
                    "contributors": ["n1"],
                }
            ],
            nodes=[
                {
                    "id": "n1",
                    "title": "Node 1",
                    "goal": "Do something",
                    "node_type": "code_change",
                    "depends_on": [],
                    "files": ["src/main.py"],
                    "read_set": ["src/context.py"],
                    "write_set": ["src/main.py"],
                    "readonly_context": ["docs/api.md"],
                    "tests": ["pytest tests/"],
                    "metrics": {"coverage": 80},
                    "constraints": {"no_print": True},
                    "review_checks": ["no secrets"],
                    "expected_outputs": {"exit_code": 0},
                    "conflict_contributions": [
                        {
                            "aggregate_target": "src/router.py",
                            "contribution_path": ".bridle/aggregate/src/router.py/n1.json",
                        }
                    ],
                    "container_policy": {"network_mode": "bridge", "timeout_seconds": 120},
                }
            ],
        )

        resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        assert resp.status_code == 200
        node = resp.json()["nodes"][0]
        assert node["read_set"] == ["src/context.py"]
        assert node["write_set"] == ["src/main.py"]
        assert node["container_policy"]["timeout_seconds"] == 120


class TestPlanSummaryAPI:
    async def test_get_summary(self, client: AsyncClient) -> None:
        """GET /plan/current/summary returns the last replacement summary."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Get Summary"})
        task_id = task_resp.json()["id"]

        # Import and replace
        plan1 = _make_plan_payload(goal="Old")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)

        plan2 = _make_plan_payload(goal="New")
        await client.put("/api/v1/plan/current", json=plan2)

        resp = await client.get("/api/v1/plan/current/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["goal"] == "Old"
        assert "replaced_at" in data
        assert "node_count" in data

    async def test_get_summary_none(self, client: AsyncClient) -> None:
        """GET /plan/current/summary returns 404 when no summary exists."""
        resp = await client.get("/api/v1/plan/current/summary")
        assert resp.status_code == 404


class TestInterfaceValidationAPI:
    """API-level tests for interface contract validation on import/replace/patch."""

    async def _create_task_and_import(self, client, nodes):
        task_resp = await client.post("/api/v1/tasks", json={"title": "Interface Test"})
        task_id = task_resp.json()["id"]
        resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json={"goal": "Test", "nodes": nodes})
        return task_id, resp

    def _expose_dict(self, name="auth_context"):
        return {"name": name, "fields": [{"name": "user_id", "type": "string"}], "endpoints": [{"name": "get_user", "method": "GET", "path": "/users/me"}]}

    def _consume_dict(self, node_id="n1", interface_name="auth_context"):
        return {"node_id": node_id, "interface_name": interface_name, "fields": ["user_id"], "endpoints": ["get_user"]}

    # --- Import validation ---

    async def test_import_valid_interfaces_succeeds(self, client: AsyncClient) -> None:
        nodes = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "test_validation",
             "depends_on": ["n1"],
             "interfaces": {"exposes": [], "consumes": [self._consume_dict()]}},
        ]
        _, resp = await self._create_task_and_import(client, nodes)
        assert resp.status_code == 200

    async def test_import_non_adjacent_consume_fails(self, client: AsyncClient) -> None:
        nodes = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change"},
            {"id": "n3", "title": "N3", "goal": "G3", "node_type": "test_validation",
             "depends_on": ["n2"],
             "interfaces": {"exposes": [], "consumes": [self._consume_dict(node_id="n1")]}},
        ]
        _, resp = await self._create_task_and_import(client, nodes)
        assert resp.status_code == 422

    async def test_import_unknown_interface_fails(self, client: AsyncClient) -> None:
        nodes = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict(name="only_this")], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "test_validation",
             "depends_on": ["n1"],
             "interfaces": {"exposes": [], "consumes": [self._consume_dict(interface_name="other")]}},
        ]
        _, resp = await self._create_task_and_import(client, nodes)
        assert resp.status_code == 422

    async def test_import_unknown_field_fails(self, client: AsyncClient) -> None:
        nodes = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "test_validation",
             "depends_on": ["n1"],
             "interfaces": {"exposes": [], "consumes": [{"node_id": "n1", "interface_name": "auth_context", "fields": ["nonexistent"], "endpoints": []}]}},
        ]
        _, resp = await self._create_task_and_import(client, nodes)
        assert resp.status_code == 422

    # --- Replace validation ---

    async def test_replace_valid_interfaces_succeeds(self, client: AsyncClient) -> None:
        nodes1 = [{"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change"}]
        task_id, _ = await self._create_task_and_import(client, nodes1)

        nodes2 = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "test_validation",
             "depends_on": ["n1"],
             "interfaces": {"exposes": [], "consumes": [self._consume_dict()]}},
        ]
        resp = await client.put("/api/v1/plan/current", json={"goal": "New", "nodes": nodes2})
        assert resp.status_code == 200

    async def test_replace_invalid_interfaces_fails(self, client: AsyncClient) -> None:
        nodes1 = [{"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change"}]
        task_id, _ = await self._create_task_and_import(client, nodes1)

        nodes2 = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change"},
            {"id": "n3", "title": "N3", "goal": "G3", "node_type": "test_validation",
             "depends_on": ["n2"],
             "interfaces": {"exposes": [], "consumes": [self._consume_dict(node_id="n1")]}},
        ]
        resp = await client.put("/api/v1/plan/current", json={"goal": "New", "nodes": nodes2})
        assert resp.status_code == 422

    # --- Patch validation ---

    async def test_patch_valid_interfaces_succeeds(self, client: AsyncClient) -> None:
        nodes = [{"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change"}]
        task_id, _ = await self._create_task_and_import(client, nodes)

        # Add n2 with valid interfaces
        resp = await client.patch("/api/v1/plan/current", json={
            "add_nodes": [
                {"id": "n2", "title": "N2", "goal": "G2", "node_type": "test_validation",
                 "depends_on": ["n1"],
                 "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
                {"id": "n3", "title": "N3", "goal": "G3", "node_type": "code_change",
                 "depends_on": ["n2"],
                 "interfaces": {"exposes": [], "consumes": [self._consume_dict(node_id="n2")]}},
            ],
        })
        assert resp.status_code == 200

    async def test_patch_invalid_interfaces_fails(self, client: AsyncClient) -> None:
        nodes = [{"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change"}]
        task_id, _ = await self._create_task_and_import(client, nodes)

        # Try to add n2 with invalid consume from non-adjacent n1
        resp = await client.patch("/api/v1/plan/current", json={
            "add_nodes": [
                {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change"},
                {"id": "n3", "title": "N3", "goal": "G3", "node_type": "test_validation",
                 "depends_on": ["n2"],
                 "interfaces": {"exposes": [], "consumes": [self._consume_dict(node_id="n1")]}},
            ],
        })
        assert resp.status_code == 422

    async def test_patch_update_interfaces_fails(self, client: AsyncClient) -> None:
        """PATCH with updated interfaces that are invalid should be rejected."""
        nodes = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change"},
        ]
        task_id, _ = await self._create_task_and_import(client, nodes)

        # Try to update n2's interfaces to consume from n1 — but n2 has no dependency on n1
        resp = await client.patch("/api/v1/plan/current", json={
            "update_nodes": [
                {"id": "n2", "interfaces": {"exposes": [], "consumes": [self._consume_dict(node_id="n1")]}},
            ],
        })
        assert resp.status_code == 422


class TestPatchRollback:
    """Verify that illegal PATCH does not change DB or current-plan.json mirror."""

    def _expose_dict(self, name="auth_context"):
        return {"name": name, "fields": [{"name": "user_id", "type": "string"}], "endpoints": [{"name": "get_user", "method": "GET", "path": "/users/me"}]}

    def _consume_dict(self, node_id="n1", interface_name="auth_context"):
        return {"node_id": node_id, "interface_name": interface_name, "fields": ["user_id"], "endpoints": ["get_user"]}

    async def test_illegal_patch_db_unchanged(self, client: AsyncClient) -> None:
        """Illegal interface PATCH returns 422 and DB state is unchanged."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Rollback DB"})
        task_id = task_resp.json()["id"]

        nodes = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change",
             "depends_on": ["n1"],
             "interfaces": {"exposes": [], "consumes": [self._consume_dict()]}},
        ]
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json={"goal": "Test", "nodes": nodes})

        before = await client.get("/api/v1/plan/current")
        assert before.status_code == 200
        before_nodes = {n["plan_node_id"]: n for n in before.json()["nodes"]}

        # Submit illegal PATCH: n2 consumes unknown interface from n1
        resp = await client.patch("/api/v1/plan/current", json={
            "update_nodes": [
                {"id": "n2", "interfaces": {
                    "exposes": [],
                    "consumes": [{"node_id": "n1", "interface_name": "BadName",
                                  "fields": ["user_id"], "endpoints": ["get_user"]}],
                }},
            ],
        })
        assert resp.status_code == 422

        after = await client.get("/api/v1/plan/current")
        assert after.status_code == 200
        after_nodes = {n["plan_node_id"]: n for n in after.json()["nodes"]}
        assert after_nodes["n2"]["interfaces"] == before_nodes["n2"]["interfaces"]

    async def test_illegal_patch_mirror_unchanged(self, client: AsyncClient, test_workspace) -> None:
        """Illegal interface PATCH does not alter the current-plan.json file mirror."""
        from bridle.config import get_config

        task_resp = await client.post("/api/v1/tasks", json={"title": "Rollback Mirror"})
        task_id = task_resp.json()["id"]

        nodes = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change",
             "depends_on": ["n1"],
             "interfaces": {"exposes": [], "consumes": [self._consume_dict()]}},
        ]
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json={"goal": "Test", "nodes": nodes})

        config = get_config()
        mirror_before = config.current_plan_path.read_text(encoding="utf-8")

        # Submit illegal PATCH with duplicate expose names
        resp = await client.patch("/api/v1/plan/current", json={
            "update_nodes": [
                {"id": "n1", "interfaces": {
                    "exposes": [
                        {"name": "dup", "fields": [], "endpoints": []},
                        {"name": "dup", "fields": [], "endpoints": []},
                    ],
                    "consumes": [],
                }},
            ],
        })
        assert resp.status_code == 422

        mirror_after = config.current_plan_path.read_text(encoding="utf-8")
        assert mirror_after == mirror_before, "current-plan.json was modified by a rolled-back PATCH"

    async def test_illegal_patch_partial_mutations_rolled_back(self, client: AsyncClient) -> None:
        """A multi-node PATCH that fails late must roll back all earlier mutations."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Partial Rollback"})
        task_id = task_resp.json()["id"]

        nodes = [
            {"id": "n1", "title": "N1", "goal": "G1", "node_type": "code_change",
             "interfaces": {"exposes": [self._expose_dict()], "consumes": []}},
            {"id": "n2", "title": "N2", "goal": "G2", "node_type": "code_change"},
        ]
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json={"goal": "Test", "nodes": nodes})

        before = await client.get("/api/v1/plan/current")
        before_nodes = {n["plan_node_id"]: n for n in before.json()["nodes"]}

        # Multi-operation: valid title update + invalid interfaces — both roll back
        resp = await client.patch("/api/v1/plan/current", json={
            "update_nodes": [
                {"id": "n1", "title": "Should Not Persist"},
                {"id": "n2", "interfaces": {
                    "exposes": [],
                    "consumes": [{"node_id": "n1", "interface_name": "unknown",
                                  "fields": [], "endpoints": []}],
                }},
            ],
        })
        assert resp.status_code == 422

        after = await client.get("/api/v1/plan/current")
        after_nodes = {n["plan_node_id"]: n for n in after.json()["nodes"]}
        assert after_nodes["n1"]["title"] == before_nodes["n1"]["title"]
        assert after_nodes["n2"]["interfaces"] == before_nodes["n2"]["interfaces"]
