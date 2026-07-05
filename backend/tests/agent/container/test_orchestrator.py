"""Tests for agent container orchestration."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from bridle.agent.container.active_slot import (
    ActiveSlotLayout,
    build_slot_mounts,
    prepare_active_slot,
    slot_allowed_mount_roots,
)
from bridle.agent.container.container_identity import build_container_labels
from bridle.agent.container.lifecycle import ModuleContainerRegistry, build_module_container_name
from bridle.agent.container.orchestrator import ContainerOrchestrator, OrchestrationError
from bridle.agent.container.runner import ContainerMount, ContainerRequest, FakeContainerRunner


def _module_layout(
    test_workspace: Path,
    *,
    module_id: str = "mod-a",
    candidate_id: str = "c1",
) -> tuple[Path, str, Path]:
    module_root = test_workspace / ".bridle" / "runtime" / "modules" / module_id
    candidate_rel = f"candidates/{candidate_id}"
    candidate = module_root / candidate_rel
    candidate.mkdir(parents=True, exist_ok=True)
    (candidate / "diagnostics").mkdir(parents=True, exist_ok=True)
    return module_root, candidate_rel, candidate


def _module_request(
    test_workspace: Path,
    module_root: Path,
    *,
    fp: str = "fp-1",
    timeout_seconds: int = 60,
    module_id: str = "mod-a",
    candidate: Path | None = None,
) -> ContainerRequest:
    layout: ActiveSlotLayout | None = None
    if candidate is not None:
        layout = prepare_active_slot(
            module_root,
            candidate,
            project_root=test_workspace,
            candidate_rel=f"candidates/{candidate.name}",
            run_id="orch-test",
        )
    else:
        from bridle.agent.container.active_slot import active_slot_dir, slot_layout

        slot = active_slot_dir(module_root)
        slot.mkdir(parents=True, exist_ok=True)
        for name in ("project", "baseline", "mocks", "output", "diagnostics"):
            (slot / name).mkdir(parents=True, exist_ok=True)
        layout = slot_layout(slot)
    slot_mounts = build_slot_mounts(layout)
    labels = build_container_labels(
        project_root=test_workspace,
        module_id=module_id,
        boundary_fingerprint=fp,
        image_version="local",
        mounts=slot_mounts,
    )
    return ContainerRequest(
        name=build_module_container_name(
            project_root=test_workspace,
            module_id=module_id,
            boundary_fingerprint=fp,
            image_version="local",
        ),
        image="bridle-agent:local",
        network_mode="none",
        mounts=slot_mounts,
        role="agent",
        allowed_mount_roots=slot_allowed_mount_roots(layout),
        timeout_seconds=timeout_seconds,
        module_id=module_id,
        boundary_fingerprint=fp,
        image_version="local",
        module_mount_root=str(layout.slot_root),
        keep_alive=True,
        labels=labels,
    )


class TestContainerOrchestrator:
    def test_run_and_wait_collects_logs_and_writes_diagnostics(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        workspace = test_workspace / ".bridle" / "runtime" / "container-workspaces" / "run-1"
        workspace.mkdir(parents=True)
        diag = workspace / "diagnostics"
        result = orch.run_and_wait(_module_request(test_workspace, workspace), diag_dir=diag)
        assert result.status == "stopped"
        assert result.health == "healthy"
        assert result.exit_code == 0
        assert (diag / "container.log").is_file()

    def test_wait_timeout_cleans_up_and_writes_diag(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        workspace = test_workspace / ".bridle" / "runtime" / "container-workspaces" / "run-1"
        workspace.mkdir(parents=True)
        diag = workspace / "diagnostics"
        with pytest.raises(OrchestrationError) as err:
            orch.run_and_wait(_module_request(test_workspace, workspace, timeout_seconds=0), diag_dir=diag)
        assert err.value.error_code == "container_start_failed"
        assert (diag / "startup.error").is_file()

    def test_module_exec_reuses_same_container_id(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, candidate_rel, candidate = _module_layout(test_workspace)
        (candidate / "diagnostics" / "test-request.json").write_text(
            '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
            encoding="utf-8",
        )
        request = _module_request(test_workspace, module_root, candidate=candidate)
        first = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-1",
            command=["python", "-m", "bridle.agent.container.entrypoint"],
            diag_dir=candidate / "diagnostics",
        )
        second = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-2",
            command=["python", "-m", "bridle.agent.container.entrypoint"],
            diag_dir=candidate / "diagnostics",
        )
        assert first.container_id == second.container_id
        assert second.reused is True

    def test_boundary_change_replaces_container(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, candidate_rel, candidate = _module_layout(test_workspace, candidate_id="c2")
        (candidate / "diagnostics" / "test-request.json").write_text(
            '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
            encoding="utf-8",
        )
        first = orch.run_module_exec(
            _module_request(test_workspace, module_root, fp="fp-a", candidate=candidate),
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-1",
            command=["echo", "one"],
            diag_dir=candidate / "diagnostics",
        )
        second = orch.run_module_exec(
            _module_request(test_workspace, module_root, fp="fp-b", candidate=candidate),
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-2",
            command=["echo", "two"],
            diag_dir=candidate / "diagnostics",
            replace_container=True,
        )
        assert first.container_id != second.container_id
        assert not runner.exists(first.container_id)

    def test_registry_clear_adopts_existing_container(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, candidate_rel, candidate = _module_layout(test_workspace, candidate_id="adopt-1")
        (candidate / "diagnostics" / "test-request.json").write_text(
            '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
            encoding="utf-8",
        )
        request = _module_request(test_workspace, module_root, candidate=candidate)
        first = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-1",
            command=["echo", "one"],
            diag_dir=candidate / "diagnostics",
        )
        orch.module_manager.registry.records.clear()
        orch.module_manager.registry.module_active_key.clear()
        second = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-2",
            command=["echo", "two"],
            diag_dir=candidate / "diagnostics",
        )
        assert second.container_id == first.container_id
        assert second.reused is True

    def test_retire_removes_duplicate_container_with_wrong_name(self, test_workspace: Path) -> None:
        from dataclasses import replace

        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, candidate_rel, candidate = _module_layout(test_workspace, candidate_id="dup-1")
        (candidate / "diagnostics" / "test-request.json").write_text(
            '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
            encoding="utf-8",
        )
        request = _module_request(test_workspace, module_root, candidate=candidate)
        first = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-1",
            command=["echo", "one"],
            diag_dir=candidate / "diagnostics",
        )
        duplicate = replace(request, name=f"{request.name}-duplicate")
        dup_result = runner.create(duplicate)
        runner.start(dup_result.container_id)
        second = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-2",
            command=["echo", "two"],
            diag_dir=candidate / "diagnostics",
        )
        assert second.container_id == first.container_id
        assert not runner.exists(dup_result.container_id)

    def test_slot_root_link_blocks_exec_and_taints_container(
        self,
        test_workspace: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import shutil

        if os.name == "nt":
            pytest.skip("POSIX directory symlink required")

        class CountingFakeRunner(FakeContainerRunner):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.exec_calls = 0

            def exec(self, container_id, command, *, timeout_seconds, environment=None):
                self.exec_calls += 1
                return super().exec(
                    container_id,
                    command,
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                )

        from bridle.agent.container.lifecycle import ModuleContainerState

        caplog.set_level(logging.INFO, logger="bridle")
        runner = CountingFakeRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, candidate_rel, candidate = _module_layout(test_workspace, candidate_id="root-link")
        (candidate / "project" / "tests").mkdir(parents=True, exist_ok=True)
        (candidate / "project" / "tests" / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
        (candidate / "diagnostics" / "test-request.json").write_text(
            '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
            encoding="utf-8",
        )
        request = _module_request(test_workspace, module_root, candidate=candidate)
        first = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-ok",
            command=["echo", "ok"],
            diag_dir=candidate / "diagnostics",
            exec_environment={"BRIDLE_ACTIVE_SLOT": "1"},
        )
        assert runner.exec_calls == 1

        outside = test_workspace / "outside-root-orchestrator"
        outside.mkdir(parents=True)
        sentinel = outside / "sentinel.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        layout = prepare_active_slot(
            module_root,
            candidate,
            project_root=test_workspace,
            candidate_rel=candidate_rel,
            run_id="setup",
        )
        project_root = layout.project
        shutil.rmtree(project_root)
        project_root.symlink_to(outside, target_is_directory=True)

        with pytest.raises(OrchestrationError) as err:
            orch.run_candidate_test_transaction(
                module_id=request.module_id,
                module_root=module_root,
                candidate_root=candidate,
                candidate_rel=candidate_rel,
                run_id="run-blocked",
                boundary_fingerprint=request.boundary_fingerprint,
                image_version=request.image_version,
                build_request=lambda slot: _module_request(test_workspace, module_root, candidate=candidate),
                command=["echo", "blocked"],
                diag_dir=candidate / "diagnostics",
            )
        assert err.value.error_code == "active_slot_root_link"
        assert runner.exec_calls == 1
        assert runner.exists(first.container_id)
        assert len(runner._containers) == 1
        record = orch.module_manager.registry.get(
            ModuleContainerRegistry.registry_key(
                project_id=str(test_workspace.resolve()),
                module_id=request.module_id,
                boundary_fingerprint=request.boundary_fingerprint,
                image_version=request.image_version,
            )
        )
        assert record is not None
        assert record.state == ModuleContainerState.TAINTED
        assert record.container_id == first.container_id
        assert sentinel.read_text(encoding="utf-8") == "keep\n"
        assert any(
            rec.message == "active_slot_root_link_rejected"
            for rec in caplog.records
            if rec.name == "bridle"
        )

    def test_active_parent_symlink_blocks_exec_and_taints_container(
        self,
        test_workspace: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import shutil

        if os.name == "nt":
            pytest.skip("POSIX directory symlink required")

        class CountingFakeRunner(FakeContainerRunner):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.exec_calls = 0
                self.create_calls = 0

            def create(self, request):
                self.create_calls += 1
                return super().create(request)

            def exec(self, container_id, command, *, timeout_seconds, environment=None):
                self.exec_calls += 1
                return super().exec(
                    container_id,
                    command,
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                )

        from bridle.agent.container.lifecycle import ModuleContainerState

        caplog.set_level(logging.INFO, logger="bridle")
        runner = CountingFakeRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, candidate_rel, candidate = _module_layout(test_workspace, candidate_id="active-parent")
        (candidate / "project" / "tests").mkdir(parents=True, exist_ok=True)
        (candidate / "diagnostics" / "test-request.json").write_text(
            '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
            encoding="utf-8",
        )
        request = _module_request(test_workspace, module_root, candidate=candidate)
        first = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-ok",
            command=["echo", "ok"],
            diag_dir=candidate / "diagnostics",
            exec_environment={"BRIDLE_ACTIVE_SLOT": "1"},
        )
        outside = test_workspace / "outside-active-orchestrator"
        for name in ("project", "baseline", "mocks", "output", "diagnostics"):
            (outside / name).mkdir(parents=True, exist_ok=True)
        (outside / "project" / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        slot = module_root / "_active"
        shutil.rmtree(slot)
        slot.symlink_to(outside, target_is_directory=True)

        with pytest.raises(OrchestrationError) as err:
            orch.run_candidate_test_transaction(
                module_id=request.module_id,
                module_root=module_root,
                candidate_root=candidate,
                candidate_rel=candidate_rel,
                run_id="run-blocked-active",
                boundary_fingerprint=request.boundary_fingerprint,
                image_version=request.image_version,
                build_request=lambda slot: _module_request(test_workspace, module_root, candidate=candidate),
                command=["echo", "blocked"],
                diag_dir=candidate / "diagnostics",
            )
        assert err.value.error_code == "active_slot_root_link"
        assert runner.exec_calls == 1
        assert runner.create_calls == 1
        assert runner.exists(first.container_id)
        assert len(runner._containers) == 1
        record = orch.module_manager.registry.get(
            ModuleContainerRegistry.registry_key(
                project_id=str(test_workspace.resolve()),
                module_id=request.module_id,
                boundary_fingerprint=request.boundary_fingerprint,
                image_version=request.image_version,
            )
        )
        assert record is not None
        assert record.state == ModuleContainerState.TAINTED
        assert (outside / "project" / "sentinel.txt").read_text(encoding="utf-8") == "keep\n"

    def test_dangling_project_symlink_blocks_exec_and_taints_container(
        self,
        test_workspace: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import shutil

        if os.name == "nt":
            pytest.skip("POSIX dangling symlink required")

        class CountingFakeRunner(FakeContainerRunner):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.exec_calls = 0
                self.create_calls = 0

            def create(self, request):
                self.create_calls += 1
                return super().create(request)

            def exec(self, container_id, command, *, timeout_seconds, environment=None):
                self.exec_calls += 1
                return super().exec(
                    container_id,
                    command,
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                )

        from bridle.agent.container.lifecycle import ModuleContainerState

        caplog.set_level(logging.INFO, logger="bridle")
        runner = CountingFakeRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, candidate_rel, candidate = _module_layout(test_workspace, candidate_id="dangling-project")
        (candidate / "project" / "tests").mkdir(parents=True, exist_ok=True)
        (candidate / "diagnostics" / "test-request.json").write_text(
            '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
            encoding="utf-8",
        )
        request = _module_request(test_workspace, module_root, candidate=candidate)
        first = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-ok",
            command=["echo", "ok"],
            diag_dir=candidate / "diagnostics",
            exec_environment={"BRIDLE_ACTIVE_SLOT": "1"},
        )
        layout = prepare_active_slot(
            module_root,
            candidate,
            project_root=test_workspace,
            candidate_rel=candidate_rel,
            run_id="setup",
        )
        project_root = layout.project
        shutil.rmtree(project_root)
        project_root.symlink_to(test_workspace / "missing-dangling-target", target_is_directory=True)

        with pytest.raises(OrchestrationError) as err:
            orch.run_candidate_test_transaction(
                module_id=request.module_id,
                module_root=module_root,
                candidate_root=candidate,
                candidate_rel=candidate_rel,
                run_id="run-blocked-dangling",
                boundary_fingerprint=request.boundary_fingerprint,
                image_version=request.image_version,
                build_request=lambda slot: _module_request(test_workspace, module_root, candidate=candidate),
                command=["echo", "blocked"],
                diag_dir=candidate / "diagnostics",
            )
        assert err.value.error_code == "active_slot_root_link"
        assert runner.exec_calls == 1
        assert runner.create_calls == 1
        record = orch.module_manager.registry.get(
            ModuleContainerRegistry.registry_key(
                project_id=str(test_workspace.resolve()),
                module_id=request.module_id,
                boundary_fingerprint=request.boundary_fingerprint,
                image_version=request.image_version,
            )
        )
        assert record is not None
        assert record.state == ModuleContainerState.TAINTED
        assert record.container_id == first.container_id
        assert any(
            rec.message == "active_slot_root_link_rejected"
            for rec in caplog.records
            if rec.name == "bridle"
        )

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_rw_root_permission_poison_recoverable_continues_exec(
        self,
        test_workspace: Path,
    ) -> None:
        class CountingFakeRunner(FakeContainerRunner):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.exec_calls = 0

            def exec(self, container_id, command, *, timeout_seconds, environment=None):
                self.exec_calls += 1
                return super().exec(
                    container_id,
                    command,
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                )

        runner = CountingFakeRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, rel_a, cand_a = _module_layout(test_workspace, candidate_id="perm-a")
        _, rel_b, cand_b = _module_layout(test_workspace, candidate_id="perm-b")
        for candidate in (cand_a, cand_b):
            (candidate / "project" / "tests").mkdir(parents=True, exist_ok=True)
            (candidate / "diagnostics" / "test-request.json").write_text(
                '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
                encoding="utf-8",
            )
        request = _module_request(test_workspace, module_root, candidate=cand_a)
        first = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=rel_a,
            run_id="run-a",
            command=["echo", "a"],
            diag_dir=cand_a / "diagnostics",
            exec_environment={"BRIDLE_ACTIVE_SLOT": "1"},
        )
        layout = prepare_active_slot(
            module_root,
            cand_a,
            project_root=test_workspace,
            candidate_rel=rel_a,
            run_id="setup",
        )
        for root_name in ("project", "output", "diagnostics"):
            os.chmod(getattr(layout, root_name), 0)

        second = orch.run_candidate_test_transaction(
            module_id=request.module_id,
            module_root=module_root,
            candidate_root=cand_b,
            candidate_rel=rel_b,
            run_id="run-b",
            boundary_fingerprint=request.boundary_fingerprint,
            image_version=request.image_version,
            build_request=lambda slot: _module_request(test_workspace, module_root, candidate=cand_b),
            command=["echo", "b"],
            diag_dir=cand_b / "diagnostics",
        )
        assert second.container_id == first.container_id
        assert runner.exec_calls == 2

    @pytest.mark.skipif(os.name == "nt", reason="POSIX chmod required")
    def test_unrecoverable_rw_root_permission_blocks_exec_and_taints(
        self,
        test_workspace: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import logging

        class CountingFakeRunner(FakeContainerRunner):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.exec_calls = 0
                self.create_calls = 0

            def create(self, request):
                self.create_calls += 1
                return super().create(request)

            def exec(self, container_id, command, *, timeout_seconds, environment=None):
                self.exec_calls += 1
                return super().exec(
                    container_id,
                    command,
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                )

        from bridle.agent.container.lifecycle import ModuleContainerState

        caplog.set_level(logging.INFO, logger="bridle")
        runner = CountingFakeRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, candidate_rel, candidate = _module_layout(test_workspace, candidate_id="perm-fail")
        (candidate / "project" / "tests").mkdir(parents=True, exist_ok=True)
        (candidate / "diagnostics" / "test-request.json").write_text(
            '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
            encoding="utf-8",
        )
        request = _module_request(test_workspace, module_root, candidate=candidate)
        first = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-ok",
            command=["echo", "ok"],
            diag_dir=candidate / "diagnostics",
            exec_environment={"BRIDLE_ACTIVE_SLOT": "1"},
        )
        layout = prepare_active_slot(
            module_root,
            candidate,
            project_root=test_workspace,
            candidate_rel=candidate_rel,
            run_id="setup",
        )
        os.chmod(layout.project, 0)
        real_chmod = os.chmod

        def _deny_restore(path: os.PathLike[str] | str, mode: int) -> None:
            if Path(path) == layout.project:
                raise PermissionError("simulated chmod denial")
            real_chmod(path, mode)

        monkeypatch.setattr(os, "chmod", _deny_restore)

        with pytest.raises(OrchestrationError) as err:
            orch.run_candidate_test_transaction(
                module_id=request.module_id,
                module_root=module_root,
                candidate_root=candidate,
                candidate_rel=candidate_rel,
                run_id="run-blocked-perm",
                boundary_fingerprint=request.boundary_fingerprint,
                image_version=request.image_version,
                build_request=lambda slot: _module_request(test_workspace, module_root, candidate=candidate),
                command=["echo", "blocked"],
                diag_dir=candidate / "diagnostics",
            )
        assert err.value.error_code == "active_slot_root_permission"
        assert runner.exec_calls == 1
        assert runner.create_calls == 1
        record = orch.module_manager.registry.get(
            ModuleContainerRegistry.registry_key(
                project_id=str(test_workspace.resolve()),
                module_id=request.module_id,
                boundary_fingerprint=request.boundary_fingerprint,
                image_version=request.image_version,
            )
        )
        assert record is not None
        assert record.state == ModuleContainerState.TAINTED
        assert record.container_id == first.container_id
        assert any(
            rec.message == "active_slot_root_permission_rejected"
            for rec in caplog.records
            if rec.name == "bridle"
        )

    def test_ab_candidates_isolated_under_same_container(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, rel_a, cand_a = _module_layout(test_workspace, candidate_id="cand-a")
        _, rel_b, cand_b = _module_layout(test_workspace, candidate_id="cand-b")
        for candidate, marker in ((cand_a, "alpha"), (cand_b, "beta")):
            project = candidate / "project"
            project.mkdir(parents=True, exist_ok=True)
            (project / "marker.txt").write_text(marker, encoding="utf-8")
            diag = candidate / "diagnostics"
            diag.mkdir(parents=True, exist_ok=True)
            (diag / "test-request.json").write_text(
                '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
                encoding="utf-8",
            )
        request = _module_request(test_workspace, module_root, candidate=cand_a)
        first = orch.run_module_exec(
            request,
            module_root=module_root,
            candidate_rel=rel_a,
            run_id="run-a",
            command=["echo", "a"],
            diag_dir=cand_a / "diagnostics",
            exec_environment={"BRIDLE_ACTIVE_SLOT": "1"},
        )
        second = orch.run_module_exec(
            _module_request(test_workspace, module_root, candidate=cand_b),
            module_root=module_root,
            candidate_rel=rel_b,
            run_id="run-b",
            command=["echo", "b"],
            diag_dir=cand_b / "diagnostics",
            exec_environment={"BRIDLE_ACTIVE_SLOT": "1"},
        )
        assert first.container_id == second.container_id
        assert (cand_a / "project" / "marker.txt").read_text(encoding="utf-8") == "alpha"
        assert (cand_b / "project" / "marker.txt").read_text(encoding="utf-8") == "beta"

    def test_adopt_rejects_wrong_mount_identity(self, test_workspace: Path) -> None:
        runner = FakeContainerRunner(workspace_root=test_workspace)
        orch = ContainerOrchestrator(runner, test_workspace)
        module_root, candidate_rel, candidate = _module_layout(test_workspace, candidate_id="adopt-bad")
        (candidate / "diagnostics" / "test-request.json").write_text(
            '{"schema":"bridle.container_test_request/v1","commands":[],"write_set":[]}',
            encoding="utf-8",
        )
        good = _module_request(test_workspace, module_root, fp="fp-same", candidate=candidate)
        first = orch.run_module_exec(
            good,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-1",
            command=["echo", "one"],
            diag_dir=candidate / "diagnostics",
            exec_environment={"BRIDLE_ACTIVE_SLOT": "1"},
        )
        orch.module_manager.registry.records.clear()
        orch.module_manager.registry.module_active_key.clear()
        bad_slot = module_root / "_active_bad"
        bad_slot.mkdir(parents=True, exist_ok=True)
        bad_mount = ContainerMount(source=bad_slot, target="/workspace", readonly=False)
        bad_labels = build_container_labels(
            project_root=test_workspace,
            module_id="mod-a",
            boundary_fingerprint="fp-same",
            image_version="local",
            mounts=[bad_mount],
        )
        bad_request = ContainerRequest(
            name=good.name,
            image=good.image,
            network_mode=good.network_mode,
            mounts=[bad_mount],
            role="agent",
            allowed_mount_roots=[str(bad_slot)],
            module_id=good.module_id,
            boundary_fingerprint=good.boundary_fingerprint,
            image_version=good.image_version,
            module_mount_root=str(bad_slot),
            keep_alive=True,
            labels=bad_labels,
        )
        second = orch.run_module_exec(
            bad_request,
            module_root=module_root,
            candidate_rel=candidate_rel,
            run_id="run-2",
            command=["echo", "two"],
            diag_dir=candidate / "diagnostics",
            exec_environment={"BRIDLE_ACTIVE_SLOT": "1"},
        )
        assert second.container_id != first.container_id

    def test_registry_key_stable(self, test_workspace: Path) -> None:
        key_a = ModuleContainerRegistry.registry_key(
            project_id=str(test_workspace),
            module_id="mod",
            boundary_fingerprint="abc",
            image_version="local",
        )
        key_b = ModuleContainerRegistry.registry_key(
            project_id=str(test_workspace),
            module_id="mod",
            boundary_fingerprint="abc",
            image_version="local",
        )
        assert key_a == key_b
