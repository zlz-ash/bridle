"""Task 1.3 — change-triggered incremental reindex: only the changed file is replaced."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.workspace.overview_service import WorkspaceOverviewService

pytestmark = pytest.mark.usefixtures("test_workspace")


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _entities(store: ProjectPlanStore) -> dict[str, dict]:
    connection = sqlite3.connect(store.database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT id, path, kind, name FROM code_entities").fetchall()
        return {row["path"]: dict(row) for row in rows}
    finally:
        connection.close()


def _relations(store: ProjectPlanStore) -> set[tuple[str, str, str]]:
    connection = sqlite3.connect(store.database_path)
    try:
        rows = connection.execute("SELECT source_id, target_id, kind FROM code_relations").fetchall()
        return {(row[0], row[1], row[2]) for row in rows}
    finally:
        connection.close()


def _file_id(rel: str) -> str:
    return WorkspaceOverviewService._entity_id("file", rel)


def test_incremental_reindex_adds_new_symbol_and_edge_only_for_changed_file(
    test_workspace: Path,
) -> None:
    """Edit one file; new symbol + import edge appear while the untouched file stays byte-stable."""
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    _write(test_workspace, "b.py", "def f():\n    return 0\n")

    store = ProjectPlanStore(test_workspace, project_id="incremental")
    store.initialize()
    before = _entities(store)
    b_symbol_before = before["b.py::f"]

    _write(
        test_workspace,
        "a.py",
        "from b import f\n\n\ndef g():\n    return 1\n\n\ndef g2():\n    return f()\n",
    )
    result = store.refresh_code_paths(["a.py"])

    assert result == {"refreshed_paths": ["a.py"]}
    after = _entities(store)
    assert "a.py::g2" in after, "new function symbol should be indexed"
    assert after["b.py::f"]["id"] == b_symbol_before["id"], "untouched file must not be re-keyed"

    relations = _relations(store)
    assert (_file_id("a.py"), _file_id("b.py"), "imports") in relations

    changes = store.changes(after_seq=0, limit=200)["items"]
    assert any(
        event["operation"] == "refresh" and event["entity_type"] == "code_entity"
        for event in changes
    )


def test_incremental_reindex_drops_stale_edges_and_symbols(test_workspace: Path) -> None:
    """Removing an import and a function deletes the old edge/symbol without orphan residue."""
    _write(test_workspace, "a.py", "from b import f\n\n\ndef g():\n    return f()\n")
    _write(test_workspace, "b.py", "def f():\n    return 0\n")

    store = ProjectPlanStore(test_workspace, project_id="incremental-drop")
    store.initialize()
    assert (_file_id("a.py"), _file_id("b.py"), "imports") in _relations(store)

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store.refresh_code_paths(["a.py"])

    relations = _relations(store)
    assert (_file_id("a.py"), _file_id("b.py"), "imports") not in relations
    # b.py side is fully untouched.
    assert "b.py::f" in _entities(store)


def test_incremental_refresh_preserves_incoming_edges_from_unmodified_caller(
    test_workspace: Path,
) -> None:
    """Editing callee must not drop import/call edges from an untouched caller file."""
    _write(test_workspace, "callee.py", "def helper():\n    return 1\n")
    _write(
        test_workspace,
        "caller.py",
        "from callee import helper\n\n\ndef run():\n    return helper()\n",
    )

    store = ProjectPlanStore(test_workspace, project_id="incoming-edge")
    store.initialize()
    relations_before = _relations(store)
    caller_file = _file_id("caller.py")
    callee_file = _file_id("callee.py")
    assert (caller_file, callee_file, "imports") in relations_before

    _write(
        test_workspace,
        "callee.py",
        "def helper():\n    return 2\n\n\ndef extra():\n    return 3\n",
    )
    store.refresh_code_paths(["callee.py"])

    relations_after = _relations(store)
    assert (caller_file, callee_file, "imports") in relations_after
    assert "caller.py::run" in _entities(store)
    assert "callee.py::extra" in _entities(store)


def test_incremental_refresh_cleans_deleted_file_artifacts_and_marks_drift(
    test_workspace: Path,
) -> None:
    """Deleting a file removes entities, relations, occurrences, symbols, blind spots; plan node drifts."""
    import json
    import sqlite3

    _write(test_workspace, "gone.py", "from missing_pkg import thing\n\n\ndef run():\n    return 1\n")
    _write(test_workspace, "stay.py", "def stay():\n    return 0\n")

    store = ProjectPlanStore(test_workspace, project_id="delete-clean")
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('gone-node', 'module', 'gone', 'g', ?, 'ready')",
            (json.dumps({"files": ["gone.py"]}),),
        )
        connection.commit()
    finally:
        connection.close()

    (test_workspace / "gone.py").unlink()
    store.refresh_code_paths(["gone.py"])

    entities = _entities(store)
    assert "gone.py" not in {path.split("::", 1)[0] for path in entities}
    assert "stay.py" in {path.split("::", 1)[0] for path in entities}

    connection = sqlite3.connect(store.database_path)
    try:
        occ = connection.execute(
            "SELECT COUNT(*) FROM code_occurrences WHERE file_path = 'gone.py'"
        ).fetchone()[0]
        sym = connection.execute(
            "SELECT COUNT(*) FROM code_symbols WHERE moniker GLOB 'gone.py::*'"
        ).fetchone()[0]
        blind = connection.execute(
            "SELECT COUNT(*) FROM map_blind_spots WHERE file_path = 'gone.py'"
        ).fetchone()[0]
        status = connection.execute(
            "SELECT status FROM plan_nodes WHERE id = 'gone-node'"
        ).fetchone()[0]
    finally:
        connection.close()

    assert int(occ) == 0
    assert int(sym) == 0
    assert int(blind) == 0
    assert status == "drifted"


def test_multihop_chain_preserves_grandparent_edge_when_leaf_changes(test_workspace: Path) -> None:
    """C → B → A: editing A must keep both C → B and B → A."""
    _write(test_workspace, "a.py", "def leaf():\n    return 1\n")
    _write(test_workspace, "b.py", "from a import leaf\n\n\ndef mid():\n    return leaf()\n")
    _write(
        test_workspace,
        "c.py",
        "from b import mid\n\n\ndef top():\n    return mid()\n",
    )

    store = ProjectPlanStore(test_workspace, project_id="multihop")
    store.initialize()

    c_file = _file_id("c.py")
    b_file = _file_id("b.py")
    a_file = _file_id("a.py")
    relations_before = _relations(store)
    assert (c_file, b_file, "imports") in relations_before
    assert (b_file, a_file, "imports") in relations_before

    _write(test_workspace, "a.py", "def leaf():\n    return 2\n\n\ndef extra():\n    return 3\n")
    store.refresh_code_paths(["a.py"])

    relations_after = _relations(store)
    assert (c_file, b_file, "imports") in relations_after
    assert (b_file, a_file, "imports") in relations_after
    assert "a.py::extra" in _entities(store)


def test_delete_leaf_preserves_grandparent_chain(test_workspace: Path) -> None:
    """Deleting A removes edges to A but keeps C → B."""
    _write(test_workspace, "a.py", "def leaf():\n    return 1\n")
    _write(test_workspace, "b.py", "from a import leaf\n\n\ndef mid():\n    return leaf()\n")
    _write(test_workspace, "c.py", "from b import mid\n\n\ndef top():\n    return mid()\n")

    store = ProjectPlanStore(test_workspace, project_id="multihop-del")
    store.initialize()

    c_file = _file_id("c.py")
    b_file = _file_id("b.py")
    a_file = _file_id("a.py")

    (test_workspace / "a.py").unlink()
    store.refresh_code_paths(["a.py"])

    relations = _relations(store)
    assert (c_file, b_file, "imports") in relations
    assert not any(target == a_file or source == a_file for source, target, _kind in relations if _kind != "contains")


def test_rename_symbol_drops_stale_call_edge_without_duplicates(test_workspace: Path) -> None:
    """Renaming a symbol removes old call edges and does not duplicate relations."""
    _write(test_workspace, "a.py", "def old():\n    return 1\n")
    _write(test_workspace, "b.py", "from a import old\n\n\ndef use():\n    return old()\n")

    store = ProjectPlanStore(test_workspace, project_id="rename-chain")
    store.initialize()
    b_file = _file_id("b.py")
    a_file = _file_id("a.py")
    assert (b_file, a_file, "imports") in _relations(store)

    _write(test_workspace, "a.py", "def new_name():\n    return 1\n")
    _write(test_workspace, "b.py", "from a import new_name\n\n\ndef use():\n    return new_name()\n")
    store.refresh_code_paths(["a.py", "b.py"])

    relations = _relations(store)
    import_edges = [(s, t) for s, t, k in relations if k == "imports" and s == b_file]
    assert len(import_edges) == 1
    assert import_edges[0][1] == a_file
    assert "a.py::old" not in _entities(store)
    assert "a.py::new_name" in _entities(store)


def _annotations(store: ProjectPlanStore) -> list[dict]:
    connection = sqlite3.connect(store.database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, status, file_hash FROM semantic_annotations"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def test_active_annotation_becomes_stale_after_file_change(test_workspace: Path) -> None:
    """Editing source content invalidates active annotations."""
    import hashlib


    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="ann-stale-active")
    store.initialize()
    fn = next(item for item in _entities(store).values() if item["path"].endswith("::g"))
    file_hash = hashlib.sha256((test_workspace / "a.py").read_bytes()).hexdigest()
    store.propose_semantic_annotation(
        source_id=fn["id"],
        summary="trusted",
        evidence={},
        model="test",
        confidence=0.95,
        file_hash=file_hash,
        risk="low",
    )
    assert _annotations(store)[0]["status"] == "active"

    _write(test_workspace, "a.py", "def g():\n    return 2\n")
    store.refresh_code_paths(["a.py"])
    assert _annotations(store)[0]["status"] == "stale"


def test_pending_annotation_cannot_be_accepted_after_file_change(test_workspace: Path) -> None:
    """Stale pending annotations reject acceptance at arbitration time."""
    import hashlib

    from bridle.api.errors import ConflictError

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="ann-stale-pending")
    store.initialize()
    fn = next(item for item in _entities(store).values() if item["path"].endswith("::g"))
    file_hash = hashlib.sha256((test_workspace / "a.py").read_bytes()).hexdigest()
    pending = store.propose_semantic_annotation(
        source_id=fn["id"],
        summary="maybe",
        evidence={},
        model="test",
        confidence=0.4,
        file_hash=file_hash,
        risk="medium",
    )

    _write(test_workspace, "a.py", "def g():\n    return 99\n")
    store.refresh_code_paths(["a.py"])
    assert _annotations(store)[0]["status"] == "stale"

    with pytest.raises(ConflictError) as exc:
        store.resolve_objection(
            pending["objection_id"],
            decision="accepted",
            resolution={"summary": "approve"},
            actor="human",
        )
    assert exc.value.api_error.code == "objection_already_resolved"


def test_same_content_refresh_preserves_active_annotation(test_workspace: Path) -> None:
    """Rewriting identical bytes must not stale an active annotation."""
    import hashlib

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="ann-same-bytes")
    store.initialize()
    fn = next(item for item in _entities(store).values() if item["path"].endswith("::g"))
    file_hash = hashlib.sha256((test_workspace / "a.py").read_bytes()).hexdigest()
    store.propose_semantic_annotation(
        source_id=fn["id"],
        summary="trusted",
        evidence={},
        model="test",
        confidence=0.95,
        file_hash=file_hash,
        risk="low",
    )
    assert _annotations(store)[0]["status"] == "active"

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store.refresh_code_paths(["a.py"])
    assert _annotations(store)[0]["status"] == "active"


def test_adjacent_file_refresh_preserves_annotation(test_workspace: Path) -> None:
    """Refreshing an unrelated path must not stale annotations on other files."""
    import hashlib

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    _write(test_workspace, "b.py", "def h():\n    return 2\n")
    store = ProjectPlanStore(test_workspace, project_id="ann-adjacent")
    store.initialize()
    fn = next(item for item in _entities(store).values() if item["path"].endswith("::g"))
    file_hash = hashlib.sha256((test_workspace / "a.py").read_bytes()).hexdigest()
    store.propose_semantic_annotation(
        source_id=fn["id"],
        summary="trusted",
        evidence={},
        model="test",
        confidence=0.95,
        file_hash=file_hash,
        risk="low",
    )

    _write(test_workspace, "b.py", "def h():\n    return 99\n")
    store.refresh_code_paths(["b.py"])
    assert _annotations(store)[0]["status"] == "active"


def test_symbol_removal_stales_annotation_once(test_workspace: Path) -> None:
    """Removing a symbol invalidates its annotation and emits one stale event."""
    import hashlib

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="ann-symbol-drop")
    store.initialize()
    fn = next(item for item in _entities(store).values() if item["path"].endswith("::g"))
    annotation_id = store.propose_semantic_annotation(
        source_id=fn["id"],
        summary="trusted",
        evidence={},
        model="test",
        confidence=0.95,
        file_hash=hashlib.sha256((test_workspace / "a.py").read_bytes()).hexdigest(),
        risk="low",
    )["id"]

    _write(test_workspace, "a.py", "def h():\n    return 2\n")
    store.refresh_code_paths(["a.py"])
    assert _annotations(store)[0]["status"] == "stale"

    connection = sqlite3.connect(store.database_path)
    try:
        stale_events = connection.execute(
            "SELECT COUNT(*) FROM change_events "
            "WHERE entity_type = 'semantic_annotation' AND operation = 'stale' AND entity_id = ?",
            (annotation_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert stale_events == 1


