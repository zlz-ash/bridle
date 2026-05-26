"""Tests for integrating node container outputs."""
from __future__ import annotations

import json
from uuid import uuid4
from pathlib import Path

import pytest

from bridle.services.integration_service import IntegrationService
from bridle.services.git_checkpoint_service import GitCheckpointService
from bridle.services.container_output_simulator import ContainerOutputSimulator

_BASELINE = "a" * 40


def _protocol_fields(run_id: str) -> dict:
    log_rel = f".aicoding/container-workspaces/{run_id}/diagnostics/container.log"
    diag_rel = f".aicoding/container-workspaces/{run_id}/diagnostics"
    return {
        "test_results": {
            "tests": [
                {
                    "name": "unit",
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
                    "name": "coverage",
                    "target": 1,
                    "actual": 1,
                    "status": "ok",
                    "source": "container",
                }
            ]
        },
        "logs": [log_rel],
        "diagnostics": [diag_rel],
        "summary": "node container completed",
    }


def _write_manifest(
    output_dir: Path,
    *,
    run_id: str = "run-1",
    node_id: str = "n1",
    baseline_revision: str = _BASELINE,
    write_files: list[str] | None = None,
    aggregate_contributions: list | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_root = output_dir.parent
    diag_dir = run_root / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    (diag_dir / "container.log").write_text("ok\n", encoding="utf-8")
    aggs = aggregate_contributions or []
    normalized_aggs = []
    for entry in aggs:
        if isinstance(entry, dict) and "aggregate_target" not in entry:
            normalized_aggs.append({**entry, "aggregate_target": entry.get("path", "")})
        else:
            normalized_aggs.append(entry)
    payload = {
        "run_id": run_id,
        "node_id": node_id,
        "baseline_revision": baseline_revision,
        "write_files": write_files or [],
        "aggregate_contributions": normalized_aggs,
        **_protocol_fields(run_id),
    }
    (output_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


class TestIntegrationService:
    def test_container_output_simulator_does_not_write_main_workspace_aggregate(self, test_workspace: Path) -> None:
        contribution = f".bridle/aggregate/src/router.json/{uuid4().hex}.json"

        ContainerOutputSimulator(test_workspace).write_for_run(
            run_id="run-1",
            node_id="n1",
            baseline_revision="a" * 40,
            write_files=[],
            aggregate_contributions=[{"path": contribution, "aggregate_target": "src/router.json"}],
        )

        assert not (test_workspace / contribution).exists()
        assert (
            test_workspace
            / ".aicoding"
            / "container-workspaces"
            / "run-1"
            / "output"
            / "aggregate"
            / contribution
        ).exists()

    def test_collects_declared_patch_and_aggregate_output(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        output_dir = root / "output"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        (baseline / "write.py").write_text("old\n", encoding="utf-8")
        (write / "write.py").write_text("new\n", encoding="utf-8")
        agg_rel = ".bridle/aggregate/src/router.json/n1.json"
        _write_manifest(
            output_dir,
            write_files=["src/write.py"],
            aggregate_contributions=[{"path": agg_rel}],
        )
        agg_src = output_dir / "aggregate" / agg_rel
        agg_src.parent.mkdir(parents=True, exist_ok=True)
        agg_src.write_text(json.dumps({"items": [{"path": "/n1"}]}), encoding="utf-8")
        (output_dir / "patch.json").unlink(missing_ok=True)
        result = IntegrationService(test_workspace).collect_node_output(
            run_id="run-1",
            allowed_files=["src/write.py"],
            allowed_aggregate_paths=[".bridle/aggregate/src/router.json/n1.json"],
            expected_baseline_revision="a" * 40,
        )

        assert result["status"] == "collected"
        assert result["file_patches"][0]["path"] == "src/write.py"
        saved = json.loads(
            (test_workspace / ".bridle" / "aggregate" / "src" / "router.json" / "n1.json").read_text(
                encoding="utf-8"
            )
        )
        assert saved == {"items": [{"path": "/n1"}]}

    def test_rejects_patch_outside_write_boundary(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        _write_manifest(output_dir, write_files=["src/not-allowed.py"])
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        (baseline / "not-allowed.py").write_text("old\n", encoding="utf-8")
        (write / "not-allowed.py").write_text("new\n", encoding="utf-8")

        with pytest.raises(ValueError, match="manifest_write_not_allowed"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=["src/write.py"],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_rejects_undeclared_aggregate_output(self, test_workspace: Path) -> None:
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        _write_manifest(
            output_dir,
            aggregate_contributions=[{"path": ".bridle/aggregate/src/other.json/n1.json"}],
        )
        (output_dir / "patch.json").write_text(
            json.dumps(
                {
                    "file_patches": [],
                    "aggregate_contributions": [
                        {"path": ".bridle/aggregate/src/other.json/n1.json", "items": []}
                    ],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="manifest_aggregate_not_allowed"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[".bridle/aggregate/src/router.json/n1.json"],
                expected_baseline_revision="a" * 40,
            )

    def test_generates_patches_from_baseline_and_write_outputs(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "workspace"
        baseline = root / "baseline" / "src"
        write = root / "write" / "src"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        (baseline / "modify.py").write_text("old\n", encoding="utf-8")
        (write / "modify.py").write_text("new\n", encoding="utf-8")
        (write / "add.py").write_text("added\n", encoding="utf-8")
        (baseline / "delete.py").write_text("delete me\n", encoding="utf-8")

        patches = IntegrationService(test_workspace).generate_file_patches(
            run_id="run-1",
            allowed_files=["src/modify.py", "src/add.py", "src/delete.py"],
        )

        by_path = {patch["path"]: patch for patch in patches}
        assert by_path["src/modify.py"]["change_type"] == "modify"
        assert "old" in by_path["src/modify.py"]["diff"]
        assert "new" in by_path["src/modify.py"]["diff"]
        assert by_path["src/add.py"]["change_type"] == "add"
        assert by_path["src/delete.py"]["change_type"] == "remove"

    def test_rejects_node_declared_patch_that_differs_from_file_outputs(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        output = root / "output"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        (baseline / "modify.py").write_text("old\n", encoding="utf-8")
        (write / "modify.py").write_text("new\n", encoding="utf-8")
        _write_manifest(output, write_files=["src/modify.py"])
        (output / "patch.json").write_text(
            json.dumps(
                {
                    "file_patches": [
                        {"path": "src/modify.py", "change_type": "modify", "diff": "not the generated diff"}
                    ],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="PatchMismatchError"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=["src/modify.py"],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_rejects_empty_tests_and_metrics(self, test_workspace: Path) -> None:
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        _write_manifest(output_dir)
        manifest_path = output_dir / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["test_results"] = {"tests": []}
        payload["metrics"] = {"items": []}
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="manifest test_results.tests must not be empty"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_rejects_failed_test_status(self, test_workspace: Path) -> None:
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        _write_manifest(output_dir)
        manifest_path = output_dir / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["test_results"]["tests"][0]["status"] = "failed"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="container_test_failed"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_rejects_failed_metric_status(self, test_workspace: Path) -> None:
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        _write_manifest(output_dir)
        manifest_path = output_dir / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["metrics"]["items"][0]["status"] = "failed"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="container_metric_failed"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_rejects_missing_log_reference(self, test_workspace: Path) -> None:
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        _write_manifest(output_dir)
        manifest_path = output_dir / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        missing_ref = ".aicoding/container-workspaces/run-1/diagnostics/missing.log"
        payload["logs"] = [missing_ref]
        payload["test_results"]["tests"][0]["log_ref"] = missing_ref
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="manifest_log_missing"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_applies_generated_patches_and_rolls_back_on_failure(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "workspace"
        write = root / "write" / "src"
        write.mkdir(parents=True, exist_ok=True)
        (test_workspace / "src").mkdir(exist_ok=True)
        (test_workspace / "src" / "a.py").write_text("before\n", encoding="utf-8")
        (write / "a.py").write_text("after\n", encoding="utf-8")

        service = IntegrationService(test_workspace)
        applied = service.apply_workspace_outputs(
            run_id="run-1",
            allowed_files=["src/a.py"],
            expected_baseline_revision="a" * 40,
        )
        assert applied["status"] == "applied"
        assert (test_workspace / "src" / "a.py").read_text(encoding="utf-8") == "after\n"

        (test_workspace / "src" / "a.py").write_text("stable\n", encoding="utf-8")
        with pytest.raises(ValueError, match="MissingOutputError"):
            service.apply_workspace_outputs(run_id="run-1", allowed_files=["src/missing.py"], expected_baseline_revision="a" * 40)
        assert (test_workspace / "src" / "a.py").read_text(encoding="utf-8") == "stable\n"

    def test_apply_rejects_baseline_mismatch(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "workspace"
        write = root / "write" / "src"
        write.mkdir(parents=True, exist_ok=True)
        (write / "a.py").write_text("after\n", encoding="utf-8")

        service = IntegrationService(test_workspace)
        with pytest.raises(ValueError, match="git_baseline_mismatch"):
            service.apply_workspace_outputs(
                run_id="run-1",
                allowed_files=["src/a.py"],
                expected_baseline_revision="b" * 40,
            )

    def test_apply_rejects_baseline_mismatch_is_logged(self, test_workspace: Path, caplog) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "workspace"
        write = root / "write" / "src"
        write.mkdir(parents=True, exist_ok=True)
        (write / "a.py").write_text("after\n", encoding="utf-8")

        service = IntegrationService(test_workspace)
        with caplog.at_level("INFO"), pytest.raises(ValueError, match="git_baseline_mismatch"):
            service.apply_workspace_outputs(
                run_id="run-1",
                allowed_files=["src/a.py"],
                expected_baseline_revision="b" * 40,
            )

        assert any("apply_rejected_by_baseline" in r.message for r in caplog.records)


class TestGitCheckpointService:
    def test_creates_checkpoint_and_rejects_baseline_mismatch(self, test_workspace: Path) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("c" * 40 + "\n", encoding="utf-8")

        service = GitCheckpointService(test_workspace)
        checkpoint = service.create_checkpoint("session-1")

        assert checkpoint["baseline_revision"] == "c" * 40
        service.assert_baseline_matches("c" * 40)
        with pytest.raises(ValueError, match="git_baseline_mismatch"):
            service.assert_baseline_matches("d" * 40)

    def test_collect_node_output_checks_baseline_before_applying(self, test_workspace: Path) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")

        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "workspace"
        write = root / "write" / "src"
        write.mkdir(parents=True, exist_ok=True)
        (write / "a.py").write_text("after\n", encoding="utf-8")
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        _write_manifest(output_dir)

        service = IntegrationService(test_workspace)
        service.assert_baseline_matches("a" * 40)

        with pytest.raises(ValueError, match="git_baseline_mismatch"):
            service.assert_baseline_matches("b" * 40)

    def test_collect_node_output_rejects_baseline_mismatch(self, test_workspace: Path) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")

        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        _write_manifest(output_dir)

        with pytest.raises(ValueError, match="git_baseline_mismatch"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="b" * 40,
            )

    def test_collect_node_output_passes_with_matching_baseline(self, test_workspace: Path) -> None:
        git_dir = test_workspace / ".git" / "refs" / "heads"
        git_dir.mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git_dir / "main").write_text("a" * 40 + "\n", encoding="utf-8")

        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        _write_manifest(output_dir)

        result = IntegrationService(test_workspace).collect_node_output(
            run_id="run-1",
            allowed_files=[],
            allowed_aggregate_paths=[],
            expected_baseline_revision="a" * 40,
        )

        assert result["status"] == "collected"

    def test_collect_node_output_rejects_git_preflight_failure(self, test_workspace: Path) -> None:
        import shutil
        shutil.rmtree(test_workspace / ".git", ignore_errors=True)
        (test_workspace / ".git").mkdir(parents=True, exist_ok=True)
        (test_workspace / ".git" / "HEAD").write_text("not-a-sha\n", encoding="utf-8")
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        _write_manifest(output_dir)

        with pytest.raises(ValueError, match="git_preflight_failed"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )


class TestIntegrationWithoutPatchFile:
    def test_collects_output_without_patch_json(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        _write_manifest(output_dir, write_files=["src/a.py"])
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        (baseline / "a.py").write_text("old\n", encoding="utf-8")
        (write / "a.py").write_text("new\n", encoding="utf-8")

        result = IntegrationService(test_workspace).collect_node_output(
            run_id="run-1",
            allowed_files=["src/a.py"],
            allowed_aggregate_paths=[],
            expected_baseline_revision="a" * 40,
        )

        assert result["status"] == "collected"
        assert len(result["file_patches"]) == 1
        assert result["file_patches"][0]["path"] == "src/a.py"
        assert result["file_patches"][0]["change_type"] == "modify"

    def test_writes_generated_patch_to_diagnostics(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        _write_manifest(output_dir, write_files=["src/a.py"])
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        (baseline / "a.py").write_text("old\n", encoding="utf-8")
        (write / "a.py").write_text("new\n", encoding="utf-8")

        IntegrationService(test_workspace).collect_node_output(
            run_id="run-1",
            allowed_files=["src/a.py"],
            allowed_aggregate_paths=[],
            expected_baseline_revision="a" * 40,
        )

        diag_path = root / "diagnostics" / "generated.patch"
        assert diag_path.exists()
        patch_content = json.loads(diag_path.read_text(encoding="utf-8"))
        assert len(patch_content) == 1
        assert patch_content[0]["path"] == "src/a.py"

    def test_rejects_node_patch_mismatch_when_patch_json_present(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        output = root / "output"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        (baseline / "modify.py").write_text("old\n", encoding="utf-8")
        (write / "modify.py").write_text("new\n", encoding="utf-8")
        _write_manifest(output, write_files=["src/modify.py"])
        (output / "patch.json").write_text(
            json.dumps(
                {
                    "file_patches": [
                        {"path": "src/modify.py", "change_type": "modify", "diff": "not the generated diff"}
                    ],
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="PatchMismatchError"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=["src/modify.py"],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_rejects_patch_only_without_manifest(self, test_workspace: Path) -> None:
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "patch.json").write_text(
            json.dumps({"file_patches": [], "aggregate_contributions": []}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="manifest.json"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_missing_manifest_and_no_patch_fails(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        with pytest.raises(ValueError, match="manifest.json"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )


class TestManifestValidation:
    def test_rejects_manifest_missing_run_id(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text(
            json.dumps({"node_id": "n1"}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="manifest missing required field: run_id"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_rejects_manifest_missing_node_id(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "manifest.json").write_text(
            json.dumps({"run_id": "run-1"}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="manifest missing required field: node_id"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_reads_aggregate_from_output_directory(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        agg_rel = ".bridle/aggregate/src/router.json/n1.json"
        _write_manifest(
            output_dir,
            aggregate_contributions=[{"path": agg_rel}],
        )
        agg_src = output_dir / "aggregate" / agg_rel
        agg_src.parent.mkdir(parents=True, exist_ok=True)
        agg_src.write_text(
            json.dumps({"items": [{"path": "/n1", "handler": "n1"}]}),
            encoding="utf-8",
        )

        result = IntegrationService(test_workspace).collect_node_output(
            run_id="run-1",
            allowed_files=[],
            allowed_aggregate_paths=[agg_rel],
            expected_baseline_revision="a" * 40,
        )

        assert result["status"] == "collected"
        saved = json.loads(
            (test_workspace / agg_rel).read_text(encoding="utf-8")
        )
        assert saved == {"items": [{"path": "/n1", "handler": "n1"}]}

    def test_rejects_manifest_write_mismatch(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest(output_dir, write_files=["src/declared.py"])
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        (baseline / "actual.py").write_text("old\n", encoding="utf-8")
        (write / "actual.py").write_text("new\n", encoding="utf-8")

        with pytest.raises(ValueError, match="manifest_write_not_allowed"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=["src/actual.py"],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_rejects_manifest_write_mismatch_after_generation(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        baseline = root / "workspace" / "baseline" / "src"
        write = root / "workspace" / "write" / "src"
        baseline.mkdir(parents=True, exist_ok=True)
        write.mkdir(parents=True, exist_ok=True)
        (baseline / "actual.py").write_text("old\n", encoding="utf-8")
        (write / "actual.py").write_text("new\n", encoding="utf-8")
        _write_manifest(output_dir, write_files=["src/declared.py"])

        with pytest.raises(ValueError, match="manifest_undeclared_write_files"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=["src/actual.py", "src/declared.py"],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_rejects_manifest_run_id_mismatch(self, test_workspace: Path) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest(output_dir, run_id="different-run")

        with pytest.raises(ValueError, match="manifest_run_id_mismatch"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

    def test_manifest_run_id_mismatch_is_logged(self, test_workspace: Path, caplog) -> None:
        root = test_workspace / ".aicoding" / "container-workspaces" / "run-1"
        output_dir = root / "output"
        _write_manifest(output_dir, run_id="different-run")

        with caplog.at_level("INFO"), pytest.raises(ValueError, match="manifest_run_id_mismatch"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="a" * 40,
            )

        assert any("manifest_run_id_mismatch" in r.message for r in caplog.records)


class TestBaselineRequired:
    def test_rejects_missing_expected_baseline_revision(self, test_workspace: Path) -> None:
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest(output_dir)

        with pytest.raises(TypeError):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
            )

    def test_baseline_mismatch_logs_integration_rejected(self, test_workspace: Path, caplog) -> None:
        output_dir = test_workspace / ".aicoding" / "container-workspaces" / "run-1" / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_manifest(output_dir)

        with caplog.at_level("INFO"), pytest.raises(ValueError, match="git_baseline_mismatch"):
            IntegrationService(test_workspace).collect_node_output(
                run_id="run-1",
                allowed_files=[],
                allowed_aggregate_paths=[],
                expected_baseline_revision="b" * 40,
            )

        assert any("integration_rejected_by_baseline" in r.message for r in caplog.records)
