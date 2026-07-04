#!/usr/bin/env python3
"""Launch candidate worker in a sandboxed subprocess or Linux Docker container."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("bridle.worker_sandbox")
SCRIPT_DIR = Path(__file__).resolve().parent
INNER_CANDIDATE_ROOT = Path("/bridle-candidate")


@dataclass(frozen=True)
class SandboxPaths:
    candidate_root: Path
    trusted_config: Path
    trusted_scripts: Path
    controller_ipc: Path | None = None


@dataclass(frozen=True)
class IsolatedDockerContext:
    docker_host: str
    network: str
    dind_name: str


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_ipc():
    return _load_module("bridle_trusted_ipc", SCRIPT_DIR / "trusted_ipc.py")


def controller_uid() -> int | None:
    return os.getuid() if hasattr(os, "getuid") else None


def use_docker_sandbox(*, public_env: Mapping[str, str] | None = None) -> bool:
    if os.environ.get("BRIDLE_FORCE_SUBPROCESS_WORKER") == "1":
        return False
    from shutil import which

    if os.name == "nt" or which("docker") is None:
        return False
    if os.environ.get("BRIDLE_WORKER_DOCKER_SANDBOX") == "1":
        return True
    return (public_env or {}).get("BRIDLE_ISOLATION_PROBE") == "1"


def worker_image_ref() -> str:
    return os.environ.get("BRIDLE_WORKER_IMAGE", "").strip() or "python:3.12-slim-bookworm"


def map_paths_for_sandbox(
    paths: SandboxPaths,
    *,
    public_env: Mapping[str, str] | None = None,
    isolated: IsolatedDockerContext | None = None,
) -> SandboxPaths:
    if not use_docker_sandbox(public_env=public_env):
        return paths
    controller_ipc = Path("/controller-ipc") if paths.controller_ipc is not None else None
    host_candidate = paths.candidate_root.resolve()
    probe = (public_env or {}).get("BRIDLE_ISOLATION_PROBE") == "1"
    if isolated is not None and not probe:
        candidate_root = INNER_CANDIDATE_ROOT
    else:
        candidate_root = host_candidate
    return SandboxPaths(
        candidate_root=candidate_root,
        trusted_config=Path("/trusted-config") / paths.trusted_config.name,
        trusted_scripts=Path("/trusted-scripts"),
        controller_ipc=controller_ipc,
    )


def _effective_candidate_root(
    paths: SandboxPaths,
    *,
    public_env: dict[str, str],
    isolated: IsolatedDockerContext | None,
) -> Path:
    if isolated is not None and public_env.get("BRIDLE_ISOLATION_PROBE") != "1":
        return INNER_CANDIDATE_ROOT
    return paths.candidate_root.resolve()


def build_request(
    *,
    paths: SandboxPaths,
    pytest_args: tuple[str, ...],
    public_env: dict[str, str],
    isolated: IsolatedDockerContext | None = None,
):
    ipc = _load_ipc()
    mapped = map_paths_for_sandbox(paths, public_env=public_env, isolated=isolated)
    return ipc.WorkerRequest(
        candidate_root=str(mapped.candidate_root),
        trusted_config=str(mapped.trusted_config),
        pytest_args=pytest_args,
        public_env=public_env,
    )


def start_isolated_docker_for_worker(*, run_id: str | None = None, candidate_host_root: Path | None = None) -> IsolatedDockerContext:
    isolated = _load_module("bridle_isolated_docker", SCRIPT_DIR / "isolated_docker.py")
    docker_host, network, dind_name = isolated.start_isolated_daemon(
        run_id=run_id,
        candidate_host_root=candidate_host_root,
    )
    return IsolatedDockerContext(docker_host=docker_host, network=network, dind_name=dind_name)


def stop_isolated_docker(context: IsolatedDockerContext | None) -> None:
    if context is None:
        return
    isolated = _load_module("bridle_isolated_docker", SCRIPT_DIR / "isolated_docker.py")
    isolated.stop_isolated_daemon(network=context.network, dind_name=context.dind_name)


def _capture_process(
    proc: subprocess.Popen[bytes],
    *,
    timeout: int,
    on_stdout_line: Callable[[str], None] | None,
):
    stream = _load_module("bridle_subprocess_stream", SCRIPT_DIR / "subprocess_stream.py")
    ipc = _load_ipc()
    return stream.capture_with_deadline(
        proc,
        max_bytes=ipc.MAX_STREAM_BYTES,
        timeout=float(timeout),
        on_stdout_line=on_stdout_line,
    )


def _spawn_subprocess_worker(
    *,
    request_payload: str,
    worker_script: Path,
    timeout: int,
    on_stdout_line: Callable[[str], None] | None,
):
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-I", str(worker_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.write(request_payload.encode("utf-8"))
    proc.stdin.close()
    return _capture_process(proc, timeout=timeout, on_stdout_line=on_stdout_line)


def _append_worker_diagnostic(message: str) -> None:
    raw = os.environ.get("BRIDLE_DOCKER_EVIDENCE_DIR", "").strip()
    if not raw:
        return
    log_path = Path(raw) / "ci-phases.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def _spawn_docker_worker(
    *,
    request_payload: str,
    paths: SandboxPaths,
    timeout: int,
    public_env: dict[str, str],
    on_stdout_line: Callable[[str], None] | None,
    isolated: IsolatedDockerContext | None,
):
    run_id = uuid.uuid4().hex[:12]
    container_name = f"bridle-worker-{run_id}"
    probe = public_env.get("BRIDLE_ISOLATION_PROBE") == "1"
    env_args: list[str] = []
    merged_env = dict(public_env)
    if isolated is not None and not probe:
        merged_env["DOCKER_HOST"] = isolated.docker_host
    for key, value in merged_env.items():
        env_args.extend(["-e", f"{key}={value}"])
    env_args.extend(["-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "-e", "BRIDLE_CANDIDATE_WORKER=1"])
    candidate_root = _effective_candidate_root(paths, public_env=public_env, isolated=isolated)
    candidate_root_text = str(candidate_root)
    env_args.extend(
        [
            "-e",
            f"PYTHONPATH={candidate_root_text}/backend/src",
            "-e",
            "HOME=/tmp",
            "-e",
            "TMPDIR=/tmp",
        ]
    )
    volume_args = [
        "-v",
        f"{paths.trusted_config.parent.resolve()}:/trusted-config:ro",
        "-v",
        f"{paths.trusted_scripts.resolve()}:/trusted-scripts:ro",
    ]
    if isolated is not None and not probe:
        host_candidate = str(paths.candidate_root.resolve())
        volume_args = [
            "--mount",
            f"type=bind,source={host_candidate},target={INNER_CANDIDATE_ROOT},bind-propagation=rshared",
            *volume_args,
        ]
    else:
        host_candidate = str(paths.candidate_root.resolve())
        volume_args = [
            "--mount",
            f"type=bind,source={host_candidate},target={host_candidate},bind-propagation=rshared",
            *volume_args,
        ]
    if paths.controller_ipc is not None:
        volume_args.extend(["-v", f"{paths.controller_ipc.resolve()}:/controller-ipc:ro"])
    network_args = ["--network", "none"] if probe else []
    if isolated is not None and not probe:
        network_args = ["--network", isolated.network]
    run_uid = os.getuid() if hasattr(os, "getuid") else 1000
    run_gid = os.getgid() if hasattr(os, "getgid") else 1000
    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        container_name,
        *network_args,
        "-u",
        f"{run_uid}:{run_gid}",
        "-i",
        *volume_args,
        *env_args,
        worker_image_ref(),
        "python",
        "/trusted-scripts/candidate_worker.py",
    ]
    LOGGER.info(
        "worker_sandbox_docker_started name=%s probe=%s image=%s isolated=%s cmd=%s",
        container_name,
        probe,
        worker_image_ref(),
        isolated is not None,
        " ".join(cmd),
    )
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    proc.stdin.write(request_payload.encode("utf-8"))
    proc.stdin.close()
    capture = _capture_process(proc, timeout=timeout, on_stdout_line=on_stdout_line)
    if capture.returncode not in (0, None):
        stderr_text = capture.stderr.decode("utf-8", errors="replace")
        stdout_text = capture.stdout.decode("utf-8", errors="replace")
        _append_worker_diagnostic(
            f"worker_container_start_failed returncode={capture.returncode} "
            f"stderr={stderr_text[-4000:]} stdout={stdout_text[-2000:]}"
        )
        _append_worker_diagnostic(f"worker_container_cmd={' '.join(cmd)}")
    if capture.timed_out:
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect.returncode == 0 and inspect.stdout.strip() == "true":
            subprocess.run(["docker", "stop", "-t", "5", container_name], capture_output=True, check=False)
    return capture


def _attach_controller_identity(observation, ipc):
    return ipc.WorkerObservation(
        worker_state=observation.worker_state,
        exit_code=observation.exit_code,
        stdout=observation.stdout,
        stderr=observation.stderr,
        truncated_stdout=observation.truncated_stdout,
        truncated_stderr=observation.truncated_stderr,
        worker_pid=observation.worker_pid,
        worker_uid=observation.worker_uid,
        controller_pid=os.getpid(),
        controller_uid=controller_uid(),
    )


def map_public_env_for_docker_worker(public_env: Mapping[str, str], candidate_root: Path) -> dict[str, str]:
    mapped = dict(public_env)
    mapped["BRIDLE_TRUSTED_CHECKOUT_ROOT"] = candidate_root.resolve().as_posix()
    return mapped


def spawn_worker(
    *,
    paths: SandboxPaths,
    pytest_args: tuple[str, ...],
    public_env: dict[str, str],
    timeout: int = 3600,
    on_stdout_line: Callable[[str], None] | None = None,
    isolated: IsolatedDockerContext | None = None,
):
    ipc = _load_ipc()
    request = build_request(
        paths=paths,
        pytest_args=pytest_args,
        public_env=public_env,
        isolated=isolated,
    )
    payload = ipc.encode_request(request)
    sandbox_mode = "docker" if use_docker_sandbox(public_env=public_env) else "subprocess"
    mapped_paths = map_paths_for_sandbox(paths, public_env=public_env, isolated=isolated)
    worker_public_env = (
        map_public_env_for_docker_worker(public_env, mapped_paths.candidate_root)
        if sandbox_mode == "docker"
        else dict(public_env)
    )
    if sandbox_mode == "docker" and isolated is not None:
        worker_public_env["DOCKER_HOST"] = isolated.docker_host
    if sandbox_mode == "docker":
        request = build_request(
            paths=paths,
            pytest_args=pytest_args,
            public_env=worker_public_env,
            isolated=isolated,
        )
        payload = ipc.encode_request(request)
    LOGGER.info("worker_sandbox_mode mode=%s probe=%s", sandbox_mode, public_env.get("BRIDLE_ISOLATION_PROBE") == "1")
    if sandbox_mode == "docker":
        capture = _spawn_docker_worker(
            request_payload=payload,
            paths=paths,
            timeout=timeout,
            public_env=worker_public_env,
            on_stdout_line=on_stdout_line,
            isolated=isolated,
        )
        proc_returncode = capture.returncode
        raw_stdout = capture.stdout
        raw_stderr = capture.stderr
    else:
        capture = _spawn_subprocess_worker(
            request_payload=payload,
            worker_script=paths.trusted_scripts / "candidate_worker.py",
            timeout=timeout,
            on_stdout_line=on_stdout_line,
        )
        proc_returncode = capture.returncode
        raw_stdout = capture.stdout
        raw_stderr = capture.stderr

    if capture.timed_out:
        stdout, stderr, truncated_stdout, truncated_stderr = _decode_streams(raw_stdout, raw_stderr, ipc)
        return (
            ipc.WorkerObservation(
                worker_state=ipc.WORKER_STATE_TIMED_OUT,
                exit_code=None,
                stdout=stdout,
                stderr=stderr,
                truncated_stdout=truncated_stdout,
                truncated_stderr=truncated_stderr,
                worker_pid=None,
                worker_uid=None,
                controller_pid=os.getpid(),
                controller_uid=controller_uid(),
            ),
            stdout,
            stderr,
        )

    pid = os.getpid()
    uid = controller_uid()
    if proc_returncode not in (0, None):
        stdout, stderr, truncated_stdout, truncated_stderr = _decode_streams(raw_stdout, raw_stderr, ipc)
        failure_msg = (
            f"worker_container_start_failed returncode={proc_returncode} "
            f"stderr={stderr[-4000:]} stdout={stdout[-2000:]}"
        )
        LOGGER.error(failure_msg)
        _append_worker_diagnostic(failure_msg)
        return (
            ipc.WorkerObservation(
                worker_state=ipc.WORKER_STATE_FAILED_BEFORE_EXEC,
                exit_code=proc_returncode,
                stdout=stdout,
                stderr=stderr,
                truncated_stdout=truncated_stdout,
                truncated_stderr=truncated_stderr,
                worker_pid=None,
                worker_uid=None,
                controller_pid=pid,
                controller_uid=uid,
            ),
            stdout,
            stderr,
        )

    lines = raw_stdout.decode("utf-8", errors="replace").strip().splitlines()
    if not lines:
        stderr = raw_stderr.decode("utf-8", errors="replace")
        return (
            ipc.WorkerObservation(
                worker_state=ipc.WORKER_STATE_FAILED_BEFORE_EXEC,
                exit_code=None,
                stdout="",
                stderr=stderr,
                truncated_stdout=False,
                truncated_stderr=len(raw_stderr) > ipc.MAX_STREAM_BYTES,
                worker_pid=None,
                worker_uid=None,
                controller_pid=pid,
                controller_uid=uid,
            ),
            "",
            stderr,
        )
    observation = _attach_controller_identity(ipc.decode_observation(lines[-1]), ipc)
    if capture.truncated_stdout:
        observation = ipc.WorkerObservation(
            worker_state=observation.worker_state,
            exit_code=observation.exit_code,
            stdout=observation.stdout,
            stderr=observation.stderr,
            truncated_stdout=True,
            truncated_stderr=observation.truncated_stderr or capture.truncated_stderr,
            worker_pid=observation.worker_pid,
            worker_uid=observation.worker_uid,
            controller_pid=observation.controller_pid,
            controller_uid=observation.controller_uid,
        )
    return observation, observation.stdout, observation.stderr


def _decode_streams(stdout: bytes, stderr: bytes, ipc) -> tuple[str, str, bool, bool]:
    truncated_stdout = len(stdout) > ipc.MAX_STREAM_BYTES
    truncated_stderr = len(stderr) > ipc.MAX_STREAM_BYTES
    text_stdout = stdout[: ipc.MAX_STREAM_BYTES].decode("utf-8", errors="replace")
    text_stderr = stderr[: ipc.MAX_STREAM_BYTES].decode("utf-8", errors="replace")
    return text_stdout, text_stderr, truncated_stdout, truncated_stderr


def parse_probe_report(stdout: str) -> dict | None:
    prefix = "BRIDLE_ISOLATION_PROBE_REPORT:"
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return json.loads(line[len(prefix) :])
    return None
