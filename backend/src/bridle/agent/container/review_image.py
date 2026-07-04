"""Build and verify Docker review images bound to current agent source."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("bridle")

REVIEW_IMAGE_TAG = "bridle-agent:review"
REVIEW_METADATA_PATH = "/opt/bridle/.review-metadata.json"
PRODUCER_VERSION = "bridle.entrypoint/v1"
REVIEW_METADATA_SCHEMA = "bridle.review_image_metadata/v1"

_SKIP_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
_SKIP_SUFFIXES = frozenset({".pyc", ".pyo", ".tmp", ".swp"})


class ReviewImageError(RuntimeError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


@dataclass(frozen=True)
class ReviewImageInfo:
    tag: str
    source_digest: str
    image_digest: str
    producer_version: str


def find_repo_root(start: Path | None = None) -> Path:
    cursor = (start or Path(__file__)).resolve()
    for parent in [cursor, *cursor.parents]:
        if (parent / "backend" / "pyproject.toml").is_file() and (parent / "backend" / "src").is_dir():
            return parent
    raise ReviewImageError("review_repo_root_missing")


def _should_hash_path(path: Path) -> bool:
    if any(part in _SKIP_DIR_NAMES for part in path.parts):
        return False
    if path.suffix in _SKIP_SUFFIXES:
        return False
    if path.name.endswith(".tmp"):
        return False
    return path.is_file()


def iter_agent_source_paths(repo_root: Path) -> list[Path]:
    root = repo_root.resolve()
    container_root = root / "backend" / "src" / "bridle" / "agent" / "container"
    src_root = root / "backend" / "src"
    paths: list[Path] = []
    for candidate in (
        root / "backend" / "pyproject.toml",
        container_root / "agent.Dockerfile",
    ):
        if candidate.is_file():
            paths.append(candidate)
    if src_root.is_dir():
        for path in sorted(src_root.rglob("*")):
            if _should_hash_path(path):
                paths.append(path)
    return paths


def compute_agent_source_digest(repo_root: Path | None = None) -> str:
    root = (repo_root or find_repo_root()).resolve()
    paths = iter_agent_source_paths(root)
    if not paths:
        raise ReviewImageError("review_source_paths_missing")
    hasher = hashlib.sha256()
    for path in paths:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        hasher.update(rel)
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _read_image_metadata(image: str) -> dict:
    create_proc = subprocess.run(
        ["docker", "create", image],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if create_proc.returncode != 0 or not create_proc.stdout.strip():
        raise ReviewImageError(
            "review_image_create_failed",
            detail=create_proc.stderr.strip() or image,
        )
    container_id = create_proc.stdout.strip()
    import tempfile
    import uuid

    temporary = Path(tempfile.gettempdir()) / f"bridle-review-metadata-{uuid.uuid4().hex}.json"
    try:
        copy_proc = subprocess.run(
            ["docker", "cp", f"{container_id}:{REVIEW_METADATA_PATH}", str(temporary)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if copy_proc.returncode != 0 or not temporary.is_file():
            raise ReviewImageError(
                "review_image_metadata_missing",
                detail=copy_proc.stderr.strip() or image,
            )
        payload = json.loads(temporary.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewImageError("review_image_metadata_invalid", detail=str(exc)) from exc
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=30)
        if temporary.exists():
            temporary.unlink()
    if not isinstance(payload, dict):
        raise ReviewImageError("review_image_metadata_invalid")
    return payload


def _resolve_image_digest(image: str) -> str:
    proc = subprocess.run(
        ["docker", "image", "inspect", "-f", "{{.Id}}", image],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ReviewImageError("review_image_missing", detail=image)
    digest = proc.stdout.strip()
    if digest.startswith("sha256:"):
        return digest
    return f"sha256:{digest}"


def verify_review_image(
    image: str,
    *,
    expected_source_digest: str,
    expected_producer_version: str = PRODUCER_VERSION,
    metadata_reader: Callable[[str], dict] | None = None,
    digest_resolver: Callable[[str], str] | None = None,
) -> ReviewImageInfo:
    read_meta = metadata_reader or _read_image_metadata
    resolve_digest = digest_resolver or _resolve_image_digest
    metadata = read_meta(image)
    declared_digest = str(metadata.get("source_digest") or "")
    declared_producer = str(metadata.get("producer") or "")
    if metadata.get("schema") != REVIEW_METADATA_SCHEMA:
        raise ReviewImageError("review_image_schema_mismatch", detail=str(metadata.get("schema")))
    if declared_digest != expected_source_digest:
        raise ReviewImageError(
            "review_image_source_stale",
            detail=f"expected={expected_source_digest} actual={declared_digest}",
        )
    if declared_producer != expected_producer_version:
        raise ReviewImageError(
            "review_image_producer_mismatch",
            detail=f"expected={expected_producer_version} actual={declared_producer}",
        )
    image_digest = resolve_digest(image)
    logger.info(
        "review_image_verified",
        extra={
            "action": "review_image_verified",
            "status": "verified",
            "detail": {
                "image": image,
                "source_digest": expected_source_digest,
                "image_digest": image_digest,
                "producer": expected_producer_version,
            },
        },
    )
    return ReviewImageInfo(
        tag=image,
        source_digest=expected_source_digest,
        image_digest=image_digest,
        producer_version=expected_producer_version,
    )


def build_review_image(
    *,
    repo_root: Path | None = None,
    tag: str = REVIEW_IMAGE_TAG,
    force: bool = False,
) -> ReviewImageInfo:
    root = (repo_root or find_repo_root()).resolve()
    source_digest = compute_agent_source_digest(root)
    if not force:
        try:
            return verify_review_image(tag, expected_source_digest=source_digest)
        except ReviewImageError as exc:
            if exc.error_code not in {
                "review_image_missing",
                "review_image_source_stale",
                "review_image_metadata_missing",
            }:
                raise
    if not _docker_available():
        raise ReviewImageError("review_docker_unavailable")
    dockerfile = root / "backend" / "src" / "bridle" / "agent" / "container" / "agent.Dockerfile"
    cmd = [
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
        str(root),
    ]
    logger.info(
        "review_image_build_started",
        extra={
            "action": "review_image_build_started",
            "status": "started",
            "detail": {"tag": tag, "source_digest": source_digest},
        },
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise ReviewImageError(
            "review_image_build_failed",
            detail=(proc.stderr or proc.stdout)[-2000:],
        )
    return verify_review_image(tag, expected_source_digest=source_digest)


def ensure_review_image(
    *,
    repo_root: Path | None = None,
    tag: str | None = None,
    force_build: bool | None = None,
) -> ReviewImageInfo:
    resolved_tag = tag or os.environ.get("BRIDLE_AGENT_IMAGE", REVIEW_IMAGE_TAG)
    if force_build is None:
        force_build = os.environ.get("BRIDLE_FORCE_REVIEW_IMAGE_BUILD") == "1"
    return build_review_image(repo_root=repo_root, tag=resolved_tag, force=force_build)
