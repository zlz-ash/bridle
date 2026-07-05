"""Tests for protected build staging and ruleset validation."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
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


def test_map_public_env_for_docker_worker_uses_host_candidate_root() -> None:
    worker_sandbox = _load("bridle_worker_sandbox_env", "worker_sandbox.py")
    host = "/home/runner/work/bridle/bridle/candidate"
    host_path = Path(host)
    mapped = worker_sandbox.map_public_env_for_docker_worker(
        {
            "BRIDLE_TRUSTED_CHECKOUT_ROOT": host,
            "BRIDLE_RUN_DOCKER_TESTS": "1",
        },
        host_path,
    )
    assert mapped["BRIDLE_TRUSTED_CHECKOUT_ROOT"] == host_path.resolve().as_posix()
    assert mapped["BRIDLE_RUN_DOCKER_TESTS"] == "1"


@pytest.mark.skipif(os.name == "nt", reason="docker sandbox path mapping is linux-specific")
def test_map_paths_for_isolated_worker_uses_inner_candidate_root() -> None:
    worker_sandbox = _load("bridle_worker_sandbox_inner", "worker_sandbox.py")
    host = Path("/home/runner/work/bridle/bridle/candidate")
    isolated = worker_sandbox.IsolatedDockerContext(
        docker_host="tcp://bridle-dind-test:2375",
        network="bridle-net-test",
        dind_name="bridle-dind-test",
    )
    mapped = worker_sandbox.map_paths_for_sandbox(
        worker_sandbox.SandboxPaths(
            candidate_root=host,
            trusted_config=host / "backend/pyproject.toml",
            trusted_scripts=Path("/trusted-scripts"),
        ),
        public_env={"BRIDLE_ISOLATION_PROBE": "0"},
        isolated=isolated,
    )
    assert mapped.candidate_root == worker_sandbox.INNER_CANDIDATE_ROOT


def test_isolated_docker_candidate_mount_uses_inner_root() -> None:
    isolated_docker = _load("bridle_isolated_docker_mount", "isolated_docker.py")
    host = "/home/runner/work/bridle/bridle/candidate"
    mounts = isolated_docker._candidate_bind_mounts(host)
    assert len(mounts) == 2
    assert f"target={isolated_docker.INNER_CANDIDATE_ROOT}" in mounts[1]
    assert "bind-propagation=rshared" in mounts[1]


def test_docker_worker_run_identity_uses_host_uid() -> None:
    worker_sandbox = _load("bridle_worker_sandbox_identity", "worker_sandbox.py")
    host_uid = os.getuid() if hasattr(os, "getuid") else 1000
    host_gid = os.getgid() if hasattr(os, "getgid") else 1000
    assert worker_sandbox.docker_worker_run_identity({"BRIDLE_RUN_DOCKER_TESTS": "1"}) == (host_uid, host_gid)


def test_parse_docker_load_id_ignores_tag_only_output() -> None:
    isolated_docker = _load("bridle_isolated_docker_load_parse", "isolated_docker.py")
    host_digest = "sha256:b7443443752b45b130caec7fb259802fad77f34e396dfca0545aad1ad3c11ab3"
    tag_only = "Loaded image: bridle-agent:review-2eb2a1d0ee39\n"
    assert isolated_docker._parse_docker_load_id(tag_only) is None
    id_line = f"Loaded image ID: {host_digest}\n"
    assert isolated_docker._parse_docker_load_id(id_line) == host_digest


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


def test_stage_candidate_writes_ownership_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRIDLE_STAGING_ROOT", raising=False)
    candidate = tmp_path / "candidate"
    staging = tmp_path / "staging"
    _write_candidate(candidate)
    stage_candidate.stage_candidate_source(candidate, staging, run_id="run-A")
    ownership = staging / stage_candidate.OWNERSHIP_FILE
    assert ownership.is_file()
    payload = json.loads(ownership.read_text(encoding="utf-8"))
    assert payload["schema"] == stage_candidate.OWNERSHIP_SCHEMA
    assert payload["run_id"] == "run-A"
    assert payload["path"] == str(staging.resolve())


def test_stage_candidate_releases_existing_with_same_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRIDLE_STAGING_ROOT", raising=False)
    candidate = tmp_path / "candidate"
    staging = tmp_path / "staging"
    _write_candidate(candidate)
    first = stage_candidate.stage_candidate_source(candidate, staging, run_id="run-A")
    assert first.is_dir()
    (staging / "leftover.txt").write_text("old\n", encoding="utf-8")
    second = stage_candidate.stage_candidate_source(candidate, staging, run_id="run-A")
    assert second == staging.resolve()
    assert not (staging / "leftover.txt").exists()


def test_stage_candidate_rejects_foreign_run_id_on_existing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRIDLE_STAGING_ROOT", raising=False)
    candidate = tmp_path / "candidate"
    staging = tmp_path / "staging"
    _write_candidate(candidate)
    stage_candidate.stage_candidate_source(candidate, staging, run_id="run-A")
    with pytest.raises(stage_candidate.StageCandidateError) as exc:
        stage_candidate.stage_candidate_source(candidate, staging, run_id="run-B")
    assert exc.value.error_code == "stage_candidate_foreign_staging"


def test_stage_candidate_rejects_existing_without_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRIDLE_STAGING_ROOT", raising=False)
    candidate = tmp_path / "candidate"
    staging = tmp_path / "staging"
    _write_candidate(candidate)
    stage_candidate.stage_candidate_source(candidate, staging, run_id="run-A")
    with pytest.raises(stage_candidate.StageCandidateError) as exc:
        stage_candidate.stage_candidate_source(candidate, staging, run_id=None)
    assert exc.value.error_code == "stage_candidate_existing_without_run_id"


def test_staging_tar_path_fails_closed_without_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated_docker = _load("bridle_isolated_docker_tar_fail", "isolated_docker.py")
    for key in ("BRIDLE_STAGING_ROOT", "BRIDLE_RUNNER_TEMP", "RUNNER_TEMP", "TMPDIR"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(isolated_docker.IsolatedDockerError) as exc:
        isolated_docker._staging_tar_path(".tar")
    assert exc.value.error_code == "isolated_docker_staging_root_missing"


def test_staging_tar_path_uses_allowed_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    isolated_docker = _load("bridle_isolated_docker_tar_root", "isolated_docker.py")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("BRIDLE_STAGING_ROOT", str(allowed))
    for key in ("BRIDLE_RUNNER_TEMP", "RUNNER_TEMP", "TMPDIR"):
        monkeypatch.delenv(key, raising=False)
    path = isolated_docker._staging_tar_path(".tar")
    assert path.parent == allowed
    assert path.name.endswith(".tar")


def test_release_review_image_rejects_foreign_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trusted_harness_local = _load("bridle_trusted_harness_release", "trusted_harness.py")
    registry = trusted_harness_local._DOCKER_REGISTRY
    registry.register_tag(run_id="run-A", tag="bridle-agent:review-x", image_id="sha256:aaa")
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=60):
        calls.append(args)
        class R:
            returncode = 0
            stdout = "sha256:bbb\n"
            stderr = ""
        return R()

    monkeypatch.setattr(trusted_harness_local, "_run", fake_run)
    with pytest.raises(RuntimeError, match="foreign_tag_owner"):
        trusted_harness_local.release_review_image("bridle-agent:review-x", run_id="run-B")


def test_release_review_image_verifies_identity_then_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trusted_harness_local = _load("bridle_trusted_harness_release_ok", "trusted_harness.py")
    registry = trusted_harness_local._DOCKER_REGISTRY
    registry.register_tag(run_id="run-A", tag="bridle-agent:review-x", image_id="sha256:aaa")
    calls: list[list[str]] = []

    def fake_run(args, *, timeout=60):
        calls.append(args)
        class R:
            returncode = 0
            stdout = "sha256:aaa\n"
            stderr = ""
        return R()

    monkeypatch.setattr(trusted_harness_local, "_run", fake_run)
    trusted_harness_local.release_review_image("bridle-agent:review-x", run_id="run-A")
    assert any("rm" in args and "-f" in args for args in calls)
    with pytest.raises(RuntimeError, match="tag_not_registered"):
        registry.verify_tag(run_id="run-A", tag="bridle-agent:review-x", image_id="sha256:aaa")


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


def test_handle_controller_line_accepts_embedded_sentinel_request(tmp_path: Path) -> None:
    controller = _load("bridle_trusted_evidence_controller_embedded", "trusted_evidence_controller.py")
    ctx_module = _load("bridle_controller_context_embedded", "controller_context.py")
    candidate = tmp_path / "candidate"
    target = candidate / "backend/.test-workspaces/ws/outside.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret\n", encoding="utf-8")
    ipc_dir = tmp_path / "ipc"
    ipc_dir.mkdir()
    ctx = ctx_module.ControllerExecutionContext(candidate_root=candidate, controller_ipc_dir=ipc_dir)
    request_id = "a3238f9e5ace"
    relative = "backend/.test-workspaces/ws/outside.txt"
    line = (
        "backend/tests/agent/container/test_docker_integration.py::"
        "TestDockerCandidateIntegration::test_real_docker_recovers_after_link_attack_in_slot "
        f'{controller.SENTINEL_REQUEST_PREFIX}{{"candidate_relative": "{relative}", "request_id": "{request_id}"}}'
    )
    controller.handle_controller_line(line, ctx=ctx, trusted_scripts=SCRIPT_DIR)
    ack_path = ipc_dir / "sentinel-acks" / f"{request_id}.json"
    assert ack_path.is_file()
    payload = json.loads(ack_path.read_text(encoding="utf-8"))
    assert payload["status"] == "registered"
    assert payload["request_id"] == request_id
    assert isinstance(payload.get("handle"), str) and payload["handle"]


def test_poll_sentinel_request_files_writes_ack(tmp_path: Path) -> None:
    controller = _load("bridle_trusted_evidence_controller_poll", "trusted_evidence_controller.py")
    ctx_module = _load("bridle_controller_context_poll", "controller_context.py")
    candidate = tmp_path / "candidate"
    target = candidate / "backend/.test-workspaces/ws/outside.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret\n", encoding="utf-8")
    ipc_dir = tmp_path / "ipc"
    requests_dir = ipc_dir / "sentinel-requests"
    acks_dir = ipc_dir / "sentinel-acks"
    requests_dir.mkdir(parents=True)
    acks_dir.mkdir(parents=True)
    ctx = ctx_module.ControllerExecutionContext(candidate_root=candidate, controller_ipc_dir=ipc_dir)
    request_id = "e960bbcd5c16"
    relative = "backend/.test-workspaces/ws/outside.txt"
    (requests_dir / f"{request_id}.json").write_text(
        json.dumps({"request_id": request_id, "candidate_relative": relative}, sort_keys=True),
        encoding="utf-8",
    )
    controller.poll_sentinel_request_files(ipc_dir=ipc_dir, ctx=ctx, trusted_scripts=SCRIPT_DIR)
    ack_path = acks_dir / f"{request_id}.json"
    assert ack_path.is_file()
    payload = json.loads(ack_path.read_text(encoding="utf-8"))
    assert payload["status"] == "registered"
    assert payload["request_id"] == request_id


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
