"""Tests for candidate worker isolation and typed IPC v2."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[6]
SCRIPT_DIR = REPO_ROOT / "scripts" / "ci"


def _load_script(name: str, relative: str):
    path = SCRIPT_DIR / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


trusted_ipc = _load_script("bridle_trusted_ipc", "trusted_ipc.py")
trusted_runner = _load_script("bridle_trusted_test_runner", "trusted_test_runner.py")
worker_sandbox = _load_script("bridle_worker_sandbox", "worker_sandbox.py")


def _write_minimal_candidate(candidate_root: Path) -> None:
    test_file = candidate_root / "backend/src/bridle/agent/container/tests/test_docker_integration.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.skip(reason='isolation-probe')\n"
        "def test_probe():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    (candidate_root / "backend").mkdir(parents=True, exist_ok=True)
    (candidate_root / "backend/pyproject.toml").write_text("[project]\nname='candidate'\n", encoding="utf-8")
    (candidate_root / "backend/src/bridle/__init__.py").write_text("", encoding="utf-8")


def _write_minimal_trusted(trusted_root: Path) -> None:
    (trusted_root / "backend").mkdir(parents=True, exist_ok=True)
    (trusted_root / "backend/pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    scripts = trusted_root / "scripts/ci"
    scripts.mkdir(parents=True, exist_ok=True)
    for path in (
        SCRIPT_DIR / "trusted_ipc.py",
        SCRIPT_DIR / "candidate_worker.py",
        SCRIPT_DIR / "worker_sandbox.py",
        SCRIPT_DIR / "candidate_isolation_probe.py",
        SCRIPT_DIR / "sentinel_registry.py",
        SCRIPT_DIR / "trusted_evidence_controller.py",
    ):
        (scripts / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    (scripts / "trusted_harness.py").write_text("# trusted harness\n", encoding="utf-8")


def test_ipc_v2_roundtrip() -> None:
    request = trusted_ipc.WorkerRequest(
        candidate_root="/candidate",
        trusted_config="/trusted-config/pyproject.toml",
        pytest_args=("-q",),
        public_env={"BRIDLE_CANDIDATE_WORKER": "1"},
    )
    decoded = trusted_ipc.decode_request(trusted_ipc.encode_request(request))
    assert decoded == request


@pytest.mark.skipif(os.name == "nt", reason="POSIX/docker worker isolation validated on Linux CI")
def test_worker_isolation_probe_preserves_trusted_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRIDLE_FORCE_SUBPROCESS_WORKER", "1")
    candidate_root = tmp_path / "candidate"
    trusted_root = tmp_path / "trusted"
    evidence_dir = tmp_path / "evidence"
    trusted_harness = tmp_path / "trusted-harness"
    _write_minimal_candidate(candidate_root)
    _write_minimal_trusted(trusted_root)
    _write_minimal_trusted(trusted_harness)
    evidence_dir.mkdir()
    harness_path = trusted_harness / "scripts/ci/trusted_harness.py"
    harness_before = harness_path.read_text(encoding="utf-8")

    import pytest as pytest_module

    before = sys.modules.get("pytest")
    trusted_runner.setup_probe_layout(candidate_root, trusted_harness, evidence_dir)
    probe_dir = str(candidate_root / ".bridle-isolation-probe")
    observation, worker_stdout, _ = trusted_runner.run_worker(
        candidate_root=candidate_root,
        trusted_root=trusted_root,
        pytest_args=[probe_dir, "-q"],
        probe=True,
    )
    probe_report = worker_sandbox.parse_probe_report(worker_stdout)
    assert probe_report is not None
    trusted_runner.verify_controller_state(
        before_pytest=before,
        harness_before=harness_before,
        harness_path=harness_path,
        evidence_dir=evidence_dir,
        probe_report=probe_report,
    )
    assert sys.modules.get("pytest") is pytest_module
    assert harness_path.read_text(encoding="utf-8") == harness_before
    assert probe_report["control_env_read"]["succeeded"] is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission-bound evidence isolation")
def test_worker_cannot_write_trusted_evidence_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import stat

    monkeypatch.setenv("BRIDLE_FORCE_SUBPROCESS_WORKER", "1")
    candidate_root = tmp_path / "candidate"
    trusted_root = tmp_path / "trusted"
    trusted_harness = tmp_path / "trusted-harness"
    evidence_dir = tmp_path / "evidence"
    _write_minimal_candidate(candidate_root)
    _write_minimal_trusted(trusted_root)
    _write_minimal_trusted(trusted_harness)
    evidence_dir.mkdir()
    evidence_dir.chmod(stat.S_IMODE(stat.S_IRUSR | stat.S_IXUSR))
    trusted_runner.setup_probe_layout(candidate_root, trusted_harness, evidence_dir)
    _, worker_stdout, _ = trusted_runner.run_worker(
        candidate_root=candidate_root,
        trusted_root=trusted_root,
        pytest_args=["-q"],
        probe=True,
    )
    probe_report = worker_sandbox.parse_probe_report(worker_stdout)
    assert probe_report is not None
    assert all(not item.get("succeeded") for item in probe_report["evidence_write"]["outcomes"])
    assert not (evidence_dir / "malicious-evidence.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX/docker worker isolation validated on Linux CI")
def test_trusted_runner_cli_probe_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIDLE_FORCE_SUBPROCESS_WORKER", "1")
    candidate_root = tmp_path / "candidate"
    trusted_root = tmp_path / "trusted"
    trusted_harness = tmp_path / "trusted-harness"
    _write_minimal_candidate(candidate_root)
    _write_minimal_trusted(trusted_root)
    _write_minimal_trusted(trusted_harness)
    transcript = tmp_path / "ipc-transcript.json"
    cmd = [
        sys.executable,
        "-I",
        str(SCRIPT_DIR / "trusted_test_runner.py"),
        "--probe-isolation",
        "--ipc-transcript",
        str(transcript),
        str(candidate_root),
        str(trusted_root),
        "--",
        "-q",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=str(tmp_path))
    assert proc.returncode in {0, 5}, proc.stderr
    assert transcript.is_file()
