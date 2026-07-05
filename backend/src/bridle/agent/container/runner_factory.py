"""Resolve container runner implementation."""
from __future__ import annotations

import os
from pathlib import Path

from bridle.agent.container.runner import ContainerRunner, FakeContainerRunner, LocalContainerRuntimeRunner


def resolve_container_runner(
    workspace_root: str | Path,
    *,
    runner: ContainerRunner | None = None,
) -> ContainerRunner:
    """Pick runner: explicit > fake/dry-run env > local Docker runtime."""
    if runner is not None:
        return runner
    mode = os.getenv("BRIDLE_CONTAINER_RUNNER", "").strip().lower()
    if mode == "fake" or os.getenv("BRIDLE_CONTAINER_DRY_RUN", "").strip() == "1":
        return FakeContainerRunner(workspace_root=workspace_root)
    return LocalContainerRuntimeRunner(workspace_root=workspace_root, use_docker=True)
