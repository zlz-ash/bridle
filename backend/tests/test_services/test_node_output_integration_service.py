"""Tests for node output production integration entry."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridle.services.node_output_integration_service import NodeOutputIntegrationService


class TestNodeOutputIntegrationService:
    def test_integrates_manifest_outputs_and_creates_checkpoint(self, test_workspace: Path) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")

        root = test_workspace / ".aicoding" / "container-workspaces" / "run-int"
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        output = root / "output"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        (baseline / "a.py").write_text("old\n", encoding="utf-8")
        (write / "a.py").write_text("new\n", encoding="utf-8")
        log_rel = ".aicoding/container-workspaces/run-int/diagnostics/container.log"
        (output / "manifest.json").write_text(
            json.dumps({
                "run_id": "run-int",
                "node_id": "n1",
                "baseline_revision": "a" * 40,
                "write_files": ["src/a.py"],
                "aggregate_contributions": [],
                "summary": "integrated",
                "logs": [log_rel],
                "diagnostics": [".aicoding/container-workspaces/run-int/diagnostics"],
                "test_results": {
                    "tests": [
                        {
                            "name": "t1",
                            "command": "echo ok",
                            "status": "passed",
                            "exit_code": 0,
                            "duration_ms": 1,
                            "log_ref": log_rel,
                        }
                    ]
                },
                "metrics": {
                    "items": [
                        {
                            "name": "m1",
                            "target": 1,
                            "actual": 1,
                            "status": "ok",
                            "source": "container",
                        }
                    ]
                },
            }),
            encoding="utf-8",
        )
        diag = output / ".." / ".." / "diagnostics"
        diag = test_workspace / ".aicoding" / "container-workspaces" / "run-int" / "diagnostics"
        diag.mkdir(parents=True, exist_ok=True)
        (diag / "container.log").write_text("ok\n", encoding="utf-8")

        result = NodeOutputIntegrationService(test_workspace).integrate_run(
            run_id="run-int",
            session_id="session-1",
            allowed_files=["src/a.py"],
            allowed_aggregate_paths=[],
            expected_baseline_revision="a" * 40,
        )

        assert result["status"] == "integrated"
        assert (test_workspace / "src" / "a.py").read_text(encoding="utf-8") == "new\n"
        checkpoint_path = test_workspace / ".aicoding" / "git-checkpoints" / "session-1.json"
        assert checkpoint_path.exists()

    def test_apply_failure_rolls_back_pre_integration_snapshot(self, test_workspace: Path) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")
        original = test_workspace / "src" / "a.py"
        original.parent.mkdir(parents=True, exist_ok=True)
        original.write_text("before\n", encoding="utf-8")

        root = test_workspace / ".aicoding" / "container-workspaces" / "run-rollback"
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        output = root / "output"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        (baseline / "a.py").write_text("old\n", encoding="utf-8")
        (write / "a.py").write_text("after\n", encoding="utf-8")
        log_rel = ".aicoding/container-workspaces/run-rollback/diagnostics/container.log"
        diag = test_workspace / ".aicoding" / "container-workspaces" / "run-rollback" / "diagnostics"
        diag.mkdir(parents=True, exist_ok=True)
        (diag / "container.log").write_text("ok\n", encoding="utf-8")
        (output / "manifest.json").write_text(
            json.dumps({
                "run_id": "run-rollback",
                "node_id": "n1",
                "baseline_revision": "a" * 40,
                "write_files": ["src/a.py"],
                "aggregate_contributions": [],
                "summary": "rollback test",
                "logs": [log_rel],
                "diagnostics": [".aicoding/container-workspaces/run-rollback/diagnostics"],
                "test_results": {
                    "tests": [
                        {
                            "name": "t1",
                            "command": "echo ok",
                            "status": "passed",
                            "exit_code": 0,
                            "duration_ms": 1,
                            "log_ref": log_rel,
                        }
                    ]
                },
                "metrics": {
                    "items": [
                        {
                            "name": "m1",
                            "target": 1,
                            "actual": 1,
                            "status": "ok",
                            "source": "container",
                        }
                    ]
                },
            }),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="MissingOutputError"):
            NodeOutputIntegrationService(test_workspace).integrate_run(
                run_id="run-rollback",
                session_id="session-rollback",
                allowed_files=["src/a.py", "src/missing-on-apply.py"],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

        assert original.read_text(encoding="utf-8") == "before\n"

    def test_rejects_baseline_mismatch_before_apply(self, test_workspace: Path) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")

        output = test_workspace / ".aicoding" / "container-workspaces" / "run-int" / "output"
        output.mkdir(parents=True, exist_ok=True)
        log_rel = ".aicoding/container-workspaces/run-int/diagnostics/container.log"
        diag = test_workspace / ".aicoding" / "container-workspaces" / "run-int" / "diagnostics"
        diag.mkdir(parents=True, exist_ok=True)
        (diag / "container.log").write_text("ok\n", encoding="utf-8")
        (output / "manifest.json").write_text(
            json.dumps({
                "run_id": "run-int",
                "node_id": "n1",
                "baseline_revision": "a" * 40,
                "write_files": [],
                "aggregate_contributions": [],
                "summary": "baseline mismatch",
                "logs": [log_rel],
                "diagnostics": [".aicoding/container-workspaces/run-int/diagnostics"],
                "test_results": {
                    "tests": [
                        {
                            "name": "t1",
                            "command": "echo ok",
                            "status": "passed",
                            "exit_code": 0,
                            "duration_ms": 1,
                            "log_ref": log_rel,
                        }
                    ]
                },
                "metrics": {
                    "items": [
                        {
                            "name": "m1",
                            "target": 1,
                            "actual": 1,
                            "status": "ok",
                            "source": "container",
                        }
                    ]
                },
            }),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="git_baseline_mismatch"):
            NodeOutputIntegrationService(test_workspace).integrate_run(
                run_id="run-int",
                session_id="session-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="b" * 40,
            )
