"""Project-scoped shared container backend registry."""
from __future__ import annotations

import threading
from pathlib import Path

from bridle.agent.container.backend import AgentContainerBackend
from bridle.agent.container.runner import ContainerRunner
from bridle.agent.container.runner_factory import resolve_container_runner

_lock = threading.Lock()
_backends: dict[str, AgentContainerBackend] = {}
_runner_overrides: dict[str, ContainerRunner | None] = {}


def configure_runner(project_root: str | Path, runner: ContainerRunner | None) -> None:
    key = str(Path(project_root).resolve())
    with _lock:
        _runner_overrides[key] = runner
        _backends.pop(key, None)


def reset_for_tests() -> None:
    with _lock:
        _backends.clear()
        _runner_overrides.clear()


def get_shared_container_backend(project_root: str | Path) -> AgentContainerBackend:
    key = str(Path(project_root).resolve())
    with _lock:
        existing = _backends.get(key)
        if existing is not None:
            return existing
        runner = _runner_overrides.get(key)
        if runner is None and key in _runner_overrides:
            resolved = _runner_overrides[key]
        else:
            resolved = resolve_container_runner(key, runner=runner)
        backend = AgentContainerBackend(key, runner=resolved)
        _backends[key] = backend
        return backend
