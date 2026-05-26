"""Integrate output artifacts produced by node containers."""
from __future__ import annotations

import json
import logging
import shutil
import difflib
from pathlib import Path
from typing import Any

from bridle.engine.git_workspace_policy import GitWorkspacePolicy
from bridle.engine.proposal_path_validator import ProposalPathValidator
from bridle.schemas.node import _validate_workspace_relative_path

logger = logging.getLogger("bridle")


class IntegrationService:
    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._git_policy = GitWorkspacePolicy()

    def collect_node_output(
        self,
        *,
        run_id: str,
        allowed_files: list[str],
        allowed_aggregate_paths: list[str],
        expected_baseline_revision: str,
    ) -> dict[str, Any]:
        check = self._git_policy.evaluate(self.workspace_root)
        if not check.ok:
            logger.info(
                "git_preflight_failed",
                extra={
                    "action": "git_preflight_failed",
                    "status": "rejected",
                    "detail": {"run_id": run_id, "error_code": check.error_code},
                },
            )
            raise ValueError("git_preflight_failed")
        if check.baseline_revision != expected_baseline_revision:
            logger.info(
                "integration_rejected_by_baseline",
                extra={
                    "action": "integration_rejected_by_baseline",
                    "status": "rejected",
                    "detail": {
                        "run_id": run_id,
                        "expected": expected_baseline_revision,
                        "actual": check.baseline_revision,
                    },
                },
            )
            raise ValueError("git_baseline_mismatch")
        logger.info(
            "git_baseline_checked",
            extra={
                "action": "git_baseline_checked",
                "status": "completed",
                "detail": {"run_id": run_id, "baseline_revision": check.baseline_revision},
            },
        )
        output_dir = self.workspace_root / ".aicoding" / "container-workspaces" / run_id / "output"
        manifest_path = output_dir / "manifest.json"
        patch_path = output_dir / "patch.json"

        if not manifest_path.exists():
            if patch_path.exists():
                raise ValueError("Node output must contain manifest.json")
            raise ValueError("Node output must contain manifest.json")

        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        for required_field in ("run_id", "node_id", "baseline_revision", "write_files", "aggregate_contributions"):
            if required_field not in manifest:
                raise ValueError(f"manifest missing required field: {required_field}")
        if manifest.get("baseline_revision") != expected_baseline_revision:
            raise ValueError("manifest_baseline_mismatch")
        if manifest["run_id"] != run_id:
            logger.info(
                "manifest_run_id_mismatch",
                extra={
                    "action": "manifest_run_id_mismatch",
                    "status": "rejected",
                    "detail": {"expected": run_id, "actual": manifest["run_id"]},
                },
            )
            raise ValueError("manifest_run_id_mismatch")
        self._validate_manifest_protocol(
            manifest,
            run_id=run_id,
            allowed_files=allowed_files,
            allowed_aggregate_paths=allowed_aggregate_paths,
        )

        file_patches: list[dict[str, str]] = []
        aggregate_contributions: list[dict[str, Any]] = list(manifest.get("aggregate_contributions") or [])

        optional_patch_patches: list[dict[str, str]] = []
        if patch_path.exists():
            payload = json.loads(patch_path.read_text(encoding="utf-8"))
            optional_patch_patches = payload.get("file_patches", [])
            if not isinstance(optional_patch_patches, list):
                raise ValueError("Node output file_patches must be a list")

        aggregate_dir = output_dir / "aggregate"
        if aggregate_dir.exists():
            manifest_aggs = manifest.get("aggregate_contributions", [])
            for entry in manifest_aggs:
                if not isinstance(entry, dict):
                    continue
                agg_rel_path = entry.get("path", "")
                if not agg_rel_path:
                    continue
                agg_src = aggregate_dir / agg_rel_path
                if agg_src.exists():
                    agg_data = json.loads(agg_src.read_text(encoding="utf-8"))
                    merged = False
                    for contribution in aggregate_contributions:
                        if contribution.get("path") == agg_rel_path:
                            contribution.update(agg_data)
                            merged = True
                            break
                    if not merged:
                        aggregate_contributions.append({"path": agg_rel_path, **agg_data})

        generated = self.generate_file_patches(run_id=run_id, allowed_files=allowed_files)
        if generated:
            if optional_patch_patches and optional_patch_patches != generated:
                raise ValueError("PatchMismatchError: node patch does not match file outputs")
            file_patches = generated
            self._write_diagnostics(run_id, file_patches)

        write_root = self.workspace_root / ".aicoding" / "container-workspaces" / run_id / "workspace" / "write"
        if write_root.exists():
            undeclared = set()
            for path in write_root.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(write_root).as_posix()
                    if rel not in set(manifest.get("write_files") or []):
                        undeclared.add(rel)
            if undeclared:
                raise ValueError(f"manifest_undeclared_write_files: {sorted(undeclared)}")
        if manifest.get("write_files"):
            actual_files = {p["path"] for p in generated} if generated else set()
            declared_files = {_validate_workspace_relative_path(path) for path in manifest["write_files"]}
            if actual_files != declared_files:
                logger.info(
                    "manifest_write_mismatch",
                    extra={
                        "action": "manifest_write_mismatch",
                        "status": "rejected",
                        "detail": {
                            "run_id": run_id,
                            "declared": sorted(declared_files),
                            "actual": sorted(actual_files),
                        },
                    },
                )
                raise ValueError("manifest_write_mismatch")

        boundary_errors = ProposalPathValidator.validate(file_patches, allowed_files)
        if boundary_errors:
            raise ValueError(f"PathBoundaryError: {boundary_errors[0]}")

        allowed_aggregates = {
            _validate_workspace_relative_path(path) for path in allowed_aggregate_paths
        }
        saved_aggregates: list[str] = []
        for contribution in aggregate_contributions:
            if not isinstance(contribution, dict):
                raise ValueError("Aggregate contribution must be an object")
            contribution_path = _validate_workspace_relative_path(str(contribution.get("path", "")))
            if contribution_path not in allowed_aggregates:
                raise ValueError(f"AggregateBoundaryError: {contribution_path}")
            target = self.workspace_root / contribution_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps({"items": contribution.get("items", [])}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            saved_aggregates.append(contribution_path)

        logger.info(
            "node_output_collected",
            extra={
                "action": "node_output_collected",
                "status": "completed",
                "run_id": run_id,
                "detail": {
                    "patch_count": len(file_patches),
                    "aggregate_count": len(saved_aggregates),
                },
            },
        )
        return {
            "status": "collected",
            "file_patches": file_patches,
            "aggregate_contributions": saved_aggregates,
            "aggregate_contributions_meta": aggregate_contributions,
        }

    def _validate_manifest_protocol(
        self,
        manifest: dict[str, Any],
        *,
        run_id: str,
        allowed_files: list[str],
        allowed_aggregate_paths: list[str],
    ) -> None:
        for field in ("test_results", "metrics", "logs", "diagnostics", "summary"):
            if field not in manifest:
                raise ValueError(f"manifest missing required field: {field}")
        if not str(manifest.get("summary", "")).strip():
            raise ValueError("manifest summary must be non-empty")
        if not isinstance(manifest.get("test_results"), dict):
            raise ValueError("manifest test_results must be an object")
        tests = manifest["test_results"].get("tests")
        if not isinstance(tests, list):
            raise ValueError("manifest test_results.tests must be a list")
        if not tests:
            raise ValueError("manifest test_results.tests must not be empty")
        for item in tests:
            if not isinstance(item, dict):
                raise ValueError("manifest test_results.tests items must be objects")
            for key in ("name", "command", "status", "exit_code", "duration_ms", "log_ref"):
                if key not in item:
                    raise ValueError(f"manifest test_results.tests missing field: {key}")
            status = str(item.get("status", ""))
            if status not in {"passed", "failed", "skipped"}:
                raise ValueError(f"manifest test status is invalid: {status}")
            if status == "failed":
                raise ValueError("container_test_failed")
            self._validate_manifest_ref_exists(
                str(item.get("log_ref", "")),
                run_id=run_id,
                expected_kind="file",
                error_code="manifest_log_missing",
            )
        if not isinstance(manifest.get("metrics"), dict):
            raise ValueError("manifest metrics must be an object")
        metric_items = manifest["metrics"].get("items")
        if not isinstance(metric_items, list):
            raise ValueError("manifest metrics.items must be a list")
        if not metric_items:
            raise ValueError("manifest metrics.items must not be empty")
        for item in metric_items:
            if not isinstance(item, dict):
                raise ValueError("manifest metrics.items entries must be objects")
            for key in ("name", "target", "actual", "status", "source"):
                if key not in item:
                    raise ValueError(f"manifest metrics.items missing field: {key}")
            status = str(item.get("status", ""))
            if status not in {"ok", "failed", "warning", "skipped"}:
                raise ValueError(f"manifest metric status is invalid: {status}")
            if status == "failed":
                raise ValueError("container_metric_failed")
        for list_field in ("logs", "diagnostics"):
            value = manifest.get(list_field)
            if not isinstance(value, list):
                raise ValueError(f"manifest {list_field} must be a list")
            for entry in value:
                self._validate_manifest_ref_exists(
                    str(entry),
                    run_id=run_id,
                    expected_kind="dir" if list_field == "diagnostics" else "file",
                    error_code="manifest_diagnostic_missing" if list_field == "diagnostics" else "manifest_log_missing",
                )
        write_files = manifest.get("write_files") or []
        if not isinstance(write_files, list):
            raise ValueError("manifest write_files must be a list")
        for path in write_files:
            normalized = _validate_workspace_relative_path(str(path))
            if normalized not in set(allowed_files):
                raise ValueError(f"manifest_write_not_allowed: {normalized}")
        for entry in manifest.get("aggregate_contributions") or []:
            if not isinstance(entry, dict):
                raise ValueError("manifest aggregate_contributions entries must be objects")
            contribution_path = _validate_workspace_relative_path(str(entry.get("path", "")))
            if contribution_path not in set(allowed_aggregate_paths):
                raise ValueError(f"manifest_aggregate_not_allowed: {contribution_path}")
            if not str(entry.get("aggregate_target", "")).strip():
                raise ValueError("manifest aggregate_contributions missing aggregate_target")

    def _validate_manifest_ref_exists(
        self,
        value: str,
        *,
        run_id: str,
        expected_kind: str,
        error_code: str,
    ) -> None:
        normalized = _validate_workspace_relative_path(value)
        allowed_prefix = f".aicoding/container-workspaces/{run_id}/"
        if not normalized.startswith(allowed_prefix):
            raise ValueError(f"manifest_path_out_of_bounds: {normalized}")
        path = self.workspace_root / normalized
        if expected_kind == "file" and not path.is_file():
            raise ValueError(error_code)
        if expected_kind == "dir" and not path.exists():
            raise ValueError(error_code)

    def generate_file_patches(self, *, run_id: str, allowed_files: list[str]) -> list[dict[str, str]]:
        root = self.workspace_root / ".aicoding" / "container-workspaces" / run_id / "workspace"
        baseline_root = root / "baseline"
        write_root = root / "write"
        if not baseline_root.exists() and not write_root.exists():
            return []

        patches: list[dict[str, str]] = []
        for path in sorted({_validate_workspace_relative_path(value) for value in allowed_files}):
            baseline_path = baseline_root / path
            write_path = write_root / path
            baseline_exists = baseline_path.exists()
            write_exists = write_path.exists()
            if not baseline_exists and not write_exists:
                continue
            before = baseline_path.read_text(encoding="utf-8") if baseline_exists else ""
            after = write_path.read_text(encoding="utf-8") if write_exists else ""
            if before == after:
                continue
            if baseline_exists and write_exists:
                change_type = "modify"
            elif write_exists:
                change_type = "add"
            else:
                change_type = "remove"
            diff = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
            )
            patches.append({"path": path, "change_type": change_type, "diff": diff})
        errors = ProposalPathValidator.validate(patches, allowed_files)
        if errors:
            raise ValueError(f"PathBoundaryError: {errors[0]}")
        return patches

    def apply_workspace_outputs(self, *, run_id: str, allowed_files: list[str], expected_baseline_revision: str) -> dict[str, Any]:
        check = self._git_policy.evaluate(self.workspace_root)
        if not check.ok:
            raise ValueError("git_preflight_failed")
        if check.baseline_revision != expected_baseline_revision:
            logger.info(
                "apply_rejected_by_baseline",
                extra={
                    "action": "apply_rejected_by_baseline",
                    "status": "rejected",
                    "detail": {
                        "run_id": run_id,
                        "expected": expected_baseline_revision,
                        "actual": check.baseline_revision,
                    },
                },
            )
            raise ValueError("git_baseline_mismatch")
        write_root = self.workspace_root / ".aicoding" / "container-workspaces" / run_id / "workspace" / "write"
        normalized = [_validate_workspace_relative_path(path) for path in allowed_files]
        snapshots: dict[str, str | None] = {}
        try:
            for path in normalized:
                source = write_root / path
                target = self.workspace_root / path
                if not source.exists():
                    raise ValueError(f"MissingOutputError: {path}")
                snapshots[path] = target.read_text(encoding="utf-8") if target.exists() else None
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        except Exception:
            for path, content in snapshots.items():
                target = self.workspace_root / path
                if content is None:
                    if target.exists():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
            raise
        logger.info(
            "patch_applied",
            extra={
                "action": "patch_applied",
                "status": "completed",
                "run_id": run_id,
                "detail": {"file_count": len(normalized)},
            },
        )
        return {"status": "applied", "file_count": len(normalized)}

    def assert_baseline_matches(self, expected_revision: str) -> None:
        check = self._git_policy.evaluate(self.workspace_root)
        if not check.ok:
            logger.info(
                "git_preflight_failed",
                extra={
                    "action": "git_preflight_failed",
                    "status": "rejected",
                    "detail": {"error_code": check.error_code},
                },
            )
            raise ValueError("git_preflight_failed")
        if check.baseline_revision != expected_revision:
            logger.info(
                "git_baseline_mismatch",
                extra={
                    "action": "git_baseline_mismatch",
                    "status": "rejected",
                    "detail": {"expected": expected_revision, "actual": check.baseline_revision},
                },
            )
            raise ValueError("git_baseline_mismatch")
        logger.info(
            "git_baseline_checked",
            extra={
                "action": "git_baseline_checked",
                "status": "completed",
                "detail": {"baseline_revision": check.baseline_revision},
            },
        )

    def _write_diagnostics(self, run_id: str, patches: list[dict[str, str]]) -> None:
        diag_dir = self.workspace_root / ".aicoding" / "container-workspaces" / run_id / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        (diag_dir / "generated.patch").write_text(
            json.dumps(patches, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "generated_patch_written",
            extra={
                "action": "generated_patch_written",
                "status": "completed",
                "detail": {"run_id": run_id, "patch_count": len(patches)},
            },
        )
