"""Module boundary fingerprint for container reuse."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_boundary_fingerprint(
    *,
    module_id: str,
    implementation_entities: list[dict[str, Any]],
    test_entities: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    readonly_files: list[str],
    test_dir: str | None = None,
) -> str:
    """Hash module boundary identity; file content hashes must not alter fingerprint."""
    payload = {
        "module_id": module_id,
        "implementation_entities": sorted(
            [(item["entity_id"], item["path"]) for item in implementation_entities],
            key=lambda pair: pair[0],
        ),
        "test_entities": sorted(
            [(item["entity_id"], item["path"]) for item in test_entities],
            key=lambda pair: pair[0],
        ),
        "interfaces": sorted(
            [
                (
                    item.get("interface_id", ""),
                    item.get("from_module", ""),
                    item.get("to_module", ""),
                    item.get("mock_hash", item.get("entity_version", "")),
                )
                for item in interfaces
            ],
            key=lambda row: row[0],
        ),
        "readonly_files": sorted(set(readonly_files)),
        "test_dir": test_dir or "",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
