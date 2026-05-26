"""Test helpers for building importable plans."""
from __future__ import annotations


def expose_dict(name: str = "auth_context") -> dict:
    return {
        "name": name,
        "fields": [{"name": "user_id", "type": "string"}],
        "endpoints": [{"name": "get_user", "method": "GET", "path": "/users/me"}],
    }


def code_change_node(
    node_id: str,
    *,
    depends_on: list[str] | None = None,
    files: list[str] | None = None,
    tests: list[str] | None = None,
    constraints: dict | None = None,
) -> dict:
    return {
        "id": node_id,
        "title": f"Node {node_id}",
        "goal": f"Goal {node_id}",
        "node_type": "code_change",
        "depends_on": depends_on or [],
        "files": files or [f"src/{node_id}.py"],
        "tests": tests if tests is not None else ["pytest tests/"],
        "metrics": {},
        "constraints": constraints if constraints is not None else {"bounded": True},
        "review_checks": [],
        "expected_outputs": {},
        "interfaces": {"exposes": [expose_dict(f"ctx_{node_id}")], "consumes": []},
    }


def two_node_plan() -> dict:
    return {
        "goal": "Two node plan",
        "nodes": [
            code_change_node("n1"),
            code_change_node("n2", depends_on=["n1"]),
        ],
    }
