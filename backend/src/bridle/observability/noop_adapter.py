"""No-op observability adapter."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bridle.observability.schema import GenerationRecord, ToolCallRecord


@dataclass
class NoopTraceHandle:
    trace_id: str = "noop"
    name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def start_span(self, name: str, **metadata: Any) -> NoopSpanHandle:
        return NoopSpanHandle(name=name, metadata=metadata)

    def end(self, *, status: str = "completed", error_code: str | None = None) -> None:
        _ = status
        _ = error_code


@dataclass
class NoopSpanHandle:
    span_id: str = "noop"
    name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def end(self, *, status: str = "completed", error_code: str | None = None) -> None:
        _ = status
        _ = error_code


class NoopObservabilityAdapter:
    def start_trace(self, name: str, **metadata: Any) -> NoopTraceHandle:
        return NoopTraceHandle(name=name, metadata=dict(metadata))

    def start_span(self, name: str, **metadata: Any) -> NoopSpanHandle:
        return NoopSpanHandle(name=name, metadata=dict(metadata))

    def record_generation(self, record: GenerationRecord) -> None:
        _ = record

    def record_tool_call(self, record: ToolCallRecord) -> None:
        _ = record

    def flush(self) -> None:
        return
