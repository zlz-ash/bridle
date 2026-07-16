from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bridle.agent.runtime.input_relay import RuntimeInputRelay
from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
from bridle.agent.runtime.parent_child_runtime import ParentChildRuntimeCoordinator
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.database import configure_sqlite_engine
from bridle.features.project_map.store import ProjectPlanStore
from bridle.features.sessions.service import ProjectSessionService
from bridle.models.agent_runtime import RuntimeInputDeliveryRecord, RuntimeInputResultRecord
from bridle.models.project import ProjectRecord
from bridle.models.project_message import ProjectMessageRecord
from bridle.models.project_session import ProjectSessionRecord


class CommitFailingSession(AsyncSession):
    fail_next_commit = False

    async def commit(self) -> None:
        if type(self).fail_next_commit:
            type(self).fail_next_commit = False
            raise RuntimeError("commit_failed")
        await super().commit()


@pytest.mark.asyncio
async def test_existing_delivery_schema_adds_result_table_without_alter(tmp_path: Path) -> None:
    import bridle.models  # noqa: F401
    from bridle.models.base import Base

    engine = configure_sqlite_engine(
        create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'upgrade.db').as_posix()}")
    )
    async with engine.begin() as connection:
        await connection.run_sync(RuntimeInputDeliveryRecord.__table__.create)
        await connection.run_sync(Base.metadata.create_all)

    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as db:
        db.add(
            RuntimeInputResultRecord(
                message_id="existing-input",
                assistant_message_id="assistant-1",
                status="handled",
                handled_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        await db.commit()
        result = (
            await db.execute(
                select(RuntimeInputResultRecord).where(
                    RuntimeInputResultRecord.message_id == "existing-input"
                )
            )
        ).scalar_one()
    assert result.assistant_message_id == "assistant-1"
    await engine.dispose()


async def _database(
    root: Path,
    *,
    session_class: type[AsyncSession] = AsyncSession,
) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    import bridle.models  # noqa: F401
    from bridle.models.base import Base

    engine = configure_sqlite_engine(
        create_async_engine(f"sqlite+aiosqlite:///{(root / 'application.db').as_posix()}")
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=session_class, expire_on_commit=False)
    async with sessions() as db:
        db.add(ProjectRecord(id="project-1", path=str(root), name="project"))
        db.add(
            ProjectSessionRecord(
                id="session-1",
                project_id="project-1",
                project_path_snapshot=str(root),
                title="runtime",
                role="planning",
                status="active",
            )
        )
        await db.commit()
    return engine, sessions


async def _counts(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with sessions() as db:
        messages = int(
            (await db.execute(select(func.count()).select_from(ProjectMessageRecord))).scalar_one()
        )
        deliveries = int(
            (await db.execute(select(func.count()).select_from(RuntimeInputDeliveryRecord))).scalar_one()
        )
    return messages, deliveries


@pytest.mark.asyncio
async def test_user_message_and_pending_delivery_are_atomic(tmp_path: Path) -> None:
    engine, sessions = await _database(tmp_path, session_class=CommitFailingSession)
    target = AgentAddress("project-1", "parent", 1)

    CommitFailingSession.fail_next_commit = True
    async with sessions() as db:
        with pytest.raises(RuntimeError, match="commit_failed"):
            await ProjectSessionService.create_runtime_input(
                db,
                "session-1",
                content="first",
                target=target,
            )
    assert await _counts(sessions) == (0, 0)

    async with sessions() as db:
        message = await ProjectSessionService.create_runtime_input(
            db,
            "session-1",
            content="second",
            target=target,
        )
        delivery = (
            await db.execute(
                select(RuntimeInputDeliveryRecord).where(
                    RuntimeInputDeliveryRecord.session_message_id == message.id
                )
            )
        ).scalar_one()
    assert delivery.message_id == message.id
    assert delivery.status == "pending"
    assert delivery.target_address == target.to_uri()
    assert await _counts(sessions) == (1, 1)
    await engine.dispose()


@pytest.mark.asyncio
async def test_runtime_input_relay_recovers_both_crash_windows(tmp_path: Path) -> None:
    engine, sessions = await _database(tmp_path)
    target = AgentAddress("project-1", "parent", 1)
    mailbox = PersistentMailbox(
        tmp_path / ".bridle" / "mail.db",
        project_id="project-1",
        consumer_id="relay",
        default_target=target,
    )
    async with sessions() as db:
        message = await ProjectSessionService.create_runtime_input(
            db,
            "session-1",
            content="recover me",
            target=target,
        )

    relay = RuntimeInputRelay(sessions, mailbox_for_project=lambda _project_id: mailbox)

    def crash_after_mail(_delivery: RuntimeInputDeliveryRecord) -> None:
        raise RuntimeError("crash_after_mail")

    with pytest.raises(RuntimeError, match="crash_after_mail"):
        await relay.relay_pending(after_enqueue=crash_after_mail)

    with closing(sqlite3.connect(mailbox.database_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mail_messages WHERE message_id = ?", (message.id,)
        ).fetchone()[0] == 1
    async with sessions() as db:
        delivery = (
            await db.execute(
                select(RuntimeInputDeliveryRecord).where(
                    RuntimeInputDeliveryRecord.message_id == message.id
                )
            )
        ).scalar_one()
        assert delivery.status == "pending"

    assert await relay.relay_pending() == 1
    assert await relay.relay_pending() == 0
    with closing(sqlite3.connect(mailbox.database_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mail_messages WHERE message_id = ?", (message.id,)
        ).fetchone()[0] == 1
    async with sessions() as db:
        delivery = (
            await db.execute(
                select(RuntimeInputDeliveryRecord).where(
                    RuntimeInputDeliveryRecord.message_id == message.id
                )
            )
        ).scalar_one()
        assert delivery.status == "delivered"
        assert delivery.mail_enqueued_at is not None
        assert delivery.attempt == 2
    await mailbox.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_first_inputs_singleflight_parent_and_deduplicate_messages(
    tmp_path: Path,
) -> None:
    engine, sessions = await _database(tmp_path)
    target = AgentAddress("project-1", "parent", 1)
    async with sessions() as db:
        message = await ProjectSessionService.create_runtime_input(
            db, "session-1", content="one turn", target=target
        )

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def provider(content: str) -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return f"reply:{content}"

    coordinator = ParentChildRuntimeCoordinator(sessions)
    first = asyncio.create_task(coordinator.handle_input(message.id, provider))
    await entered.wait()
    second = asyncio.create_task(coordinator.handle_input(message.id, provider))
    release.set()
    first_reply, second_reply = await asyncio.gather(first, second)

    assert calls == 1
    assert first_reply.id == second_reply.id
    async with sessions() as db:
        assistant_count = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(ProjectMessageRecord)
                    .where(ProjectMessageRecord.role == "assistant")
                )
            ).scalar_one()
        )
    assert assistant_count == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_http_waits_for_persisted_reply_and_preserves_provider_failure_mapping(
    tmp_path: Path,
) -> None:
    engine, sessions = await _database(tmp_path)
    target = AgentAddress("project-1", "parent", 1)
    async with sessions() as db:
        message = await ProjectSessionService.create_runtime_input(
            db, "session-1", content="wait", target=target
        )
    release = asyncio.Event()

    async def provider(_content: str) -> str:
        await release.wait()
        return "ready"

    coordinator = ParentChildRuntimeCoordinator(sessions)
    request = asyncio.create_task(coordinator.handle_input(message.id, provider))
    await asyncio.sleep(0)
    assert not request.done()
    release.set()
    reply = await request
    async with sessions() as db:
        persisted = await db.get(ProjectMessageRecord, reply.id)
        result = (
            await db.execute(
                select(RuntimeInputResultRecord).where(
                    RuntimeInputResultRecord.message_id == message.id
                )
            )
        ).scalar_one()
    assert persisted is not None and persisted.content == "ready"
    assert result.assistant_message_id == reply.id

    async with sessions() as db:
        failing = await ProjectSessionService.create_runtime_input(
            db, "session-1", content="fail", target=target
        )

    async def provider_failure(_content: str) -> str:
        raise RuntimeError("provider_failed")

    with pytest.raises(RuntimeError, match="provider_failed"):
        await coordinator.handle_input(failing.id, provider_failure)
    await engine.dispose()


@pytest.mark.asyncio
async def test_child_results_are_persisted_before_destroy_and_parent_is_idempotent(
    tmp_path: Path,
) -> None:
    engine, sessions = await _database(tmp_path)
    target = AgentAddress("project-1", "parent", 1)
    source = AgentAddress("project-1", "child", 1)
    mailbox = PersistentMailbox(
        tmp_path / ".bridle" / "mail.db",
        project_id="project-1",
        consumer_id="child-result",
        default_target=target,
    )
    coordinator = ParentChildRuntimeCoordinator(
        sessions,
        mailbox_for_project=lambda _id: mailbox,
    )
    (tmp_path / "child.py").write_text("value = 1\n", encoding="utf-8")
    store = ProjectPlanStore(tmp_path, project_id="project-1")
    store.initialize()
    with closing(sqlite3.connect(store.database_path)) as connection:
        connection.execute(
            "INSERT INTO plan_nodes(id, node_type, title, goal, payload, status) "
            "VALUES ('child-node', 'module', 'child', 'run', '{}', 'ready')"
        )
        connection.commit()
    spawn = store.dispatch_child_agent("child-node", target_role="executing")
    message_id = f"child-result-{spawn['spawn_message_id']}"
    destroyed = 0

    async def destroy() -> None:
        nonlocal destroyed
        with closing(sqlite3.connect(mailbox.database_path)) as connection:
            assert connection.execute(
                "SELECT status FROM mail_messages WHERE message_id=?", (message_id,)
            ).fetchone()[0] == "delivered"
        assert store.get_node("child-node")["status"] == "completed"
        destroyed += 1

    def apply_result(result_message_id: str, payload: dict) -> dict:
        return store.apply_child_result(
            message_id=result_message_id,
            node_id=str(payload["node_id"]),
            status=str(payload["status"]),
        )

    restarted = ParentChildRuntimeCoordinator(
        sessions,
        mailbox_for_project=lambda _id: mailbox,
    )
    assert await restarted.deliver_child_result(
        message_id=message_id,
        source=source,
        target=target,
        payload={"node_id": "child-node", "status": "completed"},
        apply_result=apply_result,
        destroy=destroy,
    )
    assert await coordinator.deliver_child_result(
        message_id=message_id,
        source=source,
        target=target,
        payload={"node_id": "child-node", "status": "completed"},
        apply_result=apply_result,
        destroy=destroy,
    )
    assert destroyed == 1
    with closing(sqlite3.connect(store.database_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM child_result_receipts WHERE message_id=?", (message_id,)
        ).fetchone()[0] == 1
    await mailbox.close()
    await engine.dispose()


@pytest.mark.asyncio
async def test_different_inputs_for_one_session_are_serialized(tmp_path: Path) -> None:
    engine, sessions = await _database(tmp_path)
    target = AgentAddress("project-1", "parent", 1)
    async with sessions() as db:
        first = await ProjectSessionService.create_runtime_input(
            db, "session-1", content="first", target=target
        )
        second = await ProjectSessionService.create_runtime_input(
            db, "session-1", content="second", target=target
        )
    coordinator = ParentChildRuntimeCoordinator(sessions)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def provider(content: str) -> str:
        if content == "first":
            first_entered.set()
            await release_first.wait()
        else:
            second_entered.set()
        return f"reply-{content}"

    first_task = asyncio.create_task(coordinator.handle_input(first.id, provider))
    await first_entered.wait()
    second_task = asyncio.create_task(coordinator.handle_input(second.id, provider))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second_entered.wait(), timeout=0.05)
    release_first.set()
    first_reply, second_reply = await asyncio.gather(first_task, second_task)
    assert first_reply.content == "reply-first"
    assert second_reply.content == "reply-second"
    await engine.dispose()


@pytest.mark.asyncio
async def test_child_result_delivery_retry_survives_parent_exit(tmp_path: Path) -> None:
    target = AgentAddress("project-1", "parent", 1)
    source = AgentAddress("project-1", "child", 1)
    mailbox = PersistentMailbox(
        tmp_path / ".bridle" / "mail.db",
        project_id="project-1",
        consumer_id="child-result",
        capacity=1,
        default_target=target,
    )
    mailbox.enqueue(
        MailEnvelope(
            message_id="capacity-holder",
            message_type="test",
            source=source,
            target=target,
            payload={},
        )
    )
    first_parent = ParentChildRuntimeCoordinator(
        None, mailbox_for_project=lambda _id: mailbox
    )
    destroys = 0

    async def destroy() -> None:
        nonlocal destroys
        destroys += 1

    assert not await first_parent.deliver_child_result(
        message_id="child-result-retry",
        source=source,
        target=target,
        payload={"status": "failed"},
        destroy=destroy,
    )
    assert destroys == 0
    claimed = mailbox.claim(target)
    assert claimed.lease_token is not None
    mailbox.ack("capacity-holder", claimed.lease_token, target=target)

    recovered_parent = ParentChildRuntimeCoordinator(
        None, mailbox_for_project=lambda _id: mailbox
    )
    assert await recovered_parent.deliver_child_result(
        message_id="child-result-retry",
        source=source,
        target=target,
        payload={"status": "failed"},
        destroy=destroy,
    )
    assert destroys == 1
    await mailbox.close()
