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
    file_sha256,
    persist_result,
    snapshot_directory_hashes,
    validate_candidate_request,
)
from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.agent.tools.sandboxed_executor import SandboxedToolExecutor, TDDStateTracker


def _formal_file_hashes(root: Path, rel_paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in rel_paths:
        path = root / Path(*rel.split("/"))
        if path.is_file():
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


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


class TestFormalProjectIsolationRegression:
    """Prove that writing through SandboxPolicy with formal workspace_root mutates the project."""

    @pytest.mark.asyncio
    async def test_patch_via_formal_workspace_root_changes_project_hash(
        self, test_workspace: Path
    ) -> None:
        src = test_workspace / "src"
        src.mkdir(parents=True)
        target = src / "module.py"
        target.write_text("before\n", encoding="utf-8")
        before = file_sha256(target)

        policy = SandboxPolicy.for_run(
            run_id="run-regression",
            node_id="n1",
            workspace_root=test_workspace,
            allowed_files=["src/module.py"],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state = TDDStateTracker()
        executor.tdd_state.disable_enforcement()

        diff = (
            "--- a/src/module.py\n"
            "+++ b/src/module.py\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "+after\n"
        )
        await executor.propose_file_patch("src/module.py", diff, "modify")

        after = file_sha256(target)
        assert before != after
        assert target.read_text(encoding="utf-8") == "after\n"

    @pytest.mark.asyncio
    async def test_patch_via_candidate_workspace_leaves_formal_unchanged(
        self, test_workspace: Path
    ) -> None:
        formal = test_workspace / "src" / "module.py"
        formal.parent.mkdir(parents=True)
        formal.write_text("formal-before\n", encoding="utf-8")
        before_formal = file_sha256(formal)

        candidate_root = (
            test_workspace / ".bridle" / "runtime" / "modules" / "iso-mod" / "candidates" / "iso-1"
        )
        candidate_project = candidate_root / "project" / "src" / "module.py"
        candidate_project.parent.mkdir(parents=True)
        candidate_project.write_text("candidate-before\n", encoding="utf-8")

        policy = SandboxPolicy.for_run(
            run_id="run-candidate",
            node_id="n1",
            workspace_root=candidate_root / "project",
            allowed_files=["src/module.py"],
            node_tests=[],
        )
        executor = SandboxedToolExecutor(policy)
        executor.tdd_state = TDDStateTracker()
        executor.tdd_state.disable_enforcement()

        diff = (
            "--- a/src/module.py\n"
            "+++ b/src/module.py\n"
            "@@ -1 +1 @@\n"
            "-candidate-before\n"
            "+candidate-after\n"
        )
        await executor.propose_file_patch("src/module.py", diff, "modify")

        assert file_sha256(formal) == before_formal
        assert candidate_project.read_text(encoding="utf-8") == "candidate-after\n"


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
