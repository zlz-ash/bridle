"""Tests for the protected Docker harness controller."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[6]
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
    "entry",
    [
        "../outside.py",
        "/absolute.py",
        "backend/../../outside.py",
        "backend\\..\\outside.py",
        "",
    ],
)
def test_manifest_rejects_non_relative_or_escaping_paths(entry: str) -> None:
    with pytest.raises(trusted_harness.TrustedHarnessError) as exc:
        trusted_harness.parse_manifest_lines([entry])
    assert exc.value.error_code == "trusted_harness_manifest_path_invalid"


def test_overlay_rejects_symlink_destination_without_touching_target(tmp_path: Path) -> None:
    relative = "backend/src/bridle/agent/container/tests/conftest.py"
    trusted_root = tmp_path / "trusted"
    candidate_root = tmp_path / "candidate"
    sentinel = tmp_path / "outside-sentinel.py"
    sentinel.write_text("outside\n", encoding="utf-8")
    _write_trusted_file(trusted_root, relative)
    destination = candidate_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.symlink_to(sentinel)
    except OSError as exc:
        pytest.skip(f"real symlink unavailable on this platform: {exc}")

    with pytest.raises(trusted_harness.TrustedHarnessError) as caught:
        trusted_harness.overlay_files(candidate_root, trusted_root, [relative])

    assert caught.value.error_code == "trusted_harness_candidate_link_rejected"
    assert sentinel.read_text(encoding="utf-8") == "outside\n"


def test_overlay_snapshot_detects_post_copy_mutation(tmp_path: Path) -> None:
    relative = "backend/src/bridle/agent/container/tests/conftest.py"
    trusted_root = tmp_path / "trusted"
    candidate_root = tmp_path / "candidate"
    _write_trusted_file(trusted_root, relative)

    snapshot = trusted_harness.overlay_files(candidate_root, trusted_root, [relative])
    trusted_harness.verify_overlay_snapshot(candidate_root, snapshot)

    (candidate_root / relative).write_text("mutated\n", encoding="utf-8")
    with pytest.raises(trusted_harness.TrustedHarnessError) as exc:
        trusted_harness.verify_overlay_snapshot(candidate_root, snapshot)
    assert exc.value.error_code == "trusted_harness_overlay_mutated"


def test_source_digest_is_normalized_and_ignores_generated_files(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    _write_trusted_file(root, "backend/pyproject.toml", "[project]\nname='demo'\n")
    _write_trusted_file(
        root,
        "backend/src/bridle/agent/container/agent.Dockerfile",
        "FROM scratch\n",
    )
    _write_trusted_file(root, "backend/src/bridle/example.py", "VALUE = 1\n")

    first = trusted_harness.compute_candidate_source_digest(root)
    _write_trusted_file(root, "backend/src/bridle/__pycache__/example.pyc", "generated")
    _write_trusted_file(root, "backend/src/bridle/.pytest_cache/state", "generated")
    second = trusted_harness.compute_candidate_source_digest(root)

    assert first == second
    assert first.startswith("sha256:")


def test_source_digest_rejects_symlinked_source_file(tmp_path: Path) -> None:
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
    try:
        linked_source.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"real symlink unavailable on this platform: {exc}")

    with pytest.raises(trusted_harness.TrustedHarnessError) as caught:
        trusted_harness.compute_candidate_source_digest(root)
    assert caught.value.error_code == "trusted_harness_source_link_rejected"


def test_manifest_audit_rejects_missing_direct_test_helper(tmp_path: Path) -> None:
    trusted_root = tmp_path / "trusted"
    test_path = "backend/src/bridle/agent/container/tests/test_docker_integration.py"
    _write_trusted_file(
        trusted_root,
        test_path,
        "from bridle.agent.container.tests.docker_test_resources import finalize_run_teardown\n",
    )
    _write_trusted_file(trusted_root, "backend/src/bridle/agent/container/tests/docker_test_resources.py")

    with pytest.raises(trusted_harness.TrustedHarnessError) as caught:
        trusted_harness.audit_manifest_import_closure(trusted_root, [test_path])
    assert caught.value.error_code == "trusted_harness_dependency_missing"


def test_manifest_contains_every_direct_trusted_test_helper() -> None:
    entries = trusted_harness.load_manifest(REPO_ROOT, REPO_ROOT / ".github/trusted-docker-harness.txt")
    assert "backend/src/bridle/agent/container/tests/docker_test_resources.py" in entries
    trusted_harness.audit_manifest_import_closure(REPO_ROOT, entries)
