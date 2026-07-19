"""Unified project session, role, and message persistence."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.agent.runtime.mailbox import AgentAddress
from bridle.agent.runtime.persistence import add_runtime_input_delivery
from bridle.api.errors import ConflictError, ForbiddenError, NotFoundError
from bridle.features.projects.service import ProjectService
from bridle.features.sessions.schemas import (
    ProjectMessageCreateSchema,
    ProjectMessageReadSchema,
    ProjectSessionReadSchema,
    SessionRoleChangeSchema,
)
from bridle.logging.facade import LoggingFacade, get_logging_facade
from bridle.models.project_message import ProjectMessageRecord
from bridle.models.project_session import ProjectSessionRecord
from bridle.models.project_session_memory import ProjectSessionMemoryRecord
from bridle.utils.datetime_util import utc_now_naive


class ProjectSessionService:
    """Own shared planning/execution conversations; inputs persist role/messages and outputs are history."""

    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        project_id: str,
        title: str,
    ) -> ProjectSessionReadSchema:
        """Create a planning session; project/title input returns persisted shared runtime state."""
        project = await ProjectService.get_record(db, project_id)
        record = ProjectSessionRecord(
            project_id=project.id,
            project_path_snapshot=project.path,
            title=title,
            role="planning",
            status="active",
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        ProjectSessionService._log("project_session_create", record, detail={"role": record.role})
        return ProjectSessionService._to_read(record)

    @staticmethod
    async def list(db: AsyncSession, *, project_id: str | None = None) -> list[ProjectSessionReadSchema]:
        """List session history; optional project input returns newest conversations first."""
        statement = select(ProjectSessionRecord).order_by(
            ProjectSessionRecord.updated_at.desc(), ProjectSessionRecord.id.desc()
        )
        if project_id is not None:
            statement = statement.where(ProjectSessionRecord.project_id == project_id)
        records = (await db.execute(statement)).scalars().all()
        return [ProjectSessionService._to_read(record) for record in records]

    @staticmethod
    async def get(db: AsyncSession, session_id: str) -> ProjectSessionReadSchema:
        """Read one shared session; DB/ID input returns availability, role, and history metadata."""
        record = await ProjectSessionService._load(db, session_id)
        return ProjectSessionService._to_read(record)

    @staticmethod
    async def change_role(
        db: AsyncSession,
        session_id: str,
        change: SessionRoleChangeSchema,
    ) -> ProjectSessionReadSchema:
        """Apply user-owned role transition; session/change input returns updated runtime state."""
        record = await ProjectSessionService._load(db, session_id)
        if change.actor != "user":
            raise ForbiddenError(
                resource="project_session",
                message="Agent cannot change session role",
                error_code="role_switch_forbidden",
            )
        if change.role == "executing" and not change.confirmed:
            raise ConflictError(
                resource="project_session",
                message="Execution requires explicit user confirmation",
                error_code="execution_confirmation_required",
            )
        if not Path(record.project_path_snapshot).is_dir():
            raise ConflictError(
                resource="project_session",
                message="Project path is unavailable",
                error_code="project_unavailable_read_only",
            )
        previous = record.role
        record.role = change.role
        await db.commit()
        await db.refresh(record)
        ProjectSessionService._log(
            "project_session_role_change",
            record,
            detail={"from_role": previous, "to_role": record.role, "actor": change.actor},
        )
        return ProjectSessionService._to_read(record)

    @staticmethod
    async def create_message(
        db: AsyncSession,
        session_id: str,
        message: ProjectMessageCreateSchema,
    ) -> ProjectMessageReadSchema:
        """Persist one message; session/message input returns it or rejects unavailable project writes."""
        session = await ProjectSessionService._load(db, session_id)
        if not Path(session.project_path_snapshot).is_dir():
            raise ConflictError(
                resource="project_session",
                message="Project path is unavailable; history is read-only",
                error_code="project_unavailable_read_only",
            )
        record = ProjectMessageRecord(
            session_id=session.id,
            role=message.role,
            content=message.content,
            tool_calls=message.tool_calls,
            tool_result=message.tool_result,
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        ProjectSessionService._log(
            "project_message_persist",
            session,
            detail={"message_id": record.id, "role": record.role},
        )
        return ProjectMessageReadSchema.model_validate(record)

    @staticmethod
    async def create_runtime_input(
        db: AsyncSession,
        session_id: str,
        *,
        content: str,
        target: AgentAddress,
        facade: LoggingFacade | None = None,
        trace_id: str | None = None,
    ) -> ProjectMessageReadSchema:
        """Atomically persist one user message and its pending runtime delivery fact."""
        session = await ProjectSessionService._load(db, session_id)
        if not Path(session.project_path_snapshot).is_dir():
            raise ConflictError(
                resource="project_session",
                message="Project path is unavailable; history is read-only",
                error_code="project_unavailable_read_only",
            )
        if target.project_id != session.project_id:
            raise ConflictError(
                resource="project_session",
                message="Runtime input target belongs to another project",
                error_code="runtime_input_target_mismatch",
            )
        record = ProjectMessageRecord(
            session_id=session.id,
            role="user",
            content=content,
        )
        db.add(record)
        try:
            await db.flush()
            await add_runtime_input_delivery(
                db,
                message_id=record.id,
                session_message_id=record.id,
                project_id=session.project_id,
                session_id=session.id,
                target_address=target.to_uri(),
                target_agent_id=target.agent_id,
                target_generation=target.generation,
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        await db.refresh(record)
        correlation = {
            "message_id": record.id,
            "project_id": session.project_id,
            "agent_id": target.agent_id,
            "generation": target.generation,
            "session_id": session.id,
        }
        if trace_id is not None:
            correlation["trace_id"] = trace_id
        (facade or get_logging_facade()).info_event(
            "runtime_input.persisted",
            "completed",
            detail={"attempt": 0},
            **correlation,
        )
        return ProjectMessageReadSchema.model_validate(record)

    @staticmethod
    async def list_messages(db: AsyncSession, session_id: str) -> list[ProjectMessageReadSchema]:
        """Read conversation history; session input returns messages even when the project is missing."""
        await ProjectSessionService._load(db, session_id)
        records = (
            await db.execute(
                select(ProjectMessageRecord)
                .where(ProjectMessageRecord.session_id == session_id)
                .order_by(ProjectMessageRecord.created_at, ProjectMessageRecord.id)
            )
        ).scalars().all()
        return [ProjectMessageReadSchema.model_validate(record) for record in records]

    @staticmethod
    async def ensure_memory_table(db: AsyncSession) -> None:
        """Create the checkpoint table for existing local databases, idempotently."""
        bind = db.bind
        if bind is None:
            raise RuntimeError("session_memory_database_bind_required")
        facade = get_logging_facade()
        facade.info_event("session_memory_table.ensure", "started")
        try:
            async with bind.begin() as connection:
                await connection.run_sync(
                    ProjectSessionMemoryRecord.__table__.create,
                    checkfirst=True,
                )
        except Exception as exc:
            facade.info_event(
                "session_memory_table.ensure",
                "failed",
                error_code=type(exc).__name__,
            )
            raise
        facade.info_event("session_memory_table.ensure", "completed")

    @staticmethod
    async def get_memory_checkpoint(
        db: AsyncSession,
        session_id: str,
    ) -> ProjectSessionMemoryRecord | None:
        """Read the summary checkpoint without loading original conversation history."""
        await ProjectSessionService._load(db, session_id)
        record = await db.get(ProjectSessionMemoryRecord, session_id)
        get_logging_facade().info_event(
            "session_memory_checkpoint.read",
            "completed",
            session_id=session_id,
            detail={"found": record is not None},
        )
        return record

    @staticmethod
    async def list_messages_after(
        db: AsyncSession,
        session_id: str,
        *,
        after_message_id: str | None,
    ) -> list[ProjectMessageReadSchema]:
        """Read only messages newer than the persisted summary anchor."""
        await ProjectSessionService._load(db, session_id)
        statement = select(ProjectMessageRecord).where(
            ProjectMessageRecord.session_id == session_id
        )
        if after_message_id:
            anchor = (
                await db.execute(
                    select(ProjectMessageRecord).where(
                        ProjectMessageRecord.session_id == session_id,
                        ProjectMessageRecord.id == after_message_id,
                    )
                )
            ).scalar_one_or_none()
            if anchor is not None:
                statement = statement.where(
                    or_(
                        ProjectMessageRecord.created_at > anchor.created_at,
                        and_(
                            ProjectMessageRecord.created_at == anchor.created_at,
                            ProjectMessageRecord.id > anchor.id,
                        ),
                    )
                )
        records = (
            await db.execute(
                statement.order_by(ProjectMessageRecord.created_at, ProjectMessageRecord.id)
            )
        ).scalars().all()
        get_logging_facade().info_event(
            "session_memory_delta.read",
            "completed",
            session_id=session_id,
            detail={"after_message_id": after_message_id, "message_count": len(records)},
        )
        return [ProjectMessageReadSchema.model_validate(record) for record in records]

    @staticmethod
    async def update_memory_checkpoint(
        db: AsyncSession,
        session_id: str,
        *,
        summary: str,
        anchor_message_id: str,
    ) -> ProjectSessionMemoryRecord:
        """Atomically advance the summary checkpoint after a successful compaction."""
        facade = get_logging_facade()
        facade.info_event(
            "session_memory_checkpoint.update",
            "started",
            session_id=session_id,
            detail={"anchor_message_id": anchor_message_id},
        )
        try:
            await ProjectSessionService._load(db, session_id)
            anchor = (
                await db.execute(
                    select(ProjectMessageRecord).where(
                        ProjectMessageRecord.session_id == session_id,
                        ProjectMessageRecord.id == anchor_message_id,
                    )
                )
            ).scalar_one_or_none()
            if anchor is None:
                raise ValueError("session_memory_anchor_not_found")
            record = await db.get(ProjectSessionMemoryRecord, session_id)
            if record is None:
                record = ProjectSessionMemoryRecord(session_id=session_id)
                db.add(record)
            record.summary = summary
            record.anchor_message_id = anchor_message_id
            record.updated_at = utc_now_naive()
            await db.commit()
            await db.refresh(record)
        except Exception as exc:
            await db.rollback()
            facade.info_event(
                "session_memory_checkpoint.update",
                "failed",
                session_id=session_id,
                error_code=type(exc).__name__,
            )
            raise
        facade.info_event(
            "session_memory_checkpoint.update",
            "completed",
            session_id=session_id,
            detail={"anchor_message_id": anchor_message_id},
        )
        return record

    @staticmethod
    async def _load(db: AsyncSession, session_id: str) -> ProjectSessionRecord:
        """Load one session; DB/ID input returns ORM state or a not-found error."""
        record = (
            await db.execute(select(ProjectSessionRecord).where(ProjectSessionRecord.id == session_id))
        ).scalar_one_or_none()
        if record is None:
            raise NotFoundError(resource="project_session", details={"session_id": session_id})
        return record

    @staticmethod
    def _to_read(record: ProjectSessionRecord) -> ProjectSessionReadSchema:
        """Serialize runtime state; ORM input returns availability and read-only reason."""
        available = Path(record.project_path_snapshot).is_dir()
        return ProjectSessionReadSchema(
            id=record.id,
            project_id=record.project_id,
            project_path=record.project_path_snapshot,
            title=record.title,
            role=record.role,
            status=record.status,
            available=available,
            readonly_reason=None if available else "project_path_unavailable",
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _log(action: str, record: ProjectSessionRecord, *, detail: dict) -> None:
        """Emit safe session event; action/record/detail input exits through the logging facade."""
        get_logging_facade().info_event(
            action,
            "completed",
            session_id=record.id,
            detail={"project_id": record.project_id, **detail},
        )

