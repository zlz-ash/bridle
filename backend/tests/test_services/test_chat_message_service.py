from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.errors import ConflictError

from bridle.models.agent_coding_session import AgentCodingSessionRecord
from bridle.models.plan import PlanRecord
from bridle.models.task import TaskRecord
from bridle.schemas.coding import ChatMessageCreateSchema
from bridle.services.chat_message_service import ChatMessageService


async def _create_coding_session(db: AsyncSession) -> AgentCodingSessionRecord:
    task = TaskRecord(title="Chat Task", status="planned")
    db.add(task)
    await db.flush()

    plan = PlanRecord(task_id=task.id, goal="Chat plan", status="active")
    db.add(plan)
    await db.flush()

    session = AgentCodingSessionRecord(plan_id=plan.id, status="active", mode="coding")
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


class TestChatMessageService:
    async def test_create_and_list_messages_in_session_order(self, db: AsyncSession) -> None:
        session = await _create_coding_session(db)

        first = await ChatMessageService.create_message(
            db,
            session_id=session.id,
            message=ChatMessageCreateSchema(role="user", content="Build the feature"),
        )
        second = await ChatMessageService.create_message(
            db,
            session_id=session.id,
            message=ChatMessageCreateSchema(role="assistant", content="Working on it"),
        )

        messages = await ChatMessageService.list_messages(db, session.id)

        assert [m.id for m in messages] == [first.id, second.id]
        assert [m.role for m in messages] == ["user", "assistant"]
        assert [m.content for m in messages] == ["Build the feature", "Working on it"]
        assert messages[0].created_at <= messages[1].created_at

    async def test_preserves_tool_metadata_as_structured_json(self, db: AsyncSession) -> None:
        session = await _create_coding_session(db)
        tool_calls = [{"id": "call-1", "name": "run_allowed_tests", "arguments": {"commands": ["pytest -q"]}}]
        tool_result = {"status": "completed", "results": [{"command": "pytest -q", "exit_code": 0}]}

        await ChatMessageService.create_message(
            db,
            session_id=session.id,
            message=ChatMessageCreateSchema(
                role="tool",
                content="pytest passed",
                tool_calls=tool_calls,
                tool_result=tool_result,
            ),
        )

        messages = await ChatMessageService.list_messages(db, session.id)

        assert messages[0].tool_calls == tool_calls
        assert messages[0].tool_result == tool_result

    async def test_messages_are_isolated_by_session(self, db: AsyncSession) -> None:
        first_session = await _create_coding_session(db)
        second_session = await _create_coding_session(db)

        await ChatMessageService.create_message(
            db,
            session_id=first_session.id,
            message=ChatMessageCreateSchema(role="user", content="first session"),
        )
        await ChatMessageService.create_message(
            db,
            session_id=second_session.id,
            message=ChatMessageCreateSchema(role="user", content="second session"),
        )

        messages = await ChatMessageService.list_messages(db, first_session.id)

        assert len(messages) == 1
        assert messages[0].session_id == first_session.id
        assert messages[0].content == "first session"

    async def test_rejects_short_term_memory_as_persisted_input(self, db: AsyncSession) -> None:
        await _create_coding_session(db)

        with pytest.raises(ValidationError):
            ChatMessageCreateSchema.model_validate(
                {
                    "role": "assistant",
                    "content": "compacted summary",
                    "short_term_memory": [{"role": "user", "content": "raw"}],
                }
            )

    async def test_user_message_triggers_reconcile(self, db: AsyncSession) -> None:
        session = await _create_coding_session(db)

        with patch(
            "bridle.services.chat_message_service.SessionReconciler.ensure_main_agent_alive",
            new_callable=AsyncMock,
        ) as ensure:
            await ChatMessageService.create_message(
                db,
                session_id=session.id,
                message=ChatMessageCreateSchema(role="user", content="go"),
            )

        ensure.assert_called_once()

    async def test_assistant_message_skips_reconcile(self, db: AsyncSession) -> None:
        session = await _create_coding_session(db)

        with patch(
            "bridle.services.chat_message_service.SessionReconciler.ensure_main_agent_alive",
            new_callable=AsyncMock,
        ) as ensure:
            await ChatMessageService.create_message(
                db,
                session_id=session.id,
                message=ChatMessageCreateSchema(role="assistant", content="done"),
            )

        ensure.assert_not_called()

    async def test_main_agent_unavailable_raises_conflict(self, db: AsyncSession) -> None:
        session = await _create_coding_session(db)

        with patch(
            "bridle.services.chat_message_service.SessionReconciler.ensure_main_agent_alive",
            new_callable=AsyncMock,
            side_effect=ConflictError(
                resource="coding_session",
                message="Main agent unavailable",
                error_code="main_agent_unavailable",
            ),
        ):
            with pytest.raises(ConflictError) as exc_info:
                await ChatMessageService.create_message(
                    db,
                    session_id=session.id,
                    message=ChatMessageCreateSchema(role="user", content="go"),
                )

        assert exc_info.value.api_error.code == "main_agent_unavailable"
