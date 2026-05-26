"""Blocker — enforce gating rules before node execution."""
from __future__ import annotations

from dataclasses import dataclass, field

from bridle.models.node import NodeRecord


@dataclass
class BlockResult:
    blocked: bool
    reason: str = ""


class Blocker:
    """Check if a node is blocked from execution.

    Blocking rules:
    - depends_on not met → blocked
    - missing tests → blocked
    - metric_validation missing metrics → blocked
    - missing constraints → blocked
    - review_gate missing review_checks → blocked
    """

    @staticmethod
    def check(node: NodeRecord, completed_node_ids: set[str]) -> BlockResult:
        # 1. Check dependencies
        for dep_id in node.depends_on:
            if dep_id not in completed_node_ids:
                return BlockResult(blocked=True, reason=f"Dependency {dep_id} not satisfied")

        # 2. Check tests defined
        if not node.tests:
            return BlockResult(blocked=True, reason="Missing test definitions")

        # 3. Check metrics for metric_validation
        if node.node_type == "metric_validation" and not node.metrics:
            return BlockResult(blocked=True, reason="Missing metric definitions for metric_validation node")

        # 4. Check constraints
        if not node.constraints:
            return BlockResult(blocked=True, reason="Missing constraint rules")

        # 5. Check review_checks for review_gate
        if node.node_type == "review_gate" and not node.review_checks:
            return BlockResult(blocked=True, reason="Missing review checks for review_gate node")

        return BlockResult(blocked=False)
