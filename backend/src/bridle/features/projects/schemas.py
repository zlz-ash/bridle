"""Project registry request/response schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectOpenSchema(BaseModel):
    """Validate project selection; path input exits as a normalized open request."""

    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=2000)


class ProjectReadSchema(BaseModel):
    """Serialize one registered project; ORM/store input exits as frontend-safe state."""

    id: str
    path: str
    name: str
    available: bool
    scan_status: str
    can_chat: bool = False
    can_edit_plan: bool = False
    readiness_reason: str | None = None
    last_opened_at: datetime


class ProjectListSchema(BaseModel):
    """Serialize project history; project list input exits under a stable envelope."""

    projects: list[ProjectReadSchema]
