"""Parent input and child-result coordination over durable application and Mail stores."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.features.sessions.schemas import ProjectMessageReadSchema
from bridle.logging.facade import LoggingFacade, get_logging_facade
from bridle.models.agent_runtime import (
    RuntimeChildResultReceiptRecord,
    RuntimeInputDeliveryRecord,
    RuntimeInputResultRecord,
)
from bridle.models.project_message import ProjectMessageRecord

Provider = Callable[[str], Awaitable[str]]
DestroyCallback = Callable[[], Awaitable[None]]
MailboxFactory = Callable[[str], PersistentMailbox]
ChildResultApplier = Callable[[str, dict], object]


class ParentChildRuntimeCoordinator:
    """Serialize each durable input and publish child results before destruction."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession] | None,
        *,
        mailbox_for_project: MailboxFactory | None = None,
        facade: LoggingFacade | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._mailbox_for_project = mailbox_for_project
        self._logging = facade or get_logging_facade()
        self._trace_id = trace_id
        self._input_locks: dict[str, asyncio.Lock] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._completed_child_results: set[str] = set()
        self._lock = asyncio.Lock()

    async def handle_input(
        self,
        message_id: str,
        provider: Provider,
        *,
        trace_id: str | None = None,
    ) -> ProjectMessageReadSchema:
        """Wait for and persist one provider reply, deduplicated by input message ID."""
        if self._sessions is None:
            raise RuntimeError("runtime_sessions_required")
        async with self._lock:
            message_lock = self._input_locks.setdefault(message_id, asyncio.Lock())
        async with message_lock:
            async with self._sessions() as session:
                delivery = await self._delivery(session, message_id)
                result = await self._result(session, message_id)
                if result is not None:
                    assistant = await session.get(
                        ProjectMessageRecord, result.assistant_message_id
                    )
                    if assistant is not None:
                        return ProjectMessageReadSchema.model_validate(assistant)
                user_message = await session.get(
                    ProjectMessageRecord, delivery.session_message_id
                )
                if user_message is None:
                    raise RuntimeError("runtime_input_message_missing")
                content = user_message.content
                session_id = delivery.session_id

            async with self._lock:
                session_lock = self._session_locks.setdefault(session_id, asyncio.Lock())
            async with session_lock:
                return await self._run_provider_and_persist(
                    message_id,
                    content,
                    provider,
                    trace_id=trace_id,
                )

    async def _run_provider_and_persist(
        self,
        message_id: str,
        content: str,
        provider: Provider,
        *,
        trace_id: str | None = None,
    ) -> ProjectMessageReadSchema:
        if self._sessions is None:
            raise RuntimeError("runtime_sessions_required")
        async with self._sessions() as session:
            delivery = await self._delivery(session, message_id)
            result = await self._result(session, message_id)
            if result is not None:
                assistant = await session.get(ProjectMessageRecord, result.assistant_message_id)
                if assistant is not None:
                    return ProjectMessageReadSchema.model_validate(assistant)
        try:
            reply_content = await provider(content)
        except Exception as exc:
            self._logging.error_event(
                "runtime_parent.input_failed",
                "failed",
                trace_id=trace_id or self._trace_id,
                message_id=message_id,
                project_id=delivery.project_id,
                agent_id=delivery.target_agent_id,
                generation=delivery.target_generation,
                session_id=delivery.session_id,
                error_code=type(exc).__name__,
                detail={"attempt": delivery.attempt},
            )
            raise

        async with self._sessions() as session:
            delivery = await self._delivery(session, message_id)
            result = await self._result(session, message_id)
            if result is not None:
                assistant = await session.get(ProjectMessageRecord, result.assistant_message_id)
                if assistant is not None:
                    return ProjectMessageReadSchema.model_validate(assistant)
            assistant = ProjectMessageRecord(
                session_id=delivery.session_id,
                role="assistant",
                content=reply_content,
            )
            session.add(assistant)
            await session.flush()
            session.add(
                RuntimeInputResultRecord(
                    message_id=message_id,
                    assistant_message_id=assistant.id,
                    status="handled",
                    handled_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            await session.commit()
            await session.refresh(assistant)
            self._logging.info_event(
                "runtime_parent.input_handled",
                "completed",
                trace_id=trace_id or self._trace_id,
                message_id=message_id,
                project_id=delivery.project_id,
                agent_id=delivery.target_agent_id,
                generation=delivery.target_generation,
                session_id=delivery.session_id,
                detail={"attempt": delivery.attempt},
            )
            return ProjectMessageReadSchema.model_validate(assistant)

    async def deliver_child_result(
        self,
        *,
        message_id: str,
        source: AgentAddress,
        target: AgentAddress,
        payload: dict,
        destroy: DestroyCallback,
        apply_result: ChildResultApplier | None = None,
        mailbox: PersistentMailbox | None = None,
        trace_id: str | None = None,
    ) -> bool:
        """Publish, apply, and acknowledge a stable result before child destruction."""
        if message_id in self._completed_child_results or await self._child_result_completed(
            message_id
        ):
            return True
        if mailbox is None and self._mailbox_for_project is None:
            raise RuntimeError("runtime_mailbox_required")
        target_mailbox = (
            mailbox
            if mailbox is not None
            else self._mailbox_for_project(target.project_id)  # type: ignore[misc]
        )
        result = target_mailbox.enqueue(
            MailEnvelope(
                message_id=message_id,
                message_type="child-result",
                source=source,
                target=target,
                payload=payload,
            )
        )
        if result.status not in {"inserted", "existing"}:
            self._logging.warn_event(
                "runtime_child.result_retry",
                "retry",
                trace_id=trace_id or self._trace_id,
                message_id=message_id,
                project_id=target.project_id,
                agent_id=source.agent_id,
                generation=source.generation,
                error_code=f"mail_{result.status}",
                detail={"attempt": max(1, result.attempt)},
            )
            return False
        if apply_result is not None:
            claimed = target_mailbox.claim(target)
            while claimed.status == "claimed" and claimed.message_id != message_id:
                if claimed.lease_token is None or claimed.message_id is None:
                    return False
                target_mailbox.nack(
                    claimed.message_id,
                    claimed.lease_token,
                    target=target,
                )
                claimed = target_mailbox.claim(target)
            if (
                claimed.status != "claimed"
                or claimed.message_id != message_id
                or claimed.lease_token is None
            ):
                return False
            try:
                apply_result(message_id, payload)
            except BaseException:
                target_mailbox.nack(message_id, claimed.lease_token, target=target)
                raise
            acknowledged = target_mailbox.ack(
                message_id,
                claimed.lease_token,
                target=target,
            )
            if acknowledged.status != "acked":
                return False
        await destroy()
        await self._record_child_result(message_id, source)
        self._completed_child_results.add(message_id)
        self._logging.info_event(
            "runtime_child.result_delivered",
            "completed",
            trace_id=trace_id or self._trace_id,
            message_id=message_id,
            project_id=target.project_id,
            agent_id=source.agent_id,
            generation=source.generation,
            detail={"attempt": max(1, result.attempt)},
        )
        return True

    async def _child_result_completed(self, message_id: str) -> bool:
        if self._sessions is None:
            return False
        async with self._sessions() as session:
            from sqlalchemy import select

            receipt = (
                await session.execute(
                    select(RuntimeChildResultReceiptRecord).where(
                        RuntimeChildResultReceiptRecord.message_id == message_id
                    )
                )
            ).scalar_one_or_none()
            return receipt is not None

    async def _record_child_result(self, message_id: str, source: AgentAddress) -> None:
        if self._sessions is None:
            return
        async with self._sessions() as session:
            session.add(
                RuntimeChildResultReceiptRecord(
                    message_id=message_id,
                    project_id=source.project_id,
                    agent_id=source.agent_id,
                    generation=source.generation,
                    delivered_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()

    @staticmethod
    async def _delivery(
        session: AsyncSession,
        message_id: str,
    ) -> RuntimeInputDeliveryRecord:
        from sqlalchemy import select

        delivery = (
            await session.execute(
                select(RuntimeInputDeliveryRecord).where(
                    RuntimeInputDeliveryRecord.message_id == message_id
                )
            )
        ).scalar_one_or_none()
        if delivery is None:
            raise RuntimeError("runtime_input_delivery_missing")
        return delivery

    @staticmethod
    async def _result(
        session: AsyncSession,
        message_id: str,
    ) -> RuntimeInputResultRecord | None:
        from sqlalchemy import select

        return (
            await session.execute(
                select(RuntimeInputResultRecord).where(
                    RuntimeInputResultRecord.message_id == message_id
                )
            )
        ).scalar_one_or_none()
