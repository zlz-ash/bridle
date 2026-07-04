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
            str(probe_root),
            *extra_args,
        ]
    test_file = candidate / "backend/src/bridle/agent/container/tests/test_docker_integration.py"
    return [
        "-m",
        "pytest",
        "-c",
        str(trusted_config.resolve()),
        "--rootdir",
        str(candidate / "backend"),
        "--confcutdir",
        str(test_file.parent),
        "-p",
        "no:cacheprovider",
        str(test_file),
        *extra_args,
    ]


def run_worker_request(request_raw: str) -> str:
    ipc = _load_ipc_module()
    request = ipc.decode_request(request_raw)
    candidate_root = Path(request.candidate_root).resolve()
    trusted_config = Path(request.trusted_config).resolve()

    clean_environment = sanitized_environment(dict(os.environ))
    os.environ.clear()
    os.environ.update(clean_environment)
    os.environ.update(request.public_env)
    os.environ["BRIDLE_CANDIDATE_WORKER"] = "1"

    candidate_source = candidate_root / "backend/src"
    sys.path.insert(0, str(candidate_source))
    os.chdir(candidate_root / "backend")

    invocation = pytest_arguments(
        candidate_root=candidate_root,
        trusted_config=trusted_config,
        extra_args=request.pytest_args,
    )
    LOGGER.info("candidate_worker_pytest_started candidate_root=%s", candidate_root)
    proc = subprocess.Popen(
        [sys.executable, *invocation],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    stdout_parts: list[bytes] = []
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        stdout_parts.append(raw)
    stderr = proc.stderr.read() if proc.stderr is not None else b""
    returncode = proc.wait()
    stdout = b"".join(stdout_parts).decode("utf-8", errors="replace")
    stderr_text = stderr.decode("utf-8", errors="replace")
    truncated_stdout = len(b"".join(stdout_parts)) > ipc.MAX_STREAM_BYTES
    truncated_stderr = len(stderr) > ipc.MAX_STREAM_BYTES
    if truncated_stdout:
        stdout = b"".join(stdout_parts)[: ipc.MAX_STREAM_BYTES].decode("utf-8", errors="replace")
    if truncated_stderr:
        stderr_text = stderr[: ipc.MAX_STREAM_BYTES].decode("utf-8", errors="replace")
    LOGGER.info("candidate_worker_pytest_finished exit_code=%d", returncode)

    worker_uid = os.getuid() if hasattr(os, "getuid") else None
    observation = ipc.WorkerObservation(
        worker_state=ipc.WORKER_STATE_EXITED,
        exit_code=int(returncode),
        stdout=stdout,
        stderr=stderr_text,
        truncated_stdout=truncated_stdout,
        truncated_stderr=truncated_stderr,
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
