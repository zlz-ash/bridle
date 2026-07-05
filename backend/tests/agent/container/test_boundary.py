"""Tests for module boundary fingerprint semantics."""
from __future__ import annotations

from bridle.agent.container.boundary import compute_boundary_fingerprint


def _entity(entity_id: str, path: str, file_hash: str = "aaa") -> dict:
    return {"entity_id": entity_id, "path": path, "file_hash": file_hash}


class TestBoundaryFingerprint:
    def test_content_hash_change_does_not_change_fingerprint(self) -> None:
        base = compute_boundary_fingerprint(
            module_id="mod-a",
            implementation_entities=[_entity("e1", "src/a.py", "hash-v1")],
            test_entities=[_entity("t1", "tests/test_a.py", "th1")],
            interfaces=[],
            readonly_files=[],
            test_dir=None,
        )
        changed = compute_boundary_fingerprint(
            module_id="mod-a",
            implementation_entities=[_entity("e1", "src/a.py", "hash-v2")],
            test_entities=[_entity("t1", "tests/test_a.py", "th2")],
            interfaces=[],
            readonly_files=[],
            test_dir=None,
        )
        assert base == changed

    def test_entity_set_change_changes_fingerprint(self) -> None:
        first = compute_boundary_fingerprint(
            module_id="mod-a",
            implementation_entities=[_entity("e1", "src/a.py")],
            test_entities=[],
            interfaces=[],
            readonly_files=[],
            test_dir=None,
        )
        second = compute_boundary_fingerprint(
            module_id="mod-a",
            implementation_entities=[_entity("e1", "src/a.py"), _entity("e2", "src/b.py")],
            test_entities=[],
            interfaces=[],
            readonly_files=[],
            test_dir=None,
        )
        assert first != second

    def test_test_dir_and_mock_version_affect_fingerprint(self) -> None:
        without_dir = compute_boundary_fingerprint(
            module_id="mod-a",
            implementation_entities=[_entity("e1", "src/a.py")],
            test_entities=[],
            interfaces=[],
            readonly_files=[],
            test_dir=None,
        )
        with_dir = compute_boundary_fingerprint(
            module_id="mod-a",
            implementation_entities=[_entity("e1", "src/a.py")],
            test_entities=[],
            interfaces=[],
            readonly_files=[],
            test_dir="pkg/spec",
        )
        with_mock = compute_boundary_fingerprint(
            module_id="mod-a",
            implementation_entities=[_entity("e1", "src/a.py")],
            test_entities=[],
            interfaces=[
                {
                    "interface_id": "iface-1",
                    "from_module": "a",
                    "to_module": "b",
                    "mock_hash": "mock-v1",
                }
            ],
            readonly_files=[],
            test_dir=None,
        )
        assert without_dir != with_dir
        assert without_dir != with_mock
