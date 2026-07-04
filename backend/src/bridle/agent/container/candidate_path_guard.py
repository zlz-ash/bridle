"""Validate candidate/module paths before any filesystem mutation."""
from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

_SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_CANDIDATE_REL_PATTERN = re.compile(r"^candidates/[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


class CandidatePathError(ValueError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        super().__init__(detail or error_code)


def validate_safe_id(value: str, *, field: str) -> str:
    text = str(value).strip()
    if not text:
        raise CandidatePathError(f"{field}_required")
    if not _SAFE_ID_PATTERN.match(text):
        raise CandidatePathError(f"{field}_invalid")
    if ".." in text or "/" in text or "\\" in text:
        raise CandidatePathError(f"{field}_invalid")
    return text


def _looks_absolute_windows(raw: str) -> bool:
    if len(raw) >= 2 and raw[1] == ":":
        return True
    return raw.startswith("\\\\") or raw.startswith("//")


def validate_candidate_rel(candidate_rel: str) -> str:
    raw = str(candidate_rel).strip()
    if not raw:
        raise CandidatePathError("candidate_rel_required")
    if raw.startswith("/") or raw.startswith("\\") or _looks_absolute_windows(raw):
        raise CandidatePathError("candidate_rel_invalid")
    if "\\" in raw:
        raise CandidatePathError("candidate_rel_invalid")
    text = raw.replace("\\", "/")
    if text.startswith("/"):
        raise CandidatePathError("candidate_rel_invalid")
    if not text or text.endswith("/"):
        raise CandidatePathError("candidate_rel_invalid")
    segments = text.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        raise CandidatePathError("candidate_rel_invalid")
    if not _CANDIDATE_REL_PATTERN.match(text):
        raise CandidatePathError("candidate_rel_invalid")
    return text


def resolve_candidate_rel(module_root: Path, candidate_rel: str) -> Path:
    """Resolve candidate_rel under module_root without following symlinks."""
    normalized = validate_candidate_rel(candidate_rel)
    module_resolved = module_root.resolve()
    candidate_root = module_resolved.joinpath(*normalized.split("/"))
    try:
        candidate_root.resolve(strict=False).relative_to(module_resolved)
    except ValueError as exc:
        raise CandidatePathError("candidate_rel_outside_module_root") from exc
    return candidate_root


def runtime_root(project_root: Path) -> Path:
    root = project_root.resolve()
    if os.name == "nt":
        drive = root.drive.upper()
        if drive and drive != "D:":
            raise CandidatePathError("project_root_must_be_on_d_drive")
    return root / ".bridle" / "runtime"


def module_execution_root(project_root: Path, module_id: str) -> Path:
    safe_module = validate_safe_id(module_id, field="module_id")
    base = runtime_root(project_root) / "modules" / safe_module
    _assert_under_runtime(base, project_root)
    return base


def candidate_root(project_root: Path, module_id: str, candidate_id: str) -> Path:
    safe_candidate = validate_safe_id(candidate_id, field="candidate_id")
    root = module_execution_root(project_root, module_id) / "candidates" / safe_candidate
    _assert_under_runtime(root, project_root)
    return root


def _assert_under_runtime(path: Path, project_root: Path) -> None:
    resolved = path.resolve()
    project = project_root.resolve()
    runtime = runtime_root(project)
    try:
        resolved.relative_to(project)
        resolved.relative_to(runtime)
    except ValueError as exc:
        raise CandidatePathError("path_outside_project_runtime") from exc


def _refuse_link_or_reparse(path: Path) -> None:
    if path.is_symlink() or _is_reparse_point(path):
        raise CandidatePathError("refuse_symlink_or_reparse_delete")
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise CandidatePathError("refuse_symlink_or_reparse_delete")
    except FileNotFoundError:
        return


def safe_rmtree(
    path: Path,
    *,
    project_root: Path,
    expected_root: Path | None = None,
) -> None:
    """Remove a directory tree; refuse symlinks/reparse points and out-of-scope targets."""
    if not path.exists() and not path.is_symlink():
        return
    _refuse_link_or_reparse(path)
    target = path.resolve()
    _assert_under_runtime(target, project_root)
    if expected_root is not None:
        expected = expected_root.resolve()
        try:
            target.relative_to(expected)
        except ValueError as exc:
            raise CandidatePathError("refuse_unexpected_delete_target") from exc
    if not target.is_dir():
        raise CandidatePathError("refuse_non_directory_delete")
    for root, dirnames, filenames in os.walk(target, topdown=False, followlinks=False):
        root_path = Path(root)
        _refuse_link_or_reparse(root_path)
        for name in filenames:
            child = root_path / name
            _refuse_link_or_reparse(child)
            child.unlink(missing_ok=True)
        for name in dirnames:
            child = root_path / name
            _refuse_link_or_reparse(child)
        if root_path != target:
            _refuse_link_or_reparse(root_path)
            root_path.rmdir()
    _refuse_link_or_reparse(target)
    shutil.rmtree(target)


def _is_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return path.is_symlink()
    try:
        attrs = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return path.is_symlink()
