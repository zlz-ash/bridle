"""Tests for GitWorkspaceInitializer."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bridle.services.git_workspace_initializer import (
    GitWorkspaceInitError,
    GitWorkspaceInitializer,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def logs() -> list[str]:
    return []


def _make_init(workspace: Path, logs: list[str]) -> GitWorkspaceInitializer:
    return GitWorkspaceInitializer(workspace, log=logs.append)


class TestExistingRepo:
    def test_existing_git_dir_skips_all_commands(
        self, workspace: Path, logs: list[str]
    ) -> None:
        (workspace / ".git").mkdir()
        svc = _make_init(workspace, logs)
        with patch("bridle.services.git_workspace_initializer.subprocess.run") as run:
            result = svc.ensure_repo()
        assert result is False
        run.assert_not_called()
        assert logs == []


class TestFreshInit:
    def test_non_git_dir_triggers_init_sequence(
        self, workspace: Path, logs: list[str]
    ) -> None:
        svc = _make_init(workspace, logs)
        with patch("bridle.services.git_workspace_initializer.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = svc.ensure_repo()
        assert result is True
        # git --version + git init + 2x git config + git commit = 5 calls
        assert run.call_count == 5
        called_cmds = [tuple(call.args[0][:3]) for call in run.call_args_list]
        assert called_cmds[0] == ("git", "--version")
        assert called_cmds[1] == ("git", "init")
        assert called_cmds[2] == ("git", "config", "user.name")[:3]
        assert called_cmds[3] == ("git", "config", "user.email")[:3]
        assert called_cmds[4] == ("git", "commit", "--allow-empty")[:3]
        assert any("自动 git init" in line for line in logs)


class TestGitCliMissing:
    def test_missing_git_cli_raises(self, workspace: Path, logs: list[str]) -> None:
        svc = _make_init(workspace, logs)
        with patch(
            "bridle.services.git_workspace_initializer.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            with pytest.raises(GitWorkspaceInitError) as exc_info:
                svc.ensure_repo()
        assert exc_info.value.code == "git_cli_unavailable"
        assert "git" in str(exc_info.value)


class TestGitCommandFailure:
    def test_git_init_nonzero_raises(self, workspace: Path, logs: list[str]) -> None:
        svc = _make_init(workspace, logs)

        def run_side_effect(args: list[str], **kwargs: object) -> MagicMock:
            if args[:2] == ["git", "--version"]:
                return MagicMock(returncode=0, stdout="git version 2.50", stderr="")
            if args[:2] == ["git", "init"]:
                return MagicMock(returncode=128, stdout="", stderr="fatal: cannot init")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "bridle.services.git_workspace_initializer.subprocess.run",
            side_effect=run_side_effect,
        ):
            with pytest.raises(GitWorkspaceInitError) as exc_info:
                svc.ensure_repo()
        assert exc_info.value.code == "git_command_failed"
        assert "fatal" in str(exc_info.value)
