"""Tests for bridle serve --no-auto-git-init flag and default behavior."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from bridle.cli import app
from bridle.services.git_workspace_initializer import GitWorkspaceInitError


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_serve_side_effects() -> Iterator[None]:
    import bridle.config as cfg

    os.environ.pop("BRIDLE_WORKSPACE", None)
    cfg._global_config = None
    yield
    os.environ.pop("BRIDLE_WORKSPACE", None)
    cfg._global_config = None


@contextmanager
def _serve_context(git_svc: MagicMock | None = None):
    """Patches to short-circuit serve before uvicorn / DB / docker actually run."""
    svc = git_svc if git_svc is not None else MagicMock()
    with (
        patch("bridle.services.git_workspace_initializer.GitWorkspaceInitializer", return_value=svc),
        patch("bridle.services.image_bootstrap.ImageBootstrapService", return_value=MagicMock()),
        patch("uvicorn.run"),
        patch("asyncio.run", side_effect=lambda coro: None),
        patch("bridle.cli._load_env_files", return_value=[]),
        patch("bridle.config.set_workspace"),
        patch("bridle.models", create=True),
        patch("bridle.database._ensure_engine"),
        patch("bridle.database._engine") as eng_patch,
    ):
        conn = MagicMock()
        eng_patch.begin.return_value.__aenter__ = AsyncMock(return_value=conn)
        eng_patch.begin.return_value.__aexit__ = AsyncMock(return_value=False)
        try:
            yield svc
        finally:
            os.environ.pop("BRIDLE_WORKSPACE", None)


class TestServeGitInitCli:
    def test_default_calls_ensure_repo(self, runner: CliRunner, workspace: Path) -> None:
        svc = MagicMock()
        with _serve_context(svc):
            runner.invoke(
                app,
                ["serve", "--workspace", str(workspace)],
                catch_exceptions=False,
            )
        svc.ensure_repo.assert_called_once_with()

    def test_no_auto_git_init_skips_initializer(
        self, runner: CliRunner, workspace: Path
    ) -> None:
        with (
            patch("bridle.services.git_workspace_initializer.GitWorkspaceInitializer") as InitCls,
            patch("bridle.services.image_bootstrap.ImageBootstrapService", return_value=MagicMock()),
            patch("uvicorn.run"),
            patch("asyncio.run", side_effect=lambda coro: None),
            patch("bridle.cli._load_env_files", return_value=[]),
            patch("bridle.config.set_workspace"),
            patch("bridle.models", create=True),
            patch("bridle.database._ensure_engine"),
            patch("bridle.database._engine") as eng_patch,
        ):
            eng_patch.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            eng_patch.begin.return_value.__aexit__ = AsyncMock(return_value=False)
            runner.invoke(
                app,
                ["serve", "--workspace", str(workspace), "--no-auto-git-init"],
                catch_exceptions=False,
            )
        InitCls.assert_not_called()

    def test_init_error_exits_code_2(self, runner: CliRunner, workspace: Path) -> None:
        svc = MagicMock()
        svc.ensure_repo.side_effect = GitWorkspaceInitError(
            "git_cli_unavailable",
            "找不到 git 命令。",
        )
        with (
            patch("bridle.services.git_workspace_initializer.GitWorkspaceInitializer", return_value=svc),
            patch("uvicorn.run") as uvicorn_run,
            patch("bridle.cli._load_env_files", return_value=[]),
            patch("bridle.config.set_workspace"),
        ):
            result = runner.invoke(
                app,
                ["serve", "--workspace", str(workspace)],
            )
        assert result.exit_code == 2
        assert "git_cli_unavailable" in (result.stderr + result.output)
        uvicorn_run.assert_not_called()
