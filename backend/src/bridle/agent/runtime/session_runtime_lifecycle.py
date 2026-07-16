"""Application/session lifecycle bridge for durable Agent runtimes."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridle.agent.runtime.agent_runtime import RuntimeHandle
from bridle.agent.runtime.host import AgentRuntimeHost
from bridle.agent.runtime.input_relay import RuntimeInputRelay
from bridle.logging.facade import LoggingFacade, get_logging_facade
from bridle.models.agent_runtime import AgentRuntimeRecord
from bridle.models.project_session import ProjectSessionRecord

_LEGACY_ACTIVE_STATES = ("CREATING", "READY", "RUNNING", "STOPPING")


class RuntimeSessionLifecycle:
    """Recover durable runtime facts before requests and own explicit session closure."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        relay: RuntimeInputRelay | None = None,
        host: AgentRuntimeHost | None = None,
        facade: LoggingFacade | None = None,
        trace_id: str | None = None,
        retry_interval_seconds: float = 1.0,
    ) -> None:
        self._sessions = sessions
        self._relay = relay
        self._host = host
        self._logging = facade or get_logging_facade()
        self._trace_id = trace_id
        if retry_interval_seconds <= 0:
            raise ValueError("retry_interval_seconds_must_be_positive")
        self._retry_interval_seconds = retry_interval_seconds

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession]:
        """Expose the application session factory to the lifespan coordinator."""
        return self._sessions

    async def recover_before_requests(self) -> int:
        """Interrupt false active rows, then relay pending inputs before admission."""
        self._logging.info_event(
            "app.runtime_recovery_started",
            "started",
            detail={"attempt": 1},
        )
        async with self._sessions() as session:
            result = await session.execute(
                update(AgentRuntimeRecord)
                .where(AgentRuntimeRecord.status.in_(_LEGACY_ACTIVE_STATES))
                .values(
                    status="INTERRUPTED",
                    status_reason="process_restart",
                    updated_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            interrupted = max(0, int(result.rowcount or 0))
            await session.commit()
        relayed = 0 if self._relay is None else await self._relay.relay_pending()
        self._logging.info_event(
            "app.runtime_recovery_completed",
            "completed",
            detail={"interrupted": interrupted, "relayed": relayed, "attempt": 1},
        )
        return interrupted + relayed

    async def run_relay_retry(self, stop: asyncio.Event) -> None:
        """Retry pending inputs at a bounded rate until application shutdown."""
        if self._relay is None:
            return
        while not stop.is_set():
            await self._relay.relay_pending()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._retry_interval_seconds)
            except TimeoutError:
                continue

    async def close_session(self, session_id: str) -> None:
        """Stop live runtime ownership and mark the session closed without deleting history."""
        if self._host is not None:
            handles = tuple(
                handle
                for handle in self._host.active_handles()
                if handle.spec.session_id == session_id
            )
            for handle in handles:
                await self._host.destroy(handle)
        async with self._sessions() as session:
            record = await session.get(ProjectSessionRecord, session_id)
            if record is None:
                raise RuntimeError("project_session_not_found")
            record.status = "closed"
            await session.commit()
            self._logging.info_event(
                "runtime_session.closed",
                "completed",
                project_id=record.project_id,
                session_id=session_id,
                detail={"attempt": 1},
            )

    async def revoke_parent(self, parent: RuntimeHandle) -> RuntimeHandle:
        """Delegate revocation to Host so active children are destroyed with the parent."""
        if self._host is None:
            raise RuntimeError("runtime_host_required")
        children_revoked = len(parent.children)
        result = await self._host.revoke(parent)
        self._logging.info_event(
            "runtime_parent.revoked",
            "completed",
            trace_id=self._trace_id,
            project_id=parent.spec.project_id,
            session_id=parent.spec.session_id,
            agent_id=parent.spec.agent_id,
            generation=parent.spec.generation,
            detail={"attempt": 1, "children_revoked": children_revoked},
        )
        return result
