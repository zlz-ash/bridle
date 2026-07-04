"""mtime + sha256 cache for constraint / rules files under workspace."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("bridle.constraint_cache")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_skeleton(path: Path, *, max_lines: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    head = lines[:max_lines]
    if len(lines) > max_lines:
        head.append("...")
    return "\n".join(head)


class ConstraintMetadataCache:
    """Cache constraint/rule file fingerprints under workspace/.bridle/."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace = workspace_root.resolve()
        self._cache_dir = self._workspace / ".bridle" / "constraint-cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self._cache_dir / "metadata.json"

    def _ensure_within_workspace(self, path: Path) -> Path:
        resolved = path.resolve()
        workspace = self._workspace
        if resolved == workspace or workspace in resolved.parents:
            return resolved
        raise ValueError(f"path outside workspace: {resolved}")

    def _load_store(self) -> dict[str, Any]:
        if not self.cache_file.exists():
            return {}
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("constraint_cache_corrupt: %s", exc)
            return {}

    def _save_store(self, store: dict[str, Any]) -> None:
        self.cache_file.write_text(
            json.dumps(store, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get_or_scan(self, path: Path) -> dict[str, Any]:
        resolved = self._ensure_within_workspace(path)
        if not resolved.is_file():
            raise FileNotFoundError(resolved)

        stat = resolved.stat()
        mtime = stat.st_mtime
        size = stat.st_size
        key = str(resolved)
        store = self._load_store()
        cached = store.get(key)
        if (
            isinstance(cached, dict)
            and cached.get("mtime") == mtime
            and cached.get("size") == size
            and cached.get("sha256")
        ):
            out = dict(cached)
            out["cache_hit"] = True
            return out

        sha256 = _file_sha256(resolved)
        if (
            isinstance(cached, dict)
            and cached.get("mtime") == mtime
            and cached.get("size") == size
            and cached.get("sha256") == sha256
        ):
            out = dict(cached)
            out["cache_hit"] = True
            return out

        entry: dict[str, Any] = {
            "path": key,
            "mtime": mtime,
            "size": size,
            "sha256": sha256,
            "metadata_skeleton": _metadata_skeleton(resolved),
            "last_scanned_at": _utc_now_iso(),
            "cache_hit": False,
        }
        store[key] = entry
        self._save_store(store)
        logger.info("constraint_cache_scan path=%s sha256=%s", key, sha256[:12])
        return entry
