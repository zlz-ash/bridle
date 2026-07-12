"""Project-map API request schemas."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ArbitrationResolveSchema(BaseModel):
    """Validate one human arbitration decision."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["accepted", "rejected", "manual"]
    resolution: dict[str, Any] = Field(default_factory=dict)
    actor: Literal["human"] = "human"


class ExecutionRefreshSchema(BaseModel):
    """Validate execution completion map refresh input."""

    model_config = ConfigDict(extra="forbid")
    execution_node_id: str = Field(min_length=1)
    changed_paths: list[str] = Field(min_length=1)
    execution_summary: str = Field(min_length=1, max_length=10_000)
    test_summary: str = Field(min_length=1, max_length=10_000)


class ModuleCandidateStatusSchema(BaseModel):
    """Validate a human module-candidate decision."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed", "rejected"]
    actor: Literal["human"] = "human"


class InterfaceCandidateStatusSchema(BaseModel):
    """Validate a human interface-candidate decision."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["confirmed", "rejected"]
    actor: Literal["human"] = "human"
