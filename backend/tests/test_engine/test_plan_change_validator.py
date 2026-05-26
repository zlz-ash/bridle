"""Tests for PlanChangeValidator."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from bridle.engine.plan_change_validator import PlanChangeValidator
from bridle.models.node import NodeRecord


class TestPlanChangeValidator:
    def test_rejects_status_mutation(self) -> None:
        errors = PlanChangeValidator.validate_allowed_fields({"status": "completed"})
        assert errors

    def test_rejects_forbidden_test_command(self) -> None:
        errors = PlanChangeValidator.validate_tests(["rm -rf /"])
        assert errors

    def test_rejects_tests_as_string(self) -> None:
        errors = PlanChangeValidator.validate_tests("rm -rf /")
        assert any("list" in e.lower() for e in errors)

    def test_rejects_tests_as_dict(self) -> None:
        errors = PlanChangeValidator.validate_tests({"cmd": "rm"})
        assert errors

    def test_rejects_tests_with_non_string_item(self) -> None:
        errors = PlanChangeValidator.validate_tests([123])
        assert errors

    def test_rejects_tests_with_empty_string(self) -> None:
        errors = PlanChangeValidator.validate_tests([""])
        assert errors

    def test_allows_valid_fields(self) -> None:
        assert PlanChangeValidator.validate_allowed_fields({"goal": "new", "tests": ["pytest tests/"]}) == []

    def test_rejects_add_node_operation(self) -> None:
        errors = PlanChangeValidator.validate_operation({
            "operation": "add_node",
            "node_id": "n9",
            "fields": {},
        })
        assert errors

    def test_rejects_remove_node_operation(self) -> None:
        errors = PlanChangeValidator.validate_operation({
            "operation": "remove_node",
            "node_id": "n1",
        })
        assert errors

    def test_rejects_update_tests_operation(self) -> None:
        errors = PlanChangeValidator.validate_operation({
            "operation": "update_tests",
            "node_id": "n1",
            "fields": {"tests": ["pytest"]},
        })
        assert errors


@pytest.mark.asyncio
async def test_apply_with_string_tests_does_not_mutate_db(client: AsyncClient, db) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "No Mutate"})
    imp = await client.post(
        f"/api/v1/tasks/{task_resp.json()['id']}/plan/import",
        json={
            "goal": "G",
            "nodes": [{
                "id": "n1", "title": "N", "goal": "G", "node_type": "code_change",
                "depends_on": [], "files": ["a.py"], "tests": ["pytest tests/"],
                "metrics": {}, "constraints": {"x": True}, "review_checks": [],
                "expected_outputs": {},
            }],
        },
    )
    plan_id = imp.json()["plan_id"]
    create = await client.post(
        "/api/v1/plan-change-proposals",
        json={
            "plan_id": plan_id,
            "proposal_type": "plan_change",
            "change_set": [{
                "operation": "update_node",
                "node_id": "n1",
                "fields": {"tests": "rm -rf /"},
                "reason": "bad",
            }],
            "risk_level": "low",
        },
    )
    assert create.status_code == 409

    result = await db.execute(select(NodeRecord).where(NodeRecord.plan_node_id == "n1"))
    node = result.scalar_one()
    assert node.tests == ["pytest tests/"]
