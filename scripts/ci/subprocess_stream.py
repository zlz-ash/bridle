#!/usr/bin/env python3
"""Bounded subprocess/container stdout/stderr drain with monotonic deadline."""
from __future__ import annotations

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


def capture_with_deadline(
    proc: subprocess.Popen[bytes],
    *,
    max_bytes: int,
    timeout: float,
    on_stdout_line: Callable[[str], None] | None = None,
) -> StreamCaptureResult:
    deadline = time.monotonic() + timeout
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_lock = threading.Lock()
    stderr_lock = threading.Lock()
    truncated_stdout = False
    truncated_stderr = False
    timed_out = False

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
        nonlocal truncated_stdout
        assert proc.stdout is not None
        buffer = b""
        while True:
            if time.monotonic() > deadline:
                return
            chunk = proc.stdout.read(4096)
            if not chunk:
                if buffer:
                    _append(stdout_chunks, stdout_lock, buffer, field="stdout")
                    if on_stdout_line is not None:
                        on_stdout_line(buffer.decode("utf-8", errors="replace").rstrip("\n"))
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
                    on_stdout_line(line.decode("utf-8", errors="replace"))
                if truncated_stdout:
                    return

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            if time.monotonic() > deadline:
                return
            chunk = proc.stderr.read(4096)
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
            proc.kill()
            break
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    stdout_thread.join(timeout=1.0)
    stderr_thread.join(timeout=1.0)
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait(timeout=5)
    return StreamCaptureResult(
        returncode=proc.returncode,
        stdout=b"".join(stdout_chunks),
        stderr=b"".join(stderr_chunks),
        truncated_stdout=truncated_stdout,
        truncated_stderr=truncated_stderr,
        timed_out=timed_out,
    )
