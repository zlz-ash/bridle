"""Tests for workspace path derivation and safety per PLAN.md."""
from __future__ import annotations

import pytest

from bridle.config import WorkspaceConfig, get_config, set_workspace


class TestWorkspacePathDerivation:
    """All paths must be derived from the explicitly specified workspace."""

    def test_runtime_dir_under_workspace(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        assert cfg.runtime_dir == test_workspace / ".bridle" / "runtime"

    def test_db_path_under_bridle_runtime(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        assert cfg.db_path == test_workspace / ".bridle" / "runtime" / "db.sqlite3"

    def test_database_url_contains_workspace_path(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        assert test_workspace.as_posix() in cfg.database_url
        assert "sqlite+aiosqlite:///" in cfg.database_url

    def test_runs_dir_under_bridle_runtime(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        assert cfg.runs_dir == test_workspace / ".bridle" / "runtime" / "runs"

    def test_logs_dir_under_bridle_runtime(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        assert cfg.logs_dir == test_workspace / ".bridle" / "runtime" / "logs"

    def test_reports_dir_under_bridle_runtime(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        assert cfg.reports_dir == test_workspace / ".bridle" / "runtime" / "reports"

    def test_context_dir_under_bridle_runtime(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        assert cfg.context_dir == test_workspace / ".bridle" / "runtime" / "context"

    def test_runtime_dirs_are_created_on_access(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        assert cfg.runs_dir.exists()
        assert cfg.logs_dir.exists()
        assert cfg.reports_dir.exists()
        assert cfg.context_dir.exists()


class TestWorkspaceRequired:
    """Workspace must be explicitly specified; no fallback to cwd or temp."""

    def test_nonexistent_workspace_raises(self) -> None:
        with pytest.raises(ValueError, match="does not exist"):
            WorkspaceConfig("/nonexistent/path/that/does/not/exist")

    def test_get_config_without_set_raises(self) -> None:
        import bridle.config as cfg_mod

        old = cfg_mod._global_config
        cfg_mod._global_config = None
        try:
            with pytest.raises(RuntimeError, match="Workspace not configured"):
                get_config()
        finally:
            cfg_mod._global_config = old

    def test_set_workspace_creates_singleton(self, test_workspace) -> None:
        set_workspace(test_workspace)
        cfg = get_config()
        assert cfg.workspace == test_workspace.resolve()


class TestPathSafety:
    """Paths must never escape the workspace or fall to system directories."""

    def test_all_paths_under_workspace(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        all_paths = [
            cfg.runtime_dir,
            cfg.db_path,
            cfg.runs_dir,
            cfg.logs_dir,
            cfg.reports_dir,
            cfg.context_dir,
        ]
        workspace_resolved = test_workspace.resolve()
        for path in all_paths:
            assert str(path).startswith(str(workspace_resolved)), (
                f"{path} is not under workspace {workspace_resolved}"
            )

    def test_workspace_must_be_explicit(self, test_workspace) -> None:
        cfg = WorkspaceConfig(test_workspace)
        assert cfg.workspace == test_workspace.resolve()
        with pytest.raises(ValueError):
            WorkspaceConfig("/nonexistent")
