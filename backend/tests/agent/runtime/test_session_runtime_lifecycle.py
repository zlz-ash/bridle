from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bridle.agent.runtime.agent_runtime import RuntimeRole, RuntimeState
from bridle.agent.runtime.authorization import (
    AgentAuthorizationService,
    AgentIdentity,
    AgentRole,
)
from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.input_relay import RuntimeInputRelay
from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
from bridle.agent.runtime.parent_child_runtime import ParentChildRuntimeCoordinator
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.runtime.session_runtime_lifecycle import RuntimeSessionLifecycle
from bridle.database import configure_sqlite_engine
from bridle.features.sessions.service import ProjectSessionService
from bridle.logging.facade import LoggingFacade
from bridle.logging.schema import LogEvent
from bridle.models.agent_runtime import AgentRuntimeRecord, RuntimeInputDeliveryRecord
from bridle.models.project import ProjectRecord
from bridle.models.project_message import ProjectMessageRecord
from bridle.models.project_session import ProjectSessionRecord


class CapturingSink:
    def __init__(self) -> None:
        self.events: list[LogEvent] = []
        self.runtime_input_delivered = asyncio.Event()

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)
        if event.action == "runtime_input.delivered":
            self.runtime_input_delivered.set()


async def _database(root: Path):
    import bridle.models  # noqa: F401
    from bridle.models.base import Base

    engine = configure_sqlite_engine(
        create_async_engine(f"sqlite+aiosqlite:///{(root / 'application.db').as_posix()}")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        db.add(ProjectRecord(id="project-1", path=str(root), name="project"))
        db.add(
            ProjectSessionRecord(
                id="session-1",
                project_id="project-1",
                project_path_snapshot=str(root),
                title="history",
                role="planning",
                status="active",
            )
        )
        await db.commit()
    return engine, sessions


def _grant(project_id: str):
    return AgentAuthorizationService().resolve(
        identity=AgentIdentity(
            principal_id="principal",
            role=AgentRole.COORDINATOR,
            project_id=project_id,
            session_id="session-1",
        ),
        policy_version="v1",
        tool_grants=(),
        skill_grants=(),
    )


@pytest.mark.asyncio
async def test_mail_full_busy_stays_pending_and_lifespan_recovers_before_requests(
    tmp_path: Path,
) -> None:
    engine, sessions = await _database(tmp_path)
    target = AgentAddress("project-1", "parent", 1)
    mailbox = PersistentMailbox(
        tmp_path / ".bridle" / "mail.db",
        project_id="project-1",
        consumer_id="lifecycle",
        capacity=1,
        default_target=target,
    )
    mailbox.enqueue(
        MailEnvelope(
            message_id="holder",
            message_type="test",
            source=AgentAddress("project-1", "source", 1),
            target=target,
            payload={},
        )
    )
    async with sessions() as db:
        message = await ProjectSessionService.create_runtime_input(
            db, "session-1", content="pending", target=target
        )
    relay = RuntimeInputRelay(sessions, mailbox_for_project=lambda _id: mailbox)
    assert await relay.relay_pending() == 0
    async with sessions() as db:
        delivery = (
            await db.execute(
                select(RuntimeInputDeliveryRecord).where(
                    RuntimeInputDeliveryRecord.message_id == message.id
                )
            )
        ).scalar_one()
        assert delivery.status == "pending"

    claimed = mailbox.claim(target)
    assert claimed.lease_token is not None
    mailbox.ack("holder", claimed.lease_token, target=target)
    lifecycle = RuntimeSessionLifecycle(sessions, relay=relay)
    admitted = asyncio.Event()
    await lifecycle.recover_before_requests()
    admitted.set()
    assert admitted.is_set()
    async with sessions() as db:
        delivery = (
            await db.execute(
                select(RuntimeInputDeliveryRecord).where(
                    RuntimeInputDeliveryRecord.message_id == message.id
                )
            )
        ).scalar_one()
        assert delivery.status == "delivered"
    await mailbox.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_lifespan_retries_pending_input_after_startup_backpressure(
    tmp_path: Path,
) -> None:
    from bridle.app import create_app

    engine, sessions = await _database(tmp_path)
    target = AgentAddress("project-1", "parent", 1)
    sink = CapturingSink()
    facade = LoggingFacade(sinks=[sink])
    mailbox = PersistentMailbox(
        tmp_path / ".bridle" / "mail.db",
        project_id="project-1",
        consumer_id="lifespan-retry",
        capacity=1,
        default_target=target,
    )
    mailbox.enqueue(
        MailEnvelope(
            message_id="holder",
            message_type="test",
            source=AgentAddress("project-1", "source", 1),
            target=target,
            payload={},
        )
    )
    async with sessions() as db:
        message = await ProjectSessionService.create_runtime_input(
            db, "session-1", content="pending", target=target
        )
    relay = RuntimeInputRelay(
        sessions,
        mailbox_for_project=lambda _id: mailbox,
        facade=facade,
    )
    lifecycle = RuntimeSessionLifecycle(
        sessions,
        relay=relay,
        facade=facade,
        retry_interval_seconds=0.01,
    )
    app = create_app(test_workspace=str(tmp_path), runtime_lifecycle=lifecycle)
    async with app.router.lifespan_context(app):
        claimed = mailbox.claim(target)
        assert claimed.lease_token is not None
        mailbox.ack("holder", claimed.lease_token, target=target)
        await asyncio.wait_for(sink.runtime_input_delivered.wait(), timeout=1)
        async with sessions() as db:
            delivery = (
                await db.execute(
                    select(RuntimeInputDeliveryRecord).where(
                        RuntimeInputDeliveryRecord.message_id == message.id
                    )
                )
            ).scalar_one()
            assert delivery.status == "delivered"
    await mailbox.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_close_and_restart_preserve_history_and_interrupt_legacy_runtime(
    tmp_path: Path,
) -> None:
    engine, sessions = await _database(tmp_path)
    async with sessions() as db:
        db.add(ProjectMessageRecord(session_id="session-1", role="user", content="history"))
        db.add(
            AgentRuntimeRecord(
                id="runtime-legacy",
                runtime_type="parent",
                owner_type="session",
                owner_id="session-1",
                project_id="project-1",
                session_id="session-1",
                agent_id="parent",
                generation=1,
                status="RUNNING",
            )
        )
        await db.commit()
    lifecycle = RuntimeSessionLifecycle(sessions)
    assert await lifecycle.recover_before_requests() == 1
    await lifecycle.close_session("session-1")
    async with sessions() as db:
        runtime = await db.get(AgentRuntimeRecord, "runtime-legacy")
        session = await db.get(ProjectSessionRecord, "session-1")
        history = int(
            (
                await db.execute(select(func.count()).select_from(ProjectMessageRecord))
            ).scalar_one()
        )
    assert runtime is not None and runtime.status == "INTERRUPTED"
    assert session is not None and session.status == "closed"
    assert history == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_parent_revocation_cascades_children_without_deleting_history(
    tmp_path: Path,
) -> None:
    engine, sessions = await _database(tmp_path)
    sink = CapturingSink()
    facade = LoggingFacade(sinks=[sink])
    async with sessions() as db:
        db.add(ProjectMessageRecord(session_id="session-1", role="user", content="keep"))
        await db.commit()
    host = AgentRuntimeHost(sessions, facade=facade, trace_id="trace-revoke")
    wait = asyncio.Event()

    async def task(_handle):
        await wait.wait()

    parent = await host.create_runtime(
        role=RuntimeRole.PARENT,
        project_id="project-1",
        agent_id="parent",
        generation=1,
        grant=_grant("project-1"),
        session_id="session-1",
        task_factory=task,
    )
    child = await host.create_runtime(
        role=RuntimeRole.CHILD,
        project_id="project-1",
        agent_id="child",
        generation=1,
        grant=_grant("project-1"),
        parent=parent,
        task_factory=task,
    )
    lifecycle = RuntimeSessionLifecycle(
        sessions,
        host=host,
        facade=facade,
        trace_id="trace-revoke",
    )
    await lifecycle.revoke_parent(parent)
    assert parent.state is RuntimeState.DESTROYED
    assert child.state is RuntimeState.DESTROYED
    assert host.active_handles() == ()
    async with sessions() as db:
        assert int(
            (
                await db.execute(select(func.count()).select_from(ProjectMessageRecord))
            ).scalar_one()
        ) == 1
    revoked = next(event for event in sink.events if event.action == "runtime_parent.revoked")
    assert revoked.trace_id == "trace-revoke"
    assert revoked.project_id == "project-1"
    assert revoked.session_id == "session-1"
    assert revoked.agent_id == "parent"
    assert revoked.generation == 1
    assert revoked.detail == {"attempt": 1, "children_revoked": 1}
    await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_coordination_logs_have_correlation_fields(tmp_path: Path) -> None:
    engine, sessions = await _database(tmp_path)
    sink = CapturingSink()
    facade = LoggingFacade(sinks=[sink])
    target = AgentAddress("project-1", "parent", 1)
    mailbox = PersistentMailbox(
        tmp_path / ".bridle" / "mail.db",
        project_id="project-1",
        consumer_id="logging",
        default_target=target,
        facade=facade,
        trace_id="trace-coordination",
    )
    async with sessions() as db:
        message = await ProjectSessionService.create_runtime_input(
            db,
            "session-1",
            content="private input",
            target=target,
            facade=facade,
            trace_id="trace-coordination",
        )
    relay = RuntimeInputRelay(
        sessions,
        mailbox_for_project=lambda _id: mailbox,
        facade=facade,
        trace_id="trace-coordination",
    )
    assert await relay.relay_pending() == 1
    coordinator = ParentChildRuntimeCoordinator(
        sessions,
        mailbox_for_project=lambda _id: mailbox,
        facade=facade,
        trace_id="trace-coordination",
    )
    await coordinator.handle_input(message.id, lambda _content: asyncio.sleep(0, result="reply"))
    destroyed = 0

    async def destroy() -> None:
        nonlocal destroyed
        destroyed += 1

    assert await coordinator.deliver_child_result(
        message_id="child-result-log",
        source=AgentAddress("project-1", "child", 2),
        target=target,
        payload={"status": "completed"},
        destroy=destroy,
    )
    assert destroyed == 1
    lifecycle = RuntimeSessionLifecycle(sessions, facade=facade)
    await lifecycle.recover_before_requests()
    await lifecycle.close_session("session-1")

    events = {event.action: event for event in sink.events}
    persisted = events["runtime_input.persisted"]
    assert (
        persisted.trace_id,
        persisted.message_id,
        persisted.project_id,
        persisted.agent_id,
        persisted.generation,
        persisted.session_id,
        persisted.detail,
    ) == (
        "trace-coordination",
        message.id,
        "project-1",
        "parent",
        1,
        "session-1",
        {"attempt": 0},
    )
    assert events["app.runtime_recovery_started"].detail == {"attempt": 1}
    assert events["app.runtime_recovery_completed"].detail == {
        "interrupted": 0,
        "relayed": 0,
        "attempt": 1,
    }
    closed = events["runtime_session.closed"]
    assert closed.project_id == "project-1"
    assert closed.session_id == "session-1"
    assert closed.detail == {"attempt": 1}
    relayed = events["runtime_input.delivered"]
    assert (
        relayed.trace_id,
        relayed.message_id,
        relayed.project_id,
        relayed.agent_id,
        relayed.generation,
        relayed.detail["attempt"],
    ) == ("trace-coordination", message.id, "project-1", "parent", 1, 1)
    handled = events["runtime_parent.input_handled"]
    assert (
        handled.trace_id,
        handled.message_id,
        handled.project_id,
        handled.agent_id,
        handled.generation,
        handled.detail["attempt"],
    ) == ("trace-coordination", message.id, "project-1", "parent", 1, 1)
    child = events["runtime_child.result_delivered"]
    assert (
        child.trace_id,
        child.message_id,
        child.project_id,
        child.agent_id,
        child.generation,
        child.detail["attempt"],
    ) == ("trace-coordination", "child-result-log", "project-1", "child", 2, 1)
    assert "private input" not in str(sink.events)
    await mailbox.close()
    await engine.dispose()
