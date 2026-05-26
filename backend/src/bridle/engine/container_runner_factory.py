"""Resolve ContainerRunner for production, tests, and dry-run."""
from __future__ import annotations

import os
from pathlib import Path

from bridle.api.deps import is_test_mode
from bridle.engine.container_runner import (
    ContainerRunner,
    FakeContainerRunner,
    LocalContainerRuntimeRunner,
)


def resolve_container_runner(
    workspace_root: str | Path,
    *,
    runner: ContainerRunner | None = None,
) -> ContainerRunner:
    """Pick runner: explicit > test/dry-run/fake env > local runtime (production default)."""
    if runner is not None:
        return runner
    mode = os.getenv("BRIDLE_CONTAINER_RUNNER", "").strip().lower()
    if (
        is_test_mode()
        or mode == "fake"
        or os.getenv("BRIDLE_CONTAINER_DRY_RUN", "").strip() == "1"
    ):
        return FakeContainerRunner(workspace_root=workspace_root)
    return LocalContainerRuntimeRunner(workspace_root=workspace_root, use_docker=True)
