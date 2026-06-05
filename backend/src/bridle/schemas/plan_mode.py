"""Schemas for Plan Mode converse API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bridle.schemas.plan import PlanImportSchema


class ChatTurnSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=0)


class PlanModeConverseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history: list[ChatTurnSchema] = Field(default_factory=list)
    workspace_overview: dict = Field(default_factory=dict)


class PlanModeResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str
    proposed_plan: PlanImportSchema | None = None
    parse_error: str | None = None
    raw_finish_reason: str | None = None
