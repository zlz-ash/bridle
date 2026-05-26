"""Git workspace preflight checks for containerized main-agent sessions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_VALID_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class GitWorkspaceCheck:
    ok: bool
    baseline_revision: str | None
    error_code: str | None = None
    message: str = ""


class GitWorkspacePolicy:
    def evaluate(self, workspace_root: str | Path) -> GitWorkspaceCheck:
        workspace = Path(workspace_root).resolve()
        git_path = workspace / ".git"
        if not git_path.exists():
            return GitWorkspaceCheck(
                ok=False,
                baseline_revision=None,
                error_code="not_git_repository",
                message="Workspace must be a git repository before starting a main-agent container",
            )

        baseline = self._read_head_revision(git_path)
        if baseline is None:
            return GitWorkspaceCheck(
                ok=False,
                baseline_revision=None,
                error_code="empty_baseline",
                message="Git HEAD revision is missing or empty",
            )
        if not _VALID_SHA_RE.match(baseline):
            return GitWorkspaceCheck(
                ok=False,
                baseline_revision=None,
                error_code="invalid_baseline",
                message=f"Git HEAD revision is not a valid 40-char SHA: {baseline!r}",
            )
        return GitWorkspaceCheck(ok=True, baseline_revision=baseline)

    def _read_head_revision(self, git_path: Path) -> str | None:
        head_path = git_path / "HEAD"
        if not head_path.is_file():
            return None
        head = head_path.read_text(encoding="utf-8").strip()
        if not head:
            return None
        if head.startswith("ref: "):
            ref_path = git_path / head.removeprefix("ref: ").strip()
            if ref_path.is_file():
                return ref_path.read_text(encoding="utf-8").strip() or None
            packed = git_path / "packed-refs"
            if packed.is_file():
                ref_name = head.removeprefix("ref: ").strip()
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.startswith("#") or line.startswith("^"):
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) == 2 and parts[1].strip() == ref_name:
                        return parts[0].strip()
            return None
        return head or None
