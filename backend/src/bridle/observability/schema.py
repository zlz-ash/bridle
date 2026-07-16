"""Unified observability schema and field conventions."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STANDARD_IDENTITY_FIELDS = frozenset({
    "trace_id",
    "message_id",
    "project_id",
    "agent_id",
    "generation",
    "session_id",
    "run_id",
    "node_id",
    "plan_id",
    "proposal_id",
})

STANDARD_EXECUTION_FIELDS = frozenset({
    "provider",
    "model",
    "phase",
    "status",
    "run_mode",
})

STANDARD_RESULT_FIELDS = frozenset({
    "error_code",
    "duration_ms",
    "exit_code",
    "timed_out",
})

STANDARD_UI_FIELDS = frozenset({
    "workspace",
    "tool_name",
    "prompt_name",
    "prompt_version",
})

CORE_EVENT_PREFIXES = (
    "project_session.",
    "agent.",
    "model.",
    "tool.",
    "workspace.",
)


@dataclass(frozen=True)
class ObservabilityContext:
    trace_id: str | None = None
    message_id: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    generation: int | None = None
    session_id: str | None = None
    run_id: str | None = None
    node_id: str | None = None
    plan_id: str | None = None
    proposal_id: str | None = None
    provider: str | None = None
    model: str | None = None
    phase: str | None = None
    run_mode: str | None = None
    workspace: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in (
            *STANDARD_IDENTITY_FIELDS,
            *STANDARD_EXECUTION_FIELDS,
            "workspace",
        ):
            value = getattr(self, key, None)
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class PromptLineage:
    prompt_name: str | None = None
    prompt_version: str | None = None
    prompt_inputs: dict[str, Any] = field(default_factory=dict)
    rendered_messages: list[dict[str, Any]] = field(default_factory=list)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "prompt_name": self.prompt_name,
            "prompt_version": self.prompt_version,
            "prompt_inputs": self.prompt_inputs,
            "rendered_messages_count": len(self.rendered_messages),
        }


@dataclass(frozen=True)
class GenerationRecord:
    name: str
    model: str
    input_summary: dict[str, Any]
    output_summary: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_lineage: PromptLineage | None = None
    duration_ms: int | None = None
    status: str = "completed"
    error_code: str | None = None


@dataclass(frozen=True)
class ToolCallRecord:
    tool_name: str
    input_summary: dict[str, Any]
    output_summary: dict[str, Any]
    duration_ms: int | None = None
    status: str = "completed"
    error_code: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
