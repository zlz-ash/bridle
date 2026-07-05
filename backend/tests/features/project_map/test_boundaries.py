"""Task 4 — co-change, metrics, clustering, boundary conflicts."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bridle.features.project_map.store import ProjectPlanStore

pytestmark = pytest.mark.usefixtures("test_workspace")


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_cochange_jaccard_weight_and_metrics_recomputed(test_workspace: Path) -> None:
    """After rescan, module_metrics rows exist with ca/ce/instability."""
    _write(test_workspace, "pkg/__init__.py", "")
    _write(test_workspace, "pkg/a.py", "def a():\n    return 1\n")
    _write(test_workspace, "other/b.py", "def b():\n    return 2\n")

    store = ProjectPlanStore(test_workspace, project_id="boundaries")
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    try:
        metrics = connection.execute(
            "SELECT module_id, metric, value FROM module_metrics WHERE module_id = 'pkg'"
        ).fetchall()
    finally:
        connection.close()

    metric_names = {row[1] for row in metrics}
    assert "ca" in metric_names
    assert "ce" in metric_names
    assert "instability" in metric_names


def test_cluster_modules_follow_directory_prior(test_workspace: Path) -> None:
    _write(test_workspace, "alpha/x.py", "def x():\n    pass\n")
    _write(test_workspace, "beta/y.py", "def y():\n    pass\n")

    store = ProjectPlanStore(test_workspace, project_id="cluster")
    store.initialize()
    modules = store.cluster_modules()["modules"]
    assert "alpha" in modules
    assert "beta" in modules
    assert any(path.endswith("alpha/x.py") for path in modules["alpha"])


def test_refresh_boundaries_updates_metrics_without_changing_node_status(test_workspace: Path) -> None:
    _write(test_workspace, "mod/a.py", "def a():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="boundary-refresh")
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('mod-node', 'module', 'mod', 'mod', ?, 'ratified')",
            (json.dumps({"files": ["mod/a.py"]}),),
        )
        connection.commit()
        status_before = connection.execute(
            "SELECT status FROM plan_nodes WHERE id = 'mod-node'"
        ).fetchone()[0]
    finally:
        connection.close()

    store.refresh_boundaries()

    connection = sqlite3.connect(store.database_path)
    try:
        status_after = connection.execute(
            "SELECT status FROM plan_nodes WHERE id = 'mod-node'"
        ).fetchone()[0]
        metric_count = connection.execute("SELECT COUNT(*) FROM module_metrics").fetchone()[0]
    finally:
        connection.close()

    assert status_before == status_after == "ratified"
    assert int(metric_count) > 0


def test_boundary_conflicts_list_is_available(test_workspace: Path) -> None:
    _write(test_workspace, "a/f.py", "def f():\n    pass\n")
    store = ProjectPlanStore(test_workspace, project_id="conflicts")
    store.initialize()
    result = store.list_boundary_conflicts(limit=5)
    assert "items" in result
    assert "debt_nodes" in result
