"""Task schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TaskStatusLiteral = Literal["created", "planned", "running", "completed", "failed"]


class TaskCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    goal: str | None = None
    status: TaskStatusLiteral = "created"


class TaskReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    goal: str | None
    status: str
    created_at: datetime
    updated_at: datetime
