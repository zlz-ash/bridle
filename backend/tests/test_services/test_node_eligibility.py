"""NodeEligibilityService complexity reason normalization."""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.models.node_agent_run import NodeAgentRunRecord
from bridle.services.node_eligibility import NodeEligibilityService
from bridle.models.node import NodeRecord
from tests.helpers.plan_factory import two_node_plan


def _blocked_node(*, issues: list[str]) -> NodeRecord:
    return NodeRecord(
        plan_id="p1",
        plan_node_id="n1",
        title="N",
        goal="Goal with clear acceptance criteria for reviewers",
        node_type="code_change",
        order=0,
        depends_on=[],
        files=["a.py"],
        tests=["pytest"],
        metrics={"complexity": {"ok": False, "issues": issues}},
        constraints={"c": True},
        review_checks=[],
        expected_outputs={},
        interfaces={"exposes": [], "consumes": []},
        status="blocked",
    )


class TestComplexityBlockReason:
    def test_granular_only_issues(self) -> None:
        node = _blocked_node(issues=["node_too_granular:estimated_minutes_too_low"])
        result = NodeEligibilityService._eligibility_blockers(node, set(), set())
        assert result is not None
        assert result[0] == "node_too_granular"

    def test_complex_only_issues(self) -> None:
        node = _blocked_node(issues=["node_too_complex:too_many_files"])
        result = NodeEligibilityService._eligibility_blockers(node, set(), set())
        assert result is not None
        assert result[0] == "node_too_complex"

    def test_mixed_issues_prefers_complex(self) -> None:
        node = _blocked_node(
            issues=[
                "node_too_granular:estimated_minutes_too_low",
                "node_too_complex:too_many_files",
            ]
        )
        result = NodeEligibilityService._eligibility_blockers(node, set(), set())
        assert result is not None
        assert result[0] == "node_too_complex"

    def test_incomplete_issues(self) -> None:
        from bridle.services.node_eligibility import complexity_block_reason

        assert complexity_block_reason(["node_incomplete:missing_tests"]) == "node_incomplete"

        node = _blocked_node(issues=["node_incomplete:missing_tests"])
        result = NodeEligibilityService._eligibility_blockers(node, set(), set())
        assert result is not None
        assert result[0] == "node_incomplete"


class TestBlockerReasonPassthrough:
    def test_blocker_reason_passes_through_blocked_by(self) -> None:
        node = NodeRecord(
            plan_id="p1",
            plan_node_id="n1",
            title="N",
            goal="Goal with clear acceptance criteria for reviewers",
            node_type="code_change",
            order=0,
            depends_on=[],
            files=["a.py"],
            tests=[],
            metrics={},
            constraints={"c": True},
            review_checks=[],
            expected_outputs={},
            interfaces={"exposes": [], "consumes": []},
            status="ready",
        )
        result = NodeEligibilityService._eligibility_blockers(node, set(), set())
        assert result is not None
        assert result[0] == "node_blocked"
        assert result[1] == ["code_change node missing tests"]


class TestNodeAttemptExhaustion:
    def test_blockers_after_two_prior_runs(self) -> None:
        node = NodeRecord(
            plan_id="p1",
            plan_node_id="n1",
            title="N",
            goal="Goal with clear acceptance criteria for reviewers",
            node_type="code_change",
            order=0,
            depends_on=[],
            files=["a.py"],
            tests=["pytest"],
            status="failed_retryable",
        )
        result = NodeEligibilityService._eligibility_blockers(node, set(), set(), prior_run_count=2)
        assert result is not None
        assert result[0] == "node_attempts_exhausted"

    def test_blockers_allow_second_attempt(self) -> None:
        node = NodeRecord(
            plan_id="p1",
            plan_node_id="n1",
            title="N",
            goal="Goal with clear acceptance criteria for reviewers",
            node_type="code_change",
            order=0,
            depends_on=[],
            files=["a.py"],
            tests=["pytest"],
            constraints={"bounded": True},
            status="ready",
        )
        result = NodeEligibilityService._eligibility_blockers(node, set(), set(), prior_run_count=1)
        assert result is None


@pytest.mark.asyncio
async def test_node_blocked_after_2_failed_attempts(db: AsyncSession, client: AsyncClient) -> None:
    task_resp = await client.post("/api/v1/tasks", json={"title": "Attempt Exhausted"})
    task_id = task_resp.json()["id"]
    imp = await client.post(f"/api/v1/tasks/{task_id}/plan/import", json=two_node_plan())
    plan_id = imp.json()["plan_id"]
    node_id = imp.json()["nodes"][0]["id"]
    session = await client.post("/api/v1/agent/coding-sessions", json={"plan_id": plan_id})
    session_id = session.json()["session_id"]

    for _ in range(2):
        db.add(
            NodeAgentRunRecord(
                session_id=session_id,
                node_id=node_id,
                plan_node_id="n1",
                status="failed",
                phase="finalizing",
                attempt=1,
                blocked_reason="tests_failed",
            )
        )
    await db.commit()

    eligible, blocked = await NodeEligibilityService.compute(db, plan_id, session_id=session_id)
    blocked_ids = {b.node_id: b.reason for b in blocked}
    assert node_id in blocked_ids
    assert blocked_ids[node_id] == "node_attempts_exhausted"
    assert all(e.node_id != node_id for e in eligible)
