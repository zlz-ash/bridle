"""Overlay and path attack tests for the protected Docker harness."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "trusted_harness.py"
SPEC = importlib.util.spec_from_file_location("bridle_trusted_harness", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
trusted_harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trusted_harness)


def _write_trusted_file(root: Path, relative: str, content: str = "trusted\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.parametrize(
    "manifest_entry",
    [
        "../outside.py",
        "backend/../../outside.py",
        "/absolute.py",
    ],
)
def test_overlay_manifest_rejects_escape_paths(manifest_entry: str) -> None:
    with pytest.raises(trusted_harness.TrustedHarnessError) as exc:
        trusted_harness.parse_manifest_lines([manifest_entry])
    assert exc.value.error_code == "trusted_harness_manifest_path_invalid"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink overlay semantics")
def test_overlay_rejects_trusted_root_symlink(tmp_path: Path) -> None:
    relative = "backend/src/bridle/agent/container/tests/conftest.py"
    trusted_root = tmp_path / "trusted"
    candidate_root = tmp_path / "candidate"
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    _write_trusted_file(trusted_root, relative)
    linked_root = tmp_path / "linked-trusted"
    linked_root.symlink_to(trusted_root)
    with pytest.raises(trusted_harness.TrustedHarnessError) as exc:
        trusted_harness.overlay_files(candidate_root, linked_root, [relative])
    assert exc.value.error_code == "trusted_harness_trusted_link_rejected"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink overlay semantics")
def test_overlay_rejects_source_symlink_in_candidate_digest(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    _write_trusted_file(root, "backend/pyproject.toml", "[project]\nname='demo'\n")
    _write_trusted_file(
        root,
        "backend/src/bridle/agent/container/agent.Dockerfile",
        "FROM scratch\n",
    )
    linked_source = root / "backend/src/bridle/example.py"
    linked_source.parent.mkdir(parents=True, exist_ok=True)
    linked_source.symlink_to(outside)
    with pytest.raises(trusted_harness.TrustedHarnessError) as exc:
        trusted_harness.compute_candidate_source_digest(root)
    assert exc.value.error_code == "trusted_harness_source_link_rejected"


def test_post_overlay_tamper_detected(tmp_path: Path) -> None:
    relative = "backend/src/bridle/agent/container/tests/conftest.py"
    trusted_root = tmp_path / "trusted"
    candidate_root = tmp_path / "candidate"
    _write_trusted_file(trusted_root, relative)
    snapshot = trusted_harness.overlay_files(candidate_root, trusted_root, [relative])
    (candidate_root / relative).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(trusted_harness.TrustedHarnessError) as exc:
        trusted_harness.verify_overlay_snapshot(candidate_root, snapshot)
    assert exc.value.error_code == "trusted_harness_overlay_mutated"


def test_protected_dockerfile_must_come_from_trusted_root(tmp_path: Path) -> None:
    trusted_root = tmp_path / "trusted"
    path = trusted_harness.protected_dockerfile_path(REPO_ROOT)
    assert path.is_file()
    with pytest.raises(trusted_harness.TrustedHarnessError):
        trusted_harness.protected_dockerfile_path(trusted_root)
