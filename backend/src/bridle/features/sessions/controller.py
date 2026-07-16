"""Unified project conversation API."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.agent.runtime.gateway import AgentGateway
from bridle.agent.runtime.role_policy import RuntimeRolePolicy
from bridle.api.deps import get_db
from bridle.features.sessions.schemas import (
    ProjectConverseSchema,
    ProjectMessageCreateSchema,
    ProjectSessionCreateSchema,
    SessionRoleChangeSchema,
)
from bridle.features.sessions.service import ProjectSessionService

router = APIRouter(prefix="/sessions", tags=["project-sessions"])
_DB_DEPENDENCY = Depends(get_db)


@router.post("", status_code=201)
async def create_session(data: ProjectSessionCreateSchema, db: AsyncSession = _DB_DEPENDENCY) -> dict:
    """Create one project conversation; request input exits as planning runtime state."""
    session = await ProjectSessionService.create(db, project_id=data.project_id, title=data.title)
    return session.model_dump(mode="json")


@router.get("")
async def list_sessions(project_id: str | None = None, db: AsyncSession = _DB_DEPENDENCY) -> list[dict]:
    """List conversation history; optional project input exits as serialized sessions."""
    sessions = await ProjectSessionService.list(db, project_id=project_id)
    return [session.model_dump(mode="json") for session in sessions]


@router.post("/{session_id}/role")
async def change_role(
    session_id: str,
    data: SessionRoleChangeSchema,
    db: AsyncSession = _DB_DEPENDENCY,
) -> dict:
    """Change planning/executing role; user transition input exits as updated session state."""
    session = await ProjectSessionService.change_role(db, session_id, data)
    return session.model_dump(mode="json")


@router.post("/{session_id}/messages", status_code=201)
async def create_message(
    session_id: str,
    data: ProjectMessageCreateSchema,
    db: AsyncSession = _DB_DEPENDENCY,
) -> dict:
    """Append a session message; request input exits as persisted message data."""
    message = await ProjectSessionService.create_message(db, session_id, data)
    return message.model_dump(mode="json")


@router.post("/{session_id}/converse", status_code=201)
async def converse(
    session_id: str,
    data: ProjectConverseSchema,
    db: AsyncSession = _DB_DEPENDENCY,
) -> dict:
    """Run one unified agent turn; session/content/node input exits as the persisted reply."""
    message = await AgentGateway.converse(db, session_id, data.content, node_id=data.node_id)
    return message.model_dump(mode="json")


@router.post("/{session_id}/close")
async def close_session(session_id: str, db: AsyncSession = _DB_DEPENDENCY) -> dict:
    """Close one session, destroy its runtimes, and preserve readable history."""
    session = await AgentGateway.close_session(db, session_id)
    return session.model_dump(mode="json")


@router.get("/{session_id}/messages")
async def list_messages(session_id: str, db: AsyncSession = _DB_DEPENDENCY) -> list[dict]:
    """Read message history; session ID input exits as ordered messages even if project is missing."""
    messages = await ProjectSessionService.list_messages(db, session_id)
    return [message.model_dump(mode="json") for message in messages]


@router.get("/{session_id}/capabilities")
async def get_capabilities(session_id: str, db: AsyncSession = _DB_DEPENDENCY) -> dict:
    """Read shared tool permissions; session ID input exits as role and fail-closed manifest."""
    session = await ProjectSessionService.get(db, session_id)
    return {"role": session.role, "tools": RuntimeRolePolicy.manifest(session.role)}

