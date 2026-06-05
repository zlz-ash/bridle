"""Workspace file read service with path safety gates."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from bridle.api.errors import BridleError, ForbiddenError, NotFoundError, PayloadTooLargeError, UnsupportedMediaError
from bridle.logging.jsonl import log_event
from bridle.schemas.workspace import WorkspaceFileReadResponse

MAX_FILE_BYTES = 1 * 1024 * 1024
_BINARY_PROBE_BYTES = 8192

_DENIED_PREFIXES = (
    ".git/",
    ".venv/",
    ".aicoding/",
    "node_modules/",
    "__pycache__/",
    ".pytest-tmp/",
    ".test-workspaces/",
    "e2e-runs/",
    "e2e-generated/",
)

_WINDOWS_ABS = re.compile(r"^[A-Za-z]:")

DENIED_WORKSPACE_PREFIXES = _DENIED_PREFIXES


class WorkspaceFileService:
    @staticmethod
    def read_text(workspace: Path, rel_path: str) -> WorkspaceFileReadResponse:
        if not rel_path or rel_path.startswith("/") or _WINDOWS_ABS.match(rel_path):
            log_event(
                "workspace_file_read",
                "rejected",
                detail={"path": rel_path, "reason": "absolute_path"},
            )
            raise ForbiddenError(
                resource="workspace_file",
                message="Absolute paths are not allowed",
                error_code="path_outside_workspace",
            )

        normalized = rel_path.replace("\\", "/")
        parts = [part for part in normalized.split("/") if part and part != "."]
        if ".." in parts:
            log_event(
                "workspace_file_read",
                "rejected",
                detail={"path": rel_path, "reason": "path_traversal"},
            )
            raise ForbiddenError(
                resource="workspace_file",
                message="Path escapes workspace",
                error_code="path_outside_workspace",
            )

        posix_input = "/".join(parts)
        for prefix in _DENIED_PREFIXES:
            if posix_input == prefix.rstrip("/") or posix_input.startswith(prefix):
                log_event(
                    "workspace_file_read",
                    "rejected",
                    detail={"path": posix_input, "reason": "denied_prefix"},
                )
                raise ForbiddenError(
                    resource="workspace_file",
                    message="Path is denied",
                    error_code="path_denied",
                    details={"path": posix_input},
                )

        workspace_resolved = workspace.resolve()
        target = (workspace_resolved / posix_input).resolve()
        if not target.is_relative_to(workspace_resolved):
            log_event(
                "workspace_file_read",
                "rejected",
                detail={"path": posix_input, "reason": "outside_workspace"},
            )
            raise ForbiddenError(
                resource="workspace_file",
                message="Path escapes workspace",
                error_code="path_outside_workspace",
            )

        posix = target.relative_to(workspace_resolved).as_posix()
        if not target.exists():
            raise BridleError(
                code="file_not_found",
                message="File not found",
                status_code=404,
                resource="workspace_file",
                details={"path": posix},
            )
        if target.is_dir():
            raise BridleError(
                code="bad_request",
                message="Path is a directory",
                status_code=400,
                resource="workspace_file",
                details={"reason": "is_directory", "path": posix},
            )

        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            log_event(
                "workspace_file_read",
                "rejected",
                detail={"path": posix, "size": size, "reason": "too_large"},
            )
            raise PayloadTooLargeError(
                resource="workspace_file",
                message="File exceeds 1MB limit",
                details={"size": size, "path": posix},
            )

        raw = target.read_bytes()
        probe = raw[:_BINARY_PROBE_BYTES]
        if b"\x00" in probe:
            log_event(
                "workspace_file_read",
                "rejected",
                detail={"path": posix, "reason": "binary"},
            )
            raise UnsupportedMediaError(
                resource="workspace_file",
                message="Binary file cannot be previewed",
                details={"reason": "binary", "path": posix},
            )

        encoding = "utf-8"
        if raw.startswith(b"\xef\xbb\xbf"):
            content = raw[3:].decode("utf-8")
        else:
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw.decode("utf-8", errors="replace")
                encoding = "utf-8-fallback-replace"

        mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=UTC).isoformat()
        log_event(
            "workspace_file_read",
            "completed",
            detail={"path": posix, "size": size},
        )
        return WorkspaceFileReadResponse(
            path=posix,
            size=size,
            mtime=mtime,
            encoding=encoding,
            content=content,
            truncated=False,
        )
