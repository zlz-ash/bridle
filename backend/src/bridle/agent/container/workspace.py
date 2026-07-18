"""Build map-driven candidate workspaces for container execution."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridle.agent.container.candidate_path_guard import (
    candidate_root as resolve_candidate_root,
)
from bridle.agent.container.candidate_path_guard import (
    module_execution_root,
    safe_rmtree,
    validate_safe_id,
)
from bridle.agent.container.workset import MapWorksetInput, ModuleWorksetResolver, WorksetFileEntry
from bridle.agent.tools.proposal_path_validator import ProposalPathValidator

logger = logging.getLogger("bridle")

_MANIFEST_SCHEMA = "bridle.candidate_workspace/v1"


@dataclass(frozen=True)
class ContainerWorkspaceResult:
    root: Path
    manifest_path: Path
    project_dir: Path
    baseline_dir: Path
    output_dir: Path
    diagnostics_dir: Path
    module_root: Path
    candidate_rel: str
    write_set: list[str]
    read_set: list[str]
    readonly_files: list[str]


class ContainerWorkspaceBuilder:
    """Create candidate directory trees under a stable module execution root."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def build_candidate_workspace(
        self,
        *,
        candidate_id: str,
        module_id: str,
        run_id: str,
        node_id: str,
        workset: MapWorksetInput,
    ) -> ContainerWorkspaceResult:
        safe_candidate = validate_safe_id(candidate_id, field="candidate_id")
        safe_module = validate_safe_id(module_id, field="module_id")

        resolver = ModuleWorksetResolver(self.workspace_root)
        resolved = resolver.resolve(workset)
        if resolved.error_code:
            raise ValueError(f"{resolved.error_code}: {resolved.error_detail}")

        module_root = module_execution_root(self.workspace_root, safe_module)
        root = resolve_candidate_root(self.workspace_root, safe_module, safe_candidate)
        paths = self._candidate_paths(root)
        module_root.mkdir(parents=True, exist_ok=True)
        root.mkdir(parents=True, exist_ok=True)
        for name, path in paths.items():
            if path.exists():
                if path.is_file():
                    raise ValueError("candidate_workspace_not_empty")
                if name != "diagnostics":
                    safe_rmtree(path, project_root=self.workspace_root, expected_root=root)
            path.mkdir(parents=True, exist_ok=True)

        baseline_hashes: dict[str, str] = {}
        for entry in resolved.entries:
            rel = entry.relative_path
            source = self._resolve_source(rel)
            if source is None:
                raise ValueError(f"module_boundary_incomplete: missing source for {rel}")
            dest = paths["mocks"] / rel if entry.entity_kind == "mock" else paths["project"] / rel
            self._copy_file(source, dest)
            if rel in resolved.write_set:
                baseline_dest = paths["baseline"] / rel
                self._copy_file(source, baseline_dest)
                baseline_hashes[rel] = hashlib.sha256(baseline_dest.read_bytes()).hexdigest()

        candidate_rel = f"candidates/{safe_candidate}"
        manifest = {
            "schema": _MANIFEST_SCHEMA,
            "candidate_id": safe_candidate,
            "module_id": safe_module,
            "run_id": run_id,
            "node_id": node_id,
            "write_set": resolved.write_set,
            "read_set": resolved.read_set,
            "readonly_files": resolved.readonly_files,
            "tests": resolved.tests,
            "baseline_hashes": baseline_hashes,
            "file_entries": [self._entry_dict(entry) for entry in resolved.entries],
            "interfaces": resolved.interfaces,
            "container_paths": {
                "module_root": "/workspace",
                "candidate_root": "/workspace",
                "project": "/workspace/project",
                "baseline": "/workspace/baseline",
                "output": "/workspace/output",
                "diagnostics": "/workspace/diagnostics",
                "mocks": "/workspace/mocks",
            },
            "ready": False,
        }
        manifest_path = root / "workspace-manifest.json"
        tmp_manifest = root / "workspace-manifest.json.tmp"
        self._write_json(tmp_manifest, manifest)
        tmp_manifest.replace(manifest_path)
        ready_manifest = dict(manifest)
        ready_manifest["ready"] = True
        self._write_json(manifest_path, ready_manifest)

        logger.info(
            "candidate_workspace_built",
            extra={
                "action": "candidate_workspace_built",
                "status": "completed",
                "detail": {
                    "candidate_id": safe_candidate,
                    "module_id": safe_module,
                    "run_id": run_id,
                    "node_id": node_id,
                    "root": str(root),
                    "write_count": len(resolved.write_set),
                    "read_count": len(resolved.read_set),
                },
            },
        )
        return ContainerWorkspaceResult(
            root=root,
            manifest_path=manifest_path,
            project_dir=paths["project"],
            baseline_dir=paths["baseline"],
            output_dir=paths["output"],
            diagnostics_dir=paths["diagnostics"],
            module_root=module_root,
            candidate_rel=candidate_rel,
            write_set=list(resolved.write_set),
            read_set=list(resolved.read_set),
            readonly_files=list(resolved.readonly_files),
        )

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
        """Legacy run-scoped workspace builder kept for transitional callers."""
        root = self.workspace_root / ".bridle" / "runtime" / "container-workspaces" / run_id
        paths = self._legacy_paths(root)
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)

        normalized_read = self._normalize_many([*read_set, *readonly_context])
        normalized_write = self._normalize_many(write_set)

        self._copy_many(normalized_write, paths["write"])
        self._copy_many(normalized_write, paths["baseline"])
        self._copy_many(normalized_read, paths["read"])
        self._write_json(paths["interfaces"] / "interfaces.json", interfaces)
        self._write_json(paths["tests"] / "tests.json", {"tests": tests})
        self._write_json(paths["metrics"] / "metrics.json", metrics)

        manifest_path = root / "workspace-manifest.json"
        self._write_json(
            manifest_path,
            {
                "run_id": run_id,
                "node_id": node_id,
                "mounts": {"write": normalized_write, "read": normalized_read, "baseline": normalized_write},
            },
        )
        return ContainerWorkspaceResult(
            root=root,
            manifest_path=manifest_path,
            project_dir=paths["write"],
            baseline_dir=paths["baseline"],
            output_dir=paths["output"],
            diagnostics_dir=root / "diagnostics",
            module_root=root,
            candidate_rel=".",
            write_set=normalized_write,
            read_set=normalized_read,
            readonly_files=list(readonly_context),
        )

    def persist_candidate_request(
        self,
        workspace: ContainerWorkspaceResult,
        request: Any,
    ) -> None:
        """Atomically bind the execution request to the existing workspace manifest."""
        manifest = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("candidate_id") != request.candidate_id
            or manifest.get("module_id") != request.module_id
            or manifest.get("node_id") != request.node_id
            or manifest.get("run_id") != request.run_id
        ):
            raise ValueError("candidate_workspace_request_identity_mismatch")
        manifest["candidate_request"] = request.to_dict()
        tmp_manifest = workspace.manifest_path.with_suffix(".json.tmp")
        self._write_json(tmp_manifest, manifest)
        tmp_manifest.replace(workspace.manifest_path)

    def _candidate_paths(self, root: Path) -> dict[str, Path]:
        return {
            "baseline": root / "baseline",
            "project": root / "project",
            "output": root / "output",
            "diagnostics": root / "diagnostics",
            "mocks": root / "mocks",
        }

    def _legacy_paths(self, root: Path) -> dict[str, Path]:
        return {
            "write": root / "workspace" / "write",
            "read": root / "workspace" / "read",
            "baseline": root / "workspace" / "baseline",
            "interfaces": root / "interfaces",
            "tests": root / "tests",
            "metrics": root / "metrics",
            "output": root / "output",
        }

    def _resolve_source(self, rel: str) -> Path | None:
        source = self.workspace_root / Path(*rel.split("/"))
        if source.is_symlink():
            try:
                resolved = source.resolve()
                resolved.relative_to(self.workspace_root)
                return resolved if resolved.is_file() else None
            except (OSError, ValueError):
                return None
        return source if source.is_file() else None

    def _copy_file(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if os.name != "nt":
            mode = source.stat().st_mode
            destination.chmod(stat.S_IMODE(mode))

    def _copy_many(self, paths: list[str], destination_root: Path) -> None:
        for relative in paths:
            source = self._resolve_source(relative)
            if source is None:
                continue
            self._copy_file(source, destination_root / Path(relative))

    def _normalize_many(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for value in values:
            path = self._validate_workspace_relative_path(value)
            if path not in seen:
                seen.add(path)
                normalized.append(path)
        return normalized

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _entry_dict(entry: WorksetFileEntry) -> dict[str, Any]:
        return {
            "relative_path": entry.relative_path,
            "source": entry.source,
            "entity_kind": entry.entity_kind,
            "module_id": entry.module_id,
            "interface_id": entry.interface_id,
            "mock_hash": entry.mock_hash,
            "entity_version": entry.entity_version,
        }

    @staticmethod
    def _validate_workspace_relative_path(path: str) -> str:
        normalized = ProposalPathValidator.normalize_workspace_relative(path)
        if not normalized or normalized.startswith("../") or normalized == "..":
            raise ValueError("Path must be workspace-relative")
        if path.startswith("/") or "\\" in path or (len(path) >= 2 and path[1] == ":"):
            raise ValueError("Path must be workspace-relative")
        if ".." in path.replace("\\", "/").split("/"):
            raise ValueError("Path must be workspace-relative")
        return normalized
