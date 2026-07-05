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
    def runtime_dir(self) -> Path:
        """Return the Bridle runtime directory, creating it on first access."""
        p = self._workspace / ".bridle" / "runtime"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        """Return the application SQLite database path."""
        return self.runtime_dir / "db.sqlite3"

    @property
    def database_url(self) -> str:
        """SQLAlchemy async connection string."""
        return f"sqlite+aiosqlite:///{self.db_path.as_posix()}"

    @property
    def runs_dir(self) -> Path:
        """Return the runtime run-artifact directory."""
        p = self.runtime_dir / "runs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def logs_dir(self) -> Path:
        """Return the structured-log directory."""
        p = self.runtime_dir / "logs"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def reports_dir(self) -> Path:
        """Return the generated-report directory."""
        p = self.runtime_dir / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def context_dir(self) -> Path:
        """Return the persisted runtime-context directory."""
        p = self.runtime_dir / "context"
        p.mkdir(parents=True, exist_ok=True)
        return p

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

    Falls back to BRIDLE_WORKSPACE env var so uvicorn reload-forked workers
    can recover the workspace set by the parent CLI process.
    Raises RuntimeError if neither is available.
    """
    global _global_config
    if _global_config is None:
        import os
        env_workspace = os.environ.get("BRIDLE_WORKSPACE")
        if env_workspace:
            _global_config = WorkspaceConfig(env_workspace)
            return _global_config
        raise RuntimeError(
            "Workspace not configured. Call set_workspace() or use --workspace."
        )
    return _global_config
