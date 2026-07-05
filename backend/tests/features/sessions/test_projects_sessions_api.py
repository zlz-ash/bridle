"""API contracts for project selection and unified persisted sessions."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_open_project_is_idempotent_and_initializes_plan_db(client, test_workspace: Path) -> None:
    """Open one path twice; path input returns one project and initializes its SQLite map once."""
    project_root = test_workspace / "sample-project"
    project_root.mkdir()
    (project_root / "main.py").write_text("print('ok')\n", encoding="utf-8")

    empty = await client.get("/api/v1/projects")
    first = await client.post("/api/v1/projects/open", json={"path": str(project_root)})
    second = await client.post("/api/v1/projects/open", json={"path": str(project_root)})

    assert empty.status_code == 200
    assert empty.json() == {"projects": []}
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["scan_status"] == "ready"
    assert first.json()["can_chat"] is True
    assert first.json()["can_edit_plan"] is True
    assert first.json()["available"] is True
    assert (project_root / ".bridle" / "plan.db").is_file()


@pytest.mark.asyncio
async def test_session_defaults_to_planning_and_only_user_changes_role(client, test_workspace: Path) -> None:
    """Create a project session; project input exits planning and role changes require user authority."""
    project_root = test_workspace / "roles-project"
    project_root.mkdir()
    opened = await client.post("/api/v1/projects/open", json={"path": str(project_root)})
    project_id = opened.json()["id"]

    created = await client.post("/api/v1/sessions", json={"project_id": project_id})
    session_id = created.json()["id"]
    rejected = await client.post(
        f"/api/v1/sessions/{session_id}/role",
        json={"role": "executing", "actor": "agent", "confirmed": True},
    )
    executing = await client.post(
        f"/api/v1/sessions/{session_id}/role",
        json={"role": "executing", "actor": "user", "confirmed": True},
    )
    planning = await client.post(
        f"/api/v1/sessions/{session_id}/role",
        json={"role": "planning", "actor": "user", "confirmed": True},
    )

    assert created.status_code == 201
    assert created.json()["role"] == "planning"
    assert rejected.status_code == 403
    assert rejected.json()["code"] == "role_switch_forbidden"
    assert executing.status_code == 200
    assert executing.json()["role"] == "executing"
    assert planning.status_code == 200
    assert planning.json()["role"] == "planning"


@pytest.mark.asyncio
async def test_executing_requires_explicit_confirmation(client, test_workspace: Path) -> None:
    """Request execution without confirmation; role input exits with confirmation_required conflict."""
    project_root = test_workspace / "confirm-project"
    project_root.mkdir()
    project = (await client.post("/api/v1/projects/open", json={"path": str(project_root)})).json()
    session = (await client.post("/api/v1/sessions", json={"project_id": project["id"]})).json()

    response = await client.post(
        f"/api/v1/sessions/{session['id']}/role",
        json={"role": "executing", "actor": "user", "confirmed": False},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "execution_confirmation_required"


@pytest.mark.asyncio
async def test_missing_project_history_is_read_only(client, test_workspace: Path) -> None:
    """Lose a project's path after session creation; history remains readable while new writes fail."""
    project_root = test_workspace / "movable-project"
    project_root.mkdir()
    project = (await client.post("/api/v1/projects/open", json={"path": str(project_root)})).json()
    session = (await client.post("/api/v1/sessions", json={"project_id": project["id"]})).json()
    first = await client.post(
        f"/api/v1/sessions/{session['id']}/messages",
        json={"role": "user", "content": "before move"},
    )
    project_root.rename(test_workspace / "moved-project")

    rejected = await client.post(
        f"/api/v1/sessions/{session['id']}/messages",
        json={"role": "user", "content": "after move"},
    )
    history = await client.get(f"/api/v1/sessions/{session['id']}/messages")

    assert first.status_code == 201
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "project_unavailable_read_only"
    assert history.status_code == 200
    assert [message["content"] for message in history.json()] == ["before move"]


@pytest.mark.asyncio
async def test_session_requires_existing_project(client) -> None:
    """Create a session without a registered project; project ID input exits with not_found."""
    response = await client.post("/api/v1/sessions", json={"project_id": "missing"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_explicit_project_rescan_refreshes_the_existing_code_map(
    client,
    test_workspace: Path,
) -> None:
    """Rescan an open project; project ID input exits with refreshed SQLite code entities."""
    root = test_workspace / "rescan-project"
    root.mkdir()
    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()
    (root / "late.py").write_text("VALUE = 1\n", encoding="utf-8")

    response = await client.post(f"/api/v1/projects/{project['id']}/rescan")
    overview = await client.get(f"/api/v1/projects/{project['id']}/map/overview")

    assert response.status_code == 200
    assert response.json()["scan_status"] == "ready"
    assert response.json()["entity_count"] >= 1
    assert overview.json()["code_entity_count"] >= 1
