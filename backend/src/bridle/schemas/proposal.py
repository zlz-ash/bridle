"""Proposal schemas for agent dry-run proposals."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bridle.engine.proposal_path_validator import ProposalPathValidator

ChangeTypeLiteral = Literal["modify", "add", "remove"]

_POSIX_RELATIVE_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_./-]*$")


def _is_posix_relative(path: str) -> bool:
    """Check that path is workspace-relative POSIX with no traversal or absolute bits."""
    if not path or not isinstance(path, str):
        return False
    trimmed = path.strip()
    if not trimmed:
        return False
    if trimmed.startswith("/"):
        return False
    if len(trimmed) >= 3 and trimmed[1] == ":" and trimmed[2] in ("\\", "/"):
        return False
    if "\\" in trimmed:
        return False
    if ".." in trimmed.split("/"):
        return False
    if trimmed.startswith(".") and not trimmed.startswith("./"):
        return False
    canon = ProposalPathValidator.normalize_workspace_relative(trimmed)
    if not canon:
        return False
    return bool(_POSIX_RELATIVE_RE.match(canon))


# ---------------------------------------------------------------------------
# Provider output: strong-typed file patch + proposal
# ---------------------------------------------------------------------------


class FilePatchSchema(BaseModel):
    """A single file patch with strict path validation."""
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1000)
    change_type: ChangeTypeLiteral
    diff: str = ""

    @model_validator(mode="after")
    def _validate_path(self) -> FilePatchSchema:
        if not _is_posix_relative(self.path):
            raise ValueError(
                f"Path '{self.path}' must be a workspace-relative POSIX path "
                f"(no absolute, no backslash, no parent traversal)"
            )
        canonical = ProposalPathValidator.normalize_workspace_relative(self.path.strip())
        self.path = canonical
        return self


class AgentProposalSchema(BaseModel):
    """Strong-typed provider output."""
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    file_patches: list[FilePatchSchema] = Field(default_factory=list)
    tests_to_run: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider input: agent context boundary
# ---------------------------------------------------------------------------


class AgentContext(BaseModel):
    """Fixed input context passed to any AgentProvider.

    Includes everything the provider is allowed to see.
    Does NOT include: non-adjacent nodes, unlisted file contents,
    archived plan/node data, or run/evidence history.
    """
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1)
    node: dict
    allowed_files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    constraints: dict = Field(default_factory=dict)
    review_checks: list[str] = Field(default_factory=list)
    expected_outputs: dict = Field(default_factory=dict)
    accessible_context: dict = Field(default_factory=dict)
    tool_capabilities: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# API schemas
# ---------------------------------------------------------------------------


class ProposalCreateSchema(BaseModel):
    """Request body for creating an agent proposal."""
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1)


class ProposalReadSchema(BaseModel):
    """Response shape for an agent proposal."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    node_id: str
    plan_node_id: str
    status: str
    instruction: str
    allowed_files: list
    accessible_context: dict
    proposal: dict
    source: str
    created_at: datetime
    updated_at: datetime
