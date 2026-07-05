"""Tests for bounded subprocess stream capture — real child processes and pipes.

Covers: callback exception propagation, silent hang, no-newline flood, stderr
flood, process-tree reaping (POSIX process groups), and candidate_worker
internal drain behavior.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "subprocess_stream.py"
SPEC = importlib.util.spec_from_file_location("bridle_subprocess_stream_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
stream = importlib.util.module_from_spec(SPEC)
sys.modules["bridle_subprocess_stream_test"] = stream
SPEC.loader.exec_module(stream)


def _spawn(cmd: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
    popen_kwargs: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    popen_kwargs.update(kwargs)
    return subprocess.Popen(cmd, **popen_kwargs)


def test_silent_hang_times_out() -> None:
    proc = _spawn([sys.executable, "-c", "import time; time.sleep(30)"])
    result = stream.capture_with_deadline(proc, max_bytes=65536, timeout=0.5)
    assert result.timed_out is True
    assert result.callback_error is None
    # The process must have been terminated (returncode set, whatever the platform).
    assert proc.poll() is not None


def test_no_newline_flood_truncates_without_hang() -> None:
    proc = _spawn(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 200000); import time; time.sleep(30)"]
    )
    result = stream.capture_with_deadline(proc, max_bytes=4096, timeout=1.0)
    assert result.truncated_stdout is True
    assert len(result.stdout) <= 4096


def test_stderr_flood_truncates_and_stdout_intact() -> None:
    proc = _spawn(
        [
            sys.executable,
            "-c",
            "import sys; print('marker', flush=True); sys.stderr.buffer.write(b'e' * 200000)",
        ]
    )
    result = stream.capture_with_deadline(proc, max_bytes=4096, timeout=5.0)
    assert result.truncated_stderr is True
    assert b"marker" in result.stdout


def test_callback_exception_propagates_and_kills_process() -> None:
    proc = _spawn([sys.executable, "-c", "print('line1', flush=True); import time; time.sleep(30)"])

    def _bad_callback(line: str) -> None:
        raise ValueError("callback_boom")

    result = stream.capture_with_deadline(
        proc, max_bytes=65536, timeout=2.0, on_stdout_line=_bad_callback
    )
    assert result.callback_error is not None
    assert "callback_boom" in result.callback_error
    assert b"line1" in result.stdout
    # Process must have been terminated, not left running.
    assert proc.poll() is not None


def test_callback_exception_in_poll_propagates() -> None:
    proc = _spawn([sys.executable, "-c", "import time; time.sleep(30)"])

    def _bad_poll() -> None:
        raise RuntimeError("poll_boom")

    result = stream.capture_with_deadline(proc, max_bytes=65536, timeout=2.0, on_poll=_bad_poll)
    assert result.callback_error is not None
    assert "poll_boom" in result.callback_error
    assert proc.poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group reaping")
def test_process_tree_reap_kills_grandchild() -> None:
    # Child spawns a grandchild that outlives the child; capture_with_deadline
    # must kill the whole process group so the grandchild does not survive.
    script = (
        "import os, time, sys\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    time.sleep(30)\n"
        "else:\n"
        "    print('child_started', flush=True)\n"
        "    time.sleep(30)\n"
    )
    proc = _spawn([sys.executable, "-c", script])
    result = stream.capture_with_deadline(proc, max_bytes=65536, timeout=0.5)
    assert result.timed_out is True
    # After timeout the process group should be gone.
    time.sleep(0.3)
    assert proc.poll() is not None


def test_concurrent_stderr_flood_with_blocking_stdout_no_deadlock() -> None:
    # Simulates the candidate_worker internal drain scenario: a child that
    # floods stderr while keeping stdout open (no EOF). Concurrent drain must
    # not deadlock; the deadline must still fire.
    script = (
        "import sys, time\n"
        "sys.stderr.buffer.write(b'e' * 200000)\n"
        "sys.stderr.buffer.flush()\n"
        "time.sleep(30)\n"
    )
    proc = _spawn([sys.executable, "-c", script])
    result = stream.capture_with_deadline(proc, max_bytes=4096, timeout=1.0)
    assert result.timed_out is True
    assert result.truncated_stderr is True
    assert proc.poll() is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group reaping")
def test_candidate_worker_internal_timeout_kills_pytest_process_group(tmp_path: Path) -> None:
    # On POSIX, candidate_worker starts pytest with start_new_session=True.
    # When the inner timeout fires, the whole pytest process group must be
    # reaped, not just the pytest leaf (otherwise grandchildren survive).
    worker_script = REPO_ROOT / "scripts" / "ci" / "candidate_worker.py"
    ipc_script = REPO_ROOT / "scripts" / "ci" / "trusted_ipc.py"
    ipc_spec = importlib.util.spec_from_file_location("bridle_test_ipc_internal", ipc_script)
    assert ipc_spec is not None and ipc_spec.loader is not None
    ipc = importlib.util.module_from_spec(ipc_spec)
    sys.modules["bridle_test_ipc_internal"] = ipc
    ipc_spec.loader.exec_module(ipc)

    candidate = tmp_path / "candidate"
    (candidate / "backend").mkdir(parents=True, exist_ok=True)
    (candidate / "backend" / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    request = ipc.encode_request(
        ipc.WorkerRequest(
            candidate_root=str(candidate),
            trusted_config=str(candidate / "backend" / "pyproject.toml"),
            pytest_args=("-c", "import time; time.sleep(30)"),
            public_env={},
        )
    )
    env = os.environ.copy()
    env["BRIDLE_WORKER_TIMEOUT"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-I", str(worker_script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(request.encode("utf-8"))
    proc.stdin.close()
    result = stream.capture_with_deadline(proc, max_bytes=65536, timeout=5.0)
    out = result.stdout.decode("utf-8", errors="replace")
    assert "timed_out" in out
    assert proc.poll() is not None
