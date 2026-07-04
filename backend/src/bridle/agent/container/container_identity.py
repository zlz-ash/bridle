"""Docker container identity labels and adoption validation."""
from __future__ import annotations

import hashlib
from pathlib import Path

from bridle.agent.container.docker_inspect import (
    cpus_to_nano,
    memory_bytes,
    normalize_cap_drop,
    normalize_security_opt,
)
from bridle.agent.container.runner import ContainerMount, ContainerRequest

LABEL_SCHEMA = "v1"
LABEL_PREFIX = "bridle."


def project_label(project_root: Path) -> str:
    return hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]


def mount_identity(mount: ContainerMount) -> str:
    readonly = "ro" if mount.readonly else "rw"
    try:
        source = str(mount.source.resolve())
    except OSError:
        source = str(mount.source)
    raw = f"{source}|{mount.target}|{readonly}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def mounts_identity(mounts: list[ContainerMount]) -> str:
    parts = sorted_mount_identities(mounts)
    if parts is None:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def sorted_mount_identities(mounts: list[ContainerMount]) -> list[str] | None:
    ids = [mount_identity(m) for m in mounts]
    if len(ids) != len(set(ids)):
        return None
    return sorted(ids)


def build_container_labels(
    *,
    project_root: Path,
    module_id: str,
    boundary_fingerprint: str,
    image_version: str,
    mounts: list[ContainerMount],
) -> dict[str, str]:
    return {
        f"{LABEL_PREFIX}schema": LABEL_SCHEMA,
        f"{LABEL_PREFIX}project": project_label(project_root),
        f"{LABEL_PREFIX}module": module_id,
        f"{LABEL_PREFIX}boundary_fp": boundary_fingerprint,
        f"{LABEL_PREFIX}image_version": image_version,
        f"{LABEL_PREFIX}mount_id": mounts_identity(mounts),
    }


def build_container_labels_from_mount(
    *,
    project_root: Path,
    module_id: str,
    boundary_fingerprint: str,
    image_version: str,
    primary_mount: ContainerMount,
) -> dict[str, str]:
    return build_container_labels(
        project_root=project_root,
        module_id=module_id,
        boundary_fingerprint=boundary_fingerprint,
        image_version=image_version,
        mounts=[primary_mount],
    )


def _command_tuple(request: ContainerRequest) -> tuple[str, ...]:
    return tuple(str(x) for x in request.command)


def validate_container_identity(expected: ContainerRequest, actual: ContainerRequest) -> list[str]:
    """Return mismatch reasons; empty list means adoptable."""
    errors: list[str] = []
    for key in ("schema", "project", "module", "boundary_fp", "image_version", "mount_id"):
        label = f"{LABEL_PREFIX}{key}"
        if expected.labels.get(label) != actual.labels.get(label):
            errors.append(f"label_mismatch:{key}")
    if expected.image_id:
        if not actual.image_id:
            errors.append("image_id_missing")
        elif expected.image_id != actual.image_id:
            errors.append("image_id_mismatch")
    elif expected.image != actual.image:
        errors.append("image_mismatch")
    if expected.network_mode != actual.network_mode:
        errors.append("network_mode_mismatch")
    if expected.keep_alive != actual.keep_alive:
        errors.append("keep_alive_mismatch")
    if expected.read_only_root != actual.read_only_root:
        errors.append("read_only_root_mismatch")
    if expected.run_user != actual.run_user:
        errors.append("run_user_mismatch")
    if _command_tuple(expected) != _command_tuple(actual):
        errors.append("command_mismatch")
    if expected.name != actual.name:
        errors.append("name_mismatch")
    if expected.privileged != actual.privileged:
        errors.append("privileged_mismatch")
    if normalize_cap_drop(expected.cap_drop) != normalize_cap_drop(actual.cap_drop):
        errors.append("cap_drop_mismatch")
    if normalize_security_opt(expected.security_opt) != normalize_security_opt(actual.security_opt):
        errors.append("security_opt_mismatch")
    if expected.pids_limit != actual.pids_limit:
        errors.append("pids_limit_mismatch")
    if memory_bytes(expected.memory) != memory_bytes(actual.memory):
        errors.append("memory_mismatch")
    if cpus_to_nano(expected.cpus) != cpus_to_nano(actual.cpus):
        errors.append("cpus_mismatch")
    expected_mounts = sorted_mount_identities(expected.mounts)
    actual_mounts = sorted_mount_identities(actual.mounts)
    if expected_mounts is None or actual_mounts is None:
        errors.append("duplicate_mount")
    else:
        expected_mount_label = expected.labels.get(f"{LABEL_PREFIX}mount_id")
        actual_mount_label = actual.labels.get(f"{LABEL_PREFIX}mount_id")
        if expected_mount_label and actual_mount_label:
            if expected_mount_label != actual_mount_label:
                errors.append("mount_identity_mismatch")
        elif expected_mounts != actual_mounts:
            errors.append("mount_identity_mismatch")
    return errors
