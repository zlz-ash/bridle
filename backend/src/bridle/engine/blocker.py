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

        # 2. tests: only code_change (aligned with plan-mode prompt)
        if node.node_type == "code_change" and not node.tests:
            return BlockResult(blocked=True, reason="code_change node missing tests")

        if node.node_type == "micro":
            return BlockResult(blocked=False)

        # 3. metric_validation still requires metrics
        if node.node_type == "metric_validation" and not node.metrics:
            return BlockResult(blocked=True, reason="metric_validation node missing metrics")

        # 4. constraints: only code_change
        if node.node_type == "code_change" and not node.constraints:
            return BlockResult(blocked=True, reason="code_change node missing constraints")

        # 5. review_gate still requires review_checks
        if node.node_type == "review_gate" and not node.review_checks:
            return BlockResult(blocked=True, reason="review_gate node missing review_checks")

        return BlockResult(blocked=False)
