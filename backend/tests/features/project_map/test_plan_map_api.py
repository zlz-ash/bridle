"""HTTP contracts for local plan patches and progressive map reads."""
from __future__ import annotations

from pathlib import Path

import pytest

from bridle.features.project_map.plan_service import PlanService
from bridle.features.project_map.store import ProjectPlanStore


def _node(node_id: str, *, parent_id: str | None = None, order: int = 0) -> dict:
    """Build a valid API node; hierarchy input exits as PlanPatchSchema-compatible JSON."""
    return {
        "id": node_id,
        "parent_id": parent_id,
        "order": order,
        "title": node_id,
        "goal": f"Complete {node_id}",
        "node_type": "code_change",
    }


@pytest.mark.asyncio
async def test_patch_uses_existing_plan_service_as_the_only_edit_entry(
    client,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch a project map; request input exits through PlanService.patch_current exactly once."""
    root = test_workspace / "plan-service-entry"
    root.mkdir()
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()
    calls: list[tuple[str, int]] = []

    async def fake_patch(db, project_id, data):
        """Capture the unique edit boundary; service inputs exit as a marker response."""
        calls.append((project_id, len(data.add_nodes)))
        return {"entry": "plan-service"}

    monkeypatch.setattr(PlanService, "patch_current", staticmethod(fake_patch))

    response = await client.patch(
        f"/api/v1/projects/{project['id']}/map",
        json={"add_nodes": [_node("root")]},
    )

    assert response.json() == {"entry": "plan-service"}
    assert calls == [(project["id"], 1)]


@pytest.mark.asyncio
async def test_patch_and_progressive_reads_share_project_plan_db(client, test_workspace: Path) -> None:
    """Patch one project map; project/pagination inputs exit through bounded read endpoints."""
    root = test_workspace / "map-project"
    root.mkdir()
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()

    patched = await client.patch(
        f"/api/v1/projects/{project['id']}/map",
        json={
            "add_nodes": [
                _node("root"),
                _node("child-0", parent_id="root", order=0),
                _node("child-1", parent_id="root", order=1),
                _node("child-2", parent_id="root", order=2),
            ],
            "replace_dependencies": [
                {"node_id": "child-1", "depends_on": ["child-0"]},
            ],
        },
    )
    overview = await client.get(f"/api/v1/projects/{project['id']}/map/overview")
    first = await client.get(
        f"/api/v1/projects/{project['id']}/map/children",
        params={"parent_id": "root", "limit": 2},
    )
    second = await client.get(
        f"/api/v1/projects/{project['id']}/map/children",
        params={"parent_id": "root", "limit": 2, "cursor": first.json()["next_cursor"]},
    )
    node = await client.get(f"/api/v1/projects/{project['id']}/map/nodes/child-1")
    search = await client.get(
        f"/api/v1/projects/{project['id']}/map/search", params={"query": "child", "limit": 2},
    )
    graph = await client.get(
        f"/api/v1/projects/{project['id']}/map/subgraph/root", params={"depth": 1, "limit": 10},
    )
    changes = await client.get(
        f"/api/v1/projects/{project['id']}/map/changes", params={"after_seq": 0, "limit": 20},
    )

    assert patched.status_code == 200
    assert overview.json()["scan_status"] == "ready"
    assert overview.json()["can_edit_plan"] is True
    assert overview.json()["plan_node_count"] == 4
    assert [item["id"] for item in first.json()["items"]] == ["child-0", "child-1"]
    assert [item["id"] for item in second.json()["items"]] == ["child-2"]
    assert node.json()["depends_on"] == ["child-0"]
    assert len(search.json()["items"]) == 2
    assert {item["id"] for item in graph.json()["nodes"]} == {
        "root", "child-0", "child-1", "child-2",
    }
    assert changes.json()["last_seq"] >= 5


@pytest.mark.asyncio
async def test_patch_returns_structured_running_conflict(client, test_workspace: Path) -> None:
    """Patch a running node; project/patch input exits as HTTP 409 with stable error code."""
    root = test_workspace / "running-map-project"
    root.mkdir()
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()
    await client.patch(
        f"/api/v1/projects/{project['id']}/map",
        json={"add_nodes": [_node("active")]},
    )
    ProjectPlanStore(root, project_id=project["id"]).set_node_status("active", "running")

    response = await client.patch(
        f"/api/v1/projects/{project['id']}/map",
        json={"update_nodes": [{"id": "active", "title": "blocked"}]},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "node_running_immutable"


@pytest.mark.asyncio
async def test_map_endpoints_require_registered_available_project(client) -> None:
    """Read an unknown project map; project ID input exits with not_found instead of global current plan."""
    response = await client.get("/api/v1/projects/missing/map/overview")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_arbitration_api_lists_and_resolves_pending_objections(client, test_workspace: Path) -> None:
    """Resolve a map objection over HTTP; project input exits with readiness restored."""
    root = test_workspace / "arbitration-api"
    root.mkdir()
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()
    objection = ProjectPlanStore(root, project_id=project["id"]).create_map_objection(
        objection_type="ambiguous_responsibility",
        related_node_ids=["code-1"],
        evidence={"reason": "unclear owner"},
        suggested_resolution={"action": "accept"},
    )

    pending = await client.get(f"/api/v1/projects/{project['id']}/map/arbitration")
    resolved = await client.post(
        f"/api/v1/projects/{project['id']}/map/arbitration/{objection['id']}/resolve",
        json={"decision": "accepted", "resolution": {"summary": "accepted"}},
    )
    overview = await client.get(f"/api/v1/projects/{project['id']}/map/overview")

    assert pending.status_code == 200
    assert pending.json()["items"][0]["id"] == objection["id"]
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert overview.json()["scan_status"] == "ready"


@pytest.mark.asyncio
async def test_execution_refresh_api_updates_only_changed_paths(client, test_workspace: Path) -> None:
    """Record execution refresh over HTTP; changed path input exits as code-map update."""
    root = test_workspace / "execution-refresh-api"
    (root / "src").mkdir(parents=True)
    (root / "src" / "keep.py").write_text("KEEP = True\n", encoding="utf-8")
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()
    (root / "src" / "added.py").write_text("ADDED = True\n", encoding="utf-8")

    response = await client.post(
        f"/api/v1/projects/{project['id']}/map/execution-refresh",
        json={
            "execution_node_id": "node-1",
            "changed_paths": ["src/added.py"],
            "execution_summary": "Implemented node.",
            "test_summary": "pytest passed",
        },
    )

    assert response.status_code == 200
    assert response.json()["refreshed_paths"] == ["src/added.py"]
    assert response.json()["execution_summary"] == "Implemented node."


@pytest.mark.asyncio
async def test_path_slice_returns_entities_for_changed_file(client, test_workspace: Path) -> None:
    """Path slice endpoint returns entities scoped to one file."""
    root = test_workspace / "path-slice-api"
    (root / "src").mkdir(parents=True)
    (root / "src" / "target.py").write_text("def fn():\n    return 1\n", encoding="utf-8")
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()

    response = await client.get(
        f"/api/v1/projects/{project['id']}/map/path-slice",
        params={"path": "src/target.py"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == "src/target.py"
    paths = {item["path"] for item in body["entities"]}
    assert "src/target.py" in paths
    assert any(path.endswith("::fn") for path in paths)

