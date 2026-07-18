"""Regression and contract tests for candidate execution isolation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bridle.agent.container.candidate_contract import (
    CandidateExecutionRequest,
    CandidateExecutionResult,
    compute_patches,
    persist_result,
    snapshot_directory_hashes,
    validate_candidate_request,
)
from bridle.agent.container.candidate_service import CandidateExecutionService


def _formal_file_hashes(root: Path, rel_paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in rel_paths:
        path = root / Path(*rel.split("/"))
        if path.is_file():
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_candidate_request_round_trips_through_existing_workspace_manifest(
    test_workspace: Path,
) -> None:
    source = test_workspace / "src" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    test_file = test_workspace / "tests" / "test_module.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_value():\n    assert True\n", encoding="utf-8")
    setup = CandidateExecutionService(test_workspace).prepare_from_snapshot(
        {
            "module_id": "mod-roundtrip",
            "node_id": "node-roundtrip",
            "implementation_entities": [
                {"entity_id": "entity-module", "path": "src/module.py"}
            ],
            "test_entities": [
                {"entity_id": "entity-test-module", "path": "tests/test_module.py"}
            ],
            "test_commands": ["python -m pytest tests/test_module.py -q"],
            "interfaces": [],
            "test_dir": "tests",
        },
        run_id="run-roundtrip",
        candidate_id="cand-roundtrip",
        base_map_seq=7,
    )

    manifest = json.loads(setup.workspace.manifest_path.read_text(encoding="utf-8"))
    restored = CandidateExecutionRequest.from_dict(manifest["candidate_request"])

    assert restored == setup.request
    assert restored.project_root == test_workspace.resolve()
    restored.validate()


class TestCandidateRequestValidation:
    def test_rejects_absolute_paths(self, test_workspace: Path) -> None:
        req = CandidateExecutionRequest(
            candidate_id="cand-1",
            run_id="run-1",
            node_id="n1",
            project_root=test_workspace,
            base_map_seq=1,
            write_set=("/etc/passwd",),
            read_set=(),
            readonly_files=(),
            tests=(),
            timeout_seconds=60,
            network_allowed=False,
        )
        errors = validate_candidate_request(req)
        assert any("absolute" in e for e in errors)

    def test_rejects_parent_traversal(self, test_workspace: Path) -> None:
        req = CandidateExecutionRequest(
            candidate_id="cand-1",
            run_id="run-1",
            node_id="n1",
            project_root=test_workspace,
            base_map_seq=1,
            write_set=("../escape.py",),
            read_set=(),
            readonly_files=(),
            tests=(),
            timeout_seconds=60,
            network_allowed=False,
        )
        errors = validate_candidate_request(req)
        assert any("parent_traversal" in e for e in errors)

    def test_rejects_windows_drive_paths(self, test_workspace: Path) -> None:
        req = CandidateExecutionRequest(
            candidate_id="cand-1",
            run_id="run-1",
            node_id="n1",
            project_root=test_workspace,
            base_map_seq=1,
            write_set=("D:/secret.py",),
            read_set=(),
            readonly_files=(),
            tests=(),
            timeout_seconds=60,
            network_allowed=False,
        )
        errors = validate_candidate_request(req)
        assert any("windows_drive" in e for e in errors)

    def test_rejects_empty_candidate_id(self, test_workspace: Path) -> None:
        req = CandidateExecutionRequest(
            candidate_id="",
            run_id="run-1",
            node_id="n1",
            project_root=test_workspace,
            base_map_seq=1,
            write_set=("src/a.py",),
            read_set=(),
            readonly_files=(),
            tests=(),
            timeout_seconds=60,
            network_allowed=False,
        )
        assert "candidate_id_required" in validate_candidate_request(req)

    def test_rejects_invalid_timeout(self, test_workspace: Path) -> None:
        req = CandidateExecutionRequest(
            candidate_id="cand-1",
            run_id="run-1",
            node_id="n1",
            project_root=test_workspace,
            base_map_seq=1,
            write_set=("src/a.py",),
            read_set=(),
            readonly_files=(),
            tests=(),
            timeout_seconds=0,
            network_allowed=False,
        )
        assert "timeout_out_of_range" in validate_candidate_request(req)

    def test_same_candidate_id_resolves_to_one_root(self, test_workspace: Path) -> None:
        req_a = CandidateExecutionRequest(
            candidate_id="reuse-me",
            run_id="run-a",
            node_id="n1",
            project_root=test_workspace,
            base_map_seq=1,
            write_set=("src/a.py",),
            read_set=(),
            readonly_files=(),
            tests=(),
            timeout_seconds=60,
            network_allowed=False,
            module_id="mod-a",
        )
        req_b = CandidateExecutionRequest(
            candidate_id="reuse-me",
            run_id="run-b",
            node_id="n1",
            project_root=test_workspace,
            base_map_seq=2,
            write_set=("src/a.py",),
            read_set=(),
            readonly_files=(),
            tests=(),
            timeout_seconds=60,
            network_allowed=False,
            module_id="mod-a",
        )
        assert req_a.candidate_root == req_b.candidate_root
        assert req_a.candidate_root == (
            test_workspace / ".bridle" / "runtime" / "modules" / "mod-a" / "candidates" / "reuse-me"
        )


class TestCandidateResultContract:
    def test_result_serializable_roundtrip(self, test_workspace: Path) -> None:
        result = CandidateExecutionResult(
            status="completed",
            changed_paths=("src/a.py",),
            patches=({"path": "src/a.py", "change_type": "modify"},),
            base_hashes={"src/a.py": "aaa"},
            candidate_hashes={"src/a.py": "bbb"},
            test_results=({"command": "echo ok", "exit_code": 0},),
            container={"container_id": "fake-1"},
            diagnostic_path=str(test_workspace / "diag"),
            candidate_id="cand-1",
            base_map_seq=3,
        )
        payload = result.to_dict()
        restored = CandidateExecutionResult.from_dict(payload)
        assert restored.status == "completed"
        assert restored.changed_paths == ("src/a.py",)
        assert json.loads(json.dumps(payload)) == payload

    def test_compute_patches_detects_modify_add_remove(self) -> None:
        base = {"src/old.py": "h1", "src/keep.py": "same"}
        cand = {"src/new.py": "h2", "src/keep.py": "same"}
        changed, patches = compute_patches(
            base_hashes=base,
            candidate_hashes=cand,
            write_set=["src/old.py", "src/new.py", "src/keep.py"],
        )
        assert "src/old.py" in changed
        assert "src/new.py" in changed
        assert "src/keep.py" not in changed
        types = {p["path"]: p["change_type"] for p in patches}
        assert types["src/old.py"] == "remove"
        assert types["src/new.py"] == "add"

    def test_persist_result_writes_under_candidate_root(self, test_workspace: Path) -> None:
        candidate_root = (
            test_workspace / ".bridle" / "runtime" / "modules" / "persist-mod" / "candidates" / "persist-1"
        )
        result = CandidateExecutionResult(
            status="blocked",
            changed_paths=(),
            patches=(),
            base_hashes={},
            candidate_hashes={},
            test_results=(),
            container={},
            diagnostic_path="",
            error_code="test_failed",
            candidate_id="persist-1",
        )
        path = persist_result(result, candidate_root)
        assert path == candidate_root / "result.json"
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded["error_code"] == "test_failed"

    def test_snapshot_directory_hashes(self, test_workspace: Path) -> None:
        f = test_workspace / "src" / "x.py"
        f.parent.mkdir(parents=True)
        f.write_text("x\n", encoding="utf-8")
        hashes = snapshot_directory_hashes(test_workspace, ["src/x.py", "src/missing.py"])
        assert "src/x.py" in hashes
        assert "src/missing.py" not in hashes
