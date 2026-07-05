"""Tests for map-driven candidate workspace construction."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from bridle.agent.container.candidate_path_guard import CandidatePathError
from bridle.agent.container.workset import MapInterfaceMock, MapWorksetInput, ModuleWorksetResolver
from bridle.agent.container.workspace import ContainerWorkspaceBuilder


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _minimal_workset(
    test_workspace: Path,
    *,
    module_id: str = "mod-a",
    impl: tuple[str, ...] = (),
    tests: tuple[str, ...] = (),
    test_commands: tuple[str, ...] = (),
) -> MapWorksetInput:
    return MapWorksetInput(
        module_id=module_id,
        node_id="n1",
        implementation_files=impl,
        test_files=tests,
        test_commands=test_commands,
    )


class TestModuleWorksetResolver:
    def test_includes_module_files_tests_and_mocks(self, test_workspace: Path) -> None:
        (test_workspace / "src" / "mod.py").parent.mkdir(parents=True)
        (test_workspace / "src" / "mod.py").write_text("mod\n", encoding="utf-8")
        (test_workspace / "tests" / "test_mod.py").parent.mkdir(parents=True)
        (test_workspace / "tests" / "test_mod.py").write_text("test\n", encoding="utf-8")
        mock_path = test_workspace / "mocks" / "iface.py"
        mock_path.parent.mkdir(parents=True)
        mock_path.write_text("mock\n", encoding="utf-8")
        mock_hash = _sha(mock_path)

        result = ModuleWorksetResolver(test_workspace).resolve(
            MapWorksetInput(
                module_id="mod-a",
                node_id="n1",
                implementation_files=("src/mod.py",),
                test_files=("tests/test_mod.py",),
                test_commands=("python -m pytest tests/test_mod.py -q",),
                interface_mocks=(
                    MapInterfaceMock(
                        interface_id="iface-1",
                        from_module="other",
                        to_module="mod-a",
                        file_path="mocks/iface.py",
                        mock_hash=mock_hash,
                        entity_version=mock_hash,
                    ),
                ),
            )
        )

        assert result.error_code is None
        assert result.write_set == ["src/mod.py", "tests/test_mod.py"]
        assert "mocks/iface.py" in result.readonly_files
        assert "mocks/iface.py" in result.read_set
        assert len(result.entries) == 3

    def test_rejects_missing_map_entity(self, test_workspace: Path) -> None:
        result = ModuleWorksetResolver(test_workspace).resolve(
            _minimal_workset(test_workspace, impl=("src/missing.py",))
        )
        assert result.error_code == "module_boundary_incomplete"

    def test_rejects_mock_hash_mismatch(self, test_workspace: Path) -> None:
        mock_path = test_workspace / "mocks" / "iface.py"
        mock_path.parent.mkdir(parents=True)
        mock_path.write_text("mock\n", encoding="utf-8")
        result = ModuleWorksetResolver(test_workspace).resolve(
            MapWorksetInput(
                module_id="mod-a",
                node_id="n1",
                implementation_files=(),
                test_files=(),
                test_commands=(),
                interface_mocks=(
                    MapInterfaceMock(
                        interface_id="iface-1",
                        from_module="a",
                        to_module="b",
                        file_path="mocks/iface.py",
                        mock_hash="deadbeef",
                        entity_version="deadbeef",
                    ),
                ),
            )
        )
        assert result.error_code == "module_boundary_incomplete"
        assert result.error_detail["reason"] == "mock_hash_mismatch"

    def test_rejects_symlink_escape(self, test_workspace: Path) -> None:
        if os.name == "nt":
            pytest.skip("symlink escape test requires POSIX symlink support")
        outside = test_workspace.parent / "outside-secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        link = test_workspace / "src" / "escape.py"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        result = ModuleWorksetResolver(test_workspace).resolve(
            _minimal_workset(test_workspace, impl=("src/escape.py",))
        )
        assert result.error_code == "module_boundary_incomplete"


class TestContainerWorkspaceBuilderCandidate:
    def test_builds_under_module_candidates_with_manifest(self, test_workspace: Path) -> None:
        impl = test_workspace / "pkg" / "calc.py"
        impl.parent.mkdir(parents=True)
        impl.write_text("def add(a, b): return a + b\n", encoding="utf-8")
        test_file = test_workspace / "tests" / "test_calc.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def test_add(): assert True\n", encoding="utf-8")

        workset = MapWorksetInput(
            module_id="calc",
            node_id="node-calc",
            implementation_files=("pkg/calc.py",),
            test_files=("tests/test_calc.py",),
            test_commands=("python -m pytest tests/test_calc.py -q",),
        )
        result = ContainerWorkspaceBuilder(test_workspace).build_candidate_workspace(
            candidate_id="cand-calc",
            module_id="calc",
            run_id="run-1",
            node_id="node-calc",
            workset=workset,
        )

        expected_root = (
            test_workspace / ".bridle" / "runtime" / "modules" / "calc" / "candidates" / "cand-calc"
        )
        assert result.root == expected_root
        assert (result.project_dir / "pkg" / "calc.py").read_text(encoding="utf-8") == impl.read_text(encoding="utf-8")
        assert (result.baseline_dir / "pkg" / "calc.py").is_file()
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["ready"] is True
        assert manifest["schema"] == "bridle.candidate_workspace/v1"
        assert manifest["write_set"] == ["pkg/calc.py", "tests/test_calc.py"]
        assert all(entry["source"] for entry in manifest["file_entries"])

    def test_unrelated_project_files_not_copied(self, test_workspace: Path) -> None:
        included = test_workspace / "mod" / "only.py"
        included.parent.mkdir(parents=True)
        included.write_text("only\n", encoding="utf-8")
        secret = test_workspace / "other" / "secret.py"
        secret.parent.mkdir(parents=True)
        secret.write_text("secret\n", encoding="utf-8")

        workset = _minimal_workset(test_workspace, module_id="only-mod", impl=("mod/only.py",))
        result = ContainerWorkspaceBuilder(test_workspace).build_candidate_workspace(
            candidate_id="cand-only",
            module_id="only-mod",
            run_id="run-1",
            node_id="n1",
            workset=workset,
        )
        assert (result.project_dir / "mod" / "only.py").is_file()
        assert not (result.project_dir / "other" / "secret.py").exists()

    def test_rejects_paths_outside_workspace(self, test_workspace: Path) -> None:
        builder = ContainerWorkspaceBuilder(test_workspace)
        workset = _minimal_workset(test_workspace, impl=("../escape.py",))
        with pytest.raises(ValueError, match="module_boundary_incomplete"):
            builder.build_candidate_workspace(
                candidate_id="bad",
                module_id="mod",
                run_id="run-1",
                node_id="n1",
                workset=workset,
            )

    def test_idempotent_candidate_root(self, test_workspace: Path) -> None:
        impl = test_workspace / "a.py"
        impl.write_text("a\n", encoding="utf-8")
        workset = _minimal_workset(test_workspace, module_id="m", impl=("a.py",))
        builder = ContainerWorkspaceBuilder(test_workspace)
        first = builder.build_candidate_workspace(
            candidate_id="same-id",
            module_id="m",
            run_id="run-1",
            node_id="n1",
            workset=workset,
        )
        second = builder.build_candidate_workspace(
            candidate_id="same-id",
            module_id="m",
            run_id="run-2",
            node_id="n1",
            workset=workset,
        )
        assert first.root == second.root

    @pytest.mark.parametrize(
        "candidate_id",
        ["../escape", "..\\escape", "C:evil", "evil/evil", ""],
    )
    def test_rejects_unsafe_candidate_id_before_fs_mutation(
        self, test_workspace: Path, candidate_id: str
    ) -> None:
        sentinel = test_workspace.parent / "path-guard-sentinel.txt"
        sentinel.write_text("sentinel\n", encoding="utf-8")
        before = _sha(sentinel)
        impl = test_workspace / "safe.py"
        impl.write_text("safe\n", encoding="utf-8")
        workset = _minimal_workset(test_workspace, module_id="mod", impl=("safe.py",))
        with pytest.raises((CandidatePathError, ValueError)):
            ContainerWorkspaceBuilder(test_workspace).build_candidate_workspace(
                candidate_id=candidate_id,
                module_id="mod",
                run_id="run-1",
                node_id="n1",
                workset=workset,
            )
        assert _sha(sentinel) == before

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics required")
    def test_symlinked_project_dir_does_not_delete_sibling_candidate(self, test_workspace: Path) -> None:
        module_id = "symlink-mod"
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / module_id
        cand_a = module_root / "candidates" / "cand-a"
        cand_b = module_root / "candidates" / "cand-b"
        for root in (cand_a, cand_b):
            (root / "project").mkdir(parents=True)
            (root / "baseline").mkdir(parents=True)
            (root / "output").mkdir(parents=True)
            (root / "diagnostics").mkdir(parents=True)
            (root / "project" / "marker.txt").write_text("b-content\n", encoding="utf-8")

        link_project = cand_a / "project"
        if link_project.exists():
            for child in link_project.iterdir():
                child.unlink()
            link_project.rmdir()
        link_project.symlink_to(cand_b / "project", target_is_directory=True)

        impl = test_workspace / "only.py"
        impl.write_text("only\n", encoding="utf-8")
        workset = _minimal_workset(test_workspace, module_id=module_id, impl=("only.py",))
        with pytest.raises(CandidatePathError):
            ContainerWorkspaceBuilder(test_workspace).build_candidate_workspace(
                candidate_id="cand-a",
                module_id=module_id,
                run_id="run-1",
                node_id="n1",
                workset=workset,
            )
        assert (cand_b / "project" / "marker.txt").read_text(encoding="utf-8") == "b-content\n"
