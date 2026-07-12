"""HTTP contracts for semantic map layers exposed to the frontend."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_code_entities_blind_spots_and_boundaries_endpoints(client, test_workspace: Path) -> None:
    """Bootstrap UI can read code entities, open blind spots, and boundary conflicts."""
    root = test_workspace / "map-layers"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text(
        "from missing import x\n\n\ndef run():\n    return getattr(__import__('os'), 'name')\n",
        encoding="utf-8",
    )
    (root / "pkg" / "tests").mkdir()
    (root / "pkg" / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")

    project = (await client.post("/api/v1/projects/open", json={"path": str(root)})).json()

    entities = await client.get(f"/api/v1/projects/{project['id']}/map/code-entities", params={"limit": 200})
    assert entities.status_code == 200
    items = entities.json()["items"]
    assert any(item["kind"] == "test" for item in items)
    assert any(item["kind"] in ("file", "function") for item in items)

    blind = await client.get(f"/api/v1/projects/{project['id']}/map/blind-spots")
    assert blind.status_code == 200
    assert isinstance(blind.json()["items"], list)

    boundaries = await client.get(f"/api/v1/projects/{project['id']}/map/boundaries")
    assert boundaries.status_code == 200
    body = boundaries.json()
    assert "items" in body
    assert "debt_nodes" in body

    relations = await client.get(f"/api/v1/projects/{project['id']}/map/code-relations", params={"limit": 50})
    assert relations.status_code == 200
    assert "items" in relations.json()
    assert "has_more" in relations.json()

    annotations = await client.get(
        f"/api/v1/projects/{project['id']}/map/semantic-annotations",
        params={"limit": 50},
    )
    assert annotations.status_code == 200
    assert "items" in annotations.json()

    modules = await client.get(f"/api/v1/projects/{project['id']}/map/module-candidates")
    assert modules.status_code == 200
    module_items = modules.json()["items"]
    assert any(item["module_id"] == "pkg" for item in module_items)
    pkg = next(item for item in module_items if item["module_id"] == "pkg")
    assert pkg["is_execution_boundary"] is False

    confirmed = await client.post(
        f"/api/v1/projects/{project['id']}/map/module-candidates/{pkg['id']}/status",
        json={"status": "confirmed", "actor": "human"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["is_execution_boundary"] is True

    interfaces = await client.get(f"/api/v1/projects/{project['id']}/map/module-interface-candidates")
    assert interfaces.status_code == 200
    assert "items" in interfaces.json()

    mocks = await client.get(f"/api/v1/projects/{project['id']}/map/interface-mocks")
    assert mocks.status_code == 200
    assert "items" in mocks.json()
