"""Workspace file read response schema."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WorkspaceFileReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    size: int
    mtime: str
    encoding: str
    content: str
    truncated: bool = False
