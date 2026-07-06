"""Executor — run allowed test commands with shell=False and structured argv.

The executor never re-parses a raw command string with shell semantics. It
imports :func:`parse_command_argv` from the policy module so validation and
execution share the same structured argv. All allowed commands are spawned
via :class:`subprocess.Popen` with ``shell=False`` inside a worker thread.

Lifecycle guarantees:

* Single deadline covering stdout/stderr drain + process exit.
* Per-pipe output cap with truncation marker; we keep draining past the cap
  so a chatty grandchild cannot deadlock the parent via pipe backpressure.
* Timeout kills the whole process tree (POSIX process group, Windows Job
  Object with ``taskkill /T /F`` fallback); partial output is preserved;
  ``cleanup_error`` is reported separately from the primary timeout fact.
* ``exit_code`` is ``None`` on timeout (no fake constant) and a real int on
  normal exit; ``timed_out`` distinguishes the two states.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.wintypes
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bridle.agent.tools.test_command_policy import (
    SHELL_META_SUBSTRINGS,
    ParsedCommand,
    parse_command_argv,
)
from bridle.config import get_config

# Per-pipe output cap. Bounds memory while still draining the pipe.
MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MiB per pipe
# Grace period after a timeout kill during which we still try to reap.
_KILL_GRACE_SECONDS = 2.0
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class _BlockingRunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    cleanup_error: str | None


class Executor:
    """Execute allowed test commands and capture stdout/stderr."""

    def __init__(
        self,
        workspace: str | None = None,
        runs_dir: str | Path | None = None,
    ) -> None:
        self.workspace = workspace
        self._runs_dir = Path(runs_dir) if runs_dir is not None else None

    async def run_command(
        self,
        command: str,
        run_id: str | None = None,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> dict:
        """Run a single allowed command via shell=False with structured argv."""
        start = time.monotonic()
        try:
            parsed = parse_command_argv(command)
        except ValueError as exc:
            return self._failure_result(run_id, start, str(exc), exit_code=None)

        head = parsed.argv[0].lower().replace("\\", "/").split("/")[-1]
        # echo/exit are cmd.exe builtins on Windows and trivial on POSIX. We
        # handle them deterministically without spawning any subprocess so no
        # shell expansion of %VAR% or ^ escapes can occur, and no second
        # command can ever run.
        if head in ("echo", "exit"):
            return self._deterministic_builtin(parsed, run_id, start)

        return await self._run_argv(
            parsed.argv,
            original=command,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            env=env,
            start=start,
        )

    async def run_python_command(
        self,
        command: str,
        run_id: str | None = None,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> dict:
        """Run a python/pytest command via Popen(shell=False)."""
        start = time.monotonic()
        try:
            parsed = parse_command_argv(command)
        except ValueError as exc:
            return self._failure_result(run_id, start, str(exc), exit_code=None)
        return await self._run_argv(
            parsed.argv,
            original=command,
            run_id=run_id,
            timeout_seconds=timeout_seconds,
            env=env,
            start=start,
        )

    async def run_node_commands(
        self,
        commands: list[str],
        run_id: str | None = None,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> list[dict]:
        """Run multiple commands sequentially, return results for each."""
        results: list[dict] = []
        for cmd in commands:
            result = await self.run_command(
                cmd,
                run_id=run_id,
                timeout_seconds=timeout_seconds,
                env=env,
            )
            results.append(result)
            if result["exit_code"] != 0:
                break
        return results

    async def _run_argv(
        self,
        argv: list[str],
        *,
        original: str,
        run_id: str | None,
        timeout_seconds: float | None,
        env: dict[str, str] | None,
        start: float,
    ) -> dict:
        """Spawn argv with shell=False, unified deadline, output caps, tree kill."""
        # Defence in depth: even if a caller bypasses the policy, refuse to
        # spawn any argv containing shell meta-characters. ``cmd.exe /c`` and
        # POSIX shells would otherwise interpret ``&``/``;``/``|``/``>`` as
        # operators and run a second command.
        meta = _argv_contains_shell_meta(argv)
        if meta is not None:
            return self._failure_result(
                run_id,
                start,
                f"Shell meta char '{meta}' in argv; refusing to spawn: {original!r}",
                exit_code=None,
            )

        path_value = env.get("PATH") if env else None
        resolved = _resolve_executable(argv[0], path_value)
        if resolved is None:
            return self._failure_result(
                run_id,
                start,
                f"Executable not found in PATH: {argv[0]}",
                exit_code=None,
            )
        prefix, _real_path = resolved
        full_argv = [*prefix, *argv[1:]]

        try:
            completed = await asyncio.to_thread(
                _run_subprocess_blocking,
                full_argv,
                cwd=self.workspace,
                env=env,
                timeout_seconds=timeout_seconds,
                start=start,
            )
        except (OSError, ValueError) as exc:
            return self._failure_result(
                run_id,
                start,
                f"{type(exc).__name__}: {exc}",
                exit_code=None,
            )

        if completed.timed_out:
            stderr_msg = f"Command timed out after {timeout_seconds}s"
            if completed.cleanup_error:
                stderr_msg += f"; cleanup_error={completed.cleanup_error}"
            stderr = (
                f"{completed.stderr}\n{stderr_msg}"
                if completed.stderr
                else stderr_msg
            )
            stdout_path, stderr_path = self._write_run_output(
                stdout=completed.stdout,
                stderr=stderr,
                run_id=run_id,
            )
            return {
                "exit_code": None,
                "stdout": completed.stdout,
                "stderr": stderr,
                "duration_ms": completed.duration_ms,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
                "timed_out": True,
                "cleanup_error": completed.cleanup_error,
            }

        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.exit_code if completed.exit_code is not None else -1
        if exit_code < 0 and not stdout and not stderr:
            stderr = (
                f"subprocess returned exit_code={exit_code} with no output. "
                f"cmd={original!r} cwd={self.workspace!r} "
                f"env_keys={sorted(env.keys()) if env else 'inherit'}"
            )
        stdout_path, stderr_path = self._write_run_output(
            stdout=stdout,
            stderr=stderr,
            run_id=run_id,
        )
        result: dict = {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": completed.duration_ms,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
        }
        if completed.cleanup_error is not None:
            result["cleanup_error"] = completed.cleanup_error
        return result

    def _deterministic_builtin(
        self,
        parsed: ParsedCommand,
        run_id: str | None,
        start: float,
    ) -> dict:
        """Handle echo/exit without spawning any subprocess."""
        argv = parsed.argv
        head = argv[0].lower()
        duration_ms = int((time.monotonic() - start) * 1000)
        if head == "echo":
            text = parsed.raw[4:].lstrip()
            stdout = f"{text}{self._line_separator()}"
            stdout_path, stderr_path = self._write_run_output(
                stdout=stdout,
                stderr="",
                run_id=run_id,
            )
            return {
                "exit_code": 0,
                "stdout": stdout,
                "stderr": "",
                "duration_ms": duration_ms,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
            }
        if head == "exit":
            if len(argv) >= 2:
                try:
                    code = int(argv[1])
                except ValueError:
                    msg = f"exit: bad code: {argv[1]}"
                    stdout_path, stderr_path = self._write_run_output(
                        stdout="",
                        stderr=msg,
                        run_id=run_id,
                    )
                    return {
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": msg,
                        "duration_ms": duration_ms,
                        "stdout_path": stdout_path,
                        "stderr_path": stderr_path,
                    }
            else:
                code = 0
            stdout_path, stderr_path = self._write_run_output(
                stdout="",
                stderr="",
                run_id=run_id,
            )
            return {
                "exit_code": code,
                "stdout": "",
                "stderr": "",
                "duration_ms": duration_ms,
                "stdout_path": stdout_path,
                "stderr_path": stderr_path,
            }
        return self._failure_result(
            run_id,
            start,
            f"Unknown builtin: {head}",
            exit_code=-1,
        )

    def _failure_result(
        self,
        run_id: str | None,
        start: float,
        message: str,
        *,
        exit_code: int | None,
    ) -> dict:
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout_path, stderr_path = self._write_run_output(
            stdout="",
            stderr=message,
            run_id=run_id,
        )
        return {
            "exit_code": exit_code,
            "stdout": "",
            "stderr": message,
            "duration_ms": duration_ms,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
        }

    def _resolve_run_output_dir(self, run_id: str) -> Path | None:
        if not run_id:
            return None

        if self._runs_dir is not None:
            run_dir = self._runs_dir / run_id
        else:
            try:
                config = get_config()
            except RuntimeError:
                if not self.workspace:
                    return None
                run_dir = Path(self.workspace) / ".bridle-runs" / run_id
            else:
                run_dir = config.runs_dir / run_id

        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _write_run_output(
        self,
        stdout: str,
        stderr: str,
        run_id: str | None,
    ) -> tuple[str | None, str | None]:
        run_dir = self._resolve_run_output_dir(run_id) if run_id else None
        if run_dir is None:
            return None, None

        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        return str(stdout_path), str(stderr_path)

    def _line_separator(self) -> str:
        if self.workspace:
            try:
                return "\r\n" if Path(self.workspace).drive else "\n"
            except OSError:
                pass
        return "\n"


def _run_subprocess_blocking(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None,
    timeout_seconds: float | None,
    start: float,
) -> _BlockingRunResult:
    """Run argv with subprocess.Popen(shell=False) and bounded pipe drain."""
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    job_handle = _create_windows_kill_job() if os.name == "nt" else None
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            shell=False,
            start_new_session=(os.name != "nt"),
            creationflags=creationflags,
        )
    except Exception:
        if job_handle is not None:
            _close_windows_handle(job_handle)
        raise
    if job_handle is not None:
        assign_error = _assign_process_to_windows_job(job_handle, proc)
        if assign_error is not None:
            _close_windows_handle(job_handle)
            job_handle = None

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_truncated_ref = [False]
    stderr_truncated_ref = [False]

    def _read_pipe(pipe, buf: bytearray, truncated_ref: list[bool]) -> None:  # noqa: ANN001
        while True:
            chunk = pipe.read(_READ_CHUNK)
            if not chunk:
                break
            if not truncated_ref[0] and len(buf) < MAX_OUTPUT_BYTES:
                room = MAX_OUTPUT_BYTES - len(buf)
                buf.extend(chunk[:room])
                if len(buf) >= MAX_OUTPUT_BYTES:
                    truncated_ref[0] = True
        with contextlib.suppress(OSError):
            pipe.close()

    stdout_thread = threading.Thread(
        target=_read_pipe,
        args=(proc.stdout, stdout_buf, stdout_truncated_ref),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_pipe,
        args=(proc.stderr, stderr_buf, stderr_truncated_ref),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    cleanup_error: str | None = None
    timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        cleanup_error = _kill_blocking_process_tree(proc, job_handle)
        try:
            proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            if cleanup_error is None:
                cleanup_error = "process did not exit after kill"

    stdout_thread.join(timeout=_KILL_GRACE_SECONDS)
    stderr_thread.join(timeout=_KILL_GRACE_SECONDS)
    if stdout_thread.is_alive() and cleanup_error is None:
        cleanup_error = "stdout reader did not finish"
    if stderr_thread.is_alive() and cleanup_error is None:
        cleanup_error = "stderr reader did not finish"

    stdout = bytes(stdout_buf).decode("utf-8", errors="replace")
    stderr = bytes(stderr_buf).decode("utf-8", errors="replace")
    if stdout_truncated_ref[0]:
        stdout += "\n...[stdout truncated]"
    if stderr_truncated_ref[0]:
        stderr += "\n...[stderr truncated]"
    if job_handle is not None:
        _close_windows_handle(job_handle)
    return _BlockingRunResult(
        exit_code=None if timed_out else proc.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((time.monotonic() - start) * 1000),
        timed_out=timed_out,
        cleanup_error=cleanup_error,
    )


def _kill_blocking_process_tree(
    proc: subprocess.Popen[bytes],
    job_handle: int | None = None,
) -> str | None:
    """Kill a Popen process tree. Returns cleanup_error string or None."""
    if proc.poll() is not None:
        return None
    pid = proc.pid
    try:
        if os.name == "nt":
            if job_handle is not None:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                if kernel32.TerminateJobObject(job_handle, 1):
                    return None
                job_error = ctypes.get_last_error()
            else:
                job_error = None
            with contextlib.suppress(OSError):
                proc.kill()
            result = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=5.0,
            )
            if result.returncode != 0 and proc.poll() is None:
                stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
                prefix = f"TerminateJobObject failed error={job_error}; " if job_error else ""
                return f"{prefix}taskkill exit={result.returncode} for pid {pid}: {stderr[:200]}"
            return None
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return None
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=_KILL_GRACE_SECONDS)
            return None
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            return None
    except (OSError, subprocess.SubprocessError) as exc:
        return f"kill failed for pid {pid}: {type(exc).__name__}: {exc}"


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.wintypes.DWORD),
        ("SchedulingClass", ctypes.wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _create_windows_kill_job() -> int | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job,
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        _close_windows_handle(job)
        return None
    return int(job)


def _assign_process_to_windows_job(
    job_handle: int,
    proc: subprocess.Popen[bytes],
) -> str | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ok = kernel32.AssignProcessToJobObject(
        ctypes.wintypes.HANDLE(job_handle),
        ctypes.wintypes.HANDLE(int(proc._handle)),  # noqa: SLF001 - Windows Popen handle
    )
    if ok:
        return None
    return f"AssignProcessToJobObject failed error={ctypes.get_last_error()}"


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    with contextlib.suppress(OSError):
        kernel32.CloseHandle(ctypes.wintypes.HANDLE(handle))


def _argv_contains_shell_meta(argv: list[str]) -> str | None:
    """Return the first shell meta-char found in any argv token, or None."""
    for tok in argv:
        for op in SHELL_META_SUBSTRINGS:
            if op in tok:
                return op
    return None


def _resolve_executable(
    name: str,
    path_value: str | None,
) -> tuple[list[str], str] | None:
    """Resolve an executable to an argv prefix.

    Returns ``(prefix_argv, real_path)`` or ``None`` if not found. For
    ``.cmd`` / ``.bat`` files on Windows, the prefix is ``["cmd.exe", "/c",
    real_path]`` because ``CreateProcess`` cannot execute batch files
    directly. Since the policy already forbids shell meta-characters in the
    raw command, the structured argv we pass to ``cmd.exe /c`` is safe.
    """
    full = shutil.which(name, path=path_value)
    if full is None:
        # Fall back to the raw name; the OS will resolve it via the env's
        # PATH at exec time. If it cannot, the spawn fails with a typed
        # error which the caller reports as exit_code=None.
        return ([name], name)
    low = full.lower()
    if low.endswith(".cmd") or low.endswith(".bat"):
        return (["cmd.exe", "/c", full], full)
    return ([full], full)
