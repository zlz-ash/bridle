"""Tests for external sentinel identity registration and verification."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[6]
SPEC = importlib.util.spec_from_file_location(
    "bridle_sentinel_registry",
    REPO_ROOT / "scripts/ci/sentinel_registry.py",
)
assert SPEC is not None and SPEC.loader is not None
sentinel_registry = importlib.util.module_from_spec(SPEC)
sys.modules["bridle_sentinel_registry"] = sentinel_registry
SPEC.loader.exec_module(sentinel_registry)


def test_register_and_verify_sentinel(tmp_path: Path) -> None:
    target = tmp_path / "outside-secret.txt"
    target.write_text("secret\n", encoding="utf-8")
    record = sentinel_registry.register_external_sentinel(target)
    sentinel_registry.verify_external_sentinel(target, record)


def test_substitute_inode_fails_verification(tmp_path: Path) -> None:
    target = tmp_path / "outside-secret.txt"
    target.write_text("secret\n", encoding="utf-8")
    record = sentinel_registry.register_external_sentinel(target)
    target.unlink()
    replacement = tmp_path / "outside-secret.txt"
    replacement.write_text("secret\n", encoding="utf-8")
    with pytest.raises(sentinel_registry.SentinelRegistryError) as exc:
        sentinel_registry.verify_external_sentinel(replacement, record)
    assert exc.value.error_code == "sentinel_identity_mismatch"


def test_same_name_rebuild_with_different_content_fails(tmp_path: Path) -> None:
    target = tmp_path / "outside-secret.txt"
    target.write_text("secret\n", encoding="utf-8")
    record = sentinel_registry.register_external_sentinel(target)
    target.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(sentinel_registry.SentinelRegistryError) as exc:
        sentinel_registry.verify_external_sentinel(target, record)
    assert exc.value.error_code == "sentinel_content_mismatch"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink sentinel semantics")
def test_external_symlink_sentinel_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "real-secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "outside-link.txt"
    link.symlink_to(outside)
    with pytest.raises(sentinel_registry.SentinelRegistryError) as exc:
        sentinel_registry.register_external_sentinel(link)
    assert exc.value.error_code == "sentinel_must_not_be_symlink"
