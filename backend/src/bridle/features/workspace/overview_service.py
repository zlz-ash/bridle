"""Workspace overview aggregation for planner context."""
from __future__ import annotations

import hashlib
from pathlib import Path

from bridle.features.workspace.file_service import DENIED_WORKSPACE_PREFIXES

OVERVIEW_EXCERPT_NAMES = ("README.md", "package.json", "pyproject.toml", "requirements.txt")
MAP_DENIED_PREFIXES = (
    ".bridle/",
    ".git/",
    ".venv/",
    "node_modules/",
    "__pycache__/",
    ".pytest-tmp/",
    ".test-workspaces/",
    "dist/",
    "build/",
    "coverage/",
    "e2e-runs/",
    "e2e-generated/",
)


class WorkspaceOverviewService:
    @staticmethod
    def _path_allowed(rel_posix: str) -> bool:
        """Check one relative path; input is POSIX text and output is a scan/read decision."""
        for prefix in DENIED_WORKSPACE_PREFIXES:
            bare = prefix.rstrip("/")
            if rel_posix == bare or rel_posix.startswith(prefix):
                return False
        return True

    @staticmethod
    def scan_entities(workspace: Path) -> list[dict]:
        """Scan a workspace; input is its root and output is stable directory/file entities."""
        root = workspace.resolve()
        if not root.exists():
            return []

        entities: list[dict] = []
        for path in root.rglob("*"):
            rel = path.relative_to(root).as_posix()
            if not WorkspaceOverviewService._map_path_allowed(rel):
                continue
            if not path.is_dir() and not path.is_file():
                continue
            kind = "directory" if path.is_dir() else "file"
            parent_path = Path(rel).parent.as_posix()
            parent_id = None if parent_path == "." else WorkspaceOverviewService._entity_id(
                "directory", parent_path,
            )
            entities.append(
                {
                    "id": WorkspaceOverviewService._entity_id(kind, rel),
                    "path": rel,
                    "kind": kind,
                    "name": path.name,
                    "parent_id": parent_id,
                    "payload": {},
                }
            )
        return sorted(entities, key=lambda item: (item["path"], item["kind"]))

    @staticmethod
    def scan_paths(workspace: Path, rel_paths: list[str]) -> list[dict]:
        """Scan explicit paths and ancestors; workspace/path input returns only incremental entities."""
        root = workspace.resolve()
        entities: dict[str, dict] = {}
        for rel in sorted(set(rel_paths)):
            candidate_paths = [
                parent.as_posix()
                for parent in reversed(Path(rel).parents)
                if parent.as_posix() != "."
            ]
            candidate_paths.append(rel)
            for candidate in candidate_paths:
                if not WorkspaceOverviewService._map_path_allowed(candidate):
                    continue
                target = root.joinpath(*candidate.split("/"))
                if not target.exists() or (not target.is_dir() and not target.is_file()):
                    continue
                kind = "directory" if target.is_dir() else "file"
                parent_path = Path(candidate).parent.as_posix()
                parent_id = None if parent_path == "." else WorkspaceOverviewService._entity_id(
                    "directory", parent_path,
                )
                entity = {
                    "id": WorkspaceOverviewService._entity_id(kind, candidate),
                    "path": candidate,
                    "kind": kind,
                    "name": target.name,
                    "parent_id": parent_id,
                    "payload": {},
                }
                entities[entity["id"]] = entity
        return sorted(entities.values(), key=lambda item: (item["path"], item["kind"]))

    @staticmethod
    def _map_path_allowed(rel_posix: str) -> bool:
        """Filter one map path; input is relative text and output excludes metadata/build trees."""
        normalized = rel_posix.rstrip("/")
        for prefix in MAP_DENIED_PREFIXES:
            bare = prefix.rstrip("/")
            if normalized == bare or normalized.startswith(prefix):
                return False
        return True

    @staticmethod
    def _entity_id(kind: str, rel_posix: str, symbol: str | None = None) -> str:
        """Derive an entity key; kind/path[/symbol] input produces a deterministic compact ID.

        Two-argument calls keep their historical hash (file/dir IDs are unchanged); passing a
        symbol qualified name folds it in so per-file symbols get stable distinct IDs.
        """
        key = f"{kind}:{rel_posix}" if symbol is None else f"{kind}:{rel_posix}:{symbol}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"code-{digest[:24]}"

    @staticmethod
    def summarize(
        workspace: Path,
        *,
        max_files: int = 50,
        max_excerpt_bytes: int = 4096,
    ) -> dict:
        """Summarize a workspace; input bounds files/excerpts and output is planner-safe context."""
        workspace_resolved = workspace.resolve()
        all_files: list[str] = []
        if workspace_resolved.exists():
            for path in workspace_resolved.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(workspace_resolved).as_posix()
                if WorkspaceOverviewService._path_allowed(rel):
                    all_files.append(rel)
        all_files.sort()
        file_count = len(all_files)
        files = all_files[:max_files]
        excerpts: dict[str, str] = {}
        for name in OVERVIEW_EXCERPT_NAMES:
            match = next(
                (f for f in all_files if f == name or f.endswith("/" + name)),
                None,
            )
            if match is None:
                continue
            target = workspace_resolved / match
            raw = target.read_bytes()[:max_excerpt_bytes]
            excerpts[match] = raw.decode("utf-8", errors="replace")
        return {
            "is_empty": file_count == 0,
            "file_count": file_count,
            "files": files,
            "excerpts": excerpts,
        }

