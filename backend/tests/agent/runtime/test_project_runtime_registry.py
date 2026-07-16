from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.project_registry import ProjectRuntimeRegistry
from bridle.api.errors import ConflictError
from bridle.features.project_map.store import ProjectPlanStore


def _project(test_workspace: Path, name: str) -> Path:
    root = test_workspace / name
    root.mkdir()
    ProjectPlanStore(root, project_id=name).initialize(scan_if_created=False)
    return root


def _registry(db: AsyncSession) -> ProjectRuntimeRegistry:
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    return ProjectRuntimeRegistry(runtime_host=AgentRuntimeHost(sessions))


@pytest.mark.asyncio
async def test_concurrent_wake_returns_one_generation(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    root = _project(test_workspace, "project-a")
    registry = _registry(db)

    handles = await asyncio.gather(
        *(registry.wake("project-a", root) for _item in range(16))
    )

    assert all(handle is handles[0] for handle in handles)
    assert registry.generation("project-a") == 1
    await registry.stop_all()


@pytest.mark.asyncio
async def test_project_identity_conflict_fails_closed(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    registry = _registry(db)
    first = _project(test_workspace, "first")
    second = _project(test_workspace, "second")
    await registry.wake("project-a", first)

    with pytest.raises(ConflictError):
        await registry.wake("project-a", second)

    await registry.stop_all()


@pytest.mark.asyncio
async def test_stop_all_cleans_active_generations(
    test_workspace: Path,
    db: AsyncSession,
) -> None:
    registry = _registry(db)
    await registry.wake("project-a", _project(test_workspace, "project-a"))
    await registry.wake("project-b", _project(test_workspace, "project-b"))

    result = await registry.stop_all()

    assert result.failures == ()
    assert registry.active_count == 0
