"""Task 2.3 — runtime feedback into blind spots + bounded reindex."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from bridle.features.project_map.store import ProjectPlanStore

pytestmark = pytest.mark.usefixtures("test_workspace")


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _runtime_spots(store: ProjectPlanStore) -> list[str]:
    connection = sqlite3.connect(store.database_path)
    try:
        rows = connection.execute(
            "SELECT id FROM map_blind_spots WHERE source = 'runtime'"
        ).fetchall()
        return [str(r[0]) for r in rows]
    finally:
        connection.close()


def test_import_error_execution_creates_runtime_blind_spot_and_reindexes(test_workspace: Path) -> None:
    """ImportError summary produces runtime blind spot and triggers refresh_code_paths once."""
    _write(test_workspace, "main.py", "def main():\n    return 1\n")

    store = ProjectPlanStore(test_workspace, project_id="runtime-fb")
    store.initialize()

    with patch.object(store, "refresh_code_paths", wraps=store.refresh_code_paths) as refresh:
        result = store.record_execution_refresh(
            execution_node_id="node-main",
            changed_paths=["main.py"],
            execution_summary="ImportError: No module named 'ghost.mod'",
            test_summary="FAILED",
        )

    refresh.assert_called_once()
    assert _runtime_spots(store)
    assert result["runtime_blind_spots"]
    assert result["reindex_attempts"] == 1


def test_runtime_reindex_stops_after_retry_limit(test_workspace: Path) -> None:
    """Repeated failures on the same path stop after MAX attempts."""
    store = ProjectPlanStore(test_workspace, project_id="runtime-limit")
    store.initialize()

    summary = "ImportError: No module named 'ghost.mod'"
    for _ in range(4):
        result = store.record_execution_refresh(
            execution_node_id="node-main",
            changed_paths=[],
            execution_summary=summary,
            test_summary="FAILED",
        )
    assert result["stopped_reason"] == "reindex_limit_reached"
