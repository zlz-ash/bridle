"""Schema acceptance for node_type=micro."""
from __future__ import annotations

from bridle.schemas.node import NodeImportSchema


def test_node_type_micro_accepted() -> None:
    node = NodeImportSchema.model_validate(
        {
            "id": "m1",
            "title": "Fix typo",
            "goal": "Correct spelling in README header for reviewers",
            "node_type": "micro",
            "depends_on": [],
            "files": ["README.md"],
            "tests": ["pytest -q"],
            "metrics": {},
            "constraints": {"c": True},
            "review_checks": [],
            "expected_outputs": {},
        }
    )
    assert node.node_type == "micro"


def test_micro_node_passes_complexity_with_single_file() -> None:
    from bridle.engine.node_complexity_policy import validate_node_complexity

    node = NodeImportSchema.model_validate(
        {
            "id": "m1",
            "title": "Fix typo",
            "goal": "Correct spelling in README header for reviewers",
            "node_type": "micro",
            "depends_on": [],
            "files": ["README.md"],
            "tests": ["pytest -q"],
            "metrics": {},
            "constraints": {"c": True},
            "review_checks": [],
            "expected_outputs": {},
            "estimated_minutes": 5,
        }
    )
    result = validate_node_complexity(node)
    assert result.ok is True
