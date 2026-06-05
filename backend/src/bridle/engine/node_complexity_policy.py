"""Node complexity estimation and validation for plan import."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MAX_FILES = 5
DEFAULT_MAX_TEST_COMMANDS = 3
DEFAULT_MIN_ESTIMATED_MINUTES = 60
DEFAULT_MAX_ESTIMATED_MINUTES = 90
DEFAULT_MAX_DEPENDENCIES = 2
DEFAULT_ESTIMATED_MINUTES = 60
MIN_ACCEPTANCE_SCOPE_LEN = 10


@dataclass(frozen=True)
class NodeComplexityLimits:
    max_files: int = DEFAULT_MAX_FILES
    max_test_commands: int = DEFAULT_MAX_TEST_COMMANDS
    min_estimated_minutes: int = DEFAULT_MIN_ESTIMATED_MINUTES
    max_estimated_minutes: int = DEFAULT_MAX_ESTIMATED_MINUTES
    max_dependencies: int = DEFAULT_MAX_DEPENDENCIES


@dataclass
class NodeComplexityEstimate:
    estimated_files_changed: int
    estimated_test_commands: int
    estimated_minutes: int
    dependency_count: int
    acceptance_scope: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "estimated_files_changed": self.estimated_files_changed,
            "estimated_test_commands": self.estimated_test_commands,
            "estimated_minutes": self.estimated_minutes,
            "dependency_count": self.dependency_count,
            "acceptance_scope": self.acceptance_scope,
        }


@dataclass
class NodeComplexityValidation:
    node_id: str
    estimate: NodeComplexityEstimate
    ok: bool
    issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "ok": self.ok,
            "issues": list(self.issues),
            "estimate": self.estimate.to_dict(),
        }


def _acceptance_scope_from_node(node: Any) -> str:
    explicit = getattr(node, "acceptance_scope", None)
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    expected = getattr(node, "expected_outputs", None)
    if isinstance(expected, dict) and expected:
        return json.dumps(expected, ensure_ascii=False, default=str)[:500]
    if isinstance(expected, list) and expected:
        return json.dumps(expected, ensure_ascii=False, default=str)[:500]
    goal = str(getattr(node, "goal", "") or "").strip()
    title = str(getattr(node, "title", "") or "").strip()
    if title and goal:
        return f"{title}: {goal}"[:500]
    return goal[:500]


def estimate_node_complexity(node: Any) -> NodeComplexityEstimate:
    files = getattr(node, "files", None) or []
    write_set = getattr(node, "write_set", None) or []
    tests = getattr(node, "tests", None) or []
    depends_on = getattr(node, "depends_on", None) or []
    file_count = len(set(files) | set(write_set)) if write_set else len(files)
    minutes_raw = getattr(node, "estimated_minutes", None)
    estimated_minutes = (
        int(minutes_raw)
        if minutes_raw is not None
        else DEFAULT_ESTIMATED_MINUTES
    )
    return NodeComplexityEstimate(
        estimated_files_changed=file_count,
        estimated_test_commands=len(tests),
        estimated_minutes=estimated_minutes,
        dependency_count=len(depends_on),
        acceptance_scope=_acceptance_scope_from_node(node),
    )


def validate_node_complexity(
    node: Any,
    *,
    limits: NodeComplexityLimits | None = None,
) -> NodeComplexityValidation:
    limits = limits or NodeComplexityLimits()
    node_id = str(getattr(node, "id", "") or "")
    node_type = str(getattr(node, "node_type", "") or "")

    metrics = getattr(node, "metrics", None)
    if isinstance(metrics, dict):
        complexity = metrics.get("complexity")
        if isinstance(complexity, dict) and complexity.get("exempted"):
            estimate = estimate_node_complexity(node)
            return NodeComplexityValidation(
                node_id=node_id,
                estimate=estimate,
                ok=True,
                issues=[],
            )

    if node_type == "micro":
        estimate = estimate_node_complexity(node)
        issues: list[str] = []
        if estimate.estimated_files_changed > 1:
            issues.append("node_too_complex:too_many_files")
        if len(estimate.acceptance_scope.strip()) < MIN_ACCEPTANCE_SCOPE_LEN:
            issues.append("node_too_complex:missing_acceptance_scope")
        return NodeComplexityValidation(
            node_id=node_id,
            estimate=estimate,
            ok=not issues,
            issues=issues,
        )

    estimate = estimate_node_complexity(node)
    issues: list[str] = []

    if estimate.estimated_files_changed > limits.max_files:
        issues.append("node_too_complex:too_many_files")
    if estimate.estimated_test_commands > limits.max_test_commands:
        issues.append("node_too_complex:too_many_test_commands")
    if estimate.estimated_minutes < limits.min_estimated_minutes:
        issues.append("node_too_granular:estimated_minutes_too_low")
    if estimate.estimated_minutes > limits.max_estimated_minutes:
        issues.append("node_too_complex:estimated_minutes_too_high")
    if estimate.dependency_count > limits.max_dependencies:
        issues.append("node_too_complex:too_many_dependencies")
    if len(estimate.acceptance_scope.strip()) < MIN_ACCEPTANCE_SCOPE_LEN:
        issues.append("node_too_complex:missing_acceptance_scope")

    if node_type == "code_change":
        tests_field = getattr(node, "tests", None) or []
        if not tests_field:
            issues.append("node_incomplete:missing_tests")

    return NodeComplexityValidation(
        node_id=node_id,
        estimate=estimate,
        ok=not issues,
        issues=issues,
    )


def validate_plan_nodes(
    nodes: list[Any],
    *,
    limits: NodeComplexityLimits | None = None,
) -> list[NodeComplexityValidation]:
    return [validate_node_complexity(node, limits=limits) for node in nodes]
