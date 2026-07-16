"""Crash-safe relay from application runtime-input facts to project Mail."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridle.agent.runtime.mailbox import AgentAddress, MailEnvelope
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.logging.facade import LoggingFacade, get_logging_facade
from bridle.models.agent_runtime import RuntimeInputDeliveryRecord
from bridle.models.project_message import ProjectMessageRecord

AfterEnqueueHook = Callable[[RuntimeInputDeliveryRecord], None]
MailboxFactory = Callable[[str], PersistentMailbox]


class RuntimeInputRelay:
    """Relay durable application facts to Mail without merging both stores."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        mailbox_for_project: MailboxFactory,
        facade: LoggingFacade | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._mailbox_for_project = mailbox_for_project
        self._logging = facade or get_logging_facade()
        self._trace_id = trace_id

    async def relay_pending(
        self,
        *,
        project_id: str | None = None,
        after_enqueue: AfterEnqueueHook | None = None,
    ) -> int:
        """Relay pending facts in creation order and return the delivered count."""
        async with self._sessions() as session:
            statement = (
                select(RuntimeInputDeliveryRecord)
                .where(RuntimeInputDeliveryRecord.status == "pending")
                .order_by(
                    RuntimeInputDeliveryRecord.created_at,
                    RuntimeInputDeliveryRecord.id,
                )
            )
            if project_id is not None:
                statement = statement.where(
                    RuntimeInputDeliveryRecord.project_id == project_id
                )
            delivery_ids = list((await session.execute(statement)).scalars().all())

        delivered = 0
        for pending in delivery_ids:
            if await self._relay_one(pending.id, after_enqueue=after_enqueue):
                delivered += 1
        return delivered

    async def _relay_one(
        self,
        delivery_id: str,
        *,
        after_enqueue: AfterEnqueueHook | None,
    ) -> bool:
        async with self._sessions() as session:
            delivery = await session.get(RuntimeInputDeliveryRecord, delivery_id)
            if delivery is None or delivery.status != "pending":
                return False
            message = await session.get(ProjectMessageRecord, delivery.session_message_id)
            if message is None:
                self._log("runtime_input.relay_retry", delivery, error_code="message_missing")
                return False
            delivery.attempt += 1
            await session.commit()
            await session.refresh(delivery)

            target = AgentAddress(
                delivery.project_id,
                delivery.target_agent_id,
                delivery.target_generation,
            )
            envelope = MailEnvelope(
                message_id=delivery.message_id,
                message_type="runtime-input",
                source=AgentAddress(delivery.project_id, "session-gateway", 1),
                target=target,
                payload={
                    "session_id": delivery.session_id,
                    "session_message_id": delivery.session_message_id,
                    "role": message.role,
                    "content": message.content,
                },
                attempt=delivery.attempt,
            )
            try:
                result = self._mailbox_for_project(delivery.project_id).enqueue(envelope)
            except Exception as exc:
                self._log(
                    "runtime_input.relay_retry",
                    delivery,
                    error_code=type(exc).__name__,
                )
                return False
            if result.status not in {"inserted", "existing"}:
                self._log(
                    "runtime_input.relay_retry",
                    delivery,
                    error_code=f"mail_{result.status}",
                )
                return False
            if after_enqueue is not None:
                after_enqueue(delivery)

            delivery.status = "delivered"
            delivery.mail_enqueued_at = datetime.now(UTC).replace(tzinfo=None)
            await session.commit()
            self._log("runtime_input.delivered", delivery)
            return True

    def _log(
        self,
        action: str,
        delivery: RuntimeInputDeliveryRecord,
        *,
        error_code: str | None = None,
    ) -> None:
        detail = {"attempt": delivery.attempt}
        if error_code is not None:
            detail["error_code"] = error_code
        self._logging.info_event(
            action,
            "retry" if error_code else "completed",
            trace_id=self._trace_id,
            message_id=delivery.message_id,
            project_id=delivery.project_id,
            agent_id=delivery.target_agent_id,
            generation=delivery.target_generation,
            session_id=delivery.session_id,
            detail=detail,
        )
