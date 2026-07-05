"""Unified logging event schema."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from bridle.observability.schema import (
    STANDARD_EXECUTION_FIELDS,
    STANDARD_IDENTITY_FIELDS,
    STANDARD_RESULT_FIELDS,
    STANDARD_UI_FIELDS,
)


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class LogEvent:
    action: str
    status: str
    level: LogLevel = LogLevel.INFO
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
    tool_name: str | None = None
    prompt_name: str | None = None
    prompt_version: str | None = None
    error_code: str | None = None
    duration_ms: int | None = None
    exit_code: int | None = None
    timed_out: bool | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "timestamp": self.timestamp,
            "level": self.level.value,
            "action": self.action,
            "status": self.status,
        }
        for key in (
            *STANDARD_IDENTITY_FIELDS,
            *STANDARD_EXECUTION_FIELDS,
            *STANDARD_RESULT_FIELDS,
            *STANDARD_UI_FIELDS,
        ):
            value = getattr(self, key, None)
            if value is not None:
                data[key] = value
        if self.detail:
            data["detail"] = self.detail
        return data
