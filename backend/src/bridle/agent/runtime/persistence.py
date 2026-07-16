"""Application-database persistence for runtime facts."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bridle.logging.facade import LoggingFacade, get_logging_facade
from bridle.models.agent_runtime import AgentRuntimeRecord, RuntimeInputDeliveryRecord


async def add_runtime_record(
    session: AsyncSession,
    *,
    runtime_type: str,
    owner_type: str,
    owner_id: str,
    project_id: str | None,
    session_id: str | None,
    parent_agent_id: str | None,
    parent_runtime_id: str | None = None,
    agent_id: str,
    generation: int,
    status: str,
    status_reason: str | None = None,
    result_summary: str | None = None,
    error_summary: str | None = None,
    facade: LoggingFacade | None = None,
    trace_id: str | None = None,
) -> AgentRuntimeRecord:
    record = AgentRuntimeRecord(
        runtime_type=runtime_type,
        owner_type=owner_type,
        owner_id=owner_id,
        project_id=project_id,
        session_id=session_id,
        parent_agent_id=parent_agent_id,
        parent_runtime_id=parent_runtime_id,
        agent_id=agent_id,
        generation=generation,
        status=status,
        status_reason=status_reason,
        result_summary=result_summary,
        error_summary=error_summary,
    )
    session.add(record)
    await session.flush()
    log_fields = {
        "project_id": project_id,
        "agent_id": agent_id,
        "generation": generation,
    }
    if trace_id is not None:
        log_fields["trace_id"] = trace_id
    (facade or get_logging_facade()).info_event(
        "runtime_record.created",
        "completed",
        detail={"runtime_id": record.id, "status": status},
        **log_fields,
    )
    return record


async def get_runtime_record(session: AsyncSession, runtime_id: str) -> AgentRuntimeRecord:
    record = await session.get(AgentRuntimeRecord, runtime_id)
    if record is None:
        raise LookupError("runtime_record_not_found")
    return record


async def update_runtime_state(
    session: AsyncSession,
    runtime_id: str,
    *,
    status: str,
    status_reason: str | None = None,
    result_summary: str | None = None,
    error_summary: str | None = None,
    facade: LoggingFacade | None = None,
    trace_id: str | None = None,
) -> AgentRuntimeRecord:
    record = await get_runtime_record(session, runtime_id)
    record.status = status
    record.status_reason = status_reason
    if result_summary is not None:
        record.result_summary = result_summary
    if error_summary is not None:
        record.error_summary = error_summary
    await session.flush()
    log_fields = {
        "project_id": record.project_id,
        "agent_id": record.agent_id,
        "generation": record.generation,
    }
    if trace_id is not None:
        log_fields["trace_id"] = trace_id
    (facade or get_logging_facade()).info_event(
        "runtime_record.state_changed",
        "completed",
        detail={"runtime_id": record.id, "status": status},
        **log_fields,
    )
    return record


_ACTIVE_RUNTIME_STATES = (
    "CREATING",
    "READY",
    "RUNNING",
    "STOPPING",
)


async def get_active_parent_runtime(
    session: AsyncSession,
    *,
    session_id: str,
) -> AgentRuntimeRecord | None:
    return await session.scalar(
        select(AgentRuntimeRecord)
        .where(
            AgentRuntimeRecord.runtime_type == "parent",
            AgentRuntimeRecord.session_id == session_id,
            AgentRuntimeRecord.status.in_(_ACTIVE_RUNTIME_STATES),
        )
        .order_by(AgentRuntimeRecord.generation.desc())
        .limit(1)
    )


async def get_active_map_runtime(
    session: AsyncSession,
    *,
    project_id: str,
) -> AgentRuntimeRecord | None:
    return await session.scalar(
        select(AgentRuntimeRecord)
        .where(
            AgentRuntimeRecord.runtime_type == "map",
            AgentRuntimeRecord.project_id == project_id,
            AgentRuntimeRecord.status.in_(_ACTIVE_RUNTIME_STATES),
        )
        .order_by(AgentRuntimeRecord.generation.desc())
        .limit(1)
    )


async def list_active_child_runtimes(
    session: AsyncSession,
    *,
    parent_runtime_id: str,
) -> tuple[AgentRuntimeRecord, ...]:
    rows = await session.scalars(
        select(AgentRuntimeRecord)
        .where(
            AgentRuntimeRecord.parent_runtime_id == parent_runtime_id,
            AgentRuntimeRecord.status.in_(_ACTIVE_RUNTIME_STATES),
        )
        .order_by(AgentRuntimeRecord.agent_id, AgentRuntimeRecord.generation)
    )
    return tuple(rows)


async def add_runtime_input_delivery(
    session: AsyncSession,
    *,
    message_id: str,
    session_message_id: str,
    project_id: str,
    session_id: str,
    target_address: str,
    target_agent_id: str,
    target_generation: int,
) -> RuntimeInputDeliveryRecord:
    record = RuntimeInputDeliveryRecord(
        message_id=message_id,
        session_message_id=session_message_id,
        project_id=project_id,
        session_id=session_id,
        target_address=target_address,
        target_agent_id=target_agent_id,
        target_generation=target_generation,
        status="pending",
        attempt=0,
    )
    session.add(record)
    await session.flush()
    return record
