"""Task 2.1 — SCIP / structural precise edges (calls/inherits) + honest degraded reporting."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bridle.features.project_map.indexer.scip_indexer import ScipIndexer
from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.workspace.overview_service import WorkspaceOverviewService

pytestmark = pytest.mark.usefixtures("test_workspace")


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _relations(store: ProjectPlanStore) -> set[tuple[str, str, str]]:
    connection = sqlite3.connect(store.database_path)
    try:
        rows = connection.execute("SELECT source_id, target_id, kind FROM code_relations").fetchall()
        return {(r[0], r[1], r[2]) for r in rows}
    finally:
        connection.close()


def test_cross_file_call_and_inherit_edges(test_workspace: Path) -> None:
    _write(
        test_workspace,
        "base.py",
        "class Base:\n    def run(self):\n        return 1\n",
    )
    _write(
        test_workspace,
        "child.py",
        "from base import Base\n\n\nclass Child(Base):\n    def go(self):\n        return self.run()\n",
    )

    store = ProjectPlanStore(test_workspace, project_id="scip-edges")
    store.initialize()

    relations = _relations(store)
    base_run = WorkspaceOverviewService._entity_id("method", "base.py", symbol="Base.run")
    child_go = WorkspaceOverviewService._entity_id("method", "child.py", symbol="Child.go")
    child_cls = WorkspaceOverviewService._entity_id("class", "child.py", symbol="Child")
    base_cls = WorkspaceOverviewService._entity_id("class", "base.py", symbol="Base")

    assert any(kind == "inherits" and target == base_cls for _s, target, kind in relations)
    assert any(kind == "calls" and s == child_go and target == base_run for s, target, kind in relations)
    assert child_cls


def test_incremental_reindex_replaces_only_target_file_occurrences(test_workspace: Path) -> None:
    _write(test_workspace, "a.py", "def fa():\n    return 1\n")
    _write(test_workspace, "b.py", "def fb():\n    return 2\n")

    store = ProjectPlanStore(test_workspace, project_id="scip-inc")
    store.initialize()
    before_b = store.scip_occurrences_for_file("b.py")
    assert before_b

    _write(test_workspace, "a.py", "def fa():\n    return 1\n\ndef fa2():\n    return fa()\n")
    store.refresh_code_paths(["a.py"])

    after_a = store.scip_occurrences_for_file("a.py")
    after_b = store.scip_occurrences_for_file("b.py")
    assert any("fa2" in row["moniker"] for row in after_a)
    assert {row["moniker"] for row in after_b} == {row["moniker"] for row in before_b}


def test_only_npx_reports_degraded_not_scip_cli(test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(test_workspace, "x.py", "def hx():\n    return 0\n")

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx" if name == "npx" else None)
    indexer = ScipIndexer()
    result = indexer.index_paths(
        test_workspace,
        ["x.py"],
        file_entities=[{"path": "x.py", "kind": "file", "id": "f"}],
        nontest_files={"x.py"},
    )
    assert result.used_scip_cli is False
    assert result.degraded is True
    assert result.symbols


def test_missing_scip_cli_reports_degraded(test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    indexer = ScipIndexer()
    result = indexer.index_paths(test_workspace, [], file_entities=[], nontest_files=set())
    assert result.used_scip_cli is False
    assert result.degraded is True


def test_scip_cli_nonzero_exit_falls_back_degraded(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(test_workspace, "y.py", "def hy():\n    return 1\n")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/scip-python" if name == "scip-python" else None)

    failed = MagicMock()
    failed.returncode = 2
    monkeypatch.setattr(
        "bridle.features.project_map.indexer.scip_indexer.subprocess.run",
        lambda *args, **kwargs: failed,
    )

    indexer = ScipIndexer()
    result = indexer.index_paths(
        test_workspace,
        ["y.py"],
        file_entities=[{"path": "y.py", "kind": "file", "id": "f"}],
        nontest_files={"y.py"},
    )
    assert result.used_scip_cli is False
    assert result.degraded is True


def test_scip_cli_success_without_protobuf_stays_degraded(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(test_workspace, "z.py", "def hz():\n    return 1\n")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/scip-python" if name == "scip-python" else None)

    ok = MagicMock()
    ok.returncode = 0
    monkeypatch.setattr(
        "bridle.features.project_map.indexer.scip_indexer.subprocess.run",
        lambda *args, **kwargs: ok,
    )

    indexer = ScipIndexer()
    result = indexer.index_paths(
        test_workspace,
        ["z.py"],
        file_entities=[{"path": "z.py", "kind": "file", "id": "f"}],
        nontest_files={"z.py"},
    )
    assert result.used_scip_cli is False
    assert result.degraded is True
