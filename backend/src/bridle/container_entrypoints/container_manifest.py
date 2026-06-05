"""Build IntegrationService-compatible container manifest.json payloads."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def build_manifest(
    *,
    run_id: str,
    plan_node_id: str,
    baseline_revision: str,
    write_files: list[str],
    summary: str,
    test_results: dict[str, Any],
    metrics: dict[str, Any] | None = None,
    aggregate_contributions: list[dict[str, Any]] | None = None,
    status: str = "completed",
    error_code: str | None = None,
    run_root: Path,
) -> dict[str, Any]:
    log_rel = f".aicoding/container-workspaces/{run_id}/diagnostics/container.log"
    diag_rel = f".aicoding/container-workspaces/{run_id}/diagnostics"
    diag_dir = run_root / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)
    log_path = diag_dir / "container.log"
    if not log_path.exists():
        log_path.write_text("container run\n", encoding="utf-8")

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "node_id": plan_node_id,
        "baseline_revision": baseline_revision,
        "write_files": write_files,
        "aggregate_contributions": aggregate_contributions or [],
        "summary": summary,
        "logs": [log_rel],
        "diagnostics": [diag_rel],
        "test_results": test_results,
        "metrics": metrics
        or {
            "items": [
                {
                    "name": "container_run",
                    "target": 1,
                    "actual": 1 if status == "completed" else 0,
                    "status": "ok" if status == "completed" else "failed",
                    "source": "container",
                }
            ]
        },
        "status": status,
    }
    if error_code:
        manifest["error_code"] = error_code
    return manifest
