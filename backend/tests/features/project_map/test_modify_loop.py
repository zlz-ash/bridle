"""Task 5 — module interfaces, dispatch, dual gates, drift."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
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


def test_dispatch_persists_plan_state_and_spawn_fact_atomically(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(test_workspace, "worker.py", "def run():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="spawn-atomic")
    store.initialize()
    with closing(sqlite3.connect(store.database_path)) as connection:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('worker', 'module', 'worker', 'run', ?, 'ready')",
            (json.dumps({"files": ["worker.py"]}),),
        )
        connection.commit()
    before_seq = store.latest_change_seq()

    original_record_change = store._record_change

    def fail_record_change(*args, **kwargs):
        raise RuntimeError("commit_failed")

    monkeypatch.setattr(store, "_record_change", fail_record_change)
    with pytest.raises(RuntimeError, match="commit_failed"):
        store.dispatch_child_agent("worker", target_role="executing")
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute(
            "SELECT status FROM plan_nodes WHERE id='worker'"
        ).fetchone()[0] == "ready"
        assert connection.execute(
            "SELECT COUNT(*) FROM child_spawn_facts WHERE node_id='worker'"
        ).fetchone()[0] == 0
    assert store.latest_change_seq() == before_seq

    monkeypatch.setattr(store, "_record_change", original_record_change)
    dispatched = store.dispatch_child_agent("worker", target_role="executing")
    with closing(sqlite3.connect(store.database_path)) as connection:
        fact = connection.execute(
            "SELECT message_id, target_role FROM child_spawn_facts WHERE node_id='worker'"
        ).fetchone()
    assert dispatched["status"] == "executing"
    assert dispatched["spawn_message_id"] == fact[0]
    assert fact[1] == "executing"
    assert store.latest_change_seq() == before_seq + 1


def test_child_result_updates_plan_and_receipt_atomically_and_idempotently(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(test_workspace, "child.py", "def run():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="child-result-atomic")
    store.initialize()
    with closing(sqlite3.connect(store.database_path)) as connection:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('child', 'module', 'child', 'run', '{}', 'ready')"
        )
        connection.commit()
    spawn = store.dispatch_child_agent("child", target_role="executing")
    before_seq = store.latest_change_seq()

    original_record_change = store._record_change

    def fail_record_change(*args, **kwargs):
        raise RuntimeError("result_commit_failed")

    monkeypatch.setattr(store, "_record_change", fail_record_change)
    with pytest.raises(RuntimeError, match="result_commit_failed"):
        store.apply_child_result(
            message_id=f"child-result-{spawn['spawn_message_id']}",
            node_id="child",
            status="completed",
        )
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute(
            "SELECT status FROM plan_nodes WHERE id='child'"
        ).fetchone()[0] == "executing"
        assert connection.execute(
            "SELECT COUNT(*) FROM child_result_receipts"
        ).fetchone()[0] == 0

    monkeypatch.setattr(store, "_record_change", original_record_change)
    first = store.apply_child_result(
        message_id=f"child-result-{spawn['spawn_message_id']}",
        node_id="child",
        status="completed",
    )
    duplicate = store.apply_child_result(
        message_id=f"child-result-{spawn['spawn_message_id']}",
        node_id="child",
        status="completed",
    )

    assert first == {"node_id": "child", "status": "completed", "applied": True}
    assert duplicate == {"node_id": "child", "status": "completed", "applied": False}
    assert store.get_node("child")["status"] == "completed"
    assert store.latest_change_seq() == before_seq + 1
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute(
            "SELECT message_id, node_id, result_status FROM child_result_receipts"
        ).fetchone() == (
            f"child-result-{spawn['spawn_message_id']}",
            "child",
            "completed",
        )
        assert connection.execute(
            "SELECT status FROM child_spawn_facts WHERE node_id='child'"
        ).fetchone()[0] == "completed"


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
