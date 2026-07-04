"""Tests for active slot layout, mounts, lease, and safe collect."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from bridle.agent.container.active_slot import (
    ActiveSlotLayout,
    align_rw_mount_roots_for_agent_uid,
    build_slot_mounts,
    collect_active_slot,
    ensure_slot_roots,
    prepare_active_slot,
    read_lease,
    rw_mount_baseline_path,
    rw_mount_trust_marker_path,
    slot_layout,
    slot_mount_identities,
    tree_hashes,
    verify_lease_token,
)
from bridle.agent.container.candidate_path_guard import CandidatePathError
from bridle.agent.container.entrypoint import run_active_slot_task


def test_align_rw_mount_roots_for_agent_uid_invokes_root_chown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_RUN_DOCKER_TESTS", "1")
    monkeypatch.setenv("BRIDLE_WORKER_IMAGE", "bridle-worker:test")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    layout = ActiveSlotLayout(
        slot_root=tmp_path / "slot",
        project=tmp_path / "slot" / "project",
        baseline=tmp_path / "slot" / "baseline",
        mocks=tmp_path / "slot" / "mocks",
        output=tmp_path / "slot" / "output",
        diagnostics=tmp_path / "slot" / "diagnostics",
    )
    for path in (layout.project, layout.output, layout.diagnostics):
        path.mkdir(parents=True, exist_ok=True)
    align_rw_mount_roots_for_agent_uid(layout)
    assert len(calls) == 3
    assert all("chown" in call for call in calls)
    assert all("1000:1000" in call for call in calls)


def _candidate(module_root: Path, candidate_id: str) -> Path:
    root = module_root / "candidates" / candidate_id
    for sub in ("project", "baseline", "mocks", "output", "diagnostics"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _umask(mask: int):
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


class TestStableMountRoots:
    def test_prepare_preserves_mount_source_identities(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "stable"
        cand_a = _candidate(module_root, "cand-a")
        cand_b = _candidate(module_root, "cand-b")
        (cand_a / "project" / "marker.txt").write_text("alpha\n", encoding="utf-8")
        (cand_b / "project" / "marker.txt").write_text("beta\n", encoding="utf-8")

        layout_a = prepare_active_slot(
            module_root,
            cand_a,
            project_root=test_workspace,
            candidate_rel="candidates/cand-a",
            run_id="run-a",
        )
        ids_a = slot_mount_identities(layout_a)
        layout_b = prepare_active_slot(
            module_root,
            cand_b,
            project_root=test_workspace,
            candidate_rel="candidates/cand-b",
            run_id="run-b",
        )
        ids_b = slot_mount_identities(layout_b)
        assert ids_a == ids_b
        assert (layout_b.project / "marker.txt").read_text(encoding="utf-8") == "beta\n"
        for name in ("project", "baseline", "mocks", "output", "diagnostics"):
            assert getattr(layout_b, name).is_dir()

    def test_clear_slot_contents_removes_symlink_without_following(self, test_workspace: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX symlink required")
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "symlink-clear"
        layout = ensure_slot_roots(module_root)
        outside = test_workspace / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        link = layout.project / "linked.txt"
        link.symlink_to(outside)
        from bridle.agent.container.active_slot import clear_slot_contents

        clear_slot_contents(layout)
        assert not link.exists()
        assert outside.read_text(encoding="utf-8") == "secret\n"

    @pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
    def test_clear_slot_contents_unlinks_junction_without_following(self, test_workspace: Path) -> None:
        import subprocess

        from bridle.agent.container.active_slot import clear_slot_contents

        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "junction-clear"
        layout = ensure_slot_roots(module_root)
        ids_before = slot_mount_identities(layout)
        outside_dir = test_workspace / "outside-project"
        outside_dir.mkdir(parents=True)
        sentinel = outside_dir / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        link = layout.project / "linked"
        if link.exists():
            link.unlink()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside_dir)],
            check=True,
            capture_output=True,
        )
        clear_slot_contents(layout)
        assert not link.exists()
        assert sentinel.read_text(encoding="utf-8") == "keep\n"
        assert slot_mount_identities(layout) == ids_before

    def test_prepare_recovers_after_collect_blocked_by_project_link(self, test_workspace: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX symlink required")
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "recover-project"
        cand_a = _candidate(module_root, "cand-a")
        cand_b = _candidate(module_root, "cand-b")
        (cand_a / "project" / "marker.txt").write_text("alpha\n", encoding="utf-8")
        (cand_b / "project" / "marker.txt").write_text("beta\n", encoding="utf-8")
        outside = test_workspace / "outside-recover.txt"
        outside.write_text("secret\n", encoding="utf-8")

        layout_a = prepare_active_slot(
            module_root,
            cand_a,
            project_root=test_workspace,
            candidate_rel="candidates/cand-a",
            run_id="run-a",
        )
        ids_before = slot_mount_identities(layout_a)
        link = layout_a.project / "evil.txt"
        link.symlink_to(outside)
        with pytest.raises(CandidatePathError, match="refuse_symlink_or_reparse"):
            collect_active_slot(module_root, cand_a, project_root=test_workspace)

        layout_b = prepare_active_slot(
            module_root,
            cand_b,
            project_root=test_workspace,
            candidate_rel="candidates/cand-b",
            run_id="run-b",
        )
        assert slot_mount_identities(layout_b) == ids_before
        assert not link.exists()
        assert outside.read_text(encoding="utf-8") == "secret\n"
        assert (layout_b.project / "marker.txt").read_text(encoding="utf-8") == "beta\n"

    def test_prepare_recovers_after_collect_blocked_by_output_link(self, test_workspace: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX symlink required")
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "recover-output"
        cand_a = _candidate(module_root, "cand-a")
        cand_b = _candidate(module_root, "cand-b")
        (cand_b / "project" / "marker.txt").write_text("beta\n", encoding="utf-8")
        outside = test_workspace / "outside-output.txt"
        outside.write_text("secret\n", encoding="utf-8")

        layout_a = prepare_active_slot(
            module_root,
            cand_a,
            project_root=test_workspace,
            candidate_rel="candidates/cand-a",
            run_id="run-a",
        )
        link = layout_a.output / "escape.txt"
        link.symlink_to(outside)
        with pytest.raises(CandidatePathError, match="refuse_symlink_or_reparse"):
            collect_active_slot(module_root, cand_a, project_root=test_workspace)

        layout_b = prepare_active_slot(
            module_root,
            cand_b,
            project_root=test_workspace,
            candidate_rel="candidates/cand-b",
            run_id="run-b",
        )
        assert not link.exists()
        assert (layout_b.project / "marker.txt").read_text(encoding="utf-8") == "beta\n"


class TestMountRootLinkRejection:
    def _replace_mount_root_with_symlink(self, layout, name: str, target: Path) -> None:
        import shutil

        root = getattr(layout, name)
        if root.is_symlink() or root.is_dir():
            if root.is_symlink():
                root.unlink()
            else:
                shutil.rmtree(root)
        root.symlink_to(target, target_is_directory=True)

    def test_prepare_rejects_project_root_symlink(self, test_workspace: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX directory symlink required")
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "root-link-project"
        cand = _candidate(module_root, "cand-a")
        (cand / "project" / "marker.txt").write_text("alpha\n", encoding="utf-8")
        outside = test_workspace / "outside-project-root"
        outside.mkdir(parents=True)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        (outside / "nested").mkdir()
        (outside / "nested" / "file.txt").write_text("nested\n", encoding="utf-8")

        layout = ensure_slot_roots(module_root)
        self._replace_mount_root_with_symlink(layout, "project", outside)
        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-a",
                run_id="run-a",
            )
        assert exc_info.value.error_code == "active_slot_root_link"
        assert sentinel.read_text(encoding="utf-8") == "keep\n"
        assert (outside / "nested" / "file.txt").read_text(encoding="utf-8") == "nested\n"

    def test_prepare_rejects_output_root_symlink(self, test_workspace: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX directory symlink required")
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "root-link-output"
        cand = _candidate(module_root, "cand-a")
        outside = test_workspace / "outside-output-root"
        outside.mkdir(parents=True)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")

        layout = ensure_slot_roots(module_root)
        self._replace_mount_root_with_symlink(layout, "output", outside)
        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-a",
                run_id="run-a",
            )
        assert exc_info.value.error_code == "active_slot_root_link"
        assert sentinel.read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
    def test_prepare_rejects_project_root_junction(self, test_workspace: Path) -> None:
        import subprocess

        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "root-junction-project"
        cand = _candidate(module_root, "cand-a")
        outside = test_workspace / "outside-junction-root"
        outside.mkdir(parents=True)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")

        layout = ensure_slot_roots(module_root)
        root = layout.project
        if root.exists():
            if root.is_dir() and not layout.project.is_symlink():
                import shutil

                shutil.rmtree(root)
            else:
                root.unlink()
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(root), str(outside)],
            check=True,
            capture_output=True,
        )
        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-a",
                run_id="run-a",
            )
        assert exc_info.value.error_code == "active_slot_root_link"
        assert sentinel.read_text(encoding="utf-8") == "keep\n"


class TestActiveSlotParentAndDanglingRejection:
    def _outside_slot_tree(self, base: Path) -> Path:
        for name in ("project", "baseline", "mocks", "output", "diagnostics"):
            (base / name).mkdir(parents=True, exist_ok=True)
        nested = base / "project" / "nested"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        (base / "project" / "marker.txt").write_text("outside\n", encoding="utf-8")
        return base

    def test_prepare_rejects_active_parent_symlink(self, test_workspace: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX directory symlink required")
        import shutil

        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "active-parent-link"
        cand = _candidate(module_root, "cand-a")
        outside = self._outside_slot_tree(test_workspace / "outside-active-tree")
        module_root.mkdir(parents=True, exist_ok=True)
        slot = module_root / "_active"
        slot.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(slot)
        slot.symlink_to(outside, target_is_directory=True)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-a",
                run_id="run-a",
            )
        assert exc_info.value.error_code == "active_slot_root_link"
        assert (outside / "project" / "nested" / "sentinel.txt").read_text(encoding="utf-8") == "keep\n"
        assert (outside / "project" / "marker.txt").read_text(encoding="utf-8") == "outside\n"

    @pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
    def test_prepare_rejects_active_parent_junction(self, test_workspace: Path) -> None:
        import subprocess

        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "active-parent-junction"
        cand = _candidate(module_root, "cand-a")
        outside = self._outside_slot_tree(test_workspace / "outside-active-junction")
        module_root.mkdir(parents=True, exist_ok=True)
        slot = module_root / "_active"
        if slot.exists():
            import shutil

            shutil.rmtree(slot)
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(slot), str(outside)],
            check=True,
            capture_output=True,
        )

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-a",
                run_id="run-a",
            )
        assert exc_info.value.error_code == "active_slot_root_link"
        assert (outside / "project" / "nested" / "sentinel.txt").read_text(encoding="utf-8") == "keep\n"

    @pytest.mark.parametrize("root_name", ["_active", "project", "output"])
    def test_prepare_rejects_dangling_directory_symlink(self, test_workspace: Path, root_name: str) -> None:
        if os.name == "nt":
            pytest.skip("POSIX dangling symlink required")
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / f"dangling-{root_name}"
        cand = _candidate(module_root, "cand-a")
        layout = ensure_slot_roots(module_root)
        if root_name == "_active":
            target = module_root / "_active"
            import shutil

            shutil.rmtree(target)
            target.symlink_to(test_workspace / "missing-active-target", target_is_directory=True)
        else:
            root = getattr(layout, root_name)
            import shutil

            shutil.rmtree(root)
            root.symlink_to(test_workspace / f"missing-{root_name}-target", target_is_directory=True)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-a",
                run_id="run-a",
            )
        assert exc_info.value.error_code == "active_slot_root_link"
        assert not isinstance(exc_info.value.__cause__, FileExistsError)


class TestActiveSlotMounts:
    def test_build_slot_mounts_split_readonly(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "m1"
        cand = _candidate(module_root, "c1")
        (cand / "project" / "tests").mkdir(parents=True)
        (cand / "project" / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n")
        (cand / "baseline" / "tests").mkdir(parents=True)
        (cand / "baseline" / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n")
        (cand / "mocks" / "iface.py").write_text("mock\n")

        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/c1",
            run_id="run-1",
        )
        mounts = build_slot_mounts(layout)
        by_target = {m.target: m for m in mounts}
        assert by_target["/workspace/project"].readonly is False
        assert by_target["/workspace/output"].readonly is False
        assert by_target["/workspace/diagnostics"].readonly is False
        assert by_target["/workspace/baseline"].readonly is True
        assert by_target["/workspace/mocks"].readonly is True

    def test_lease_written_and_verified(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "m2"
        cand = _candidate(module_root, "c2")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/c2",
            run_id="run-lease",
        )
        lease = read_lease(layout)
        assert lease is not None
        assert lease.candidate_rel == "candidates/c2"
        assert lease.run_id == "run-lease"
        verify_lease_token(layout, token=lease.token)

    def test_collect_rejects_symlink_in_output(self, test_workspace: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX symlink required")
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "m3"
        cand = _candidate(module_root, "c3")
        outside = test_workspace / "outside-secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/c3",
            run_id="run-3",
        )
        link = layout.output / "escape.txt"
        link.symlink_to(outside)
        with pytest.raises(CandidatePathError, match="refuse_symlink_or_reparse"):
            collect_active_slot(module_root, cand, project_root=test_workspace)


class TestEntrypointBaselineGuard:
    def test_baseline_tamper_fails_closed(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "m4"
        cand = _candidate(module_root, "c4")
        tests = cand / "project" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
        (cand / "baseline" / "tests").mkdir(parents=True)
        (cand / "baseline" / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
        approved_cmd = "python -m pytest tests/test_ok.py -q"
        baseline_hashes = tree_hashes(cand / "baseline")
        mock_hashes = tree_hashes(cand / "mocks")
        (cand / "diagnostics" / "test-request.json").write_text(
            json.dumps(
                {
                    "schema": "bridle.container_test_request/v1",
                    "commands": [
                        {
                            "command_id": "cmd-1",
                            "argv": approved_cmd.split(),
                            "raw_command": approved_cmd,
                        }
                    ],
                    "write_set": ["tests/test_ok.py"],
                    "protected_hashes": {"baseline": baseline_hashes, "mocks": mock_hashes},
                }
            ),
            encoding="utf-8",
        )
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/c4",
            run_id="run-4",
        )
        baseline_file = layout.baseline / "tests" / "test_ok.py"
        baseline_file.write_text("def test_ok(): assert False\n", encoding="utf-8")
        exit_code = run_active_slot_task(layout.slot_root)
        manifest = json.loads((layout.output / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == 5
        assert manifest["error_code"] == "baseline_or_mock_tampered"

    def test_runtime_baseline_write_triggers_post_exec_guard(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "m5"
        cand = _candidate(module_root, "c5")
        tests = cand / "project" / "tests"
        tests.mkdir(parents=True)
        (tests / "test_tamper.py").write_text(
            """
from pathlib import Path

def test_attempt_baseline_write():
    target = Path("../baseline/tests/evil.py")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("tampered\\n", encoding="utf-8")
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (cand / "baseline" / "tests").mkdir(parents=True)
        (cand / "baseline" / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
        approved_cmd = "python -m pytest tests/test_tamper.py -q"
        baseline_hashes = tree_hashes(cand / "baseline")
        mock_hashes = tree_hashes(cand / "mocks")
        (cand / "diagnostics" / "test-request.json").write_text(
            json.dumps(
                {
                    "schema": "bridle.container_test_request/v1",
                    "commands": [
                        {
                            "command_id": "cmd-1",
                            "argv": approved_cmd.split(),
                            "raw_command": approved_cmd,
                        }
                    ],
                    "write_set": ["tests/test_tamper.py"],
                    "protected_hashes": {"baseline": baseline_hashes, "mocks": mock_hashes},
                }
            ),
            encoding="utf-8",
        )
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/c5",
            run_id="run-5",
        )
        exit_code = run_active_slot_task(layout.slot_root)
        manifest = json.loads((layout.output / "manifest.json").read_text(encoding="utf-8"))
        assert exit_code == 5
        assert manifest["error_code"] == "baseline_or_mock_tampered"
        assert tree_hashes(cand / "baseline") == baseline_hashes


class TestMountRootPermissionRecovery:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    @pytest.mark.parametrize(
        ("umask_value", "initial_mode"),
        [(0o077, 0o700), (0o027, 0o750), (0o007, 0o770)],
    )
    def test_prepare_restores_to_trusted_baseline_mode(
        self,
        test_workspace: Path,
        umask_value: int,
        initial_mode: int,
    ) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / f"perm-mode-{initial_mode:o}"
        with _umask(umask_value):
            ensure_slot_roots(module_root)

        cand_a = _candidate(module_root, "cand-a")
        cand_b = _candidate(module_root, "cand-b")
        (cand_a / "project" / "marker.txt").write_text("alpha\n", encoding="utf-8")
        (cand_b / "project" / "marker.txt").write_text("beta\n", encoding="utf-8")

        layout_a = prepare_active_slot(
            module_root,
            cand_a,
            project_root=test_workspace,
            candidate_rel="candidates/cand-a",
            run_id="run-a",
        )
        ids_a = slot_mount_identities(layout_a)
        for root_name in ("project", "output", "diagnostics"):
            os.chmod(getattr(layout_a, root_name), 0)

        layout_b = prepare_active_slot(
            module_root,
            cand_b,
            project_root=test_workspace,
            candidate_rel="candidates/cand-b",
            run_id="run-b",
        )
        ids_b = slot_mount_identities(layout_b)
        assert ids_a == ids_b
        assert (layout_b.project / "marker.txt").read_text(encoding="utf-8") == "beta\n"
        for root_name in ("project", "output", "diagnostics"):
            mode = os.stat(getattr(layout_b, root_name)).st_mode & 0o777
            assert mode == initial_mode, f"{root_name} mode={oct(mode)} expected {oct(initial_mode)}"
            assert mode & stat.S_IRGRP == (initial_mode & stat.S_IRGRP)
            assert mode & stat.S_IROTH == (initial_mode & stat.S_IROTH)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_prepare_restores_chmod_poisoned_rw_roots(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-recover"
        cand_a = _candidate(module_root, "cand-a")
        cand_b = _candidate(module_root, "cand-b")
        (cand_a / "project" / "marker.txt").write_text("alpha\n", encoding="utf-8")
        (cand_b / "project" / "marker.txt").write_text("beta\n", encoding="utf-8")

        layout_a = prepare_active_slot(
            module_root,
            cand_a,
            project_root=test_workspace,
            candidate_rel="candidates/cand-a",
            run_id="run-a",
        )
        ids_a = slot_mount_identities(layout_a)
        trusted_modes = {
            root_name: os.stat(getattr(layout_a, root_name)).st_mode & 0o777
            for root_name in ("project", "output", "diagnostics")
        }
        for root_name in ("project", "output", "diagnostics"):
            os.chmod(getattr(layout_a, root_name), 0)

        layout_b = prepare_active_slot(
            module_root,
            cand_b,
            project_root=test_workspace,
            candidate_rel="candidates/cand-b",
            run_id="run-b",
        )
        ids_b = slot_mount_identities(layout_b)
        assert ids_a == ids_b
        assert (layout_b.project / "marker.txt").read_text(encoding="utf-8") == "beta\n"
        for root_name in ("project", "output", "diagnostics"):
            mode = os.stat(getattr(layout_b, root_name)).st_mode & 0o777
            assert mode == trusted_modes[root_name], f"{root_name} mode={oct(mode)}"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_baseline_file_outside_container_writable_roots(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-baseline-loc"
        cand = _candidate(module_root, "cand-loc")
        prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-loc",
            run_id="run-loc",
        )
        baseline_path = rw_mount_baseline_path(module_root)
        assert baseline_path.is_file()
        assert baseline_path.parent == module_root
        slot = module_root / "_active"
        for root_name in ("project", "output", "diagnostics"):
            assert baseline_path != slot / root_name
            assert not str(baseline_path).startswith(str(slot / root_name))

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_fresh_init_writes_complete_baseline_once(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-init-once"
        ensure_slot_roots(module_root)
        baseline_path = rw_mount_baseline_path(module_root)
        marker_path = rw_mount_trust_marker_path(module_root)
        assert baseline_path.is_file()
        assert marker_path.is_file()
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        assert payload["state"] == "ready"
        assert set(payload["roots"]) == {"project", "output", "diagnostics"}
        first_mtime = baseline_path.stat().st_mtime

        ensure_slot_roots(module_root)
        assert baseline_path.stat().st_mtime == first_mtime

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_baseline_missing_fail_closed(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-baseline-missing"
        cand = _candidate(module_root, "cand-missing")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-missing",
            run_id="run-setup",
        )
        baseline_path = rw_mount_baseline_path(module_root)
        assert baseline_path.is_file()
        assert rw_mount_trust_marker_path(module_root).is_file()
        baseline_path.unlink()
        os.chmod(layout.project, 0)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-missing",
                run_id="run-blocked",
            )
        assert exc_info.value.error_code == "active_slot_root_permission"
        assert "baseline_missing" in str(exc_info.value.detail)
        assert not baseline_path.is_file()
        assert os.stat(layout.project).st_mode & 0o777 == 0

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_baseline_truncated_fail_closed(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-baseline-trunc"
        cand = _candidate(module_root, "cand-trunc")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-trunc",
            run_id="run-setup",
        )
        baseline_path = rw_mount_baseline_path(module_root)
        baseline_path.write_text("{", encoding="utf-8")
        os.chmod(layout.project, 0)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-trunc",
                run_id="run-blocked",
            )
        assert exc_info.value.error_code == "active_slot_root_permission"
        assert "baseline_incomplete" in str(exc_info.value.detail)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_baseline_missing_root_entry_fail_closed(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-baseline-entry"
        cand = _candidate(module_root, "cand-entry")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-entry",
            run_id="run-setup",
        )
        baseline_path = rw_mount_baseline_path(module_root)
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        del payload["roots"]["project"]
        baseline_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(layout.project, 0)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-entry",
                run_id="run-blocked",
            )
        assert exc_info.value.error_code == "active_slot_root_permission"
        assert "baseline_incomplete" in str(exc_info.value.detail)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_initialization_interrupt_incomplete_fail_closed(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-baseline-tmp"
        ensure_slot_roots(module_root)
        baseline_path = rw_mount_baseline_path(module_root)
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_path.unlink()
        tmp_path = baseline_path.with_name(f"{baseline_path.name}.tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        cand = _candidate(module_root, "cand-tmp")
        layout = slot_layout(module_root / "_active")
        os.chmod(layout.project, 0)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-tmp",
                run_id="run-blocked",
            )
        assert exc_info.value.error_code == "active_slot_root_permission"
        assert "baseline_incomplete" in str(exc_info.value.detail)
        assert not baseline_path.is_file()

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_baseline_inode_tampered_fail_closed(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-baseline-tamper"
        cand = _candidate(module_root, "cand-tamper")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-tamper",
            run_id="run-setup",
        )
        baseline_path = rw_mount_baseline_path(module_root)
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        payload["roots"]["project"]["ino"] = payload["roots"]["project"]["ino"] + 99999
        baseline_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(layout.project, 0)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-tamper",
                run_id="run-blocked",
            )
        assert exc_info.value.error_code == "active_slot_root_permission"
        assert "baseline_inode_mismatch" in str(exc_info.value.detail)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_collect_restores_chmod_poisoned_output_root(self, test_workspace: Path) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-collect"
        cand = _candidate(module_root, "cand-collect")
        (cand / "project" / "artifact.txt").write_text("data\n", encoding="utf-8")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-collect",
            run_id="run-collect",
        )
        trusted_mode = os.stat(layout.output).st_mode & 0o777
        (layout.output / "result.txt").write_text("out\n", encoding="utf-8")
        os.chmod(layout.output, 0)

        collect_active_slot(module_root, cand, project_root=test_workspace)
        assert (cand / "output" / "result.txt").read_text(encoding="utf-8") == "out\n"
        assert os.stat(layout.output).st_mode & 0o777 == trusted_mode

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_unrecoverable_permission_raises_stable_error(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-fail"
        cand = _candidate(module_root, "cand-fail")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-fail",
            run_id="run-setup",
        )
        os.chmod(layout.project, 0)

        real_chmod = os.chmod

        def _deny_restore(path: os.PathLike[str] | str, mode: int) -> None:
            if Path(path) == layout.project:
                raise PermissionError("simulated chmod denial")
            real_chmod(path, mode)

        monkeypatch.setattr(os, "chmod", _deny_restore)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-fail",
                run_id="run-blocked",
            )
        assert exc_info.value.error_code == "active_slot_root_permission"
        assert not isinstance(exc_info.value, PermissionError)


class TestTrustSchemaValidation:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    @pytest.mark.parametrize(
        "baseline_mutator,expected_recovery",
        [
            (lambda payload: payload.update({"version": 0}), "baseline_version_mismatch"),
            (lambda payload: payload.update({"version": 99}), "baseline_version_mismatch"),
            (lambda payload: payload.pop("version", None), "baseline_version_mismatch"),
            (lambda payload: payload.update({"version": "1"}), "baseline_version_mismatch"),
            (lambda payload: payload.update({"version": True}), "baseline_version_mismatch"),
            (lambda payload: payload.update({"version": 1.0}), "baseline_version_mismatch"),
            (lambda payload: payload.update({"state": "pending"}), "baseline_incomplete"),
        ],
    )
    def test_baseline_version_and_state_matrix_fail_closed(
        self,
        test_workspace: Path,
        baseline_mutator,
        expected_recovery: str,
    ) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / f"perm-schema-{expected_recovery}"
        cand = _candidate(module_root, "cand-schema")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-schema",
            run_id="run-setup",
        )
        baseline_path = rw_mount_baseline_path(module_root)
        payload = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_mutator(payload)
        baseline_path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(layout.project, 0)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-schema",
                run_id="run-blocked",
            )
        assert exc_info.value.error_code == "active_slot_root_permission"
        assert expected_recovery in str(exc_info.value.detail)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    @pytest.mark.parametrize(
        "marker_body",
        ["", "garbage", json.dumps({"schema": "x", "version": 1, "state": "ready"})],
    )
    def test_invalid_trust_marker_blocks_restore_without_reinit(
        self,
        test_workspace: Path,
        marker_body: str,
    ) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-marker-invalid"
        cand = _candidate(module_root, "cand-marker")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-marker",
            run_id="run-setup",
        )
        baseline_path = rw_mount_baseline_path(module_root)
        baseline_before = baseline_path.read_text(encoding="utf-8")
        rw_mount_trust_marker_path(module_root).write_text(marker_body, encoding="utf-8")
        os.chmod(layout.project, 0)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-marker",
                run_id="run-blocked",
            )
        assert exc_info.value.error_code == "active_slot_root_permission"
        assert "baseline_marker_invalid" in str(exc_info.value.detail)
        assert baseline_path.read_text(encoding="utf-8") == baseline_before

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_bad_marker_and_missing_baseline_does_not_re_tofu(
        self,
        test_workspace: Path,
    ) -> None:
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "perm-no-retofu"
        cand = _candidate(module_root, "cand-retofu")
        layout = prepare_active_slot(
            module_root,
            cand,
            project_root=test_workspace,
            candidate_rel="candidates/cand-retofu",
            run_id="run-setup",
        )
        baseline_path = rw_mount_baseline_path(module_root)
        assert baseline_path.is_file()
        rw_mount_trust_marker_path(module_root).write_text("garbage", encoding="utf-8")
        baseline_path.unlink()
        os.chmod(layout.project, 0)

        with pytest.raises(CandidatePathError) as exc_info:
            prepare_active_slot(
                module_root,
                cand,
                project_root=test_workspace,
                candidate_rel="candidates/cand-retofu",
                run_id="run-blocked",
            )
        assert exc_info.value.error_code == "active_slot_root_permission"
        assert "baseline_marker_invalid" in str(exc_info.value.detail)
        assert not baseline_path.is_file()
        assert rw_mount_trust_marker_path(module_root).read_text(encoding="utf-8") == "garbage"
