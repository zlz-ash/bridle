"""Tests for protected build staging and ruleset validation."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[6]
SCRIPT_DIR = REPO_ROOT / "scripts" / "ci"


def _load(name: str, relative: str):
    path = SCRIPT_DIR / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


stage_candidate = _load("bridle_stage_candidate_source", "stage_candidate_source.py")
trusted_harness = _load("bridle_trusted_harness", "trusted_harness.py")
verify_ruleset = _load("bridle_verify_github_ruleset", "verify_github_ruleset.py")
run_lease = _load("bridle_run_lease", "run_lease.py")
subprocess_stream = _load("bridle_subprocess_stream", "subprocess_stream.py")


def _write_candidate(root: Path, *, dockerfile: str = "FROM scratch\n") -> None:
    (root / "backend").mkdir(parents=True, exist_ok=True)
    (root / "backend/pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    docker = root / "backend/src/bridle/agent/container/agent.Dockerfile"
    docker.parent.mkdir(parents=True, exist_ok=True)
    docker.write_text(dockerfile, encoding="utf-8")
    source = root / "backend/src/bridle/example.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")


def test_stage_candidate_excludes_candidate_dockerfile_from_staging(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    staging = tmp_path / "staging"
    _write_candidate(candidate, dockerfile="FROM evil\n")
    stage_candidate.stage_candidate_source(candidate, staging)
    assert not (staging / "backend/src/bridle/agent/container/agent.Dockerfile").exists()
    assert (staging / "backend/src/bridle/example.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_staged_digest_ignores_malicious_candidate_dockerfile(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    staging = tmp_path / "staging"
    _write_candidate(candidate, dockerfile="FROM evil-a\n")
    stage_candidate.stage_candidate_source(candidate, staging)
    first = stage_candidate.compute_staged_source_digest(staging)
    (candidate / "backend/src/bridle/agent/container/agent.Dockerfile").write_text("FROM evil-b\n", encoding="utf-8")
    second_staging = tmp_path / "staging-b"
    stage_candidate.stage_candidate_source(candidate, second_staging)
    second = stage_candidate.compute_staged_source_digest(second_staging)
    assert first == second


def test_protected_dockerfile_is_required_from_trusted_root() -> None:
    path = trusted_harness.protected_dockerfile_path(REPO_ROOT)
    assert path.name == "agent.Dockerfile"
    assert "protected" in path.as_posix()


def test_ruleset_spec_is_valid() -> None:
    payload = json.loads(
        (REPO_ROOT / ".github/rulesets/protected-docker-posix-gate.json").read_text(encoding="utf-8")
    )
    errors = verify_ruleset.validate_ruleset(payload, strict=False)
    assert errors == []


def test_ruleset_strict_passes_with_repository_id() -> None:
    payload = json.loads(
        (REPO_ROOT / ".github/rulesets/protected-docker-posix-gate.json").read_text(encoding="utf-8")
    )
    errors = verify_ruleset.validate_ruleset(payload, strict=True)
    assert errors == []


def test_ruleset_strict_rejects_null_repository_id() -> None:
    payload = json.loads(
        (REPO_ROOT / ".github/rulesets/protected-docker-posix-gate.json").read_text(encoding="utf-8")
    )
    for rule in payload["rules"]:
        if rule.get("type") == "workflows":
            rule["parameters"]["workflows"][0]["repository_id"] = None
    errors = verify_ruleset.validate_ruleset(payload, strict=True)
    assert "required_workflow_repository_id_missing" in errors


def test_ruleset_rejects_missing_required_workflow() -> None:
    payload = json.loads(
        (REPO_ROOT / ".github/rulesets/protected-docker-posix-gate.json").read_text(encoding="utf-8")
    )
    payload["rules"] = []
    errors = verify_ruleset.validate_ruleset(payload, strict=False)
    assert "required_workflows_missing" in errors


def test_ruleset_rejects_status_only_checks() -> None:
    payload = json.loads(
        (REPO_ROOT / ".github/rulesets/protected-docker-posix-gate.json").read_text(encoding="utf-8")
    )
    payload["rules"] = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "protected-docker-posix-gate"}]},
        }
    ]
    errors = verify_ruleset.validate_ruleset(payload, strict=False)
    assert "required_workflows_missing" in errors
    assert "status_only_checks_not_sufficient" in errors


def test_ruleset_rejects_wrong_workflow_path() -> None:
    payload = json.loads(
        (REPO_ROOT / ".github/rulesets/protected-docker-posix-gate.json").read_text(encoding="utf-8")
    )
    for rule in payload["rules"]:
        if rule.get("type") == "workflows":
            rule["parameters"]["workflows"][0]["path"] = ".github/workflows/evil.yml"
    errors = verify_ruleset.validate_ruleset(payload, strict=False)
    assert "required_workflow_path_mismatch" in errors


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics for protected dockerfile path")
def test_protected_dockerfile_rejects_parent_symlink(tmp_path: Path) -> None:
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    outside = tmp_path / "outside-scripts"
    (outside / "ci" / "protected").mkdir(parents=True)
    (outside / "ci" / "protected" / "agent.Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (trusted_root / "scripts").symlink_to(outside)
    with pytest.raises(trusted_harness.TrustedHarnessError) as exc:
        trusted_harness.protected_dockerfile_path(trusted_root)
    assert exc.value.error_code == "trusted_harness_protected_dockerfile_link_rejected"


@pytest.mark.skipif(os.name == "nt", reason="docker sandbox selection is linux-specific")
def test_worker_sandbox_uses_docker_for_main_and_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_sandbox = _load("bridle_worker_sandbox_mode", "worker_sandbox.py")
    monkeypatch.setenv("BRIDLE_RUN_DOCKER_TESTS", "1")
    monkeypatch.setenv("BRIDLE_WORKER_DOCKER_SANDBOX", "1")
    monkeypatch.delenv("BRIDLE_FORCE_SUBPROCESS_WORKER", raising=False)
    assert worker_sandbox.use_docker_sandbox(public_env={"BRIDLE_ISOLATION_PROBE": "0"}) is True
    assert worker_sandbox.use_docker_sandbox(public_env={"BRIDLE_ISOLATION_PROBE": "1"}) is True


def test_stage_candidate_uses_explicit_run_scoped_subdirectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = tmp_path / "staging-root"
    allowed.mkdir()
    monkeypatch.setenv("BRIDLE_STAGING_ROOT", str(allowed))
    candidate = tmp_path / "candidate"
    staging_target = allowed / "candidate-source-staging"
    _write_candidate(candidate)
    actual = stage_candidate.stage_candidate_source(candidate, staging_target, run_id="fixed-run")
    assert actual == staging_target.resolve()
    assert actual.is_dir()
    assert (actual / "backend/src/bridle/example.py").is_file()


def test_stage_candidate_rejects_allowed_root_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allowed = tmp_path / "staging-root"
    allowed.mkdir()
    monkeypatch.setenv("BRIDLE_STAGING_ROOT", str(allowed))
    candidate = tmp_path / "candidate"
    _write_candidate(candidate)
    actual = stage_candidate.stage_candidate_source(candidate, allowed, run_id="fixed-run")
    assert actual.parent == allowed.resolve()
    assert actual.name == "candidate-staging-fixed-run"


def test_run_register_from_candidate_is_rejected(tmp_path: Path) -> None:
    controller = _load("bridle_trusted_evidence_controller", "trusted_evidence_controller.py")
    ctx_module = _load("bridle_controller_context", "controller_context.py")
    ctx = ctx_module.ControllerExecutionContext(candidate_root=tmp_path / "candidate")
    with pytest.raises(RuntimeError, match="run_register_from_candidate_rejected"):
        controller.handle_controller_line(
            f'{run_lease.RUN_REGISTER_PREFIX}{{"it_run_id":"foreign-run"}}',
            ctx=ctx,
            trusted_scripts=SCRIPT_DIR,
        )


def test_run_lease_rejects_foreign_it_run_id(tmp_path: Path) -> None:
    ipc_dir = tmp_path / "ipc"
    ipc_dir.mkdir()
    registry = run_lease.get_registry()
    lease = registry.create_lease(candidate_root=tmp_path / "candidate", ipc_dir=ipc_dir)
    run_lease.handle_run_register_line(
        f'{run_lease.RUN_REGISTER_PREFIX}{{"it_run_id":"owned-run"}}',
        lease_id=lease.lease_id,
        ipc_dir=ipc_dir,
    )
    registry.assert_teardown_allowed(lease.lease_id, "owned-run")
    with pytest.raises(RuntimeError, match="run_teardown_foreign_it_run_id"):
        registry.assert_teardown_allowed(lease.lease_id, "foreign-run")


def test_subprocess_stream_times_out_hanging_child() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = subprocess_stream.capture_with_deadline(proc, max_bytes=65536, timeout=0.5)
    assert result.timed_out is True
    assert b"started" in result.stdout


def test_subprocess_stream_truncates_flood() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'x' * 200000)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = subprocess_stream.capture_with_deadline(proc, max_bytes=4096, timeout=5.0)
    assert result.timed_out is False
    assert len(result.stderr) <= 4096
    assert result.truncated_stderr is True
