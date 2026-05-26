"""Tests for current-plan.json file mirror: consistency, write-after-mutation, resync."""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from httpx import AsyncClient


def _make_plan_payload(**overrides) -> dict:
    base = dict(
        goal="Mirror test",
        nodes=[
            {
                "id": "n1", "title": "Node 1", "goal": "G", "node_type": "code_change",
                "depends_on": [], "files": [], "tests": ["pytest"], "metrics": {},
                "constraints": {"c": True}, "review_checks": [], "expected_outputs": {},
            }
        ],
    )
    base.update(overrides)
    return base


class TestJsonFileMirror:
    """After import, PATCH, PUT, the current-plan.json must reflect DB state."""

    async def test_import_writes_json(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "JSON Import"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(goal="Check JSON")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        from bridle.config import get_config
        config = get_config()
        assert config.current_plan_path.exists()

        data = json.loads(config.current_plan_path.read_text(encoding="utf-8"))
        assert data["goal"] == "Check JSON"

    async def test_patch_updates_json(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "JSON Patch"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(goal="Before patch")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        patch_data = {"update_nodes": [{"id": "n1", "title": "Updated"}]}
        patch_resp = await client.patch("/api/v1/plan/current", json=patch_data)
        assert patch_resp.status_code == 200

        from bridle.config import get_config
        config = get_config()
        data = json.loads(config.current_plan_path.read_text(encoding="utf-8"))
        assert any(n["title"] == "Updated" for n in data["nodes"])

    async def test_put_updates_json(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "JSON PUT"})
        task_id = task_resp.json()["id"]

        plan1 = _make_plan_payload(goal="Old")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)

        plan2 = _make_plan_payload(goal="New")
        await client.put("/api/v1/plan/current", json=plan2)

        from bridle.config import get_config
        config = get_config()
        data = json.loads(config.current_plan_path.read_text(encoding="utf-8"))
        assert data["goal"] == "New"


class TestJsonResync:
    """If JSON is manually modified, the next GET should resync from DB."""

    async def test_resync_on_get(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Resync"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(goal="Original")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        from bridle.config import get_config
        config = get_config()

        tampered = {"goal": "TAMPERED", "nodes": []}
        config.current_plan_path.write_text(json.dumps(tampered), encoding="utf-8")

        resp = await client.get("/api/v1/plan/current")
        assert resp.status_code == 200
        assert resp.json()["goal"] == "Original"

        data = json.loads(config.current_plan_path.read_text(encoding="utf-8"))
        assert data["goal"] == "Original"

    async def test_no_resync_when_consistent(self, client: AsyncClient) -> None:
        """If JSON is consistent with DB, no resync needed."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "Consistent"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(goal="Same")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        from bridle.config import get_config
        config = get_config()

        mtime_before = config.current_plan_path.stat().st_mtime

        import time
        time.sleep(0.05)
        await client.get("/api/v1/plan/current")

        mtime_after = config.current_plan_path.stat().st_mtime
        assert mtime_before == mtime_after


class TestPutSummary:
    """PUT should generate plan-summary.json for the old plan."""

    async def test_put_generates_summary_file(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Summary File"})
        task_id = task_resp.json()["id"]

        plan1 = _make_plan_payload(goal="Old plan")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan1)

        plan2 = _make_plan_payload(goal="New plan")
        await client.put("/api/v1/plan/current", json=plan2)

        from bridle.config import get_config
        config = get_config()
        assert config.plan_summary_path.exists()

        summary = json.loads(config.plan_summary_path.read_text(encoding="utf-8"))
        assert summary["goal"] == "Old plan"
        assert "replaced_at" in summary
        assert "node_count" in summary

    async def test_patch_does_not_generate_summary(self, client: AsyncClient) -> None:
        """PATCH should NOT generate a plan-summary.json."""
        task_resp = await client.post("/api/v1/tasks", json={"title": "No Summary"})
        task_id = task_resp.json()["id"]

        plan = _make_plan_payload(goal="Patch test")
        await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)

        from bridle.config import get_config
        config = get_config()

        if config.plan_summary_path.exists():
            config.plan_summary_path.unlink()

        patch_data = {"update_nodes": [{"id": "n1", "title": "Changed"}]}
        await client.patch("/api/v1/plan/current", json=patch_data)

        assert not config.plan_summary_path.exists()
