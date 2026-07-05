"""Project-map patch schemas for `.bridle/plan.db`."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanNodePatchSchema(BaseModel):
    """Node payload accepted by the local project-map patch endpoint."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    parent_id: str | None = None
    order: int = 0
    node_type: str = "task"
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class NodeUpdateSchema(BaseModel):
    """Partial update for an existing project-map node."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    parent_id: str | None = None
    order: int | None = None
    node_type: str | None = None
    title: str | None = None
    goal: str | None = None
    depends_on: list[str] | None = None


class DependencyReplaceSchema(BaseModel):
    """Complete replacement of one node dependency list."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class PlanPatchSchema(BaseModel):
    """Patch a local project map without replacing the whole database."""

    model_config = ConfigDict(extra="forbid")

    update_nodes: list[NodeUpdateSchema] = Field(default_factory=list)
    add_nodes: list[PlanNodePatchSchema] = Field(default_factory=list)
    remove_node_ids: list[str] = Field(default_factory=list)
    replace_dependencies: list[DependencyReplaceSchema] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reject_dependency_updates_in_node_payload(self) -> PlanPatchSchema:
        for node in self.update_nodes:
            values: dict[str, Any] = node.model_dump(exclude_unset=True)
            if "depends_on" in values:
                raise ValueError("Use replace_dependencies to update node dependencies")
        return self
