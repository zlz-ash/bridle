"""File watcher triggers incremental refresh after debounced edits."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.project_map.watcher import CodeMapRefreshWatcher


def test_stop_joins_thread_and_clears_registration(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = CodeMapRefreshWatcher()
    monkeypatch.setattr(watcher, "_snapshot", lambda root: {})
    watcher.start(test_workspace, project_id="join-project")

    assert watcher.stop("join-project", timeout_seconds=1.0) is True
    assert watcher.active_project_ids() == ()
    assert watcher.status("join-project") is None


def test_concurrent_start_creates_one_live_registration(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = CodeMapRefreshWatcher()
    monkeypatch.setattr(watcher, "_snapshot", lambda root: {})
    gate = threading.Barrier(9)

    def start() -> None:
        gate.wait()
        watcher.start(test_workspace, project_id="concurrent-project")

    threads = [threading.Thread(target=start) for _ in range(8)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(timeout=2.0)

    status = watcher.status("concurrent-project")
    assert status is not None and status.thread_alive
    assert watcher.active_project_ids() == ("concurrent-project",)
    assert watcher.stop("concurrent-project", timeout_seconds=1.0) is True


def test_stop_timeout_retains_live_registration_for_retry(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = CodeMapRefreshWatcher()
    monkeypatch.setattr(watcher, "_snapshot", lambda root: {})
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()

    def block_cleanup(project_id: str, generation: int) -> None:
        del project_id, generation
        cleanup_entered.set()
        assert allow_cleanup.wait(timeout=2.0)

    monkeypatch.setattr(watcher, "_before_registration_cleanup", block_cleanup)
    watcher.start(test_workspace, project_id="timeout-project")

    assert watcher.stop("timeout-project", timeout_seconds=0.01) is False
    assert cleanup_entered.wait(timeout=1.0)
    assert watcher.active_project_ids() == ("timeout-project",)
    allow_cleanup.set()
    assert watcher.stop("timeout-project", timeout_seconds=1.0) is True
    assert watcher.active_project_ids() == ()


def test_new_generation_waits_for_old_registration_cleanup(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = CodeMapRefreshWatcher()
    monkeypatch.setattr(watcher, "_snapshot", lambda root: {})
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()

    def block_cleanup(project_id: str, generation: int) -> None:
        del project_id, generation
        cleanup_entered.set()
        assert allow_cleanup.wait(timeout=2.0)

    monkeypatch.setattr(watcher, "_before_registration_cleanup", block_cleanup)
    watcher.start(test_workspace, project_id="generation-project")
    first = watcher.status("generation-project")
    assert first is not None
    assert watcher.stop("generation-project", timeout_seconds=0.01) is False
    assert cleanup_entered.wait(timeout=1.0)

    watcher.start(test_workspace, project_id="generation-project")
    overlapping = watcher.status("generation-project")
    assert overlapping is not None
    assert overlapping.generation == first.generation

    allow_cleanup.set()
    assert watcher.stop("generation-project", timeout_seconds=1.0) is True
    watcher.start(test_workspace, project_id="generation-project")
    second = watcher.status("generation-project")
    assert second is not None and second.generation > first.generation
    assert watcher.stop("generation-project", timeout_seconds=1.0) is True


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
