"""Task 2.2 — structural blind spots."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bridle.features.project_map.store import ProjectPlanStore

pytestmark = pytest.mark.usefixtures("test_workspace")


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _blind_spots(store: ProjectPlanStore) -> list[dict]:
    connection = sqlite3.connect(store.database_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT kind, file_path, status, source, detail FROM map_blind_spots"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def test_dynamic_dispatch_and_unresolved_import_create_open_blind_spots(test_workspace: Path) -> None:
    """getattr dispatch and unresolvable import each yield one open static blind spot."""
    _write(
        test_workspace,
        "dyn.py",
        "from missing_pkg import thing\nimport mod\n\n\ndef run(name):\n    return getattr(mod, name)()\n",
    )

    store = ProjectPlanStore(test_workspace, project_id="blind-spots")
    store.initialize()

    spots = _blind_spots(store)
    kinds = {s["kind"] for s in spots if s["source"] == "static" and s["status"] in ("open", "routed")}
    assert "dynamic_dispatch" in kinds
    assert "unresolved_ref" in kinds
    unresolved = next(s for s in spots if s["kind"] == "unresolved_ref")
    detail = json.loads(unresolved["detail"])
    assert "missing_pkg" in detail.get("module", "")


def _seed_open_blind_spots(store: ProjectPlanStore, file_path: str, count: int) -> None:
    with store._connect() as connection:
        for index in range(count):
            connection.execute(
                "INSERT INTO map_blind_spots("
                "id, kind, file_path, range, detail, source, status"
                ") VALUES (?, 'unresolved_ref', ?, NULL, '{}', 'static', 'open')",
                (f"seed-blind-{index}", file_path),
            )


def _open_blind_count(store: ProjectPlanStore) -> int:
    connection = sqlite3.connect(store.database_path)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM map_blind_spots WHERE status = 'open'"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_semantic_scan_routes_51_blind_spots_in_one_default_run(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default runner loops batches until all open blind spots are routed."""
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="batch-51")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    _seed_open_blind_spots(store, "a.py", 51)

    store.run_semantic_scan()

    assert _open_blind_count(store) == 0
    assert int(store._metadata("semantic_scan_remaining") or "0") == 0
    assert int(store._metadata("semantic_scan_processed") or "0") == 51
    assert int(store._metadata("semantic_scan_routed") or "0") == 51
    objections = store.list_arbitration_items()["items"]
    assert len(objections) == 51
    assert len({item["id"] for item in objections}) == 51


@pytest.mark.parametrize("blind_count", [100, 101])
def test_semantic_scan_cumulative_metadata_matches_blind_spot_count(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    blind_count: int,
) -> None:
    """Multi-batch runs report full cumulative processed/routed totals."""
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id=f"batch-{blind_count}")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    _seed_open_blind_spots(store, "a.py", blind_count)

    store.run_semantic_scan()

    assert int(store._metadata("semantic_scan_processed") or "0") == blind_count
    assert int(store._metadata("semantic_scan_routed") or "0") == blind_count
    assert len(store.list_arbitration_items()["items"]) == blind_count


def test_semantic_scan_batch_failure_retry_preserves_cumulative_totals(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed middle batch can retry without losing cumulative counters."""
    from bridle.features.project_map.semantic_scan_service import SemanticScanService

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="batch-fail-mid")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    _seed_open_blind_spots(store, "a.py", 55)
    original_run = SemanticScanService.run
    calls = {"count": 0}

    def flaky_run(self, target_store: ProjectPlanStore) -> dict:
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("batch_two_failed")
        return original_run(self, target_store)

    monkeypatch.setattr(SemanticScanService, "run", flaky_run)

    with pytest.raises(RuntimeError):
        store.run_semantic_scan()

    assert int(store._metadata("semantic_scan_processed") or "0") == 50
    store.run_semantic_scan()
    assert int(store._metadata("semantic_scan_processed") or "0") == 55
    assert len(store.list_arbitration_items()["items"]) == 55


def test_semantic_scan_logs_each_batch_progress(test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Batch logs include per-batch and cumulative counters."""
    from .test_treesitter_indexer import _RecordingFacade

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    facade = _RecordingFacade()
    store = ProjectPlanStore(test_workspace, project_id="batch-logs", facade=facade)
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    _seed_open_blind_spots(store, "a.py", 51)
    store.run_semantic_scan()

    batch_logs = [
        detail
        for action, status, detail in facade.events
        if action == "project_map_semantic_scan" and status == "batch" and detail is not None
    ]
    assert len(batch_logs) == 2
    assert batch_logs[-1]["cumulative_processed"] == 51
    completed = next(
        detail
        for action, status, detail in facade.events
        if action == "project_map_semantic_scan" and status == "completed" and detail is not None
    )
    assert completed["cumulative_processed"] == 51


def test_semantic_scan_crash_recovery_preserves_cumulative_totals(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restarting after an interrupted run keeps cumulative processed/routed totals."""
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="batch-crash-recover")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    _seed_open_blind_spots(store, "a.py", 51)
    assert store._try_acquire_semantic_scan_lock()
    batch = store._run_semantic_batch()
    with store._connect() as connection:
        store._set_metadata(connection, "semantic_scan_processed", str(batch["processed"]))
        store._set_metadata(connection, "semantic_scan_routed", str(batch["routed"]))
        store._set_metadata(connection, "semantic_scan_deferred", str(batch["deferred"]))
        store._set_metadata(connection, "semantic_scan_remaining", str(batch["remaining"]))

    restarted = ProjectPlanStore.open_existing(test_workspace)
    restarted.initialize()

    assert int(restarted._metadata("semantic_scan_processed") or "0") == 51
    assert int(restarted._metadata("semantic_scan_routed") or "0") == 51
    assert _open_blind_count(restarted) == 0
    assert len(restarted.list_arbitration_items()["items"]) == 51


def test_semantic_scan_failed_log_includes_cumulative_fields(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure logs expose cumulative progress fields."""
    from .test_treesitter_indexer import _RecordingFacade

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    facade = _RecordingFacade()
    store = ProjectPlanStore(test_workspace, project_id="batch-fail-log", facade=facade)
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    _seed_open_blind_spots(store, "a.py", 55)

    def boom(_store: ProjectPlanStore) -> None:
        raise RuntimeError("semantic_failed")

    with pytest.raises(RuntimeError):
        store.run_semantic_scan(executor=boom)

    failed = next(
        detail
        for action, status, detail in facade.events
        if action == "project_map_semantic_scan" and status == "failed" and detail is not None
    )
    assert failed["error_code"] == "RuntimeError"
    assert failed["cumulative_processed"] == 0
    assert failed["cumulative_routed"] == 0
    assert failed["remaining"] == 55
    assert int(store._metadata("semantic_scan_remaining") or "0") == 55


def test_semantic_scan_failed_log_remaining_matches_open_count_after_partial_batch(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure after the first batch reports remaining open blind spots accurately."""
    from bridle.features.project_map.semantic_scan_service import SemanticScanService

    from .test_treesitter_indexer import _RecordingFacade

    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    facade = _RecordingFacade()
    store = ProjectPlanStore(test_workspace, project_id="batch-fail-remaining", facade=facade)
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    _seed_open_blind_spots(store, "a.py", 55)
    original_run = SemanticScanService.run
    calls = {"count": 0}

    def flaky_run(self, target_store: ProjectPlanStore) -> dict:
        calls["count"] += 1
        if calls["count"] == 1:
            return original_run(self, target_store)
        raise RuntimeError("batch_two_failed")

    monkeypatch.setattr(SemanticScanService, "run", flaky_run)

    with pytest.raises(RuntimeError):
        store.run_semantic_scan()

    failed = next(
        detail
        for action, status, detail in facade.events
        if action == "project_map_semantic_scan" and status == "failed" and detail is not None
    )
    assert failed["remaining"] == 5
    assert int(store._metadata("semantic_scan_remaining") or "0") == 5
    assert _open_blind_count(store) == 5


def test_semantic_scan_failed_batch_retries_without_structure_rescan(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantic failure keeps structure and allows retry from failed state."""
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="batch-retry")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    _seed_open_blind_spots(store, "a.py", 12)

    def boom(_store: ProjectPlanStore) -> None:
        raise RuntimeError("semantic_failed")

    with pytest.raises(RuntimeError):
        store.run_semantic_scan(executor=boom)

    assert store._metadata("semantic_scan_status") == "failed"
    assert store._metadata("scan_status") == "semantic_scanning"

    store.run_semantic_scan()
    assert _open_blind_count(store) == 0
    assert len(store.list_arbitration_items()["items"]) == 12


def test_maybe_run_semantic_scan_continues_from_semantic_scanning(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending semantic work resumes when map is already semantic_scanning."""
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="batch-continue")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    _seed_open_blind_spots(store, "a.py", 55)
    with store._connect() as connection:
        store._set_metadata(connection, "semantic_scan_status", "pending")
        store._set_map_status(connection, "semantic_scanning", reason="blind_spots_remaining")

    monkeypatch.setattr(
        store,
        "_maybe_run_semantic_scan",
        lambda: ProjectPlanStore._maybe_run_semantic_scan(store),
    )
    store._maybe_run_semantic_scan()

    assert _open_blind_count(store) == 0
    assert int(store._metadata("semantic_scan_remaining") or "0") == 0

