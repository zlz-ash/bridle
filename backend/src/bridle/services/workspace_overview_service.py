"""Workspace overview aggregation for planner context."""
from __future__ import annotations

from pathlib import Path

from bridle.services.workspace_file_service import DENIED_WORKSPACE_PREFIXES

OVERVIEW_EXCERPT_NAMES = ("README.md", "package.json", "pyproject.toml", "requirements.txt")


class WorkspaceOverviewService:
    @staticmethod
    def _path_allowed(rel_posix: str) -> bool:
        for prefix in DENIED_WORKSPACE_PREFIXES:
            bare = prefix.rstrip("/")
            if rel_posix == bare or rel_posix.startswith(prefix):
                return False
        return True

    @staticmethod
    def summarize(
        workspace: Path,
        *,
        max_files: int = 50,
        max_excerpt_bytes: int = 4096,
    ) -> dict:
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
