"""Task 1.2 — test-folder convention: register test files but never connect edges."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bridle.features.project_map.indexer.treesitter_indexer import classify_is_test
from bridle.features.project_map.store import ProjectPlanStore

pytestmark = pytest.mark.usefixtures("test_workspace")


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _entities(store: ProjectPlanStore) -> list[dict]:
    connection = sqlite3.connect(store.database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT id, path, kind FROM code_entities").fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def _relations(store: ProjectPlanStore) -> list[tuple[str, str, str]]:
    connection = sqlite3.connect(store.database_path)
    try:
        rows = connection.execute(
            "SELECT source_id, target_id, kind FROM code_relations"
        ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]
    finally:
        connection.close()


def test_classify_is_test_default_and_explicit_override() -> None:
    """The pure classifier: default tests/ convention, with explicit declarations overriding it."""
    # Default convention: a path segment named tests.
    assert classify_is_test("mod/tests/test_x.py", set()) is True
    assert classify_is_test("mod/src/x.py", set()) is False

    # Explicit declaration makes a custom dir a test dir.
    assert classify_is_test("pkg/spec/test_y.py", {"pkg/spec"}) is True
    # Within a module that declared a custom test_dir, the default tests/ is suppressed.
    assert classify_is_test("pkg/tests/test_z.py", {"pkg/spec"}) is False
    # Other modules keep the default convention.
    assert classify_is_test("other/tests/test_a.py", {"pkg/spec"}) is True


def test_test_files_are_registered_without_any_edges(test_workspace: Path) -> None:
    """Files under tests/ become kind='test' and own no code_relations as source or target."""
    _write(test_workspace, "mod/__init__.py", "")
    _write(test_workspace, "mod/x.py", "def x():\n    return 1\n")
    _write(
        test_workspace,
        "mod/tests/test_x.py",
        "import mod.x\n\n\ndef test_f():\n    assert mod.x.x() == 1\n",
    )

    store = ProjectPlanStore(test_workspace, project_id="test-folder")
    store.initialize()

    entities = _entities(store)
    test_entity = next(entity for entity in entities if entity["path"] == "mod/tests/test_x.py")
    assert test_entity["kind"] == "test"

    # No symbol entities were extracted from the test file.
    assert not any(entity["path"].startswith("mod/tests/test_x.py::") for entity in entities)

    # No edge touches the test entity (not even contains).
    test_id = test_entity["id"]
    assert all(source != test_id and target != test_id for source, target, _ in _relations(store))

    # Production code under the same module still produces its symbol + nothing leaks.
    assert any(entity["path"] == "mod/x.py::x" for entity in entities)


def test_declared_test_dir_metadata_reclassifies_files(test_workspace: Path) -> None:
    """A plan node's payload.test_dir steers classification through a full rescan."""
    _write(test_workspace, "pkg/code.py", "def run():\n    return 1\n")
    _write(test_workspace, "pkg/spec/test_y.py", "def test_y():\n    assert True\n")

    store = ProjectPlanStore(test_workspace, project_id="declared-test-dir")
    store.initialize()

    # Insert a module node declaring its tests live under pkg/spec (no default tests/ here).
    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload) VALUES (?, ?, ?, ?, ?)",
            ("module-pkg", "module", "pkg", "pkg module", json.dumps({"test_dir": "pkg/spec"})),
        )
        connection.commit()
    finally:
        connection.close()

    store.rescan()

    entities = _entities(store)
    spec_entity = next(entity for entity in entities if entity["path"] == "pkg/spec/test_y.py")
    assert spec_entity["kind"] == "test"
    assert all(
        source != spec_entity["id"] and target != spec_entity["id"]
        for source, target, _ in _relations(store)
    )
