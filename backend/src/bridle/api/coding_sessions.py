"""Agent coding session API."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.errors import ValidationError

from bridle.api.deps import get_db
from bridle.schemas.coding import (
    ChatMessageCreateSchema,
    CodingSessionCreateSchema,
    SelectNodeIntentSchema,
)
from bridle.services.agent_coding_session_service import AgentCodingSessionService
from bridle.services.chat_message_service import ChatMessageService

router = APIRouter(prefix="/agent", tags=["coding-sessions"])


@router.post("/coding-sessions")
async def create_coding_session(data: CodingSessionCreateSchema, db: AsyncSession = Depends(get_db)) -> dict:
    session = await AgentCodingSessionService.create_session(
        db, data.plan_id, data.auto_continue_budget,
    )
    return session.model_dump()


@router.get("/coding-sessions")
async def list_coding_sessions(
    db: AsyncSession = Depends(get_db),
    status: str = "all",
    plan_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    from bridle.schemas.coding import CodingSessionListResponseSchema

    sessions, total = await AgentCodingSessionService.list_sessions(
        db,
        status=status,
        plan_id=plan_id,
        limit=limit,
        offset=offset,
    )
    payload = CodingSessionListResponseSchema(
        sessions=sessions,
        total=total,
        limit=limit,
        offset=offset,
    )
    return payload.model_dump()


@router.get("/coding-sessions/{session_id}")
async def get_coding_session(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    session = await AgentCodingSessionService.get_session(db, session_id)
    return session.model_dump()


@router.post("/coding-sessions/{session_id}/messages", status_code=201)
async def create_chat_message(
    session_id: str,
    data: ChatMessageCreateSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    message = await ChatMessageService.create_message(db, session_id=session_id, message=data)
    return message.model_dump()


@router.get("/coding-sessions/{session_id}/messages")
async def list_chat_messages(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    created_after: str | None = Query(default=None),
) -> list[dict]:
    parsed_after: datetime | None = None
    if created_after is not None:
        text = created_after.strip()
        if not text:
            raise ValidationError(
                resource="chat_message",
                message="created_after must be a non-empty ISO-8601 timestamp",
                details={"created_after": created_after},
            )
        normalized = text.replace("Z", "+00:00")
        try:
            parsed_after = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValidationError(
                resource="chat_message",
                message="created_after must be a valid ISO-8601 timestamp",
                details={"created_after": created_after},
            ) from exc
        if parsed_after.tzinfo is not None:
            parsed_after = parsed_after.replace(tzinfo=None)
    messages = await ChatMessageService.list_messages(db, session_id, created_after=parsed_after)
    return [message.model_dump() for message in messages]


@router.post("/coding-sessions/{session_id}/fail")
async def fail_coding_session(
    session_id: str,
    body: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    reason = str(body.get("reason") or "complexity negotiation failed")
    session = await AgentCodingSessionService.fail_session(db, session_id, reason=reason)
    return session.model_dump()


@router.post("/coding-sessions/{session_id}/cancel")
async def cancel_coding_session(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    session = await AgentCodingSessionService.cancel_session(db, session_id)
    return session.model_dump()


@router.get("/coding-sessions/{session_id}/eligible-nodes")
async def get_eligible_nodes(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    result = await AgentCodingSessionService.get_eligible_nodes(db, session_id)
    return result.model_dump()


@router.get("/coding-sessions/{session_id}/recent-failed-runs")
async def get_recent_failed_runs(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=3, ge=1, le=10),
) -> list[dict]:
    return await AgentCodingSessionService.get_recent_failed_runs(
        db, session_id, limit=limit,
    )


@router.post("/coding-sessions/{session_id}/select-node")
async def select_node(
    session_id: str,
    data: SelectNodeIntentSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = await AgentCodingSessionService.select_node(db, session_id, data)
    return run.model_dump()
