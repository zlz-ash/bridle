"""Host candidate ↔ container active slot staging with split RO/RW mounts."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridle.agent.container.candidate_path_guard import CandidatePathError, safe_rmtree
from bridle.agent.container.json_strict import JsonIntError, require_json_int
from bridle.agent.container.runner import ContainerMount

logger = logging.getLogger("bridle")

_ACTIVE_DIR = "_active"
_LEASE_FILE = ".lease.json"
_SLOT_SUBDIRS = ("project", "baseline", "mocks", "output", "diagnostics")
_COLLECT_SUBDIRS = ("output", "diagnostics", "project")
_CONTAINER_TARGETS = {
    "project": "/workspace/project",
    "output": "/workspace/output",
    "diagnostics": "/workspace/diagnostics",
    "baseline": "/workspace/baseline",
    "mocks": "/workspace/mocks",
}
_READONLY_MOUNTS = frozenset({"baseline", "mocks"})
_RW_MOUNT_ROOTS = frozenset({"project", "output", "diagnostics"})
_RW_MOUNT_BASELINE_FILE = ".rw_mount_baseline.json"
_RW_MOUNT_BASELINE_TMP_SUFFIX = ".tmp"
_RW_MOUNT_TRUST_MARKER_FILE = ".rw_mount_trust.ready"
_RW_MOUNT_BASELINE_VERSION = 2
_RW_MOUNT_TRUST_MARKER_VERSION = 2
_TRUST_MARKER_SCHEMA = "bridle.rw_mount_trust_marker/v1"
_BASELINE_STATE_READY = "ready"


def rw_mount_baseline_path(module_root: Path) -> Path:
    """Host-only trusted permission baseline; not bind-mounted into containers."""
    return module_root / _RW_MOUNT_BASELINE_FILE


def rw_mount_baseline_tmp_path(module_root: Path) -> Path:
    return module_root / f"{_RW_MOUNT_BASELINE_FILE}{_RW_MOUNT_BASELINE_TMP_SUFFIX}"


def rw_mount_trust_marker_path(module_root: Path) -> Path:
    """Persistent READY marker; survives baseline deletion for fail-closed recovery."""
    return module_root / _RW_MOUNT_TRUST_MARKER_FILE


@dataclass(frozen=True)
class ActiveSlotLayout:
    slot_root: Path
    project: Path
    baseline: Path
    mocks: Path
    output: Path
    diagnostics: Path


@dataclass(frozen=True)
class ActiveSlotLease:
    candidate_rel: str
    run_id: str
    token: str


def active_slot_dir(module_root: Path) -> Path:
    return module_root / _ACTIVE_DIR


def slot_layout(slot_root: Path) -> ActiveSlotLayout:
    return ActiveSlotLayout(
        slot_root=slot_root,
        project=slot_root / "project",
        baseline=slot_root / "baseline",
        mocks=slot_root / "mocks",
        output=slot_root / "output",
        diagnostics=slot_root / "diagnostics",
    )


def build_slot_mounts(layout: ActiveSlotLayout) -> list[ContainerMount]:
    mounts: list[ContainerMount] = []
    for name in _SLOT_SUBDIRS:
        source = getattr(layout, name)
        mounts.append(
            ContainerMount(
                source=source,
                target=_CONTAINER_TARGETS[name],
                readonly=name in _READONLY_MOUNTS,
            )
        )
    return mounts


def slot_allowed_mount_roots(layout: ActiveSlotLayout) -> list[str]:
    return [str(getattr(layout, name).resolve()) for name in _SLOT_SUBDIRS]


def _lease_token(*, candidate_rel: str, run_id: str) -> str:
    raw = f"{candidate_rel}:{run_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def tree_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not root.is_dir():
        return hashes
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def write_lease(
    layout: ActiveSlotLayout,
    *,
    candidate_rel: str,
    run_id: str,
    module_root: Path | None = None,
) -> ActiveSlotLease:
    lease = ActiveSlotLease(
        candidate_rel=candidate_rel,
        run_id=run_id,
        token=_lease_token(candidate_rel=candidate_rel, run_id=run_id),
    )
    payload = {
        "candidate_rel": lease.candidate_rel,
        "run_id": lease.run_id,
        "token": lease.token,
    }

    def _write() -> None:
        layout.diagnostics.mkdir(parents=True, exist_ok=True)
        path = layout.diagnostics / _LEASE_FILE
        tmp = layout.diagnostics / f"{_LEASE_FILE}.tmp"
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    _run_mount_root_access(
        layout.diagnostics,
        root_name="diagnostics",
        module_root=module_root,
        operation="write_lease",
        action=_write,
    )
    return lease


def read_lease(layout: ActiveSlotLayout) -> ActiveSlotLease | None:
    path = layout.diagnostics / _LEASE_FILE
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ActiveSlotLease(
        candidate_rel=str(payload.get("candidate_rel") or ""),
        run_id=str(payload.get("run_id") or ""),
        token=str(payload.get("token") or ""),
    )


def verify_lease_token(layout: ActiveSlotLayout, *, token: str) -> None:
    lease = read_lease(layout)
    if lease is None or not token or lease.token != token:
        raise CandidatePathError("active_slot_lease_mismatch")


def directory_identity(path: Path) -> tuple[int, int]:
    """Stable (st_dev, st_ino) for bind mount source directories."""
    stat_result = os.stat(path, follow_symlinks=False)
    return (int(stat_result.st_dev), int(stat_result.st_ino))


def slot_mount_identities(layout: ActiveSlotLayout) -> dict[str, tuple[int, int]]:
    return {name: directory_identity(getattr(layout, name)) for name in _SLOT_SUBDIRS}


def _entry_metadata(parent: Path, name: str) -> os.stat_result | None:
    try:
        for entry in os.scandir(parent):
            if entry.name == name:
                return entry.stat(follow_symlinks=False)
    except OSError:
        return None
    return None


def _ensure_slot_tree_component(module_root: Path, parts: tuple[str, ...]) -> bool:
    _validate_slot_component_chain(module_root, parts)
    parent = module_root
    for part in parts[:-1]:
        parent = parent / part
    name = parts[-1]
    if _entry_metadata(parent, name) is not None:
        _validate_slot_component_chain(module_root, parts)
        return False
    try:
        (parent / name).mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        metadata = _entry_metadata(parent, name)
        if metadata is None:
            _reject_mount_root_link(root_name=name, root_path=parent / name, module_root=module_root)
        if _is_link_or_reparse_stat(metadata):
            _reject_mount_root_link(root_name=name, root_path=parent / name, module_root=module_root)
        return False
    _validate_slot_component_chain(module_root, parts)
    return True


def _classify_rw_mount_trust_state(module_root: Path) -> str:
    marker_path = rw_mount_trust_marker_path(module_root)
    baseline_path = rw_mount_baseline_path(module_root)
    tmp_path = rw_mount_baseline_tmp_path(module_root)
    if tmp_path.is_file() and not baseline_path.is_file():
        return "baseline_incomplete"
    marker_present = marker_path.is_file()
    baseline_present = baseline_path.is_file()
    if marker_present and _is_trust_initialized(module_root):
        if baseline_present and _load_ready_rw_mount_baseline(module_root) is not None:
            return "ready"
        return "trust_invalid"
    if marker_present or baseline_present:
        return "trust_invalid"
    return "absent"


def _is_reparse_stat(st: os.stat_result) -> bool:
    if os.name != "nt":
        return False
    try:
        attrs = st.st_file_attributes  # type: ignore[attr-defined]
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except AttributeError:
        return False


def _is_link_or_reparse_stat(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or _is_reparse_stat(st)


def _reject_mount_root_link(*, root_name: str, root_path: Path, module_root: Path | None) -> None:
    logger.info(
        "active_slot_root_link_rejected",
        extra={
            "action": "active_slot_root_link_rejected",
            "status": "rejected",
            "detail": {
                "root_name": root_name,
                "root_path": str(root_path),
                "module_root": str(module_root) if module_root is not None else "",
                "error_code": "active_slot_root_link",
            },
        },
    )
    raise CandidatePathError("active_slot_root_link", detail=f"{root_name}:{root_path}")


def _mount_root_metadata(path: Path, *, root_name: str, module_root: Path | None) -> os.stat_result | None:
    if module_root is None:
        try:
            return os.lstat(path)
        except OSError:
            return None
    if root_name == "_active":
        return _entry_metadata(module_root, _ACTIVE_DIR)
    return _entry_metadata(module_root / _ACTIVE_DIR, root_name)


def _json_int_or_none(value: object, *, field: str) -> int | None:
    try:
        return require_json_int(value, field=field)
    except JsonIntError:
        return None


def _is_trust_initialized(module_root: Path) -> bool:
    path = rw_mount_trust_marker_path(module_root)
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    version = _json_int_or_none(payload.get("version"), field="version")
    return (
        payload.get("schema") == _TRUST_MARKER_SCHEMA
        and version == _RW_MOUNT_TRUST_MARKER_VERSION
        and payload.get("state") == _BASELINE_STATE_READY
    )


def _write_trust_marker(module_root: Path) -> None:
    path = rw_mount_trust_marker_path(module_root)
    payload = {
        "schema": _TRUST_MARKER_SCHEMA,
        "version": _RW_MOUNT_TRUST_MARKER_VERSION,
        "state": _BASELINE_STATE_READY,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _validate_baseline_root_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    dev = _json_int_or_none(entry.get("dev"), field="dev")
    ino = _json_int_or_none(entry.get("ino"), field="ino")
    mode = _json_int_or_none(entry.get("mode"), field="mode")
    if dev is None or ino is None or mode is None:
        return False
    if dev < 0 or ino < 0:
        return False
    return 0 <= mode <= 0o7777


def _roots_payload_complete(roots: Any) -> bool:
    if not isinstance(roots, dict):
        return False
    for name in _RW_MOUNT_ROOTS:
        entry = roots.get(name)
        if not _validate_baseline_root_entry(entry):
            return False
    return True


def _load_rw_mount_baseline_payload(module_root: Path) -> dict[str, Any] | None:
    path = rw_mount_baseline_path(module_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _baseline_recovery_reason(module_root: Path, *, root_name: str) -> str:
    tmp_path = rw_mount_baseline_tmp_path(module_root)
    path = rw_mount_baseline_path(module_root)
    if tmp_path.is_file() and not path.is_file():
        return "baseline_incomplete"
    payload = _load_rw_mount_baseline_payload(module_root)
    if payload is None:
        if path.is_file():
            return "baseline_incomplete"
        if rw_mount_trust_marker_path(module_root).is_file() and not _is_trust_initialized(module_root):
            return "baseline_marker_invalid"
        if _is_trust_initialized(module_root):
            return "baseline_missing"
        return "baseline_incomplete"
    if payload.get("state") != _BASELINE_STATE_READY:
        return "baseline_incomplete"
    version = _json_int_or_none(payload.get("version"), field="version")
    if version is None or version != _RW_MOUNT_BASELINE_VERSION:
        return "baseline_version_mismatch"
    roots = payload.get("roots")
    if not _roots_payload_complete(roots):
        return "baseline_incomplete"
    entry = (roots or {}).get(root_name)
    if not isinstance(entry, dict):
        return "baseline_incomplete"
    return "baseline_missing"


def _load_ready_rw_mount_baseline(module_root: Path) -> dict[str, Any] | None:
    tmp_path = rw_mount_baseline_tmp_path(module_root)
    path = rw_mount_baseline_path(module_root)
    if tmp_path.is_file() and not path.is_file():
        return None
    payload = _load_rw_mount_baseline_payload(module_root)
    if payload is None:
        return None
    if payload.get("state") != _BASELINE_STATE_READY:
        return None
    version = _json_int_or_none(payload.get("version"), field="version")
    if version is None or version != _RW_MOUNT_BASELINE_VERSION:
        return None
    roots = payload.get("roots")
    if not _roots_payload_complete(roots):
        return None
    return payload


def _collect_rw_mount_roots(module_root: Path) -> dict[str, dict[str, int]]:
    active = module_root / _ACTIVE_DIR
    roots: dict[str, dict[str, int]] = {}
    for name in _RW_MOUNT_ROOTS:
        metadata = _entry_metadata(active, name)
        if metadata is None or _is_link_or_reparse_stat(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise CandidatePathError("active_slot_root_invalid", detail=f"{name}:{active / name}")
        roots[name] = {
            "dev": int(metadata.st_dev),
            "ino": int(metadata.st_ino),
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    return roots


def _commit_rw_mount_baseline(module_root: Path, roots: dict[str, dict[str, int]]) -> None:
    path = rw_mount_baseline_path(module_root)
    payload = {
        "version": _RW_MOUNT_BASELINE_VERSION,
        "state": _BASELINE_STATE_READY,
        "roots": roots,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = rw_mount_baseline_tmp_path(module_root)
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _initialize_rw_mount_trust(module_root: Path, *, slot_tree_newly_created: bool) -> None:
    """Record trusted POSIX modes once when slot roots are first created."""
    state = _classify_rw_mount_trust_state(module_root)
    if state == "ready":
        return
    if state != "absent":
        logger.info(
            "active_slot_trust_state_invalid",
            extra={
                "action": "active_slot_trust_state_invalid",
                "status": "rejected",
                "detail": {"module_root": str(module_root), "state": state},
            },
        )
        return
    if not slot_tree_newly_created:
        logger.info(
            "active_slot_trust_init_skipped",
            extra={
                "action": "active_slot_trust_init_skipped",
                "status": "skipped",
                "detail": {"module_root": str(module_root), "reason": "slot_not_newly_created"},
            },
        )
        return
    roots = _collect_rw_mount_roots(module_root)
    _commit_rw_mount_baseline(module_root, roots)
    _write_trust_marker(module_root)
    logger.info(
        "active_slot_root_baseline_recorded",
        extra={
            "action": "active_slot_root_baseline_recorded",
            "status": "completed",
            "detail": {
                "module_root": str(module_root),
                "roots": sorted(roots),
                "version": _RW_MOUNT_BASELINE_VERSION,
            },
        },
    )


def _load_rw_mount_baseline(module_root: Path) -> dict[str, Any] | None:
    return _load_ready_rw_mount_baseline(module_root)


def _reject_mount_root_permission(
    *,
    root_name: str,
    root_path: Path,
    module_root: Path | None,
    operation: str,
    recovery: str,
    error: str = "",
) -> None:
    logger.info(
        "active_slot_root_permission_rejected",
        extra={
            "action": "active_slot_root_permission_rejected",
            "status": "rejected",
            "detail": {
                "root_name": root_name,
                "root_path": str(root_path),
                "module_root": str(module_root) if module_root is not None else "",
                "operation": operation,
                "recovery": recovery,
                "error": error,
                "error_code": "active_slot_root_permission",
            },
        },
    )
    raise CandidatePathError(
        "active_slot_root_permission",
        detail=f"{root_name}:{root_path}:{operation}:{recovery}",
    )


def _restore_rw_mount_root_access(
    path: Path,
    *,
    root_name: str,
    module_root: Path | None,
    operation: str,
) -> None:
    if root_name not in _RW_MOUNT_ROOTS:
        return
    if module_root is None:
        return
    if not _is_trust_initialized(module_root):
        recovery = "baseline_incomplete"
        if rw_mount_baseline_path(module_root).is_file() or rw_mount_trust_marker_path(module_root).is_file():
            recovery = "baseline_marker_invalid"
        _reject_mount_root_permission(
            root_name=root_name,
            root_path=path,
            module_root=module_root,
            operation=operation,
            recovery=recovery,
        )
    metadata = _mount_root_metadata(path, root_name=root_name, module_root=module_root)
    if metadata is None or _is_link_or_reparse_stat(metadata) or not stat.S_ISDIR(metadata.st_mode):
        return
    payload = _load_ready_rw_mount_baseline(module_root)
    if payload is None:
        _reject_mount_root_permission(
            root_name=root_name,
            root_path=path,
            module_root=module_root,
            operation=operation,
            recovery=_baseline_recovery_reason(module_root, root_name=root_name),
        )
    roots = payload.get("roots") or {}
    entry = roots.get(root_name)
    if not isinstance(entry, dict):
        _reject_mount_root_permission(
            root_name=root_name,
            root_path=path,
            module_root=module_root,
            operation=operation,
            recovery="baseline_incomplete",
        )
    entry_dev = _json_int_or_none(entry.get("dev"), field="dev")
    entry_ino = _json_int_or_none(entry.get("ino"), field="ino")
    entry_mode = _json_int_or_none(entry.get("mode"), field="mode")
    if entry_dev is None or entry_ino is None or entry_mode is None:
        _reject_mount_root_permission(
            root_name=root_name,
            root_path=path,
            module_root=module_root,
            operation=operation,
            recovery="baseline_incomplete",
        )
    identity = (int(metadata.st_dev), int(metadata.st_ino))
    expected_identity = (entry_dev, entry_ino)
    if identity != expected_identity:
        _reject_mount_root_permission(
            root_name=root_name,
            root_path=path,
            module_root=module_root,
            operation=operation,
            recovery="baseline_inode_mismatch",
        )
    target_mode = stat.S_IMODE(entry_mode)
    current_mode = stat.S_IMODE(metadata.st_mode)
    if current_mode == target_mode:
        return
    try:
        os.chmod(path, target_mode)
    except OSError as exc:
        _reject_mount_root_permission(
            root_name=root_name,
            root_path=path,
            module_root=module_root,
            operation=operation,
            recovery="chmod_failed",
            error=str(exc),
        )
    logger.info(
        "active_slot_root_baseline_restored",
        extra={
            "action": "active_slot_root_baseline_restored",
            "status": "completed",
            "detail": {
                "root_name": root_name,
                "root_path": str(path),
                "module_root": str(module_root),
                "operation": operation,
                "target_mode": oct(target_mode),
                "previous_mode": oct(current_mode),
            },
        },
    )


def _run_mount_root_access(
    path: Path,
    *,
    root_name: str,
    module_root: Path | None,
    operation: str,
    action: Callable[[], Any],
) -> Any:
    _validate_mount_root(path, root_name=root_name, module_root=module_root)
    _restore_rw_mount_root_access(path, root_name=root_name, module_root=module_root, operation=operation)
    try:
        return action()
    except PermissionError as exc:
        _restore_rw_mount_root_access(path, root_name=root_name, module_root=module_root, operation=operation)
        try:
            return action()
        except PermissionError:
            _reject_mount_root_permission(
                root_name=root_name,
                root_path=path,
                module_root=module_root,
                operation=operation,
                recovery="access_denied",
                error=str(exc),
            )


def _validate_slot_component_chain(module_root: Path, parts: tuple[str, ...]) -> None:
    parent = module_root
    for part in parts:
        metadata = _entry_metadata(parent, part)
        if metadata is None:
            return
        if _is_link_or_reparse_stat(metadata):
            _reject_mount_root_link(root_name=part, root_path=parent / part, module_root=module_root)
        if not stat.S_ISDIR(metadata.st_mode):
            raise CandidatePathError("active_slot_root_invalid", detail=f"{part}:{parent / part}")
        parent = parent / part


def _validate_mount_root(path: Path, *, root_name: str, module_root: Path | None = None) -> None:
    if module_root is None:
        if _is_link_or_reparse(path):
            _reject_mount_root_link(root_name=root_name, root_path=path, module_root=module_root)
        if not path.exists():
            return
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise CandidatePathError("active_slot_root_invalid", detail=f"{root_name}:{path}")
        return
    parts = (_ACTIVE_DIR,) if root_name == "_active" else (_ACTIVE_DIR, root_name)
    _validate_slot_component_chain(module_root, parts)


def _validate_layout_mount_roots(layout: ActiveSlotLayout, *, module_root: Path | None = None) -> None:
    if module_root is not None:
        _validate_slot_component_chain(module_root, (_ACTIVE_DIR,))
        for name in _SLOT_SUBDIRS:
            _validate_slot_component_chain(module_root, (_ACTIVE_DIR, name))
        return
    for name in _SLOT_SUBDIRS:
        _validate_mount_root(getattr(layout, name), root_name=name, module_root=module_root)


def ensure_slot_roots(module_root: Path) -> ActiveSlotLayout:
    """Create _active and five mount source dirs once; never replace them."""
    module_root.mkdir(parents=True, exist_ok=True)
    slot_tree_newly_created = _ensure_slot_tree_component(module_root, (_ACTIVE_DIR,))
    slot = active_slot_dir(module_root)
    for name in _SLOT_SUBDIRS:
        _ensure_slot_tree_component(module_root, (_ACTIVE_DIR, name))
    _initialize_rw_mount_trust(module_root, slot_tree_newly_created=slot_tree_newly_created)
    return slot_layout(slot)


def clear_slot_contents(layout: ActiveSlotLayout, *, module_root: Path | None = None) -> None:
    """Remove children inside each mount source dir without deleting the dirs themselves."""
    _validate_layout_mount_roots(layout, module_root=module_root)
    for name in _SLOT_SUBDIRS:
        _clear_directory_children(
            getattr(layout, name),
            root_name=name,
            module_root=module_root,
        )


def clear_active_slot(module_root: Path, *, project_root: Path) -> None:
    layout = ensure_slot_roots(module_root)
    clear_slot_contents(layout, module_root=module_root)


def _clear_directory_children(root: Path, *, root_name: str, module_root: Path | None = None) -> None:
    _validate_mount_root(root, root_name=root_name, module_root=module_root)
    metadata = None
    if module_root is not None:
        if root_name == "_active":
            metadata = _entry_metadata(module_root, _ACTIVE_DIR)
        else:
            metadata = _entry_metadata(module_root / _ACTIVE_DIR, root_name)
    if metadata is None and not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        return
    if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
        raise CandidatePathError("active_slot_root_invalid", detail=f"{root_name}:{root}")

    def _clear_children() -> None:
        for child in list(root.iterdir()):
            if _is_link_or_reparse(child):
                _remove_link_entry(child)
                logger.info(
                    "active_slot_link_removed",
                    extra={
                        "action": "active_slot_link_removed",
                        "status": "completed",
                        "detail": {"path": str(child), "slot_dir": str(root)},
                    },
                )
            elif child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)

    _run_mount_root_access(
        root,
        root_name=root_name,
        module_root=module_root,
        operation="clear_children",
        action=_clear_children,
    )


def _is_link_or_reparse(path: Path) -> bool:
    return path.is_symlink() or _is_reparse(path)


def _remove_link_entry(path: Path) -> None:
    if _is_reparse(path) and path.is_dir():
        path.rmdir()
    else:
        path.unlink(missing_ok=True)


_AGENT_CONTAINER_UID = 1000
_AGENT_CONTAINER_GID = 1000


def align_rw_mount_roots_for_agent_uid(
    layout: ActiveSlotLayout,
    *,
    uid: int = _AGENT_CONTAINER_UID,
    gid: int = _AGENT_CONTAINER_GID,
) -> None:
    if os.environ.get("BRIDLE_RUN_DOCKER_TESTS") != "1":
        return
    if os.environ.get("BRIDLE_CONTAINER_DRY_RUN") == "1":
        return
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    if not docker_host and shutil.which("docker") is None:
        return
    docker_exe = shutil.which("docker") or "docker"
    image = (
        os.environ.get("BRIDLE_WORKER_IMAGE", "").strip()
        or os.environ.get("BRIDLE_AGENT_IMAGE", "").strip()
        or "alpine:3.20"
    )
    for name in _RW_MOUNT_ROOTS:
        root = getattr(layout, name)
        if not root.is_dir():
            continue
        cmd = [
            docker_exe,
            "run",
            "--rm",
            "--user",
            "0",
            "-v",
            f"{root.resolve()}:/mnt:rw",
            image,
            "chown",
            "-R",
            f"{uid}:{gid}",
            "/mnt",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "chown failed").strip()
            raise CandidatePathError(
                "active_slot_uid_align_failed",
                detail=f"{name}:{root}:{detail}",
            )


def prepare_active_slot(
    module_root: Path,
    candidate_root: Path,
    *,
    project_root: Path,
    candidate_rel: str,
    run_id: str,
) -> ActiveSlotLayout:
    """Clear and populate the module active slot from one host candidate."""
    layout = ensure_slot_roots(module_root)
    clear_slot_contents(layout, module_root=module_root)
    for name in _SLOT_SUBDIRS:
        src = candidate_root / name
        dst = getattr(layout, name)

        def _populate(*, _src: Path = src, _dst: Path = dst) -> None:
            if _src.is_dir():
                _copy_tree_no_links(_src, _dst)
            elif not _dst.exists():
                _dst.mkdir(parents=True, exist_ok=True)

        if name in _RW_MOUNT_ROOTS:
            _run_mount_root_access(
                dst,
                root_name=name,
                module_root=module_root,
                operation="prepare_populate",
                action=_populate,
            )
        else:
            _validate_mount_root(dst, root_name=name, module_root=module_root)
            _populate()
    write_lease(layout, candidate_rel=candidate_rel, run_id=run_id, module_root=module_root)
    align_rw_mount_roots_for_agent_uid(layout)
    logger.info(
        "active_slot_prepared",
        extra={
            "action": "active_slot_prepared",
            "status": "completed",
            "detail": {
                "module_root": str(module_root),
                "candidate_root": str(candidate_root),
                "candidate_rel": candidate_rel,
                "run_id": run_id,
            },
        },
    )
    return layout


def collect_active_slot(
    module_root: Path,
    candidate_root: Path,
    *,
    project_root: Path,
) -> None:
    """Copy writable slot artifacts back; refuse links and out-of-scope targets."""
    layout = slot_layout(active_slot_dir(module_root))
    _validate_layout_mount_roots(layout, module_root=module_root)
    slot_metadata = _entry_metadata(module_root, _ACTIVE_DIR)
    if slot_metadata is None or not stat.S_ISDIR(slot_metadata.st_mode):
        return
    for name in _COLLECT_SUBDIRS:
        src = getattr(layout, name)
        dst = candidate_root / name
        if not src.exists():
            continue

        def _collect_one(*, _src: Path = src, _dst: Path = dst) -> None:
            if _dst.exists():
                safe_rmtree(_dst, project_root=project_root, expected_root=candidate_root)
            if _src.is_dir():
                _copy_tree_no_links(_src, _dst, expected_root=layout.slot_root)
            else:
                _refuse_link(_src)
                _dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(_src, _dst)

        if name in _RW_MOUNT_ROOTS:
            _run_mount_root_access(
                src,
                root_name=name,
                module_root=module_root,
                operation="collect",
                action=_collect_one,
            )
        else:
            _collect_one()
    logger.info(
        "active_slot_collected",
        extra={
            "action": "active_slot_collected",
            "status": "completed",
            "detail": {
                "module_root": str(module_root),
                "candidate_root": str(candidate_root),
            },
        },
    )


def _refuse_link(path: Path) -> None:
    if path.is_symlink() or _is_reparse(path):
        raise CandidatePathError("refuse_symlink_or_reparse_collect")


def _is_reparse(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        attrs = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _copy_tree_no_links(
    src: Path,
    dst: Path,
    *,
    expected_root: Path | None = None,
) -> None:
    if not src.is_dir():
        raise CandidatePathError("refuse_non_directory_copy")
    _refuse_link(src)
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirnames, filenames in os.walk(src, topdown=True, followlinks=False):
        root_path = Path(root)
        _refuse_link(root_path)
        if expected_root is not None:
            try:
                root_path.resolve().relative_to(expected_root.resolve())
            except ValueError as exc:
                raise CandidatePathError("refuse_unexpected_collect_source") from exc
        rel = root_path.relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        dirnames[:] = [name for name in dirnames if not (root_path / name).is_symlink()]
        for name in filenames:
            child = root_path / name
            _refuse_link(child)
            out = target_dir / name
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, out)
