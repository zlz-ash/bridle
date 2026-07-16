"""In-process host for parent, child, and project-map runtime generations."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bridle.agent.runtime import persistence
from bridle.agent.runtime.agent_runtime import (
    ALLOWED_TRANSITIONS,
    CapabilityView,
    RuntimeError,
    RuntimeHandle,
    RuntimeRole,
    RuntimeSpec,
    RuntimeState,
)
from bridle.agent.runtime.authorization import AgentGrant
from bridle.agent.runtime.persistent_mailbox import PersistentMailbox
from bridle.agent.skills.registry import SkillRegistry
from bridle.agent.tools.registry import AgentToolRegistry
from bridle.logging.facade import LoggingFacade, get_logging_facade


class AgentRuntimeHost:
    """Own live handles while persisting every lifecycle transition."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        facade: LoggingFacade | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._sessions = sessions
        self._logging = facade or get_logging_facade()
        self._trace_id = trace_id or f"runtime-{secrets.token_hex(8)}"
        self._handles: dict[str, RuntimeHandle] = {}
        self._singletons: dict[tuple[str, str, str | None], RuntimeHandle] = {}
        self._create_lock = asyncio.Lock()

    async def create_runtime(
        self,
        *,
        role: RuntimeRole,
        project_id: str,
        agent_id: str,
        generation: int,
        grant: AgentGrant,
        session_id: str | None = None,
        parent: RuntimeHandle | None = None,
        tools: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None,
        skills: Mapping[str, Mapping[str, Any]] | None = None,
        tool_registry: AgentToolRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        task_factory: Callable[[RuntimeHandle], Coroutine[Any, Any, Any]] | None = None,
        mailbox: PersistentMailbox | None = None,
    ) -> RuntimeHandle:
        if role is RuntimeRole.CHILD and parent is None:
            raise RuntimeError("runtime_parent_required")
        if parent is not None:
            self._require_active_parent(parent)
        key = self._singleton_key(role, project_id, session_id)
        async with self._create_lock:
            if parent is not None:
                self._require_active_parent(parent)
            if key is not None:
                existing = self._singletons.get(key)
                if existing is not None and existing.state is not RuntimeState.DESTROYED:
                    if (
                        existing.spec.agent_id == agent_id
                        and existing.spec.generation == generation
                    ):
                        return existing
                    raise RuntimeError("runtime_conflict")

            capabilities = CapabilityView(
                grant=grant,
                tools=tools,
                skills=skills,
                tool_registry=tool_registry,
                skill_registry=skill_registry,
                parent=parent.capabilities if parent is not None else None,
                unknown_logger=lambda kind, capability_id: self._log_unknown(
                    handle,
                    kind,
                    capability_id,
                ),
            )
            record = await self._create_record(
                role=role,
                project_id=project_id,
                session_id=session_id,
                parent=parent,
                agent_id=agent_id,
                generation=generation,
            )
            spec = RuntimeSpec(
                runtime_id=record.id,
                project_id=project_id,
                agent_id=agent_id,
                generation=generation,
                role=role,
                session_id=session_id,
                parent_runtime_id=parent.spec.runtime_id if parent is not None else None,
            )
            handle = RuntimeHandle(
                spec=spec,
                state=RuntimeState.CREATING,
                status_reason="create_started",
                grant=grant,
                capabilities=capabilities,
            )
            self._handles[spec.runtime_id] = handle
            if key is not None:
                self._singletons[key] = handle
            if parent is not None:
                parent.children.add(spec.runtime_id)
            if mailbox is not None:
                handle.add_resource(mailbox.close)
            creation_error: BaseException | None = None
            creation_cancelled = False
            try:
                if task_factory is not None:
                    handle.task = asyncio.create_task(task_factory(handle))
                await self.transition(handle, RuntimeState.READY, reason="created")
            except BaseException as exc:
                creation_error = exc
                creation_cancelled = isinstance(exc, asyncio.CancelledError)
                target = (
                    RuntimeState.CANCELLED if creation_cancelled else RuntimeState.FAILED
                )
                try:
                    await self.transition(
                        handle,
                        target,
                        reason="create_cancelled" if creation_cancelled else "create_failed",
                    )
                except BaseException as persist_exc:
                    self._log(
                        "runtime.state_persist_failed",
                        "failed",
                        handle,
                        error_code=type(persist_exc).__name__,
                    )
                self._remove_handle(handle)
        if creation_error is not None:
            await self._cleanup_failed_creation(
                handle,
                cancelled=creation_cancelled,
            )
            raise creation_error.with_traceback(creation_error.__traceback__)
        async with self._create_lock:
            self._log("runtime.created", "completed", handle)
            self._log("runtime.capability_view_created", "completed", handle)
            return handle

    async def _cleanup_failed_creation(
        self,
        handle: RuntimeHandle,
        *,
        cancelled: bool,
    ) -> None:
        await self._cancel_task(handle)
        await handle.close_resources(
            lambda exc: self._log(
                "runtime.resource_close_failed",
                "failed",
                handle,
                error_code=type(exc).__name__,
            )
        )
        if cancelled and handle.state is RuntimeState.CANCELLED:
            try:
                await self.transition(handle, RuntimeState.DESTROYED, reason="destroyed")
            except BaseException as exc:
                self._log(
                    "runtime.state_persist_failed",
                    "failed",
                    handle,
                    error_code=type(exc).__name__,
                )

    async def _create_record(
        self,
        *,
        role: RuntimeRole,
        project_id: str,
        session_id: str | None,
        parent: RuntimeHandle | None,
        agent_id: str,
        generation: int,
    ):
        async with self._sessions() as session:
            try:
                record = await persistence.add_runtime_record(
                    session,
                    runtime_type=role.value,
                    owner_type="session" if role is RuntimeRole.PARENT else "project",
                    owner_id=session_id or project_id,
                    project_id=project_id,
                    session_id=session_id,
                    parent_agent_id=parent.spec.agent_id if parent is not None else None,
                    parent_runtime_id=parent.spec.runtime_id if parent is not None else None,
                    agent_id=agent_id,
                    generation=generation,
                    status=RuntimeState.CREATING,
                    status_reason="create_started",
                    facade=self._logging,
                    trace_id=self._trace_id,
                )
                await session.commit()
                return record
            except BaseException:
                await session.rollback()
                raise

    async def transition(
        self,
        handle: RuntimeHandle,
        target: RuntimeState,
        *,
        reason: str,
    ) -> RuntimeHandle:
        async with handle._transition_lock:
            source = handle.state
            if target not in ALLOWED_TRANSITIONS[source]:
                raise RuntimeError("runtime_invalid_transition")
            async with self._sessions() as session:
                try:
                    await persistence.update_runtime_state(
                        session,
                        handle.spec.runtime_id,
                        status=target,
                        status_reason=reason,
                        facade=self._logging,
                        trace_id=self._trace_id,
                    )
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise
            handle.state = target
            handle.status_reason = reason
            self._log(
                "runtime.state_changed",
                "completed",
                handle,
                from_state=source,
                to_state=target,
                reason=reason,
            )
            return handle

    async def stop(self, handle: RuntimeHandle) -> RuntimeHandle:
        async with self._create_lock, handle._stop_lock:
            if handle.state in {
                RuntimeState.COMPLETED,
                RuntimeState.FAILED,
                RuntimeState.CANCELLED,
                RuntimeState.INTERRUPTED,
                RuntimeState.DESTROYED,
            }:
                return handle
            if self._finalizer_needs_retry(handle._stop_task):
                handle._stop_task = None
            if handle._stop_task is None:
                if handle.state is not RuntimeState.STOPPING:
                    await self.transition(
                        handle,
                        RuntimeState.STOPPING,
                        reason="stop_requested",
                    )
                handle._stop_task = asyncio.create_task(self._finish_stop(handle))
                handle._stop_task.add_done_callback(
                    lambda task: self._observe_finalizer(handle, "stop", task)
                )
            stop_task = handle._stop_task
        if asyncio.current_task() is handle.task:
            return handle
        return await asyncio.shield(stop_task)

    async def _finish_stop(self, handle: RuntimeHandle) -> RuntimeHandle:
        for child in self._children(handle):
            await self.stop(child)
        await self._cancel_task(handle)
        await handle.close_resources(
            lambda exc: self._log(
                "runtime.resource_close_failed",
                "failed",
                handle,
                error_code=type(exc).__name__,
            )
        )
        if handle.state is RuntimeState.STOPPING:
            await self.transition(handle, RuntimeState.COMPLETED, reason="stopped")
        self._log("runtime.stopped", "completed", handle)
        return handle

    async def destroy(self, handle: RuntimeHandle) -> RuntimeHandle:
        async with handle._stop_lock:
            if self._finalizer_needs_retry(handle._destroy_task):
                handle._destroy_task = None
            if handle._destroy_task is None:
                handle._destroy_task = asyncio.create_task(self._finish_destroy(handle))
                handle._destroy_task.add_done_callback(
                    lambda task: self._observe_finalizer(handle, "destroy", task)
                )
            destroy_task = handle._destroy_task
        if asyncio.current_task() is handle.task:
            return handle
        return await asyncio.shield(destroy_task)

    async def _finish_destroy(self, handle: RuntimeHandle) -> RuntimeHandle:
        await self.stop(handle)
        for child in self._children(handle):
            await self.destroy(child)
        async with handle._stop_lock:
            if handle.state is not RuntimeState.DESTROYED:
                await self.transition(handle, RuntimeState.DESTROYED, reason="destroyed")
        async with self._create_lock:
            self._remove_handle(handle)
        self._log("runtime.destroyed", "completed", handle)
        return handle

    @staticmethod
    def _finalizer_needs_retry(task: asyncio.Task[RuntimeHandle] | None) -> bool:
        if task is None or not task.done():
            return False
        return task.cancelled() or task.exception() is not None

    def _observe_finalizer(
        self,
        handle: RuntimeHandle,
        finalizer: str,
        task: asyncio.Task[RuntimeHandle],
    ) -> None:
        if task.cancelled():
            error_code = "CancelledError"
        else:
            exception = task.exception()
            if exception is None:
                return
            error_code = type(exception).__name__
        self._log(
            "runtime.finalizer_failed",
            "failed",
            handle,
            finalizer=finalizer,
            error_code=error_code,
        )

    @staticmethod
    async def _cancel_task(handle: RuntimeHandle) -> None:
        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
            await asyncio.gather(handle.task, return_exceptions=True)

    def active_handles(self) -> tuple[RuntimeHandle, ...]:
        return tuple(self._handles.values())

    @staticmethod
    def _require_active_parent(parent: RuntimeHandle) -> None:
        if parent.state not in {RuntimeState.READY, RuntimeState.RUNNING}:
            raise RuntimeError("runtime_parent_inactive")

    async def revoke(self, handle: RuntimeHandle) -> RuntimeHandle:
        handle.grant.revocation.revoke()
        self._log("runtime.revoked", "completed", handle)
        return await self.destroy(handle)

    def _children(self, handle: RuntimeHandle) -> tuple[RuntimeHandle, ...]:
        return tuple(
            child
            for runtime_id in tuple(handle.children)
            if (child := self._handles.get(runtime_id)) is not None
        )

    def _remove_handle(self, handle: RuntimeHandle) -> None:
        self._handles.pop(handle.spec.runtime_id, None)
        key = self._singleton_key(
            handle.spec.role,
            handle.spec.project_id,
            handle.spec.session_id,
        )
        if key is not None and self._singletons.get(key) is handle:
            self._singletons.pop(key, None)

    @staticmethod
    def _singleton_key(
        role: RuntimeRole,
        project_id: str,
        session_id: str | None,
    ) -> tuple[str, str, str | None] | None:
        if role is RuntimeRole.PARENT:
            if session_id is None:
                raise RuntimeError("runtime_session_required")
            return (role.value, project_id, session_id)
        if role is RuntimeRole.MAP:
            return (role.value, project_id, None)
        return None

    def _log_unknown(
        self,
        handle: RuntimeHandle,
        kind: str,
        capability_id: str,
    ) -> None:
        self._log(
            "runtime.unknown_capability",
            "failed",
            handle,
            error_code="unknown_capability",
            capability_kind=kind,
        )

    def _log(
        self,
        action: str,
        status: str,
        handle: RuntimeHandle,
        **detail: Any,
    ) -> None:
        started = time.perf_counter()
        error_code = detail.pop("error_code", None)
        payload = {
            "runtime_id": handle.spec.runtime_id,
            "role": handle.spec.role.value,
            "parent_runtime_id": handle.spec.parent_runtime_id,
            **detail,
        }
        self._logging.info_event(
            action,
            status,
            trace_id=self._trace_id,
            project_id=handle.spec.project_id,
            session_id=handle.spec.session_id,
            agent_id=handle.spec.agent_id,
            generation=handle.spec.generation,
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            error_code=error_code,
            detail=payload,
        )
