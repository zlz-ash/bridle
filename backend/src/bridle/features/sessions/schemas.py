"""Unified project session request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SessionRole = Literal["planning", "executing", "mapping"]
MessageRole = Literal["system", "user", "assistant", "tool"]


class ProjectSessionCreateSchema(BaseModel):
    """Validate session creation; project/title input exits as a planning conversation request."""

    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1)
    title: str = Field(default="New conversation", min_length=1, max_length=500)


class ProjectSessionReadSchema(BaseModel):
    """Serialize shared runtime state; ORM input exits with project availability and role."""

    id: str
    project_id: str
    project_path: str
    title: str
    role: SessionRole
    status: str
    available: bool
    readonly_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class SessionRoleChangeSchema(BaseModel):
    """Validate a role transition; role/actor/confirmation input gates user-owned switching."""

    model_config = ConfigDict(extra="forbid")
    role: SessionRole
    actor: Literal["user", "agent"]
    confirmed: bool = False


class ProjectMessageCreateSchema(BaseModel):
    """Validate one message write; role/content/tool input exits as persistence-ready data."""

    model_config = ConfigDict(extra="forbid")
    role: MessageRole
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_result: dict[str, Any] | None = None


class ProjectConverseSchema(BaseModel):
    """Validate unified agent input; content/node selection enters one persisted session turn."""

    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=100_000)
    node_id: str | None = Field(default=None, min_length=1)


class ProjectMessageReadSchema(BaseModel):
    """Serialize one persisted message; ORM input exits as ordered conversation data."""

    id: str
    session_id: str
    role: MessageRole
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_result: dict[str, Any] | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
