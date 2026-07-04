"""Unit tests for Docker integration evidence writer and validator."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridle.agent.container.tests import docker_evidence as de

SOURCE = "sha256:" + "a" * 64
IMAGE = "sha256:" + "b" * 64
GITHUB_SHA = "abc123def456"


@pytest.fixture(autouse=True)
def _reset_evidence_state(monkeypatch: pytest.MonkeyPatch) -> None:
    de.reset_evidence_state_for_tests()
    monkeypatch.setenv("BRIDLE_RUN_DOCKER_TESTS", "1")
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(de, "docker_gate_enabled", lambda: True)


@pytest.fixture
def evidence_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "evidence"
    monkeypatch.setenv("BRIDLE_DOCKER_EVIDENCE_DIR", str(root))
    monkeypatch.setenv("BRIDLE_REVIEW_SOURCE_DIGEST", SOURCE)
    return root


def _node_id(test_key: str) -> str:
    function_name = de.CRITICAL_TEST_SPEC[test_key]
    return (
        "src/bridle/agent/container/tests/test_docker_integration.py::"
        f"{de.CRITICAL_TEST_CLASS}::{function_name}"
    )


SOURCE = "sha256:" + "a" * 64
IMAGE = "sha256:" + "b" * 64
GITHUB_SHA = "abc123def456"
EXTERNAL_TARGET = "/tmp/bridle-outside-secret"


def _link_results() -> list[dict]:
    return [
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
    ]


def _chmod_results() -> list[dict]:
    return [
        {"path": "/workspace/project", "uid": 1000, "rc": 0, "after_mode": 0},
        {"path": "/workspace/output", "uid": 1000, "rc": 1},
        {"path": "/workspace/diagnostics", "uid": 1000, "rc": 1},
    ]


def _link_primary(**overrides: object) -> dict:
    sentinel = {
        "schema": "bridle.external_sentinel/v1",
        "canonical_path": EXTERNAL_TARGET,
        "device": 42,
        "inode": 1001,
        "file_type": "file",
        "mode": 33188,
        "content_digest": "sha256:" + "c" * 64,
    }
    primary = {
        "attack_uid": 1000,
        "attack_results": _link_results(),
        "entry_command": "python -m pytest tests/test_link_attack.py -q -s --capture=no",
        "container_id": "sha256:container",
        "it_run_id": "run-owner",
        "module_id": "docker-link-run-owner",
        "first_run_id": "run-a",
        "attack_run_id": "run-attack",
        "second_run_id": "run-b",
        "container_reused": True,
        "symlinks_removed": True,
        "outside_secret_intact": True,
        "sentinel_before": sentinel,
        "sentinel_after": dict(sentinel),
    }
    primary.update(overrides)
    return primary


def _chmod_primary(**overrides: object) -> dict:
    primary = {
        "attack_uid": 1000,
        "chmod_results": _chmod_results(),
        "entry_command": "python -m pytest tests/test_chmod_poison.py -q -s --capture=no",
        "container_id": "sha256:container",
        "it_run_id": "run-owner",
        "module_id": "docker-perm-run-owner",
        "first_run_id": "run-poison",
        "second_run_id": "run-recover",
        "container_reused": True,
        "trusted_modes": {"project": 493, "output": 493, "diagnostics": 493},
        "recovered_modes": {"project": 493, "output": 493, "diagnostics": 493},
    }
    primary.update(overrides)
    return primary


def _passed_entry(*, test_key: str = "link_attack", session_id: str = "sess-1") -> dict:
    primary = _link_primary() if test_key == "link_attack" else _chmod_primary()
    return {
        "schema": de.DOCKER_EVIDENCE_ENTRY_SCHEMA,
        "version": de.DOCKER_EVIDENCE_VERSION,
        "producer": de.PRODUCER_VERSION,
        "complete": True,
        "status": de.EVIDENCE_STATUS_PASSED,
        "session_id": session_id,
        "test_key": test_key,
        "test_node_id": _node_id(test_key),
        "github_sha": GITHUB_SHA,
        "source_digest": SOURCE,
        "image_digest": IMAGE,
        "recorded_at": "2026-07-02T00:00:00+00:00",
        "pytest_outcome": "passed",
        "primary": primary,
        "teardown": {
            "owner_run_id": primary["it_run_id"],
            "remaining_container_count": 0,
            "remaining_image_count": 0,
            "remaining_image_registry_count": 0,
            "remaining_tag_registry_count": 0,
            "query_failures": [],
            "zero_leftover": True,
        },
    }


def _passed_summary(*, session_id: str = "sess-1") -> dict:
    entries = [
        _passed_entry(test_key="link_attack", session_id=session_id),
        _passed_entry(test_key="chmod_poison", session_id=session_id),
    ]
    return {
        "schema": de.DOCKER_EVIDENCE_SUMMARY_SCHEMA,
        "version": de.DOCKER_EVIDENCE_VERSION,
        "producer": de.PRODUCER_VERSION,
        "complete": True,
        "status": de.EVIDENCE_STATUS_PASSED,
        "session_id": session_id,
        "github_sha": GITHUB_SHA,
        "source_digest": SOURCE,
        "recorded_at": "2026-07-02T00:00:00+00:00",
        "pytest_exitstatus": 0,
        "critical_test_keys": sorted(de.CRITICAL_TEST_KEYS),
        "entry_digests": {entry["test_key"]: de.canonical_entry_digest(entry) for entry in entries},
        "entries": entries,
    }


def _write_valid_directory(root: Path, *, session_id: str = "sess-1") -> None:
    root.mkdir(parents=True, exist_ok=True)
    summary = _passed_summary(session_id=session_id)
    for test_key in de.CRITICAL_TEST_KEYS:
        entry = next(item for item in summary["entries"] if item["test_key"] == test_key)
        (root / f"{test_key}.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    (root / "session-summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")


class FakeTeardown:
    owner_run_id = "run-owner"
    remaining_container_count = 0
    remaining_image_count = 0
    remaining_image_registry_count = 0
    remaining_tag_registry_count = 0
    query_failures: list[str] = []


def test_begin_session_invalidates_old_files(evidence_root: Path) -> None:
    stale = evidence_root / "link_attack.json"
    evidence_root.mkdir(parents=True, exist_ok=True)
    stale.write_text('{"status":"passed"}', encoding="utf-8")
    session_id = de.begin_docker_evidence_session()
    assert session_id
    assert not stale.exists()
    assert list(evidence_root.glob("link_attack.tainted.*.json"))


def test_publish_passed_evidence_round_trip(evidence_root: Path) -> None:
    de.begin_docker_evidence_session()
    de.publish_passed_evidence(
        "link_attack",
        test_node_id=_node_id("link_attack"),
        image_digest=IMAGE,
        primary=_link_primary(note="café"),
        teardown_result=FakeTeardown(),
    )
    payload = json.loads((evidence_root / "link_attack.json").read_text(encoding="utf-8"))
    de.validate_evidence_entry(payload, recompute_success=True)
    assert payload["primary"]["note"] == "café"


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("attack_uid", None, "docker_evidence_primary_invalid"),
        ("attack_uid", 0, "docker_evidence_primary_invalid"),
        ("container_reused", False, "docker_evidence_primary_invalid"),
        ("symlinks_removed", False, "docker_evidence_primary_invalid"),
        ("outside_secret_intact", False, "docker_evidence_primary_invalid"),
        ("attack_results", [], "docker_evidence_primary_invalid"),
    ],
)
def test_validate_rejects_invalid_link_primary(field: str, value: object, error_code: str) -> None:
    entry = _passed_entry(test_key="link_attack")
    entry["primary"][field] = value
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_evidence_entry(entry, recompute_success=True)
    assert exc.value.error_code == error_code


def test_validate_rejects_none_primary_values_for_chmod() -> None:
    entry = _passed_entry(test_key="chmod_poison")
    entry["primary"]["trusted_modes"] = {"project": None, "output": 493, "diagnostics": 493}
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_evidence_entry(entry, recompute_success=True)
    assert exc.value.error_code == "docker_evidence_primary_invalid"


def test_validate_rejects_wrong_test_node_id() -> None:
    entry = _passed_entry()
    entry["test_node_id"] = "node::link_attack"
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_evidence_entry(entry, recompute_success=True)
    assert exc.value.error_code == "docker_evidence_test_node_mismatch"


def test_validate_rejects_teardown_run_mismatch() -> None:
    entry = _passed_entry()
    entry["teardown"]["owner_run_id"] = "foreign-run"
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_evidence_entry(entry, recompute_success=True)
    assert exc.value.error_code == "docker_evidence_teardown_run_mismatch"


@pytest.mark.parametrize(
    "target",
    [
        "relative/outside-secret.txt",
        "/tmp/../workspace/project/outside-secret.txt",
        "//workspace/project/outside-secret.txt",
        "/tmp/./outside-secret.txt",
    ],
)
def test_validate_rejects_noncanonical_or_internal_link_target(target: str) -> None:
    entry = _passed_entry(test_key="link_attack")
    for result in entry["primary"]["attack_results"]:
        result["target"] = target

    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_evidence_entry(entry, recompute_success=True)

    assert exc.value.error_code == "docker_evidence_primary_invalid"


def test_validate_rejects_mixed_session_entry_and_summary(evidence_root: Path) -> None:
    _write_valid_directory(evidence_root, session_id="sess-a")
    summary = json.loads((evidence_root / "session-summary.json").read_text(encoding="utf-8"))
    summary["session_id"] = "sess-b"
    (evidence_root / "session-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_evidence_directory_for_gate(
            evidence_root,
            expected_source_digest=SOURCE,
            expected_image_digest=IMAGE,
            expected_github_sha=GITHUB_SHA,
        )
    assert exc.value.error_code == "docker_evidence_session_mismatch"


def test_validate_rejects_replaced_entry_file(evidence_root: Path) -> None:
    _write_valid_directory(evidence_root)
    tampered = _passed_entry(test_key="link_attack")
    tampered["primary"]["attack_uid"] = 9999
    (evidence_root / "link_attack.json").write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_evidence_directory_for_gate(
            evidence_root,
            expected_source_digest=SOURCE,
            expected_image_digest=IMAGE,
            expected_github_sha=GITHUB_SHA,
        )
    assert exc.value.error_code in {
        "docker_evidence_entry_digest_mismatch",
        "docker_evidence_primary_invalid",
    }


def test_validate_rejects_false_zero_leftover_with_residual_count() -> None:
    entry = _passed_entry()
    entry["teardown"]["remaining_container_count"] = 1
    entry["teardown"]["zero_leftover"] = True
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_evidence_entry(entry, recompute_success=True)
    assert exc.value.error_code == "docker_evidence_teardown_not_clean"


def test_validate_rejects_summary_passed_with_failed_entry() -> None:
    summary = _passed_summary()
    summary["entries"][0]["status"] = de.EVIDENCE_STATUS_FAILED
    summary["entry_digests"]["link_attack"] = de.canonical_entry_digest(summary["entries"][0])
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_session_summary(
            summary,
            expected_source_digest=SOURCE,
            expected_image_digest=IMAGE,
            expected_github_sha=GITHUB_SHA,
        )
    assert exc.value.error_code == "docker_evidence_entry_not_passed"


def test_validate_rejects_non_zero_pytest_exitstatus() -> None:
    summary = _passed_summary()
    summary["pytest_exitstatus"] = 1
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_session_summary(
            summary,
            expected_source_digest=SOURCE,
            expected_image_digest=IMAGE,
            expected_github_sha=GITHUB_SHA,
        )
    assert exc.value.error_code == "docker_evidence_pytest_exit_not_zero"


def test_validate_rejects_extra_entry_digest_key() -> None:
    summary = _passed_summary()
    summary["entry_digests"]["extra"] = "deadbeef"
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_session_summary(
            summary,
            expected_source_digest=SOURCE,
            expected_image_digest=IMAGE,
            expected_github_sha=GITHUB_SHA,
        )
    assert exc.value.error_code == "docker_evidence_summary_keys_invalid"


def test_validate_directory_success(evidence_root: Path) -> None:
    _write_valid_directory(evidence_root)
    de.validate_evidence_directory_for_gate(
        evidence_root,
        expected_source_digest=SOURCE,
        expected_image_digest=IMAGE,
        expected_github_sha=GITHUB_SHA,
    )


@pytest.mark.parametrize(
    ("test_key", "mutator", "detail"),
    [
        (
            "link_attack",
            lambda primary: primary.update({"entry_command": "echo forged"}),
            "entry_command",
        ),
        (
            "link_attack",
            lambda primary: primary["attack_results"][0].update({"uid": 0}),
            "uid",
        ),
        (
            "link_attack",
            lambda primary: primary["attack_results"][0].update({"target": "/workspace/project/x"}),
            "internal_target",
        ),
        (
            "link_attack",
            lambda primary: primary["attack_results"][1].update({"target": "/tmp/other-target"}),
            "target_mismatch",
        ),
        (
            "link_attack",
            lambda primary: primary["attack_results"][0].update({"symlink_rc": False}),
            "rc",
        ),
        (
            "chmod_poison",
            lambda primary: primary.update({"entry_command": "echo forged"}),
            "entry_command",
        ),
        (
            "chmod_poison",
            lambda primary: primary["chmod_results"].append(
                {"path": "/workspace/project", "uid": 1000, "rc": 0, "after_mode": 0}
            ),
            "dup",
        ),
        (
            "chmod_poison",
            lambda primary: primary["chmod_results"][0].update({"rc": False}),
            "rc",
        ),
        (
            "chmod_poison",
            lambda primary: primary["trusted_modes"].update({"project": -1}),
            "range",
        ),
        (
            "chmod_poison",
            lambda primary: primary["trusted_modes"].update({"extra": 493}),
            "keys",
        ),
    ],
)
def test_validate_rejects_forged_primary_matrix(
    test_key: str,
    mutator: object,
    detail: str,
) -> None:
    entry = _passed_entry(test_key=test_key)
    mutator(entry["primary"])
    with pytest.raises(de.DockerEvidenceError) as exc:
        de.validate_evidence_entry(entry, recompute_success=True)
    assert exc.value.error_code == "docker_evidence_primary_invalid"
