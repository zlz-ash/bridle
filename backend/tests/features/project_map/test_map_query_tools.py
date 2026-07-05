"""Task 3 — map query tools, mapping role isolation, semantic annotation validation."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from bridle.agent.runtime.role_policy import RuntimeRolePolicy
from bridle.api.errors import ForbiddenError, ValidationError
from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.workspace.overview_service import WorkspaceOverviewService

pytestmark = pytest.mark.usefixtures("test_workspace")


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _file_hash(workspace: Path, rel: str) -> str:
    return hashlib.sha256((workspace / rel).read_bytes()).hexdigest()


def _open_seed(store: ProjectPlanStore) -> str:
    spots = store.map_blind_spots(status="open", max_nodes=5)["items"]
    assert spots, "fixture should produce at least one open blind spot"
    return spots[0]["id"]


def test_mapping_role_rejects_propose_file_patch() -> None:
    with pytest.raises(ForbiddenError) as error:
        RuntimeRolePolicy.require("mapping", "propose_file_patch")
    assert error.value.api_error.code == "role_capability_denied"


def test_mapping_manifest_allows_annotation_not_code_patch() -> None:
    manifest = RuntimeRolePolicy.manifest("mapping")
    assert manifest["propose_semantic_annotation"]["allowed"] is True
    assert manifest["propose_file_patch"]["allowed"] is False
    assert manifest["read_code_map"]["allowed"] is True


def test_map_query_requires_blind_spot_seed_for_mapping(test_workspace: Path) -> None:
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="map-query")
    store.initialize()

    file_id = WorkspaceOverviewService._entity_id("file", "a.py")
    store.map_neighbors(file_id, max_nodes=5)

    with pytest.raises(ValidationError):
        store.map_blind_spots(require_seed=True)


def test_mapping_rejects_fake_and_resolved_seed(test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(test_workspace, "a.py", "from missing import x\n\ndef g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="mapping-seed")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    seed_id = _open_seed(store)
    file_id = WorkspaceOverviewService._entity_id("file", "a.py")

    with pytest.raises(ValidationError):
        store.map_get_node(file_id, mapping_seed="fake-seed")

    connection = sqlite3.connect(store.database_path)
    try:
        connection.execute(
            "UPDATE map_blind_spots SET status = 'resolved' WHERE id = ?", (seed_id,)
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValidationError):
        store.map_get_node(file_id, mapping_seed=seed_id)


def test_mapping_neighbors_outside_seed_neighborhood_rejected(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(test_workspace, "a.py", "from missing import x\n\ndef g():\n    return 1\n")
    _write(test_workspace, "far.py", "def far():\n    return 2\n")
    store = ProjectPlanStore(test_workspace, project_id="mapping-scope")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    seed_id = _open_seed(store)
    far_id = WorkspaceOverviewService._entity_id("function", "far.py", symbol="far")

    with pytest.raises(ValidationError):
        store.map_neighbors(far_id, max_nodes=5, mapping_seed=seed_id)


def test_mapping_progressive_read_within_seed_neighborhood(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(test_workspace, "a.py", "from missing import x\n\ndef g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="mapping-ok")
    monkeypatch.setattr(store, "_maybe_run_semantic_scan", lambda: None)
    store.initialize()
    seed_id = _open_seed(store)
    fn_id = WorkspaceOverviewService._entity_id("function", "a.py", symbol="g")

    node = store.map_get_node(fn_id, mapping_seed=seed_id)
    assert node["id"] == fn_id
    neighbors = store.map_neighbors(fn_id, max_nodes=10, mapping_seed=seed_id)
    assert all(item["id"] for item in neighbors["items"])


def test_high_confidence_annotation_auto_adopts(test_workspace: Path) -> None:
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="anno-high")
    store.initialize()
    source = WorkspaceOverviewService._entity_id("function", "a.py", symbol="g")

    result = store.propose_semantic_annotation(
        source_id=source,
        summary="handles increment",
        evidence={"line": 1},
        model="test",
        confidence=0.95,
        file_hash=_file_hash(test_workspace, "a.py"),
        risk="low",
    )
    assert result["decision"] == "auto_adopt"
    assert result["status"] == "active"
    assert "objection_id" not in result


def test_annotation_rejects_bad_hash_and_confidence(test_workspace: Path) -> None:
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="anno-bad")
    store.initialize()
    source = WorkspaceOverviewService._entity_id("function", "a.py", symbol="g")

    with pytest.raises(ValidationError):
        store.propose_semantic_annotation(
            source_id=source,
            summary="bad hash",
            evidence={},
            model="test",
            confidence=0.5,
            file_hash="stale",
            risk="low",
        )

    with pytest.raises(ValidationError):
        store.propose_semantic_annotation(
            source_id=source,
            summary="bad confidence",
            evidence={},
            model="test",
            confidence=1.5,
            file_hash=_file_hash(test_workspace, "a.py"),
            risk="low",
        )


def test_low_confidence_annotation_goes_to_objection_queue(test_workspace: Path) -> None:
    _write(test_workspace, "a.py", "def g():\n    return 1\n")
    store = ProjectPlanStore(test_workspace, project_id="anno-low")
    store.initialize()
    source = WorkspaceOverviewService._entity_id("function", "a.py", symbol="g")

    result = store.propose_semantic_annotation(
        source_id=source,
        summary="maybe side effect",
        evidence={"guess": True},
        model="test",
        confidence=0.5,
        file_hash=_file_hash(test_workspace, "a.py"),
        risk="high",
    )
    assert result["decision"] == "objection"
    assert result["status"] == "pending"
    assert result["objection_id"]

    connection = sqlite3.connect(store.database_path)
    try:
        pending = connection.execute(
            "SELECT COUNT(*) FROM map_objections WHERE status = 'pending'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert int(pending) >= 1
