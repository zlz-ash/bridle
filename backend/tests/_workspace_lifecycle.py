"""Shared workspace fixture lifecycle for backend tests.

Both ``backend/tests/conftest.py`` and ``backend/tests/agent/container/
conftest.py`` import this module so they share one workspace-creation
helper with a single zero-leftover contract:

* per-test unique directory on D drive;
* Windows ACL baseline (inheritable Full Access for Everyone) so remove
  patches can exercise real deletion on hosts that otherwise lack delete
  rights;
* identity registered at creation (path, st_ino, st_dev, st_mode, symlink/
  reparse flag);
* teardown verifies identity, restores the trusted ACL/mode baseline before
  deleting, and refuses to follow a symlink/junction that replaced the
  workspace;
* only directories registered in *this* session are deleted — the 16k+
  historical dirs left by previous runs are never touched;
* cleanup failures are returned as structured diagnostics so the fixture
  can fail the test while preserving the main test result.

This module is import-safe (no pytest import) so it can be loaded from
either conftest without affecting confcutdir semantics.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger("bridle.test")

_IS_WINDOWS = os.name == "nt"
# Inheritable Full Access for Everyone. Matches the original SDDL used by
# backend/tests/conftest.py before this refactor.
_WINDOWS_ACL_SDDL = "D:P(A;OICI;FA;;;WD)"
# POSIX baseline mode: rwx for owner, rx for group/other. Restored before
# recursive delete so a test that tightened mode does not block cleanup.
_POSIX_BASELINE_MODE = 0o755

# Dirs created during *this* session. Only these are eligible for teardown.
# Historical dirs from previous runs (16k+) are deliberately NOT touched.
_CREATED_THIS_SESSION: set[Path] = set()


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.wintypes.LPVOID),
        ("bInheritHandle", ctypes.wintypes.BOOL),
    ]


@dataclass(frozen=True)
class WorkspaceIdentity:
    path: Path
    st_ino: int
    st_dev: int
    st_mode: int
    is_symlink_or_reparse: bool


def _is_reparse_point(path: Path) -> bool:
    if not _IS_WINDOWS:
        return False
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except OSError:
        return False
    if attrs == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
        return False
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def _identity_for(path: Path) -> WorkspaceIdentity:
    """Capture identity without following symlinks (lstat)."""
    st = path.lstat()
    is_link = os.path.islink(path) or _is_reparse_point(path)
    return WorkspaceIdentity(
        path=path.resolve(),
        st_ino=st.st_ino,
        st_dev=st.st_dev,
        st_mode=stat.S_IMODE(st.st_mode),
        is_symlink_or_reparse=is_link,
    )


def _create_with_acl(path: Path) -> None:
    """Create ``path`` with the Windows ACL baseline (or plain mkdir on POSIX)."""
    if not _IS_WINDOWS:
        path.mkdir(parents=True, exist_ok=True)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return

    advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    sd = ctypes.wintypes.LPVOID()
    sd_size = ctypes.wintypes.ULONG()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(ctypes.wintypes.LPVOID),
        ctypes.POINTER(ctypes.wintypes.ULONG),
    ]
    convert.restype = ctypes.wintypes.BOOL
    create_directory = kernel32.CreateDirectoryW
    create_directory.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(_SecurityAttributes),
    ]
    create_directory.restype = ctypes.wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.wintypes.HLOCAL]
    local_free.restype = ctypes.wintypes.HLOCAL
    if not convert(_WINDOWS_ACL_SDDL, 1, ctypes.byref(sd), ctypes.byref(sd_size)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        sa = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), sd, False)
        if not create_directory(str(path), ctypes.byref(sa)):
            err = ctypes.get_last_error()
            if not path.exists():
                raise ctypes.WinError(err)
    finally:
        if sd:
            local_free(sd)


def _restore_acl_baseline(path: Path) -> str | None:
    """Restore the trusted ACL/mode baseline so delete can proceed.

    Returns an error string on failure, or None on success. Idempotent.
    """
    if not _IS_WINDOWS:
        try:
            os.chmod(path, _POSIX_BASELINE_MODE)
        except OSError as exc:
            return f"chmod({path}, 0o{_POSIX_BASELINE_MODE:o}) failed: {type(exc).__name__}: {exc}"
        return None
    # Restore to baseline without /reset. A reset can drop the creation-time
    # FullControl ACE inherited by children, after which this restricted test
    # token may no longer have WRITE_DAC to grant it back. Remove deny ACEs
    # for Everyone, then grant FullControl by SID so this is locale-neutral.
    inherit_flag = "(OI)(CI)" if path.is_dir() else ""
    grant_ace = f"*S-1-1-0:{inherit_flag}F"
    try:
        remove_deny = subprocess.run(
            ["icacls", str(path), "/remove:d", "*S-1-1-0", "/T", "/C", "/Q"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"icacls remove deny failed: {type(exc).__name__}: {exc}"
    if remove_deny.returncode != 0:
        return f"icacls remove deny exit={remove_deny.returncode}: {remove_deny.stderr.strip()[:200]}"
    try:
        grant = subprocess.run(
            ["icacls", str(path), "/grant:r", grant_ace, "/T", "/C", "/Q"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"icacls grant failed: {type(exc).__name__}: {exc}"
    if grant.returncode != 0:
        return f"icacls grant exit={grant.returncode}: {grant.stderr.strip()[:200]}"
    return None


def _verify_identity(path: Path, baseline: WorkspaceIdentity) -> str | None:
    """Verify the path is still the same trusted object we created.

    Refuses to delete if the path was replaced with a symlink/junction or
    if inode/device identity changed (e.g. rebound to a foreign object).
    """
    if not path.exists() and not path.is_symlink():
        return None  # already gone — nothing to verify or delete.
    try:
        current = _identity_for(path)
    except OSError as exc:
        return f"lstat failed: {type(exc).__name__}: {exc}"
    if current.is_symlink_or_reparse:
        return (
            f"refusing to delete {path!r}: path is now a symlink/reparse point "
            "(possible rebinding attack); not following."
        )
    if current.st_ino != baseline.st_ino or current.st_dev != baseline.st_dev:
        return (
            f"refusing to delete {path!r}: inode/device identity changed "
            f"(baseline ino={baseline.st_ino} dev={baseline.st_dev}, "
            f"current ino={current.st_ino} dev={current.st_dev})."
        )
    return None


def _rmtree_with_acl_restore(path: Path) -> str | None:
    """Recursively delete, restoring ACL/mode on access errors."""

    def _on_error(func, target, exc):  # noqa: ANN001
        exc_obj = exc[1] if isinstance(exc, tuple) and len(exc) > 1 else exc
        # Restore baseline on the failing target, then retry once.
        target_path = Path(target)
        if target_path.is_symlink():
            # Don't follow a symlink inside the tree — fail-closed.
            raise
        restore_err = _restore_acl_baseline(target_path) if _IS_WINDOWS else None
        if _IS_WINDOWS and restore_err:
            if isinstance(exc_obj, BaseException):
                raise RuntimeError(f"ACL restore failed at {target}: {restore_err}") from exc_obj
            raise RuntimeError(f"ACL restore failed at {target}: {restore_err}")
        try:
            func(target)
        except OSError as retry_exc:
            raise RuntimeError(f"delete retry failed at {target}: {retry_exc}") from retry_exc

    try:
        shutil.rmtree(path, onerror=_on_error)
    except RuntimeError as exc:
        return str(exc)
    except OSError as exc:
        return f"rmtree failed: {type(exc).__name__}: {exc}"
    return None


def register_session_dir(path: Path) -> None:
    """Track a directory as created this session (eligible for teardown)."""
    _CREATED_THIS_SESSION.add(path.resolve())


def session_created_dirs() -> set[Path]:
    """Return a copy of dirs created this session (for verification fixtures)."""
    return set(_CREATED_THIS_SESSION)


def clear_session_registry() -> None:
    """Drop the session registry (used by self-tests)."""
    _CREATED_THIS_SESSION.clear()


def create_workspace(
    name: str,
    parent: Path,
    *,
    with_git: bool = True,
) -> tuple[Path, WorkspaceIdentity]:
    """Create a unique workspace under ``parent`` and register its identity.

    The directory name is sanitized from ``name`` (the test node id) and
    suffixed with a short uuid to guarantee uniqueness. A minimal git
    fixture is created unless ``with_git`` is False.
    """
    safe = name
    for ch in '<>:"|?*':
        safe = safe.replace(ch, "_")
    safe = (
        safe.replace("[", "_")
        .replace("]", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
    )
    path = parent / f"{safe[:80]}-{uuid4().hex[:8]}"
    _create_with_acl(path)
    if with_git:
        git_dir = path / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    identity = _identity_for(path)
    register_session_dir(path)
    return path, identity


def teardown_workspace(path: Path, baseline: WorkspaceIdentity) -> str | None:
    """Verify identity, restore ACL baseline, delete. Returns cleanup error or None.

    Only paths registered this session are deleted. Identity is verified
    FIRST via lstat (no symlink follow) so a rebinding attack (the workspace
    replaced with a symlink/junction) is detected before the registry
    lookup. The registry check then uses the baseline path (captured at
    creation) rather than re-resolving the current path, so a junction that
    re-points elsewhere cannot bypass ownership.
    """
    resolved = baseline.path
    if not path.exists() and not path.is_symlink():
        # Already gone — nothing to verify or delete.
        _CREATED_THIS_SESSION.discard(resolved)
        return None
    verify_err = _verify_identity(path, baseline)
    if verify_err:
        _CREATED_THIS_SESSION.discard(resolved)
        return verify_err
    if resolved not in _CREATED_THIS_SESSION:
        _CREATED_THIS_SESSION.discard(resolved)
        return (
            f"refusing to delete {path!r}: not registered as created this session "
            "(cannot prove ownership; historical dirs are never auto-deleted)."
        )
    if not path.exists():
        _CREATED_THIS_SESSION.discard(resolved)
        return None
    # Restore ACL baseline at the root first, then rmtree (which also restores
    # on per-entry errors).
    root_restore = _restore_acl_baseline(resolved)
    if root_restore:
        # Surface the restore failure but still try the rmtree — it may
        # succeed for entries that are already deletable.
        logger.warning("ACL baseline restore failed at %s: %s", resolved, root_restore)
    delete_err = _rmtree_with_acl_restore(resolved)
    _CREATED_THIS_SESSION.discard(resolved)
    if delete_err:
        return delete_err
    return None
