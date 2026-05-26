"""Simulate node container output for fake/test container runs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bridle.engine.container_runner import FakeContainerRunner
from bridle.engine.container_runner_factory import resolve_container_runner


class ContainerOutputSimulator:
    """Write protocol-compliant container output when the runtime is fake/in-memory."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    @staticmethod
    def should_simulate(workspace_root: str | Path) -> bool:
        runner = resolve_container_runner(workspace_root)
        return isinstance(runner, FakeContainerRunner)

    def write_for_run(
        self,
        *,
        run_id: str,
        node_id: str,
        baseline_revision: str,
        write_files: list[str],
        summary: str = "container execution completed",
        aggregate_contributions: list[dict[str, Any]] | None = None,
    ) -> Path:
        root = self.workspace_root / ".aicoding" / "container-workspaces" / run_id
        baseline_dir = root / "workspace" / "baseline"
        write_dir = root / "workspace" / "write"
        output_dir = root / "output"
        diag_dir = root / "diagnostics"
        for path in (baseline_dir, write_dir, output_dir, diag_dir):
            path.mkdir(parents=True, exist_ok=True)

        for rel in write_files:
            rel_path = Path(rel)
            (baseline_dir / rel_path).parent.mkdir(parents=True, exist_ok=True)
            (write_dir / rel_path).parent.mkdir(parents=True, exist_ok=True)
            (baseline_dir / rel_path).write_text("before\n", encoding="utf-8")
            (write_dir / rel_path).write_text("after\n", encoding="utf-8")

        log_rel = f".aicoding/container-workspaces/{run_id}/diagnostics/container.log"
        diag_rel = f".aicoding/container-workspaces/{run_id}/diagnostics"
        (diag_dir / "container.log").write_text("simulated container log\n", encoding="utf-8")

        manifest = {
            "run_id": run_id,
            "node_id": node_id,
            "baseline_revision": baseline_revision,
            "write_files": write_files,
            "aggregate_contributions": aggregate_contributions or [],
            "test_results": {
                "tests": [
                    {
                        "name": "simulated",
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
                        "name": "simulated_metric",
                        "target": 1,
                        "actual": 1,
                        "status": "ok",
                        "source": "container",
                    }
                ]
            },
            "logs": [log_rel],
            "diagnostics": [diag_rel],
            "summary": summary,
        }
        for entry in aggregate_contributions or []:
            rel = str(entry.get("path", "")).strip()
            if not rel:
                continue
            agg_copy = output_dir / "aggregate" / rel
            agg_copy.parent.mkdir(parents=True, exist_ok=True)
            agg_copy.write_text(
                json.dumps({"items": [{"path": "/sim", "handler": "sim"}]}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        manifest_path = output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return manifest_path
