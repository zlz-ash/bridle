"""Structured negotiation decision from plan-mode AI."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, TypeAdapter

NegotiationActionLiteral = Literal["merge", "expand", "split", "accept_as_is", "replan"]


class MergeDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_ids: list[str] = Field(min_length=2)
    new_title: str = Field(min_length=1)
    new_goal: str = Field(min_length=1)
    new_estimated_minutes: int = Field(ge=1, le=600)
    merged_files: list[str] = Field(default_factory=list)
    merged_depends_on: list[str] = Field(default_factory=list)


class ExpandDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    new_goal: str = Field(min_length=1)
    new_acceptance_scope: str = Field(min_length=1)
    new_estimated_minutes: int = Field(ge=1, le=600)
    additional_files: list[str] = Field(default_factory=list)
    new_tests: list[str] = Field(default_factory=list)


class SplitChildNodePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    estimated_minutes: int = Field(ge=1, le=600)
    files: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    node_type: str = "code_change"
    tests: list[str] = Field(default_factory=list)


class SplitDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    into: list[SplitChildNodePayload] = Field(min_length=2)


class AcceptAsIsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ReplanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)


class MergeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["merge"]
    merge: MergeDecisionPayload


class ExpandDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["expand"]
    expand: ExpandDecisionPayload


class SplitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["split"]
    split: SplitDecisionPayload


class AcceptAsIsDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["accept_as_is"]
    accept_as_is: AcceptAsIsPayload


class ReplanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["replan"]
    replan: ReplanPayload


NegotiationDecision = Annotated[
    Union[
        MergeDecision,
        ExpandDecision,
        SplitDecision,
        AcceptAsIsDecision,
        ReplanDecision,
    ],
    Discriminator("action"),
]

_negotiation_decision_adapter = TypeAdapter(NegotiationDecision)


def validate_negotiation_decision(data: object) -> NegotiationDecision:
    """Parse a negotiation decision dict into the action-specific model."""
    return _negotiation_decision_adapter.validate_python(data)
