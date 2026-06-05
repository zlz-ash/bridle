"""Health check response schema."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class HealthResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    version: str
    workspace: str
    db: str
    uptime_seconds: int
    events_subscribers: int
