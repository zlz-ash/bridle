"""Contract tests for module_execution_snapshot from the real project map store."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bridle.features.project_map.store import ProjectPlanStore


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _insert_node(store: ProjectPlanStore, node_id: str, payload: dict, *, module_id: str | None = None) -> None:
    connection = sqlite3.connect(store.database_path)
    try:
        body = dict(payload)
        if module_id is not None:
            body["module_id"] = module_id
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, "code_change", node_id, node_id, json.dumps(body), "running"),
        )
        connection.commit()
    finally:
        connection.close()


def test_nested_module_finds_parent_tests_directory(test_workspace: Path) -> None:
    _write(test_workspace, "pkg/sub/module.py", "def run():\n    return 1\n")
    _write(test_workspace, "pkg/tests/test_module.py", "def test_run():\n    assert True\n")

    store = ProjectPlanStore(test_workspace, project_id="nested-module")
    store.initialize()
    _insert_node(
        store,
        "node-nested",
        {"files": ["pkg/sub/module.py"], "tests": ["python -m pytest pkg/tests/test_module.py -q"]},
        module_id="module-pkg",
    )
    store.rescan()

    snapshot = store.module_execution_snapshot("node-nested")
    assert snapshot.get("error_code") is None
    assert snapshot["module_id"] == "module-pkg"
    assert snapshot["node_id"] == "node-nested"
    impl_paths = {item["path"] for item in snapshot["implementation_entities"]}
    test_paths = {item["path"] for item in snapshot["test_entities"]}
    assert impl_paths == {"pkg/sub/module.py"}
    assert "pkg/tests/test_module.py" in test_paths


def test_module_id_differs_from_node_for_interfaces(test_workspace: Path) -> None:
    _write(test_workspace, "svc/api.py", "def api():\n    return 1\n")
    _write(test_workspace, "mocks/iface.py", "class Mock:\n    pass\n")

    store = ProjectPlanStore(test_workspace, project_id="module-id-map")
    store.initialize()
    _insert_node(
        store,
        "exec-node-1",
        {"files": ["svc/api.py"], "tests": ["python -m pytest svc/tests/test_api.py -q"]},
        module_id="svc-module",
    )
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO module_interfaces(id, from_module, to_module, symbol, signature, mock, confidence, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "iface-1",
                "other",
                "svc-module",
                "Api",
                "{}",
                json.dumps({"file_path": "mocks/iface.py"}),
                1.0,
                "active",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    store.rescan()

    snapshot = store.module_execution_snapshot("exec-node-1")
    assert snapshot.get("error_code") is None
    assert snapshot["module_id"] == "svc-module"
    assert len(snapshot["interfaces"]) == 1
    assert snapshot["interfaces"][0]["interface_id"] == "iface-1"
    assert snapshot["interfaces"][0]["file_path"] == "mocks/iface.py"
