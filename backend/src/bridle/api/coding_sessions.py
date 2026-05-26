"""Agent coding session API."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.api.deps import get_db
from bridle.schemas.coding import CodingSessionCreateSchema, SelectNodeIntentSchema
from bridle.services.agent_coding_session_service import AgentCodingSessionService

router = APIRouter(prefix="/agent", tags=["coding-sessions"])


@router.post("/coding-sessions")
async def create_coding_session(data: CodingSessionCreateSchema, db: AsyncSession = Depends(get_db)) -> dict:
    session = await AgentCodingSessionService.create_session(
        db, data.plan_id, data.auto_continue_budget,
    )
    return session.model_dump()


@router.get("/coding-sessions/{session_id}")
async def get_coding_session(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    session = await AgentCodingSessionService.get_session(db, session_id)
    return session.model_dump()


@router.post("/coding-sessions/{session_id}/cancel")
async def cancel_coding_session(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    session = await AgentCodingSessionService.cancel_session(db, session_id)
    return session.model_dump()


@router.get("/coding-sessions/{session_id}/eligible-nodes")
async def get_eligible_nodes(session_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    result = await AgentCodingSessionService.get_eligible_nodes(db, session_id)
    return result.model_dump()


@router.post("/coding-sessions/{session_id}/select-node")
async def select_node(
    session_id: str,
    data: SelectNodeIntentSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    run = await AgentCodingSessionService.select_node(db, session_id, data)
    return run.model_dump()
