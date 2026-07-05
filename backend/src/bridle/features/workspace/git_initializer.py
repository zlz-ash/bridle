"""Auto-init a workspace as a git repo when ``bridle serve`` starts.

Project-map and workspace safety checks use git state as a stable local
boundary. This service creates the smallest usable repo when a workspace has
not been initialized yet.
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
    """Make ``workspace`` a usable git repo for Bridle project operations."""

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
        self.log(f"workspace {self.workspace} is not a git repo; running git init")
        self._run(["git", "init"])
        self._ensure_identity()
        self._run(["git", "commit", "--allow-empty", "-m", _BOOTSTRAP_COMMIT_MESSAGE])
        self.log("git init completed with bootstrap commit")
        return True

    def _is_git_repo(self) -> bool:
        # ``.git`` is a directory for normal repos and a file for worktrees; both are valid.
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
                "git command not found. Install git and add it to PATH.",
            ) from exc

    def _ensure_identity(self) -> None:
        # Repo-local config: does not touch the user's global git identity.
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
                f"{' '.join(args)} failed: {tail[-300:]}",
            )