"""ChatMessageService - persisted main agent chat history."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.errors import NotFoundError
from bridle.events.bus import publish_event_safe
from bridle.logging.jsonl import log_event
from bridle.models.agent_coding_session import AgentCodingSessionRecord
from bridle.models.chat_message import ChatMessageRecord
from bridle.schemas.coding import ChatMessageCreateSchema, ChatMessageReadSchema
from bridle.services.session_reconciler import SessionReconciler


class ChatMessageService:
    @staticmethod
    async def create_message(
        db: AsyncSession,
        *,
        session_id: str,
        message: ChatMessageCreateSchema,
    ) -> ChatMessageReadSchema:
        await ChatMessageService._ensure_session(db, session_id)
        session = await ChatMessageService._load_session(db, session_id)
        if session.mode == "coding" and message.role == "user":
            await SessionReconciler.ensure_main_agent_alive(session_id, db)
        record = ChatMessageRecord(
            session_id=session_id,
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
            tool_result=message.tool_result,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        log_event(
            "chat_message_persisted",
            "completed",
            detail={"session_id": session_id, "message_id": record.id, "role": record.role},
        )
        publish_event_safe(
            "chat_message_appended",
            {
                "session_id": session_id,
                "message_id": record.id,
                "role": record.role,
                "created_at": record.created_at.isoformat(),
            },
        )
        return ChatMessageReadSchema.model_validate(record)

    @staticmethod
    async def list_messages(
        db: AsyncSession,
        session_id: str,
        *,
        created_after: datetime | None = None,
    ) -> list[ChatMessageReadSchema]:
        await ChatMessageService._ensure_session(db, session_id)
        query = (
            select(ChatMessageRecord)
            .where(ChatMessageRecord.session_id == session_id)
            .order_by(ChatMessageRecord.created_at, ChatMessageRecord.id)
        )
        if created_after is not None:
            query = query.where(ChatMessageRecord.created_at > created_after)
        result = await db.execute(query)
        records = result.scalars().all()
        log_event(
            "chat_messages_listed",
            "completed",
            detail={"session_id": session_id, "message_count": len(records)},
        )
        return [ChatMessageReadSchema.model_validate(record) for record in records]

    @staticmethod
    async def _load_session(db: AsyncSession, session_id: str) -> AgentCodingSessionRecord:
        result = await db.execute(
            select(AgentCodingSessionRecord).where(AgentCodingSessionRecord.id == session_id)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise NotFoundError(
                resource="coding_session",
                message="Coding session not found",
                details={"session_id": session_id},
            )
        return record

    @staticmethod
    async def _ensure_session(db: AsyncSession, session_id: str) -> None:
        await ChatMessageService._load_session(db, session_id)
