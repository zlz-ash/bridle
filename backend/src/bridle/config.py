"""Bridle configuration — workspace-anchored path resolution.

All paths are derived from an explicitly-specified workspace directory.
No path should ever fall back to cwd or system temp directories.
"""
from __future__ import annotations

from pathlib import Path


class WorkspaceConfig:
    """Centralised workspace configuration.

    Every path in the application is derived from the workspace root.
    The workspace must be specified explicitly — never inferred from cwd.
    """

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).resolve()
        if not self._workspace.is_dir():
            raise ValueError(f"Workspace directory does not exist: {self._workspace}")

    @property
    def workspace(self) -> Path:
        """The root workspace directory."""
        return self._workspace

    @property
    def aicoding_dir(self) -> Path:
        """The .aicoding directory under the workspace root."""
        p = self._workspace / ".aicoding"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        """SQLite database path."""
        return self.aicoding_dir / "db.sqlite3"

    @property
    def database_url(self) -> str:
        """SQLAlchemy async connection string."""
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def runs_dir(self) -> Path:
        p = self.aicoding_dir / "runs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def logs_dir(self) -> Path:
        p = self.aicoding_dir / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def reports_dir(self) -> Path:
        p = self.aicoding_dir / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def context_dir(self) -> Path:
        p = self.aicoding_dir / "context"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def current_plan_path(self) -> Path:
        """Path to the current-plan.json file mirror."""
        return self.aicoding_dir / "current-plan.json"

    @property
    def plan_summary_path(self) -> Path:
        """Path to the plan-summary.json file."""
        return self.aicoding_dir / "plan-summary.json"


# ---------------------------------------------------------------------------
# Global singleton — set once at startup, read everywhere else.
# ---------------------------------------------------------------------------

_global_config: WorkspaceConfig | None = None


def set_workspace(workspace: str | Path) -> WorkspaceConfig:
    """Set the global workspace configuration. Called once at startup."""
    global _global_config
    _global_config = WorkspaceConfig(workspace)
    return _global_config


def get_config() -> WorkspaceConfig:
    """Get the global workspace configuration.

    Raises RuntimeError if set_workspace() has not been called.
    """
    if _global_config is None:
        raise RuntimeError(
            "Workspace not configured. Call set_workspace() or use --workspace."
        )
    return _global_config
