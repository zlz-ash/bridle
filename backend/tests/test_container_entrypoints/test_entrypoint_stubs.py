"""Step 1: console scripts resolve and exit cleanly."""
from __future__ import annotations

import subprocess
import sys


def test_main_agent_entrypoint_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "bridle.container_entrypoints.main_agent"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, "BRIDLE_CONTAINER_ENTRYPOINT_STUB": "1"},
    )
    assert result.returncode == 0, result.stderr


def test_node_agent_entrypoint_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "bridle.container_entrypoints.node_agent"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, "BRIDLE_CONTAINER_ENTRYPOINT_STUB": "1"},
    )
    assert result.returncode == 0, result.stderr
