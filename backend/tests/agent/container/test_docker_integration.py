"""Opt-in Docker integration tests for candidate container execution."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from bridle.agent.container.active_slot import (
    build_slot_mounts,
    prepare_active_slot,
    rw_mount_baseline_path,
    slot_allowed_mount_roots,
)
from bridle.agent.container.backend import AgentContainerBackend
from bridle.agent.container.candidate_service import CandidateExecutionService
from bridle.agent.container.container_identity import (
    build_container_labels as _build_container_labels,
)
from bridle.agent.container.container_identity import (
    project_label,
)
from bridle.agent.container.image_identity import resolve_image_identity
from bridle.agent.container.lifecycle import ModuleContainerRegistry, build_module_container_name
from bridle.agent.container.runner import ContainerMount, ContainerRequest, LocalContainerRuntimeRunner

from .docker_evidence import (
    publish_failed_evidence,
    publish_passed_evidence,
    record_pending_primary,
)
from .docker_test_resources import (
    IMAGE_IDENTITY_MISMATCH,
    IT_LABEL,
    IT_TEST_IDENTITY_LABEL,
    assert_image_absent,
    assert_run_teardown_clean,
    assert_tag_absent,
    cleanup_registered_image,
    finalize_run_teardown,
    list_containers_for_run,
    list_images_for_run,
    query_image_identity,
    register_built_image,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("BRIDLE_RUN_DOCKER_TESTS") != "1",
    reason="Set BRIDLE_RUN_DOCKER_TESTS=1 to run Docker integration tests",
)

PYTEST_CMD = "python -m pytest tests/test_isolation.py -q"
CHMOD_POISON_CMD = (
    "python -m pytest tests/test_chmod_poison.py -q -s --capture=no "
    "-p no:cacheprovider --basetemp=/tmp/bridle-chmod-pytest"
)
CHMOD_POISON_REPORT_PREFIX = "BRIDLE_CHMOD_POISON_REPORT:"
LINK_ATTACK_CMD = "python -m pytest tests/test_link_attack.py -q -s --capture=no"
LINK_ATTACK_REPORT_PREFIX = "BRIDLE_LINK_ATTACK_REPORT:"
CRITICAL_EVIDENCE_PREFIX = "BRIDLE_CRITICAL_EVIDENCE:"
SENTINEL_READY_PREFIX = "BRIDLE_SENTINEL_READY:"
SENTINEL_REQUEST_PREFIX = "BRIDLE_SENTINEL_REQUEST:"
RUN_REGISTER_PREFIX = "BRIDLE_RUN_REGISTER:"
CONTROLLER_IPC_ROOT = Path("/controller-ipc")
CRITICAL_EVIDENCE_ACK_ROOT = CONTROLLER_IPC_ROOT / "critical-evidence-acks"
TAMPER_CMD = "python -m pytest tests/test_tamper_baseline.py -q"
_KEEP_ALIVE = ["python", "-m", "bridle.agent.container.entrypoint", "--keep-alive"]


@dataclass(frozen=True)
class _TrustedReviewImageRef:
    tag: str
    image_digest: str


@pytest.fixture(scope="session")
def review_agent_image():
    tag = os.environ.get("BRIDLE_AGENT_IMAGE", "").strip()
    image_digest = os.environ.get("BRIDLE_REVIEW_IMAGE_DIGEST", "").strip()
    if not tag or not image_digest.startswith("sha256:") or len(image_digest) != 71:
        pytest.fail("trusted review image identity was not injected by the protected workflow")
    return _TrustedReviewImageRef(tag=tag, image_digest=image_digest)


def _container_ids_match(left: str, right: str) -> bool:
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return longer.startswith(shorter)


def build_container_labels(*, it_run_id: str | None = None, **kwargs):
    labels = _build_container_labels(**kwargs)
    if it_run_id:
        labels[IT_LABEL] = it_run_id
    return labels


@pytest.fixture
def it_run_id() -> str:
    issued = os.environ.get("BRIDLE_IT_RUN_ID", "").strip()
    run_id = issued or uuid.uuid4().hex[:12]
    yield run_id
    if os.environ.get("BRIDLE_CANDIDATE_WORKER") == "1":
        return
    teardown = finalize_run_teardown(run_id)
    assert_run_teardown_clean(teardown)


@pytest.fixture(autouse=True)
def inject_it_labels(monkeypatch: pytest.MonkeyPatch, it_run_id: str) -> None:
    def _labels(**kwargs):
        return build_container_labels(it_run_id=it_run_id, **kwargs)

    monkeypatch.setattr(
        "bridle.agent.container.container_identity.build_container_labels",
        _labels,
    )
    monkeypatch.setattr(
        "bridle.agent.container.backend.build_container_labels",
        _labels,
    )


def _cleanup_it_containers(run_id: str) -> None:
    from .docker_test_resources import cleanup_containers_for_run

    for result in cleanup_containers_for_run(run_id):
        if result.status in {"failed", "query_failed"}:
            raise AssertionError(
                f"container cleanup failed for {run_id}: status={result.status} "
                f"code={result.error_code} detail={result.detail}"
            )


def _count_it_label_containers(run_id: str) -> int:
    container_ids, list_error = list_containers_for_run(run_id)
    if list_error is not None:
        raise AssertionError(
            f"docker container list failed for {run_id}: phase={list_error.phase} "
            f"stderr={list_error.stderr.strip() or list_error.error}"
        )
    return len(container_ids)


def _assert_it_label_containers_zero(run_id: str) -> None:
    assert _count_it_label_containers(run_id) == 0, (
        f"expected zero containers with label {IT_LABEL}={run_id}, "
        f"found {_count_it_label_containers(run_id)}"
    )


@pytest.fixture
def docker_available() -> None:
    if shutil.which("docker") is None:
        pytest.fail("docker executable not found while BRIDLE_RUN_DOCKER_TESTS=1")


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_for_node(test_workspace: Path, node: dict) -> dict:
    impl_path = node["files"][0]
    impl_file = test_workspace / impl_path
    impl_file.parent.mkdir(parents=True, exist_ok=True)
    impl_file.write_text("x = 1\n", encoding="utf-8")
    digest = _file_hash(impl_file)
    return {
        "module_id": node["id"],
        "node_id": node["id"],
        "implementation_entities": [
            {"entity_id": f"file:{impl_path}", "path": impl_path, "kind": "file", "file_hash": digest},
        ],
        "test_entities": [],
        "test_commands": list(node.get("tests") or []),
        "interfaces": [],
    }


def _cleanup_module_containers(test_workspace: Path, module_id: str) -> None:
    proj = project_label(test_workspace)
    listed = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=bridle.project={proj}",
            "--filter",
            f"label=bridle.module={module_id}",
        ],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return
    for cid in [line.strip() for line in listed.stdout.splitlines() if line.strip()]:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def _prepare_candidate(
    module_root: Path,
    candidate_id: str,
    *,
    marker: str,
    sibling_id: str | None = None,
) -> tuple[str, Path]:
    rel = f"candidates/{candidate_id}"
    candidate = module_root / rel
    for sub in ("project", "baseline", "output", "diagnostics", "mocks"):
        (candidate / sub).mkdir(parents=True, exist_ok=True)
    tests = candidate / "project" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    sibling_lines = ""
    if sibling_id:
        sibling_lines = f"""
    assert not Path("../../{sibling_id}/project/secret.txt").exists()
    assert not Path("/workspace/project/../../{sibling_id}/project/secret.txt").exists()
"""
    (tests / "test_isolation.py").write_text(
        f"""
from pathlib import Path

def test_no_sibling_leak():
{sibling_lines}
    assert Path("/workspace/project/marker.txt").read_text(encoding="utf-8") == "{marker}\\n"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (candidate / "project" / "marker.txt").write_text(f"{marker}\n", encoding="utf-8")
    (candidate / "baseline" / "tests").mkdir(parents=True, exist_ok=True)
    (candidate / "baseline" / "tests" / "test_ok.py").write_text(
        "def test_ok(): assert True\n", encoding="utf-8"
    )
    return rel, candidate


def _prepare_chmod_poison_candidate(
    module_root: Path,
    candidate_id: str,
    *,
    marker: str,
) -> tuple[str, Path]:
    rel, candidate = _prepare_candidate(module_root, candidate_id, marker=marker)
    tests = candidate / "project" / "tests"
    (tests / "test_chmod_poison.py").write_text(
        f"""
import errno
import json
import os

REPORT_PREFIX = "{CHMOD_POISON_REPORT_PREFIX}"

def _poison_mount_root(mount_path: str) -> dict:
    uid = os.getuid()
    gid = os.getgid()
    st = os.stat(mount_path)
    before = st.st_mode & 0o777
    entry = {{
        "path": mount_path,
        "uid": uid,
        "gid": gid,
        "before_mode": before,
        "owner_uid": int(st.st_uid),
        "owner_gid": int(st.st_gid),
    }}
    fd = None
    try:
        fd = os.open(mount_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fchmod(fd, 0)
        except OSError:
            os.chmod(mount_path, 0)
        after_mode = os.fstat(fd).st_mode & 0o777
        if after_mode == 0:
            entry["rc"] = 0
            entry["after_mode"] = 0
        else:
            entry["rc"] = errno.EPERM
            entry["after_mode"] = after_mode
            entry["error"] = "chmod_succeeded_but_mode_not_zero"
    except OSError as exc:
        entry["rc"] = exc.errno
        entry["error"] = str(exc)
    finally:
        if fd is not None:
            os.close(fd)
    return entry

def test_container_chmods_rw_mount_roots():
    results = []
    for mount_path in ("/workspace/output", "/workspace/diagnostics", "/workspace/project"):
        results.append(_poison_mount_root(mount_path))
    report = {{"uid": os.getuid(), "gid": os.getgid(), "results": results}}
    print(REPORT_PREFIX + json.dumps(report), flush=True)
    try:
        project = next(item for item in results if item["path"] == "/workspace/project")
        assert project.get("rc") == 0, project
        assert project.get("after_mode") == 0, project
    finally:
        for item in results:
            before = item.get("before_mode")
            if before is not None:
                try:
                    os.chmod(item["path"], int(before))
                except OSError:
                    pass
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return rel, candidate


def _parse_chmod_poison_report(run_result: dict) -> dict:
    for item in run_result.get("test_results") or []:
        stdout = item.get("stdout") or ""
        for line in stdout.splitlines():
            if line.startswith(CHMOD_POISON_REPORT_PREFIX):
                return json.loads(line[len(CHMOD_POISON_REPORT_PREFIX) :])
    raise AssertionError("missing chmod poison report in container stdout")


def _prepare_link_attack_candidate(
    module_root: Path,
    candidate_id: str,
    *,
    outside: Path,
) -> tuple[str, Path]:
    rel, candidate = _prepare_candidate(module_root, candidate_id, marker="attack")
    target = str(outside.resolve())
    (candidate / "project" / "tests" / "test_link_attack.py").write_text(
        f"""
import json
import os
from pathlib import Path

REPORT_PREFIX = "{LINK_ATTACK_REPORT_PREFIX}"

def test_container_creates_slot_escape_symlinks():
    uid = os.getuid()
    target = {target!r}
    links = [
        ("attack.txt", "/workspace/project/attack.txt"),
        ("escape.txt", "/workspace/output/escape.txt"),
    ]
    results = []
    for name, link_path in links:
        entry = {{"name": name, "link_path": link_path, "target": target, "uid": uid}}
        link = Path(link_path)
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            os.symlink(target, link_path)
            entry["symlink_rc"] = 0
        except OSError as exc:
            entry["symlink_rc"] = exc.errno
            entry["symlink_error"] = str(exc)
        try:
            st = os.lstat(link_path)
            entry["lstat_is_symlink"] = Path(link_path).is_symlink()
            entry["lstat_mode"] = st.st_mode
        except OSError as exc:
            entry["lstat_error"] = str(exc)
        results.append(entry)
    report = {{"uid": uid, "results": results}}
    print(REPORT_PREFIX + json.dumps(report), flush=True)
    assert all(item.get("symlink_rc") == 0 for item in results), results
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return rel, candidate


def _candidate_root_for_relative_paths() -> Path:
    container_root = os.environ.get("BRIDLE_CANDIDATE_CONTAINER_ROOT")
    if container_root:
        return Path(container_root).resolve()
    return Path(os.environ["BRIDLE_TRUSTED_CHECKOUT_ROOT"]).resolve()


def _await_controller_sentinel_ack(outside: Path) -> str:
    candidate_root = _candidate_root_for_relative_paths()
    container_root = os.environ.get("BRIDLE_CANDIDATE_CONTAINER_ROOT", "")
    request_id = uuid.uuid4().hex[:16]
    request_dir = CONTROLLER_IPC_ROOT / "sentinel-requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    resolved_outside = outside.resolve()
    if container_root and resolved_outside.is_absolute() and str(resolved_outside).startswith(container_root):
        container_path = str(resolved_outside)
    else:
        container_path = ""
    candidate_relative = resolved_outside.relative_to(candidate_root).as_posix()
    request_path = request_dir / f"{request_id}.json"
    request_path.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "candidate_relative": candidate_relative,
                "container_path": container_path,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    ack_path = CONTROLLER_IPC_ROOT / "sentinel-acks" / f"{request_id}.json"
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if ack_path.is_file():
            payload = json.loads(ack_path.read_text(encoding="utf-8"))
            handle = payload.get("handle")
            if payload.get("status") == "registered" and isinstance(handle, str) and handle:
                return handle
        time.sleep(0.05)
    pytest.fail(f"sentinel_ack_timeout request_id={request_id}")


def _parse_link_attack_report(run_result: dict) -> dict:
    for item in run_result.get("test_results") or []:
        stdout = item.get("stdout") or ""
        for line in stdout.splitlines():
            if line.startswith(LINK_ATTACK_REPORT_PREFIX):
                return json.loads(line[len(LINK_ATTACK_REPORT_PREFIX) :])
    raise AssertionError("missing link attack report in container stdout")


def _emit_worker_critical_evidence(payload: dict) -> None:
    request_id: str | None = None
    primary = payload.get("primary")
    if (
        payload.get("test_key") == "link_attack"
        and isinstance(primary, dict)
        and primary.get("sentinel_handle")
    ):
        request_id = uuid.uuid4().hex[:16]
        payload["controller_request_id"] = request_id
    print(CRITICAL_EVIDENCE_PREFIX + json.dumps(payload, sort_keys=True), flush=True)
    if request_id is None:
        return
    ack_path = CRITICAL_EVIDENCE_ACK_ROOT / f"{request_id}.json"
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if ack_path.is_file():
            ack = json.loads(ack_path.read_text(encoding="utf-8"))
            if (
                ack.get("status") == "verified"
                and ack.get("request_id") == request_id
                and ack.get("sentinel_handle") == primary["sentinel_handle"]
            ):
                return
            pytest.fail(f"critical_evidence_ack_invalid request_id={request_id}")
        time.sleep(0.05)
    pytest.fail(f"critical_evidence_ack_timeout request_id={request_id}")


def _finalize_critical_test_evidence(
    *,
    test_workspace: Path,
    it_run_id: str,
    module_id: str,
    test_key: str,
    test_node_id: str,
    image_version: str,
    primary: dict,
    extra: dict | None = None,
    primary_error: str | None = None,
) -> None:
    _cleanup_it_containers(it_run_id)
    _cleanup_module_containers(test_workspace, module_id)
    _assert_it_label_containers_zero(it_run_id)
    teardown = finalize_run_teardown(it_run_id)
    try:
        assert_run_teardown_clean(teardown)
    except AssertionError as exc:
        if os.environ.get("BRIDLE_CANDIDATE_WORKER") == "1":
            payload = {
                "test_key": test_key,
                "test_node_id": test_node_id,
                "image_digest": image_version,
                "status": "failed",
                "primary": primary or {"error": primary_error or "unknown"},
                "error": primary_error or str(exc),
                "teardown": {
                    "owner_run_id": it_run_id,
                    "remaining_container_count": -1,
                    "remaining_image_count": -1,
                    "remaining_image_registry_count": -1,
                    "remaining_tag_registry_count": -1,
                    "query_failures": ["cleanup_failed"],
                },
            }
            _emit_worker_critical_evidence(payload)
            raise
        if primary or primary_error:
            publish_failed_evidence(
                test_key,
                test_node_id=test_node_id,
                image_digest=image_version,
                primary=primary or {"error": primary_error or "unknown"},
                error=primary_error or "primary_assertion_failed",
                cleanup_failure=str(exc),
            )
        raise
    if primary:
        if os.environ.get("BRIDLE_CANDIDATE_WORKER") == "1":
            payload = {
                "test_key": test_key,
                "test_node_id": test_node_id,
                "image_digest": image_version,
                "status": "passed",
                "primary": primary,
                "teardown": {
                    "owner_run_id": teardown.owner_run_id,
                    "remaining_container_count": teardown.remaining_container_count,
                    "remaining_image_count": teardown.remaining_image_count,
                    "remaining_image_registry_count": teardown.remaining_image_registry_count,
                    "remaining_tag_registry_count": teardown.remaining_tag_registry_count,
                    "query_failures": list(teardown.query_failures),
                },
            }
            _emit_worker_critical_evidence(payload)
            return
        publish_passed_evidence(
            test_key,
            test_node_id=test_node_id,
            image_digest=image_version,
            primary=primary,
            teardown_result=teardown,
            extra=extra,
        )


def _prepare_tamper_candidate(module_root: Path, candidate_id: str) -> tuple[str, Path]:
    rel = f"candidates/{candidate_id}"
    candidate = module_root / rel
    for sub in ("project", "baseline", "output", "diagnostics", "mocks"):
        (candidate / sub).mkdir(parents=True, exist_ok=True)
    tests = candidate / "project" / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_tamper_baseline.py").write_text(
        """
from pathlib import Path

def test_baseline_is_read_only():
    target = Path("/workspace/baseline/tests/evil.txt")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("tampered\\n", encoding="utf-8")
        tampered = True
    except OSError:
        tampered = False
    assert not tampered, "baseline mount must reject writes"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (candidate / "baseline" / "tests").mkdir(parents=True, exist_ok=True)
    (candidate / "baseline" / "tests" / "test_ok.py").write_text(
        "def test_ok(): assert True\n", encoding="utf-8"
    )
    (candidate / "mocks" / "iface.py").write_text("# mock\n", encoding="utf-8")
    return rel, candidate


def _run_backend_case(
    backend: AgentContainerBackend,
    *,
    module_root: Path,
    candidate: Path,
    rel: str,
    module_id: str,
    boundary_fp: str,
    run_id: str,
    image: str,
    image_version: str,
    test_commands: list[str],
    write_set: list[str],
    replace_container: bool = False,
) -> dict:
    from bridle.agent.container.backend import AgentContainerError
    try:
        return backend.run_tests_in_candidate(
            candidate_root=candidate,
            module_root=module_root,
            candidate_rel=rel,
            run_id=run_id,
            node_id=f"node-{run_id}",
            module_id=module_id,
            boundary_fingerprint=boundary_fp,
            test_commands=test_commands,
            write_set=write_set,
            test_entity_id=f"node-{run_id}",
            map_seq=1,
            image=image,
            image_version=image_version,
            timeout_seconds=180,
            replace_container=replace_container,
        )
    except AgentContainerError as exc:
        detail = exc.detail or {}
        stdout = str(detail.get("stdout") or "")
        stderr = str(detail.get("stderr") or "")
        if stdout or stderr:
            print(f"BRIDLE_BACKEND_DIAGNOSTICS:run_id={run_id} error={exc.error_code}")
            for line in stdout.splitlines():
                print(f"BRIDLE_BACKEND_STDOUT:{line}")
            for line in stderr.splitlines():
                print(f"BRIDLE_BACKEND_STDERR:{line}")
        raise


def _module_request(
    test_workspace: Path,
    module_root: Path,
    candidate: Path,
    *,
    module_id: str,
    boundary_fp: str,
    image: str,
    image_version: str,
    candidate_rel: str,
) -> ContainerRequest:
    layout = prepare_active_slot(
        module_root,
        candidate,
        project_root=test_workspace,
        candidate_rel=candidate_rel,
        run_id="adopt-check",
    )
    slot_mounts = build_slot_mounts(layout)
    labels = build_container_labels(
        project_root=test_workspace,
        module_id=module_id,
        boundary_fingerprint=boundary_fp,
        image_version=image_version,
        mounts=slot_mounts,
    )
    resolved_image_id = resolve_image_identity(image)
    return ContainerRequest(
        name=build_module_container_name(
            project_root=test_workspace,
            module_id=module_id,
            boundary_fingerprint=boundary_fp,
            image_version=resolved_image_id,
        ),
        image=image,
        image_id=resolved_image_id,
        run_user="1000",
        network_mode="none",
        mounts=slot_mounts,
        role="agent",
        allowed_mount_roots=slot_allowed_mount_roots(layout),
        module_id=module_id,
        boundary_fingerprint=boundary_fp,
        image_version=resolved_image_id,
        module_mount_root=str(layout.slot_root),
        keep_alive=True,
        read_only_root=True,
        command=_KEEP_ALIVE,
        labels=labels,
    )


def _create_identity_decoy_container(
    test_workspace: Path,
    module_root: Path,
    candidate: Path,
    *,
    candidate_rel: str,
    module_id: str,
    boundary_fp: str,
    image: str,
    it_run_id: str,
    kind: str,
) -> str:
    from dataclasses import replace

    base = _module_request(
        test_workspace,
        module_root,
        candidate,
        module_id=module_id,
        boundary_fp=boundary_fp,
        image=image,
        image_version=resolve_image_identity(image),
        candidate_rel=candidate_rel,
    )
    suffix = uuid.uuid4().hex[:8]
    decoy_name = f"{base.name}-decoy-{kind}-{suffix}"
    if kind == "user":
        decoy = replace(base, name=decoy_name, run_user="0")
    elif kind == "rootfs":
        decoy = replace(base, name=decoy_name, read_only_root=False)
    elif kind == "command":
        decoy = replace(base, name=decoy_name, command=["sleep", "infinity"])
    else:
        raise ValueError(f"unknown decoy kind: {kind}")
    labels = {**decoy.labels, IT_LABEL: it_run_id}
    runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, executable="docker", use_docker=True)
    created = runner.create(replace(decoy, labels=labels))
    runner.start(created.container_id)
    return created.container_id


def _mutate_create_command(cmd: list[str], *, name: str, kind: str) -> list[str]:
    mutated = list(cmd)
    name_idx = mutated.index("--name")
    mutated[name_idx + 1] = name
    if kind == "privileged":
        network_idx = mutated.index("--network")
        mutated.insert(network_idx, "--privileged")
    elif kind == "cap_drop":
        while True:
            try:
                idx = mutated.index("--cap-drop")
                mutated.pop(idx)
                mutated.pop(idx)
            except ValueError:
                break
    elif kind == "security_opt":
        while True:
            try:
                idx = mutated.index("--security-opt")
                mutated.pop(idx)
                mutated.pop(idx)
            except ValueError:
                break
    elif kind == "pids":
        idx = mutated.index("--pids-limit")
        mutated[idx + 1] = "0"
    elif kind == "memory":
        idx = mutated.index("--memory")
        mutated[idx + 1] = "128m"
    elif kind == "cpus":
        idx = mutated.index("--cpus")
        mutated[idx + 1] = "2.0"
    elif kind == "duplicate":
        pass
    else:
        raise ValueError(f"unknown hardening decoy kind: {kind}")
    return mutated


def _create_hardening_decoy_container(
    test_workspace: Path,
    module_root: Path,
    candidate: Path,
    *,
    candidate_rel: str,
    module_id: str,
    boundary_fp: str,
    image: str,
    it_run_id: str,
    kind: str,
) -> str:
    from dataclasses import replace

    base = _module_request(
        test_workspace,
        module_root,
        candidate,
        module_id=module_id,
        boundary_fp=boundary_fp,
        image=image,
        image_version=resolve_image_identity(image),
        candidate_rel=candidate_rel,
    )
    suffix = uuid.uuid4().hex[:8]
    decoy_name = f"{base.name}-decoy-{kind}-{suffix}"
    runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, executable="docker", use_docker=True)
    cmd = _mutate_create_command(
        runner.build_create_command(replace(base, name=decoy_name)),
        name=decoy_name,
        kind=kind,
    )
    for idx, token in enumerate(cmd):
        if token == "--label" and idx + 1 < len(cmd) and cmd[idx + 1].startswith(f"{IT_LABEL}="):
            cmd[idx + 1] = f"{IT_LABEL}={it_run_id}"
            break
    else:
        cmd.extend(["--label", f"{IT_LABEL}={it_run_id}"])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "decoy_create_failed")
    container_id = result.stdout.strip()
    subprocess.run(["docker", "start", container_id], capture_output=True, check=True)
    return container_id


class TestDockerCandidateIntegration:
    def test_module_container_name_stable(self, test_workspace: Path, docker_available: None) -> None:
        service = CandidateExecutionService(test_workspace)
        node = {
            "id": "node-1",
            "files": ["pkg/mod.py"],
            "tests": [PYTEST_CMD],
        }
        snapshot = _snapshot_for_node(test_workspace, node)
        first = service.prepare(
            run_id="r1",
            node=node,
            base_map_seq=1,
            readonly_files=[],
            map_snapshot=snapshot,
        )
        second = service.prepare(
            run_id="r2",
            node=node,
            base_map_seq=1,
            readonly_files=[],
            candidate_id=first.candidate_id,
            map_snapshot=snapshot,
        )
        assert first.boundary_fingerprint == second.boundary_fingerprint
        key = ModuleContainerRegistry.registry_key(
            project_id=str(test_workspace),
            module_id=first.module_id,
            boundary_fingerprint=first.boundary_fingerprint,
            image_version=first.request.image_version,
        )
        assert key

    def test_local_runner_hardening_snapshot(self, test_workspace: Path, docker_available: None) -> None:
        runner = LocalContainerRuntimeRunner(workspace_root=test_workspace, executable="docker", use_docker=False)
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / "mod"
        module_root.mkdir(parents=True)
        slot = module_root / "_active" / "project"
        slot.mkdir(parents=True)
        mount = ContainerMount(source=slot, target="/workspace/project", readonly=False)
        labels = build_container_labels(
            project_root=test_workspace,
            module_id="mod",
            boundary_fingerprint="abc123",
            image_version="local",
            mounts=[mount],
        )
        request = ContainerRequest(
            name=build_module_container_name(
                project_root=test_workspace,
                module_id="mod",
                boundary_fingerprint="abc123",
                image_version="local",
            ),
            image="bridle-agent:local",
            network_mode="none",
            mounts=[mount],
            role="agent",
            allowed_mount_roots=[str(module_root / "_active")],
            module_id="mod",
            boundary_fingerprint="abc123",
            module_mount_root=str(module_root / "_active"),
            keep_alive=True,
            labels=labels,
        )
        cmd = runner.build_create_command(request)
        joined = " ".join(cmd)
        assert "--network none" in joined
        assert "--read-only" in joined
        assert "bridle.module=mod" in joined
        assert "BRIDLE_AGENT_API_KEY" not in joined

    def test_backend_uses_real_docker_runner_when_opted_in(
        self, test_workspace: Path, docker_available: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        backend = AgentContainerBackend(test_workspace)
        assert isinstance(backend._runner, LocalContainerRuntimeRunner)
        assert backend._runner.use_docker is True

    def test_real_docker_production_chain_isolation_and_adopt(
        self,
        test_workspace: Path,
        docker_available: None,
        monkeypatch: pytest.MonkeyPatch,
        it_run_id: str,
        review_agent_image,
    ) -> None:
        image = review_agent_image.tag
        image_version = review_agent_image.image_digest

        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        run_id = uuid.uuid4().hex[:12]
        module_id = f"docker-it-{run_id}"
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / module_id
        module_root.mkdir(parents=True, exist_ok=True)
        boundary_fp = f"fp-{run_id}"
        _cleanup_module_containers(test_workspace, module_id)

        backend = AgentContainerBackend(test_workspace)
        first_container_id: str | None = None
        b_secret_hash: str | None = None

        try:
            for candidate_id, marker in (("cand-a", "alpha"), ("cand-b", "beta")):
                sibling = "cand-b" if candidate_id == "cand-a" else "cand-a"
                rel, candidate = _prepare_candidate(
                    module_root,
                    candidate_id,
                    marker=marker,
                    sibling_id=sibling,
                )
                if candidate_id == "cand-b":
                    (candidate / "project" / "secret.txt").write_text("secret-b\n", encoding="utf-8")
                    b_secret_hash = _file_hash(candidate / "project" / "secret.txt")

                result = backend.run_tests_in_candidate(
                    candidate_root=candidate,
                    module_root=module_root,
                    candidate_rel=rel,
                    run_id=f"run-{candidate_id}",
                    node_id=f"node-{candidate_id}",
                    module_id=module_id,
                    boundary_fingerprint=boundary_fp,
                    test_commands=[PYTEST_CMD],
                    write_set=["tests/test_isolation.py", "marker.txt"],
                    test_entity_id=f"node-{candidate_id}",
                    map_seq=1,
                    image=image,
                    image_version=image_version,
                    timeout_seconds=180,
                )
                assert result["exit_code"] == 0, result
                manifest = result.get("manifest") or {}
                assert manifest.get("status") == "completed"
                evidence = json.loads(
                    (candidate / "diagnostics" / "control-envelope.json").read_text(encoding="utf-8")
                )
                assert evidence["host_attestation"]["container_id"] == result["container_id"]
                assert evidence["host_attestation"]["image_digest"] == image_version
                assert marker in (candidate / "project" / "marker.txt").read_text(encoding="utf-8")
                if first_container_id is None:
                    first_container_id = result["container_id"]
                else:
                    assert result["container_id"] == first_container_id
                    assert result["container_reused"] is True

            assert b_secret_hash is not None
            assert (
                _file_hash(module_root / "candidates" / "cand-b" / "project" / "secret.txt") == b_secret_hash
            )

            backend.orchestrator.module_manager.registry.records.clear()
            backend.orchestrator.module_manager.registry.module_active_key.clear()
            backend._runner._containers.clear()
            backend._runner._logs.clear()

            rel_c, cand_c = _prepare_candidate(module_root, "cand-c", marker="gamma")
            fresh_backend = AgentContainerBackend(test_workspace)
            adopt_result = fresh_backend.run_tests_in_candidate(
                candidate_root=cand_c,
                module_root=module_root,
                candidate_rel=rel_c,
                run_id="run-c",
                node_id="node-c",
                module_id=module_id,
                boundary_fingerprint=boundary_fp,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
                test_entity_id="node-c",
                map_seq=1,
                image=image,
                image_version=image_version,
                timeout_seconds=180,
            )
            assert adopt_result["container_id"] == first_container_id
            assert adopt_result["container_reused"] is True
            assert adopt_result["exit_code"] == 0
            assert "gamma" in (cand_c / "project" / "marker.txt").read_text(encoding="utf-8")
        finally:
            _cleanup_it_containers(it_run_id)
            _cleanup_module_containers(test_workspace, module_id)
            _assert_it_label_containers_zero(it_run_id)

        assert first_container_id is not None
        assert subprocess.run(["docker", "inspect", first_container_id], capture_output=True).returncode != 0

    def test_real_docker_rejects_wrong_identity_decoy_containers(
        self,
        test_workspace: Path,
        docker_available: None,
        monkeypatch: pytest.MonkeyPatch,
        it_run_id: str,
        review_agent_image,
    ) -> None:
        image = review_agent_image.tag
        image_version = review_agent_image.image_digest

        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        module_id = f"docker-identity-{it_run_id}"
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / module_id
        module_root.mkdir(parents=True, exist_ok=True)
        boundary_fp = f"fp-identity-{it_run_id}"
        _cleanup_it_containers(it_run_id)
        _cleanup_module_containers(test_workspace, module_id)

        rel, candidate = _prepare_candidate(module_root, "cand-identity", marker="identity-v1")
        backend = AgentContainerBackend(test_workspace)
        decoy_ids: list[str] = []

        try:
            good = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=candidate,
                rel=rel,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-good",
                image=image,
                image_version=image_version,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
            )
            good_id = good["container_id"]

            for kind in ("user", "rootfs", "command"):
                decoy_ids.append(
                    _create_identity_decoy_container(
                        test_workspace,
                        module_root,
                        candidate,
                        candidate_rel=rel,
                        module_id=module_id,
                        boundary_fp=boundary_fp,
                        image=image,
                        it_run_id=it_run_id,
                        kind=kind,
                    )
                )

            assert _count_it_label_containers(it_run_id) == len(decoy_ids) + 1

            second = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=candidate,
                rel=rel,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-after-decoys",
                image=image,
                image_version=image_version,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
            )
            assert second["container_id"] == good_id
            assert second["container_reused"] is True
            for decoy_id in decoy_ids:
                assert subprocess.run(["docker", "inspect", decoy_id], capture_output=True).returncode != 0
        finally:
            _cleanup_it_containers(it_run_id)
            _cleanup_module_containers(test_workspace, module_id)
            _assert_it_label_containers_zero(it_run_id)

    def test_real_docker_rejects_hardening_and_duplicate_decoys(
        self,
        test_workspace: Path,
        docker_available: None,
        monkeypatch: pytest.MonkeyPatch,
        it_run_id: str,
        review_agent_image,
    ) -> None:
        image = review_agent_image.tag
        image_version = review_agent_image.image_digest

        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        module_id = f"docker-hardening-{it_run_id}"
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / module_id
        module_root.mkdir(parents=True, exist_ok=True)
        boundary_fp = f"fp-hardening-{it_run_id}"
        _cleanup_it_containers(it_run_id)
        _cleanup_module_containers(test_workspace, module_id)

        rel, candidate = _prepare_candidate(module_root, "cand-hardening", marker="hardening-v1")
        backend = AgentContainerBackend(test_workspace)
        decoy_ids: list[str] = []

        try:
            good = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=candidate,
                rel=rel,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-good",
                image=image,
                image_version=image_version,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
            )
            good_id = good["container_id"]

            for kind in ("privileged", "cap_drop", "security_opt", "pids", "memory", "cpus", "duplicate"):
                decoy_ids.append(
                    _create_hardening_decoy_container(
                        test_workspace,
                        module_root,
                        candidate,
                        candidate_rel=rel,
                        module_id=module_id,
                        boundary_fp=boundary_fp,
                        image=image,
                        it_run_id=it_run_id,
                        kind=kind,
                    )
                )

            second = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=candidate,
                rel=rel,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-after-hardening-decoys",
                image=image,
                image_version=image_version,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
            )
            assert second["container_id"] == good_id
            assert second["container_reused"] is True
            for decoy_id in decoy_ids:
                assert subprocess.run(["docker", "inspect", decoy_id], capture_output=True).returncode != 0
            listed = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label=bridle.module={module_id}",
                    "--filter",
                    f"label=bridle.project={project_label(test_workspace)}",
                ],
                capture_output=True,
                text=True,
            )
            active_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
            assert len(active_ids) == 1
            assert _container_ids_match(active_ids[0], good_id)
        finally:
            _cleanup_it_containers(it_run_id)
            _cleanup_module_containers(test_workspace, module_id)
            _assert_it_label_containers_zero(it_run_id)

    def test_real_docker_recovers_after_link_attack_in_slot(
        self,
        test_workspace: Path,
        docker_available: None,
        monkeypatch: pytest.MonkeyPatch,
        it_run_id: str,
        review_agent_image,
        request: pytest.FixtureRequest,
    ) -> None:
        if os.name == "nt":
            pytest.skip("POSIX symlink required for link attack scenario")
        image = review_agent_image.tag
        image_version = review_agent_image.image_digest
        test_node_id = request.node.nodeid

        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        module_id = f"docker-link-{it_run_id}"
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / module_id
        module_root.mkdir(parents=True, exist_ok=True)
        boundary_fp = f"fp-link-{it_run_id}"
        _cleanup_it_containers(it_run_id)
        _cleanup_module_containers(test_workspace, module_id)

        rel_a, cand_a = _prepare_candidate(module_root, "cand-a", marker="alpha")
        rel_b, cand_b = _prepare_candidate(module_root, "cand-b", marker="beta")
        outside = test_workspace / "outside-link-secret.txt"
        outside.write_text("secret\n", encoding="utf-8")
        sentinel_handle = _await_controller_sentinel_ack(outside)
        rel_attack, cand_attack = _prepare_link_attack_candidate(
            module_root,
            "cand-attack",
            outside=outside,
        )
        backend = AgentContainerBackend(test_workspace)
        primary: dict = {}
        primary_error: str | None = None

        try:
            first = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=cand_a,
                rel=rel_a,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-a",
                image=image,
                image_version=image_version,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
            )
            attack = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=cand_attack,
                rel=rel_attack,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-attack",
                image=image,
                image_version=image_version,
                test_commands=[LINK_ATTACK_CMD],
                write_set=["tests/test_link_attack.py"],
            )
            assert attack["container_id"] == first["container_id"], attack
            assert attack["exit_code"] == 0, attack
            attack_report = _parse_link_attack_report(attack)
            assert attack_report["uid"] == 1000, attack_report

            slot = module_root / "_active"
            assert (slot / "project" / "attack.txt").is_symlink()
            assert (slot / "output" / "escape.txt").is_symlink()
            assert os.readlink(slot / "project" / "attack.txt") == str(outside.resolve())
            record_pending_primary(
                "link_attack",
                {
                    "attack_uid": attack_report["uid"],
                    "attack_results": attack_report["results"],
                    "entry_command": LINK_ATTACK_CMD,
                },
            )

            second = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=cand_b,
                rel=rel_b,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-b",
                image=image,
                image_version=image_version,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
            )
            assert second["container_id"] == first["container_id"]
            assert second["exit_code"] == 0, second
            assert (slot / "project" / "marker.txt").read_text(encoding="utf-8") == "beta\n"
            assert not (slot / "project" / "attack.txt").exists()
            assert not (slot / "output" / "escape.txt").exists()
            assert outside.read_text(encoding="utf-8") == "secret\n"
            primary = {
                "attack_uid": attack_report["uid"],
                "attack_results": attack_report["results"],
                "entry_command": LINK_ATTACK_CMD,
                "container_id": second["container_id"],
                "it_run_id": it_run_id,
                "module_id": module_id,
                "first_run_id": "run-a",
                "attack_run_id": "run-attack",
                "second_run_id": "run-b",
                "container_reused": second["container_id"] == first["container_id"],
                "symlinks_removed": True,
                "outside_secret_intact": True,
                "sentinel_handle": sentinel_handle,
            }
        except Exception as exc:
            primary_error = str(exc)
            raise
        finally:
            _finalize_critical_test_evidence(
                test_workspace=test_workspace,
                it_run_id=it_run_id,
                module_id=module_id,
                test_key="link_attack",
                test_node_id=test_node_id,
                image_version=image_version,
                primary=primary,
                primary_error=primary_error,
            )

    def test_real_docker_recovers_after_rw_root_permission_poisoning(
        self,
        test_workspace: Path,
        docker_available: None,
        monkeypatch: pytest.MonkeyPatch,
        it_run_id: str,
        review_agent_image,
        request: pytest.FixtureRequest,
    ) -> None:
        if os.name == "nt":
            pytest.skip("POSIX bind-mount chmod semantics required for RW root permission poison scenario")
        image = review_agent_image.tag
        image_version = review_agent_image.image_digest
        test_node_id = request.node.nodeid

        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        module_id = f"docker-perm-{it_run_id}"
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / module_id
        module_root.mkdir(parents=True, exist_ok=True)
        boundary_fp = f"fp-perm-{it_run_id}"
        _cleanup_it_containers(it_run_id)
        _cleanup_module_containers(test_workspace, module_id)

        rel_a, cand_a = _prepare_chmod_poison_candidate(
            module_root,
            "cand-poison",
            marker="poisoned",
        )
        rel_b, cand_b = _prepare_candidate(module_root, "cand-recover", marker="recovered")
        backend = AgentContainerBackend(test_workspace)
        primary: dict = {}
        primary_error: str | None = None

        try:
            first = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=cand_a,
                rel=rel_a,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-poison",
                image=image,
                image_version=image_version,
                test_commands=[CHMOD_POISON_CMD],
                write_set=["tests/test_chmod_poison.py"],
            )
            assert first["exit_code"] == 0, first
            report = _parse_chmod_poison_report(first)
            assert report["uid"] == 1000, report
            results = report["results"]
            assert len(results) == 3, results
            succeeded = [item for item in results if item.get("rc") == 0]
            denied = [item for item in results if item.get("rc", 0) != 0]
            assert succeeded, (
                "product contract requires container UID 1000 to chmod at least one RW root; "
                f"denied={denied}"
            )
            record_pending_primary(
                "chmod_poison",
                {
                    "attack_uid": report["uid"],
                    "chmod_results": results,
                    "entry_command": CHMOD_POISON_CMD,
                },
            )

            slot = module_root / "_active"
            baseline_path = rw_mount_baseline_path(module_root)
            assert baseline_path.is_file()
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            trusted_modes = {
                root_name: int(baseline["roots"][root_name]["mode"]) & 0o777
                for root_name in ("project", "output", "diagnostics")
            }
            project_result = next(
                item for item in results if item["path"] == "/workspace/project"
            )
            assert project_result.get("rc") == 0, project_result
            assert project_result.get("after_mode") == 0, project_result

            second = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=cand_b,
                rel=rel_b,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-recover",
                image=image,
                image_version=image_version,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
            )
            assert second["container_id"] == first["container_id"]
            assert second["exit_code"] == 0, second
            assert (slot / "project" / "marker.txt").read_text(encoding="utf-8") == "recovered\n"
            for root_name in ("project", "output", "diagnostics"):
                mode = os.stat(slot / root_name).st_mode & 0o777
                assert mode == trusted_modes[root_name], (
                    f"{root_name} mode={oct(mode)} expected trusted {oct(trusted_modes[root_name])}"
                )
            primary = {
                "attack_uid": report["uid"],
                "chmod_results": results,
                "entry_command": CHMOD_POISON_CMD,
                "container_id": second["container_id"],
                "it_run_id": it_run_id,
                "module_id": module_id,
                "first_run_id": "run-poison",
                "second_run_id": "run-recover",
                "trusted_modes": trusted_modes,
                "recovered_modes": {
                    root_name: os.stat(slot / root_name).st_mode & 0o777
                    for root_name in ("project", "output", "diagnostics")
                },
                "container_reused": second["container_id"] == first["container_id"],
            }
        except Exception as exc:
            primary_error = str(exc)
            raise
        finally:
            _finalize_critical_test_evidence(
                test_workspace=test_workspace,
                it_run_id=it_run_id,
                module_id=module_id,
                test_key="chmod_poison",
                test_node_id=test_node_id,
                image_version=image_version,
                primary=primary,
                primary_error=primary_error,
            )

    def test_real_docker_baseline_tamper_blocked(
        self,
        test_workspace: Path,
        docker_available: None,
        monkeypatch: pytest.MonkeyPatch,
        it_run_id: str,
        review_agent_image,
    ) -> None:
        image = review_agent_image.tag
        image_version = review_agent_image.image_digest

        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        module_id = f"docker-tamper-{it_run_id}"
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / module_id
        module_root.mkdir(parents=True, exist_ok=True)
        boundary_fp = f"fp-tamper-{it_run_id}"
        _cleanup_it_containers(it_run_id)
        _cleanup_module_containers(test_workspace, module_id)

        rel, candidate = _prepare_tamper_candidate(module_root, "cand-tamper")
        baseline_hash_before = _file_hash(candidate / "baseline" / "tests" / "test_ok.py")
        mock_hash_before = _file_hash(candidate / "mocks" / "iface.py")
        backend = AgentContainerBackend(test_workspace)

        try:
            result = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=candidate,
                rel=rel,
                module_id=module_id,
                boundary_fp=boundary_fp,
                run_id="run-tamper",
                image=image,
                image_version=image_version,
                test_commands=[TAMPER_CMD],
                write_set=["tests/test_tamper_baseline.py"],
            )
            assert result["exit_code"] == 0, result
            manifest = result.get("manifest") or {}
            assert manifest.get("status") == "completed"
            assert _file_hash(candidate / "baseline" / "tests" / "test_ok.py") == baseline_hash_before
            assert _file_hash(candidate / "mocks" / "iface.py") == mock_hash_before
            assert not (candidate / "baseline" / "tests" / "evil.txt").exists()
        finally:
            _cleanup_it_containers(it_run_id)
            _cleanup_module_containers(test_workspace, module_id)

    def test_real_docker_wrong_boundary_replaces_container(
        self,
        test_workspace: Path,
        docker_available: None,
        monkeypatch: pytest.MonkeyPatch,
        it_run_id: str,
        review_agent_image,
    ) -> None:
        image = review_agent_image.tag
        image_version = review_agent_image.image_digest

        monkeypatch.delenv("BRIDLE_CONTAINER_DRY_RUN", raising=False)
        module_id = f"docker-boundary-{it_run_id}"
        module_root = test_workspace / ".bridle" / "runtime" / "modules" / module_id
        module_root.mkdir(parents=True, exist_ok=True)
        _cleanup_it_containers(it_run_id)
        _cleanup_module_containers(test_workspace, module_id)

        rel, candidate = _prepare_candidate(module_root, "cand-boundary", marker="boundary-v1")
        backend = AgentContainerBackend(test_workspace)
        first_id: str | None = None

        try:
            first = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=candidate,
                rel=rel,
                module_id=module_id,
                boundary_fp=f"fp-a-{it_run_id}",
                run_id="run-a",
                image=image,
                image_version=image_version,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
            )
            first_id = first["container_id"]
            second = _run_backend_case(
                backend,
                module_root=module_root,
                candidate=candidate,
                rel=rel,
                module_id=module_id,
                boundary_fp=f"fp-b-{it_run_id}",
                run_id="run-b",
                image=image,
                image_version=image_version,
                test_commands=[PYTEST_CMD],
                write_set=["tests/test_isolation.py", "marker.txt"],
                replace_container=True,
            )
            assert second["container_id"] != first_id
            assert subprocess.run(["docker", "inspect", first_id], capture_output=True).returncode != 0
            assert subprocess.run(["docker", "inspect", second["container_id"]], capture_output=True).returncode == 0
        finally:
            _cleanup_it_containers(it_run_id)
            _cleanup_module_containers(test_workspace, module_id)


class TestReviewImageStaleBinding:
    def test_rejects_stale_source_digest_with_unique_tag(
        self,
        docker_available: None,
        it_run_id: str,
    ) -> None:
        from bridle.agent.container.review_image import (
            PRODUCER_VERSION,
            ReviewImageError,
            compute_agent_source_digest,
            find_repo_root,
            verify_review_image,
        )

        repo_root = find_repo_root()
        stale_tag = f"bridle-agent:review-stale-{uuid.uuid4().hex[:12]}"
        stale_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        dockerfile = repo_root / "backend" / "src" / "bridle" / "agent" / "container" / "agent.Dockerfile"
        build = subprocess.run(
            [
                "docker",
                "build",
                "-f",
                str(dockerfile),
                "--label",
                f"{IT_LABEL}={it_run_id}",
                "--build-arg",
                f"REVIEW_SOURCE_DIGEST={stale_digest}",
                "--build-arg",
                f"PRODUCER_VERSION={PRODUCER_VERSION}",
                "-t",
                stale_tag,
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert build.returncode == 0, build.stderr[-2000:]
        registered = register_built_image(tag=stale_tag, owner_run_id=it_run_id)
        try:
            current = compute_agent_source_digest(repo_root)
            with pytest.raises(ReviewImageError) as exc_info:
                verify_review_image(stale_tag, expected_source_digest=current)
            assert exc_info.value.error_code == "review_image_source_stale"
        finally:
            result = cleanup_registered_image(registered)
            assert result.removed, result.detail

    def test_cleanup_refuses_when_tag_rebound(
        self,
        docker_available: None,
        it_run_id: str,
    ) -> None:
        from bridle.agent.container.review_image import (
            PRODUCER_VERSION,
            compute_agent_source_digest,
            find_repo_root,
        )

        repo_root = find_repo_root()
        owned_tag = f"bridle-agent:review-rebind-{uuid.uuid4().hex[:12]}"
        decoy_tag = f"bridle-agent:review-decoy-{uuid.uuid4().hex[:12]}"
        owned_test_identity = f"owned-{it_run_id}"
        decoy_test_identity = f"decoy-{it_run_id}"
        source_digest = compute_agent_source_digest(repo_root)
        dockerfile = repo_root / "backend" / "src" / "bridle" / "agent" / "container" / "agent.Dockerfile"
        build_owned = subprocess.run(
            [
                "docker",
                "build",
                "-f",
                str(dockerfile),
                "--label",
                f"{IT_LABEL}={it_run_id}",
                "--label",
                f"{IT_TEST_IDENTITY_LABEL}={owned_test_identity}",
                "--build-arg",
                f"REVIEW_SOURCE_DIGEST={source_digest}",
                "--build-arg",
                f"PRODUCER_VERSION={PRODUCER_VERSION}",
                "-t",
                owned_tag,
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert build_owned.returncode == 0, build_owned.stderr[-2000:]
        registered_owned = register_built_image(tag=owned_tag, owner_run_id=it_run_id)

        build_decoy = subprocess.run(
            [
                "docker",
                "build",
                "-f",
                str(dockerfile),
                "--label",
                f"{IT_LABEL}={it_run_id}",
                "--label",
                f"{IT_TEST_IDENTITY_LABEL}={decoy_test_identity}",
                "--build-arg",
                f"REVIEW_SOURCE_DIGEST={source_digest}",
                "--build-arg",
                f"PRODUCER_VERSION={PRODUCER_VERSION}",
                "-t",
                decoy_tag,
                str(repo_root),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert build_decoy.returncode == 0, build_decoy.stderr[-2000:]
        registered_decoy = register_built_image(tag=decoy_tag, owner_run_id=it_run_id)

        assert registered_owned.image_id != registered_decoy.image_id, (
            "owned and decoy builds must produce distinct image IDs before rebind; "
            f"owned={registered_owned.image_id} decoy={registered_decoy.image_id}"
        )

        owned_before = query_image_identity(owned_tag)
        decoy_before = query_image_identity(decoy_tag)
        assert owned_before.status == "resolved"
        assert decoy_before.status == "resolved"
        assert owned_before.image_id == registered_owned.image_id
        assert decoy_before.image_id == registered_decoy.image_id

        rebind = subprocess.run(
            ["docker", "tag", decoy_tag, owned_tag],
            capture_output=True,
            text=True,
        )
        assert rebind.returncode == 0, rebind.stderr

        owned_after = query_image_identity(owned_tag)
        assert owned_after.status == "resolved"
        assert owned_after.image_id == registered_decoy.image_id
        assert owned_after.image_id != registered_owned.image_id

        result = cleanup_registered_image(registered_owned)
        assert not result.removed
        assert result.status == "refused"
        assert result.error_code == IMAGE_IDENTITY_MISMATCH
        assert registered_owned.image_id in result.detail or owned_tag in result.detail

        owned_ids, owned_list_error = list_images_for_run(it_run_id)
        assert owned_list_error is None
        assert registered_owned.image_id in owned_ids
        assert registered_decoy.image_id in owned_ids
        assert len(owned_ids) == 2

        teardown = finalize_run_teardown(it_run_id)
        assert_run_teardown_clean(teardown)
        assert_image_absent(registered_owned.image_id)
        assert_image_absent(registered_decoy.image_id)
        assert_tag_absent(owned_tag)
        assert_tag_absent(decoy_tag)
        final_ids, final_list_error = list_images_for_run(it_run_id)
        assert final_list_error is None
        assert final_ids == []
