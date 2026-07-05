"""Task 1.1 — tree-sitter symbol + literal-import indexing contract tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from bridle.features.project_map.indexer.treesitter_indexer import (
    IndexResult,
    TreeSitterIndexer,
)
from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.workspace.overview_service import WorkspaceOverviewService

pytestmark = pytest.mark.usefixtures("test_workspace")


class _RecordingFacade:
    """Capture emitted events; the indexer's degrade path should land here as a log."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any] | None]] = []

    def info_event(self, action: str, status: str, *, detail: dict | None = None, **_: Any) -> None:
        self.events.append((action, status, detail))

    def warn_event(self, action: str, status: str, *, detail: dict | None = None, **_: Any) -> None:
        self.events.append((action, status, detail))

    def error_event(self, action: str, status: str, *, detail: dict | None = None, **_: Any) -> None:
        self.events.append((action, status, detail))


def _write(workspace: Path, rel: str, content: str) -> None:
    """Write one source file under the workspace, creating parent dirs."""
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _file_id(rel: str) -> str:
    return WorkspaceOverviewService._entity_id("file", rel)


def test_extracts_python_symbols_and_literal_import_edge(test_workspace: Path) -> None:
    """Parse two python files; output has function/class/method symbols and an a->b import edge."""
    _write(
        test_workspace,
        "a.py",
        "from b import f\n\n\ndef g():\n    return f()\n\n\nclass C:\n    def m(self):\n        return 1\n",
    )
    _write(test_workspace, "b.py", "def f():\n    return 0\n")

    result = TreeSitterIndexer().index_workspace(test_workspace, test_dirs=set())

    assert isinstance(result, IndexResult)
    by_name = {(entity["kind"], entity["name"]) for entity in result.symbol_entities}
    assert ("function", "g") in by_name
    assert ("class", "C") in by_name
    assert ("method", "m") in by_name
    assert ("function", "f") in by_name

    g = next(entity for entity in result.symbol_entities if entity["name"] == "g")
    assert g["parent_id"] == _file_id("a.py")
    assert g["path"] == "a.py::g"
    assert g["payload"]["range"]["start_line"] == 4

    klass = next(entity for entity in result.symbol_entities if entity["name"] == "C")
    method = next(entity for entity in result.symbol_entities if entity["name"] == "m")
    assert method["parent_id"] == klass["id"]
    assert method["path"] == "a.py::C.m"

    assert any(
        relation["source_id"] == _file_id("a.py")
        and relation["target_id"] == _file_id("b.py")
        and relation["kind"] == "imports"
        for relation in result.relations
    )


def test_repeat_scan_is_idempotent(test_workspace: Path) -> None:
    """Index the same tree twice; symbol IDs and import edges are byte-for-byte stable."""
    _write(test_workspace, "a.py", "from b import f\n\n\ndef g():\n    return f()\n")
    _write(test_workspace, "b.py", "def f():\n    return 0\n")

    indexer = TreeSitterIndexer()
    first = indexer.index_workspace(test_workspace, test_dirs=set())
    second = indexer.index_workspace(test_workspace, test_dirs=set())

    def entity_key(result: IndexResult) -> list[tuple]:
        return sorted(
            (e["id"], e["path"], e["kind"], e["name"], e["parent_id"]) for e in result.symbol_entities
        )

    def relation_key(result: IndexResult) -> list[tuple]:
        return sorted((r["source_id"], r["target_id"], r["kind"]) for r in result.relations)

    assert entity_key(first) == entity_key(second)
    assert relation_key(first) == relation_key(second)


def test_extracts_typescript_symbols_and_relative_import_edge(test_workspace: Path) -> None:
    """Parse .ts files; output has function/class/method symbols and a resolved relative import edge."""
    _write(
        test_workspace,
        "a.ts",
        'import { f } from "./b";\nimport os from "path";\n'
        "export function g() { return f(); }\n"
        "export class C extends Base { m() { return 1; } }\n",
    )
    _write(test_workspace, "b.ts", "export function f() { return 0; }\n")

    result = TreeSitterIndexer().index_workspace(test_workspace, test_dirs=set())

    names = {(entity["kind"], entity["name"]) for entity in result.symbol_entities}
    assert ("function", "g") in names
    assert ("class", "C") in names
    assert ("method", "m") in names
    assert ("function", "f") in names

    assert any(
        relation["source_id"] == _file_id("a.ts")
        and relation["target_id"] == _file_id("b.ts")
        and relation["kind"] == "imports"
        for relation in result.relations
    )
    # The bare "path" import is external and must NOT yield an edge in this phase.
    assert all(
        relation["target_id"] != WorkspaceOverviewService._entity_id("file", "path")
        for relation in result.relations
    )


def test_broken_file_degrades_to_file_entity_with_log(test_workspace: Path) -> None:
    """A syntactically broken file yields no symbols, is recorded degraded, and logs the event."""
    _write(test_workspace, "bad.py", "def g(:\n    return\nclass : pass\n")
    _write(test_workspace, "ok.py", "def h():\n    return 1\n")

    facade = _RecordingFacade()
    result = TreeSitterIndexer(facade=facade).index_workspace(test_workspace, test_dirs=set())

    assert not any(entity["path"].startswith("bad.py::") for entity in result.symbol_entities)
    assert "bad.py" in result.degraded_paths
    assert any(entity["name"] == "h" for entity in result.symbol_entities)
    assert any(action == "treesitter_parse_degraded" for action, _status, _detail in facade.events)


def test_broken_file_stays_file_entity_after_store_rescan(test_workspace: Path) -> None:
    """Full rescan keeps a broken file as kind='file' in code_entities with no symbol children."""
    _write(test_workspace, "bad.py", "def g(:\n    return\n")
    _write(test_workspace, "ok.py", "def h():\n    return 1\n")

    store = ProjectPlanStore(test_workspace, project_id="degraded-rescan")
    store.initialize()

    connection = sqlite3.connect(store.database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT path, kind FROM code_entities").fetchall()
    finally:
        connection.close()

    by_path = {row["path"]: row["kind"] for row in rows}
    assert by_path["bad.py"] == "file"
    assert not any(path.startswith("bad.py::") for path in by_path)
    assert "ok.py::h" in by_path
