"""Tests for node container workspace construction."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridle.engine.container_workspace import ContainerWorkspaceBuilder


class TestContainerWorkspaceBuilder:
    def test_builds_minimal_workspace_with_baseline(self, test_workspace: Path) -> None:
        (test_workspace / "src").mkdir(exist_ok=True)
        (test_workspace / "src" / "write.py").write_text("print('write')\n", encoding="utf-8")
        (test_workspace / "src" / "read.py").write_text("print('read')\n", encoding="utf-8")
        (test_workspace / "src" / "secret.py").write_text("secret\n", encoding="utf-8")

        result = ContainerWorkspaceBuilder(test_workspace).build_node_workspace(
            run_id="run-1",
            node_id="n1",
            read_set=["src/read.py"],
            write_set=["src/write.py"],
            readonly_context=["src/read.py"],
            interfaces={"consumes": []},
            tests=["pytest tests/"],
            metrics={"coverage": 80},
            conflict_contributions=[
                {
                    "aggregate_target": "src/router.py",
                    "contribution_path": ".bridle/aggregate/src/router.py/n1.json",
                }
            ],
        )

        assert (result.root / "workspace" / "write" / "src" / "write.py").read_text(encoding="utf-8") == "print('write')\n"
        assert (result.root / "workspace" / "baseline" / "src" / "write.py").read_text(encoding="utf-8") == "print('write')\n"
        assert (result.root / "workspace" / "read" / "src" / "read.py").read_text(encoding="utf-8") == "print('read')\n"
        assert not (result.root / "workspace" / "read" / "src" / "secret.py").exists()
        assert (result.root / "output").is_dir()
        assert (result.root / "interfaces" / "interfaces.json").is_file()
        assert (result.root / "tests" / "tests.json").is_file()
        assert (result.root / "metrics" / "metrics.json").is_file()
        assert (result.root / "aggregate" / ".bridle" / "aggregate" / "src" / "router.py").is_dir()

        manifest = json.loads((result.root / "workspace-manifest.json").read_text(encoding="utf-8"))
        assert manifest["node_id"] == "n1"
        assert manifest["mounts"]["write"] == ["src/write.py"]
        assert manifest["mounts"]["read"] == ["src/read.py"]
        assert manifest["mounts"]["baseline"] == ["src/write.py"]

    def test_rejects_paths_outside_workspace(self, test_workspace: Path) -> None:
        builder = ContainerWorkspaceBuilder(test_workspace)

        with pytest.raises(ValueError, match="workspace-relative"):
            builder.build_node_workspace(
                run_id="run-1",
                node_id="n1",
                read_set=["../secret.py"],
                write_set=[],
                readonly_context=[],
                interfaces={},
                tests=[],
                metrics={},
                conflict_contributions=[],
            )
