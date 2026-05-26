"""Node import and read schemas."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Node types as Literal for strict JSON validation
NodeTypeLiteral = Literal["code_change", "test_validation", "metric_validation", "review_gate"]

NodeStatusLiteral = Literal[
    "pending",
    "blocked",
    "ready",
    "running",
    "completed",
    "failed",
    "failed_retryable",
    "missing_evidence",
    "needs_review",
    "needs_review_retryable",
    "archived",
]

ContainerNetworkModeLiteral = Literal["bridge", "none"]
CONTAINER_BOUNDARY_KEY = "__container_boundary__"
ORIGINAL_CONSTRAINTS_KEY = "__original_constraints__"

_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


def _is_snake_case(s: str) -> bool:
    return bool(_SNAKE_CASE_RE.match(s))


def _validate_workspace_relative_path(path: str) -> str:
    text = str(path).strip()
    if not text:
        raise ValueError("path must be non-empty")
    if "\\" in text:
        raise ValueError(f"path must use forward slashes: {text}")
    if text.startswith("/") or (len(text) >= 2 and text[1] == ":") or ".." in text.split("/"):
        raise ValueError(f"path must be workspace-relative: {text}")
    return text


class WorkspacePathSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _validate_path(self) -> WorkspacePathSchema:
        self.path = _validate_workspace_relative_path(self.path)
        return self


class AggregateContributionSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aggregate_target: str = Field(min_length=1, max_length=1000)
    contribution_path: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _validate_paths(self) -> AggregateContributionSchema:
        self.aggregate_target = _validate_workspace_relative_path(self.aggregate_target)
        self.contribution_path = _validate_workspace_relative_path(self.contribution_path)
        return self


class ContainerPolicySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    network_mode: ContainerNetworkModeLiteral = "bridge"
    env_allowlist: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    health_check: dict | list = Field(default_factory=dict)


def pack_container_boundary(
    constraints: dict | list,
    *,
    read_set: list[str],
    write_set: list[str],
    readonly_context: list[str],
    conflict_contributions: list[AggregateContributionSchema],
    container_policy: ContainerPolicySchema,
) -> dict | list:
    boundary = {
        "read_set": read_set,
        "write_set": write_set,
        "readonly_context": readonly_context,
        "conflict_contributions": [item.model_dump() for item in conflict_contributions],
        "container_policy": container_policy.model_dump(),
    }
    if isinstance(constraints, dict):
        packed = dict(constraints)
        packed[CONTAINER_BOUNDARY_KEY] = boundary
        return packed
    return {
        ORIGINAL_CONSTRAINTS_KEY: constraints,
        CONTAINER_BOUNDARY_KEY: boundary,
    }


def unpack_container_boundary(raw_constraints: dict | list) -> tuple[dict | list, dict]:
    if not isinstance(raw_constraints, dict):
        return raw_constraints, {}
    boundary = raw_constraints.get(CONTAINER_BOUNDARY_KEY) or {}
    if ORIGINAL_CONSTRAINTS_KEY in raw_constraints:
        return raw_constraints[ORIGINAL_CONSTRAINTS_KEY], boundary
    clean = {k: v for k, v in raw_constraints.items() if k != CONTAINER_BOUNDARY_KEY}
    return clean, boundary


# ---------------------------------------------------------------------------
# Interface contract sub-models
# ---------------------------------------------------------------------------


class InterfaceFieldSchema(BaseModel):
    """A single field exposed by an interface."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=100)
    required: bool = False
    description: str = ""

    @model_validator(mode="after")
    def _validate_name(self) -> InterfaceFieldSchema:
        if not _is_snake_case(self.name):
            raise ValueError(f"Field name '{self.name}' must be snake_case")
        return self


class InterfaceEndpointSchema(BaseModel):
    """A single endpoint exposed by an interface."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    method: str = Field(min_length=1, max_length=20)
    path: str = Field(min_length=1, max_length=500)
    description: str = ""

    @model_validator(mode="after")
    def _validate_name(self) -> InterfaceEndpointSchema:
        if not _is_snake_case(self.name):
            raise ValueError(f"Endpoint name '{self.name}' must be snake_case")
        return self


class InterfaceExposeSchema(BaseModel):
    """An interface that this node exposes to adjacent nodes."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    fields: list[InterfaceFieldSchema] = Field(default_factory=list)
    endpoints: list[InterfaceEndpointSchema] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_name_and_dupes(self) -> InterfaceExposeSchema:
        if not _is_snake_case(self.name):
            raise ValueError(f"Expose name '{self.name}' must be snake_case")
        field_names = [f.name for f in self.fields]
        if len(field_names) != len(set(field_names)):
            raise ValueError(f"Duplicate field names in expose '{self.name}'")
        ep_names = [e.name for e in self.endpoints]
        if len(ep_names) != len(set(ep_names)):
            raise ValueError(f"Duplicate endpoint names in expose '{self.name}'")
        return self


class InterfaceConsumeSchema(BaseModel):
    """A consumption of an adjacent node's exposed interface."""
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1)
    interface_name: str = Field(min_length=1, max_length=200)
    fields: list[str] = Field(default_factory=list)
    endpoints: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_no_dupes(self) -> InterfaceConsumeSchema:
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("Duplicate field names in consume")
        if len(self.endpoints) != len(set(self.endpoints)):
            raise ValueError("Duplicate endpoint names in consume")
        return self


class NodeInterfacesSchema(BaseModel):
    """The full interfaces contract for a node."""
    model_config = ConfigDict(extra="forbid")

    exposes: list[InterfaceExposeSchema] = Field(default_factory=list)
    consumes: list[InterfaceConsumeSchema] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_no_duplicate_exposes(self) -> NodeInterfacesSchema:
        expose_names = [e.name for e in self.exposes]
        if len(expose_names) != len(set(expose_names)):
            raise ValueError("Duplicate expose names in interfaces")
        return self


# ---------------------------------------------------------------------------
# Node schemas
# ---------------------------------------------------------------------------


class NodeImportSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    goal: str = Field(min_length=1)
    node_type: NodeTypeLiteral
    depends_on: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    metrics: dict | list = Field(default_factory=dict)
    constraints: dict | list = Field(default_factory=dict)
    review_checks: list[str] = Field(default_factory=list)
    expected_outputs: dict | list = Field(default_factory=dict)
    interfaces: NodeInterfacesSchema = Field(default_factory=NodeInterfacesSchema)
    read_set: list[str] = Field(default_factory=list)
    write_set: list[str] = Field(default_factory=list)
    readonly_context: list[str] = Field(default_factory=list)
    conflict_contributions: list[AggregateContributionSchema] = Field(default_factory=list)
    container_policy: ContainerPolicySchema = Field(default_factory=ContainerPolicySchema)

    @model_validator(mode="after")
    def _validate_container_paths(self) -> NodeImportSchema:
        self.files = [_validate_workspace_relative_path(p) for p in self.files]
        self.read_set = [_validate_workspace_relative_path(p) for p in self.read_set]
        self.write_set = [_validate_workspace_relative_path(p) for p in self.write_set]
        self.readonly_context = [_validate_workspace_relative_path(p) for p in self.readonly_context]
        if not self.write_set:
            self.write_set = list(self.files)
        if not self.read_set:
            self.read_set = list(dict.fromkeys([*self.files, *self.write_set]))
        if not self.files and self.write_set:
            self.files = list(self.write_set)
        return self


class NodeReadSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    plan_id: str
    plan_node_id: str
    title: str
    goal: str
    node_type: str
    order: int
    depends_on: list
    files: list
    tests: list
    metrics: dict | list
    constraints: dict | list
    review_checks: list
    expected_outputs: dict | list
    interfaces: dict | list = Field(default_factory=dict)
    read_set: list = Field(default_factory=list)
    write_set: list = Field(default_factory=list)
    readonly_context: list = Field(default_factory=list)
    conflict_contributions: list = Field(default_factory=list)
    container_policy: dict | list = Field(default_factory=dict)
    status: str
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _unpack_container_boundary(cls, data):
        if isinstance(data, dict):
            source = dict(data)
        else:
            source = {
                "id": getattr(data, "id"),
                "plan_id": getattr(data, "plan_id"),
                "plan_node_id": getattr(data, "plan_node_id"),
                "title": getattr(data, "title"),
                "goal": getattr(data, "goal"),
                "node_type": getattr(data, "node_type"),
                "order": getattr(data, "order"),
                "depends_on": getattr(data, "depends_on"),
                "files": getattr(data, "files"),
                "tests": getattr(data, "tests"),
                "metrics": getattr(data, "metrics"),
                "constraints": getattr(data, "constraints"),
                "review_checks": getattr(data, "review_checks"),
                "expected_outputs": getattr(data, "expected_outputs"),
                "interfaces": getattr(data, "interfaces"),
                "status": getattr(data, "status"),
                "created_at": getattr(data, "created_at"),
                "updated_at": getattr(data, "updated_at"),
            }
        constraints, boundary = unpack_container_boundary(source.get("constraints", {}))
        source["constraints"] = constraints
        source.setdefault("read_set", boundary.get("read_set", []))
        source.setdefault("write_set", boundary.get("write_set", []))
        source.setdefault("readonly_context", boundary.get("readonly_context", []))
        source.setdefault("conflict_contributions", boundary.get("conflict_contributions", []))
        source.setdefault("container_policy", boundary.get("container_policy", {}))
        return source
