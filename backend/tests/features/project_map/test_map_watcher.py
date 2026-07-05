"""File watcher triggers incremental refresh after debounced edits."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.project_map.watcher import CodeMapRefreshWatcher


@pytest.mark.asyncio
async def test_watcher_refreshes_changed_file(test_workspace: Path) -> None:
    target = test_workspace / "watch"
    target.mkdir()
    (target / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")

    store = ProjectPlanStore(target, project_id="watch-proj")
    store.initialize()

    watcher = CodeMapRefreshWatcher()
    watcher.start(target, project_id="watch-proj")
    try:
        time.sleep(0.3)
        (target / "a.py").write_text(
            "def a():\n    return 1\n\ndef b():\n    return 2\n",
            encoding="utf-8",
        )
        deadline = time.monotonic() + 8.0
        found = False
        while time.monotonic() < deadline:
            entities = store.list_code_entities(limit=200)["items"]
            if any(entity["path"] == "a.py::b" for entity in entities):
                found = True
                break
            time.sleep(0.5)
        assert found, "watcher should refresh new symbol after file edit"
    finally:
        watcher.stop("watch-proj")


def test_watcher_refreshes_after_file_delete(test_workspace: Path) -> None:
    """Deleted source files are debounced into refresh_code_paths and removed from the map."""
    target = test_workspace / "watch-del"
    target.mkdir()
    (target / "drop.py").write_text("def drop():\n    return 1\n", encoding="utf-8")
    (target / "keep.py").write_text("def keep():\n    return 1\n", encoding="utf-8")

    store = ProjectPlanStore(target, project_id="watch-del-proj")
    store.initialize()
    assert any(item["path"].startswith("drop.py") for item in store.list_code_entities(limit=50)["items"])

    watcher = CodeMapRefreshWatcher()
    watcher.start(target, project_id="watch-del-proj")
    try:
        time.sleep(0.3)
        (target / "drop.py").unlink()
        deadline = time.monotonic() + 8.0
        removed = False
        while time.monotonic() < deadline:
            paths = {item["path"].split("::", 1)[0] for item in store.list_code_entities(limit=50)["items"]}
            if "drop.py" not in paths:
                removed = True
                break
            time.sleep(0.5)
        assert removed, "watcher should refresh after file deletion"
        assert "keep.py" in paths
    finally:
        watcher.stop("watch-del-proj")
