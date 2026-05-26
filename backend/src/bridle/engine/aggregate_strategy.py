"""AggregateMergeStrategy — deterministic, auditable merge policy for aggregate files."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

DuplicatePolicy = Literal["reject", "last_wins"]
MergeStrategyName = Literal["json_list"]


@dataclass(frozen=True)
class AggregateMergeStrategy:
    aggregate_target: str
    merge_strategy: MergeStrategyName = "json_list"
    contribution_schema: dict[str, str] = field(default_factory=dict)
    unique_key: str = ""
    sort_key: str | None = None
    duplicate_policy: DuplicatePolicy = "reject"
    validation_commands: list[str] = field(default_factory=list)
