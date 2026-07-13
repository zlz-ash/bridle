from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.agent.runtime.project_map_agent import ProjectMapAgentState
from bridle.agent.runtime.project_registry import ProjectRuntimeRegistry
from bridle.features.projects.service import ProjectService
from tests.agent.runtime.test_project_runtime_registry import FakeWatcher


def _project(test_workspace: Path, name: str) -> Path:
    root = test_workspace / name
    root.mkdir()
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_repeated_open_reuses_project_runtime(
    db: AsyncSession,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = FakeWatcher()
    registry = ProjectRuntimeRegistry(watcher=watcher)
    monkeypatch.setattr(
        "bridle.features.projects.service.get_project_runtime_registry",
        lambda: registry,
    )
    root = _project(test_workspace, "reuse")

    first = await ProjectService.open_project(db, str(root))
    second = await ProjectService.open_project(db, str(root))

    assert first.id == second.id
    assert registry.active_count == 1
    assert len(watcher.start_calls) == 1
    await registry.stop_all()


@pytest.mark.asyncio
async def test_commit_failure_does_not_start_project_runtime(
    db: AsyncSession,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = FakeWatcher()
    registry = ProjectRuntimeRegistry(watcher=watcher)
    monkeypatch.setattr(
        "bridle.features.projects.service.get_project_runtime_registry",
        lambda: registry,
    )

    async def fail_commit() -> None:
        raise RuntimeError("commit_failed")

    monkeypatch.setattr(db, "commit", fail_commit)

    with pytest.raises(RuntimeError, match="commit_failed"):
        await ProjectService.open_project(db, str(_project(test_workspace, "commit")))

    assert registry.active_count == 0
    assert watcher.start_calls == []


@pytest.mark.asyncio
async def test_runtime_start_failure_rolls_back_and_open_can_retry(
    db: AsyncSession,
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = FakeWatcher()
    watcher.fail_after_start.add("placeholder")
    registry = ProjectRuntimeRegistry(watcher=watcher)
    monkeypatch.setattr(
        "bridle.features.projects.service.get_project_runtime_registry",
        lambda: registry,
    )
    root = _project(test_workspace, "retry")

    original_start = watcher.start
    failed_once = False

    def fail_once(project_root: Path, *, project_id: str) -> None:
        nonlocal failed_once
        original_start(project_root, project_id=project_id)
        if not failed_once:
            failed_once = True
            raise RuntimeError("watcher_start_failed_after_acquire")

    monkeypatch.setattr(watcher, "start", fail_once)
    with pytest.raises(RuntimeError, match="watcher_start_failed_after_acquire"):
        await ProjectService.open_project(db, str(root))

    assert registry.active_count == 0
    assert watcher.active_project_ids() == ()
    opened = await ProjectService.open_project(db, str(root))
    assert registry.get(opened.id).state is ProjectMapAgentState.RUNNING
    await registry.stop_all()
