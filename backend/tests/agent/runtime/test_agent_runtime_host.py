from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bridle.database import configure_sqlite_engine
from bridle.logging.facade import LoggingFacade
from bridle.logging.schema import LogEvent


class CapturingSink:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)


class FailingSink:
    def emit(self, event: LogEvent) -> None:
        raise RuntimeError("hidden prompt and absolute path D:/secret")


class TrackingSession(AsyncSession):
    identities: list[int] = []
    fail_next_commit = False
    commit_count = 0
    fail_commit_numbers: set[int] = set()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.identities.append(id(self))

    async def commit(self) -> None:
        type(self).commit_count += 1
        if type(self).commit_count in type(self).fail_commit_numbers:
            raise RuntimeError("commit_failed")
        if type(self).fail_next_commit:
            type(self).fail_next_commit = False
            raise RuntimeError("commit_failed")
        await super().commit()


async def _database(
    root: Path,
    *,
    session_class: type[AsyncSession] = AsyncSession,
):
    import bridle.models  # noqa: F401
    from bridle.models.base import Base

    path = root / "application.db"
    engine = configure_sqlite_engine(
        create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", echo=False)
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(
        engine,
        class_=session_class,
        expire_on_commit=False,
    )


def _grant(project_id: str, *, tools: tuple[str, ...] = (), skills: tuple[str, ...] = ()):
    from bridle.agent.runtime.authorization import (
        AgentAuthorizationService,
        AgentIdentity,
        AgentRole,
        SkillGrant,
        ToolGrant,
    )

    return AgentAuthorizationService().resolve(
        identity=AgentIdentity(
            principal_id="principal",
            role=AgentRole.COORDINATOR,
            project_id=project_id,
            session_id="session-1",
        ),
        policy_version="v1",
        tool_grants=tuple(ToolGrant(tool_id) for tool_id in tools),
        skill_grants=tuple(SkillGrant(skill_id) for skill_id in skills),
    )


def _claimed_mailbox(root: Path, *, project_id: str, agent_id: str, consumer_id: str):
    from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
    from bridle.agent.runtime.persistent_mailbox import PersistentMailbox

    target = AgentAddress(project_id, agent_id, 1)
    mailbox = PersistentMailbox(
        root / ".bridle" / f"{consumer_id}.db",
        project_id=project_id,
        consumer_id=consumer_id,
        default_target=target,
    )
    mailbox.enqueue(
        MailEnvelope(
            message_id=f"message-{consumer_id}",
            source=AgentAddress(project_id, "sender", 1),
            target=target,
            message_type="runtime-input",
            payload={"value": 1},
        )
    )
    assert mailbox.claim(target).status == "claimed"
    return mailbox, target


@pytest.mark.asyncio
async def test_all_runtime_roles_share_identity_and_persist_each_transition(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.mailbox import AgentAddress
    from bridle.agent.runtime.persistence import get_runtime_record

    engine, sessions = await _database(test_workspace)
    host = AgentRuntimeHost(sessions)
    parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-1",
        session_id="session-1",
        agent_id="parent",
        generation=1,
        grant=_grant("project-1"),
    )
    child = await host.create_runtime(
        role=RuntimeRole.CHILD,
        project_id="project-1",
        session_id="session-1",
        agent_id="child",
        generation=1,
        parent=parent,
        grant=_grant("project-1"),
    )
    map_runtime = await host.create_runtime(
        role=RuntimeRole.MAP,
        project_id="project-1",
        agent_id="map",
        generation=1,
        grant=_grant("project-1"),
    )

    assert {type(item.spec.address) for item in (parent, child, map_runtime)} == {AgentAddress}
    assert child.spec.parent_runtime_id == parent.spec.runtime_id
    assert AgentAddress.parse(parent.spec.address.to_uri()) == parent.spec.address

    async def persisted_snapshot(handle) -> tuple[object, ...]:
        async with sessions() as session:
            record = await get_runtime_record(session, handle.spec.runtime_id)
            return (
                record.status,
                record.status_reason,
                record.generation,
                record.parent_runtime_id,
                record.updated_at,
            )

    handles = (parent, child, map_runtime)
    for handle in handles:
        ready = await persisted_snapshot(handle)
        assert ready[:4] == (
            RuntimeState.READY,
            "created",
            handle.spec.generation,
            handle.spec.parent_runtime_id,
        )
        assert ready[4] is not None
        await host.transition(handle, RuntimeState.RUNNING, reason="work_started")
        running = await persisted_snapshot(handle)
        assert running[:4] == (
            RuntimeState.RUNNING,
            "work_started",
            handle.spec.generation,
            handle.spec.parent_runtime_id,
        )
        assert running[4] is not None

    before_invalid = await persisted_snapshot(parent)
    with pytest.raises(Exception, match="runtime_invalid_transition"):
        await host.transition(parent, RuntimeState.CREATING, reason="invalid")
    assert await persisted_snapshot(parent) == before_invalid

    await engine.dispose()
    reopened_engine, reopened_sessions = await _database(test_workspace)
    async with reopened_sessions() as session:
        for handle in handles:
            record = await get_runtime_record(session, handle.spec.runtime_id)
            assert record.status == RuntimeState.RUNNING
            assert record.status_reason == "work_started"
            assert record.generation == handle.spec.generation
            assert record.parent_runtime_id == handle.spec.parent_runtime_id
            assert record.updated_at is not None
    await reopened_engine.dispose()


@pytest.mark.asyncio
async def test_each_transition_uses_new_session_and_commit_failure_is_retryable(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_runtime_record

    TrackingSession.identities = []
    TrackingSession.fail_next_commit = False
    TrackingSession.commit_count = 0
    TrackingSession.fail_commit_numbers = set()
    engine, sessions = await _database(test_workspace, session_class=TrackingSession)
    captured = CapturingSink()
    host = AgentRuntimeHost(sessions, facade=LoggingFacade(sinks=[captured]))
    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-transaction",
        session_id="session-1",
        agent_id="parent",
        generation=1,
        grant=_grant("project-transaction"),
    )
    before_state = handle.state
    TrackingSession.fail_next_commit = True
    with pytest.raises(RuntimeError, match="commit_failed"):
        await host.transition(handle, RuntimeState.RUNNING, reason="first")
    assert handle.state == before_state

    await host.transition(handle, RuntimeState.RUNNING, reason="retry")
    assert len(TrackingSession.identities) == len(set(TrackingSession.identities))
    assert len(TrackingSession.identities) >= 4
    async with sessions() as session:
        record = await get_runtime_record(session, handle.spec.runtime_id)
        assert record.status == RuntimeState.RUNNING
        assert record.status_reason == "retry"

    mailbox, target = _claimed_mailbox(
        test_workspace,
        project_id="project-create-failure",
        agent_id="map",
        consumer_id="failed-owner",
    )
    task_finished = asyncio.Event()

    async def runtime_task(_handle) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            task_finished.set()

    TrackingSession.fail_commit_numbers = {TrackingSession.commit_count + 2}
    with pytest.raises(RuntimeError, match="commit_failed"):
        await host.create_runtime(
            role=RuntimeRole.MAP,
            project_id="project-create-failure",
            agent_id="map",
            generation=1,
            grant=_grant("project-create-failure"),
            task_factory=runtime_task,
            mailbox=mailbox,
        )
    await asyncio.wait_for(task_finished.wait(), timeout=1)
    assert all(item.spec.project_id != "project-create-failure" for item in host.active_handles())
    from bridle.agent.runtime.persistent_mailbox import PersistentMailbox

    replacement = PersistentMailbox(
        mailbox.database_path,
        project_id="project-create-failure",
        consumer_id="replacement-owner",
        default_target=target,
    )
    assert replacement.claim(target).status == "claimed"
    await replacement.close()

    cancelled_mailbox, cancelled_target = _claimed_mailbox(
        test_workspace,
        project_id="project-create-cancelled",
        agent_id="map",
        consumer_id="cancelled-owner",
    )
    runtime_task_finished = asyncio.Event()
    ready_transition_entered = asyncio.Event()
    cancelled_runtime_ids: list[str] = []

    async def cancelled_runtime_task(_handle) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            runtime_task_finished.set()

    original_transition = host.transition

    async def blocking_ready_transition(handle, target_state, *, reason: str):
        if (
            handle.spec.project_id == "project-create-cancelled"
            and target_state is RuntimeState.READY
        ):
            cancelled_runtime_ids.append(handle.spec.runtime_id)
            ready_transition_entered.set()
            await asyncio.Event().wait()
        return await original_transition(handle, target_state, reason=reason)

    host.transition = blocking_ready_transition  # type: ignore[method-assign]
    cancelled_creation = asyncio.create_task(
        host.create_runtime(
            role=RuntimeRole.MAP,
            project_id="project-create-cancelled",
            agent_id="cancelled-map",
            generation=1,
            grant=_grant("project-create-cancelled"),
            task_factory=cancelled_runtime_task,
            mailbox=cancelled_mailbox,
        )
    )
    await asyncio.wait_for(ready_transition_entered.wait(), timeout=1)
    cancelled_creation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_creation
    await asyncio.wait_for(runtime_task_finished.wait(), timeout=1)
    cancelled_records = [
        event.to_dict().get("detail", {}).get("to_state")
        for event in captured.events
        if event.to_dict().get("project_id") == "project-create-cancelled"
        and event.action == "runtime.state_changed"
    ]
    no_cancelled_handle = all(
        item.spec.project_id != "project-create-cancelled" for item in host.active_handles()
    )
    async with sessions() as session:
        record = await get_runtime_record(session, cancelled_runtime_ids[0])
        cancelled_status = record.status
    from bridle.agent.runtime.persistent_mailbox import PersistentMailbox

    cancelled_replacement = PersistentMailbox(
        cancelled_mailbox.database_path,
        project_id="project-create-cancelled",
        consumer_id="cancelled-replacement-owner",
        default_target=cancelled_target,
    )
    assert cancelled_replacement.claim(cancelled_target).status == "claimed"
    await cancelled_replacement.close()
    await engine.dispose()
    assert cancelled_records == [RuntimeState.CANCELLED, RuntimeState.DESTROYED]
    assert no_cancelled_handle
    assert cancelled_status == RuntimeState.DESTROYED


@pytest.mark.asyncio
async def test_parent_and_map_are_singleflight_while_children_get_distinct_generations(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_active_map_runtime

    engine, sessions = await _database(test_workspace)
    host = AgentRuntimeHost(sessions)
    parent_factory_calls = 0
    parent_factory_entered = asyncio.Event()
    parent_factory_release = asyncio.Event()

    async def parent_factory(_handle) -> None:
        nonlocal parent_factory_calls
        parent_factory_calls += 1
        parent_factory_entered.set()
        await parent_factory_release.wait()

    parent_args = dict(
        role=RuntimeRole.PARENT,
        project_id="project-flight",
        session_id="session-1",
        agent_id="parent",
        generation=1,
        grant=_grant("project-flight"),
        task_factory=parent_factory,
    )
    parent_a, parent_b = await asyncio.gather(
        host.create_runtime(**parent_args),
        host.create_runtime(**parent_args),
    )
    await asyncio.wait_for(parent_factory_entered.wait(), timeout=1)
    assert parent_a is parent_b
    assert parent_factory_calls == 1
    with pytest.raises(Exception, match="runtime_conflict"):
        await host.create_runtime(
            role=RuntimeRole.PARENT,
            project_id="project-flight",
            session_id="session-1",
            agent_id="other-parent",
            generation=2,
            grant=_grant("project-flight"),
        )

    map_factory_calls = 0
    map_factory_entered = asyncio.Event()
    map_factory_release = asyncio.Event()

    async def map_factory(_handle) -> None:
        nonlocal map_factory_calls
        map_factory_calls += 1
        map_factory_entered.set()
        await map_factory_release.wait()

    map_args = dict(
        role=RuntimeRole.MAP,
        project_id="project-flight",
        agent_id="map",
        generation=1,
        grant=_grant("project-flight"),
        task_factory=map_factory,
    )
    map_a, map_b = await asyncio.gather(host.create_runtime(**map_args), host.create_runtime(**map_args))
    await asyncio.wait_for(map_factory_entered.wait(), timeout=1)
    assert map_a is map_b
    assert map_factory_calls == 1

    child_a, child_b = await asyncio.gather(
        host.create_runtime(
            role=RuntimeRole.CHILD,
            project_id="project-flight",
            session_id="session-1",
            agent_id="child-a",
            generation=1,
            parent=parent_a,
            grant=_grant("project-flight"),
        ),
        host.create_runtime(
            role=RuntimeRole.CHILD,
            project_id="project-flight",
            session_id="session-1",
            agent_id="child-b",
            generation=2,
            parent=parent_a,
            grant=_grant("project-flight"),
        ),
    )
    assert child_a is not child_b
    assert child_a.spec.parent_runtime_id == child_b.spec.parent_runtime_id == parent_a.spec.runtime_id
    assert {child_a.spec.generation, child_b.spec.generation} == {1, 2}

    factory_failures = 0

    def failing_factory(_handle):
        nonlocal factory_failures
        factory_failures += 1
        raise RuntimeError("factory_failed")

    with pytest.raises(RuntimeError, match="factory_failed"):
        await host.create_runtime(
            role=RuntimeRole.MAP,
            project_id="project-factory-retry",
            agent_id="map-retry",
            generation=1,
            grant=_grant("project-factory-retry"),
            task_factory=failing_factory,  # type: ignore[arg-type]
        )
    assert factory_failures == 1
    assert all(item.spec.project_id != "project-factory-retry" for item in host.active_handles())
    async with sessions() as session:
        assert await get_active_map_runtime(
            session,
            project_id="project-factory-retry",
        ) is None
    retried_map = await host.create_runtime(
        role=RuntimeRole.MAP,
        project_id="project-factory-retry",
        agent_id="map-retry",
        generation=2,
        grant=_grant("project-factory-retry"),
    )
    assert retried_map in host.active_handles()

    parent_factory_release.set()
    map_factory_release.set()
    await host.destroy(retried_map)
    await host.destroy(map_a)
    await host.destroy(parent_a)
    await engine.dispose()


@pytest.mark.asyncio
async def test_stop_destroy_are_lifo_idempotent_and_cascade_children(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import list_active_child_runtimes

    engine, sessions = await _database(test_workspace)
    host = AgentRuntimeHost(sessions)
    mailbox, target = _claimed_mailbox(
        test_workspace,
        project_id="project-cleanup",
        agent_id="parent",
        consumer_id="cleanup-owner",
    )
    task_finished = asyncio.Event()

    async def runtime_task(_handle) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            task_finished.set()

    parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-cleanup",
        session_id="session-1",
        agent_id="parent",
        generation=1,
        grant=_grant("project-cleanup"),
        task_factory=runtime_task,
        mailbox=mailbox,
    )
    child = await host.create_runtime(
        role=RuntimeRole.CHILD,
        project_id="project-cleanup",
        session_id="session-1",
        agent_id="child",
        generation=1,
        parent=parent,
        grant=_grant("project-cleanup"),
    )
    order: list[str] = []

    async def close(name: str, *, fail: bool = False) -> None:
        order.append(name)
        if fail:
            raise RuntimeError("close_failed")

    parent.add_resource(lambda: close("first"))
    parent.add_resource(lambda: close("second", fail=True))
    parent.add_resource(lambda: close("third"))
    await asyncio.wait_for(
        asyncio.gather(host.stop(parent), host.stop(parent)),
        timeout=1,
    )
    await asyncio.wait_for(task_finished.wait(), timeout=1)
    assert order == ["third", "second", "first"]
    assert parent.task is not None and parent.task.cancelled()
    assert mailbox.claim(target).status == "closed"
    assert parent.state == child.state == RuntimeState.COMPLETED
    await asyncio.wait_for(
        asyncio.gather(host.destroy(parent), host.destroy(parent)),
        timeout=1,
    )
    assert parent.state == child.state == RuntimeState.DESTROYED
    assert order == ["third", "second", "first"]

    class BarrierLock:
        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self._block_first = True

        async def __aenter__(self):
            await self._lock.acquire()
            if self._block_first:
                self._block_first = False
                self.entered.set()
                await self.release.wait()
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            self._lock.release()

    for lifecycle_action in ("stop", "destroy"):
        race_parent = await host.create_runtime(
            role=RuntimeRole.PARENT,
            project_id="project-cleanup",
            session_id=f"session-race-{lifecycle_action}",
            agent_id=f"parent-{lifecycle_action}",
            generation=1,
            grant=_grant("project-cleanup"),
        )

        lifecycle_lock = BarrierLock()
        host._create_lock = lifecycle_lock
        lifecycle_task = asyncio.create_task(
            getattr(host, lifecycle_action)(race_parent)
        )
        await asyncio.wait_for(lifecycle_lock.entered.wait(), timeout=1)
        late_child_id = f"late-child-{lifecycle_action}"
        late_child_task = asyncio.create_task(
            host.create_runtime(
                role=RuntimeRole.CHILD,
                project_id="project-cleanup",
                session_id=f"session-race-{lifecycle_action}",
                agent_id=late_child_id,
                generation=1,
                parent=race_parent,
                grant=_grant("project-cleanup"),
            )
        )
        assert not late_child_task.done()
        lifecycle_lock.release.set()
        await asyncio.wait_for(lifecycle_task, timeout=1)
        with pytest.raises(Exception, match="runtime_parent_inactive"):
            await late_child_task
        assert all(item.spec.agent_id != late_child_id for item in host.active_handles())
        async with sessions() as session:
            assert await list_active_child_runtimes(
                session,
                parent_runtime_id=race_parent.spec.runtime_id,
            ) == ()
        if lifecycle_action == "stop":
            await asyncio.wait_for(host.destroy(race_parent), timeout=1)

    host._create_lock = asyncio.Lock()
    reentry_started = asyncio.Event()
    reentry_completed = asyncio.Event()

    async def reentrant_runtime_task(runtime_handle) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            reentry_started.set()
            await host.destroy(runtime_handle)
            reentry_completed.set()

    reentrant_parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-cleanup",
        session_id="session-reentrant",
        agent_id="parent-reentrant",
        generation=1,
        grant=_grant("project-cleanup"),
        task_factory=reentrant_runtime_task,
    )
    outer_destroy = asyncio.create_task(host.destroy(reentrant_parent))
    try:
        await asyncio.wait_for(reentry_started.wait(), timeout=1)
        await asyncio.wait_for(reentry_completed.wait(), timeout=1)
        await asyncio.wait_for(outer_destroy, timeout=1)
    finally:
        if not outer_destroy.done():
            outer_destroy.cancel()
        await asyncio.gather(outer_destroy, return_exceptions=True)
        await engine.dispose()
    assert reentrant_parent.state == RuntimeState.DESTROYED


@pytest.mark.asyncio
async def test_runtime_task_self_destroy_finishes_destroying_handle(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_runtime_record

    engine, sessions = await _database(test_workspace)
    host = AgentRuntimeHost(sessions)
    destroy_returned = asyncio.Event()

    async def self_destroying_task(runtime_handle) -> None:
        await host.destroy(runtime_handle)
        destroy_returned.set()

    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-self-destroy",
        session_id="session-self-destroy",
        agent_id="parent-self-destroy",
        generation=1,
        grant=_grant("project-self-destroy"),
        task_factory=self_destroying_task,
    )
    try:
        await asyncio.wait_for(destroy_returned.wait(), timeout=1)
        assert handle._destroy_task is not None
        await asyncio.wait_for(asyncio.shield(handle._destroy_task), timeout=1)
        assert handle.state is RuntimeState.DESTROYED
        assert handle not in host.active_handles()
        async with sessions() as session:
            record = await get_runtime_record(session, handle.spec.runtime_id)
            assert record.status == RuntimeState.DESTROYED
    finally:
        await asyncio.wait_for(host.destroy(handle), timeout=1)
        await engine.dispose()


@pytest.mark.asyncio
async def test_destroy_caller_cancellation_does_not_leave_active_handle(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_runtime_record

    class BlockSecondAcquire:
        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self._acquisitions = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def __aenter__(self):
            await self._lock.acquire()
            self._acquisitions += 1
            if self._acquisitions == 2:
                self.entered.set()
                try:
                    await self.release.wait()
                except BaseException:
                    self._lock.release()
                    raise
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
            self._lock.release()

    engine, sessions = await _database(test_workspace)
    host = AgentRuntimeHost(sessions)
    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-cancel-destroy",
        session_id="session-cancel-destroy",
        agent_id="parent-cancel-destroy",
        generation=1,
        grant=_grant("project-cancel-destroy"),
    )
    await host.stop(handle)
    barrier = BlockSecondAcquire()
    host._create_lock = barrier
    destroy_caller = asyncio.create_task(host.destroy(handle))
    try:
        await asyncio.wait_for(barrier.entered.wait(), timeout=1)
        assert handle.state is RuntimeState.DESTROYED
        async with sessions() as session:
            record = await get_runtime_record(session, handle.spec.runtime_id)
            assert record.status == RuntimeState.DESTROYED
        destroy_caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await destroy_caller
        barrier.release.set()
        if handle._destroy_task is not None:
            await asyncio.wait_for(asyncio.shield(handle._destroy_task), timeout=1)
        await asyncio.wait_for(host.destroy(handle), timeout=1)
        assert handle.state is RuntimeState.DESTROYED
        assert handle not in host.active_handles()
        async with sessions() as session:
            record = await get_runtime_record(session, handle.spec.runtime_id)
            assert record.status == RuntimeState.DESTROYED
    finally:
        barrier.release.set()
        host._create_lock = asyncio.Lock()
        if not destroy_caller.done():
            destroy_caller.cancel()
        await asyncio.gather(destroy_caller, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_stop_finalizer_commit_failure_is_retryable(test_workspace: Path) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_runtime_record

    TrackingSession.identities = []
    TrackingSession.fail_next_commit = False
    TrackingSession.commit_count = 0
    TrackingSession.fail_commit_numbers = set()
    engine, sessions = await _database(test_workspace, session_class=TrackingSession)
    host = AgentRuntimeHost(sessions)
    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-stop-retry",
        session_id="session-stop-retry",
        agent_id="parent-stop-retry",
        generation=1,
        grant=_grant("project-stop-retry"),
    )

    TrackingSession.fail_commit_numbers = {TrackingSession.commit_count + 2}
    with pytest.raises(RuntimeError, match="commit_failed"):
        await host.stop(handle)
    assert handle.state is RuntimeState.STOPPING
    async with sessions() as session:
        record = await get_runtime_record(session, handle.spec.runtime_id)
        assert record.status == RuntimeState.STOPPING

    TrackingSession.fail_commit_numbers = set()
    retry_error: RuntimeError | None = None
    try:
        await host.stop(handle)
    except RuntimeError as exc:
        retry_error = exc
    final_state = handle.state
    async with sessions() as session:
        record = await get_runtime_record(session, handle.spec.runtime_id)
        final_status = record.status
    await engine.dispose()
    assert retry_error is None
    assert final_state is RuntimeState.COMPLETED
    assert final_status == RuntimeState.COMPLETED


@pytest.mark.asyncio
async def test_destroy_finalizer_commit_failure_is_retryable_and_removes_handle(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_runtime_record

    TrackingSession.identities = []
    TrackingSession.fail_next_commit = False
    TrackingSession.commit_count = 0
    TrackingSession.fail_commit_numbers = set()
    engine, sessions = await _database(test_workspace, session_class=TrackingSession)
    host = AgentRuntimeHost(sessions)
    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-destroy-retry",
        session_id="session-destroy-retry",
        agent_id="parent-destroy-retry",
        generation=1,
        grant=_grant("project-destroy-retry"),
    )
    await host.stop(handle)

    TrackingSession.fail_next_commit = True
    with pytest.raises(RuntimeError, match="commit_failed"):
        await host.destroy(handle)
    assert handle.state is RuntimeState.COMPLETED
    assert handle in host.active_handles()
    async with sessions() as session:
        record = await get_runtime_record(session, handle.spec.runtime_id)
        assert record.status == RuntimeState.COMPLETED

    retry_error: RuntimeError | None = None
    try:
        await host.destroy(handle)
    except RuntimeError as exc:
        retry_error = exc
    final_state = handle.state
    still_active = handle in host.active_handles()
    async with sessions() as session:
        record = await get_runtime_record(session, handle.spec.runtime_id)
        final_status = record.status
    await engine.dispose()
    assert retry_error is None
    assert final_state is RuntimeState.DESTROYED
    assert not still_active
    assert final_status == RuntimeState.DESTROYED


@pytest.mark.parametrize(
    ("operation", "expected_state"),
    [
        ("stop", "COMPLETED"),
        ("destroy", "DESTROYED"),
    ],
)
async def test_cancelled_stop_finalizer_does_not_abandon_async_resource_cleanup(
    test_workspace: Path,
    operation: str,
    expected_state: str,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_runtime_record

    engine, sessions = await _database(test_workspace)
    host = AgentRuntimeHost(sessions)
    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id=f"project-resource-retry-{operation}",
        session_id=f"session-resource-retry-{operation}",
        agent_id=f"parent-resource-retry-{operation}",
        generation=1,
        grant=_grant(f"project-resource-retry-{operation}"),
    )
    closer_started = asyncio.Event()
    release_closer = asyncio.Event()
    closer_completed = 0

    async def blocked_closer() -> None:
        nonlocal closer_completed
        closer_started.set()
        await release_closer.wait()
        closer_completed += 1

    handle.add_resource(blocked_closer)
    finalizer_call = asyncio.create_task(getattr(host, operation)(handle))
    try:
        await asyncio.wait_for(closer_started.wait(), timeout=1)
        assert handle._stop_task is not None
        handle._stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await finalizer_call

        release_closer.set()
        await asyncio.wait_for(getattr(host, operation)(handle), timeout=1)
        assert closer_completed == 1
        assert handle.state is RuntimeState(expected_state)
        async with sessions() as session:
            record = await get_runtime_record(session, handle.spec.runtime_id)
            assert record.status == RuntimeState(expected_state)
        if operation == "destroy":
            assert handle not in host.active_handles()
    finally:
        release_closer.set()
        await asyncio.wait_for(host.destroy(handle), timeout=1)
        await engine.dispose()


async def test_self_destroy_background_failure_is_logged_and_retryable(
    test_workspace: Path,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost
    from bridle.agent.runtime.persistence import get_runtime_record

    class RecordingFacade:
        def __init__(self) -> None:
            self.events: list[tuple[str, str, dict[str, Any]]] = []

        def info_event(self, action: str, status: str, **fields: Any) -> None:
            self.events.append((action, status, fields))

    class FailDestroyOnceHost(AgentRuntimeHost):
        fail_destroy_transition = True

        async def transition(self, handle, new_state, *, reason: str):
            if new_state is RuntimeState.DESTROYED and self.fail_destroy_transition:
                self.fail_destroy_transition = False
                raise RuntimeError("destroy_transition_failed")
            return await super().transition(handle, new_state, reason=reason)

    engine, sessions = await _database(test_workspace)
    facade = RecordingFacade()
    host = FailDestroyOnceHost(sessions, facade=facade)  # type: ignore[arg-type]
    start_destroy = asyncio.Event()

    async def self_destroying_task(runtime_handle) -> None:
        await start_destroy.wait()
        await host.destroy(runtime_handle)

    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-self-destroy-log",
        session_id="session-self-destroy-log",
        agent_id="parent-self-destroy-log",
        generation=1,
        grant=_grant("project-self-destroy-log"),
        task_factory=self_destroying_task,
    )
    try:
        start_destroy.set()

        async def destroy_finished() -> None:
            while handle._destroy_task is None or not handle._destroy_task.done():
                await asyncio.sleep(0)

        await asyncio.wait_for(destroy_finished(), timeout=1)
        await asyncio.sleep(0)
        failure_events = [
            fields
            for action, status, fields in facade.events
            if action == "runtime.finalizer_failed" and status == "failed"
        ]
        assert len(failure_events) == 1
        assert failure_events[0]["agent_id"] == "parent-self-destroy-log"
        assert failure_events[0]["error_code"] == "RuntimeError"

        await asyncio.wait_for(host.destroy(handle), timeout=1)
        assert handle.state is RuntimeState.DESTROYED
        assert handle not in host.active_handles()
        async with sessions() as session:
            record = await get_runtime_record(session, handle.spec.runtime_id)
            assert record.status == RuntimeState.DESTROYED
    finally:
        await asyncio.wait_for(host.destroy(handle), timeout=1)
        await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_and_capability_events_are_correlated_and_sink_safe(
    test_workspace: Path,
    caplog,
) -> None:
    from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
    from bridle.agent.runtime.host import AgentRuntimeHost

    captured = CapturingSink()
    facade = LoggingFacade(sinks=[FailingSink(), captured])
    engine, sessions = await _database(test_workspace)
    host = AgentRuntimeHost(sessions, facade=facade, trace_id="trace-runtime")
    handle = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-log",
        session_id="session-1",
        agent_id="parent",
        generation=1,
        grant=_grant("project-log", tools=("visible",)),
        tools={"visible": lambda arguments: arguments, "hidden": lambda arguments: arguments},
    )
    await host.transition(handle, RuntimeState.RUNNING, reason="run")
    assert handle.capabilities.execute_tool("missing", {}) == {
        "status": "failed",
        "error_code": "unknown_capability",
    }
    await host.revoke(handle)

    actions = {event.action for event in captured.events}
    assert {
        "runtime.created",
        "runtime.state_changed",
        "runtime.capability_view_created",
        "runtime.unknown_capability",
        "runtime.revoked",
        "runtime.destroyed",
    } <= actions
    for event in captured.events:
        payload = event.to_dict()
        assert payload["trace_id"] == "trace-runtime"
        assert payload["project_id"] == "project-log"
        assert payload["agent_id"] == "parent"
        assert payload["generation"] == 1
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "hidden" not in serialized
        assert "D:/secret" not in serialized
    await engine.dispose()
