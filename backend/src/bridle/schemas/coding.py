"""Schemas for agent coding orchestration."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

HeartbeatStatusLiteral = Literal["running", "waiting_tool", "retrying", "blocked"]


class CodingSessionCreateSchema(BaseModel):
    plan_id: str
    auto_continue_budget: int | None = None


class CodingSessionReadSchema(BaseModel):
    session_id: str
    plan_id: str
    status: str
    mode: str
    auto_continue_budget: int
    auto_continue_used: int
    created_at: datetime
    capabilities: list[str]
    main_agent_container: dict | None = None

    model_config = {"from_attributes": True}


class EligibleNodeSchema(BaseModel):
    node_id: str
    plan_node_id: str
    status: str
    title: str


class BlockedNodeSchema(BaseModel):
    node_id: str
    plan_node_id: str
    status: str
    reason: str
    blocked_by: list[str] = Field(default_factory=list)


class EligibleNodesResponseSchema(BaseModel):
    session_id: str
    eligible_nodes: list[EligibleNodeSchema]
    blocked_nodes: list[BlockedNodeSchema]


class SelectNodeIntentSchema(BaseModel):
    intent: str = "select_node"
    node_id: str
    reason: str = ""
    expected_action: str = "create_proposal"


class NodeAgentRunReadSchema(BaseModel):
    run_id: str
    session_id: str
    node_id: str
    plan_node_id: str
    status: str
    phase: str
    attempt: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    timeout_at: datetime | None = None
    duration_ms: int | None = None
    blocked_reason: str | None = None
    result_summary: str | None = None
    container_id: str | None = None
    container_status: str | None = None
    container_health: str | None = None
    container_error: str | None = None
    container_logs_summary: str | None = None
    diagnostic_path: str | None = None
    error_code: str | None = None
    test_summary: str | None = None
    metrics_summary: str | None = None
    integration_result: dict | None = None

    model_config = {"from_attributes": True}


class HeartbeatSchema(BaseModel):
    run_id: str
    node_id: str
    status: HeartbeatStatusLiteral
    phase: str
    message: str = Field(default="", max_length=500)
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    last_error: str | None = Field(default=None, max_length=200)
    blocked_reason: str | None = Field(default=None, max_length=500)
    next_action: str | None = Field(default=None, max_length=200)

    @field_validator("last_error")
    @classmethod
    def _truncate_error(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v[:200]


class NodeAgentResultSubmitSchema(BaseModel):
    run_id: str
    node_id: str
    status: str
    result_type: str
    proposal_id: str | None = None
    summary: str = ""
    confidence: float | None = None
    issues: list = Field(default_factory=list)
    recommended_next_action: str | None = None
