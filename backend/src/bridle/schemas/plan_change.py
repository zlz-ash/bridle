"""Schemas for plan change proposals."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PlanChangeOperationSchema(BaseModel):
    operation: str
    node_id: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class PlanChangeProposalCreateSchema(BaseModel):
    plan_id: str
    proposal_type: str = "plan_change"
    change_set: list[PlanChangeOperationSchema]
    risk_level: str = "low"
    requires_human_review: bool = True


class PlanChangeProposalReadSchema(BaseModel):
    proposal_id: str
    plan_id: str
    proposal_type: str
    change_set: list[dict]
    risk_level: str
    requires_human_review: bool
    status: str
    created_at: datetime
    rejection_reason: str | None = None

    model_config = {"from_attributes": True}
