"""Tests for node complexity policy."""
from __future__ import annotations

from bridle.engine.node_complexity_policy import (
    NodeComplexityLimits,
    estimate_node_complexity,
    validate_node_complexity,
)
from bridle.schemas.node import NodeImportSchema


def _node(**overrides) -> NodeImportSchema:
    base = {
        "id": "n1",
        "title": "Node",
        "goal": "Implement feature with clear acceptance criteria for reviewers",
        "node_type": "code_change",
        "depends_on": [],
        "files": ["src/a.py"],
        "tests": ["pytest tests/ -q"],
        "metrics": {},
        "constraints": {"c": True},
        "review_checks": [],
        "expected_outputs": {"exit": 0},
    }
    base.update(overrides)
    return NodeImportSchema(**base)


class TestNodeComplexityPolicy:
    def test_valid_node_passes(self) -> None:
        result = validate_node_complexity(_node())
        assert result.ok is True
        assert result.issues == []
        assert result.estimate.estimated_files_changed == 1
        assert result.estimate.estimated_test_commands == 1

    def test_too_many_files(self) -> None:
        files = [f"src/f{i}.py" for i in range(6)]
        result = validate_node_complexity(_node(files=files))
        assert result.ok is False
        assert any("too_many_files" in issue for issue in result.issues)

    def test_too_many_test_commands(self) -> None:
        tests = [f"pytest t{i}.py" for i in range(4)]
        result = validate_node_complexity(_node(tests=tests))
        assert result.ok is False
        assert any("too_many_test_commands" in issue for issue in result.issues)

    def test_estimated_minutes_below_default_min_fails(self) -> None:
        result = validate_node_complexity(_node(estimated_minutes=45))
        assert result.ok is False
        assert "node_too_granular:estimated_minutes_too_low" in result.issues

    def test_estimated_minutes_at_default_min_passes(self) -> None:
        result = validate_node_complexity(_node(estimated_minutes=60))
        assert result.ok is True

    def test_estimated_minutes_out_of_range(self) -> None:
        low = validate_node_complexity(_node(estimated_minutes=10))
        high = validate_node_complexity(_node(estimated_minutes=120))
        assert low.ok is False
        assert high.ok is False
        assert any(i.startswith("node_too_granular:") for i in low.issues)
        assert any(i.startswith("node_too_complex:") for i in high.issues)

    def test_issue_prefixes_by_violation_type(self) -> None:
        files_issue = validate_node_complexity(
            _node(files=[f"src/f{i}.py" for i in range(6)])
        )
        assert any(i.startswith("node_too_complex:") for i in files_issue.issues)

        deps_issue = validate_node_complexity(_node(depends_on=["a", "b", "c"]))
        assert any("too_many_dependencies" in i for i in deps_issue.issues)
        assert all(i.startswith("node_too_complex:") for i in deps_issue.issues)

    def test_too_many_dependencies(self) -> None:
        result = validate_node_complexity(_node(depends_on=["a", "b", "c"]))
        assert result.ok is False
        assert any("too_many_dependencies" in issue for issue in result.issues)

    def test_missing_acceptance_scope(self) -> None:
        result = validate_node_complexity(
            _node(goal="x", expected_outputs={}),
            limits=NodeComplexityLimits(),
        )
        assert result.ok is False
        assert any("missing_acceptance_scope" in issue for issue in result.issues)

    def test_estimate_uses_explicit_acceptance_scope(self) -> None:
        estimate = estimate_node_complexity(
            _node(acceptance_scope="Independent acceptance for module X")
        )
        assert "module X" in estimate.acceptance_scope

    def test_code_change_without_tests_is_blocked(self) -> None:
        result = validate_node_complexity(_node(tests=[]))
        assert result.ok is False
        assert "node_incomplete:missing_tests" in result.issues

    def test_code_change_with_tests_passes(self) -> None:
        result = validate_node_complexity(_node(tests=["pytest tests/test_x.py -q"]))
        assert result.ok is True

    def test_micro_without_tests_passes(self) -> None:
        result = validate_node_complexity(
            _node(
                node_type="micro",
                tests=[],
                files=["src/a.py"],
                goal="Fix typo in module with clear acceptance scope",
            )
        )
        assert "node_incomplete:missing_tests" not in result.issues
