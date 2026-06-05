"""Tests for constraint/rule file metadata cache."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridle.engine.constraint_metadata_cache import ConstraintMetadataCache


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestConstraintMetadataCache:
    def test_first_scan_writes_cache(self, test_workspace: Path) -> None:
        rules = test_workspace / ".cursor" / "rules" / "demo.mdc"
        _write(rules, "# rule\nalways apply\n")
        cache = ConstraintMetadataCache(test_workspace)
        meta = cache.get_or_scan(rules)
        assert meta["path"] == str(rules.resolve())
        assert meta["sha256"]
        assert meta["metadata_skeleton"]
        assert cache.cache_file.exists()

    def test_second_scan_hits_cache(self, test_workspace: Path) -> None:
        rules = test_workspace / "AGENTS.md"
        _write(rules, "agent rules\n")
        cache = ConstraintMetadataCache(test_workspace)
        first = cache.get_or_scan(rules)
        second = cache.get_or_scan(rules)
        assert first["sha256"] == second["sha256"]
        assert second.get("cache_hit") is True

    def test_mtime_change_triggers_rescan(self, test_workspace: Path) -> None:
        rules = test_workspace / "plan.md"
        _write(rules, "v1\n")
        cache = ConstraintMetadataCache(test_workspace)
        first = cache.get_or_scan(rules)
        _write(rules, "v2\n")
        second = cache.get_or_scan(rules)
        assert second["sha256"] != first["sha256"]
        assert second.get("cache_hit") is not True

    def test_mtime_hit_skips_sha256(self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import hashlib

        rules = test_workspace / "AGENTS.md"
        _write(rules, "stable\n")
        cache = ConstraintMetadataCache(test_workspace)
        calls: list[Path] = []

        def counting_sha256(path: Path) -> str:
            calls.append(path)
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            return digest.hexdigest()

        from bridle.engine import constraint_metadata_cache as cmc

        monkeypatch.setattr(cmc, "_file_sha256", counting_sha256)
        cache.get_or_scan(rules)
        cache.get_or_scan(rules)
        assert len(calls) == 1

    def test_corrupt_cache_falls_back(self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        rules = test_workspace / "constraints.txt"
        _write(rules, "ok\n")
        cache = ConstraintMetadataCache(test_workspace)
        cache.get_or_scan(rules)
        cache.cache_file.write_text("{not json", encoding="utf-8")
        meta = cache.get_or_scan(rules)
        assert meta["sha256"]
        assert meta["path"] == str(rules.resolve())
