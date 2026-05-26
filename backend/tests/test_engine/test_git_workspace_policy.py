"""Tests for git workspace preflight checks."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bridle.engine.git_workspace_policy import GitWorkspacePolicy


def _clear_git(workspace: Path) -> None:
    shutil.rmtree(workspace / ".git", ignore_errors=True)


class TestGitWorkspacePolicy:
    def test_accepts_workspace_with_branch_head(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        ref_dir = git_dir / "refs" / "heads"
        ref_dir.mkdir(parents=True, exist_ok=True)
        ref_dir.joinpath("main").write_text("a" * 40 + "\n", encoding="utf-8")

        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is True
        assert result.error_code is None
        assert result.baseline_revision == "a" * 40

    def test_accepts_workspace_with_detached_head(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(exist_ok=True)
        git_dir.joinpath("HEAD").write_text("b" * 40 + "\n", encoding="utf-8")

        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is True
        assert result.baseline_revision == "b" * 40

    def test_rejects_workspace_without_git(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is False
        assert result.error_code == "not_git_repository"
        assert result.baseline_revision is None

    def test_accepts_packed_refs(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        refs_dir = git_dir / "refs" / "heads"
        refs_dir.mkdir(parents=True, exist_ok=True)
        (git_dir / "packed-refs").write_text(
            "# pack-refs\n" + "c" * 40 + " refs/heads/main\n",
            encoding="utf-8",
        )

        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is True
        assert result.baseline_revision == "c" * 40

    def test_rejects_missing_head_file(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(exist_ok=True)

        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is False
        assert result.error_code == "empty_baseline"
        assert result.baseline_revision is None

    def test_rejects_empty_head_ref(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text("\n", encoding="utf-8")

        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is False
        assert result.error_code == "empty_baseline"
        assert result.baseline_revision is None

    def test_rejects_ref_pointing_to_missing_file(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        refs_dir = git_dir / "refs" / "heads"
        refs_dir.mkdir(parents=True, exist_ok=True)

        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is False
        assert result.error_code == "empty_baseline"
        assert result.baseline_revision is None

    def test_rejects_ref_with_empty_content(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        ref_dir = git_dir / "refs" / "heads"
        ref_dir.mkdir(parents=True, exist_ok=True)
        ref_dir.joinpath("main").write_text("\n", encoding="utf-8")

        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is False
        assert result.error_code == "empty_baseline"
        assert result.baseline_revision is None

    def test_rejects_detached_head_with_invalid_sha(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text("not-a-sha\n", encoding="utf-8")

        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is False
        assert result.error_code == "invalid_baseline"
        assert result.baseline_revision is None

    def test_rejects_detached_head_with_short_sha(self, test_workspace: Path) -> None:
        _clear_git(test_workspace)
        git_dir = test_workspace / ".git"
        git_dir.mkdir(exist_ok=True)
        (git_dir / "HEAD").write_text("abc123\n", encoding="utf-8")

        result = GitWorkspacePolicy().evaluate(test_workspace)

        assert result.ok is False
        assert result.error_code == "invalid_baseline"
        assert result.baseline_revision is None
