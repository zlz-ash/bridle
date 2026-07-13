"""Semantic-map module candidates and mock-backed interface candidates."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bridle.features.project_map.store import ProjectPlanStore


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _insert_execution_node(store: ProjectPlanStore, node_id: str, payload: dict) -> None:
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) VALUES (?, ?, ?, ?, ?, ?)",
            (node_id, "code_change", node_id, node_id, json.dumps(payload), "running"),
        )
        connection.commit()
    finally:
        connection.close()


def test_module_candidates_are_generated_from_structure(test_workspace: Path) -> None:
    _write(test_workspace, "core/lib.py", "def value():\n    return 1\n")
    _write(test_workspace, "svc/api.py", "from core.lib import value\n\n\ndef api():\n    return value()\n")

    store = ProjectPlanStore(test_workspace, project_id="semantic-candidates")
    store.initialize()

    candidates = store.list_module_candidates()["items"]
    by_module = {item["module_id"]: item for item in candidates}
    assert {"core", "svc"}.issubset(by_module)
    assert by_module["svc"]["status"] == "candidate"
    assert by_module["svc"]["is_execution_boundary"] is False
    assert {item["file_path"] for item in by_module["svc"]["files"]} == {"svc/api.py"}

    interfaces = store.list_module_interface_candidates()["items"]
    assert interfaces
    iface = interfaces[0]
    assert iface["from_module"] == "core"
    assert iface["to_module"] == "svc"
    assert iface["mock_file_path"].startswith(".bridle/semantic-map/mocks/")
    assert (test_workspace / Path(*iface["mock_file_path"].split("/"))).is_file()
    assert iface["mock_hash"]


def test_only_confirmed_module_candidates_drive_execution_boundary(test_workspace: Path) -> None:
    _write(test_workspace, "core/lib.py", "def value():\n    return 1\n")
    _write(test_workspace, "svc/api.py", "from core.lib import value\n\n\ndef api():\n    return value()\n")

    store = ProjectPlanStore(test_workspace, project_id="semantic-boundary")
    store.initialize()
    svc = next(item for item in store.list_module_candidates()["items"] if item["module_id"] == "svc")
    _insert_execution_node(store, "exec-svc", {"module_id": "svc", "tests": []})

    unconfirmed = store.module_execution_snapshot("exec-svc")
    assert unconfirmed["error_code"] == "module_boundary_unconfirmed"

    confirmed = store.set_module_candidate_status(svc["id"], status="confirmed")
    assert confirmed["is_execution_boundary"] is True

    snapshot = store.module_execution_snapshot("exec-svc")
    assert snapshot.get("error_code") is None
    assert snapshot["module_id"] == "svc"
    assert {item["path"] for item in snapshot["implementation_entities"]} == {"svc/api.py"}


def test_confirmed_interface_candidate_publishes_mock_for_snapshot(test_workspace: Path) -> None:
    _write(test_workspace, "core/lib.py", "def value():\n    return 1\n")
    _write(test_workspace, "svc/api.py", "from core.lib import value\n\n\ndef api():\n    return value()\n")

    store = ProjectPlanStore(test_workspace, project_id="semantic-interface")
    store.initialize()
    svc = next(item for item in store.list_module_candidates()["items"] if item["module_id"] == "svc")
    store.set_module_candidate_status(svc["id"], status="confirmed")
    iface = store.list_module_interface_candidates()["items"][0]

    confirmed = store.set_module_interface_candidate_status(iface["id"], status="confirmed")
    assert confirmed["status"] == "confirmed"

    _insert_execution_node(store, "exec-svc", {"module_id": "svc", "tests": []})
    snapshot = store.module_execution_snapshot("exec-svc")
    assert snapshot.get("error_code") is None
    assert len(snapshot["interfaces"]) == 1
    assert snapshot["interfaces"][0]["file_path"] == confirmed["mock_file_path"]
    assert snapshot["interfaces"][0]["mock_hash"] == confirmed["mock_hash"]


def test_confirmed_interface_candidate_survives_semantic_refresh(test_workspace: Path) -> None:
    _write(test_workspace, "core/lib.py", "def value():\n    return 1\n")
    _write(test_workspace, "svc/api.py", "from core.lib import value\n\n\ndef api():\n    return value()\n")

    store = ProjectPlanStore(test_workspace, project_id="semantic-interface-refresh")
    store.initialize()
    svc = next(item for item in store.list_module_candidates()["items"] if item["module_id"] == "svc")
    store.set_module_candidate_status(svc["id"], status="confirmed")
    iface = store.list_module_interface_candidates()["items"][0]
    confirmed = store.set_module_interface_candidate_status(iface["id"], status="confirmed")
    _insert_execution_node(store, "exec-svc", {"module_id": "svc", "tests": []})

    before_snapshot = store.module_execution_snapshot("exec-svc")
    store.refresh_semantic_map_candidates()
    refreshed = next(
        item for item in store.list_module_interface_candidates()["items"] if item["id"] == confirmed["id"]
    )
    after_snapshot = store.module_execution_snapshot("exec-svc")

    assert refreshed["status"] == "confirmed"
    assert refreshed["mock_hash"] == confirmed["mock_hash"]
    assert before_snapshot["interfaces"] == after_snapshot["interfaces"]
    assert after_snapshot["interfaces"][0]["mock_hash"] == confirmed["mock_hash"]


def test_rejected_interface_candidate_revokes_snapshot_interface(test_workspace: Path) -> None:
    _write(test_workspace, "core/lib.py", "def value():\n    return 1\n")
    _write(test_workspace, "svc/api.py", "from core.lib import value\n\n\ndef api():\n    return value()\n")

    store = ProjectPlanStore(test_workspace, project_id="semantic-interface-reject")
    store.initialize()
    svc = next(item for item in store.list_module_candidates()["items"] if item["module_id"] == "svc")
    store.set_module_candidate_status(svc["id"], status="confirmed")
    iface = store.list_module_interface_candidates()["items"][0]
    confirmed = store.set_module_interface_candidate_status(iface["id"], status="confirmed")
    mock_path = test_workspace / Path(*confirmed["mock_file_path"].split("/"))
    _insert_execution_node(store, "exec-svc", {"module_id": "svc", "tests": []})

    assert len(store.module_execution_snapshot("exec-svc")["interfaces"]) == 1
    rejected = store.set_module_interface_candidate_status(iface["id"], status="rejected")
    rejected_snapshot = store.module_execution_snapshot("exec-svc")
    assert rejected["status"] == "rejected"
    assert rejected_snapshot["interfaces"] == []
    assert mock_path.is_file()

    store.refresh_semantic_map_candidates()
    refreshed_rejected = next(
        item for item in store.list_module_interface_candidates()["items"] if item["id"] == iface["id"]
    )
    refreshed_artifact = next(
        item for item in store.list_interface_mock_artifacts()["items"] if item["interface_candidate_id"] == iface["id"]
    )
    assert refreshed_rejected["status"] == "rejected"
    assert refreshed_artifact["status"] == "rejected"
    assert store.module_execution_snapshot("exec-svc")["interfaces"] == []

    reconfirmed = store.set_module_interface_candidate_status(iface["id"], status="confirmed")
    reconfirmed_snapshot = store.module_execution_snapshot("exec-svc")
    assert reconfirmed["status"] == "confirmed"
    assert len(reconfirmed_snapshot["interfaces"]) == 1


def test_stale_interface_candidate_does_not_leak_to_snapshot(test_workspace: Path) -> None:
    _write(test_workspace, "core/lib.py", "def value():\n    return 1\n")
    _write(test_workspace, "svc/api.py", "from core.lib import value\n\n\ndef api():\n    return value()\n")

    store = ProjectPlanStore(test_workspace, project_id="semantic-interface-stale")
    store.initialize()
    svc = next(item for item in store.list_module_candidates()["items"] if item["module_id"] == "svc")
    store.set_module_candidate_status(svc["id"], status="confirmed")
    iface = store.list_module_interface_candidates()["items"][0]
    confirmed = store.set_module_interface_candidate_status(iface["id"], status="confirmed")
    _insert_execution_node(store, "exec-svc", {"module_id": "svc", "tests": []})
    assert len(store.module_execution_snapshot("exec-svc")["interfaces"]) == 1

    _write(test_workspace, "svc/api.py", "def api():\n    return 2\n")
    store.refresh_code_paths(["svc/api.py"])
    refreshed_svc = next(item for item in store.list_module_candidates()["items"] if item["module_id"] == "svc")
    store.set_module_candidate_status(refreshed_svc["id"], status="confirmed")
    stale = next(
        item for item in store.list_module_interface_candidates()["items"] if item["id"] == confirmed["id"]
    )
    snapshot = store.module_execution_snapshot("exec-svc")

    assert stale["status"] == "stale"
    assert snapshot["interfaces"] == []
