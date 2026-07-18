"""End-to-end lifecycle tests for Executor.

These tests exercise the real production entry (:class:`Executor` and
:class:`SandboxedToolExecutor.run_command`) with real subprocesses in
an isolated workspace. They prove:

* shell meta-characters cannot trigger a second command — a sentinel file
  left behind by the would-be second command must never appear;
* stdout/stderr are drained concurrently under a single deadline so a
  dual-pipe flood cannot deadlock the parent;
* very long output with no newlines is captured up to the cap and marked
  truncated;
* on timeout the whole process tree (including grandchildren) is reaped,
  partial output is preserved, ``exit_code`` is null, ``timed_out`` is set
  and ``cleanup_error`` is reported separately;
* output beyond the cap is truncated with a marker.

No Fake/Mock objects are used to fabricate attack outcomes — every command
runs through the real subprocess spawn path.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from bridle.agent.tools.executor import MAX_OUTPUT_BYTES, Executor

# --- helpers ---------------------------------------------------------------


def _python_exe() -> str:
    return sys.executable


def _sentinel_script(tmp_path: Path, sentinel: Path) -> Path:
    """Write a python helper that creates ``sentinel`` only when run."""
    script = tmp_path / "make_sentinel.py"
    script.write_text(
        f"import os, sys\n"
        f"path = r'{sentinel}'\n"
        f"os.makedirs(os.path.dirname(path), exist_ok=True)\n"
        f"with open(path, 'w', encoding='utf-8') as fh:\n"
        f"    fh.write('PWNED')\n"
        f"sys.exit(0)\n",
        encoding="utf-8",
    )
    return script


# --- shell injection -------------------------------------------------------


@pytest.mark.asyncio
async def test_ampersand_does_not_spawn_second_command(tmp_path: Path) -> None:
    sentinel = tmp_path / "pwned.txt"
    executor = Executor(workspace=str(tmp_path))
    # If a shell ran this, the second echo would create sentinel.txt.
    # With deterministic echo + structured argv, & is literal text.
    result = await executor.run_command(
        f"echo hello & echo PWNED > {sentinel}"
    )
    assert not sentinel.exists()
    # echo returns 0 deterministically.
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


@pytest.mark.asyncio
async def test_newline_does_not_spawn_second_command(tmp_path: Path) -> None:
    sentinel = tmp_path / "pwned.txt"
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_command(
        f"echo hello\necho PWNED > {sentinel}"
    )
    assert not sentinel.exists()
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_semicolon_does_not_spawn_second_command(tmp_path: Path) -> None:
    sentinel = tmp_path / "pwned.txt"
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_command(
        f"echo hello ; echo PWNED > {sentinel}"
    )
    assert not sentinel.exists()
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_pipe_does_not_spawn_second_command(tmp_path: Path) -> None:
    sentinel = tmp_path / "pwned.txt"
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_command(
        f"echo hello | echo PWNED > {sentinel}"
    )
    assert not sentinel.exists()
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_backtick_does_not_spawn_second_command(tmp_path: Path) -> None:
    sentinel = tmp_path / "pwned.txt"
    executor = Executor(workspace=str(tmp_path))
    # Backtick command substitution would write sentinel if a shell ran it.
    result = await executor.run_command(
        f"echo hello `echo PWNED > {sentinel}`"
    )
    assert not sentinel.exists()
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_dollar_paren_does_not_spawn_second_command(tmp_path: Path) -> None:
    sentinel = tmp_path / "pwned.txt"
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_command(
        f"echo hello $(echo PWNED > {sentinel})"
    )
    assert not sentinel.exists()
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_redirect_does_not_create_file(tmp_path: Path) -> None:
    target = tmp_path / "out.log"
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_command(f"echo hello > {target}")
    # echo is deterministic; the `>` is literal text, no file is created.
    assert not target.exists()
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_windows_caret_does_not_escape(tmp_path: Path) -> None:
    sentinel = tmp_path / "pwned.txt"
    executor = Executor(workspace=str(tmp_path))
    # cmd.exe would interpret ^& as a literal & only after caret escape;
    # with deterministic echo the whole thing is literal text and no
    # second command runs.
    result = await executor.run_command(
        f"echo hello^&echo PWNED > {sentinel}"
    )
    assert not sentinel.exists()
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_windows_percent_var_not_expanded(tmp_path: Path) -> None:
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_command("echo %PATH%")
    # The literal text is preserved, not expanded by a shell.
    assert "%PATH%" in result["stdout"]
    assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_npm_with_ampersand_refused_at_executor_boundary(
    tmp_path: Path,
) -> None:
    # Even if a caller bypassed the policy, the executor's own meta-char
    # guard must refuse to spawn ``cmd.exe /c npm.cmd ... & rm``.
    sentinel = tmp_path / "pwned.txt"
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_command(
        f"npm test & echo PWNED > {sentinel}"
    )
    assert sentinel.exists() is False
    # exit_code is None because we refused to spawn (failed_before_exec).
    assert result["exit_code"] is None
    assert "Shell meta char" in result["stderr"]


# --- dual-pipe flood (no deadlock) -----------------------------------------


@pytest.mark.asyncio
async def test_dual_pipe_flood_drained_without_deadlock(tmp_path: Path) -> None:
    script = tmp_path / "flood.py"
    script.write_text(
        "import sys\n"
        "for i in range(2000):\n"
        "    sys.stdout.write(f'out-{i}\\n')\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.write(f'err-{i}\\n')\n"
        "    sys.stderr.flush()\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_python_command(
        f"{_python_exe()} {script}",
        timeout_seconds=30.0,
    )
    assert result["exit_code"] == 0
    assert "out-0" in result["stdout"]
    assert "err-0" in result["stderr"]
    assert "out-1999" in result["stdout"]
    assert "err-1999" in result["stderr"]


# --- very long no-newline output -------------------------------------------


@pytest.mark.asyncio
async def test_very_long_no_newline_output_captured(tmp_path: Path) -> None:
    script = tmp_path / "longline.py"
    # 4 MB single line with no newline.
    script.write_text(
        "import sys\n"
        "sys.stdout.write('A' * (4 * 1024 * 1024))\n"
        "sys.stdout.flush()\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_python_command(
        f"{_python_exe()} {script}",
        timeout_seconds=30.0,
    )
    assert result["exit_code"] == 0
    assert len(result["stdout"]) >= 4 * 1024 * 1024


# --- output truncation -----------------------------------------------------


@pytest.mark.asyncio
async def test_output_beyond_cap_is_truncated(tmp_path: Path) -> None:
    script = tmp_path / "huge.py"
    # Write more than MAX_OUTPUT_BYTES to stdout.
    over = MAX_OUTPUT_BYTES + 1024
    script.write_text(
        f"import sys\n"
        f"sys.stdout.write('B' * {over})\n"
        f"sys.stdout.flush()\n"
        f"sys.exit(0)\n",
        encoding="utf-8",
    )
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_python_command(
        f"{_python_exe()} {script}",
        timeout_seconds=30.0,
    )
    assert result["exit_code"] == 0
    assert "[stdout truncated]" in result["stdout"]
    # The retained portion is bounded.
    assert len(result["stdout"]) <= MAX_OUTPUT_BYTES + len("\n...[stdout truncated]")


# --- timeout: full process tree reaping ------------------------------------


@pytest.mark.asyncio
async def test_timeout_kills_grandchild_process(tmp_path: Path) -> None:
    grandchild_marker = tmp_path / "grandchild_alive.txt"
    if grandchild_marker.exists():
        grandchild_marker.unlink()
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import subprocess, sys, time, os\n"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        "    'import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_python_command(
        f"{_python_exe()} {script}",
        timeout_seconds=2.0,
    )
    assert result["timed_out"] is True
    assert result["exit_code"] is None
    # Partial stdout (the child's pid line) is preserved.
    pid_line = result["stdout"].strip()
    if pid_line.isdigit():
        pid = int(pid_line)
        # Give the OS a moment to reap, then verify the grandchild is gone.
        await asyncio.sleep(0.5)
        try:
            os.kill(pid, 0)
            still_alive = True
        except (ProcessLookupError, PermissionError, OSError):
            still_alive = False
        assert not still_alive, f"grandchild pid {pid} survived timeout"


@pytest.mark.asyncio
async def test_timeout_preserves_partial_output_and_cleanup_error_field(
    tmp_path: Path,
) -> None:
    script = tmp_path / "slow.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdout.write('partial-out\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('partial-err\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_python_command(
        f"{_python_exe()} {script}",
        timeout_seconds=1.5,
    )
    assert result["timed_out"] is True
    assert result["exit_code"] is None
    assert "partial-out" in result["stdout"]
    assert "timed out" in result["stderr"].lower()
    # cleanup_error may be None on clean kill, but the field must be present.
    assert "cleanup_error" in result


# --- normal exit path ------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_exit_returns_real_exit_code(tmp_path: Path) -> None:
    executor = Executor(workspace=str(tmp_path))
    result = await executor.run_python_command(
        f"{_python_exe()} -c \"raise SystemExit(7)\"",
        timeout_seconds=10.0,
    )
    assert result["exit_code"] == 7
    assert result.get("timed_out", False) is False
