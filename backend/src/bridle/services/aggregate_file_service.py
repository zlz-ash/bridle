"""Merge aggregate file contributions produced by node containers."""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from bridle.engine.aggregate_strategy import AggregateMergeStrategy
from bridle.schemas.node import _validate_workspace_relative_path

logger = logging.getLogger("bridle")


class AggregateFileService:
    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def merge_with_strategy(
        self,
        *,
        strategy: AggregateMergeStrategy,
        contribution_paths: list[str],
    ) -> dict[str, Any]:
        if strategy.merge_strategy != "json_list":
            raise ValueError(f"Unknown merge strategy: {strategy.merge_strategy}")

        logger.info(
            "aggregate_strategy_validated",
            extra={
                "action": "aggregate_strategy_validated",
                "status": "completed",
                "detail": {
                    "aggregate_target": strategy.aggregate_target,
                    "merge_strategy": strategy.merge_strategy,
                    "unique_key": strategy.unique_key,
                    "duplicate_policy": strategy.duplicate_policy,
                },
            },
        )

        target = _validate_workspace_relative_path(strategy.aggregate_target)
        normalized_contributions = [
            _validate_workspace_relative_path(path) for path in contribution_paths
        ]

        merged: list[dict[str, Any]] = []
        seen: dict[Any, tuple[str, int]] = {}
        for contribution in normalized_contributions:
            payload = json.loads((self.workspace_root / contribution).read_text(encoding="utf-8"))
            items = payload.get("items")
            if not isinstance(items, list):
                raise ValueError(f"aggregate contribution must contain an items list: {contribution}")
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(f"aggregate item must be an object: {contribution}")
                if strategy.contribution_schema:
                    for field_name in strategy.contribution_schema:
                        if field_name not in item:
                            raise ValueError(
                                f"aggregate item missing required field {field_name}: {contribution}"
                            )
                key = item.get(strategy.unique_key)
                if key is None:
                    raise ValueError(f"aggregate item missing unique key {strategy.unique_key}: {contribution}")
                if key in seen:
                    if strategy.duplicate_policy == "reject":
                        raise ValueError(
                            f"duplicate aggregate item {strategy.unique_key}={key!r} from {contribution}; first seen in {seen[key][0]}"
                        )
                    elif strategy.duplicate_policy == "last_wins":
                        orig_contribution, orig_index = seen[key]
                        merged[orig_index] = item
                        seen[key] = (contribution, orig_index)
                        continue
                seen[key] = (contribution, len(merged))
                merged.append(item)

        if strategy.sort_key:
            merged.sort(key=lambda item: str(item.get(strategy.sort_key, "")))

        target_path = self.workspace_root / target
        previous_content: str | None = None
        if target_path.exists():
            previous_content = target_path.read_text(encoding="utf-8")
        merged_text = json.dumps(merged, indent=2, ensure_ascii=False)
        candidate_dir = self.workspace_root / ".aicoding" / "aggregate-candidates"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = candidate_dir / target.replace("/", "__")
        candidate_path.write_text(merged_text, encoding="utf-8")

        if strategy.validation_commands:
            for cmd in strategy.validation_commands:
                command = (
                    cmd.replace("{candidate}", f'"{candidate_path}"')
                    .replace("{target}", f'"{target_path}"')
                    .replace("{workspace}", f'"{self.workspace_root}"')
                )
                env = {
                    **os.environ,
                    "BRIDLE_AGGREGATE_CANDIDATE": str(candidate_path),
                    "BRIDLE_AGGREGATE_TARGET": str(target_path),
                    "BRIDLE_WORKSPACE_ROOT": str(self.workspace_root),
                }
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=str(self.workspace_root),
                    env=env,
                )
                if result.returncode != 0:
                    try:
                        if candidate_path.exists():
                            candidate_path.unlink()
                    except OSError:
                        pass
                    diag_dir = self.workspace_root / ".aicoding" / "aggregate-diagnostics"
                    diag_dir.mkdir(parents=True, exist_ok=True)
                    (diag_dir / f"{target.replace('/', '_')}.validation.log").write_text(
                        f"command: {command}\nexit_code: {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}\n",
                        encoding="utf-8",
                    )
                    raise ValueError(
                        f"validation_command_failed: {command} (exit_code={result.returncode})"
                    )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(candidate_path.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            candidate_path.unlink()
        except OSError:
            pass

        logger.info(
            "aggregate_file_merged",
            extra={
                "action": "aggregate_file_merged",
                "status": "completed",
                "detail": {
                    "aggregate_target": target,
                    "contribution_count": len(normalized_contributions),
                    "item_count": len(merged),
                    "merge_strategy": strategy.merge_strategy,
                    "duplicate_policy": strategy.duplicate_policy,
                },
            },
        )
        return {
            "status": "merged",
            "aggregate_target": target,
            "contribution_count": len(normalized_contributions),
            "item_count": len(merged),
        }

    def merge_json_list(
        self,
        *,
        aggregate_target: str,
        contribution_paths: list[str],
        unique_key: str,
        sort_key: str | None = None,
    ) -> dict[str, Any]:
        strategy = AggregateMergeStrategy(
            aggregate_target=aggregate_target,
            merge_strategy="json_list",
            unique_key=unique_key,
            sort_key=sort_key,
            duplicate_policy="reject",
        )
        return self.merge_with_strategy(
            strategy=strategy,
            contribution_paths=contribution_paths,
        )
