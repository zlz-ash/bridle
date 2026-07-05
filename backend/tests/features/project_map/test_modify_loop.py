"""Task 5 — module interfaces, dispatch, dual gates, drift."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bridle.agent.safety.sandbox_policy import SandboxPolicy
from bridle.api.errors import ConflictError
from bridle.features.project_map.modify_loop_service import CONSISTENCY_GATE_ERROR, TDD_GATE_ERROR
from bridle.features.project_map.store import ProjectPlanStore

pytestmark = pytest.mark.usefixtures("test_workspace")


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_mock_path_is_readonly_for_patch_policy(test_workspace: Path) -> None:
    policy = SandboxPolicy.for_run(
        run_id="r1",
        node_id="n1",
        workspace_root=test_workspace,
        allowed_files=["src/a.py", ".bridle/mocks/mod.json"],
        node_tests=[],
    ).with_readonly_files({".bridle/mocks/mod.json"})

    errors = policy.validate_patch_path(".bridle/mocks/mod.json")
    assert errors
    assert "readonly" in errors[0].lower()


def test_consistency_gate_rejects_undeclared_exposed_symbols(test_workspace: Path) -> None:
    store = ProjectPlanStore(test_workspace, project_id="consistency")
    store.initialize()
    store.declare_module_interface(
        from_module="consumer",
        to_module="provider",
        symbol="fetch",
        signature={"params": []},
        mock={"file_path": ".bridle/mocks/fetch.json", "returns": {}},
    )

    with pytest.raises(ConflictError) as error:
        store.verify_node("consumer", exposed_symbols={"fetch", "secret_leak"}, has_red=True, has_green=True)
    assert error.value.api_error.code == CONSISTENCY_GATE_ERROR


def test_consistency_gate_rejects_exposed_symbols_when_no_interface_map(test_workspace: Path) -> None:
    store = ProjectPlanStore(test_workspace, project_id="consistency-empty")
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('n-empty', 'module', 'n', 'g', '{}', 'verifying')"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ConflictError) as error:
        store.verify_node("n-empty", exposed_symbols={"anything"}, has_red=True, has_green=True)
    assert error.value.api_error.code == CONSISTENCY_GATE_ERROR

    connection = sqlite3.connect(store.database_path)
    try:
        status = connection.execute(
            "SELECT status FROM plan_nodes WHERE id = 'n-empty'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert status == "failed"


def test_tdd_gate_rejects_red_without_green(test_workspace: Path) -> None:
    store = ProjectPlanStore(test_workspace, project_id="tdd-red-only")
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('n-red', 'module', 'n', 'g', '{}', 'verifying')"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ConflictError) as error:
        store.verify_node("n-red", exposed_symbols=set(), has_red=True, has_green=False)
    assert error.value.api_error.code == TDD_GATE_ERROR

    connection = sqlite3.connect(store.database_path)
    try:
        status = connection.execute(
            "SELECT status FROM plan_nodes WHERE id = 'n-red'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert status == "failed"


def test_tdd_gate_rejects_without_red(test_workspace: Path) -> None:
    store = ProjectPlanStore(test_workspace, project_id="tdd-gate")
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('n1', 'module', 'n', 'g', '{}', 'verifying')"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ConflictError) as error:
        store.verify_node("n1", exposed_symbols=set(), has_red=False, has_green=False)
    assert error.value.api_error.code == TDD_GATE_ERROR


def test_dispatch_and_complete_node_flow(test_workspace: Path) -> None:
    _write(test_workspace, "svc.py", "def run():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="dispatch-flow")
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('svc', 'module', 'svc', 'run service', ?, 'ready')",
            (json.dumps({"files": ["svc.py"]}),),
        )
        connection.commit()
    finally:
        connection.close()

    dispatched = store.dispatch_child_agent("svc", target_role="executing")
    assert dispatched["status"] == "executing"

    node = store.verify_node("svc", exposed_symbols=set(), has_red=True, has_green=True)
    assert node["status"] == "completed"


def test_external_code_change_marks_drifted_node(test_workspace: Path) -> None:
    _write(test_workspace, "node.py", "def run():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="drift")
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('n-drift', 'module', 'n', 'g', ?, 'ready')",
            (json.dumps({"files": ["node.py", "missing.py"]}),),
        )
        connection.commit()
    finally:
        connection.close()

    _write(test_workspace, "node.py", "def run():\n    return 2\n")
    store.refresh_code_paths(["node.py"])

    connection = sqlite3.connect(store.database_path)
    try:
        status = connection.execute(
            "SELECT status FROM plan_nodes WHERE id = 'n-drift'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert status == "drifted"
