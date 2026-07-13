from __future__ import annotations

import asyncio
import os
import threading
from collections import defaultdict
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from bridle.agent.runtime.project_map_agent import (
    ProjectMapAgent,
    ProjectMapAgentState,
    ProjectRuntimeShutdownError,
)
from bridle.agent.runtime.project_registry import ProjectRuntimeRegistry
from bridle.api.errors import ConflictError
from bridle.app import create_app
from bridle.features.project_map.watcher import CodeMapRefreshWatcher


class FakeWatcher:
    def __init__(self) -> None:
        self.start_calls: list[tuple[str, Path]] = []
        self.stop_calls: list[str] = []
        self.active: set[str] = set()
        self.start_failures: dict[str, int] = defaultdict(int)
        self.stop_failures: dict[str, int] = defaultdict(int)
        self.stop_false_failures: dict[str, int] = defaultdict(int)
        self.persistent_stop_failures: set[str] = set()
        self.fail_after_start: set[str] = set()

    def start(self, project_root: Path, *, project_id: str) -> None:
        self.start_calls.append((project_id, Path(project_root).resolve()))
        self.active.add(project_id)
        if project_id in self.fail_after_start:
            self.fail_after_start.remove(project_id)
            raise RuntimeError("watcher_start_failed_after_acquire")
        if self.start_failures[project_id] > 0:
            self.start_failures[project_id] -= 1
            self.active.discard(project_id)
            raise RuntimeError("watcher_start_failed")

    def stop(self, project_id: str, *, timeout_seconds: float = 5.0) -> bool:
        del timeout_seconds
        self.stop_calls.append(project_id)
        if project_id in self.persistent_stop_failures:
            raise RuntimeError("watcher_stop_persistent")
        if self.stop_failures[project_id] > 0:
            self.stop_failures[project_id] -= 1
            raise RuntimeError("watcher_stop_transient")
        if self.stop_false_failures[project_id] > 0:
            self.stop_false_failures[project_id] -= 1
            return False
        self.active.discard(project_id)
        return True

    def active_project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.active))


def _project(test_workspace: Path, name: str) -> Path:
    root = test_workspace / name
    root.mkdir()
    return root


@pytest.mark.asyncio
async def test_project_map_agent_start_is_idempotent(test_workspace: Path) -> None:
    watcher = FakeWatcher()
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)

    first = await agent.start()
    task = agent.task
    second = await agent.start()

    assert first is second is agent
    assert agent.state is ProjectMapAgentState.RUNNING
    assert agent.task is task
    assert [item[0] for item in watcher.start_calls] == ["project-a"]
    await agent.stop()


@pytest.mark.asyncio
async def test_project_map_agent_stop_is_idempotent(test_workspace: Path) -> None:
    watcher = FakeWatcher()
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)
    await agent.start()

    first = await agent.stop()
    second = await agent.stop()

    assert first is second is ProjectMapAgentState.STOPPED
    assert watcher.stop_calls == ["project-a"]
    assert agent.task is not None and agent.task.done()


@pytest.mark.asyncio
async def test_project_map_agent_stop_failure_can_retry(test_workspace: Path) -> None:
    watcher = FakeWatcher()
    watcher.stop_failures["project-a"] = 1
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)
    await agent.start()

    with pytest.raises(ProjectRuntimeShutdownError):
        await agent.stop()
    assert agent.state is not ProjectMapAgentState.STOPPED
    assert "project-a" in watcher.active_project_ids()

    assert await agent.stop() is ProjectMapAgentState.STOPPED
    assert watcher.stop_calls == ["project-a", "project-a"]
    assert agent.task is not None and agent.task.done()
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_project_map_agent_rolls_back_partially_started_watcher(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    watcher.fail_after_start.add("project-a")
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)

    with pytest.raises(RuntimeError, match="watcher_start_failed_after_acquire"):
        await agent.start()

    assert watcher.active_project_ids() == ()
    assert watcher.stop_calls == ["project-a"]
    assert agent.task is None
    assert agent.state is ProjectMapAgentState.FAILED


@pytest.mark.asyncio
async def test_start_rollback_stop_failure_retains_retryable_owner(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    watcher.fail_after_start.add("project-a")
    watcher.stop_failures["project-a"] = 1
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)

    with pytest.raises(ProjectRuntimeShutdownError):
        await agent.start()

    assert agent.state is ProjectMapAgentState.STOP_FAILED
    assert watcher.active_project_ids() == ("project-a",)
    assert await agent.stop() is ProjectMapAgentState.STOPPED
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_project_map_agent_rolls_back_watcher_when_task_creation_fails(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()

    def fail_task_factory(
        coroutine: Coroutine[Any, Any, None], *, name: str
    ) -> asyncio.Task[None]:
        del name
        coroutine.close()
        raise RuntimeError("task_create_failed")

    agent = ProjectMapAgent(
        "project-a",
        _project(test_workspace, "a"),
        watcher=watcher,
        task_factory=fail_task_factory,
    )

    with pytest.raises(RuntimeError, match="task_create_failed"):
        await agent.start()

    assert watcher.active_project_ids() == ()
    assert watcher.stop_calls == ["project-a"]
    assert agent.task is None
    assert agent.state is ProjectMapAgentState.FAILED


@pytest.mark.asyncio
async def test_concurrent_ensure_returns_one_project_map_agent(test_workspace: Path) -> None:
    watcher = FakeWatcher()
    registry = ProjectRuntimeRegistry(watcher=watcher)
    root = _project(test_workspace, "a")
    gate = asyncio.Event()

    async def ensure() -> ProjectMapAgent:
        await gate.wait()
        return await registry.ensure_started("project-a", root)

    tasks = [asyncio.create_task(ensure()) for _ in range(12)]
    gate.set()
    handles = await asyncio.gather(*tasks)

    assert all(handle is handles[0] for handle in handles)
    assert registry.active_count == 1
    assert len(watcher.start_calls) == 1
    await registry.stop_all()


@pytest.mark.asyncio
async def test_same_project_id_with_different_path_fails_closed(
    test_workspace: Path,
) -> None:
    registry = ProjectRuntimeRegistry(watcher=FakeWatcher())
    first = await registry.ensure_started("project-a", _project(test_workspace, "a"))

    with pytest.raises(ConflictError) as raised:
        await registry.ensure_started("project-a", _project(test_workspace, "b"))

    assert raised.value.api_error.code == "project_runtime_identity_conflict"
    assert registry.get("project-a") is first
    await registry.stop_all()


@pytest.mark.asyncio
async def test_same_path_with_different_project_id_fails_closed(
    test_workspace: Path,
) -> None:
    registry = ProjectRuntimeRegistry(watcher=FakeWatcher())
    root = _project(test_workspace, "a")
    first = await registry.ensure_started("project-a", root)

    with pytest.raises(ConflictError) as raised:
        await registry.ensure_started("project-b", root)

    assert raised.value.api_error.code == "project_runtime_identity_conflict"
    assert registry.get("project-a") is first
    await registry.stop_all()


@pytest.mark.asyncio
async def test_windows_case_variant_path_cannot_claim_second_owner(
    test_workspace: Path,
) -> None:
    registry = ProjectRuntimeRegistry(watcher=FakeWatcher())
    root = test_workspace / "NonexistentCaseRoot"
    case_variant = Path(str(root).swapcase())
    assert os.path.normcase(str(root)) == os.path.normcase(str(case_variant))
    first = await registry.ensure_started("project-a", root)

    with pytest.raises(ConflictError) as raised:
        await registry.ensure_started("project-b", case_variant)

    assert raised.value.api_error.code == "project_runtime_identity_conflict"
    assert registry.get("project-a") is first
    assert registry.active_project_ids == ("project-a",)
    await registry.stop_all()


@pytest.mark.asyncio
async def test_failed_start_leaves_no_registry_keys_and_can_retry(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    watcher.start_failures["project-a"] = 1
    registry = ProjectRuntimeRegistry(watcher=watcher)
    root = _project(test_workspace, "a")

    with pytest.raises(RuntimeError, match="watcher_start_failed"):
        await registry.ensure_started("project-a", root)

    assert registry.active_count == 0
    assert watcher.active_project_ids() == ()
    handle = await registry.ensure_started("project-a", root)
    assert handle.state is ProjectMapAgentState.RUNNING
    await registry.stop_all()


@pytest.mark.asyncio
async def test_persistent_start_rollback_failure_keeps_registry_owner(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    watcher.fail_after_start.add("project-a")
    watcher.persistent_stop_failures.add("project-a")
    registry = ProjectRuntimeRegistry(watcher=watcher)
    root = _project(test_workspace, "a")

    with pytest.raises(ProjectRuntimeShutdownError):
        await registry.ensure_started("project-a", root)

    assert registry.active_project_ids == ("project-a",)
    assert registry.get("project-a").state is ProjectMapAgentState.STOP_FAILED
    watcher.persistent_stop_failures.clear()
    replacement = await registry.ensure_started("project-a", root)
    assert replacement.state is ProjectMapAgentState.RUNNING
    await registry.stop_all()


@pytest.mark.asyncio
async def test_false_start_rollback_failure_keeps_registry_owner(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    watcher.fail_after_start.add("project-a")
    watcher.stop_false_failures["project-a"] = 1
    registry = ProjectRuntimeRegistry(watcher=watcher)
    root = _project(test_workspace, "a")

    with pytest.raises(ProjectRuntimeShutdownError):
        await registry.ensure_started("project-a", root)

    assert registry.active_project_ids == ("project-a",)
    assert registry.get("project-a").state is ProjectMapAgentState.STOP_FAILED
    replacement = await registry.ensure_started("project-a", root)
    assert replacement.state is ProjectMapAgentState.RUNNING
    assert watcher.stop_calls == ["project-a", "project-a"]
    await registry.stop_all()


@pytest.mark.asyncio
async def test_blocked_project_start_does_not_block_unrelated_project(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()
    original_start = watcher.start

    def blocking_start(project_root: Path, *, project_id: str) -> None:
        if project_id == "project-a":
            entered.set()
            assert release.wait(timeout=2.0)
        original_start(project_root, project_id=project_id)

    watcher.start = blocking_start  # type: ignore[method-assign]
    registry = ProjectRuntimeRegistry(watcher=watcher)
    first = asyncio.create_task(
        registry.ensure_started("project-a", _project(test_workspace, "a"))
    )
    assert await asyncio.to_thread(entered.wait, 1.0)

    second = await asyncio.wait_for(
        registry.ensure_started("project-b", _project(test_workspace, "b")),
        timeout=1.0,
    )
    assert second.project_id == "project-b"
    release.set()
    await first
    await registry.stop_all()


@pytest.mark.asyncio
async def test_cancelled_ensure_exposes_late_start_failure(test_workspace: Path) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()
    original_start = watcher.start

    def blocking_failed_start(project_root: Path, *, project_id: str) -> None:
        entered.set()
        assert release.wait(timeout=2.0)
        raise RuntimeError("late_watcher_start_failure")

    watcher.start = blocking_failed_start  # type: ignore[method-assign]
    registry = ProjectRuntimeRegistry(watcher=watcher)
    task = asyncio.create_task(
        registry.ensure_started("project-a", _project(test_workspace, "a"))
    )
    assert await asyncio.to_thread(entered.wait, 1.0)
    task.cancel()
    release.set()

    with pytest.raises(RuntimeError, match="late_watcher_start_failure"):
        await task
    assert registry.active_project_ids == ()
    assert watcher.active_project_ids() == ()
    watcher.start = original_start  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_mutate_shared_start_error(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()
    original_start = watcher.start
    watcher.fail_after_start.add("project-a")
    watcher.persistent_stop_failures.add("project-a")

    def blocking_start(project_root: Path, *, project_id: str) -> None:
        entered.set()
        assert release.wait(timeout=2.0)
        original_start(project_root, project_id=project_id)

    watcher.start = blocking_start  # type: ignore[method-assign]
    registry = ProjectRuntimeRegistry(watcher=watcher)
    root = _project(test_workspace, "a")
    cancelled_waiter = asyncio.create_task(registry.ensure_started("project-a", root))
    assert await asyncio.to_thread(entered.wait, 1.0)
    normal_waiter = asyncio.create_task(registry.ensure_started("project-a", root))
    await asyncio.sleep(0)
    cancelled_waiter.cancel()
    release.set()

    async def capture(task: asyncio.Task[ProjectMapAgent]) -> ProjectRuntimeShutdownError:
        with pytest.raises(ProjectRuntimeShutdownError) as raised:
            await task
        return raised.value

    cancelled_error, normal_error = await asyncio.gather(
        capture(cancelled_waiter),
        capture(normal_waiter),
    )
    assert cancelled_error is not normal_error
    assert cancelled_error.cancelled is True
    assert normal_error.cancelled is False
    watcher.persistent_stop_failures.clear()
    await registry.stop_all()


@pytest.mark.asyncio
async def test_cancelled_stop_waits_for_successful_registry_cleanup(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()
    original_stop = watcher.stop

    def blocking_stop(project_id: str, *, timeout_seconds: float = 5.0) -> bool:
        entered.set()
        assert release.wait(timeout=2.0)
        return original_stop(project_id, timeout_seconds=timeout_seconds)

    watcher.stop = blocking_stop  # type: ignore[method-assign]
    registry = ProjectRuntimeRegistry(watcher=watcher)
    await registry.ensure_started("project-a", _project(test_workspace, "a"))
    task = asyncio.create_task(registry.stop("project-a"))
    assert await asyncio.to_thread(entered.wait, 1.0)
    task.cancel()
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert registry.active_project_ids == ()
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_cancelled_stop_exposes_late_registry_cleanup_failure(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()

    def blocking_failed_stop(
        project_id: str, *, timeout_seconds: float = 5.0
    ) -> bool:
        del project_id, timeout_seconds
        entered.set()
        assert release.wait(timeout=2.0)
        raise RuntimeError("late_registry_stop_failure")

    watcher.stop = blocking_failed_stop  # type: ignore[method-assign]
    registry = ProjectRuntimeRegistry(watcher=watcher)
    await registry.ensure_started("project-a", _project(test_workspace, "a"))
    task = asyncio.create_task(registry.stop("project-a"))
    assert await asyncio.to_thread(entered.wait, 1.0)
    task.cancel()
    release.set()

    with pytest.raises(ProjectRuntimeShutdownError) as raised:
        await task
    assert raised.value.cancelled is True
    assert registry.active_project_ids == ("project-a",)
    assert watcher.active_project_ids() == ("project-a",)


@pytest.mark.asyncio
async def test_cancelled_stop_waits_for_pending_start_then_cleans_owner(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()
    original_start = watcher.start

    def blocking_start(project_root: Path, *, project_id: str) -> None:
        entered.set()
        assert release.wait(timeout=2.0)
        original_start(project_root, project_id=project_id)

    watcher.start = blocking_start  # type: ignore[method-assign]
    registry = ProjectRuntimeRegistry(watcher=watcher)
    root = _project(test_workspace, "a")
    ensure = asyncio.create_task(registry.ensure_started("project-a", root))
    assert await asyncio.to_thread(entered.wait, 1.0)
    stop = asyncio.create_task(registry.stop("project-a"))
    await asyncio.sleep(0)
    stop.cancel()
    release.set()

    await ensure
    with pytest.raises(asyncio.CancelledError):
        await stop
    assert registry.active_project_ids == ()
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_real_watcher_thread_start_failure_releases_registration(
    test_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = CodeMapRefreshWatcher()
    original_start = threading.Thread.start

    def fail_map_watcher_start(thread: threading.Thread) -> None:
        if thread.name.startswith("map-watcher-"):
            raise RuntimeError("map_watcher_thread_start_failed")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_map_watcher_start)
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)

    with pytest.raises(RuntimeError, match="map_watcher_thread_start_failed"):
        await agent.start()
    assert agent.state is ProjectMapAgentState.FAILED
    assert watcher.status("project-a") is None
    assert watcher.stop("project-a") is True

    monkeypatch.setattr(threading.Thread, "start", original_start)
    replacement = ProjectMapAgent("project-a", agent.canonical_path, watcher=watcher)
    await replacement.start()
    await replacement.stop()


@pytest.mark.asyncio
async def test_cancelled_start_waits_for_watcher_and_rolls_back(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()
    original_start = watcher.start

    def blocking_start(project_root: Path, *, project_id: str) -> None:
        entered.set()
        assert release.wait(timeout=2.0)
        original_start(project_root, project_id=project_id)

    watcher.start = blocking_start  # type: ignore[method-assign]
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)
    task = asyncio.create_task(agent.start())
    assert await asyncio.to_thread(entered.wait, 1.0)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.state is ProjectMapAgentState.FAILED
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_cancelled_start_exposes_rollback_failure(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()
    original_start = watcher.start

    def blocking_start(project_root: Path, *, project_id: str) -> None:
        entered.set()
        assert release.wait(timeout=2.0)
        original_start(project_root, project_id=project_id)

    watcher.start = blocking_start  # type: ignore[method-assign]
    watcher.persistent_stop_failures.add("project-a")
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)
    task = asyncio.create_task(agent.start())
    assert await asyncio.to_thread(entered.wait, 1.0)
    task.cancel()
    release.set()

    with pytest.raises(ProjectRuntimeShutdownError) as raised:
        await task
    assert raised.value.cancelled is True
    assert agent.state is ProjectMapAgentState.STOP_FAILED
    assert watcher.active_project_ids() == ("project-a",)
    watcher.persistent_stop_failures.clear()
    await agent.stop()


@pytest.mark.asyncio
async def test_cancelled_stop_finishes_cleanup_before_propagating(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)
    await agent.start()
    entered = threading.Event()
    release = threading.Event()
    original_stop = watcher.stop

    def blocking_stop(project_id: str, *, timeout_seconds: float = 5.0) -> bool:
        entered.set()
        assert release.wait(timeout=2.0)
        return original_stop(project_id, timeout_seconds=timeout_seconds)

    watcher.stop = blocking_stop  # type: ignore[method-assign]
    task = asyncio.create_task(agent.stop())
    assert await asyncio.to_thread(entered.wait, 1.0)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert agent.state is ProjectMapAgentState.STOPPED
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_cancelled_stop_exposes_cleanup_failure(test_workspace: Path) -> None:
    watcher = FakeWatcher()
    agent = ProjectMapAgent("project-a", _project(test_workspace, "a"), watcher=watcher)
    await agent.start()
    entered = threading.Event()
    release = threading.Event()

    def blocking_failed_stop(
        project_id: str, *, timeout_seconds: float = 5.0
    ) -> bool:
        del project_id, timeout_seconds
        entered.set()
        assert release.wait(timeout=2.0)
        raise RuntimeError("watcher_stop_failed_after_cancel")

    watcher.stop = blocking_failed_stop  # type: ignore[method-assign]
    task = asyncio.create_task(agent.stop())
    assert await asyncio.to_thread(entered.wait, 1.0)
    task.cancel()
    release.set()

    with pytest.raises(ProjectRuntimeShutdownError) as raised:
        await task
    assert raised.value.cancelled is True
    assert agent.state is ProjectMapAgentState.STOP_FAILED
    assert watcher.active_project_ids() == ("project-a",)


@pytest.mark.asyncio
async def test_cancelled_actor_task_still_cleans_watcher(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    actor_started = asyncio.Event()

    async def blocked_actor() -> None:
        actor_started.set()
        await asyncio.Event().wait()

    def blocked_task_factory(
        coroutine: Coroutine[Any, Any, None], *, name: str
    ) -> asyncio.Task[None]:
        coroutine.close()
        return asyncio.create_task(blocked_actor(), name=name)

    agent = ProjectMapAgent(
        "project-a",
        _project(test_workspace, "a"),
        watcher=watcher,
        task_factory=blocked_task_factory,
    )
    await agent.start()
    await actor_started.wait()
    stop_task = asyncio.create_task(agent.stop())

    async def wait_until_stopping() -> None:
        while agent.state is not ProjectMapAgentState.STOPPING:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_stopping(), timeout=1.0)
    assert agent.task is not None
    agent.task.cancel()

    with pytest.raises(ProjectRuntimeShutdownError):
        await stop_task
    assert agent.state is ProjectMapAgentState.STOP_FAILED
    assert watcher.stop_calls == ["project-a"]
    assert watcher.active_project_ids() == ()
    assert await agent.stop() is ProjectMapAgentState.STOPPED


@pytest.mark.asyncio
async def test_actor_task_cancelled_before_stop_reports_cleanup_error(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    actor_started = asyncio.Event()

    async def blocked_actor() -> None:
        actor_started.set()
        await asyncio.Event().wait()

    def blocked_task_factory(
        coroutine: Coroutine[Any, Any, None], *, name: str
    ) -> asyncio.Task[None]:
        coroutine.close()
        return asyncio.create_task(blocked_actor(), name=name)

    agent = ProjectMapAgent(
        "project-a",
        _project(test_workspace, "a"),
        watcher=watcher,
        task_factory=blocked_task_factory,
    )
    await agent.start()
    await actor_started.wait()
    assert agent.task is not None
    agent.task.cancel()
    await asyncio.gather(agent.task, return_exceptions=True)

    with pytest.raises(ProjectRuntimeShutdownError):
        await agent.stop()
    assert agent.state is ProjectMapAgentState.STOP_FAILED
    assert watcher.stop_calls == ["project-a"]
    assert watcher.active_project_ids() == ()
    assert await agent.stop() is ProjectMapAgentState.STOPPED


@pytest.mark.asyncio
async def test_actor_cancel_does_not_hide_watcher_cleanup_failure(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    actor_started = asyncio.Event()

    async def blocked_actor() -> None:
        actor_started.set()
        await asyncio.Event().wait()

    def blocked_task_factory(
        coroutine: Coroutine[Any, Any, None], *, name: str
    ) -> asyncio.Task[None]:
        coroutine.close()
        return asyncio.create_task(blocked_actor(), name=name)

    original_stop = watcher.stop

    def failed_stop(project_id: str, *, timeout_seconds: float = 5.0) -> bool:
        del project_id, timeout_seconds
        raise RuntimeError("watcher_cleanup_failed")

    agent = ProjectMapAgent(
        "project-a",
        _project(test_workspace, "a"),
        watcher=watcher,
        task_factory=blocked_task_factory,
    )
    await agent.start()
    await actor_started.wait()
    assert agent.task is not None
    agent.task.cancel()
    await asyncio.gather(agent.task, return_exceptions=True)
    watcher.stop = failed_stop  # type: ignore[method-assign]

    with pytest.raises(ProjectRuntimeShutdownError) as raised:
        await agent.stop()
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "watcher_cleanup_failed"
    assert agent.state is ProjectMapAgentState.STOP_FAILED

    watcher.stop = original_stop  # type: ignore[method-assign]
    await agent.stop()


@pytest.mark.asyncio
async def test_stop_all_retries_failures_after_cleaning_other_projects(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    watcher.stop_failures["project-a"] = 1
    registry = ProjectRuntimeRegistry(watcher=watcher)
    await registry.ensure_started("project-a", _project(test_workspace, "a"))
    await registry.ensure_started("project-b", _project(test_workspace, "b"))

    result = await registry.stop_all()

    assert result.failures == ()
    assert registry.active_count == 0
    assert watcher.stop_calls == ["project-a", "project-b", "project-a"]


@pytest.mark.asyncio
async def test_stop_all_rejects_new_project_until_cleanup_finishes(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()
    original_stop = watcher.stop

    def blocking_stop(project_id: str, *, timeout_seconds: float = 5.0) -> bool:
        if project_id == "project-a":
            entered.set()
            assert release.wait(timeout=2.0)
        return original_stop(project_id, timeout_seconds=timeout_seconds)

    watcher.stop = blocking_stop  # type: ignore[method-assign]
    registry = ProjectRuntimeRegistry(watcher=watcher)
    await registry.ensure_started("project-a", _project(test_workspace, "a"))
    stop_all = asyncio.create_task(registry.stop_all())
    assert await asyncio.to_thread(entered.wait, 1.0)

    with pytest.raises(ProjectRuntimeShutdownError) as raised:
        await registry.ensure_started("project-b", _project(test_workspace, "b"))
    assert raised.value.error_code == "project_runtime_registry_shutting_down"
    release.set()
    assert (await stop_all).failures == ()
    assert registry.active_project_ids == ()
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_cancelled_stop_all_keeps_barrier_until_cleanup_finishes(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    entered = threading.Event()
    release = threading.Event()
    original_stop = watcher.stop

    def blocking_stop(project_id: str, *, timeout_seconds: float = 5.0) -> bool:
        entered.set()
        assert release.wait(timeout=2.0)
        return original_stop(project_id, timeout_seconds=timeout_seconds)

    watcher.stop = blocking_stop  # type: ignore[method-assign]
    registry = ProjectRuntimeRegistry(watcher=watcher)
    await registry.ensure_started("project-a", _project(test_workspace, "a"))
    stop_all = asyncio.create_task(registry.stop_all())
    assert await asyncio.to_thread(entered.wait, 1.0)
    stop_all.cancel()
    stop_all.cancel()

    with pytest.raises(ProjectRuntimeShutdownError) as raised:
        await registry.ensure_started("project-b", _project(test_workspace, "b"))
    assert raised.value.error_code == "project_runtime_registry_shutting_down"
    assert not stop_all.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await stop_all
    assert registry.active_project_ids == ()
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_normal_stop_false_retains_registry_owner_for_retry(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    watcher.stop_false_failures["project-a"] = 1
    registry = ProjectRuntimeRegistry(watcher=watcher)
    agent = await registry.ensure_started("project-a", _project(test_workspace, "a"))

    with pytest.raises(ProjectRuntimeShutdownError):
        await registry.stop("project-a")
    assert registry.get("project-a") is agent
    assert agent.state is ProjectMapAgentState.STOP_FAILED
    assert watcher.active_project_ids() == ("project-a",)

    await registry.stop("project-a")
    assert registry.active_project_ids == ()
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_stop_all_retains_persistently_failed_owner(test_workspace: Path) -> None:
    watcher = FakeWatcher()
    watcher.persistent_stop_failures.add("project-a")
    registry = ProjectRuntimeRegistry(watcher=watcher)
    failed = await registry.ensure_started("project-a", _project(test_workspace, "a"))
    await registry.ensure_started("project-b", _project(test_workspace, "b"))

    result = await registry.stop_all()

    assert [failure.project_id for failure in result.failures] == ["project-a"]
    assert result.failures[0].error_code == "project_runtime_stop_failed"
    assert registry.active_project_ids == ("project-a",)
    assert registry.get("project-a") is failed
    watcher.persistent_stop_failures.clear()
    await registry.stop_all()


@pytest.mark.asyncio
async def test_app_lifespan_stops_all_project_runtimes(test_workspace: Path) -> None:
    watcher = FakeWatcher()
    watcher.stop_failures["project-a"] = 1
    registry = ProjectRuntimeRegistry(watcher=watcher)
    app = create_app(project_runtime_registry=registry)

    async with app.router.lifespan_context(app):
        await registry.ensure_started("project-a", _project(test_workspace, "a"))
        await registry.ensure_started("project-b", _project(test_workspace, "b"))

    assert registry.active_count == 0
    assert watcher.active_project_ids() == ()


@pytest.mark.asyncio
async def test_app_lifespan_exposes_persistent_shutdown_failure(
    test_workspace: Path,
) -> None:
    watcher = FakeWatcher()
    watcher.persistent_stop_failures.add("project-a")
    registry = ProjectRuntimeRegistry(watcher=watcher)
    app = create_app(project_runtime_registry=registry)

    with pytest.raises(ProjectRuntimeShutdownError) as raised:
        async with app.router.lifespan_context(app):
            await registry.ensure_started("project-a", _project(test_workspace, "a"))
            await registry.ensure_started("project-b", _project(test_workspace, "b"))

    assert raised.value.error_code == "project_runtime_shutdown_failed"
    assert registry.active_project_ids == ("project-a",)
    watcher.persistent_stop_failures.clear()
    await registry.stop_all()
