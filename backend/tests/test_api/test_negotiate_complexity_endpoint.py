"""POST /plans/{plan_id}/negotiate-complexity API."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.engine.node_complexity_policy import validate_node_complexity
from bridle.models.node import NodeRecord
from bridle.models.plan import PlanRecord
from bridle.models.task import TaskRecord
from bridle.schemas.complexity_negotiation import validate_negotiation_decision
from bridle.schemas.node import NodeImportSchema
from bridle.services.complexity_negotiation_service import (
    ComplexityNegotiationService,
    clear_runtime_negotiation_cache,
)


def _too_low_node_payload(**overrides) -> dict:
    base = {
        "id": "n1",
        "title": "N",
        "goal": "Work item with clear acceptance criteria for QA",
        "node_type": "code_change",
        "depends_on": [],
        "files": ["src/a.py"],
        "tests": ["pytest -q"],
        "metrics": {},
        "constraints": {"c": True},
        "review_checks": [],
        "expected_outputs": {},
        "estimated_minutes": 15,
    }
    base.update(overrides)
    return base


async def _seed_blocked_too_low_plan(db: AsyncSession) -> str:
    """Insert a blocked node whose estimate lives only in metrics.complexity."""
    task = TaskRecord(title="Renegotiate blocked", status="planned")
    db.add(task)
    await db.flush()

    plan = PlanRecord(task_id=task.id, goal="G", status="active")
    db.add(plan)
    await db.flush()

    schema = NodeImportSchema.model_validate(_too_low_node_payload())
    complexity = validate_node_complexity(schema).to_dict()
    assert complexity["ok"] is False

    node = NodeRecord(
        plan_id=plan.id,
        plan_node_id="n1",
        title=schema.title,
        goal=schema.goal,
        node_type=schema.node_type,
        order=0,
        depends_on=schema.depends_on,
        files=schema.files,
        tests=schema.tests,
        metrics={"complexity": complexity},
        constraints=schema.constraints,
        review_checks=schema.review_checks,
        expected_outputs=schema.expected_outputs,
        interfaces=schema.interfaces.model_dump(),
        status="blocked",
    )
    db.add(node)
    await db.commit()
    return plan.id


@pytest.mark.asyncio
async def test_negotiate_complexity_returns_200(client: AsyncClient) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Renegotiate"})
    task_id = task_resp.json()["id"]
    plan = {
        "goal": "G",
        "nodes": [
            {
                "id": "n1",
                "title": "N",
                "goal": "Work item with clear acceptance criteria for QA",
                "node_type": "code_change",
                "depends_on": [],
                "files": ["src/a.py"],
                "tests": ["pytest -q"],
                "metrics": {},
                "constraints": {"c": True},
                "review_checks": [],
                "expected_outputs": {},
                "estimated_minutes": 60,
            }
        ],
    }
    imported = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
    plan_id = imported.json()["plan_id"]
    clear_runtime_negotiation_cache()

    resp = await client.post(f"/api/v1/plans/{plan_id}/negotiate-complexity")
    assert resp.status_code == 200
    assert resp.json()["renegotiated"] is False


@pytest.mark.asyncio
async def test_negotiate_detects_too_low_from_db(
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    plan_id = await _seed_blocked_too_low_plan(db)
    clear_runtime_negotiation_cache(plan_id)

    resp = await client.post(f"/api/v1/plans/{plan_id}/negotiate-complexity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["renegotiated"] is True
    assert body["action"] == "expand"

    nodes_resp = await client.get("/api/v1/plan/current")
    node = nodes_resp.json()["nodes"][0]
    assert node["status"] == "pending"
    estimate = (node.get("metrics") or {}).get("complexity", {}).get("estimate") or {}
    assert estimate.get("estimated_minutes", 0) >= 60


@pytest.mark.asyncio
async def test_negotiate_returns_422_when_ai_cant_fix(
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    plan_id = await _seed_blocked_too_low_plan(db)
    clear_runtime_negotiation_cache(plan_id)

    still_too_low = validate_negotiation_decision(
        {
            "action": "expand",
            "expand": {
                "node_id": "n1",
                "new_goal": "Still too small for complexity rules",
                "new_acceptance_scope": "Does not meet integration bar",
                "new_estimated_minutes": 15,
                "additional_files": [],
            },
        }
    )
    with patch.object(
        ComplexityNegotiationService,
        "negotiate",
        new_callable=AsyncMock,
        return_value=still_too_low,
    ):
        resp = await client.post(f"/api/v1/plans/{plan_id}/negotiate-complexity")

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "plan_not_executable"
    issues = body.get("details", {}).get("last_issues") or []
    issue_codes = {
        issue
        for row in issues
        for issue in (row.get("issues") or [])
    }
    assert "node_too_granular:estimated_minutes_too_low" in issue_codes


@pytest.mark.asyncio
async def test_negotiate_complexity_cache_hit(
    client: AsyncClient,
    db: AsyncSession,
) -> None:
    plan_id = await _seed_blocked_too_low_plan(db)
    clear_runtime_negotiation_cache(plan_id)

    call_count = 0
    original_negotiate = ComplexityNegotiationService.negotiate

    async def _counting_negotiate(self, **kwargs):
        nonlocal call_count
        call_count += 1
        return await original_negotiate(self, **kwargs)

    with patch.object(ComplexityNegotiationService, "negotiate", _counting_negotiate):
        resp1 = await client.post(f"/api/v1/plans/{plan_id}/negotiate-complexity")
        resp2 = await client.post(f"/api/v1/plans/{plan_id}/negotiate-complexity")

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp2.json() == resp1.json()
    assert call_count == 1
