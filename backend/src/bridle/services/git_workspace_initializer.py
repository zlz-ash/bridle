"""Auto-init workspace as git repo when ``bridle serve`` starts.

Bridle's container session pipeline requires the workspace to be a git repo
(``GitWorkspacePolicy`` uses the current revision as a rollback anchor for
checkpointing).  Forcing users to remember ``git init`` is friction; this
service detects the missing ``.git`` and runs the minimal sequence to make
the workspace usable.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

_GIT_TIMEOUT_SEC = 30
_BOOTSTRAP_USER_NAME = "bridle-bootstrap"
_BOOTSTRAP_USER_EMAIL = "bridle@localhost"
_BOOTSTRAP_COMMIT_MESSAGE = "bootstrap: bridle workspace init"


class GitWorkspaceInitError(RuntimeError):
    """Auto-init failed; ``code`` is machine-readable, message is user-facing."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class GitWorkspaceInitializer:
    """Make ``workspace`` a usable git repo for Bridle's container session pipeline."""

    def __init__(
        self,
        workspace: Path,
        *,
        log: Callable[[str], None] = print,
    ) -> None:
        self.workspace = workspace.resolve()
        self.log = log

    def ensure_repo(self) -> bool:
        """Return True if init was performed; False if already a git repo."""
        if self._is_git_repo():
            return False
        self._check_git_cli()
        self.log(f"workspace {self.workspace} 不是 git 仓库，正在自动 git init …")
        self._run(["git", "init"])
        self._ensure_identity()
        self._run(["git", "commit", "--allow-empty", "-m", _BOOTSTRAP_COMMIT_MESSAGE])
        self.log("已自动 git init 并创建 bootstrap commit。")
        return True

    def _is_git_repo(self) -> bool:
        # ``.git`` is a directory for normal repos, a file for worktrees — both OK.
        return (self.workspace / ".git").exists()

    def _check_git_cli(self) -> None:
        try:
            subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT_SEC,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise GitWorkspaceInitError(
                "git_cli_unavailable",
                "找不到 git 命令。请装好 git 并加入 PATH 后重试。",
            ) from exc

    def _ensure_identity(self) -> None:
        # repo-local config: does not touch the user's global git identity.
        self._run(["git", "config", "user.name", _BOOTSTRAP_USER_NAME])
        self._run(["git", "config", "user.email", _BOOTSTRAP_USER_EMAIL])

    def _run(self, args: list[str]) -> None:
        result = subprocess.run(
            args,
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT_SEC,
            check=False,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()
            raise GitWorkspaceInitError(
                "git_command_failed",
                f"{' '.join(args)} 失败：{tail[-300:]}",
            )
