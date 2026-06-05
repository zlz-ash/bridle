from __future__ import annotations

from pathlib import Path

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bridle.database import configure_sqlite_engine
from bridle.models.agent_coding_session import AgentCodingSessionRecord
from bridle.models.base import Base
from bridle.models.chat_message import ChatMessageRecord
from bridle.models.plan import PlanRecord
from bridle.models.task import TaskRecord


@pytest_asyncio.fixture
async def coding_session(db: AsyncSession) -> AgentCodingSessionRecord:
    task = TaskRecord(title="Persist Chat", status="planned")
    db.add(task)
    await db.flush()
    plan = PlanRecord(task_id=task.id, goal="Persist messages", status="active")
    db.add(plan)
    await db.flush()
    session = AgentCodingSessionRecord(plan_id=plan.id, status="active", mode="coding")
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


class TestChatMessagePersistence:
    async def test_create_chat_message(self, db: AsyncSession, coding_session: AgentCodingSessionRecord) -> None:
        message = ChatMessageRecord(
            session_id=coding_session.id,
            role="user",
            content="Keep this original message",
        )
        db.add(message)
        await db.commit()

        assert message.id is not None
        assert message.created_at is not None
        assert message.session_id == coding_session.id
        assert message.tool_calls is None
        assert message.tool_result is None

    async def test_restart_recovery(self, recovery_db_path: Path) -> None:
        recovery_engine = create_async_engine(
            f"sqlite+aiosqlite:///{recovery_db_path.as_posix()}",
            echo=False,
        )
        configure_sqlite_engine(recovery_engine)
        async with recovery_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        factory = async_sessionmaker(recovery_engine, class_=AsyncSession, expire_on_commit=False)

        async with factory() as s1:
            task = TaskRecord(title="Recovery Chat", status="planned")
            s1.add(task)
            await s1.flush()
            plan = PlanRecord(task_id=task.id, goal="Recover chat", status="active")
            s1.add(plan)
            await s1.flush()
            session = AgentCodingSessionRecord(plan_id=plan.id, status="active", mode="coding")
            s1.add(session)
            await s1.flush()
            message = ChatMessageRecord(
                session_id=session.id,
                role="assistant",
                content="Original assistant response",
                tool_calls=[{"id": "tc1", "name": "read_allowed_file"}],
                tool_result={"status": "completed"},
            )
            s1.add(message)
            await s1.commit()
            session_id = session.id

        async with factory() as s2:
            result = await s2.execute(
                select(ChatMessageRecord).where(ChatMessageRecord.session_id == session_id)
            )
            messages = result.scalars().all()

        assert len(messages) == 1
        assert messages[0].content == "Original assistant response"
        assert messages[0].tool_calls == [{"id": "tc1", "name": "read_allowed_file"}]
        assert messages[0].tool_result == {"status": "completed"}

        await recovery_engine.dispose()
