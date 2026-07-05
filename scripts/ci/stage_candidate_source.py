#!/usr/bin/env python3
"""Stage normalized candidate source for protected Docker builds."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("bridle.stage_candidate_source")
SKIP_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
SKIP_SUFFIXES = frozenset({".pyc", ".pyo", ".tmp", ".swp"})
EXCLUDED_RELATIVE_PATHS = frozenset(
    {
        "backend/src/bridle/agent/container/agent.Dockerfile",
    }
)
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
OWNERSHIP_SCHEMA = "bridle.staging_ownership/v1"
OWNERSHIP_FILE = ".bridle-staging-ownership.json"


@dataclass(frozen=True)
class StagingIdentity:
    path: str
    device: int
    inode: int
    is_symlink: bool


def _identity(path: Path) -> StagingIdentity:
    metadata = os.lstat(path)
    return StagingIdentity(
        path=str(path),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        is_symlink=stat.S_ISLNK(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT),
    )


def _write_ownership(staging: Path, *, run_id: str) -> None:
    identity = _identity(staging)
    payload = {
        "schema": OWNERSHIP_SCHEMA,
        "run_id": run_id,
        "path": identity.path,
        "device": identity.device,
        "inode": identity.inode,
        "is_symlink": identity.is_symlink,
        "created_at": uuid.uuid4().hex,
    }
    ownership_path = staging / OWNERSHIP_FILE
    temporary = ownership_path.with_name(f".{ownership_path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, ownership_path)


def _verify_ownership(staging: Path, *, run_id: str) -> None:
    ownership_path = staging / OWNERSHIP_FILE
    if not ownership_path.is_file() or _is_link_or_reparse(ownership_path):
        raise StageCandidateError("staging_ownership_missing", detail=str(ownership_path))
    try:
        payload = json.loads(ownership_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StageCandidateError("staging_ownership_invalid", detail=str(exc)) from exc
    if not isinstance(payload, dict):
        raise StageCandidateError("staging_ownership_invalid", detail="not_object")
    if payload.get("schema") != OWNERSHIP_SCHEMA:
        raise StageCandidateError("staging_ownership_schema_mismatch", detail=str(payload.get("schema")))
    if payload.get("run_id") != run_id:
        raise StageCandidateError(
            "staging_ownership_foreign_run",
            detail=f"expected={run_id} got={payload.get('run_id')}",
        )
    current = _identity(staging)
    if payload.get("path") != current.path:
        raise StageCandidateError(
            "staging_ownership_path_mismatch",
            detail=f"expected={payload.get('path')} got={current.path}",
        )
    if payload.get("device") != current.device or payload.get("inode") != current.inode:
        raise StageCandidateError(
            "staging_ownership_identity_mismatch",
            detail=f"expected dev={payload.get('device')} ino={payload.get('inode')} got dev={current.device} ino={current.inode}",
        )
    if payload.get("is_symlink") or current.is_symlink:
        raise StageCandidateError("staging_ownership_symlink_rejected")


def _release_staging(staging: Path, *, run_id: str) -> None:
    try:
        _verify_ownership(staging, run_id=run_id)
    except StageCandidateError:
        raise
    shutil.rmtree(staging)
    LOGGER.info("stage_candidate_staging_released path=%s run_id=%s", staging, run_id)


def _allowed_staging_root() -> Path | None:
    raw = os.environ.get("BRIDLE_STAGING_ROOT", "").strip()
    if not raw:
        return None
    root = Path(raw).resolve()
    if _is_link_or_reparse(root) or not root.is_dir():
        raise StageCandidateError("stage_candidate_allowed_root_invalid", detail=str(root))
    return root


def _validate_staging_target(staging_root: Path) -> Path:
    staging = staging_root.resolve()
    if _is_link_or_reparse(staging):
        raise StageCandidateError("stage_candidate_link_rejected", detail=str(staging))
    allowed = _allowed_staging_root()
    if allowed is not None:
        try:
            staging.relative_to(allowed)
        except ValueError as exc:
            raise StageCandidateError("stage_candidate_outside_allowed_root", detail=str(staging)) from exc
        if staging == allowed:
            raise StageCandidateError("stage_candidate_must_not_equal_allowed_root", detail=str(staging))
    return staging


class StageCandidateError(RuntimeError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _should_copy(path: Path) -> bool:
    if any(part in SKIP_DIR_NAMES for part in path.parts):
        return False
    if path.suffix in SKIP_SUFFIXES or path.name.endswith(".tmp"):
        return False
    return path.is_file() and not _is_link_or_reparse(path)


def stage_candidate_source(candidate_root: Path, staging_root: Path, *, run_id: str | None = None) -> Path:
    source = candidate_root.resolve()
    allowed = _allowed_staging_root()
    requested = staging_root.resolve()
    if allowed is not None:
        try:
            requested.relative_to(allowed)
        except ValueError:
            staging_root = requested
        else:
            if requested == allowed:
                owner = run_id or uuid.uuid4().hex[:12]
                staging_root = allowed / f"candidate-staging-{owner}"
            else:
                staging_root = requested
    else:
        staging_root = requested
    staging = _validate_staging_target(staging_root)
    if staging.exists():
        if _is_link_or_reparse(staging):
            raise StageCandidateError("stage_candidate_link_rejected", detail=str(staging))
        if run_id:
            try:
                _verify_ownership(staging, run_id=run_id)
            except StageCandidateError as exc:
                raise StageCandidateError(
                    "stage_candidate_foreign_staging",
                    detail=f"{exc.error_code}:{exc.detail}",
                ) from exc
            _release_staging(staging, run_id=run_id)
        else:
            raise StageCandidateError(
                "stage_candidate_existing_without_run_id",
                detail=str(staging),
            )
    staging.mkdir(parents=True, exist_ok=True)
    effective_run_id = run_id or uuid.uuid4().hex[:12]
    _write_ownership(staging, run_id=effective_run_id)

    copied = 0
    for relative_root in ("backend/pyproject.toml",):
        src = source / relative_root
        if not src.is_file() or _is_link_or_reparse(src):
            raise StageCandidateError("stage_candidate_required_missing", detail=relative_root)
        dst = staging / relative_root
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst, follow_symlinks=False)
        copied += 1

    src_root = source / "backend" / "src"
    if not src_root.is_dir():
        raise StageCandidateError("stage_candidate_src_missing", detail=str(src_root))
    for path in sorted(src_root.rglob("*")):
        if _is_link_or_reparse(path):
            raise StageCandidateError("stage_candidate_link_rejected", detail=str(path))
        if not _should_copy(path):
            continue
        relative = path.relative_to(source).as_posix()
        if relative in EXCLUDED_RELATIVE_PATHS:
            continue
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        shutil.copyfile(path, temporary, follow_symlinks=False)
        os.replace(temporary, destination)
        copied += 1

    if copied == 0:
        raise StageCandidateError("stage_candidate_empty")
    LOGGER.info("stage_candidate_source_complete files=%d staging=%s", copied, staging)
    return staging


def compute_staged_source_digest(staging_root: Path) -> str:
    root = staging_root.resolve()
    paths: list[Path] = []
    for candidate in (
        root / "backend" / "pyproject.toml",
    ):
        if candidate.is_file() and not _is_link_or_reparse(candidate):
            paths.append(candidate)
    src_root = root / "backend" / "src"
    if src_root.is_dir():
        for path in sorted(src_root.rglob("*")):
            if _should_copy(path):
                relative = path.relative_to(root).as_posix()
                if relative not in EXCLUDED_RELATIVE_PATHS:
                    paths.append(path)
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate_root", type=Path)
    parser.add_argument("staging_root", type=Path)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        staged = stage_candidate_source(args.candidate_root, args.staging_root, run_id=args.run_id or None)
    except (OSError, StageCandidateError) as exc:
        code = getattr(exc, "error_code", "stage_candidate_io_error")
        detail = getattr(exc, "detail", str(exc))
        LOGGER.error("stage_candidate_failed code=%s detail=%s", code, detail)
        return 1
    print(staged)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
