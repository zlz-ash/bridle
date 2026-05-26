"""Build aggregate merge strategies from plan mirror."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bridle.engine.aggregate_strategy import AggregateMergeStrategy


def load_aggregate_strategies(workspace_root: str | Path) -> list[AggregateMergeStrategy]:
    plan_path = Path(workspace_root).resolve() / ".aicoding" / "current-plan.json"
    if not plan_path.exists():
        return []
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    strategies: list[AggregateMergeStrategy] = []
    for item in payload.get("aggregate_files", []):
        if not isinstance(item, dict):
            continue
        validation = item.get("validation") or {}
        if isinstance(validation, list):
            validation = {}
        duplicate_policy = validation.get("duplicate_policy", "reject")
        if duplicate_policy not in ("reject", "last_wins"):
            duplicate_policy = "reject"
        strategies.append(
            AggregateMergeStrategy(
                aggregate_target=str(item.get("target_path", "")),
                merge_strategy="json_list",
                unique_key=str(validation.get("unique_key", "")),
                sort_key=validation.get("sort_key"),
                duplicate_policy=duplicate_policy,
                contribution_schema=dict(validation.get("contribution_schema") or {}),
                validation_commands=list(validation.get("validation_commands") or []),
            )
        )
    return [s for s in strategies if s.aggregate_target and s.unique_key]


def map_contributions_by_target(
    manifest_contributions: list[dict[str, Any]],
    *,
    allowed_paths: list[str],
) -> dict[str, list[str]]:
    allowed = set(allowed_paths)
    grouped: dict[str, list[str]] = {}
    for entry in manifest_contributions:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path", "")).strip()
        target = str(entry.get("aggregate_target", "")).strip()
        if not path or not target or path not in allowed:
            continue
        grouped.setdefault(target, []).append(path)
    return grouped
