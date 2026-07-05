"""Contract tests for the project-local SQLite plan map."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from bridle.api.errors import ConflictError
from bridle.features.project_map.patch_schemas import PlanPatchSchema
from bridle.features.project_map.store import ProjectPlanStore

pytestmark = pytest.mark.usefixtures("test_workspace")


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _node(
    node_id: str,
    *,
    title: str | None = None,
    parent_id: str | None = None,
    order: int = 0,
    depends_on: list[str] | None = None,
) -> dict:
    """Build one valid patch node; inputs customize hierarchy/deps, output is schema-ready data."""
    return {
        "id": node_id,
        "title": title or node_id.title(),
        "goal": f"Complete {node_id}",
        "node_type": "code_change",
        "parent_id": parent_id,
        "order": order,
        "depends_on": depends_on or [],
    }


def test_initialize_scans_real_workspace_and_keeps_stable_entity_ids(test_workspace: Path) -> None:
    """Initialize from real files; input is a workspace, output is a stable filtered code map."""
    (test_workspace / "src").mkdir()
    (test_workspace / "src" / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (test_workspace / "node_modules").mkdir()
    (test_workspace / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
    (test_workspace / "dist").mkdir()
    (test_workspace / "dist" / "ignored.js").write_text("ignored", encoding="utf-8")

    store = ProjectPlanStore(test_workspace, project_id="project-1")
    first = store.initialize()
    first_entities = store.list_code_entities(limit=100)["items"]
    second = store.initialize()
    second_entities = store.list_code_entities(limit=100)["items"]

    assert store.database_path == test_workspace / ".bridle" / "plan.db"
    assert store.database_path.is_file()
    assert first["scan_status"] == "ready"
    assert first["can_chat"] is True
    assert first["can_edit_plan"] is True
    assert second["created"] is False
    assert [(item["id"], item["path"]) for item in first_entities] == [
        (item["id"], item["path"]) for item in second_entities
    ]
    paths = {item["path"] for item in first_entities}
    assert "src" in paths
    assert "src/main.py" in paths
    assert not any(path.startswith("node_modules") or path.startswith("dist") for path in paths)


def test_rescan_leaves_semantic_pending_until_explicit_completion(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structure rescan alone must not mark semantic scan completed."""
    (test_workspace / "src").mkdir()
    (test_workspace / "src" / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id="semantic-state")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    store.rescan()
    status = store.semantic_scan_status()
    assert status["scan_status"] == "structure_ready"
    assert status["semantic_scan_status"] == "pending"
    assert store.readiness()["can_chat"] is False

    store.run_semantic_scan()
    assert store.readiness()["scan_status"] == "ready"
    assert store.readiness()["can_chat"] is True


def test_run_semantic_scan_failure_sets_failed_state(test_workspace: Path) -> None:
    store = ProjectPlanStore(test_workspace, project_id="semantic-fail")
    store.database_path.parent.mkdir(parents=True, exist_ok=True)
    with store._connect() as connection:
        from bridle.features.project_map import store as store_module
        connection.executescript(store_module._SCHEMA)
        store._initialize_metadata(connection)
        store._migrate_schema(connection)
    store.rescan()

    def boom(_store: ProjectPlanStore) -> None:
        raise RuntimeError("semantic_failed")

    with pytest.raises(RuntimeError):
        store.run_semantic_scan(executor=boom)

    assert store.semantic_scan_status()["semantic_scan_status"] == "failed"
    assert store.readiness()["scan_status"] == "semantic_scanning"


def test_recover_interrupted_semantic_scan_retries_pending(test_workspace: Path) -> None:
    store = ProjectPlanStore(test_workspace, project_id="semantic-recover")
    store.database_path.parent.mkdir(parents=True, exist_ok=True)
    with store._connect() as connection:
        from bridle.features.project_map import store as store_module
        connection.executescript(store_module._SCHEMA)
        store._initialize_metadata(connection)
        store._migrate_schema(connection)
        store._set_map_status(connection, "structure_ready", reason="deterministic_scan_completed")
        store._set_metadata(connection, "semantic_scan_status", "running")
        store._set_metadata(connection, "semantic_scan_run_id", "7")
        store._set_metadata(connection, "semantic_scan_processed", "50")
    store._recover_interrupted_semantic_scan()
    assert store.semantic_scan_status()["semantic_scan_status"] == "pending"
    assert store._metadata("semantic_scan_interrupted") == "1"
    assert store._metadata("semantic_scan_run_id") == "7"
    assert store._metadata("semantic_scan_processed") == "50"


def test_pending_annotation_becomes_active_only_after_acceptance(test_workspace: Path) -> None:
    import hashlib

    (test_workspace / "a.py").write_text("def g():\n    return 1\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id="annotation-arb")
    store.initialize()
    source = store.list_code_entities(limit=20)["items"]
    fn = next(item for item in source if item["path"].endswith("::g"))
    file_hash = hashlib.sha256((test_workspace / "a.py").read_bytes()).hexdigest()

    pending = store.propose_semantic_annotation(
        source_id=fn["id"],
        summary="maybe",
        evidence={"line": 1},
        model="test",
        confidence=0.5,
        file_hash=file_hash,
        risk="high",
    )
    assert pending["status"] == "pending"
    objection_id = pending["objection_id"]

    listed = store.list_semantic_annotations(limit=10)["items"]
    assert listed[0]["status"] == "pending"

    store.resolve_objection(
        objection_id,
        decision="accepted",
        resolution={"summary": "approved"},
        actor="human",
    )
    listed_after = store.list_semantic_annotations(limit=10)["items"]
    assert listed_after[0]["status"] == "active"

    with pytest.raises(ConflictError) as duplicate:
        store.resolve_objection(
            objection_id,
            decision="accepted",
            resolution={"summary": "again"},
            actor="human",
        )
    assert duplicate.value.api_error.code == "objection_already_resolved"


def test_rejected_annotation_stays_non_authoritative(test_workspace: Path) -> None:
    import hashlib

    (test_workspace / "a.py").write_text("def g():\n    return 1\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id="annotation-reject")
    store.initialize()
    fn = next(
        item
        for item in store.list_code_entities(limit=20)["items"]
        if item["path"].endswith("::g")
    )
    file_hash = hashlib.sha256((test_workspace / "a.py").read_bytes()).hexdigest()
    pending = store.propose_semantic_annotation(
        source_id=fn["id"],
        summary="guess",
        evidence={},
        model="test",
        confidence=0.4,
        file_hash=file_hash,
        risk="medium",
    )
    store.resolve_objection(
        pending["objection_id"],
        decision="rejected",
        resolution={"summary": "no"},
        actor="human",
    )
    listed = store.list_semantic_annotations(limit=10)["items"]
    assert listed[0]["status"] == "rejected"


def test_code_entities_pagination_has_no_gaps_or_duplicates_over_2000(test_workspace: Path) -> None:
    """Cursor paging must cover large entity sets without silent truncation."""
    import sqlite3

    store = ProjectPlanStore(test_workspace, project_id="pager")
    store.initialize()
    connection = sqlite3.connect(store.database_path)
    try:
        for index in range(2100):
            connection.execute(
                "INSERT INTO code_entities(id, path, kind, name, parent_id, payload) "
                "VALUES (?, ?, 'file', ?, NULL, '{}')",
                (f"bulk-{index}", f"bulk/file_{index}.py", f"file_{index}.py"),
            )
        connection.commit()
    finally:
        connection.close()

    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    while pages < 20:
        page = store.list_code_entities(cursor=cursor, limit=200)
        for item in page["items"]:
            assert item["id"] not in seen
            seen.add(item["id"])
        pages += 1
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert len(seen) >= 2100


def test_initialize_empty_workspace_creates_valid_empty_map(test_workspace: Path) -> None:
    """Initialize an empty project; input has no source files, output remains an openable empty DB."""
    store = ProjectPlanStore(test_workspace, project_id="empty")

    result = store.initialize()

    assert result["scan_status"] == "ready"
    assert result["entity_count"] == 0
    assert store.overview()["plan_node_count"] == 0
    assert store.overview()["can_chat"] is True


def test_patch_updates_only_target_node_and_records_changes(test_workspace: Path) -> None:
    """Apply local patch input; output changes one row and emits ordered incremental events."""
    store = ProjectPlanStore(test_workspace, project_id="patch")
    store.initialize()
    store.patch(
        PlanPatchSchema(
            add_nodes=[
                _node("root", order=1),
                _node("child", parent_id="root", order=2, depends_on=["root"]),
            ]
        )
    )
    root_before = store.get_node("root")

    store.patch(
        PlanPatchSchema(
            update_nodes=[{"id": "child", "title": "Changed child"}],
        )
    )

    assert store.get_node("child")["title"] == "Changed child"
    assert store.get_node("root") == root_before
    events = store.changes(after_seq=0, limit=20)
    assert [event["change_seq"] for event in events["items"]] == sorted(
        event["change_seq"] for event in events["items"]
    )
    assert events["items"][-1]["entity_id"] == "child"
    assert events["items"][-1]["operation"] == "update"


def test_plan_patch_is_rejected_until_project_map_is_ready(test_workspace: Path) -> None:
    """Gate plan edits; map status input exits with structured readiness rejection."""
    store = ProjectPlanStore(test_workspace, project_id="not-ready")
    store.initialize()
    store.mark_map_status("semantic_scanning", reason="background_refresh")

    with pytest.raises(ConflictError) as error:
        store.patch(PlanPatchSchema(add_nodes=[_node("blocked")]))

    assert error.value.api_error.code == "project_map_not_ready"
    assert error.value.api_error.details["scan_status"] == "semantic_scanning"
    with store._connect() as connection:
        store._set_metadata(connection, "semantic_scan_status", "running")
        store._set_metadata(connection, "semantic_scan_run_id", "99")
    store.mark_semantic_scan_completed(run_id="99")
    assert store.patch(PlanPatchSchema(add_nodes=[_node("allowed")]))["changed_node_ids"] == ["allowed"]


def test_semantic_objection_requires_human_arbitration_before_ready(test_workspace: Path) -> None:
    """Persist AI objection; arbitration input exits by restoring readiness only after resolution."""
    store = ProjectPlanStore(test_workspace, project_id="arbitration")
    store.initialize()
    store.record_semantic_annotation(
        source_id="code-file-1",
        summary="Module handles project loading.",
        evidence={"path": "src/project.py"},
        model="fake-model",
        confidence=0.8,
        file_hash="hash-1",
    )
    objection = store.create_map_objection(
        objection_type="ambiguous_responsibility",
        related_node_ids=["code-file-1"],
        evidence={"reason": "Two modules look similar"},
        suggested_resolution={"action": "keep_annotation"},
    )

    assert store.readiness()["scan_status"] == "needs_arbitration"
    assert store.readiness()["can_edit_plan"] is False
    assert store.list_arbitration_items()["items"][0]["id"] == objection["id"]

    resolved = store.resolve_objection(
        objection["id"],
        decision="accepted",
        resolution={"summary": "Human accepted the AI note."},
        actor="human",
    )

    assert resolved["status"] == "resolved"
    assert store.readiness()["scan_status"] == "ready"
    assert store.list_arbitration_items()["items"] == []


def test_execution_refresh_records_summary_and_only_changed_paths(test_workspace: Path) -> None:
    """Finish execution; changed paths input exits with incremental refresh and audit event."""
    (test_workspace / "src").mkdir()
    keep = test_workspace / "src" / "keep.py"
    added = test_workspace / "src" / "added.py"
    keep.write_text("KEEP = True\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id="execution-refresh")
    store.initialize()
    keep_before = next(
        item for item in store.list_code_entities(limit=100)["items"] if item["path"] == "src/keep.py"
    )
    added.write_text("ADDED = True\n", encoding="utf-8")

    result = store.record_execution_refresh(
        execution_node_id="node-1",
        changed_paths=["src/added.py"],
        execution_summary="Implemented the node.",
        test_summary="pytest passed",
    )

    entities = store.list_code_entities(limit=100)["items"]
    keep_after = next(item for item in entities if item["path"] == "src/keep.py")
    assert result["refreshed_paths"] == ["src/added.py"]
    assert result["execution_summary"] == "Implemented the node."
    assert any(item["path"] == "src/added.py" for item in entities)
    assert keep_after["id"] == keep_before["id"]
    changes = store.changes(after_seq=0, limit=50)["items"]
    assert any(
        event["entity_type"] == "execution_refresh" and event["operation"] == "record"
        for event in changes
    )


def test_running_node_and_its_dependency_semantics_are_immutable(test_workspace: Path) -> None:
    """Guard running state; patch input touching node/edge exits with node_running_immutable."""
    store = ProjectPlanStore(test_workspace, project_id="running")
    store.initialize()
    store.patch(
        PlanPatchSchema(
            add_nodes=[_node("base"), _node("active", depends_on=["base"])],
        )
    )
    store.set_node_status("active", "running")

    with pytest.raises(ConflictError) as update_error:
        store.patch(PlanPatchSchema(update_nodes=[{"id": "active", "title": "No"}]))
    assert update_error.value.api_error.code == "node_running_immutable"

    with pytest.raises(ConflictError) as dependency_error:
        store.patch(
            PlanPatchSchema(
                replace_dependencies=[{"node_id": "active", "depends_on": []}],
            )
        )
    assert dependency_error.value.api_error.code == "node_running_immutable"

    with pytest.raises(ConflictError) as removal_error:
        store.patch(PlanPatchSchema(remove_node_ids=["base"]))
    assert removal_error.value.api_error.code == "node_running_immutable"


def test_progressive_reads_are_bounded_and_cursor_stable(test_workspace: Path) -> None:
    """Read hierarchy incrementally; cursor/limit input returns stable non-overlapping pages."""
    store = ProjectPlanStore(test_workspace, project_id="reads")
    store.initialize()
    store.patch(
        PlanPatchSchema(
            add_nodes=[
                _node("root", order=0),
                *[
                    _node(f"child-{index}", parent_id="root", order=index)
                    for index in range(5)
                ],
            ]
        )
    )

    first = store.children(parent_id="root", limit=2)
    second = store.children(parent_id="root", cursor=first["next_cursor"], limit=2)
    found = store.search("child", limit=3)
    graph = store.subgraph("root", depth=1, limit=10)

    assert [item["id"] for item in first["items"]] == ["child-0", "child-1"]
    assert [item["id"] for item in second["items"]] == ["child-2", "child-3"]
    assert len(found["items"]) == 3
    assert {item["id"] for item in graph["nodes"]} == {
        "root",
        "child-0",
        "child-1",
        "child-2",
        "child-3",
        "child-4",
    }


def test_refresh_code_paths_updates_only_changed_files(test_workspace: Path) -> None:
    """Refresh explicit paths; changed-file input updates add/remove rows without a full workspace scan."""
    (test_workspace / "src").mkdir()
    keep = test_workspace / "src" / "keep.py"
    removed = test_workspace / "src" / "removed.py"
    keep.write_text("KEEP = True\n", encoding="utf-8")
    removed.write_text("REMOVE = True\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id="refresh")
    store.initialize()
    keep_before = next(
        item for item in store.list_code_entities(limit=100)["items"] if item["path"] == "src/keep.py"
    )
    removed.unlink()
    added = test_workspace / "src" / "added.py"
    added.write_text("ADDED = True\n", encoding="utf-8")

    with patch(
        "bridle.features.workspace.overview_service.WorkspaceOverviewService.scan_entities",
        side_effect=AssertionError("full scan is forbidden"),
    ):
        result = store.refresh_code_paths(["src/removed.py", "src/added.py"])

    entities = store.list_code_entities(limit=100)["items"]
    paths = {item["path"] for item in entities}
    keep_after = next(item for item in entities if item["path"] == "src/keep.py")
    assert result == {"refreshed_paths": ["src/added.py", "src/removed.py"]}
    assert "src/removed.py" not in paths
    assert "src/added.py" in paths
    assert keep_after["id"] == keep_before["id"]


def test_payload_fields_use_sqlite_json_set_and_reset_completed_node(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update one JSON field; patch input exits through json_set and resets completed semantics."""
    store = ProjectPlanStore(test_workspace, project_id="project-json-set")
    store.initialize()
    store.patch(PlanPatchSchema(add_nodes=[_node("target")]))
    store.set_node_status("target", "completed")
    statements: list[str] = []
    original_connect = store._connect

    @contextmanager
    def traced_connect():
        """Capture SQLite statements; no input exits as the original managed connection."""
        with original_connect() as connection:
            connection.set_trace_callback(statements.append)
            yield connection

    monkeypatch.setattr(store, "_connect", traced_connect)

    store.patch(PlanPatchSchema(update_nodes=[{"id": "target", "constraints": {"safe": True}}]))

    assert any("json_set" in statement.lower() for statement in statements)
    assert store.get_node("target")["constraints"] == {"safe": True}
    assert store.get_node("target")["status"] == "pending"


def test_start_node_is_an_atomic_status_transition(test_workspace: Path) -> None:
    """Start one node twice; node ID input exits running once then node_not_runnable."""
    store = ProjectPlanStore(test_workspace, project_id="atomic-start")
    store.initialize()
    store.patch(PlanPatchSchema(add_nodes=[_node("target")]))

    first = store.start_node("target")

    assert first["status"] == "running"
    with pytest.raises(ConflictError) as second:
        store.start_node("target")
    assert second.value.api_error.code == "node_not_runnable"


def test_default_semantic_scan_routes_blind_spots_to_arbitration(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default semantic executor routes open blind spots into pending arbitration."""
    (test_workspace / "dyn.py").write_text(
        "from missing_pkg import thing\nimport mod\n\n\ndef run(name):\n    return getattr(mod, name)()\n",
        encoding="utf-8",
    )
    store = ProjectPlanStore(test_workspace, project_id="semantic-route")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()

    result = store.run_semantic_scan()
    assert result["scan_status"] == "needs_arbitration"
    assert len(store.list_arbitration_items()["items"]) >= 1
    assert store._metadata("semantic_scan_routed") not in (None, "0")


def test_default_semantic_scan_completes_ready_without_blind_spots(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero blind spots completes semantic scan and reaches ready."""
    (test_workspace / "main.py").write_text("print('ok')\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id="semantic-zero")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()

    result = store.run_semantic_scan()
    assert result["scan_status"] == "ready"
    assert store._metadata("semantic_scan_processed") == "0"


def test_run_semantic_scan_rejects_structure_failed(test_workspace: Path) -> None:
    """Structure failed blocks semantic scan startup."""
    store = ProjectPlanStore(test_workspace, project_id="struct-fail")
    store.database_path.parent.mkdir(parents=True, exist_ok=True)
    with store._connect() as connection:
        from bridle.features.project_map import store as store_module
        connection.executescript(store_module._SCHEMA)
        store._initialize_metadata(connection)
        store._migrate_schema(connection)
        store._set_map_status(connection, "failed", reason="scan_failed")
        store._set_metadata(connection, "semantic_scan_status", "pending")

    with pytest.raises(ConflictError) as exc:
        store.run_semantic_scan()
    assert exc.value.api_error.code == "semantic_scan_not_allowed"


def test_run_semantic_scan_concurrent_lock_single_winner(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one concurrent semantic scan acquires the running lock."""
    import threading

    (test_workspace / "main.py").write_text("print('ok')\n", encoding="utf-8")
    store = ProjectPlanStore(test_workspace, project_id="semantic-lock")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()

    release = threading.Event()
    results: list[str] = []
    barrier = threading.Barrier(2)

    def slow_executor(_store: ProjectPlanStore) -> None:
        release.wait(timeout=2)

    def runner() -> None:
        barrier.wait()
        try:
            store.run_semantic_scan(executor=slow_executor)
            results.append("ok")
        except ConflictError:
            results.append("conflict")
        finally:
            release.set()

    threads = [threading.Thread(target=runner), threading.Thread(target=runner)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results.count("ok") == 1
    assert results.count("conflict") == 1


def test_semantic_runner_success_does_not_cover_structure_failed(test_workspace: Path) -> None:
    """A late semantic success must not overwrite structure failed."""
    import threading

    store = ProjectPlanStore(test_workspace, project_id="semantic-runner-success-race")
    store.database_path.parent.mkdir(parents=True, exist_ok=True)
    with store._connect() as connection:
        from bridle.features.project_map import store as store_module

        connection.executescript(store_module._SCHEMA)
        store._initialize_metadata(connection)
        store._migrate_schema(connection)
        store._set_map_status(connection, "structure_ready", reason="ready")
        store._set_metadata(connection, "semantic_scan_status", "pending")

    runner_started = threading.Event()
    barrier = threading.Barrier(2)

    def executor(_store: ProjectPlanStore) -> None:
        runner_started.set()
        barrier.wait()

    def mark_failed() -> None:
        runner_started.wait(timeout=5)
        with store._connect() as connection:
            store._set_map_status(connection, "failed", reason="structure_failed")
        barrier.wait()

    thread = threading.Thread(target=lambda: store.run_semantic_scan(executor=executor))
    interrupter = threading.Thread(target=mark_failed)
    thread.start()
    interrupter.start()
    thread.join(timeout=5)
    interrupter.join(timeout=5)

    assert store._metadata("scan_status") == "failed"
    assert store._metadata("semantic_scan_status") == "pending"


def test_semantic_runner_failure_does_not_cover_structure_failed(test_workspace: Path) -> None:
    """A late semantic exception must not overwrite structure failed."""
    import threading

    store = ProjectPlanStore(test_workspace, project_id="semantic-runner-error-race")
    store.database_path.parent.mkdir(parents=True, exist_ok=True)
    with store._connect() as connection:
        from bridle.features.project_map import store as store_module

        connection.executescript(store_module._SCHEMA)
        store._initialize_metadata(connection)
        store._migrate_schema(connection)
        store._set_map_status(connection, "structure_ready", reason="ready")
        store._set_metadata(connection, "semantic_scan_status", "pending")

    runner_started = threading.Event()
    barrier = threading.Barrier(2)

    def executor(_store: ProjectPlanStore) -> None:
        runner_started.set()
        barrier.wait()
        raise RuntimeError("runner_failed")

    def mark_failed() -> None:
        runner_started.wait(timeout=5)
        with store._connect() as connection:
            store._set_map_status(connection, "failed", reason="structure_failed")
        barrier.wait()

    def try_scan() -> None:
        try:
            store.run_semantic_scan(executor=executor)
        except RuntimeError:
            return

    thread = threading.Thread(target=try_scan)
    interrupter = threading.Thread(target=mark_failed)
    thread.start()
    interrupter.start()
    thread.join(timeout=5)
    interrupter.join(timeout=5)

    assert store._metadata("scan_status") == "failed"
    assert store._metadata("semantic_scan_status") == "pending"


def test_semantic_run_id_monotonic_after_rescan(test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Structure rescan must not reuse prior semantic run ids."""
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="run-id-monotonic")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    assert store._try_acquire_semantic_scan_lock()
    first_run_id = store._metadata("semantic_scan_run_id")
    assert first_run_id == "1"
    with store._connect() as connection:
        store._set_metadata(connection, "semantic_scan_status", "pending")
    store.rescan()
    assert store._metadata("semantic_scan_run_id") == ""
    assert store._metadata("semantic_scan_run_seq") == "1"
    assert store._try_acquire_semantic_scan_lock()
    second_run_id = store._metadata("semantic_scan_run_id")
    assert second_run_id == "2"
    assert int(second_run_id) > int(first_run_id)


def test_semantic_runner_does_not_cover_real_rescan(test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An old semantic runner cannot overwrite state after a real structure rescan."""
    import threading

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="semantic-runner-rescan-race")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    old_run_id: str | None = None
    runner_started = threading.Event()
    barrier = threading.Barrier(2)

    def executor(_store: ProjectPlanStore) -> None:
        nonlocal old_run_id
        old_run_id = _store._metadata("semantic_scan_run_id")
        runner_started.set()
        barrier.wait()

    def perform_rescan() -> None:
        runner_started.wait(timeout=5)
        store.rescan()
        barrier.wait()

    thread = threading.Thread(target=lambda: store.run_semantic_scan(executor=executor))
    interrupter = threading.Thread(target=perform_rescan)
    thread.start()
    interrupter.start()
    thread.join(timeout=10)
    interrupter.join(timeout=10)

    assert old_run_id == "1"
    assert store._metadata("semantic_scan_run_id") == ""
    assert int(store._metadata("semantic_scan_run_seq") or "0") >= 1
    assert store._metadata("scan_status") == "structure_ready"
    assert store._metadata("semantic_scan_status") == "pending"


def test_old_semantic_runner_error_does_not_corrupt_new_run_after_rescan(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale runner exception must not mark a new semantic run failed."""
    import threading

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="semantic-runner-rescan-error")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    runner_started = threading.Event()
    barrier = threading.Barrier(2)

    def executor(_store: ProjectPlanStore) -> None:
        runner_started.set()
        barrier.wait()
        raise RuntimeError("stale_runner_failed")

    def perform_rescan_and_start_new_run() -> None:
        runner_started.wait(timeout=5)
        store.rescan()
        assert store._try_acquire_semantic_scan_lock()
        barrier.wait()

    def try_scan() -> None:
        try:
            store.run_semantic_scan(executor=executor)
        except RuntimeError:
            return

    thread = threading.Thread(target=try_scan)
    interrupter = threading.Thread(target=perform_rescan_and_start_new_run)
    thread.start()
    interrupter.start()
    thread.join(timeout=10)
    interrupter.join(timeout=10)

    assert store._metadata("semantic_scan_run_id") == "2"
    assert store._metadata("semantic_scan_status") == "running"
    assert store._metadata("scan_status") == "semantic_scanning"


def _terminal_commit_snapshot(store: ProjectPlanStore) -> dict[str, object]:
    events = store.changes(after_seq=0, limit=1000)
    last_event = events["items"][-1] if events["items"] else None
    return {
        "scan_status": store._metadata("scan_status"),
        "semantic_scan_status": store._metadata("semantic_scan_status"),
        "readiness_reason": store._metadata("readiness_reason"),
        "run_id": store._metadata("semantic_scan_run_id"),
        "change_count": len(events["items"]),
        "last_event_status": last_event["operation"] if last_event else None,
        "last_event_entity": last_event["entity_id"] if last_event else None,
    }


def _seed_semantic_terminal_state(
    store: ProjectPlanStore,
    *,
    run_id: str = "1",
    semantic_status: str = "running",
    scan_status: str = "semantic_scanning",
    reason: str = "semantic_scan_started",
) -> None:
    store.database_path.parent.mkdir(parents=True, exist_ok=True)
    with store._connect() as connection:
        from bridle.features.project_map import store as store_module

        connection.executescript(store_module._SCHEMA)
        store._initialize_metadata(connection)
        store._migrate_schema(connection)
        store._set_map_status(connection, scan_status, reason=reason)
        store._set_metadata(connection, "semantic_scan_status", semantic_status)
        store._set_metadata(connection, "semantic_scan_run_id", run_id)
        store._set_metadata(connection, "semantic_scan_run_seq", run_id)


def test_semantic_terminal_commit_is_all_or_nothing_when_semantic_not_running(
    test_workspace: Path,
) -> None:
    """Completing a run must not write map terminal state when semantic is not running."""
    store = ProjectPlanStore(test_workspace, project_id="terminal-commit-non-running")
    _seed_semantic_terminal_state(
        store,
        semantic_status="pending",
        scan_status="semantic_scanning",
    )

    before = _terminal_commit_snapshot(store)
    with store._connect() as connection:
        committed = store._commit_semantic_run_terminal(
            connection,
            "1",
            semantic_status="completed",
            map_status="ready",
            reason="semantic_scan_completed",
        )
    after = _terminal_commit_snapshot(store)

    assert committed is False
    assert after == before
    assert after["scan_status"] == "semantic_scanning"
    assert after["semantic_scan_status"] == "pending"


def test_semantic_terminal_commit_syncs_completed_states(test_workspace: Path) -> None:
    """Successful completion must persist map and semantic terminal states together."""
    store = ProjectPlanStore(test_workspace, project_id="terminal-commit-completed")
    _seed_semantic_terminal_state(store)

    store.mark_semantic_scan_completed(run_id="1")

    assert store._metadata("scan_status") == "ready"
    assert store._metadata("semantic_scan_status") == "completed"
    assert store._metadata("readiness_reason") == ""


def test_semantic_terminal_commit_syncs_deferred_states(test_workspace: Path) -> None:
    """Deferred completion keeps map scanning while semantic returns to pending."""
    store = ProjectPlanStore(test_workspace, project_id="terminal-commit-deferred")
    _seed_semantic_terminal_state(store)

    with store._connect() as connection:
        committed = store._commit_semantic_run_terminal(
            connection,
            "1",
            semantic_status="pending",
            map_status="semantic_scanning",
            reason="blind_spots_remaining",
        )

    assert committed is True
    assert store._metadata("scan_status") == "semantic_scanning"
    assert store._metadata("semantic_scan_status") == "pending"


def test_semantic_terminal_commit_syncs_failed_states(test_workspace: Path) -> None:
    """Failed completion must keep map scanning and mark semantic failed together."""
    store = ProjectPlanStore(test_workspace, project_id="terminal-commit-failed")
    _seed_semantic_terminal_state(store)

    with store._connect() as connection:
        committed = store._commit_semantic_run_terminal(
            connection,
            "1",
            semantic_status="failed",
            map_status="semantic_scanning",
            reason="RuntimeError",
        )

    assert committed is True
    assert store._metadata("scan_status") == "semantic_scanning"
    assert store._metadata("semantic_scan_status") == "failed"
    assert store._metadata("readiness_reason") == "RuntimeError"


def test_semantic_terminal_commit_rejects_stale_owner_after_concurrent_run_switch(
    test_workspace: Path,
) -> None:
    """A stale terminal commit must lose the CAS race when another connection switches the run token."""
    import sqlite3
    import threading

    store = ProjectPlanStore(test_workspace, project_id="terminal-commit-toctou")
    _seed_semantic_terminal_state(store, run_id="1")
    stale_ready = threading.Event()
    release_stale = threading.Event()
    outcome: dict[str, bool | None] = {"committed": None}

    def stale_commit() -> None:
        connection = sqlite3.connect(store.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        first_update_seen = {"done": False}

        def trace(sql: str) -> None:
            normalized = sql.strip().upper()
            if not first_update_seen["done"] and normalized.startswith("UPDATE METADATA"):
                first_update_seen["done"] = True
                stale_ready.set()
                release_stale.wait(timeout=5)

        connection.set_trace_callback(trace)
        try:
            with connection:
                outcome["committed"] = store._commit_semantic_run_terminal(
                    connection,
                    "1",
                    semantic_status="completed",
                    map_status="ready",
                    reason="semantic_scan_completed",
                )
        finally:
            connection.set_trace_callback(None)
            connection.close()

    def switch_run_token() -> None:
        assert stale_ready.wait(timeout=5)
        with store._connect() as connection:
            store._set_metadata(connection, "semantic_scan_run_id", "2")
            store._set_metadata(connection, "semantic_scan_status", "running")
        release_stale.set()

    stale_thread = threading.Thread(target=stale_commit)
    switch_thread = threading.Thread(target=switch_run_token)
    stale_thread.start()
    switch_thread.start()
    stale_thread.join(timeout=10)
    switch_thread.join(timeout=10)

    assert outcome["committed"] is False
    assert store._metadata("semantic_scan_run_id") == "2"
    assert store._metadata("semantic_scan_status") == "running"
    assert store._metadata("scan_status") == "semantic_scanning"
    events = store.changes(after_seq=0, limit=1000)
    assert not any(event["operation"] == "ready" for event in events["items"])

