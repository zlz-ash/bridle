"""Plan import, patch, and read schemas with strict validation."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bridle.schemas.node import NodeImportSchema, NodeInterfacesSchema, NodeTypeLiteral, _validate_workspace_relative_path

PlanStatusLiteral = Literal["draft", "active", "completed", "failed", "archived"]


# ---------------------------------------------------------------------------
# Plan Import (full plan definition)
# ---------------------------------------------------------------------------

class AggregateFileSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_path: str = Field(min_length=1, max_length=1000)
    contribution_dir: str = Field(min_length=1, max_length=1000)
    merge_strategy: str = Field(min_length=1, max_length=100)
    owner: str = Field(min_length=1, max_length=200)
    contributors: list[str] = Field(default_factory=list)
    validation: dict | list = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_paths(self) -> AggregateFileSchema:
        self.target_path = _validate_workspace_relative_path(self.target_path)
        self.contribution_dir = _validate_workspace_relative_path(self.contribution_dir)
        return self


class PlanImportSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    aggregate_files: list[AggregateFileSchema] = Field(default_factory=list)
    nodes: list[NodeImportSchema] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_deps_exist(self) -> PlanImportSchema:
        """All depends_on references must point to existing node IDs."""
        node_ids = {n.id for n in self.nodes}
        for n in self.nodes:
            for dep in n.depends_on:
                if dep not in node_ids:
                    raise ValueError(f"Node {n.id} depends on unknown node {dep}")
        targets = [item.target_path for item in self.aggregate_files]
        if len(targets) != len(set(targets)):
            raise ValueError("Duplicate aggregate file target_path")
        aggregates = {item.target_path: item for item in self.aggregate_files}
        for node in self.nodes:
            for contribution in node.conflict_contributions:
                aggregate = aggregates.get(contribution.aggregate_target)
                if aggregate is None:
                    raise ValueError(
                        f"Node {node.id} contributes to undeclared aggregate target "
                        f"{contribution.aggregate_target}"
                    )
                if node.id not in aggregate.contributors:
                    raise ValueError(
                        f"Node {node.id} is not a contributor for aggregate target "
                        f"{contribution.aggregate_target}"
                    )
                prefix = aggregate.contribution_dir.rstrip("/") + "/"
                if not contribution.contribution_path.startswith(prefix):
                    raise ValueError(
                        f"Node {node.id} contribution_path must be under aggregate contribution_dir "
                        f"{aggregate.contribution_dir}"
                    )
        return self


# ---------------------------------------------------------------------------
# Plan Patch (partial update)
# ---------------------------------------------------------------------------

class NodeUpdateSchema(BaseModel):
    """Partial update for an existing node. Only specified fields are changed."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    node_type: NodeTypeLiteral | None = None
    title: str | None = None
    goal: str | None = None
    tests: list[str] | None = None
    metrics: dict | list | None = None
    constraints: dict | list | None = None
    review_checks: list[str] | None = None
    expected_outputs: dict | list | None = None
    interfaces: NodeInterfacesSchema | None = None
    read_set: list[str] | None = None
    write_set: list[str] | None = None
    readonly_context: list[str] | None = None
    conflict_contributions: list = None
    container_policy: dict | None = None

    @model_validator(mode="after")
    def _validate_container_boundary_fields(self) -> NodeUpdateSchema:
        if self.read_set is not None:
            self.read_set = [_validate_workspace_relative_path(p) for p in self.read_set]
        if self.write_set is not None:
            self.write_set = [_validate_workspace_relative_path(p) for p in self.write_set]
        if self.readonly_context is not None:
            self.readonly_context = [_validate_workspace_relative_path(p) for p in self.readonly_context]
        if self.conflict_contributions is not None:
            from bridle.schemas.node import AggregateContributionSchema

            self.conflict_contributions = [
                item if isinstance(item, AggregateContributionSchema) else AggregateContributionSchema(**item)
                for item in self.conflict_contributions
            ]
        if self.container_policy is not None:
            from bridle.schemas.node import ContainerPolicySchema

            self.container_policy = (
                self.container_policy
                if isinstance(self.container_policy, ContainerPolicySchema)
                else ContainerPolicySchema(**self.container_policy)
            )
        return self


class DependencyReplaceSchema(BaseModel):
    """Complete replacement of a node's dependency list."""
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)


class PlanPatchSchema(BaseModel):
    """Partial update to the current plan.

    Supports:
    - update_nodes: modify fields of existing nodes
    - add_nodes: add new nodes
    - remove_node_ids: archive nodes by ID
    - replace_dependencies: replace a node's dependency list
    """
    model_config = ConfigDict(extra="forbid")

    update_nodes: list[NodeUpdateSchema] = Field(default_factory=list)
    add_nodes: list[NodeImportSchema] = Field(default_factory=list)
    remove_node_ids: list[str] = Field(default_factory=list)
    replace_dependencies: list[DependencyReplaceSchema] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Plan Read
# ---------------------------------------------------------------------------

class PlanReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    goal: str
    aggregate_files: list = Field(default_factory=list)
    status: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Plan Summary (generated on full replacement)
# ---------------------------------------------------------------------------

class KeyNodeSummary(BaseModel):
    """Summary of a key node for the plan summary."""
    id: str
    title: str
    status: str
    node_type: str


class KeyTestResult(BaseModel):
    """Summary of a key test result."""
    node_id: str
    node_title: str
    exit_code: int | None
    duration_ms: int | None


class PlanSummarySchema(BaseModel):
    """Summary generated when a plan is replaced."""
    plan_id: str
    goal: str
    task_id: str
    replaced_at: datetime
    final_status: str
    node_count: int
    completed_count: int
    failed_count: int
    key_nodes: list[KeyNodeSummary] = Field(default_factory=list)
    key_test_results: list[KeyTestResult] = Field(default_factory=list)
    key_metrics: dict = Field(default_factory=dict)
