"""Unit tests for Docker CI gate hooks and validator CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridle.agent.container.tests import docker_evidence as de
from bridle.agent.container.tests import docker_gate as dg
from bridle.agent.container.tests import docker_gate_paths as dgp

SOURCE = "sha256:" + "a" * 64
IMAGE = "sha256:" + "b" * 64
GITHUB_SHA = "abc123def456"
EXTERNAL_TARGET = "/tmp/bridle-outside-secret"


@pytest.fixture
def trusted_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "candidate"
    canonical = (
        root
        / "backend"
        / "src"
        / "bridle"
        / "agent"
        / "container"
        / "tests"
        / "test_docker_integration.py"
    )
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("# integration tests\n", encoding="utf-8")
    monkeypatch.setenv("BRIDLE_TRUSTED_CHECKOUT_ROOT", str(root))
    return root, canonical


class _FakeItem:
    def __init__(self, nodeid: str, *, fspath: str | Path | None = None) -> None:
        self.nodeid = nodeid
        self.fspath = Path(fspath) if fspath is not None else Path(nodeid.split("::", 1)[0])


def _node_id(test_key: str, canonical: Path) -> str:
    function_name = de.CRITICAL_TEST_SPEC[test_key]
    return f"{canonical}::{de.CRITICAL_TEST_CLASS}::{function_name}"


def _link_primary() -> dict:
    sentinel = {
        "schema": "bridle.external_sentinel/v1",
        "canonical_path": EXTERNAL_TARGET,
        "device": 42,
        "inode": 1001,
        "file_type": "file",
        "mode": 33188,
        "content_digest": "sha256:" + "c" * 64,
    }
    return {
        "attack_uid": 1000,
        "attack_results": [
            {
                "name": "attack.txt",
                "link_path": "/workspace/project/attack.txt",
                "target": EXTERNAL_TARGET,
                "uid": 1000,
                "symlink_rc": 0,
                "lstat_is_symlink": True,
            },
            {
                "name": "escape.txt",
                "link_path": "/workspace/output/escape.txt",
                "target": EXTERNAL_TARGET,
                "uid": 1000,
                "symlink_rc": 0,
                "lstat_is_symlink": True,
            },
        ],
        "entry_command": de.APPROVED_ENTRY_COMMANDS["link_attack"],
        "container_id": "cid",
        "it_run_id": "run-owner",
        "module_id": "mod",
        "first_run_id": "run-a",
        "attack_run_id": "run-attack",
        "second_run_id": "run-b",
        "container_reused": True,
        "symlinks_removed": True,
        "outside_secret_intact": True,
        "sentinel_before": sentinel,
        "sentinel_after": dict(sentinel),
    }


def _chmod_primary() -> dict:
    return {
        "attack_uid": 1000,
        "chmod_results": [
            {"path": "/workspace/project", "uid": 1000, "rc": 0, "after_mode": 0},
            {"path": "/workspace/output", "uid": 1000, "rc": 1},
            {"path": "/workspace/diagnostics", "uid": 1000, "rc": 1},
        ],
        "entry_command": de.APPROVED_ENTRY_COMMANDS["chmod_poison"],
        "container_id": "cid",
        "it_run_id": "run-owner",
        "module_id": "mod",
        "first_run_id": "run-poison",
        "second_run_id": "run-recover",
        "container_reused": True,
        "trusted_modes": {"project": 493, "output": 493, "diagnostics": 493},
        "recovered_modes": {"project": 493, "output": 493, "diagnostics": 493},
    }


def _build_valid_evidence(root: Path, *, canonical: Path) -> None:
    entries = []
    for test_key in de.CRITICAL_TEST_KEYS:
        primary = _link_primary() if test_key == "link_attack" else _chmod_primary()
        entries.append(
            {
                "schema": de.DOCKER_EVIDENCE_ENTRY_SCHEMA,
                "version": de.DOCKER_EVIDENCE_VERSION,
                "producer": de.PRODUCER_VERSION,
                "complete": True,
                "status": de.EVIDENCE_STATUS_PASSED,
                "session_id": "sess",
                "test_key": test_key,
                "test_node_id": _node_id(test_key, canonical),
                "github_sha": GITHUB_SHA,
                "source_digest": SOURCE,
                "image_digest": IMAGE,
                "recorded_at": "2026-07-02T00:00:00+00:00",
                "pytest_outcome": "passed",
                "primary": primary,
                "teardown": {
                    "owner_run_id": "run-owner",
                    "remaining_container_count": 0,
                    "remaining_image_count": 0,
                    "remaining_image_registry_count": 0,
                    "remaining_tag_registry_count": 0,
                    "query_failures": [],
                    "zero_leftover": True,
                },
            }
        )
    summary = {
        "schema": de.DOCKER_EVIDENCE_SUMMARY_SCHEMA,
        "version": de.DOCKER_EVIDENCE_VERSION,
        "producer": de.PRODUCER_VERSION,
        "complete": True,
        "status": de.EVIDENCE_STATUS_PASSED,
        "session_id": "sess",
        "github_sha": GITHUB_SHA,
        "source_digest": SOURCE,
        "recorded_at": "2026-07-02T00:00:00+00:00",
        "pytest_exitstatus": 0,
        "critical_test_keys": sorted(de.CRITICAL_TEST_KEYS),
        "entry_digests": {entry["test_key"]: de.canonical_entry_digest(entry) for entry in entries},
        "entries": entries,
    }
    root.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        (root / f"{entry['test_key']}.json").write_text(json.dumps(entry), encoding="utf-8")
    (root / "session-summary.json").write_text(json.dumps(summary), encoding="utf-8")


def test_assert_critical_tests_collected_success(trusted_layout: tuple[Path, Path]) -> None:
    _, canonical = trusted_layout
    items = [
        _FakeItem(_node_id("link_attack", canonical)),
        _FakeItem(_node_id("chmod_poison", canonical)),
    ]
    dgp.assert_critical_tests_collected(items)


def test_assert_critical_tests_collected_missing(trusted_layout: tuple[Path, Path]) -> None:
    _, canonical = trusted_layout
    items = [_FakeItem(_node_id("link_attack", canonical))]
    with pytest.raises(de.DockerEvidenceError) as exc:
        dgp.assert_critical_tests_collected(items)
    assert exc.value.error_code == "docker_gate_critical_tests_not_collected"


def test_assert_critical_tests_collected_duplicate(trusted_layout: tuple[Path, Path]) -> None:
    _, canonical = trusted_layout
    nodeid = _node_id("link_attack", canonical)
    items = [
        _FakeItem(nodeid),
        _FakeItem(nodeid),
        _FakeItem(_node_id("chmod_poison", canonical)),
    ]
    with pytest.raises(de.DockerEvidenceError) as exc:
        dgp.assert_critical_tests_collected(items)
    assert exc.value.error_code == "docker_gate_critical_tests_duplicate"


def test_decoy_filename_does_not_match_collection(trusted_layout: tuple[Path, Path]) -> None:
    _, canonical = trusted_layout
    decoy = canonical.with_name("test_docker_integration.py_decoy.py")
    decoy.write_text("# decoy\n", encoding="utf-8")
    found = dgp.critical_node_ids_from_items(
        [
            _FakeItem(_node_id("link_attack", canonical), fspath=decoy),
            _FakeItem(_node_id("chmod_poison", canonical)),
        ]
    )
    assert found["link_attack"] == []
    assert len(found["chmod_poison"]) == 1


def test_foreign_root_does_not_match_collection(
    trusted_layout: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    _, canonical = trusted_layout
    foreign = (
        tmp_path
        / "foreign"
        / "backend"
        / "src"
        / "bridle"
        / "agent"
        / "container"
        / "tests"
        / "test_docker_integration.py"
    )
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("# foreign copy\n", encoding="utf-8")
    nodeid = f"{foreign}::{de.CRITICAL_TEST_CLASS}::{de.CRITICAL_TEST_SPEC['link_attack']}"
    found = dgp.critical_node_ids_from_items([_FakeItem(nodeid, fspath=foreign)])
    assert found["link_attack"] == []


def test_different_class_does_not_match_collection(trusted_layout: tuple[Path, Path]) -> None:
    _, canonical = trusted_layout
    nodeid = (
        f"{canonical}::TestOtherClass::test_real_docker_recovers_after_link_attack_in_slot"
    )
    found = dgp.critical_node_ids_from_items([_FakeItem(nodeid, fspath=canonical)])
    assert found["link_attack"] == []


def test_similar_suffix_does_not_match_collection(trusted_layout: tuple[Path, Path]) -> None:
    _, canonical = trusted_layout
    nodeid = (
        f"{canonical}::{de.CRITICAL_TEST_CLASS}::"
        "test_real_docker_recovers_after_link_attack_in_slot_extra"
    )
    found = dgp.critical_node_ids_from_items([_FakeItem(nodeid, fspath=canonical)])
    assert found["link_attack"] == []


def test_validate_evidence_cli_success(
    tmp_path: Path,
    trusted_layout: tuple[Path, Path],
) -> None:
    _, canonical = trusted_layout
    evidence_root = tmp_path / "evidence"
    _build_valid_evidence(evidence_root, canonical=canonical)
    dg.validate_evidence_cli(
        evidence_root,
        expected_source_digest=SOURCE,
        expected_image_digest=IMAGE,
        expected_github_sha=GITHUB_SHA,
    )


def test_validate_evidence_cli_missing_image_digest(
    tmp_path: Path,
    trusted_layout: tuple[Path, Path],
) -> None:
    _, canonical = trusted_layout
    evidence_root = tmp_path / "evidence"
    _build_valid_evidence(evidence_root, canonical=canonical)
    with pytest.raises(de.DockerEvidenceError) as exc:
        dg.validate_evidence_cli(
            evidence_root,
            expected_source_digest=SOURCE,
            expected_image_digest="",
            expected_github_sha=GITHUB_SHA,
        )
    assert exc.value.error_code == "docker_evidence_image_digest_required"


def test_validate_evidence_cli_mixed_entry_images(
    tmp_path: Path,
    trusted_layout: tuple[Path, Path],
) -> None:
    _, canonical = trusted_layout
    evidence_root = tmp_path / "evidence"
    _build_valid_evidence(evidence_root, canonical=canonical)
    chmod_entry = json.loads((evidence_root / "chmod_poison.json").read_text(encoding="utf-8"))
    chmod_entry["image_digest"] = "sha256:" + "c" * 64
    (evidence_root / "chmod_poison.json").write_text(json.dumps(chmod_entry), encoding="utf-8")
    with pytest.raises(de.DockerEvidenceError) as exc:
        dg.validate_evidence_cli(
            evidence_root,
            expected_source_digest=SOURCE,
            expected_image_digest=IMAGE,
            expected_github_sha=GITHUB_SHA,
        )
    assert exc.value.error_code in {
        "docker_evidence_image_digest_mismatch",
        "docker_evidence_entry_digest_mismatch",
    }


def test_main_returns_non_zero_on_failure(tmp_path: Path) -> None:
    evidence_root = tmp_path / "missing"
    evidence_root.mkdir()
    assert (
        dg.main(
            [
                str(evidence_root),
                "--source-digest",
                SOURCE,
                "--image-digest",
                IMAGE,
                "--github-sha",
                GITHUB_SHA,
            ]
        )
        == 1
    )


def test_main_returns_zero_on_success(
    tmp_path: Path,
    trusted_layout: tuple[Path, Path],
) -> None:
    _, canonical = trusted_layout
    evidence_root = tmp_path / "evidence"
    _build_valid_evidence(evidence_root, canonical=canonical)
    assert (
        dg.main(
            [
                str(evidence_root),
                "--source-digest",
                SOURCE,
                "--image-digest",
                IMAGE,
                "--github-sha",
                GITHUB_SHA,
            ]
        )
        == 0
    )


def test_is_critical_docker_item_uses_same_rules(trusted_layout: tuple[Path, Path]) -> None:
    _, canonical = trusted_layout
    decoy = canonical.with_name("test_docker_integration.py_decoy.py")
    decoy.write_text("# decoy\n", encoding="utf-8")
    assert dgp.is_critical_docker_item(_FakeItem(_node_id("link_attack", canonical))) is True
    assert (
        dgp.is_critical_docker_item(
            _FakeItem(_node_id("link_attack", canonical), fspath=decoy)
        )
        is False
    )
