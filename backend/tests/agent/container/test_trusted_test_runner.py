"""Tests for the protected pytest bootstrap."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "trusted_test_runner.py"
SPEC = importlib.util.spec_from_file_location("bridle_trusted_test_runner", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
trusted_runner = importlib.util.module_from_spec(SPEC)
sys.modules["bridle_trusted_test_runner"] = trusted_runner
SPEC.loader.exec_module(trusted_runner)


def test_sanitized_environment_disables_pytest_injection() -> None:
    source = {
        "PATH": "bin",
        "PYTEST_ADDOPTS": "-p candidate_plugin",
        "PYTEST_PLUGINS": "candidate_plugin",
        "PYTHONPATH": "candidate",
    }

    result = trusted_runner.sanitized_environment(source)

    assert result["PATH"] == "bin"
    assert result["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "PYTEST_ADDOPTS" not in result
    assert "PYTEST_PLUGINS" not in result
    assert "PYTHONPATH" not in result


def test_build_public_env_excludes_evidence_dir(tmp_path: Path) -> None:
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    public = trusted_runner.build_public_env(candidate_root=candidate_root, probe=False)
    assert "BRIDLE_DOCKER_EVIDENCE_DIR" not in public
    assert public["BRIDLE_TRUSTED_CHECKOUT_ROOT"] == str(candidate_root.resolve())
    assert public["BRIDLE_CANDIDATE_WORKER"] == "1"


def test_resolve_candidate_relative_path(tmp_path: Path) -> None:
    controller = trusted_runner._load_module(
        "bridle_trusted_evidence_controller",
        REPO_ROOT / "scripts/ci/trusted_evidence_controller.py",
    )
    candidate = tmp_path / "candidate"
    target = candidate / "backend/.test-workspaces/ws/outside.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    resolved = controller.resolve_candidate_relative_path(
        candidate,
        "backend/.test-workspaces/ws/outside.txt",
    )
    assert resolved == target.resolve()
    with pytest.raises(RuntimeError, match="sentinel_candidate_relative_invalid"):
        controller.resolve_candidate_relative_path(candidate, "../outside.txt")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink sentinel semantics")
def test_resolve_rejects_intermediate_symlink_component(tmp_path: Path) -> None:
    controller = trusted_runner._load_module(
        "bridle_trusted_evidence_controller_link",
        REPO_ROOT / "scripts/ci/trusted_evidence_controller.py",
    )
    candidate = tmp_path / "candidate"
    secret_dir = candidate / "secret"
    secret_dir.mkdir(parents=True)
    (secret_dir / "outside.txt").write_text("secret\n", encoding="utf-8")
    safe_dir = candidate / "safe"
    safe_dir.mkdir()
    (safe_dir / "subdir").symlink_to(secret_dir, target_is_directory=True)
    with pytest.raises(RuntimeError, match="sentinel_component_is_link"):
        controller.resolve_candidate_relative_path(
            candidate,
            "safe/subdir/outside.txt",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink sentinel semantics")
def test_resolve_rejects_symlink_pointing_outside_candidate(tmp_path: Path) -> None:
    controller = trusted_runner._load_module(
        "bridle_trusted_evidence_controller_escape",
        REPO_ROOT / "scripts/ci/trusted_evidence_controller.py",
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("secret\n", encoding="utf-8")
    link = candidate / "escape.txt"
    link.symlink_to(outside_secret)
    with pytest.raises(RuntimeError, match="sentinel_component_is_link"):
        controller.resolve_candidate_relative_path(candidate, "escape.txt")


def test_resolve_container_path_to_host(tmp_path: Path) -> None:
    controller = trusted_runner._load_module(
        "bridle_trusted_evidence_controller_container",
        REPO_ROOT / "scripts/ci/trusted_evidence_controller.py",
    )
    candidate = tmp_path / "candidate"
    target = candidate / "backend/.test-workspaces/ws/outside.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    container_path = "/bridle-candidate/backend/.test-workspaces/ws/outside.txt"
    resolved = controller.resolve_container_path_to_host(container_path, candidate)
    assert resolved == target.resolve()


def test_resolve_container_path_rejects_outside_candidate(tmp_path: Path) -> None:
    controller = trusted_runner._load_module(
        "bridle_trusted_evidence_controller_outside",
        REPO_ROOT / "scripts/ci/trusted_evidence_controller.py",
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    with pytest.raises(RuntimeError, match="sentinel_container_path_outside_candidate"):
        controller.resolve_container_path_to_host("/other/path.txt", candidate)
    with pytest.raises(RuntimeError, match="sentinel_container_path_is_root"):
        controller.resolve_container_path_to_host("/bridle-candidate", candidate)


def test_verify_worker_observation_rejects_missing_exit_code() -> None:
    trusted_ipc = trusted_runner._load_module(
        "bridle_trusted_ipc_obs",
        REPO_ROOT / "scripts/ci/trusted_ipc.py",
    )
    observation = trusted_ipc.WorkerObservation(
        worker_state="exited",
        exit_code=None,
        stdout="",
        stderr="",
        truncated_stdout=False,
        truncated_stderr=False,
        worker_pid=1,
        worker_uid=1000,
        controller_pid=2,
        controller_uid=1000,
    )
    with pytest.raises(RuntimeError, match="worker_exit_code_missing"):
        trusted_runner.verify_worker_observation(observation, probe=False)


def test_container_conftest_provides_test_workspace_with_worker_confcutdir() -> None:
    candidate_worker = trusted_runner._load_module(
        "bridle_candidate_worker",
        REPO_ROOT / "scripts/ci/candidate_worker.py",
    )
    candidate_root = REPO_ROOT
    trusted_config = REPO_ROOT / "backend/pyproject.toml"
    args = candidate_worker.pytest_arguments(
        candidate_root=candidate_root,
        trusted_config=trusted_config,
        extra_args=("-q",),
    )
    assert "--confcutdir" in args
    cut_index = args.index("--confcutdir")
    confcutdir = Path(args[cut_index + 1])
    assert confcutdir.name == "container"
    assert confcutdir.parent.name == "agent"
    assert any("test_docker_integration.py" in arg for arg in args)


def test_pytest_arguments_disable_capture_for_docker_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate_worker = trusted_runner._load_module(
        "bridle_candidate_worker_capture",
        REPO_ROOT / "scripts/ci/candidate_worker.py",
    )
    monkeypatch.setenv("BRIDLE_RUN_DOCKER_TESTS", "1")
    args = candidate_worker.pytest_arguments(
        candidate_root=REPO_ROOT,
        trusted_config=REPO_ROOT / "backend/pyproject.toml",
        extra_args=("-q",),
    )
    assert "-s" in args
    assert "--capture=no" in args


@dataclass
class _StubContext:
    candidate_root: Path
    controller_ipc_dir: Path | None = None
    sentinel_by_handle: dict[str, Any] = field(default_factory=dict)
    handled_request_ids: set[str] = field(default_factory=set)
    lease_id: str | None = None
    issued_it_run_id: str | None = None
    lease_registry: Any = None
    isolated_docker_host: str | None = None
    isolated_dind_name: str | None = None
    isolated_network: str | None = None


def _load_controller(name: str):
    return trusted_runner._load_module(
        name,
        REPO_ROOT / "scripts/ci/trusted_evidence_controller.py",
    )


def test_register_sentinel_request_rejects_replayed_nonce(tmp_path: Path) -> None:
    controller = _load_controller("bridle_trusted_evidence_controller_replay")
    candidate = tmp_path / "candidate"
    target = candidate / "outside.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret\n", encoding="utf-8")
    ipc_dir = tmp_path / "ipc"
    ctx = _StubContext(candidate_root=candidate, controller_ipc_dir=ipc_dir)
    payload = {"request_id": "req-fixed-001", "candidate_relative": "outside.txt"}
    handle = controller.register_sentinel_request(
        payload, ctx=ctx, trusted_scripts=REPO_ROOT / "scripts/ci"
    )
    assert handle.startswith("sent-")
    assert "req-fixed-001" in ctx.handled_request_ids
    with pytest.raises(RuntimeError, match="sentinel_request_replayed"):
        controller.register_sentinel_request(
            payload, ctx=ctx, trusted_scripts=REPO_ROOT / "scripts/ci"
        )


def test_register_sentinel_request_accepts_container_path(tmp_path: Path) -> None:
    controller = _load_controller("bridle_trusted_evidence_controller_container_req")
    candidate = tmp_path / "candidate"
    target = candidate / "outside.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret\n", encoding="utf-8")
    ctx = _StubContext(candidate_root=candidate, controller_ipc_dir=None)
    payload = {
        "request_id": "req-container-001",
        "container_path": "/bridle-candidate/outside.txt",
    }
    handle = controller.register_sentinel_request(
        payload, ctx=ctx, trusted_scripts=REPO_ROOT / "scripts/ci"
    )
    record = ctx.sentinel_by_handle[handle]
    assert record["canonical_path"] == str(target.resolve())


def test_sentinel_ack_is_scoped_to_request_id(tmp_path: Path) -> None:
    controller = _load_controller("bridle_trusted_evidence_controller_ack")
    candidate = tmp_path / "candidate"
    target = candidate / "outside.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret\n", encoding="utf-8")
    ipc_dir = tmp_path / "ipc"
    ctx = _StubContext(candidate_root=candidate, controller_ipc_dir=ipc_dir)
    controller.register_sentinel_request(
        {"request_id": "req-ack-001", "candidate_relative": "outside.txt"},
        ctx=ctx,
        trusted_scripts=REPO_ROOT / "scripts/ci",
    )
    ack_dir = ipc_dir / "sentinel-acks"
    assert (ack_dir / "req-ack-001.json").is_file()
    stale_ack = ack_dir / "req-stale-999.json"
    stale_ack.write_text(
        json.dumps(
            {"status": "registered", "handle": "sent-stale", "request_id": "req-stale-999"},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    fresh = controller.wait_for_sentinel_ack(ipc_dir, "req-ack-001", timeout=1.0)
    assert fresh["request_id"] == "req-ack-001"
    with pytest.raises(TimeoutError, match="sentinel_ack_timeout"):
        controller.wait_for_sentinel_ack(ipc_dir, "req-new-002", timeout=0.5)
