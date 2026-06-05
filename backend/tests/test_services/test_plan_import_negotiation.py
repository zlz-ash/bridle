"""Plan import complexity negotiation integration."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from bridle.engine.node_complexity_policy import validate_plan_nodes
from bridle.schemas.complexity_negotiation import validate_negotiation_decision
from bridle.services.complexity_negotiation_service import (
    PlanComplexityFailedError,
    ReplanRequestedError,
    apply_negotiation_decision,
    run_import_complexity_negotiation,
)
from bridle.schemas.node import NodeImportSchema


def _make_plan_payload(**overrides) -> dict:
    base = dict(
        goal="Test plan",
        nodes=[
            {
                "id": "n1",
                "title": "Node",
                "goal": "Do something with clear acceptance criteria here",
                "node_type": "code_change",
                "depends_on": [],
                "files": ["src/main.py"],
                "tests": ["pytest tests/"],
                "metrics": {},
                "constraints": {"c": True},
                "review_checks": [],
                "expected_outputs": {},
                "estimated_minutes": 15,
            }
        ],
    )
    base.update(overrides)
    return base


def _node() -> NodeImportSchema:
    return NodeImportSchema.model_validate(_make_plan_payload()["nodes"][0])


class TestRunImportComplexityNegotiation:
    @pytest.mark.asyncio
    async def test_expand_fixes_node_in_one_round(self) -> None:
        nodes = [_node()]
        svc = AsyncMock()
        svc.negotiate.return_value = validate_negotiation_decision(
            {
                "action": "expand",
                "expand": {
                    "node_id": "n1",
                    "new_goal": "Expanded scope with clear acceptance criteria",
                    "new_acceptance_scope": "Module passes integration tests in CI",
                    "new_estimated_minutes": 75,
                    "additional_files": [],
                },
            }
        )
        result = await run_import_complexity_negotiation(nodes, negotiation_service=svc)
        assert all(v.ok for v in result)
        assert svc.negotiate.await_count == 1

    @pytest.mark.asyncio
    async def test_expand_adds_tests_fixes_missing_tests(self) -> None:
        nodes = [NodeImportSchema.model_validate({**_make_plan_payload()["nodes"][0], "tests": []})]
        svc = AsyncMock()
        svc.negotiate.return_value = validate_negotiation_decision(
            {
                "action": "expand",
                "expand": {
                    "node_id": "n1",
                    "new_goal": "Implement roman converter with clear acceptance criteria",
                    "new_acceptance_scope": "Converter passes pytest suite in CI",
                    "new_estimated_minutes": 75,
                    "additional_files": [],
                    "new_tests": ["pytest tests/test_roman_converter.py -q"],
                },
            }
        )
        result = await run_import_complexity_negotiation(nodes, negotiation_service=svc)
        assert all(v.ok for v in result)
        assert nodes[0].tests == ["pytest tests/test_roman_converter.py -q"]

    @pytest.mark.asyncio
    async def test_three_rounds_still_failing_raises(self) -> None:
        nodes = [
            _node(),
            NodeImportSchema.model_validate(
                {
                    **_make_plan_payload()["nodes"][0],
                    "id": "n2",
                    "title": "Second",
                }
            ),
        ]
        svc = AsyncMock()
        svc.negotiate.side_effect = [
            validate_negotiation_decision(
                {
                    "action": "expand",
                    "expand": {
                        "node_id": "n1",
                        "new_goal": "Expanded n1 with clear acceptance criteria for QA",
                        "new_acceptance_scope": "Module n1 passes integration tests in CI",
                        "new_estimated_minutes": 75,
                        "additional_files": [],
                    },
                }
            ),
            validate_negotiation_decision(
                {
                    "action": "expand",
                    "expand": {
                        "node_id": "n2",
                        "new_goal": "Still too small for n2",
                        "new_acceptance_scope": "short",
                        "new_estimated_minutes": 20,
                        "additional_files": [],
                    },
                }
            ),
            validate_negotiation_decision(
                {
                    "action": "expand",
                    "expand": {
                        "node_id": "n2",
                        "new_goal": "Still failing on n2",
                        "new_acceptance_scope": "still short",
                        "new_estimated_minutes": 25,
                        "additional_files": [],
                    },
                }
            ),
        ]
        with pytest.raises(PlanComplexityFailedError) as exc_info:
            await run_import_complexity_negotiation(nodes, negotiation_service=svc)
        assert exc_info.value.rounds_used == 3
        assert svc.negotiate.await_count == 3
        failing_ids = {row.node_id for row in exc_info.value.last_validations if not row.ok}
        assert "n2" in failing_ids
        assert any(
            "node_too_granular:estimated_minutes_too_low" in row.issues
            for row in exc_info.value.last_validations
            if not row.ok
        )

    @pytest.mark.asyncio
    async def test_replan_raises(self) -> None:
        nodes = [_node()]
        svc = AsyncMock()
        svc.negotiate.return_value = validate_negotiation_decision(
            {"action": "replan", "replan": {"reason": "plan too fragmented"}}
        )
        with pytest.raises(ReplanRequestedError):
            await run_import_complexity_negotiation(nodes, negotiation_service=svc)

    def test_merge_reduces_node_count(self) -> None:
        nodes = [
            _node(),
            NodeImportSchema.model_validate(
                {
                    **_make_plan_payload()["nodes"][0],
                    "id": "n2",
                    "title": "Second",
                }
            ),
        ]
        decision = validate_negotiation_decision(
            {
                "action": "merge",
                "merge": {
                    "node_ids": ["n1", "n2"],
                    "new_title": "Merged",
                    "new_goal": "Combined scope with clear acceptance criteria for QA",
                    "new_estimated_minutes": 90,
                    "merged_files": ["src/main.py"],
                    "merged_depends_on": [],
                },
            }
        )
        apply_negotiation_decision(nodes, decision)
        assert len(nodes) == 1


class TestPlanImportAPIWithNegotiation:
    async def test_import_complex_plan_after_negotiation_succeeds(self, client: AsyncClient) -> None:
        task_resp = await client.post("/api/v1/tasks", json={"title": "Negotiated"})
        task_id = task_resp.json()["id"]
        plan = _make_plan_payload()

        async def _fix(nodes: list[NodeImportSchema]):
            for node in nodes:
                node.estimated_minutes = 60
                node.goal = "Expanded goal with clear acceptance criteria for reviewers"
            vals = validate_plan_nodes(nodes)
            return vals

        with patch(
            "bridle.services.complexity_negotiation_service.run_import_complexity_negotiation",
            new_callable=AsyncMock,
            side_effect=_fix,
        ):
            resp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=plan)
        assert resp.status_code == 200
        node = resp.json()["nodes"][0]
        assert node["status"] == "pending"
