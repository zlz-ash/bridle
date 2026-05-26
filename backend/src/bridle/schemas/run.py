"""Run schemas."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RunReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    node_id: str
    status: str
    exit_code: int | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
