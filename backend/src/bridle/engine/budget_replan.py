"""Replan decisions when sub-node tool budgets are exhausted."""
from __future__ import annotations

from typing import Any


def redact_budget_payload(data: dict[str, Any]) -> dict[str, Any]:
    from bridle.engine.deepseek_agent_provider import sanitize_model_response_text

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str):
            return sanitize_model_response_text(value)
        return value

    return walk(dict(data))


def build_replan_decision(budget_report: dict[str, Any]) -> dict[str, Any]:
    suggested = budget_report.get("suggested_split")
    if not isinstance(suggested, list):
        suggested = [
            "types/schema",
            "core logic",
            "tests",
            "integration verification",
        ]
    return {
        "replan_required": True,
        "decision": "replan_required",
        "reason": "tool_budget_exhausted",
        "needs_replan": bool(budget_report.get("needs_replan", True)),
        "budget": budget_report.get("budget"),
        "changed_files": list(budget_report.get("changed_files") or []),
        "last_test_result": budget_report.get("last_test_result"),
        "last_tool_call": budget_report.get("last_tool_call"),
        "suggested_split": suggested,
        "source_error_code": budget_report.get("error_code", "tool_budget_exhausted"),
    }
