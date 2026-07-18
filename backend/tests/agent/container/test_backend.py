"""Tests for AgentContainerBackend active slot isolation and command policy."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bridle.agent.container.active_slot import (
    active_slot_dir,
    prepare_active_slot,
    read_lease,
    slot_layout,
    tree_hashes,
)
from bridle.agent.container.backend import AgentContainerBackend
from bridle.agent.container.candidate_contract import CandidateExecutionRequest
from bridle.agent.container.candidate_path_guard import CandidatePathError
from bridle.agent.container.container_control import (
    EXECUTION_EXITED,
    EXECUTION_FAILED_BEFORE_EXEC,
    EXECUTION_PHASE_CLEANUP,
    EXECUTION_PHASE_COLLECT,
    EXECUTION_PHASE_CREATE,
    EXECUTION_PHASE_EXEC,
    EXECUTION_PHASE_PREPARE,
    EXECUTION_PHASE_START,
    EXECUTION_STARTED_UNKNOWN,
    EXECUTION_TIMED_OUT,
    SECONDARY_COLLECT_ERROR_CODE,
    SECONDARY_START_CLEANUP_ERROR_CODE,
    build_control_envelope,
    format_control_envelope_line,
    load_authoritative_evidence,
    parse_control_envelope_from_exec_output,
)
from bridle.agent.container.runner import (
    ContainerResult,
    FakeContainerRunner,
    LocalContainerRuntimeRunner,
)
from bridle.agent.container.test_backend import ModuleContainerTestBackend
from bridle.agent.container.test_command_compiler import TestCommandCompiler
from bridle.agent.safety.sandbox_policy import SandboxPolicy


class StructuredLockRunner(FakeContainerRunner):
    """In-process lock tests without subprocess or process-global side effects."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.exec_intervals: list[dict[str, float | str]] = []
        self._exec_delay = 0.05

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        env = environment or {}
        run_id = env.get("BRIDLE_RUN_ID", "run-unknown")
        request, current = self._load(container_id)
        candidate_rel = env.get("BRIDLE_CANDIDATE_REL", "")
        if not candidate_rel and request.module_mount_root:
            lease_path = Path(request.module_mount_root) / "diagnostics" / ".lease.json"
            if lease_path.is_file():
                candidate_rel = str(
                    json.loads(lease_path.read_text(encoding="utf-8")).get("candidate_rel") or ""
                )
        start = time.monotonic()
        manifest = {
            "schema": "bridle.container_test_result/v1",
            "status": "completed",
            "exit_code": 0,
            "results": [{"command_id": "cmd-1", "exit_code": 0, "stdout": "ok\n"}],
        }
        envelope = build_control_envelope(
            manifest=manifest,
            run_id=run_id,
            candidate_rel=candidate_rel or None,
            exit_code=0,
        )
        time.sleep(self._exec_delay)
        end = time.monotonic()
        self.exec_intervals.append(
            {"container_id": container_id, "run_id": run_id, "start": start, "end": end}
        )
        stdout = format_control_envelope_line(envelope)
        result = ContainerResult(
            container_id=container_id,
            name=current.name,
            status="running",
            network_mode=current.network_mode,
            health="healthy",
            finished_at=datetime.now(UTC),
            exit_code=0,
            stdout=stdout,
            stderr="",
        )
        self._containers[container_id] = (request, result)
        self._logs[container_id].append(stdout)
        return result


class BlockingStructuredLockRunner(StructuredLockRunner):
    """Structured runner that blocks in exec until released — used for module-lock tests."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.exec_entered = threading.Event()
        self.exec_release = threading.Event()
        self.exec_barrier: threading.Barrier | None = None
        self._block_first_exec = True

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        if self._block_first_exec:
            self.exec_entered.set()
            if self.exec_barrier is not None:
                self.exec_barrier.wait(timeout=5)
            if not self.exec_release.wait(timeout=5):
                raise TimeoutError("exec_release not signaled")
            self._block_first_exec = False
        return super().exec(
            container_id,
            command,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )


class BlockingFakeContainerRunner(FakeContainerRunner):
    """Fake runner that blocks in exec until released — used for module-lock tests."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.exec_entered = threading.Event()
        self.exec_release = threading.Event()
        self.exec_barrier: threading.Barrier | None = None
        self._block_first_exec = True

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        if self._block_first_exec:
            self.exec_entered.set()
            if self.exec_barrier is not None:
                self.exec_barrier.wait(timeout=5)
            if not self.exec_release.wait(timeout=5):
                raise TimeoutError("exec_release not signaled")
            self._block_first_exec = False
        return super().exec(
            container_id,
            command,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _layout_module(test_workspace: Path, module_id: str = "iso-mod") -> Path:
    return test_workspace / ".bridle" / "runtime" / "modules" / module_id


def _candidate(module_root: Path, candidate_id: str) -> tuple[str, Path]:
    rel = f"candidates/{candidate_id}"
    root = module_root / rel
    for sub in ("project", "baseline", "output", "diagnostics", "mocks"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return rel, root


def _write_evil_pytest(project: Path, sibling_id: str) -> None:
    """Malicious test fixture for Docker E2E isolation proofs."""
    tests = project / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    script = f'''
from pathlib import Path
import pytest

def test_cannot_touch_sibling():
    targets = [
        Path("../../{sibling_id}/project/secret.txt"),
        Path("/container/candidates/{sibling_id}/project/secret.txt"),
        Path("../{sibling_id}/project/secret.txt"),
    ]
    for target in targets:
        assert not target.exists(), f"unexpected sibling visibility: {{target}}"
'''
    (tests / "test_evil.py").write_text(script.strip() + "\n", encoding="utf-8")


class TestActiveSlotIsolation:
    def test_active_slot_excludes_sibling_candidates(self, test_workspace: Path) -> None:
        module_root = _layout_module(test_workspace)
        module_root.mkdir(parents=True)
        rel_a, cand_a = _candidate(module_root, "cand-a")
        _, cand_b = _candidate(module_root, "cand-b")
        (cand_b / "project" / "secret.txt").write_text("secret-b\n", encoding="utf-8")
        b_hash_before = _file_hash(cand_b / "project" / "secret.txt")

        tests_dir = cand_a / "project" / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
        pytest_cmd = "python -m pytest tests/test_ok.py -q"
        approved = TestCommandCompiler.compile_commands(
            test_commands=[pytest_cmd],
            test_entity_id="node-evil",
            map_seq=1,
        )
        request_manifest = {
            "schema": "bridle.container_test_request/v1",
            "commands": TestCommandCompiler.manifest_commands(approved),
            "write_set": ["tests/test_ok.py"],
        }
        (cand_a / "diagnostics" / "test-request.json").write_text(
            json.dumps(request_manifest), encoding="utf-8"
        )
        (cand_a / "baseline" / "tests").mkdir(parents=True, exist_ok=True)
        (cand_a / "baseline" / "tests" / "test_ok.py").write_text(
            "def test_ok(): assert True\n", encoding="utf-8"
        )

        slot = prepare_active_slot(
            module_root,
            cand_a,
            project_root=test_workspace,
            candidate_rel=rel_a,
            run_id="run-a",
        )
        assert not (slot.slot_root / "candidates").exists()
        assert not (slot.project / "secret.txt").exists()
        assert not any(slot.project.rglob("secret.txt"))

        runner = FakeContainerRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        result = backend.run_tests_in_candidate(
            candidate_root=cand_a,
            module_root=module_root,
            candidate_rel=rel_a,
            run_id="run-a",
            node_id="node-evil",
            module_id="iso-mod",
            boundary_fingerprint="fp-iso",
            test_commands=[pytest_cmd],
            write_set=["tests/test_ok.py"],
            test_entity_id="node-evil",
            map_seq=1,
        )
        manifest = json.loads((cand_a / "output" / "manifest.json").read_text(encoding="utf-8"))
        assert result["exit_code"] == 0
        assert manifest["status"] == "completed"
        assert _file_hash(cand_b / "project" / "secret.txt") == b_hash_before

    def test_backend_mounts_active_slot_not_module_root(self, test_workspace: Path) -> None:
        module_root = _layout_module(test_workspace, "mount-mod")
        module_root.mkdir(parents=True)
        rel, cand = _candidate(module_root, "c1")
        tests_dir = cand / "project" / "tests"
        tests_dir.mkdir(parents=True)
        (tests_dir / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
        pytest_cmd = "python -m pytest tests/test_ok.py -q"
        approved = TestCommandCompiler.compile_commands(
            test_commands=[pytest_cmd],
            test_entity_id="n1",
            map_seq=1,
        )
        (cand / "diagnostics" / "test-request.json").write_text(
            json.dumps(
                {
                    "schema": "bridle.container_test_request/v1",
                    "commands": TestCommandCompiler.manifest_commands(approved),
                    "write_set": ["tests/test_ok.py", "marker.txt"],
                }
            ),
            encoding="utf-8",
        )
        (cand / "baseline" / "tests").mkdir(parents=True, exist_ok=True)
        (cand / "baseline" / "tests" / "test_ok.py").write_text(
            "def test_ok(): assert True\n", encoding="utf-8"
        )
        runner = FakeContainerRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        backend.run_tests_in_candidate(
            candidate_root=cand,
            module_root=module_root,
            candidate_rel=rel,
            run_id="run-1",
            node_id="n1",
            module_id="mount-mod",
            boundary_fingerprint="fp-mount",
            test_commands=[pytest_cmd],
            write_set=["tests/test_ok.py"],
            test_entity_id="n1",
            map_seq=1,
        )
        created_id = next(iter(runner._containers))
        request, _ = runner._containers[created_id]
        by_target = {m.target: m for m in request.mounts}
        assert by_target["/workspace/project"].readonly is False
        assert by_target["/workspace/baseline"].readonly is True
        assert by_target["/workspace/mocks"].readonly is True
        assert len(request.mounts) == 5


def _minimal_pytest_candidate(
    module_root: Path,
    candidate_id: str,
    *,
    marker: str,
    test_workspace: Path,
) -> tuple[str, Path]:
    rel, cand = _candidate(module_root, candidate_id)
    tests_dir = cand / "project" / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (cand / "project" / "marker.txt").write_text(marker, encoding="utf-8")
    (cand / "baseline" / "tests").mkdir(parents=True, exist_ok=True)
    (cand / "baseline" / "tests" / "test_ok.py").write_text(
        "def test_ok(): assert True\n", encoding="utf-8"
    )
    pytest_cmd = "python -m pytest tests/test_ok.py -q"
    approved = TestCommandCompiler.compile_commands(
        test_commands=[pytest_cmd],
        test_entity_id=f"node-{candidate_id}",
        map_seq=1,
    )
    (cand / "diagnostics" / "test-request.json").write_text(
        json.dumps(
            {
                "schema": "bridle.container_test_request/v1",
                "commands": TestCommandCompiler.manifest_commands(approved),
                "write_set": ["tests/test_ok.py", "marker.txt"],
                "protected_hashes": {
                    "baseline": tree_hashes(cand / "baseline"),
                    "mocks": tree_hashes(cand / "mocks"),
                },
            }
        ),
        encoding="utf-8",
    )
    return rel, cand


class TestModuleLockConcurrency:
    @pytest.mark.parametrize("attempt", range(20))
    def test_same_module_candidates_serialize_without_cross_contamination(
        self, test_workspace: Path, attempt: int
    ) -> None:
        module_root = _layout_module(test_workspace, f"lock-mod-{attempt}")
        module_root.mkdir(parents=True, exist_ok=True)
        rel_a, cand_a = _minimal_pytest_candidate(
            module_root, "cand-a", marker="alpha", test_workspace=test_workspace
        )
        rel_b, cand_b = _minimal_pytest_candidate(
            module_root, "cand-b", marker="beta", test_workspace=test_workspace
        )
        pytest_cmd = "python -m pytest tests/test_ok.py -q"
        runner = BlockingStructuredLockRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        b_done = threading.Event()
        b_errors: list[Exception] = []
        a_errors: list[Exception] = []

        def run_a() -> None:
            try:
                backend.run_tests_in_candidate(
                    candidate_root=cand_a,
                    module_root=module_root,
                    candidate_rel=rel_a,
                    run_id="run-a",
                    node_id="node-a",
                    module_id="lock-mod",
                    boundary_fingerprint="fp-lock",
                    test_commands=[pytest_cmd],
                    write_set=["tests/test_ok.py", "marker.txt"],
                    test_entity_id="node-a",
                    map_seq=1,
                )
            except Exception as exc:
                a_errors.append(exc)

        def run_b() -> None:
            try:
                backend.run_tests_in_candidate(
                    candidate_root=cand_b,
                    module_root=module_root,
                    candidate_rel=rel_b,
                    run_id="run-b",
                    node_id="node-b",
                    module_id="lock-mod",
                    boundary_fingerprint="fp-lock",
                    test_commands=[pytest_cmd],
                    write_set=["tests/test_ok.py", "marker.txt"],
                    test_entity_id="node-b",
                    map_seq=1,
                )
            except Exception as exc:
                b_errors.append(exc)
            finally:
                b_done.set()

        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        assert runner.exec_entered.wait(5)
        thread_b = threading.Thread(target=run_b)
        thread_b.start()
        time.sleep(0.3)
        assert not b_done.is_set()
        layout = slot_layout(active_slot_dir(module_root))
        lease = read_lease(layout)
        assert lease is not None
        assert lease.candidate_rel == rel_a
        assert lease.run_id == "run-a"
        runner.exec_release.set()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)
        assert not a_errors
        assert not b_errors
        assert b_done.is_set()
        assert (cand_a / "project" / "marker.txt").read_text(encoding="utf-8") == "alpha"
        assert (cand_b / "project" / "marker.txt").read_text(encoding="utf-8") == "beta"
        assert len(runner.exec_intervals) == 2
        first, second = runner.exec_intervals
        assert first["end"] <= second["start"] or second["end"] <= first["start"]

    @pytest.mark.parametrize("attempt", range(20))
    def test_different_modules_can_run_in_parallel(self, test_workspace: Path, attempt: int) -> None:
        mod_a = _layout_module(test_workspace, f"parallel-a-{attempt}")
        mod_b = _layout_module(test_workspace, f"parallel-b-{attempt}")
        mod_a.mkdir(parents=True, exist_ok=True)
        mod_b.mkdir(parents=True, exist_ok=True)
        rel_a, cand_a = _minimal_pytest_candidate(
            mod_a, "c1", marker="mod-a", test_workspace=test_workspace
        )
        rel_b, cand_b = _minimal_pytest_candidate(
            mod_b, "c1", marker="mod-b", test_workspace=test_workspace
        )
        pytest_cmd = "python -m pytest tests/test_ok.py -q"
        runner_a = StructuredLockRunner(workspace_root=test_workspace)
        runner_b = StructuredLockRunner(workspace_root=test_workspace)
        backend_a = AgentContainerBackend(test_workspace, runner=runner_a)
        backend_b = AgentContainerBackend(test_workspace, runner=runner_b)
        errors: list[Exception] = []

        def run_module(
            backend: AgentContainerBackend,
            *,
            module_root: Path,
            rel: str,
            cand: Path,
            module_id: str,
            run_id: str,
        ) -> None:
            try:
                backend.run_tests_in_candidate(
                    candidate_root=cand,
                    module_root=module_root,
                    candidate_rel=rel,
                    run_id=run_id,
                    node_id=run_id,
                    module_id=module_id,
                    boundary_fingerprint=f"fp-{module_id}",
                    test_commands=[pytest_cmd],
                    write_set=["tests/test_ok.py", "marker.txt"],
                    test_entity_id=run_id,
                    map_seq=1,
                )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(
                target=run_module,
                kwargs={
                    "backend": backend_a,
                    "module_root": mod_a,
                    "rel": rel_a,
                    "cand": cand_a,
                    "module_id": f"parallel-a-{attempt}",
                    "run_id": "run-a",
                },
            ),
            threading.Thread(
                target=run_module,
                kwargs={
                    "backend": backend_b,
                    "module_root": mod_b,
                    "rel": rel_b,
                    "cand": cand_b,
                    "module_id": f"parallel-b-{attempt}",
                    "run_id": "run-b",
                },
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not errors
        assert len(runner_a.exec_intervals) == 1
        assert len(runner_b.exec_intervals) == 1
        interval_a = runner_a.exec_intervals[0]
        interval_b = runner_b.exec_intervals[0]
        assert interval_a["start"] < interval_b["end"] and interval_b["start"] < interval_a["end"]

    def test_slot_cleared_after_failed_collect(self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from bridle.agent.container import orchestrator as orchestrator_mod
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "collect-fail-mod")
        module_root.mkdir(parents=True, exist_ok=True)
        rel_a, cand_a = _minimal_pytest_candidate(
            module_root, "cand-a", marker="alpha", test_workspace=test_workspace
        )
        rel_b, cand_b = _minimal_pytest_candidate(
            module_root, "cand-b", marker="beta", test_workspace=test_workspace
        )
        pytest_cmd = "python -m pytest tests/test_ok.py -q"
        runner = FakeContainerRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)

        backend.run_tests_in_candidate(
            candidate_root=cand_a,
            module_root=module_root,
            candidate_rel=rel_a,
            run_id="run-a",
            node_id="node-a",
            module_id="collect-fail-mod",
            boundary_fingerprint="fp-collect",
            test_commands=[pytest_cmd],
            write_set=["tests/test_ok.py", "marker.txt"],
            test_entity_id="node-a",
            map_seq=1,
        )

        real_collect = orchestrator_mod.collect_active_slot

        def _fail_collect(*args, **kwargs):
            raise CandidatePathError("refuse_symlink_or_reparse", detail="collect blocked for test")

        monkeypatch.setattr(orchestrator_mod, "collect_active_slot", _fail_collect)
        with pytest.raises(AgentContainerError):
            backend.run_tests_in_candidate(
                candidate_root=cand_b,
                module_root=module_root,
                candidate_rel=rel_b,
                run_id="run-b",
                node_id="node-b",
                module_id="collect-fail-mod",
                boundary_fingerprint="fp-collect",
                test_commands=[pytest_cmd],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-b",
                map_seq=1,
            )

        monkeypatch.setattr(orchestrator_mod, "collect_active_slot", real_collect)
        backend.run_tests_in_candidate(
            candidate_root=cand_a,
            module_root=module_root,
            candidate_rel=rel_a,
            run_id="run-a2",
            node_id="node-a2",
            module_id="collect-fail-mod",
            boundary_fingerprint="fp-collect",
            test_commands=[pytest_cmd],
            write_set=["tests/test_ok.py", "marker.txt"],
            test_entity_id="node-a2",
            map_seq=1,
        )
        layout = slot_layout(active_slot_dir(module_root))
        lease = read_lease(layout)
        assert lease is not None
        assert lease.candidate_rel == rel_a
        assert lease.run_id == "run-a2"
        assert (layout.project / "marker.txt").read_text(encoding="utf-8") == "alpha"
        assert (cand_b / "project" / "marker.txt").read_text(encoding="utf-8") == "beta"


@pytest.mark.asyncio
async def test_module_test_backend_rejects_disallowed_command_before_runner() -> None:
    allowed_cmd = "python -m pytest tests/test_a.py -q"
    approved = TestCommandCompiler.compile_commands(
        test_commands=[allowed_cmd],
        test_entity_id="node-1",
        map_seq=1,
    )
    mock_backend = MagicMock()
    request = CandidateExecutionRequest(
        candidate_id="c1",
        run_id="run-1",
        node_id="node-1",
        project_root=Path("."),
        base_map_seq=1,
        write_set=(),
        read_set=(),
        readonly_files=(),
        tests=(allowed_cmd,),
        timeout_seconds=60,
        network_allowed=False,
        module_id="mod",
    )
    backend = ModuleContainerTestBackend(
        mock_backend,
        candidate_request=request,
        candidate_root="/tmp/c",
        module_root="/tmp/m",
        candidate_rel="candidates/c1",
        test_entity_id="node-1",
        required_commands=["echo forbidden"],
        required_command_ids=[approved[0].command_id],
    )
    policy = SandboxPolicy.for_run(
        run_id="run-1",
        node_id="node-1",
        workspace_root=Path("."),
        allowed_files=[],
        node_tests=[allowed_cmd],
    )
    result = await backend.run_authoritative_tests(policy=policy)
    assert result["status"] == "failed"
    assert result["error_code"] == "CommandPolicyError"
    mock_backend.run_tests_in_candidate.assert_not_called()


@pytest.mark.asyncio
async def test_module_test_backend_rejects_unknown_pytest_path() -> None:
    allowed_cmd = "python -m pytest tests/test_a.py -q"
    approved = TestCommandCompiler.compile_commands(
        test_commands=[allowed_cmd],
        test_entity_id="node-1",
        map_seq=1,
    )
    mock_backend = MagicMock()
    request = CandidateExecutionRequest(
        candidate_id="c1",
        run_id="run-1",
        node_id="node-1",
        project_root=Path("."),
        base_map_seq=1,
        write_set=(),
        read_set=(),
        readonly_files=(),
        tests=(allowed_cmd,),
        timeout_seconds=60,
        network_allowed=False,
        module_id="mod",
    )
    backend = ModuleContainerTestBackend(
        mock_backend,
        candidate_request=request,
        candidate_root="/tmp/c",
        module_root="/tmp/m",
        candidate_rel="candidates/c1",
        test_entity_id="node-1",
        required_commands=["python -m pytest tests/other.py -q"],
        required_command_ids=[approved[0].command_id],
    )
    policy = SandboxPolicy.for_run(
        run_id="run-1",
        node_id="node-1",
        workspace_root=Path("."),
        allowed_files=[],
        node_tests=[allowed_cmd],
    )
    result = await backend.run_authoritative_tests(policy=policy)
    assert result["status"] == "failed"
    assert result["error_code"] == "CommandPolicyError"
    mock_backend.run_tests_in_candidate.assert_not_called()


class PhaseTrackingRunner(StructuredLockRunner):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.exec_calls: list[str] = []
        self.create_calls = 0

    def create(self, request):
        self.create_calls += 1
        return super().create(request)

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        self.exec_calls.append(container_id)
        return super().exec(
            container_id,
            command,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )


class CreateFailRunner(FakeContainerRunner):
    def create(self, request):
        raise ValueError("simulated container create failure")


class StartAfterCreateFailRunner(FakeContainerRunner):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.last_created_id: str | None = None

    def create(self, request):
        created = super().create(request)
        self.last_created_id = created.container_id
        return created

    def start(self, container_id: str):
        raise ValueError("simulated container start failure after create")


class StartAfterCreateCleanupFailRunner(StartAfterCreateFailRunner):
    def remove(self, container_id: str) -> None:
        raise RuntimeError("simulated strict cleanup remove failure")


class StartAfterCreateStopFailRemoveOkRunner(StartAfterCreateFailRunner):
    def stop(self, container_id: str):
        raise OSError("simulated stop failure before remove")


class StartAfterCreateStopWeirdFailRemoveOkRunner(StartAfterCreateFailRunner):
    def stop(self, container_id: str):
        raise TypeError("simulated non-runtime stop failure")


class StartAfterCreateRemoveCapabilityMissingRunner(StartAfterCreateFailRunner):
    """Start fails after create, and the runner exposes no usable remove capability.

    The strict cleanup path must fail closed: report a cleanup secondary with
    ``resource_may_remain=True`` and preserve the original container identity
    instead of treating "stop without remove" as success.
    """

    remove = None  # type: ignore[assignment]


class StartAfterCreateOSErrorRunner(StartAfterCreateFailRunner):
    """Start raises ``OSError`` after a successful create.

    Exercises the full backend chain when the adapter raises a non-``ValueError``
    transport error: lifecycle must still map to the start phase, run strict
    cleanup, and persist container identity without executing anything.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.exec_calls: list[str] = []

    def start(self, container_id: str):
        raise OSError("simulated container start transport failure")

    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        self.exec_calls.append(container_id)
        return super().exec(
            container_id,
            command,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )


class ExecFailRunner(PhaseTrackingRunner):
    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        self.exec_calls.append(container_id)
        raise RuntimeError("simulated exec transport loss")


class ExecTimeoutRunner(PhaseTrackingRunner):
    def exec(
        self,
        container_id: str,
        command: list[str],
        *,
        timeout_seconds: int,
        environment: dict[str, str] | None = None,
    ) -> ContainerResult:
        self.exec_calls.append(container_id)
        raise TimeoutError("container_wait_timeout")


def _failed_host_attestation(candidate: Path) -> dict:
    evidence = json.loads(
        (candidate / "diagnostics" / "control-envelope.json").read_text(encoding="utf-8")
    )
    assert evidence["status"] == "failed"
    return evidence["host_attestation"]


def _collect_failure_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    from bridle.agent.container import orchestrator as orchestrator_mod

    def _fail_collect(*args, **kwargs) -> None:
        raise CandidatePathError("refuse_symlink_or_reparse", detail="collect blocked for test")

    monkeypatch.setattr(orchestrator_mod, "collect_active_slot", _fail_collect)


class TestExecutionPhaseEvidence:
    def test_prepare_failure_records_failed_before_exec_without_exec(
        self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bridle.agent.container import orchestrator as orchestrator_mod
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "phase-prepare")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-prepare", marker="prepare", test_workspace=test_workspace
        )
        runner = PhaseTrackingRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)

        def _fail_prepare(*args, **kwargs) -> None:
            raise CandidatePathError("active_slot_prepare_failed", detail="prepare blocked for test")

        monkeypatch.setattr(orchestrator_mod, "prepare_active_slot", _fail_prepare)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-prepare",
                node_id="node-prepare",
                module_id="phase-prepare",
                boundary_fingerprint="fp-prepare",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-prepare",
                map_seq=1,
            )
        assert exc_info.value.error_code == "active_slot_prepare_failed"
        att = _failed_host_attestation(cand)
        assert att["execution_state"] == EXECUTION_FAILED_BEFORE_EXEC
        assert att["execution_phase"] == EXECUTION_PHASE_PREPARE
        assert att["side_effect_possible"] is False
        assert att["exec_exit_code"] is None
        assert att["container_id"] == ""
        assert runner.exec_calls == []
        assert runner.create_calls == 0

    def test_create_failure_records_create_phase_without_container(
        self, test_workspace: Path
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "phase-create")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-create", marker="create", test_workspace=test_workspace
        )
        runner = CreateFailRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-create",
                node_id="node-create",
                module_id="phase-create",
                boundary_fingerprint="fp-create",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-create",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_create_failed"
        att = _failed_host_attestation(cand)
        assert att["execution_state"] == EXECUTION_FAILED_BEFORE_EXEC
        assert att["execution_phase"] == EXECUTION_PHASE_CREATE
        assert att["side_effect_possible"] is False
        assert att["container_id"] == ""
        assert not runner._containers

    def test_start_failure_after_create_records_identity_and_cleans_up(
        self, test_workspace: Path
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "phase-start")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-start", marker="start", test_workspace=test_workspace
        )
        runner = StartAfterCreateFailRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-start",
                node_id="node-start",
                module_id="phase-start",
                boundary_fingerprint="fp-start",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-start",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_start_failed"
        assert runner.last_created_id is not None
        att = _failed_host_attestation(cand)
        assert att["execution_state"] == EXECUTION_FAILED_BEFORE_EXEC
        assert att["execution_phase"] == EXECUTION_PHASE_START
        assert att["side_effect_possible"] is True
        assert att["exec_exit_code"] is None
        assert att["container_id"] == runner.last_created_id
        assert not runner.exists(runner.last_created_id)
        assert "secondary_execution_phase" not in att
        assert "resource_may_remain" not in att

    def test_start_oserror_after_create_runs_strict_cleanup_and_persists_phase(
        self, test_workspace: Path
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "phase-start-oserror")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-start-oserror", marker="start-oserror", test_workspace=test_workspace
        )
        runner = StartAfterCreateOSErrorRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-start-oserror",
                node_id="node-start-oserror",
                module_id="phase-start-oserror",
                boundary_fingerprint="fp-start-oserror",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-start-oserror",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_start_failed"
        assert runner.last_created_id is not None
        att = _failed_host_attestation(cand)
        assert att["execution_state"] == EXECUTION_FAILED_BEFORE_EXEC
        assert att["execution_phase"] == EXECUTION_PHASE_START
        assert att["side_effect_possible"] is True
        assert att["exec_exit_code"] is None
        assert att["container_id"] == runner.last_created_id
        assert runner.exec_calls == []
        assert not runner.exists(runner.last_created_id)
        assert "secondary_execution_phase" not in att
        assert "resource_may_remain" not in att

    def test_start_failure_with_cleanup_failure_reports_secondary_and_leak(
        self, test_workspace: Path
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "phase-start-cleanup-fail")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-start-cleanup", marker="start-cleanup", test_workspace=test_workspace
        )
        runner = StartAfterCreateCleanupFailRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-start-cleanup",
                node_id="node-start-cleanup",
                module_id="phase-start-cleanup-fail",
                boundary_fingerprint="fp-start-cleanup",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-start-cleanup",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_start_failed"
        assert runner.last_created_id is not None
        att = _failed_host_attestation(cand)
        assert att["execution_phase"] == EXECUTION_PHASE_START
        assert att["execution_state"] == EXECUTION_FAILED_BEFORE_EXEC
        assert att["container_id"] == runner.last_created_id
        assert att["secondary_execution_phase"] == EXECUTION_PHASE_CLEANUP
        assert att["secondary_error_code"] == SECONDARY_START_CLEANUP_ERROR_CODE
        assert att["secondary_detail"]
        assert att["start_cleanup_failure"] == att["secondary_detail"]
        assert att["resource_may_remain"] is True
        assert runner.exists(runner.last_created_id)

    @pytest.mark.parametrize(
        ("runner_cls", "module_id", "expect_secondary", "expect_resource_may_remain", "expect_container_gone"),
        [
            (StartAfterCreateStopFailRemoveOkRunner, "matrix-stop-ok", True, False, True),
            (StartAfterCreateStopWeirdFailRemoveOkRunner, "matrix-stop-weird", True, False, True),
            (StartAfterCreateCleanupFailRunner, "matrix-remove-fail", True, True, False),
        ],
    )
    def test_start_cleanup_state_matrix(
        self,
        test_workspace: Path,
        runner_cls: type,
        module_id: str,
        expect_secondary: bool,
        expect_resource_may_remain: bool,
        expect_container_gone: bool,
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, module_id)
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-matrix", marker="matrix", test_workspace=test_workspace
        )
        runner = runner_cls(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id=f"run-{module_id}",
                node_id="node-matrix",
                module_id=module_id,
                boundary_fingerprint="fp-matrix",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-matrix",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_start_failed"
        assert runner.last_created_id is not None
        att = _failed_host_attestation(cand)
        assert att["execution_phase"] == EXECUTION_PHASE_START
        if expect_secondary:
            assert att["secondary_execution_phase"] == EXECUTION_PHASE_CLEANUP
            assert att["secondary_error_code"] == SECONDARY_START_CLEANUP_ERROR_CODE
            assert att["secondary_detail"]
            assert att["start_cleanup_failure"] == att["secondary_detail"]
            assert "secondary_collect_error" not in exc_info.value.detail
        if expect_resource_may_remain:
            assert att["resource_may_remain"] is True
        else:
            assert att["resource_may_remain"] is False
        assert runner.exists(runner.last_created_id) is not expect_container_gone

    def test_docker_remove_nonzero_preserves_identity_and_cleanup_evidence(
        self, test_workspace: Path
    ) -> None:
        import subprocess

        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "docker-rm-fail")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-docker-rm", marker="docker-rm", test_workspace=test_workspace
        )
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, use_docker=True)
        container_id = "docker-cid-rm-fail"

        def _fake_run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
            sub = command[1] if len(command) > 1 else ""
            if sub == "create":
                return subprocess.CompletedProcess(command, 0, stdout=f"{container_id}\n", stderr="")
            if sub == "inspect":
                return subprocess.CompletedProcess(command, 0, stdout=f"{container_id}\n", stderr="")
            if sub == "start":
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="simulated docker start failed")
            if sub == "rm":
                return subprocess.CompletedProcess(
                    command, 1, stdout="rm stdout payload\n", stderr="permission denied"
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        runner._run_command = _fake_run  # type: ignore[method-assign]
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-docker-rm-fail",
                node_id="node-docker-rm",
                module_id="docker-rm-fail",
                boundary_fingerprint="fp-docker-rm",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-docker-rm",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_start_failed"
        assert exc_info.value.detail.get("secondary_cleanup_error")
        assert "secondary_collect_error" not in exc_info.value.detail
        att = _failed_host_attestation(cand)
        assert att["container_id"] == container_id
        assert att["secondary_execution_phase"] == EXECUTION_PHASE_CLEANUP
        assert att["resource_may_remain"] is True
        assert "exit_code=1" in att["secondary_detail"]
        assert "permission denied" in att["secondary_detail"]
        diagnostics = att["secondary_diagnostics"]
        assert diagnostics["container_id"] == container_id
        assert diagnostics["remove_executed"] is True
        assert diagnostics["remove_outcome"] == "failed"
        assert diagnostics["remove_exit_code"] == 1
        assert diagnostics["remove_stdout"] == "rm stdout payload\n"
        assert diagnostics["remove_stderr"] == "permission denied"
        assert diagnostics["remove_timed_out"] is False
        assert container_id in runner._containers

    def test_docker_remove_timeout_preserves_identity_and_structured_evidence(
        self, test_workspace: Path
    ) -> None:
        import subprocess

        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "docker-rm-timeout")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-docker-rm-timeout", marker="docker-rm-timeout", test_workspace=test_workspace
        )
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, use_docker=True)
        container_id = "docker-cid-rm-timeout"

        def _fake_run(command: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
            sub = command[1] if len(command) > 1 else ""
            if sub == "create":
                return subprocess.CompletedProcess(command, 0, stdout=f"{container_id}\n", stderr="")
            if sub == "inspect":
                return subprocess.CompletedProcess(command, 0, stdout=f"{container_id}\n", stderr="")
            if sub == "start":
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="simulated docker start failed")
            if sub == "rm":
                raise subprocess.TimeoutExpired(
                    cmd=["docker", "rm", "-f", container_id],
                    timeout=5,
                    output="partial rm stdout",
                    stderr="partial rm stderr",
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        runner._run_command = _fake_run  # type: ignore[method-assign]
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-docker-rm-timeout",
                node_id="node-docker-rm-timeout",
                module_id="docker-rm-timeout",
                boundary_fingerprint="fp-docker-rm-timeout",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-docker-rm-timeout",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_start_failed"
        assert exc_info.value.detail.get("secondary_cleanup_error")
        assert "secondary_collect_error" not in exc_info.value.detail
        att = _failed_host_attestation(cand)
        assert att["container_id"] == container_id
        assert att["secondary_execution_phase"] == EXECUTION_PHASE_CLEANUP
        assert att["resource_may_remain"] is True
        diagnostics = att["secondary_diagnostics"]
        assert diagnostics["container_id"] == container_id
        assert diagnostics["remove_executed"] is True
        assert diagnostics["remove_outcome"] == "failed"
        assert diagnostics["remove_exit_code"] is None
        assert diagnostics["remove_stdout"] == "partial rm stdout"
        assert diagnostics["remove_stderr"] == "partial rm stderr"
        assert diagnostics["remove_timed_out"] is True
        assert container_id in runner._containers

    def test_start_failure_without_remove_capability_reports_leak(
        self, test_workspace: Path
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "phase-start-no-remove")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-start-no-remove", marker="start-no-remove", test_workspace=test_workspace
        )
        runner = StartAfterCreateRemoveCapabilityMissingRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-start-no-remove",
                node_id="node-start-no-remove",
                module_id="phase-start-no-remove",
                boundary_fingerprint="fp-start-no-remove",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-start-no-remove",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_start_failed"
        assert runner.last_created_id is not None
        att = _failed_host_attestation(cand)
        assert att["execution_phase"] == EXECUTION_PHASE_START
        assert att["execution_state"] == EXECUTION_FAILED_BEFORE_EXEC
        assert att["container_id"] == runner.last_created_id
        assert att["secondary_execution_phase"] == EXECUTION_PHASE_CLEANUP
        assert att["secondary_error_code"] == SECONDARY_START_CLEANUP_ERROR_CODE
        assert att["secondary_detail"]
        assert att["start_cleanup_failure"] == att["secondary_detail"]
        assert att["resource_may_remain"] is True
        diagnostics = att["secondary_diagnostics"]
        assert diagnostics["container_id"] == runner.last_created_id
        assert diagnostics["remove_executed"] is False
        assert diagnostics["remove_outcome"] == "unknown"
        assert diagnostics["resource_may_remain"] is True
        assert runner.exists(runner.last_created_id)

    def test_exec_exception_records_started_unknown_with_side_effect(
        self, test_workspace: Path
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "phase-exec")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-exec", marker="exec", test_workspace=test_workspace
        )
        runner = ExecFailRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-exec",
                node_id="node-exec",
                module_id="phase-exec",
                boundary_fingerprint="fp-exec",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-exec",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_exec_failed"
        assert len(runner.exec_calls) == 1
        att = _failed_host_attestation(cand)
        assert att["execution_state"] == EXECUTION_STARTED_UNKNOWN
        assert att["execution_phase"] == EXECUTION_PHASE_EXEC
        assert att["side_effect_possible"] is True
        assert att["exec_exit_code"] is None
        assert att["container_id"] == runner.exec_calls[0]

    def test_exec_timeout_records_timed_out_with_side_effect(self, test_workspace: Path) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "phase-timeout")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-timeout", marker="timeout", test_workspace=test_workspace
        )
        runner = ExecTimeoutRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-timeout",
                node_id="node-timeout",
                module_id="phase-timeout",
                boundary_fingerprint="fp-timeout",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-timeout",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_wait_timeout"
        assert len(runner.exec_calls) == 1
        att = _failed_host_attestation(cand)
        assert att["execution_state"] == EXECUTION_TIMED_OUT
        assert att["execution_phase"] == EXECUTION_PHASE_EXEC
        assert att["side_effect_possible"] is True
        assert att["exec_exit_code"] is None

    def test_collect_failure_records_exited_with_side_effect(
        self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bridle.agent.container import orchestrator as orchestrator_mod
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "phase-collect")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-collect", marker="collect", test_workspace=test_workspace
        )
        runner = PhaseTrackingRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)

        real_collect = orchestrator_mod.collect_active_slot

        def _fail_collect(*args, **kwargs) -> None:
            raise CandidatePathError("refuse_symlink_or_reparse", detail="collect blocked for test")

        monkeypatch.setattr(orchestrator_mod, "collect_active_slot", _fail_collect)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-collect",
                node_id="node-collect",
                module_id="phase-collect",
                boundary_fingerprint="fp-collect",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-collect",
                map_seq=1,
            )
        assert exc_info.value.error_code == "active_slot_collect_failed"
        assert len(runner.exec_calls) == 1
        att = _failed_host_attestation(cand)
        assert att["execution_state"] == EXECUTION_EXITED
        assert att["execution_phase"] == EXECUTION_PHASE_COLLECT
        assert att["side_effect_possible"] is True
        assert att["exec_exit_code"] == 0
        assert att["container_id"] == runner.exec_calls[0]
        monkeypatch.setattr(orchestrator_mod, "collect_active_slot", real_collect)

    @pytest.mark.parametrize(
        ("runner_cls", "expected_primary_code", "expected_state", "expected_phase", "expected_exit"),
        [
            (
                ExecFailRunner,
                "container_exec_failed",
                EXECUTION_STARTED_UNKNOWN,
                EXECUTION_PHASE_EXEC,
                None,
            ),
            (
                ExecTimeoutRunner,
                "container_wait_timeout",
                EXECUTION_TIMED_OUT,
                EXECUTION_PHASE_EXEC,
                None,
            ),
        ],
    )
    def test_exec_primary_preserved_when_collect_also_fails(
        self,
        test_workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        runner_cls: type,
        expected_primary_code: str,
        expected_state: str,
        expected_phase: str,
        expected_exit: int | None,
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, f"combo-{runner_cls.__name__}")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-combo", marker="combo", test_workspace=test_workspace
        )
        runner = runner_cls(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        _collect_failure_patch(monkeypatch)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-combo",
                node_id="node-combo",
                module_id=f"combo-{runner_cls.__name__}",
                boundary_fingerprint="fp-combo",
                test_commands=["python -m pytest tests/test_ok.py -q"],
                write_set=["tests/test_ok.py", "marker.txt"],
                test_entity_id="node-combo",
                map_seq=1,
            )
        assert exc_info.value.error_code == expected_primary_code
        assert len(runner.exec_calls) == 1
        att = _failed_host_attestation(cand)
        assert att["execution_state"] == expected_state
        assert att["execution_phase"] == expected_phase
        assert att["side_effect_possible"] is True
        assert att["exec_exit_code"] == expected_exit
        assert att["secondary_execution_phase"] == EXECUTION_PHASE_COLLECT
        assert att["secondary_error_code"] == SECONDARY_COLLECT_ERROR_CODE
        assert att["secondary_detail"]

    def test_nonzero_exit_with_collect_failure_preserves_exit_and_secondary(
        self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "combo-nonzero-collect")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-nz", marker="fail", test_workspace=test_workspace
        )
        tests_dir = cand / "project" / "tests"
        (tests_dir / "test_fail.py").write_text(
            "def test_fail():\n    assert False\n",
            encoding="utf-8",
        )
        pytest_cmd = "python -m pytest tests/test_fail.py -q"
        approved = TestCommandCompiler.compile_commands(
            test_commands=[pytest_cmd],
            test_entity_id="node-nz",
            map_seq=1,
        )
        (cand / "diagnostics" / "test-request.json").write_text(
            json.dumps(
                {
                    "schema": "bridle.container_test_request/v1",
                    "commands": TestCommandCompiler.manifest_commands(approved),
                    "write_set": ["tests/test_fail.py", "marker.txt"],
                    "protected_hashes": {
                        "baseline": tree_hashes(cand / "baseline"),
                        "mocks": tree_hashes(cand / "mocks"),
                    },
                }
            ),
            encoding="utf-8",
        )
        runner = FakeContainerRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        _collect_failure_patch(monkeypatch)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-nz",
                node_id="node-nz",
                module_id="combo-nonzero-collect",
                boundary_fingerprint="fp-nz",
                test_commands=[pytest_cmd],
                write_set=["tests/test_fail.py", "marker.txt"],
                test_entity_id="node-nz",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_exit_failed"
        att = _failed_host_attestation(cand)
        assert att["execution_phase"] == EXECUTION_PHASE_EXEC
        assert att["execution_state"] == EXECUTION_EXITED
        assert att["exec_exit_code"] == 1
        assert att["side_effect_possible"] is True
        assert att["secondary_execution_phase"] == EXECUTION_PHASE_COLLECT
        assert att["secondary_error_code"] == SECONDARY_COLLECT_ERROR_CODE
        assert att["secondary_detail"]
        exit_diag = cand / "diagnostics" / "exit.error"
        assert exit_diag.is_file()
        body = exit_diag.read_text(encoding="utf-8")
        assert "exit_code=1" in body
        assert "stderr=" in body

    def test_real_subprocess_side_effect_before_collect_failure(
        self, test_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import uuid

        from bridle.agent.container.backend import AgentContainerError

        run_id = f"run-real-marker-{uuid.uuid4().hex[:8]}"
        marker_token = f"side-effect-{run_id}"

        module_root = _layout_module(test_workspace, "real-side-effect")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-real", marker="real", test_workspace=test_workspace
        )
        tests_dir = cand / "project" / "tests"
        (tests_dir / "test_marker.py").write_text(
            f"""
def test_write_run_marker():
    from pathlib import Path
    Path("marker.txt").write_text("{marker_token}", encoding="utf-8")
    assert True
""".strip()
            + "\n",
            encoding="utf-8",
        )
        (cand / "baseline" / "tests" / "test_marker.py").write_text(
            (tests_dir / "test_marker.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        pytest_cmd = "python -m pytest tests/test_marker.py -q"
        approved = TestCommandCompiler.compile_commands(
            test_commands=[pytest_cmd],
            test_entity_id="node-real",
            map_seq=1,
        )
        (cand / "diagnostics" / "test-request.json").write_text(
            json.dumps(
                {
                    "schema": "bridle.container_test_request/v1",
                    "commands": TestCommandCompiler.manifest_commands(approved),
                    "write_set": ["tests/test_marker.py", "marker.txt"],
                    "protected_hashes": {
                        "baseline": tree_hashes(cand / "baseline"),
                        "mocks": tree_hashes(cand / "mocks"),
                    },
                }
            ),
            encoding="utf-8",
        )
        runner = FakeContainerRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        _collect_failure_patch(monkeypatch)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id=run_id,
                node_id="node-real",
                module_id="real-side-effect",
                boundary_fingerprint="fp-real",
                test_commands=[pytest_cmd],
                write_set=["tests/test_marker.py", "marker.txt"],
                test_entity_id="node-real",
                map_seq=1,
            )
        assert exc_info.value.error_code == "active_slot_collect_failed"
        layout = slot_layout(active_slot_dir(module_root))
        assert (layout.project / "marker.txt").read_text(encoding="utf-8") == marker_token
        container_id = next(iter(runner._containers))
        _, exec_result = runner._containers[container_id]
        envelope = parse_control_envelope_from_exec_output(
            exec_result.stdout,
            expected_run_id=run_id,
            expected_candidate_rel=rel,
        )
        assert envelope["run_id"] == run_id
        evidence = load_authoritative_evidence(cand)
        assert evidence["run_id"] == run_id
        assert evidence["candidate_rel"] == rel
        att = _failed_host_attestation(cand)
        assert att["run_id"] == run_id
        assert att["execution_phase"] == EXECUTION_PHASE_COLLECT
        assert att["execution_state"] == EXECUTION_EXITED
        assert att["exec_exit_code"] == 0
        assert att["container_id"] == container_id
        assert att["side_effect_possible"] is True


class TestSubprocessDiagnostics:
    def test_unicode_output_survives_fake_subprocess_chain(self, test_workspace: Path) -> None:
        from bridle.agent.container.container_control import parse_control_envelope_from_exec_output

        module_root = _layout_module(test_workspace, "unicode-subprocess")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-u", marker="unicode", test_workspace=test_workspace
        )
        tests_dir = cand / "project" / "tests"
        (tests_dir / "test_unicode.py").write_text(
            """
def test_replacement_character():
    import sys
    sys.stdout.buffer.write(b"\\xff\\xfd\\n")
    sys.stdout.buffer.flush()
    assert True
""".strip()
            + "\n",
            encoding="utf-8",
        )
        pytest_cmd = "python -m pytest tests/test_unicode.py -q -s --capture=no"
        approved = TestCommandCompiler.compile_commands(
            test_commands=[pytest_cmd],
            test_entity_id="node-u",
            map_seq=1,
        )
        request_manifest = {
            "schema": "bridle.container_test_request/v1",
            "commands": TestCommandCompiler.manifest_commands(approved),
            "write_set": ["tests/test_unicode.py", "marker.txt"],
            "protected_hashes": {
                "baseline": tree_hashes(cand / "baseline"),
                "mocks": tree_hashes(cand / "mocks"),
            },
        }
        (cand / "diagnostics" / "test-request.json").write_text(
            json.dumps(request_manifest),
            encoding="utf-8",
        )
        runner = FakeContainerRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        result = backend.run_tests_in_candidate(
            candidate_root=cand,
            module_root=module_root,
            candidate_rel=rel,
            run_id="run-unicode",
            node_id="node-u",
            module_id="unicode-subprocess",
            boundary_fingerprint="fp-unicode",
            test_commands=[pytest_cmd],
            write_set=["tests/test_unicode.py", "marker.txt"],
            test_entity_id="node-u",
            map_seq=1,
        )
        assert result["exit_code"] == 0
        _, exec_result = runner._containers[next(iter(runner._containers))]
        envelope = parse_control_envelope_from_exec_output(
            exec_result.stdout,
            expected_run_id="run-unicode",
            expected_candidate_rel=rel,
        )
        stdout_blob = envelope["manifest"]["results"][0]["stdout"]
        assert "\ufffd" in stdout_blob

    def test_nonzero_subprocess_failure_preserves_truncated_diagnostics(
        self, test_workspace: Path
    ) -> None:
        from bridle.agent.container.backend import AgentContainerError

        module_root = _layout_module(test_workspace, "stderr-subprocess")
        module_root.mkdir(parents=True, exist_ok=True)
        rel, cand = _minimal_pytest_candidate(
            module_root, "cand-f", marker="fail", test_workspace=test_workspace
        )
        tests_dir = cand / "project" / "tests"
        (tests_dir / "test_fail_stderr.py").write_text(
            """
def test_fail_with_stderr():
    import sys
    sys.stderr.write("E" * 5000 + "\\n")
    assert False, "boom"
""".strip()
            + "\n",
            encoding="utf-8",
        )
        pytest_cmd = "python -m pytest tests/test_fail_stderr.py -q -s --capture=no"
        approved = TestCommandCompiler.compile_commands(
            test_commands=[pytest_cmd],
            test_entity_id="node-f",
            map_seq=1,
        )
        (cand / "diagnostics" / "test-request.json").write_text(
            json.dumps(
                {
                    "schema": "bridle.container_test_request/v1",
                    "commands": TestCommandCompiler.manifest_commands(approved),
                    "write_set": ["tests/test_fail_stderr.py", "marker.txt"],
                    "protected_hashes": {
                        "baseline": tree_hashes(cand / "baseline"),
                        "mocks": tree_hashes(cand / "mocks"),
                    },
                }
            ),
            encoding="utf-8",
        )
        runner = FakeContainerRunner(workspace_root=test_workspace)
        backend = AgentContainerBackend(test_workspace, runner=runner)
        with pytest.raises(AgentContainerError) as exc_info:
            backend.run_tests_in_candidate(
                candidate_root=cand,
                module_root=module_root,
                candidate_rel=rel,
                run_id="run-fail",
                node_id="node-f",
                module_id="stderr-subprocess",
                boundary_fingerprint="fp-fail",
                test_commands=[pytest_cmd],
                write_set=["tests/test_fail_stderr.py", "marker.txt"],
                test_entity_id="node-f",
                map_seq=1,
            )
        assert exc_info.value.error_code == "container_exit_failed"
        exit_diag = cand / "diagnostics" / "exit.error"
        assert exit_diag.is_file()
        body = exit_diag.read_text(encoding="utf-8")
        assert "stderr=" in body
        assert len(body) < 12000
        att = _failed_host_attestation(cand)
        assert att["execution_state"] == EXECUTION_EXITED
        assert att["execution_phase"] == EXECUTION_PHASE_EXEC
        assert att["side_effect_possible"] is True
        assert att["exec_exit_code"] == 1
