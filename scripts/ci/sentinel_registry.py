#!/usr/bin/env python3
"""Register and verify pre-attack external sentinel filesystem identity."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("bridle.sentinel_registry")
SENTINEL_SCHEMA = "bridle.external_sentinel/v1"


class SentinelRegistryError(RuntimeError):
    def __init__(self, error_code: str, *, detail: str = "") -> None:
        self.error_code = error_code
        self.detail = detail
        super().__init__(detail or error_code)


@dataclass(frozen=True)
class SentinelRecord:
    schema: str
    canonical_path: str
    device: int
    inode: int
    file_type: str
    mode: int
    content_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "canonical_path": self.canonical_path,
            "device": self.device,
            "inode": self.inode,
            "file_type": self.file_type,
            "mode": self.mode,
            "content_digest": self.content_digest,
        }


def _file_type(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def register_external_sentinel(path: Path) -> SentinelRecord:
    target = Path(os.path.abspath(os.fspath(path)))
    if not target.exists():
        raise SentinelRegistryError("sentinel_missing", detail=str(target))
    metadata = os.lstat(target)
    if stat.S_ISLNK(metadata.st_mode):
        raise SentinelRegistryError("sentinel_must_not_be_symlink", detail=str(target))
    if not stat.S_ISREG(metadata.st_mode):
        raise SentinelRegistryError("sentinel_must_be_regular_file", detail=str(target))
    record = SentinelRecord(
        schema=SENTINEL_SCHEMA,
        canonical_path=str(target),
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        file_type=_file_type(metadata.st_mode),
        mode=int(metadata.st_mode),
        content_digest=_sha256_bytes(target.read_bytes()),
    )
    LOGGER.info(
        "sentinel_registered path=%s device=%s inode=%s digest=%s",
        record.canonical_path,
        record.device,
        record.inode,
        record.content_digest,
    )
    return record


def verify_external_sentinel(path: Path, record: SentinelRecord | dict[str, Any]) -> None:
    expected = record if isinstance(record, SentinelRecord) else _record_from_dict(record)
    target = Path(os.path.abspath(os.fspath(path)))
    if str(target) != expected.canonical_path:
        raise SentinelRegistryError(
            "sentinel_path_mismatch",
            detail=f"expected={expected.canonical_path} actual={target}",
        )
    if not target.exists():
        raise SentinelRegistryError("sentinel_missing", detail=str(target))
    metadata = os.lstat(target)
    actual_type = _file_type(metadata.st_mode)
    if actual_type != expected.file_type:
        raise SentinelRegistryError("sentinel_type_mismatch", detail=actual_type)
    if int(metadata.st_dev) != expected.device or int(metadata.st_ino) != expected.inode:
        raise SentinelRegistryError(
            "sentinel_identity_mismatch",
            detail=f"device={metadata.st_dev} inode={metadata.st_ino}",
        )
    if not stat.S_ISREG(metadata.st_mode):
        raise SentinelRegistryError("sentinel_must_be_regular_file", detail=str(target))
    actual_digest = _sha256_bytes(target.read_bytes())
    if actual_digest != expected.content_digest:
        raise SentinelRegistryError("sentinel_content_mismatch", detail=actual_digest)


def _record_from_dict(payload: dict[str, Any]) -> SentinelRecord:
    if payload.get("schema") != SENTINEL_SCHEMA:
        raise SentinelRegistryError("sentinel_schema_mismatch", detail=str(payload.get("schema")))
    for field in ("canonical_path", "file_type", "content_digest"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SentinelRegistryError("sentinel_record_invalid", detail=field)
    for field in ("device", "inode", "mode"):
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise SentinelRegistryError("sentinel_record_invalid", detail=field)
    return SentinelRecord(
        schema=SENTINEL_SCHEMA,
        canonical_path=str(payload["canonical_path"]),
        device=int(payload["device"]),
        inode=int(payload["inode"]),
        file_type=str(payload["file_type"]),
        mode=int(payload["mode"]),
        content_digest=str(payload["content_digest"]),
    )


def write_sentinel_record(path: Path, record: SentinelRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def read_sentinel_record(path: Path) -> SentinelRecord:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SentinelRegistryError("sentinel_record_invalid", detail=str(path))
    return _record_from_dict(payload)
