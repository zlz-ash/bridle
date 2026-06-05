"""Blocker behavior for micro nodes."""
from __future__ import annotations

from bridle.engine.blocker import Blocker
from bridle.models.node import NodeRecord


def test_micro_node_without_metrics_not_blocked() -> None:
    node = NodeRecord(
        plan_id="p1",
        plan_node_id="m1",
        title="Tiny fix",
        goal="Fix one line",
        node_type="micro",
        order=0,
        depends_on=[],
        files=["a.py"],
        tests=["pytest"],
        metrics={},
        constraints={"c": True},
        review_checks=[],
        expected_outputs={},
        interfaces={"exposes": [], "consumes": []},
        status="pending",
    )
    result = Blocker.check(node, completed_node_ids=set())
    assert result.blocked is False
