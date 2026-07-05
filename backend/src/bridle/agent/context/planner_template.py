"""Planner system prompt template for planning conversations."""
from __future__ import annotations

import json
from typing import Any

PLANNER_EXAMPLE_PLAN: dict[str, Any] = {
    "goal": "Implement a roman numeral converter",
    "aggregate_files": [],
    "nodes": [
        {
            "id": "node-001",
            "title": "implement roman converter",
            "goal": "implement conversion logic and keep the target workflow node responsible for its tests",
            "node_type": "code_change",
            "depends_on": [],
            "files": ["src/roman_converter.py"],
            "tests": ["pytest tests/test_roman_converter.py -q"],
            "test_details": [
                {
                    "command": "pytest tests/test_roman_converter.py -q",
                    "purpose": "prove numeral parsing before implementation",
                }
            ],
        },
    ],
}


def _format_example_fence(plan: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(plan, indent=2) + "\n```"


def validate_tdd_plan_structure(plan_data: dict[str, Any]) -> list[str]:
    """Return violations when plans separate tests into independent workflow nodes."""
    violations: list[str] = []
    nodes = list(plan_data.get("nodes") or [])
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("node_type") == "test_validation":
            violations.append(
                f"node {node.get('id')!r} uses test_validation as an independent node; tests must stay on the workflow node",
            )
        if node.get("node_type") != "code_change":
            continue
        tests = list(node.get("tests") or [])
        details = list(node.get("test_details") or [])
        if tests and not details:
            violations.append(
                f"code_change node {node.get('id')!r} must describe its tests in test_details on the workflow node",
            )
    return violations


PLANNER_SYSTEM_TEMPLATE = (
    "You are the planning agent for Bridle. You do NOT write code or call tools. "
    "You converse with the user to discover the goal, inspect the workspace context, "
    "and draft a structured execution plan.\n\n"
    "Input you receive:\n"
    "- Conversation history between user and assistant.\n"
    "- A workspace overview (file list and optional excerpts from README, package.json, "
    "pyproject.toml, requirements.txt).\n\n"
    "Output rules:\n"
    "- Always reply in natural language first.\n"
    "- Do not invoke tools or claim you executed commands.\n"
    "- When the user clearly confirms the plan is ready, or you have enough information "
    "to propose a complete plan, append ONE fenced JSON block at the end of your reply.\n"
    "- The fence MUST be lowercase ```json (not ```JSON), and the JSON inside MUST validate "
    "as PlanImportSchema.\n"
    "- Do not wrap the entire response in JSON; only the plan lives in the fence.\n"
    "- If still gathering requirements, omit the JSON fence entirely.\n\n"
    "PlanImportSchema (strict):\n"
    "- Top-level required: goal (str), nodes (non-empty list).\n"
    "- Top-level optional: aggregate_files (list, default []).\n"
    "- Each node REQUIRED: id (str), title (str), goal (str), node_type (one of "
    "\"code_change\", \"test_validation\", \"metric_validation\", \"review_gate\", \"micro\").\n"
    "- Each node OPTIONAL: depends_on (list[str], default []), files (list[str]), tests (list[str]), "
    "test_details (list[{command,purpose}]), "
    "metrics, constraints, review_checks, expected_outputs, interfaces, "
    "read_set, write_set, readonly_context, conflict_contributions, "
    "estimated_minutes, acceptance_scope.\n"
    "- For every code_change node, tests MUST be a non-empty list of test commands "
    '(e.g. "pytest tests/test_xxx.py -q"). Empty tests array will be rejected at import time.\n'
    "- For every code_change node, test_details SHOULD describe each test command's purpose on the same workflow node.\n"
    "- tests (list[str]) is optional for non-code_change node types.\n"
    "- Use node_type=\"code_change\" for nodes that implement code, "
    "\"test_validation\" for nodes that write or run tests, "
    "\"metric_validation\" for measurable acceptance checks, "
    "\"review_gate\" for human / agent review gates.\n"
    "- Use node_type=\"micro\" for tiny atomic fixes (typo / 1-line tweak / single import) "
    "that do not warrant estimated_minutes>=60; micro nodes may use accept_as_is exemption "
    "during complexity negotiation.\n\n"
    "Test-first planning (default for new feature work):\n"
    "- Keep tests attached to the workflow node that will own the implementation.\n"
    "- Do not create independent test nodes for the same feature; describe each test command in test_details.\n"
    "- Use test_details purpose strings to explain what each command verifies.\n"
    "- The workflow node may still point at pytest files that are created as part of the same implementation step.\n\n"
    "Example fence (single workflow node with attached tests; always include node_type):\n"
    f"{_format_example_fence(PLANNER_EXAMPLE_PLAN)}"
)


def build_planner_messages(
    history: list[dict[str, str]],
    workspace_overview: dict[str, Any],
) -> list[dict[str, str]]:
    overview_text = json.dumps(workspace_overview, ensure_ascii=False, default=str)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": PLANNER_SYSTEM_TEMPLATE},
        {"role": "system", "content": f"Workspace overview:\n{overview_text}"},
    ]
    for turn in history:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        if role not in ("user", "assistant"):
            role = "user"
        messages.append({"role": role, "content": content})
    return messages
