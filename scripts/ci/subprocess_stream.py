#!/usr/bin/env python3
"""Bounded subprocess/container stdout/stderr drain with monotonic deadline.

Features:
- Concurrent stdout/stderr drain (no pipe backpressure deadlock).
- Bounded capture buffers (truncation markers preserved).
- Deadline-aware: timeout kills the whole process tree, not just the leaf.
- Callback exceptions in ``on_stdout_line`` propagate back to the caller via
  ``StreamCaptureResult.callback_error`` and trigger process-tree termination.
- Process-tree reaping: POSIX uses process groups (``start_new_session``),
  Windows uses ``taskkill /T`` against the spawned PID.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class StreamCaptureResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    truncated_stdout: bool
    truncated_stderr: bool
    timed_out: bool
    callback_error: str | None = None


def _terminate_process_tree(proc: subprocess.Popen[bytes], *, force: bool = True) -> None:
    """Terminate the whole process tree rooted at proc.

    POSIX: kill the process group created via start_new_session=True.
    Windows: taskkill /T /F against the PID (recursive child kill).
    Fallback: proc.kill() the direct process.
    """
    pid = proc.pid
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            pgid = os.getpgid(pid)
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    elif os.name == "nt":
        try:
            flag = "/F" if force else ""
            subprocess.run(
                ["taskkill", "/T", flag, "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def capture_with_deadline(
    proc: subprocess.Popen[bytes],
    *,
    max_bytes: int,
    timeout: float,
    on_stdout_line: Callable[[str], None] | None = None,
    on_poll: Callable[[], None] | None = None,
) -> StreamCaptureResult:
    deadline = time.monotonic() + timeout
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_lock = threading.Lock()
    stderr_lock = threading.Lock()
    truncated_stdout = False
    truncated_stderr = False
    timed_out = False
    callback_error: str | None = None
    callback_error_event = threading.Event()

    def _append(chunks: list[bytes], lock: threading.Lock, data: bytes, *, field: str) -> None:
        nonlocal truncated_stdout, truncated_stderr
        if not data:
            return
        with lock:
            current = sum(len(item) for item in chunks)
            if current >= max_bytes:
                if field == "stdout":
                    truncated_stdout = True
                else:
                    truncated_stderr = True
                return
            remaining = max_bytes - current
            chunks.append(data[:remaining])
            if len(data) > remaining:
                if field == "stdout":
                    truncated_stdout = True
                else:
                    truncated_stderr = True

    def _drain_stdout() -> None:
        nonlocal truncated_stdout, callback_error
        assert proc.stdout is not None
        buffer = b""
        while True:
            if time.monotonic() > deadline:
                return
            try:
                chunk = proc.stdout.read(4096)
            except (OSError, ValueError):
                return
            if not chunk:
                if buffer:
                    _append(stdout_chunks, stdout_lock, buffer, field="stdout")
                    if on_stdout_line is not None:
                        try:
                            on_stdout_line(buffer.decode("utf-8", errors="replace").rstrip("\n"))
                        except Exception as exc:  # noqa: BLE001 — propagate to caller
                            callback_error = f"{type(exc).__name__}:{exc}"
                            callback_error_event.set()
                            return
                return
            buffer += chunk
            if len(buffer) > max_bytes:
                _append(stdout_chunks, stdout_lock, buffer[:max_bytes], field="stdout")
                truncated_stdout = True
                return
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                _append(stdout_chunks, stdout_lock, line + b"\n", field="stdout")
                if on_stdout_line is not None:
                    try:
                        on_stdout_line(line.decode("utf-8", errors="replace"))
                    except Exception as exc:  # noqa: BLE001 — propagate to caller
                        callback_error = f"{type(exc).__name__}:{exc}"
                        callback_error_event.set()
                        return
                if truncated_stdout:
                    return

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            if time.monotonic() > deadline:
                return
            try:
                chunk = proc.stderr.read(4096)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            _append(stderr_chunks, stderr_lock, chunk, field="stderr")

    stdout_thread = threading.Thread(target=_drain_stdout, name="worker-stdout-drain", daemon=True)
    stderr_thread = threading.Thread(target=_drain_stderr, name="worker-stderr-drain", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process_tree(proc)
            break
        if callback_error_event.is_set():
            _terminate_process_tree(proc)
            break
        if on_poll is not None:
            try:
                on_poll()
            except Exception as exc:  # noqa: BLE001 — poll callback failure is a primary failure
                callback_error = f"poll_error:{type(exc).__name__}:{exc}"
                callback_error_event.set()
                _terminate_process_tree(proc)
                break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    # Join drain threads with the remaining deadline so we never return while
    # a thread is still writing to the shared buffers.
    join_timeout = max(1.0, deadline - time.monotonic())
    stdout_thread.join(timeout=join_timeout)
    stderr_thread.join(timeout=join_timeout)
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    return StreamCaptureResult(
        returncode=proc.returncode,
        stdout=b"".join(stdout_chunks),
        stderr=b"".join(stderr_chunks),
        truncated_stdout=truncated_stdout,
        truncated_stderr=truncated_stderr,
        timed_out=timed_out,
        callback_error=callback_error,
    )
