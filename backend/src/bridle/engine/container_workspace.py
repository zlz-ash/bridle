"""Build minimal per-node workspaces for container execution."""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridle.schemas.node import _validate_workspace_relative_path

logger = logging.getLogger("bridle")


@dataclass(frozen=True)
class ContainerWorkspaceResult:
    root: Path
    manifest_path: Path
    write_dir: Path
    read_dir: Path
    baseline_dir: Path
    output_dir: Path
    aggregate_dir: Path


class ContainerWorkspaceBuilder:
    """Create the host-side directory tree mounted into one node container."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def build_node_workspace(
        self,
        *,
        run_id: str,
        node_id: str,
        read_set: list[str],
        write_set: list[str],
        readonly_context: list[str],
        interfaces: dict | list,
        tests: list[str],
        metrics: dict | list,
        conflict_contributions: list[dict[str, Any]],
    ) -> ContainerWorkspaceResult:
        root = self.workspace_root / ".aicoding" / "container-workspaces" / run_id
        paths = self._workspace_paths(root)
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)

        normalized_read = self._normalize_many([*read_set, *readonly_context])
        normalized_write = self._normalize_many(write_set)
        contributions = [self._normalize_contribution(item) for item in conflict_contributions]

        self._copy_files(normalized_write, paths["write"])
        self._copy_files(normalized_write, paths["baseline"])
        self._copy_files(normalized_read, paths["read"])
        self._write_json(paths["interfaces"] / "interfaces.json", interfaces)
        self._write_json(paths["tests"] / "tests.json", {"tests": tests})
        self._write_json(paths["metrics"] / "metrics.json", metrics)
        for contribution in contributions:
            target_dir = paths["aggregate"] / Path(contribution["contribution_path"]).parent
            target_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "run_id": run_id,
            "node_id": node_id,
            "mounts": {
                "write": normalized_write,
                "read": normalized_read,
                "baseline": normalized_write,
                "aggregate": [item["contribution_path"] for item in contributions],
            },
            "container_paths": {
                "write": "/container/workspace/write",
                "read": "/container/workspace/read",
                "baseline": "/container/workspace/baseline",
                "interfaces": "/container/interfaces",
                "tests": "/container/tests",
                "metrics": "/container/metrics",
                "aggregate": "/container/aggregate",
                "output": "/container/output",
                "tmp": "/container/tmp",
            },
        }
        manifest_path = root / "workspace-manifest.json"
        self._write_json(manifest_path, manifest)

        logger.info(
            "container_workspace_built",
            extra={
                "action": "container_workspace_built",
                "status": "completed",
                "detail": {
                    "run_id": run_id,
                    "node_id": node_id,
                    "root": str(root),
                    "write_count": len(normalized_write),
                    "read_count": len(normalized_read),
                },
            },
        )
        return ContainerWorkspaceResult(
            root=root,
            manifest_path=manifest_path,
            write_dir=paths["write"],
            read_dir=paths["read"],
            baseline_dir=paths["baseline"],
            output_dir=paths["output"],
            aggregate_dir=paths["aggregate"],
        )

    def _workspace_paths(self, root: Path) -> dict[str, Path]:
        return {
            "write": root / "workspace" / "write",
            "read": root / "workspace" / "read",
            "baseline": root / "workspace" / "baseline",
            "interfaces": root / "interfaces",
            "tests": root / "tests",
            "metrics": root / "metrics",
            "aggregate": root / "aggregate",
            "output": root / "output",
            "tmp": root / "tmp",
        }

    def _normalize_many(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for value in values:
            path = _validate_workspace_relative_path(value)
            if path not in seen:
                seen.add(path)
                normalized.append(path)
        return normalized

    def _normalize_contribution(self, item: dict[str, Any]) -> dict[str, str]:
        return {
            "aggregate_target": _validate_workspace_relative_path(str(item["aggregate_target"])),
            "contribution_path": _validate_workspace_relative_path(str(item["contribution_path"])),
        }

    def _copy_files(self, paths: list[str], destination_root: Path) -> None:
        for relative in paths:
            source = self.workspace_root / Path(relative)
            if not source.exists():
                continue
            if not source.is_file():
                continue
            destination = destination_root / Path(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
