"""Tests for the protected pytest bootstrap."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[6]
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
