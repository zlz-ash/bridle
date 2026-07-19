"""Context layer data structures for agent prompt assembly."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ToolDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    purpose: str
    when_to_use: str
    input_summary: str
    output_summary: str
    constraints: str
    reserved: bool = False


class ChildAgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    status: str
    result_summary: str = ""
    test_summary: str = ""
    metrics_summary: str = ""
    evidence_refs: list[str] = []


class ContextPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str
    node: dict[str, Any]
    allowed_files: list[str]
    tests: list[str]
    metrics: dict[str, Any]
    constraints: dict[str, Any]
    review_checks: list[str]
    expected_outputs: dict[str, Any]
    accessible_context: dict[str, Any]
    tool_capabilities: dict[str, Any]
    short_term_memory: list[dict[str, Any]]
    tool_context: list[dict[str, Any]]
    child_agent_results: list[dict[str, Any]]
