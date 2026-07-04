#!/usr/bin/env python3
"""Protected Docker harness filesystem and identity controller."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from docker_resource_registry import DockerResourceRegistry

LOGGER = logging.getLogger("bridle.trusted_harness")
SKIP_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
SKIP_SUFFIXES = frozenset({".pyc", ".pyo", ".tmp", ".swp"})
REVIEW_METADATA_PATH = "/opt/bridle/.review-metadata.json"
REVIEW_METADATA_SCHEMA = "bridle.review_image_metadata/v1"
PRODUCER_VERSION = "bridle.entrypoint/v1"
PROTECTED_DOCKERFILE = "scripts/ci/protected/agent.Dockerfile"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_DOCKER_REGISTRY = DockerResourceRegistry()


class TrustedHarnessError(RuntimeError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _absolute_without_resolve(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _validated_relative_path(raw: str) -> PurePosixPath:
    text = raw.strip()
    if not text or "\\" in text:
        raise TrustedHarnessError("trusted_harness_manifest_path_invalid", detail=raw)
    relative = PurePosixPath(text)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise TrustedHarnessError("trusted_harness_manifest_path_invalid", detail=raw)
    return relative


def parse_manifest_lines(lines: list[str]) -> list[str]:
    entries: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        relative = _validated_relative_path(raw)
        normalized = relative.as_posix()
        if normalized in seen:
            raise TrustedHarnessError("trusted_harness_manifest_duplicate", detail=normalized)
        seen.add(normalized)
        entries.append(normalized)
    return entries


def load_manifest(trusted_root: Path, manifest_path: Path) -> list[str]:
    trusted = _absolute_without_resolve(trusted_root)
    manifest = _absolute_without_resolve(manifest_path)
    try:
        manifest.relative_to(trusted)
    except ValueError as exc:
        raise TrustedHarnessError("trusted_harness_manifest_outside_root", detail=str(manifest)) from exc
    if _is_link_or_reparse(manifest) or not manifest.is_file():
        raise TrustedHarnessError("trusted_harness_manifest_invalid", detail=str(manifest))
    lines = [
        line
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return parse_manifest_lines(lines)


def _module_file_candidates(module_name: str) -> tuple[str, str]:
    module_path = module_name.replace(".", "/")
    return (
        f"backend/src/{module_path}.py",
        f"backend/src/{module_path}/__init__.py",
    )


def audit_manifest_import_closure(trusted_root: Path, entries: list[str]) -> None:
    trusted = _absolute_without_resolve(trusted_root)
    entry_set = set(parse_manifest_lines(entries))
    prefix = "bridle.agent.container.tests"
    missing: set[str] = set()
    forbidden_dynamic: set[str] = set()

    for relative in sorted(entry_set):
        if not relative.endswith(".py"):
            continue
        source_path = trusted.joinpath(*PurePosixPath(relative).parts)
        source_text = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source_text, filename=relative)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == prefix:
                    imported_modules.update(f"{prefix}.{alias.name}" for alias in node.names)
                else:
                    imported_modules.add(node.module)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if "scripts/ci/" in value or value.endswith("sentinel_registry.py"):
                    forbidden_dynamic.add(f"{relative}:{value}")
        for module_name in imported_modules:
            if not module_name.startswith(f"{prefix}."):
                continue
            candidates = _module_file_candidates(module_name)
            existing = next(
                (
                    candidate
                    for candidate in candidates
                    if trusted.joinpath(*PurePosixPath(candidate).parts).is_file()
                ),
                None,
            )
            if existing is not None and existing not in entry_set:
                missing.add(existing)
        if "importlib.util.spec_from_file_location" in source_text or "spec_from_file_location" in source_text:
            forbidden_dynamic.add(relative)
    if missing:
        raise TrustedHarnessError(
            "trusted_harness_dependency_missing",
            detail=",".join(sorted(missing)),
        )
    if forbidden_dynamic:
        raise TrustedHarnessError(
            "trusted_harness_dynamic_script_reference",
            detail=",".join(sorted(forbidden_dynamic)),
        )
    LOGGER.info("trusted_harness_dependency_audit_passed files=%d", len(entry_set))


def _assert_safe_components(
    root: Path,
    relative: str,
    *,
    link_error: str,
    leaf_may_be_missing: bool,
) -> Path:
    root = _absolute_without_resolve(root)
    if _is_link_or_reparse(root):
        raise TrustedHarnessError(link_error, detail=str(root))
    if not root.is_dir():
        raise TrustedHarnessError("trusted_harness_root_invalid", detail=str(root))

    destination = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    parts = PurePosixPath(relative).parts
    for index, part in enumerate(parts):
        current = current / part
        is_leaf = index == len(parts) - 1
        if not os.path.lexists(current):
            if is_leaf and leaf_may_be_missing:
                break
            if not is_leaf:
                continue
            raise TrustedHarnessError("trusted_harness_file_missing", detail=str(current))
        if _is_link_or_reparse(current):
            raise TrustedHarnessError(link_error, detail=str(current))
        if not is_leaf and not current.is_dir():
            raise TrustedHarnessError("trusted_harness_parent_not_directory", detail=str(current))
    return destination


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def overlay_files(
    candidate_root: Path,
    trusted_root: Path,
    entries: list[str],
) -> dict[str, str]:
    candidate = _absolute_without_resolve(candidate_root)
    trusted = _absolute_without_resolve(trusted_root)
    candidate.mkdir(parents=True, exist_ok=True)
    snapshot: dict[str, str] = {}

    for relative in parse_manifest_lines(entries):
        source = _assert_safe_components(
            trusted,
            relative,
            link_error="trusted_harness_trusted_link_rejected",
            leaf_may_be_missing=False,
        )
        destination = _assert_safe_components(
            candidate,
            relative,
            link_error="trusted_harness_candidate_link_rejected",
            leaf_may_be_missing=True,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        _assert_safe_components(
            candidate,
            relative,
            link_error="trusted_harness_candidate_link_rejected",
            leaf_may_be_missing=True,
        )
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temporary, follow_symlinks=False)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        digest = _sha256_file(destination)
        snapshot[relative] = digest
        LOGGER.info(
            "trusted_harness_file_overlaid path=%s sha256=%s",
            relative,
            digest,
        )
    return snapshot


def verify_overlay_snapshot(candidate_root: Path, snapshot: dict[str, str]) -> None:
    candidate = _absolute_without_resolve(candidate_root)
    for relative, expected_digest in snapshot.items():
        path = _assert_safe_components(
            candidate,
            relative,
            link_error="trusted_harness_candidate_link_rejected",
            leaf_may_be_missing=False,
        )
        actual_digest = _sha256_file(path)
        if actual_digest != expected_digest:
            raise TrustedHarnessError(
                "trusted_harness_overlay_mutated",
                detail=f"{relative}: expected={expected_digest} actual={actual_digest}",
            )
    LOGGER.info("trusted_harness_snapshot_verified files=%d", len(snapshot))


def _should_hash_path(path: Path) -> bool:
    if any(part in SKIP_DIR_NAMES for part in path.parts):
        return False
    if path.suffix in SKIP_SUFFIXES or path.name.endswith(".tmp"):
        return False
    return path.is_file() and not _is_link_or_reparse(path)


def compute_candidate_source_digest(candidate_root: Path) -> str:
    root = _absolute_without_resolve(candidate_root)
    paths: list[Path] = []
    container_root = root / "backend" / "src" / "bridle" / "agent" / "container"
    src_root = root / "backend" / "src"
    for candidate in (
        root / "backend" / "pyproject.toml",
        container_root / "agent.Dockerfile",
    ):
        if candidate.is_file() and not _is_link_or_reparse(candidate):
            paths.append(candidate)
    if src_root.is_dir():
        for path in sorted(src_root.rglob("*")):
            if _is_link_or_reparse(path):
                raise TrustedHarnessError("trusted_harness_source_link_rejected", detail=str(path))
            if _should_hash_path(path):
                paths.append(path)
    if not paths:
        raise TrustedHarnessError("trusted_harness_source_paths_missing")
    digest = hashlib.sha256()
    for path in paths:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise TrustedHarnessError("trusted_harness_source_path_escape", detail=str(path)) from exc
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    result = f"sha256:{digest.hexdigest()}"
    LOGGER.info("trusted_harness_source_digest digest=%s files=%d", result, len(paths))
    return result


def protected_dockerfile_path(trusted_root: Path) -> Path:
    trusted = _absolute_without_resolve(trusted_root)
    relative = PurePosixPath(PROTECTED_DOCKERFILE)
    current = trusted
    for part in relative.parts:
        current = current / part
        if not os.path.lexists(current):
            if part == relative.parts[-1]:
                raise TrustedHarnessError("trusted_harness_protected_dockerfile_missing", detail=str(current))
            continue
        if _is_link_or_reparse(current):
            raise TrustedHarnessError("trusted_harness_protected_dockerfile_link_rejected", detail=str(current))
    dockerfile = current
    if not dockerfile.is_file():
        raise TrustedHarnessError("trusted_harness_protected_dockerfile_missing", detail=str(dockerfile))
    return dockerfile


def _run_bytes(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    LOGGER.info("trusted_harness_command_started command=%s", json.dumps(command))
    result = subprocess.run(
        command,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    LOGGER.info(
        "trusted_harness_command_finished returncode=%d stdout_bytes=%d stderr_bytes=%d",
        result.returncode,
        len(result.stdout or b""),
        len(result.stderr or b""),
    )
    return result


def _decode_output(result: subprocess.CompletedProcess[bytes]) -> tuple[str, str]:
    stdout = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    return stdout, stderr


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    raw = _run_bytes(command, timeout=timeout)
    stdout, stderr = _decode_output(raw)
    return subprocess.CompletedProcess(command, raw.returncode, stdout, stderr)


def _workspace_temp_dir() -> Path:
    for key in ("BRIDLE_RUNNER_TEMP", "RUNNER_TEMP", "TMPDIR"):
        value = os.environ.get(key, "").strip()
        if value:
            candidate = Path(value)
            if candidate.is_dir():
                return candidate
    return Path(tempfile.gettempdir())


def read_image_metadata_via_cp(image: str, *, run_id: str | None = None) -> dict[str, Any]:
    owner = run_id or uuid.uuid4().hex[:12]
    container_name = f"bridle-metadata-{owner}"
    create_result = _run(["docker", "create", "--name", container_name, image], timeout=60)
    if create_result.returncode != 0 or not create_result.stdout.strip():
        raise TrustedHarnessError(
            "trusted_harness_image_create_failed",
            detail=create_result.stderr.strip() or image,
        )
    container_id = create_result.stdout.strip()
    _DOCKER_REGISTRY.register_container(run_id=owner, name=container_name, container_id=container_id)
    temporary = _workspace_temp_dir() / f"bridle-metadata-{uuid.uuid4().hex}.json"
    payload: dict[str, Any] | None = None
    try:
        copy_result = _run(
            ["docker", "cp", f"{container_id}:{REVIEW_METADATA_PATH}", str(temporary)],
            timeout=60,
        )
        if copy_result.returncode != 0 or not temporary.is_file():
            raise TrustedHarnessError(
                "trusted_harness_image_metadata_missing",
                detail=copy_result.stderr.strip() or image,
            )
        payload = json.loads(temporary.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrustedHarnessError("trusted_harness_image_metadata_invalid", detail=str(exc)) from exc
    finally:
        try:
            inspect = _run(["docker", "inspect", "-f", "{{.Id}}", container_name], timeout=15)
            if inspect.returncode != 0 or not inspect.stdout.strip():
                raise TrustedHarnessError(
                    "trusted_harness_metadata_container_missing",
                    detail=container_name,
                )
            actual_id = inspect.stdout.strip()
            _DOCKER_REGISTRY.verify_container(run_id=owner, name=container_name, container_id=actual_id)
            remove_result = _run(["docker", "rm", container_name], timeout=30)
            if remove_result.returncode != 0:
                raise TrustedHarnessError(
                    "trusted_harness_container_remove_failed",
                    detail=remove_result.stderr.strip() or container_name,
                )
            _DOCKER_REGISTRY.release_container(run_id=owner, name=container_name)
        except RuntimeError as exc:
            raise TrustedHarnessError("trusted_harness_metadata_cleanup_failed", detail=str(exc)) from exc
        if temporary.exists():
            temporary.unlink()
    if payload is None or not isinstance(payload, dict):
        raise TrustedHarnessError("trusted_harness_image_metadata_invalid")
    return payload


def build_protected_review_image(
    *,
    trusted_root: Path,
    staging_root: Path,
    tag: str,
    source_digest: str,
    run_id: str | None = None,
) -> None:
    trusted = _absolute_without_resolve(trusted_root)
    if _is_link_or_reparse(trusted):
        raise TrustedHarnessError("trusted_harness_root_link_rejected", detail=str(trusted))
    dockerfile = protected_dockerfile_path(trusted_root)
    staging = _absolute_without_resolve(staging_root)
    if _is_link_or_reparse(staging) or not staging.is_dir():
        raise TrustedHarnessError("trusted_harness_staging_invalid", detail=str(staging))
    owner = run_id or uuid.uuid4().hex[:12]
    build_result = _run(
        [
            "docker",
            "build",
            "-f",
            str(dockerfile),
            "--build-arg",
            f"REVIEW_SOURCE_DIGEST={source_digest}",
            "--build-arg",
            f"PRODUCER_VERSION={PRODUCER_VERSION}",
            "-t",
            tag,
            str(staging),
        ],
        timeout=900,
    )
    if build_result.returncode != 0:
        raise TrustedHarnessError(
            "trusted_harness_image_build_failed",
            detail=(build_result.stderr or build_result.stdout)[-2000:],
        )
    inspect = _run(["docker", "image", "inspect", "-f", "{{.Id}}", tag], timeout=15)
    if inspect.returncode != 0 or not inspect.stdout.strip():
        raise TrustedHarnessError("trusted_harness_image_missing", detail=tag)
    _DOCKER_REGISTRY.register_tag(run_id=owner, tag=tag, image_id=inspect.stdout.strip())
    LOGGER.info(
        "trusted_harness_image_built tag=%s source_digest=%s dockerfile=%s run_id=%s",
        tag,
        source_digest,
        dockerfile,
        owner,
    )


def verify_review_image(image: str, expected_source_digest: str, *, run_id: str | None = None) -> str:
    metadata = read_image_metadata_via_cp(image, run_id=run_id)
    if metadata.get("schema") != REVIEW_METADATA_SCHEMA:
        raise TrustedHarnessError("trusted_harness_image_schema_mismatch")
    if metadata.get("source_digest") != expected_source_digest:
        raise TrustedHarnessError("trusted_harness_image_source_mismatch")
    if metadata.get("producer") != PRODUCER_VERSION:
        raise TrustedHarnessError("trusted_harness_image_producer_mismatch")

    inspect_result = _run(
        ["docker", "image", "inspect", "-f", "{{.Id}}", image],
        timeout=15,
    )
    if inspect_result.returncode != 0 or not inspect_result.stdout.strip():
        raise TrustedHarnessError("trusted_harness_image_missing", detail=image)
    digest = inspect_result.stdout.strip()
    if not digest.startswith("sha256:"):
        digest = f"sha256:{digest}"
    if run_id:
        _DOCKER_REGISTRY.verify_tag(run_id=run_id, tag=image, image_id=digest)
    LOGGER.info(
        "trusted_harness_image_verified image=%s source_digest=%s image_digest=%s",
        image,
        expected_source_digest,
        digest,
    )
    return digest


def _write_snapshot(path: Path, snapshot: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(snapshot, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _read_snapshot(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise TrustedHarnessError("trusted_harness_snapshot_invalid", detail=str(path))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    overlay_parser = subparsers.add_parser("overlay")
    overlay_parser.add_argument("candidate_root", type=Path)
    overlay_parser.add_argument("trusted_root", type=Path)
    overlay_parser.add_argument("manifest", type=Path)
    overlay_parser.add_argument("snapshot", type=Path)

    verify_parser = subparsers.add_parser("verify-overlay")
    verify_parser.add_argument("candidate_root", type=Path)
    verify_parser.add_argument("snapshot", type=Path)

    digest_parser = subparsers.add_parser("source-digest")
    digest_parser.add_argument("candidate_root", type=Path)

    image_parser = subparsers.add_parser("verify-image")
    image_parser.add_argument("image")
    image_parser.add_argument("--source-digest", required=True)

    build_parser = subparsers.add_parser("build-image")
    build_parser.add_argument("trusted_root", type=Path)
    build_parser.add_argument("staging_root", type=Path)
    build_parser.add_argument("tag")
    build_parser.add_argument("--source-digest", required=True)

    args = parser.parse_args(argv)
    _configure_logging()
    try:
        if args.command == "overlay":
            entries = load_manifest(args.trusted_root, args.manifest)
            audit_manifest_import_closure(args.trusted_root, entries)
            snapshot = overlay_files(args.candidate_root, args.trusted_root, entries)
            _write_snapshot(args.snapshot, snapshot)
        elif args.command == "verify-overlay":
            verify_overlay_snapshot(args.candidate_root, _read_snapshot(args.snapshot))
        elif args.command == "source-digest":
            print(compute_candidate_source_digest(args.candidate_root))
        elif args.command == "verify-image":
            print(verify_review_image(args.image, args.source_digest))
        elif args.command == "build-image":
            build_protected_review_image(
                trusted_root=args.trusted_root,
                staging_root=args.staging_root,
                tag=args.tag,
                source_digest=args.source_digest,
            )
            print(verify_review_image(args.tag, args.source_digest))
    except (OSError, subprocess.TimeoutExpired, TrustedHarnessError) as exc:
        error_code = getattr(exc, "error_code", "trusted_harness_io_error")
        detail = getattr(exc, "detail", str(exc))
        LOGGER.error("trusted_harness_failed code=%s detail=%s", error_code, detail)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
