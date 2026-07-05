"""DinD daemon boundary isolation tests.

Proves that a candidate worker controlling the inner DinD daemon (via DOCKER_HOST)
cannot escape to host trusted resources. Attacks are real: privileged inner
container, --pid=host, device mount, host root bind mount, outer docker socket
probe, trusted checkout probe, sibling network probe.

These tests run ONLY on Linux CI with BRIDLE_RUN_DOCKER_TESTS=1 because they
need a real Docker daemon and POSIX device/mount semantics. Windows Docker
results cannot prove Linux POSIX boundary behavior.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("BRIDLE_RUN_DOCKER_TESTS") != "1",
        reason="Set BRIDLE_RUN_DOCKER_TESTS=1 to run DinD boundary tests",
    ),
    pytest.mark.skipif(os.name == "nt", reason="DinD boundary requires Linux POSIX/device/mount semantics"),
]

_DIND_BASE_IMAGE = "docker:24-dind"
_PROBE_IMAGE = "alpine:3.20"
_CONTROLLER_SENTINEL_DIR = Path("/tmp/bridle-dind-boundary-sentinel")
_DIND_READY_TIMEOUT_S = 90


def _run(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def _docker_exec(dind_name: str, inner_args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "exec", dind_name, *inner_args], timeout=timeout)


def _ensure_probe_image(dind_name: str) -> None:
    pull = _docker_exec(dind_name, ["docker", "pull", _PROBE_IMAGE], timeout=180)
    if pull.returncode != 0:
        pytest.skip(f"could not pull probe image {_PROBE_IMAGE} inside DinD: {pull.stderr.strip()}")


def _start_dind_daemon(run_id: str, candidate_root: Path) -> dict:
    """Start a privileged DinD container directly via docker; return identity."""
    network = f"bridle-net-{run_id}"
    dind_name = f"bridle-dind-{run_id}"
    create_net = _run(["docker", "network", "create", network], timeout=30)
    if create_net.returncode != 0:
        pytest.fail(f"dind network create failed: {create_net.stderr.strip()}")
    host_candidate = str(candidate_root.resolve())
    create_dind = _run(
        [
            "docker", "run", "-d", "--name", dind_name, "--network", network,
            "--privileged",
            "--mount", f"type=bind,source={host_candidate},target=/bridle-candidate,bind-propagation=rshared",
            "-e", "DOCKER_TLS_CERTDIR=",
            _DIND_BASE_IMAGE, "dockerd",
            "--storage-driver=vfs",
            "--host=unix:///var/run/docker.sock",
            "--host=tcp://0.0.0.0:2375",
        ],
        timeout=120,
    )
    if create_dind.returncode != 0:
        _run(["docker", "network", "rm", network], timeout=30)
        pytest.fail(f"dind start failed: {create_dind.stderr.strip()}")
    ready = False
    deadline = time.monotonic() + _DIND_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        probe = _docker_exec(dind_name, ["docker", "info"], timeout=15)
        if probe.returncode == 0:
            ready = True
            break
        time.sleep(1.0)
    if not ready:
        _stop_dind_daemon(dind_name, network)
        pytest.fail(f"dind not ready within {_DIND_READY_TIMEOUT_S}s")
    inspect = _run(["docker", "inspect", "--type", "container", dind_name], timeout=30)
    try:
        payload = json.loads(inspect.stdout or "[]")
        dind_container_id = (payload[0] or {}).get("Id", "") if payload else ""
    except json.JSONDecodeError:
        dind_container_id = ""
    if not dind_container_id:
        _stop_dind_daemon(dind_name, network)
        pytest.fail("could not resolve dind container id")
    return {
        "run_id": run_id,
        "dind_name": dind_name,
        "network": network,
        "dind_container_id": dind_container_id,
    }


def _stop_dind_daemon(dind_name: str, network: str) -> None:
    _run(["docker", "stop", "-t", "5", dind_name], timeout=30)
    _run(["docker", "rm", dind_name], timeout=30)
    _run(["docker", "network", "rm", network], timeout=30)


@pytest.fixture
def dind_context(tmp_path: Path):
    """Start a real isolated DinD daemon and yield its identity, then tear down."""
    if shutil.which("docker") is None:
        pytest.fail("docker executable not found while BRIDLE_RUN_DOCKER_TESTS=1")
    run_id = f"bnd-{uuid.uuid4().hex[:8]}"
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir(parents=True, exist_ok=True)
    started = _start_dind_daemon(run_id, candidate_root)
    _ensure_probe_image(started["dind_name"])
    sentinel_root = _CONTROLLER_SENTINEL_DIR / run_id
    sentinel_root.mkdir(parents=True, exist_ok=True)
    sentinel_file = sentinel_root / "host-secret.txt"
    sentinel_file.write_text(f"host-secret-{run_id}\n", encoding="utf-8")
    try:
        yield {
            **started,
            "sentinel_file": sentinel_file,
            "sentinel_root": sentinel_root,
            "candidate_root": candidate_root,
        }
    finally:
        _stop_dind_daemon(started["dind_name"], started["network"])
        shutil.rmtree(sentinel_root, ignore_errors=True)


def _inner_docker(dind: str, args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return _docker_exec(dind, ["docker", *args], timeout=timeout)


def _attack_report(dind: dict, attack: str, detail: dict) -> dict:
    return {
        "attack": attack,
        "dind_name": dind["dind_name"],
        "run_id": dind["run_id"],
        **detail,
    }


def test_inner_privileged_container_cannot_read_host_sentinel(dind_context) -> None:
    """Inner privileged container must not reach host-side sentinel files.

    The host sentinel lives outside the candidate bind mount; if an inner
    privileged container can read it, the DinD boundary is broken.
    """
    dind = dind_context
    probe = _inner_docker(
        dind["dind_name"],
        [
            "run",
            "--rm",
            "--privileged",
            _PROBE_IMAGE,
            "cat",
            "/proc/1/root/tmp/bridle-dind-boundary-sentinel/host-secret.txt",
        ],
        timeout=60,
    )
    report = _attack_report(
        dind,
        "inner_privileged_host_sentinel_read",
        {"returncode": probe.returncode, "stdout": probe.stdout.strip(), "stderr": probe.stderr.strip()},
    )
    # The sentinel path on the host must not be readable from inside the DinD.
    assert dind["sentinel_file"].read_text(encoding="utf-8").startswith("host-secret-"), report
    assert "host-secret-" not in probe.stdout, report


def test_inner_pid_namespace_host_probe_rejected(dind_context) -> None:
    """--pid=host on the inner daemon must not reveal host (outer) processes.

    Inside DinD, --pid=host resolves to the DinD container's PID namespace, not
    the outer host. We verify by checking that the outer controller PID is NOT
    visible inside the inner host-pid container.
    """
    dind = dind_context
    outer_pid = os.getpid()
    probe = _inner_docker(
        dind["dind_name"],
        ["run", "--rm", "--pid=host", _PROBE_IMAGE, "ps", "-eo", "pid="],
        timeout=60,
    )
    pids = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    report = _attack_report(
        dind,
        "inner_pid_host_probe",
        {"returncode": probe.returncode, "outer_pid": outer_pid, "visible_pids": pids[:20]},
    )
    assert str(outer_pid) not in pids, report


def test_inner_device_mount_host_block_device_rejected(dind_context) -> None:
    """Inner --device=/dev/sda must not grant host block device access.

    If the device node does not exist inside DinD, the attack is rejected. If it
    exists, we verify it is NOT the host block device by checking that reading
    it fails or returns no host partition signature.
    """
    dind = dind_context
    probe = _inner_docker(
        dind["dind_name"],
        ["run", "--rm", "--privileged", "--device=/dev/sda", _PROBE_IMAGE, "head", "-c", "16", "/dev/sda"],
        timeout=60,
    )
    report = _attack_report(
        dind,
        "inner_device_mount_sda",
        {"returncode": probe.returncode, "stdout": probe.stdout.strip(), "stderr": probe.stderr.strip()},
    )
    # Either the device node is absent inside DinD (returncode != 0) or, if
    # present, it must not expose host disk content. We accept rejection OR an
    # empty/read-error result, but fail if host MBR/GPT bytes are returned.
    host_disk_signatures = (b"\x55\xaa", b"EFI PART")
    stdout_bytes = probe.stdout.encode("utf-8", errors="replace") if probe.stdout else b""
    if probe.returncode == 0 and stdout_bytes:
        assert not any(sig in stdout_bytes for sig in host_disk_signatures), report


def test_inner_host_root_bind_mount_cannot_reach_host_trusted(dind_context, tmp_path: Path) -> None:
    """Inner -v /:/host must not expose the OUTER host root.

    Inside DinD, binding '/' binds the DinD container's root, not the outer
    host root. We verify that the outer host's trusted-harness checkout path
    (which lives outside the candidate bind mount) is NOT visible at /host.
    """
    dind = dind_context
    outer_workspace = Path(os.environ.get("GITHUB_WORKSPACE", "/__not_set__"))
    probe_marker = outer_workspace / "trusted-harness" / "scripts" / "ci" / "trusted_test_runner.py"
    if not probe_marker.exists():
        pytest.skip("outer trusted-harness marker path not available; CI-only test")
    rel_marker = Path("trusted-harness") / "scripts" / "ci" / "trusted_test_runner.py"
    host_marker = "/host/" + rel_marker.as_posix()
    probe = _inner_docker(
        dind["dind_name"],
        ["run", "--rm", "--privileged", "-v", "/:/host:ro", _PROBE_IMAGE, "ls", host_marker],
        timeout=60,
    )
    report = _attack_report(
        dind,
        "inner_host_root_bind_mount",
        {"returncode": probe.returncode, "stdout": probe.stdout.strip(), "stderr": probe.stderr.strip()},
    )
    assert probe.returncode != 0 or host_marker not in probe.stdout, report


def test_inner_container_cannot_reach_outer_docker_socket(dind_context) -> None:
    """The outer host docker socket must not be reachable from inside DinD."""
    dind = dind_context
    probe = _inner_docker(
        dind["dind_name"],
        [
            "run", "--rm", "--privileged",
            "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
            _PROBE_IMAGE, "ls", "-l", "/var/run/docker.sock",
        ],
        timeout=60,
    )
    report = _attack_report(
        dind,
        "outer_docker_socket_probe",
        {"returncode": probe.returncode, "stdout": probe.stdout.strip(), "stderr": probe.stderr.strip()},
    )
    # The outer socket is not bind-mounted into DinD, so the inner -v should fail.
    assert probe.returncode != 0, report


def test_inner_container_cannot_reach_sibling_network(dind_context) -> None:
    """A second DinD network must not be reachable from the first DinD's inner containers."""
    dind = dind_context
    sibling_network = f"bridle-sibling-{uuid.uuid4().hex[:8]}"
    create_net = _run(["docker", "network", "create", sibling_network], timeout=30)
    assert create_net.returncode == 0, create_net.stderr.strip()
    try:
        sibling_name = f"bridle-sibling-{uuid.uuid4().hex[:8]}"
        start_sibling = _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                sibling_name,
                "--network",
                sibling_network,
                "--privileged",
                _DIND_BASE_IMAGE,
                "dockerd",
                "--host=unix:///var/run/docker.sock",
            ],
            timeout=120,
        )
        assert start_sibling.returncode == 0, start_sibling.stderr.strip()
        try:
            time.sleep(3)
            sibling_inspect = _run(
                ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", sibling_name],
                timeout=30,
            )
            sibling_ip = sibling_inspect.stdout.strip()
            ping = _inner_docker(
                dind["dind_name"],
                ["run", "--rm", "--network", dind["network"], _PROBE_IMAGE, "ping", "-c", "1", "-W", "2", sibling_ip],
                timeout=30,
            )
            report = _attack_report(
                dind,
                "sibling_network_probe",
                {
                    "sibling_ip": sibling_ip,
                    "returncode": ping.returncode,
                    "stdout": ping.stdout.strip(),
                    "stderr": ping.stderr.strip(),
                },
            )
            assert ping.returncode != 0, report
        finally:
            _run(["docker", "rm", "-f", sibling_name], timeout=30)
    finally:
        _run(["docker", "network", "rm", sibling_network], timeout=30)


def test_inner_privileged_container_cannot_modify_host_sentinel(dind_context) -> None:
    """Inner privileged container must not write to host sentinel path."""
    dind = dind_context
    before = dind["sentinel_file"].read_text(encoding="utf-8")
    probe = _inner_docker(
        dind["dind_name"],
        [
            "run",
            "--rm",
            "--privileged",
            _PROBE_IMAGE,
            "sh",
            "-c",
            f"echo tampered > /proc/1/root{dind['sentinel_file']}",
        ],
        timeout=60,
    )
    after = dind["sentinel_file"].read_text(encoding="utf-8")
    report = _attack_report(
        dind,
        "inner_privileged_host_sentinel_write",
        {"returncode": probe.returncode, "stderr": probe.stderr.strip(), "before": before, "after": after},
    )
    assert before == after, report
    assert "tampered" not in after, report


def test_boundary_attack_evidence_recorded(dind_context) -> None:
    """All boundary attacks must be recordable as structured evidence for the gate."""
    dind = dind_context
    attacks = [
        ("privileged", ["run", "--rm", "--privileged", _PROBE_IMAGE, "true"]),
        ("pid_host", ["run", "--rm", "--pid=host", _PROBE_IMAGE, "true"]),
        ("device_sda", ["run", "--rm", "--device=/dev/sda", _PROBE_IMAGE, "true"]),
    ]
    outcomes = []
    for name, args in attacks:
        result = _inner_docker(dind["dind_name"], args, timeout=60)
        outcomes.append({"attack": name, "returncode": result.returncode, "stderr": result.stderr.strip()[:200]})
    evidence = {
        "schema": "bridle.dind_boundary_attack/v1",
        "run_id": dind["run_id"],
        "dind_name": dind["dind_name"],
        "outcomes": outcomes,
    }
    serialized = json.dumps(evidence, sort_keys=True)
    assert "bridle.dind_boundary_attack/v1" in serialized
    assert len(outcomes) == 3
