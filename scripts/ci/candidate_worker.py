#!/usr/bin/env python3
"""Isolated candidate pytest worker — returns untrusted observations only."""
from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

LOGGER = logging.getLogger("bridle.candidate_worker")
INJECTABLE_ENV = frozenset(
    {
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
    }
)


def _load_ipc_module():
    script_dir = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("bridle_trusted_ipc", script_dir / "trusted_ipc.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("trusted_ipc_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bridle_trusted_ipc"] = module
    spec.loader.exec_module(module)
    return module


def sanitized_environment(source: dict[str, str]) -> dict[str, str]:
    result = {key: value for key, value in source.items() if key not in INJECTABLE_ENV}
    result["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result["BRIDLE_CANDIDATE_WORKER"] = "1"
    return result


def pytest_arguments(
    *,
    candidate_root: Path,
    trusted_config: Path,
    extra_args: tuple[str, ...],
) -> list[str]:
    candidate = candidate_root.resolve()
    if os.environ.get("BRIDLE_ISOLATION_PROBE") == "1":
        probe_root = candidate / ".bridle-isolation-probe"
        return [
            "-m",
            "pytest",
            "-c",
            str(trusted_config.resolve()),
            "--rootdir",
            str(probe_root),
            "--confcutdir",
            str(probe_root),
            str(probe_root),
            *extra_args,
        ]
    docker_gate = os.environ.get("BRIDLE_RUN_DOCKER_TESTS") == "1"
    trusted_backend = trusted_config.resolve().parent
    test_file = (
        trusted_backend / "tests/agent/container/test_docker_integration.py"
        if docker_gate
        else candidate / "backend/tests/agent/container/test_docker_integration.py"
    )
    rootdir = trusted_backend if docker_gate else candidate / "backend"
    capture_args: list[str] = []
    plugin_args: list[str] = []
    if docker_gate:
        # Sentinel REQUEST and CRITICAL_EVIDENCE must reach the controller stream in real time.
        capture_args = ["-s", "--capture=no"]
        # Load the trusted test observer from the trusted scripts tree so the
        # controller can prove critical tests really ran, independent of stdout.
        trusted_scripts = os.environ.get("BRIDLE_TRUSTED_SCRIPTS_DIR", "").strip()
        if trusted_scripts:
            observer = str(Path(trusted_scripts) / "trusted_test_observer.py")
            if Path(observer).is_file():
                plugin_args = ["-p", "trusted_test_observer", "-p", "no:cacheprovider"]
                # Make the plugin importable as a top-level module.
                pypath = os.environ.get("PYTHONPATH", "")
                os.environ["PYTHONPATH"] = (
                    f"{trusted_scripts}{os.pathsep}{pypath}" if pypath else trusted_scripts
                )
    if not plugin_args:
        plugin_args = ["-p", "no:cacheprovider"]
    return [
        "-m",
        "pytest",
        "-c",
        str(trusted_config.resolve()),
        "--rootdir",
        str(rootdir),
        "--confcutdir",
        str(test_file.parent),
        *plugin_args,
        *capture_args,
        str(test_file),
        *extra_args,
    ]


def _prepend_pythonpath(path: Path) -> None:
    path_text = str(path)
    current_pythonpath = os.environ.get("PYTHONPATH", "")
    current_entries = [entry for entry in current_pythonpath.split(os.pathsep) if entry]
    if path_text not in current_entries:
        os.environ["PYTHONPATH"] = os.pathsep.join([path_text, *current_entries])


def _load_stream_module():
    script_dir = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "bridle_candidate_subprocess_stream", script_dir / "subprocess_stream.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("subprocess_stream_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bridle_candidate_subprocess_stream"] = module
    spec.loader.exec_module(module)
    return module


def run_worker_request(request_raw: str) -> str:
    ipc = _load_ipc_module()
    stream = _load_stream_module()
    request = ipc.decode_request(request_raw)
    candidate_root = Path(request.candidate_root).resolve()
    trusted_config = Path(request.trusted_config).resolve()

    clean_environment = sanitized_environment(dict(os.environ))
    os.environ.clear()
    os.environ.update(clean_environment)
    os.environ.update(request.public_env)
    os.environ["BRIDLE_CANDIDATE_WORKER"] = "1"

    candidate_source = candidate_root / "backend/src"
    _prepend_pythonpath(candidate_source)
    if os.environ.get("BRIDLE_RUN_DOCKER_TESTS") == "1":
        integration_test = trusted_config.parent / "tests/agent/container/test_docker_integration.py"
        if not integration_test.is_file():
            raise RuntimeError(f"candidate_worker_test_file_missing path={integration_test}")
        os.environ.setdefault(
            "BRIDLE_TEST_WORKSPACES_ROOT",
            str(candidate_root / "backend/.test-workspaces"),
        )
        if os.environ.get("DOCKER_HOST", "").strip():
            docker_probe = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if docker_probe.returncode != 0:
                raise RuntimeError(
                    "candidate_worker_docker_unreachable "
                    f"host={os.environ.get('DOCKER_HOST')} "
                    f"stderr={(docker_probe.stderr or docker_probe.stdout or '').strip()}"
                )

    sys.path.insert(0, str(candidate_source))
    os.chdir(candidate_root / "backend")

    invocation = pytest_arguments(
        candidate_root=candidate_root,
        trusted_config=trusted_config,
        extra_args=request.pytest_args,
    )
    LOGGER.info("candidate_worker_pytest_started candidate_root=%s", candidate_root)
    pytest_env = dict(os.environ)
    pytest_env["PYTHONUNBUFFERED"] = "1"
    timeout = float(os.environ.get("BRIDLE_WORKER_TIMEOUT", "3600"))

    def _forward_line(line: str) -> None:
        sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()

    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": pytest_env,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", *invocation],
            **popen_kwargs,
        )
    except OSError as exc:
        worker_uid = os.getuid() if hasattr(os, "getuid") else None
        observation = ipc.WorkerObservation(
            worker_state=ipc.WORKER_STATE_FAILED_BEFORE_EXEC,
            exit_code=None,
            stdout="",
            stderr=f"candidate_worker_popen_failed:{type(exc).__name__}:{exc}",
            truncated_stdout=False,
            truncated_stderr=False,
            worker_pid=os.getpid(),
            worker_uid=worker_uid,
            controller_pid=os.getppid(),
            controller_uid=None,
        )
        return ipc.encode_observation(observation)

    capture = stream.capture_with_deadline(
        proc,
        max_bytes=ipc.MAX_STREAM_BYTES,
        timeout=timeout,
        on_stdout_line=_forward_line,
    )
    stdout = capture.stdout.decode("utf-8", errors="replace")
    stderr_text = capture.stderr.decode("utf-8", errors="replace")
    if capture.callback_error:
        stderr_text = (stderr_text + "\n" + f"callback_error:{capture.callback_error}").strip()
    LOGGER.info(
        "candidate_worker_pytest_finished returncode=%s timed_out=%s",
        capture.returncode,
        capture.timed_out,
    )

    worker_uid = os.getuid() if hasattr(os, "getuid") else None
    if capture.timed_out:
        worker_state = ipc.WORKER_STATE_TIMED_OUT
        exit_code = None
    elif capture.returncode is None:
        worker_state = ipc.WORKER_STATE_FAILED_BEFORE_EXEC
        exit_code = None
    else:
        worker_state = ipc.WORKER_STATE_EXITED
        exit_code = int(capture.returncode)
    observation = ipc.WorkerObservation(
        worker_state=worker_state,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr_text,
        truncated_stdout=capture.truncated_stdout,
        truncated_stderr=capture.truncated_stderr,
        worker_pid=os.getpid(),
        worker_uid=worker_uid,
        controller_pid=os.getppid(),
        controller_uid=None,
    )
    return ipc.encode_observation(observation)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    request_raw = sys.stdin.read()
    if not request_raw.strip():
        print('{"schema":"bridle.trusted_ipc/v2","error":"empty_request"}', file=sys.stderr)
        return 2
    try:
        observation_raw = run_worker_request(request_raw)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("candidate_worker_failed")
        print(str(exc), file=sys.stderr)
        return 1
    sys.stdout.write(observation_raw)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
